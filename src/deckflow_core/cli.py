"""CLI entry point.

Two invariants the tests hold to:

- stdout carries the machine contract and nothing else.  Business commands emit
  exactly one JSON object; `providers` prints a human table by default and
  switches to the strict envelope under `--json`.  Progress, prompts and errors
  go to stderr.
- a deferred command is absent, not stubbed.  `parse`, `editor` and
  `export pptx` are unknown arguments in v0.1 and exit 2.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .commands import editor as editor_cmd, export_pptx, parse as parse_cmd, providers_cmd
from .diagnostics import Diagnostic
from .envelope import STATUS_FAILED, Envelope
from .exits import EXIT_INTERRUPT, EXIT_OK, EXIT_OUTPUT, EXIT_USAGE, CoreError
from .fsutil import atomic_write_text
from .providers import resolve as resolver

_EPILOG = """\
network policy:
  Core reaches the network for exactly one purpose: acquiring a pinned provider
  into its own managed cache. Source files, HTML, assets and PPTX are never
  uploaded, and cloud provider modes are only used when you ask for them.

writes:
  providers install/remove touch only $DECKFLOW_HOME/providers (default
  ~/.deckflow/providers). Nothing is installed globally and no project
  directory is modified.

exit codes:
  0 succeeded/partial  2 usage  3 input/precondition
  4 contract  5 provider or execution failure  6 output conflict  130 interrupt
  Read the JSON `status`; the exit code only classifies why a run ended.
