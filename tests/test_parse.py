"""Public `deckflow parse` policy and provider orchestration tests."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from deckflow_core.commands import parse as parse_cmd
from deckflow_core.envelope import EXTRACT_STATUS_MAP, Envelope
from deckflow_core.exits import CoreError
from deckflow_core.extract.resolve import STATUS_READY, Extract

_SRC = str(Path(__file__).resolve().parents[1] / "src")


def run_parse(*args: str, home: str | None = None) -> tuple[int, str, str]:
    env = {**os.environ, "PYTHONPATH": _SRC, "DECKFLOW_HOME": home or tempfile.mkdtemp()}
    completed = subprocess.run(
        [sys.executable, "-m", "deckflow_core", "parse", *args],
        capture_output=True,
        check=False,
        text=True,
        env=env,
        timeout=300,
    )
    return completed.returncode, completed.stdout, completed.stderr


def options(**overrides) -> argparse.Namespace:
    base = {
        "input": "x",
        "project": "project",
        "brief": "Create a summary",
        "deck_language": "zh-CN",
        "title": None,
        "replace": False,
        "mode": "local",
        "upgrade": "never",
        "type": None,
        "ocr": None,
        "max_pages": None,
        "max_table_rows": None,
        "provider_timeout": None,
        "timeout": 900,
        "human": False,
        "extract_bin": None,
        "offline": False,
        "skill_root": None,
        "report": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def fake_resolution() -> Extract:
    return Extract(
        status=STATUS_READY,
        version="0.3.0",
        command=["/fake/deckflow-extract"],
    )


class InputPolicyTest(unittest.TestCase):
    def test_url_is_refused(self) -> None:
        for url in ("https://example.com/post", "http://x.test", "ftp://f.test/a"):
            with self.assertRaises(CoreError) as caught:
                parse_cmd._resolve_input(url)
            self.assertEqual(caught.exception.diagnostic.rule_id, "PARSE_INPUT_NOT_LOCAL")
            self.assertEqual(caught.exception.exit_code, 3)

    def test_missing_input_is_an_input_error(self) -> None:
        with self.assertRaises(CoreError) as caught:
            parse_cmd._resolve_input("/definitely/not/here.pdf")
        self.assertEqual(caught.exception.diagnostic.rule_id, "PARSE_INPUT_MISSING")

    def test_directory_and_symlink_inputs_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            source = root_path / "source.md"
            source.write_text("# x\n", encoding="utf-8")
            link = root_path / "link.md"
            link.symlink_to(source)
            for path in (root_path, link):
                with self.assertRaises(CoreError) as caught:
                    parse_cmd._resolve_input(str(path))
                self.assertEqual(
                    caught.exception.diagnostic.rule_id,
                    "PARSE_INPUT_NOT_A_FILE",
                )

    def test_hardlinked_input_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source.md"
            source.write_text("# x\n", encoding="utf-8")
            linked = Path(root) / "linked.md"
            os.link(source, linked)
            with self.assertRaises(CoreError) as caught:
                parse_cmd._resolve_input(str(source))
            self.assertEqual(
                caught.exception.diagnostic.rule_id,
                "PARSE_INPUT_HARDLINKED",
            )

    def test_project_must_be_a_direct_existing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            project = Path(root) / "project"
            project.mkdir()
            link = Path(root) / "project-link"
            link.symlink_to(project, target_is_directory=True)
            with self.assertRaises(CoreError) as caught:
                parse_cmd._resolve_project(str(link))
            self.assertEqual(caught.exception.diagnostic.rule_id, "PARSE_PROJECT_INVALID")

    def test_windows_drive_letter_is_not_mistaken_for_url(self) -> None:
        with self.assertRaises(CoreError) as caught:
            parse_cmd._resolve_input(r"C:\docs\report.pdf")
        self.assertEqual(caught.exception.diagnostic.rule_id, "PARSE_INPUT_MISSING")


class CommandConstructionTest(unittest.TestCase):
    def _command(self, **overrides) -> list[str]:
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "in.md"
            source.write_text("# x", encoding="utf-8")
            return parse_cmd._build_command(
                fake_resolution(),
                source,
                Path(root) / "transient-parse",
                options(**overrides),
            )

    def test_core_forces_a_new_replaceable_transient_bundle(self) -> None:
        command = self._command()
        self.assertIn("--out", command)
        self.assertIn("--replace", command)
        self.assertEqual(command[command.index("--anchors") + 1], "on")

    def test_luna_metadata_is_not_forwarded_to_extract(self) -> None:
        command = self._command()
        for absent in ("--project", "--brief", "--deck-language", "--title"):
            self.assertNotIn(absent, command)

    def test_local_mode_and_remote_image_policy_are_explicit(self) -> None:
        command = self._command()
        self.assertEqual(command[command.index("--mode") + 1], "local")
        self.assertEqual(command[command.index("--fetch-remote-images") + 1], "off")

    def test_cloud_only_when_requested(self) -> None:
        command = self._command(mode="cloud")
        self.assertEqual(command[command.index("--mode") + 1], "cloud")

    def test_engine_upgrade_defaults_to_never(self) -> None:
        command = self._command()
        self.assertEqual(command[command.index("--upgrade") + 1], "never")

    def test_passthrough_options_are_only_added_when_set(self) -> None:
        command = self._command(ocr="auto", max_pages=10, type="pdf")
        self.assertEqual(command[command.index("--ocr") + 1], "auto")
        self.assertEqual(command[command.index("--max-pages") + 1], "10")
        self.assertEqual(command[command.index("--type") + 1], "pdf")
        self.assertNotIn("--timeout", command)


class CredentialWithholdingTest(unittest.TestCase):
    def test_cloud_secrets_are_removed_for_local_parse(self) -> None:
        with unittest.mock.patch.dict(
            os.environ,
            {
                "DECKFLOW_API_KEY": "secret",
                "DECKOPS_API_KEY": "secret",
                "DECKFLOW_TOKEN": "token",
                "DECKFLOW_SPACE_ID": "space",
                "PATH": "/usr/bin",
            },
        ):
            env = parse_cmd._environ(cloud=False)
            for key in (
                "DECKFLOW_API_KEY",
                "DECKOPS_API_KEY",
                "DECKFLOW_TOKEN",
                "DECKFLOW_SPACE_ID",
            ):
                self.assertNotIn(key, env)
            self.assertEqual(env["DECKFLOW_NO_STORED_CREDENTIALS"], "1")
            self.assertIn("PATH", env)

    def test_provider_sidecar_uses_configurable_deckflow_home(self) -> None:
        with unittest.mock.patch.dict(
            os.environ,
            {"DECKFLOW_HOME": "/tmp/deckflow-test-home"},
        ):
            env = parse_cmd._environ(cloud=False)
        self.assertEqual(
            env["DECKFLOW_EXTRACT_HOME"],
            str(Path("/tmp/deckflow-test-home") / "parse"),
        )

    def test_cloud_secrets_are_kept_only_for_explicit_cloud_mode(self) -> None:
        with unittest.mock.patch.dict(
            os.environ,
            {
                "DECKFLOW_API_KEY": "secret",
                "DECKFLOW_HOME": "/tmp/deckflow-home",
                "DECKFLOW_CONFIG_DIR": "",
                "DECKHTML_CONFIG_DIR": "",
            },
        ):
            env = parse_cmd._environ(cloud=True)
        self.assertIn("DECKFLOW_API_KEY", env)
        self.assertNotIn("DECKFLOW_NO_STORED_CREDENTIALS", env)
        self.assertEqual(env["DECKFLOW_CONFIG_DIR"], str(Path("/tmp/deckflow-home")))


class StatusAndSanitizationTest(unittest.TestCase):
    def test_provider_states_map_as_documented(self) -> None:
        self.assertEqual(EXTRACT_STATUS_MAP["parsed"], "succeeded")
        self.assertEqual(EXTRACT_STATUS_MAP["repairable"], "partial")
        self.assertEqual(EXTRACT_STATUS_MAP["needs-input"], "failed")
        self.assertEqual(EXTRACT_STATUS_MAP["blocked"], "failed")

    def test_provider_result_drops_transient_paths_and_commands(self) -> None:
        result = parse_cmd._public_provider_result(
            {
                "status": "parsed",
                "bundle": "/tmp/run/parse",
                "manifest": "/tmp/run/parse/parse-manifest.json",
                "archive": "/tmp/run/parse.zip",
                "engine_acquisition": {
                    "status": "failed",
                    "sidecar": "/tmp/engine",
                    "commands": ["install"],
                    "error": "failed at /tmp/private/file",
                },
            },
            secrets=["/tmp/run"],
        )
        text = json.dumps(result)
        self.assertNotIn("bundle", result)
        self.assertNotIn("manifest", result)
        self.assertNotIn("archive", result)
        self.assertNotIn("sidecar", text)
        self.assertNotIn("commands", text)
        self.assertNotIn("/tmp/", text)

    def test_upgrade_recommendation_is_surfaced(self) -> None:
        envelope = Envelope(command="parse")
        parse_cmd._carry_diagnostics(
            envelope,
            {
                "tier": 0,
                "decision": {
                    "usable": True,
                    "recommended": "install",
                    "reason": "shape provenance unavailable",
                },
            },
        )
        diagnostic = next(
            item
            for item in envelope.diagnostics
            if item.rule_id == "PARSE_UPGRADE_AVAILABLE"
        )
        self.assertIn("--upgrade auto", diagnostic.recovery)

    def test_accept_produces_no_upgrade_noise(self) -> None:
        envelope = Envelope(command="parse")
        parse_cmd._carry_diagnostics(
            envelope,
            {"decision": {"recommended": "accept"}},
        )
        self.assertEqual(envelope.diagnostics, [])


class CliBoundaryTest(unittest.TestCase):
    def test_input_is_checked_before_provider_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            code, stdout, _ = run_parse(
                "https://example.com",
                "--project",
                root,
                "--brief",
                "Create a summary",
                "--deck-language",
                "en-US",
                "--offline",
            )
        payload = json.loads(stdout)
        self.assertEqual(code, 3)
        self.assertEqual(payload["diagnostics"][0]["rule_id"], "PARSE_INPUT_NOT_LOCAL")
        self.assertIsNone(payload["extract"])

    def test_invalid_deck_language_is_rejected_before_provider_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "input.md"
            source.write_text("# x\n", encoding="utf-8")
            code, stdout, _ = run_parse(
                str(source),
                "--project",
                root,
                "--brief",
                "Create a summary",
                "--deck-language",
                "not a tag",
                "--offline",
            )
        payload = json.loads(stdout)
        self.assertEqual(code, 3)
        self.assertEqual(
            payload["diagnostics"][0]["rule_id"],
            "PARSE_DECK_LANGUAGE_INVALID",
        )
        self.assertIsNone(payload["extract"])

    def test_report_cannot_be_inside_source_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "input.md"
            source.write_text("# x\n", encoding="utf-8")
            report = Path(root) / "source-bundle" / "report.json"
            code, stdout, _ = run_parse(
                str(source),
                "--project",
                root,
                "--brief",
                "Create a summary",
                "--deck-language",
                "en-US",
                "--report",
                str(report),
                "--offline",
            )
        payload = json.loads(stdout)
        self.assertEqual(code, 6)
        self.assertEqual(payload["diagnostics"][0]["rule_id"], "REPORT_PATH_CONFLICT")
        self.assertFalse(report.exists())

    def test_help_states_new_boundary_and_no_old_output_flags(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "deckflow_core", "parse", "--help"],
            capture_output=True,
            check=False,
            text=True,
            env={**os.environ, "PYTHONPATH": _SRC},
            timeout=60,
        )
        text = " ".join(completed.stdout.split())
        self.assertIn("canonical Source Bundle", text)
        self.assertIn("without AI", text)
        self.assertIn("--deck-language", text)
        self.assertNotIn("--out", text)
        self.assertNotIn("--overwrite", text)
        self.assertNotIn("--replace-confirmed", text)


if __name__ == "__main__":
    unittest.main()
