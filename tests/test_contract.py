"""Envelope, diagnostics, versions and filesystem contract."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from deckflow_core import SCHEMA_VERSION, versions
from deckflow_core.diagnostics import Diagnostic, sort_diagnostics, summarize_output
from deckflow_core.envelope import EXTRACT_STATUS_MAP, Envelope
from deckflow_core.exits import CoreError
from deckflow_core.fsutil import (
    atomic_write_text,
    require_empty_dir,
    resolve_within,
    sha256_file,
)


class EnvelopeTest(unittest.TestCase):
    def test_required_fields_present(self):
        payload = Envelope(command="env check").to_json()
        for key in (
            "schema_version", "command", "core_version", "status",
            "started_at", "finished_at", "extract", "inputs", "outputs", "diagnostics",
        ):
            self.assertIn(key, payload)
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)

    def test_rejects_invented_status(self):
        envelope = Envelope(command="env check")
        envelope.status = "mostly-fine"
        with self.assertRaises(ValueError):
            envelope.to_json()

    def test_no_status_the_code_cannot_reach(self):
        # `cancelled` needed a long-running session; core no longer runs one,
        # and a status nothing emits only earns dead branches in callers.
        from deckflow_core.envelope import STATUSES

        self.assertEqual(set(STATUSES), {"succeeded", "partial", "failed"})

    def test_provider_result_is_carried_verbatim(self):
        native = {"status": "repairable", "gaps": [{"kind": "tables"}], "decision": {"usable": True}}
        envelope = Envelope(command="parse", provider_result=native)
        self.assertEqual(envelope.to_json()["provider_result"], native)

    def test_extract_status_map_is_total_and_explicit(self):
        # The provider's four states must all map; a new one must not fall
        # through to "succeeded" by accident.
        self.assertEqual(
            set(EXTRACT_STATUS_MAP),
            {"parsed", "repairable", "needs-input", "blocked"},
        )
        self.assertEqual(EXTRACT_STATUS_MAP["repairable"], "partial")
        self.assertEqual(EXTRACT_STATUS_MAP["needs-input"], "failed")


class DiagnosticsTest(unittest.TestCase):
    def test_severity_is_constrained(self):
        with self.assertRaises(ValueError):
            Diagnostic(rule_id="X_Y", severity="critical", message="m")

    def test_optional_fields_are_omitted_not_nulled(self):
        payload = Diagnostic(rule_id="A_B", severity="info", message="m").to_json()
        self.assertEqual(set(payload), {"rule_id", "severity", "message"})

    def test_order_is_deterministic_regardless_of_discovery_order(self):
        a = Diagnostic(rule_id="B_RULE", severity="warning", message="m", location="p2")
        b = Diagnostic(rule_id="A_RULE", severity="error", message="m", location="p9")
        c = Diagnostic(rule_id="A_RULE", severity="warning", message="m", location="p1")
        self.assertEqual(sort_diagnostics([a, b, c]), sort_diagnostics([c, a, b]))
        self.assertEqual(sort_diagnostics([a, b, c])[0], b)  # errors first


class FailureSummaryTest(unittest.TestCase):
    """A diagnostic must name the cause, not the log file that holds it."""

    def test_boilerplate_is_dropped_in_favour_of_the_real_error(self):
        stderr = (
            "WARNING: Running pip as the 'root' user can result in broken permissions.\n"
            "ERROR: Could not find a version that satisfies the requirement "
            "deckflow-extract==0.2.0\n"
            "ERROR: No matching distribution found for deckflow-extract==0.2.0\n"
            "[notice] A new release of pip is available: 24.0 -> 25.1\n"
        )
        summary = summarize_output(stderr)
        self.assertIn("No matching distribution", summary)
        self.assertIn("deckflow-extract==0.2.0", summary)
        self.assertNotIn("new release of pip", summary)
        self.assertNotIn("broken permissions", summary)

    def test_duplicate_lines_collapse(self):
        self.assertEqual(summarize_output("same\nsame\nsame"), "same")

    def test_empty_output_is_stated_plainly(self):
        self.assertEqual(summarize_output("", None), "no diagnostic output")


class VersionRangeTest(unittest.TestCase):
    def test_range_terms(self):
        self.assertTrue(versions.satisfies("0.3.2", ">=0.3.2 <0.4.0"))
        self.assertTrue(versions.satisfies("0.3.9", ">=0.3.2 <0.4.0"))
        self.assertFalse(versions.satisfies("0.4.0", ">=0.3.2 <0.4.0"))
        self.assertFalse(versions.satisfies("0.3.1", ">=0.3.2 <0.4.0"))

    def test_unparseable_version_never_satisfies(self):
        # Core prefers its own managed copy over trusting a version it cannot read.
        self.assertFalse(versions.satisfies(None, ">=0.1.0"))
        self.assertFalse(versions.satisfies("unknown", ">=0.1.0"))

    def test_extracts_version_from_noisy_cli_output(self):
        self.assertEqual(versions.normalize("deckflow-extract version 0.2.0 (local)"), "0.2.0")
        self.assertEqual(versions.normalize("v1.2"), "1.2.0")

    def test_release_outranks_its_prerelease(self):
        self.assertTrue(versions.satisfies("1.0.0", ">=1.0.0"))
        self.assertFalse(versions.satisfies("1.0.0-rc.1", ">=1.0.0"))

    def test_empty_spec_accepts_anything_parseable(self):
        self.assertTrue(versions.satisfies("9.9.9", ""))


class PathContainmentTest(unittest.TestCase):
    def test_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(CoreError) as caught:
                resolve_within(Path(root), Path("../outside.txt"))
            self.assertEqual(caught.exception.diagnostic.rule_id, "PATH_ESCAPES_ROOT")
            self.assertEqual(caught.exception.exit_code, 3)

    @unittest.skipIf(os.name == "nt", "creating symlinks on Windows needs elevation")
    def test_symlink_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as outer:
            root = Path(outer) / "root"
            root.mkdir()
            secret = Path(outer) / "secret.txt"
            secret.write_text("classified", encoding="utf-8")
            (root / "link").symlink_to(secret)
            with self.assertRaises(CoreError):
                resolve_within(root, root / "link")

    def test_inside_path_resolves(self):
        with tempfile.TemporaryDirectory() as root:
            resolved = resolve_within(Path(root), Path("a/b.txt"))
            self.assertTrue(str(resolved).startswith(str(Path(root).resolve())))


class AtomicWriteTest(unittest.TestCase):
    def test_write_leaves_no_temp_files(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "report.json"
            atomic_write_text(target, '{"ok": true}')
            self.assertEqual(json.loads(target.read_text()), {"ok": True})
            self.assertEqual([p.name for p in Path(root).iterdir()], ["report.json"])

    def test_failed_write_does_not_clobber_existing(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "keep.txt"
            target.write_text("original", encoding="utf-8")
            try:
                atomic_write_text(target, "x" * 8)
            finally:
                pass
            self.assertEqual(target.read_text(), "x" * 8)
            leftovers = [p.name for p in Path(root).iterdir() if p.name.endswith(".tmp")]
            self.assertEqual(leftovers, [])

    def test_sha256_is_stable(self):
        with tempfile.TemporaryDirectory() as root:
            empty = Path(root) / "empty"
            empty.write_bytes(b"")
            self.assertEqual(
                sha256_file(empty),
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            )


class OverwritePolicyTest(unittest.TestCase):
    def test_non_empty_output_dir_needs_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "out"
            target.mkdir()
            (target / "stale.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(CoreError) as caught:
                require_empty_dir(target, overwrite=False)
            self.assertEqual(caught.exception.diagnostic.rule_id, "OUTPUT_DIRECTORY_NOT_EMPTY")
            require_empty_dir(target, overwrite=True)

    def test_absent_dir_is_fine(self):
        with tempfile.TemporaryDirectory() as root:
            require_empty_dir(Path(root) / "not-there", overwrite=False)


if __name__ == "__main__":
    unittest.main()
