---
artifact: build-plan
version: 1
scope: investments-v1
branch: feat/investment-sync
depends_on:
  - artifact: data-model
  - artifact: api-contract
  - artifact: api-notes-plaid
  - artifact: architecture
  - artifact: operational-spec
governed_by:
  - artifact: data-model
    dispositions:
      - "Every stored amount is signed from the operator's point of view → conforms, and this plan is the second feed's worth of evidence the norm's own Scope (2026-09-09) says it does not have. Holdings carry a market value and investment transactions carry an amount; both go through the connector's existing normalization rather than a second one written beside it. 🔴 The norm's amendment about VALUATIONS is the clause that actually binds here: an investment `institution_value` is price times quantity and arrives at whatever precision that arithmetic produced (measured: a sandbox 401k at 23631.9805 USD), so it is rounded half-even and logged, while a ledger amount is still converted exactly or refused"
      - "A valuation is rounded to a minor unit this build KNOWS, else refused per ROW (amendment 2026-09-10) → conforms and is exercised for the first time by a table other than balances. `securities.close_price_minor`, `holdings.market_value_minor` and `holdings.cost_basis_minor` all pass through `store/types.py::minor_digits`; a security denominated in an `unofficial_currency_code` this build has no exponent for is refused ROW-WISE, keeping the rest of the account's positions"
      - "All monetary values are integer minor units, no floats → conforms. 🔴 And the counterpart the schema already draws is the one to hold onto: a QUANTITY is not money. `holdings.quantity` and `investment_transactions.quantity` are exact decimal TEXT, parsed to `Decimal` and never through a float, because fractional shares are routine and a scaled integer's scale would be a guess"
      - "Calendar dates and UTC instants are distinct and never mix → conforms. `as_of_date`, `trade_date`, `settlement_date` and `close_price_as_of` are calendar dates; `captured_at`, `first_seen_at`, `updated_at` are UTC instants"
      - "Every silver row carries exclusive provenance and its derivation version → conforms. Every row this plan writes is `source='aggregator'` with a `raw_response_id` and no `manual_import_id`, which the tables' own CHECK constraints already enforce. The derivation version is bumped when the derivation changes, per the norm"
      - "🔴 The daily balance and HOLDINGS series are append-only → this plan is the first code that writes the holdings half, so the norm stops being partly hypothetical here. The composite PK is the structural half; the behavioural half is the rule `_write_balance` already implements and holdings must reuse rather than reimplement — FIRST capture of the day wins, decided by COMPARING captures rather than by arrival order, and a row with no `raw_response_id` (a manual import) is never replaced. Copying that logic into a second deriver is how the two drift; the plan factors it (Chunk 01)"
      - "A source value is never overwritten in place → conforms; this plan adds no override column and writes no interpretation over a source field"
      - "A transaction is never hard-deleted; removal is a soft delete → conforms in SHAPE, and by a different mechanism than `transactions`. `investment_transactions.removed_at` carries the same column and the same never-a-DELETE rule, but 🔴 the feed sends **no removal signal of any kind** — measured, `api-notes-plaid.md` §26: `/investments/transactions/get` is a windowed read, not a delta, so there is no `removed` array to read and `cancel_transaction_id` is a cancellation reference rather than a tombstone. What makes the norm satisfiable anyway is that the window comes back WHOLE: a stored row inside the requested window whose id did not return has gone away, and that is the removal signal. It is only trustworthy when the run actually exhausted the window, so the reconciliation is guarded on exhaustion and a partial run marks nothing — an unguarded one would soft-delete every row it merely had not reached yet"
      - "A migration's DDL is frozen once written → conforms, and is the reason this plan adds NO migration. `securities`, `holdings` and `investment_transactions` were created in `core_schema.py` at build step 1 and are untouched here. If the real API shape turns out to need a column, that is a new migration and a recorded decision, never an edit to the frozen DDL"
  - artifact: architecture
    dispositions:
      - "Every writable handle comes from the one writer factory, which takes the exclusive lock before it returns → conforms; the investments pull persists through the same `_persist` path every other endpoint uses and opens no handle of its own"
      - "Read-role handles are `mode=ro`, hold no snapshot beyond the statement, never fall back → conforms. The health-surface work in Chunk 03 is read-only and goes through the existing reader"
      - "No component creates the datastore implicitly → conforms; nothing here opens a datastore path"
      - "A process that does not recognize the schema version refuses to serve → conforms; no schema version moves"
  - artifact: api-contract
    dispositions:
      - "The MCP surface is read-only, and the one permitted write class is agent-authored rows in a declared sidecar table → conforms. Both new tools are reads. No handle in the server process becomes writable, and neither tool reaches a table carrying `raw_response_id` or `derivation_version_id` — `holdings` and `balances_daily` carry both and are read-only to this surface whatever the column"
      - "🔴 Every response carries a freshness stamp, and incompleteness rides the success path as a warning rather than an exception → conforms, and is the norm Chunk 03 exists to satisfy. A second sync domain that no health surface reports is silent staleness with a new cause, which AC-4.4 names as this system's primary failure mode. A connection that is fresh for transactions and three weeks stale for investments must say so on the success path"
      - "The CLI's three-way exit code is a contract, and the 1/2 split is not collapsible → conforms. An investments-domain failure is a connection that ran and found a problem (`1`), never a run that could not start (`2`); a run that completed but still owes an investments backfill keeps the existing `75`"
      - "A tool's boundary is drawn where the answer shape changes → conforms, and it decides the shape of both new tools rather than merely permitting them. A holding is a new row shape, so `list_holdings` is a new tool and not an action parameter (Chunk 05). `balance_history` absorbed `net_worth` under the merge this norm forced, so its per-account and aggregate answers must come back under ONE strict row shape with no optional fields — `{date, account_id, assets_minor, liabilities_minor, net_minor, currency}`, `account_id` null for the aggregate row, derived in `discovery-mcp-tool-surface.md` (Chunk 07). `mcp._refuse_optional_row_fields` enforces the guardrail at registration, so a row field that is present only sometimes is a startup failure rather than a review comment"
      - "🔴 A stored balance is reported with its lifecycle, and no total over balances is emitted without stating its treatment of non-active accounts → conforms, and Chunk 08 is where it is paid. The norm was written about BALANCES and its arithmetic is identical for holdings: a position in a closed brokerage account is a plausible wrong number whose wrongness has no signal. Treatment is include-and-flag with the MAGNITUDE, not the flag alone — a count and a signed sum, present-and-zero rather than absent — because the norm's own steady-state condition was met by seeing the guard go red with the magnitude removed, not merely with the flag flipped. `list_holdings` (Chunk 05) emits no total and carries the per-row lifecycle; `balance_history` (Chunk 08) emits the total and owes the figure"
      - "Additive changes only; new warning codes are additive and need no version bump; consumers must tolerate an unknown kind → conforms. Both tools are `experimental` and both are already named in the § Surface Inventory and the tool table, so `test_the_documented_tool_surface_is_the_built_one.py` (built ⊆ specified) passes the moment the code lands — 🔴 and would FAIL if the contract rows were rewritten ahead of the code, which is why neither tool is a two-commit job"
  - artifact: security-model
    dispositions:
      - "Secrets live only in the OS keychain and never reach a log, exception or `repr` → conforms; the investments calls take the same access token by the same path"
      - "The aggregator's API is the only network destination → conforms; two new endpoints on the same host, no new destination"
      - "Log redaction happens at the formatter, over-redacts by design, and is keyed to credential shape → conforms, and is worth naming because this build deliberately logs values it did not log before. The valuation-rounding line carries a security's amount and currency and the per-row refusal names the instrument; neither is credential-shaped, and the formatter over-redacts anything that is. No redaction rule is added and none is relaxed — which also means a long opaque `security_id` may well come back `[REDACTED]` in a log line, and that is the over-redaction working"
      - "No tracked file carries a credential shape, and no roster or operator identity reaches a remote → 🔴 conforms, and carries a real risk this plan must handle deliberately. The `verify-api` probe in Chunk 01 captures a REAL response containing security names, tickers and quantities, and it is committed as a fixture. **The probe is run against the SANDBOX, never against a real connection** — that is what makes the capture canned data rather than the operator's holdings, and it is the whole safety argument, not a preference. `tests/connector/fixtures/accounts_get.json` is the precedent: sandbox ids and balances, committed as captured. `tests/preferences/check-no-personal-data.sh` is the backstop at push time, and it matches the roster's own tokens, so it would not catch a real holding from an institution the roster does not name — the sandbox rule is the one doing the work here"
  - artifact: operational-spec
    dispositions:
      - "No filesystem path is hardcoded → conforms"
      - "A backup destination is never created implicitly and never overwritten → inapplicable because this plan touches no backup path"
      - "🔴 `bankmachine store rebuild` after step 5 if any connection is not syncing (§ the operational note this plan discharges) → conforms and is the reason Chunk 04 exists. The spec already warns that a rebuild is owed once investments land; this plan makes the replay a tested property rather than an instruction the operator has to remember"
