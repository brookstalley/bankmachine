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
      - "A valuation is rounded to a minor unit this build KNOWS, else refused per ROW (amendment 2026-09-10) → conforms and is exercised for the first time by a table other than balances. `securities.close_price_minor`, `holdings.market_value_minor` and `holdings.cost_basis_minor` all pass through `store/types.py::minor_digits`; a security denominated in an `unofficial_currency_code` this build has no exponent for is refused ROW-WISE, keeping the rest of the account's positions. 🔴 The norm's other half -- the refusal NAMED under `rule-applied` -- was unbuilt for holdings through wave 1: a refused position reached a log line and nothing a read can see. Chunk 06 records it (migration 011, `refused_holdings`) so `list_holdings` can name it"
      - "All monetary values are integer minor units, no floats → conforms. 🔴 And the counterpart the schema already draws is the one to hold onto: a QUANTITY is not money. `holdings.quantity` and `investment_transactions.quantity` are exact decimal TEXT, parsed to `Decimal` and never through a float, because fractional shares are routine and a scaled integer's scale would be a guess"
      - "Calendar dates and UTC instants are distinct and never mix → conforms. `as_of_date`, `trade_date`, `settlement_date` and `close_price_as_of` are calendar dates; `captured_at`, `first_seen_at`, `updated_at` are UTC instants"
      - "Every silver row carries exclusive provenance and its derivation version → conforms. Every row this plan writes is `source='aggregator'` with a `raw_response_id` and no `manual_import_id`, which the tables' own CHECK constraints already enforce. The derivation version is bumped when the derivation changes, per the norm"
      - "🔴 The daily balance and HOLDINGS series are append-only → this plan is the first code that writes the holdings half, so the norm stops being partly hypothetical here. The composite PK is the structural half; the behavioural half is the rule `_write_balance` already implements and holdings must reuse rather than reimplement — FIRST capture of the day wins, decided by COMPARING captures rather than by arrival order, and a row with no `raw_response_id` (a manual import) is never replaced. Copying that logic into a second deriver is how the two drift; the plan factors it (Chunk 01)"
      - "A source value is never overwritten in place → conforms; this plan adds no override column and writes no interpretation over a source field"
      - "A transaction is never hard-deleted; removal is a soft delete → conforms in SHAPE, and by a different mechanism than `transactions`. `investment_transactions.removed_at` carries the same column and the same never-a-DELETE rule, but 🔴 the feed sends **no removal signal of any kind** — measured, `api-notes-plaid.md` §26: `/investments/transactions/get` is a windowed read, not a delta, so there is no `removed` array to read and `cancel_transaction_id` is a cancellation reference rather than a tombstone. What makes the norm satisfiable anyway is that the window comes back WHOLE: a stored row inside the requested window whose id did not return has gone away, and that is the removal signal. It is only trustworthy when the run actually exhausted the window, so the reconciliation is guarded on exhaustion and a partial run marks nothing — an unguarded one would soft-delete every row it merely had not reached yet"
      - "A migration's DDL is frozen once written → conforms. Wave 1 added no migration: `securities`, `holdings` and `investment_transactions` were created in `core_schema.py` at build step 1 and are untouched. The one column the real API shape turned out to need, `holdings.price_as_of`, is migration 010 in its own module -- a new migration and a recorded decision (the trajectory checkpoint's, built in Chunk 05), never an edit to the frozen DDL. Migration 011 (`refused_holdings`, Chunk 06, owner's decision 2026-09-13) is the second, in its own module on the same rule"
  - artifact: architecture
    dispositions:
      - "Every writable handle comes from the one writer factory, which takes the exclusive lock before it returns → conforms; the investments pull persists through the same `_persist` path every other endpoint uses and opens no handle of its own"
      - "Read-role handles are `mode=ro`, hold no snapshot beyond the statement, never fall back → conforms. The health-surface work in Chunk 03 is read-only and goes through the existing reader"
      - "No component creates the datastore implicitly → conforms; nothing here opens a datastore path"
      - "A process that does not recognize the schema version refuses to serve → conforms; migrations 010 and 011 move the served schema version to 11, and a store behind it is refused until `bankmachine store init` migrates it"
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
partition: serial, and examined per wave rather than once. Wave 1 (01–04): every chunk writes into `connector/plaid/derivers.py` and `cli/sync_run.py`, and 03 widens the same `query.py` domain filters that 01 and 02 create rows behind — two delegates collide in all three files on their first commit. Waves 2–3 (05–08) look parallelizable and are not: 05 and 07 both add a definition to the one `_tool_definitions()` function, 06 and 08 both touch the warning vocabulary and the instructions primer that a budget test holds, and every wave depends on the one before it for its fixtures. The wall-clock saving is smaller than the merge in all three cases. Chunks 09–10: serial — 10 edits `query.py`, whose shape 09 changes, and both re-point cases in `verify_norms_go_red.py`, which must run alone
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
- [x] Chunk 05: `list_holdings` — positions, under one strict row shape
- [x] Chunk 06: What a holdings answer must disclose about itself
- [x] Chunk 07: `balance_history` — one series, read two ways
- [x] Chunk 08: Net worth, and the two ways it can be quietly wrong
- [x] Chunk 09: Holdings and the balance series leave `query.py` (a pure move, #115)
- [ ] Chunk 10: An investment account's activity counts as coverage (#107)

Context: Wave 1 (chunks 01-04) merged to `develop` as PR #114 on 2026-09-13; the branch
continues. VRF-020/021 (production-only) are re-raised and pending, and with VRF-022 (the holdings
warnings read in a real client) they block the wave 2 PR. Chunk 05 is committed (`fd47661`).
Chunk 06 is committed as `eec7af8` plus `f59ec2c`, which settles its cumulative review. Changes:
- `positions_not_current`, a new request-scoped kind: an old or unknown price, an account a newer
  capture of its connection listed nothing for, or a stopped investments feed.
- `rule-applied` naming each position refused for its unit. The refusal is recorded by migration
  011's `refused_holdings` (`DERIVATION_VERSION` 10), and a day's first capture decides a key both
  tables hold.
- `account_no_longer_active` extended to positions.
- The coverage surface now names the transactions feed. #107 stays open as a design question.

The review raised 0 blocking and 4 warnings, all fixed, and the verify-resolutions pass
found nothing further. Verified on the sandbox store at schema 11
after rebuild.

Chunk 07 is committed as `23d0245`: `balance_history` on the owner's four decisions, with the three
refinements recorded under its deliverables, plus the carried `list_holdings` fix. Its review
(`rev-20260913T201311Z-35ac5f2c`) raised 0 blocking, 3 warnings and 4 notes. R-1 to R-5 are carried
into Chunk 08's deliverables, R-6 is fixed in a doc-only commit, and R-7 is informational. Verified
on the sandbox store. The gate passed; all 198 norm breaks are caught.
Chunk 08 is built on the owner's two decisions of 2026-09-13, recorded under its deliverables:
- A non-active account stops counting in net worth after its last capture, and the answer names
  what stopped counting, with the figure.
- `list_holdings` carries a `totals` block.

It also carries R-1 to R-5, the double-count guard and the magnitude go-red cases, and moves
`DERIVATION_VERSION` to 11. Verified on the sandbox store rebuilt at 11: net worth is unchanged at
-7716415 on four complete days, the 14 relinked accounts are named with that sum, and holdings
total 2544640 over 13 positions. The go-red harness caught 210 of 211. The survivor was the
version-bump case, which went blind because its fixture sat two versions back; it is retargeted at a
rebuild test for this bump.

Chunk 08 is committed as `30a28cf` plus `d1d9a96`. Its cumulative review
(`rev-20260913T213037Z-b39388c0`, waves 2 and 3 together) raised 1 blocking, 3 warnings and 5 notes:

- **R-6 (blocking):** a record-lint false positive.
- **R-1:** a midnight-UTC false warning in `positions_not_current`. Its fix changed R-3's rule to
  "the last attempt archived no holdings reply", recorded as a decision above.
- **R-3, R-4, R-5/R-7:** fixed in `d1d9a96`.
- **R-2 and R-8:** filed as #115 and #116.

The `verify-resolutions` pass (`rev-20260913T215727Z-da2ca441`) found nothing further. The gate passed
at 1583, and the PR coverage gate is satisfied. All eight chunks are done. The PR (one for waves 2
and 3, or two) is the owner's call. It blocks on VRF-020/021 (production-only), VRF-022 and VRF-023.
🔴 A wave 2 PR opened before wave 3 lands is cut from `1320344`.

**Reopened 2026-09-13 with chunks 09 and 10, on the owner's decision.** Waves 2 and 3 ship as ONE
PR, and #115 and #107 land on this branch before it opens. #115 goes here because `list_holdings`
and `balance_history` have never been on `develop`: moved now, the PR shows them arriving in their
own modules, and nobody reviews a 900-line move. #107 goes here because it edits the coverage code
beside that move, and the VRF-022/023 client session can read its answer in the same sitting. The
PR still blocks on VRF-020/021, VRF-022 and VRF-023, and chunk 10 adds its own client reading.

## The Program

This plan covers all three things the owner asked for — investment sync, `list_holdings`,
and `balance_history` — in three waves, drawn in full here on the owner's decision of
2026-09-12.

| Wave | Chunks | What ships | Ships as |
|---|---|---|---|
| 1 | 01–04 | Investment sync: capability-gated pulls, derivation, per-domain sync state, health visibility | its own PR |
| 2 | 05–06 | `list_holdings` — current positions, one new tool, one new row shape | its own PR |
| 3 | 07–08 | `balance_history` — value over time per account and as net worth | its own PR |
| — | 09–10 | #115's move out of `query.py`, and #107's investment coverage | with waves 2 and 3 |

🔴 **Amended 2026-09-13 (owner): waves 2 and 3 ship as one PR, and chunks 09–10 ride in it.** The
three-PR shape below held for wave 1 (PR #114). Wave 3 was committed before wave 2's PR opened, so
a separate wave 2 PR would now be cut from an old commit for no review benefit. Chunk 10 closes
with a `final` review over 09 and 10. That review composes with Chunk 08's cumulative to cover the
PR, and the PR gate is re-run to confirm it rather than assumed.

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
  `discovery-mcp-tool-surface.md` (position row, admitted alone), `api-notes-plaid.md` §22-23
- **Deliverables:**
  - 🔴 **the price's own date, stored.** *(Added at the trajectory checkpoint, owner's decision
    2026-09-13.)* `institution_price_as_of` is the only date the aggregator puts on a position
    — `2021-05-25` on every sandbox position — and wave 1 drops it, while `as_of_date` is always
    the sync day. A new migration adds nullable `holdings.price_as_of` (a calendar date); the
    DDL goes in its own module, never into `core_schema`. `DERIVATION_VERSION` moves so
    `store rebuild` fills it from the archived bodies, and its guard pins the historical version
    as a literal. **Null means "this row predates the column and has not been rebuilt", or that
    the aggregator sent no price date** — never "priced today", so the read path does not coalesce
    it. The append rule is unchanged: the first capture of the day keeps its row
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
  - the row carries `price_as_of` present-and-nullable, beside `as_of_date`, so a consumer can
    see a position captured today at a price years old
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
  path; a rebuilt store fills `price_as_of` from the recorded capture and a row stamped at the
  old derivation version serves null. Integration — the tool answers against a store built by
  wave 1's own sync, with fixtures taken from Chunk 01's real captures rather than hand-written.
  Schema and rebuild both change, so the norm tests are re-proven red
  (`tests/preferences/verify_norms_go_red.py`).
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
  price is older than the capture, a security could not be denominated, or the account is closed.
- **Depends on:** Chunk 05
- **Artifacts consumed:** `api-contract.md` § Direction (freshness stamp and warnings;
  balance lifecycle), `.prawduct/artifacts/api-contract.md` § warning vocabulary
- **Deliverables:**
  - a request-scoped warning kind for **a stale position** (`positions_not_current`), raised on two triggers kept distinct
    in its detail. *(Amended at the trajectory checkpoint, 2026-09-13.)* First and commonest:
    `price_as_of` more than 4 calendar days before the row's `as_of_date` — the sandbox serves a
    2021 price on a 2026 capture, and a capture date alone can never show it
    `[ASSUMPTION: 4 calendar days, enough to clear a long weekend | MED impact | owner can
    override]`. Second: the latest holdings capture is older than the connection's transactions
    freshness, which Chunk 03's independent domain failure can produce. *(Split after the cumulative
    review, 2026-09-13: an account behind its OWN connection's newest capture was listed with no
    position and may hold none now -- a working feed, named apart from a stopped one -- and a
    non-active account is left to `account_no_longer_active`.)* A null `price_as_of`
    is named as unknown, not treated as fresh
  - `rule-applied` naming every position refused at derivation because its currency has no
    known exponent **or states none** — account, security and currency — on the capture day
    the answer reads. *(Amended while building, 2026-09-13.)* 🔴 The refusal wave 1 built was
    visible nowhere: `derive_investments_holdings` skips the row and logs it, and
    `holdings.market_value_minor` is NOT NULL, so nothing a read can see recorded it. And
    `list_holdings` emits no total, so "excluded from the total" has no figure to apply to —
    the position is absent from the ROWS, and that is what is named
  - 🔴 **the refusal recorded, so it can be named.** `[DECISION: migration 011 adds
    `refused_holdings` — (account, security, capture day, currency, provenance) — derived from
    the archived bodies | owner, 2026-09-13 | chosen over inferring refusals from
    `securities.currency` (names the instrument, not the account, and misfires on a security
    seen only in investment transactions) and over descoping with the norm departure recorded]`.
    The table obeys the holdings append rule through the shared `_claim_capture_day`, carries a
    raw response as its only provenance (nothing but a sync can refuse a position), and is
    rebuildable by the classification `store.rebuild` already derives. It is not one of FR-6's
    thirteen, which are a minimum: it is declared beside them as a later table, so the metadata
    guard still refuses a table nobody declared. `DERIVATION_VERSION` moves to 10. The latest
    capture day per account is read across BOTH tables, so an account whose only position was
    refused is named rather than read as holding nothing. The populated-store upgrade module
    re-points at 011, keeping 010's fixture and rewind
  - `account_no_longer_active` reaching holdings, not only balances: the existing kind
    already means what is needed, so this is an emitter, not a new kind. It rides beside
    `roster_observed_empty`, as on every other answer surface, and its detail says a POSITION
    froze rather than a balance
  - 🔴 **say on the coverage surface that its measurements are the TRANSACTIONS domain's.**
    `query._account_coverage` is pinned to that domain, so a brokerage account whose only
    activity is investment transactions reports `transaction_count` 0 and draws
    `accounts_without_coverage` on the night its trades were pulled. Wave 1 descoped the fix
    (#107) and recorded it at the site in `query.py`, which is the only place it is written —
    defensible while every field on that row is transaction-named and no tool can answer about
    positions, and indefensible the moment `list_holdings` ships. So either the
    `get_coverage_report` field table and `accounts_without_coverage`'s guidance say which
    domain they speak for, or #107 is resolved here; what is not available any more is
    silence. **Route taken (2026-09-13): the domain is named, and #107 stays open** for the fix
    that changes two published row shapes
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
  old answers with the warning and still returns the rows; an unpriceable position is named
  under `rule-applied` — account, security and currency — and is absent from the rows rather than
  silently rounded (the tool emits no total, so there is none to exclude it from); an account whose
  only position was refused still names it. Migration 011 — a populated store upgrades with the
  table empty and `store rebuild` fills it from the archive; a refusal obeys the append rule as a
  position does. Schema and rebuild both change, so the norm tests are re-proven red.
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
  - 🔴 **carried from Chunk 06's verify-resolutions** (an observation, not a finding): in
    `query.list_holdings`, an account left out of a newer capture, on a connection whose newest
    capture is itself behind its transactions, gets only the "the feed is working" wording, which
    is false there. Name both facts for that account, with a test for the combination and a go-red
    case. It rides this chunk's commit rather than buying a review round of its own
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
  - 🔴 **Decisions taken at this chunk's start (owner, 2026-09-13), all four on the recommendation
    offered:**
    - `[DECISION: the name stays `balance_history` | owner | chosen over `net_worth_history`]` —
      the contract table, §5, the surface inventory and the wire's unbuilt roster all name it, so a
      rename is a same-commit sweep that buys selection for one reading and costs it for the other.
      The selection cost for "net worth over time" is ACCEPTED, and the tool description is what buys
      it back: it opens with that question
    - `[DECISION: an aggregate row is emitted only for a COMPLETE day | owner | chosen over summing
      what was captured with a warning, and over carrying a balance forward]` — the net-worth row for
      `(date, currency)` exists only when every account holding a balance in that currency, whose
      first capture is on or before that date, was captured that day. Otherwise it is withheld and the
      withholding is named under `rule-applied` (accounts and days): a partial net worth is the most
      believable wrong number this tool could emit. 🔴 **Corrected while building, from the option's
      own wording** ("between its own first and LAST capture"): ending an account's span at its last
      capture makes a connection whose sync stopped three days ago drop out of the last three days'
      totals silently — the exact failure the option was chosen to prevent. So an ACTIVE account's span
      is open-ended from its first capture, and only an account NO LONGER ACTIVE (closed, or no longer
      listed) keeps the option's "through its last capture" — refined again while building, because the
      sandbox store holds fourteen accounts replaced by relinked ones, and an open-ended span for them
      would withhold every net worth since. Their exclusion is STATED under `account_no_longer_active`;
      Chunk 08 turns it into include-and-flag with the magnitude. `[user can veto]`
    - `[DECISION: windowed, capped and paged like `query_transactions` | owner | chosen over interval
      sampling and over deferring the cap]` — `since`, `until`, `account_id`, `limit`, `cursor`; at
      most `MAX_ROWS` rows, newest day first, the day's net-worth rows before its account rows. The
      window is reconciled against the BALANCE series' own span, never the transactions' (balances
      begin at enrollment, transactions two years earlier, so a transactions clamp would claim coverage
      no balance has). So the window, the cursor and the `rows_truncated` wording each learn which
      series they describe; `transactions_in_effective_window` is not carried. (`gapped` is phrased
      against the requested window as on every windowed answer — corrected while building from "keeps
      its unwindowed phrasing", whose "this request named no window" would be false.) With
      `account_id`, the answer is that account's rows and no aggregate row. The wire spells the amounts
      `*_minor_units`, the surface's convention, where discovery's shape used the column spelling
    - `[DECISION: assets and liabilities split by `balance_class` | owner | chosen over the sign of the
      balance]` — `data-model.md` records `balance_class` as existing because net worth is its
      consumer, and an operator's reclassification then reaches the report. An asset-class account's
      balance is `assets_minor` (an overdraft reads negative there); a liability-class account's is
      `liabilities_minor` as the magnitude owed (a credit balance reads negative there). `net_minor` is
      the signed balance, so `net = assets - liabilities` at both levels by construction
  - both readings from ONE statement: each (account, currency)'s first capture across the whole
    store, left-joined to its captures inside the window, so the completeness test and the rows it
    judges come from one snapshot
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
    value is **already in `balances_daily`**, because the aggregator's accounts-get endpoint reports a brokerage
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
  - `docs/system-requirements.md` and `api-contract.md` amended for the lifecycle treatment. *(The
    unbuilt note already dropped to `find_recurring` alone in Chunk 07, because the documented
    tool-surface guard holds every counted document to the built set in the same commit.)*
  - 🔴 **carried from Chunk 07's review (`rev-20260913T201311Z-35ac5f2c`)**, riding this chunk's
    commit rather than buying a round of its own. Each was accepted there with this deliverable as
    its reason, so dropping one is dropping a recorded commitment:
    - **R-1:** walk `balance_history`'s cursor through the MCP boundary (`_call`) until it is gone,
      every row arriving once. Show each tool refusing the other's cursor there. Add a go-red case
      that removes `after=series_cursor`.
    - **R-2:** judge completeness over every captured day crossed with the currencies counted, so a
      currency whose accounts all missed a day the sync ran is withheld and named, not silently
      absent. Add a two-currency case.
    - **R-3:** decide `positions_not_current`'s "stopped" from the INVESTMENTS domain's own
      `sync_state`: its `last_success_at` older than the transactions domain's, or a
      `last_error_code`. Stop inferring it from capture dates, since a successful pull that lists no
      position writes none. Add a test where such a pull emits no "stopped" wording.
    - **R-4:** claim a position's `(account, security, day)` key across `holdings` and
      `refused_holdings` at WRITE time, so the two tables never share a key. `list_holdings` then
      drops `held_first` and `overruled`, and the holdings total this chunk builds reads one table.
      Test that replay order changes nothing, price any `DERIVATION_VERSION` move, and keep the
      manual-row rule.
    - **R-5:** make `SeriesCursor.position()` delegate to `series_position`. Add a two-currency
      paging case.
  - 🔴 **Decisions taken at this chunk's start (owner, 2026-09-13).** Both were put with the sandbox
    store measured first:
    - `[DECISION: in net worth over time, an account no longer active counts through its last capture
      and not after; the answer states what stopped counting | owner | chosen over carrying its last
      balance forward]`. The measurement decided it: the sandbox's 14 relinked accounts, last captured
      2026-09-08/09, hold last balances summing to −7,716,415, identical to their 14 replacements', so
      carrying them forward serves net worth at exactly 2× on every later day. That is the shape of the
      2× `money_summary` total this chunk exists to prevent, and it also puts balances on days nobody
      captured, which Chunk 07's no-smoothing rule refused. Recorded as a **ruling at the edge of**
      `api-contract.md` § Direction's lifecycle norm, not an amendment. The norm chose include over
      exclude because an exclusion is "invisible by construction — no field can point at what is not
      there". In a series the exclusion is visible: the account's own rows end on its last day, and
      the answer states it. The magnitude therefore stays load-bearing. `account_no_longer_active` on
      `balance_history` names each account's last captured day and its signed last balance, plus the
      per-currency count and signed sum. That sum is the figure `coverage.accounts_not_active` /
      `not_active_balance_minor_units` already carries present-and-zero. A go-red case removes the
      magnitude from the detail. The go-red case "an account no longer active counts only through its
      last capture" stays, and the test it names gains the magnitude assertion
    - `[DECISION: `list_holdings` gains a per-currency `totals` block | owner | chosen over settling
      the lifecycle ruling with no total built, which the builder recommended]`. The recommendation's
      cost, recorded so a reviewer can weigh it: a position sum is a SECOND value for money net worth
      already counts through the balance series, and it does not reconcile to that balance (the sandbox
      401k is 6% over). The block therefore carries the double-count warning in the tool description
      and the contract row. Its shape: one entry per currency, every key present and zero where nothing
      qualifies — `currency`, `positions`, `market_value_minor_units`, `not_active_positions`,
      `not_active_market_value_minor_units` (signed). It is summed from the returned rows, never from a
      second read, on `_flow_class_totals`' reason; `list_holdings` is uncapped, so the rows are every
      position. The block is always present, and empty when there are no rows. Non-active positions
      stay IN `market_value_minor_units` (include and flag, per the norm), with their count and value
      beside it. Not in it: cost basis, which is nullable per position, so a sum over the known ones
      is a wrong number with no signal; and refused positions, which have no minor units to add and are
      already named under `rule-applied`. `mcp._output_schema`'s existing `totals` flag carries it, so
      no fourth conditional envelope key arrives. R-4 is what lets the block read one table
    - `[DECISION: a stopped investments feed is one whose last attempt archived no holdings reply, and a
      left-out account is judged against the newest archived reply's day | taken in the cumulative
      review's fix pass, 2026-09-13 | refines R-3's wording above | user can veto]`. The review found
      that R-3 as worded compared the calendar days of separate stamps of one sync, and any two of
      them can straddle midnight UTC. That named a working feed stopped, or every account left out,
      until the next sync. `sync run` attempts investments, archives the holdings reply, stamps the
      domain, then attempts transactions (measured on the sandbox store), so a reply archived after
      the attempt began is that attempt's positions. That makes the rule exact, with no calendar days.
      Two refinements follow: a failure after the reply landed is the trades' problem, not the
      positions', and a window that merely came back short is not a stopped feed.
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

### Chunk 09: Holdings and the balance series leave `query.py` (a pure move, #115)

- **Description:** `query.py` holds every tool's assembly. The two tools this plan added reach the
  rest of it only through a narrow shared core, and they are what pushed the file past twice the
  size that prompted the envelope extraction (#39). Move each cluster into its own module, and
  change nothing a client, a test assertion or a stored row can see. It follows the envelope
  precedent (`build-plan-envelope-module.md`), including that plan's two recorded traps.
- **Depends on:** Chunk 08
- **Backlog:** `brookstalley/bankmachine#115`
- **Requirements confidence:** High. The boundary is named in #115, and the precedent measured how a
  move like this fails.
- **The boundary:**
  - **Moves to new `src/bankmachine/query_holdings.py`:** `POSITION_PRICE_STALE_AFTER`,
    `InvestmentFeed`, `_investment_feeds`, `_holdings_totals`, `_positions_not_current_caveat`,
    `_refused_positions_caveat`, `list_holdings`.
  - **Moves to new `src/bankmachine/query_balances.py`:** `_BALANCE_ROWS`, `_series_ends`,
    `BalanceCapture`, `WithheldNetWorth`, `_split`, `compose_balance_series`, `_series_row`,
    `_withheld_net_worth_caveat`, `balance_history`.
  - **Stays in `query.py`:** the shared core both clusters import (`_readable`, `_unusable`,
    `_answer`, `_account_lifecycle`, `_not_active_caveat`, `_roster_observed_empty_caveat`,
    `_account_exists`, the `_undenominable_*` family) and every other tool. A name used by a moved
    cluster AND by something that stays, stays.
  - 🔴 **No re-export from `query.py`.** The new modules import the core from `query.py`, so
    `query.py` importing them back is a cycle. Every caller is re-pointed instead: `mcp.py`'s
    dispatch, and the tests that import or monkeypatch a moved name.
- **Open assumptions:**
  - `[ASSUMPTION: the modules are named query_holdings.py and query_balances.py | LOW impact | user
    can correct; it is a rename away until the chunk commits]`. The `query_` prefix keeps them read
    as the query layer, not as `store/investments.py`'s write path.
  - ~~`[ASSUMPTION: importing the core's underscore names across modules is acceptable | LOW impact]`~~
    — **checked 2026-09-13:** `[tool.ruff.lint] select` is `E, F, I, N, UP, B, SIM`, which has no
    private-import rule. The core keeps its names; renaming it inside a pure move would stop the
    move being pure.
- **Deliverables:**
  - the two modules, and `query.py` without the moved definitions
  - `mcp.py` and every test re-pointed. 🔴 **Monkeypatch targets move with the reads.** The envelope
    move found a test that patched the old module while the code read the new one. It stayed green
    while asserting nothing, and only failed loudly by luck. Grep `setattr` targets and
    `"bankmachine.query.` strings, not just imports
  - every `verify_norms_go_red.py` case whose anchor lies in the moved code re-pointed to a new path
    constant. Recount them at build from the anchors themselves; do not copy a planning figure.
    Assert each mutation lands, then run the harness ALONE and see each case go RED
  - artifact and docstring references to the moved names re-pointed where they name a file
  - 🔴 **Not in this chunk, said so rather than dropped:** #115 also observes that
    `mcp._tool_definitions` has "the same shape". Its Expected section asks only for the two
    clusters, so that extraction is not done here. #115's close-out names it, and it is filed only
    if the owner wants it
- **Tests:** no assertion changes. `git diff` over `tests/` shows only import lines, monkeypatch
  targets and the harness's path constants and file fields, and the Critic is pointed at that diff.
  The suite is green, and the harness catches every case it caught at `68566f2`.
- **Acceptance criteria:** the published surface is byte-identical. `_tool_definitions()`
  serialised with sorted keys, and `mcp_resources.documents()` over it, match a dump from `68566f2`
  byte for byte. The result is recorded here, as the envelope plan recorded its own.
- **Result, recorded 2026-09-13:**
  - **Published surface:** `_tool_definitions()` for all seven tools, serialised with sorted keys,
    is 90,238 bytes before and after, and `cmp` is clean. `mcp_resources.documents()` over it
    (both reference documents) is 34,306 bytes both sides, `cmp` clean. Both are dumped from this
    checkout at `68566f2`'s code and again after the move.
  - **Tests:** across the three test files that changed, the lines removed and the lines added
    differ only by the `query_holdings.`/`query_balances.` prefix and the import line. Checked as a
    multiset of lines, not by eye. Four lines the longer prefix pushed past the limit were reflowed
    by `ruff format`. No monkeypatch target named a moved attribute.
  - **Harness:** 25 cases re-pointed (16 to `QUERY_HOLDINGS`, 9 to `QUERY_BALANCES`). An anchor was
    retargeted only when its count in `query.py` fell to zero and it appeared in exactly one new
    module. The other 42 `QUERY` cases keep their count in `query.py`. The harness was run alone and
    detached: all 211 norm breaks were caught, with no SKIP, AMBIGUOUS or INVALID. The surface dump
    was repeated after it and is still byte-identical, so no mutation was left behind.
- **Type:** code
- **Critic mode:** chunk
- **Done when:**
  1. Acceptance criteria met, the harness run alone, then the full gate green
  2. Committed, then `/prawduct:critic` run and blocking findings resolved
  3. Chunk marked `[x]` in Status

### Chunk 10: An investment account's activity counts as coverage (#107)

- **Description:** An account whose only activity is investment trades and positions reads
  `transaction_count` 0 on `list_accounts` and `get_coverage_report`. Both listings then raise
  `accounts_without_coverage`, which tells an agent to distrust an account whose data IS in the
  store. Chunk 06 labelled the field; this chunk fixes what the listings say.
- **Depends on:** Chunk 09
- **Backlog:** `brookstalley/bankmachine#107`
- **Artifacts consumed:** `api-contract.md` § *Coverage is reported per account*, the `list_accounts`
  and `get_coverage_report` field tables, and the `accounts_without_coverage` row and its scoping
  paragraph
- **Requirements confidence:** High on behaviour; field names are assumptions.
- **Decisions:**
  - `[DECISION: add per-account investment facts beside transaction_count, and narrow the LISTINGS'
    warning to accounts neither feed holds anything for | owner, 2026-09-13 | chosen over widening
    transaction_count, and over narrowing the predicate with no new field]`. Widening would silently
    change what a documented field counts, and would count trades as if `query_transactions` could
    return them, which it cannot. Narrowing alone would leave an agent reading 0 with no field
    saying where the activity is.
  - `[DECISION: query_transactions(account_id=N) and money_summary keep the transactions-feed
    predicate | taken in this plan | user can veto]`. Both answer from the transactions feed, so an
    empty answer from either about an investment-only account really is "no data for this tool".
    The warning stays true there and keeps pointing at `list_holdings`. Only the two listings, which
    describe the account itself, change.
- **Open assumptions:**
  - `[ASSUMPTION: the fields are investment_transaction_count (integer, 0 not null, soft-deleted
    trades excluded) and holdings_as_of (the newest captured holdings day, nullable) | LOW impact |
    user can rename before commit]`. `holdings_as_of` follows `balance_as_of` on the same row.
  - `[ASSUMPTION: a position refused for its unit does not count as a capture for holdings_as_of |
    LOW impact | user can override]`. It is named under `rule-applied` on `list_holdings`, so an
    account whose EVERY position was refused would read as having no data. Rare, and saying "no
    positions" there is closer to what can be served than naming a day with nothing on it.
- **Deliverables:**
  - `AccountCoverage` gains both facts and a predicate for "neither feed holds anything". `uncovered`
    keeps its current meaning, because four callers read it and two of them keep it.
  - 🔴 **The two facts come from grouped subqueries joined per account, never from widening the
    existing outer join.** Joining `investment_transactions` beside `transactions` multiplies each
    count by the other, and the join-site comment already records that failure for `sync_state`.
    That comment's "what this DOES leave unsaid" paragraph is rewritten to say what is now said.
  - `list_accounts` and `get_coverage_report` raise `accounts_without_coverage` only for accounts
    with no data in either feed, with detail naming what each account lacks.
    `query_transactions(account_id=N)` and `money_summary` are unchanged.
  - `mcp._coverage_row_fields` is the one schema fragment both tools publish. It gains both fields,
    required and present, and `transaction_count`'s description points at the new count rather than
    only at `list_holdings`.
  - every surface that says what `accounts_without_coverage` means on a listing:
    - both tool descriptions in `mcp.py`
    - `mcp_resources.py`'s guidance for the kind
    - `api-contract.md`: both field tables, the vocabulary row, the paragraph on where the kind
      fires, and the § Operations note that leaves #107 open
    - `docs/connecting-an-mcp-client.md`
  - 🔴 **The envelope reference's "What this server cannot answer" says trades are stored and
    "read by no tool", and this chunk makes that false.** Counting them is reading them.
    `tests/test_mcp.py::test_the_unserved_trades_claim_holds_against_what_every_tool_reads`
    captures every statement each tool executes and fails the moment any tool touches
    `investment_transactions`. That is the guard working, and it is not to be quieted. The claim
    becomes what is true: trades are counted per account and served as rows by no tool. The guard
    is re-aimed at that claim, and it must stay able to fail. It holds that no tool's ROWS carry a
    trade, and it still fails if the text claims more or less than the SQL does.
    `[DECISION: re-aim rather than delete | surfaced while reading the guard before building this
    chunk, 2026-09-13 | user can veto]`. The guard's purpose, which is that the cannot-answer list
    matches what the tools do, survives. The old sentence it held does not.
  - 🔴 **Carried from Chunk 09's review (`rev-20260913T234353Z-a12c5cf0`), riding this chunk's commit
    rather than buying a round of its own.** Record each as accepted, citing this commit, once it
    lands:
    - **R-2 (warning):** `docs/connecting-an-mcp-client.md` and `envelope.py` still describe a
      stopped investments feed as "investments stopped while transactions did not". Since
      `d1d9a96` the rule is that the last investments attempt archived no holdings reply. Correct
      both to that rule.
    - **R-1/R-4:** `_POSITIONS_FROZE` is used only by `list_holdings`, so by Chunk 09's own
      boundary rule it moves to `query_holdings.py`.
    - **R-3:** the `mcp.py` and `envelope.py` docstrings still say every tool runs through
      `query.py`. `_holdings_totals` refers to `_flow_class_totals` as if the two shared a file.
- **Tests** (`tests/test_account_coverage.py`, beside the cases they refine):
  - an investment-only account reports its trade count and holdings day. Neither listing names it,
    and `query_transactions(account_id=N)` still warns about it
  - an account with both feeds reports each count exactly, not their product
  - a soft-deleted trade is not counted
  - an account with positions and no trades is not "no data"
  - an account with neither feed is still named by both listings
  - the existing "two tools never disagree" case extends to the new fields
  - go-red cases, each seen RED: the listing predicate reverted to `uncovered`; the count taken
    through the widened join; the soft-delete filter removed
- **Acceptance criteria:** on the sandbox store, `list_accounts` shows each investment account's
  trade count and holdings day, and neither listing raises `accounts_without_coverage` for it.
  `query_transactions(account_id=` one of them `)` still does. An account with no data in either
  feed is still named.
- **Visual change:** yes — the wording an agent reads to decide whether an account is empty. The
  operator verification entry is VRF-024, to be read in the same client session as VRF-022/023.
- **Result, recorded 2026-09-13:**
  - **Sandbox store (read-only, this build):** 28 accounts. The two investment accounts (ids 20
    and 21) carry `transaction_count` 0, 219 and 948 trades, and `holdings_as_of` 2026-09-13.
    Neither listing names them. `list_accounts` and `get_coverage_report` each name the same 16
    accounts, exactly the set with nothing in any feed.
    `query_transactions(account_id=20)` returns no rows and names account 20.
  - **Harness:** six new cases, covering both listing predicates, `query_transactions` keeping its
    own predicate, the widened join, the removed-trade filter and the cannot-answer claim. Each was
    seen RED in a subset run. The full harness run alone and detached then caught all 217.
  - **Tests written first and seen red:** the four new cases the code had to change failed before
    it did. The two that pin unchanged behaviour (`query_transactions` and `money_summary` still
    naming an investment account) passed before and after, as intended.
  - 🔴 **One test expectation was wrong, and it was corrected rather than the code bent to it.** The
    listing test first expected exactly two named accounts. The captures' rosters also add the
    sandbox's checking, loan and card accounts, which have no data in any feed, so naming them is
    correct. The expectation now derives the set from what the fixture wrote, not from the store, so
    it is still exact.
- **Type:** code
- **Critic mode:** final
  <!-- Reviews 09 and 10 together. Chunk 08's cumulative covers the branch to 68566f2, and the PR
       gate composes the two, re-run to confirm. -->
- **Done when:**
  1. Acceptance criteria met on the sandbox store, the harness run alone, then the full gate green
  2. Committed, then `/prawduct:critic` run and blocking findings resolved
  3. #107 and #115 marked shipped through `/prawduct:backlog` once the PR merges, and the chunk
     marked `[x]` in Status

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
  **Ran 2026-09-13, against the sandbox store and `api-notes-plaid.md` §22-23.** Cost basis:
  populated 13/13 but documented nullable, so Chunk 05's present-and-null field stands. Capture
  dates: holdings and balances land on the same day by construction, so Chunk 08's argument holds.
  Its property must not assume Σ holdings = balance: the IRA reconciles exactly and the 401k is
  6% over. A field the row should expose: `institution_price_as_of`, dropped by wave 1, which
  moved chunks 05 and 06.
- **Mid-cycle wire text is not kept accurate between waves** (owner, 2026-09-13). The envelope
  reference names `list_holdings` from wave 1 onward, although the tool registers in Chunk 05 and
  both MCP servers run from this checkout. Accepted because nobody uses or tests the server until
  all three waves land; Chunk 05's deliverable writes the final text.
- **After Chunk 06** — the warning surfaces: is every kind covered in the guidance map, and
  did the primer stay under budget without losing the sentence that made a kind actionable?
- **Chunk 08 (cumulative)** — full-bundle review across all three waves, with particular
  attention to two things: whether any artifact still claims investments or the two tools are
  unbuilt, and whether the double-count guard actually holds a test that fails when the guard
  is removed.
- **Chunk 10 (final, over 09 and 10)** — two things in particular. Did the move leave any test
  asserting through a patch aimed at the old module? And does any surface still say a listing's
  `accounts_without_coverage` counts the transactions feed alone?
