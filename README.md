# deckflow-core

One CLI over the three Deckflow tools, with providers acquired **on demand**.

```
deckflow parse       -> deckflow-extract       (PyPI,  ~4MB)     ✅
deckflow editor      -> @deckflow/html-editor  (npm,   ~3MB)     ✅
deckflow export pptx -> @deckflow/deckhtml     (npm,  ~45MB)     ✅
deckflow providers   -> status / install / remove                ✅
```

Core is a **capability broker**: it owns the CLI contract, the result envelope,
the version pins and the safety policy. The three providers own the actual
work, stay independently versioned, and are only fetched when a command needs
them. A run that produces HTML and nothing else downloads nothing — and never
needs a Node runtime.

## Install

Pure standard library, zero third-party dependencies. That is a hard constraint
rather than a preference: it is what lets core be installed without a
virtualenv, or vendored into a Skill with no install step at all.

```bash
# Managed install — no venv, no pipx, no uv, works on a PEP 668 interpreter
python3 -m pip install --target ~/.deckflow/core/0.1.1 deckflow-core==0.1.1
PYTHONPATH=~/.deckflow/core/0.1.1 python3 -m deckflow_core providers
```

```bash
# Or as an ordinary package, if you already manage an environment
pip install deckflow-core
deckflow providers
```

Requires Python 3.10+. Node.js is **not** a dependency of core; it is only
needed for the two commands whose providers are npm packages.

## `deckflow providers`

The observable surface of the on-demand contract. It answers "what will this
core run, and will using it download anything" — with no side effects.

```console
$ deckflow providers
provider   pinned    resolution      status        public   unlocks
-------------------------------------------------------------------
deckhtml   0.4.0     cache 0.4.0     ready         yes      export pptx
editor     0.1.4     —               not-acquired  ORG-ONLY editor       (~3MB on first use)
extract    0.2.0     —               not-acquired  ORG-ONLY parse        (~4MB on first use)

managed cache: /Users/you/.deckflow/providers
acquisition writes only there; it never installs globally.
```

`--json` switches stdout to the strict envelope, which is what
`check_environment.py` and any agent should read.

```bash
deckflow providers --json
deckflow providers install deckhtml     # explicit acquisition
deckflow providers remove  deckhtml     # deletes the managed directory
```

A provider whose runtime is absent reports `status: blocked` with
`blocked_by: node-runtime-missing`, so a caller can degrade to HTML-only
delivery instead of failing mid-run.

## `deckflow parse`

```bash
deckflow parse <file> --out <dir> [--report r.json] [--overwrite]
```

Extracts one local file into a Parse Bundle (`parse-manifest.json` +
`document.md` + `assets/`) through the deckflow-extract provider.

This is the thinnest of the three commands on purpose — the provider already
has the best-shaped contract of the three, so core adds boundaries and gets out
of the way:

- **the bundle passes through untouched.** Core does not rewrite `document.md`,
  recompute fidelity, or invent a second artifact vocabulary alongside it.
- **`recommendations[]` reaches the caller verbatim.** When the provider says a
  heavier engine would extract 65 images instead of 5, that surfaces as an
  `info` diagnostic — choosing is the caller's job, never core's.
- **engine upgrades default to `never`.** Provider *acquisition* defaults to
  `auto`, but the provider's own optional engines (56MB PDF, 107MB OCR) change
  what the extraction produces, so they are opt-in via `--upgrade`.
- **`--mode local` is forced and cloud credentials are withheld**, and
  `--fetch-remote-images off` is passed explicitly so a change in the
  provider's defaults cannot put the content plane on the network.
- **URLs are refused.** The provider can fetch them; core does not, because
  "the content plane never reaches the network" is not worth stating with an
  exception in it. The refusal names the direct provider command.

## `deckflow editor`

```bash
deckflow editor <project> [--page <slide-id>] [--report r.json]
```

Opens a loopback visual editor over `deck/pages/*.html` and reports what the
session changed. Long-running: stdout is NDJSON — a `ready` event carrying the
URL, then the final envelope as the last line. Ctrl-C ends the session.

The provider has no session protocol: it starts a server, auto-saves, and tells
core nothing about what happened in between. So core proves the boundary from
the outside — hash every file under `deck/` before and after:

- **`changed_pages[]`** with before/after sha256 per page;
- **`EDITOR_TOUCHED_PROTECTED_FILE`** if anything outside `deck/pages/` moved.
  The provider's own backups and temp files are recognised as artefacts, not
  violations;
- **`EDITOR_ELEMENT_IDENTITY_CHANGED`** if a page's set of `data-element-id`
  values gained or lost a member. Those identities bind pages to their sources
  and to the export mapping.

What this deliberately does *not* claim: per-operation intent. That needs the
provider to emit save events, and every session carries an `info` diagnostic
saying so rather than approximating it. Run the Skill's page and project
validators afterwards.

Ending the session uses explicit SIGINT/SIGTERM handlers rather than the
default KeyboardInterrupt — a process launched in the background inherits
SIGINT as `SIG_IGN`, and without re-arming it the session could not be ended at
all. SIGTERM gets the same clean finish, which is what a supervising agent
sends.

## `deckflow export pptx`

```bash
deckflow export pptx <project> --output deck.pptx [--report r.json] [--overwrite]
```

Converts the project's canonical `deck/pages/*.html` into an editable PPTX
through the DeckHTML provider, acquired on demand.

What core contributes over calling the converter directly:

