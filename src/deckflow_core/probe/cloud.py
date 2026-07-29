"""Whether a cloud credential is configured, asked of the provider.

Since deckflow-extract 0.3.0 the credential lives in `~/.deckflow/credentials`,
the file it shares with DeckHTML.  Two consequences the Skill has to act on:

- reading `DECKFLOW_API_KEY` is no longer a complete answer.  A user who logged
  into DeckHTML for a PPTX export has also configured cloud parsing, with no
  variable visible anywhere in the environment;
- so the authorization question ("may I upload this source material?") must be
  decided on `configured`, not on the environment.

Core answers it by asking extract, because core manages the provider and knows
where its managed copy lives — the Skill would otherwise have to reconstruct a
`PYTHONPATH` invocation of a package it does not manage.

**This probe never acquires anything.**  Answering "is cloud available" must not
cost a 4MB download; an unacquired provider reports `available: false` with the
reason, and `configured: null` — not `false`, because "we did not ask" and "we
asked and there is nothing" are different answers.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from .. import home as home_mod
from ..extract.resolve import Extract

SHARED_WITH = "deckhtml"


def _unknown(reason: str) -> dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "configured": None,
        "credential_source": None,
        "api_base": None,
        "config_file": str(home_mod.credentials_file()),
        "shared_with": SHARED_WITH,
    }


def probe(extract: Extract, *, timeout: int = 30) -> dict[str, Any]:
    if not extract.ready:
        return _unknown("extract-not-acquired")

    try:
        completed = subprocess.run(
            [*extract.command, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=timeout,
            # The real environment on purpose: the question is whether *this*
            # machine has a credential, so nothing may be withheld here. Parse
            # is where credentials get stripped, not the probe that reports them.
            env={**os.environ, **home_mod.credential_env(), **extract.env},
        )
    except (OSError, subprocess.SubprocessError):
        return _unknown("probe-failed")

    payload = _last_json_line(completed.stdout)
    cloud = (payload or {}).get("cloud")
    if not isinstance(cloud, dict):
        return _unknown("probe-failed")

    return {
        "available": True,
        "configured": bool(cloud.get("configured")),
        "credential_source": cloud.get("source"),
        "space_id_configured": bool(cloud.get("space_id")),
        "api_base": cloud.get("api_base"),
        "config_file": str(cloud.get("config_file") or home_mod.credentials_file()),
        "shared_with": SHARED_WITH,
    }


def _last_json_line(stdout: str) -> dict[str, Any] | None:
    for line in reversed((stdout or "").strip().splitlines()):
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


__all__ = ["probe", "SHARED_WITH"]