partition: serial, and examined per wave rather than once. Wave 1 (01–04): every chunk writes into `connector/plaid/derivers.py` and `cli/sync_run.py`, and 03 widens the same `query.py` domain filters that 01 and 02 create rows behind — two delegates collide in all three files on their first commit. Waves 2–3 (05–08) look parallelizable and are not: 05 and 07 both add a definition to the one `_tool_definitions()` function, 06 and 08 both touch the warning vocabulary and the instructions primer that a budget test holds, and every wave depends on the one before it for its fixtures. The wall-clock saving is smaller than the merge in all three cases
last_validated: 2026-09-12
---

# Build Plan: Investment Sync

## Problem

Build step 5 is half built. Balances land daily; **investments have never been pulled at
all.** The schema for them has existed since build step 1 — `securities`, `holdings`,
`investment_transactions` are created, constrained, and completely empty — and
`connections.capabilities` has recorded which connections could serve them since build
step 3.

Nothing reads that capability. There is no `/investments/*` call anywhere in the
connector, no deriver registered for one, and no `sync_state` row has ever carried
`domain = 'investments'`. An operator holding a 401k sees a brokerage account in
`list_accounts` with a balance, and nothing whatsoever about what is inside it.

Three things downstream are blocked on this and say so in their own artifacts:
`list_holdings` ("waits on build step 5"), `balance_history`'s investments half, and
AC-3.2/AC-3.3 themselves, which are the only two acceptance criteria in FR-3 with no
implementation.

## Requirements Confidence

**Level:** Medium

**Why:** The requirements are unusually well-pinned — AC-3.2 and AC-3.3 are explicit, the
schema is frozen and already expresses the hard decisions (quantity as text, valuations
rounded, append-only holdings), and the norms that govern the work are ratified and
tested. What is *not* pinned is the shape of the two API responses this plan is built
around. `api-notes-plaid.md` has thirteen measured sections and **none of them is about
investments**: no `/investments/holdings/get` or `/investments/transactions/get` response
has ever been observed by this project. Every field mapping below is therefore reasoned
from the schema and from the SDK, not from a payload.

**What would raise it:** Chunk 01's `verify-api` step, which is the first thing built and
costs one sandbox probe. It moves this to High or it rewrites Chunk 01's field mapping,
and either outcome is worth more than more reasoning.

**Both wave-1 probes are now done, and both rewrote what they measured** — §22-25 for
holdings, §26 for investment transactions. Wave 1 is **High** on response shape: every field
either of its derivers reads has been seen in a real payload. It stays Medium on one thing
only, and §26 is why — the *granted window* is unobservable on this endpoint, so AC-3.3 is
satisfied by recording what returned rather than by detecting a shortfall.

🔴 **Waves 2 and 3 (chunks 05–08) are Medium for a second, independent reason**, and it is
not the same unknown: their row shapes and warning sets are designed against a schema and two
discovery artifacts rather than against holdings rows, because none exist yet. What raises
*that* is wave 1 landing — which is why the checkpoint after Chunk 04 is a mandatory re-read
of chunks 05–08 rather than a formality. `balance_history`'s row shape is the one part of
waves 2–3 that is **not** Medium: it was derived and argued in
`discovery-mcp-tool-surface.md` and is quoted rather than re-invented here.

**Open assumptions:**

