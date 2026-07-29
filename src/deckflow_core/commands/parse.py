"""`deckflow parse` — one local file into a Parse Bundle via the extract provider.

Deliberately thin. `deckflow-extract` already has a well-shaped contract — a
real status machine, a fidelity vector, gap accounting and executable
recommendations — so core adds boundaries and gets out of the way:

- the Parse Bundle is passed through untouched. Core does not rewrite
  `document.md`, recompute fidelity, or invent a second artifact vocabulary;
- `recommendations[]` reaches the caller verbatim. Choosing among them is the
  Skill's decision and the user's, never core's;
- the engine ladder stays the provider's business. Core manages the *provider*;
  the provider manages its own optional engines.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from ..diagnostics import Diagnostic, summarize_output
from ..envelope import (
    EXTRACT_STATUS_MAP,
    STATUS_FAILED,
    Envelope,
    file_record,
)
from ..exits import EXIT_EXECUTION, EXIT_INPUT, EXIT_OK, EXIT_OUTPUT, CoreError
from ..extract import resolve as extract_resolve
from ..fsutil import require_empty_dir, resolve_within, sha256_file
from ..home import credential_env, deckflow_home

COMMAND = "parse"
_URL_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.IGNORECASE)
_MANIFEST = "parse-manifest.json"


def _resolve_input(raw: str) -> Path:
    """Accept exactly one existing local file.

    URLs are refused rather than forwarded. The provider can fetch them, but
    core promises that the content plane never reaches the network, and a
    promise with an exception in it is not worth stating. Anyone who wants a
    URL parsed can call the provider directly.
    """
    if _URL_RE.match(raw):
        raise CoreError(
            Diagnostic(
                rule_id="PARSE_INPUT_NOT_LOCAL",
                severity="error",
                message="`deckflow parse` accepts a local file, not a URL.",
                location=raw,
                expected="a path to an existing local file",
                actual="a URL",
                recovery=(
                    "Download the page first and parse the file, or call the provider "
                    "directly: deckflow-extract parse <url> --out <dir>."
                ),
            ),
            exit_code=EXIT_INPUT,
        )
    path = Path(raw).expanduser()
    if not path.exists():
        raise CoreError(
            Diagnostic(
                rule_id="PARSE_INPUT_MISSING",
                severity="error",
                message="The input file does not exist.",
                location=str(path),
                expected="an existing local file",
                actual="no such path",
                recovery="Check the path, then re-run.",
            ),
            exit_code=EXIT_INPUT,
        )
    if not path.is_file():
        raise CoreError(
            Diagnostic(
                rule_id="PARSE_INPUT_NOT_A_FILE",
                severity="error",
                message="The input must be a single file.",
                location=str(path),
                expected="one file",
                actual="a directory",
                recovery="Parse one file per run so failures stay isolated and retries stay cheap.",
            ),
            exit_code=EXIT_INPUT,
        )
    return path.resolve()


def _build_command(resolution: extract_resolve.Extract, source: Path,
                   out: Path, options: Any) -> list[str]:
    command = [
        *resolution.command, "parse", str(source),
        "--out", str(out),
        # Cloud parsing uploads the source. It happens only when the caller
        # asks for it; a key in the environment is not a request.
        "--mode", "cloud" if options.mode == "cloud" else "local",
        # Off for local inputs already, but stated explicitly so a future
        # change to the provider's default cannot quietly put the content
        # plane on the network.
        "--fetch-remote-images", "off",
        "--upgrade", options.upgrade or "never",
    ]
    if options.overwrite:
        command.append("--replace")
    for flag, value in (
        ("--type", options.type),
        ("--ocr", options.ocr),
        ("--max-pages", options.max_pages),
        ("--max-table-rows", options.max_table_rows),
        ("--timeout", options.provider_timeout),
    ):
        if value is not None:
            command += [flag, str(value)]
    return command


def _last_json_line(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.strip().splitlines()):
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _bundle_outputs(out: Path) -> list[dict[str, Any]]:
    """Record the bundle root and its manifest; leave the contents alone."""
    outputs: list[dict[str, Any]] = [{"path": str(out), "kind": "parse-bundle"}]
    manifest = out / _MANIFEST
    if manifest.is_file():
        outputs.append(
            file_record(str(manifest), sha256=sha256_file(manifest), size=manifest.stat().st_size)
        )
    return outputs


def _refuse_unsafe_overwrite(out: Path, actual: str) -> None:
    raise CoreError(
        Diagnostic(
            rule_id="PARSE_OVERWRITE_UNOWNED",
            severity="error",
            message="Refusing to replace a directory that is not a valid Parse Bundle.",
            location=str(out),
            expected=(
                "a deckflow-extract bundle containing a valid parse-manifest.json, "
                "document, and assets directory"
            ),
            actual=actual,
            recovery="Choose a new --out directory. Move unrelated files manually if they are no longer needed.",
        ),
        exit_code=EXIT_OUTPUT,
    )


def _require_safe_output(out: Path, *, overwrite: bool) -> None:
    """Allow replacement only for a complete deckflow-extract-owned bundle."""
    require_empty_dir(out, overwrite=overwrite)
    if not out.exists() or not any(out.iterdir()) or not overwrite:
        return

    manifest_path = out / _MANIFEST
    if manifest_path.is_symlink() or not manifest_path.is_file():
        _refuse_unsafe_overwrite(out, "missing a direct parse-manifest.json file")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        _refuse_unsafe_overwrite(out, f"unreadable parse-manifest.json: {error}")
    if not isinstance(manifest, dict):
        _refuse_unsafe_overwrite(out, "parse-manifest.json is not a JSON object")

    tool = manifest.get("tool")
    outputs = manifest.get("outputs")
    schema_version = manifest.get("schema_version")
    if (
        not isinstance(tool, dict)
        or tool.get("name") != "deckflow-extract"
        or not isinstance(schema_version, int)
        or schema_version < 1
        or not isinstance(outputs, dict)
    ):
        _refuse_unsafe_overwrite(out, "manifest ownership or schema fields are invalid")

    document_value = outputs.get("document")
    assets_value = outputs.get("assets_dir")
    if not isinstance(document_value, str) or not isinstance(assets_value, str):
        _refuse_unsafe_overwrite(out, "manifest output paths are missing or invalid")
    try:
        document = resolve_within(out, Path(document_value))
        assets = resolve_within(out, Path(assets_value))
    except CoreError:
        _refuse_unsafe_overwrite(out, "manifest output paths escape the bundle directory")
    if not document.is_file() or not assets.is_dir():
        _refuse_unsafe_overwrite(out, "manifest output files are incomplete")


def _require_input_outside_output(source: Path, out: Path) -> None:
    if source == out or out in source.parents:
        raise CoreError(
            Diagnostic(
                rule_id="PARSE_OUTPUT_CONTAINS_INPUT",
                severity="error",
                message="The Parse Bundle output directory contains the input file.",
                location=str(out),
                expected="an output directory separate from the input file",
                actual=str(source),
                recovery="Choose --out outside the directory tree containing the input.",
            ),
            exit_code=EXIT_OUTPUT,
        )


def _carry_diagnostics(envelope: Envelope, provider_result: dict[str, Any]) -> None:
    """Surface the provider's own findings without re-judging them."""
    for entry in provider_result.get("diagnostics") or ():
        if not isinstance(entry, dict):
            continue
        severity = entry.get("severity")
        envelope.add(
            Diagnostic(
                rule_id="PARSE_PROVIDER_DIAGNOSTIC",
                severity=severity if severity in ("error", "warning", "info") else "info",
                message=str(entry.get("message") or entry.get("code") or "provider diagnostic"),
                location=str(entry.get("code") or "") or None,
                recovery=str(entry.get("hint")) if entry.get("hint") else None,
            )
        )

    decision = provider_result.get("decision") or {}
    recommended = decision.get("recommended")
    if recommended and recommended != "accept":
        envelope.add(
            Diagnostic(
                rule_id="PARSE_UPGRADE_AVAILABLE",
                severity="info",
                message=f"The provider suggests '{recommended}' to improve this extraction.",
                location=str(provider_result.get("tier") or "") or None,
                expected="the highest fidelity this input can yield",
                actual=str(decision.get("reason") or "a lower-cost engine was used"),
                recovery=(
                    "Read provider_result.recommendations[] and present the high-priority "
                    "options alongside 'accept'. The choice is the user's, not core's."
                ),
            )
        )


