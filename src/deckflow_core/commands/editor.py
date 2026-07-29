"""`deckflow editor` — a loopback visual editor over the canonical pages.

The provider has no session protocol: it starts a server, auto-saves, and tells
core nothing about what happened in between. So core proves the boundary from
the outside instead — hash every file under `deck/` before and after, and read
the difference.

That answers the questions the Skill actually needs answered ("which pages
changed", "did anything outside pages/ move", "did an element id disappear")
without waiting on provider changes. What it cannot answer is per-operation
intent; that needs the provider to emit save events, and is deliberately absent
rather than approximated.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import project
from ..diagnostics import Diagnostic, summarize_output
from ..envelope import STATUS_FAILED, STATUS_SUCCEEDED, Envelope
from ..exits import EXIT_CONTRACT, EXIT_EXECUTION, EXIT_INPUT, EXIT_OK, CoreError
from ..fsutil import deckflow_home, sha256_file
from ..providers import matrix
from ..providers import resolve as resolver

COMMAND = "editor"

_ELEMENT_ID_RE = re.compile(r"""data-element-id\s*=\s*["']([^"']+)["']""")
_URL_RE = re.compile(r"https?://[0-9A-Za-z\.\-]+:\d+\S*")
_SLIDE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# The provider's own working files. They appear under the project root by
# design, so they are expected artefacts rather than boundary violations.
_EDITOR_ARTEFACTS = (".local-html-editor",)
_READY_TIMEOUT = 60


@dataclass
class Snapshot:
    files: dict[str, str]
    element_ids: dict[str, tuple[str, ...]]


def _is_editor_artefact(relative: Path) -> bool:
    parts = relative.parts
    return any(part in _EDITOR_ARTEFACTS for part in parts) or ".local-html-editor-" in relative.name


def _snapshot(deck_dir: Path) -> Snapshot:
    files: dict[str, str] = {}
    element_ids: dict[str, tuple[str, ...]] = {}
    for path in sorted(deck_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(deck_dir)
        if _is_editor_artefact(relative):
            continue
        files[relative.as_posix()] = sha256_file(path)
        if relative.parts[:1] == ("pages",) and path.suffix.lower() in (".html", ".htm"):
            element_ids[relative.as_posix()] = _element_ids(path)
    return Snapshot(files=files, element_ids=element_ids)


def _element_ids(path: Path) -> tuple[str, ...]:
    """Lexical scan, not a DOM diff.

    Enough to answer "did the set of identities change", which is the invariant
    that matters; it deliberately does not claim to understand the document.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    return tuple(sorted(set(_ELEMENT_ID_RE.findall(text))))


def _preflight(root: Path, page: str | None) -> tuple[Path, Path]:
    """The minimum needed to open a session — not a project validation.

    Full structural and freshness checking stays with the Skill's own
    validator; duplicating it here would create a second, drifting opinion.
    """
    root = Path(root).expanduser().resolve()
    deck_dir = root / "deck"
    pages_dir = deck_dir / "pages"
    if not pages_dir.is_dir():
        raise CoreError(
            Diagnostic(
                rule_id="PROJECT_PAGES_MISSING",
                severity="error",
                message="The project has no canonical page directory.",
                location=str(pages_dir),
                expected="deck/pages/ containing at least one HTML page",
                actual="no such directory",
                recovery="Author the deck pages before opening the editor.",
            ),
            exit_code=EXIT_INPUT,
        )
    pages_root = pages_dir.resolve()
    page_candidates = sorted(candidate for candidate in pages_dir.glob("*.html") if candidate.is_file())
    escaped = [candidate for candidate in page_candidates if candidate.resolve().parent != pages_root]
    if escaped:
        raise CoreError(
            Diagnostic(
                rule_id="EDITOR_PAGE_ESCAPES_ROOT",
                severity="error",
                message="A canonical page resolves outside deck/pages/.",
                location=str(escaped[0]),
                expected=f"a direct HTML file inside {pages_root}",
                actual=str(escaped[0].resolve()),
                recovery="Replace the escaping symlink with a real canonical page inside deck/pages/.",
            ),
            exit_code=EXIT_INPUT,
        )
    pages = [candidate.resolve() for candidate in page_candidates]
    if not pages:
        raise CoreError(
            Diagnostic(
                rule_id="PROJECT_PAGES_EMPTY",
                severity="error",
                message="There are no canonical pages to edit.",
                location=str(pages_dir),
                expected="at least one deck/pages/*.html",
                actual="an empty directory",
                recovery="Author at least one page before opening the editor.",
            ),
            exit_code=EXIT_INPUT,
        )
    if page is None:
        return deck_dir, pages_root
    if not _SLIDE_ID_RE.fullmatch(page):
        raise CoreError(
            Diagnostic(
                rule_id="EDITOR_PAGE_ID_INVALID",
                severity="error",
                message="The requested page is not a safe slide id.",
                location=page,
                expected="an alphanumeric slide id containing only letters, digits, dot, underscore, or hyphen",
                actual=page,
                recovery="Pass the stem of a direct deck/pages/*.html file, without path separators or `..`.",
            ),
            exit_code=EXIT_INPUT,
        )
    pages_by_id = {candidate.stem: candidate for candidate in pages}
    target = pages_by_id.get(page)
    if target is None:
        raise CoreError(
            Diagnostic(
                rule_id="EDITOR_PAGE_NOT_FOUND",
                severity="error",
                message=f"No canonical page named {page}.",
                location=str(pages_root / f"{page}.html"),
                expected=f"one of: {', '.join(p.stem for p in pages)}",
                actual=page,
                recovery="Pass --page with a slide id that exists, or omit it to open all pages.",
            ),
            exit_code=EXIT_INPUT,
        )
    return deck_dir, target


