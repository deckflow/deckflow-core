"""The bootstrap script a Skill ships in `scripts/deckflow`.

It exists because the obvious prerequisite line does not work: on a PEP 668
interpreter — the default on Homebrew macOS and Debian 12+ — `pip install
deckflow-core` is refused outright, macOS `/usr/bin/python3` is below the
version floor, and a successful `--user` install leaves the console script off
PATH. An agent that hits any of those improvises, so the Skill ships a line
that cannot fail that way.

These tests are the guard on that claim.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
_LAUNCHER = _ROOT / "launcher" / "deckflow"


def load_launcher():
    """Import a file with no `.py` extension, the way a Skill will not have to."""
    spec = importlib.util.spec_from_loader(
        "deckflow_launcher", importlib.machinery.SourceFileLoader("deckflow_launcher", str(_LAUNCHER))
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_skill(root: Path, *, vendored: bool) -> Path:
    (root / "scripts").mkdir(parents=True)
    (root / "SKILL.md").write_text(
        '---\nname: launcher-test\nmetadata:\n  version: "7.7.7"\n---\n# T\n', encoding="utf-8"
    )
    shim = root / "scripts" / "deckflow"
    shim.write_bytes(_LAUNCHER.read_bytes())
    shim.chmod(0o755)
    if vendored:
        (root / "vendor").mkdir()
        subprocess.run(
            ["cp", "-R", str(_ROOT / "src" / "deckflow_core"), str(root / "vendor" / "deckflow_core")],
            check=True,
        )
    return root


class ShippedFileTest(unittest.TestCase):
    def test_the_launcher_has_no_third_party_imports(self):
        """It runs before anything is installed, so it gets the stdlib only."""
        source = _LAUNCHER.read_text(encoding="utf-8")
        for line in source.splitlines():
            if line.startswith(("import ", "from ")) and "deckflow_core" not in line:
                module = line.split()[1].split(".")[0]
                self.assertIn(
                    module,
                    {"__future__", "json", "os", "shutil", "subprocess", "sys", "pathlib"},
                    f"unexpected import: {line}",
                )

    def test_the_pinned_core_version_matches_this_package(self):
        from deckflow_core import __version__

        module = load_launcher()
        self.assertEqual(module.CORE_VERSION, __version__)


class BootstrapTest(unittest.TestCase):
    def test_a_vendored_core_is_found_and_reported_as_vendored(self):
        with tempfile.TemporaryDirectory() as root:
            skill = make_skill(Path(root) / "skill", vendored=True)
            env = {**os.environ, "DECKFLOW_HOME": str(Path(root) / "home")}
            env.pop("DECKFLOW_SKILL_ROOT", None)
            env.pop("PYTHONPATH", None)
            completed = subprocess.run(
                [sys.executable, str(skill / "scripts" / "deckflow"), "env", "check"],
                capture_output=True, text=True, env=env, timeout=120,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["env"]["runtime"]["installation"], "vendored")

    def test_the_skill_is_identified_without_any_configuration(self):
        """The launcher knows its own location, so nothing has to be exported."""
        with tempfile.TemporaryDirectory() as root:
            skill = make_skill(Path(root) / "skill", vendored=True)
            env = {**os.environ, "DECKFLOW_HOME": str(Path(root) / "home")}
            env.pop("DECKFLOW_SKILL_ROOT", None)
            env.pop("PYTHONPATH", None)
            completed = subprocess.run(
                [sys.executable, str(skill / "scripts" / "deckflow"), "env", "check"],
                capture_output=True, text=True, env=env, timeout=120,
            )
        skill_record = json.loads(completed.stdout)["env"]["skill"]
        self.assertEqual(skill_record["name"], "launcher-test")
        self.assertEqual(skill_record["version"], "7.7.7")

    def test_a_vendored_copy_wins_over_a_managed_one(self):
        """A Skill that ships a core has pinned it deliberately."""
        module = load_launcher()
        with tempfile.TemporaryDirectory() as root:
            skill = make_skill(Path(root) / "skill", vendored=True)
            managed = Path(root) / "home" / "core" / "9.9.9" / "deckflow_core"
            managed.mkdir(parents=True)
            with mock.patch.dict(os.environ, {"DECKFLOW_HOME": str(Path(root) / "home")}):
                located = module.locate_core(skill)
            self.assertEqual(located, skill / "vendor")

    def test_the_newest_managed_version_is_selected(self):
        module = load_launcher()
        with tempfile.TemporaryDirectory() as root:
            for version in ("0.9.0", "0.10.0", "0.3.0"):
                (Path(root) / "core" / version / "deckflow_core").mkdir(parents=True)
            with mock.patch.dict(os.environ, {"DECKFLOW_HOME": root}):
                located = module.locate_core(None)
        self.assertEqual(located.name, "0.10.0")

    def test_version_ordering_is_numeric_not_lexical(self):
        module = load_launcher()
        self.assertGreater(module.version_key("0.10.0"), module.version_key("0.9.0"))
        # A stray directory must not be able to break selection.
        self.assertEqual(module.version_key("not-a-version"), (-1,))


class InstallShapeTest(unittest.TestCase):
    def test_the_install_is_a_target_install_and_never_breaks_system_packages(self):
        """`--target` is the whole point: PEP 668 does not refuse it."""
        module = load_launcher()
        recorded: list[list[str]] = []

        def fake_run(command, **_kwargs):
            recorded.append(command)
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="boom")

        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ, {"DECKFLOW_HOME": home}), \
                 mock.patch.object(module.subprocess, "run", fake_run), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                 self.assertRaises(SystemExit):
                module.install_core()

        self.assertTrue(recorded)
        for command in recorded:
            self.assertIn("--target", command)
            self.assertNotIn("--break-system-packages", command)
            self.assertNotIn("--user", command)


class FailureEnvelopeTest(unittest.TestCase):
    def test_a_bootstrap_failure_is_still_a_parseable_envelope(self):
        """Callers are told they can json.loads(stdout) unconditionally.

        That promise has to hold for a failure that never reached core, or the
        very first line of SKILL.md becomes the one case it does not cover.
        """
        module = load_launcher()
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr), \
             self.assertRaises(SystemExit) as caught:
            module.fail("CORE_INSTALL_FAILED", "could not install", "check the network")
        self.assertEqual(caught.exception.code, 5)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["diagnostics"][0]["rule_id"], "CORE_INSTALL_FAILED")
        self.assertIn("could not install", stderr.getvalue())

    def test_the_failure_envelope_matches_the_published_schema(self):
        from deckflow_core import schemas_dir

        from test_schema import validate

        module = load_launcher()
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            module.fail("PYTHON_TOO_OLD", "too old", "install a newer python3")
        payload = json.loads(stdout.getvalue())
        # The bootstrap cannot know these, and the schema requires them; fill
        # in what core would have supplied so the rest is really checked.
        payload["core_version"] = "0.0.0"
        payload["started_at"] = payload["finished_at"] = "1970-01-01T00:00:00.000Z"
        schema = json.loads((schemas_dir() / "envelope.schema.json").read_text(encoding="utf-8"))
        errors = validate(payload, schema, schema)
        self.assertEqual(errors, [], "\n".join(errors))


if __name__ == "__main__":
    unittest.main()