def run(options: Any) -> tuple[Envelope, str | None, int]:
    envelope = Envelope(command=COMMAND)
    source = _resolve_input(options.input)
    out = Path(options.out).expanduser().resolve()
    _require_input_outside_output(source, out)
    _require_safe_output(out, overwrite=options.overwrite)

    resolution = extract_resolve.resolve(
        bin_override=options.extract_bin,
        offline=extract_resolve.offline_from(options.offline),
        home=deckflow_home(),
    )
    envelope.extract = resolution.to_json()
    envelope.extend(resolution.diagnostics)
    envelope.inputs = [
        file_record(str(source), sha256=sha256_file(source), size=source.stat().st_size)
    ]

    command = _build_command(resolution, source, out, options)
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=options.timeout,
            env={**_environ(cloud=options.mode == "cloud"), **resolution.env},
        )
    except subprocess.TimeoutExpired as error:
        raise CoreError(
            Diagnostic(
                rule_id="PARSE_TIMEOUT",
                severity="error",
                message=f"Parsing timed out after {options.timeout}s.",
                location=str(source),
                expected="a completed extraction",
                actual="the provider did not finish",
                recovery="Re-run with a longer --timeout, or with --max-pages to bound the work.",
            ),
            exit_code=EXIT_EXECUTION,
        ) from error

    provider_result = _last_json_line(completed.stdout)
    if provider_result is None:
        # No parseable result means the provider failed to run, which is a
        # different problem from it judging the input unusable.
        raise CoreError(
            Diagnostic(
                rule_id="PARSE_PROVIDER_FAILED",
                severity="error",
                message="The extract provider produced no machine-readable result.",
                location=str(source),
                expected="one JSON line on stdout",
                actual=f"exit code {completed.returncode}: "
                       f"{summarize_output(completed.stderr, completed.stdout)}",
                recovery="Re-run; if it persists, run the provider directly to see its output.",
            ),
            exit_code=EXIT_EXECUTION,
        )

    envelope.provider_result = provider_result
    provider_status = str(provider_result.get("status") or "blocked")
    envelope.status = EXTRACT_STATUS_MAP.get(provider_status, STATUS_FAILED)
    envelope.extra["parse_status"] = provider_status
    for key in ("tier", "fidelity"):
        if key in provider_result:
            envelope.extra[key] = provider_result[key]
    _carry_diagnostics(envelope, provider_result)

    if envelope.status == STATUS_FAILED:
        envelope.add(
            Diagnostic(
                rule_id="PARSE_INPUT_UNUSABLE",
                severity="error",
                message=f"The provider could not produce a usable bundle ({provider_status}).",
                location=str(source),
                expected="a Parse Bundle",
                actual=str(provider_result.get("reason") or provider_status),
                recovery=str(provider_result.get("hint") or "")
                or "Read provider_result for the question or blocker the provider reported.",
            )
        )
        # The provider ran and judged the input unusable — that is an input
        # problem, not an execution failure.
        return envelope, None, EXIT_INPUT

    envelope.outputs = _bundle_outputs(out)
    human = (
        f"{provider_status} -> {out}\n"
        f"tier={provider_result.get('tier')} "
        f"recommended={(provider_result.get('decision') or {}).get('recommended')}"
    ) if options.human else None
    return envelope, human, EXIT_OK


