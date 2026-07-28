"""Deck project layout semantics.

The providers are generic: `deckhtml` converts whatever HTML files it is
handed, in the order it is handed them.  Knowing *which* files, *in what
order*, and *what must not be touched* is Deckflow knowledge, and it lives here
so each command does not re-derive it.

Core reads these records. It never writes them — the canonical writers are the
Skill's own scripts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .diagnostics import Diagnostic
from .exits import EXIT_INPUT, CoreError

# The five intent-owned stage sizes. Mirrored from the Skill rather than
# imported: core cannot depend on the Skill, and five constants are cheaper
# than a coupling.
DECK_SIZES: dict[str, tuple[int, int]] = {
    "landscape-16-9": (1920, 1080),
    "landscape-4-3": (1440, 1080),
    "portrait-9-16": (1080, 1920),
    "portrait-3-4": (1080, 1440),
    "square-1-1": (1080, 1080),
}
DEFAULT_DECK_SIZE = "landscape-16-9"

# Written by the Skill, read by core, never modified by core.
PROTECTED_PATHS = ("deck/index.html", "deck/deck-head.html", "deck/build-manifest.json")


@dataclass(frozen=True)
class Slide:
    slide_id: str
    order: int
    path: Path


@dataclass(frozen=True)
class DeckSize:
    id: str
    width: int
    height: int

    @property
    def is_16_9(self) -> bool:
        return self.width * 9 == self.height * 16


@dataclass(frozen=True)
class DeckProject:
    root: Path
    slides: tuple[Slide, ...]
    deck_size: DeckSize
    title: str | None

    @property
    def pages_dir(self) -> Path:
        return self.root / "deck" / "pages"

    def page_paths(self) -> list[Path]:
        return [slide.path for slide in self.slides]


def _fail(rule_id: str, message: str, *, location: str, expected: str,
          actual: str, recovery: str, exit_code: int = EXIT_INPUT) -> CoreError:
    return CoreError(
        Diagnostic(
            rule_id=rule_id, severity="error", message=message, location=location,
            expected=expected, actual=actual, recovery=recovery,
        ),
        exit_code=exit_code,
    )


def _read_json(path: Path, rule_id: str, what: str) -> dict[str, Any]:
    if not path.is_file():
        raise _fail(
            rule_id,
            f"The project is missing its {what}.",
            location=str(path),
            expected=f"a readable {path.name}",
            actual="no such file",
            recovery=f"Run the Skill's project initialisation, or pass the correct project root.",
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as error:
        raise _fail(
            rule_id,
            f"The project's {what} could not be read as JSON.",
            location=str(path),
            expected="valid UTF-8 JSON",
            actual=str(error),
            recovery=f"Repair {path.name} with the Skill's writers before exporting.",
        ) from error


def _deck_size(root: Path) -> DeckSize:
    intent = _read_json(root / "intent-detail.json", "PROJECT_INTENT_MISSING", "intent record")
    size_id = str(intent.get("deck_size") or DEFAULT_DECK_SIZE).strip()
    if size_id not in DECK_SIZES:
        raise _fail(
            "PROJECT_DECK_SIZE_UNKNOWN",
            f"Unsupported deck_size: {size_id}.",
            location=str(root / "intent-detail.json"),
            expected=" | ".join(DECK_SIZES),
            actual=size_id,
            recovery="Correct `deck_size` in intent-detail.json to one of the five supported sizes.",
        )
    width, height = DECK_SIZES[size_id]
    return DeckSize(id=size_id, width=width, height=height)


def _planned_slides(root: Path) -> list[tuple[str, int]]:
    """Slide identity and order come from the plan, not from filenames.

    Filename order happens to agree while pages are zero-padded and no page is
    ever renamed or reordered — the plan is the record that actually carries
    the approved sequence, so read that instead of hoping.
    """
    plan = _read_json(root / "deck-plan.json", "PROJECT_PLAN_MISSING", "deck plan")
    entries = plan.get("slides")
    if not isinstance(entries, list) or not entries:
        raise _fail(
            "PROJECT_PLAN_EMPTY",
            "The deck plan declares no slides.",
            location=str(root / "deck-plan.json"),
            expected="a non-empty `slides` array",
            actual=f"{type(entries).__name__} with {len(entries or [])} entries",
            recovery="Complete narrative planning before exporting.",
        )
    planned: list[tuple[str, int]] = []
    for index, entry in enumerate(entries):
        slide_id = str(entry.get("id") or "").strip()
        if not slide_id:
            raise _fail(
                "PROJECT_PLAN_SLIDE_UNIDENTIFIED",
                "A deck plan entry has no slide id.",
                location=f"{root / 'deck-plan.json'}#/slides/{index}",
                expected="a non-empty `id`",
                actual="missing or blank",
                recovery="Give every planned slide a stable id before exporting.",
            )
        order = entry.get("order")
        planned.append((slide_id, int(order) if isinstance(order, int) else index + 1))
    planned.sort(key=lambda item: (item[1], item[0]))
    return planned


def _close_over_pages(root: Path, planned: list[tuple[str, int]]) -> tuple[Slide, ...]:
    """Every planned slide needs a page, and every page needs to be planned.

    A one-way check would let an unplanned leftover page silently become a
    slide in the delivered deck.
    """
    pages_dir = root / "deck" / "pages"
    if not pages_dir.is_dir():
        raise _fail(
            "PROJECT_PAGES_MISSING",
            "The project has no canonical page directory.",
            location=str(pages_dir),
            expected="deck/pages/ containing one HTML file per slide",
            actual="no such directory",
            recovery="Author the deck pages before exporting.",
        )

    on_disk = {path.stem: path for path in sorted(pages_dir.glob("*.html")) if path.is_file()}
    planned_ids = [slide_id for slide_id, _ in planned]

    missing = [slide_id for slide_id in planned_ids if slide_id not in on_disk]
    if missing:
        raise _fail(
            "PROJECT_PAGE_MISSING",
            f"{len(missing)} planned slide(s) have no canonical page.",
            location=str(pages_dir),
            expected=f"one page per planned slide ({len(planned_ids)} total)",
            actual=f"missing: {', '.join(missing)}",
            recovery="Author the missing pages, or remove them from deck-plan.json.",
        )

    unplanned = [name for name in on_disk if name not in set(planned_ids)]
    if unplanned:
        raise _fail(
            "PROJECT_PAGE_UNPLANNED",
            f"{len(unplanned)} page(s) exist that the deck plan does not declare.",
            location=str(pages_dir),
            expected="every page declared in deck-plan.json",
            actual=f"unplanned: {', '.join(sorted(unplanned))}",
            recovery=(
                "Add these slides to deck-plan.json, or delete the leftover pages. "
                "Exporting them silently would put unapproved slides in the deck."
            ),
        )

    return tuple(
        Slide(slide_id=slide_id, order=order, path=on_disk[slide_id])
        for slide_id, order in planned
    )


def load(root: Path) -> DeckProject:
    """Read a deck project, or explain precisely why it cannot be used."""
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise _fail(
            "PROJECT_ROOT_MISSING",
            "The project root does not exist.",
            location=str(root),
            expected="an existing deck project directory",
            actual="no such directory",
            recovery="Pass the path to a Deckflow project root.",
        )
    planned = _planned_slides(root)
    slides = _close_over_pages(root, planned)
    plan = _read_json(root / "deck-plan.json", "PROJECT_PLAN_MISSING", "deck plan")
    return DeckProject(
        root=root,
        slides=slides,
        deck_size=_deck_size(root),
        title=plan.get("title"),
    )


def protected_snapshot(root: Path) -> dict[str, str | None]:
    """sha256 of the records core must never modify, for before/after proof."""
    from .fsutil import sha256_file

    snapshot: dict[str, str | None] = {}
    for relative in PROTECTED_PATHS:
        path = Path(root) / relative
        snapshot[relative] = sha256_file(path) if path.is_file() else None
    return snapshot
