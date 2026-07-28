"""CLI contract: stdout discipline, exit codes, and the absence of stubs."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from deckflow_core import __version__
from deckflow_core.cli import main

_SRC = str(Path(__file__).resolve().parents[1] / "src")


def run_cli(*args: str, home: str | None = None) -> tuple[int, str, str]:
    """Run the CLI in a subprocess so exit code and stream separation are real."""
    env = {**os.environ, "PYTHONPATH": _SRC, "DECKFLOW_HOME": home or tempfile.mkdtemp()}
    completed = subprocess.run(
        [sys.executable, "-m", "deckflow_core", *args],
        capture_output=True, text=True, env=env, timeout=120,
    )
    return completed.returncode, completed.stdout, completed.stderr


class DeferredCommandsTest(unittest.TestCase):
    """A deferred command must be absent, not stubbed.

    Shipping `parse` as a "not implemented" response would put the name in
    --help and let a caller believe the capability exists.
    """

    def test_deferred_commands_are_not_registered(self):
        # `validate html` is deferred beyond 0.1.x. It must not appear as a
        # stub or a "not implemented" response: either would put the name in
        # --help and let a caller believe the capability exists.
        code, stdout, stderr = run_cli("validate")
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", stderr)
        self.assertEqual(stdout, "")

    def test_help_does_not_advertise_deferred_commands(self):
        _, stdout, _ = run_cli("--help")
        self.assertNotIn("    validate", stdout)

    def test_help_lists_every_implemented_command(self):
        _, stdout, _ = run_cli("--help")
        for name in ("providers", "parse", "editor", "export"):
            self.assertIn(f"    {name}", stdout)

    def test_export_advertises_only_the_format_it_implements(self):
        code, stdout, _ = run_cli("export", "--help")
        self.assertEqual(code, 0)
        self.assertIn("pptx", stdout)
        for absent in ("pdf", "png", "html"):
            self.assertNotIn(f"    {absent}", stdout)

    def test_export_without_a_format_shows_help_rather_than_failing_oddly(self):
        code, stdout, _ = run_cli("export")
        self.assertEqual(code, 0)
        self.assertIn("pptx", stdout)


class StdoutDisciplineTest(unittest.TestCase):
    def test_json_mode_emits_exactly_one_object(self):
        code, stdout, _ = run_cli("providers", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(stdout.strip().splitlines()), 1)
        payload = json.loads(stdout)
        self.assertEqual(payload["command"], "providers")
        self.assertEqual(payload["core_version"], __version__)

    def test_human_mode_keeps_json_off_stdout(self):
        code, stdout, _ = run_cli("providers")
        self.assertEqual(code, 0)
        self.assertIn("provider", stdout)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(stdout)

    def test_failures_still_produce_a_parseable_envelope(self):
        code, stdout, stderr = run_cli("providers", "install", "nope", "--json")
        self.assertEqual(code, 2)
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["diagnostics"][0]["rule_id"], "PROVIDER_UNKNOWN")
        self.assertIn("nope", stderr)

    def test_version_flag(self):
        code, stdout, _ = run_cli("--version")
        self.assertEqual(code, 0)
        self.assertIn(__version__, stdout)


class ReportTest(unittest.TestCase):
    def test_report_is_semantically_identical_to_stdout(self):
        with tempfile.TemporaryDirectory() as root:
            report = Path(root) / "providers.json"
            code, stdout, _ = run_cli("providers", "--json", "--report", str(report))
            self.assertEqual(code, 0)
            written = json.loads(report.read_text(encoding="utf-8"))
            emitted = json.loads(stdout)
            # Timestamps are generated per serialization; everything else must match.
            for payload in (written, emitted):
                payload.pop("started_at"), payload.pop("finished_at")
            self.assertEqual(written, emitted)

    def test_report_is_written_even_in_human_mode(self):
        with tempfile.TemporaryDirectory() as root:
            report = Path(root) / "providers.json"
            run_cli("providers", "--report", str(report))
            self.assertEqual(json.loads(report.read_text())["command"], "providers")


class ProvidersListingTest(unittest.TestCase):
    def test_listing_has_no_side_effects(self):
        with tempfile.TemporaryDirectory() as home:
            run_cli("providers", "--json", home=home)
            self.assertFalse((Path(home) / "providers").exists())

    def test_listing_reports_every_matrix_provider(self):
        _, stdout, _ = run_cli("providers", "--json")
        names = {entry["name"] for entry in json.loads(stdout)["providers"]}
        self.assertEqual(names, {"extract", "editor", "deckhtml"})

    def test_listing_discloses_download_size_and_what_each_unlocks(self):
        _, stdout, _ = run_cli("providers", "--json")
        for entry in json.loads(stdout)["providers"]:
            self.assertIn("approx_mb", entry)
            self.assertTrue(entry["unlocks"])
            self.assertIn(entry["status"], ("ready", "not-acquired", "blocked"))

    def test_remove_on_an_empty_cache_is_not_an_error(self):
        code, stdout, _ = run_cli("providers", "remove", "deckhtml", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["diagnostics"][0]["rule_id"], "PROVIDER_NOT_IN_CACHE")

    def test_bad_provider_bin_syntax_is_a_usage_error(self):
        code, _, stderr = run_cli("providers", "--provider-bin", "no-equals-sign", "--json")
        self.assertEqual(code, 2)

    def test_bad_policy_is_rejected_by_argparse(self):
        code, _, stderr = run_cli("providers", "--provider-install", "sometimes")
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", stderr)


class InProcessTest(unittest.TestCase):
    def test_bare_invocation_prints_help_and_succeeds(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main([])
        self.assertEqual(code, 0)
        self.assertIn("usage: deckflow", stdout.getvalue())

    def test_help_documents_the_network_and_write_boundary(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit) as caught:
            main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        # Collapse the help text's own wrapping before matching phrases.
        text = " ".join(stdout.getvalue().split())
        self.assertIn("network policy", text)
        self.assertIn("are never uploaded", text)
        self.assertIn("exit codes", text)
        self.assertIn("$DECKFLOW_HOME/providers", text)
        self.assertIn("Nothing is installed globally", text)


if __name__ == "__main__":
    unittest.main()
