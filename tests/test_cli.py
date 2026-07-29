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
    env.pop("DECKFLOW_SKILL_ROOT", None)
    env.pop("DECKFLOW_EXTRACT_BIN", None)
    completed = subprocess.run(
        [sys.executable, "-m", "deckflow_core", *args],
        capture_output=True, text=True, env=env, timeout=120,
    )
    return completed.returncode, completed.stdout, completed.stderr


class UnbrokeredCommandsTest(unittest.TestCase):
    """A capability core does not broker must be absent, not stubbed.

    Shipping `editor` or `export pptx` as a thin forwarder would put the name
    in --help and let a caller believe core owns the contract. The Skill calls
    html-editor and deckhtml directly instead. `providers` is on this list for
    a different reason: it was core's own word for one package, and the
    capability now lives under `env`.
    """

    def test_unbrokered_commands_are_not_registered(self):
        for name in ("editor", "export", "validate", "providers"):
            with self.subTest(command=name):
                code, stdout, stderr = run_cli(name)
                self.assertEqual(code, 2)
                self.assertIn("invalid choice", stderr)
                self.assertEqual(stdout, "")

    def test_help_does_not_advertise_unbrokered_commands(self):
        _, stdout, _ = run_cli("--help")
        for name in ("editor", "export", "validate", "providers"):
            self.assertNotIn(f"    {name}", stdout)

    def test_help_lists_every_implemented_command(self):
        _, stdout, _ = run_cli("--help")
        for name in ("env", "auth", "parse", "update"):
            self.assertIn(f"    {name}", stdout)


class StdoutDisciplineTest(unittest.TestCase):
    """JSON is the default, because every caller of this CLI is a program."""

    def test_json_is_the_default_and_is_exactly_one_object(self):
        code, stdout, _ = run_cli("env", "check")
        self.assertEqual(code, 0)
        self.assertEqual(len(stdout.strip().splitlines()), 1)
        payload = json.loads(stdout)
        self.assertEqual(payload["command"], "env check")
        self.assertEqual(payload["core_version"], __version__)

    def test_human_mode_keeps_json_off_stdout(self):
        code, stdout, _ = run_cli("env", "check", "--human")
        self.assertEqual(code, 0)
        self.assertIn("python", stdout)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(stdout)

    def test_a_common_flag_works_before_the_subcommand_too(self):
        """`deckflow env --human check` must not be silently reset to JSON.

        A subparser that re-declares a parent's flag overwrites the parsed
        value with its own default unless the default is suppressed.
        """
        code, stdout, _ = run_cli("env", "--human", "check")
        self.assertEqual(code, 0)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(stdout)

    def test_failures_still_produce_a_parseable_envelope(self):
        code, stdout, stderr = run_cli("env", "setup", "--offline")
        self.assertEqual(code, 5)
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["diagnostics"][0]["rule_id"], "EXTRACT_MISSING")
        self.assertIn("offline", stderr)

    def test_version_flag(self):
        code, stdout, _ = run_cli("--version")
        self.assertEqual(code, 0)
        self.assertIn(__version__, stdout)


class ReportTest(unittest.TestCase):
    def test_report_is_semantically_identical_to_stdout(self):
        with tempfile.TemporaryDirectory() as root:
            report = Path(root) / "env.json"
            code, stdout, _ = run_cli("env", "check", "--report", str(report))
            self.assertEqual(code, 0)
            written = json.loads(report.read_text(encoding="utf-8"))
            emitted = json.loads(stdout)
            # Timestamps are generated per serialization; everything else must match.
            for payload in (written, emitted):
                payload.pop("started_at"), payload.pop("finished_at")
            self.assertEqual(written, emitted)

    def test_report_is_written_even_in_human_mode(self):
        with tempfile.TemporaryDirectory() as root:
            report = Path(root) / "env.json"
            run_cli("env", "check", "--human", "--report", str(report))
            self.assertEqual(json.loads(report.read_text())["command"], "env check")

    def test_existing_report_is_not_silently_overwritten(self):
        with tempfile.TemporaryDirectory() as root:
            report = Path(root) / "env.json"
            report.write_text("keep me", encoding="utf-8")
            code, stdout, _ = run_cli("env", "check", "--report", str(report))
            self.assertEqual(code, 6)
            self.assertEqual(json.loads(stdout)["diagnostics"][0]["rule_id"], "REPORT_EXISTS")
            self.assertEqual(report.read_text(encoding="utf-8"), "keep me")

    def test_report_target_cannot_be_a_directory(self):
        with tempfile.TemporaryDirectory() as root:
            code, stdout, _ = run_cli("env", "check", "--report", root)
            self.assertEqual(code, 6)
            self.assertEqual(
                json.loads(stdout)["diagnostics"][0]["rule_id"], "REPORT_NOT_A_FILE"
            )


class EnvCommandTest(unittest.TestCase):
    def test_check_has_no_side_effects(self):
        """Documented as safe to run on every invocation, so it must be."""
        with tempfile.TemporaryDirectory() as home:
            run_cli("env", "check", home=home)
            self.assertEqual(list(Path(home).iterdir()), [])

    def test_bare_env_is_the_check(self):
        code, stdout, _ = run_cli("env")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["command"], "env check")

    def test_check_exits_zero_even_with_nothing_installed(self):
        """It is a report, not an assertion.

        A non-zero exit on the first line of a Skill's prerequisites tells an
        agent the Skill is broken, and it will then try to repair a machine
        that is fine.
        """
        with tempfile.TemporaryDirectory() as home:
            code, stdout, _ = run_cli("env", "check", home=home)
            self.assertEqual(code, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload["status"], "succeeded")
            self.assertEqual(payload["extract"]["status"], "not-acquired")

    def test_check_discloses_the_download_before_a_long_job(self):
        code, stdout, _ = run_cli("env", "check")
        extract = json.loads(stdout)["extract"]
        self.assertIn("download_mb", extract)
        self.assertIn(extract["status"], ("ready", "not-acquired"))

    def test_clean_on_an_empty_home_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as home:
            code, stdout, _ = run_cli("env", "clean", home=home)
            self.assertEqual(code, 0)
            self.assertEqual(
                json.loads(stdout)["diagnostics"][0]["rule_id"], "EXTRACT_NOT_IN_CACHE"
            )

    def test_clean_removes_only_the_managed_extract(self):
        with tempfile.TemporaryDirectory() as home:
            managed = Path(home) / "extract" / "0.3.0"
            managed.mkdir(parents=True)
            (Path(home) / "credentials").write_text("{}", encoding="utf-8")
            (Path(home) / "parse").mkdir()
            code, _, _ = run_cli("env", "clean", home=home)
            self.assertEqual(code, 0)
            self.assertFalse((Path(home) / "extract").exists())
            # The provider's own engine sidecars and the shared credential file
            # are inside our home but are not ours to delete.
            self.assertTrue((Path(home) / "credentials").is_file())
            self.assertTrue((Path(home) / "parse").is_dir())


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
        self.assertIn("$DECKFLOW_HOME", text)
        self.assertIn("Nothing is installed globally", text)


if __name__ == "__main__":
    unittest.main()
