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
five of the eight tools below are implemented and three are specification only, recorded as a dated
descope under the tool table. This contract is therefore *description* for most of the CLI, *both*
for the five shipped tools, and *specification* for the remaining three — and each operation below
says which.
Writing it now is the point: introducing an error model or a versioning handle after consumers exist
is a breaking change.

---

## Direction

Norms. These bind future work; departure is a recorded decision, never silent
(`/prawduct:methodology norms`). The first three ratified 2026-09-07 by the owner; the
fourth born 2026-09-09, and it is the only one carrying a migration.

🔴 **The norms ratified 2026-09-07 were born before the surface they govern existed** — the MCP tool
layer was build step 7. That is deliberate and has direct precedent here: `architecture.md`'s norms
were also born before any code, and the point of ratifying early is that the build gets **built to**
them rather than discovering them afterwards. No retroactivity decision applied to those, because
there was nothing yet to migrate or grandfather. **The fourth is the exception and says so in its
own entry:** it was born against a surface that already shipped, and it carried one migration, now
paid.

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

- **A tool's boundary is drawn where the answer *shape* changes — never where the question changes.**
  Two guardrails carry it: a merge is permitted only when **one strict row schema covers every
  parameter value with no optional fields** (nullable is fine — a null field is present and null,
  never dropped; *absent* is not), and within one shape, **merge only across questions that share a
  domain**. Verification and analysis are different domains, as this document already declares them.
  Why: the owner directed "minimize tools, use actions to cover related capabilities", and applied
  literally that destroys what the per-tool `outputSchema` buys — MCP allows one schema per tool, and
  the schemas are per-tool precisely so a key's ABSENCE is information. Both the tool-per-question and
  the action-parameter extremes assume a boundary tracks the *question*; that shared premise is what
  fails. Factoring on answer shape satisfies the directive in substance while making the schema
  property a **consequence** of the rule rather than something defended against it: a tool defined by
  its answer shape has one answer shape by construction, and two tools can never become near-twins,
  because if they were they would share a shape and already be one tool. Capabilities grow fast; row
  entities do not. The first guardrail is checkable at registration, so the norm enforces itself
  rather than resting on the judgment of whoever adds the next tool.
  🔴 **Enforced structurally since 2026-09-09**, not merely checkable: `mcp._refuse_optional_row_fields`
  refuses a definition whose row schema declares a field it does not require, and it runs inside
  `_tool_definitions()` — the one function that hands out a definition — so no caller can route
  around it. `mcp._refuse_colliding_parameters` sits beside it for #30's A3, because both fail for
  one reason: a surface that cannot be described strictly should not be advertised. The check is
  scoped to ROWS. The stronger rule — every declared property required, everywhere in the schema —
  refuses the shipped surface, because a warning carries `connection_id` only when it is about one
  connection; that is absence-as-information at the envelope level, which this norm does not govern.
  🔴 **Retroactive, and its one migration is PAID:** `spending_summary` was its own tool and is now
  `money_summary`, the grouped aggregate. Every MCP tool is `experimental` (§ Surface Inventory),
  where "removing one is the policy working, not a violation", so the migration cost no version bump
  and opened no deprecation window. No other shipped tool changed shape.
  Born 2026-09-09, from #30. Derivation and the alternatives priced against it:
  `discovery-mcp-tool-surface.md`.
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

### MCP tool surface — the eight tools (§5) · *five built, three specified*

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
| `money_summary` | Money in and out over a period, grouped by category / merchant / account / month / flow class, per currency, and split by flow class under every grouping | yes |
| `balance_history` | Value over time, per account or aggregated as net worth, investments included | yes |
| `list_holdings` | Current investment positions with cost basis where available | yes |
| `find_recurring` | Detected recurring charges with cadence, amount drift, last-seen | yes |
| `get_coverage_report` | Per account: first and last transaction, posting cadence, and trailing silence measured against it | yes |

Every tool is safe and idempotent, trivially — nothing writes.

