---
artifact: build-plan
version: 1
scope: unservable-datastore
branch: fix/unservable-datastore-refuses
critic_mode: cumulative-final
depends_on:
  - artifact: api-contract
    file_path: .prawduct/artifacts/api-contract.md
  - artifact: architecture
    file_path: .prawduct/artifacts/architecture.md
governed_by:
  - artifact: api-contract
    dispositions:
      - "🔴 § Hard errors: *a schema version the process does not recognize is a hard error, including for the reader — it answers `get_pipeline_health` with a refusal rather than serving queries* → this plan is the implementation of that sentence. The contract already said it; the code did not do it"
      - "incompleteness rides the success path as a warning field, not an exception → conforms, and the boundary is the point. An unservable store is not an incomplete answer, it is NO answer over data that exists. The success-path rule keeps every case it already covered"
      - "the unreadable-store path carries coverage present-and-zero → PRESERVED for the one state that still reaches it. This plan does not change `_unusable`'s shape, it changes which states arrive there"
      - "the warning vocabulary is closed → conforms; no kind is added, removed or re-spelled. `partial` stops being emitted for a state it never meant"
      - "no stack traces, internal identifiers or PII cross the boundary → conforms; the refusal carries a stable code and a remedy sentence this product wrote"
  - artifact: architecture
    dispositions:
      - "🔴 § Direction: *a process that does not recognize the datastore's schema version refuses to serve, loudly* → this is the governing norm and the plan honours it rather than amending it. Ruled by the owner 2026-09-09"
      - "§ Direction: *no component creates the datastore implicitly* → conforms; nothing here creates, migrates or writes anything. The refusal is emitted from a read path and its remedy NAMES `store init` rather than performing it, which is the norm's distinction held exactly"
      - "§ Direction: *every writable handle comes from the one writer factory, and that factory takes the exclusive writer lock* → conforms; no writable handle is opened in `src/` by this change. The new tests build their states through the two handles that already exist: `initializing_writer` for a pre-migration store (the migration runner's handle, and the one path permitted to open an unsupported schema) and `writer()` for stamping a version forward on a healthy one. Both are the factory rather than around it — no test opens a connection of its own"
      - "§ Direction: *every read-role handle is opened read-only at the file (`mode=ro`), holds no read snapshot* → conforms; `inspect` already opens with `require_supported_schema=False` and this change reads its result rather than changing how it opens"
---

# Build Plan — an unservable datastore refuses, loudly

## Problem

`query._unusable` answers every tool with zeroed coverage and `rows: []` on the success path
(`isError: False`) whenever the datastore cannot be served. Measured against a store holding **14
accounts and 388 transactions** at schema 2 against a build serving 4: every tool reports the
household owns nothing, as a success.

Two ratified artifacts say this must be a refusal:

- `api-contract.md` § Hard errors — *"A schema version the process does not recognize is a hard
  error, including for the reader — it answers `get_pipeline_health` with a refusal rather than
  serving queries, because a reader running older code against a migrated schema returns plausible,
  structurally valid, wrong answers."*
- `architecture.md` § Direction — the same rule as a steady-state norm, with the same why.

Filed as **#58**; **#57** (the hardcoded remedy sentence) is expected to close as a consequence, and
that expectation is tested rather than assumed.

## The ruling this plan was unblocked by

**Owner ruling, 2026-09-09.** Honour the norm. The carve-out in AC-ARCH.3 — *"The MCP server starts
successfully when the datastore is empty or missing, and reports that state through
`get_pipeline_health` rather than crashing"* — is **scoped to a store that is not there**, which is
what its own words say and what both go-red cases enforcing it anchor to
(`test_the_server_starts_and_answers_when_the_datastore_is_missing`,
`test_cmd_mcp_itself_starts_against_a_missing_datastore`).

🔴 **The line this plan draws, and the reason it is the right line.** A missing store has no data to
misreport, so answering "nothing, and here is why" is honest. Every other unservable state means
**data exists and cannot be read**, and answering zero about data that exists is the failure both
artifacts name. `query._readable` collapses all five of `connection.inspect`'s distinct problems into
one path, and that collapse is the defect — not the carve-out itself.

**Amending either norm to match the current code was explicitly off the table.**

## Requirements Confidence

**High.** The behaviour is not being designed here; it is written in two ratified artifacts in
near-identical words, and the owner ruled on which side of AC-ARCH.3's carve-out the schema-mismatch
state falls. What this plan chooses is mechanism, not policy.

### Open assumptions

- `[ASSUMPTION: after this change the ONLY state reaching `_unusable` is "datastore missing", so
  #57's hardcoded "Run `bankmachine store init` to create it" becomes correct rather than wrong.]`
  Vetoable, and **checked by a test that enumerates `inspect()`'s branches** rather than by
  inspection — if a sixth state exists, the test names it.
