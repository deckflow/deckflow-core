"""The one result shape every command returns.

`deckflow-extract` has its own parsed/repairable/needs-input/blocked state
machine.  Core normalizes it to this envelope and carries the provider's own
payload verbatim under `provider_result`: unified, but lossless.

`extract` is a single object, not the `providers[]` array of schema 1.  Core
brokers one tool; an array of one made every caller index into a list to find
the only element it could ever contain.  If a second brokered tool ever
appears, it gets a sibling key — not a resurrected plural abstraction.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from typing import Any

from . import SCHEMA_VERSION, __version__
from .diagnostics import Diagnostic, sort_diagnostics

STATUS_SUCCEEDED = "succeeded"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"

# No `cancelled`: only a long-running session could produce it, and core no
# longer runs one. A status the code cannot emit is a status a caller writes
# dead branches for.
STATUSES = (STATUS_SUCCEEDED, STATUS_PARTIAL, STATUS_FAILED)


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class Envelope:
    command: str
    status: str = STATUS_SUCCEEDED
    started_at: str = field(default_factory=utc_now)
    finished_at: str | None = None
    extract: dict[str, Any] | None = None
    inputs: list[dict[str, Any]] = field(default_factory=list)
    outputs: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    provider_result: Any = None
    extra: dict[str, Any] = field(default_factory=dict)

    def add(self, diagnostic: Diagnostic) -> None:
        self.diagnostics.append(diagnostic)

    def extend(self, diagnostics: list[Diagnostic]) -> None:
        self.diagnostics.extend(diagnostics)

    def to_json(self) -> dict[str, Any]:
        if self.status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}, got {self.status!r}")
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "command": self.command,
            "core_version": __version__,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at or utc_now(),
            # Always present, null when this command never resolved the
            # provider — one less conditional in every caller.
            "extract": self.extract,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "diagnostics": [d.to_json() for d in sort_diagnostics(self.diagnostics)],
        }
        payload.update(self.extra)
        if self.provider_result is not None:
            payload["provider_result"] = self.provider_result
        return payload

    def dumps(self) -> str:
        return json.dumps(self.to_json(), ensure_ascii=False, sort_keys=False)


# Provider status -> core status.  Kept as an explicit table rather than
# scattered conditionals: the mapping is part of the public contract and is
# asserted directly in the tests.
EXTRACT_STATUS_MAP = {
    "parsed": STATUS_SUCCEEDED,
    "repairable": STATUS_PARTIAL,
    "needs-input": STATUS_FAILED,
    "blocked": STATUS_FAILED,
}


def file_record(path: str, *, sha256: str | None = None, size: int | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {"path": path}
    if sha256 is not None:
        record["sha256"] = sha256
    if size is not None:
        record["bytes"] = size
    return record
