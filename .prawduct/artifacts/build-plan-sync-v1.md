---
artifact: build-plan
version: 2
scope: sync-v1
branch: feature/sync-v1
depends_on:
  - artifact: system-requirements
    file_path: docs/system-requirements.md
  - artifact: build-plan-enrollment-v1
    file_path: .prawduct/artifacts/build-plan-enrollment-v1.md
  - artifact: api-notes-plaid
    file_path: .prawduct/artifacts/api-notes-plaid.md
  - artifact: data-model
    file_path: .prawduct/artifacts/data-model.md
governed_by:
  - artifact: architecture
    dispositions:
      - "every writable handle comes from the one writer factory, which takes the exclusive lock before it returns → 🔴 conforms, and this plan is the first to depend on it for correctness rather than tidiness. AC-2.1 requires the cursor to be persisted transactionally with the data it accompanies, and AC-2.5 requires a crash mid-sync not to advance it. Both rest on one writer holding one lock for the unit"
      - "read-role handles open mode=ro and hold no snapshot beyond the statement that needs it → conforms; the sync loop is a writer throughout. The coverage report that reads its output is a later step"
      - "no component creates the datastore implicitly → conforms; `sync` refuses an absent datastore before reaching the aggregator, in the shape `enroll` and `connector check` already use"
      - "a process that does not recognize the schema version refuses to serve → conforms by inheritance through the writer path"
  - artifact: data-model
    dispositions:
      - "every stored amount is signed from the operator's point of view → 🔴 conforms and is this plan's sharpest obligation. The aggregator reports a purchase as a POSITIVE amount leaving a depository account, which is the opposite of this norm's convention. `connector/plaid/derivers.py` already normalizes balances; transactions are the larger surface and the one where getting it wrong is wrong by twice the amount on every row"
      - "all monetary values are stored as integer minor units, no floats → conforms; bodies parse with `parse_float=str` and `to_minor` refuses what it cannot convert exactly. Ledger amounts are exact or refused — the valuation exception is investments only"
      - "calendar dates and UTC instants are distinct types and never mix → 🔴 conforms, and transactions are where the norm was written for. `posted_date` and `authorized_date` are calendar dates from the institution; `first_seen_at`, `updated_at` and `removed_at` are UTC instants. The off-by-one this prevents lands exactly at period boundaries, which is the product's headline query"
      - "every silver row carries the evidence for whether it is aggregator-sourced or manually imported, and the derivation version that produced it → 🔴 conforms and is enforced by the schema: `transactions.derivation_version_id` is NOT NULL, and the row carries `raw_response_id` XOR `manual_import_id`"
      - "the daily balance and holdings series are append-only; a day already recorded is never overwritten → 🔴 engaged, and the tempting violation is named in the norm itself: AC-2.4's idempotency requirement is what will pull the sync writer toward `ON CONFLICT DO UPDATE` on `balances_daily`. It must not. A second capture on a recorded day is rejected"
      - "a source value is never overwritten in place; local interpretation lives in its own column → conforms; `source_category_primary`/`_detailed` hold what the aggregator sent and `category_override` holds local intent. A modified transaction rewrites source fields from the new response, which is the source correcting itself rather than local interpretation being lost"
      - "a transaction is never hard-deleted → 🔴 conforms; AC-2.2's `removed` list sets `removed_at` and nothing issues a DELETE. The `removed` entry carries only `transaction_id` and `account_id`, which is all a soft delete needs"
      - "a migration's DDL is frozen once written, and the Core metadata and that DDL are written independently → conforms by not participating; every column this plan writes already exists"
  - artifact: api-contract
    dispositions:
      - "the CLI's three-way exit code is a contract: 0 success, 1 ran and found a problem, 2 could not run → 🔴 conforms, and this plan owes the decision `build-plan-connector-v1.md` deferred. A sync that ran and found one connection degraded is `1`; a sync that could not reach the aggregator at all, or could not open the datastore, is `2`. AC-4.1's 'one broken connection never aborts another' is what makes the split meaningful here: the run completes, and its exit code reports what it found"
      - "the MCP surface is read-only → inapplicable because this plan adds no MCP tool"
      - "every response carries a freshness stamp, and incompleteness rides the success path as a warning → inapplicable to this plan's own surface, but it is the CONSUMER of what this plan records. `sync_state.last_success_at` and the granted-window shortfall are the facts that layer will read"
  - artifact: security-model
    dispositions:
      - "secrets live only in the OS keychain, and nothing returns one into a log, an exception message or a repr → conforms; the sync loop reads each connection's access token through `get_access_token` and the token never enters a row, a log line or an error"
      - "the aggregator's API is the only network destination → conforms; no new destination"
      - "log redaction happens at the formatter → conforms by inheritance, asserted over a real sync log rather than assumed"
  - artifact: operational-spec
    dispositions:
      - "no filesystem path is hardcoded → conforms; this plan adds no path"
      - "a backup destination is never created implicitly and never overwritten → inapplicable; this plan writes no backup"
  - artifact: project-preferences
    dispositions:
      - "provider-agnostic engine, no roster identity in code or schema → conforms; the sync loop iterates `connections` rows and knows no institution by name"
      - "errors are exceptions, specific not broad; per-connection errors never abort other connections; silence is the one disallowed outcome → 🔴 conforms, and AC-4.1 is exactly this norm applied to the sync loop. A connection that fails records its error on its own row and the loop continues"
