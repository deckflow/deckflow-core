"""Acquire an npm provider into core's managed cache.

The explicit `--registry` / `--@scope:registry` pair is not belt-and-braces: a
user `.npmrc` that maps `@deckflow` to another registry will otherwise silently
resolve a different — or missing — version of the pinned package.  That failure
was reproduced on a real machine before this module existed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from ..diagnostics import Diagnostic, summarize_output
from ..exits import EXIT_EXECUTION, CoreError
from .matrix import ProviderSpec

_DEFAULT_REGISTRY = "https://registry.npmjs.org/"


def _npm_recovery(spec: ProviderSpec, detail: str) -> str:
    """Say what actually went wrong instead of "check your network".

    A 404 on a scoped package almost always means the version is not published
    to the declared registry — a different problem from being offline, with a
    different fix.
    """
    registry = spec.registry or _DEFAULT_REGISTRY
    lowered = detail.lower()
    if "e404" in lowered or "404" in lowered:
        return (
            f"{spec.requirement} was not found on {registry}. "
            f"Confirm that version is published there (`npm view {spec.package} versions "
            f"--registry={registry}`); a scope mapping in your .npmrc cannot cause this, "
            "because core always passes the registry explicitly."
        )
    if "e401" in lowered or "e403" in lowered or "auth" in lowered:
        return (
            f"{registry} refused the request for {spec.package}. "
            "If the package is published to a registry that requires a token, it cannot be "
            "acquired on demand; publish it to the public registry instead."
        )
    return f"Check network access to {registry}, or install manually: npm install -g {spec.requirement}"


def npm_executable() -> str | None:
    return shutil.which("npm")


def node_available() -> bool:
    return npm_executable() is not None


def scope_of(package: str) -> str | None:
    return package.split("/", 1)[0] if package.startswith("@") else None


def install_dir(home: Path, spec: ProviderSpec) -> Path:
    return home / "providers" / spec.name / spec.version


def bin_path(prefix: Path, spec: ProviderSpec) -> Path:
    return prefix / "node_modules" / ".bin" / spec.bin


def installed_version(prefix: Path, spec: ProviderSpec) -> str | None:
    """Read the version from the installed package manifest.

    Reading `package.json` beats running `<bin> --version`: it costs no process
    spawn, and it still answers correctly for a package whose CLI does not
    implement `--version`.
    """
    manifest = prefix / "node_modules" / Path(spec.package) / "package.json"
    try:
        return json.loads(manifest.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError):
        return None


def registry_args(spec: ProviderSpec, registry: str | None = None) -> list[str]:
    """Pin the default registry and the package's scope separately.

    Both halves matter, for different reasons:

    - the default registry is pinned to npmjs so a scope mapping in the user's
      .npmrc cannot hijack the pin, and so transitive dependencies keep
      resolving where they actually live;
    - the scope is pinned to wherever *this* package is published, which is not
      always npmjs.

    Collapsing these into one `--registry` breaks any provider published
    outside npmjs: its ordinary dependencies get looked up on that registry too
    and 404.
    """
    home = registry or spec.registry or _DEFAULT_REGISTRY
    scope = scope_of(spec.package)
    if not scope:
        return [f"--registry={home}"]
    return [f"--registry={_DEFAULT_REGISTRY}", f"--{scope}:registry={home}"]


def registries(spec: ProviderSpec) -> list[str]:
    """Primary registry first, then the declared fallback.

    The matrix names the *intended public* registry first even before the
    package is published there. Once it is, the primary starts succeeding and
    the fallback simply stops being reached — no code change, no pin change.
    """
    primary = spec.registry or _DEFAULT_REGISTRY
    ordered = [primary]
    if spec.fallback_registry and spec.fallback_registry != primary:
        ordered.append(spec.fallback_registry)
    return ordered


def origins(spec: ProviderSpec) -> list[tuple[str, str]]:
    """Every place this package can be installed from, in order.

    A `source` — a git spec npm understands — comes last: it is the escape
    hatch for a package not yet on its intended registry, and it costs a clone,
    so it must never win over a registry that would have worked.
    """
    ordered = [(registry, spec.requirement) for registry in registries(spec)]
    if spec.source:
        ordered.append((spec.source, spec.source))
    return ordered


def is_registry(origin: str) -> bool:
    return origin.startswith("http://") or origin.startswith("https://")


def acquire(spec: ProviderSpec, home: Path, *, timeout: int = 600) -> Path:
    """Install the pinned version into `$DECKFLOW_HOME/providers/<name>/<version>`.

    Never writes outside that directory: no global install, no touching the
    caller's project `node_modules`.
    """
    npm = npm_executable()
    if npm is None:
        raise CoreError(
            Diagnostic(
                rule_id="PROVIDER_RUNTIME_MISSING",
                severity="error",
                message=f"Node.js/npm is required to acquire the {spec.name} provider.",
                location=spec.package,
                expected="npm on PATH",
                actual="npm not found",
                recovery="Install Node.js 22 or newer, then re-run. HTML-only workflows do not need it.",
            ),
            exit_code=EXIT_EXECUTION,
        )

    prefix = install_dir(home, spec)
    _cleanup(prefix)
    prefix.mkdir(parents=True, exist_ok=True)
    # npm refuses to treat a bare directory as an install prefix without one.
    manifest = prefix / "package.json"
    if not manifest.exists():
        manifest.write_text(
            json.dumps({"name": f"deckflow-provider-{spec.name}", "version": "0.0.0", "private": True}) + "\n",
            encoding="utf-8",
        )

    failures: list[str] = []
    for origin, requirement in origins(spec):
        # A git origin is not a registry: pin the scope only for registry
        # installs, and let a git spec resolve its dependencies from npmjs.
        args = (
            registry_args(spec, origin) if is_registry(origin)
            else [f"--registry={_DEFAULT_REGISTRY}"]
        )
        command = [
            npm, "install",
            "--prefix", str(prefix),
            *args,
            "--no-audit", "--no-fund", "--omit=dev",
            requirement,
        ]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            _cleanup(prefix)
            raise CoreError(
                Diagnostic(
                    rule_id="PROVIDER_ACQUIRE_TIMEOUT",
                    severity="error",
                    message=f"Acquiring {spec.requirement} from {origin} timed out after {timeout}s.",
                    location=spec.package,
                    recovery=f"Re-run, or install manually: npm install -g {spec.requirement}",
                ),
                exit_code=EXIT_EXECUTION,
            ) from error
        if completed.returncode == 0:
            break
        failures.append(f"{origin}: {summarize_output(completed.stderr, completed.stdout, limit=2)}")
    else:
        detail = " | ".join(failures)
        _cleanup(prefix)
        raise CoreError(
            Diagnostic(
                rule_id="PROVIDER_ACQUIRE_FAILED",
                severity="error",
                message=f"Could not install {spec.requirement} from any declared origin.",
                location=spec.package,
                expected=f"{spec.requirement} available on "
                         f"{' or '.join(origin for origin, _ in origins(spec))}",
                actual=detail,
                recovery=_npm_recovery(spec, detail),
            ),
            exit_code=EXIT_EXECUTION,
        )

    # Verify before it counts as installed: a half-installed provider that
    # still resolves is worse than one that is plainly absent.
    found = installed_version(prefix, spec)
    if found != spec.version or not bin_path(prefix, spec).exists():
        _cleanup(prefix)
        raise CoreError(
            Diagnostic(
                rule_id="PROVIDER_ACQUIRE_UNVERIFIED",
                severity="error",
                message=f"{spec.requirement} installed but could not be verified.",
                location=str(prefix),
                expected=f"{spec.bin} executable and exact version {spec.version}",
                actual=f"version={found}, bin_exists={bin_path(prefix, spec).exists()}",
                recovery="Remove the directory and retry, or install the provider manually.",
            ),
            exit_code=EXIT_EXECUTION,
        )
    return prefix


def _cleanup(prefix: Path) -> None:
    shutil.rmtree(prefix, ignore_errors=True)
