"""`env check` and `auth` — the probes, and the promises they make.

Both commands answer questions, and answering a question may not cost the user
a download or leak a credential. That is what most of this file is about.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deckflow_core.commands import auth as auth_cmd
from deckflow_core.commands import env as env_cmd
from deckflow_core.exits import CoreError
from deckflow_core.extract import resolve as resolver
from deckflow_core import home as home_mod
from deckflow_core.probe import cloud as cloud_probe
from deckflow_core.probe import skill as skill_probe

_SRC = str(Path(__file__).resolve().parents[1] / "src")


def run_cli(*args: str, home: str | None = None, env_extra: dict | None = None):
    env = {**os.environ, "PYTHONPATH": _SRC, "DECKFLOW_HOME": home or tempfile.mkdtemp()}
    env.pop("DECKFLOW_SKILL_ROOT", None)
    env.pop("DECKFLOW_EXTRACT_BIN", None)
    env.update(env_extra or {})
    completed = subprocess.run(
        [sys.executable, "-m", "deckflow_core", *args],
        capture_output=True, text=True, env=env, timeout=120,
    )
    return completed.returncode, completed.stdout, completed.stderr


def options(**overrides) -> argparse.Namespace:
    base = dict(
        human=False, report=None, skill_root=None, offline=False, extract_bin=None,
        env_action=None, auth_action=None, key=None, stdin=False,
        no_open=False, timeout=30,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class EnvPayloadTest(unittest.TestCase):
    def test_every_section_is_present(self):
        with tempfile.TemporaryDirectory() as home:
            _, stdout, _ = run_cli("env", "check", home=home)
        env = json.loads(stdout)["env"]
        self.assertEqual(
            set(env), {"skill", "runtime", "python", "cloud", "host", "home"}
        )

    def test_python_reports_the_interpreter_not_just_the_version(self):
        """Two machines both reporting 3.12 differ in whether pip will work."""
        _, stdout, _ = run_cli("env", "check")
        python = json.loads(stdout)["env"]["python"]
        self.assertEqual(python["executable"], sys.executable)
        self.assertIn("externally_managed", python)
        self.assertTrue(python["satisfies_requires_python"])

    def test_host_reports_facts_never_verdicts(self):
        """Core observes Node; it must not claim PPTX export will work.

        That also depends on registry reachability and the deck's stage size,
        neither of which core knows — so `host` carries presence and versions
        and nothing that reads as a capability promise.
        """
        _, stdout, _ = run_cli("env", "check")
        host = json.loads(stdout)["env"]["host"]
        self.assertEqual(set(host), {"node", "npx"})
        self.assertLessEqual(set(host["node"]), {"present", "path", "version"})
        for key in ("pptx", "pptx_available", "editor", "deckhtml", "export"):
            self.assertNotIn(key, json.dumps(host))


class SkillDiscoveryTest(unittest.TestCase):
    """Core does not go looking for a skill; the caller declares one."""

    def _skill(self, root: Path, frontmatter: str) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        (root / "SKILL.md").write_text(frontmatter, encoding="utf-8")
        return root

    def test_no_declaration_reports_no_skill_and_is_not_an_error(self):
        code, stdout, _ = run_cli("env", "check")
        self.assertEqual(code, 0)
        self.assertIsNone(json.loads(stdout)["env"]["skill"])

    def test_version_comes_from_the_frontmatter(self):
        with tempfile.TemporaryDirectory() as root:
            skill = self._skill(
                Path(root) / "s",
                '---\nname: demo-skill\ndescription: >\n  a folded block\n  with a version: 9 decoy\n'
                'metadata:\n  author: someone\n  version: "1.2.3-beta.4"\n---\n# Demo\n',
            )
            record, diagnostics = skill_probe.probe(str(skill))
        self.assertEqual(record["name"], "demo-skill")
        self.assertEqual(record["version"], "1.2.3-beta.4")
        self.assertEqual(record["version_source"], "frontmatter")
        self.assertEqual(diagnostics, [])

    def test_the_manifest_wins_over_the_frontmatter(self):
        with tempfile.TemporaryDirectory() as root:
            skill = self._skill(
                Path(root) / "s", '---\nname: demo\nmetadata:\n  version: "0.0.1"\n---\n'
            )
            (skill / skill_probe.MANIFEST).write_text(
                json.dumps({"name": "demo", "version": "2.0.0",
                            "update": {"command": "git pull"}}),
                encoding="utf-8",
            )
            record, _ = skill_probe.probe(str(skill))
        self.assertEqual(record["version"], "2.0.0")
        self.assertEqual(record["version_source"], "manifest")
        self.assertEqual(record["update_command"], "git pull")

    def test_a_directory_without_a_skill_document_is_reported_not_guessed(self):
        with tempfile.TemporaryDirectory() as root:
            record, diagnostics = skill_probe.probe(root)
        self.assertIsNone(record)
        self.assertEqual([d.rule_id for d in diagnostics], ["SKILL_ROOT_INVALID"])

    def test_an_unreadable_version_is_reported_rather_than_invented(self):
        with tempfile.TemporaryDirectory() as root:
            skill = self._skill(Path(root) / "s", "# No frontmatter at all\n")
            record, diagnostics = skill_probe.probe(str(skill))
        self.assertIsNone(record["version"])
        self.assertEqual([d.rule_id for d in diagnostics], ["SKILL_VERSION_UNKNOWN"])

    def test_the_environment_variable_is_honoured(self):
        with tempfile.TemporaryDirectory() as root:
            skill = self._skill(
                Path(root) / "s", '---\nname: envvar-skill\nmetadata:\n  version: "3.3.3"\n---\n'
            )
            _, stdout, _ = run_cli(
                "env", "check", env_extra={"DECKFLOW_SKILL_ROOT": str(skill)}
            )
        self.assertEqual(json.loads(stdout)["env"]["skill"]["version"], "3.3.3")


class CloudProbeTest(unittest.TestCase):
    """The one rule: answering "is cloud available" may not cost 4MB."""

    def test_an_unacquired_provider_is_never_downloaded_to_answer(self):
        unacquired = resolver.Extract(status=resolver.STATUS_NOT_ACQUIRED)
        with mock.patch.object(cloud_probe.subprocess, "run",
                               side_effect=AssertionError("must not run the provider")):
            result = cloud_probe.probe(unacquired)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "extract-not-acquired")

    def test_not_asked_is_null_not_false(self):
        """`configured: false` would be a claim core did not verify.

        The authorization decision branches on this field: reporting a
        confident "no" for a question never asked is how a logged-in user's
        source material gets uploaded without being asked.
        """
        result = cloud_probe.probe(resolver.Extract(status=resolver.STATUS_NOT_ACQUIRED))
        self.assertIsNone(result["configured"])

    def test_a_ready_provider_is_asked_and_its_answer_carried(self):
        ready = resolver.Extract(status=resolver.STATUS_READY, command=["/fake/extract"])
        payload = json.dumps({
            "status": "ok",
            "cloud": {"configured": True, "source": "file", "space_id": "s-1",
                      "api_base": "https://app.deckflow.com/v1",
                      "config_file": "/home/u/.deckflow/credentials"},
        })
        completed = subprocess.CompletedProcess([], 0, stdout=payload, stderr="")
        with mock.patch.object(cloud_probe.subprocess, "run", return_value=completed):
            result = cloud_probe.probe(ready)
        self.assertTrue(result["available"])
        self.assertTrue(result["configured"])
        self.assertEqual(result["credential_source"], "file")
        self.assertEqual(result["shared_with"], "deckhtml")

    def test_the_provider_is_pointed_at_the_credential_file_core_reports(self):
        ready = resolver.Extract(status=resolver.STATUS_READY, command=["/fake/extract"])
        completed = subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps(
                {
                    "status": "ok",
                    "cloud": {
                        "configured": False,
                        "config_file": "/tmp/deckflow-home/credentials",
                    },
                }
            ),
            stderr="",
        )
        with mock.patch.dict(
            os.environ,
            {
                "DECKFLOW_HOME": "/tmp/deckflow-home",
                "DECKFLOW_CONFIG_DIR": "",
                "DECKHTML_CONFIG_DIR": "",
            },
        ), mock.patch.object(
            cloud_probe.subprocess, "run", return_value=completed
        ) as run:
            cloud_probe.probe(ready)

        self.assertEqual(
            run.call_args.kwargs["env"]["DECKFLOW_CONFIG_DIR"],
            str(Path("/tmp/deckflow-home")),
        )

    def test_an_unparseable_answer_is_unknown_not_unconfigured(self):
        ready = resolver.Extract(status=resolver.STATUS_READY, command=["/fake/extract"])
        completed = subprocess.CompletedProcess([], 1, stdout="boom", stderr="")
        with mock.patch.object(cloud_probe.subprocess, "run", return_value=completed):
            result = cloud_probe.probe(ready)
        self.assertFalse(result["available"])
        self.assertIsNone(result["configured"])


class AuthTest(unittest.TestCase):
    def test_status_never_acquires(self):
        with mock.patch.object(resolver, "acquire", side_effect=AssertionError("must not acquire")):
            envelope, _, code = auth_cmd.run_status(options(auth_action="status"))
        self.assertEqual(code, 0)
        self.assertIn("cloud", envelope.extra)

    def test_status_on_an_empty_machine_leaves_the_home_untouched(self):
        with tempfile.TemporaryDirectory() as home:
            code, _, _ = run_cli("auth", "status", home=home)
            self.assertEqual(code, 0)
            self.assertEqual(list(Path(home).iterdir()), [])

    def test_login_is_refused_without_a_terminal(self):
        """The flow needs a browser, a callback port and five minutes.

        None of that is reachable from an agent subprocess, so refusing up
        front beats hanging and then reporting a timeout as a fault.
        """
        with mock.patch.object(auth_cmd.sys.stdin, "isatty", return_value=False):
            envelope, _, code = auth_cmd.run_login(options(auth_action="login"))
        self.assertEqual(code, 5)
        self.assertEqual(envelope.status, "failed")
        self.assertEqual(
            [d.rule_id for d in envelope.diagnostics], ["LOGIN_REQUIRES_TERMINAL"]
        )

    def test_the_login_refusal_offers_the_path_that_does_work(self):
        with mock.patch.object(auth_cmd.sys.stdin, "isatty", return_value=False):
            envelope, _, _ = auth_cmd.run_login(options(auth_action="login"))
        self.assertIn("set-key", envelope.diagnostics[0].recovery)

    def test_an_empty_key_is_a_usage_error_that_recommends_stdin(self):
        with self.assertRaises(CoreError) as caught:
            auth_cmd._read_key(options(key=None, stdin=False))
        self.assertEqual(caught.exception.exit_code, 2)
        self.assertIn("process list", caught.exception.diagnostic.recovery)

    def test_set_key_never_puts_the_secret_in_provider_arguments(self):
        ready = resolver.Extract(
            status=resolver.STATUS_READY,
            version="0.3.0",
            command=["/fake/deckflow-extract"],
        )
        completed = subprocess.CompletedProcess(
            [], 0, stdout='{"status":"ok","config_file":"/tmp/credentials"}\n'
        )
        with mock.patch.object(auth_cmd.sys, "stdin", io.StringIO("worker-secret\n")), \
             mock.patch.object(auth_cmd.extract_resolve, "resolve", return_value=ready), \
             mock.patch.object(auth_cmd.subprocess, "run", return_value=completed) as run:
            envelope, _, code = auth_cmd.run_set_key(
                options(auth_action="set-key", stdin=True)
            )

        command = run.call_args.args[0]
        self.assertEqual(command[-4:], ["config", "set", "api-key", "--stdin"])
        self.assertNotIn("worker-secret", command)
        self.assertEqual(run.call_args.kwargs["input"], "worker-secret\n")
        self.assertEqual(
            run.call_args.kwargs["env"]["DECKFLOW_CONFIG_DIR"],
            str(home_mod.credentials_dir()),
        )
        self.assertEqual(code, 0)
        self.assertEqual(envelope.status, "succeeded")

    def test_there_is_no_logout_but_status_says_where_to_clear(self):
        """Removing a capability may not remove the user's way to find it."""
        code, stdout, stderr = run_cli("auth", "logout")
        self.assertEqual(code, 2)
        envelope, _, _ = auth_cmd.run_status(options(auth_action="status"))
        recoveries = " ".join(d.recovery or "" for d in envelope.diagnostics)
        self.assertIn("deckflow-extract auth logout", recoveries + auth_cmd._CLEAR_HINT)


class EnvSetupTest(unittest.TestCase):
    def test_setup_reports_rather_than_reinstalls_when_already_present(self):
        ready = resolver.Extract(
            status=resolver.STATUS_READY, version="0.3.0", resolution="managed", path="/x",
        )
        with mock.patch.object(env_cmd.extract_resolve, "resolve", return_value=ready), \
             mock.patch.object(env_cmd.extract_resolve, "acquire",
                               side_effect=AssertionError("must not re-acquire")):
            envelope, _, code = env_cmd.run_setup(options(env_action="setup"))
        self.assertEqual(code, 0)
        self.assertEqual(
            [d.rule_id for d in envelope.diagnostics], ["EXTRACT_ALREADY_AVAILABLE"]
        )


if __name__ == "__main__":
    unittest.main()