- **slide order comes from `deck-plan.json`**, not from `glob`. Filename order
  only happens to agree.
- **page/plan closure is checked both ways.** A planned slide with no page
  fails; so does a leftover page the plan does not declare, which would
  otherwise become an unapproved slide in the delivered deck.
- **non-16:9 decks are refused.** DeckHTML derives its viewport height from the
  width at a fixed 16:9, so four of the five Deckflow stage sizes would be laid
  out at the wrong shape *and still open cleanly*. Refusing is the only way the
  caller finds out.
- **`--mode local` is forced and cloud credentials are withheld** from the
  converter's environment, so a key that happens to be set cannot turn a local
  export into an upload.
- **conversion happens in a scratch directory** and the file is moved into place
  only after verification, so a failed run leaves no plausible-looking deck.
- **the result is verified independently** — core reopens the OOXML package with
  `zipfile` and checks slide count against the plan and that no relationship
  points off the machine. A converter should not be the only witness to its own
  output.
- **project records are hashed before and after**; if the export touched
  `index.html`, `deck-head.html` or the build manifest, the run fails.

Canonical pages, the generated index, the theme and the build manifest are
never written. The only outputs are the `.pptx` and the optional `--report`.

## How a provider is resolved

| # | Source | Notes |
| ---: | --- | --- |
| 1 | `--provider-bin <name>=<path>`, or `DECKFLOW_<NAME>_BIN` | wins over everything |
| 2 | already in the environment | used only if its version satisfies the pinned range |
| 3 | core's managed cache | `$DECKFLOW_HOME/providers/<name>/<version>/` |
| 4 | on-demand acquisition | governed by `--provider-install` |
| 5 | structured failure | `PROVIDER_MISSING` with a runnable recovery command |

An ambient install outside the pinned range is not an error: core records a
`PROVIDER_VERSION_MISMATCH` warning and uses its own copy, so a global install
can never quietly change what a pinned run executes.

Every result reports which rung it landed on:

```json
{"name": "deckhtml", "package": "@deckflow/deckhtml", "pinned_version": "0.4.0",
 "version": "0.4.0", "resolution": "managed-cache", "status": "ready",
 "acquired": false, "pinned": true, "public": true}
```

## Acquisition policy

`--provider-install auto|ask|never` (env `DECKFLOW_PROVIDER_INSTALL`), default
`auto`. `auto` is narrowly bounded:

- writes **only** into `$DECKFLOW_HOME/providers/<name>/<version>/` — never a
  global install, never your project's `node_modules`, never your Python env;
- installs **only** the exact pinned version, never a floating tag;
- passes the registry/index **explicitly**, so a scope mapping in your `.npmrc`
  cannot hijack the pin;
- verifies before the install counts, and removes the directory if it cannot;
- reports the acquisition in the envelope as `acquired: true`.

`ask` degrades to `never` without a TTY, so an agent is never blocked on a
prompt. `never` is the CI and offline setting.

## Version pins

`providers.json` declares the exact provider versions this core release was
tested against. Changing any pin is a core release. Override for development
with `--provider-spec deckhtml=0.4.0-rc.1` or
`DECKFLOW_PROVIDER_DECKHTML_VERSION`; such runs are marked `pinned: false` so
release verification can reject them.

## Network and content

Two separate planes:

| Plane | Policy |
| --- | --- |
| Providers (fetching code) | network allowed, for pinned packages from declared registries only, written only to the managed cache, always reported |
| Content (sources, HTML, assets, PPTX) | never uploaded. Cloud modes of the providers are used only when you explicitly ask for them; the presence of an API key is not authorization |

## Output contract

`--json` and `--report` produce the same envelope. Diagnostics are sorted
deterministically so two isolated runs over the same inputs produce the same
report bytes.

```json
{"schema_version": 1, "command": "providers", "core_version": "0.1.1",
 "status": "succeeded", "started_at": "...", "finished_at": "...",
 "providers": [], "inputs": [], "outputs": [], "diagnostics": []}
```

`status` is one of `succeeded` / `partial` / `cancelled` / `failed`. Read it —
the exit code only classifies *why* a run ended:

| Code | Meaning |
| ---: | --- |
| 0 | succeeded, partial, or a normally ended session |
| 2 | usage |
| 3 | input missing/invalid, or a precondition not met |
| 4 | a validation or export contract did not pass |
| 5 | a provider or converter failed to run |
| 6 | output conflict, permission, or atomic write failure |
| 130 | interrupted |

A failure still prints a parseable envelope on stdout; prose goes to stderr.

## Scope of this release

v0.1.1 registers `providers`, `parse`, `editor` and `export pptx` — the whole
planned surface. This patch release hardens report/output collision handling,
Parse Bundle replacement, editor page boundaries and provider version
verification, and reports editor crashes after readiness as failures.

`validate html` is deferred beyond 0.1.x and is not registered at all: a
deferred command may not ship as a stub, a placeholder, or a "not implemented"
response, because either would put the name in `--help` and let a caller
believe the capability exists. Concretely, **a successful export is not
evidence that the HTML passed browser validation** — nothing in this release
checks rendered geometry, overflow, fonts or print output.

## Tests

```bash
PYTHONPATH=src:tests python3 -m unittest discover -s tests
```

Conversion tests that actually run the provider are opt-in, because they
download it:

```bash
DECKFLOW_LIVE_TESTS=1 PYTHONPATH=src:tests python3 -m unittest discover -s tests
```

## License

MIT
