"""Deck project fixtures.

Small but real: standalone pages with `data-element-id` attributes and a fixed
stage, so an export test exercises the same shape the Skill actually produces.
"""

from __future__ import annotations

import json
from pathlib import Path

_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  :root {{ --deck-width: {width}px; --deck-height: {height}px; }}
  html, body {{ margin: 0; padding: 0; }}
  .hd-slide {{
    width: {width}px; height: {height}px;
    background: #ffffff; color: #101418;
    font-family: Helvetica, Arial, sans-serif;
    box-sizing: border-box; padding: 96px; position: relative;
  }}
  h1 {{ font-size: 72px; margin: 0 0 32px; }}
  p  {{ font-size: 32px; margin: 0; color: #3c4650; }}
</style>
</head>
<body class="hd-page">
<section class="hd-slide" data-slide-id="{slide_id}">
  <h1 data-element-id="{slide_id}-title">{title}</h1>
  <p data-element-id="{slide_id}-body">{body}</p>
</section>
</body>
</html>
"""


def write_project(root: Path, *, slides: int = 2, deck_size: str = "landscape-16-9",
                  width: int = 1920, height: int = 1080) -> Path:
    """Create a minimal but valid deck project and return its root."""
    root = Path(root)
    pages = root / "deck" / "pages"
    pages.mkdir(parents=True, exist_ok=True)

    planned = []
    for index in range(1, slides + 1):
        slide_id = f"slide-{index:02d}"
        title = f"Slide {index}"
        body = f"Body text for slide {index}."
        (pages / f"{slide_id}.html").write_text(
            _PAGE.format(slide_id=slide_id, title=title, body=body, width=width, height=height),
            encoding="utf-8",
        )
        planned.append({"id": slide_id, "order": index, "takeaway": title})

    (root / "deck-plan.json").write_text(
        json.dumps({"schema_version": 1, "title": "Fixture deck", "slides": planned}, indent=2),
        encoding="utf-8",
    )
    (root / "intent-detail.json").write_text(
        json.dumps({"schema_version": 1, "deck_size": deck_size}, indent=2), encoding="utf-8"
    )
    (root / "deck" / "deck-head.html").write_text("<!-- deck head -->\n", encoding="utf-8")
    (root / "deck" / "index.html").write_text("<!-- player -->\n", encoding="utf-8")
    (root / "deck" / "build-manifest.json").write_text(
        json.dumps({"schema_version": 1, "outputs": []}, indent=2), encoding="utf-8"
    )
    return root