def _emit_event(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _drain(stream, sink: list[str]) -> None:
    """Forward a child stream to stderr and keep it for diagnostics.

    Runs on a thread so a blocking `readline` can never stall the deadline in
    `_await_ready` — the child may print nothing at all, and a deadline checked
    only between reads would then never be reached.
    """
    try:
        for line in iter(stream.readline, ""):
            sink.append(line.rstrip())
            sys.stderr.write(f"[editor] {line}")
    except (ValueError, OSError):  # stream closed while shutting down
        pass


def _await_ready(stdout_lines: list[str], child: subprocess.Popen) -> str | None:
    """Wait for the provider to announce a loopback URL.

    Prefix parsing is a stopgap: the provider prints prose, not JSON. It is the
    top item on the provider gap list; until that lands, a URL that never
    appears is reported rather than guessed at.
    """
    deadline = time.monotonic() + _READY_TIMEOUT
    seen = 0
    while time.monotonic() < deadline:
        while seen < len(stdout_lines):
            match = _URL_RE.search(stdout_lines[seen])
            seen += 1
            if match:
                return match.group(0)
        if child.poll() is not None:
            return None
        time.sleep(0.05)
    return None


def _wait_for_session_end(child: subprocess.Popen) -> bool:
    """Block until the child exits or the operator asks to stop.

    Explicit handlers rather than KeyboardInterrupt, for two reasons: a process
    launched in the background inherits SIGINT as SIG_IGN, so the default
    handler would never fire and the session could not be ended at all; and a
    supervising agent sends SIGTERM, which deserves the same clean finish as a
    human's Ctrl-C. Re-arming both here overrides whatever was inherited.
    """
    stop = threading.Event()

    def _handle(_signum, _frame):
        stop.set()

    previous = {}
    for number in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[number] = signal.signal(number, _handle)
        except (ValueError, OSError):  # not the main thread, or unsupported
            pass
    try:
        # Polling rather than waitpid: it does not depend on a signal
        # interrupting a blocking syscall, which is not portable.
        while child.poll() is None and not stop.is_set():
            time.sleep(0.1)
        interrupted = stop.is_set() and child.poll() is None
        if interrupted:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        return interrupted
    finally:
        for number, handler in previous.items():
            try:
                signal.signal(number, handler)
            except (ValueError, OSError):
                pass


def _classify(before: Snapshot, after: Snapshot,
              pages_prefix: str = "pages/") -> tuple[list[dict[str, Any]], list[Diagnostic]]:
    """Turn the before/after difference into changed pages and violations."""
    changed_pages: list[dict[str, Any]] = []
    findings: list[Diagnostic] = []

    touched = {
        name for name in set(before.files) | set(after.files)
        if before.files.get(name) != after.files.get(name)
    }

    for name in sorted(touched):
        if not name.startswith(pages_prefix):
            continue
        changed_pages.append({
            "slide_id": Path(name).stem,
            "path": name,
            "before_sha256": before.files.get(name),
            "after_sha256": after.files.get(name),
        })

    outside = sorted(name for name in touched if not name.startswith(pages_prefix))
    if outside:
        findings.append(
            Diagnostic(
                rule_id="EDITOR_TOUCHED_PROTECTED_FILE",
                severity="error",
                message="The editing session changed files outside deck/pages/.",
                location="deck/",
                expected="only deck/pages/*.html modified",
                actual=f"also changed: {', '.join(outside[:5])}",
                recovery=(
                    "Restore these from the Skill's own writers. The editor keeps copies "
                    "under deck/.local-html-editor/backups/."
                ),
            )
        )

    for name in sorted(set(before.element_ids) & set(after.element_ids)):
        was, now = set(before.element_ids[name]), set(after.element_ids[name])
        if was == now:
            continue
        removed, added = sorted(was - now), sorted(now - was)
        findings.append(
            Diagnostic(
                rule_id="EDITOR_ELEMENT_IDENTITY_CHANGED",
                severity="error",
                message="The set of element identities on a page changed.",
                location=name,
                expected="the same data-element-id set before and after",
                actual=f"removed: {removed or 'none'}; added: {added or 'none'}",
                recovery=(
                    "Element identities bind pages to sources and to the export mapping. "
                    "Restore the page from deck/.local-html-editor/backups/ and re-edit "
                    "text only."
                ),
            )
        )

    return changed_pages, findings


def run(options: Any) -> tuple[Envelope, str | None, int]:
    envelope = Envelope(command=COMMAND)
    deck_dir, target = _preflight(Path(options.project), options.page)
    session_id = str(uuid.uuid4())

    spec = matrix.get("editor", options.provider_specs)
    resolution = resolver.resolve(
        spec,
        policy=resolver.policy_from(options.provider_install),
        bin_overrides=options.provider_bins,
        home=deckflow_home(),
    )
    envelope.providers = [resolution.to_json()]
    envelope.extend(resolution.diagnostics)

    before = _snapshot(deck_dir)
    protected_before = project.protected_snapshot(deck_dir.parent)

    command = [
        *resolution.command, str(target),
        # Root is deck/, not pages/: canonical pages reference ../assets/…, so
        # a narrower root would break every resource in the preview.
        "--root", str(deck_dir),
        "--port", str(options.port),
    ]
    if not options.open:
        command.append("--no-open")

    child = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, env=dict(os.environ, **resolution.env),
    )
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    drainers: list[threading.Thread] = []
    for stream, sink in ((child.stdout, stdout_lines), (child.stderr, stderr_lines)):
        thread = threading.Thread(target=_drain, args=(stream, sink), daemon=True)
        thread.start()
        drainers.append(thread)

    url = _await_ready(stdout_lines, child)
    if url is None:
        child.kill()
        raise CoreError(
            Diagnostic(
                rule_id="EDITOR_START_FAILED",
                severity="error",
                message="The editor did not start.",
                location=str(target),
                expected=f"a loopback URL within {_READY_TIMEOUT}s",
                actual=summarize_output("\n".join(stderr_lines), "\n".join(stdout_lines)),
                recovery="Check that the port is free, then re-run.",
            ),
            exit_code=EXIT_EXECUTION,
        )

    _emit_event({
        "schema_version": 1, "command": COMMAND, "event": "ready",
        "session_id": session_id, "url": url,
        "project": str(deck_dir.parent), "editing": str(target),
    })
    sys.stderr.write("[deckflow] press Ctrl-C when you are done editing\n")

    interrupted = _wait_for_session_end(child)
    for thread in drainers:
        thread.join(timeout=1)
    provider_exit_code = child.poll()
    provider_failed = not interrupted and provider_exit_code != 0
    provider_output = summarize_output("\n".join(stderr_lines), "\n".join(stdout_lines))

    after = _snapshot(deck_dir)
    changed_pages, findings = _classify(before, after)
    envelope.extend(findings)
    if provider_failed:
        envelope.add(
            Diagnostic(
                rule_id="EDITOR_PROVIDER_EXITED",
                severity="error",
                message="The editor provider exited unexpectedly after announcing readiness.",
                location=str(target),
                expected="exit code 0, or a session ended by the supervising interrupt",
                actual=f"exit code {provider_exit_code}: {provider_output}",
                recovery="Restore any affected pages from the editor backups, then re-run the session.",
            )
        )
    envelope.extra.update({
        "event": "finished",
        "session_id": session_id,
        "project": str(deck_dir.parent),
        "changed_pages": changed_pages,
        "ended_by": (
            "interrupt" if interrupted else "provider-error" if provider_failed else "editor-exit"
        ),
        "provider_exit_code": provider_exit_code,
        "backups": str(deck_dir / ".local-html-editor" / "backups"),
    })

    if project.protected_snapshot(deck_dir.parent) != protected_before and not findings:
        envelope.add(
            Diagnostic(
                rule_id="EDITOR_TOUCHED_PROTECTED_FILE",
                severity="error",
                message="The editing session changed project records it must never write.",
                location=str(deck_dir.parent),
                expected="index.html, deck-head.html and build-manifest.json unchanged",
                actual="one or more changed",
                recovery="Restore them from the Skill's own writers before continuing.",
            )
        )

    envelope.add(
        Diagnostic(
            rule_id="EDITOR_OPERATION_AUDIT_UNAVAILABLE",
            severity="info",
            message="This session was audited at file level, not per operation.",
            location=str(deck_dir),
            expected="per-element operation records",
            actual="before/after page hashes and element-identity checks",
            recovery=(
                "Run the Skill's page and project validators on the changed pages; "
                "core proves only that the session stayed inside deck/pages/."
            ),
        )
    )

    has_error = any(d.severity == "error" for d in envelope.diagnostics)
    envelope.status = STATUS_FAILED if has_error else STATUS_SUCCEEDED
    if provider_failed:
        return envelope, None, EXIT_EXECUTION
    return envelope, None, EXIT_CONTRACT if has_error else EXIT_OK