- `[ASSUMPTION: capabilities are read from the recorded column and never refreshed | MED
  impact | user can correct]` — `connections.capabilities` is written at enrollment, by
  `enroll.py`, and by nothing else. `derive_item` explicitly says so. A connection
  enrolled before its institution gained investments therefore never starts pulling them,
  and nothing tells the operator that. Conforming to AC-3.2 as written ("recorded
  capabilities") is what this plan does; the refresh is a separate piece of work and is filed
  as **#109** rather than folded in, because folding it in means calling `/item/get` per
  connection per run, which is a cost decision the owner should take on its own. 🔴 The id is
  written here because a "filed" claim with no id is a claim nobody can check — this one went
  unfiled for a whole wave and was caught by a review rather than by a reader.
- ~~`[ASSUMPTION: investment transactions request the same `history_days` window as
  transactions | LOW impact | user can override]`~~ — **measured 2026-09-12 (§26)**: asking
  for the configured maximum returned rows spanning the whole 730 days, so the window is
  requested and granted as one. What the probe also settled is that the grant is not
  *stated* anywhere, and the returned rows cannot stand in for it — see §26 and Chunk 02's
  descope of the shortfall warning.
- `[ASSUMPTION: a holding is captured once per day and the first capture of that day is
  the one kept | MED impact | user can override]` — this is `_write_balance`'s rule
  applied to the series the same norm names. The alternative (last capture wins) makes the
  series depend on what time of day the operator happened to run a sync, which is the
  thing the norm exists to prevent.
- `[DECISION: the investments write path stays on SQLAlchemy Core | taken while building
  Chunk 04 | closes the revisit backlog #43 booked for this moment]` — #43 exists so the
  Core-vs-ORM question is asked once, at investment-sync scoping, on the reasoning that
  "holdings, positions and tax lots are genuinely relational, genuinely mutable". Built, they
  are not: `_upsert_security`, `_write_holding` and `_write_investment_transaction` each
  converge ONE row on a natural key, and the only relationship traversed is
  `holdings.security_id`, resolved from a dict built once per response. Nothing loads an
  object graph, mutates it and flushes, which is what an ORM buys. Two mechanisms would have
  to go to adopt one: `schema.py` and `core_schema.py` are an independently-written pair
  compared column by column on every test run, which is what makes schema drift visible and
  which a declarative base collapses into one source; and `store/connection.py` is the sole
  constructor of every connection, enforced structurally, which a `Session` would sit above.
  🔴 Tax lots — #43's own strongest case — are not modelled in v1 at all: the feed SENDS them
  (3 of the 13 positions in the recorded capture carry lots) and this build drops every one,
  since `tax_lot` appears nowhere under `src/`. So the relational case #43 was written to test
  has not arrived in the schema, and the item's revisit condition is now "whenever lots are
  modelled" rather than "at step 5". No code changed; the decision is the deliverable.
- `[DECISION: the replayed window reconciliation runs at each window's closing page, not once
  over the finished tables | taken while building Chunk 04 | corrects this plan]` — the
  deliverable said "at the end of the replay" and that is unbuildable. Once every page is
  replayed, each surviving row carries the `raw_response_id` of the LAST page it appeared on, so
  the first window's reconciliation run over the finished tables finds every row that only
  arrived in a later window absent from it and retires all of them — a conclusion no run ever
  reached, and a store no sync ever produced. A removal is evidence about the rows that existed
  when its window closed, so the replay reproduces the SEQUENCE of window conclusions.
- `[DECISION: only a sync records a domain's measured history range | taken while building
  Chunk 04 | user can veto]` — `record_investment_transaction_window` now returns the range it
  measured and the sync command writes it, in the same transaction as the removals. A rebuild
  re-runs that reconciliation from the archive, and a replay that stamped `sync_state` would
  rewind a freshness claim to the archive's instant — the sync stamps `last_success_at` from the
  clock after the window concludes — so the content digest would refuse a rebuild that had
  reproduced every row correctly. Same rule as Chunk 03's move of `last_success_at` out of the
  derivers, and it matches how the transactions domain has always recorded its own range.
- `[DECISION: an investments failure degrades the investments DOMAIN, not the connection |
  taken in this plan, Chunk 03 | user can veto]` — `connections.status` is one column and
  means credential health. A `PRODUCT_NOT_READY` on the investments pull is not a
  statement about the login, and marking the connection degraded for it would send the
  operator to `connections reauth` for something reauth cannot fix. A credential error
  reached through the investments call still degrades the connection, because that one IS
  about the login.

## Status

- [x] Chunk 01: One capable connection's holdings, end to end
- [x] Chunk 02: Investment transactions, and the window that actually came back
- [x] Chunk 03: Investments fails on its own, and the health surface says so
- [x] Chunk 04: Rebuild, idempotency, and the properties that hold across both
- [ ] Chunk 05: `list_holdings` — positions, under one strict row shape
- [ ] Chunk 06: What a holdings answer must disclose about itself
- [ ] Chunk 07: `balance_history` — one series, read two ways
- [ ] Chunk 08: Net worth, and the two ways it can be quietly wrong

Context: Chunks 01-04 built 2026-09-12; wave 1 is code-complete and awaiting its `cumulative`
review and PR. Chunk 04 closed the one direction of AC-5.2 that no Chunk 02 test could see: a
rebuild replaying the archive through the derivers alone cleared every investment-transaction
soft delete, so the rebuilt store held rows the synced store had retired and reported success.
`connector/plaid/window.py` now reassembles each window from the `request_context` its pages
carry and re-runs the reconciliation 🔴 at the page that CLOSED that window — not once over the
finished tables, which would judge an early window against rows that only arrived in a later
one. `store.rebuild` gained one seam for it (`ReplayPass`). The measured history range moved out
of the store function and into the sync, because a replay must not restate a domain's progress.
Wave 1 was then verified against the real sandbox: VRF-017, VRF-018 and VRF-019 all drained on
2026-09-12 from one enrolled connection — positions, the investment-transaction window and its
recorded range, idempotency on a same-day re-run, and two clean `store rebuild`s reporting
identical content over 1167 replayed transactions. 🔴 **That pass found the one defect no test
here could see**: `quantity` is exact decimal TEXT, and `sync shell`'s account-number rule blanks
any run of eight digits, so every high-precision position rendered `0.****3644`. The cell renderer
now spares a value that is wholly digits-point-digits; `boundary-patterns.md` carries it as a
fourth boundary, and the premise it corrects — that every arithmetic quantity in this schema is an
integer — is the one investments invalidated. VRF-014 is NOT drainable from a session: the MCP
server outlives `/clear`, so the reachable one answers on whatever build the client launched
(measured `efd64ad` against HEAD `7985a3b`), and only relaunching the client moves it.
Wave 1's `cumulative` review is done (`rev-20260913T021056Z-81d120ad`): 0 blocking, 5 warning,
8 note — four warnings fixed, R-1 filed as #113 (a window archived but never reconciled leaves
`store rebuild` refusing permanently; the refusal now names that cause even though the divergence
is unfixed), R-9 accepted to #93. Three `verify-resolutions` rounds returned clean.
Next: wave 1's PR — blocked only by the operator-verification queue, where VRF-014 needs the
client relaunched and VRF-015/016 need production — then the mandatory re-read of chunks 05-08
against what was built.

