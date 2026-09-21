---
artifact: build-plan
version: 2
scope: investment-activity-e2e
branch: feature/investment-activity-e2e
depends_on:
  - artifact: api-contract
  - artifact: data-model
  - artifact: discovery-mcp-tool-surface
governed_by:
  - artifact: api-contract
    dispositions:
      - "the MCP surface is read-only → conforms; the one tool added reads `investment_transactions` and `securities` through the read-role handle and writes nothing"
      - "every response carries a freshness stamp, and incompleteness rides the success path as a warning → conforms, and this plan is about that norm's other failure: a warning that fires when nothing is incomplete teaches the reader to skip the real ones. Chunk 01 removes two such warnings. Chunk 03 adds one request-scoped kind, which rides the success path like every other"
      - "a tool's boundary is drawn where the answer shape changes → conforms; a trade row carries security, quantity, unit price and fees, which no transaction row has, so it is a new shape and a new tool. Merging it into `query_transactions` would need optional fields, which `_refuse_optional_row_fields` refuses at registration"
      - "a stored balance is reported with its lifecycle, and no total over balances is emitted without stating its treatment of non-active accounts → conforms; trade rows carry the lifecycle row fields, and the new `totals` is a sum of trades, not of balances, so the include-and-flag treatment is inapplicable to it. The rows on non-active accounts stay in it and are marked"
      - "the CLI's exit codes are a contract → inapplicable because no CLI command changes"
  - artifact: data-model
    dispositions:
      - "every stored amount is signed from the operator's point of view → conforms; amounts are served as stored, and the deriver already signs them, so nothing is re-signed on the way out"
      - "all monetary values are integer minor units → conforms; amounts, prices and fees are served as stored integers; `quantity` stays exact decimal text"
      - "calendar dates and UTC instants are distinct → conforms; `trade_date` is a calendar date and the window clamps against calendar dates"
      - "a transaction is never hard-deleted; removal is a soft delete → conforms; rows with `removed_at` set are excluded from rows, counts and totals, the same as `investment_transaction_count` already does"
      - "every silver row carries provenance and derivation version → inapplicable because no row is written and no deriver changes, so `DERIVATION_VERSION` does not move"
      - "a source value is never overwritten in place → inapplicable because nothing is written"
      - "the daily balance and holdings series are append-only → inapplicable because neither series is written or read differently"
      - "a migration's DDL is frozen, and metadata and DDL are written independently → inapplicable because no migration is added and no table changes"
  - artifact: architecture
    dispositions:
      - "read-role handles open read-only → conforms; the new tool reads through `reader_connection` like every other tool"
      - "a process that does not recognize the schema version refuses to serve → conforms; the new tool takes the `_readable` / `_unusable` path the other windowed tools take"
      - "every writable handle comes from the one writer factory → inapplicable because nothing opens a writer"
      - "no component creates the datastore implicitly → conforms; a missing store answers through `_unusable` and nothing creates one"
  - artifact: security-model
    dispositions:
      - "third-party text is quoted, never followed → conforms; a trade's `description` is the institution's text and is declared third-party on the wire, like `description` and `merchant` on a transaction"
      - "secrets live only in the OS keychain and never reach a log → inapplicable because no credential is read or logged"
      - "log redaction happens at the formatter → inapplicable because no log line is added that carries a credential; the one new refusal message names a caller-supplied type and the types the store holds"
      - "no tracked file carries a credential-shaped string → conforms; the new fixture use reads the existing recorded capture, which the guard already scans"
      - "the aggregator's API is the only network destination → inapplicable because nothing here reaches the network"
  - artifact: project-preferences
    dispositions:
      - "no financial-institution, account or product name from the roster in code, schema, fixtures or anything pushed → conforms; every fixture is synthetic, and this plan names the production connections by their shape only"
partition: serial — every chunk edits `query.py`, `mcp.py` and `mcp_resources.py`, and chunks 02 and 03 both extend the closed warning and tool vocabularies
last_validated: 2026-09-14
lifecycle: completed
archived: 2026-09-21
released_in: v0.2.0
maintained: false
---

> **Archived — no longer maintained.** This plan records what was built, not what will be. Do not edit it to reflect later changes; write those where they are true.

# Build Plan: Investment Activity, End to End, With No False Alarms

## Problem

A production agent session asked for its investment activity and was told it was not available.
The diagnosis, read from the production archive without writing:

- **The data is stored and no tool serves it.** The investment-only connection's
  `/transactions/sync` pages all say `HISTORICAL_UPDATE_COMPLETE` with nothing added. That is its
  real answer: its accounts are investment accounts, and their activity arrives on
  `/investments/transactions/get` instead. 420 trades are stored (buys, sells, dividends,
  contributions, deposits, withdrawals), covering about two years. `api-contract.md` records this
  on purpose: "Counted, never served: no tool returns a trade as a row".
