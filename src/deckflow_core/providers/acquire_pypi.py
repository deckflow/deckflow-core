"""Acquire a Python provider into core's managed cache.

`pip install --target` is the chosen primitive because it is the only one that
needs no virtualenv, no pipx, and no uv, and is not refused by a PEP 668
externally-managed interpreter — verified on Homebrew Python 3.14, which ships
the `EXTERNALLY-MANAGED` marker.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from ..diagnostics import Diagnostic, summarize_output
from ..exits import EXIT_EXECUTION, CoreError
from .matrix import ProviderSpec

_DIST_INFO_RE = re.compile(r"^(?P<name>.+?)-(?P<version>\d[^-]*)\.dist-info$")


def install_dir(home: Path, spec: ProviderSpec) -> Path:
    return home / "providers" / spec.name / spec.version


def installed_version(target: Path, spec: ProviderSpec) -> str | None:
    """Read the version out of the `*.dist-info` directory name.

    Cheaper and more reliable than importing the package or running its CLI,
    and it works even when the console script's shebang points at a different
    interpreter than the one we would invoke.
    """
    normalized = spec.package.replace("-", "_").lower()
    if not target.is_dir():
        return None
    for entry in target.iterdir():
        match = _DIST_INFO_RE.match(entry.name)
        if match and match.group("name").replace("-", "_").lower() == normalized:
            return match.group("version")
    return None


def invocation(target: Path, spec: ProviderSpec) -> tuple[list[str], dict[str, str]]:
    """Argv and environment for a managed `--target` install.

    Always `python -m <module>` with PYTHONPATH, never the generated console
    script: that script's shebang resolves to the *installing* interpreter and
    runs without PYTHONPATH, so it would fail to import its own package.
    """
    if not spec.module:
        raise CoreError(
            Diagnostic(
                rule_id="PROVIDER_MODULE_UNDECLARED",
                severity="error",
                message=f"The {spec.name} provider declares no importable module.",
                location=spec.package,
                recovery="Add a `module` field to the provider entry in providers.json.",
            ),
            exit_code=EXIT_EXECUTION,
        )
    return [sys.executable, "-m", spec.module], {"PYTHONPATH": str(target)}


def _attempts(spec: ProviderSpec, target: Path) -> list[tuple[str, list[str]]]:
    """Index first, declared source second.

    Ordering it this way means publishing the package to the index later needs
    no code change: the index simply starts succeeding and the fallback stops
    being reached.
    """
    base = [
        sys.executable, "-m", "pip", "install",
        "--target", str(target),
        "--no-input", "--disable-pip-version-check",
    ]
    plan = [(
        spec.index_url or "https://pypi.org/simple",
        [*base, "--index-url", spec.index_url or "https://pypi.org/simple", spec.requirement],
    )]
    if spec.source:
        plan.append((spec.source, [*base, spec.source]))
    return plan


def acquire(spec: ProviderSpec, home: Path, *, timeout: int = 600) -> Path:
    target = install_dir(home, spec)
    _cleanup(target)
    target.mkdir(parents=True, exist_ok=True)

    failures: list[str] = []
    for origin, command in _attempts(spec, target):
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            _cleanup(target)
            raise CoreError(
                Diagnostic(
                    rule_id="PROVIDER_ACQUIRE_TIMEOUT",
                    severity="error",
                    message=f"Acquiring {spec.requirement} from {origin} timed out after {timeout}s.",
                    location=spec.package,
                    recovery=f"Re-run, or install manually: pip install {spec.requirement}",
                ),
                exit_code=EXIT_EXECUTION,
            ) from error
        if completed.returncode == 0:
            break
        failures.append(f"{origin}: {summarize_output(completed.stderr, completed.stdout, limit=2)}")
        target.mkdir(parents=True, exist_ok=True)
    else:
        index = spec.index_url or "https://pypi.org/simple"
        _cleanup(target)
        raise CoreError(
            Diagnostic(
                rule_id="PROVIDER_ACQUIRE_FAILED",
                severity="error",
                message=f"Could not install {spec.requirement} from any declared origin.",
                location=spec.package,
                expected=f"{spec.requirement} installable from {index}" + (
                    f" or {spec.source}" if spec.source else ""
                ),
                actual=" | ".join(failures),
                recovery=(
                    f"Publish {spec.package} {spec.version} to {index}, or declare a reachable "
                    f"`source` for it in the provider matrix."
                ),
            ),
            exit_code=EXIT_EXECUTION,
        )

    found = installed_version(target, spec)
    if found != spec.version:
        _cleanup(target)
        raise CoreError(
            Diagnostic(
                rule_id="PROVIDER_ACQUIRE_UNVERIFIED",
                severity="error",
                message=f"{spec.requirement} installed but could not be verified.",
                location=str(target),
                expected=f"a {spec.package} {spec.version} dist-info directory",
                actual=f"installed version={found}",
                recovery="Remove the directory and retry, or install the provider manually.",
            ),
            exit_code=EXIT_EXECUTION,
        )
    return target


def _cleanup(target: Path) -> None:
    shutil.rmtree(target, ignore_errors=True)
