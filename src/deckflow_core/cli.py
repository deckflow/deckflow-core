"""CLI entry point.

Three invariants the tests hold to:

- **stdout carries the machine contract.**  Every command emits exactly one
  JSON object; progress, prompts and errors go to stderr.  JSON is the default
  rather than an opt-in `--json`, because every caller of this CLI is an agent
  or a script; `--human` is the opt-in for people.
- **a command core does not implement is absent, not stubbed.**  `editor`,
  `export` and `validate` are unknown arguments and exit 2, because a name
  visible in `--help` lets a caller believe the capability exists here.
- **`env check` exits 0 whenever the check ran.**  It is the first line of a
  Skill's prerequisites; a non-zero exit there tells an agent the Skill is
  broken and sends it off to repair a machine that is fine.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .commands import auth as auth_cmd
from .commands import env as env_cmd
from .commands import parse as parse_cmd
from .commands import update as update_cmd
from .diagnostics import Diagnostic
from .envelope import STATUS_FAILED, Envelope
from .exits import EXIT_INTERRUPT, EXIT_OK, EXIT_OUTPUT, EXIT_USAGE, CoreError
from .fsutil import atomic_write_text

_EPILOG = """\
network policy:
  Core reaches the network to acquire its pinned provider, and for `update`.
  Parse defaults to local-only; a source is uploaded only when you explicitly
  pass `--mode cloud`.

writes:
  env setup/clean and update touch only $DECKFLOW_HOME (default ~/.deckflow).
  `auth` writes the shared credential file through the provider, never directly.
  `parse` atomically updates only <project>/source-bundle and an optional report.
  Nothing is installed globally.

exit codes:
  0 succeeded/partial  2 usage  3 input/precondition
  5 provider or execution failure  6 output conflict  130 interrupt
  Read the JSON `status`; the exit code only classifies why a run ended.
