"""Facts about host tools core does not broker.

Core does not run `@deckflow/deckhtml` or `@deckflow/html-editor` — the Skill
calls them directly, and wrapping them back up was refused for good reasons.
Reporting whether Node exists is a different act: running a tool is brokering,
observing that it is installed is honest reporting, and the Skill has to answer
the question anyway.  Giving it here means one JSON reader instead of two.

The line this file must not cross: **facts, never verdicts**.  There is no
`"pptx_available"` here, because that also depends on registry reachability and
the deck's stage size, neither of which core knows.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

from .. import versions


def _tool(name: str, *, with_version: bool) -> dict[str, Any]:
    path = shutil.which(name)
    if path is None:
        return {"present": False}
    record: dict[str, Any] = {"present": True, "path": path}
    if with_version:
        try:
            completed = subprocess.run(
                [path, "--version"], capture_output=True, text=True, timeout=10
            )
            record["version"] = versions.normalize(completed.stdout or completed.stderr)
        except (OSError, subprocess.SubprocessError):
            record["version"] = None
    return record


def probe() -> dict[str, Any]:
    return {
        "node": _tool("node", with_version=True),
        # npx is what the Skill actually invokes; its own version is npm's and
        # tells a caller nothing useful, so only presence is reported.
        "npx": _tool("npx", with_version=False),
    }


__all__ = ["probe"]