partition: serial — Chunk 01 fixes the cursor/transaction boundary that Chunks 02 and 03 both write through, and Chunk 03's granted-window computation reads what Chunk 02's deriver stores. A fan-out would have delegates writing the same deriver module against a boundary none of them had settled.
last_validated: null
---

## Requirements Confidence

**Level:** High

**Why:** FR-2's six acceptance criteria are written at field level, the schema they land in shipped
in build step 1 with every index this needs (`transactions_source_identity`,
`transactions_pending_link`, `transactions_by_account_date`), and `POST /transactions/sync`'s
surface was read from the pinned SDK before this plan was drafted rather than recalled
(`api-notes-plaid.md` §16). The one design decision that is genuinely open is named below rather
than left to be discovered mid-build.

**Open assumptions:**

- `[ASSUMPTION: the sync command syncs every live connection by default, with an optional
  `--connection <id>` to narrow it | LOW impact | user can correct]` AC-4.1 requires one failure not
  to abort the others, which only means something when the default is "all of them".
- `[ASSUMPTION: `accounts` continues to come from `POST /accounts/get` rather than from the
  `accounts` array that rides every sync response | MED impact | user can correct]` Using the sync
  response would save a call per run. It would also make account rows a side effect of transaction
  sync, so a connection with no transaction activity would stop refreshing its accounts — which is
  the case where a stale balance is least likely to be noticed.

## What I Would Do Differently

**The cursor's transaction boundary is the whole plan, and I would not let it become a chunk-02
detail.** AC-2.1 says the cursor is persisted *transactionally with the data it accompanies* and
AC-2.5 says a crash mid-sync does not advance it. Those are one requirement stated twice, and every
plausible implementation satisfies them or violates them at a single seam. So Chunk 01 is that seam
and nothing else — no transaction rows, no pagination, just the boundary and the tests that prove a
killed process leaves the cursor where it was.

**The design I recommend, and the one I rejected.** `apply_response` commits the archive and the
derivation separately, so the cursor cannot ride the archive commit. The options are (a) the
*deriver* writes `sync_state.cursor` from `next_cursor` in the same body it derives rows from, so
the cursor lands in the derivation's own transaction by construction; or (b) the sync command writes
the cursor itself in a transaction it shares with the derivation. **I recommend (a)** — it makes the
guarantee structural rather than procedural, in the shape this project has repeatedly chosen: the
cursor and the rows come from one response and commit together because they are derived together,
not because a caller remembered to wrap them. Its cost is that a deriver writes `sync_state`, a
table the rebuild classifies as operator state and never empties; a replay re-derives the same final
cursor, which is correct but should be asserted rather than assumed.

**What I would cut if this needs to be smaller.** Chunk 03 — the granted-window computation. It is
the payoff for AC-1.3a and AC-11.8 and I would rather ship it, but it is the one part that can be
added later without touching the sync loop.

## Chunks

### Chunk 01 — The cursor boundary, and what a crash leaves

**Type:** code
**Foreign API:** Plaid `POST /transactions/sync`

The seam AC-2.1 and AC-2.5 both rest on, built and proved before anything writes a transaction row.

