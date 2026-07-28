"""Deckflow capability broker.

`deckflow-core` owns the CLI contract; the three providers own the work:

    parse         -> deckflow-extract      (PyPI)
    editor        -> @deckflow/html-editor (npm)
    export pptx   -> @deckflow/deckhtml    (npm)

Providers are acquired on demand, pinned to an exact version, and installed
only into core's own managed cache.  A run that only produces HTML never
downloads a converter, and never needs a Node runtime.
"""

from __future__ import annotations

from pathlib import Path

__version__ = "0.1.0"
SCHEMA_VERSION = 1


def schemas_dir() -> Path:
    """Locate the published JSON Schemas.

    They sit beside the package once installed, and one level up in a source
    checkout; resolving both keeps the contract tests honest in either layout.
    """
    packaged = Path(__file__).parent / "schemas"
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[2] / "schemas"


__all__ = ["__version__", "SCHEMA_VERSION", "schemas_dir"]