"""


class _Parser(argparse.ArgumentParser):
    """Argparse exits 2 on usage errors; keep the message on stderr."""

    def error(self, message: str) -> Any:  # noqa: D401
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: error: {message}\n")
        raise SystemExit(EXIT_USAGE)


def _key_value(values: Sequence[str] | None, flag: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in values or ():
        if "=" not in item:
            raise CoreError(
                Diagnostic(
                    rule_id="CLI_USAGE",
                    severity="error",
                    message=f"{flag} expects <provider>=<value>.",
                    expected=f"{flag} deckhtml=/path/to/bin",
                    actual=item,
                    recovery=f"Re-run with {flag} <provider>=<value>.",
                ),
                exit_code=EXIT_USAGE,
            )
        name, _, value = item.partition("=")
        parsed[name.strip()] = value.strip()
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="deckflow",
        description=(
            "Deckflow capability broker. One CLI over deckflow-extract, "
            "@deckflow/html-editor and @deckflow/deckhtml, with providers acquired on demand."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"deckflow-core {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--provider-install", choices=list(resolver.POLICIES), default=None,
        help="on-demand acquisition policy (default: auto; env DECKFLOW_PROVIDER_INSTALL). "
             "ask degrades to never without a TTY",
    )
    common.add_argument(
        "--provider-bin", action="append", metavar="NAME=PATH", dest="provider_bin",
        help="use this executable for a provider instead of resolving it (repeatable)",
    )
    common.add_argument(
        "--provider-spec", action="append", metavar="NAME=VERSION", dest="provider_spec",
        help="override a pinned provider version; marks the run as unpinned (repeatable)",
    )
    common.add_argument("--json", action="store_true", help="emit the strict JSON envelope on stdout")
    common.add_argument("--report", metavar="PATH", help="also write the envelope to this JSON file")

    subcommands = parser.add_subparsers(dest="command", metavar="<command>")

    providers = subcommands.add_parser(
        "providers", parents=[common],
        help="show provider status, or install/remove a managed provider",
        description=(
            "Report what each provider resolves to and whether using it will download "
            "anything. Without a subcommand this has no side effects."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    actions = providers.add_subparsers(dest="provider_action", metavar="<action>")
    install = actions.add_parser("install", parents=[common], help="acquire a pinned provider into the managed cache")
    install.add_argument("provider_name", metavar="<provider>", help="extract | editor | deckhtml")
    remove = actions.add_parser("remove", parents=[common], help="delete a provider from the managed cache")
    remove.add_argument("provider_name", metavar="<provider>", help="extract | editor | deckhtml")

    parse = subcommands.add_parser(
        "parse", parents=[common],
        help="extract one local file into a Parse Bundle",
        description=(
            "Extract one local file into a Parse Bundle (parse-manifest.json + document.md "
            "+ assets/) via the deckflow-extract provider, acquired on demand (~4MB). Runs "
            "locally: the file is never uploaded, and cloud credentials in the environment "
            "are withheld from the provider unless you pass --mode cloud. Writes only the "
            "--out directory and the optional --report; no project state is touched. The "
            "bundle is passed through untouched, including the provider's gaps and "
            "recommendations — deciding among them is yours, not core's. Accepts a local "
            "file only, not a URL."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parse.add_argument("input", metavar="<file>", help="one existing local file")
    parse.add_argument("--out", required=True, metavar="DIR", help="Parse Bundle directory")
    parse.add_argument("--overwrite", action="store_true", help="replace a non-empty --out")
    parse.add_argument(
        "--mode", choices=("local", "cloud"), default="local",
        help="cloud uploads the source and consumes quota; local is the default and never does",
    )
    parse.add_argument(
        "--upgrade", choices=("never", "ask", "auto"), default="never",
        help="whether the provider may fetch a heavier parsing engine (default: never)",
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

    editor = subcommands.add_parser(
        "editor", parents=[common],
        help="open a loopback visual editor over the canonical pages",
        description=(
            "Start a local browser editor bound to 127.0.0.1 over deck/pages/*.html, and "
            "report what the session changed. Long-running: stdout is NDJSON — a `ready` "
            "event with the URL, then the final envelope as the last line. Press Ctrl-C to "
            "end the session. Writes only the pages you edit; index.html, deck-head.html "
            "and the build manifest are verified unchanged afterwards and the run fails if "
            "they moved. Auditing is file-level (before/after page hashes plus an "
            "element-identity check), not per operation — run the Skill's page and project "
            "validators afterwards. Requires the editor provider, acquired on demand (~3MB)."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    editor.add_argument("project", metavar="<project>", help="deck project root")
    editor.add_argument("--page", metavar="SLIDE_ID", help="open this page first")
    editor.add_argument("--port", type=int, default=0, metavar="N", help="0 picks a free port")
    editor.add_argument("--open", action="store_true", help="also open the system browser")

    export = subcommands.add_parser(
        "export", parents=[], help="produce a derived output from a deck project",
        description="Derived outputs. The canonical deck stays HTML; nothing here writes back to it.",
    )
    formats = export.add_subparsers(dest="export_format", metavar="<format>")
    pptx = formats.add_parser(
        "pptx", parents=[common],
        help="convert a deck project's canonical pages into an editable PPTX",
        description=(
            "Convert deck/pages/*.html into a PPTX, in the order approved in deck-plan.json. "
            "Runs locally: nothing is uploaded, and a cloud API key in the environment is "
            "withheld from the converter rather than used. Writes only the target .pptx and "
            "the optional --report; canonical pages, index.html, deck-head.html and the build "
            "manifest are never modified. Requires the deckhtml provider, which is acquired on "
            "demand (~45MB) unless --provider-install never. Only landscape-16-9 decks can be "
            "exported; the converter cannot represent the other four stage sizes."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    pptx.add_argument("project", metavar="<project>", help="deck project root")
    pptx.add_argument("--output", required=True, metavar="PATH", help="target .pptx path")
    pptx.add_argument("--overwrite", action="store_true", help="replace an existing target")
    pptx.add_argument("--browser", metavar="PATH", help="Chromium executable for the converter")
    pptx.add_argument(
        "--exclude", action="append", metavar="SELECTOR",
        help="CSS selector for runtime/navigation elements to leave out (repeatable)",
    )
    pptx.add_argument(
        "--timeout", type=int, default=900, metavar="SECONDS",
        help="conversion timeout (default: 900)",
    )

    return parser


def _normalize(options: argparse.Namespace) -> argparse.Namespace:
    options.provider_bins = _key_value(getattr(options, "provider_bin", None), "--provider-bin")
    options.provider_specs = _key_value(getattr(options, "provider_spec", None), "--provider-spec")
    options.provider_action = getattr(options, "provider_action", None)
    options.provider_name = getattr(options, "provider_name", None)
    # Validate the policy early so a bad value fails before any work happens.
    resolver.policy_from(getattr(options, "provider_install", None))
    return options


def _prepare_report_path(options: argparse.Namespace) -> None:
    """Resolve and validate the report before any provider or project can run.

    A report is an output in its own right. It may not alias another output,
    overwrite an input, or land inside the canonical ``project/deck`` tree.
    Clearing ``options.report`` before validation also ensures that a rejected
    report path is never used while emitting the failure envelope.
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
    protect("output", getattr(options, "output", None))
    protect("browser executable", getattr(options, "browser", None))
    for name, path in getattr(options, "provider_bins", {}).items():
        protect(f"{name} provider executable", path)

    if getattr(options, "command", None) == "parse":
        output_root = Path(options.out).expanduser().resolve()
        if report == output_root or output_root in report.parents:
            conflicts.append(("parse output directory", output_root))

    project_value = getattr(options, "project", None)
    if project_value:
        project_root = Path(project_value).expanduser().resolve()
        deck_root = project_root / "deck"
        if report == deck_root or deck_root in report.parents:
            conflicts.append(("canonical deck tree", deck_root))
        for relative in ("deck-plan.json", "intent-detail.json"):
            protect(f"project record {relative}", project_root / relative)

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
                recovery="Choose a separate --report path outside the parse bundle and canonical deck tree.",
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

    overwrite = bool(getattr(options, "overwrite", False))
    if report.exists() and not overwrite:
        raise CoreError(
            Diagnostic(
                rule_id="REPORT_EXISTS",
                severity="error",
                message="The report target already exists.",
                location=str(report),
                expected="a new report path, or an explicit --overwrite",
                actual="an existing path",
                recovery="Choose another --report path, or pass --overwrite on commands that support it.",
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
        if options.command == "providers":
            envelope, human, code = providers_cmd.run(options)
        elif options.command == "editor":
            envelope, human, code = editor_cmd.run(options)
        elif options.command == "parse":
            envelope, human, code = parse_cmd.run(options)
        elif options.command == "export":
            if getattr(options, "export_format", None) is None:
                parser.parse_args(["export", "--help"])
            envelope, human, code = export_pptx.run(options)
        else:  # pragma: no cover - argparse rejects unknown commands first
            raise CoreError(
                Diagnostic(
                    rule_id="CLI_USAGE", severity="error",
                    message=f"Unknown command: {options.command}.",
                    recovery="Run `deckflow --help`.",
                ),
                exit_code=EXIT_USAGE,
            )
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


__all__ = ["main", "build_parser"]
