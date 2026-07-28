"""Deck project semantics: order, closure, stage size, protected records."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from deckflow_core import ooxml, project
from deckflow_core.exits import CoreError
from fixtures import write_project


class OrderTest(unittest.TestCase):
    def test_order_comes_from_the_plan_not_the_filenames(self):
        """The plan is the record that carries the approved sequence.

        Filename order only happens to agree; here the plan deliberately
        disagrees, and the plan must win.
        """
        with tempfile.TemporaryDirectory() as root:
            write_project(Path(root), slides=3)
            plan = json.loads((Path(root) / "deck-plan.json").read_text())
            plan["slides"] = [
                {"id": "slide-03", "order": 1},
                {"id": "slide-01", "order": 2},
                {"id": "slide-02", "order": 3},
            ]
            (Path(root) / "deck-plan.json").write_text(json.dumps(plan), encoding="utf-8")

            deck = project.load(Path(root))
            self.assertEqual(
                [slide.slide_id for slide in deck.slides],
                ["slide-03", "slide-01", "slide-02"],
            )

    def test_missing_order_falls_back_to_declaration_order(self):
        with tempfile.TemporaryDirectory() as root:
            write_project(Path(root), slides=2)
            plan = json.loads((Path(root) / "deck-plan.json").read_text())
            for entry in plan["slides"]:
                entry.pop("order")
            (Path(root) / "deck-plan.json").write_text(json.dumps(plan), encoding="utf-8")
            deck = project.load(Path(root))
            self.assertEqual([s.slide_id for s in deck.slides], ["slide-01", "slide-02"])


class ClosureTest(unittest.TestCase):
    def test_planned_slide_without_a_page_fails(self):
        with tempfile.TemporaryDirectory() as root:
            write_project(Path(root), slides=2)
            (Path(root) / "deck" / "pages" / "slide-02.html").unlink()
            with self.assertRaises(CoreError) as caught:
                project.load(Path(root))
            self.assertEqual(caught.exception.diagnostic.rule_id, "PROJECT_PAGE_MISSING")

    def test_unplanned_page_fails_rather_than_being_exported(self):
        """A leftover page must not silently become a slide in the deck."""
        with tempfile.TemporaryDirectory() as root:
            write_project(Path(root), slides=2)
            (Path(root) / "deck" / "pages" / "slide-99.html").write_text("<html></html>", encoding="utf-8")
            with self.assertRaises(CoreError) as caught:
                project.load(Path(root))
            self.assertEqual(caught.exception.diagnostic.rule_id, "PROJECT_PAGE_UNPLANNED")
            self.assertIn("slide-99", caught.exception.diagnostic.actual)

    def test_empty_plan_fails(self):
        with tempfile.TemporaryDirectory() as root:
            write_project(Path(root), slides=1)
            (Path(root) / "deck-plan.json").write_text(
                json.dumps({"schema_version": 1, "slides": []}), encoding="utf-8"
            )
            with self.assertRaises(CoreError) as caught:
                project.load(Path(root))
            self.assertEqual(caught.exception.diagnostic.rule_id, "PROJECT_PLAN_EMPTY")

    def test_missing_records_are_named_precisely(self):
        with tempfile.TemporaryDirectory() as root:
            write_project(Path(root), slides=1)
            (Path(root) / "intent-detail.json").unlink()
            with self.assertRaises(CoreError) as caught:
                project.load(Path(root))
            self.assertEqual(caught.exception.diagnostic.rule_id, "PROJECT_INTENT_MISSING")
            self.assertEqual(caught.exception.exit_code, 3)


class StageSizeTest(unittest.TestCase):
    def test_all_five_skill_sizes_load(self):
        for size_id in project.DECK_SIZES:
            with tempfile.TemporaryDirectory() as root:
                write_project(Path(root), slides=1, deck_size=size_id)
                self.assertEqual(project.load(Path(root)).deck_size.id, size_id)

    def test_only_landscape_16_9_is_16_9(self):
        sixteen_nine = [
            size_id for size_id, (w, h) in project.DECK_SIZES.items() if w * 9 == h * 16
        ]
        self.assertEqual(sixteen_nine, ["landscape-16-9"])

    def test_unknown_size_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            write_project(Path(root), slides=1)
            (Path(root) / "intent-detail.json").write_text(
                json.dumps({"deck_size": "cinema-21-9"}), encoding="utf-8"
            )
            with self.assertRaises(CoreError) as caught:
                project.load(Path(root))
            self.assertEqual(caught.exception.diagnostic.rule_id, "PROJECT_DECK_SIZE_UNKNOWN")


class ProtectedSnapshotTest(unittest.TestCase):
    def test_snapshot_covers_the_records_core_must_not_write(self):
        with tempfile.TemporaryDirectory() as root:
            write_project(Path(root), slides=1)
            snapshot = project.protected_snapshot(Path(root))
            self.assertEqual(set(snapshot), set(project.PROTECTED_PATHS))
            self.assertTrue(all(value for value in snapshot.values()))

    def test_snapshot_detects_a_change(self):
        with tempfile.TemporaryDirectory() as root:
            write_project(Path(root), slides=1)
            before = project.protected_snapshot(Path(root))
            (Path(root) / "deck" / "index.html").write_text("<!-- tampered -->", encoding="utf-8")
            self.assertNotEqual(before, project.protected_snapshot(Path(root)))


class OoxmlInspectorTest(unittest.TestCase):
    def test_missing_file_is_reported_not_raised(self):
        report = ooxml.inspect(Path("/definitely/not/here.pptx"))
        self.assertFalse(report.readable)
        self.assertIn("does not exist", report.error)

    def test_non_pptx_zip_is_rejected(self):
        import zipfile

        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "fake.pptx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("hello.txt", "not a deck")
            report = ooxml.inspect(path)
            self.assertFalse(report.readable)
            self.assertIn("PresentationML", report.error)

    def test_garbage_file_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "garbage.pptx"
            path.write_bytes(b"not a zip at all")
            self.assertFalse(ooxml.inspect(path).readable)


if __name__ == "__main__":
    unittest.main()
