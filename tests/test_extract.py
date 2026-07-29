"""The pin, the resolution ladder, and the managed install.

This is what survived deleting the provider abstraction: one pinned package,
four rungs, and one directory. The tests that went with it — a matrix loader,
publication reachability, per-run version overrides — described machinery that
existed to hold N providers, and there is one.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deckflow_core import versions
from deckflow_core.exits import CoreError
from deckflow_core.extract import install as installer
from deckflow_core.extract import pin
from deckflow_core.extract import resolve as resolver


class PinTest(unittest.TestCase):
    def test_the_pin_is_exact_never_floating(self):
        self.assertNotIn(pin.VERSION, ("latest", "*", ""))
        self.assertEqual(pin.REQUIREMENT, f"{pin.PACKAGE}=={pin.VERSION}")

    def test_the_pinned_version_satisfies_its_own_compatible_range(self):
        """A pin outside its range would make every ambient install look wrong."""
        self.assertTrue(versions.satisfies(pin.VERSION, pin.COMPATIBLE))

    def test_the_source_fallback_names_the_pinned_version(self):
        """A stale fallback silently installs the wrong version when PyPI is down."""
        self.assertIn(pin.VERSION, pin.SOURCE)

    def test_the_module_is_importable_by_name(self):
        self.assertEqual(pin.MODULE, pin.PACKAGE.replace("-", "_"))


class ResolutionLadderTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="deckflow-home-"))
        self._clear_override = mock.patch.dict(os.environ, {}, clear=False)
        self._clear_override.start()
        os.environ.pop("DECKFLOW_EXTRACT_BIN", None)
        os.environ.pop("DECKFLOW_OFFLINE", None)

    def tearDown(self):
        self._clear_override.stop()

    def test_probe_only_never_acquires(self):
        with mock.patch.object(resolver, "acquire", side_effect=AssertionError("must not acquire")):
            result = resolver.resolve(home=self.home, probe_only=True)
        self.assertFalse(result.acquired)
        self.assertIn(result.status, ("not-acquired", "ready"))

    def test_missing_override_path_fails_loudly(self):
        with self.assertRaises(CoreError) as caught:
            resolver.resolve(home=self.home, bin_override="/definitely/not/here")
        self.assertEqual(caught.exception.diagnostic.rule_id, "EXTRACT_OVERRIDE_MISSING")

    @unittest.skipIf(os.name == "nt", "resolves a POSIX shell script as the provider binary")
    def test_override_wins_over_everything(self):
        fake = self.home / "fake-extract"
        fake.parent.mkdir(parents=True, exist_ok=True)
        fake.write_text(f"#!/bin/sh\necho {pin.VERSION}\n", encoding="utf-8")
        fake.chmod(0o755)
        with mock.patch.object(resolver, "acquire", side_effect=AssertionError("must not acquire")):
            result = resolver.resolve(home=self.home, bin_override=str(fake))
        self.assertEqual(result.resolution, "override")
        self.assertTrue(result.ready)
        self.assertEqual(result.command, [str(fake)])

    @unittest.skipIf(os.name == "nt", "resolves a POSIX shell script as the provider binary")
    def test_an_override_at_the_wrong_version_warns_but_still_runs(self):
        fake = self.home / "dev-extract"
        fake.parent.mkdir(parents=True, exist_ok=True)
        fake.write_text("#!/bin/sh\necho 9.9.9-dev\n", encoding="utf-8")
        fake.chmod(0o755)
        result = resolver.resolve(home=self.home, bin_override=str(fake))
        self.assertTrue(result.ready)
        self.assertEqual(
            [d.rule_id for d in result.diagnostics], ["EXTRACT_OVERRIDE_VERSION_MISMATCH"]
        )

    def test_offline_refuses_instead_of_downloading(self):
        with mock.patch.object(resolver, "_ambient", return_value=None), \
             mock.patch.object(resolver, "acquire", side_effect=AssertionError("must not acquire")):
            with self.assertRaises(CoreError) as caught:
                resolver.resolve(home=self.home, offline=True)
        self.assertEqual(caught.exception.diagnostic.rule_id, "EXTRACT_MISSING")
        self.assertEqual(caught.exception.exit_code, 5)

    def test_offline_can_be_set_in_the_environment(self):
        with mock.patch.dict(os.environ, {"DECKFLOW_OFFLINE": "1"}):
            self.assertTrue(resolver.offline_from(False))
        with mock.patch.dict(os.environ, {"DECKFLOW_OFFLINE": "0"}):
            self.assertFalse(resolver.offline_from(False))
        self.assertTrue(resolver.offline_from(True))

    def test_incompatible_ambient_is_ignored_with_a_warning(self):
        """A user's global install may not quietly change what a pinned run executes."""
        with mock.patch.object(resolver.shutil, "which", return_value="/usr/local/bin/deckflow-extract"), \
             mock.patch.object(resolver, "_probe_version", return_value="0.1.0"):
            result = resolver.resolve(home=self.home, probe_only=True)
        self.assertEqual(result.resolution, "missing")
        self.assertEqual(
            [d.rule_id for d in result.diagnostics], ["EXTRACT_VERSION_MISMATCH"]
        )

    def test_compatible_ambient_is_used_as_is(self):
        with mock.patch.object(resolver.shutil, "which", return_value="/usr/local/bin/deckflow-extract"), \
             mock.patch.object(resolver, "_probe_version", return_value=pin.VERSION):
            result = resolver.resolve(home=self.home, probe_only=True)
        self.assertEqual(result.resolution, "ambient")
        self.assertTrue(result.ready)
        self.assertEqual(result.diagnostics, [])


class ManagedInstallTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="deckflow-home-"))

    def test_layout_is_package_then_version(self):
        from deckflow_core.home import extract_dir

        self.assertEqual(extract_dir(pin.VERSION, self.home), self.home / "extract" / pin.VERSION)

    def test_installed_version_reads_the_dist_info_directory(self):
        target = self.home / "extract" / pin.VERSION
        (target / f"deckflow_extract-{pin.VERSION}.dist-info").mkdir(parents=True)
        self.assertEqual(installer.installed_version(target, pin.PACKAGE), pin.VERSION)

    def test_a_cache_at_the_wrong_version_is_not_ready(self):
        target = self.home / "extract" / pin.VERSION
        (target / "deckflow_extract-0.0.1.dist-info").mkdir(parents=True)
        result = resolver.resolve(home=self.home, probe_only=True)
        self.assertEqual(result.resolution, "missing")

    def test_invocation_runs_the_module_never_the_console_script(self):
        """The generated script's shebang points at the *installing* python.

        It also runs without PYTHONPATH, so it cannot import the package it was
        generated for.
        """
        command, env = installer.invocation(self.home / "x", pin.MODULE)
        self.assertEqual(command[1:], ["-m", pin.MODULE])
        self.assertEqual(env["PYTHONPATH"], str(self.home / "x"))
        self.assertNotIn(pin.BIN, " ".join(command))


class InstallOriginTest(unittest.TestCase):
    def _attempts(self, source: str | None):
        return installer._attempts(
            pin.PACKAGE, pin.VERSION, Path("/tmp/t"), pin.INDEX_URL, source,
        )

    def test_the_index_is_passed_explicitly(self):
        """So a local pip.conf cannot redirect a pinned acquisition elsewhere."""
        _, command = self._attempts(None)[0]
        self.assertIn("--index-url", command)
        self.assertEqual(command[command.index("--index-url") + 1], pin.INDEX_URL)
        self.assertIn(pin.REQUIREMENT, command)

    def test_the_declared_source_is_tried_after_the_index_never_before(self):
        """So publishing to the index later needs no code change."""
        plan = self._attempts(pin.SOURCE)
        self.assertEqual([origin for origin, _ in plan], [pin.INDEX_URL, pin.SOURCE])

    def test_without_a_source_there_is_only_the_index_attempt(self):
        self.assertEqual(len(self._attempts(None)), 1)

    def test_install_is_a_target_install_never_a_global_one(self):
        _, command = self._attempts(None)[0]
        self.assertIn("--target", command)
        self.assertNotIn("--user", command)

    def test_uv_tool_environments_without_pip_use_the_uv_installer(self):
        with mock.patch.object(
            installer.importlib.util, "find_spec", return_value=None
        ), mock.patch.object(installer.shutil, "which", return_value="/usr/local/bin/uv"):
            _, command = self._attempts(None)[0]
        self.assertEqual(command[:3], ["/usr/local/bin/uv", "pip", "install"])
        self.assertIn("--target", command)


if __name__ == "__main__":
    unittest.main()
