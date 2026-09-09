# bankmachine — System Requirements v2

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
and the contract; `tests/preferences/check-no-personal-data.sh` enforces the boundary on every push.

**AC-0.1** — 🔴 Every requirement in the deployment-requirements document must be satisfiable by
configuration plus an adapter, with **zero change to this system.** A deployment requirement that
cannot be met that way is a gap in *this* document, and must be fixed here rather than special-cased
there.

**AC-0.2** — Institutions and accounts are added and removed over the system's life. Neither is a
code change, and removal never destroys history (see FR-6).

**AC-0.3** — 🔴 **Roster identity is matched by explicit per-entry tokens carried in the roster
config**, on word boundaries — never by matching institution labels directly. Matching labels fails
both ways: a label is the institution's own spelling, so a shortened token in code slips past a
literal match, while tokenizing a label collides with unrelated legitimate text (a hardware or OS
name that happens to share a word). Either failure ends with someone weakening this norm's own test
on its first red run. A token that cannot match is worse than a missing one, because it still counts
toward a reassuring total — so tokens are **validated at load and rejected loudly**, never silently
normalized into something unmatchable.

**AC-0.4** — Two automated checks apply that token rule at two different scopes, and the scopes are
stated separately because they are not the same guarantee:

- **Source and schema roots** — asserts no roster name reaches the code. This is the engine-level
  norm, and it is what a build regression would trip.
- **Every commit being pushed** — asserts no roster or operator identity reaches a remote, over the
  *commits* rather than the working tree. This is wider on purpose: the exposure this project
  actually had was documentation sitting in already-pushed history behind a clean tip, which a
  worktree or tip-only check reports clean. `tests/preferences/check-no-personal-data.sh`, wired into
  `pre-push`, is this one.

🔴 Both **fail closed.** An error condition — an unreadable token file, a regex the engine rejects,
a missing script — must abort, never report clean: a check whose only bad-news channel is the
absence of output cannot report that it stopped checking.

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

> **Resolved** in `.prawduct/artifacts/architecture.md`, whose Data Ownership & Consistency section
> is this criterion's answer: WAL journal mode, a process-level `flock` serialising writer-role
> runs, and a `query_only` reader that releases its snapshot every tool call. Four of its rules are
> ratified norms in that artifact's Direction section. The claims were measured against this
> platform's SQLCipher build rather than taken from documentation.

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
window requested**.

**AC-1.3a** — 🔴 **The granted history window is recorded when it first becomes observable, which is
not at enrollment.** No enrollment-path response reports it, so `granted_history_days` is null on a
newly enrolled connection and is written once the initial backfill reveals the oldest transaction
the aggregator actually returned. A null granted window means *not yet known*, and must never be
read as *no shortfall* — AC-11.8 computes the shortfall only once this is set.

> **Amendment (2026-09-07, build step 3 planning).** AC-1.3 previously required enrollment to record
> "the history window actually granted." It cannot: the aggregator's enrollment path does not carry
> that value anywhere. Verified against the pinned SDK rather than the vendor docs —
> `/link/token/create` returns only `link_token`, `expiration`, `request_id`, `hosted_link_url`,
> `user_id` (and `api-notes-plaid.md` §11 already recorded that it does not echo the request);
> `/link/token/get`'s metadata carries only `initial_products`, `webhook`, `country_codes`,
> `language`, `redirect_uri`, `client_name`, `institution_data`, `account_filters`; and `/item/get`'s
> `Item` carries no window field at all. The requirement as written was unsatisfiable by any
> implementation, so it is split rather than quietly under-delivered: enrollment records what it can
> know (requested), and AC-1.3a homes the granted window where it is first knowable. **This moves
> when a value is recorded, not whether** — the shortfall AC-11.8 exists to make visible is
> unchanged, and null is given an explicit meaning so the gap cannot be read as its absence.
>
> The schema had already assumed this. `store/migrations/core_schema.py` has carried the comment
> *"`granted_history_days` is nullable because AC-1.3 records what the source actually gave, which is
> not known until the first sync returns"* since build step 1 — so the DDL and AC-1.3 have disagreed
> from the day both existed, and every reader who reached the column believed the schema. This
> amendment settles the disagreement in favour of the one that was right, rather than discovering it
> as a defect during the build.

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

