"""The `~/.deckflow` layout, in one place.

One directory per package core installs, named after the package.  The old
`providers/<name>/<version>` nesting existed to hold N providers; there is one,
so the extra level bought nothing but a word the CLI no longer uses.

    ~/.deckflow/
    ├── core/<version>/       core itself; the launcher runs the newest
    ├── extract/<version>/    core's managed copy of deckflow-extract
    ├── parse/                deckflow-extract's OWN engine sidecars — not ours
    └── credentials           shared with DeckHTML; only extract may write it

`parse/` and `credentials` are listed because they are inside our home and we
must not touch them: `parse/` belongs to the provider's own engine ladder, and
`credentials` is rewritten by two other tools whose merge rules we do not own.
"""

from __future__ import annotations

import os
from pathlib import Path


def deckflow_home() -> Path:
    """Root for core's managed installs, shared with deckflow-extract."""
    override = os.environ.get("DECKFLOW_HOME")
    return Path(override).expanduser() if override else Path.home() / ".deckflow"


def core_root(home: Path | None = None) -> Path:
    return (home or deckflow_home()) / "core"


def core_dir(version: str, home: Path | None = None) -> Path:
    return core_root(home) / version


def extract_root(home: Path | None = None) -> Path:
    return (home or deckflow_home()) / "extract"


def extract_dir(version: str, home: Path | None = None) -> Path:
    return extract_root(home) / version


def credentials_dir(home: Path | None = None) -> Path:
    """The shared DeckHTML/extract credential directory.

    Extract does not know about ``DECKFLOW_HOME``: it accepts its own
    ``DECKFLOW_CONFIG_DIR`` and DeckHTML's equivalent. Core resolves those
    names here and passes the result to every provider invocation so the path
    it reports is always the path the provider actually reads and writes.
    """
    if home is not None:
        return home
    override = os.environ.get("DECKFLOW_CONFIG_DIR") or os.environ.get(
        "DECKHTML_CONFIG_DIR"
    )
    return Path(override).expanduser() if override else deckflow_home()


def credentials_file(home: Path | None = None) -> Path:
    """Written by `deckflow-extract config set` / `auth login`, never by core.

    Core reports its path so a user who wants to clear a credential can find
    it; `deckflow` deliberately has no `auth logout`.
    """
    return credentials_dir(home) / "credentials"


def credential_env(home: Path | None = None) -> dict[str, str]:
    """Environment that makes extract use the credential path core reports."""
    return {"DECKFLOW_CONFIG_DIR": str(credentials_dir(home))}


def installed_core_versions(home: Path | None = None) -> list[str]:
    """Managed core versions, newest first, as the launcher orders them."""
    root = core_root(home)
    if not root.is_dir():
        return []
    found = [entry.name for entry in root.iterdir() if (entry / "deckflow_core").is_dir()]
    return sorted(found, key=version_key, reverse=True)


def version_key(text: str) -> tuple[int, ...]:
    """Sort directory names numerically so 0.10.0 outranks 0.9.0.

    Anything unparseable sorts lowest rather than raising: a stray directory in
    the managed home must not be able to break version selection.
    """
    parts: list[int] = []
    for chunk in text.replace("-", ".").split("."):
        if chunk.isdigit():
            parts.append(int(chunk))
        else:
            break
    return tuple(parts) or (-1,)


__all__ = [
    "deckflow_home",
    "core_root",
    "core_dir",
    "extract_root",
    "extract_dir",
    "credentials_dir",
    "credentials_file",
    "credential_env",
    "installed_core_versions",
    "version_key",
]
