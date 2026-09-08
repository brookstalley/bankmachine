---
artifact: api-contract
version: 1
depends_on:
  - artifact: system-requirements
    file_path: docs/system-requirements.md
  - artifact: data-model
    file_path: .prawduct/artifacts/data-model.md
  - artifact: security-model
    file_path: .prawduct/artifacts/security-model.md
last_validated: null
---

# API Contract — bankmachine

**Scope:** the two programmatic surfaces this product exposes — the **MCP tool surface** consumed by
an analyst agent, and the **CLI** the operator drives — with their operations, error model,
versioning decision, and stability tiers.

**Dependency note.** The template's usual product-brief upstream does not exist as a separate
artifact; §0 and §5 of `docs/system-requirements.md` carry that content.

**Build status** *(2026-09-08)*. The **CLI exists** through build step 4 — `store`, `connector`,
`sync shell`, `enroll`, `connections`, `sync run`, `mcp`. The **MCP surface exists in first slice**:
four of the ten tools below are implemented and six are specification only, recorded as a dated
descope under the tool table. This contract is therefore *description* for most of the CLI, *both*
for the four shipped tools, and *specification* for the remaining six — and each operation below
says which.
Writing it now is the point: introducing an error model or a versioning handle after consumers exist
is a breaking change.

---

## Direction

Norms. These bind future work; departure is a recorded decision, never silent
(`/prawduct:methodology norms`). Ratified 2026-09-07 by the owner.

🔴 **Two of the three are born before the surface they govern exists** — the MCP tool layer is build
step 7. That is deliberate and has direct precedent here: `architecture.md`'s four norms were also
born before any code, and the point of ratifying early is that step 7 gets **built to** them rather
than discovering them afterwards. No retroactivity decision applies, because there is nothing yet to
migrate or grandfather.

- **The MCP surface is read-only. There are no mutation tools, and adding one is not a decision this
  norm leaves open.**
  Why: §5 states it without qualification, and the structural half already holds — every read-role
  handle is opened `mode=ro` at the file, so the refusal lives in the file handle rather than in a
  session flag (`architecture.md` § Direction). The norm exists so the *tool surface* cannot drift
  where the handle cannot: vetting a comparable server surfaced 19 mutation tools including
  `delete_transaction` with no undo, on a datastore holding the same class of data. A read-only
  pipeline and a pipeline that can move money are different risk classes, and the difference is one
  tool wide.
  Status: steady-state.

- **Every response carries a freshness stamp, and incompleteness rides the success path as a warning
  field rather than as an exception.** Warnings distinguish at minimum `stale`, `degraded`, `gapped`,
  `partial`, and `rule-applied`; an aggregate that applied an account rule says so.
  Why: this is the whole product thesis in one sentence. A hard error is the easy case; the dangerous
  case is a **successful** response computed over incomplete data, because nothing throws and the
  numbers simply stop being true (AC-4.4, AC-9.3). The consumer is an analyst agent that **cannot see
  a caveat which is not in the payload** — documentation, log files, and a health tool it did not
  think to call are all invisible at the moment of answering. Putting incompleteness on the error
  channel would make it invisible exactly when it matters. AC-8.3's rule-applied clause is the same
  argument for exclusions: an exclusion that is not announced is one that gets silently forgotten
  during analysis.
  Status: steady-state.

- **The CLI's three-way exit code is a contract: `0` success, `1` ran and found a problem, `2` could
  not run.** The `1`/`2` distinction is not collapsible.
  Why: the scheduled job invokes the CLI, so these codes are a machine interface, not operator
  ergonomics. Collapsing them makes **a broken scheduler indistinguishable from a degraded feed** —
  which is this product's primary failure mode arriving through the operational door, and the one
  place where an ops shortcut reproduces the exact bug the product exists to prevent.
  Status: steady-state.

---

## Overview & Surface Type

Two surface types, with different consumers and different stakes:

| | **MCP tool surface** | **CLI** |
|---|---|---|
| Type | Network-service-shaped, but **local stdio transport** | CLI — flags, exit codes, stdout/stderr |
| Consumer | An **analyst agent** (Claude, via the MCP client) | The **operator**, and launchd |
| Consumers per classification | `both` — but both are on this machine | |
| Canonical contract | The MCP tool schemas and their descriptions | `--help` output and the exit-code scheme below |
| Mutating? | 🔴 **Never.** Read-only, no exceptions | Yes — `store init`, `store rebuild`, enrollment, sync |

