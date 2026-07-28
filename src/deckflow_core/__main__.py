"""Entry point for `python -m deckflow_core`.

This is the invocation the Skill uses for both distribution modes: a managed
`pip install --target` directory on PYTHONPATH, or a vendored copy inside the
Skill package.  Both must produce byte-identical envelopes.
"""

from __future__ import annotations

import sys

from deckflow_core.cli import main

if __name__ == "__main__":
    sys.exit(main())
