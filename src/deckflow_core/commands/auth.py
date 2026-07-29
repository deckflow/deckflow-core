"""`deckflow auth` — the shared Deckflow credential, asked of the provider.

Core owns no credential of its own.  `~/.deckflow/credentials` is written by
`deckflow-extract` and read by DeckHTML too, and its merge rules — DeckHTML
silently rewrites the whole file when it sees a key it does not recognise — are
not core's to reimplement.  So every subcommand here forwards to extract and
carries its answer back; core never opens that file.

This is core's one exception to "core writes only --out and --report", and it
is narrow on purpose: two commands, both explicit user actions, both delegated.

There is no `logout`.  It exists on extract and not on DeckHTML, and an agent
never needs it — but that means clearing a credential has no entry point here,
so `status` says where the file is and which command clears it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

from ..diagnostics import Diagnostic
from ..envelope import STATUS_FAILED, Envelope
from ..exits import EXIT_EXECUTION, EXIT_OK, EXIT_USAGE, CoreError
from ..extract import pin
from ..extract import resolve as extract_resolve
from ..home import credential_env, credentials_file, deckflow_home
from ..probe import cloud as cloud_probe

COMMAND = "auth"

_CLEAR_HINT = (
    "Clear a stored credential with `deckflow-extract auth logout`; "
    "`deckflow` deliberately has no logout."
)


def run_status(options: Any) -> tuple[Envelope, str | None, int]:
    """Read-only, and never acquires: a question must not cost a download."""
    envelope = Envelope(command="auth status")
    extract = extract_resolve.resolve(
        bin_override=options.extract_bin, probe_only=True, home=deckflow_home(),
    )
    envelope.extract = extract.to_json()
    envelope.extend(extract.diagnostics)

    cloud = cloud_probe.probe(extract)
    envelope.extra["cloud"] = cloud

    if not cloud["available"]:
        envelope.add(
            Diagnostic(
                rule_id="CLOUD_STATUS_UNKNOWN",
                severity="info",
                message="Cloud credentials were not inspected.",
                location=str(credentials_file()),
                expected=f"{pin.PACKAGE} available to answer the question",
                actual=cloud["reason"],
                recovery=(
                    f"Run `deckflow env setup` (~{pin.DOWNLOAD_MB}MB) if you need this answer. "
                    "Until then treat cloud as unavailable rather than as not configured."
                ),
            )
        )
    elif not cloud["configured"]:
        envelope.add(
            Diagnostic(
                rule_id="CLOUD_NOT_CONFIGURED",
                severity="info",
                message="No cloud credential is configured on this machine.",
                location=cloud["config_file"],
                recovery="Run `deckflow auth set-key --stdin`, or `deckflow auth login` in your own terminal.",
            )
        )
    else:
        envelope.add(
            Diagnostic(
                rule_id="CLOUD_CONFIGURED",
                severity="info",
                message=f"A cloud credential is configured (source: {cloud['credential_source']}).",
                location=cloud["config_file"],
                expected="authorization is still the user's to give, per request",
                actual="a usable credential exists",
                recovery=_CLEAR_HINT,
            )
        )
    return envelope, _render_status(cloud) if options.human else None, EXIT_OK


def _render_status(cloud: dict[str, Any]) -> str:
    if not cloud["available"]:
        return f"cloud: unknown ({cloud['reason']})\nfile:  {cloud['config_file']}"
    state = "configured" if cloud["configured"] else "not configured"
    return (
        f"cloud: {state}"
        + (f" via {cloud['credential_source']}" if cloud["configured"] else "")
        + f"\nfile:  {cloud['config_file']} (shared with {cloud['shared_with']})"
    )


def run_login(options: Any) -> tuple[Envelope, str | None, int]:
    """Browser login, refused where it cannot work.

    The flow opens a browser, serves a callback on a fixed local port and waits
    up to five minutes. In an agent subprocess none of that is reachable, so
    core refuses up front and hands the user the command instead of hanging and
    then reporting a timeout as if something had gone wrong.
    """
    envelope = Envelope(command="auth login")
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        envelope.status = STATUS_FAILED
        envelope.add(
            Diagnostic(
                rule_id="LOGIN_REQUIRES_TERMINAL",
                severity="error",
                message="Browser login needs an interactive terminal.",
                location=str(credentials_file()),
                expected="a TTY on stdin and stderr",
                actual="a non-interactive process",
                recovery=(
                    "Ask the user to run `deckflow auth login` in their own terminal, or use "
                    "`deckflow auth set-key --stdin` with a space worker secret, which needs no browser."
                ),
            )
        )
        return envelope, None, EXIT_EXECUTION

    extract = _require_extract(options, envelope)
    command = [*extract.command, "auth", "login"]
    if options.no_open:
        command.append("--no-open")
    return _forward(envelope, extract, command, timeout=options.timeout)


def run_set_key(options: Any) -> tuple[Envelope, str | None, int]:
    """Store a space worker secret, read from stdin unless given on argv."""
    envelope = Envelope(command="auth set-key")
    key = _read_key(options)
    extract = _require_extract(options, envelope)
    return _forward(
        envelope,
        extract,
        [*extract.command, "config", "set", "api-key", "--stdin"],
        timeout=60,
        input_text=key + "\n",
    )


def _read_key(options: Any) -> str:
    if options.stdin:
        key = sys.stdin.read().strip()
    else:
        key = (options.key or "").strip()
    if not key:
        raise CoreError(
            Diagnostic(
                rule_id="CLI_USAGE",
                severity="error",
                message="No API key was provided.",
                expected="`deckflow auth set-key <key>`, or --stdin with the key on stdin",
                actual="an empty key",
                recovery=(
                    "Prefer --stdin: a key passed as an argument is visible in the process "
                    "list and in shell history."
                ),
            ),
            exit_code=EXIT_USAGE,
        )
    return key


def _require_extract(options: Any, envelope: Envelope) -> extract_resolve.Extract:
    """Both write commands are explicit user actions, so acquiring is allowed."""
    extract = extract_resolve.resolve(
        bin_override=options.extract_bin,
        offline=extract_resolve.offline_from(options.offline),
        home=deckflow_home(),
    )
    envelope.extract = extract.to_json()
    envelope.extend(extract.diagnostics)
    return extract


def _forward(envelope: Envelope, extract: extract_resolve.Extract,
             command: list[str], *, timeout: int,
             input_text: str | None = None) -> tuple[Envelope, str | None, int]:
    """Run the provider, keeping its stderr visible and its stdout private.

    stderr is inherited so a login prints its URL and progress where the user
    can see it; stdout is captured because core's own stdout carries exactly
    one JSON object and nothing else.
    """
    try:
        completed = subprocess.run(
            command, capture_output=False, stdout=subprocess.PIPE, text=True,
            timeout=timeout,
            env={**os.environ, **credential_env(), **extract.env},
            input=input_text,
        )
    except subprocess.TimeoutExpired as error:
        raise CoreError(
            Diagnostic(
                rule_id="AUTH_TIMEOUT",
                severity="error",
                message=f"The provider did not finish within {timeout}s.",
                location=pin.PACKAGE,
                recovery="Re-run, or configure the credential directly with `deckflow-extract`.",
            ),
            exit_code=EXIT_EXECUTION,
        ) from error

    payload = _last_json_line(completed.stdout)
    if payload is None:
        raise CoreError(
            Diagnostic(
                rule_id="AUTH_PROVIDER_FAILED",
                severity="error",
                message="The provider produced no machine-readable result.",
                location=pin.PACKAGE,
                expected="one JSON line on stdout",
                actual=f"exit code {completed.returncode}",
                recovery="Re-run; if it persists, run `deckflow-extract auth status` directly.",
            ),
            exit_code=EXIT_EXECUTION,
        )

    envelope.provider_result = payload
    _carry(envelope, payload)
    if str(payload.get("status")) != "ok":
        envelope.status = STATUS_FAILED
        return envelope, None, EXIT_EXECUTION
    envelope.extra["config_file"] = payload.get("config_file") or str(credentials_file())
    return envelope, None, EXIT_OK


def _carry(envelope: Envelope, payload: dict[str, Any]) -> None:
    """Surface the provider's findings verbatim.

    `config set` warns when an environment variable shadows the value it just
    wrote — dropping that would report success for a credential that will never
    be used.
    """
    for entry in payload.get("diagnostics") or ():
        if not isinstance(entry, dict):
            continue
        severity = entry.get("severity")
        envelope.add(
            Diagnostic(
                rule_id="AUTH_PROVIDER_DIAGNOSTIC",
                severity=severity if severity in ("error", "warning", "info") else "info",
                message=str(entry.get("message") or entry.get("code") or "provider diagnostic"),
                location=str(entry.get("code") or "") or None,
                recovery=str(payload.get("hint")) if payload.get("hint") else None,
            )
        )


def _last_json_line(stdout: str) -> dict[str, Any] | None:
    for line in reversed((stdout or "").strip().splitlines()):
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


_ACTIONS = {"status": run_status, "login": run_login, "set-key": run_set_key}


def run(options: Any) -> tuple[Envelope, str | None, int]:
    action = getattr(options, "auth_action", None) or "status"
    handler = _ACTIONS.get(action)
    if handler is None:  # pragma: no cover - argparse constrains the choices
        raise CoreError(
            Diagnostic(
                rule_id="CLI_USAGE",
                severity="error",
                message=f"Unknown auth action: {action}.",
                expected=" | ".join(_ACTIONS),
                actual=str(action),
                recovery="Run `deckflow auth --help`.",
            ),
            exit_code=EXIT_USAGE,
        )
    return handler(options)