- **Three warnings say something is wrong when nothing is:**
  1. `partial` — "granted history window is not yet known; it is measured when the initial backfill
     completes" rides every answer for the investment-only connection. The backfill did complete.
     `_record_granted_window` measures from the oldest transaction and returns early when there are
     none, so this caveat can never clear.
  2. `gapped` — on a request with `since` and no `until`, the detail says "the window reaches past
     today, and that tail is unanswered". An open `until` resolves to today, so nothing reaches past
     it.
  3. `accounts_without_coverage` on `query_transactions` and `money_summary` names every investment
     account as "DATA NOT PRESENT". Their activity is present, in the other feed.
- **One warning is true but worded as a fault.** `list_accounts` names three accounts with nothing
  recorded in any feed as "DATA NOT PRESENT, never no activity". The feeds that would carry them
  completed. The aggregator gives no signal that tells a quiet account from one the institution does
  not report: on every archived `/transactions/sync` page, `accounts` lists exactly the accounts the
  page touched. So the store cannot say which it is, and it should say that, not imply a fault.

## Three questions

- **Problem:** an agent cannot read investment activity, and it is told about problems that do not
  exist, which buries the ones that do.
- **Success:** the agent can list, filter, page and total every stored trade. It is told where each
  account's activity lives. No warning on a production answer describes a fault the store does not
  have. Verified by running this branch's MCP server read-only against the production store and
  reading every warning on every tool.
- **Out of scope:** matching transfers against investment cash activity (checked: none of the 179
  unpaired transfer-category rows matches an investment cash entry by amount and date); recurring
  detection; any change to the sync, the derivers or the schema; the `gapped` caveat for the cash
  connection, which is true (it granted 549 of 730 days); #126.

## Requirements Confidence

**Medium.** The defects and the data are measured. Open assumptions:

- `[ASSUMPTION: "this connection's transactions backfill is complete" can be read from stored state without trusting the transactions domain's last_success_at, which #126 shows can be stamped by a cursor page at any status | HIGH impact | chunk 01's first step verifies it; if no such state exists, the chunk stops and reports rather than keying on the domain stamp]`
- `[ASSUMPTION: the agent's MCP client delivers the server instructions in full, so one added sentence naming the new tool is read | MED impact | learnings: "measure what the client delivers" — the client cannot be measured from here, so the tool's own description carries the routing too]`
- `[ASSUMPTION: investment_type values are open-ended institution vocabulary, served verbatim and filtered by exact match rather than a closed enum | LOW impact | user can override]`

## Decisions

- `[DECISION: a connection whose transactions backfill completed with no transaction reports granted_history_status "no_transactions_to_measure" and carries no partial caveat; granted_history_days stays null | null is documented as NOT YET MEASURED, and the connection has nothing in that feed that a grant could have cut, so no answer from it can be short; a new status field says which null this is rather than overloading null. Derived at read time, so no schema change, and it clears itself when a transaction arrives and the next complete sync measures the window | user can veto/override]`
- `[DECISION: the new tool is query_investment_transactions — windowed on trade_date, capped at MAX_ROWS, keyset-paged on (trade_date DESC, investment_transaction_id DESC) with its own cursor scheme that refuses a transactions or series cursor, filters since/until/account_id/investment_type, and a totals block per (currency, investment_type, investment_subtype) of count and signed amount | the shape norm makes it its own tool; totals classify rather than filter, so a contribution total is one read, not a sum over pages | user can veto/override]`
- `[DECISION: on the transactions tools, an account with no transaction but with investments-feed data is named under a new request-scoped kind, activity_in_another_feed, whose detail names query_investment_transactions — not under accounts_without_coverage | its activity is present, so "DATA NOT PRESENT" is false; but an empty query_transactions for that account with no warning would read as no activity, so it still needs a signal, and the signal must route rather than alarm | user can veto/override]`
- `[DECISION: accounts_without_coverage keeps firing for an account with nothing in any feed, but when its connection's feeds have completed, the detail says the store cannot tell a quiet account from one the institution does not report, and the guidance says to report "no recorded activity", not missing data or a sync fault | the aggregator provides no coverage signal for a quiet account (measured on every archived sync page), so suppressing the warning would claim a zero nobody measured, and the old wording claimed a fault nobody measured | user can veto/override]`

## Chunks

### Chunk 01 — The two warnings that can never be true

**Type:** code · **Critic mode:** chunk

