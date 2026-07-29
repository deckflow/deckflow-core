"""Deckflow capability broker.

`deckflow-core` owns the CLI contract; `deckflow-extract` owns the work:

    parse -> deckflow-extract (PyPI)

Extract is acquired on demand, pinned to an exact version, and installed only
into core's own managed home.  Core is pure Python and never needs a Node
runtime: `@deckflow/html-editor` and `@deckflow/deckhtml` are called directly
by the Skill, not brokered here.  `env check` reports whether Node exists
because the Skill needs the fact, but core reports facts, never verdicts.
"""

from __future__ import annotations

from pathlib import Path

__version__ = "0.3.0"

# 2 renamed the envelope's `providers[]` array to a single `extract{}` object,
# dropped `pinned`, and added the `env` payload.  There were no 0.2.x users.
SCHEMA_VERSION = 2

REQUIRES_PYTHON = ">=3.10"


def schemas_dir() -> Path:
    """Locate the published JSON Schemas.

    They live inside the package rather than beside it, so this is one path
    with no fallback: a vendored copy of `deckflow_core/` carries its schemas
    with it, and there is no layout in which they can go missing.
    """
    return Path(__file__).parent / "schemas"


__all__ = ["__version__", "SCHEMA_VERSION", "REQUIRES_PYTHON", "schemas_dir"]
