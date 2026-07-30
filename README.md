# deckflow-core

One CLI over the Deckflow tools core actually brokers, with the provider
acquired **on demand**.

```
deckflow env      check / setup / clean the environment          ✅
deckflow auth     the cloud credential, via deckflow-extract     ✅
deckflow parse    -> deckflow-extract       (PyPI, ~4MB)         ✅
deckflow update   install a newer core beside the running one    ✅
```

Core is a **capability broker**: it owns the CLI contract, the result envelope,
the version pin, the safety policy, and canonical Source Bundle assembly. The
provider owns parsing and optional parsing engines, stays independently
versioned, and is only fetched when a command needs it.

## What core deliberately does not broker

`@deckflow/deckhtml` (PPTX export) and `@deckflow/html-editor` (visual editing)
are **called directly by the Skill**, not through core. Wrapping them here meant
restating two contracts core does not own — slide ordering, stage geometry,
element identity, session lifecycle — and then keeping that restatement correct
across three release cadences. The tools already publish those contracts; a
second copy in core could only drift from them.

`deckflow env check` does report whether Node and npx exist, because the Skill
needs the fact and would otherwise write a second probe. That is the line:
**facts, never verdicts.** There is no `pptx_available` field, because that also
depends on registry reachability and the deck's stage size, and core knows
neither.

## Install

Pure standard library, zero third-party dependencies. That is a hard constraint
rather than a preference: it is what lets core be installed without a
virtualenv, or vendored into a Skill with no install step at all.

**If you are integrating a Skill, do not write an install step at all.** Copy
[`launcher/deckflow`](launcher/deckflow) into the Skill's `scripts/` and make
the prerequisite one line:

```bash
python3 scripts/deckflow env check
```

The launcher finds a suitable interpreter, locates core (vendored → managed →
importable), installs it into `~/.deckflow/core/<version>/` if it is missing,
and declares the Skill's root. It exists because the obvious alternative does
not work:

| What breaks | Where |
| --- | --- |
| `pip install deckflow-core` → `error: externally-managed-environment` | any PEP 668 interpreter: Homebrew macOS, Debian 12+ |
| `Requires-Python >=3.10` unsatisfied | macOS `/usr/bin/python3` is 3.9 |
| `deckflow: command not found` right after a successful install | `--user` installs land outside PATH |

An agent that hits any of those improvises, and the improvisation is usually
`--break-system-packages` on someone's system Python.

For a human managing their own environment:

```bash
pipx install deckflow-core        # or: uv tool install deckflow-core
deckflow env check
```

```bash
# Or a managed install by hand — no venv, works on a PEP 668 interpreter
python3 -m pip install --target ~/.deckflow/core/0.3.1 deckflow-core==0.3.1
PYTHONPATH=~/.deckflow/core/0.3.1 python3 -m deckflow_core env check
```

Requires Python 3.10+. Node.js is not involved anywhere in core.

## `deckflow env`

```bash
deckflow env check     # report; no side effects, no downloads, always exit 0
deckflow env setup     # acquire the pinned provider (~4MB) — the only one that downloads
deckflow env clean     # remove the managed provider install
```

`env check` is designed to be the first line of a Skill's prerequisites, which
fixes three of its properties: it never writes anything, it **exits 0 whenever
the check itself ran**, and it reports facts rather than verdicts. A non-zero
exit there would tell an agent the Skill is broken and send it off to repair a
machine that is fine.

JSON is the default output. `--human` is the opt-in, for people.

```json
{
  "schema_version": 2, "command": "env check", "core_version": "0.3.1",
  "status": "succeeded",
  "extract": { "status": "not-acquired", "pinned_version": "0.3.1",
               "resolution": "missing", "acquired": false, "download_mb": 4 },
  "env": {
    "skill":   { "name": "gezhe-ppt", "version": "0.4.0-beta.2",
                 "root": "/…/gezhe-ppt", "version_source": "frontmatter" },
    "runtime": { "version": "0.3.1", "installation": "managed", "location": "…" },
    "python":  { "version": "3.14.5", "executable": "/opt/homebrew/bin/python3",
                 "satisfies_requires_python": true, "externally_managed": true },
    "cloud":   { "available": false, "reason": "extract-not-acquired",
                 "configured": null, "shared_with": "deckhtml" },
    "host":    { "node": { "present": true, "version": "22.3.0" },
                 "npx": { "present": true } },
    "home": "/Users/you/.deckflow"
  }
}
```

