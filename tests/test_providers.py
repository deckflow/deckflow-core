"""Pin matrix, resolution ladder and acquisition policy."""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deckflow_core.exits import CoreError
from deckflow_core.providers import acquire_npm, matrix
from deckflow_core.providers import resolve as resolver


class MatrixTest(unittest.TestCase):
    def test_every_provider_declares_what_it_unlocks(self):
        for name, spec in matrix.load_matrix().items():
            self.assertTrue(spec.unlocks, f"{name} unlocks nothing")
            self.assertIn(spec.kind, ("npm", "python"))
            self.assertTrue(spec.compatible, f"{name} has no compatible range")

    def test_pins_are_exact_never_floating(self):
        for name, spec in matrix.load_matrix().items():
            self.assertNotIn(spec.version, ("latest", "*", ""), f"{name} is not pinned")
            self.assertRegex(spec.version, r"^\d+\.\d+\.\d+")

    def test_python_provider_declares_an_importable_module(self):
        # A --target install must be invoked as `python -m <module>`; the
        # generated console script would run without PYTHONPATH and fail.
        spec = matrix.get("extract")
        self.assertEqual(spec.kind, "python")
        self.assertTrue(spec.module)

    def test_requirement_syntax_matches_the_ecosystem(self):
        deckhtml = matrix.get("deckhtml")
        self.assertEqual(deckhtml.requirement, f"@deckflow/deckhtml@{deckhtml.version}")
        self.assertEqual(matrix.get("extract").requirement, "deckflow-extract==0.2.0")

    def test_pinned_version_satisfies_its_own_compatible_range(self):
        # A pin outside its own range would make every managed install look
        # incompatible the moment it is resolved.
        from deckflow_core import versions

        for name, spec in matrix.load_matrix().items():
            self.assertTrue(
                versions.satisfies(spec.version, spec.compatible),
                f"{name}: pin {spec.version} is outside {spec.compatible}",
            )


class PublicationReachabilityTest(unittest.TestCase):
    """The Skill is integrated from outside the organisation."""

    def test_every_provider_declares_public_reachability(self):
        for name, spec in matrix.load_matrix().items():
            self.assertIsInstance(spec.public, bool, f"{name} does not declare `public`")

    def test_non_public_provider_declares_a_fallback_origin(self):
        # Otherwise it is simply unacquirable, which the matrix should not encode.
        for name, spec in matrix.load_matrix().items():
            if not spec.public:
                self.assertTrue(
                    spec.fallback_registry or spec.source,
                    f"{name} is not public and declares no fallback origin",
                )

    def test_non_public_provider_warns_on_resolution(self):
        # Synthetic: every shipped provider is public today, but the warning
        # has to keep working for the next one that is not.
        spec = dataclasses.replace(matrix.get("editor"), public=False)
        diagnostic = resolver._publication_warning(spec)
        self.assertEqual(diagnostic.rule_id, "PROVIDER_NOT_PUBLICLY_PUBLISHED")
        self.assertEqual(diagnostic.severity, "warning")
        self.assertIn("outside", diagnostic.recovery)

    def test_shipped_matrix_is_reachable_without_organisation_credentials(self):
        private = [name for name, spec in matrix.load_matrix().items() if not spec.public]
        self.assertEqual(private, [], f"not publicly acquirable: {private}")


class VersionOverrideTest(unittest.TestCase):
    def test_version_override_marks_the_run_unpinned(self):
        spec = matrix.get("deckhtml", {"deckhtml": "0.4.0-rc.1"})
        self.assertEqual(spec.version, "0.4.0-rc.1")
        self.assertFalse(spec.pinned)

    def test_version_override_drops_the_source_fallback(self):
        # The source names a specific ref; it no longer matches the request.
        self.assertTrue(matrix.get("extract").source)
        self.assertIsNone(matrix.get("extract", {"extract": "0.9.9"}).source)

    def test_source_fallback_ref_matches_the_pinned_version(self):
        for name, spec in matrix.load_matrix().items():
            if spec.source:
                self.assertIn(spec.version, spec.source, f"{name} source ref does not name its pin")

    def test_env_override_is_honoured(self):
        with mock.patch.dict(os.environ, {"DECKFLOW_PROVIDER_DECKHTML_VERSION": "0.9.9"}):
            self.assertEqual(matrix.get("deckhtml").version, "0.9.9")

    def test_unknown_provider_is_a_usage_error(self):
        with self.assertRaises(CoreError) as caught:
            matrix.get("nope")
        self.assertEqual(caught.exception.exit_code, 2)