## The Program

This plan covers all three things the owner asked for — investment sync, `list_holdings`,
and `balance_history` — in three waves, drawn in full here on the owner's decision of
2026-09-12.

| Wave | Chunks | What ships | Ships as |
|---|---|---|---|
| 1 | 01–04 | Investment sync: capability-gated pulls, derivation, per-domain sync state, health visibility | its own PR |
| 2 | 05–06 | `list_holdings` — current positions, one new tool, one new row shape | its own PR |
| 3 | 07–08 | `balance_history` — value over time per account and as net worth | its own PR |

🔴 **One branch, three PRs — not one PR at the end.** `project-preferences.md` sets the
merge strategy to merge commit precisely so a reused branch's merge-base stays correct, so
`feat/investment-sync` survives each merge and continues. Each wave's last chunk (04, 06,
08) therefore takes a `cumulative` review, which is that PR's gate. A single eight-chunk PR
would put a schema-adjacent sync, a new MCP tool and a net-worth aggregate under one review
and let one blocking finding in any of them stall the other two.

🔴 **The cost of drawing waves 2 and 3 now, stated because it is real and was raised
before the decision.** Their chunks are designed against the schema and the discovery
artifacts rather than against holdings rows, because none exist yet. Two things in
particular may move when wave 1's `verify-api` lands: whether cost basis is populated
enough to be worth a row field, and whether the `as_of_date` of a holdings capture reliably
tracks the balance capture of the same day. **The first checkpoint after Chunk 04 is where
chunks 05–08 get re-read against what was actually built**, and amending them there is the
plan working, not a planning failure.

Wave-crossing dependencies, so they are not rediscovered at a chunk boundary:

- Chunk 05 needs wave 1's rows to exist, and its fixtures come from the real captures
  Chunk 01 committed — not from hand-written ones.
- Chunk 06 adds warning kinds, which cascade across a **closed set** and must land in one
  commit (the surfaces are enumerated in that chunk).
- Chunk 08 owes the lifecycle ruling that Chunk 03 deliberately declines to settle: a total
  over holdings needs the same include-and-flag-with-the-magnitude treatment a total over
  balances has.

## Verification Strategy

Tests are the floor, and three things here are not testable from a fixture:

1. **The `verify-api` probe** (Chunk 01, step 0) is run against the live sandbox before
   any handler is written, and its captured shape goes into `api-notes-plaid.md` as a new
   measured section. Fakes are built after it, never before.
2. **Two institutions, not one.** 🔴 The capability gate cannot be proven against a single
   enrolled connection, and this project has already been burned by exactly that: every
   capability fixture was drawn from `ins_109508`, where investments happens to sit in
   `available_products`, so the right read and the wrong read agreed and the whole suite
   passed against a criterion that was inverted. `ins_109511` reports `products: ['investments',
   'transactions']` and is the discriminating case. Chunk 01 enrolls it in the sandbox and
   keeps both.
3. **The operator's own run.** After Chunk 04, `bankmachine sync run` against the sandbox
   with both connections, then `sync shell` to read `holdings` and
   `investment_transactions` directly, then `get_pipeline_health` through the MCP server to
   confirm the investments domain is reported and its staleness is legible. Queued as an
   operator-verification entry, because the real-money version of that run is the operator's
   and cannot be done here.
4. **The tools answered in a real client** (waves 2 and 3). A tool's output schema can be
   correct and its answer still unusable — whether a warning prompts the right next action,
   and whether a net-worth answer reads as the figure it is, are model-facing judgments no
   test makes. Chunks 06 and 08 each queue an operator-verification entry.
   🔴 **Know what that queue costs before adding to it:** VRF-011 has been raised three times
   and accepted three times because the reachable sandbox MCP server was never on the build
   under test. An entry added here inherits that blocker unless the server is relaunched on
   the merged build, so relaunching it is part of the wave's close, not an afterthought.

## Build Chunks

### Chunk 01: One capable connection's holdings, end to end

- **Description:** The thin vertical slice: capability gate → client call → raw archive →
  deriver → `securities` and `holdings` rows → `sync_state` investments row. One endpoint
  only; transactions wait for Chunk 02. This proves the whole path before it widens, and
  it is where the append rule and the valuation rounding are settled.
- **Depends on:** none
- **Artifacts consumed:** `data-model.md` § `securities`, `holdings`, `investment_transactions`;
  `api-notes-plaid.md` §13 (capabilities are four lists and none of them is the answer);
  `docs/system-requirements.md` AC-3.2
- **Deliverables:**
  - `INVESTMENTS_HOLDINGS_GET` in `src/bankmachine/connector/__init__.py`, beside the
    other endpoint constants
  - `investments_holdings_get` on `PlaidClient` in `src/bankmachine/connector/plaid/client.py`
  - a capability predicate reading `connections.capabilities`, used by `src/bankmachine/cli/sync_run.py`
    to decide whether the call is made at all — **capability, never institution** (AC-3.2)
  - `derive_investments_holdings` in `src/bankmachine/connector/plaid/derivers.py`,
    registered in `PLAID_DERIVERS`
  - the first-capture-of-the-day rule **factored out of** `_write_balance` and shared, not
    copied: same comparison of `(captured_at, raw_response_id)`, same refusal to replace a
    row that carries no `raw_response_id`
  - a `sync_state` row at `domain = 'investments'`, written on success
  - new `tests/connector/fixtures/investments_holdings_get.json` — redacted, captured from
    the real probe
  - new `tests/connector/test_investment_derivers.py`
- **Tests:** unit — security upsert on `source_security_id`; quantity kept as exact decimal
  text through a value a float would round; a valuation at four decimal places rounded
  half-even and logged; a security in a currency with no known minor-unit exponent refused
  **per row** with the rest of the account's positions kept; the append rule, including the
  earlier-archived-response case and the manual-row case. Integration — the capability gate
  across **two** institutions, asserted in both directions (capable pulls, non-capable does
  not), on whole-value comparison rather than substring, because `products` is a substring
  of `available_products`. Sandbox — the live probe, marked `sandbox`.