`python.executable` is there because a bare version is not actionable: an agent
host commonly has three `python3` binaries on PATH, and two machines both
reporting 3.12 differ in whether an install will be refused.

**Core never goes looking for a Skill.** Scanning upward for a `SKILL.md` would
find the user's project, not the Skill. The caller declares one with
`--skill-root`, or `DECKFLOW_SKILL_ROOT`, or by invoking the launcher, which
knows its own location. No declaration reports `"skill": null` and is not an
error. The version is read from `deckflow-skill.json` if present, else from
`SKILL.md` frontmatter `metadata.version`.

## `deckflow auth`

```bash
deckflow auth status              # read-only; never downloads to answer
deckflow auth login               # browser login; refused without a TTY
deckflow auth set-key --stdin     # a space worker secret; no browser needed
```

The credential lives in `~/.deckflow/credentials` and is **shared with
DeckHTML**. Core owns none of it: every action here forwards to
`deckflow-extract`, which owns that file's merge rules. Credential writes are
delegated rather than reimplemented; `parse` is the only command that changes
project state, and it is limited to `<project>/source-bundle`.

Two consequences worth knowing:

- **branch on `configured`, not on `DECKFLOW_API_KEY`.** A user who logged into
  DeckHTML for a PPTX export has also configured cloud parsing, with no variable
  visible anywhere in the environment.
- **`configured: null` means "not asked", not "no".** `auth status` refuses to
  trigger a 4MB download to answer a question, so an unacquired provider reports
  `available: false` and leaves `configured` null. A confident "no" for a
  question never asked is how a logged-in user's material gets uploaded.

There is no `logout`. Clear a stored credential with
`deckflow-extract auth logout`.

## `deckflow parse`

```bash
deckflow parse <file> \
  --project <deck-project> \
  --brief "<user task>" \
  --deck-language <bcp47> \
  [--replace] [--upgrade auto] [--report r.json]
```

Parses one direct local file, validates the provider's transient Parse Bundle,
and atomically appends the accepted result to
`<project>/source-bundle`. Luna calls this command once; it does not import or
inspect the intermediate bundle.

- **No AI is used.** `brief` is stored after trimming outer whitespace.
  `deck-language` is the eventual Deck language. The parser's independently
  detected `source_language` is stored under `manifest.imports[]`; no
  translation happens here.
- **Only usable results are committed.** The input SHA-256, schema, paths,
  files, asset hashes, locator profile, provider report, fidelity, coverage,
  gaps and decision must close. `decision.usable` must be true and no gap may
  be blocking.
- **The canonical write is transactional.** Core builds and validates a sibling
  staging directory, computes the Source Bundle fingerprint, and then swaps it
  into place. Any failure leaves the old bundle unchanged.
- **draft and review-ready bundles accept append.** `--replace` rebuilds one
  from the current input. A `confirmed` Source Bundle is immutable through this
  command because changing it also requires downstream invalidation.
- **engine upgrades default to `never`.** Explicit `--upgrade auto` authorizes
  extract to install, self-check, activate and reselect one local enhancement.
  If installation fails but the fallback is usable, the bundle is committed
  and core reports `partial`.
- **The Parse Bundle is private.** Its directory, manifest path and provider
  command never appear in stdout, reports or canonical provenance. Sanitized
  fidelity, coverage, gaps, recommendations and acquisition outcome remain
  available for audit.
- **Local mode is explicit and cloud credentials are withheld unless
  `--mode cloud` is passed.** `--fetch-remote-images off` is also passed so a
  change in provider defaults cannot put local content on the network.
  Withholding means both halves: credential variables are removed from the
  child environment and `DECKFLOW_NO_STORED_CREDENTIALS=1` is set, because the
  provider also reads `~/.deckflow/credentials`.
- **URLs are refused.** The provider can fetch them; core does not, because
  "the content plane never reaches the network" is not worth stating with an
  exception in it.

## How the provider is resolved

| # | Source | Notes |
| ---: | --- | --- |
| 1 | `--extract-bin <path>`, or `DECKFLOW_EXTRACT_BIN` | wins over everything; for developing core and extract together |
| 2 | already in the environment | used only if its version satisfies the pinned range |
| 3 | core's managed home | `$DECKFLOW_HOME/extract/<version>/` |
| 4 | on-demand acquisition | unless `--offline` |
| 5 | structured failure | `EXTRACT_MISSING` with a runnable recovery command |