🔴 **The transport is local stdio only**, confirmed as a decision rather than left an unexamined
default. There is no listener, so there is no authentication, TLS, or rate limiting at this boundary —
see `security-model.md` § Authentication for why that is sound here and what it would cost to change.

**A third consumer worth naming: launchd.** The scheduled job calls the CLI, so the CLI's **exit
codes are a machine contract**, not just operator ergonomics.

---

## Operations

### MCP tool surface — the ten tools (§5) · *four built, six specified*

🔴 **Read-only. No mutation tools. No exceptions.** (Vetting a comparable server surfaced 19 mutation
tools including `delete_transaction` with no undo. Not reproducing that.)

🔴 **Aggregate-first (AC-9.1).** Tools return computed summaries by default, not raw rows. Dumping 24
months of transactions into an LLM context is slow, expensive, and *worse at arithmetic than SQL is.*
Raw-row access exists but is paginated and hard-capped.

| Tool | Returns | Safe / idempotent |
|---|---|---|
| `get_pipeline_health` | Per-connection sync status, last success, error codes, per-account coverage window, row counts, staleness flags, rule anomalies | yes |
| `list_accounts` | Accounts with type, institution, mask, current balance, lifecycle state | yes |
| `query_transactions` | Filtered rows (date range, account, category, amount range, merchant search). **Paginated, capped** | yes |
| `spending_summary` | Aggregated by category / merchant / account over a period, with period-over-period comparison | yes |
| `cashflow_summary` | Income vs. outflow by month | yes |
| `balance_history` | Balance time series per account or aggregate | yes |
| `net_worth` | Assets minus liabilities over time, investments included | yes |
| `list_holdings` | Current investment positions with cost basis where available | yes |
| `find_recurring` | Detected recurring charges with cadence, amount drift, last-seen | yes |
| `get_coverage_report` | Per account: first and last transaction date, gaps against the account's own cadence, source breakdown | yes |

Every tool is safe and idempotent, trivially — nothing writes.