- **Acceptance criteria:** a sandbox sync against the investments-capable connection writes
  securities and holdings rows for one day; the non-capable connection makes no investments
  call; a second run the same day changes nothing.
- **Foreign API:** plaid-investments-holdings
- **Done when:**
  <!-- prawduct:allow prawduct/chunk-ref-missing -- the aggregator's own HTTP endpoint path, which a verify-api step is obliged to name; not a file this repo has or should have -->
  0. verify-api — probe the live sandbox for `/investments/holdings/get`, capture the actual
     response shape into `.prawduct/artifacts/api-notes-plaid.md` as a new measured section,
     and confirm which fields are nullable in practice. Fakes and fixtures are written after
     this, from it.
  1. Acceptance criteria met and tests pass
  2. `/prawduct:critic` run and blocking findings resolved
  3. Committed and chunk marked `[x]` in Status

### Chunk 02: Investment transactions, and the window that actually came back

- **Description:** The second endpoint, which unlike holdings is paginated and windowed —
  and therefore the chunk that owes AC-3.3. Requests the full configured window and
  **records the range that actually returned**, which is what AC-3.3 asks for in those words.
  🔴 Amended after this chunk's `verify-api` (`api-notes-plaid.md` §26): the returned range
  is recorded but **no shortfall is inferred from it**, because the endpoint cannot tell a
  short window from a quiet account.
- **Depends on:** Chunk 01
- **Artifacts consumed:** `docs/system-requirements.md` AC-3.3, AC-2.4;
  `data-model.md` § `sync_state`
- **Deliverables:**
  - `INVESTMENTS_TRANSACTIONS_GET` and its client method, paged to exhaustion by
    `options.offset`/`options.count` against the stated `total_investment_transactions`,
    under the same page ceiling. 🔴 **There is no cursor** (§26), so the far-end idempotence
    `TRANSACTIONS_SYNC` documents is not available: restart discipline here is that a run
    which stopped early re-reads the window from offset 0 next time, which converges because
    the writes are upserts, and that the SHORTFALL of a cursor is recorded as the reason
    rather than papered over
  - the request window derived from `config.history_days`, which is the one configured
    window
  - `derive_investment_transactions` writing `investment_transactions` with the identity and
    provenance `transactions` already carries, reusing `_exact_quantity` for the 17-digit
    quantities §26 measured. 🔴 `settlement_date` is written as **null and stays null** — the
    feed has no such field (§26), so the frozen column keeps its meaning for the manual
    importer, which is now its only possible writer
  - `removed_at` driven by **reconciling the returned window against stored rows**, guarded
    on exhaustion: a stored row whose `trade_date` lies inside the requested window and whose
    `source_investment_transaction_id` did not come back is soft-deleted; a run that did NOT
    exhaust the window (page ceiling, transport failure, crash) marks **nothing**.
    `[DECISION: window reconciliation is the removal signal, guarded on exhaustion | taken
    2026-09-12 after §26 measured that the feed sends none | user chose it over
    `cancel_transaction_id`, over both, and over descoping removal]`. The guard failing OPEN
    is silent data loss, so the partial-run case is a test before it is a branch
  - the returned range recorded on the investments `sync_state` row
    (`history_start_date`), computed from the rows because §26 measured that nothing states
    it — and computable only at exhaustion, since rows arrive newest-first. A window that was
    never exhausted **writes nothing**, leaving the column as it was: null there means nobody
    has ever measured, which is UNMEASURED and not a shortfall of zero, and overwriting a
    range an earlier run did measure would destroy a fact to record the absence of one
  - 🔴 **descoped here, with the measurement as the reason:** no shortfall warning is derived
    from the returned range. §26 measured that this endpoint cannot distinguish a window the
    aggregator truncated from an account that simply had no trades, so `_is_short`'s shape is
    deliberately NOT reused for it — doing so would report every quiet brokerage as a
    truncated history on every run. A cancellation's effect on the ledger
    (`cancel_transaction_id`, null on 100/100 sandbox rows) is filed as **#110** rather than guessed
- **Tests:** unit — an investment transaction's amount signed from the operator's point of
  view; a quantity kept as exact text through 17 digits; a price rounded half-even where §26
  found sub-cent prices; `trade_date` as a calendar date against `captured_at` as an instant.
  Integration — pagination to exhaustion by offset; AC-2.4, a second consecutive run produces
  zero net changes; a vanished row soft-deleted once the window is exhausted, and 🔴 **the
  partial-run case: a run stopped at the page ceiling soft-deletes NOTHING and records a null
  range** — the two assertions that make the guard fail closed rather than open.
- **Acceptance criteria:** a sandbox sync writes investment transactions for the capable
  connection; the recorded range matches what the responses actually carried; running twice
  changes nothing; a run that did not exhaust the window leaves every stored row live.
- **Foreign API:** plaid-investments-transactions
- **Done when:**
  <!-- prawduct:allow prawduct/chunk-ref-missing -- as in Chunk 01: the aggregator's endpoint path, named because probing it IS the step; not a file reference -->
  0. ~~verify-api~~ **Done 2026-09-12 — `api-notes-plaid.md` §26.** Offset/count against a
     stated total, no cursor; the range is not stated and must be computed from the rows,
     which arrive newest-first so it is knowable only at exhaustion; and three things this
     chunk was written against are absent from the body — `settlement_date`, any removal
     signal, and a cursor. The deliverables above are amended accordingly
  1. Acceptance criteria met and tests pass
  2. `/prawduct:critic` run and blocking findings resolved
  3. Committed and chunk marked `[x]` in Status

### Chunk 03: Investments fails on its own, and the health surface says so

- **Description:** 🔴 The chunk that keeps this feature from becoming a new way to be
  silently stale. Two domains now advance independently, and today **every read of
  `sync_state` pins `domain == TRANSACTIONS_DOMAIN`** — correct, deliberate, and blind the
  moment a second domain exists. This chunk makes an investments failure isolated,
  recorded, and reported.
- **Depends on:** Chunk 02
- **Artifacts consumed:** `docs/system-requirements.md` AC-4.1, AC-4.2, AC-4.4, AC-4.5;
  `api-contract.md` § Direction (incompleteness rides the success path as a warning)
