---
artifact: build-plan
version: 2
scope: datastore-v1
# branch: feature/datastore-v1
# Left commented until the branch exists — a plan claiming a branch no repo has is
# reported as exactly that by the session briefing. This plan was drawn on
# feature/architecture alongside the artifact it depends on; uncomment when Chunk 01
# cuts feature/datastore-v1, and drop the active_build_plan scalar at the same time.
depends_on:
  - artifact: system-requirements
    file_path: docs/system-requirements.md
  - artifact: architecture
    file_path: .prawduct/artifacts/architecture.md
governed_by:
  - artifact: architecture
    dispositions:
      - "every writable handle comes from the one writer factory, which takes the lock before it returns → conforms; Chunk 01 builds the factory and the structural test that no other path yields a writable handle"
      - "read-role handles open mode=ro, hold no cross-call read transaction, and never fall back to a writable handle → conforms; Chunk 01 builds the reader opener, the after-query_only=OFF refusal test and the checkpoint test. The MCP server itself is build step 7, so this plan holds the norm at the connection layer the server will later use"
      - "no component creates the datastore implicitly → conforms; `store init` is the sole creator, and Chunk 01 tests that every other entry point refuses"
      - "a process that does not recognize the schema version refuses to serve, and a migration's DDL and version stamp commit in one transaction → conforms; Chunk 01 builds the version check and the single-transaction migration runner, with a kill-mid-migration test"
  - artifact: project-preferences
    dispositions:
      - "provider-agnostic engine, no roster identity in code or schema → conforms; this plan builds no institution-specific path, and Chunk 01 migrates the guard to tests/preferences/ where the test runner can invoke it"
      - "no roster or operator identity in anything pushed → conforms; the pre-push guard stays wired throughout"
      - "requirement ids unique within a requirements document → inapplicable because this plan adds no requirement ids"
partition: serial — 02 and 03 both extend the same store module and schema, and every chunk builds on 01's connection layer. Nothing here is independent enough to fan out, and the first chunk is the one that must not be built twice.
last_validated: null
---

## Requirements Confidence

**Level:** High

**Why:** The requirements are unusually complete — §4 and §6 specify the schema at field level in
places, AC-ARCH.7 is resolved and measured, and the two questions this plan was drawn Medium on have
both been answered by the owner. What remains open (retention, aggregator pluggability) does not
gate any chunk here.

**Decisions taken, both by the owner on 2026-09-05:**

- `[DECISION: the store layer uses SQLAlchemy Core — typed table metadata and a query builder, no
  ORM, no session or identity map | chosen over hand-written SQL and over the full ORM; the builder
  recommended hand-written SQL and the owner chose Core | user can revisit]` The reason the
  recommendation lost is a good one: table metadata in one typed place is worth more over thirteen
  tables than the dependency costs, and composable query construction is what `get_coverage_report`
  and `spending_summary` will actually be made of. **The recorded objection is answered by
  construction rather than dropped** — SQLAlchemy is wired through `create_engine(..., creator=...)`,
  where the creator is our own keyed connection from `store/connection.py`. That keeps every bespoke
  step (key-before-anything, WAL, `mode=ro`, `query_only`, the writer lock) inside the module that
  owns the norms, and leaves SQLAlchemy doing only the part it is good at. Verified before adopting:
  both the built-in `sqlite+pysqlcipher` dialect (which resolves to `sqlcipher3`) and the `creator=`
  route drive SQLCipher correctly and produce a ciphertext file. The `creator=` route is the one
  this plan builds, because the dialect route would move connection setup into a URL string and out
  of the module the norms are enforced in.
- `[DECISION: risk surfaces are the aggregator client, the sync/cursor layer, schema and migrations,
  the account-rule engine, and the secrets layer | the proposal already recorded in
  project-state.yaml, confirmed | user can revisit]` Recorded as `risk_surfaces:` in
  `project-state.yaml`, so Chunk 02 onward reviews at the deeper tier. These are the paths where a
  silent defect becomes wrong analysis rather than a crash, which is this product's named primary
  failure mode.

**Open assumptions:**

