# MCPlaid — System Requirements v2

**Layer:** the engine. **Companion:** a deployment-requirements document (one operator's roster),
which is deliberately not in this repository — `deployment-requirements.template.md` is its shape
and says where the filled-in copy goes.
**Date:** 2026-09-05 (v2 — provider-agnostic split) · **Status:** ready to plan

> **Supersedes** a v1 acceptance-criteria document that split into two: this one, holding the
> engine, and a deployment-requirements document holding the roster. Every v1 criterion lands in
> one of the two, generalized or instantiated, except one — a pre-enrollment entity check that
> the roster explicitly superseded by deciding not to enroll that connection at all.

---

## 0. Purpose and success condition

A locally-hosted, permanently-running service that pulls an operator's financial data from an
aggregator API and from file imports into an encrypted local datastore, and exposes it read-only
over MCP.

**Done when:** an analyst agent can ask a period-over-period question — *"what did I spend on
restaurants in Q2 2025 vs Q2 2026"* — and get a correct answer without a browser or a CSV in the
loop, **and** can independently establish that the underlying data is complete and fresh before
answering.

**Not about** financial planning, budgeting logic, or advice. It is a data pipeline. Analysis
happens downstream, after the verification gate in §7 passes.

### 0.1 The layering rule

This document describes a system that **knows nothing about any particular financial institution.**
No **financial-institution, account, or financial-product name** appears in this document, in the
code, or in the schema.

The **aggregator is expressly carved out** and is not what this rule governs: it is a single named
dependency in v1 (see the scope note below), so its client package, the keychain service name, and
the product name may name it. The rule binds *roster* identity.

The set of institutions an operator actually connects — which ones, in what order, which need file
import instead of the aggregator, which carry special rules — lives in the operator's own
deployment-requirements document and is expressed as **configuration and adapters**. 🔴 That
document is **not** version-controlled here: a roster names institutions and usually balances, and
this repository is a general-purpose tool. `deployment-requirements.template.md` carries the shape
and the contract; `scripts/check-no-personal-data.sh` enforces the boundary on every push.

**AC-0.1** — 🔴 Every requirement in the deployment-requirements document must be satisfiable by
configuration plus an adapter, with **zero change to this system.** A deployment requirement that
cannot be met that way is a gap in *this* document, and must be fixed here rather than special-cased
there.

**AC-0.2** — Institutions and accounts are added and removed over the system's life. Neither is a
code change, and removal never destroys history (see FR-6).

**AC-0.3** — An automated check asserts that no name from the deployment roster appears under the
source root. A second, wider check — `scripts/check-no-personal-data.sh`, wired into `pre-push` —
asserts the same over **every tracked file**, because this repository may be published and the
leak this project actually had was in documentation, not in code. 🔴 **The roster config carries explicit per-entry match tokens** and the check matches
*those*, on word boundaries, over the source and schema roots only. Matching on roster labels
directly fails both ways: a label is the institution's own spelling, so a shortened token in code
slips past a literal match, while tokenizing a label collides with unrelated legitimate text (a
hardware or OS name that happens to share a word). Either failure ends with someone weakening this
norm's own test on its first red run.

> **Scope note on "provider".** In this document *provider* means a financial institution. The
> **aggregator** — the API vendor through which institutions are reached — is a single named
> dependency in v1. §3 describes the connector boundary so a second aggregator is possible later,
> but v1 builds exactly one implementation. Widening this is a scope decision, not a refactor.

---

## 1. Architecture

```
aggregator API ─┐
                ├─> sync daemon (scheduled, daily) ─> encrypted datastore ─> MCP server ─> MCP client
file imports  ──┘                                                                              │
                                                                                 (proxied to analyst agent)
```

- **Runtime:** Python 3.11+, native on macOS (Apple Silicon). Not containerized.
- **Datastore:** SQLCipher (Community Edition), single file. Key held in the OS keychain.
- **Repo:** a git repo. Data, logs, and secrets are gitignored.
- **Scheduling:** an OS-level user agent, daily. Survives reboot; requires no open terminal.
- **MCP transport:** stdio, registered in the MCP client's config.

**AC-ARCH.1** — A fresh clone plus documented setup steps produce a working sync on a clean machine,
with no undocumented manual steps.

**AC-ARCH.2** — The scheduled job runs daily, logs to a configured log directory, and **recovers from a
missed run** (machine asleep) on next wake rather than skipping the window.

**AC-ARCH.3** — The MCP server starts successfully when the datastore is empty or missing, and reports
that state through `get_pipeline_health` rather than crashing.

**AC-ARCH.4** — 🔴 **No filesystem path is hardcoded.** The datastore path, log directory, and config
location are configuration values with documented defaults. The repo location is not assumed by
any code path.

**AC-ARCH.5** — The datastore is encrypted at rest with page-level encryption, so that a file-copy
backup is ciphertext without further work. Encryption is verified by a test asserting that a known
plaintext written through the schema is not recoverable from the raw file bytes.

**AC-ARCH.6** — Because page encryption breaks ad-hoc SQL tooling, the system ships `sync shell` — an
authenticated REPL against the datastore. 🔴 **Built in step 1, not step 9:** it is the primary
debugging affordance for every step in between, most of all the §7 gate.

**AC-ARCH.7** — The sync writer and the MCP reader are separate processes over one datastore file, and
may run concurrently. Journal mode, locking behavior, and reader isolation under encryption are
specified in the system architecture, not left to whichever component encounters them first. The
MCP server must not depend on sync liveness, nor the reverse.

---

## 2. Non-goals

Do not build these; do not let them expand the surface area.

- **Any write path to an aggregator or an institution.** Read-only, permanently.
- **Budgeting, forecasting, categorization rules engines, or advice.** Downstream concerns.
- **A web UI.** MCP and the CLI are the only interfaces.
- **Multi-user or partner access.** Single operator.
- **Aggregator webhooks.** Requires a publicly reachable endpoint. Poll for v1; leave a clean seam.
- **Automating any institution the aggregator does not serve.** The system *tolerates* such accounts
  through the import path (FR-7); it never special-cases them.

---

## 3. Aggregator integration

### FR-1 · Enrollment

**AC-1.1** — A CLI command creates a link token and prints a hosted enrollment URL. No local web
server, no frontend.

**AC-1.2** — 🔴 **The link token MUST request the maximum available transaction history window.**
The requested window is configuration with a documented maximum, and **cannot be raised after
enrollment** without removing and re-linking the connection. This is the single highest-stakes
parameter in the system: a build that ships with the vendor default has failed.

**AC-1.3** — On success the public token is exchanged for an access token, persisted per §6, and the
connection is recorded with its institution identifier, enrollment timestamp, and **the history
window actually granted** — which may be less than requested.

**AC-1.4** — Enrollment is idempotent: re-running for an already-enrolled institution updates rather
than duplicating the connection.

**AC-1.5** — The system refuses to exceed the configured plan connection cap, explains the limit, and
lists current connections so one can be removed. 🔴 **The cap is configuration, not a literal** —
plan tiers change.

**AC-1.6** — A connection can be **retired**: removed from active sync without deleting its history
(see AC-6.5).

### FR-2 · Transaction sync

**AC-2.1** — Cursor-based incremental sync, looping until the source reports no more pages,
persisting the cursor **per connection, transactionally with the data it accompanies.**

**AC-2.2** — Applies all three change types: added, modified, removed. A removed transaction is
**soft-deleted** (retained with a removal timestamp), never hard-deleted.

**AC-2.3** — Pending→posted transitions do not create duplicates. A posting transaction updates the
pending row, matched on the source's pending identifier.

**AC-2.4** — Sync is **idempotent**: a second consecutive run produces zero net changes. Automated
test, not a manual observation.

**AC-2.5** — A crash mid-sync does not advance the cursor. The next run resumes without data loss.
Tested by killing the process mid-pagination.

**AC-2.6** — The initial historical pull is handled gracefully: a not-yet-ready response triggers
backoff-and-retry rather than failure, because a multi-year backfill takes time to materialize.

### FR-3 · Balances and investments

**AC-3.1** — Daily capture of account balances into a time-series table. **Never overwrite; append.**
Net-worth-over-time depends on this and no aggregator backfills it.

**AC-3.2** — 🔴 **Investment holdings and transactions are pulled for any connection whose recorded
capabilities include investments** — never for a named institution. Capabilities are discovered at
enrollment and stored per connection. Securities live in their own table, referenced by ID.

**AC-3.3** — Investment pulls request the full configured window where supported, and **record the
actual date range returned**, so shortfalls are visible rather than silent.

**AC-3.4** — 🔴 **Liability accounts (loans, lines of credit) are covered by account type, balance,
and transactions — the same path as any other account.** Liability-*product* detail (APR, minimum
payment, payoff date, statement schedule) is **explicitly out of scope for v1**, and no liabilities
capability or table is specified. This is a recorded descope, not an omission: a debt's contribution
to net worth and cashflow needs only its balance and its transactions, both of which the general
path supplies. Adding liability detail later is a new capability under AC-3.2's model, not a
redesign.

### FR-4 · Connection health and re-auth

**AC-4.1** — Auth-required, locked, institution-down, and rate-limit errors are caught **per
connection.** One broken connection must never abort the sync for the others.

**AC-4.2** — A degraded connection is recorded with its error code **and the timestamp of its last
successful sync.**

**AC-4.3** — A repair command produces an update-mode enrollment URL that re-authenticates a broken
connection **without losing its history or cursor.**

**AC-4.4** — Degraded state is visible through `get_pipeline_health`. 🔴 **Silent staleness is the
primary failure mode of this entire system** — a connection that quietly stopped three weeks ago
produces confidently wrong analysis. Loud failure is a hard requirement.

**AC-4.5** — A degraded-state record without a last-successful-sync date is **insufficient**: the
size of the resulting data hole must be computable, not guessed.

---

## 4. Data model

### FR-5 · Raw preservation

**AC-5.1** — Every aggregator API response is persisted verbatim (compressed JSON) with endpoint,
timestamp, connection ID, and a hash, **before any normalization.**

**AC-5.2** — Normalized tables are rebuildable from raw responses alone, via a documented rebuild
command.

**AC-5.3** — 🔴 **The derivation logic carries a version, and that version is recorded in the
datastore** alongside the rows it produced. Losslessness is only well-defined relative to a recorded
derivation version — without it, an upstream taxonomy change silently breaks an invariant that is
supposed to be permanent.

*Rationale: a bronze/silver split. A categorization bug becomes a re-run rather than a re-fetch, and
lineage back to source is preserved. Disk cost is trivial at this volume.*

### FR-6 · Core schema

Minimum tables: `connections`, `institutions`, `accounts`, `transactions`, `balances_daily`,
`securities`, `holdings`, `investment_transactions`, `sync_state`, `raw_responses`, `account_rules`,
`manual_imports`, `derivation_versions`.

**AC-6.1** — Transactions retain the source's own category fields **and** carry a separate nullable
override column. **Never overwrite a source value in place.**

**AC-6.2** — 🔴 **All monetary values stored as integer minor units.** No floats anywhere in the
schema or in aggregation code.

**AC-6.3** — Every account carries a **stable local ID that survives re-enrollment** of its
connection, so history is not orphaned when an institution is re-authenticated.

**AC-6.4** — Timezone handling is explicit: transaction dates stored as dates, sync metadata as UTC
timestamps. The two are never mixed.

**AC-6.5** — 🔴 **Accounts carry a lifecycle** — active or inactive, with an opened/first-seen date
and a closed/retired date. A retired account's dormant period must not read as a permanent coverage
gap, and retiring an account never deletes its history.

**AC-6.6** — Institution identity is data. No table, column, enum, or code path encodes a specific
institution's name or behavior.

### FR-7 · Import seam

**AC-7.1** — A documented file-import path writes into the same normalized tables with a manual
source marker, for any account the aggregator cannot reach.

**AC-7.2** — 🔴 **Import formats are pluggable adapters selected by configuration**, never a
hardcoded branch per institution. Adding a format is adding an adapter plus a config entry.

**AC-7.3** — Adapters are verified against **real exported sample files**, not hand-written fixtures
that encode our assumptions about the format.

**AC-7.4** — Manually imported rows are distinguishable from aggregator-sourced rows in **every** MCP
query result. Provenance is never lost.

**AC-7.5** — Import is idempotent: re-importing the same file produces zero net changes. Overlapping
files (e.g. per-statement exports covering a shared boundary) must not duplicate rows.

### FR-8 · Account rules

Some accounts need their transactions interpreted differently from the default. This is a **typed,
data-driven rule engine**, not query-logic special cases.

**AC-8.1** — 🔴 **Rules are encoded as data in `account_rules`**, never hardcoded in query logic, and
never keyed to a named institution.

**AC-8.2** — The v1 rule type is **`contribution_only`**, for an account the operator funds but does
not solely own (a shared or household account). Its semantics: inbound credits originating from the
operator's own accounts surface as a single expense category; **all outbound spending from that
account is excluded** from the operator's personal spending aggregates by default.

**AC-8.3** — 🔴 **Excluded rows are filtered, never deleted** — and any MCP aggregate that applied a
rule **must say so in its response**, so an exclusion can never be silently forgotten during
analysis.

**AC-8.4** — A `contribution_only` account is configured with the set of accounts whose inbound
transfers count as contributions. An inbound credit from **outside** that set is an **anomaly to
surface via `get_pipeline_health`**, not something to silently classify.

**AC-8.5** — The rule engine is extensible: adding a rule type is a code change, but applying any
existing rule type to any account is configuration.

---

## 5. MCP tool surface

🔴 **Read-only. No mutation tools. No exceptions.**

**AC-9.1** — **Aggregate-first.** Tools return computed summaries by default, not raw rows. Dumping
24 months of transactions into an LLM context is slow, expensive, and worse at arithmetic than SQL
is. Raw-row access exists but is paginated and hard-capped (suggested 500 rows).

Required tools:

| Tool | Returns |
|---|---|
| `get_pipeline_health` | Per-connection sync status, last success, error codes, per-account coverage window, row counts, staleness flags, rule anomalies |
| `list_accounts` | Accounts with type, institution, mask, current balance, lifecycle state |
| `query_transactions` | Filtered rows (date range, account, category, amount range, merchant search). Paginated, capped |
| `spending_summary` | Aggregated by category/merchant/account over a period, with period-over-period comparison |
| `cashflow_summary` | Income vs outflow by month |
| `balance_history` | Balance time series per account or aggregate |
| `net_worth` | Assets minus liabilities over time, investments included |
| `list_holdings` | Current investment positions with cost basis where available |
| `find_recurring` | Detected recurring charges with cadence, amount drift, last-seen |
| `get_coverage_report` | Per account: first and last transaction date, gaps >7 days, source breakdown |

**AC-9.2** — Every tool response includes a **freshness stamp** — last successful sync per
contributing account.

**AC-9.3** — 🔴 Any response computed over data with a known gap, a degraded connection, or a
bounded-history account **must carry an explicit warning field.** Analysis over incomplete data must
be impossible to do accidentally. Warnings distinguish at minimum: *stale*, *degraded*, *gapped*,
*partial*, *rule-applied*.

**AC-9.4** — Tool descriptions state **units** (minor units), **sign conventions** (is a debit
positive or negative?), and **whether any account rule was applied.** Ambiguity here produces wrong
analysis that looks right.

**AC-9.5** — Coverage is reported **per account, never per institution.** One institution may hold
many accounts with different coverage windows, and an institution-level summary hides that.

---

## 6. Security

**AC-10.1** — Aggregator credentials, all access tokens, **and the datastore encryption key** live in
the OS keychain, accessed at runtime. Never in the repo, never in a plaintext dotfile, never in
shell history, never in a log line.

**AC-10.2** — `.gitignore` covers data, logs, and any credential path. Verified by a test that greps
a fresh `git ls-files` for token-shaped strings.

**AC-10.3** — Logs redact access tokens and account numbers. Account masks (last 4) are acceptable.

**AC-10.4** — No telemetry, no analytics, no error reporting to third parties. The **only** network
destinations are the aggregator's API hosts.

**AC-10.5** — The MCP server opens no network sockets and reads only its own datastore file.

**AC-10.6** — Sandbox vs. production is an **explicit config flag, logged loudly at every startup.**
Syncing fixture data into the real datastore must be hard to do by accident.

---

## 7. Verification gate

🔴 **No financial analysis begins until every check below passes.** This gate is the deliverable, not
a formality — the failure mode being guarded against is confident analysis of quietly incomplete
data.

**AC-11.1 · Coverage** — Every aggregator-linked account shows continuous coverage from enrollment
back to the full history window the institution actually supplied, with all gaps >7 days enumerated
and explained. Genuine no-activity periods are fine but must be identified as such; a retired
account's post-closure period is not a gap.

**AC-11.2 · Reconciliation** — For each account, the balance derived by summing transactions matches
the reported current balance within a documented tolerance. Discrepancies are **itemized, not
averaged away.**

**AC-11.3 · Deduplication** — Zero duplicate transactions, tested across the pending→posted
transition, across a re-sync, and across overlapping file imports.

**AC-11.4 · Idempotency** — Two consecutive syncs produce zero net changes. Automated.

**AC-11.5 · Rebuild** — Rebuild from raw responses reproduces the normalized tables
**byte-identically, given a recorded derivation version** (AC-5.3).

**AC-11.6 · Account inventory** — The set of accounts in the datastore is reconciled against an
**operator-supplied expected inventory.** Unrecognized accounts are a finding, not noise.

**AC-11.7 · Independent spot-check** — At least 10 transactions verified by eye against the
institution's own statement. Automation can be wrong in ways that are internally consistent.

**AC-11.8 · Shortfall recording** — Where the aggregator returned **less history than requested**,
the delta is recorded explicitly as a known gap rather than the returned window being treated as
complete.

---

## 8. Build sequence

1. Schema, migrations, raw-response layer, encrypted datastore, **and `sync shell`**
2. Aggregator client against sandbox fixtures; full test suite
3. Enrollment flow — 🔴 **verify the granted history window on ONE real connection before enrolling
   any others.** Getting this wrong means re-linking every institution.
4. Transaction sync with cursor persistence; idempotency and crash-recovery tests
5. Balances and investments (capability-driven, per AC-3.2)
6. Connection health, error handling, update-mode repair
7. MCP server, aggregate-first tools
8. Scheduling and logging
9. Verification gate (§7) — run it, publish the report
10. Import adapters (FR-7)

---

## 9. Open questions (system layer)

1. **Retention on raw responses.** Keep indefinitely (recommended — volume is trivial and it is the
   system of record) or prune after N months?
2. **Aggregator pluggability.** v1 builds one aggregator implementation. Is a second ever expected?
   The answer changes how hard the connector boundary is drawn.
3. **Project name.** `MCPlaid` is a working name. Cost of renaming rises once package names, the
   scheduler label, and the keychain service name are fixed.

Deployment-layer open questions live in the operator's own deployment-requirements document, and
so are not listed here — see `deployment-requirements.template.md` §6 for how they are tracked.