- **Deliverables:**
  - per-domain error recording in `src/bankmachine/cli/sync_run.py`: an investments failure
    writes `last_error_code` / `last_error_at` on the investments `sync_state` row and
    leaves the transactions domain's success intact, and vice versa
  - 🔴 **the carried failure recorded even when the page loop returns early.** Chunk 01
    carries an investments failure past the transactions pages and degrades the connection
    afterwards — but a connection still materializing its history returns from inside that
    loop, before the degrade lands, so on a first sync the failure is logged and reaches no
    column at all. Recording against the domain rather than the connection is what closes
    it, which is why it is this chunk's rather than a patch on the one before
  - the decision above made structural — a credential error reached through an investments
    call still degrades the connection; a product error does not
  - the three `domain == TRANSACTIONS_DOMAIN` reads in `src/bankmachine/query.py` widened
    from "pin one domain" to "report per domain", **without multiplying any count** — each
    of those three sites carries a comment saying an unfiltered join returns a row per
    domain, and this is the change those comments were written for
  - `get_pipeline_health` reporting investments freshness per connection, including the
    AC-4.5 case: a domain that has **never** succeeded is distinguishable from one that has
    gone stale, because the size of the hole must be computable rather than guessed
  - the lifecycle question recorded against wave 2 (see § The Program) rather than settled
- **Tests:** integration — a connection fresh for transactions and stale for investments
  reports both, and the staleness is legible; a connection that has never pulled investments
  is reported as never, not as zero; counts in `_coverage` and the health rows do **not**
  double after the second domain lands (the regression the comments predict); AC-4.1, one
  connection's investments failure does not stop another connection's sync.
- **Acceptance criteria:** `get_pipeline_health` shows the investments domain for every
  capable connection; a forced investments failure appears there with its code and its last
  success, and the transactions domain beside it is untouched.
- **Done when:**
  1. Acceptance criteria met and tests pass
  2. `/prawduct:critic` run and blocking findings resolved
  3. Committed and chunk marked `[x]` in Status

### Chunk 04: Rebuild, idempotency, and the properties that hold across both

- **Description:** Closes the guarantees that span the whole feature rather than any one
  endpoint: AC-5.2 (normalized tables rebuildable from raw responses alone) and AC-2.4
  (idempotency) now have to hold with investments in them. `operational-spec.md` already
  tells the operator to rebuild once step 5 lands; this chunk makes that a tested property
  instead of an instruction to remember.
- **Depends on:** Chunk 03
- **Artifacts consumed:** `docs/system-requirements.md` AC-5.2, AC-2.4;
  `project-preferences.md` § Testing (hypothesis property tests for money arithmetic,
  idempotency and rebuild losslessness)
- **Deliverables:**
  - `src/bankmachine/store/rebuild.py` replaying both new endpoints, with the holdings
    append rule landing on the same rows regardless of replay order
  - 🔴 **the investment-transaction window reconciliation re-run DURING the replay, at the
    page that closed each window** (corrected from "at the end of the replay" while building;
    see the decision below), which Chunk 02 created the need for and could not discharge. Its removal signal is a
    row's ABSENCE from a complete window, and a deriver sees one page — so replaying pages
    re-upserts every row that ever appeared and CLEARS `removed_at` on each, silently
    resurrecting every soft delete. Without this, AC-5.2 is false in the one direction no
    test in Chunk 02 can see: the rebuilt store holds rows the synced store had retired.
    `store.investments.record_investment_transaction_window` is already factored for a
    second caller; what this chunk owes is deciding which archived pages constituted one
    complete window (the `request_context` Chunk 02 archives carries
    `window=…..… offset=… count=…` for exactly this) and a test asserting a rebuild
    reproduces a soft delete rather than undoing it
  - property tests: rebuild losslessness over generated holdings and investment
    transactions; money arithmetic over valuations at the rounding boundary
    — 🔴 **generated for the window sequences and the rounding boundary; pinned by
    named cases for holdings, deliberately.** A holdings capture's whole ordering
    space is two-valued (the day's first capture is the one kept, and a replay
    either meets it first or does not), so generating sequences over it ranges
    over the same two orderings the two named cases already state, and states them
    less legibly. If a later chunk gives holdings a second within-day rule, that
    argument stops holding and the generator is owed.
  - an entry in `.prawduct/operator-verification.md` for the real-connection run, which is
    the operator's and cannot be done here
  - `docs/system-requirements.md` and `api-contract.md` updated to say build step 5 is
    built — and, in the same edit, that `list_holdings` now waits on **wave 2** rather than
    on step 5, because that sentence becomes wrong the moment this plan lands
- **Tests:** property — rebuild from an emptied normalized store reproduces every holding
  and investment transaction exactly; a second sync after a rebuild is a no-op. Integration —
  a rebuild across a store holding both manual and aggregator rows leaves the manual ones
  alone; 🔴 **a rebuild of a store holding a soft-deleted investment transaction reproduces
  the `removed_at` rather than clearing it** — the assertion that catches the resurrection,
  which every other rebuild test passes straight through.
- **Acceptance criteria:** `bankmachine store rebuild` on a sandbox store with investments
  reproduces the tables byte-for-byte by content; the suite is green; the verification entry
  is queued.
- **Critic mode:** cumulative
  <!-- Wave 1's last chunk. This branch ships three PRs, so this review is wave 1's PR
       gate rather than the plan's final one — the plan's `cumulative-final` is Chunk 08. -->
- **Visual change:** yes — `get_pipeline_health`'s output gains a domain dimension, and
  whether that reads clearly to an agent is not something a test can speak to
- **Done when:**
  1. Acceptance criteria met and tests pass
  2. Committed, then `/prawduct:critic cumulative` run and blocking findings resolved
  3. Chunk marked `[x]` in Status
  4. **Wave 1 PR opened and merged**, then chunks 05–08 re-read against what was built —
     the checkpoint below says what to look for

### Chunk 05: `list_holdings` — positions, under one strict row shape

- **Description:** Wave 2 opens. The first tool that answers what is *inside* an investment
  account. One row per position, one strict schema, no optional fields — the tool-boundary
  norm's first guardrail is checked at registration by `mcp._refuse_optional_row_fields`, so
  a field that is present only when the aggregator supplied it fails at startup rather than
  in review.
- **Depends on:** Chunk 04
- **Artifacts consumed:** `api-contract.md` § Direction (tool boundary; read-only surface),
  `discovery-mcp-tool-surface.md` (position row, admitted alone)
