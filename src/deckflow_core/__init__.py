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

__version__ = "0.1.1"
SCHEMA_VERSION = 1


def schemas_dir() -> Path:
    """Locate the published JSON Schemas.

    They live inside the package rather than beside it, so this is one path
    with no fallback: a vendored copy of `deckflow_core/` carries its schemas
    with it, and there is no layout in which they can go missing.
    """
    return Path(__file__).parent / "schemas"


__all__ = ["__version__", "SCHEMA_VERSION", "schemas_dir"]
