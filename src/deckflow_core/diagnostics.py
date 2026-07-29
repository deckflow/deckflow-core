"""Structured diagnostics.

Shape and rules come from the design document (upstream requirements §5.4):

- `rule_id` is stable and may not change meaning within a major version;
- `severity` is exactly one of error / warning / info;
- an error means the promised output is unavailable;
- a warning may not paper over missing coverage or editability;
- `recovery` must be actionable — never "please check the file".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

SEVERITIES = ("error", "warning", "info")

# Ordered most severe first so `sorted` gives a caller-useful ranking.
_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}


@dataclass(frozen=True)
class Diagnostic:
    rule_id: str
    severity: str
    message: str
    location: str | None = None
    expected: str | None = None
    actual: str | None = None
    recovery: str | None = None

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"severity must be one of {SEVERITIES}, got {self.severity!r}")
        if not self.rule_id or not self.rule_id.replace("_", "").isalnum():
            raise ValueError(f"rule_id must be a stable UPPER_SNAKE token, got {self.rule_id!r}")

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "message": self.message,
        }
        for key in ("location", "expected", "actual", "recovery"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        return payload


def sort_diagnostics(diagnostics: Iterable[Diagnostic]) -> list[Diagnostic]:
    """Stable order by severity, then location, then rule_id.

    Determinism matters here: two isolated runs over the same inputs must
    produce the same report bytes, so the ordering may not depend on the order
    in which checks happened to run.
    """
    return sorted(
        diagnostics,
        key=lambda d: (_SEVERITY_RANK[d.severity], d.location or "", d.rule_id),
    )


# Boilerplate pip prints around a failure. Keeping these out is the difference
# between "check network access" and "no matching distribution for 0.2.0".
_NOISE = (
    "[notice]",
    "you should consider upgrading",
    "warning: running pip as the",
    "defaulting to user installation",
)


def summarize_output(*streams: str | None, limit: int = 3) -> str:
    """Pick the most informative lines out of a failed subprocess's output."""
    lines: list[str] = []
    for stream in streams:
        for line in (stream or "").splitlines():
            text = line.strip()
            if not text or any(noise in text.lower() for noise in _NOISE):
                continue
            if text not in lines:
                lines.append(text)
    if not lines:
        return "no diagnostic output"
    return " / ".join(lines[:limit])