class RegistryPinningTest(unittest.TestCase):
    def test_scoped_package_pins_both_registry_and_scope(self):
        # A user .npmrc mapping @deckflow elsewhere must not be able to hijack
        # the pin; this was reproduced against a real machine.
        args = acquire_npm.registry_args(matrix.get("deckhtml"))
        self.assertIn("--registry=https://registry.npmjs.org/", args)
        self.assertIn("--@deckflow:registry=https://registry.npmjs.org/", args)

    def test_off_npmjs_install_keeps_the_default_registry_on_npmjs(self):
        """Only the scope moves; ordinary dependencies stay on npmjs.

        Collapsing both into one --registry sends the provider's own
        dependencies (css-select, htmlparser2) to the scoped registry, where
        they 404 — observed against GitHub Packages before this split existed.
        """
        spec = matrix.get("editor")
        args = acquire_npm.registry_args(spec, "https://npm.pkg.github.com/")
        self.assertEqual(args[0], "--registry=https://registry.npmjs.org/")
        self.assertIn("--@deckflow:registry=https://npm.pkg.github.com/", args)

    def test_fallback_registry_is_tried_after_the_public_one(self):
        # The matrix can name the intended public registry first even before
        # the package is published there; publishing then needs no code change.
        # Built synthetically so this holds regardless of which providers
        # currently happen to need a fallback.
        spec = dataclasses.replace(
            matrix.get("editor"),
            registry="https://registry.npmjs.org/",
            fallback_registry="https://npm.pkg.github.com/",
        )
        self.assertEqual(
            acquire_npm.registries(spec),
            ["https://registry.npmjs.org/", "https://npm.pkg.github.com/"],
        )

    def test_a_fallback_equal_to_the_primary_is_not_tried_twice(self):
        spec = dataclasses.replace(
            matrix.get("editor"),
            registry="https://registry.npmjs.org/",
            fallback_registry="https://registry.npmjs.org/",
        )
        self.assertEqual(acquire_npm.registries(spec), ["https://registry.npmjs.org/"])

    def test_provider_without_a_fallback_tries_exactly_one_registry(self):
        self.assertEqual(acquire_npm.registries(matrix.get("deckhtml")),
                         ["https://registry.npmjs.org/"])

    def test_git_source_is_tried_last_never_before_a_registry(self):
        """A clone must never win over a registry that would have worked."""
        ordered = acquire_npm.origins(matrix.get("editor"))
        self.assertTrue(acquire_npm.is_registry(ordered[0][0]))
        self.assertEqual(ordered[-1][0], matrix.get("editor").source)
        self.assertFalse(acquire_npm.is_registry(ordered[-1][0]))

    def test_git_origin_installs_the_spec_itself_not_the_package_name(self):
        ordered = acquire_npm.origins(matrix.get("editor"))
        origin, requirement = ordered[-1]
        self.assertEqual(requirement, origin)
        # A registry origin installs `@scope/name@version` instead.
        self.assertEqual(ordered[0][1], matrix.get("editor").requirement)

    def test_provider_without_a_source_has_registry_origins_only(self):
        for origin, _ in acquire_npm.origins(matrix.get("deckhtml")):
            self.assertTrue(acquire_npm.is_registry(origin))

    def test_unscoped_package_uses_its_declared_registry_directly(self):
        spec = matrix.get("deckhtml")
        object.__setattr__(spec, "package", "deckhtml")
        object.__setattr__(spec, "registry", "https://example.test/")
        self.assertEqual(acquire_npm.registry_args(spec), ["--registry=https://example.test/"])

    def test_scope_detection(self):
        self.assertEqual(acquire_npm.scope_of("@deckflow/deckhtml"), "@deckflow")
        self.assertIsNone(acquire_npm.scope_of("deckhtml"))

    def test_404_recovery_names_the_publication_gap_not_the_network(self):
        recovery = acquire_npm._npm_recovery(
            matrix.get("editor"), "npm error code E404 / npm error 404 Not Found"
        )
        self.assertIn("not found on", recovery)
        self.assertIn("npm view", recovery)
        self.assertNotIn("Check network access", recovery)

    def test_auth_recovery_points_at_the_registry_choice(self):
        recovery = acquire_npm._npm_recovery(matrix.get("editor"), "npm error code E401 unauthorized")
        self.assertIn("requires a token", recovery)

    def test_unclassified_failure_falls_back_to_network_advice(self):
        recovery = acquire_npm._npm_recovery(matrix.get("editor"), "socket hang up")
        self.assertIn("Check network access", recovery)


class ResolutionLadderTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="deckflow-home-"))

    def test_probe_only_never_acquires(self):
        with mock.patch.object(resolver, "acquire", side_effect=AssertionError("must not acquire")):
            result = resolver.resolve(matrix.get("extract"), home=self.home, probe_only=True)
        self.assertFalse(result.acquired)
        self.assertIn(result.status, ("not-acquired", "blocked", "ready"))

    def test_npm_provider_is_blocked_without_node(self):
        with mock.patch.object(acquire_npm, "node_available", return_value=False), \
             mock.patch("shutil.which", return_value=None):
            result = resolver.resolve(matrix.get("deckhtml"), home=self.home, probe_only=True)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.blocked_by, "node-runtime-missing")

    def test_missing_override_path_fails_loudly(self):
        with self.assertRaises(CoreError) as caught:
            resolver.resolve(
                matrix.get("deckhtml"), home=self.home,
                bin_overrides={"deckhtml": "/definitely/not/here"},
            )
        self.assertEqual(caught.exception.diagnostic.rule_id, "PROVIDER_OVERRIDE_MISSING")

    @unittest.skipIf(os.name == "nt", "resolves a POSIX shell script as the provider binary")
    def test_override_wins_over_everything(self):
        fake = self.home / "fake-deckhtml"
        fake.parent.mkdir(parents=True, exist_ok=True)
        fake.write_text("#!/bin/sh\necho 0.3.2\n", encoding="utf-8")
        fake.chmod(0o755)
        result = resolver.resolve(
            matrix.get("deckhtml"), home=self.home, bin_overrides={"deckhtml": str(fake)}
        )
        self.assertEqual(result.resolution, "override")
        self.assertTrue(result.ready)

    def test_policy_never_refuses_instead_of_downloading(self):
        with mock.patch("shutil.which", return_value="/usr/bin/npm"), \
             mock.patch.object(acquire_npm, "node_available", return_value=True), \
             mock.patch.object(resolver, "_ambient", return_value=None):
            with self.assertRaises(CoreError) as caught:
                resolver.resolve(matrix.get("deckhtml"), home=self.home, policy="never")
        self.assertEqual(caught.exception.diagnostic.rule_id, "PROVIDER_MISSING")
        self.assertEqual(caught.exception.exit_code, 5)

    def test_ask_without_a_tty_behaves_as_never(self):
        # An agent has no TTY and must never be blocked on a prompt.
        with mock.patch("sys.stdin.isatty", return_value=False):
            self.assertFalse(resolver._may_acquire("ask"))
        self.assertTrue(resolver._may_acquire("auto"))
        self.assertFalse(resolver._may_acquire("never"))

    @unittest.skipIf(os.name == "nt", "resolves a POSIX shell script as the provider binary")
    def test_incompatible_ambient_is_ignored_with_a_warning(self):
        stale = self.home / "stale-deckhtml"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("#!/bin/sh\necho 0.1.0\n", encoding="utf-8")
        stale.chmod(0o755)
        with mock.patch("shutil.which", return_value=str(stale)):
            result = resolver._ambient(matrix.get("deckhtml"))
        self.assertFalse(result.ready)
        self.assertEqual(result.diagnostics[0].rule_id, "PROVIDER_VERSION_MISMATCH")
        self.assertEqual(result.diagnostics[0].severity, "warning")

    @unittest.skipIf(os.name == "nt", "resolves a POSIX shell script as the provider binary")
    def test_compatible_ambient_is_used_as_is(self):
        # Derive the version from the matrix so a pin bump does not break this.
        pinned = matrix.get("deckhtml").version
        good = self.home / "good-deckhtml"
        good.parent.mkdir(parents=True, exist_ok=True)
        good.write_text(f"#!/bin/sh\necho {pinned}\n", encoding="utf-8")
        good.chmod(0o755)
        with mock.patch("shutil.which", return_value=str(good)):
            result = resolver._ambient(matrix.get("deckhtml"))
        self.assertTrue(result.ready)
        self.assertEqual(result.resolution, "ambient")
        self.assertFalse(result.acquired)

    def test_bad_policy_name_is_a_usage_error(self):
        with self.assertRaises(CoreError) as caught:
            resolver.policy_from("sometimes")
        self.assertEqual(caught.exception.exit_code, 2)

    def test_default_policy_is_auto(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DECKFLOW_PROVIDER_INSTALL", None)
            self.assertEqual(resolver.policy_from(None), "auto")


class ManagedCacheTest(unittest.TestCase):
    def test_npm_cache_layout_is_name_then_version(self):
        home = Path("/tmp/dfhome")
        spec = matrix.get("deckhtml")
        prefix = acquire_npm.install_dir(home, spec)
        self.assertEqual(prefix, home / "providers" / "deckhtml" / spec.version)
        self.assertEqual(
            acquire_npm.bin_path(prefix, spec),
            prefix / "node_modules" / ".bin" / "deckhtml",
        )

    def test_installed_version_reads_the_package_manifest(self):
        spec = matrix.get("deckhtml")
        with tempfile.TemporaryDirectory() as root:
            prefix = Path(root)
            manifest = prefix / "node_modules" / "@deckflow" / "deckhtml"
            manifest.mkdir(parents=True)
            (manifest / "package.json").write_text(
                json.dumps({"version": spec.version}), encoding="utf-8"
            )
            self.assertEqual(acquire_npm.installed_version(prefix, spec), spec.version)


if __name__ == "__main__":
    unittest.main()
