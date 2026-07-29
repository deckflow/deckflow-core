"""Where the running core came from.

Worth reporting because the three installation modes fail differently: a
vendored copy can never be updated by `deckflow update`, a managed copy is
replaced by installing a new version beside it, and a site-packages copy is
owned by whatever installed it.  A caller that sees the wrong `version` needs
to know which of those it is looking at before it can fix anything.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import __version__
from .. import home as home_mod


def location() -> Path:
    """The directory that was on `sys.path` for this import."""
    return Path(__file__).resolve().parent.parent.parent


def probe(skill_root: str | None = None, home: Path | None = None) -> dict[str, Any]:
    here = location()
    core_root = home_mod.core_root(home).resolve()

    installation = "site-packages"
    if here.parent == core_root:
        installation = "managed"
    elif skill_root and _is_within(here, Path(skill_root).expanduser().resolve()):
        installation = "vendored"

    return {
        "version": __version__,
        "installation": installation,
        "location": str(here),
    }


def _is_within(candidate: Path, root: Path) -> bool:
    return candidate == root or root in candidate.parents


__all__ = ["probe", "location"]