- `TRANSACTIONS_SYNC` endpoint, archivable — its body carries no credential.
- `transactions_sync(access_token, cursor)` on the client, returning a `FetchedResponse`.
- The cursor lands in the same transaction as the rows derived from the same response.
- 🔴 A killed process does not advance the cursor.

**Done-when:**

0. **`verify-api`** — §16 is read, not re-derived; probe live for the shape of a *first* sync (null
   cursor) against a sandbox item, and record whether `transactions_update_status` is `NOT_READY` on
   a freshly enrolled connection. That value gates Chunk 03 and is not observable later.
1. A response with `next_cursor` writes both its rows and the cursor, or neither. Proved by making
   the row write fail and asserting the cursor did not move.
2. A process killed mid-derivation leaves `sync_state.cursor` at its previous value, tested by
   killing a real subprocess rather than by raising in-process — the guarantee is about the
   database, and an in-process exception does not exercise the same recovery.
3. A rebuild replays to the same final cursor.

### Chunk 02 — Added, modified, removed, and the pending that becomes posted

**Type:** code

- The transactions deriver: `added` inserts, `modified` updates in place, `removed` sets
  `removed_at` (AC-2.2). Nothing issues a DELETE.
- 🔴 Amounts normalized to the operator's sign convention. The aggregator reports a purchase as a
  positive amount leaving a depository account; stored, it is negative.
- Pending→posted matched on `source_pending_transaction_id`, so a posting transaction updates the
  pending row rather than duplicating it (AC-2.3).
- The loop pages until `has_more` is false (AC-2.1).
- `sync` command: every live connection, one failure never aborting another (AC-4.1), exit `1` if
  it ran and found a degraded connection, `2` if it could not run.

**Done-when:**

1. A second consecutive sync produces zero net changes (AC-2.4), asserted by comparing a full table
   digest before and after rather than by counting rows.
2. A pending transaction that posts leaves exactly one row, carrying the posted identity.
3. A removed transaction is still present with `removed_at` set, and its amount still participates
   in nothing that sums live rows.
4. A purchase reported positive against a depository account is stored negative, with a norm-break
   case proving the assertion goes red without the normalization.
5. One connection failing leaves the others synced and the run reporting `1`.

### Chunk 03 — What the aggregator actually granted

**Type:** cumulative-final

- `NOT_READY` on the success path triggers backoff-and-retry rather than failure (AC-2.6).
- 🔴 `sync_state.history_start_date` and `connections.granted_history_days` are written **only** at
  `HISTORICAL_UPDATE_COMPLETE`.
- AC-11.8's shortfall — requested minus granted — becomes computable for the first time.

**Done-when:**

1. A `NOT_READY` response backs off and retries; it is not an error and does not degrade the
   connection.
2. 🔴 `granted_history_days` stays null through `INITIAL_UPDATE_COMPLETE` and is written at
   `HISTORICAL_UPDATE_COMPLETE`. A test proves that computing it early would record a shortfall that
   does not exist — this is the confidently-wrong number the whole product exists to prevent.
3. A shortfall is recorded as a known gap where granted is less than requested (AC-11.8).

## Verification Strategy

The sandbox serves canned transaction data through a real enrollment, so unlike Chunk 02 of the
enrollment plan this one *can* be driven live end to end: `POST /sandbox/public_token/create` mints
an item whose transactions are real-shaped and known. That is the substrate for the idempotency and
pending→posted tests.

🔴 **What sandbox cannot prove is the granted window.** Its canned history is the aggregator's
choice, not a real institution's, so a shortfall observed there says nothing about production. The
first real connection remains the only place AC-11.8 is verified, and it stays gated behind the
owner's roster decision.

## Governance Checkpoints

1. **After Chunk 01's probe** — if `transactions_update_status` does not behave as §16 reads, Chunk
   03's gate is unsound and must be redesigned before it is built.
2. **Chunk 02 close** — the sign convention across a real transaction corpus.
3. **Chunk 03** — cumulative review across the plan.

## Status

- [ ] Chunk 01 — The cursor boundary, and what a crash leaves
- [ ] Chunk 02 — Added, modified, removed, and the pending that becomes posted
- [ ] Chunk 03 — What the aggregator actually granted

## Context

Base is `develop`. Follows `build-plan-enrollment-v1.md`, which completed build step 3 and left
`granted_history_days` null by design (AC-1.3a) for this plan to fill.