"""


class _Parser(argparse.ArgumentParser):
    """Argparse exits 2 on usage errors; keep the message on stderr."""

    def error(self, message: str) -> Any:
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: error: {message}\n")
        raise SystemExit(EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="deckflow",
        description=(
            "Deckflow capability broker. One CLI over deckflow-extract, acquired on demand. "
            "Deck editing and PPTX export are not brokered here — call html-editor and "
            "deckhtml directly."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"deckflow-core {__version__}")

    # Every common flag suppresses its default so that `deckflow env --human
    # check` works: without this, the subparser's own default would overwrite
    # the value the parent already parsed. Defaults are applied in _normalize.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--human", action="store_true", default=argparse.SUPPRESS,
        help="print a human-readable summary instead of the JSON envelope",
    )
    common.add_argument(
        "--report", metavar="PATH", default=argparse.SUPPRESS,
        help="also write the envelope to this JSON file",
    )
    common.add_argument(
        "--skill-root", metavar="DIR", dest="skill_root", default=argparse.SUPPRESS,
        help="the calling skill's directory (default: $DECKFLOW_SKILL_ROOT)",
    )
    common.add_argument(
        "--offline", action="store_true", default=argparse.SUPPRESS,
        help="never reach the network; a missing provider becomes an error (env DECKFLOW_OFFLINE)",
    )
    common.add_argument(
        "--extract-bin", metavar="PATH", dest="extract_bin", default=argparse.SUPPRESS,
        help="use this deckflow-extract executable instead of resolving one "
             "(env DECKFLOW_EXTRACT_BIN; for developing core and extract together)",
    )

    subcommands = parser.add_subparsers(dest="command", metavar="<command>")

    env = subcommands.add_parser(
        "env", parents=[common],
        help="check the environment, or prepare and clean the managed home",
        description=(
            "Without a subcommand this is `env check`: a side-effect-free report of what "
            "this machine can do. It never downloads, never writes, and exits 0 whenever "
            "the check itself ran."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    env_actions = env.add_subparsers(dest="env_action", metavar="<action>")
    env_actions.add_parser("check", parents=[common], help="report the environment; no side effects")
    env_actions.add_parser("setup", parents=[common], help="acquire the pinned provider (~4MB)")
    env_actions.add_parser("clean", parents=[common], help="remove the managed provider install")

    auth = subcommands.add_parser(
        "auth", parents=[common],
        help="inspect or configure the Deckflow cloud credential",
        description=(
            "The credential lives in ~/.deckflow/credentials and is shared with DeckHTML. "
            "Core never writes that file itself; every action here is forwarded to "
            "deckflow-extract, which owns its merge rules. `status` never downloads "
            "anything: an unacquired provider reports cloud as unknown, not as unconfigured."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    auth_actions = auth.add_subparsers(dest="auth_action", metavar="<action>")
    auth_actions.add_parser(
        "status", parents=[common], help="report whether a cloud credential is configured",
    )
    login = auth_actions.add_parser(
        "login", parents=[common], help="browser login; refused without an interactive terminal",
    )
    login.add_argument("--no-open", action="store_true", help="print the URL instead of opening a browser")
    login.add_argument("--timeout", type=int, default=330, metavar="SECONDS")
    set_key = auth_actions.add_parser(
        "set-key", parents=[common], help="store a space worker secret; no browser needed",
    )
    set_key.add_argument("key", nargs="?", metavar="<key>", help="omit and pass --stdin instead")
    set_key.add_argument(
        "--stdin", action="store_true",
        help="read the key from stdin, keeping it out of the process list and shell history",
    )

    parse = subcommands.add_parser(
        "parse", parents=[common],
        help="ingest one local file into a project's canonical Source Bundle",
        description=(
            "Parse one direct local file through deckflow-extract, validate the transient "
            "result, and atomically append it to the canonical Source Bundle at "
            "<project>/source-bundle. The Parse Bundle "
            "is an internal temporary artifact and is never exposed to the caller. brief "
            "and deck-language are stored as metadata without AI interpretation or "
            "translation. Local mode never uploads the source; cloud credentials are "
            "withheld unless --mode cloud is explicit."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parse.add_argument("input", metavar="<file>", help="one existing local file")
    parse.add_argument("--project", required=True, metavar="DIR", help="existing Deck project")
    parse.add_argument("--brief", required=True, help="user task text; stored without AI rewriting")
    parse.add_argument(
        "--deck-language",
        required=True,
        dest="deck_language",
        help="BCP 47 language of the eventual Deck; distinct from detected source language",
    )
    parse.add_argument("--title", help="optional Source Bundle title")
    parse.add_argument(
        "--replace",
        action="store_true",
        help="rebuild a draft/review-ready Source Bundle from only this input",
    )
    parse.add_argument(
        "--mode", choices=("local", "cloud"), default="local",
        help="cloud uploads the source and consumes quota; local is the default and never does",
    )
    parse.add_argument(
        "--upgrade", choices=("never", "auto"), default="never",
        help="auto explicitly authorizes a local engine download; default: never",
    )
    parse.add_argument("--type", metavar="FORMAT", help="override format detection")
    parse.add_argument("--ocr", choices=("off", "auto"), help="local OCR for scanned input")
    parse.add_argument("--max-pages", type=int, metavar="N")
    parse.add_argument("--max-table-rows", type=int, metavar="N")
    parse.add_argument(
        "--provider-timeout", type=int, metavar="SECONDS",
        help="timeout passed to the provider for network/conversion steps",
    )
    parse.add_argument(
        "--timeout", type=int, default=900, metavar="SECONDS",
        help="overall extraction timeout (default: 900)",
    )

    update = subcommands.add_parser(
        "update", parents=[common],
        help="install a newer core beside the running one",
        description=(
            "Installs into ~/.deckflow/core/<version>/ and takes effect on the next run; "
            "the running copy is never modified. The provider pin moves with core, so "
            "there is no separate provider update."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    update.add_argument("--check", action="store_true", help="report whether an update exists; install nothing")
    update_actions = update.add_subparsers(dest="update_target", metavar="<target>")
    update_actions.add_parser(
        "skill", parents=[common],
        help="report the skill's version and who updates it (core never writes a skill)",
    )

    return parser


_COMMON_DEFAULTS: dict[str, Any] = {
    "human": False,
    "report": None,
    "skill_root": None,
    "offline": False,
    "extract_bin": None,
}


def _normalize(options: argparse.Namespace) -> argparse.Namespace:
    for key, default in _COMMON_DEFAULTS.items():
        if not hasattr(options, key):
            setattr(options, key, default)
    return options


def _prepare_report_path(options: argparse.Namespace) -> None:
    """Resolve and validate the report before any provider can run.

    A report is an output in its own right. It may not alias another output or
    overwrite an input. Clearing ``options.report`` before validation also
    ensures that a rejected report path is never used while emitting the
    failure envelope.
    """
    raw = getattr(options, "report", None)
    if not raw:
        return

    options.report = None
    report = Path(raw).expanduser().resolve()
    conflicts: list[tuple[str, Path]] = []

    def protect(label: str, value: str | Path | None) -> None:
        if value is None:
            return
        protected = Path(value).expanduser().resolve()
        if report == protected:
            conflicts.append((label, protected))

    protect("input", getattr(options, "input", None))
    protect("extract executable", getattr(options, "extract_bin", None))

    if getattr(options, "command", None) == "parse":
        output_root = Path(options.project).expanduser().resolve() / "source-bundle"
        if report == output_root or output_root in report.parents:
            conflicts.append(("Source Bundle output directory", output_root))

    if conflicts:
        label, protected = conflicts[0]
        raise CoreError(
            Diagnostic(
                rule_id="REPORT_PATH_CONFLICT",
                severity="error",
                message=f"The report path conflicts with the command's {label}.",
                location=str(report),
                expected="a distinct report path that cannot overwrite command inputs or outputs",
                actual=f"same as or inside {protected}",
                recovery="Choose a separate --report path outside the Source Bundle.",
            ),
            exit_code=EXIT_OUTPUT,
        )

    if report.is_dir():
        raise CoreError(
            Diagnostic(
                rule_id="REPORT_NOT_A_FILE",
                severity="error",
                message="The report target is a directory.",
                location=str(report),
                expected="a JSON file path",
                actual="an existing directory",
                recovery="Choose a file path for --report.",
            ),
            exit_code=EXIT_OUTPUT,
        )

    if report.exists():
        raise CoreError(
            Diagnostic(
                rule_id="REPORT_EXISTS",
                severity="error",
                message="The report target already exists.",
                location=str(report),
                expected="a new report path",
                actual="an existing path",
                recovery="Choose another --report path.",
            ),
            exit_code=EXIT_OUTPUT,
        )
    options.report = str(report)


def _emit(envelope: Envelope, human: str | None, report: str | None) -> None:
    payload = envelope.dumps()
    if report:
        atomic_write_text(Path(report).expanduser().resolve(), payload + "\n")
    if human is None:
        sys.stdout.write(payload + "\n")
    else:
        sys.stdout.write(human + "\n")


_COMMANDS = {
    "env": env_cmd.run,
    "auth": auth_cmd.run,
    "parse": parse_cmd.run,
    "update": update_cmd.run,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        parser.print_help(sys.stdout)
        return EXIT_OK

    options = parser.parse_args(args)
    if options.command is None:
        parser.print_help(sys.stdout)
        return EXIT_OK

    try:
        options = _normalize(options)
        _prepare_report_path(options)
        handler = _COMMANDS.get(options.command)
        if handler is None:  # pragma: no cover - argparse rejects unknown commands first
            raise CoreError(
                Diagnostic(
                    rule_id="CLI_USAGE", severity="error",
                    message=f"Unknown command: {options.command}.",
                    recovery="Run `deckflow --help`.",
                ),
                exit_code=EXIT_USAGE,
            )
        envelope, human, code = handler(options)
        _emit(envelope, human, getattr(options, "report", None))
        return code
    except CoreError as error:
        envelope = Envelope(command=getattr(options, "command", "deckflow") or "deckflow")
        envelope.status = STATUS_FAILED
        envelope.add(error.diagnostic)
        # Even a failure owes the caller a parseable result, so the envelope
        # goes to stdout and the prose goes to stderr.
        sys.stderr.write(f"[deckflow] {error.diagnostic.message}\n")
        if error.diagnostic.recovery:
            sys.stderr.write(f"[deckflow] {error.diagnostic.recovery}\n")
        _emit(envelope, None, getattr(options, "report", None))
        return error.exit_code
    except KeyboardInterrupt:
        envelope = Envelope(command=getattr(options, "command", "deckflow") or "deckflow")
        envelope.status = STATUS_FAILED
        envelope.add(
            Diagnostic(
                rule_id="INTERRUPTED", severity="error",
                message="The run was interrupted before it finished.",
                recovery="Re-run the command. Nothing was left partially written.",
            )
        )
        _emit(envelope, None, getattr(options, "report", None))
        return EXIT_INTERRUPT


__all__ = ["build_parser", "main"]
