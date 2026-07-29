"""Facts about the interpreter running core.

A bare `"python": "3.12"` is not enough to act on.  An agent host commonly has
three `python3` binaries on PATH, and two machines both reporting 3.12 differ
in whether `pip install` is refused: Homebrew and Debian ship the PEP 668
`EXTERNALLY-MANAGED` marker, Apple's `/usr/bin/python3` is 3.9.6 and below the
floor entirely.  So report the path and the two properties that decide whether
an install can succeed.
"""

from __future__ import annotations

import os
import platform
import sys
import sysconfig
from typing import Any

from .. import REQUIRES_PYTHON

MINIMUM = (3, 10)


def externally_managed() -> bool:
    """True when PEP 668 forbids installing into this interpreter.

    Checked by file rather than by attempting an install: this runs on every
    `env check`, and the answer must cost nothing.
    """
    marker = os.path.join(sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED")
    return os.path.exists(marker)


def probe() -> dict[str, Any]:
    return {
        "version": platform.python_version(),
        "executable": sys.executable,
        "requires_python": REQUIRES_PYTHON,
        "satisfies_requires_python": sys.version_info[:2] >= MINIMUM,
        "externally_managed": externally_managed(),
    }


__all__ = ["probe", "externally_managed", "MINIMUM"]
