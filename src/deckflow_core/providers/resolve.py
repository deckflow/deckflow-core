"""The provider resolution ladder.

Order is fixed: explicit override, then whatever the environment already has,
then core's managed cache, then acquisition, then a structured failure.  Every
run reports which rung it landed on, so a report can always answer "where did
this binary come from, and did this run download anything".
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import versions
from ..diagnostics import Diagnostic
from ..exits import EXIT_EXECUTION, CoreError
from ..fsutil import deckflow_home
from . import acquire_npm, acquire_pypi
from .matrix import ProviderSpec

POLICY_AUTO = "auto"
POLICY_ASK = "ask"
POLICY_NEVER = "never"
POLICIES = (POLICY_AUTO, POLICY_ASK, POLICY_NEVER)

RESOLUTION_OVERRIDE = "override"
RESOLUTION_AMBIENT = "ambient"
RESOLUTION_MANAGED = "managed-cache"
RESOLUTION_ACQUIRED = "acquired"
RESOLUTION_MISSING = "missing"

STATUS_READY = "ready"
STATUS_NOT_ACQUIRED = "not-acquired"
STATUS_BLOCKED = "blocked"


@dataclass
class Resolution:
    """Where a provider came from, and how to run it."""

    spec: ProviderSpec
    resolution: str = RESOLUTION_MISSING
    status: str = STATUS_NOT_ACQUIRED
    version: str | None = None
    command: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    path: str | None = None
    acquired: bool = False
    blocked_by: str | None = None
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.status == STATUS_READY

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.spec.name,
            "package": self.spec.package,
            "pinned_version": self.spec.version,
            "version": self.version,
            "resolution": self.resolution,
            "status": self.status,
            "acquired": self.acquired,
            "pinned": self.spec.pinned,
            "public": self.spec.public,
            "unlocks": list(self.spec.unlocks),
            "approx_mb": self.spec.approx_mb,
        }
        if self.path:
            payload["path"] = self.path
        if self.blocked_by:
            payload["blocked_by"] = self.blocked_by
        return payload


def policy_from(value: str | None) -> str:
    resolved = value or os.environ.get("DECKFLOW_PROVIDER_INSTALL") or POLICY_AUTO
    if resolved not in POLICIES:
        raise CoreError(
            Diagnostic(
                rule_id="CLI_USAGE",
                severity="error",
                message=f"Unknown --provider-install policy: {resolved}.",
                expected=" | ".join(POLICIES),
                actual=resolved,
                recovery="Pass one of: auto, ask, never.",
            ),
            exit_code=2,
        )
    return resolved


def _bin_override(spec: ProviderSpec, overrides: dict[str, str] | None) -> str | None:
    if overrides and spec.name in overrides:
        return overrides[spec.name]
    return os.environ.get(f"DECKFLOW_{spec.name.upper()}_BIN")


def _probe_version(command: list[str], env: dict[str, str] | None = None) -> str | None:
    merged = {**os.environ, **(env or {})}
    try:
        completed = subprocess.run(
            [*command, "--version"], capture_output=True, text=True, timeout=30, env=merged
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return versions.normalize(f"{completed.stdout}\n{completed.stderr}")


def _managed(spec: ProviderSpec, home: Path) -> Resolution | None:
    """A previously verified install in core's own cache."""
    if spec.kind == "npm":
        prefix = acquire_npm.install_dir(home, spec)
        found = acquire_npm.installed_version(prefix, spec)
        binary = acquire_npm.bin_path(prefix, spec)
        if found and binary.exists():
            return Resolution(
                spec=spec, resolution=RESOLUTION_MANAGED, status=STATUS_READY,
                version=found, command=[str(binary)], path=str(binary),
            )
        return None
    target = acquire_pypi.install_dir(home, spec)
    found = acquire_pypi.installed_version(target, spec)
    if found:
        command, env = acquire_pypi.invocation(target, spec)
        return Resolution(
            spec=spec, resolution=RESOLUTION_MANAGED, status=STATUS_READY,
            version=found, command=command, env=env, path=str(target),
        )
    return None


def _ambient(spec: ProviderSpec) -> Resolution | None:
    """Something already on PATH that satisfies the compatible range.

    An incompatible ambient version is not an error — core falls through to its
    own managed copy and records a warning, so a user's global install can
    never quietly change what a pinned run executes.
    """
    binary = shutil.which(spec.bin)
    if binary is None:
        return None
    found = _probe_version([binary])
    resolution = Resolution(
        spec=spec, resolution=RESOLUTION_AMBIENT, version=found,
        command=[binary], path=binary,
    )
    if versions.satisfies(found, spec.compatible):
        resolution.status = STATUS_READY
        return resolution
    resolution.status = STATUS_NOT_ACQUIRED
    resolution.diagnostics.append(
        Diagnostic(
            rule_id="PROVIDER_VERSION_MISMATCH",
            severity="warning",
            message=f"Ignoring the {spec.name} on PATH: its version is outside the pinned range.",
            location=binary,
            expected=f"{spec.package} {spec.compatible}",
            actual=str(found),
            recovery=f"Core will use its own pinned {spec.requirement}; no action needed.",
        )
    )
    return resolution


def _may_acquire(policy: str) -> bool:
    if policy == POLICY_AUTO:
        return True
    if policy == POLICY_NEVER:
        return False
    # `ask` cannot block an agent: without a TTY it degrades to `never`.
    return sys.stdin.isatty() and sys.stderr.isatty()