_CLOUD_CREDENTIALS = (
    "DECKFLOW_API_KEY", "DECKOPS_API_KEY",
    "DECKFLOW_TOKEN", "DECKOPS_TOKEN",
    "DECKFLOW_SPACE_ID", "DECKOPS_SPACE_ID",
)


def _environ(*, cloud: bool) -> dict[str, str]:
    """Withhold cloud credentials unless cloud mode was explicitly requested.

    `--mode local` already forbids uploading. Removing the credentials as well
    means a future change in provider defaults cannot turn a local parse into
    one that ships the user's source material off the machine.

    Since deckflow-extract 0.3 the provider also reads credentials from
    `~/.deckflow/credentials`, the file it shares with DeckHTML — so removing
    variables no longer removes anything on a machine where either tool has
    been logged in. `DECKFLOW_NO_STORED_CREDENTIALS` is the provider's switch
    for exactly this: it makes the environment the only source of truth, which
    is what the stripping above assumes.
    """
    import os

    if cloud:
        environ = dict(os.environ)
    else:
        environ = {
            key: value for key, value in os.environ.items() if key not in _CLOUD_CREDENTIALS
        }
        environ["DECKFLOW_NO_STORED_CREDENTIALS"] = "1"
    environ.update(credential_env())
    return environ
