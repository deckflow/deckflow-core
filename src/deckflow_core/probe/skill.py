"""Which Skill is calling, if any.

Core does not go looking.  Scanning upward from the working directory for a
`SKILL.md` would be wrong in the common case: the agent's cwd is the *project*,
not the Skill; a machine may have several Skills installed; and a user's own
project may contain a `SKILL.md` of its own.  So the caller declares itself —
`--skill-root`, then `DECKFLOW_SKILL_ROOT`, which both Skills already export,
and which the launcher sets from its own location.

No declaration is not an error.  Core runs perfectly well without a Skill; it
reports `"skill": null` and moves on.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..diagnostics import Diagnostic

MANIFEST = "deckflow-skill.json"
SKILL_DOC = "SKILL.md"

# Enough for any plausible frontmatter block; a Skill document itself can be
# tens of kilobytes and none of the rest is ours to read.
_FRONTMATTER_LIMIT = 8192


def declared_root(explicit: str | None) -> str | None:
    return explicit or os.environ.get("DECKFLOW_SKILL_ROOT") or None


def probe(explicit: str | None) -> tuple[dict[str, Any] | None, list[Diagnostic]]:
    raw = declared_root(explicit)
    if not raw:
        return None, []

    root = Path(raw).expanduser()
    if not (root / SKILL_DOC).is_file():
        return None, [
            Diagnostic(
                rule_id="SKILL_ROOT_INVALID",
                severity="warning",
                message="The declared skill root does not contain a SKILL.md.",
                location=str(root),
                expected=f"a directory containing {SKILL_DOC}",
                actual="no such file",
                recovery="Correct --skill-root, or unset DECKFLOW_SKILL_ROOT to report no skill.",
            )
        ]

    record: dict[str, Any] = {
        "name": None,
        "version": None,
        "root": str(root.resolve()),
        "version_source": None,
    }

    manifest = _read_manifest(root / MANIFEST)
    if manifest is not None:
        record["name"] = manifest.get("name")
        record["version"] = manifest.get("version")
        record["version_source"] = "manifest"
        update = manifest.get("update")
        if isinstance(update, dict) and update.get("command"):
            record["update_command"] = str(update["command"])

    if not record["version"]:
        name, version = _read_frontmatter(root / SKILL_DOC)
        record["name"] = record["name"] or name
        if version:
            record["version"] = version
            record["version_source"] = "frontmatter"

    diagnostics: list[Diagnostic] = []
    if not record["version"]:
        diagnostics.append(
            Diagnostic(
                rule_id="SKILL_VERSION_UNKNOWN",
                severity="info",
                message="The skill declares no version core can read.",
                location=str(root),
                expected=f"{MANIFEST} with a `version`, or SKILL.md frontmatter `metadata.version`",
                actual="neither present",
                recovery=f"Add a `version` to {MANIFEST} if the version needs to be machine-readable.",
            )
        )
    return record, diagnostics


def _read_manifest(path: Path) -> dict[str, Any] | None:
    """The machine-owned source, preferred because it needs no guessing."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_frontmatter(path: Path) -> tuple[str | None, str | None]:
    """Pull `name` and `metadata.version` out of YAML frontmatter.

    Deliberately not a YAML parser: core has zero third-party dependencies, and
    this needs two scalars out of a block whose shape both Skills already fix.
    Anything it cannot read confidently comes back as None, which surfaces as
    `SKILL_VERSION_UNKNOWN` rather than as a wrong version.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            head = handle.read(_FRONTMATTER_LIMIT)
    except OSError:
        return None, None

    lines = head.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, None

    name: str | None = None
    version: str | None = None
    in_metadata = False
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if not line.strip():
            continue
        indented = line[:1].isspace()
        if not indented:
            # A new top-level key ends any block we were inside; folded scalars
            # (`description: >`) are indented and therefore skipped here.
            in_metadata = line.split(":", 1)[0].strip() == "metadata"
            if not in_metadata:
                key, _, value = line.partition(":")
                if key.strip() == "name":
                    name = _scalar(value) or name
            continue
        if in_metadata:
            key, _, value = line.partition(":")
            if key.strip() == "version":
                version = _scalar(value) or version
    return name, version


def _scalar(value: str) -> str | None:
    text = value.strip().strip('"').strip("'").strip()
    return text or None


__all__ = ["probe", "declared_root", "MANIFEST"]
