"""Independent verification of a produced PPTX.

The converter reports what it believes it wrote.  This reads the file back with
nothing but `zipfile` and `xml.etree` and reports what is actually there — a
separate pair of eyes, which is the point: a converter cannot be the only
witness to its own output.

Deliberately shallow. Cross-application visual fidelity belongs to a
verification harness, not to a single command.
"""

from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

_PRESENTATION = "ppt/presentation.xml"
_PRESENTATION_RELS = "ppt/_rels/presentation.xml.rels"
_SLIDE_RE = re.compile(r"^ppt/slides/slide\d+\.xml$")

_NS = {
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}

_SLIDE_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
_REMOTE_RE = re.compile(r"^(https?|ftp|ws|wss)://", re.IGNORECASE)


@dataclass
class PptxReport:
    readable: bool
    slide_count: int = 0
    slide_parts: list[str] = field(default_factory=list)
    slide_size: tuple[int, int] | None = None
    remote_relationships: list[str] = field(default_factory=list)
    text_samples: dict[str, list[str]] = field(default_factory=dict)
    error: str | None = None

    def to_json(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "readable": self.readable,
            "slide_count": self.slide_count,
            "slide_parts": self.slide_parts,
            "remote_relationships": self.remote_relationships,
        }
        if self.slide_size:
            payload["slide_size_emu"] = {"width": self.slide_size[0], "height": self.slide_size[1]}
        if self.error:
            payload["error"] = self.error
        return payload


def _ordered_slide_parts(archive: zipfile.ZipFile) -> list[str]:
    """Slide order is the r:id order in presentation.xml, not the part names.

    `slide12.xml` sorts before `slide2.xml` lexically, and the part numbering
    need not match the presentation order at all, so resolve through the
    relationships the way PowerPoint does.
    """
    try:
        presentation = ET.fromstring(archive.read(_PRESENTATION))
        rels = ET.fromstring(archive.read(_PRESENTATION_RELS))
    except (KeyError, ET.ParseError):
        return sorted(name for name in archive.namelist() if _SLIDE_RE.match(name))

    targets: dict[str, str] = {}
    for relationship in rels.findall("rel:Relationship", _NS):
        if relationship.get("Type") == _SLIDE_REL_TYPE:
            target = relationship.get("Target") or ""
            targets[relationship.get("Id") or ""] = posixpath.normpath(
                posixpath.join("ppt", target)
            )

    ordered: list[str] = []
    slide_list = presentation.find("p:sldIdLst", _NS)
    if slide_list is not None:
        for entry in slide_list.findall("p:sldId", _NS):
            rid = entry.get(f"{{{_NS['r']}}}id")
            if rid in targets:
                ordered.append(targets[rid])
    return ordered or sorted(name for name in archive.namelist() if _SLIDE_RE.match(name))


def _slide_size(archive: zipfile.ZipFile) -> tuple[int, int] | None:
    try:
        presentation = ET.fromstring(archive.read(_PRESENTATION))
    except (KeyError, ET.ParseError):
        return None
    size = presentation.find("p:sldSz", _NS)
    if size is None:
        return None
    try:
        return int(size.get("cx", "0")), int(size.get("cy", "0"))
    except ValueError:
        return None


def _remote_relationships(archive: zipfile.ZipFile) -> list[str]:
    """Any relationship pointing off the machine.

    Core promises the content plane never reaches the network; a remote
    relationship baked into the deck would break that promise every time the
    file is opened.
    """
    found: list[str] = []
    for name in archive.namelist():
        if not name.endswith(".rels"):
            continue
        try:
            rels = ET.fromstring(archive.read(name))
        except ET.ParseError:
            continue
        for relationship in rels.findall("rel:Relationship", _NS):
            target = relationship.get("Target") or ""
            if _REMOTE_RE.match(target):
                found.append(f"{name} -> {target}")
    return sorted(found)


def _visible_text(archive: zipfile.ZipFile, part: str) -> list[str]:
    try:
        slide = ET.fromstring(archive.read(part))
    except (KeyError, ET.ParseError):
        return []
    return [
        node.text.strip()
        for node in slide.iter(f"{{{_NS['a']}}}t")
        if node.text and node.text.strip()
    ]


def inspect(path: Path, *, sample_text: bool = True) -> PptxReport:
    """Reopen a PPTX and report what is really in it."""
    path = Path(path)
    if not path.is_file():
        return PptxReport(readable=False, error="output file does not exist")
    try:
        with zipfile.ZipFile(path) as archive:
            broken = archive.testzip()
            if broken is not None:
                return PptxReport(readable=False, error=f"corrupt archive entry: {broken}")
            if _PRESENTATION not in archive.namelist():
                return PptxReport(readable=False, error="not a PresentationML package")
            parts = _ordered_slide_parts(archive)
            report = PptxReport(
                readable=True,
                slide_count=len(parts),
                slide_parts=parts,
                slide_size=_slide_size(archive),
                remote_relationships=_remote_relationships(archive),
            )
            if sample_text:
                report.text_samples = {part: _visible_text(archive, part) for part in parts}
            return report
    except (zipfile.BadZipFile, OSError) as error:
        return PptxReport(readable=False, error=str(error))
