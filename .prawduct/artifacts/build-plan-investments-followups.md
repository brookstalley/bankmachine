---
artifact: build-plan
version: 1
scope: investments-followups
branch: feature/investments-followups
depends_on:
  - artifact: system-requirements
    file_path: docs/system-requirements.md
  - artifact: api-contract
    file_path: .prawduct/artifacts/api-contract.md
  - artifact: data-model
    file_path: .prawduct/artifacts/data-model.md
  - artifact: boundary-patterns
    file_path: .prawduct/artifacts/boundary-patterns.md
  - artifact: api-notes-plaid
    file_path: .prawduct/artifacts/api-notes-plaid.md
governed_by:
  - artifact: api-contract
    dispositions:
      - "the MCP surface is read-only → conforms; Chunk 03 adds a read and a warning, and nothing writes. Migration 012 is DDL run by `store migrate`, never by the server"
      - "every response carries a freshness stamp, and incompleteness rides the success path as a warning field → conforms, and Chunk 03 is an application of it: rows derived under another version are a successful answer computed over rows that may not be what this build would produce, so the answer says so. Additive by the vocabulary's own evolution rule"
      - "CLI three-way exit code → conforms; `store status` gains a line and its exit code is unchanged — a store awaiting a rebuild is still a healthy file, and the line names the remedy"
      - "a tool's boundary is drawn where the answer shape changes → conforms; no tool is added. `get_pipeline_health` gains a coverage key, precedent `transactions_in_effective_window`, which only windowed answers carry"
      - "a stored balance is reported with its lifecycle → inapplicable because no total over balances is emitted or changed"
  - artifact: data-model
    dispositions:
      - "a migration's DDL is frozen once written, and the Core metadata and that DDL are written independently → conforms; migration 012 is a new module whose indexes are declared again, independently, in `schema.py`, and `tests/store/test_schema.py` compares the two"
      - "every stored amount is signed from the operator's point of view → inapplicable because no amount is written or read"
      - "all monetary values are integer minor units → inapplicable because no amount is written or read"
      - "calendar dates and UTC instants are distinct types and never mix → inapplicable because no date is written or read"
      - "every silver row carries the evidence for which it is → conforms, and Chunk 03 reads that evidence: `derivation_version_id` is what the check asks about"
      - "the daily balance and holdings series are append-only → inapplicable because nothing here writes a series row"
      - "a source value is never overwritten in place → inapplicable because nothing here writes a source value"
      - "a transaction is never hard-deleted → inapplicable because nothing here deletes a transaction"
  - artifact: security-model
    dispositions:
      - "log redaction over-redacts by design and is keyed to credential shape → conforms, narrowly: Chunk 02 spares ONE cell shape in ONE surface (`sync shell`) where two independent conditions both hold, a column the schema declares a public identifier and a value that passes that identifier's check digit. The log formatter and `redact_free_text` are untouched"
      - "secrets live only in the OS keychain → inapplicable because nothing here touches a credential"
      - "no tracked file carries a credential-shaped string → inapplicable because no credential is written anywhere"
      - "the aggregator's API is the only network destination → inapplicable because nothing here reaches the network"
partition: serial — the chunks share little code but Chunk 02 and Chunk 03 both edit `store/schema.py` and the change-log, and the whole is small enough that a delegate's integration cost exceeds the wall clock it would save.
last_validated: 2026-09-14
---

# Build plan — three investments follow-ups: the bill, a CUSIP, and rows from an older derivation

Closes brookstalley/bankmachine#106, #112 and #116, each filed during `build-plan-investment-sync`.
All three decisions were put to the owner on 2026-09-14 and ruled on in this session.

## Advisory — what I would do differently