> **Amendment (2026-09-08, build step 7's first slice; revised 2026-09-09).** 🔴 **Five of these
> eight ship; three do not yet.** Built: `get_pipeline_health`, `list_accounts`,
> `query_transactions`, `money_summary`, `get_coverage_report`.
> Not built: `balance_history`, `list_holdings`, `find_recurring`.
>
> Recorded as a descope rather than left to be noticed, because the same commit updated the README
> and `architecture.md` to say the MCP surface was "built and serving" — which is true of a surface
> and not of *this* surface, and a reader comparing the two would have found the contract claiming
> ten and the code answering four with nothing saying which was current.
>
> **`get_coverage_report` was the sharpest of these omissions and is now BUILT.** It is half the
> verification surface this document names two paragraphs above — the product's headline goal is
> "answer it, or say why you should not", and the coverage half of that was missing. Of what
> remains, `list_holdings` waits on build step 5, which has not started; `balance_history` and
> `find_recurring` are analysis conveniences whose data is already in the datastore.
>
> **What the shipped tools do not yet carry**, also descoped rather than silently unimplemented:
> `query_transactions` is specified paginated with category, amount-range and merchant filters and
> currently offers a date range, an account and a hard cap; `money_summary` is specified with
> period-over-period comparison and does not yet offer it — it does now carry the merchant, account,
> month and flow-class groupings, both directions, and per-currency rows.
>
> The `experimental` tier permits these changes without a version bump. It does not permit them
> going unrecorded, which is what this amendment exists to prevent.

🔴 **Two of these eight are the verification surface, not the analysis surface.** `get_pipeline_health`
and `get_coverage_report` exist so the analyst agent can **establish completeness *before* answering**.
The product's headline goal is not "answer the question" but "answer it, or say why you should not."

> **Amendment (2026-09-09, #30 — applied; the table above now lists eight).**
> § Direction's fourth norm draws a tool boundary where the answer *shape* changes, and applying it
> re-factored this specification from ten tools to eight: `cashflow_summary` merged into a grouped
> aggregate tool alongside `spending_summary` — both are now **`money_summary`** — and `net_worth`
> merges into the time series alongside `balance_history`. Neither merge costs a published
> `outputSchema`; both eliminate a near-twin pair. `discovery-mcp-tool-surface.md` carries the
> derivation and the row shape that permits each merge.
>
> 🔴 **The table and the code move in the SAME commit, and the guard enforces it.**
> `test_the_documented_tool_surface_is_the_built_one.py` derives the *specified* set from these rows
> and the *built* set from `_tool_definitions()`, then asserts built ⊆ specified. Rewriting the rows
> ahead of the code drops a tool from the specification while it is still on the wire, and the guard
> fails — correctly. A rename is therefore never a two-commit job.
>
> 🔴 **`money_summary`'s parent requirement is `docs/system-requirements.md` §5**, amended the same
> day. That section is the contract of record — `boundary-patterns.md` designates it so — and a
> merged tool tracing up to a requirement that named a different tool is how the next builder ends
> up writing the tool this norm removed.

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
`effective_window`** — see the snapshot qualifier below for the one interleaving that can move the
reported bound.

🔴 **The covered end is not bare `today`, and the difference is load-bearing.** A row dated ahead of
today is not forbidden — an authorization can post forward, and an institution a day ahead in local
time posts a date this UTC clock has not reached. The row-level predicate uses the caller's `until`,
so such a row is returned; had the effective end been `today`, the answer would have handed back a
December row while claiming to stop in September. That is a row outside the window the answer
claims — this defect class wearing the fix's clothes. Taking the later of the two makes the
containment guarantee hold against the coverage the resolver was handed, rather than merely being
true of the current fixture, which cannot express the case.

🔴 **The qualifier, stated at the strength the mechanism holds.** Containment holds against the
*coverage* snapshot, not against the *row* snapshot, because the two are separate reads on a handle
that holds no read snapshot. A soft delete of the store's **oldest** row landing between them moves
`earliest_transaction` forward and can push `effective_since` past a row already returned; the
mirror case needs a future-dated latest row removed in the same gap, since the covered end cannot
fall below today. Both are rare — they need the boundary row itself removed inside a sub-millisecond
gap, and ordinary sync removals are recent pending rows rather than the oldest row of a two-year
store — and the cost is a reported bound off by a little, never a wrong figure or a wrong row. **What
would earn the unqualified claim is a single read snapshot per answer (#27); until then this contract
states the conditional version rather than the one that reads better.**

*(`as_of` is not part of this. It is captured after the rows, so `today` — and therefore the covered
end — can only widen relative to what the rows saw, and a wider window still contains them. The
hazardous ordering would be capturing `as_of` first, which is not what the code does.)*

An unbounded request reports the covered span, which is where a caller most needs it: "all of it"
means nothing until you know what "all" covers.

**Why clamp rather than refuse.** `operational-spec.md` refuses an out-of-range *enrollment* window
rather than clamping it, "because a clamp would enroll at a window the operator never chose and
never told them about". That reason is about not being told, and on a read it points the other way:
refusing "show me 2024" against a store beginning 2024-09-16 refuses an ordinary question, and
enrollment's cost — history that cannot be bought back — has no analogue here. Ruled 2026-09-08:
clamp, and say so in band. `effective_window` plus its request-scoped warning is the saying so.

**What it fixes.** `spending_summary` — the tool `money_summary` replaced — over a window preceding
coverage returned no rows, and
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

### A capped answer says how much it left behind (#17)

🔴 **Every capped tool carries `truncation`** — `{returned, matching, truncated}` — and a tool that
returns everything it finds carries no such key at all. Absence means "this tool is not capped", so a
consumer branching on the key gets a true answer either way. `query_transactions` is the only capped
tool today; aggregates carry no block, because the row cap does not apply to them.

**`returned` is the count of rows actually in the payload**, derived from the rows themselves rather
than from the caller's `limit` — a `limit` above the hard cap is clamped, so the two are not the same
number. **`matching` is the count the request selects**, over the same predicates and the same tables
as the row query. **`truncated` is `returned < matching`**, derived rather than stored: a third
number can disagree with the other two, and a derived one cannot.

🔴 **The measured harm this closes.** An acceptance round found the documented default of `limit: 100`
silently dropping ~16 months of one account's history, and a caller summing a two-year card total
understating it by roughly 40% — with a payload that read as a complete answer throughout. `rows`
alone cannot say it, because "100 rows" is a believable complete answer.

**A truncated answer also raises `rows_truncated`**, because a structured field is not where a
consumer looks when the numbers seem wrong. The caveat names the shortfall and a remedy the caller
can actually follow — paging leads, because it is the only route that reaches every matching row,
and raising `limit` is offered only while `limit` has something left to give: at the cap it would be
advice the answer's own `returned` contradicts.

🔴 **`matching` and the rows are two snapshots, and the contract says which way that resolves.**
The read handle is opened in autocommit — `store/connection.py`: *"every statement is its own
snapshot"* — and the scheduled sync writer soft-deletes transactions while the MCP reader may be
mid-query. So a row counted in the first statement and removed before the second makes the count come
back *below* the rows already in hand. **That is a data condition, not an error:** `matching` floors
at `returned`, because those rows were observed to match and reporting fewer would contradict the
payload beside them; `truncated` is then false, which is true, since nothing is being hidden. The
skew is not smoothed away — `counted_during_change` announces it, so a consumer comparing two calls
seconds apart knows a write landed between them. **Refusing to answer here would be the wrong
trade**: it would turn a harmless skew into a failed tool call, which the Direction above forbids in
as many words.

**Cost, measured rather than assumed** — full method, caveats and the figures below in
`.prawduct/artifacts/mcp-count-latency-2026-09-08.md`, which records that these are single-process
warm-cache medians on one developer machine rather than a portable benchmark (2026-09-08, synthetic
stores, 14 accounts over 24 months):
the `matching` count runs at roughly the cost of the row query itself — ~1ms at 10k rows, ~89ms at
200k — and a full `query_transactions` call lands at ~18ms / ~435ms respectively, inside the ~1s
target in `nonfunctional-requirements.md` with room to spare at volumes well beyond a real 24-month
store. **`matching` therefore ships exact**; the approximate-count fallback that was held in reserve
is not needed and is not built.

### A truncated answer carries the route to the rest (#17)

🔴 **A truncated answer carries `truncation.next_cursor`, when and only when `truncated` is true.**
The caller passes it straight back as `query_transactions`'s optional `cursor` argument, with the
same window and account, and repeats until `truncated` is false — at which point no `next_cursor` is
present. **The key's presence is the loop condition**: a consumer pages while it is there and stops
when it is gone, without comparing two counts to decide. Visibility without a route past the cap
would have left the honest answer still unobtainable, which is why #17 needed both halves.

**`cursor` narrows the request the way `since` does.** `matching` counts what is left from the
cursor's position onward, not what lies behind every page — a count over the whole result set would
leave `truncated` true on the final page forever and a caller paging until it went false would never
stop.

🔴 **A keyset, never an offset.** The cursor is opaque state over `(posted_date, transaction_id)`,
the total order rows already come back in. An offset shifts under a concurrent sync — one insert
between two pages and the caller sees a row twice and never sees another — which would reintroduce
this cycle's own defect through a new door: a paged answer that reads as complete and is not.

🔴 **A cursor is usable only against the request that issued it.** The predicate it was issued for
travels inside it and is compared when it comes back, so a cursor sent with a different window or
account is refused by name rather than answered. That case is the reachable one: it selects real
rows, in the right order, and answers a question the caller did not ask — no error, no warning, and
a payload that reads as a continuation. A cursor that is malformed, forged, or from a scheme this
build does not issue is refused the same way, in one sentence that names `cursor` and leaks nothing
of the decoder. **It is never read as "start from the newest row"**: that fallback returns page one
under the name of page two.

**The cap does not move.** Paging is what makes it escapable; ~500 stays a contract term, and
measurement already refuted raising it — 74.2% of rows are dropped at full coverage, so no default a
human would pick fixes this.

🔴 **A walk that ends on a `counted_during_change` page may have stopped early.** That warning means
the count came back below the rows already in hand, so `matching` floored at `returned`, `truncated`
read false, and no cursor was issued — correct for the numbers in the payload, and possibly short of
the window if enough rows were removed mid-walk. The warning is the telling: ask again for a count
taken after the change. Refusing to answer instead would turn a harmless skew into a failed tool
call, which the Direction above forbids.

### Window-scoped coverage rides beside the store-wide figure, never replacing it

🔴 **`coverage.transactions` stays store-wide.** A windowed answer gains a *sibling*,
`coverage.transactions_in_effective_window`, counted over the effective bounds and never narrowed by
`account_id` — per-account coverage is its own change (#19), and shipping half of it here would leave
that work amending a field this one just added. Narrowing `coverage.transactions` in place would be
the repurpose the evolution rules forbid: a consumer still reading it would get a wrong answer rather
than an error. The sibling is present on exactly the answers that carry `effective_window`, including
when the datastore cannot be read, so the key set a consumer branches on never depends on the store's
health.

### An aggregate says which money actually left, and it classifies rather than filters (#18)

🔴 **Every `money_summary` row carries `flow_class`** — `external_spend`, `internal_transfer` or
`debt_service` — and the class is a *grouping dimension under every value of `group_by`*, not a
field that appears on one grouping and is guessed on the others. It has to be: an account holds a
transfer and a coffee, a month holds all three by definition, and even a category can split,
because the category key reads `category_override` first while the class is fixed to read
`source_category_primary` only. Attaching one row's class to a group that spans classes would
state it for the others, and making the field *optional* is refused by § Direction's fourth norm,
which merges tools only where one strict row schema covers every parameter value. The consequence
is accepted and is the point: one month can return three rows per currency where it returned one.

🔴 **Classify, do not filter.** Every row is kept and gains a class; nothing is dropped, precisely
so there is no invisible undercount. That is also why this emits no `rule-applied` warning —
that kind means an account rule filtered rows *out* of an aggregate, and a tool that excludes
nothing saying so would be a false statement about the answer carrying it.

🔴 **The class is read from the source column, never from `category_override`.** An override is
local interpretation of what a transaction was *for*; the flow class is about whose money moved and
in which direction. Letting a re-categorisation reclassify a transfer as spending would reintroduce
the overcount through the back door, silently and in the direction that inflates. An unrecognised
or null category falls to `external_spend`, which is the conservative direction on this surface's
own principle: an overcount gets questioned and an undercount gets believed.

### A classifying tool carries `totals`, per currency, and the three add up

🔴 **`money_summary` gains a `totals` block** — one entry per currency, carrying the window's
*outflow* under each of the three classes. `external_spend_outflow_minor_units` is the figure to
quote when asked what was spent; the other two are money that never left the holder's accounts or
that settles purchases already counted under the categories they were spent in, so summing all
three double-counts.

🔴 **Measured against the sandbox store on 2026-09-09, over its full 24 months:** $267,692.77 of
outflow, of which $164,400.00 is internal transfer and $50,484.00 is debt service — leaving
**$52,808.77 of actual spending, one fifth of the raw figure.** That is the whole of #18 in one
line, and it is why the headline rides the envelope rather than waiting for a caller to derive it
from rows they may never read. The ratio is a property of this fixture and not a constant; what the
contract fixes is that the decomposition is always present, never the size of the gap.

The three sum to the window's total outflow in that currency, and that identity is the contract:
it is what proves the classification *partitions* the rows rather than quietly dropping some.
🔴 **Per currency, never one integer across currencies**, by the ruling that governs every
aggregate here — a summed integer over two currencies is not a wrong number, it is not a number.
The block is present and empty when the datastore cannot be read, exactly as `coverage` is present
and zero, so the key set a consumer branches on never depends on the store's health.

### Coverage is reported per account, never per institution (AC-9.5)

🔴 **Ruling, 2026-09-09 — where the three new findings live.** Two discovery passes each asked
whether their per-account and per-connection findings belong on `get_coverage_report` or
`get_pipeline_health`, and both deferred it so it would be decided once. 🔴 **§ Direction's fourth
norm already answers it: a tool's boundary is drawn where the answer *shape* changes, never where
the question changes.** So the split is by the shape of the finding, not by its subject matter:
a **per-account** finding goes to `get_coverage_report` (AC-12.7's retired-account silence,
AC-13.5's stranded hold), and a **per-connection** finding goes to `get_pipeline_health`
(AC-14.2's sign-convention check). This is an application of the existing norm, not a new one —
no fifth norm is born here, and the candidate one proposed alongside AC-12.8 stays unborn until
that ruling is taken.

One institution may hold many accounts with different coverage windows, and an institution-level
summary hides exactly that.

🔴 **Every `list_accounts` row carries `first_transaction_date`, `last_transaction_date` and
`transaction_count` — always, not behind a parameter.** Measured: nine of fourteen sandbox accounts
have never had a transaction, 82% of the balance sheet by magnitude, and nothing in any payload said
so. `query_transactions(account_id=9)` answered `[]`, which is indistinguishable from a quiet month,
while three fields actively implied the opposite — `coverage.accounts` counted all fourteen, health
reported the connection `active`, and the only warning was about depth rather than breadth. An agent
that never thought to call the verification tool is exactly that failure, so completeness rides the
answer rather than a channel nobody reads. **A null date means NO TRANSACTION HAS EVER BEEN
RECORDED, never "no activity"; `transaction_count` is `0` rather than null, because a null would be
a second spelling of the same fact.**

🔴 **The cost was measured before the parameter was ruled out, not after.** The walk costs 3.0ms at
the expected 10k-row volume against an 18.2ms end-to-end `list_accounts` and a ~1s target
(`mcp-coverage-latency-2026-09-09.md`). Cost was the only argument for making completeness something
a caller had to ask for.

**`get_coverage_report` is the verification surface built on the same producer**, and carries the
analysis `list_accounts` does not: `median_interval_days` (this account's own posting cadence),
`days_silent`, `silence_ratio`, `silence_exceeds_cadence`, and `source_breakdown` by provenance.
🔴 **One producer feeds both** — built twice they can disagree, and a verification surface that
contradicts the analysis surface is worse than one that is absent.

🔴 **Trailing silence against the account's own cadence, not an enumeration of gaps between
transactions.** The earlier "gaps > 7 days" rule was falsified by measurement: every sandbox account
is monthly, so two of them exceeded 7 days on 100% of their intervals — ~146 findings and no signal.
The ratio is reported as a NUMBER rather than collapsed into a flag, because 28 days silent on a
30-day cycle is genuinely borderline and a boolean is what destroys that.
`silence_exceeds_cadence` is one full missed cycle, because one cycle is the only non-arbitrary
unit. **The divisor is floored at one day**: an account whose rows cluster on the same dates has a
median interval of literally 0, and dividing by it left the busiest feeds — the ones most likely to
stop — answering "not silent" however long they had been dead. The reported median is not floored;
0 is a true cadence meaning "posts more than once a day", which is the opposite of null's "no
interval exists".

### Provenance survives into every result (AC-7.4)

Manually imported rows are distinguishable from aggregator-sourced rows in **every** query result.

### Pagination and caps

| | Value |
|---|---|
| Raw-row cap | 🔴 **~500 rows, hard** (AC-9.1) — a contract term, not a tuning knob |
| Pagination | Cursor-based, opaque — `truncation.next_cursor` on `query_transactions`, passed back as `cursor` |
| Aggregates | Unpaginated — bounded by the grouping, not by row count |
| Hitting the cap | Never silent — `truncation` carries `returned`/`matching`/`truncated`, and `rows_truncated` warns |
| Escaping the cap | `next_cursor`, present when and only when `truncated`; absent on the last page |

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
| `window_extends_past_coverage` | The window asked for reaches past the covered end — today, or the last transaction when that is later |
| `rows_truncated` | The request matched more rows than the cap returned, and the answer holds only the newest of them |
| `counted_during_change` | A write landed between the row read and the count read, so the two describe moments a fraction apart |
| `accounts_without_coverage` | An account in the scope of THIS request has never had a transaction recorded, so its empty result means data not present, never no activity |
| `account_no_longer_active` *(specified, not built)* | An account in the scope of THIS request is closed or is no longer listed by its institution, so its balance is frozen as of the date beside it and is not a fact about today |
| `includes_pending_rows` *(specified, not built)* | This answer's rows include authorisation holds that have not settled, so a figure computed from it may change without any new activity |
| `sign_convention_unverified` *(specified, not built)* | This answer draws on a connection whose sign convention has not been observed against a known inflow, so its direction is assumed rather than confirmed |

🔴 **The window/row/account kinds below the line are REQUEST-scoped; the connection kinds above them
are CONNECTION-scoped, and the distinction is the reason they exist.** A connection-scoped warning describes the standing state of the pipeline,
so it rides every response equally — measurement found the `gapped` notice arriving
character-for-character identical on a window wholly inside coverage, a window wholly outside it, a
future window, and a query for an account that does not exist. It is therefore true and useless: it
cannot tell a caller whether *this* answer is the degraded one, and a field that fires on every
response trains its reader to skip it. A request-scoped warning fires only when the request it rides
on actually crosses the boundary it names, so its presence is information and **so is its absence**.

🔴 **`accounts_without_coverage` is request-scoped for exactly that reason, and the obvious reading
is wrong.** "This account has never had a transaction" looks like standing state of the store, and
therefore connection-scoped — but a fifth kind riding every response equally would reproduce the
defect the paragraph above records. It fires only when *this* request's scope actually contains an
uncovered account: on `list_accounts` when the listing holds one, and on
`query_transactions(account_id=N)` when the account asked about has none.

🔴 **This table is prose and `envelope.WARNING_KINDS` is the code; nothing holds them together.**
`test_the_warning_vocabulary_is_closed.py` scans source only, so a kind added to the vocabulary
without being added here goes unnoticed — which is how `accounts_without_coverage` was missing from
this table for a full work cycle after it shipped. Adding a kind means editing both until something
derives one from the other.

🔴 **The three kinds marked *specified, not built* were proposed by two separate discovery
passes that could not see each other, and each registered only its own.** They are recorded here
together for that reason. Whichever ships first must add **all three** to
`envelope.REQUEST_SCOPED_KINDS` in the same change, or the closed-vocabulary test fails and a
schema-validating client rejects the whole answer rather than the unknown code. Sources:
`.prawduct/artifacts/discovery-account-lifecycle.md` (AC-12.x) and
`.prawduct/artifacts/discovery-production-data-semantics.md` (AC-13.x, AC-14.x). The two names
the second document left to the builder are fixed here so the three are chosen as a set.

Added additively under the evolution rules below (new warning codes need no version bump; consumers
must tolerate a code they do not recognize), so AC-9.3's list — itself a minimum — is unamended.

🔴 **`window_extends_past_coverage` was briefly named `window_extends_past_today`, and the rename
happened before any consumer could depend on it.** The old name states the wrong bound: the covered
end is today *or the last transaction when that is later*, so the name was stale by one word while
the detail text beside it was accurate. Renaming a shipped warning code is a breaking change under
the evolution rules below, which is exactly why it was done while the surface was still unreleased
rather than left to become permanent. The pair now names one boundary concept from its two ends.

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

**A second one, and it is not this product's to choose:** the **MCP protocol revision**, negotiated
on every `initialize`. The server offers `2024-11-05`, `2025-03-26`, `2025-06-18` and `2025-11-25`,
honours the client's requested revision when it is one of those, and counter-offers `2025-03-26`
otherwise.

🔴 **`2026-07-28` is deliberately not offered.** It is not reachable through `initialize` at all —
the SDK's registry partitions handshake revisions from that one, whose sessions use a stateless
per-request envelope reached by a `server/discover` probe, and both `InitializeRequestParams` and
`InitializeResult` are documented as removed there. Offering it agreed, on the handshake, to an era
this server has no code for. Independently, `ListToolsResult` is a `CacheableResult` on that
revision and its required `ttlMs`/`cacheScope` are not sent — so the claim was never serviceable
from either end. Supporting it is a build, not a constant.

🔴 **`2024-11-05` is offered on purpose, not by inertia.** The counter-offer only rescues a client
that can speak something *newer* than it asked for; omitting the oldest revision means answering a
client pinned there with one it cannot speak, and the spec has such a client disconnect rather than
downgrade. Nothing this server puts on the wire distinguishes the two anyway.

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

**MCP tools** — all `experimental` until the §7 verification gate passes. As of 2026-09-09 five of
the eight are implemented (`get_pipeline_health`, `list_accounts`, `query_transactions`,
`money_summary`, `get_coverage_report`) and three are still specification only; see the amendment
under the tool table above for what is descoped and why. `experimental` therefore means two
different things in this list, and the distinction is worth keeping in view: for the shipped five it
means *this may break*, and for the other three it means *this does not exist yet*:

- `get_pipeline_health` — experimental
- `list_accounts` — experimental
- `query_transactions` — experimental
- `money_summary` — experimental
- `balance_history` — experimental
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

**MCP resources** — the reference surface, served by URI so an agent reads it without spending a
tool call. Both `experimental`, for the same reason the tools are:

- `bankmachine://reference/warnings` — every warning kind, what it implies, and what to do about it
- `bankmachine://reference/envelope` — every envelope field and which tools carry it

🔴 **Both are DERIVED, not authored.** The warning reference walks `envelope.WARNING_KINDS`; the
envelope reference renders from the tools' published `outputSchema`. A kind added to the vocabulary
reaches the document by itself — unexplained rather than missing — which is the property that keeps
this from becoming a third hand-maintained copy of the vocabulary.

**What rides the handshake**, and is contract even though the inventory grades no member of it: the
negotiated protocol revision (above), `serverInfo` restricted to the keys `Implementation` declares,
and build identity in `_meta` under `bankmachine/build`. 🔴 Build identity does **not** ride
`serverInfo` — that type declares no field for it and an SDK-based client discards what it does not
declare, so keys placed there are dropped in transit rather than delivered.

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