- `[ASSUMPTION: refusing is safe for the CLI paths, because they do not route through `_unusable` —
  `store status` reports unhealthy by design and must keep doing so.]` Checked by the existing CLI
  suite, which this plan does not touch.

## Decisions

- **A new stable error code, `datastore_unservable`**, rather than reusing `internal_error`. A
  consumer branching on the code needs to tell "this build cannot serve this store, fix the store"
  from "something broke, retry is pointless". The remedy sentence is the operator's action.
- **The refusal is a tool error (`isError: True`), not a JSON-RPC protocol error.** The pipe stays
  open and the session survives; the client sees a failed call rather than a vanished server, which
  is the same reasoning the existing broad catch at the boundary records.
- **`_readable` returns the `DatastoreStatus`, not a string.** The remedy must branch on
  `exists` / `readable` / `schema_version`, never on parsing prose. This is the one-line collapse the
  problem statement names, undone.
- **The server still starts against any store, including an unservable one.** AC-ARCH.3's start
  guarantee is about the process, not about whether calls succeed. Both `cmd_mcp` go-red cases stay
  green untouched.

## Status

- [x] **00 · The split: `_readable` carries structure, and only a missing store answers**
- [x] **01 · The refusal reaches the client as a coded tool error**
- [x] **02 · The remedy sentence tells the truth per state (#57)**
- [x] **03 · The norm's untested clause, and the go-red re-anchoring**

**Context.** All four chunks built in one pass on `fix/unservable-datastore-refuses`; the change is
small enough that splitting the commit would have separated the refusal from the test that proves it.
Suite green. Both go-red cases were run individually and both report RED, with the mutation asserted
to have landed before the run — a green mutation that never applied is the failure mode the harness
itself warns about.

🔴 **`_readable`'s one-line collapse was the whole defect.** Five distinct states from
`connection.inspect`, one return value, so the carve-out written for a missing store reached a
populated one. The fix is that `inspect` now says WHICH state it found (`DatastoreProblem`) and
`_readable` branches on that value rather than on the wording of a sentence.

**The assumption in § Open assumptions held, and is now tested rather than believed.** After the
split the only state reaching `_unusable` is a missing store, for which its hardcoded
*"Run `bankmachine store init` to create it"* is correct — so **#57 closes as a consequence of #58**
rather than needing its own pass. `test_every_problem_the_enum_declares_is_constructed_and_remedied`
is what keeps a sixth state from silently falling through to the generic remedy: it is driven from
`DatastoreProblem` itself, so a new member goes red until something builds a store in that state and
asserts what the operator is told.

## Chunks

### Chunk 00: The split — `_readable` carries structure, and only a missing store answers

Delivers: `connection.inspect` reports a `DatastoreProblem` beside its prose `problem`, so a caller
can branch on the state; `_readable` raises `DatastoreUnservableError` for every unservable state
EXCEPT a missing store, and returns the missing store's reason as before; `_unusable` keeps its
present shape and is reached only by the missing case.

**As built, differing from this chunk's first draft:** `_readable` keeps its `str | None` return
rather than handing back the whole `DatastoreStatus`. The five call sites already read
*"a reason, or None"* and none of them wanted the status object — passing it up would have widened
five signatures so that one new branch could be taken in one place. The branch lives in
`_unservable_remedy` instead, which is the only thing that needed it. The plan's requirement — branch
on structure, never on the wording of `problem` — is met by `DatastoreProblem`, which is where it
belongs: in what `inspect` reports, not in what `_readable` forwards.

🔴 **`_unusable`'s wire shape does not change.** The present-and-zero coverage, the
`effective_window` with null bounds, the empty `totals` list — all pinned by `api-contract.md` and
two `verify_norms_go_red.py` cases, all preserved. This chunk changes *which states arrive*, not
*what arrives*.

**Done when:** every non-missing unservable state raises; the missing state answers exactly as it
does today, asserted byte-for-byte against the current payload.

### Chunk 01: The refusal reaches the client as a coded tool error

Delivers: `DatastoreUnservableError` added to the named-exception tuple at the `tools/call`
boundary, ahead of the broad catch, returning `_tool_error(..., "datastore_unservable", remedy)`.

🔴 **Ahead of the broad catch, or the remedy is lost.** Falling through to `internal_error` would
hand back "the failure has been logged" for a state the operator can fix in one command, which is
the recoverable-vs-not distinction the code vocabulary exists to carry.

**Done when:** driving the server over stdio against a schema-2 store returns `isError: True`,
`error.code == "datastore_unservable"`, and the pipe survives a following call.

### Chunk 02: The remedy sentence tells the truth per state (#57)

Delivers: a remedy per state — missing, no recognized version, **schema behind this build, schema
ahead of it**, lock unopenable, key absent — composed from the structured status.

🔴 **Six states, not five. The schema mismatch is TWO.** `inspect` sets its schema reason on
`version != SUPPORTED_SCHEMA_VERSION`, which is true in both directions, and `migrate()` is
forward-only — so one sentence covering both prescribes "run the pending migrations" to an operator
whose store is AHEAD, where it applies nothing and changes nothing. That direction is the one
`api-contract.md` § Hard errors names as the reason the state refuses at all (*a reader running older
code against a migrated schema*), so answering it with the other direction's remedy misses the case
the requirement was written for. Caught by the Critic after the first implementation shipped exactly
that collapse — the same defect this plan exists to fix, one level down.

🔴 **The key-absent remedy must agree with `secrets.py`.** That module carries an explicit comment
that its message is *"deliberately not `run bankmachine store init`"*, because `store init` refuses
to mint a key for an existing store. The MCP surface currently recommends exactly that command for
exactly that state.

🔴 **No remedy names `store rebuild` without a test that runs it.** `learnings-detail.md` § *A
documented remedy is a claim and is asserted like one* records that step rolling back on this very
shape of store. A remedy that fails spends the operator's trust on the way to failing.

**Done when:** each remedy has a test that builds a store in that state and asserts the sentence;
`#57` is either closed by this chunk or its residue is named.

### Chunk 03: The norm's untested clause, and the go-red re-anchoring

Delivers: a test for the clause both artifacts state and nothing covered — that
`get_pipeline_health` refuses on a schema mismatch; the `verify_norms_go_red.py` case whose label
reads *"AC-ARCH.3: the MCP server reports an unreadable datastore, never refuses"* re-anchored to the
missing store it actually enforces.

🔴 **That label is where the over-generalization is written down.** Its mutation target is
`_readable`, and its anchor test is named for a *missing* datastore — so the case is sound and the
label is not. Fixing the label without moving the anchor is the whole change.

**Done when:** the suite is green, every go-red case is RED, and `test_connection_norms.py` § Norm 4
covers the MCP clause rather than stopping at the store layer.

## What this plan does NOT close

- **#59** — nothing here tests a populated store upgrading across schema versions. This plan makes
  the unservable state *loud*; it does not make the upgrade out of it *safe*. Separate item,
  deliberately not folded in: it is a store-layer concern with its own fixtures.
- **The `blocks:production` label on #58** and its pairing with numbered item 8 in
  `mcp-production-readiness.md` — bookkeeping owed at merge, not here.
- **#56** — untouched. Same files, unrelated defect.
- **Five sibling test names in `test_mcp.py`** still say *"unreadable datastore"* where the state
  they build is a MISSING one — imprecise rather than wrong, and every one still passes and still
  tests what it was written to test. Accepted from the Critic review (R-6) and **carried, not
  dropped: the next commit that opens `test_mcp.py` renames them.** The label the code actually
  reads from is the go-red case, and that one is fixed here.
- **`partial` is still off-label for the one state that still answers.** The contract defines it as
  *"A contributing account has bounded history"*; a MISSING datastore has no accounts at all, so the
  kind is imprecise there for the same reason it was imprecise for the states this branch moved to a
  refusal. Not fixed here, and deliberately: the vocabulary is CLOSED and enforced by a schema
  validator, so a new kind lands with its declaration, its guidance and the server instructions in
  one commit or it takes whole answers down — a bigger, separate change than the one this plan
  ruled. Named so the residue is visible rather than inherited silently.
- **No test asserts `cmd_mcp`'s per-state startup phrasing.** Raised as an observation and
  deliberately not taken: it is a LOG line rather than a wire contract, and asserting its wording
  pins prose that is free to improve — the decay this repo already has a rule against. The half of
  that observation that WAS actionable is discharged rather than deferred: all three go-red cases
  touched by this branch were run individually and all three report RED, with the mutation asserted
  to differ from the original before each run. The harness-reach test proves an anchor is findable,
  not that it still goes red, so the two checks are not substitutes.
- **A closed-set guard over the new error-code vocabulary** — filed from the same review (R-7). The
  table in `api-contract.md` is this repo's fourth published vocabulary and the only one nothing
  enforces; the other three each have a `tests/preferences/` guard.

## What this branch carries besides this plan

🔴 **Named because the diff is wider than the plan, and a plan that does not say so is a plan its own
reviewer has to reconcile by hand.** This branch was cut mid-session, after governance work that was
already on the working tree:

- `mcp-production-readiness.md` — the "Cutover readiness — checked 2026-09-09" section and numbered
  blocker **item 8**, which is the finding this plan fixes. Item 8 is marked FIXED here because the
  fix and the entry landing apart would leave the readiness document asserting an open blocker
  against a build that closed it.
- `build-plan-production-blockers.md` — a `### Status` heading promoted to `## Status`. That plan's
  ticks were nested one level too deep, so governance read a fully-ticked plan as having chunks
  outstanding. Unrelated to this fix and committed alongside it rather than stranded.
