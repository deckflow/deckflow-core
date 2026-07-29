"""Exit codes and the one exception type that carries them.

Codes must not be extended without a major version bump.  Callers are expected
to read the JSON `status`; the exit code only classifies *why* a run ended.

    0   succeeded or partial
    2   CLI usage, unknown argument, missing required argument
    3   input missing/invalid, or a precondition is not met
    5   a provider failed to run, is missing, or is incompatible
    6   output path conflict, permission, or atomic write failure
    130 the process was interrupted

There is no code 4: contract verification belonged to `export pptx`, which core
no longer brokers.
"""

from __future__ import annotations

from .diagnostics import Diagnostic

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INPUT = 3
EXIT_EXECUTION = 5
EXIT_OUTPUT = 6
EXIT_INTERRUPT = 130


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