- **Deliverables:**
  - a holdings query in `src/bankmachine/query.py`, reading the **latest captured
    `as_of_date` per account** rather than today's — a position is what it was on the day it
    was captured, and asking for today's row returns nothing on any day the sync has not run
  - the tool definition in `src/bankmachine/mcp.py`, registered through `_tool_definitions()`
    so both registration guards see it
  - 🔴 **cost basis is nullable, never absent** — the contract says "with cost basis where
    available", and under the strict-row guardrail that means a present-and-null field. A
    field dropped when the aggregator did not supply it is the exact shape the guardrail
    refuses, and it would also make the absence unreadable to a consumer
  - the row carries its security's identity from `securities` rather than restating it, so a
    position and an investment transaction in the same instrument agree about what it is
  - 🔴 **what the server says it cannot answer moves with the tool, and only half of that is
    forced.** Registering `list_holdings` fails `tests/test_mcp_resources.py` until the name
    leaves `mcp_resources.UNBUILT_TOOLS`; nothing forces the rest. The primer in
    `mcp._instructions()` names "holdings or positions" as unanswerable in typed prose — drop
    it there, keeping `INSTRUCTIONS_BUDGET` — and check the investments bullet in
    `mcp_resources._CANNOT_ANSWER`, which was written ahead for the finished surface, against
    what shipped, deleting its transitional comment
- **Tests:** unit — the strict-row guard sees the new definition (a deliberately optional
  field is a startup failure); a position whose cost basis is unknown comes back null rather
  than missing; quantity round-trips as exact decimal text with no float anywhere on the
  path. Integration — the tool answers against a store built by wave 1's own sync, with
  fixtures taken from Chunk 01's real captures rather than hand-written.
- **Acceptance criteria:** `list_holdings` returns every position in the sandbox
  investments account with quantity, market value and currency; a store with no holdings
  answers empty rather than erroring.
- **Exposed API:** bankmachine-mcp
  <!-- Already-recorded decisions: every tool is `experimental` (§ Surface Inventory) and the
       error model is the envelope's — incompleteness rides the success path as a warning.
       No new decision is owed; the annotation is here so the check can see that. -->
- **Done when:**
  1. Acceptance criteria met and tests pass
  2. `/prawduct:critic` run and blocking findings resolved
  3. Committed and chunk marked `[x]` in Status

### Chunk 06: What a holdings answer must disclose about itself

- **Description:** 🔴 The chunk that keeps `list_holdings` from being confidently wrong. A
  position carries three ways of being stale or partial that a well-formed answer hides, and
  each needs to ride the **success path as a warning** rather than be smoothed away: the
  capture is older than today, a security could not be denominated, or the account is closed.
- **Depends on:** Chunk 05
- **Artifacts consumed:** `api-contract.md` § Direction (freshness stamp and warnings;
  balance lifecycle), `.prawduct/artifacts/api-contract.md` § warning vocabulary
- **Deliverables:**
  - a request-scoped warning kind for **a holdings capture older than the answer's own
    freshness stamp** — a 401k priced eleven days ago is not a fact about today, and nothing
    in the row says so
  - `rule-applied` carrying any security excluded from a minor-units figure because its
    currency has no known exponent — the per-row refusal wave 1 built, made visible here
  - `account_no_longer_active` reaching holdings, not only balances: the existing kind
    already means what is needed, so this is an emitter, not a new kind
  - 🔴 **say on the coverage surface that its measurements are the TRANSACTIONS domain's.**
    `query._account_coverage` is pinned to that domain, so a brokerage account whose only
    activity is investment transactions reports `transaction_count` 0 and draws
    `accounts_without_coverage` on the night its trades were pulled. Wave 1 descoped the fix
    (#107) and recorded it at the site in `query.py`, which is the only place it is written —
    defensible while every field on that row is transaction-named and no tool can answer about
    positions, and indefensible the moment `list_holdings` ships. So either the
    `get_coverage_report` field table and `accounts_without_coverage`'s guidance say which
    domain they speak for, or #107 is resolved here; what is not available any more is
    silence
  - 🔴 **the new kind landed across every surface in ONE commit.** A warning kind is a shared
    closed set, and the surfaces are: `envelope.REQUEST_SCOPED_KINDS`; the guidance map in
    `src/bankmachine/mcp_resources.py` (a kind with no section fails
    `tests/test_mcp_resources.py`); the warning `enum` in `src/bankmachine/mcp.py`, which
    derives from the tuple and needs no edit; the server instructions in `_instructions()`;
    and the vocabulary table in `.prawduct/artifacts/api-contract.md`. **`INSTRUCTIONS_BUDGET`
    is 1800 and a test holds the primer to it** — expect to trim prose to fit rather than to
    discover the ceiling at chunk close
- **Tests:** unit — the kind is in the request-scoped tuple and not the connection-scoped
  one, and its absence is information (a fresh capture raises nothing); the guidance map
  covers every kind in the vocabulary. Integration — a store whose last holdings capture is
  old answers with the warning and still returns the rows; an unpriceable security is named
  under `rule-applied` and excluded from the total rather than silently rounded.
- **Acceptance criteria:** every degraded holdings answer says so on the success path; no
  answer raises an exception for incompleteness; the primer is within budget.
- **Critic mode:** cumulative
  <!-- Wave 2's last chunk, and its PR gate. -->
- **Visual change:** yes — the warning text is read by a model, and whether it prompts the
  right next action is not a test's judgment
- **Done when:**
  1. Acceptance criteria met and tests pass
  2. Committed, then `/prawduct:critic cumulative` run and blocking findings resolved
  3. Chunk marked `[x]` in Status, and an operator-verification entry queued for the warning
     text in a real client
  4. **Wave 2 PR opened and merged**

### Chunk 07: `balance_history` — one series, read two ways

- **Description:** Wave 3 opens. The time series, which under the 2026-09-09 tool merge
  absorbed `net_worth` — so per-account history and net-worth-over-time are **one tool with
  one row shape**, not two tools. The shape was derived in discovery and is not open here:
  `{date, account_id, assets_minor, liabilities_minor, net_minor, currency}`, with
  `account_id` **null meaning the aggregate row**. The operator-signed convention is what
  makes the split derivable at both levels, and `net = assets - liabilities` holds
  identically at both.
- **Depends on:** Chunk 06
- **Artifacts consumed:** `discovery-mcp-tool-surface.md` (the unifying row shape and why the
  merge was admitted), `api-contract.md` § Direction (tool boundary)
- **Deliverables:**
  - the series query over `balances_daily` in `src/bankmachine/query.py`, keyed
    `(account_id, as_of_date)` so both readings come from one series rather than two queries
    that can disagree
  - the tool in `src/bankmachine/mcp.py`, with the aggregate row distinguished by a null
    `account_id` and every field present at both levels
  - 🔴 **a decision on the name, recorded rather than defaulted.** Discovery left this open
    deliberately and said why: under the merge, "what is my net worth over time" is reached
    as `balance_history`, which is *worse at selection* than `net_worth` was. The shape rule
    fixed where the split goes and says nothing about the name. Name it, record the choice
    with its reason, and if the answer is to keep `balance_history` then say that the
    selection cost was accepted and what buys it back (the tool description is the lever)
  - a gap in the series reported rather than interpolated — a day with no capture is a day
    nobody looked, and drawing a line through it invents a balance
  - the same unforced half as for `list_holdings`: drop "balance history or net worth over
    time" from the primer in `mcp._instructions()` and the balance-history bullet from
    `mcp_resources._CANNOT_ANSWER` — the test only moves `UNBUILT_TOOLS`
- **Tests:** unit — `net = assets - liabilities` at both levels over generated series; a
  single account's degenerate split (a balance is either an asset or a liability, never
  both); the strict-row guard sees the definition. Integration — the aggregate row and the
  sum of the per-account rows for the same day agree.
