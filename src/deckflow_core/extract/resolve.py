"""The resolution ladder for `deckflow-extract`.

Order is fixed: explicit override, then whatever the environment already has,
then core's managed copy, then acquisition, then a structured failure.  Every
run reports which rung it landed on, so a report can always answer "where did
this come from, and did this run download anything".

The provider is a Python package, so there is no runtime to be missing: it is
either present, acquirable, or refused because the caller asked for `--offline`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import home as home_mod
from .. import versions
from ..diagnostics import Diagnostic
from ..exits import EXIT_EXECUTION, CoreError
from . import install as installer
from . import pin

RESOLUTION_OVERRIDE = "override"
RESOLUTION_AMBIENT = "ambient"
RESOLUTION_MANAGED = "managed"
RESOLUTION_ACQUIRED = "acquired"
RESOLUTION_MISSING = "missing"

STATUS_READY = "ready"
STATUS_NOT_ACQUIRED = "not-acquired"


@dataclass
class Extract:
    """Where the provider came from, and how to run it."""

    resolution: str = RESOLUTION_MISSING
    status: str = STATUS_NOT_ACQUIRED
    version: str | None = None
    command: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    path: str | None = None
    acquired: bool = False
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.status == STATUS_READY

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "version": self.version,
            "pinned_version": pin.VERSION,
            "package": pin.PACKAGE,
            "resolution": self.resolution,
            "acquired": self.acquired,
            "download_mb": pin.DOWNLOAD_MB,
        }
        if self.path:
            payload["path"] = self.path
        return payload


def offline_from(flag: bool | None) -> bool:
    """`--offline`, or `DECKFLOW_OFFLINE` in the environment.

    Replaces the old three-way auto/ask/never policy.  `ask` degraded to
    `never` without a TTY, which is every agent invocation, and a 4MB download
    does not earn an interactive prompt — so the only decision left is whether
    acquisition is permitted at all.
    """
    if flag:
        return True
    value = (os.environ.get("DECKFLOW_OFFLINE") or "").strip().lower()
    return value not in ("", "0", "false", "no")


def _probe_version(command: list[str], env: dict[str, str] | None = None) -> str | None:
    merged = {**os.environ, **(env or {})}
    try:
        completed = subprocess.run(
            [*command, "--version"], capture_output=True, text=True, timeout=30, env=merged
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return versions.normalize(f"{completed.stdout}\n{completed.stderr}")


def _override_path(explicit: str | None) -> str | None:
    return explicit or os.environ.get("DECKFLOW_EXTRACT_BIN")


def _managed(home: Path) -> Extract | None:
    """A previously verified install in core's own home."""
    target = home_mod.extract_dir(pin.VERSION, home)
    found = installer.installed_version(target, pin.PACKAGE)
    if found == pin.VERSION:
        command, env = installer.invocation(target, pin.MODULE)
        return Extract(
            resolution=RESOLUTION_MANAGED, status=STATUS_READY,
            version=found, command=command, env=env, path=str(target),
        )
    return None


def _ambient() -> Extract | None:
    """Something already on PATH that satisfies the compatible range.

    An incompatible ambient version is not an error — core falls through to its
    own managed copy and records a warning, so a user's global install can
    never quietly change what a pinned run executes.
    """
    binary = shutil.which(pin.BIN)
    if binary is None:
        return None
    found = _probe_version([binary])
    resolved = Extract(
        resolution=RESOLUTION_AMBIENT, version=found, command=[binary], path=binary,
    )
    if versions.satisfies(found, pin.COMPATIBLE):
        resolved.status = STATUS_READY
        return resolved
    resolved.status = STATUS_NOT_ACQUIRED
    resolved.diagnostics.append(
        Diagnostic(
            rule_id="EXTRACT_VERSION_MISMATCH",
            severity="warning",
            message=f"Ignoring the {pin.BIN} on PATH: its version is outside the pinned range.",
            location=binary,
            expected=f"{pin.PACKAGE} {pin.COMPATIBLE}",
            actual=str(found),
            recovery=f"Core will use its own pinned {pin.REQUIREMENT}; no action needed.",
        )
    )
    return resolved


