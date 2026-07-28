"""`deckflow providers` — the observable surface of the lazy-provider contract.

Not a general health check: it answers exactly one question, "what will this
core run, and will using it download anything".  An agent needs that answer
*before* starting a long job, and `check_environment.py` needs something to
call.  The name is deliberately narrower than `doctor`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..diagnostics import Diagnostic
from ..envelope import STATUS_FAILED, Envelope
from ..exits import EXIT_OK, EXIT_USAGE, CoreError
from ..fsutil import deckflow_home, remove_tree
from ..providers import matrix, resolve as resolver

COMMAND = "providers"


def _listing(options: Any, home: Path) -> list[resolver.Resolution]:
    specs = matrix.load_matrix(options.provider_specs)
    return [
        resolver.resolve(
            spec,
            policy=resolver.POLICY_NEVER,
            bin_overrides=options.provider_bins,
            home=home,
            probe_only=True,
        )
        for _, spec in sorted(specs.items())
    ]


def _render_table(resolutions: list[resolver.Resolution], home: Path) -> str:
    header = f"{'provider':<10} {'pinned':<9} {'resolution':<15} {'status':<13} {'public':<8} unlocks"
    lines = [header, "-" * len(header)]
    for item in resolutions:
        found = item.version or ""
        resolution = item.resolution
        if resolution == resolver.RESOLUTION_AMBIENT and found:
            resolution = f"ambient {found}"
        elif resolution == resolver.RESOLUTION_MANAGED and found:
            resolution = f"cache {found}"
        elif resolution == resolver.RESOLUTION_MISSING:
            resolution = "—"
        status = item.status
        if item.blocked_by:
            status = f"{status} ({item.blocked_by})"
        unlocks = ", ".join(item.spec.unlocks)
        note = f"  (~{item.spec.approx_mb}MB on first use)" if item.status == resolver.STATUS_NOT_ACQUIRED else ""
        public = "yes" if item.spec.public else "ORG-ONLY"
        lines.append(
            f"{item.spec.name:<10} {item.spec.version:<9} {resolution:<15} "
            f"{status:<13} {public:<8} {unlocks:<14}{note}"
        )
    lines.append("")
    lines.append(f"managed cache: {home}/providers")
    lines.append("acquisition writes only there; it never installs globally.")
    if any(not item.spec.public for item in resolutions):
        lines.append("")
        lines.append(
            "ORG-ONLY providers are not on a public registry yet; an integrator outside "
            "the organisation cannot acquire them."
        )
    return "\n".join(lines)


def run_list(options: Any) -> tuple[Envelope, str | None]:
    home = deckflow_home()
    resolutions = _listing(options, home)
    envelope = Envelope(command=COMMAND)
    envelope.providers = [item.to_json() for item in resolutions]
    for item in resolutions:
        envelope.extend(item.diagnostics)
    envelope.extra["managed_cache"] = str(home / "providers")
    envelope.extra["provider_install_policy"] = options.provider_install or "auto"
    human = None if options.json else _render_table(resolutions, home)
    return envelope, human


def run_install(options: Any, name: str) -> tuple[Envelope, str | None]:
    home = deckflow_home()
    spec = matrix.get(name, options.provider_specs)
    envelope = Envelope(command=f"{COMMAND}.install")

    existing = resolver.resolve(
        spec, policy=resolver.POLICY_NEVER, bin_overrides=options.provider_bins,
        home=home, probe_only=True,
    )
    if existing.ready:
        envelope.providers = [existing.to_json()]
        envelope.add(
            Diagnostic(
                rule_id="PROVIDER_ALREADY_AVAILABLE",
                severity="info",
                message=f"{spec.name} is already available; nothing to acquire.",
                location=existing.path,
                expected=spec.requirement,
                actual=f"{spec.package}@{existing.version} via {existing.resolution}",
                recovery=f"Force a managed copy by removing it first: deckflow providers remove {spec.name}.",
            )
        )
        human = None if options.json else f"{spec.name} already available ({existing.resolution}, {existing.version})"
        return envelope, human

    # An explicit `providers install` is the user asking for it; the acquisition
    # policy governs implicit installs during a business command, not this.
    resolution = resolver.acquire(spec, home=home)
    envelope.providers = [resolution.to_json()]
    envelope.extend(resolution.diagnostics)
    human = None if options.json else f"installed {spec.package}@{resolution.version} -> {resolution.path}"
    return envelope, human


def run_remove(options: Any, name: str) -> tuple[Envelope, str | None]:
    home = deckflow_home()
    spec = matrix.get(name, options.provider_specs)
    envelope = Envelope(command=f"{COMMAND}.remove")
    target = home / "providers" / spec.name

    if not target.exists():
        envelope.add(
            Diagnostic(
                rule_id="PROVIDER_NOT_IN_CACHE",
                severity="info",
                message=f"No managed install of {spec.name} to remove.",
                location=str(target),
                recovery="Nothing to do. An ambient install on PATH is not managed by core.",
            )
        )
        return envelope, None if options.json else f"{spec.name}: nothing in the managed cache"

    remove_tree(target)
    envelope.outputs.append({"path": str(target), "removed": True})
    return envelope, None if options.json else f"removed {target}"


def run(options: Any) -> tuple[Envelope, str | None, int]:
    action = options.provider_action
    if action is None:
        envelope, human = run_list(options)
    elif action == "install":
        envelope, human = run_install(options, options.provider_name)
    elif action == "remove":
        envelope, human = run_remove(options, options.provider_name)
    else:  # pragma: no cover - argparse constrains the choices
        raise CoreError(
            Diagnostic(
                rule_id="CLI_USAGE",
                severity="error",
                message=f"Unknown providers action: {action}.",
                expected="install | remove | (none)",
                actual=str(action),
                recovery="Run `deckflow providers --help`.",
            ),
            exit_code=EXIT_USAGE,
        )
    exit_code = EXIT_OK if envelope.status != STATUS_FAILED else 5
    return envelope, human, exit_code
