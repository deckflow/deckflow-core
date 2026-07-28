"""Exit codes and the one exception type that carries them.

Codes are fixed by the design document (upstream requirements §5.5) and must
not be extended without a major version bump.  Callers are expected to read the
JSON `status`; the exit code only classifies *why* a run ended.
"""

from __future__ import annotations

from .diagnostics import Diagnostic

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INPUT = 3
EXIT_CONTRACT = 4
EXIT_EXECUTION = 5
EXIT_OUTPUT = 6
EXIT_INTERRUPT = 130

_MEANING = {
    EXIT_OK: "succeeded, partial, or a normally ended/cancelled session",
    EXIT_USAGE: "CLI usage, unknown argument, missing required argument",
    EXIT_INPUT: "input missing/invalid, or a precondition is not met",
    EXIT_CONTRACT: "a validation or export contract did not pass",
    EXIT_EXECUTION: "a provider, parser or converter failed to run",
    EXIT_OUTPUT: "output path conflict, permission, or atomic write failure",
    EXIT_INTERRUPT: "the process was interrupted",
}


def meaning(code: int) -> str:
    return _MEANING.get(code, "unspecified")


class CoreError(Exception):
    """A failure that already knows its exit code and its diagnostic.

    Raising this is how command code declines to continue.  `cli.main` turns it
    into a well-formed envelope, so a failure still produces machine-readable
    output on stdout rather than a traceback.
    """

    def __init__(self, diagnostic: Diagnostic, exit_code: int = EXIT_EXECUTION) -> None:
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic
        self.exit_code = exit_code


class UsageError(CoreError):
    def __init__(self, message: str, *, recovery: str | None = None) -> None:
        super().__init__(
            Diagnostic(
                rule_id="CLI_USAGE",
                severity="error",
                message=message,
                recovery=recovery or "Run `deckflow --help` for the supported syntax.",
            ),
            exit_code=EXIT_USAGE,
        )