> **Amendment (2026-09-08, build step 7's first slice).** 🔴 **Four of these ten ship; six do not
> yet.** Built: `get_pipeline_health`, `list_accounts`, `query_transactions`, `spending_summary`.
> Not built: `cashflow_summary`, `balance_history`, `net_worth`, `list_holdings`, `find_recurring`,
> `get_coverage_report`.
>
> Recorded as a descope rather than left to be noticed, because the same commit updated the README
> and `architecture.md` to say the MCP surface was "built and serving" — which is true of a surface
> and not of *this* surface, and a reader comparing the two would have found the contract claiming
> ten and the code answering four with nothing saying which was current.
>
> **Two of the six are not ordinary omissions.** `get_coverage_report` is half the verification
> surface this document names two paragraphs above — the product's headline goal is "answer it, or
> say why you should not", and the coverage half of that is missing. `list_holdings` waits on build
> step 5, which has not started. The other four are analysis conveniences whose data is already in
> the datastore.
>
> **What the shipped four do not yet carry**, also descoped rather than silently unimplemented:
> `query_transactions` is specified paginated with category, amount-range and merchant filters and
> currently offers a date range, an account and a hard cap; `spending_summary` is specified with
> merchant and account breakdowns and period-over-period comparison and currently aggregates by
> category over one window.
>
> The `experimental` tier permits these changes without a version bump. It does not permit them
> going unrecorded, which is what this amendment exists to prevent.

🔴 **Two of these ten are the verification surface, not the analysis surface.** `get_pipeline_health`
and `get_coverage_report` exist so the analyst agent can **establish completeness *before* answering**.
The product's headline goal is not "answer the question" but "answer it, or say why you should not."

### CLI — the operator surface

**Built:**

| Command | Purpose | Mutating |
|---|---|---|
| `store init` | Create the encrypted datastore and run migrations | yes — 🔴 the **only explicit creator**, with the migration runner; everything else opens a file that must already exist |
| `store status` | Report the datastore's state without changing it | no |
| `store rebuild` | Re-derive the silver layer from raw responses at a recorded derivation version (AC-5.2) | yes |
| `store backup` | Write a verified, consistent, encrypted single-file copy under the writer lock | 🔴 writes only to the **destination**; the datastore is untouched |
| `connector check` | Verify aggregator credentials and reachability | no |
| `connector set-secret` | Write the aggregator secret to the keychain | yes (keychain) |
| `sync shell` | 🔴 Authenticated SQLCipher REPL — read-role handle | no |
| `enroll` | Link one institution and record the connection | yes (datastore + keychain + network) |
| `connections list` | Show enrolled connections and slots used | no |
| `connections retire` | Stop syncing a connection, keep its history, remove it at the aggregator | yes (datastore + keychain + network) |
| `sync run` | Fetch each connection's accounts and transactions since its cursor | yes (datastore + network) |
| `mcp` | Serve the datastore to an MCP client over stdio — 🔴 read-only | no |

🔴 **The three-way exit code is carried on the exception, not decided by the caller.** A refusal type
declares its own code — a full roster and an abandoned enrollment are both `1` — and the base class
defaults to `2`, so a new refusal that forgets produces the safe answer rather than silently claiming
the command ran and found a problem.

**Specified, not yet built** (steps 5–10): `sync repair` (update-mode re-auth), `import`, and the
§7 verification-gate runner. Their
names are not fixed by this document; their *contract obligations* below are.

🔴 **`sync shell` is built in step 1, not step 9** — deliberately. Page encryption is what breaks
ad-hoc `sqlite3` and Datasette access, and that access is the primary debugging affordance for every
step in between, most of all the §7 gate. *Building the replacement alongside the thing that breaks it
is what keeps the encryption decision honest.*

---

## Inputs & Outputs

Entities are defined in `data-model.md` and are not restated here.

### What every MCP response carries

🔴 **1. A freshness stamp (AC-9.2)** — last successful sync per contributing account. Not per
institution, not global.

🔴 **2. Warning fields where the data is incomplete (AC-9.3)** — see the error model below. *Analysis
over incomplete data must be impossible to do accidentally.*

🔴 **3. Enough self-description to be read correctly (AC-9.4)** — tool descriptions state **units**
(minor units), **sign conventions** (the operator's point of view; a card balance is negative), and
**whether any account rule was applied.** Ambiguity here produces wrong analysis that looks right.

### A windowed answer says which window it covered (#16)

🔴 **Every windowed tool carries `effective_window`** — `{requested: {since, until}, effective:
{since, until}}` — and unwindowed tools carry no such key at all. Absence means "this tool takes no
window"; `effective` nulls mean "the window you asked for and the data this store holds do not
overlap", which is a different statement and needs to stay distinguishable from it.

**The window is clamped, and the clamp is reportorial rather than selective.** `effective` is the
requested window intersected with `[earliest covered date, covered end]`, where the covered end is
today *or the last transaction date when that is later*. Nothing narrows a SQL predicate, and the
guarantee a consumer gets is the one that matters: **every returned row lies inside
`effective_window`.**

🔴 **The covered end is not bare `today`, and the difference is load-bearing.** A row dated ahead of
today is not forbidden — an authorization can post forward, and an institution a day ahead in local
time posts a date this UTC clock has not reached. The row-level predicate uses the caller's `until`,
so such a row is returned; had the effective end been `today`, the answer would have handed back a
December row while claiming to stop in September. That is a row outside the window the answer
claims — this defect class wearing the fix's clothes. Taking the later of the two makes the
containment guarantee true by construction rather than true of the current fixture, which cannot
express the case.

An unbounded request reports the covered span, which is where a caller most needs it: "all of it"
means nothing until you know what "all" covers.

**Why clamp rather than refuse.** `operational-spec.md` refuses an out-of-range *enrollment* window
rather than clamping it, "because a clamp would enroll at a window the operator never chose and
never told them about". That reason is about not being told, and on a read it points the other way:
refusing "show me 2024" against a store beginning 2024-09-16 refuses an ordinary question, and
enrollment's cost — history that cannot be bought back — has no analogue here. Ruled 2026-09-08:
clamp, and say so in band. `effective_window` plus its request-scoped warning is the saying so.

**What it fixes.** `spending_summary` over a window preceding coverage returned no rows, and
"you spent nothing" and "this is not knowable" were the same payload — measured across four
acceptance rounds as the most believable wrong answer this surface can produce.

### The answer says which build produced it

🔴 **Every response carries `build`** — `{version, commit, dirty}` — beside `environment` and
`as_of`. The MCP server is a subprocess the client launches, so it runs whatever code existed at
connect time; without this a caller cannot tell a server running current code from one running a
build from before the fix it is testing.

**It is captured once at process start and never re-read, and that is the requirement rather than
an optimization.** A hash read per request reports the *repository's* current HEAD, so a server
left running across a merge would answer with the merged commit while still serving pre-merge code
— reporting itself current at exactly the moment it is not. Re-reading it would build the defect
into the instrument meant to expose it. The honest cost: a long-lived process reports the build it
started with even after the checkout moves under it, which is the true statement about that
process.

🔴 **`commit: null` means the build could not be identified, and `dirty` is then `null` too — never
`false`.** `false` asserts the tree matches its commit, and a build we cannot identify supports no
such assertion. `dirty: true` matters on its own: a bare hash is a lie by omission about
uncommitted code that is running and is not in that commit.

*This is build **provenance**, not API versioning.* The versioning decision below (scheme: none,
deferred) is about letting consumers negotiate a contract, and it stands unchanged — including its
revisit trigger. What `build` answers is "which code answered me", which belongs with `as_of`.

### Coverage is reported per account, never per institution (AC-9.5)

One institution may hold many accounts with different coverage windows, and an institution-level
summary hides exactly that.

### Provenance survives into every result (AC-7.4)

Manually imported rows are distinguishable from aggregator-sourced rows in **every** query result.

### Pagination and caps

| | Value |
|---|---|
| Raw-row cap | 🔴 **~500 rows, hard** (AC-9.1) — a contract term, not a tuning knob |
| Pagination | Cursor-based, opaque |
| Aggregates | Unpaginated — bounded by the grouping, not by row count |

The cap is what keeps the sub-second target in `nonfunctional-requirements.md` reachable, and it is
what stops a caller driving unbounded cost (OWASP API4).

### CLI output

Human-readable on stdout; diagnostics and the environment banner on stderr. Machine-readable output
is **not yet a contract** — no `--json` flag is promised. Adding one later is additive.

---

## Error Model   <!-- recorded decision → api_error_model_approach -->

**Status: active** (recorded 2026-09-05).

**Envelope:** MCP tool-level errors for hard failures, **plus in-band warning fields on successful
responses.**

🔴 **The rationale is the whole design.** A hard error is the easy case. The dangerous case is a
**successful** response computed over incomplete data — so incompleteness is **a field on the success
path rather than an exception.** An agent that only handles exceptions would sail straight past a
three-week-old hole in the data and answer confidently.

### The warning vocabulary — stable, machine-readable

| Code | Means |
|---|---|
| `stale` | Last sync older than expected |
| `degraded` | A contributing connection is in error |
| `gapped` | A known coverage hole in the queried window |
| `partial` | A contributing account has bounded history |
| `rule-applied` | An account rule filtered rows from this aggregate |
| `window_starts_before_coverage` | The window asked for reaches back past the first covered date |
| `window_extends_past_today` | The window asked for reaches past the covered end — today, or the last transaction when that is later |

🔴 **The last two are REQUEST-scoped; the first five are CONNECTION-scoped, and the distinction is
the reason they exist.** A connection-scoped warning describes the standing state of the pipeline,
so it rides every response equally — measurement found the `gapped` notice arriving
character-for-character identical on a window wholly inside coverage, a window wholly outside it, a
future window, and a query for an account that does not exist. It is therefore true and useless: it
cannot tell a caller whether *this* answer is the degraded one, and a field that fires on every
response trains its reader to skip it. A request-scoped warning fires only when the request it rides
on actually crosses the boundary it names, so its presence is information and **so is its absence**.

Added additively under the evolution rules below (new warning codes need no version bump; consumers
must tolerate a code they do not recognize), so AC-9.3's list — itself a minimum — is unamended.

These are the **minimum** distinctions, not the maximum. 🔴 AC-8.3: any aggregate that applied a rule
**must say so** — an exclusion can never be silently forgotten during analysis.

### Hard errors

Reported as MCP tool errors with a stable code and a remedy sentence. 🔴 **A schema version the process
does not recognize is a hard error, including for the reader** — it answers `get_pipeline_health` with
a refusal rather than serving queries, because a reader running older code against a migrated schema
returns *plausible, structurally valid, wrong* answers. Refusing is recoverable; a wrong number that
looks right is not.

No stack traces, internal identifiers, or PII cross the boundary. Log redaction is separate and
applies regardless (`security-model.md`).

### CLI exit codes — a stable contract, already shipped

| Code | Name | Meaning |
|---|---|---|
| `0` | `EXIT_OK` | Success |
| `1` | `EXIT_UNHEALTHY` | 🔴 **Ran fine; the answer is "unhealthy."** `store status` on a missing or empty datastore |
| `2` | `EXIT_ERROR` | The command could not run — config error, keychain failure, connector failure |

🔴 **The 1/2 split is load-bearing and must not be collapsed.** launchd needs to distinguish *"the job
ran and found a problem"* from *"the job could not run."* Merging them would make a broken scheduler
indistinguishable from a degraded feed, which is the silent-staleness failure mode arriving through
the operational door.

Expected failures print one sentence to stderr, not a traceback — **but they are never silent, which
is the one outcome this project disallows.**

---

## Versioning   <!-- recorded decision → api_versioning_approach -->

**Scheme: none — internal-only surface.**
**Status: deferred** (recorded 2026-09-05).
**Granularity if adopted: whole-surface.**
**Deprecation policy:** not applicable while the only consumers are this machine's CLI and the
operator's own MCP client.
**Stability tiers:** none *(superseded — the inventory below now declares per-member tiers).*

**Rationale.** The MCP tool surface has exactly one consumer, on one machine, owned by the same person
who changes it. Versioning would be ceremony with no beneficiary. Recorded as a **deliberate decision
rather than an omission**, because adding versioning later is a breaking change for every consumer
that exists by then.

🔴 **Revisit trigger:** *any consumer outside this machine, or any second person's client, reaches the
MCP surface.* Adding a version handle is cheap now and a coordinated migration later.

**One versioned thing already exists and is not covered by this deferral:** the **datastore schema
version**, which is negotiated between the writer and the reader on every open, and whose mismatch
behaviour is specified above. That is an internal contract between two processes, not a consumer API,
but it is the reason this product already has a working version-refusal habit.

---

## Deprecation & Compatibility

**Evolution rules** (the practice that makes new versions rare):

- **Additive changes only** on the MCP surface: new tools, new optional arguments, new response
  fields.
- **Tolerant reader** on the consuming side — unknown fields in aggregator responses are preserved in
  the bronze layer, never rejected.
- 🔴 **Never remove or repurpose an existing field.** Repurposing is worse than removing: a consumer
  that still reads it gets a wrong answer instead of an error.
- **Tolerate unknown enum values** — the warning vocabulary is a minimum, so a consumer must not fail
  on a warning code it does not recognize. It should surface it.
- **New warning codes are additive** and do not require a version bump; **removing one would.**

**How a breaking change is signalled:** the operator is the only consumer, so the channel is the
change log plus a CLI warning for a deprecated command for one release before removal. This is
proportionate now and is exactly what the revisit trigger above would change.

Retention: additive-first; removal of a `stable` member defers to a major version.

---

## Surface Inventory & Stability Tiers

The public contract, declared rather than inferred. Members not listed are internal and carry no
promise. `experimental` means *this may break* — removing one is the policy working, not a violation.

**MCP tools** — all `experimental` until the §7 verification gate passes. As of 2026-09-08 four of
the ten are implemented (`get_pipeline_health`, `list_accounts`, `query_transactions`,
`spending_summary`) and six are still specification only; see the amendment under the tool table
above for what is descoped and why. `experimental` therefore means two different things in this
list, and the distinction is worth keeping in view: for the shipped four it means *this may break*,
and for the other six it means *this does not exist yet*:

- `get_pipeline_health` — experimental
- `list_accounts` — experimental
- `query_transactions` — experimental
- `spending_summary` — experimental
- `cashflow_summary` — experimental
- `balance_history` — experimental
- `net_worth` — experimental
- `list_holdings` — experimental
- `find_recurring` — experimental
- `get_coverage_report` — experimental

**CLI** — the built commands, which launchd and the operator's muscle memory already depend on:

- `bankmachine store init` — stable
- `bankmachine store status` — stable
- `bankmachine store rebuild` — stable
- `bankmachine store backup` — experimental
- `bankmachine sync shell` — stable
- `bankmachine connector check` — experimental
- `bankmachine connector set-secret` — experimental
- `--config` — stable
- `--verbose` — stable
- exit codes `0` / `1` / `2` — stable

The `connector` commands are `experimental` because the connector is mid-build (step 2) and its
command shape may still move; `store init`/`status`/`rebuild` and `sync shell` shipped in step 1 and
are depended on. 🔴 **`store backup` is `experimental` despite being a `store` command**, because
the inventory's criterion is *shipped and depended on*, not *which noun it starts with* — it was
written on 2026-09-07, nothing schedules it yet, and its restore path has never been rehearsed. The
`Retention:` rule defers removal of a `stable` member to a major, so grading a day-old command
`stable` would bind the surface to a shape nobody has used in anger.

**Internal, explicitly not contract:** every `bankmachine.*` Python module. This is an application, not
a library — importing it is not a supported use.

---

## Conventions

Small choices that are expensive to change once a consumer depends on them.

| Concern | Convention |
|---|---|
| **Money** | 🔴 **Integer minor units. Never a float, never a decimal string.** Currency explicit on every amount |
| **Sign** | 🔴 Operator's point of view — positive is money in / value held, negative is money out / value owed. A card balance is **negative** |
| **Quantities** | Exact **decimal text**, not minor units — fractional shares are routine and a share is not divided into hundredths |
| **Calendar dates** | `YYYY-MM-DD`. A transaction date is a **calendar fact**, not an instant |
| **Timestamps** | ISO-8601 UTC with an explicit `+00:00` offset |
| **The two never mix** | AC-6.4 — enforced in the database, in the type system, and at this boundary |
| **IDs** | Local integer ids. **Opaque to the consumer**; not the source's ids |
| **Enums** | Named string values, never magic integers |
| **Null vs. absent** | `null` means *known to be absent*; a missing field means *not applicable to this row*. `granted_history_days` being null is not the same as it being zero |
| **Naming** | `snake_case` for tool arguments and response fields; `kebab-case` for CLI subcommands and flags |

🔴 **`available_minor` and `limit_minor` are the two documented exceptions to the sign convention** —
they hold magnitudes as reported and participate in no total. Any response exposing them says so.

**Why sequential integer ids are safe here** where they would be a finding on a public API: there is
no BOLA surface to enumerate against — one operator owns every object (`security-model.md`).

---

## Security

Authentication and authorization live in `security-model.md`. This section names the **API-design**
failure modes at this boundary. Three of the OWASP API Top 10 apply; the assessment of why the others
do not is in that document.

🔴 **API4 — Unrestricted resource consumption.** A caller can drive cost: an unbounded
`query_transactions` over 24 months would be slow, expensive in context, and worse at arithmetic than
the SQL it replaces. Controls: the ~500-row hard cap, cursor pagination, aggregate-first defaults, and
bounded date ranges. **The cap is a contract term** — a consumer may not raise it.

🔴 **API9 — Improper inventory management.** A forgotten tool is a live risk on a surface that grows
one tool at a time. The declared inventory above is the control: it is the product's own declaration,
and dropping a member from the list in the same change that removes it is amending the norm to match
the code, not retiring the promise.

🔴 **API10 — Unsafe consumption of third-party APIs.** The aggregator's responses are unowned input.
Controls: verify the real shape by reading SDK source or probing a live sandbox **before** writing
handlers — vendor docs lag code and training data lags further, and a fake built from an assumed
signature passes against a test that shares the assumption. Responses are persisted verbatim before
normalization, and the database's own constraints reject anything that would violate an invariant.

**Input validation** at this boundary: date ranges are bounded, row caps enforced server-side, and
`sync shell` accepts arbitrary operator SQL **on a `mode=ro` handle** — the read-only guarantee lives
in the file handle, so the REPL cannot be talked out of it.

**Excessive data exposure** cuts differently here: the consumer is an LLM with finite, expensive
context, so returning only what is needed is simultaneously a security posture and the performance
requirement.

---

## Conditional Patterns

- **Long-running operations:** none on the MCP surface — every tool is a bounded query. The long
  operation is the **sync**, which is a scheduled CLI job the MCP surface only *observes* through
  `get_pipeline_health`. This is AC-ARCH.7 at the contract level: the reader never waits on the writer.
- **Concurrency and consistency:** the reader takes a fresh short-lived snapshot per tool call and
  holds none between calls. A consumer therefore may see two adjacent calls land either side of a sync
  commit. 🔴 **The freshness stamp is how they can tell** — which is why it is on every response rather
  than available from one health tool.
- **Events and callbacks:** none. **Webhooks are deliberately not built** (v1) — they require a
  publicly reachable endpoint, which this machine is not and should not become. A clean seam is left.
- **Consumer correlation:** the sync's per-run correlation id appears in logs and is surfaced through
  `get_pipeline_health`, so a health report can be traced to the run that produced it
  (`observability-strategy.md`).
- **Cross-origin / untrusted callers:** not applicable — stdio, no browser, no socket.
