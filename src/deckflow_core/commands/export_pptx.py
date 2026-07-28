"""`deckflow export pptx` — deck project to editable PPTX via the DeckHTML provider.

Core owns four things the converter cannot know:

1. **which pages, in what order** — the approved order in `deck-plan.json`,
   not whatever `glob` returns;
2. **whether this deck is exportable at all** — DeckHTML locks its viewport to
   16:9, and four of the five Deckflow stage sizes are not;
3. **output safety** — convert into a scratch directory, verify, and only then
   move into place, so a failed run never leaves a plausible-looking deck;
4. **independent verification** — reopen the OOXML package and check it against
   the project rather than trusting the converter's own summary.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .. import ooxml, project
from ..diagnostics import Diagnostic, summarize_output
from ..envelope import STATUS_FAILED, STATUS_SUCCEEDED, Envelope, file_record
from ..exits import EXIT_CONTRACT, EXIT_EXECUTION, EXIT_OK, EXIT_OUTPUT, CoreError
from ..fsutil import deckflow_home, require_writable_target, sha256_file
from ..providers import matrix
from ..providers import resolve as resolver

COMMAND = "export.pptx"
_IDENTITY_ATTRIBUTE = "data-element-id"


def _require_16_9(deck: project.DeckProject) -> None:
    """Refuse a stage the converter cannot represent.

    DeckHTML derives its viewport height from the width at a fixed 16:9
    (`resolveViewport`), so a portrait or 4:3 deck would be laid out at the
    wrong shape and still produce a file that opens cleanly. Failing here is
    the only way the caller finds out.
    """
    if deck.deck_size.is_16_9:
        return
    raise CoreError(
        Diagnostic(
            rule_id="PPTX_EXPORT_STAGE_UNSUPPORTED",
            severity="error",
            message=f"The DeckHTML provider cannot export a {deck.deck_size.id} deck.",
            location=str(deck.root / "intent-detail.json"),
            expected="a 16:9 stage (landscape-16-9)",
            actual=f"{deck.deck_size.id} ({deck.deck_size.width}x{deck.deck_size.height})",
            recovery=(
                "Deliver this deck as HTML, or re-author it at landscape-16-9. "
                "Exporting it anyway would silently produce a 16:9 file with the wrong layout."
            ),
        ),
        exit_code=EXIT_CONTRACT,
    )


def _convert(resolution: resolver.Resolution, deck: project.DeckProject,
             scratch: Path, options: Any) -> tuple[Path, dict[str, Any] | None, list[str]]:
    """Run the provider into a scratch directory and return what it produced."""
    target = scratch / "deck.pptx"
    command = [
        *resolution.command,
        *(str(slide.path) for slide in deck.slides),
        "--output", str(target),
        # Never let a key in the environment turn this into an upload.
        "--mode", "local",
        "--width", str(deck.deck_size.width),
        "--identity-attribute", _IDENTITY_ATTRIBUTE,
        "--json", "--report",
    ]
    for selector in options.exclude or ():
        command += ["--exclude", selector]
    if options.browser:
        command += ["--executable-path", str(Path(options.browser).expanduser().resolve())]

    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=options.timeout,
            env={**resolution.env, **_clean_env()},
        )
    except subprocess.TimeoutExpired as error:
        raise CoreError(
            Diagnostic(
                rule_id="PPTX_EXPORT_TIMEOUT",
                severity="error",
                message=f"Conversion timed out after {options.timeout}s.",
                location=str(deck.root),
                expected=f"conversion of {len(deck.slides)} slides",
                actual="the converter did not finish",
                recovery="Re-run with a longer --timeout, or reduce per-page complexity.",
            ),
            exit_code=EXIT_EXECUTION,
        ) from error

    notes: list[str] = []
    if completed.returncode != 0 or not target.is_file():
        raise CoreError(
            Diagnostic(
                rule_id="PPTX_EXPORT_CONVERSION_FAILED",
                severity="error",
                message="The DeckHTML provider did not produce a PPTX.",
                location=str(deck.root),
                expected="exit code 0 and a written .pptx",
                actual=f"exit code {completed.returncode}: "
                       f"{summarize_output(completed.stderr, completed.stdout)}",
                recovery="Check the page HTML for unsupported constructs, then re-run.",
            ),
            exit_code=EXIT_EXECUTION,
        )

    provider_result = _parse_json_line(completed.stdout)
    report_path = scratch / "deck.pptx.report.json"
    if report_path.is_file():
        try:
            conversion_report = json.loads(report_path.read_text(encoding="utf-8"))
            if isinstance(provider_result, dict):
                provider_result["conversion_report"] = conversion_report
        except ValueError:
            notes.append("the provider's conversion report was not valid JSON")
    return target, provider_result, notes


def _clean_env() -> dict[str, str]:
    """Environment for the converter, with cloud credentials withheld.

    The presence of an API key is not authorization to upload. `--mode local`
    already forbids it; dropping the key means a future provider change cannot
    quietly turn a local export into a network one.
    """
    import os

    withheld = {"DECKHTML_API_KEY", "DECKFLOW_API_KEY"}
    return {key: value for key, value in os.environ.items() if key not in withheld}


def _parse_json_line(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.strip().splitlines()):
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _verify(produced: Path, deck: project.DeckProject) -> tuple[ooxml.PptxReport, list[Diagnostic]]:
    """Reopen the package and check it against the project."""
    report = ooxml.inspect(produced)
    findings: list[Diagnostic] = []

    if not report.readable:
        findings.append(
            Diagnostic(
                rule_id="PPTX_OUTPUT_UNREADABLE",
                severity="error",
                message="The produced PPTX could not be reopened as an OOXML package.",
                location=str(produced),
                expected="a readable PresentationML package",
                actual=report.error or "unknown",
                recovery="Re-run the export; if it persists, report the failing deck upstream.",
            )
        )
        return report, findings

    if report.slide_count != len(deck.slides):
        findings.append(
            Diagnostic(
                rule_id="PPTX_SLIDE_COUNT_MISMATCH",
                severity="error",
                message="The PPTX slide count does not match the approved plan.",
                location=str(produced),
                expected=f"{len(deck.slides)} slides",
                actual=f"{report.slide_count} slides",
                recovery="Do not deliver this file. Re-run the export and check for pages the converter skipped.",
            )
        )

    if report.remote_relationships:
        findings.append(
            Diagnostic(
                rule_id="PPTX_REMOTE_RELATIONSHIP",
                severity="error",
                message="The PPTX references remote resources.",
                location=str(produced),
                expected="no http/https relationships",
                actual="; ".join(report.remote_relationships[:3]),
                recovery="Replace remote fonts/images in the page HTML with local assets, then re-run.",
            )
        )

    return report, findings


def run(options: Any) -> tuple[Envelope, str | None, int]:
    envelope = Envelope(command=COMMAND)
    deck = project.load(Path(options.project))
    _require_16_9(deck)

    output = Path(options.output).expanduser().resolve()
    if output.suffix.lower() != ".pptx":
        raise CoreError(
            Diagnostic(
                rule_id="OUTPUT_EXTENSION_UNEXPECTED",
                severity="error",
                message="The export target must be a .pptx path.",
                location=str(output),
                expected="a path ending in .pptx",
                actual=output.suffix or "no extension",
                recovery="Pass --output <name>.pptx.",
            ),
            exit_code=EXIT_OUTPUT,
        )
    require_writable_target(output, overwrite=options.overwrite, kind="file")
    output.parent.mkdir(parents=True, exist_ok=True)

    spec = matrix.get("deckhtml", options.provider_specs)
    resolution = resolver.resolve(
        spec,
        policy=resolver.policy_from(options.provider_install),
        bin_overrides=options.provider_bins,
        home=deckflow_home(),
    )
    envelope.providers = [resolution.to_json()]
    envelope.extend(resolution.diagnostics)

    envelope.inputs = [
        file_record(str(slide.path), sha256=sha256_file(slide.path)) for slide in deck.slides
    ]
    before = project.protected_snapshot(deck.root)

    # Convert into a scratch directory beside the target: a failed or unverified
    # run must never leave something that looks like a finished deck.
    scratch = Path(tempfile.mkdtemp(dir=str(output.parent), prefix=".deckflow-export-"))
    try:
        produced, provider_result, notes = _convert(resolution, deck, scratch, options)
        report, findings = _verify(produced, deck)
        envelope.extend(findings)
        for note in notes:
            envelope.add(
                Diagnostic(
                    rule_id="PPTX_PROVIDER_REPORT_UNREADABLE", severity="warning",
                    message=note.capitalize() + ".", location=str(scratch),
                    recovery="The PPTX itself was still verified independently by core.",
                )
            )

        if any(item.severity == "error" for item in findings):
            envelope.status = STATUS_FAILED
            envelope.provider_result = provider_result
            envelope.extra["verification"] = report.to_json()
            return envelope, None, EXIT_CONTRACT

        produced.replace(output)
        envelope.status = STATUS_SUCCEEDED
        envelope.provider_result = provider_result
        envelope.outputs = [
            file_record(str(output), sha256=sha256_file(output), size=output.stat().st_size)
        ]
        envelope.extra["verification"] = report.to_json()
        envelope.extra["slides"] = [
            {"slide_id": slide.slide_id, "order": index + 1, "source": str(slide.path)}
            for index, slide in enumerate(deck.slides)
        ]
        envelope.extra["deck_size"] = {
            "id": deck.deck_size.id,
            "width": deck.deck_size.width,
            "height": deck.deck_size.height,
        }
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    after = project.protected_snapshot(deck.root)
    if after != before:
        changed = sorted(key for key in before if before[key] != after.get(key))
        envelope.add(
            Diagnostic(
                rule_id="PROJECT_PROTECTED_FILE_CHANGED",
                severity="error",
                message="Export modified project records it must never write.",
                location=str(deck.root),
                expected="index.html, deck-head.html and build-manifest.json unchanged",
                actual=f"changed: {', '.join(changed)}",
                recovery="Restore these records from the Skill's own writers and report this upstream.",
            )
        )
        envelope.status = STATUS_FAILED
        return envelope, None, EXIT_CONTRACT

    human = None if options.json else (
        f"exported {len(deck.slides)} slides -> {output}\n"
        f"verified: {report.slide_count} slides in the reopened package, no remote relationships"
    )
    return envelope, human, EXIT_OK