**#106 was filed against the wrong cost.** It worries that the union gate initializes investments
on Items that only *could* serve them. But enrollment has asked for `investments` as an OPTIONAL
product since 2026-09-09 (`ENROLLMENT_OPTIONAL_PRODUCTS`, owner decision 2026-09-07). So wherever an
institution supports it, the product is on the Item from the moment it's linked, and the gate reaches
almost nothing new. The unrecorded cost is a **second subscription**. The aggregator bills Investments
Holdings and Investments Transactions separately, and calling `/investments/transactions/get` adds
the second (<https://plaid.com/docs/account/billing/>, fetched 2026-09-14). Every sync makes that call
for every capable connection. The owner ruled: accept both, keep the union gate, document it.

**One acceptance box on #106 is NOT met, and this plan says so rather than ticking it:** "the
per-Item cost … established from the owner's contract". The aggregator publishes no investments
rate, and the owner supplied none. The artifacts record the cost as **unpriced**.

**Scope I am deliberately NOT adding:** a config switch for either investments call (offered, not
chosen); refreshing `connections.capabilities` after enrollment (#109); rendering any other
identifier column unmasked; re-deriving automatically when the version moves (the remedy stays
`store rebuild`, which the operator runs).

## Requirements Confidence

**Level:** High

**Why:** each item is one observable problem with a ruled answer. #106: the artifacts under-state
what a production sync bills. #112: a real brokerage's all-digit CUSIP reads `****3100` in the
shell. #116: a store derived at an older version answers with nothing saying so.

**Open assumptions / unknowns:**

- [ASSUMPTION: the #116 warning fires when ANY derived row carries a version OTHER than this build's
  — older or newer — not only older | MED impact | user can veto] — the newer case is a server
  running an older build against a store a newer sync wrote. That is exactly how the production MCP
  server is running today, on a build from before holdings. The remedy differs (upgrade the server,
  not rebuild the store), so `detail` says which.
- [ASSUMPTION: the kind is spelled `derivation_version_mismatch` and joins
  `envelope.CONNECTION_SCOPED_KINDS` | LOW impact | user can override] — that tuple is what the
  handshake calls "describe the PIPELINE and ride every answer", and `partial` already fires there
  for a store-wide fact (no connection enrolled). Renaming the tuple is out of scope; its comment is
  amended to say store-wide facts belong too.
- [ASSUMPTION: the structured field is `coverage.derivation` on `get_pipeline_health` only, as
  `{"current_version": int, "versions_in_store": [int, ...]}` | LOW impact | user can override] —
  store-wide, so not on the per-connection rows; per tool, like `transactions_in_effective_window`.
- [ASSUMPTION: the CUSIP exemption matches the RESULT column's name against the names `schema.py`
  flags, because the Python driver does not expose a result column's origin table | MED impact |
  user can veto] — a name alone is the relationship this repo's learnings warn about. The check digit
  is the second condition that makes the name safe: `SELECT description AS cusip` renders masked
  unless the description is itself a valid 9-character CUSIP.
- [UNVERIFIED: what `/investments/holdings/get` returns for an Item whose client has investments
  disabled in the dashboard — the production guide's suggested way to avoid the bill. If it refuses,
  every nightly sync records an investments error and exits `1`. The sandbox cannot show this; it
  goes to `api-notes-plaid.md` § Still to verify, and the production guide says so.]

**What would raise confidence:** N/A at High.

## Status

- [x] Chunk 01: The investments bill, stated where the operator decides it
- [x] Chunk 02: A CUSIP reads as itself in `sync shell`
- [x] Chunk 03: An answer says when its rows were derived by another version

Context: all three chunks are committed: `0568c96` (01), `9c4ca16` (02), `32d0966` (03), and the
cumulative review's resolutions `9bbc404`. The cumulative review raised 2 blocking; both are fixed,
and so are both warnings. The two notes were accepted with their terms in the handoff notes. The
verify-resolutions pass found 0 blocking. Suite green (1,826). VRF-025 and VRF-026 were re-raised
as VRF-028 (#22) and VRF-029 (#23) and accepted to open the PR, to be raised again on the next
branch. VRF-027 was run against the sandbox as VRF-030 and VRF-031, both verified. Owed at ship:
close #106 (price box unmet), #112 and #116.

`[DECISION: Chunk 03 also changes \`rebuild\`'s content-change expectation to count the dimension
tables' derivation versions | found while verifying AC-5.4's remedy on the product: a store whose
only older rows were \`securities\` made \`store rebuild\` refuse, while the new warning told the
operator to run it. AC-5.4 promises the warning holds "until those rows are re-derived", so a
remedy that refuses is inside that criterion, not new scope. A test isolating the dimension-only case
was seen red before the fix | user can veto]`

`[DECISION: \`store rebuild\` refuses when any stored row is newer than this build | cumulative
review R-5: a rebuild re-stamps what it writes, so an older build would silently replace newer rows
and the warning would go quiet. Raised by the review of this chunk, fixed in its resolution commit |
user can veto]`

## Scaffolding

None. `rebuild.derived_tables()` already enumerates every table carrying `derivation_version_id` by
its foreign key, and `rebuild._previous_derivation_versions` already asks the question Chunk 03 asks.
`_pipeline_warnings` is the one site pipeline kinds are built. `sync._render` is the one site a shell
cell is rendered.

### Verification Strategy

Tests for each chunk, then the product. Chunk 02: run `sync shell` against a scratch copy of the
sandbox store with a securities row carrying `037833100`. Chunk 03: migrate a scratch copy of the
sandbox store to schema 12, then drive `bankmachine mcp` over stdio on this build (confirm by
`build.commit`). First with every row current: the kind is absent. Then with one row stamped at a
lower version: the kind is on every tool's answer. Then after `store rebuild`: absent again. Latency
of the new check is measured on a synthetic two-year store, in the same form as
`mcp-search-latency-2026-09-14.md`.

## Build Chunks

### Chunk 01: The investments bill, stated where the operator decides it

- **Description:** An operator reading the production guide before the cutover learns that a
  capable connection carries two investments subscriptions, when each one starts, and that neither
  is priced here.
- **Depends on:** nothing
- **Artifacts consumed:** #106; `api-notes-plaid.md` §13, §25; `docs/system-requirements.md` AC-3.2;
  `docs/first-production-connection.md` §1.2
- **Deliverables:**
  - AC-3.2 records the ruling in its as-built note: the union gate stands; enrollment's optional
    `investments` adds Investments Holdings at link time; the first sync's
    `/investments/transactions/get` adds Investments Transactions; both accepted 2026-09-14,
    unpriced.
  - §25 gains the billing fact with its source and fetch date, and its closing "cost decision
    belongs to the owner" becomes the decision taken. §13's finding — `products` alone discovers
    nothing — is left standing, and the note says the gate did not change.
  - The production guide §1.2 names both subscriptions and when each starts, and marks the
    dashboard-disabled path's sync behaviour as unverified.
  - `api-notes-plaid.md` § Still to verify gains that open question.
  - The comment on `ENROLLMENT_OPTIONAL_PRODUCTS` and the gate comment in `sync_run._sync_one` stop
    implying one bill.
- **Tests:** none — no behaviour changes. `git grep -n -i "bill"` over `docs/`, `.prawduct/artifacts/`
  and `src/` is re-run as a Done-when step, and every claim about investments billing it finds
  either names both subscriptions or is explicitly about one of them.
- **Type:** docs
- **Done when:**
  1. The grep above is clean
  2. Committed
  3. Chunk marked `[x]` in Status

### Chunk 02: A CUSIP reads as itself in `sync shell`

- **Description:** An operator selecting `securities` sees `037833100`, not `****3100`, while every
  other run of eight or more digits — including one aliased into a column named `cusip` — stays
  masked.
- **Depends on:** nothing
- **Artifacts consumed:** #112; `boundary-patterns.md` § Operator SQL Surface; AC-10.3
- **Deliverables:**
  - `securities.cusip` declared a public identifier in `schema.py` (`Column.info`), and a function
    that reads the flagged column names out of the metadata rather than a list in `sync.py`.
  - `_print_table` passes each cell's column name to `_render`. A cell renders verbatim when its
    column is flagged AND the value is nine characters of the CUSIP alphabet with a valid check
    digit. Anything else takes the value rule as today.
  - `boundary-patterns.md` gains the fifth boundary — a public identifier is not an account number —
    with both conditions and why neither alone suffices (the name is forgeable by an alias; the
    shape alone passes about one random 9-digit number in ten).
  - `[DECISION: ISIN is not flagged | its two-letter country prefix leaves no eight-digit run for the
    rule to catch, so it already renders intact | user can veto]`
- **Tests:** unit — the check digit against published CUSIPs (including one with letters) and
  against each single-digit corruption of one; a flagged column with a valid CUSIP renders verbatim;
  a flagged column with an invalid 9-digit value, an unflagged column with a valid CUSIP, and an
  8+-digit run aliased `AS cusip` each render masked, every fixture valid in every respect but the one
  it exercises. Guard — the flagged set is non-empty and every name in it is a real column.
- **Type:** code
- **Done when:**
  1. Tests pass, `bash scripts/check.sh` green
  2. Each new assertion verified by breaking what it names — drop the check digit test and watch the
     aliased case go red; drop the column test and watch the unflagged case go red
  3. Product verified against a scratch copy of the sandbox store
  4. Committed. Reviewed by Chunk 03's cumulative Critic, not a chunk review of its own
  5. Chunk marked `[x]` in Status

  `[DECISION: no separate chunk Critic for Chunk 02 | a review snapshots the tree, so an edit under
  review voids it. Chunk 03 edits `store/schema.py` too, and would wait out the whole review. The
  diff is small, and Chunk 03's `cumulative` covers merge-base...HEAD, which includes it. Amended
  2026-09-14, mid-build | user can veto]`

### Chunk 03: An answer says when its rows were derived by another version

- **Description:** Until the operator runs `store rebuild`, every MCP answer from a store holding
  rows derived at a version other than this build's carries a warning naming the versions and the
  remedy. `get_pipeline_health` and `store status` say the same in structured form.
- **Depends on:** nothing
- **Artifacts consumed:** #116; AC-5.3; `api-contract.md` § The warning vocabulary;
  `data-model.md` § Provenance, § Constraints 7; `envelope.py` scope tuples
- **Deliverables:**
  - **The requirement, written first.** AC-5.4: a store whose derived rows carry a derivation version
    other than this build's says so on every MCP answer, on `get_pipeline_health` and on
    `store status`, until those rows are re-derived.
  - **Migration 012** — one index on `derivation_version_id` for every table `derived_tables()` names,
    declared again independently in `schema.py`. A guard fails when a derived table has no index
    leading with that column, so a future derived table cannot slip out of it.
  - One store-reading function (in `src/bankmachine/store/rebuild.py`, replacing
    `_previous_derivation_versions` rather than duplicating it) returning the distinct versions present across derived
    tables, as index lookups.
  - `derivation_version_mismatch` in `CONNECTION_SCOPED_KINDS`, built in `_pipeline_warnings`, with
    `detail` distinguishing older rows (run `bankmachine store rebuild`) from newer ones (this server
    is older than the build that derived them). `_GUIDANCE` entry in `mcp_resources.py`; the handshake
    text picks it up from the tuple.
  - `coverage.derivation` on `get_pipeline_health` and its `outputSchema`; a `derivation:`
    line in `store status`.
  - `api-contract.md` vocabulary table, `data-model.md` (migration 012, the indexes), change-log.
- **Tests:** unit — absent when every row is current; present on every tool's answer when one row
  in any derived table is older; the newer case says the server is older; present on an empty store
  never. Multi-hop — sync-derived rows at version N, then a rebuild at N+1 clears it: this proves a
  rebuild re-stamps the dimension tables (`securities` is upserted, not deleted) and does not leave
  the warning standing forever. Migration — 011 → 012 upgrade on a populated fixture; metadata/DDL
  index comparison. Existing vocabulary guards (closed kinds, guidance for every kind, handshake names
  every pipeline kind) extended, not duplicated.
- **Type:** cumulative-final
  <!-- Last chunk: its review IS the one `/prawduct:critic cumulative` over
       merge-base...HEAD — commit first, run once, no separate `final`. -->
- **Done when:**
  1. Tests pass, `bash scripts/check.sh` green
  2. Each new assertion verified by breaking what it names — in particular, point the check at
     `rebuildable_tables()` instead of `derived_tables()` and watch the `securities` case go red
  3. Latency measured and recorded
  4. Product verified over stdio on this build against a scratch copy of the sandbox store
  5. Committed, then `/prawduct:critic cumulative` run and blocking findings resolved
  6. Chunk marked `[x]` in Status
