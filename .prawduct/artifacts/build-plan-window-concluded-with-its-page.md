---
artifact: build-plan
version: 2
scope: window-concluded-with-its-page
branch: feature/rebuild-after-interrupted-reconciliation
depends_on:
  - artifact: data-model
  - artifact: architecture
  - artifact: build-plan-investment-sync
governed_by:
  - artifact: architecture
    dispositions:
      - "every writable handle comes from the one writer factory, which takes the exclusive lock before it returns → conforms, and this plan removes a use of it: the separate writer acquisition that concluded a window is gone, and the conclusion rides the handle `_persist` already holds for the closing page. That acquisition is the gap a concurrent `store backup` won"
      - "every read-role handle is opened read-only → inapplicable because no reader is added or changed"
      - "no component creates the datastore implicitly → inapplicable because nothing here opens a datastore"
      - "a process that does not recognize the schema version refuses to serve → inapplicable because no schema changes"
  - artifact: data-model
    dispositions:
      - "a transaction is never hard-deleted; removal is a soft delete → conforms; the removal is still `removed_at`, stamped at the closing page's archived instant exactly as before. Only the transaction it commits in moves"
      - "a source value is never overwritten in place → inapplicable because no source column is written differently"
      - "every silver row carries its evidence and derivation version → conforms; no row is written differently, so `DERIVATION_VERSION` does not move. A live store and its rebuild conclude the same removals at the same instants, which is the property the version exists to make checkable"
      - "all monetary values are integer minor units; amounts are operator-signed; dates and instants never mix → inapplicable because no amount, date or instant is derived differently"
      - "the daily balance and holdings series are append-only → inapplicable because neither series is touched"
      - "a migration's DDL is frozen once written → inapplicable because no migration is added"
  - artifact: api-contract
    dispositions:
      - "the CLI's three-way exit code is a contract → conforms; a sync's exit codes are unchanged, including when the writer lock is taken mid-run"
      - "the MCP surface is read-only; every response carries a freshness stamp; tool boundaries; balance lifecycle → inapplicable because no answer changes"
partition: serial — one chunk. The signature change to `apply_response` and every call site must land in one commit or the suite does not import, and the sync change is meaningless without it
last_validated: 2026-09-14
---

# Build Plan: An Investments Window Is Concluded With the Page That Closes It

Closes brookstalley/bankmachine#113, and #121: a stale comment in `sync_run.py`, which this change
already touches.

## Problem

`_pull_investment_transactions` archives and derives each page through `_persist`, which holds one
writer handle across both transactions. After the loop, it opens a **second** writer handle to
conclude the window: `record_investment_transaction_window` soft-deletes what the window did not
return, and records the domain's measured history start.

Two things can happen in between. A `store backup` running beside the sync takes the writer lock,
and `writer_connection` raises `AnotherWriterRunningError`. Or the process is killed. Either way,
the archive holds a **complete** window whose removals were never concluded. The next run re-reads
the window and concludes its removals at the new closing page's instant, or never, if the rows came
back.

`store rebuild` replays the archive through `InvestmentWindowReplay`, which concludes the interrupted
window at **its** closing page's instant. The replay touches only `removed_at IS NULL` rows, so it
never revisits that stamp. The digest differs at an unchanged derivation version, and `store rebuild`
refuses permanently.

