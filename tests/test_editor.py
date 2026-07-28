"""`deckflow editor` contract.

The provider tells core nothing about the session, so everything asserted here
is derived from the outside: what a before/after snapshot can prove, and what
it honestly cannot.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from deckflow_core.commands import editor as editor_cmd
from deckflow_core.exits import CoreError
from fixtures import write_project

_SRC = str(Path(__file__).resolve().parents[1] / "src")


class PreflightTest(unittest.TestCase):
    def test_missing_pages_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(CoreError) as caught:
                editor_cmd._preflight(Path(root), None)
            self.assertEqual(caught.exception.diagnostic.rule_id, "PROJECT_PAGES_MISSING")
            self.assertEqual(caught.exception.exit_code, 3)

    def test_empty_pages_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "deck" / "pages").mkdir(parents=True)
            with self.assertRaises(CoreError) as caught:
                editor_cmd._preflight(Path(root), None)
            self.assertEqual(caught.exception.diagnostic.rule_id, "PROJECT_PAGES_EMPTY")

    def test_unknown_page_lists_the_ones_that_exist(self):
        with tempfile.TemporaryDirectory() as root:
            write_project(Path(root), slides=2)
            with self.assertRaises(CoreError) as caught:
                editor_cmd._preflight(Path(root), "slide-99")
            self.assertEqual(caught.exception.diagnostic.rule_id, "EDITOR_PAGE_NOT_FOUND")
            self.assertIn("slide-01", caught.exception.diagnostic.expected)

    def test_preflight_does_not_require_a_complete_project(self):
        """Opening the editor is not a project validation.

        Duplicating the Skill's validator here would create a second, drifting
        opinion about what a valid project is.
        """
        with tempfile.TemporaryDirectory() as root:
            write_project(Path(root), slides=2)
            (Path(root) / "deck-plan.json").unlink()
            (Path(root) / "intent-detail.json").unlink()
            deck_dir, target = editor_cmd._preflight(Path(root), "slide-01")
            self.assertEqual(deck_dir, Path(root).resolve() / "deck")
            self.assertEqual(target.name, "slide-01.html")

    def test_without_a_page_the_whole_directory_opens(self):
        with tempfile.TemporaryDirectory() as root:
            write_project(Path(root), slides=2)
            _, target = editor_cmd._preflight(Path(root), None)
            self.assertTrue(target.is_dir())
            self.assertEqual(target.name, "pages")


class SnapshotTest(unittest.TestCase):
    def _deck(self, root: str) -> Path:
        write_project(Path(root), slides=2)
        return Path(root) / "deck"

    def test_editor_working_files_are_not_mistaken_for_changes(self):
        """The provider writes backups and temp files under the project root.

        Counting those as boundary violations would fail every real session.
        """
        with tempfile.TemporaryDirectory() as root:
            deck = self._deck(root)
            before = editor_cmd._snapshot(deck)
            backups = deck / ".local-html-editor" / "backups"
            backups.mkdir(parents=True)
            (backups / "slide-01.html").write_text("<old>", encoding="utf-8")
            (deck / "pages" / "slide-01.html.local-html-editor-123.tmp").write_text("x", encoding="utf-8")
            self.assertEqual(editor_cmd._snapshot(deck).files, before.files)

    def test_element_ids_are_collected_per_page(self):
        with tempfile.TemporaryDirectory() as root:
            deck = self._deck(root)
            ids = editor_cmd._snapshot(deck).element_ids["pages/slide-01.html"]
            self.assertIn("slide-01-title", ids)
            self.assertIn("slide-01-body", ids)

    def test_element_ids_are_sorted_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as root:
            page = Path(root) / "p.html"
            page.write_text(
                '<a data-element-id="b"></a><b data-element-id="a"></b>'
                "<i data-element-id='b'></i>", encoding="utf-8",
            )
            self.assertEqual(editor_cmd._element_ids(page), ("a", "b"))


class ClassificationTest(unittest.TestCase):
    """The heart of the command: what a before/after difference means."""

    def _snap(self, files, ids=None):
        return editor_cmd.Snapshot(files=files, element_ids=ids or {})

    def test_an_edited_page_is_reported_with_both_hashes(self):
        changed, findings = editor_cmd._classify(
            self._snap({"pages/slide-01.html": "a" * 64}),
            self._snap({"pages/slide-01.html": "b" * 64}),
        )
        self.assertEqual(findings, [])
        self.assertEqual(changed[0]["slide_id"], "slide-01")
        self.assertEqual(changed[0]["before_sha256"], "a" * 64)
        self.assertEqual(changed[0]["after_sha256"], "b" * 64)

    def test_an_untouched_session_reports_nothing(self):
        snapshot = self._snap({"pages/slide-01.html": "a" * 64})
        self.assertEqual(editor_cmd._classify(snapshot, snapshot), ([], []))

    def test_a_change_outside_pages_is_an_error(self):
        _, findings = editor_cmd._classify(
            self._snap({"index.html": "a" * 64}),
            self._snap({"index.html": "b" * 64}),
        )
        self.assertEqual(findings[0].rule_id, "EDITOR_TOUCHED_PROTECTED_FILE")
        self.assertEqual(findings[0].severity, "error")
        self.assertIn("backups", findings[0].recovery)

    def test_a_new_file_outside_pages_is_an_error(self):
        _, findings = editor_cmd._classify(
            self._snap({}), self._snap({"runtime/theme.css": "b" * 64})
        )
        self.assertEqual(findings[0].rule_id, "EDITOR_TOUCHED_PROTECTED_FILE")

    def test_a_removed_element_identity_is_an_error(self):
        _, findings = editor_cmd._classify(
            self._snap({"pages/s.html": "a" * 64}, {"pages/s.html": ("title", "body")}),
            self._snap({"pages/s.html": "b" * 64}, {"pages/s.html": ("title",)}),
        )
        identity = next(f for f in findings if f.rule_id == "EDITOR_ELEMENT_IDENTITY_CHANGED")
        self.assertEqual(identity.severity, "error")
        self.assertIn("body", identity.actual)

    def test_an_added_element_identity_is_also_an_error(self):
        _, findings = editor_cmd._classify(
            self._snap({"pages/s.html": "a" * 64}, {"pages/s.html": ("title",)}),
            self._snap({"pages/s.html": "b" * 64}, {"pages/s.html": ("title", "extra")}),
        )
        self.assertTrue(any(f.rule_id == "EDITOR_ELEMENT_IDENTITY_CHANGED" for f in findings))

    def test_text_edits_that_keep_identities_are_not_flagged(self):
        _, findings = editor_cmd._classify(
            self._snap({"pages/s.html": "a" * 64}, {"pages/s.html": ("title", "body")}),
            self._snap({"pages/s.html": "b" * 64}, {"pages/s.html": ("title", "body")}),
        )
        self.assertEqual(findings, [])


class ReadinessTest(unittest.TestCase):
    def test_the_url_is_picked_out_of_the_provider_prose(self):
        lines = ["Local HTML Editor: http://127.0.0.1:43127", "Project: /x"]
        self.assertEqual(
            editor_cmd._await_ready(lines, _StubChild(alive=True)),
            "http://127.0.0.1:43127",
        )

    def test_a_child_that_exits_without_a_url_stops_the_wait(self):
        # Must not sit until the full timeout when there is nothing to wait for.
        started = time.monotonic()
        self.assertIsNone(editor_cmd._await_ready([], _StubChild(alive=False)))
        self.assertLess(time.monotonic() - started, 5)


class _StubChild:
    def __init__(self, alive: bool) -> None:
        self._alive = alive

    def poll(self):
        return None if self._alive else 0


class CliTest(unittest.TestCase):
    def test_help_states_the_audit_limits(self):
        env = {**os.environ, "PYTHONPATH": _SRC}
        completed = subprocess.run(
            [sys.executable, "-m", "deckflow_core", "editor", "--help"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        text = " ".join(completed.stdout.split())
        self.assertIn("NDJSON", text)
        self.assertIn("Ctrl-C", text)
        self.assertIn("not per operation", text)
        self.assertIn("127.0.0.1", text)

    def test_broken_project_fails_before_the_provider_is_touched(self):
        with tempfile.TemporaryDirectory() as root:
            env = {**os.environ, "PYTHONPATH": _SRC, "DECKFLOW_HOME": tempfile.mkdtemp()}
            completed = subprocess.run(
                [sys.executable, "-m", "deckflow_core", "editor", root,
                 "--json", "--provider-install", "never"],
                capture_output=True, text=True, env=env, timeout=120,
            )
            self.assertEqual(completed.returncode, 3)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["diagnostics"][0]["rule_id"], "PROJECT_PAGES_MISSING")
            self.assertEqual(payload["providers"], [])


@unittest.skipUnless(
    os.environ.get("DECKFLOW_LIVE_TESTS") == "1",
    "live editor session: set DECKFLOW_LIVE_TESTS=1 (acquires the provider, ~3MB)",
)
class LiveSessionTest(unittest.TestCase):
    """Starts the real editor, edits a page from outside, then ends the session."""

    def _session(self, root: Path, mutate) -> dict:
        env = {**os.environ, "PYTHONPATH": _SRC}
        events = root / "events.ndjson"
        with open(events, "w") as sink:
            child = subprocess.Popen(
                [sys.executable, "-m", "deckflow_core", "editor", str(root / "project")],
                stdout=sink, stderr=subprocess.DEVNULL, env=env,
            )
            try:
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline and events.stat().st_size == 0:
                    time.sleep(0.2)
                self.assertGreater(events.stat().st_size, 0, "editor never announced ready")
                mutate(root / "project")
                time.sleep(0.5)
                child.send_signal(signal.SIGTERM)
                child.wait(timeout=60)
            finally:
                if child.poll() is None:
                    child.kill()
        return json.loads(events.read_text().strip().splitlines()[-1])

    def test_an_edited_page_is_reported_with_before_and_after_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_project(root / "project", slides=3)

            def edit(project_root: Path) -> None:
                page = project_root / "deck" / "pages" / "slide-02.html"
                page.write_text(
                    page.read_text().replace("Body text for slide 2.", "Rewritten."),
                    encoding="utf-8",
                )

            final = self._session(root, edit)
            self.assertEqual(final["event"], "finished")
            self.assertEqual(final["status"], "succeeded")
            self.assertEqual([c["slide_id"] for c in final["changed_pages"]], ["slide-02"])
            changed = final["changed_pages"][0]
            self.assertNotEqual(changed["before_sha256"], changed["after_sha256"])

    def test_touching_a_protected_record_fails_the_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_project(root / "project", slides=2)
            final = self._session(
                root,
                lambda p: (p / "deck" / "index.html").write_text("<!-- tampered -->", encoding="utf-8"),
            )
            self.assertEqual(final["status"], "failed")
            self.assertTrue(
                any(d["rule_id"] == "EDITOR_TOUCHED_PROTECTED_FILE" for d in final["diagnostics"])
            )

    def test_a_session_that_changes_nothing_is_reported_as_such(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_project(root / "project", slides=2)
            final = self._session(root, lambda p: None)
            self.assertEqual(final["status"], "succeeded")
            self.assertEqual(final["changed_pages"], [])


if __name__ == "__main__":
    unittest.main()