> **Amendment (2026-09-09).** This lifecycle has never had a transition. `lifecycle_status`
> is written once, to `active`, by the accounts deriver, and no code path and no operator
> command has ever set it to `inactive`; `closed_date` has zero readers and zero writers in
> `src/`, and the coverage report AC-11.1 audits reads neither column. **FR-9's AC-12.4–12.6
> supply the transition.** See `.prawduct/artifacts/discovery-account-lifecycle.md`.

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


### FR-9 · Account lifecycle, made visible

*Derived in `.prawduct/artifacts/discovery-account-lifecycle.md` (#40). Every criterion below was derived in the cited discovery document, which records the evidence, the alternatives weighed, and its assumptions as **vetoable**. They are in force as requirements; an assumption the owner rejects retires the criteria that rest on it.*

🔴 **The treatment ruling was taken by the owner on 2026-09-09: a total over account balances
INCLUDES non-active accounts and flags them**, stating the magnitude they contributed. The
alternative — excluding them and stating the excluded magnitude — was the recommendation on file
and was not taken; the argument on both sides is preserved in the discovery document. The ruling
follows #18's *classify, do not filter*: an overcount gets questioned and an undercount gets
believed, and a net worth that drops with no visible cause is the quieter of the two errors. What
makes the inclusion safe is the second half, which is not optional — the magnitude rides the
payload, so the reader can perform the subtraction the system refuses to perform for them.

**AC-12.1 · Lifecycle rides every account row.** 🔴 Every `list_accounts` row carries the lifecycle
fields — always present, never behind a parameter, on every row. *Why:* the same argument #19 settled
for coverage. The consumer this fails is the agent that never thought to ask the verification
surface, and a field a caller must opt into is a field that caller still does not have. AC-9.5's
per-account rule and `api-contract.md` § Direction's second norm both land here.

**AC-12.2 · The vocabulary names the observation, never the conclusion.** The lifecycle value is
drawn from a closed set published as an `enum` on the tool's `outputSchema`, and no value asserts a
closure the aggregator did not report. The value meaning *"the institution stopped listing this
account"* is named for that observation and its description says, in the payload, that it is
consistent with closure, with de-selection from sharing, and with the institution changing what it
shares. *Why:* the aggregator reports no closure signal (§ *What the connector can actually know*),
so a value named `closed` computed from absence would be a confident wrong number of exactly the
class this surface exists to refuse.

**AC-12.3 · The evidence rides beside the verdict, as dates rather than as a flag.** Every row
carries the dates the lifecycle value was computed from, present and nullable, never absent. *Why:*
the `silence_ratio` precedent, ruled on this same surface — a number lets a reader see a borderline
case and a boolean is what destroys that. A consumer must be able to re-derive the verdict from the
row without a second call.

**AC-12.4 · The roster observation is recorded — per account AND per connection — so the state is
reachable at all.** Each successful roster derivation records two things: per account, the date that
account was last listed, as a **monotone maximum** (the mirror of `first_seen_date`'s minimum); and
per connection, the date the roster itself was successfully observed. *Why:* the per-account half is
the requirement #40 says does not exist — without a recorded observation there is no non-`active`
state for the read path to read, and a min/max pair is what makes the record order-independent under
archive replay.

🔴 **The per-connection half was added by amendment on 2026-09-09, reversing this criterion's
original choice to DERIVE it.** The original derived the connection's observation as the maximum over
its own accounts' dates, and that derivation is what AC-12.5's whole-roster clause rested on. The
argument for deriving was real and is preserved rather than deleted: a derived value cannot
disagree with the rows it is computed from, and it needs no column, no migration and no write path.
**It was reversed because a derived maximum cannot express the one fact AC-12.5 needs at every
scale** — *the roster was observed, and this account was not in it*. A maximum taken over the
accounts that were listed moves with them, so when none are listed there is nothing behind it, and
the absence becomes inexpressible exactly when it is real. Storing the observation separates
"we looked" from "here is what we found", which is the distinction the whole criterion turns on.
`connections.last_success_at` is not that fact: it records a sync attempt succeeding, not a roster
being read, and the two diverge whenever a sync succeeds without a roster call.

**AC-12.5 · Absence is measured within one connection, against the RECORDED observation, and never
against silence — and it holds at every roster size.** An account is absent when the connection's
recorded roster observation is later than that account's own last-listed date. A connection whose
roster could not be fetched records no observation and therefore marks nothing absent. An account
with no connection — the FR-7 import-only path — has no roster to be absent from and is never marked
absent. *Why:* each clause removes a way for a pipeline failure to be reported as a household event,
and measuring against a recorded observation rather than a derived maximum is what makes the measure
independent of how many accounts a connection has.

🔴 **The whole-roster clause is REPLACED by this amendment, not annotated.** It read: *a connection
whose entire roster is absent marks nothing absent*, justified by scale — fourteen simultaneous
closures is not a thing that happens, so a vanished roster is a pipeline failure and belongs to
`get_pipeline_health`. **That argument does not survive at one account.** One account closing is
entirely ordinary and it IS the whole roster, so the clause made a single-account connection report
`active` indefinitely beside a frozen balance — the exact failure FR-9 exists to remove, surviving
inside FR-9. The argument also weakened continuously as the roster shrank and never said where it
stopped, which is the tell that it was a heuristic standing in for a missing fact rather than a
criterion.

🔴 **What replaces it does not discard the old clause's insight; it stops collapsing two facts into
one answer.** A successfully observed EMPTY roster now yields **both**:

1. **Every account on that connection is marked absent.** This is the account-level truth and it is
   what a reader of `list_accounts` needs, because the balance beside each one froze on the day it
   was last reported.
2. **The connection raises `roster_observed_empty`.** This is the connection-level anomaly — the
   thing the old clause was really trying to say — and it is what distinguishes fourteen closures
   from a broken feed.

The old criterion had only one channel, so it had to choose, and it chose to suppress the
account-level truth to avoid publishing the connection-level lie. With two channels there is nothing
to trade: a consumer sees fourteen frozen balances *and* sees that the roster came back empty, and
can tell which story it is. Suppressing (1) is what produced the N=1 failure; publishing (1) without
(2) would produce the mass-closure lie the old clause correctly feared.

**AC-12.5a · An empty roster is a successful observation of zero accounts, and is recorded as one.**
A roster response listing no accounts advances the connection's recorded observation and raises
`roster_observed_empty`. It is never treated as a failed fetch, **and it is surfaced by
`get_pipeline_health` as well as on the answer that draws on it.** *Why:* the two are genuinely
different events and only the caller can tell them apart — an operator who de-selected every account
produces a truthful empty roster, and a broken feed produces a lie — so the pipeline records what it
saw and says the shape is anomalous, rather than guessing which it was. The state that must not
exist is the third one this replaces: a roster response that type-checks, runs a loop zero times,
and records a successful observation of nothing with no signal at all.

🔴 **The `get_pipeline_health` clause is not the option that was rejected.** The ruling of 2026-09-09
rejected `get_pipeline_health` as the *only* home for this signal, because a consumer reading
`list_accounts` would then see `active` beside a frozen balance and never learn otherwise — an agent
cannot see a caveat that is not in the payload. It did not reject the surface. An empty roster IS a
connection-level anomaly and the verification surface is where those live; what the criterion refuses
is making the operator call it to find out something the answer itself should have said.

**AC-12.6 · The operator's declaration outranks the derived signal, and a rebuild never undoes it.**
Where a stored `lifecycle_status` and the derived observation disagree, the stored value is
authoritative and the observation still rides the row as evidence. *Why:* `_OPERATOR_OWNED` already
declares the intent and `accounts` is not a rebuildable table (it carries no `derivation_version_id`,
so `rebuild.py`'s classification never empties it), so the property is nearly free — but it is only
*true* once something can set the column, and it must be asserted rather than assumed.

**AC-12.7 · A retired account's silence is identified as closure, not reported as a hole.**
`get_coverage_report` reads the lifecycle state, and a non-active account's trailing silence is not
reported as a coverage finding. *Why:* this is `data-model.md` § Account lifecycle's existing 🔴 rule,
verified unmet in shipped code (§ *Premise verification*), and AC-11.1's *"a retired account's
post-closure period is not a gap"* says the same thing from the verification gate's side. One
producer serves both tools, as `_account_coverage` already does — built twice they can disagree, and
a verification surface that contradicts the analysis surface is worse than one that is absent.

**AC-12.8 · A total over account balances includes non-active accounts, and never without stating
what they contributed.** 🔴 *Ruled by the owner 2026-09-09: include and flag.* A response carrying a
count or a sum over accounts includes non-active accounts in the figure, states that it did, and
states the magnitude they contributed — a count and a signed sum, both always present and zero
rather than absent when none qualify. The answer also carries `account_no_longer_active`. The
envelope's own `coverage.accounts` count — an unfiltered `COUNT(*)` over `accounts` in
`query._coverage` when this was written — is covered by this clause: it keeps counting every account
and gains the non-active figure beside it.
*Why:* both failure modes are quiet. Silently including a frozen balance is #40's filed bug; silently
excluding one is a net worth that drops with no visible cause, which is #18's ruling arriving from
the other side. Stating the treatment is what both rulings have in common, and it is why this
criterion can be ratified before the ruling is.

**AC-12.9 · The transition is exercised by a fixture in which a roster shrinks, and the assertion is
seen red.** A test replays two archived roster observations for one connection, the second listing
one fewer account, and asserts the account's lifecycle changes and its evidence dates are what the
observations imply. *Why:* two reasons, and the second is the one worth recording. First, this repo's
`verify_norms_go_red.py` discipline — an assertion never seen fail is not evidence. Second, and
unlike this document's sibling preconditions: 🔴 **#40 is testable in-repo.** #22 needs a real
settlement cycle and #23 needs a real deposit, which is why both were deferred; a shrinking roster is
just two archived responses, and the sandbox's inability to *produce* one is not an inability to
*replay* one. This is what makes #40 the buildable member of the remaining three, and the claim
should be pinned by a test rather than left as an assertion in a discovery document.

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
| `money_summary` | Money in and out over a period, grouped by category, merchant, account, month or flow class, per currency |
| `balance_history` | Value over time, per account or aggregated as net worth, investments included |
| `list_holdings` | Current investment positions with cost basis where available |
| `find_recurring` | Detected recurring charges with cadence, amount drift, last-seen |
| `get_coverage_report` | Per account: first and last transaction date, gaps against the account's own cadence, source breakdown |

*(Build status 2026-09-09: `get_pipeline_health`, `list_accounts`, `query_transactions`,
`money_summary` and `get_coverage_report` are implemented; `balance_history`, `list_holdings` and
`find_recurring` are not yet built, and the descope — including what the shipped tools do not yet
carry — is recorded in `.prawduct/artifacts/api-contract.md`.)*

> 🔴 **`list_accounts`'s "lifecycle state" has never been on the wire (2026-09-09).** It is
> named in the row above and in `api-contract.md`'s equivalent table, and it was never built.
> `test_the_documented_tool_surface_is_the_built_one.py` compares tool **name** sets only, so a row
> *field* promised in two contracts and never delivered is invisible to every guard in this repo.
> **FR-9's AC-12.1–12.3 build it.** The reusable lesson is the gap rather than the field: nothing
> here checks the shape of a row against what the contracts say it carries.

> **Amendment (2026-09-09, ratified by `api-contract.md` § Direction's fourth norm).** This table
> went from ten tools to eight. 🔴 **A tool's boundary is drawn where the answer *shape* changes,
> never where the question changes**, and two pairs here were near-twins by that test:
> `spending_summary` and `cashflow_summary` are one grouped aggregate — cashflow-by-month is that
> aggregate grouped by month with inflows kept — and they merge into **`money_summary`**;
> `net_worth` is `balance_history` aggregated across accounts, and merges into it.
>
> 🔴 **Recorded HERE because this section is the contract of record.** `boundary-patterns.md`
> designates §5 "the product's public API contract" and `api-contract.md` describes itself against
> it, so a merged tool that traced up to a requirement naming a different tool would send the next
> builder to write `cashflow_summary` — the tool this norm merged away. The derivation is
> `.prawduct/artifacts/discovery-mcp-tool-surface.md`; the merge shipped in
> `mcp-answer-scope-completion`. The `balance_history`/`net_worth` merge is **specification only**:
> neither is built, and the owner's 2026-09-08 ruling excludes both from that build.

> **Amendment (2026-09-08, owner ruling).** `get_coverage_report`'s gap rule was **"gaps >7 days"**
> and is now **gaps measured against each account's own cadence** — a per-account threshold derived
> from the account's median interval, reporting trailing silence against it.
>
> 🔴 **The constant was falsified by measurement, not by preference.** Against real data every
> account here is monthly: CD and Money Market exceed 7 days on **100%** of their intervals with a
> 30-day minimum, and Saving on 50%. The constant yields **~146 findings and no signal**, because a
> monthly account is silent for 30 days *by design*. A field that fires on almost every interval
> trains its reader to ignore it — which is the failure `warnings` already has (issue #16), reached
> by a different route, and the report exists to be believed at the moment someone is deciding
> whether to trust an answer.
>
> Cadence-relative, the same data surfaces exactly two accounts worth a second look: CD and Money
> Market, each 28 days silent on a 30-day cycle.
>
> AC-11.1's "all gaps >7 days enumerated" carries the same constant for the same reason and is
> amended with it; see the note there.

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
back to the full history window the institution actually supplied, with all gaps **against the
account's own cadence** enumerated and explained. Genuine no-activity periods are fine but must be
identified as such; a retired account's post-closure period is not a gap.

> **Note (2026-09-09).** This clause's *"a retired account's post-closure period is not a
> gap"* is **not met by the shipped `get_coverage_report`**, which reads no lifecycle column
> and so reports a closed account's silence as an ordinary and permanent coverage finding.
> AC-12.7 closes it. Recorded here rather than only in the discovery document because the
> gate is the thing that is currently claiming more than the code delivers.

> **Amendment (2026-09-08, owner ruling).** This said "gaps >7 days"; the constant is amended here
> for the reason recorded under AC-9.1, and the two must stay in step — this gate is what
> `get_coverage_report` is checked against, so a gate holding one threshold and a tool computing
> another would make the tool's output unauditable.
>
> Note this clause already carried the principle the fixed constant violated: *"genuine no-activity
> periods are fine but must be identified as such."* A monthly account's 30-day silence is exactly
> that, and the 7-day rule was reporting it as a gap.

**AC-11.2 · Reconciliation** — For each account, the balance derived by summing transactions matches
the reported current balance within a documented tolerance. Discrepancies are **itemized, not
averaged away.**

**AC-11.3 · Deduplication** — Zero duplicate transactions, tested across the pending→posted
transition, across a re-sync, and across overlapping file imports.

> **Amendment (2026-09-09).** AC-11.3 covers *duplication* across the pending→posted
> transition, and that half is implemented and tested. It does **not** cover three things
> #22 is about, each a different failure from a duplicate: a settlement that **changes the
> amount** (the ordinary case — a tip, a fuel hold — which both existing dedup cases miss,
> since they send the same amount on the hold and on the posting); a hold that **expires
> without posting**, which is a disappearance rather than a duplication; and whether a total
> **discloses** how much of itself is pending. Zero duplicates is compatible with all three
> going wrong. They are covered by AC-13.2, AC-13.4 and AC-13.1 respectively, and all three
> now have tests (`tests/connector/test_transaction_derivers.py`,
> `tests/test_pending_semantics.py`); AC-11.3's own scope is unchanged by that.

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


### Pending-transaction semantics (#22)

*Derived in `.prawduct/artifacts/discovery-production-data-semantics.md`. Every criterion below was derived in the cited discovery document, which records the evidence, the alternatives weighed, and its assumptions as **vetoable**. They are in force as requirements; an assumption the owner rejects retires the criteria that rest on it.*

**Ordinary build requirements, gating on nothing: AC-13.1 through AC-13.7.** Only AC-13.8 and AC-13.9
require production data, and § 7 is their home because they are operator-run gate checks of the
kind AC-11.6 and AC-11.7 already are.

**AC-13.1 · Pending is disclosed in the payload.** Every aggregate answer states how many of its
contributing rows are `pending` and their signed magnitude, as an **always-present** field — present
and zero, never absent. A total that mixes authorisation holds with settled amounts and does not say
so is the defect class this surface exists to refuse.

**AC-13.2 · Settlement may change the amount.** A posting entry whose amount differs from its
pending row updates the amount in place; the row count stays one and the local `transaction_id`
survives. The hold amount is not retained as a second figure.

**AC-13.3 · Order-independence across the change lists.** A pending row delivered in `removed` in
the same page as, or a later page than, its own posting resolves to exactly **one** non-removed row,
whichever order the three change lists are applied in and however the pages are split.

**AC-13.4 · An expired hold is attributable, not silent.** A pending row that is removed without ever
posting is retained with `removed_at` (AC-2.2, unchanged) **and** its exit is visible: a total that
shrank because a hold dropped off is distinguishable by the consumer from a total that shrank
because the data is incomplete.

**AC-13.5 · A stranded hold is reported.** A row still `pending` beyond a declared age threshold is
surfaced on the verification surface. The threshold is declared with its derivation, per the
precedent that a fixed constant against a variable cadence produces findings and no signal
(AC-9.1/AC-11.1's amendment).

**AC-13.6 · The pending link is described as it is implemented.** `transactions_pending_link` either
serves a query that exists or is removed. Schema, `data-model.md` § Constraints, and the resolving
query state **one** account of how a posting row finds its pending row.

**AC-13.7 · The read path is exercised against pending rows.** At least one query-layer and one
MCP-layer fixture carries `pending: true` rows. Every such fixture in the suite today hardcodes
`pending: False`, so no read-path assertion can currently distinguish correct handling from none.

**AC-13.8 · One real settlement is observed and recorded.** *(Production; gating — see § The gate.)*
A pending row is noted while pending and re-queried after it posts: it appears exactly once, at the
settled amount, with the local id preserved. Recorded in `.prawduct/operator-verification.md`.

**AC-13.9 · The observed settlement becomes a regression fixture.** The raw
`/transactions/sync` pages spanning that settlement are extracted from `raw_responses`, redacted,
and committed, and AC-13.2 and AC-13.3 are re-asserted against them.

### Sign convention on a real inflow (#23)

*Derived in `.prawduct/artifacts/discovery-production-data-semantics.md`. Every criterion below was derived in the cited discovery document, which records the evidence, the alternatives weighed, and its assumptions as **vetoable**. They are in force as requirements; an assumption the owner rejects retires the criteria that rest on it.*

**Ordinary build requirements, gating on nothing: AC-14.1 through AC-14.6.** AC-14.7–14.9 require
production data across at least two institutions.

**AC-14.1 · The convention is a per-feed claim and is stated as one.** The operator-signed
convention rests on a measured premise about **one** aggregator on **one** connection
(`api-notes-plaid.md` §17). `data-model.md` § Direction records it as a claim whose scope is
per-connection, so a second institution is a new observation rather than a covered case.

**AC-14.2 · Per-connection sign-convention check.** Over a declared set of never-plausibly-inflow
categories, the stored sign distribution is computed **per connection**. A connection whose
distribution is inverted relative to the convention is reported.

**AC-14.3 · The check declares its category set, its threshold, and its controls.** The negative
control is the measured sandbox baseline (219 of 219 negative on one connection, 2026-09-09); the
positive control is a synthetic inverted feed. A check with no observed go-red is not coverage.

**AC-14.4 · An inverted feed is reported, never silently corrected.** Auto-inverting a suspect
connection is a heuristic that can be wrong in the direction that *understates* spending — the
argument that decided #18 and #20. The system refuses and names the connection.

**AC-14.5 · No aggregate is computed over a flagged connection without saying so.** The finding
rides the success path as a warning kind, not the error channel and not a log line
(`api-contract.md` § Direction).

**AC-14.6 · Free-text description is never a sign oracle.** The sandbox's payroll row reads
"ACH Electronic Credit" and is categorised `TRANSFER_OUT` in both structured fields. Round 2's
adjudication — faithful passthrough — is settled, and this criterion exists so it is not re-derived
from the description a fourth time.

**AC-14.7 · One real deposit and one real debit are checked by eye and recorded.** *(Production;
gating.)* A known paycheck reads positive and a known bill negative, against amounts the operator
already knows. Recorded in `.prawduct/operator-verification.md`.

**AC-14.8 · The check is exercised across at least two institutions.** *(Production; gating.)* The
per-connection check is meaningless on one connection, which is exactly why the sandbox cannot
exercise it. Two connections at different institutions is the minimum that makes AC-14.2 an
observation rather than a tautology.

**AC-14.9 · The observed deposit becomes a regression fixture.** Its raw page is extracted from
`raw_responses`, redacted, and committed, so the inflow direction is machine-checked from that
commit forward.

---

## 8. Build sequence

1. Schema, migrations, raw-response layer, encrypted datastore, **and `sync shell`**. This step
   fixes the package name, the keychain service name and the scheduler label, so **the rename
   decision must be settled before it starts** (§9.3). It also lands the test runner, which is when
   the leak guard moves from `scripts/` to `tests/preferences/check-no-personal-data.sh` — a test
   runner invokes it without the per-clone `core.hooksPath` config a git hook needs. The pre-push
   wiring follows the script, so push-time enforcement is kept rather than traded away.
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

*(Two questions that stood here are closed.)*

**The project-name question is closed:** the product is named **bankmachine**, chosen 2026-09-05
before build step 1 fixed any identifier. See `project-state.yaml`.

**§9.2, aggregator pluggability, is closed** — decided by the owner 2026-09-06 and built out over
build step 2. **One aggregator in v1, drawn so that a second is a new module rather than a
rewrite.** The boundary is therefore *containment*, not an abstract interface: an abstract client
`Protocol` with exactly one implementation would encode that implementation's shape and call it a
contract, and the honest version cannot be written until a second aggregator exists to disagree
with the first.

What containment means in practice, and what enforces each part:

- Nothing outside `src/bankmachine/connector/plaid/` imports the aggregator SDK, and nothing in
  `connector/` imports a module that hands out a datastore handle
  (`tests/preferences/test_connector_is_contained.py`). The second half is what makes AC-5.1
  structural: the connector *cannot* write, so there is no path through it that normalizes before
  archiving.
- Everything outside the boundary speaks only local types — `FetchedResponse`, `Endpoint`,
  `LinkToken`, `AccessGrant`, and the error taxonomy — all defined in `connector/__init__.py` so
  that catching an aggregator failure, or naming an access grant, never requires importing the
  aggregator.
- The derivation registry is composed in `bankmachine/derivers.py`, above both layers, rather than
  in `store.derivation` — which keeps the SDK out of the import graph of every process that opens
  the datastore, the read-only query surface included.

**A second aggregator would be a new module beside `connector/plaid/` and one line in
`bankmachine/derivers.py`**, and the error types it defines would have to declare `retryable`
because `ConnectorError.__init_subclass__` refuses one that does not. That is the shape this
answer was chosen for, and the point at which the abstract interface could be written honestly.

Deployment-layer open questions live in the operator's own deployment-requirements document, and
so are not listed here — see `deployment-requirements.template.md` §6 for how they are tracked.