- Step 0: find the stored state that says a connection's transactions backfill is complete (the
  first assumption). Report it before writing code.
- `get_pipeline_health` rows gain `granted_history_status`: `measured` | `not_yet_measured` |
  `no_transactions_to_measure`. The `partial` "not yet known" caveat fires only for
  `not_yet_measured`.
- `_gapped_detail`: an open `until` is today. Only an explicit `until` after today reaches past it.
- Tests: a connection with a complete, empty transactions backfill carries no `partial` and reads
  `no_transactions_to_measure`. A connection mid-backfill with no transactions still reads
  `not_yet_measured` and still carries it. A transaction arriving moves it to `measured` after the
  next measurement. `since` with no `until` inside coverage gets the "wholly inside" sentence; an
  explicit future `until` keeps the "reaches past today" sentence. Each guard seen red with its fix
  reverted.
- Artifacts: `api-contract.md` pipeline row fields; the `partial` guidance in `mcp_resources.py`;
  the `get_pipeline_health` description ("a granted history window of null means NOT YET
  MEASURED").

### Chunk 02 — `query_investment_transactions`

**Type:** code · **Critic mode:** chunk

- New `query_investments.py`, beside `query_holdings.py` and `query_balances.py`. Rows from
  `investment_transactions` left-joined to `securities` and `accounts`, `removed_at IS NULL`.
- Row fields (all required, nullable where the store is): `investment_transaction_id`,
  `account_id`, `account`, `trade_date`, `investment_type`, `investment_subtype`, `security_id`,
  `security_name`, `ticker`, `security_type`, `quantity` (exact text), `price_minor_units`,
  `fees_minor_units`, `amount_minor_units`, `currency`, `description` (third-party), and the
  lifecycle row fields.
- Window resolved against the stored trades' own span (a new `WindowSeries`), capped, keyset cursor
  with its own scheme and a request fingerprint over every filter.
- `totals`: per `(currency, investment_type, investment_subtype)`, `transactions` and
  `amount_minor_units`, over the whole matching set, not the page.
- Registered in `_tool_definitions()` with a strict output schema; the `_dispatch_tool` branch;
  the tool table in `api-contract.md` and `docs/system-requirements.md` §5 in the same commit (the
  surface guard enforces it); the envelope reference resource names the new fields and totals.
- Tests: rows and totals over a synthetic store; removed rows excluded; window clamp to the trades
  span; paging walks every row exactly once; a transactions cursor and a series cursor are refused;
  an unknown `account_id` is refused like the other tools; an unreadable store answers through
  `_unusable`; property test: every page's rows lie inside `effective_window`, and walking all pages
  yields the totals' count.

### Chunk 03 — Warnings that route, and a surface that says what it serves

**Type:** cumulative-final · **Visual change:** yes

- New request-scoped kind `activity_in_another_feed` in `envelope.REQUEST_SCOPED_KINDS`, its
  guidance, and its row in the warnings table. `query_transactions` and `money_summary` name an
  account under it when it has no transaction and has trades or a holdings capture, and under
  `accounts_without_coverage` only when it has neither.
- `accounts_without_coverage` detail and guidance: the complete-feeds wording from the fourth
  decision, on every tool that emits it.
- Sweep every "never served" / "recorded apart from" claim (measured before building: 10 lines in
  `api-contract.md`, `mcp.py`, `mcp_resources.py`, `query.py`, `tests/test_mcp.py`,
  `tests/preferences/verify_norms_go_red.py`) to name the tool; server instructions gain one
  sentence naming it; `get_coverage_report` and `list_accounts` descriptions point at it.
- `gapped` on an answer read from another series (trades, balances) says the shortfall limits the
  connection's transactions and does not affect this answer, instead of phrasing it against a window
  that was never clamped to the grant. Found verifying chunk 02 against production.
- Carried from chunk 02's review (`rev-20260915T005910Z-e9b5e129`): the trades tool's refusals are
  tested through the server (unknown type, non-text type); the paging invariant is walked over
  varied scopes, not only page sizes; the set of connections holding transactions is read only when
  a connection could need it.
- Verification: this branch's `bankmachine mcp` against the production store, read-only. Call every
  tool with defaults and with an investment `account_id`. Read every warning, and confirm the only
  ones left describe true facts (the cash connection's grant shortfall, pending holds, unpaired
  transfers, and the accounts with nothing recorded, in the new wording). Enqueue a VRF entry for
  the operator to confirm the same in the desktop client after the release reaches production.

## Status

- [x] Chunk 01 — The two warnings that can never be true
- [x] Chunk 02 — `query_investment_transactions`
- [x] Chunk 03 — Warnings that route, and a surface that says what it serves