The realistic trigger is the first one. Production will run a scheduled backup and a nightly sync on
the same machine (#10).

## Decision (owner, 2026-09-14)

### [DECISION] The closing page's derivation transaction concludes its window, through the replay's own pass

Rejected: (b) recording on the archive that a window was reconciled, which needs a new persisted
column (schema 13, lock-in) and makes the rebuild depend on a derived record rather than raw
responses alone (AC-5.2). And (c) concluding missed windows on the next run: it needs a marker of
concluded windows, and it is unsafe once a later window's pages have re-pointed row provenance,
because concluding the older window would then soft-delete rows it really returned.

- **The live sync runs `InvestmentWindowReplay`, the pass the rebuild runs.** `apply_response`
  observes each response through the passes it is given, inside the derivation transaction, after the
  deriver. Live and rebuild then conclude every window with the same code, at the same response, at
  the same archived instant, in a transaction that commits with that page's rows or not at all.
- **`apply_response` takes `replay_passes` as a required keyword argument, with no default.** Every
  caller states what it passes, and nearly all pass `()`. The learning this applies: `rebuild` shipped
  `replay_passes` with a default, and omitting it cleared every soft delete and reported success. A
  default that is silently wrong when omitted is a trap.
- **Only the sync records the domain's history start.** A sync-owned pass in `sync_run.py` wraps
  `InvestmentWindowReplay`. When the inner pass concludes a window at this response, the wrapper records
  `record_domain_history_start` in the same transaction, which keeps today's "removals and the range
  measured after them commit together". The rebuild uses the bare pass and never stamps `sync_state`,
  because an archived body is evidence about rows, never about how current a domain is.
- **`InvestmentWindowReplay` exposes its last conclusion** (the closing page's id and its `WindowOutcome`), so the sync
  reports removals and the window's verdict from the conclusion that actually committed rather than
  recomputing it.
- **A window that did not close concludes nothing, in both paths**, exactly as now: short, page
  ceiling, empty page before the total, or no stated total. The sync still logs the incomplete
  window itself.
- **Ordering is safe.** The live path pairs transfers per response and the rebuild pairs them once at
  the end, but `pair_transfers` writes only `transactions`, so where the window pass runs relative to
  it cannot change `investment_transactions`.

### What this plan does not close, stated explicitly

- **A crash between a page's archive commit and its derivation commit** still leaves an archived
  page the live store never derived. The window's removals can no longer be separated from its closing
  page, but that page itself can still be lost to derivation, and the rebuild would then diverge. The gap
  is milliseconds on one held handle, no lock can intervene, and it applies to **every** endpoint, not
  to investments windows alone. Filed separately as **#122** (`stage: research`) rather than folded in.
- **Stores already holding an unconcluded window stay unrebuildable.** No store has one that matters:
  production does not exist, and the sandbox has never had its reconciliation interrupted. Repairing
  one is the operator accepting the rebuild's instants, which `RebuildNotReproducibleError` already
  explains.

## Requirements confidence

**High.** #113 names the repro, and the decision settles the shape. Every path involved has been read:
`_persist`, `apply_response`, `_pull_investment_transactions`, `record_investment_transaction_window`,
`InvestmentWindowReplay` and the rebuild loop.

**Open assumptions:**
- `[ASSUMPTION: no caller other than sync run archives investment-transaction pages | MED impact | user can correct]`
  — `enroll` archives items and accounts, and `connections reauth` archives `/item/get`. Held by passing
  `()` at those sites, and by a test that a sync concludes a window only through the pass.

## Chunk 01 — The window concludes with its closing page

**Files.** `src/bankmachine/store/derivation.py` (`apply_response` takes and runs `replay_passes`);
`src/bankmachine/connector/plaid/window.py` (`InvestmentWindowReplay` records its conclusions);
`src/bankmachine/cli/sync_run.py` (a sync-owned pass; `_persist` passes it; the separate conclusion
transaction is removed); `src/bankmachine/cli/enroll.py` and `src/bankmachine/cli/connections.py` (pass
`()`); `src/bankmachine/store/investments.py` and `src/bankmachine/store/rebuild.py` (docstrings that
describe the old boundary); every test caller of `apply_response`; `tests/store/test_rebuild_investments.py`
(`sync_a_window` mirrors the new shape); `tests/cli/test_sync_run.py`; `.prawduct/change-log.md`.

**Tests.**
1. **A backup taking the writer lock after the closing page leaves the store rebuildable.** Through the
   real `sync run`: one run concludes a window of two rows; a second run's window returns one. After
   the second run's closing page is persisted, the next writer acquisition raises
   `AnotherWriterRunningError`. The store then rebuilds with no content change, and the dropped row
   carries the closing page's instant. Must fail on the current code.
2. **A run killed after the closing page leaves the store rebuildable.** The same shape, with the
   interruption escaping the run instead of degrading a domain.
3. **The removal and the measured history start commit with the closing page.** Read the store at the
   moment the closing page's derivation commits: rows retired, and the domain's history start recorded.
4. **A window that does not close concludes nothing** on the live path, for each of: short at the page
   ceiling, empty page before the total, no stated total. The existing tests for these keep passing
   unchanged.
5. **The rebuild never stamps `sync_state`.** The existing byte-identical rebuild tests keep passing,
   which is what fails if the replay records a domain's progress.
6. **The property test.** `test_a_rebuild_is_lossless_for_any_sequence_of_windows` keeps passing with
   `sync_a_window` concluding in the closing page's transaction. A generated interruption point after
   any page of any window is added, and asserts the store still rebuilds without content change.

**Done when.** All of the above pass. The full suite is green. `verify_norms_go_red.py` runs clean
detached, per project preferences, since the raw and rebuild layer changes. Every new test fails
against the pre-change source. `/prawduct:critic` has run, with findings dispositioned.

## Status

- [ ] Chunk 01 — The window concludes with its closing page

## Context

Branched from `develop` at `2983d4b`.

Built. Both interruption tests were run against a planted copy of the old shape (no pass in the page
loop, and the window concluded under a separate writer acquisition afterwards) and failed there: the
dropped row was never retired. The planted file was then restored byte for byte. Affected test files
green (147), and mypy clean across source and tests.

One finding from building: mypy refused the test reading `writer_connection` through `sync_run`,
which does not re-export it. The helper reads it from `store.engine` directly.