- **Acceptance criteria:** the tool returns a per-account series and an aggregate series from
  one query; a day with no capture is visible as absent rather than smoothed.
- **Exposed API:** bankmachine-mcp
- **Done when:**
  1. Acceptance criteria met and tests pass
  2. `/prawduct:critic` run and blocking findings resolved
  3. Committed and chunk marked `[x]` in Status

### Chunk 08: Net worth, and the two ways it can be quietly wrong

- **Description:** The last chunk, and the one that pays the norm debt. A net-worth total is
  the most believable wrong number this product can emit, and there are exactly two ways it
  goes wrong silently. Both are closed here.
- **Depends on:** Chunk 07
- **Artifacts consumed:** `api-contract.md` § Direction (balance lifecycle — include and
  flag, with the magnitude), `docs/system-requirements.md` AC-12.8
- **Deliverables:**
  - 🔴 **the double-count guard, and it is the finding this plan most wants on the record.**
    The contract says the series has "investments included" — and an investment account's
    value is **already in `balances_daily`**, because `/accounts/get` reports a brokerage
    account's `current` balance like any other account's (wave 1 measured a sandbox 401k at
    23631.9805 there). `holdings` **decomposes** that balance; it does not add to it. Summing
    both counts the same money twice. This project has already shipped an exactly-2× total
    once — `money_summary` returned `outflow_minor_units: 2229892` against a true `1114946` —
    and nothing in the payload named it. So net worth reads the balance series, holdings
    answer *what is inside* a position, and a test asserts the two are never summed
  - the lifecycle treatment the norm requires: non-active accounts stay in the figure, and
    the answer states **how many they were and what they contributed** — a count and a signed
    sum, present-and-zero rather than absent. The magnitude is the load-bearing half: a
    warning without the figure tells a consumer something is wrong and leaves them unable to
    do anything about it
  - the same treatment extended to a holdings total, which is the ruling Chunk 03 declined to
    settle and this chunk owes
  - `docs/system-requirements.md` and `api-contract.md` amended: the three-tools-not-built
    note drops to one (`find_recurring`), and the § Surface Inventory rows stay as they are
    because both tools were always specified
- **Tests:** integration — a store holding a closed brokerage account reports the total, the
  count and the contributed magnitude, and the guard is seen red **with the magnitude
  removed**, not merely with the flag flipped (that is the condition the norm set for its own
  steady state). Property — net worth over a generated store equals the signed sum of the
  latest balance per account and is invariant to how many holdings rows decompose it.
- **Acceptance criteria:** a net-worth answer over a store with a closed account and an
  investment account is correct, states its lifecycle treatment with the magnitude, and does
  not double-count the investment account.
- **Type:** cumulative-final
  <!-- The plan's last chunk: its review IS the one `/prawduct:critic cumulative` for wave 3.
       Commit first, run once, no separate `final`. -->
- **Visual change:** yes — a net-worth answer is the one an operator will read most literally
- **Done when:**
  1. Acceptance criteria met and tests pass
  2. Committed, then `/prawduct:critic cumulative` run and blocking findings resolved
  3. Chunk marked `[x]` in Status, and the plan archived once the release carries it

## Early Feedback Milestone

**Milestone chunk:** 01
**What the user can do:** run a sandbox sync and read their own positions out of `holdings`
through `sync shell` — the first time this product has ever recorded what is *inside* an
investment account rather than only what it is worth.

## Governance Checkpoints

**Commit & PR cadence:** commit per chunk after its Critic review passes. **Three PRs off one
branch**, at chunks 04, 06 and 08 — each wave's last chunk takes a `cumulative` review, which
is that PR's gate. The branch is reused across merges, which the merge-commit strategy
supports and which squash would break.

- **After Chunk 01** — architecture validation, and the one that matters most here: did the
  real API shape match the schema the plan was written against, and is the shared
  append-rule factoring clean enough that Chunk 02 can build on it rather than around it?
- **After Chunk 03** — coherence of the health surface with a second domain in it. This is
  where a double-counted row or a missing "never succeeded" case would hide.
- **After Chunk 04, before Chunk 05** — 🔴 **the trajectory checkpoint this plan's shape
  makes mandatory.** Chunks 05–08 were drawn against the schema, not against rows. Re-read
  them against what wave 1 actually built and amend what moved: whether cost basis arrives
  populated often enough to be worth a row field; whether a holdings `as_of_date` tracks the
  balance capture of the same day (Chunk 08's double-count argument leans on it); and whether
  the real response carried a field the row shape should expose. Amending here is the plan
  working.
- **After Chunk 06** — the warning surfaces: is every kind covered in the guidance map, and
  did the primer stay under budget without losing the sentence that made a kind actionable?
- **Chunk 08 (cumulative)** — full-bundle review across all three waves, with particular
  attention to two things: whether any artifact still claims investments or the two tools are
  unbuilt, and whether the double-count guard actually holds a test that fails when the guard
  is removed.
