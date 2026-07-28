"""The pinned provider compatibility matrix.

Bumping any pin here is a core release.  This file is why the version pin no
longer lives inside a Skill script: one place declares which provider versions
this core was tested against, and `deckflow providers --json` publishes it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..diagnostics import Diagnostic
from ..exits import EXIT_USAGE, CoreError

_MATRIX_PATH = Path(__file__).with_name("providers.json")


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    kind: str
    package: str
    version: str
    compatible: str
    bin: str
    unlocks: tuple[str, ...]
    approx_mb: int
    provides: tuple[str, ...]
    module: str | None = None
    registry: str | None = None
    fallback_registry: str | None = None
    index_url: str | None = None
    source: str | None = None
    # False while the package is only reachable with organisation credentials.
    # The Skill is integrated by an engineer outside the organisation, so this
    # is the difference between "works for us" and "works for them".
    public: bool = True
    pinned: bool = True

    @property
    def requirement(self) -> str:
        """The exact spec handed to the package manager."""
        if self.kind == "npm":
            return f"{self.package}@{self.version}"
        return f"{self.package}=={self.version}"

    def with_version(self, version: str) -> "ProviderSpec":
        """Override the pin — development and testing only.

        The result carries `pinned=False`, which surfaces in the envelope so a
        release verification can reject runs that did not use the matrix.  The
        `source` fallback is dropped: it names a specific ref that no longer
        matches the requested version.
        """
        return ProviderSpec(
            name=self.name,
            kind=self.kind,
            package=self.package,
            version=version,
            compatible="",
            bin=self.bin,
            unlocks=self.unlocks,
            approx_mb=self.approx_mb,
            provides=self.provides,
            module=self.module,
            registry=self.registry,
            fallback_registry=self.fallback_registry,
            index_url=self.index_url,
            source=None,
            public=self.public,
            pinned=False,
        )


def _load() -> dict[str, Any]:
    return json.loads(_MATRIX_PATH.read_text(encoding="utf-8"))


def _spec_from(name: str, raw: dict[str, Any]) -> ProviderSpec:
    return ProviderSpec(
        name=name,
        kind=raw["kind"],
        package=raw["package"],
        version=raw["version"],
        compatible=raw.get("compatible", ""),
        bin=raw["bin"],
        unlocks=tuple(raw.get("unlocks", ())),
        approx_mb=int(raw.get("approx_mb", 0)),
        provides=tuple(raw.get("provides", ())),
        module=raw.get("module"),
        registry=raw.get("registry"),
        fallback_registry=raw.get("fallback_registry"),
        index_url=raw.get("index_url"),
        source=raw.get("source"),
        public=bool(raw.get("public", True)),
    )


def _env_version_override(name: str) -> str | None:
    return os.environ.get(f"DECKFLOW_PROVIDER_{name.upper()}_VERSION")


def load_matrix(version_overrides: dict[str, str] | None = None) -> dict[str, ProviderSpec]:
    """Load the matrix, applying `--provider-spec` and environment overrides."""
    overrides = dict(version_overrides or {})
    raw = _load()["providers"]
    specs: dict[str, ProviderSpec] = {}
    for name, entry in raw.items():
        spec = _spec_from(name, entry)
        override = overrides.pop(name, None) or _env_version_override(name)
        if override:
            spec = spec.with_version(override)
        specs[name] = spec
    if overrides:
        unknown = ", ".join(sorted(overrides))
        raise CoreError(
            Diagnostic(
                rule_id="PROVIDER_UNKNOWN",
                severity="error",
                message=f"Unknown provider name in --provider-spec: {unknown}.",
                expected=f"one of {', '.join(sorted(specs))}",
                actual=unknown,
                recovery="Run `deckflow providers` to list the provider names this core knows.",
            ),
            exit_code=EXIT_USAGE,
        )
    return specs


def get(name: str, version_overrides: dict[str, str] | None = None) -> ProviderSpec:
    specs = load_matrix(version_overrides)
    if name not in specs:
        raise CoreError(
            Diagnostic(
                rule_id="PROVIDER_UNKNOWN",
                severity="error",
                message=f"Unknown provider: {name}.",
                expected=f"one of {', '.join(sorted(specs))}",
                actual=name,
                recovery="Run `deckflow providers` to list the provider names this core knows.",
            ),
            exit_code=EXIT_USAGE,
        )
    return specs[name]
