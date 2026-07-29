"""`deckflow env` — what this machine can do, and how to prepare it.

`env check` is the first line of a Skill's prerequisites, which fixes three of
its properties:

- **no side effects.** It never downloads, never creates a directory, never
  writes a file. Safe to run on every invocation.
- **exit 0 whenever the check itself ran.** It is a report, not an assertion.
  A non-zero exit on the first line of SKILL.md tells an agent the Skill is
  broken, and the agent will then try to "fix" a machine that is fine.
- **facts, not verdicts.** It reports that Node exists, never that PPTX export
  will work — that also depends on registry reachability and the deck's stage
  size, neither of which core knows.

`env setup` is the only subcommand that reaches the network, and `env clean`
touches only the managed extract directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..diagnostics import Diagnostic
from ..envelope import STATUS_PARTIAL, Envelope
from ..exits import EXIT_EXECUTION, EXIT_OK, EXIT_USAGE, CoreError
from ..extract import pin
from ..extract import resolve as extract_resolve
from ..fsutil import remove_tree
from ..home import deckflow_home, extract_root
from ..probe import cloud as cloud_probe
from ..probe import host as host_probe
from ..probe import python_env
from ..probe import runtime as runtime_probe
from ..probe import skill as skill_probe

COMMAND = "env"


def run_check(options: Any) -> tuple[Envelope, str | None, int]:
    home = deckflow_home()
    envelope = Envelope(command="env check")

    extract = extract_resolve.resolve(
        bin_override=options.extract_bin, probe_only=True, home=home,
    )
    envelope.extract = extract.to_json()
    envelope.extend(extract.diagnostics)

    skill, skill_diagnostics = skill_probe.probe(options.skill_root)
    envelope.extend(skill_diagnostics)

    python = python_env.probe()
    if not python["satisfies_requires_python"]:
        envelope.status = STATUS_PARTIAL
        envelope.add(
            Diagnostic(
                rule_id="PYTHON_TOO_OLD",
                severity="error",
                message="This interpreter is below the version core requires.",
                location=python["executable"],
                expected=f"Python {python['requires_python']}",
                actual=f"Python {python['version']}",
                recovery="Run deckflow with a newer python3; on macOS /usr/bin/python3 is 3.9.",
            )
        )

    envelope.extra["env"] = {
        "skill": skill,
        "runtime": runtime_probe.probe(
            skill_root=(skill or {}).get("root"), home=home,
        ),
        "python": python,
        "cloud": cloud_probe.probe(extract),
        "host": host_probe.probe(),
        "home": str(home),
    }

    human = _render_check(envelope.extra["env"], envelope.extract) if options.human else None
    # A report always succeeds at reporting.
    return envelope, human, EXIT_OK


def _render_check(env: dict[str, Any], extract: dict[str, Any]) -> str:
    skill = env["skill"] or {}
    cloud = env["cloud"]
    host = env["host"]
    node = host["node"]
    lines = [
        f"skill    {skill.get('name') or '—'} {skill.get('version') or ''}".rstrip(),
        f"core     {env['runtime']['version']} ({env['runtime']['installation']})",
        f"python   {env['python']['version']} at {env['python']['executable']}",
        f"extract  {extract['status']}"
        + (f" {extract['version']} via {extract['resolution']}" if extract["version"]
           else f" (~{extract['download_mb']}MB on first use)"),
        f"cloud    {_render_cloud(cloud)}",
        f"node     {node.get('version') or '—'}   npx {'yes' if host['npx']['present'] else 'no'}",
        "",
        f"home     {env['home']}",
    ]
    return "\n".join(lines)


def _render_cloud(cloud: dict[str, Any]) -> str:
    if not cloud["available"]:
        return f"unknown ({cloud['reason']})"
    if not cloud["configured"]:
        return "not configured"
    return f"configured via {cloud['credential_source']}"


def run_setup(options: Any) -> tuple[Envelope, str | None, int]:
    home = deckflow_home()
    envelope = Envelope(command="env setup")

    existing = extract_resolve.resolve(
        bin_override=options.extract_bin, probe_only=True, home=home,
    )
    if existing.ready:
        envelope.extract = existing.to_json()
        envelope.add(
            Diagnostic(
                rule_id="EXTRACT_ALREADY_AVAILABLE",
                severity="info",
                message=f"{pin.PACKAGE} is already available; nothing to acquire.",
                location=existing.path,
                expected=pin.REQUIREMENT,
                actual=f"{pin.PACKAGE}@{existing.version} via {existing.resolution}",
                recovery="Force a fresh managed copy with `deckflow env clean` first.",
            )
        )
        human = f"{pin.PACKAGE} already available ({existing.resolution}, {existing.version})"
        return envelope, human if options.human else None, EXIT_OK

    # An explicit `env setup` is the user asking for the download; --offline is
    # the only thing that can still refuse it.
    if extract_resolve.offline_from(options.offline):
        _refuse_offline()

    acquired = extract_resolve.acquire(home=home)
    envelope.extract = acquired.to_json()
    envelope.extend(acquired.diagnostics)
    human = f"installed {pin.PACKAGE}@{acquired.version} -> {acquired.path}"
    return envelope, human if options.human else None, EXIT_OK


def _refuse_offline() -> None:
    raise CoreError(
        Diagnostic(
            rule_id="EXTRACT_MISSING",
            severity="error",
            message=f"{pin.PACKAGE} is not installed and --offline forbids acquiring it.",
            location=pin.PACKAGE,
            expected=f"{pin.REQUIREMENT} available",
            actual="not installed",
            recovery="Re-run `deckflow env setup` without --offline on a machine with network access.",
        ),
        exit_code=EXIT_EXECUTION,
    )


def run_clean(options: Any) -> tuple[Envelope, str | None, int]:
    """Remove the managed extract copy.

    Deliberately narrow. Managed core versions are pruned by `deckflow update`
    after a successful install, not here: a freshly installed core that the
    launcher has not selected yet is indistinguishable from a stale one, and
    deleting it would break the next run.
    """
    home = deckflow_home()
    envelope = Envelope(command="env clean")
    target = extract_root(home)

    if not target.exists():
        envelope.add(
            Diagnostic(
                rule_id="EXTRACT_NOT_IN_CACHE",
                severity="info",
                message="No managed extract install to remove.",
                location=str(target),
                recovery="Nothing to do. An install on PATH is not managed by core.",
            )
        )
        return envelope, "nothing in the managed home" if options.human else None, EXIT_OK

    removed = sorted(entry.name for entry in target.iterdir() if entry.is_dir())
    remove_tree(target)
    envelope.outputs.append({"path": str(target), "removed": True, "versions": removed})
    return envelope, f"removed {target}" if options.human else None, EXIT_OK


_ACTIONS = {"check": run_check, "setup": run_setup, "clean": run_clean}


def run(options: Any) -> tuple[Envelope, str | None, int]:
    # Bare `deckflow env` is the check: it is the only one that is free.
    action = getattr(options, "env_action", None) or "check"
    handler = _ACTIONS.get(action)
    if handler is None:  # pragma: no cover - argparse constrains the choices
        raise CoreError(
            Diagnostic(
                rule_id="CLI_USAGE",
                severity="error",
                message=f"Unknown env action: {action}.",
                expected=" | ".join(_ACTIONS),
                actual=str(action),
                recovery="Run `deckflow env --help`.",
            ),
            exit_code=EXIT_USAGE,
        )
    return handler(options)


def managed_extract_path(home: Path | None = None) -> Path:
    return extract_root(home)
