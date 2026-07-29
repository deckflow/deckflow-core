"""`deckflow parse` contract.

The extraction itself is the provider's. What is asserted here is core's part:
which inputs it accepts, what it forces on the provider's command line, how the
provider's status becomes a core status, and that the bundle comes through
untouched.
"""

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
from deckflow_core.envelope import EXTRACT_STATUS_MAP
from deckflow_core.exits import CoreError
from deckflow_core.providers import matrix
from deckflow_core.providers.resolve import Resolution

_SRC = str(Path(__file__).resolve().parents[1] / "src")


def run_parse(*args: str, home: str | None = None) -> tuple[int, str, str]:
    env = {**os.environ, "PYTHONPATH": _SRC, "DECKFLOW_HOME": home or tempfile.mkdtemp()}
    completed = subprocess.run(
        [sys.executable, "-m", "deckflow_core", "parse", *args],
        capture_output=True, text=True, env=env, timeout=300,
    )
    return completed.returncode, completed.stdout, completed.stderr


def options(**overrides) -> argparse.Namespace:
    base = dict(
        input="x", out="y", overwrite=False, mode="local", upgrade="never",
        type=None, ocr=None, max_pages=None, max_table_rows=None,
        provider_timeout=None, timeout=900, json=True,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def fake_resolution() -> Resolution:
    return Resolution(spec=matrix.get("extract"), command=["/fake/deckflow-extract"])


class InputPolicyTest(unittest.TestCase):
    def test_url_is_refused_rather_than_forwarded(self):
        """Core promises the content plane never reaches the network.

        The provider can fetch URLs; core does not use that, because a promise
        with an exception in it is not worth stating.
        """
        for url in ("https://example.com/post", "http://x.test", "ftp://f.test/a"):
            with self.assertRaises(CoreError) as caught:
                parse_cmd._resolve_input(url)
            self.assertEqual(caught.exception.diagnostic.rule_id, "PARSE_INPUT_NOT_LOCAL")
            self.assertEqual(caught.exception.exit_code, 3)

    def test_the_url_refusal_names_the_direct_command(self):
        with self.assertRaises(CoreError) as caught:
            parse_cmd._resolve_input("https://example.com")
        self.assertIn("deckflow-extract parse", caught.exception.diagnostic.recovery)

    def test_missing_input_is_an_input_error(self):
        with self.assertRaises(CoreError) as caught:
            parse_cmd._resolve_input("/definitely/not/here.pdf")
        self.assertEqual(caught.exception.diagnostic.rule_id, "PARSE_INPUT_MISSING")

    def test_directory_input_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(CoreError) as caught:
                parse_cmd._resolve_input(root)
            self.assertEqual(caught.exception.diagnostic.rule_id, "PARSE_INPUT_NOT_A_FILE")

    def test_a_windows_drive_letter_is_not_mistaken_for_a_url(self):
        # `C:\...` must not match the URL scheme pattern.
        with self.assertRaises(CoreError) as caught:
            parse_cmd._resolve_input(r"C:\docs\report.pdf")
        self.assertEqual(caught.exception.diagnostic.rule_id, "PARSE_INPUT_MISSING")


class CommandConstructionTest(unittest.TestCase):
    """What core forces on the provider, regardless of the caller."""

    def _command(self, **overrides) -> list[str]:
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "in.md"
            source.write_text("# x", encoding="utf-8")
            return parse_cmd._build_command(
                fake_resolution(), source, Path(root) / "out", options(**overrides)
            )

    def test_local_mode_is_forced_by_default(self):
        command = self._command()
        self.assertIn("--mode", command)
        self.assertEqual(command[command.index("--mode") + 1], "local")

    def test_cloud_only_when_explicitly_requested(self):
        command = self._command(mode="cloud")
        self.assertEqual(command[command.index("--mode") + 1], "cloud")

    def test_remote_image_fetching_is_always_disabled(self):
        # Off for local inputs already; stated explicitly so a change to the
        # provider's default cannot put the content plane on the network.
        command = self._command()
        self.assertEqual(command[command.index("--fetch-remote-images") + 1], "off")

    def test_engine_upgrades_default_to_never(self):
        """A 56MB engine download is the user's call, not a side effect.

        Provider *acquisition* defaults to auto; the provider's own optional
        engines do not, because they change what the extraction produces.
        """
        command = self._command()
        self.assertEqual(command[command.index("--upgrade") + 1], "never")

    def test_overwrite_maps_to_the_provider_flag(self):
        self.assertNotIn("--replace", self._command())
        self.assertIn("--replace", self._command(overwrite=True))

    def test_unset_passthroughs_are_omitted_entirely(self):
        command = self._command()
        for absent in ("--type", "--ocr", "--max-pages", "--max-table-rows", "--timeout"):
            self.assertNotIn(absent, command)

    def test_set_passthroughs_are_forwarded(self):
        command = self._command(ocr="auto", max_pages=10, type="pdf")
        self.assertEqual(command[command.index("--ocr") + 1], "auto")
        self.assertEqual(command[command.index("--max-pages") + 1], "10")
        self.assertEqual(command[command.index("--type") + 1], "pdf")


class CredentialWithholdingTest(unittest.TestCase):
    def test_cloud_keys_are_removed_for_a_local_run(self):
        with unittest.mock.patch.dict(
            os.environ, {"DECKFLOW_API_KEY": "secret", "DECKFLOW_SPACE_ID": "s", "PATH": "/usr/bin"}
        ):
            env = parse_cmd._environ(cloud=False)
            self.assertNotIn("DECKFLOW_API_KEY", env)
            self.assertNotIn("DECKFLOW_SPACE_ID", env)
            self.assertIn("PATH", env)

    def test_cloud_keys_are_kept_when_cloud_was_requested(self):
        with unittest.mock.patch.dict(os.environ, {"DECKFLOW_API_KEY": "secret"}):
            self.assertIn("DECKFLOW_API_KEY", parse_cmd._environ(cloud=True))


class StatusMappingTest(unittest.TestCase):
    def test_provider_states_map_as_documented(self):
        self.assertEqual(EXTRACT_STATUS_MAP["parsed"], "succeeded")
        self.assertEqual(EXTRACT_STATUS_MAP["repairable"], "partial")
        self.assertEqual(EXTRACT_STATUS_MAP["needs-input"], "failed")
        self.assertEqual(EXTRACT_STATUS_MAP["blocked"], "failed")

    def test_an_unknown_provider_state_is_never_treated_as_success(self):
        self.assertEqual(EXTRACT_STATUS_MAP.get("brand-new-state", "failed"), "failed")

    def test_upgrade_recommendation_is_surfaced_but_not_acted_on(self):
        from deckflow_core.envelope import Envelope

        envelope = Envelope(command="parse")
        parse_cmd._carry_diagnostics(envelope, {
            "tier": 0,
            "decision": {"usable": True, "recommended": "cloud", "reason": "图片 提取 5/65"},
        })
        rules = [d.rule_id for d in envelope.diagnostics]
        self.assertIn("PARSE_UPGRADE_AVAILABLE", rules)
        found = next(d for d in envelope.diagnostics if d.rule_id == "PARSE_UPGRADE_AVAILABLE")
        self.assertEqual(found.severity, "info")
        self.assertIn("The choice is the user's", found.recovery)

    def test_accept_produces_no_upgrade_noise(self):
        from deckflow_core.envelope import Envelope

        envelope = Envelope(command="parse")
        parse_cmd._carry_diagnostics(envelope, {"decision": {"recommended": "accept"}})
        self.assertEqual([d.rule_id for d in envelope.diagnostics], [])


class CliTest(unittest.TestCase):
    def test_non_empty_output_directory_needs_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "in.md"
            source.write_text("# x", encoding="utf-8")
            out = Path(root) / "out"
            out.mkdir()
            (out / "stale.json").write_text("{}", encoding="utf-8")
            code, stdout, _ = run_parse(
                str(source), "--out", str(out), "--json", "--provider-install", "never"
            )
            self.assertEqual(code, 6)
            self.assertEqual(
                json.loads(stdout)["diagnostics"][0]["rule_id"], "OUTPUT_DIRECTORY_NOT_EMPTY"
            )

    def test_overwrite_refuses_an_unowned_non_empty_directory(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "in.md"
            source.write_text("# x", encoding="utf-8")
            out = Path(root) / "out"
            out.mkdir()
            unrelated = out / "important.txt"
            unrelated.write_text("keep", encoding="utf-8")
            code, stdout, _ = run_parse(
                str(source), "--out", str(out), "--overwrite", "--json",
                "--provider-install", "never",
            )
            self.assertEqual(code, 6)
            self.assertEqual(
                json.loads(stdout)["diagnostics"][0]["rule_id"], "PARSE_OVERWRITE_UNOWNED"
            )
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")

    def test_overwrite_accepts_a_complete_owned_parse_bundle(self):
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / "out"
            (out / "assets").mkdir(parents=True)
            (out / "document.md").write_text("# old", encoding="utf-8")
            (out / "parse-manifest.json").write_text(
                json.dumps({
                    "schema_version": 2,
                    "tool": {"name": "deckflow-extract", "version": "0.2.0"},
                    "outputs": {"document": "document.md", "assets_dir": "assets"},
                }),
                encoding="utf-8",
            )
            parse_cmd._require_safe_output(out, overwrite=True)

    def test_overwrite_refuses_when_the_bundle_contains_the_input(self):
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / "out"
            (out / "assets").mkdir(parents=True)
            source = out / "document.md"
            source.write_text("# preserve me", encoding="utf-8")
            (out / "parse-manifest.json").write_text(
                json.dumps({
                    "schema_version": 2,
                    "tool": {"name": "deckflow-extract", "version": "0.2.0"},
                    "outputs": {"document": "document.md", "assets_dir": "assets"},
                }),
                encoding="utf-8",
            )
            code, stdout, _ = run_parse(
                str(source), "--out", str(out), "--overwrite", "--json",
                "--provider-install", "never",
            )
            self.assertEqual(code, 6)
            self.assertEqual(
                json.loads(stdout)["diagnostics"][0]["rule_id"],
                "PARSE_OUTPUT_CONTAINS_INPUT",
            )
            self.assertEqual(source.read_text(encoding="utf-8"), "# preserve me")

    def test_report_cannot_alias_the_input_file(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "in.md"
            source.write_text("# preserve me", encoding="utf-8")
            code, stdout, _ = run_parse(
                str(source), "--out", str(Path(root) / "out"),
                "--report", str(source), "--json", "--provider-install", "never",
            )
            self.assertEqual(code, 6)
            self.assertEqual(
                json.loads(stdout)["diagnostics"][0]["rule_id"], "REPORT_PATH_CONFLICT"
            )
            self.assertEqual(source.read_text(encoding="utf-8"), "# preserve me")

    def test_input_is_checked_before_the_provider_is_touched(self):
        code, stdout, _ = run_parse(
            "https://example.com", "--out", "/tmp/never-created", "--json",
            "--provider-install", "never",
        )
        payload = json.loads(stdout)
        self.assertEqual(code, 3)
        self.assertEqual(payload["diagnostics"][0]["rule_id"], "PARSE_INPUT_NOT_LOCAL")
        self.assertEqual(payload["providers"], [])
        self.assertFalse(Path("/tmp/never-created").exists())

    def test_help_states_the_boundaries(self):
        env = {**os.environ, "PYTHONPATH": _SRC}
        completed = subprocess.run(
            [sys.executable, "-m", "deckflow_core", "parse", "--help"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        text = " ".join(completed.stdout.split())
        self.assertIn("never uploaded", text)
        self.assertIn("not a URL", text)
        self.assertIn("passed through untouched", text)
        self.assertIn("acquired on demand", text)


@unittest.skipUnless(
    os.environ.get("DECKFLOW_LIVE_TESTS") == "1",
    "live extraction: set DECKFLOW_LIVE_TESTS=1 (acquires the provider, ~4MB)",
)
class LiveParseTest(unittest.TestCase):
    _MARKDOWN = "# Title\n\nRevenue was 12.0M, up 18%.\n\n| Region | Revenue |\n| --- | --- |\n| East | 620 |\n"

    def test_bundle_is_produced_and_content_survives_verbatim(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "notes.md"
            source.write_text(self._MARKDOWN, encoding="utf-8")
            out = Path(root) / "bundle"
            code, stdout, stderr = run_parse(str(source), "--out", str(out), "--json")
            self.assertEqual(code, 0, stderr)

            payload = json.loads(stdout)
            self.assertEqual(payload["status"], "succeeded")
            self.assertEqual(payload["parse_status"], "parsed")
            self.assertTrue((out / "parse-manifest.json").is_file())

            # Core must not rewrite the bundle: exact figures survive.
            document = (out / "document.md").read_text(encoding="utf-8")
            self.assertIn("12.0M", document)
            self.assertIn("18%", document)
            self.assertIn("| East | 620 |", document)

    def test_provider_payload_reaches_the_caller_intact(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "notes.md"
            source.write_text(self._MARKDOWN, encoding="utf-8")
            _, stdout, _ = run_parse(str(source), "--out", str(Path(root) / "b"), "--json")
            native = json.loads(stdout)["provider_result"]
            for key in ("status", "tier", "fidelity", "decision", "recommendations", "gaps"):
                self.assertIn(key, native, f"{key} was dropped on the way through core")


if __name__ == "__main__":
    unittest.main()
