"""`deckflow update` — install a newer core beside the running one.

Never in place.  Upgrading the package that is currently executing is the kind
of operation that half-works: a new version lands in `~/.deckflow/core/<new>/`
and the launcher picks it up on the next run, so an interrupted update leaves
the working copy untouched and rollback is `rm -rf` of one directory.

Updating core is also how the provider pin moves: `deckflow-extract` is pinned
by core's matrix, so there is deliberately no `deckflow update extract` — an
independently upgradable provider is not a pinned one.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .. import __version__, versions
from ..diagnostics import Diagnostic
from ..envelope import STATUS_FAILED, Envelope
from ..exits import EXIT_EXECUTION, EXIT_OK, CoreError
from ..extract import install as installer
from ..extract import pin
from ..extract import resolve as extract_resolve
from ..fsutil import remove_tree
from ..home import core_dir, core_root, deckflow_home, installed_core_versions, version_key
from ..probe import runtime as runtime_probe
from ..probe import skill as skill_probe

COMMAND = "update"

PACKAGE = "deckflow-core"
MODULE = "deckflow_core"
INDEX_URL = "https://pypi.org/simple"
_RELEASE_API = "https://pypi.org/pypi/deckflow-core/json"


def run(options: Any) -> tuple[Envelope, str | None, int]:
    if getattr(options, "update_target", None) == "skill":
        return run_skill(options)
    return run_runtime(options)


def run_runtime(options: Any) -> tuple[Envelope, str | None, int]:
    home = deckflow_home()
    envelope = Envelope(command="update")
    runtime = runtime_probe.probe(home=home)
    envelope.extra["runtime"] = runtime

    if extract_resolve.offline_from(options.offline):
        raise CoreError(
            Diagnostic(
                rule_id="UPDATE_OFFLINE",
                severity="error",
                message="Updating needs network access, and --offline forbids it.",
                expected="a reachable package index",
                actual="--offline",
                recovery="Re-run without --offline.",
            ),
            exit_code=EXIT_EXECUTION,
        )

    latest = _latest_version(envelope)
    envelope.extra["update"] = {
        "current": __version__,
        "latest": latest,
        "available": bool(latest and versions.is_newer(latest, __version__)),
    }

    if latest is None:
        envelope.status = STATUS_FAILED
        return envelope, None, EXIT_EXECUTION

    if not versions.is_newer(latest, __version__):
        envelope.add(
            Diagnostic(
                rule_id="UPDATE_NOT_NEEDED",
                severity="info",
                message=f"{PACKAGE} {__version__} is already the newest published version.",
                location=PACKAGE,
                recovery="Nothing to do.",
            )
        )
        return envelope, f"{PACKAGE} {__version__} is current" if options.human else None, EXIT_OK

    if options.check:
        envelope.add(
            Diagnostic(
                rule_id="UPDATE_AVAILABLE",
                severity="info",
                message=f"{PACKAGE} {latest} is available (running {__version__}).",
                location=PACKAGE,
                expected=latest,
                actual=__version__,
                recovery="Run `deckflow update` to install it.",
            )
        )
        return envelope, f"{__version__} -> {latest} available" if options.human else None, EXIT_OK

    if runtime["installation"] == "vendored":
        # A vendored copy ships inside the Skill and is replaced by updating the
        # Skill. Installing a managed copy beside it would not change what runs.
        envelope.status = STATUS_FAILED
        envelope.add(
            Diagnostic(
                rule_id="UPDATE_VENDORED",
                severity="error",
                message="This core is vendored inside a skill and cannot update itself.",
                location=runtime["location"],
                expected="a managed install under ~/.deckflow/core",
                actual="a vendored copy",
                recovery="Update the skill instead; see `deckflow update skill`.",
            )
        )
        return envelope, None, EXIT_EXECUTION

    target = core_dir(latest, home)
    installer.install(
        package=PACKAGE, version=latest, target=target, index_url=INDEX_URL,
    )
    envelope.outputs.append({"path": str(target), "kind": "core-runtime", "version": latest})
    envelope.add(
        Diagnostic(
            rule_id="UPDATE_INSTALLED",
            severity="info",
            message=f"Installed {PACKAGE} {latest}; it takes effect on the next run.",
            location=str(target),
            expected=latest,
            actual=f"{__version__} still running in this process",
            recovery="Re-run any deckflow command to use the new version.",
        )
    )
    _prune(envelope, latest, home)
    _note_pin_move(envelope, home)
    return envelope, f"installed {PACKAGE} {latest} -> {target}" if options.human else None, EXIT_OK


def _latest_version(envelope: Envelope) -> str | None:
    try:
        # Fixed HTTPS origin, never caller input; custom/file schemes cannot
        # reach this call.
        with urllib.request.urlopen(_RELEASE_API, timeout=30) as response:  # nosec B310
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
        envelope.add(
            Diagnostic(
                rule_id="UPDATE_INDEX_UNAVAILABLE",
                severity="error",
                message="Could not ask the index for the newest version.",
                location=_RELEASE_API,
                expected="a JSON release listing",
                actual=str(error),
                recovery=f"Check network access, or install manually: pip install --upgrade {PACKAGE}",
            )
        )
        return None
    version = ((payload or {}).get("info") or {}).get("version")
    return str(version) if version else None


def _prune(envelope: Envelope, keep: str, home: Path) -> None:
    """Delete managed versions older than the one just installed.

    The running version is never deleted even when it is older: it may be the
    copy executing this call.  Newer directories are left alone too, so a
    concurrent update cannot be undone by this one.
    """
    threshold = version_key(keep)
    removed: list[str] = []
    for version in installed_core_versions(home):
        if version in (keep, __version__):
            continue
        if version_key(version) < threshold:
            remove_tree(core_root(home) / version)
            removed.append(version)
    if removed:
        envelope.outputs.append({"path": str(core_root(home)), "removed": sorted(removed)})


def _note_pin_move(envelope: Envelope, home: Path) -> None:
    """Say when the provider will move with the new core.

    Core does not acquire the new pin here: the new core has its own matrix and
    will acquire what it needs on first use.  But a user who already has a
    managed extract deserves to know the version they hold may be superseded.
    """
    current = extract_resolve.resolve(probe_only=True, home=home)
    if current.resolution != extract_resolve.RESOLUTION_MANAGED:
        return
    envelope.add(
        Diagnostic(
            rule_id="EXTRACT_PIN_MAY_MOVE",
            severity="info",
            message=f"The managed {pin.PACKAGE} {current.version} belongs to the old pin.",
            location=current.path,
            expected="whatever the new core pins",
            actual=f"{pin.PACKAGE}=={pin.VERSION}",
            recovery="Run `deckflow env setup` after the update to acquire the new pin.",
        )
    )


def run_skill(options: Any) -> tuple[Envelope, str | None, int]:
    """Report, never write.

    Core does not update a skill directory, for four reasons that all have to
    be answered before it could: the distribution channel is not declared
    anywhere, ownership would become a cycle (the skill installs core, core
    rewrites the skill), a rewrite would clobber themes and references the user
    edited, and a self-updating skill is remote code execution on the next
    agent run.  So this reports the version and hands back the host's command.
    """
    envelope = Envelope(command="update skill")
    skill, diagnostics = skill_probe.probe(options.skill_root)
    envelope.extend(diagnostics)
    envelope.extra["skill"] = skill

    if skill is None:
        envelope.add(
            Diagnostic(
                rule_id="SKILL_NOT_DECLARED",
                severity="info",
                message="No skill was declared, so there is nothing to report on.",
                expected="--skill-root <path> or DECKFLOW_SKILL_ROOT",
                actual="neither is set",
                recovery="Run this from a skill, or pass --skill-root.",
            )
        )
        return envelope, None, EXIT_OK

    command = skill.get("update_command")
    envelope.add(
        Diagnostic(
            rule_id="SKILL_UPDATE_NOT_MANAGED",
            severity="info",
            message=f"{skill.get('name') or 'This skill'} {skill.get('version') or ''}".strip()
                    + " is updated by its host, not by core.",
            location=skill["root"],
            expected="the host's own update path",
            actual="core does not write into a skill directory",
            recovery=command or (
                "Update through whatever installed the skill. To make this machine-readable, "
                f"declare `update.command` in {skill_probe.MANIFEST}."
            ),
        )
    )
    human = f"{skill.get('name')} {skill.get('version')}\nupdate: {command or 'see the skill host'}"
    return envelope, human if options.human else None, EXIT_OK
