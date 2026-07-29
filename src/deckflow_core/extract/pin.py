"""The pinned `deckflow-extract` version.

Bumping anything here is a core release.  This is why the pin no longer lives
inside a Skill script: one place declares which provider version this core was
tested against, and `deckflow env check` publishes it.

It used to be a JSON matrix with a schema and a loader, sized for N providers.
There is one, and there is no `--provider-spec` override any more: to test a
release candidate, change these constants and build a core.  A pin that any
caller can quietly replace is not a pin.
"""

from __future__ import annotations

PACKAGE = "deckflow-extract"
MODULE = "deckflow_extract"
BIN = "deckflow-extract"

VERSION = "0.3.0"

# The range an install already on PATH may satisfy.  0.3.0 is the floor because
# that is the first version reading `~/.deckflow/credentials`, which the whole
# shared-credential contract with DeckHTML depends on.
COMPATIBLE = ">=0.3.0 <0.4.0"

DOWNLOAD_MB = 4

INDEX_URL = "https://pypi.org/simple"

# Tried after the index.  Ordering it second means publishing to PyPI later
# needs no code change: the index simply starts succeeding and this stops being
# reached.
SOURCE = (
    "https://github.com/deckflow/deckflow-extract/releases/download/"
    f"v{VERSION}/deckflow_extract-{VERSION}-py3-none-any.whl"
)

REQUIREMENT = f"{PACKAGE}=={VERSION}"

__all__ = [
    "PACKAGE", "MODULE", "BIN", "VERSION", "COMPATIBLE",
    "DOWNLOAD_MB", "INDEX_URL", "SOURCE", "REQUIREMENT",
]
