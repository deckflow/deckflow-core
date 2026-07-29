"""Install a pinned Python package into a managed directory.

`pip install --target` is the chosen primitive because it is the only one that
needs no virtualenv, no pipx, and no uv, and is not refused by a PEP 668
externally-managed interpreter — verified on Homebrew Python 3.14, which ships
the `EXTERNALLY-MANAGED` marker and rejects a plain `pip install` outright.

Written against a package name rather than a provider object because two
callers need it: acquiring `deckflow-extract`, and `deckflow update` installing
a newer `deckflow-core` beside the running one.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

from ..diagnostics import Diagnostic, summarize_output
from ..exits import EXIT_EXECUTION, CoreError

_DIST_INFO_RE = re.compile(r"^(?P<name>.+?)-(?P<version>\d[^-]*)\.dist-info$")


def _installer() -> tuple[list[str], list[str]]:
    """Choose an installer without modifying the running environment.

    ``uv tool install`` deliberately creates a tool environment without pip.
    In that environment ``sys.executable -m pip`` cannot acquire extract or a
    newer core, even though the uv executable that created the environment is
    available. Prefer pip when present; otherwise use uv's compatible
    ``pip install --target`` surface.
    """
    if importlib.util.find_spec("pip") is not None:
        return [sys.executable, "-m", "pip"], [
            "--no-input",
            "--disable-pip-version-check",
        ]
    uv = shutil.which("uv")
    if uv:
        return [uv, "pip"], []
    # Keep the familiar pip error for the final diagnostic when neither is
    # available; install() captures it and returns a structured failure.
    return [sys.executable, "-m", "pip"], [
        "--no-input",
        "--disable-pip-version-check",
    ]


def installed_version(target: Path, package: str) -> str | None:
    """Read the version out of the `*.dist-info` directory name.

    Cheaper and more reliable than importing the package or running its CLI,
    and it works even when the console script's shebang points at a different
    interpreter than the one we would invoke.
    """
    normalized = package.replace("-", "_").lower()
    if not target.is_dir():
        return None
    for entry in target.iterdir():
        match = _DIST_INFO_RE.match(entry.name)
        if match and match.group("name").replace("-", "_").lower() == normalized:
            return match.group("version")
    return None


def invocation(target: Path, module: str) -> tuple[list[str], dict[str, str]]:
    """Argv and environment for a managed `--target` install.

    Always `python -m <module>` with PYTHONPATH, never the generated console
    script: that script's shebang resolves to the *installing* interpreter and
    runs without PYTHONPATH, so it would fail to import its own package.
    """
    return [sys.executable, "-m", module], {"PYTHONPATH": str(target)}


def _attempts(package: str, version: str, target: Path,
              index_url: str, source: str | None) -> list[tuple[str, list[str]]]:
    installer, installer_options = _installer()
    base = [
        *installer, "install",
        "--target", str(target),
        *installer_options,
    ]
    plan = [(index_url, [*base, "--index-url", index_url, f"{package}=={version}"])]
    if source:
        plan.append((source, [*base, source]))
    return plan


def install(*, package: str, version: str, target: Path,
            index_url: str, source: str | None = None, timeout: int = 600) -> Path:
    """Fetch `package==version` into `target`, verified, or raise.

    The target is cleared first and on every failure path: a half-installed
    directory that still looks resolvable is worse than an absent one.
    """
    requirement = f"{package}=={version}"
    _cleanup(target)
    target.mkdir(parents=True, exist_ok=True)

    failures: list[str] = []
    for origin, command in _attempts(package, version, target, index_url, source):
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            _cleanup(target)
            raise CoreError(
                Diagnostic(
                    rule_id="INSTALL_TIMEOUT",
                    severity="error",
                    message=f"Installing {requirement} from {origin} timed out after {timeout}s.",
                    location=package,
                    recovery=f"Re-run, or install manually: pip install --target {target} {requirement}",
                ),
                exit_code=EXIT_EXECUTION,
            ) from error
        if completed.returncode == 0:
            break
        failures.append(f"{origin}: {summarize_output(completed.stderr, completed.stdout, limit=2)}")
        target.mkdir(parents=True, exist_ok=True)
    else:
        _cleanup(target)
        raise CoreError(
            Diagnostic(
                rule_id="INSTALL_FAILED",
                severity="error",
                message=f"Could not install {requirement} from any declared origin.",
                location=package,
                expected=f"{requirement} installable from {index_url}"
                         + (f" or {source}" if source else ""),
                actual=" | ".join(failures),
                recovery=(
                    f"Check network access to {index_url}. If {package} {version} is not "
                    "published yet, this core cannot acquire it."
                ),
            ),
            exit_code=EXIT_EXECUTION,
        )

    found = installed_version(target, package)
    if found != version:
        _cleanup(target)
        raise CoreError(
            Diagnostic(
                rule_id="INSTALL_UNVERIFIED",
                severity="error",
                message=f"{requirement} installed but could not be verified.",
                location=str(target),
                expected=f"a {package} {version} dist-info directory",
                actual=f"installed version={found}",
                recovery="Run `deckflow env clean`, then retry.",
            ),
            exit_code=EXIT_EXECUTION,
        )
    return target


def _cleanup(target: Path) -> None:
    shutil.rmtree(target, ignore_errors=True)


__all__ = ["install", "installed_version", "invocation"]