- `[ASSUMPTION: raw responses are retained indefinitely, with retention as a config value defaulting
  to "keep" | LOW impact | user can defer]` — system-requirements §9.1 is open. Building the knob now
  and defaulting to "keep" costs nothing and keeps the decision open; what would be expensive is a
  schema with nowhere to record the answer.

## Status

- [ ] Chunk 01: Walking skeleton — config, key, encrypted WAL datastore, and the two connection roles
- [ ] Chunk 02: The core schema (FR-6)
- [ ] Chunk 03: Raw-response layer and rebuild (FR-5)
- [ ] Chunk 04: `sync shell` (AC-ARCH.6)
Context: Plan drawn 2026-09-05, directly after `.prawduct/artifacts/architecture.md` resolved
AC-ARCH.7. Nothing built yet — the repo still holds zero lines of Python. Next: Chunk 01, which is
also where the two Requirements-Confidence questions above get answered. This plan covers build
step 1 of `docs/system-requirements.md` §8 and nothing beyond it; step 2 (the aggregator client)
gets its own plan.

## Scaffolding

### Project Initialization

`uv init --package --name bankmachine .` then
`uv add sqlcipher3-wheels sqlalchemy keyring` and
`uv add --dev pytest hypothesis mypy ruff`.

🔴 **This is the step that fixes the package name (`bankmachine`), the keychain service name, and
the scheduler label** — §8 step 1 says so, and the rename decision that unblocks it is settled and
recorded. The keychain service name is configuration with a documented default, not a literal, and
the test suite uses a test-scoped service name per `project-preferences.md`.

### Dependencies