An ambient install outside the pinned range is not an error: core records an
`EXTRACT_VERSION_MISMATCH` warning and uses its own copy, so a global install
can never quietly change what a pinned run executes.

Acquisition is narrowly bounded. It writes **only** into
`$DECKFLOW_HOME/extract/<version>/` — never a global install, never your Python
environment; installs **only** the exact pinned version; passes the index
**explicitly**, so local pip configuration cannot redirect the pin; verifies
before the install counts, and removes the directory if it cannot; and reports
itself in the envelope as `acquired: true`.

`--offline` (env `DECKFLOW_OFFLINE`) is the CI setting: a missing provider
becomes an error instead of a download.

## `deckflow update`

Installs a newer core into `~/.deckflow/core/<version>/` and takes effect on the
next run — **never in place**. Upgrading the package that is currently executing
is the kind of operation that half-works; installing beside it means an
interrupted update leaves the working copy untouched and rollback is removing
one directory. Older managed versions are pruned after a successful install.

There is deliberately no `deckflow update extract`: the provider pin moves with
core, and an independently upgradable provider is not a pinned one.

`deckflow update skill` **reports and never writes.** Core does not update a
Skill directory: the distribution channel is not declared anywhere, ownership
would become a cycle (the Skill installs core, core rewrites the Skill), a
rewrite would clobber files the user edited, and a self-updating Skill is remote
code execution on the next agent run. A Skill that wants this machine-readable
declares `update.command` in `deckflow-skill.json`, and core hands that command
back.

## The managed home

```
~/.deckflow/
├── core/<version>/       core itself; the launcher runs the newest
├── extract/<version>/    core's managed copy of deckflow-extract
├── parse/                deckflow-extract's OWN engine sidecars — not ours
└── credentials           shared with DeckHTML; only extract may write it
```

## Network and content

Two separate planes:

| Plane | Policy |
| --- | --- |
| Providers (fetching code) | network allowed, for the pinned package from declared indexes only, written only to the managed home, always reported |
| Content (sources, extracted text, assets) | local mode never uploads; the provider's cloud mode uploads only when you explicitly ask for it, and the presence of an API key is not authorization |

## Output contract

stdout and `--report` carry the same envelope. Diagnostics are sorted
deterministically so two isolated runs over the same inputs produce the same
report bytes.

```json
{"schema_version": 2, "command": "env check", "core_version": "0.3.1",
 "status": "succeeded", "started_at": "...", "finished_at": "...",
 "extract": null, "inputs": [], "outputs": [], "diagnostics": []}
```

`extract` is a single object — schema 1 had a `providers[]` array, which made
every caller index into a list to find the only element it could contain. It is
always present, and null when the command never resolved the provider.

`status` is one of `succeeded` / `partial` / `failed`. Read it — the exit code
only classifies *why* a run ended:

| Code | Meaning |
| ---: | --- |
| 0 | succeeded or partial |
| 2 | usage |
| 3 | input missing/invalid, or a precondition not met |
| 5 | a provider failed to run, is missing, or is incompatible |
| 6 | output conflict, permission, or atomic write failure |
| 130 | interrupted |

A failure still prints a parseable envelope on stdout; prose goes to stderr.
That holds for the launcher too: a bootstrap that never reached core emits the
same shape rather than a traceback.

## Scope of this release

v0.3.1 keeps `env`, `auth`, `parse` and `update` as the complete command
surface, and changes `parse` into the transactional canonical Source Bundle
ingestion entrypoint. It distinguishes caller-supplied Deck language from
parser-detected source language, supports draft/review-ready append and
replace, and keeps confirmed bundles immutable.

`editor`, `export`, `validate` and `providers` are not registered at all: an
unregistered name is an argparse invalid choice and exit 2, never a stub or a
"not implemented" response, because either would put the name in `--help` and
let a caller believe core owns the capability.

`providers` is on that list because it was core's own word for one package. The
resolution ladder, the pin and the managed install all survive — the noun does
not. See `deckflow-core-refactor.md`.

## Tests

```bash
PYTHONPATH=src:tests python3 -m unittest discover -s tests
```

## License

MIT
