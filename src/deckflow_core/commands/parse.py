"""`deckflow parse` — one local file into the project's canonical Source Bundle.

The extract provider remains a pure parser. Core gives it a transient output
directory, validates the Parse Bundle and provider report as one trust
boundary, then delegates deterministic canonical assembly to source_bundle.
The transient conversion is never exposed to Luna or copied into provenance.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
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
from ..fsutil import sha256_file
from ..home import credential_env, deckflow_home
from ..source_bundle import (
    SourceBundleError,
    assemble_source_bundle,
    scrub_provenance,
    validate_source_bundle,
)

COMMAND = "parse"
_URL_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.IGNORECASE)
_BCP47_RE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")


def _resolve_input(raw: str) -> Path:
    if _URL_RE.match(raw):
        raise CoreError(
            Diagnostic(
                rule_id="PARSE_INPUT_NOT_LOCAL",
                severity="error",
                message="`deckflow parse` accepts a local file, not a URL.",
                location=raw,
                expected="a direct path to an existing local file",
                actual="a URL",
                recovery="Download the page as a local HTML file, then parse that file.",
            ),
            exit_code=EXIT_INPUT,
        )
    path = Path(raw).expanduser()
    try:
        info = path.lstat()
    except OSError:
        raise CoreError(
            Diagnostic(
                rule_id="PARSE_INPUT_MISSING",
                severity="error",
                message="The input file does not exist.",
                location=str(path),
                expected="an existing direct regular file",
                actual="no such path",
                recovery="Check the path, then re-run.",
            ),
            exit_code=EXIT_INPUT,
        )
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise CoreError(
            Diagnostic(
                rule_id="PARSE_INPUT_NOT_A_FILE",
                severity="error",
                message="The input must be a direct regular file.",
                location=str(path),
                expected="one regular file, not a symlink",
                actual="a directory, symlink, or special file",
                recovery="Pass the original local file directly.",
            ),
            exit_code=EXIT_INPUT,
        )
    if info.st_nlink != 1:
        raise CoreError(
            Diagnostic(
                rule_id="PARSE_INPUT_HARDLINKED",
                severity="error",
                message="Hard-linked inputs are not accepted for canonical ingestion.",
                location=str(path),
                expected="a file with one filesystem link",
                actual=f"link count {info.st_nlink}",
                recovery="Copy the original to a new regular file, then ingest that copy.",
            ),
            exit_code=EXIT_INPUT,
        )
    return path.resolve()


def _resolve_project(raw: str) -> Path:
    project = Path(raw).expanduser()
    try:
        info = project.lstat()
    except OSError:
        info = None
    if info is None or stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise CoreError(
            Diagnostic(
                rule_id="PARSE_PROJECT_INVALID",
                severity="error",
                message="The Deck project directory is missing or is not direct.",
                location=str(project),
                expected="an existing direct project directory",
                actual="missing, non-directory, or symlink",
                recovery="Initialize the Deck project, then pass it with --project.",
            ),
            exit_code=EXIT_INPUT,
        )
    return project.resolve()


def _require_input_outside_bundle(source: Path, bundle: Path) -> None:
    if source == bundle or bundle in source.parents:
        raise CoreError(
            Diagnostic(
                rule_id="PARSE_OUTPUT_CONTAINS_INPUT",
                severity="error",
                message="The canonical Source Bundle contains the input file.",
                location=str(bundle),
                expected="an input outside <project>/source-bundle",
                actual=str(source),
                recovery="Move the original outside source-bundle, then ingest it.",
            ),
            exit_code=EXIT_OUTPUT,
        )


def _build_command(
    resolution: extract_resolve.Extract,
    source: Path,
    parse_out: Path,
    options: Any,
) -> list[str]:
    command = [
        *resolution.command,
        "parse",
        str(source),
        "--out",
        str(parse_out),
        "--replace",
        "--anchors",
        "on",
        "--mode",
        "cloud" if options.mode == "cloud" else "local",
        "--fetch-remote-images",
        "off",
        "--upgrade",
        options.upgrade or "never",
    ]
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


def _public_provider_result(
    provider_result: dict[str, Any],
    *,
    secrets: list[str],
) -> dict[str, Any]:
    public = {
        key: value
        for key, value in provider_result.items()
        if key not in {"bundle", "manifest", "archive"}
    }
    scrubbed = scrub_provenance(public, secrets=secrets)
    return scrubbed if isinstance(scrubbed, dict) else {}


def _source_error(error: SourceBundleError, bundle: Path) -> CoreError:
    if error.code == "source-bundle-confirmed":
        rule_id = "PARSE_SOURCE_BUNDLE_CONFIRMED"
        exit_code = EXIT_OUTPUT
        recovery = "Start a new project or use the future explicit invalidation workflow."
    elif error.code == "source-already-included":
        rule_id = "PARSE_SOURCE_ALREADY_INCLUDED"
        exit_code = EXIT_INPUT
        recovery = "Do not ingest the same source bytes twice."
    elif error.code.startswith("source-") and error.code not in {
        "source-input-changed",
        "source-asset-copy-mismatch",
    }:
        rule_id = "PARSE_SOURCE_BUNDLE_INVALID"
        exit_code = EXIT_OUTPUT
        recovery = "Repair or move the existing Source Bundle; core will not overwrite it."
    else:
        rule_id = "PARSE_PROVIDER_CONTRACT_INVALID"
        exit_code = EXIT_EXECUTION
        recovery = "Treat this as a core/provider contract failure and inspect diagnostics."
    return CoreError(
        Diagnostic(
            rule_id=rule_id,
            severity="error",
            message="The canonical Source Bundle was not changed.",
            location=str(bundle),
            expected="a validated, self-contained Luna Source Bundle",
            actual=f"{error.code}: {error.message}",
            recovery=recovery,
        ),
        exit_code=exit_code,
    )


def _bundle_outputs(bundle: Path) -> list[dict[str, Any]]:
    manifest = bundle / "manifest.json"
    return [
        {"path": str(bundle), "kind": "source-bundle"},
        file_record(
            str(manifest),
            sha256=sha256_file(manifest),
            size=manifest.stat().st_size,
        ),
    ]


def _carry_diagnostics(envelope: Envelope, provider_result: dict[str, Any]) -> None:
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
                expected="the highest fidelity authorized for this run",
                actual=str(decision.get("reason") or "a fallback engine was used"),
                recovery=(
                    "Inspect provider_result.recommendations[]. "
                    "Pass --upgrade auto only after local installation is authorized."
                ),
            )
        )


def run(options: Any) -> tuple[Envelope, str | None, int]:
    envelope = Envelope(command=COMMAND)
    source = _resolve_input(options.input)
    project = _resolve_project(options.project)
    source_bundle = project / "source-bundle"
    _require_input_outside_bundle(source, source_bundle)

    brief = options.brief.strip()
    if not brief:
        raise CoreError(
            Diagnostic(
                rule_id="PARSE_BRIEF_REQUIRED",
                severity="error",
                message="The presentation brief cannot be empty.",
                expected="the user's task description",
                actual="empty or whitespace",
                recovery="Pass the task text with --brief.",
            ),
            exit_code=EXIT_INPUT,
        )
    if not _BCP47_RE.match(options.deck_language):
        raise CoreError(
            Diagnostic(
                rule_id="PARSE_DECK_LANGUAGE_INVALID",
                severity="error",
                message="The Deck language must be a BCP 47 tag.",
                expected="a tag such as zh-CN or en-US",
                actual=str(options.deck_language),
                recovery="Pass the eventual Deck language with --deck-language.",
            ),
            exit_code=EXIT_INPUT,
        )

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

    workdir = Path(tempfile.mkdtemp(prefix="deckflow-parse-"))
    parse_out = workdir / "parse-bundle"
    try:
        command = _build_command(resolution, source, parse_out, options)
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                text=True,
                timeout=options.timeout,
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
                    recovery="Re-run with a longer --timeout, or use --max-pages.",
                ),
                exit_code=EXIT_EXECUTION,
            ) from error

        provider_result = _last_json_line(completed.stdout)
        if provider_result is None:
            raise CoreError(
                Diagnostic(
                    rule_id="PARSE_PROVIDER_FAILED",
                    severity="error",
                    message="The extract provider produced no machine-readable result.",
                    location=str(source),
                    expected="one JSON line on stdout",
                    actual=f"exit code {completed.returncode}: "
                    f"{summarize_output(completed.stderr, completed.stdout)}",
                    recovery="Re-run; if it persists, inspect the provider directly.",
                ),
                exit_code=EXIT_EXECUTION,
            )

        secrets = [str(parse_out), str(workdir)]
        public_result = _public_provider_result(provider_result, secrets=secrets)
        envelope.provider_result = public_result
        provider_status = str(provider_result.get("status") or "blocked")
        decision = provider_result.get("decision")
        blocking = any(
            isinstance(gap, dict) and gap.get("severity") == "blocking"
            for gap in provider_result.get("gaps") or ()
        )
        importable = (
            provider_status in {"parsed", "repairable"}
            and isinstance(decision, dict)
            and decision.get("usable") is True
            and not blocking
        )
        envelope.status = (
            EXTRACT_STATUS_MAP.get(provider_status, STATUS_FAILED)
            if importable
            else STATUS_FAILED
        )
        envelope.extra["parse_status"] = provider_status
        for key in ("tier", "fidelity", "engine_acquisition"):
            if key in public_result:
                envelope.extra[key] = public_result[key]
        _carry_diagnostics(envelope, public_result)

        if not importable:
            envelope.add(
                Diagnostic(
                    rule_id="PARSE_INPUT_UNUSABLE",
                    severity="error",
                    message=f"The provider could not produce usable source material ({provider_status}).",
                    location=str(source),
                    expected="decision.usable=true with no blocking gap",
                    actual=str(provider_result.get("reason") or provider_status),
                    recovery=str(provider_result.get("hint") or "")
                    or "Inspect provider_result for the affected route and recovery.",
                )
            )
            return envelope, None, EXIT_INPUT

        try:
            result = assemble_source_bundle(
                project=project,
                input_path=source,
                parse_bundle=parse_out,
                provider_result=provider_result,
                brief=brief,
                deck_language=options.deck_language,
                title=options.title,
                replace=options.replace,
            )
            validate_source_bundle(source_bundle)
        except SourceBundleError as error:
            raise _source_error(error, source_bundle) from error

        envelope.extra.update(
            {
                "source_id": result["source_id"],
                "material_id": result["material_id"],
                "import_id": result["import_id"],
                "previous_fingerprint": result["previous_fingerprint"],
                "content_fingerprint": result["content_fingerprint"],
            }
        )
        envelope.outputs = _bundle_outputs(source_bundle)
        human = (
            f"{provider_status} -> {source_bundle}\n"
            f"tier={provider_result.get('tier')} "
            f"recommended={(provider_result.get('decision') or {}).get('recommended')}"
        ) if options.human else None
        return envelope, human, EXIT_OK
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


_CLOUD_CREDENTIALS = (
    "DECKFLOW_API_KEY",
    "DECKOPS_API_KEY",
    "DECKFLOW_TOKEN",
    "DECKOPS_TOKEN",
    "DECKFLOW_SPACE_ID",
    "DECKOPS_SPACE_ID",
)


def _environ(*, cloud: bool) -> dict[str, str]:
    if cloud:
        environ = dict(os.environ)
    else:
        environ = {
            key: value for key, value in os.environ.items() if key not in _CLOUD_CREDENTIALS
        }
        environ["DECKFLOW_NO_STORED_CREDENTIALS"] = "1"
    environ.update(credential_env())
    environ["DECKFLOW_EXTRACT_HOME"] = str(deckflow_home() / "parse")
    return environ
