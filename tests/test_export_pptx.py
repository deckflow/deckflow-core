"""`deckflow export pptx` contract.

The conversion itself belongs to the provider; what is asserted here is what
core is responsible for — refusing what cannot be exported correctly, leaving
no plausible-looking output behind on failure, and never writing project state.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fixtures import write_project

_SRC = str(Path(__file__).resolve().parents[1] / "src")


def run_export(*args: str, home: str | None = None) -> tuple[int, str, str]:
    env = {**os.environ, "PYTHONPATH": _SRC, "DECKFLOW_HOME": home or tempfile.mkdtemp()}
    completed = subprocess.run(
        [sys.executable, "-m", "deckflow_core", "export", "pptx", *args],
        capture_output=True, text=True, env=env, timeout=180,
    )
    return completed.returncode, completed.stdout, completed.stderr


class StageGuardTest(unittest.TestCase):
    """Four of the five stage sizes cannot be represented by the converter."""

    def test_non_16_9_deck_is_refused_before_anything_is_converted(self):
        for size_id in ("portrait-9-16", "portrait-3-4", "square-1-1", "landscape-4-3"):
            with tempfile.TemporaryDirectory() as root:
                write_project(Path(root), slides=1, deck_size=size_id)
                target = Path(root) / "out" / "deck.pptx"
                code, stdout, _ = run_export(
                    root, "--output", str(target), "--json", "--provider-install", "never"
                )
                payload = json.loads(stdout)
                self.assertEqual(code, 4, size_id)
                self.assertEqual(payload["status"], "failed")
                self.assertEqual(
                    payload["diagnostics"][0]["rule_id"], "PPTX_EXPORT_STAGE_UNSUPPORTED", size_id
                )
                self.assertFalse(target.exists(), "refusal must not leave an output")

    def test_the_refusal_says_what_to_do_instead(self):
        with tempfile.TemporaryDirectory() as root:
            write_project(Path(root), slides=1, deck_size="portrait-9-16")
            _, stdout, _ = run_export(
                root, "--output", str(Path(root) / "d.pptx"), "--json",
                "--provider-install", "never",
            )
            recovery = json.loads(stdout)["diagnostics"][0]["recovery"]
            self.assertIn("HTML", recovery)
            self.assertIn("landscape-16-9", recovery)


class PreconditionTest(unittest.TestCase):
    def test_broken_project_fails_before_the_provider_is_touched(self):
        """Project problems must not be reported as provider problems."""
        with tempfile.TemporaryDirectory() as root:
            write_project(Path(root), slides=2)
            (Path(root) / "deck" / "pages" / "slide-02.html").unlink()
            code, stdout, _ = run_export(
                root, "--output", str(Path(root) / "d.pptx"), "--json",
                "--provider-install", "never",
            )
            payload = json.loads(stdout)
            self.assertEqual(code, 3)
            self.assertEqual(payload["diagnostics"][0]["rule_id"], "PROJECT_PAGE_MISSING")
            self.assertEqual(payload["providers"], [])

    def test_existing_output_needs_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            write_project(Path(root), slides=1)
            target = Path(root) / "deck.pptx"
            target.write_bytes(b"existing")
            code, stdout, _ = run_export(
                root, "--output", str(target), "--json", "--provider-install", "never"
            )
            self.assertEqual(code, 6)
            self.assertEqual(json.loads(stdout)["diagnostics"][0]["rule_id"], "OUTPUT_EXISTS")
            self.assertEqual(target.read_bytes(), b"existing", "must not clobber")

    def test_non_pptx_extension_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            write_project(Path(root), slides=1)
            code, stdout, _ = run_export(
                root, "--output", str(Path(root) / "deck.key"), "--json",
                "--provider-install", "never",
            )
            self.assertEqual(code, 6)
            self.assertEqual(
                json.loads(stdout)["diagnostics"][0]["rule_id"], "OUTPUT_EXTENSION_UNEXPECTED"
            )

    def test_missing_provider_under_never_is_a_provider_error(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as home:
            write_project(Path(root), slides=1)
            code, stdout, _ = run_export(
                root, "--output", str(Path(root) / "d.pptx"), "--json",
                "--provider-install", "never", home=home,
            )
            payload = json.loads(stdout)
            # 5 when the provider is simply absent; 0 if a compatible deckhtml
            # happens to be on PATH, in which case the export really can run.
            if code == 5:
                self.assertEqual(payload["diagnostics"][0]["rule_id"], "PROVIDER_MISSING")


@unittest.skipUnless(
    os.environ.get("DECKFLOW_LIVE_TESTS") == "1",
    "live conversion: set DECKFLOW_LIVE_TESTS=1 (acquires the provider, ~45MB)",
)
class LiveConversionTest(unittest.TestCase):
    """Runs the real converter. Opt-in, because it downloads and takes ~30s."""

    def test_export_produces_a_reopenable_deck_in_plan_order(self):
        from deckflow_core import ooxml

        with tempfile.TemporaryDirectory() as root:
            write_project(Path(root), slides=3)
            plan_path = Path(root) / "deck-plan.json"
            plan = json.loads(plan_path.read_text())
            # Plan order deliberately disagrees with filename order.
            plan["slides"] = [
                {"id": "slide-03", "order": 1},
                {"id": "slide-02", "order": 2},
                {"id": "slide-01", "order": 3},
            ]
            plan_path.write_text(json.dumps(plan), encoding="utf-8")

            target = Path(root) / "out" / "deck.pptx"
            code, stdout, stderr = run_export(root, "--output", str(target), "--json")
            self.assertEqual(code, 0, stderr)

            payload = json.loads(stdout)
            self.assertEqual(payload["status"], "succeeded")
            self.assertEqual(
                [s["slide_id"] for s in payload["slides"]],
                ["slide-03", "slide-02", "slide-01"],
            )

            report = ooxml.inspect(target)
            self.assertTrue(report.readable)
            self.assertEqual(report.slide_count, 3)
            self.assertEqual(report.remote_relationships, [])
            # Text must be native runs, not a rasterised page.
            headings = [report.text_samples[part][0] for part in report.slide_parts]
            self.assertEqual(headings, ["Slide 3", "Slide 2", "Slide 1"])

    def test_export_does_not_touch_project_records(self):
        with tempfile.TemporaryDirectory() as root:
            write_project(Path(root), slides=1)
            from deckflow_core import project

            before = project.protected_snapshot(Path(root))
            pages_before = {
                path.name: path.read_bytes()
                for path in (Path(root) / "deck" / "pages").glob("*.html")
            }
            code, _, stderr = run_export(
                root, "--output", str(Path(root) / "out" / "d.pptx"), "--json"
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(project.protected_snapshot(Path(root)), before)
            self.assertEqual(
                {p.name: p.read_bytes() for p in (Path(root) / "deck" / "pages").glob("*.html")},
                pages_before,
            )


class HelpContractTest(unittest.TestCase):
    def test_help_states_the_boundaries_that_matter(self):
        env = {**os.environ, "PYTHONPATH": _SRC}
        completed = subprocess.run(
            [sys.executable, "-m", "deckflow_core", "export", "pptx", "--help"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        text = " ".join(completed.stdout.split())
        self.assertIn("deck-plan.json", text)
        self.assertIn("nothing is uploaded", text)
        self.assertIn("never modified", text)
        self.assertIn("landscape-16-9", text)
        self.assertIn("acquired on demand", text)


if __name__ == "__main__":
    unittest.main()