def _confirm(spec: ProviderSpec) -> bool:
    sys.stderr.write(
        f"[deckflow] {spec.name} is not installed. "
        f"Acquire {spec.requirement} (~{spec.approx_mb}MB) into the managed cache? [y/N] "
    )
    sys.stderr.flush()
    try:
        return sys.stdin.readline().strip().lower() in {"y", "yes"}
    except (OSError, KeyboardInterrupt):
        return False


def _publication_warning(spec: ProviderSpec) -> Diagnostic:
    """Flag a provider that only organisation members can acquire.

    Worth a first-class diagnostic rather than a README footnote: the Skill is
    integrated from outside the organisation, so "it worked on my machine" here
    means "my credentials worked", not "this is reachable".
    """
    where = spec.fallback_registry or spec.source or "an organisation-internal location"
    target = spec.registry or spec.index_url or "a public registry"
    return Diagnostic(
        rule_id="PROVIDER_NOT_PUBLICLY_PUBLISHED",
        severity="warning",
        message=f"{spec.package} {spec.version} is not yet available on a public registry.",
        location=spec.package,
        expected=f"{spec.package} {spec.version} on {target}",
        actual=f"reachable only via {where}, which requires organisation credentials",
        recovery=(
            f"Publish {spec.package} {spec.version} to {target}. Until then, anyone outside "
            "the organisation will fail to acquire this provider."
        ),
    )


def resolve(
    spec: ProviderSpec,
    *,
    policy: str = POLICY_AUTO,
    bin_overrides: dict[str, str] | None = None,
    home: Path | None = None,
    probe_only: bool = False,
) -> Resolution:
    """Walk the ladder. `probe_only` reports status without side effects."""
    home = home or deckflow_home()
    carried: list[Diagnostic] = []
    if not spec.public:
        carried.append(_publication_warning(spec))

    override = _bin_override(spec, bin_overrides)
    if override:
        path = Path(override)
        if not path.exists():
            raise CoreError(
                Diagnostic(
                    rule_id="PROVIDER_OVERRIDE_MISSING",
                    severity="error",
                    message=f"The {spec.name} override path does not exist.",
                    location=override,
                    expected="an existing executable",
                    actual="no such file",
                    recovery=f"Correct --provider-bin {spec.name}=<path> or unset DECKFLOW_{spec.name.upper()}_BIN.",
                ),
                exit_code=EXIT_EXECUTION,
            )
        return Resolution(
            spec=spec, resolution=RESOLUTION_OVERRIDE, status=STATUS_READY,
            version=_probe_version([str(path)]), command=[str(path)], path=str(path),
            diagnostics=carried,
        )

    ambient = _ambient(spec)
    if ambient is not None:
        if ambient.ready:
            ambient.diagnostics.extend(carried)
            return ambient
        carried.extend(ambient.diagnostics)

    managed = _managed(spec, home)
    if managed is not None:
        managed.diagnostics.extend(carried)
        return managed

    # Nothing usable yet. An npm provider on a machine without Node is blocked
    # rather than merely absent — no policy can fix it.
    if spec.kind == "npm" and not acquire_npm.node_available():
        return Resolution(
            spec=spec, resolution=RESOLUTION_MISSING, status=STATUS_BLOCKED,
            blocked_by="node-runtime-missing", diagnostics=carried,
        )

    if probe_only:
        return Resolution(
            spec=spec, resolution=RESOLUTION_MISSING, status=STATUS_NOT_ACQUIRED,
            diagnostics=carried,
        )

    if not _may_acquire(policy) or (policy == POLICY_ASK and not _confirm(spec)):
        raise CoreError(
            Diagnostic(
                rule_id="PROVIDER_MISSING",
                severity="error",
                message=f"The {spec.name} provider is not installed and acquisition is not permitted.",
                location=spec.package,
                expected=f"{spec.requirement} available",
                actual=f"not installed (--provider-install {policy})",
                recovery=f"Run `deckflow providers install {spec.name}`, or re-run with --provider-install auto.",
            ),
            exit_code=EXIT_EXECUTION,
        )

    return acquire(spec, home=home, carried=carried)


def acquire(spec: ProviderSpec, *, home: Path | None = None,
            carried: list[Diagnostic] | None = None) -> Resolution:
    """Fetch the pinned version, then re-resolve it out of the managed cache."""
    home = home or deckflow_home()
    if spec.kind == "npm":
        acquire_npm.acquire(spec, home)
    else:
        acquire_pypi.acquire(spec, home)

    resolution = _managed(spec, home)
    if resolution is None:  # pragma: no cover - acquire() verifies before returning
        raise CoreError(
            Diagnostic(
                rule_id="PROVIDER_ACQUIRE_UNVERIFIED",
                severity="error",
                message=f"{spec.requirement} was installed but is still not resolvable.",
                location=spec.package,
                recovery="Remove the managed directory and retry.",
            ),
            exit_code=EXIT_EXECUTION,
        )
    resolution.resolution = RESOLUTION_ACQUIRED
    resolution.acquired = True
    resolution.diagnostics.extend(carried or [])
    # `acquire` is a public entry point of its own (`providers install`), so the
    # publication warning has to be attached here too, not only in `resolve`.
    if not spec.public and not any(
        d.rule_id == "PROVIDER_NOT_PUBLICLY_PUBLISHED" for d in resolution.diagnostics
    ):
        resolution.diagnostics.append(_publication_warning(spec))
    resolution.diagnostics.append(
        Diagnostic(
            rule_id="PROVIDER_ACQUIRED",
            severity="info",
            message=f"Acquired {spec.package} {resolution.version} into the managed cache.",
            location=resolution.path,
            expected=spec.requirement,
            actual=f"{spec.package}@{resolution.version}",
            recovery=f"Remove it with `deckflow providers remove {spec.name}`.",
        )
    )
    return resolution