Three runtime packages and four dev, and the smallness is deliberate — the standing supply-chain
risk factor (a public tool pulling real bank data on a stranger's machine) argues for a runtime
dependency surface small enough to actually read.

- `sqlcipher3-wheels` — the SQLCipher binding that ships working wheels for Apple Silicon.
  `sqlcipher3-binary` has no wheel for this platform and `sqlcipher3`/`pysqlcipher3` need a Homebrew
  build step; this is recorded in `project-preferences.md` and was re-confirmed at 0.5.7 while
  resolving AC-ARCH.7.
- `keyring` — credential storage behind one interface, per the recorded macOS-with-seams decision.
  Its macOS backend was confirmed on this machine with a set/get/delete round-trip.
- `sqlalchemy` — Core only (table metadata + query builder), per the recorded decision above. Wired
  via `creator=`, so it never opens a connection itself.
- dev: `pytest`, `hypothesis` (property tests for money arithmetic and idempotency, per
  preferences), `mypy` (strict), `ruff` (format + lint).

No ORM layer (Core only — no `declarative_base`, no `Session`), no migration framework (the runner
is ~50 lines and must own its own transaction boundary, per the architecture's migration-atomicity
rule), and no CLI framework — argparse is stdlib and the CLI here is four subcommands. A dependency manifest artifact does not exist yet; this section is its
stand-in, and creating it properly is `public-readiness` work, not step-1 work.

### Build & Test Configuration

`uv run pytest -q` runs everything. `tests/` mirrors `src/bankmachine/`, with `tests/preferences/`
for norm tests — including `scripts/check-no-personal-data.sh`, which **moves under
`tests/preferences/` in Chunk 01**. That migration is an obligation recorded in three places
(`project-preferences.md`, system-requirements §8 step 1, and the handoff notes) precisely because
it is the kind of thing that gets forgotten: the guard is a shell script only because no Python
scaffold existed, and a test runner can invoke it without the per-clone `core.hooksPath` config a
git hook needs. **The pre-push hook keeps working after the move** — the wiring follows the script;
losing push-time enforcement to gain test-time enforcement would be a straight downgrade.

`test_command:` stays undeclared in `project-state.yaml` until this plan lands, for the reason
recorded there (the key requires a `{junit_xml}` literal). Chunk 01 is where declaring it becomes
correct, and it should be declared then.

### Scaffold Verification

`uv run bankmachine store init` creates an encrypted datastore at the configured path;
`uv run bankmachine store status` reports it as present, WAL, at schema version 1, holding no
connections; `uv run pytest -q` passes; `uv run mypy --strict src` and `uv run ruff check` are clean.

### Verification Strategy

Tests are the floor, not the verification. Three things get exercised as the operator would
exercise them, because each is a place where a passing test and a working product can diverge:

- **The encryption claim is verified against file bytes, not against the API** — a known plaintext
  written through the schema must not be recoverable from `store.db`, `store.db-wal` or
  `store.db-shm`. AC-ARCH.5 asks for exactly this, and the WAL and shm files are named explicitly
  because a WAL-mode datastore is three files and the criterion is trivially satisfiable by
  checking only the first.
- **The concurrency norms are verified by running two processes**, not by asserting on a mock. Real
  subprocesses, real `flock`, real `SIGKILL` — the measured behaviours in the architecture artifact
  are the expected values, so these tests reproduce the probes that produced them.
- **`sync shell` is exercised by hand against a real datastore** at the end of Chunk 04, because it
  is a REPL and its whole justification (AC-ARCH.6) is being usable by a human under pressure.

## Project Structure

```
src/bankmachine/
├── config.py          # documented defaults, no hardcoded paths (AC-ARCH.4)
├── secrets.py         # keyring seam: datastore key, aggregator creds, tokens
├── store/             # the ONLY module that opens the datastore
│   ├── connection.py  # the writer factory (flock) and the reader opener (mode=ro)
│   ├── engine.py      # SQLAlchemy engines built with creator= over connection.py
│   ├── migrations/    # numbered, forward-only; DDL + version stamp in one txn
│   └── schema.py      # SQLAlchemy Core table metadata
├── cli/               # argparse; each command lands in the chunk that owns it
└── __main__.py         # console-script entry point (Chunk 01)
tests/
├── preferences/       # norm tests + the migrated leak guard
└── store/ …           # mirrors the source tree
```

The command names above are the architecture artifact's canonical surface table, not a second
enumeration — `sync shell` is `sync shell` because AC-ARCH.6 names it that. `scheduler.py` is
deliberately **absent**: the macOS-with-seams decision justifies the seam, but its only
implementation and its only caller both arrive at build step 8, and an empty protocol drawn seven
steps early is speculative generality. Step 8 introduces it alongside launchd.

### Module Boundaries

**Nothing outside `store/` opens a database connection, and nothing outside `store/connection.py`
constructs one.** `engine.py` hands SQLAlchemy a `creator=` and nothing else. Every other layer — and every layer this
plan does not yet build (connector, sync, rules, mcp) — takes a connection from `store.connection`,
in one of exactly two roles. That single rule is what makes the four architecture norms enforceable
by a structural test rather than by review, and it is what bounds the reversal cost of the
data-modeling decision above.

`secrets.py` is the only module that imports `keyring`. `config.py` imports nothing of ours.

## Build Chunks

### Chunk 01: Walking skeleton — config, key, encrypted WAL datastore, and the two connection roles

- **Description:** The thin vertical slice through every layer this product has: read config → get
  the key from the keychain → open an encrypted WAL datastore → run migrations → read it back
  through the reader role → print it from the CLI. It is deliberately the widest chunk in the plan
  because it is the one that proves the architecture, and every later chunk is a table or a command
  on top of a path this chunk establishes. It also lands all four norm-enforcement tests, closing
  issue **#1**.
- **Depends on:** none
- **Artifacts consumed:** `.prawduct/artifacts/architecture.md` (Direction, Data Ownership &
  Consistency), `docs/system-requirements.md` (AC-ARCH.3, AC-ARCH.4, AC-ARCH.5, AC-10.1, AC-10.6)
- **Deliverables:** the uv package; new `src/bankmachine/config.py`, new `src/bankmachine/secrets.py`,
  new `src/bankmachine/store/connection.py`, new `src/bankmachine/store/engine.py`, new
  `src/bankmachine/store/migrations/` with migration 001 creating `schema_version` only, new
  `src/bankmachine/cli/`, new `src/bankmachine/__main__.py`; `scripts/check-no-personal-data.sh`
  moved under `tests/preferences/` with
  its pre-push wiring intact; `test_command:` declared in `project-state.yaml`
- **Tests:** unit — config defaults and overrides, with a test that no absolute path is baked in;
  integration — the norm tests from issue **#1**, namely: two writer processes where the second
  refuses; a write through a read-role handle that **still** refuses after `PRAGMA query_only=OFF`
  (the on-case alone stays green while the hole is open); `wal_checkpoint(PASSIVE)` moving all frames
  while a reader sits between calls, against a WAL holding real frames; no implicit creation, with
  health reporting *missing* rather than *empty*; schema-version refusal; and **migration atomicity —
  a migration interrupted between its DDL and its version stamp leaves no store that reports
  healthy**. Also the AC-ARCH.5 byte-scan across `store.db`, `store.db-wal` and `store.db-shm`; a
  wrong-key path asserting it surfaces as an authentication failure rather than the
  `file is not a database` message SQLCipher actually raises; the sandbox/production flag logged at
  startup (AC-10.6); and a formatter-level redaction test asserting a token-shaped value never
  reaches a log line (AC-10.3)
- **Acceptance criteria:** scaffold verification above passes end to end on a clean checkout; the
  four norms have tests that fail when the norm is violated — verified by breaking each one
  deliberately and watching its test go red, because a norm test that has never been red is a claim,
  not a check
- **Done when:**
  1. Acceptance criteria met and tests pass
  2. `/prawduct:critic` run and blocking findings resolved
  3. Issue **#1** moved to `shipped` with `closed-by: datastore-v1`
  4. Committed and chunk marked `[x]` in Status

### Chunk 02: The core schema (FR-6)

- **Description:** The thirteen tables of FR-6, as forward-only migrations. **This is the plan's
  lock-in chunk** — the schema is the format every later consumer depends on, and it is being
  designed before its consumers exist.
- **Depends on:** Chunk 01
- **Artifacts consumed:** `docs/system-requirements.md` §4 (FR-5, FR-6, AC-6.1 through AC-6.6),
  §5 (the tool table — these are the consumers)
- **Deliverables:** migrations 002+ creating `connections`, `institutions`, `accounts`,
  `transactions`, `balances_daily`, `securities`, `holdings`, `investment_transactions`,
  `sync_state`, `raw_responses`, `account_rules`, `manual_imports`, `derivation_versions`; Core table
  metadata in `src/bankmachine/store/schema.py`; **`.prawduct/artifacts/boundary-patterns.md`
  populated**, with the datastore schema as its first contract surface — the architecture artifact
  declares this step-1 work and names this chunk as its owner
- **The questions this data must answer, enumerated before any field is designed** — the planning
  guide requires this of a persisted format, and here the consumers are already written down as the
  §5 tool table, so they are elicited rather than inferred: per-account coverage windows with gaps
  over 7 days and their source breakdown (`get_coverage_report`, AC-9.5 — *per account, never per
  institution*, which is a schema constraint before it is a query); period-over-period spending by
  category, merchant and account (`spending_summary`); income against outflow by month
  (`cashflow_summary`); a balance time series that no aggregator backfills, so it must be appended
  daily and never overwritten (`balance_history`, AC-3.1); assets minus liabilities over time with
  investments included (`net_worth`); recurring charges with cadence and amount drift
  (`find_recurring`); and per-connection health with last-success timestamps and error codes
  (`get_pipeline_health`). Two constraints fall out of that list rather than out of taste: every
  monetary column is integer minor units (AC-6.2, no floats anywhere), and every row carries enough
  provenance to answer *where did this come from* (AC-7.4) and *how was it derived* (AC-5.3).
- **Tests:** unit — round-trip of each typed mapping; property-based (hypothesis) — money arithmetic
  never leaves integer minor units, per preferences; integration — migrations apply forward on an
  empty datastore and are idempotent when re-run; a test asserting a calendar date and a UTC instant
  cannot be assigned to each other (AC-6.4's mechanism is `mypy strict`, so this one is a type-check
  assertion, not a runtime one)
- **Acceptance criteria:** all thirteen tables exist at the declared schema version; a stable local
  account id survives a simulated re-enrollment of its connection (AC-6.3); an account carries a
  lifecycle and retiring one destroys no history (AC-6.5)
- **Done when:**
  1. Acceptance criteria met and tests pass
  2. `/prawduct:critic` run and blocking findings resolved
  3. Committed and chunk marked `[x]` in Status

### Chunk 03: Raw-response layer and rebuild (FR-5)

- **Description:** The bronze half of the bronze/silver split: every response persisted verbatim
  before normalization, and a rebuild command that reconstructs the normalized tables from raw
  responses alone. Built now, with no aggregator to feed it, because retro-fitting raw preservation
  after a sync path exists means the sync path was written against the wrong contract.
- **Depends on:** Chunk 02
- **Artifacts consumed:** `docs/system-requirements.md` FR-5 (AC-5.1, AC-5.2, AC-5.3), AC-11.5
- **Deliverables:** raw-write path in `src/bankmachine/store/` (compressed JSON, endpoint,
  timestamp, connection id, hash), derivation-version recording, `bankmachine store rebuild`
- **Tests:** unit — hashing and compression round-trip; property-based — rebuild losslessness, which
  `project-preferences.md` names as one of the three invariants worth property-testing; integration
  — rebuild from a fixture corpus reproduces the normalized tables byte-identically given a recorded
  derivation version (AC-11.5), and a *changed* derivation version produces a different, recorded
  result rather than a silent one
- **Acceptance criteria:** normalized tables are rebuildable from raw responses alone; the
  derivation version is recorded alongside the rows it produced, so losslessness is well-defined
  (AC-5.3)
- **Done when:**
  1. Acceptance criteria met and tests pass
  2. `/prawduct:critic` run and blocking findings resolved
  3. Committed and chunk marked `[x]` in Status

### Chunk 04: `sync shell` (AC-ARCH.6)

- **Description:** An authenticated REPL against the encrypted datastore. Page encryption breaks
  every ad-hoc SQL tool, so without this the operator has no way to look at their own data — which
  is why AC-ARCH.6 puts it in step 1 rather than step 9, and why it is in this plan at all.
- **Depends on:** Chunk 03
- **Artifacts consumed:** `docs/system-requirements.md` AC-ARCH.6, AC-10.3
- **Deliverables:** `bankmachine sync shell` in `src/bankmachine/cli/`
- **Tests:** integration — a query runs and returns rows; a write is refused when the shell is
  opened in the reader role; output redacts anything AC-10.3 requires redacted, tested against a row
  containing a token-shaped string
- **Acceptance criteria:** an operator can open the shell against a real datastore, run a query, and
  read the result. **Verified by hand, not only by test** — the whole justification for this command
  is being usable by a human under pressure, and no assertion speaks to that
- **Type:** cumulative-final
- **Visual change:** yes — REPL output formatting and the redaction behaviour need a human look
- **Done when:**
  1. Acceptance criteria met and tests pass
  2. An entry appended to `.prawduct/operator-verification.md` for the by-hand check
  3. Committed, then `/prawduct:critic cumulative` run and blocking findings resolved
  4. Chunk marked `[x]` in Status

## Early Feedback Milestone

**Milestone chunk:** 01
**What the user can do:** run `bankmachine store init` and `bankmachine store status` against a real
encrypted datastore on their own machine, and see the key come out of their own keychain. Chunk 04
is when they can look *inside* it.

## Governance Checkpoints

**Commit & PR cadence:** commit per chunk after its Critic review passes. Chunk 04's `cumulative`
review is the PR gate.

- **After Chunk 01 — the architecture checkpoint.** This is the one that matters: the chunk exists
  to prove the topology, so the review asks whether the connection layer actually holds the four
  norms, not merely whether the tests pass. If the two-role split has already leaked — anything
  outside `store/` holding a connection — it is cheaper to fix here than anywhere later.
- **After Chunk 02 — the lock-in checkpoint.** The schema is the most expensive thing in this plan
  to get wrong, and the last point at which changing it is free. Re-read the enumerated consumer
  questions against the delivered tables and name anything they cannot answer.
- **After Chunk 04 — cumulative.** Full-bundle review; confirm the guard migration kept push-time
  enforcement, and that step 2 has a clean seam to build the aggregator client against.