def resolve(
    *,
    bin_override: str | None = None,
    offline: bool = False,
    probe_only: bool = False,
    home: Path | None = None,
) -> Extract:
    """Walk the ladder. `probe_only` reports status without side effects."""
    home = home or home_mod.deckflow_home()

    override = _override_path(bin_override)
    if override:
        return _from_override(override)

    ambient = _ambient()
    carried: list[Diagnostic] = []
    if ambient is not None:
        if ambient.ready:
            return ambient
        carried.extend(ambient.diagnostics)

    managed = _managed(home)
    if managed is not None:
        managed.diagnostics.extend(carried)
        return managed

    if probe_only:
        return Extract(
            resolution=RESOLUTION_MISSING, status=STATUS_NOT_ACQUIRED, diagnostics=carried,
        )

    if offline:
        raise CoreError(
            Diagnostic(
                rule_id="EXTRACT_MISSING",
                severity="error",
                message=f"{pin.PACKAGE} is not installed and --offline forbids acquiring it.",
                location=pin.PACKAGE,
                expected=f"{pin.REQUIREMENT} available",
                actual="not installed",
                recovery="Run `deckflow env setup` on a machine with network access, or drop --offline.",
            ),
            exit_code=EXIT_EXECUTION,
        )

    return acquire(home=home, carried=carried)


def _from_override(override: str) -> Extract:
    """Use an explicit executable, for developing core and extract together.

    Kept after the rest of the provider machinery was deleted because the
    ambient rung cannot serve this case: a development build reports a version
    outside the compatible range, which is exactly what that rung refuses.
    """
    path = Path(override)
    if not path.exists():
        raise CoreError(
            Diagnostic(
                rule_id="EXTRACT_OVERRIDE_MISSING",
                severity="error",
                message="The --extract-bin override path does not exist.",
                location=override,
                expected="an existing executable",
                actual="no such file",
                recovery="Correct --extract-bin <path>, or unset DECKFLOW_EXTRACT_BIN.",
            ),
            exit_code=EXIT_EXECUTION,
        )
    found = _probe_version([str(path)])
    carried: list[Diagnostic] = []
    if found != pin.VERSION:
        carried.append(
            Diagnostic(
                rule_id="EXTRACT_OVERRIDE_VERSION_MISMATCH",
                severity="warning",
                message="The explicit override does not report the pinned version.",
                location=str(path),
                expected=pin.VERSION,
                actual=str(found),
                recovery=(
                    "Keep this override for development only; release verification should "
                    "run against the pinned version."
                ),
            )
        )
    return Extract(
        resolution=RESOLUTION_OVERRIDE, status=STATUS_READY,
        version=found, command=[str(path)], path=str(path), diagnostics=carried,
    )


def acquire(*, home: Path | None = None, carried: list[Diagnostic] | None = None) -> Extract:
    """Fetch the pinned version, then re-resolve it out of the managed home."""
    home = home or home_mod.deckflow_home()
    installer.install(
        package=pin.PACKAGE,
        version=pin.VERSION,
        target=home_mod.extract_dir(pin.VERSION, home),
        index_url=pin.INDEX_URL,
        source=pin.SOURCE,
    )

    resolved = _managed(home)
    if resolved is None:  # pragma: no cover - install() verifies before returning
        raise CoreError(
            Diagnostic(
                rule_id="EXTRACT_ACQUIRE_UNVERIFIED",
                severity="error",
                message=f"{pin.REQUIREMENT} was installed but is still not resolvable.",
                location=pin.PACKAGE,
                recovery="Run `deckflow env clean`, then retry.",
            ),
            exit_code=EXIT_EXECUTION,
        )
    resolved.resolution = RESOLUTION_ACQUIRED
    resolved.acquired = True
    resolved.diagnostics.extend(carried or [])
    resolved.diagnostics.append(
        Diagnostic(
            rule_id="EXTRACT_ACQUIRED",
            severity="info",
            message=f"Acquired {pin.PACKAGE} {resolved.version} (~{pin.DOWNLOAD_MB}MB).",
            location=resolved.path,
            expected=pin.REQUIREMENT,
            actual=f"{pin.PACKAGE}=={resolved.version}",
            recovery="Remove it with `deckflow env clean`.",
        )
    )
    return resolved


__all__ = [
    "Extract", "resolve", "acquire", "offline_from",
    "STATUS_READY", "STATUS_NOT_ACQUIRED",
    "RESOLUTION_OVERRIDE", "RESOLUTION_AMBIENT", "RESOLUTION_MANAGED",
    "RESOLUTION_ACQUIRED", "RESOLUTION_MISSING",
]
