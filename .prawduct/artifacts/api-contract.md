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
eight of the nine tools below are implemented and one is specification only, recorded as a dated
descope under the tool table. This contract is therefore *description* for most of the CLI, *both*
for the shipped tools, and *specification* for the rest — and each operation below
says which.
Writing it now is the point: introducing an error model or a versioning handle after consumers exist
is a breaking change.

---

## Direction

Norms. These bind future work; departure is a recorded decision, never silent
(`/prawduct:methodology norms`). Each entry carries its own birth date and status; read those rather
than any count here.

🔴 **The norms ratified 2026-09-07 were born before the surface they govern existed** — the MCP tool
layer was build step 7. That is deliberate and has direct precedent here: `architecture.md`'s norms
were also born before any code, and the point of ratifying early is that the build gets **built to**
them rather than discovering them afterwards. No retroactivity decision applied to those, because
there was nothing yet to migrate or grandfather. **The norms born later are the exception and each
says so in its own entry**, because a norm born against a surface that already shipped has to state
what it does with what is already there: the tool-boundary norm carried one migration, now paid, and
the balance-lifecycle norm names one unmigrated emitter it does not grandfather.

- **The MCP surface is read-only. There are no mutation tools, and adding one is not a decision this
  norm leaves open.**
  Why: §5 states it without qualification, and the structural half already holds — every read-role
  handle is opened `mode=ro` at the file, so the refusal lives in the file handle rather than in a
  session flag (`architecture.md` § Direction). The norm exists so the *tool surface* cannot drift
  where the handle cannot: vetting a comparable server surfaced 19 mutation tools including
  `delete_transaction` with no undo, on a datastore holding the same class of data. A read-only
  pipeline and a pipeline that can move money are different risk classes, and the difference is one
  tool wide.

  > **Amendment, 2026-09-10 — one class of write is permitted, and the class is defined by who
  > authored the row.** *Statement:* the surface is read-only over everything the aggregator
  > produced. An agent-originated write may touch only a table named in an explicit **agent-writable
  > declaration**, and no table carrying `raw_response_id` or `derivation_version_id` may be named in
  > it. A table carrying either stays read-only to this surface **whatever the column**. Adding a
  > mutation tool that reaches a derived row is still not a decision this norm leaves open.
  > *Why:* the why above is an argument about *derived financial data* — 19 mutation tools including
  > `delete_transaction`, a pipeline that can move money. It is not an argument about a note. An
  > agent that cannot record what it learned re-learns it every session or, worse, learns it
  > differently, and there is nowhere else for a learning to live that travels with the store
  > (FR-11). What the norm was actually protecting is that the *derived* store cannot be written by
  > anything but the deriver, and scoping the permission by row authorship protects exactly that
  > while costing the note nothing.
  > 🔴 *The declaration is what grants; the provenance test only forbids.* Stated as the provenance
  > test alone the bound has a hole and **eight tables fall through it** (measured 2026-09-10):
  > `account_rules`, `accounts`, `connections`, `derivation_versions`, `institutions`,
  > `manual_imports`, `raw_responses`, `sync_state`. Three of those settle it — `raw_responses` is
  > the archive every rebuild replays from and passes because its `raw_response_id` is a primary key
  > rather than a foreign key (`rebuild.py` records the same trap for its own classification);
  > `account_rules` produces the `rule-applied` warning and filters rows out of aggregates, so
  > permitting it would contradict the reason `category_override` is held out of reach in the same
  > sentence; `accounts` carries `lifecycle_status`, which the balance-lifecycle norm below makes
  > arithmetic. That is AC-15.1's finding one layer up: no table is unwritable merely because nothing
  > currently says so.
  > 🔴 *And the row is the unit, not the column.* `transactions.category_override` is
  > operator-authored and stays out of an agent's reach, because SQLite has no per-column grant — a
  > column-scoped rule could only live in application logic, which is the thing this norm exists to
  > avoid resting on — and because it is what `money_summary` groups by, so an agent that could move
  > it could move a total.
  > *Retroactivity:* none owed. No mutation tool exists, so nothing on the wire changes meaning, and
  > this amendment lands **before** the build rather than as a migration after it — the same
  > sequencing as the `EX_TEMPFAIL` amendment below.
  > *What it does not license:* a writable handle in the server process. The recommended write path
  > is out-of-process, so `architecture.md` § Direction's `mode=ro` norm is untouched and AC-16.10
  > states that as a criterion, precisely so a build cannot satisfy this amendment's letter by
  > opening the handle the norm was protecting.
  > `[DECISION: the agent-write permission is scoped by row authorship — a declared sidecar table,
  > with no derived table admissible to the declaration — rather than by column or by tool intent |
  > the norm's why is about derived financial data, and a table-scoped permission is the only shape
  > a test can check, since SQLite has no per-column grant | user can veto/override]`
  Born 2026-09-07. Amended 2026-09-10 on the owner's ruling of the same date; derivation
  `discovery-agent-annotations.md`, requirement FR-11.
  Status: in-transition — brookstalley/bankmachine#87 tracks the build. **Interim rule:** until #87
  lands there is no mutation tool and no agent-writable declaration, every MCP handle stays
  `mode=ro`, and new work does not open a writable handle in the server process.

- **Every response carries a freshness stamp, and incompleteness rides the success path as a warning
  field rather than as an exception.** Warnings distinguish at minimum `stale`, `degraded`, `gapped`,
  `partial`, and `rule-applied`; an aggregate that deliberately excluded rows says so.
  Why: this is the whole product thesis in one sentence. A hard error is the easy case; the dangerous
  case is a **successful** response computed over incomplete data, because nothing throws and the
  numbers simply stop being true (AC-4.4, AC-9.3). The consumer is an analyst agent that **cannot see
  a caveat which is not in the payload** — documentation, log files, and a health tool it did not
  think to call are all invisible at the moment of answering. Putting incompleteness on the error
  channel would make it invisible exactly when it matters. AC-8.3's rule-applied clause is the same
  argument for exclusions: an exclusion that is not announced is one that gets silently forgotten
  during analysis.

  > **Amendment, 2026-09-10 — `rule-applied` covers every deliberate exclusion, not only an
  > account rule's.** *Statement:* the kind means "rows were excluded from this aggregate on
  > purpose"; the excluding mechanism is named in `detail` rather than fixed by the kind.
  > *Why:* as written, the kind named the one mechanism this build does not have. `account_rules`
  > exists in the schema with no reader and no writer — the rule engine is FR-8 and is unbuilt —
  > while two exclusions that DO happen had no way to announce themselves: a row whose currency is
  > unknown (#86) and one whose amount cannot be represented exactly in minor units (#84). Both
  > are excluded from minor-units aggregates for the same reason an account rule would be, and the
  > vocabulary is closed, so the choice was to widen this kind's meaning or to leave real
  > exclusions silent. Silent exclusion is the failure the kind was created to prevent.
  > *Retroactivity:* none owed. The kind had no emitter to reinterpret — that absence is what #34
  > filed — so nothing already on the wire changes meaning. When FR-8's rule engine lands it
  > becomes a third emitter of the same kind, needing no further amendment.
  > 🔴 *And it moved scope in the same breath:* `rule-applied` sat in `CONNECTION_SCOPED_KINDS`
  > while it had no producer, because *which accounts this store cannot denominate* is standing
  > state. Its emitters are not — they fire only on an answer that computes a total, and only
  > when that answer's own scope holds such an account — so it now sits in
  > `REQUEST_SCOPED_KINDS`. A kind in the connection tuple promises to ride every response
  > equally; once one member of one set behaves like the other's, the absence of ANY kind in
  > either set stops being readable as information, which is the guarantee the split exists to
  > make.
  Status: steady-state.

- **The CLI's three-way exit code is a contract: `0` success, `1` ran and found a problem, `2` could
  not run.** The `1`/`2` distinction is not collapsible.
  Why: the scheduled job invokes the CLI, so these codes are a machine interface, not operator
  ergonomics. Collapsing them makes **a broken scheduler indistinguishable from a degraded feed** —
  which is this product's primary failure mode arriving through the operational door, and the one
  place where an ops shortcut reproduces the exact bug the product exists to prevent.

  > **Amendment, 2026-09-10 — a fourth code, `75` (`EX_TEMPFAIL`), for a run that did not
  > finish.** *Statement:* the vocabulary is `0` success, `1` ran and found a problem, `2` could not
  > run, and `75` ran, found nothing wrong, and still owes work — come back. `sync run` answers `75`
  > for every state in which more history is owed and nothing is broken: `NOT_READY`,
  > `INITIAL_UPDATE_COMPLETE`, and a page run stopped at its ceiling. `1` outranks `75` on a run
  > that produced both. The `1`/`2` distinction is untouched and still not collapsible; an
  > unexpected exception is `2`, which is the existing norm being *obeyed* rather than amended —
  > `1` was never available to a command that did not finish.
  > *Why:* this is an ADDITION to the vocabulary, not a collapse of it, and it rests on the same
  > argument that made `1`/`2` non-collapsible. The CLI is not the MCP envelope. An envelope can
  > carry incompleteness as a `partial` warning field because its consumer reads the payload; a
  > scheduled runner reads the exit code and nothing else. A first sync on a real institution
  > reaches `INITIAL_UPDATE_COMPLETE` — roughly thirty days of a 730-day grant — minutes to hours
  > before the rest lands, and under a bare `0` build step 8's launchd agent cannot tell that from a
  > whole history, so the connection sits at thirty of its 730 days until tomorrow's window. The
  > only mitigation that existed was a paragraph in `docs/first-production-connection.md` § 3.6,
  > which works solely for an operator reading it at that moment. `EX_TEMPFAIL` rather than a fourth
  > small integer, because 75 already means "temporary failure, retry" to every piece of operational
  > tooling that reads exit codes at all. **One code rather than two:** splitting "history complete"
  > from "still arriving" would encode an internal distinction the caller cannot act on differently.
  > *Retroactivity:* owed, and paid in the same commit rather than deferred. § 3.6 asserted *"Exit 0
  > with nothing applied is the expected first result on a real institution"* and is now false; it
  > is rewritten, together with the page-ceiling paragraph beside it, the standing-routine guidance
  > on writing your own `cron`/`launchd` entry, and `operational-spec.md`'s scheduling contract and
  > failure table. Nothing is grandfathered because nothing yet reads these codes in production: the
  > scheduler is unbuilt, which is why this amendment lands **before** build step 8 rather than as a
  > migration after it.
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
  around it. `mcp._refuse_colliding_parameters` sits beside it — two parameters sharing a name with
  different types are a startup failure, not a runtime surprise — because both fail for
  one reason: a surface that cannot be described strictly should not be advertised. The check is
  scoped to ROWS. The stronger rule — every declared property required, everywhere in the schema —
  refuses the shipped surface, because a warning carries `connection_id` only when it is about one
  connection; that is absence-as-information at the envelope level, which this norm does not govern.
  🔴 **Retroactive, and its one migration is PAID:** `spending_summary` was its own tool and is now
  `money_summary`, the grouped aggregate. Every MCP tool is `experimental` (§ Surface Inventory),
  where "removing one is the policy working, not a violation", so the migration cost no version bump
  and opened no deprecation window. No other shipped tool changed shape.
  Born 2026-09-09, from the tool-surface consolidation work
  (`brookstalley/bankmachine#30`, since shipped). Derivation and the alternatives priced against it:
  `discovery-mcp-tool-surface.md`.
  Status: steady-state.

- **A stored balance is reported with its lifecycle, and no total over balances is emitted without
  stating its treatment of non-active accounts.** The treatment is **include and flag**: non-active
  accounts stay in the figure, and the answer states how many they were and what they contributed.
  Why: the second norm's thesis applied to the balance sheet. A closed card's last balance is a
  plausible wrong number whose wrongness has no signal, and the direction of the error depends on
  `balance_class` — including a closed liability overstates debt, and including a closed asset
  double-counts money that already moved into another enrolled account. Neither treatment repairs
  that arithmetic; what the norm forbids is applying either one silently. **Include-and-flag was
  ruled over exclude-and-state on the asymmetry in detectability**: an included frozen balance is
  correctable by any reader handed the flag and the figure, while an exclusion is invisible by
  construction — the total is simply smaller, and no field can point at what is not there. That is
  *classify, do not filter* arriving on this surface — the rule that made a spending total decompose
  its internal transfers and debt service rather than drop them — and the flagged magnitude is what
  keeps it from reproducing that rule's own failure mode instead: a total inflated by items that do
  not belong to it, with nothing in the payload to net them out.
  🔴 **The magnitude is the load-bearing half, not the flag** — a count and a signed sum, present
  and zero rather than absent, on the same `total_external_spend` precedent that makes an exclusion
  legible elsewhere. A warning without the
  figure tells a consumer something is wrong and leaves it unable to do anything about it.
  Born 2026-09-09, from #40, on the owner's ruling of the same date. Derivation, and the argument
  for the treatment that was not ruled: `discovery-account-lifecycle.md` § *The ruling the owner
  owes*. Requirement: AC-12.8.
  🔴 **Retroactivity: one debt, and it is named.** `query._coverage`'s `accounts` count is an
  unfiltered `COUNT(*)` sitting between two counts that both filter, and it is the one shipped
  emitter this norm already governs. It is not grandfathered; AC-12.8 covers it by name.
  Status: steady-state since 2026-09-09, on the condition this entry set for itself. Every emitter
  AC-12.8 names carries the figure — `list_accounts`, `get_coverage_report`, `query_transactions`,
  `money_summary` and `get_pipeline_health` all reach it through one `_coverage`, and the unreadable
  store path carries it present-and-zero — and the guard has been seen red **with the magnitude
  removed**, not merely with the flag flipped. Those are two cases in `verify_norms_go_red.py`
  deliberately, because a flag with no figure passes the norm's letter and fails the reason it was
  born: a consumer told something is included and handed nothing to subtract.
  **Ruling (2026-09-13, owner): net worth over TIME sits at this norm's edge, not inside it.**
  `balance_history` counts an account no longer active through its last capture and not after,
  rather than carrying its last balance forward into later days. The why that chose include over
  exclude does not reach a series: an exclusion was rejected as invisible, since no field can point
  at what is not there. In a series the account's own rows end on its last day, and the answer names
  that day, its signed last balance, and the per-currency count and sum that stopped counting
  (`account_no_longer_active`, beside `coverage.not_active_balance_minor_units`). Where a later
  account row in the same identity partition took the balance over, as a re-link leaves it, the
  answer also names that account and its first day and balance. The claim that a later net worth
  moves with no activity behind it is made only of the part nothing replaced, since across a
  handover net worth moves only by the difference between the two balances. A net-worth day
  strictly between the two ends counts neither account, and is named with the balance it leaves
  out. A successor two stopped accounts would share replaces neither. The figure stays whole. What refusing
  would cost was measured first. On the sandbox store, 14 relinked accounts' last balances equal
  their 14 replacements' to the cent, so carrying them forward serves every later net worth at
  exactly 2×. That is a wrong figure with its correction beside it; the ruling serves the right
  figure with its exclusion named. **The magnitude stays load-bearing**: `verify_norms_go_red.py`
  removes it from the detail and the guard goes red. A total at one instant is untouched and still
  includes and flags, and `list_holdings`' `totals` does exactly that.

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

### MCP tool surface — the nine tools (§5) · *eight built, one specified*

🔴 **Read-only over everything the aggregator produced; no mutation tool reaches a derived row.**
(Vetting a comparable server surfaced 19 mutation tools including `delete_transaction` with no undo.
Not reproducing that.) §5's 2026-09-10 amendment permits exactly one class of write — to sidecar
tables bankmachine itself maintains, which is FR-11's annotations and nothing shipped today. The
norm entry in § Direction carries the amendment and its bound.

🔴 **Aggregate-first (AC-9.1).** Tools return computed summaries by default, not raw rows. Dumping 24
months of transactions into an LLM context is slow, expensive, and *worse at arithmetic than SQL is.*
Raw-row access exists but is paginated and hard-capped.

| Tool | Returns | Safe / idempotent |
|---|---|---|
| `get_pipeline_health` | Per-connection sync status, last success, error codes, per-account coverage window, row counts, staleness flags, rule anomalies | yes |
| `list_accounts` | Accounts with type, institution, mask, current balance, lifecycle state | yes |
| `query_transactions` | Filtered rows (date range, account, category, amount range, merchant search). **Paginated, capped** | yes |
| `query_investment_transactions` | An investment account's activity — trades, income, contributions, withdrawals and fees — windowed on trade date and filtered by account and type. **Paginated, capped**, with totals per currency, type and subtype | yes |
| `money_summary` | Money in and out over a period, grouped by category / merchant / account / month / flow class, per currency, and split by flow class under every grouping | yes |
| `balance_history` | Value over time, per account or aggregated as net worth, investments included through their balances and never by adding positions | yes |
| `list_holdings` | Current investment positions with cost basis where available | yes |
| `find_recurring` | Detected recurring charges with cadence, amount drift, last-seen | yes |
| `get_coverage_report` | Per account: first and last transaction, posting cadence, and trailing silence measured against it | yes |

Every tool is safe and idempotent, trivially — nothing writes.

> **Amendment (2026-09-08, build step 7's first slice; revised 2026-09-09 and 2026-09-13).** 🔴 **Seven
> of these eight ship; one does not yet.** Built: `get_pipeline_health`, `list_accounts`, `query_investment_transactions`,
> `list_holdings`, `balance_history`, `query_transactions`, `money_summary`, `get_coverage_report`.
> Not built: `find_recurring`.
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
> `money_summary` is specified with period-over-period comparison and does not yet offer it — it
> does now carry the merchant, account, month and flow-class groupings, both directions, and
> per-currency rows. *(2026-09-14: `query_transactions` has left this sentence. It is paginated and
> carries every filter AC-9.1 names — category, a signed amount range and a literal text `search` —
> under AC-9.6.)*
>
> The `experimental` tier permits these changes without a version bump. It does not permit them
> going unrecorded, which is what this amendment exists to prevent.

> **Amendment (2026-09-12, investment sync, wave 1).** 🔴 **`list_holdings` no longer waits on
> build step 5; it waits on wave 2 of the investment-sync plan.** Step 5's investments half is
> built: holdings and investment transactions are pulled for every connection whose recorded
> capabilities name the product, the window that came back is recorded, and a `store rebuild`
> reproduces both -- soft deletes included. So the blocker named in the amendment above has moved
> from "no data exists" to "no tool reads it", which is a different kind of gap and a shorter one.
>
> Still not built, unchanged: `balance_history`, `list_holdings`, `find_recurring`. The tool table
> above is the specification and is not amended here -- what changed is which sentence explains the
> distance between it and the code.

> **Amendment (2026-09-13, investment sync, wave 2).** 🔴 **`list_holdings` is BUILT; six of these
> eight ship.** `balance_history` and `find_recurring` remain specified and not built. A row is one
> position as its account's LATEST holdings capture recorded it, under one strict row shape (§ *The
> published field shapes*), and it carries the date of the price the position was valued at beside
> the day it was captured -- the aggregator's sandbox values every position at a 2021 price, so a
> capture date alone would present a years-old value as current. No total is emitted: a total over
> holdings owes the same lifecycle treatment a total over balances does, and is not built.
> **What a holdings answer cannot vouch for rides the success path:** `positions_not_current` when a
> price or a capture is old, `rule-applied` naming a position the store could not record (migration
> 011's `refused_holdings`), and `account_no_longer_active` for a position on an account that has
> stopped being reported. The coverage surface's counts are the TRANSACTIONS feed's and now say so;
> an investment-only account reading uncovered there remains open as #107.

> **Amendment (2026-09-13, investment sync, wave 3).** 🔴 **`balance_history` is BUILT; seven of
> these eight ship.** It keeps its name, and the net-worth question's selection cost is accepted
> and bought back by a description that opens with it. One series, read two ways under one strict
> row shape (§ *The published field shapes*): a row per account per day a balance was captured,
> and a NET-WORTH row per day per currency marked by a null `account_id`. Assets and liabilities
> split by `balance_class`; `net = assets - liabilities` at both levels.
> **A net-worth row exists only for a complete day** -- every account it counts was captured on it
> -- and a withheld one is named under `rule-applied`. An active account counts from its first
> capture onward; an account no longer active counts only through its last capture, which
> `account_no_longer_active` names. A day with no capture is absent at both levels.
> It is windowed, capped and paged like `query_transactions`, but its window is reconciled against
> the days BALANCES were captured, its cursor is a keyset over its own order and refuses a
> transactions cursor, and it carries no `transactions_in_effective_window`. The lifecycle
> treatment the fifth norm requires of a total over balances -- include and flag, with the
> magnitude -- is the plan's Chunk 08; until then a non-active account's exclusion is stated rather
> than quantified.

> **Amendment (2026-09-13, investment sync, Chunk 08).** 🔴 **The lifecycle treatment is paid on
> both new tools.** For each account no longer active, `balance_history` names the last day it
> counted in net worth and its signed last balance, and the account that took the balance over
> where one did. It also gives the per-currency count and sum that stopped counting, split into
> the replaced part and the part nothing replaced, on the ruling under § Direction's lifecycle
> norm. `list_holdings` now
> carries `totals`: per currency, the positions and their market value, with the part on accounts
> that are not `active` counted and valued beside it (§ *The published field shapes*). 🔴 **A
> holdings total DECOMPOSES the balances net worth already counts and is never added to them.**
> Net worth reads the balance series alone, and a test holds it unmoved by any positions. A
> position's day is claimed across `holdings` and `refused_holdings` at write time, so the two
> tables never share a key. `positions_not_current` reads "stopped" from the investments
> domain's last attempt and its newest archived holdings reply, never from calendar days of separate
> stamps, which a sync crossing midnight UTC splits.

> **Amendment (2026-09-13, #107).** 🔴 **An investment account's activity is data on the coverage
> rows.** `list_accounts` and `get_coverage_report` carry `investment_transaction_count` and
> `holdings_as_of` beside `transaction_count`. They name an account under
> `accounts_without_coverage` only when nothing is recorded for it in any feed. `query_transactions`
> and `money_summary` keep naming an account with no transaction, because they answer from that
> feed alone. Trades are counted per account and still served as rows by no tool.

> **Amendment (2026-09-14, investment activity).** 🔴 **`query_investment_transactions` is BUILT;
> eight of these nine ship.** An investment account's activity reaches the aggregator on its own
> feed, so an investment-only institution holds hundreds of trades and no transaction, and until
> this tool no agent could read one. A trade carries a security, a quantity, a unit price and a fee,
> which no transaction row has, so § Direction's shape norm makes it a new tool rather than optional
> fields on `query_transactions` — which `_refuse_optional_row_fields` would refuse at registration.
> Windowed on `trade_date` and clamped to the trades' own span, capped and keyset-paged under its
> own cursor scheme, which refuses a transactions or series cursor. `totals` groups the whole request
> by currency, type and subtype and is never netted into one figure per currency. The sentence above
> this amendment is superseded by it.

> **Amendment (2026-09-14, investment activity, chunk 03).** 🔴 **An investment account is routed, not called absent.** `query_transactions` and `money_summary` name an account with no transaction and with trades or positions under `activity_in_another_feed`, which names the tool that serves it; they name only an account with nothing in any feed under `accounts_without_coverage`, as the listings already did. The #107 amendment's sentence that the transactions tools "keep naming an account with no transaction" is superseded. `accounts_without_coverage` now says, where the account's connection has completed a sync, that the store cannot tell a quiet account from an unreported one, because the aggregator lists only the accounts a sync page touched and so gives no signal for either.

🔴 **Two of these tools are the verification surface, not the analysis surface.** `get_pipeline_health`
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
| `connections reauth` | Repair a connection whose login expired, through an update-mode session — 🔴 the item, cursor, granted window and accounts are all preserved, which is the requirement rather than a side effect | yes (datastore + network); on the connection row it writes `status`, `last_error_code`, `last_error_at` and `updated_at` and **nothing else** — no cursor, no granted window, no `enrolled_at`, no account or transaction row. It also archives each `/item/get` it polled, and derives `source_error_code` / `consent_expires_at` from the ones that belong to this connection |
| `sync run` | Fetch each connection's accounts and transactions since its cursor | yes (datastore + network) |
| `mcp` | Serve the datastore to an MCP client over stdio — 🔴 read-only | no |

🔴 **The three-way exit code is carried on the exception, not decided by the caller.** A refusal type
declares its own code — a full roster and an abandoned enrollment are both `1` — and the base class
defaults to `2`, so a new refusal that forgets produces the safe answer rather than silently claiming
the command ran and found a problem.

**Specified, not yet built** (steps 5–10): `import` and the §7 verification-gate runner. Their
names are not fixed by this document; their *contract obligations* below are.

🔴 **Update-mode re-auth landed as `connections reauth <id>`, not as `sync repair`.** It belongs
beside `list` and `retire` because all three take a connection id and act on that one connection,
and because the refusal an operator meets — *re-running `enroll` counts this institution's history
twice* — has to name a command in the same breath as the listing that shows which id to name. The
sentence above is why that rename needed no amendment: this document fixes obligations, not names.

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
today *or the last transaction date when that is later*. 🔴 **Covered by the series the tool reads:**
`balance_history` is clamped to its first and last CAPTURED day, never to the transactions' span —
balances begin at enrollment and transactions years earlier, so a transactions clamp would claim
coverage over days no balance exists for. Nothing narrows a SQL predicate, and the
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
consumer branching on the key gets a true answer either way. `query_transactions` and
`balance_history` are the capped, paged tools; `money_summary` caps its group list and issues no
cursor, and `rows_truncated` names what each one counts — transactions, series rows or groups.

**`returned` is the count of rows actually in the payload**, derived from the rows themselves rather
than from the caller's `limit` — a `limit` above the hard cap is clamped, so the two are not the same
number. **`matching` is the count the WHOLE request selects**, over the same predicates and the same
tables as the row query, and it does not move as a caller pages. **`remaining` is what was still
ahead of this page** — the same count taken from the cursor's position onward, equal to `matching` on
an unpaged call. **`truncated` is `returned < remaining`**, derived rather than stored: a stored flag
can disagree with the counts beside it, and a derived one cannot. 🔴 It turns on `remaining` rather
than on `matching` because only `remaining` falls to the rows in hand on the last page.

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
back *below* the rows already in hand. **That is a data condition, not an error:** `remaining` floors
at `returned`, because those rows were observed to match and reporting fewer would contradict the
payload beside them, and `matching` floors at `remaining` for the same reason one level out;
`truncated` is then false, which is true, since nothing is being hidden. The
skew is not smoothed away — `counted_during_change` announces it, so a consumer comparing two calls
seconds apart knows a write landed between them. **Refusing to answer here would be the wrong
trade**: it would turn a harmless skew into a failed tool call, which the Direction above forbids in
as many words.

**Cost, measured rather than assumed** — full method, caveats and the figures below in
`.prawduct/artifacts/mcp-count-latency-2026-09-08.md`, which records that these are single-process
warm-cache medians on one developer machine rather than a portable benchmark (2026-09-08, synthetic
stores, 14 accounts over 24 months):
each count runs at roughly the cost of the row query itself — ~1ms at 10k rows, ~89ms at
200k — and a full `query_transactions` call lands at ~18ms / ~435ms respectively, inside the ~1s
target in `nonfunctional-requirements.md` with room to spare at volumes well beyond a real 24-month
store. **The counts therefore ship exact**; the approximate-count fallback that was held in reserve
is not needed and is not built. A *paged* call takes a second count so that `matching` can stand for
the whole request; at the 200k figure above that is one further ~89ms on a call already measured at
~435ms — still inside the target, and paid only by the requests that page.

### A truncated answer carries the route to the rest (#17)

🔴 **A truncated answer carries `truncation.next_cursor`, when and only when `truncated` is true.**
The caller passes it straight back as the optional `cursor` argument of the tool that issued it, with the
same window, account and filters, and repeats until `truncated` is false — at which point no `next_cursor` is
present. **The key's presence is the loop condition**: a consumer pages while it is there and stops
when it is gone, without comparing two counts to decide. Visibility without a route past the cap
would have left the honest answer still unobtainable, which is why #17 needed both halves.

**`cursor` narrows the rows and `remaining`, and leaves `matching` alone.** `remaining` counts what
is left from the cursor's position onward, which is what makes `truncated` go false on the page that
exhausts the window — a loop condition taken over the whole result set would stay true forever and a
caller paging until it went false would never stop. `matching` counts what the whole request selects
and reads the same on every page, because it is the figure a caller QUOTES: measured on the sandbox
store, one name carrying the paged count reported 390, 290, 190, 90 across a walk while
`coverage.transactions_in_effective_window` beside it read 390 throughout, so the answer contradicted
itself and an agent quoting the final page answered "90 transactions" to a question about the year.
🔴 **The two are separate counts and only a paged request pays for both:** an unpaged call asks one
question, so `remaining` and `matching` are one statement and one query.

🔴 **A keyset, never an offset.** The cursor is opaque state over `(posted_date, transaction_id)`,
the total order rows already come back in — on `balance_history`, over `(day, net-worth row first,
account, currency)`, with its own scheme tag so each tool refuses the other's cursor. An offset shifts under a concurrent sync — one insert
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
the count came back below the rows already in hand, so `remaining` floored at `returned`,
`truncated` read false, and no cursor was issued — correct for the numbers in the payload, and possibly short of
the window if enough rows were removed mid-walk. The warning is the telling: ask again for a count
taken after the change. Refusing to answer instead would turn a harmless skew into a failed tool
call, which the Direction above forbids.

### Window-scoped coverage rides beside the store-wide figure, never replacing it

🔴 **`coverage.transactions` stays store-wide.** A windowed answer gains a *sibling*,
`coverage.transactions_in_effective_window`, counted over the effective bounds and never narrowed by
`account_id` — per-account coverage is its own change (#19), and shipping half of it here would leave
that work amending a field this one just added. Narrowing `coverage.transactions` in place would be
the repurpose the evolution rules forbid: a consumer still reading it would get a wrong answer rather
than an error. The sibling is present on exactly the answers windowed over TRANSACTIONS, including
when the datastore cannot be read, so the key set a consumer branches on never depends on the store's
health. `balance_history` is windowed and does not carry it: a count of transactions beside a
balance series would read as a count of its rows.

### An aggregate says which money actually left, and it classifies rather than filters (#18)

🔴 **Every `money_summary` row carries `flow_class`** — `external_spend`, `internal_transfer` or
`debt_service` — and the class is a *grouping dimension under every value of `group_by`*, not a
field that appears on one grouping and is guessed on the others. It has to be: an account holds a
transfer and a coffee, a month holds all three by definition, and even a category can split,
because the category key reads `category_override` first while the class is fixed to read
`source_category_detailed` only. Attaching one row's class to a group that spans classes would
state it for the others, and making the field *optional* is refused by § Direction's fourth norm,
which merges tools only where one strict row schema covers every parameter value. The consequence
is accepted and is the point: one month can return three rows per currency where it returned one.

🔴 **Classify, do not filter.** Every row is kept and gains a class; nothing is dropped, precisely
so there is no invisible undercount. That is also why this emits no `rule-applied` warning —
that kind means an account rule filtered rows *out* of an aggregate, and a tool that excludes
nothing saying so would be a false statement about the answer carrying it.

> **Amendment, 2026-09-10 — the three classes answer whether money crossed the household
> boundary, not what the aggregator called the row.** *Statement:* `internal_transfer` means value
> moved between two accounts **this store holds**, matched leg to leg; `debt_service` means a
> payment toward a liability **this store holds**; everything else is `external_spend`. The class
> is read from `source_category_detailed`, and both non-spending classes require a matched
> opposite leg — equal magnitude, opposite sign, a different enrolled account, same currency,
> `ledger_date` within ±3 days — recorded at derivation time as `transactions.transfer_pair_id`.
> The three class NAMES are unchanged; this surface is stable and a rename would break every
> consumer for no gain.
> *Why:* classifying on the aggregator's primary category alone made the tool answer a question
> nobody asked. Measured on the review's own scenario, it reported **"spent $5,000, income $0"**
> where the truth was ≈$8,900 and $6,000: a mortgage to an unenrolled lender, ATM cash and ACH
> rent were all excluded from spending as though the money had merely moved between the
> household's own accounts, and a payroll deposit categorised `TRANSFER_IN` was excluded from
> income for the same reason. Both errors ran in the direction that gets believed.
> *What this deliberately does NOT do:* it does not split a loan payment into principal and
> interest. The aggregator does not decompose one per transaction, and deriving a split from
> balance movement would be an inference presented as a record — so the whole payment classifies
> together. It also does not net refunds against spending; that remains descoped.
> *Retroactivity:* owed and paid in the same commit. Every figure this tool has ever returned for
> a store holding transfer-shaped rows was computed under the old rule, and no answer is
> re-issued — the change is forward-only. An unmatched transfer-shaped row now counts as
> spending and the answer carries a `partial` warning saying how many, so a caller can see the
> classifier fell back rather than concluded. `store rebuild` recomputes the pairing over the
> whole archive, which is what makes an existing store's answers move to the new rule.

🔴 **The class is read from the source column, never from `category_override`.** An override is
local interpretation of what a transaction was *for*; the flow class is about whose money moved and
in which direction. Letting a re-categorisation reclassify a transfer as spending would reintroduce
the overcount through the back door, silently and in the direction that inflates. An unrecognised
or null category falls to `external_spend`, which is the conservative direction on this surface's
own principle: an overcount gets questioned and an undercount gets believed.

### A classifying tool carries `totals`, per currency, and the three add up

🔴 **`money_summary` gains a `totals` block** — one entry per currency, carrying the window's
`inflow_minor_units` and `outflow_minor_units` and then splitting that *outflow* under each of the
three classes. Quote `outflow_minor_units` when asked how much went out and
`external_spend_outflow_minor_units` when asked about external spend, and **name the other two
classes beside it**: the split describes the outflow rather than filtering it. 🔴 Since the
2026-09-10 amendment the classifier matches a counterparty leg, so a mortgage payment to an
unenrolled lender and an ATM withdrawal ARE `external_spend` and need no allowance made for them.
What still needs saying is the other direction: a transfer-shaped row whose counterparty is an
account nobody enrolled counts as spending, and the answer's `partial` warning says how many.

🔴 **Measured against the sandbox store on 2026-09-09, over its full 24 months:** $267,692.77 of
outflow, of which $164,400.00 is internal transfer and $50,484.00 is debt service — leaving
**$52,808.77 of actual spending, one fifth of the raw figure.** That is the whole of #18 in one
line, and it is why the headline rides the envelope rather than waiting for a caller to derive it
from rows they may never read. The ratio is a property of this fixture and not a constant; what the
contract fixes is that the decomposition is always present, never the size of the gap.

🔴 **The block also carries the hold decomposition (AC-13.1, AC-13.4), and those six keys are
always present.** Beside the three outflow classes, each currency entry states
`pending_transactions` and `pending_net_minor_units` — how much of this figure is authorisation
holds that have not settled — plus `expired_holds` / `expired_holds_net_minor_units` and
`settled_from_hold` / `settled_from_hold_net_minor_units`. Present and zero, never absent: a total
that mixes holds into settled money without saying so is the defect this surface exists to refuse,
and a key that appears only when it is non-zero cannot be branched on. The two exit paths are
distinguished on purpose — an *expired* hold is one that never posted (`pending` with a removal),
while a row that settled and was later withdrawn left as a settled row, and counting the second as
the first would tell a consumer a hold dropped off when a real transaction was retracted.

🔴 **`balance_unreconciled` and `reconciliation_not_applicable` are request-scoped kinds that
ONLY `get_coverage_report` emits, and that restriction is recorded here because their scope class
alone would misdescribe them.** A request-scoped kind promises that its absence is information — it
fires whenever THIS request's scope holds the condition — and on the analysis tools these two never
fire at all. So an unexplained residual makes `money_summary` and `query_transactions` wrong by the
residual over that account, silently, on answers whose silence the scope rule invites a consumer to
read as clean. Two things follow and neither is optional: an agent establishes reconciliation on the
**verification surface before** quoting an analysis figure, which is what that surface is for and
what this document's headline instruction already says; and **extending either kind to an analysis
answer is a change to this restriction, not an addition to a tool** — the row fields are per account
and an analysis answer is not, so the emitter would have to decide which accounts a total drew on.
Recorded rather than fixed by widening, because a kind that fires on some surfaces and not others is
exactly the ambiguity the two scope tuples exist to remove, and the honest repair is to say where it
fires.

🔴 **`get_coverage_report` rows carry `stranded_holds` and `oldest_stranded_hold` (AC-13.5), and
`get_pipeline_health` rows carry `sign_convention` with the counts it was judged on —
`sign_convention_rows_judged` and `sign_convention_rows_positive` (AC-14.2).** The counts ride beside
the verdict for the `silence_ratio` reason: a verdict with no evidence under it is a claim the reader
must take on faith, and this check's whole subject is a claim that was taken on faith once already.
`oldest_stranded_hold`, where present, names `transaction_id`, `posted_date`, `days_pending`,
`amount_minor_units` and `currency` — the id and the age together, because the operator's next move
is to go and look at the transaction.
Both sit on the verification surface rather than on an analysis answer, which is this document's own
per-account ruling below rather than a placement chosen here — and putting the stranded finding on
the analysis answers as well was tried and withdrawn, because it produced a count scoped to the
request beside a hold count scoped to the page, and escaped the non-active suppression its sibling
applies. `oldest_stranded_hold` is null both when there is nothing to report and when the account is
not active: a closed account's hold can never settle and can never be cleared, so the *call to
action* is withheld while the *count* stays as measured.

🔴 **Every `list_accounts` and `get_coverage_report` row carries the lifecycle block (FR-9)** —
`lifecycle`, `closed_date`, `last_seen_in_roster`, `roster_last_observed` — and `coverage` carries
`accounts_not_active` and `not_active_balance_minor_units` beside its `accounts` count, per AC-12.8's
ruled include-and-flag treatment. The balance figure is per currency for the same reason every other
figure here is.

The three sum to the window's total outflow in that currency, and that identity is the contract:
it is what proves the classification *partitions* the rows rather than quietly dropping some.
🔴 **Per currency, never one integer across currencies**, by the ruling that governs every
aggregate here — a summed integer over two currencies is not a wrong number, it is not a number.
The block is present and empty when the datastore cannot be read, exactly as `coverage` is present
and zero, so the key set a consumer branches on never depends on the store's health.

🔴 **An account this store cannot denominate contributes to neither the rows nor the totals, and
`money_summary` says which account and why.** Two states reach it: an account whose `currency` is
null, because the aggregator has never stated one, and an account whose currency has no known
minor-unit exponent, so no amount in it can be expressed exactly. Adding either to a figure in a
unit that IS known would produce a number that means nothing — the arithmetic succeeds and the
result is meaningless — so their rows are excluded from every figure in the answer and the exclusion
rides `rule-applied`, whose `detail` names the account ids and, where the code is known, the code.

The disclosure is computed from `accounts` rather than from the rows the answer returned, and that
is load-bearing: an account whose unit has no known scale typically has **no derivable rows at
all**, because each was refused at derivation, so a scan of the returned rows would find nothing
excluded and report nothing. These are the kind's first two emitters, per § Direction's amendment
of 2026-09-10.

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
`days_silent`, `silence_ratio`, `silence_exceeds_cadence`, `interior_gaps` and `interior_gap_detail`, and `source_breakdown` by provenance.
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

### The published field shapes

The sections above argue the design; this one is the reference. **Every field the shipped tools put
on the wire is described — here, or in a section above that already describes it.** That is not an
aspiration: `tests/preferences/test_the_documented_wire_is_the_published_one.py` walks every
`outputSchema` at every depth and fails on a published name this document does not carry. The guard
checks that a name is *accounted for*, never that its description is *accurate*; accuracy is the
Critic's, under the norms in § Direction.

#### The extraction contract

🔴 **These tables are the authoritative statement of row shape, and this subsection is what makes
them machine-readable.** It exists because this document backticks tool names, warning kinds, column
names, CLI subcommands and ordinary prose terms as well as fields — so *"a backticked token is a
field"* is false here, and a substring sweep over the whole document cannot recover the documented
field set. Anything reconciling documentation against the wire in the other direction reads these
tables and nothing else:

- **Authoritative tables:** every table in this section, and only these. A table elsewhere in this
  document — the warning vocabulary, the CLI exit codes, the caps, the conventions — describes
  something that is not a response field, and reading one as a field list is the mistake this
  paragraph exists to prevent.
- **How a table is recognised:** each is introduced by a bolded caption beginning with the literal
  word **Fields**, followed by an em dash and the path of the block it describes; and each carries
  the header row `| Field | Type | Means |`.
- **Which column holds the name:** the first, and only the first. It holds the field's own name in
  backticks and **nothing else** — never a path, never a parent-qualified spelling, never two names
  in one cell. Stripping the backticks from a first cell therefore yields a field name exactly, with
  no parsing.
- **How a nested block is spelled:** a block gets **its own table**, whose caption carries the
  dotted path from the response root (`coverage.not_active_balance_minor_units[]`,
  `rows[].source_breakdown`) while its rows still hold bare names. The block also appears as a row
  in its parent's table, typed `object` or `array of object`. So every name is written once per
  block it occurs in, and a reader never has to take a path apart to get a field name back out.
- **`[]` in a caption means the table describes one ELEMENT** of that array, not the array itself.
- **One caption may name several paths** when the blocks are the same shape (`effective_window`'s
  two halves are the clearest case). The paths are comma-separated; the rows below are the shape
  all of them have.
- **The same name in two different blocks is two rows in two tables**, deliberately.
  `current_minor_units` is a balance on an account row and a per-currency subtotal inside
  `coverage`; `transactions` is a store-wide count in `coverage` and a per-group count on a
  `money_summary` row.
  Collapsing either pair into one entry would document one of the two and silently imply the other.
- **A tool named in a caption is the only tool that carries that block.** Where no tool is named,
  the block rides every response.

**What these tables deliberately do NOT state: whether a field is required.** That is the schema's
to say, and § Direction's fourth norm already fixes it for rows — every row field is required,
nullable where it has nothing to say, never absent. Absence carries meaning only at the envelope,
where each case is argued in a section above (`truncation` absent means *this tool is not capped*;
`effective_window` absent means *this tool takes no window*) rather than compressed into a column
here.

**Fields — the response root.**

| Field | Type | Means |
|---|---|---|
| `environment` | string | which datastore answered, so a fixture cannot pass for real money |
| `as_of` | string | when this answer was assembled, ISO-8601 UTC. Captured *after* the rows, which is what keeps the effective window from narrowing under them |
| `build` | object | which code answered — build provenance, not an API version |
| `warnings` | array of object | 🔴 every reason this answer is less complete than it looks. **Read before drawing a conclusion:** an answer can be perfectly well-formed and still be computed over incomplete data, which is the failure this whole surface exists to make impossible to miss. Empty is a real and common answer |
| `coverage` | object | what the store HOLDS, which is how an empty answer is told from an empty world |
| `rows` | array of object | the answer itself. One shape per tool, tabled below |
| `effective_window` | object | windowed tools only (`query_transactions`, `query_investment_transactions`, `money_summary`, `balance_history`) |
| `truncation` | object | capped tools only (`query_transactions`, `query_investment_transactions`, `money_summary`, `balance_history`) |
| `totals` | array of object | `money_summary`, `list_holdings` and `query_investment_transactions` only — three different blocks, tabled apart below |

**Fields — `build`.**

| Field | Type | Means |
|---|---|---|
| `version` | string | the package version this process was built from |
| `commit` | string, nullable | the git commit it was built from. Null means the build could not be identified — and then `dirty` is null too, never `false` |
| `dirty` | boolean, nullable | whether uncommitted changes were present in that build |

**Fields — `warnings[]`.**

| Field | Type | Means |
|---|---|---|
| `kind` | string | one value of the closed vocabulary in § *The warning vocabulary*. Tolerate one you do not recognise and surface it; the list is a minimum |
| `detail` | string | the measured specifics of this one caveat, in a sentence |
| `connection_id` | integer | the connection this caveat is about. Present only when it is about one — absence here is information |
| `institution` | string | that connection's institution name, present on the same condition. It rides beside the id because an operator with ten institutions cannot act on a bare "some data is stale" |

**Fields — `coverage`.**

| Field | Type | Means |
|---|---|---|
| `connections` | integer | live connections in the store — retired ones are not counted |
| `accounts` | integer | every account, INCLUDING the ones no longer active. Read `accounts_not_active` beside it rather than assuming this figure was filtered |
| `accounts_not_active` | integer | how many of `accounts` are closed or no longer reported. 0 means every account is still being reported |
| `not_active_balance_minor_units` | array of object | what those accounts contribute to any total over balances. Empty means they contribute nothing |
| `transactions` | integer | non-removed transactions **store-wide**, never narrowed by the question asked. On a windowed answer read `transactions_in_effective_window` beside it |
| `earliest_transaction` | string, nullable | the oldest posted date held anywhere in the store, `YYYY-MM-DD`; null when the store holds no transaction |
| `latest_transaction` | string, nullable | the newest posted date held anywhere in the store, `YYYY-MM-DD`; null when the store holds no transaction. 🔴 Store-wide like `transactions`, so it is **not** the last date in this answer's window and a caller must not read it as one |
| `transactions_in_effective_window` | integer | windowed tools only: how many rows the window this answer actually covered holds. Never narrowed by `account_id` — per-account coverage is its own field on the rows |
| `derivation` | object | `get_pipeline_health` only (AC-5.4): `current_version`, the derivation version this build stamps, and `versions_in_store`, every version a derived row in the store carries, ascending. Anything in the list other than `current_version` raises `derivation_version_mismatch`. An empty list means the store holds no derived row, or could not be read — the `partial` warning beside it says which |

**Fields — `coverage.not_active_balance_minor_units[]`.**

| Field | Type | Means |
|---|---|---|
| `currency` | string | the currency this subtotal is in. Per currency, never one integer across currencies |
| `current_minor_units` | integer | what the closed and no-longer-reported accounts holding this currency contribute to any total over balances, in MINOR UNITS and operator-signed. Quote it beside any balance total you report — it is the figure that lets a reader do the subtraction this surface refuses to do for them. Not the balance of any one account |

**Fields — `effective_window`** *(`query_transactions`, `money_summary`)*.

| Field | Type | Means |
|---|---|---|
| `requested` | object | the window the caller asked for, verbatim — both bounds null on an unbounded request. It is kept beside `effective` so that a clamp is *visible* rather than something the caller has to infer from a figure that came back smaller than expected |
| `effective` | object | the window the answer was actually computed over. Both bounds null means the window asked for and the data this store holds do not overlap |

**Fields — `effective_window.requested`, `effective_window.effective`.**

| Field | Type | Means |
|---|---|---|
| `since` | string, nullable | the window's inclusive start, `YYYY-MM-DD` |
| `until` | string, nullable | the window's inclusive end, `YYYY-MM-DD` |

**Fields — `truncation`** *(`query_transactions`)*.

| Field | Type | Means |
|---|---|---|
| `returned` | integer | rows actually in this payload, counted from the rows themselves rather than from the caller's `limit` |
| `remaining` | integer | rows this request still had ahead of it when this page began — the whole result set on an unpaged call, and what lies from the cursor's position onward on a resumed one. It falls as a caller pages and reaches `returned` on the last page. Floors at `returned` |
| `matching` | integer | rows the WHOLE request selects, over the same predicates and tables as the row query with the cursor predicate deliberately left off. 🔴 It does **not** move as a caller pages, so `returned` stays below it on the final page and `returned < matching` is not a loop condition. This is the figure to quote for "how many transactions match". Floors at `remaining` |
| `truncated` | boolean | `returned < remaining`, derived rather than stored |
| `next_cursor` | string | opaque state to pass back as `cursor` for the next page. Present when and only when `truncated` is true, so its presence is the loop condition |

**Fields — `rows[]`** *(`list_accounts`)*.

| Field | Type | Means |
|---|---|---|
| `account_id` | integer | this store's own id for the account, and the value `query_transactions(account_id=…)` takes. Opaque — it is not the institution's id |
| `institution` | string | the institution's name as this store records it. A display name, never a key: two connections at one institution share it, so it identifies nothing on its own |
| `name` | string | the account's own name, as the institution reports it. Operator-facing text — it can change under a caller and is not an identifier |
| `mask` | string, nullable | the last four digits of the account number, and 🔴 **the only fragment of an account number stored anywhere in this product**. Null when the institution reports none |
| `type` | string | the account's kind — 🔴 **in the SOURCE's vocabulary, retained verbatim and not normalised.** Two aggregators would spell the same kind differently, and translating would invent a value nobody reported. Do not branch arithmetic on it: `balance_class` is the field that carries whether the balance is owned or owed |
| `subtype` | string, nullable | the source's finer classification, on the same terms; null when it reports none |
| `balance_class` | string | `asset` or `liability` — this store's own classification of which way the balance points, operator-correctable and the field a report partitions on |
| `current_minor_units` | integer, nullable | the latest recorded balance, in MINOR UNITS and operator-signed, so a card balance is negative. Null when no balance has ever been recorded for the account. 🔴 Read it with `lifecycle`: on a non-active account this figure is FROZEN and is not a fact about today |
| `currency` | string, nullable | the currency that balance is in; null on the same condition as the balance |
| `balance_as_of` | string, nullable | the date of the balance snapshot `current_minor_units` came from, `YYYY-MM-DD`; null when there is none. 🔴 It is a property of the BALANCE, not of the answer — `as_of` on the envelope says when the answer was assembled, and on a stale or non-active account the two are far apart. That distance is the whole signal |
| `first_transaction_date` | string, nullable | the oldest transaction recorded for this account, `YYYY-MM-DD`. 🔴 Null means NO TRANSACTION HAS EVER BEEN RECORDED, never "no activity" |
| `history_starts` | string, nullable | Where this account's CONNECTION was granted history from. 🔴 It is what makes `first_transaction_date` readable: alone, that date cannot distinguish a recently opened account — a TRUE zero before it — from one whose history was TRUNCATED BY THE GRANT, where everything earlier is absent. A first transaction within 7 days of this date is read as truncation; materially after it, the account genuinely begins there. The tie goes to truncation on purpose: calling absent data a true zero is the error that gets believed. Null means the granted window is not yet measured — the question is unanswerable, never that the account is covered |
| `consent_expires_at` | string, nullable | When the operator's authorisation for this connection lapses. 🔴 After it does, data stops arriving with **no failure to notice** — the pipeline is poll-only, so expiry otherwise surfaces as a failed run rather than in advance. A `partial` warning fires within 14 days of it and a `degraded` one once it has passed. Null means the connection has not been polled since this was recorded — never that consent does not expire |
| `source_error_code` | string, nullable | The aggregator's STANDING complaint about this connection. 🔴 Not `last_error_code`, which records the last sync *attempt* failing: a connection can be unwell while the most recent poll succeeded, and folding the two together would let one success bury a complaint nobody resolved. Non-null raises `degraded` |
| `last_transaction_date` | string, nullable | the newest transaction recorded for this account, `YYYY-MM-DD`; null on the same condition |
| `transaction_count` | integer | how many transactions this store holds for the account. `0` rather than null, because a null here would be a second spelling of the same fact. 🔴 Counted from the TRANSACTIONS feed alone: investment trades are not in it, so an investment account can read `0` here. `investment_transaction_count` and `holdings_as_of` beside it say what the store holds for such an account |
| `investment_transaction_count` | integer | how many investment trades (buys, sells, dividends, fees) this store holds for the account, soft-deleted ones excluded. `0` rather than null, for `transaction_count`'s reason. The trades themselves are served as rows by `query_investment_transactions` |
| `holdings_as_of` | string, nullable | the newest day a position was captured for this account, `YYYY-MM-DD`. On an account whose newest capture recorded positions, it is the day `list_holdings` names as their `as_of_date`. Null means no position has ever been captured for it, never "holds nothing today". A position refused for its unit is not a capture here; `list_holdings` names it under `rule-applied` |
| `lifecycle` | string | `active`, `closed`, or `no_longer_reported` — see § *A classifying tool carries `totals`* and FR-9. `no_longer_reported` names an OBSERVATION and not a closure; `closed` is the operator's own declaration and is the only value that asserts one |
| `closed_date` | string, nullable | when the operator recorded this account as closed; null when none has been recorded, **including** for an account that is merely no longer reported |
| `last_seen_in_roster` | string, nullable | the date this account was last listed by its institution; null when there is no roster observation behind this account: an import-only account (FR-7) has no connection, and an aggregator account's connection has none until its first sync after migration 004. 🔴 A null is silence, never a statement that the account is import-only — read `lifecycle` and the row's own provenance for that. A DIFFERENT fact from `last_transaction_date` and often a much later one — neither may be derived from the other |
| `roster_last_observed` | string, nullable | the date this account's institution's roster was last successfully observed; null for an import-only account, and equally for any aggregator account whose connection's roster has never been observed — which is every one of them during the upgrade window migration 004 opens. Read against `last_seen_in_roster`: the two being equal is what makes an account `active`, and the earlier one is the whole derivation of `no_longer_reported`, so the verdict can be re-derived from the row without a second call |

**Fields — `rows[]`** *(`list_holdings`)*.

| Field | Type | Means |
|---|---|---|
| `account_id` | integer | this store's id for the account holding the position — the same value `list_accounts` publishes |
| `account` | string | the account's NAME. Display text, not a key: two accounts can share it, and `account_id` is the join |
| `security_id` | integer | this store's id for the instrument, converged on the aggregator's own id for it, so one security held in two accounts carries one value. Opaque |
| `security_name` | string, nullable | the instrument's name as the institution reports it; null when none is reported |
| `ticker` | string, nullable | the instrument's ticker symbol; null when none is reported |
| `security_type` | string, nullable | the institution's own classification of the instrument, retained verbatim and not normalised |
| `quantity` | string | 🔴 the position size as EXACT DECIMAL TEXT, never a JSON number: fractional shares are routine and a float drops digits before anyone can look. Parse it as a decimal |
| `market_value_minor_units` | integer | the position's value on `as_of_date`, in MINOR UNITS of `currency` — a valuation, rounded half-even to the minor unit. 🔴 It DECOMPOSES the account's balance and never adds to it, and a sum over one account's positions need not equal that balance |
| `cost_basis_minor_units` | integer, nullable | what the position cost, in MINOR UNITS; null means the institution supplied none — never zero |
| `currency` | string | the currency both figures are in |
| `as_of_date` | string | the day this position was CAPTURED, `YYYY-MM-DD` — the most recent capture for its account, which is not today on a connection that has not synced today |
| `price_as_of` | string, nullable | 🔴 the date of the PRICE the value was computed at, `YYYY-MM-DD`, and the only date the institution puts on a position. It can be years before `as_of_date`. Null means the date is UNKNOWN — the row predates migration 010 and the store has not been rebuilt, or the institution sent none — and never that the price is as recent as the capture |
| `lifecycle` | string | the holding account's lifecycle, as on `list_accounts`: a position on an account that is not `active` froze on the day it was captured |
| `closed_date` | string, nullable | as on `list_accounts` |
| `last_seen_in_roster` | string, nullable | as on `list_accounts` |
| `roster_last_observed` | string, nullable | as on `list_accounts` |

**Fields — `totals[]`** *(`list_holdings`)*. One entry per currency, summed from `rows`. Every key
is present and zero where nothing qualifies, and the block is present and empty when there are no
rows.

| Field | Type | Means |
|---|---|---|
| `currency` | string | the currency this entry's figures are in. One entry per currency, never one integer across currencies |
| `positions` | integer | how many rows are in this currency |
| `market_value_minor_units` | integer | the sum of those rows' `market_value_minor_units`, in MINOR UNITS. 🔴 It DECOMPOSES the investment accounts' balances, which `balance_history` and `list_accounts` already count: never add it to a balance or a net worth, and do not expect it to equal those balances. It INCLUDES positions on accounts that are not `active`. A position named under `rule-applied` is not in it, and cost basis is not totalled, since a sum over the positions that state one is a wrong figure with no signal |
| `not_active_positions` | integer | how many of those positions are on an account that is not `active`. `0` is a real answer |
| `not_active_market_value_minor_units` | integer | what those positions are worth, SIGNED, in MINOR UNITS: the part of `market_value_minor_units` that froze with its account, stated so a reader can subtract it |

**Fields — `rows[]`** *(`query_investment_transactions`)*. One row per stored trade, newest first; removed trades are not rows.

| Field | Type | Means |
|---|---|---|
| `investment_transaction_id` | integer | this store's own id for the trade. Opaque, and the tiebreak of the order rows come back in |
| `account_id` | integer | the investment account the trade is on, as `list_accounts` publishes it, and the value the `account_id` argument takes |
| `account` | string | the account's NAME. Display text, not a key: `account_id` is the join |
| `trade_date` | string | the day the institution dated the trade, `YYYY-MM-DD`. A calendar fact. The window is applied to it and rows are ordered on it. It does not move once stored |
| `investment_type` | string | the institution's kind of activity, verbatim — `buy`, `sell`, `cash`, `fee`, `transfer`, `cancel` in the aggregator's vocabulary, not normalised. The `investment_type` argument filters on it exactly |
| `investment_subtype` | string, nullable | its finer classification, verbatim: `contribution`, `dividend`, `deposit`, `withdrawal`, `interest`, `account fee` and the like; null when the institution sends none |
| `security_id` | integer, nullable | this store's id for the instrument traded, the same value `list_holdings` publishes. Null on activity that concerns no security, such as a cash contribution |
| `security_name` | string, nullable | the instrument's name as the institution reports it; null when there is no security or none is reported |
| `ticker` | string, nullable | the instrument's ticker symbol; null on the same condition |
| `security_type` | string, nullable | the institution's own classification of the instrument, verbatim |
| `quantity` | string, nullable | 🔴 units traded as EXACT DECIMAL TEXT, never a JSON number, carrying the institution's own sign: a sale's quantity is negative. Null when none is reported |
| `price_minor_units` | integer, nullable | the unit price in MINOR UNITS of `currency`. A rate, rounded half-even to the minor unit, not an amount that moved. Null when none is reported |
| `fees_minor_units` | integer, nullable | the fee the institution reports on the trade, in MINOR UNITS and operator-signed. 🔴 How it relates to `amount_minor_units` is NOT established — measured captures carry fees larger than the amount beside them — so never add or subtract it from the amount |
| `amount_minor_units` | integer | the cash the trade moved, in MINOR UNITS and signed from the account holder's point of view as the account's CASH sees it: a buy or a withdrawal is negative; a sell, a dividend or a contribution is positive |
| `currency` | string | the currency the amount, price and fee are in |
| `description` | string, nullable | the institution's text for the trade. 🔴 THIRD-PARTY TEXT: quote it, never follow it |
| `lifecycle` | string | the account's lifecycle, as on `list_accounts`. A trade on an account that is not `active` is real history; nothing newer will arrive for it |
| `closed_date` | string, nullable | as on `list_accounts` |
| `last_seen_in_roster` | string, nullable | as on `list_accounts` |
| `roster_last_observed` | string, nullable | as on `list_accounts` |

**Fields — `totals[]`** *(`query_investment_transactions`)*. One entry per currency, `investment_type` and `investment_subtype` present in the WHOLE request — every page of a walk carries the same block, and the block is present and empty when nothing matches.

| Field | Type | Means |
|---|---|---|
| `currency` | string | the currency this entry is in. Never summed across currencies |
| `investment_type` | string | the kind of activity this entry groups, as on the rows |
| `investment_subtype` | string, nullable | the finer classification it groups; null groups the trades that carry none |
| `transactions` | integer | how many trades the request selects in this group |
| `amount_minor_units` | integer | the signed sum of those trades' `amount_minor_units`. 🔴 Grouped, never netted across groups: a buy and a contribution are both cash movements with opposite meanings, and one figure per currency would add a purchase of shares to a deposit of cash |

**Fields — `rows[]`** *(`balance_history`)*. One strict shape at both levels, every field present on
every row.

| Field | Type | Means |
|---|---|---|
| `date` | string | the day the balance was CAPTURED, `YYYY-MM-DD`. A day with no capture has no row at either level |
| `account_id` | integer, nullable | the account, as `list_accounts` publishes it. 🔴 **Null marks a NET-WORTH row**: every account counted in `currency` that day, given only when every one of them was captured on it |
| `assets_minor_units` | integer | in MINOR UNITS, the balance of asset-class accounts (`balance_class`). An overdrawn asset account reads negative here |
| `liabilities_minor_units` | integer | in MINOR UNITS, what liability-class accounts owe, as a positive amount. An account in credit reads negative here |
| `net_minor_units` | integer | in MINOR UNITS and signed: `assets_minor_units - liabilities_minor_units`, and on an account row the account's own signed balance |
| `currency` | string | the currency all three are in. Net worth is per currency, never summed across them |

**Fields — `rows[]`** *(`query_transactions`)*.

| Field | Type | Means |
|---|---|---|
| `transaction_id` | integer | this store's own id for the transaction. Opaque, and the id `get_coverage_report`'s `oldest_stranded_hold` names when it points at one |
| `account_id` | integer | this store's id for the account the transaction is on — the same value `list_accounts` publishes, `get_coverage_report` keys on, and the `account_id` argument takes. 🔴 It is the only join between a row and the account it belongs to: `account` beside it is display text that two accounts can share |
| `account` | string | the NAME of the account the transaction is on, not its id. 🔴 It is display text and not a key — filter with the `account_id` argument, which is what selects rows; two accounts can carry the same name and this field would not tell them apart |
| `date` | string | the transaction's POSTED date, `YYYY-MM-DD`. A CALENDAR FACT and never an instant (§ Conventions). 🔴 **It moves.** The source reports a charge's authorisation date while it is pending and its posting date once it settles, so the same transaction can carry a different `date` between two syncs. It answers *when did this arrive*; `ledger_date` answers *which period does this money belong to*, and the effective window is applied to that one |
| `ledger_date` | string, nullable | the day the money was committed from the account holder's point of view — the authorisation date where the institution reports one, else the posting date. Stamped once at derivation and **never moved by settlement**, which is what makes a monthly total stable under re-sync. Rows are ordered on it, the window filters on it, and every returned row's `ledger_date` lies inside `effective_window.effective`. 🔴 **Null means the row predates this column and the datastore has not been rebuilt — never that the money was committed on the posting date.** A null row is excluded from every window, and a `partial` warning on the answer names how many and the command that fixes it |
| `description` | string | the institution's own string for the transaction, and 🔴 **the authoritative one.** When it and `merchant` disagree, this is the one that came from the bank |
| `merchant` | string, nullable | the aggregator's guess at a merchant name, 🔴 **unvalidated** — it is a normalisation the aggregator performed and this product did not check. Null when it offered none. Grouping `money_summary` by merchant falls back to `description` where this is null, so one merchant can split across several raw institution strings and each rollup understates it |
| `amount_minor_units` | integer | the amount in MINOR UNITS, signed from the account holder's point of view: negative is money out |
| `currency` | string | the currency the amount is in |
| `pending` | boolean | this row is an authorisation hold that has not settled. A pending amount can settle at a different figure or expire without settling, so a total computed over these rows can move with no new activity — which is what `includes_pending_rows` warns about |
| `category` | string, nullable | the category this transaction is filed under: 🔴 **the operator's override where one exists, and the source's category otherwise.** Read `category_is_override` beside it to know which you are looking at. Null when neither exists |
| `category_is_override` | boolean | whether `category` came from the operator rather than from the source. It matters beyond provenance: `flow_class` on `money_summary` is fixed to read the source's DETAILED category only, so an overridden row can be grouped under one category and classed as though it were under another — and that is deliberate, because a re-categorisation must not be able to reclassify a transfer as spending |

**Fields — `rows[]`** *(`money_summary`)*.

| Field | Type | Means |
|---|---|---|
| `group_key` | string | the group this row is for, 🔴 **always a string whatever the grouping** — an account id rendered as text under `group_by=account`, a `YYYY-MM` month under `month`, the category or merchant name under those, and the flow class itself under `flow_class`. It is the key to act on: under `account` it is the value `query_transactions(account_id=…)` takes, once read as an integer |
| `group_label` | string | the same group, named for reading. Equal to `group_key` under every grouping except `account`, where the key is the id and the label is the account's name. Never a second key — two accounts can share a label |
| `currency` | string | the currency this row's figures are in. Rows are per currency, because a figure summed across currencies is not a wrong number, it is not a number |
| `flow_class` | string | `external_spend`, `internal_transfer` or `debt_service` — a GROUPING DIMENSION under every value of `group_by`, so one month or one account can return up to three rows. Read from `source_category_detailed` only, never from an override. 🔴 It says **whether the money crossed the household boundary**: `internal_transfer` and `debt_service` both require a matched counterparty leg on an account this store holds, so an ATM withdrawal, a payment to a person and a mortgage to an unenrolled lender are all `external_spend` |
| `transactions` | integer | how many transactions this group holds. 🔴 A per-group count, and a different figure from `coverage.transactions`, which is store-wide and never narrowed by the question asked |
| `inflow_minor_units` | integer | money IN over this window for this group, 🔴 **a POSITIVE MAGNITUDE** in minor units — not operator-signed. The sign convention is carried by `net_minor_units`; these two are the halves it is made of |
| `outflow_minor_units` | integer | money OUT over this window for this group, likewise a positive magnitude. It is the figure the `totals` block decomposes by flow class |
| `net_minor_units` | integer | `inflow_minor_units` minus `outflow_minor_units`, signed from the account holder's point of view: negative is money lost over the window |
| `pending_transactions` | integer | how many of this group's rows are authorisation holds that have not settled. `0` is a real answer and the key is always present |
| `pending_net_minor_units` | integer | the part of `net_minor_units` that is NOT settled money, signed the same way. A hold can settle at a different figure or expire without settling, so this is how far this row can move with no new activity at all. 🔴 Never quote a group as money spent without saying what part of it is this |

**Fields — `totals[]`** *(`money_summary`)*.

| Field | Type | Means |
|---|---|---|
| `currency` | string | the currency this entry's figures are in. One entry per currency, never one integer across currencies |
| `inflow_minor_units` | integer | everything that came IN over the whole window in this currency, a positive magnitude. 🔴 **Inflow is not income:** refunds sit in it under `external_spend`, and a paycheque can sit in it under `internal_transfer` |
| `outflow_minor_units` | integer | everything that went OUT over the whole window in this currency, a positive magnitude and before any classification. 🔴 **The figure to quote when asked how much went out** |
| `external_spend_outflow_minor_units` | integer | the part of `outflow_minor_units` the aggregator categorised as neither a transfer nor a loan payment — the closest figure to external spend, and a residual rather than a verification |
| `internal_transfer_outflow_minor_units` | integer | the part that moved between two accounts THIS STORE HOLDS, 🔴 **matched leg to leg** — equal magnitude, opposite sign, a different enrolled account, same currency, within three days. An ATM withdrawal, a P2P payment and rent paid by ACH are NOT in it; they are `external_spend`, because from the household's point of view that money is gone |
| `debt_service_outflow_minor_units` | integer | the part the aggregator categorised as a loan or card payment. A card payment settles purchases counted under their own categories **only if that card is enrolled**; a mortgage, auto or student-loan payment is money out |
| `pending_transactions` | integer | how many of the rows behind these totals are authorisation holds that have not settled. `0` is a real answer |
| `pending_net_minor_units` | integer | what those holds come to, SIGNED — the amount these totals could move by when the holds settle or expire, with no new activity at all |
| `expired_holds` | integer | holds in this window that were withdrawn without ever posting. 🔴 They are EXCLUDED from every figure here, so a total that shrank against an earlier answer is explained by this rather than by missing data |
| `expired_holds_net_minor_units` | integer | what those withdrawn holds came to, signed — the amount that left these totals by expiring |
| `settled_from_hold` | integer | rows in this window whose amount arrived by settling an earlier hold. A settlement may differ from the hold, so these are the rows whose contribution *changed* rather than appeared — which is why they are counted apart from `expired_holds` rather than with them |
| `settled_from_hold_net_minor_units` | integer | what those settled rows come to, signed |

🔴 **The three class figures add up to `outflow_minor_units` in that currency, and that identity is
the contract** — it is what proves the classification *partitions* the rows rather than quietly
dropping some, and it is why the whole-window figure is published beside them rather than left to a
caller to add up.

**Fields — `rows[]`** *(`get_pipeline_health`)*.

One row per connection, retired ones included, and 🔴 **a row is returned even when everything is
fine** — "healthy" is an answer, and an empty result would be indistinguishable from a broken query.

| Field | Type | Means |
|---|---|---|
| `connection_id` | integer | this store's own id for the connection, and the id a warning names in its own `connection_id` |
| `institution` | string | the institution this connection is to. A display name, not a key — a retired connection and its live replacement at the same institution share it |
| `status` | string | `active`, `degraded`, or `retired`. 🔴 `degraded` means the LAST sync attempt failed and nothing has succeeded since — read `last_success_at` beside it for how long that has been true, because `degraded` alone does not distinguish an hour from a month |
| `last_success_at` | string, nullable | when this connection last completed a sync run in full, ISO-8601 UTC; null when it never has. 🔴 It advances only on a COMPLETE run: a run that fetched pages successfully and stopped mid-history clears the error state without moving this, because this is the field the staleness warning reads and advancing it would report a connection current while it is behind |
| `last_error_code` | string, nullable | what the most recent failure was, null when the last attempt succeeded. The aggregator's own code, verbatim, wherever it sent one; otherwise one of the codes this product records on its own behalf, listed below. Treat an unrecognised value as a value rather than as a defect — the aggregator adds codes |
| `requested_history_days` | integer, nullable | how many days of history was asked for when this connection was enrolled; null when nothing was requested. Read `granted_history_days` against it — the shortfall between them is a known gap, not an absence of data |
| `granted_history_days` | integer, nullable | how many days the institution actually granted, measured from the oldest transaction it returned. 🔴 Null means NOT YET MEASURED, never "no shortfall", unless `granted_history_status` says otherwise |
| `granted_history_status` | string | which kind of value `granted_history_days` is: `measured` (it carries the number); `not_yet_measured` (null, nobody has counted); or `no_transactions_to_measure` (null, because the connection's backfill completed with no transaction, as for an institution whose accounts post nothing to the transactions feed — nothing in that feed can have been cut, so the unmeasured-window `partial` caveat does not ride the answer; the connection's other domains still report their own state). Keyed on the connection's `last_success_at`, which is stamped only once the aggregator reports the history complete |
| `history_starts` | string, nullable | the oldest date this connection's history reaches back to, `YYYY-MM-DD`; null before any history has been measured. It is the date `granted_history_days` was counted from, so it answers "how far back can I ask?" without arithmetic |
| `retired` | boolean | this connection has been retired: it is no longer synced, its history is kept, and it was removed at the aggregator. 🔴 Its accounts and transactions are still in every figure this surface reports, so a retired connection's rows are history rather than absence — and its `last_success_at` will never advance again |
| `sign_convention` | string | `consistent`, `inverted`, or `undetermined` — whether this connection's stored amounts point the way the rest of the store's do, measured over categories that are never plausibly money arriving. 🔴 `undetermined` means NOT CHECKED, never "fine". An `inverted` connection is reported and never corrected |
| `sign_convention_rows_judged` | integer | rows the verdict was computed over: this connection's non-removed transactions in those categories with a non-zero amount. Under 8 the verdict is `undetermined` |
| `sign_convention_rows_positive` | integer | how many of those are stored positive. `0` is the conforming reading; equal to `sign_convention_rows_judged` is a wholly inverted feed. The counts ride beside the verdict because a verdict with no evidence under it is a claim the reader must take on faith, and this check's subject is a claim that was taken on faith once already |
| `domains` | array | 🔴 **A connection is not one stream.** One entry per sync domain this connection has ever ATTEMPTED, each reporting how that domain is doing on its own — because `sync_state` is keyed on `(connection, domain)` and the domains advance on their own schedules. A connection can be `active` with a fresh `last_success_at` while one domain has not landed in weeks, and every field above it would still read healthy (AC-4.4). An EMPTY array means nothing has ever been attempted for this connection; a domain missing from a non-empty one has never been attempted, which is not the same as one present with a null `last_success_at`. The array rides the connection's own row rather than becoming rows of its own, so a consumer counting connections still counts each one once |

**Fields — `rows[].domains[]`** *(`get_pipeline_health`)*.

One entry per sync domain the connection has ever attempted. 🔴 **Three of these names also appear on the connection row above and mean the same KIND of fact at a narrower scope** — `last_success_at`, `last_error_code` and `history_starts` are the connection's when read there and this domain's when read here. Read them from the level you asked about; a domain entry never speaks for the connection, and the connection row never speaks for a domain.

| Field | Type | Means |
|---|---|---|
| `domain` | string | which class of data it is about — `transactions` or `investments`. Treat an unrecognised value as a domain this build gained after the reader learned the list, not as a defect |
| `last_attempt_at` | string, nullable | when this domain was last tried, ISO-8601 UTC, whatever came of it |
| `last_success_at` | string, nullable | when this domain last got **everything it asked for**, ISO-8601 UTC. 🔴 Null means it has been tried and has NEVER landed in full — the hole is this domain's whole history (AC-4.5) — never "fine". It advances only on a complete pull, so a connection whose positions arrived while its investment-transaction window came back short leaves this exactly where it was |
| `last_error_code` | string, nullable | what the last attempt at this domain failed with, null when it succeeded — the same vocabulary as the connection's own. 🔴 Present here while the connection's own `status` is `active` is the **expected** shape rather than a contradiction: one domain failing is not a statement about the login, so it does not degrade the connection and `connections reauth` repairs nothing |
| `last_error_at` | string, nullable | when that failure happened, ISO-8601 UTC. Read against `last_success_at` beside it — the span between them is how long this domain has been stopped, which is the figure AC-4.5 says must be computable rather than guessed |
| `history_starts` | string, nullable | the oldest date this domain's own history reaches back to, `YYYY-MM-DD`; null before any complete pull has measured one |

**Codes this product records in `last_error_code`** *(connection and domain scope)*.

Recorded only when the aggregator sent no code of its own: a failure that never reached it, or a
refusal whose body carried none. 🔴 **Never a Python class name**, which would publish implementation
detail and change under a rename. The set is closed and disjoint from the aggregator's vocabulary, so
a reader can tell whose code they are reading
(`tests/preferences/test_the_failure_code_vocabulary_is_closed.py`).

| Code | Means | What to do |
|---|---|---|
| `CREDENTIAL_UNREADABLE` | the connection's access token could not be read from the keychain | restore the keychain entry; nothing at the aggregator is wrong |
| `AGGREGATOR_UNREACHABLE` | the aggregator did not answer, so nothing about the request was judged | usually nothing — the next run retries; check the network if it persists |
| `AGGREGATOR_NOT_CONFIGURED` | this product's aggregator credentials or settings are not usable, and no code came back | fix the configuration; retrying with the same values cannot help |
| `AGGREGATOR_REFUSED_WITHOUT_CODE` | the aggregator refused, and its answer carried no code | read the log line for that run; the refusal's own sentence is there |
| `RESPONSE_UNUSABLE` | the aggregator answered with something this build cannot use | a build defect or a changed response shape; report it, retrying will not help |
| `DATASTORE_LOCKED` | another writer held the datastore — typically `store backup` running beside the sync | nothing; the next run picks up where this one stopped |
| `DERIVATION_FAILED` | a response was archived and could not be derived | read the log for the raw response id, and `sync shell` for its body |
| `DATASTORE_FAILED` | the datastore refused a write for another reason | `store status`, and the log line for that run |

**Fields — `rows[]`** *(`get_coverage_report`)*.

One row per account. It is built on the **same producer** as `list_accounts`' rows — the first nine
fields are that row's identity and lifecycle, restated here so a verification answer is readable
without a second call — and carries the analysis `list_accounts` does not.

| Field | Type | Means |
|---|---|---|
| `account_id` | integer | this store's own id for the account, as on a `list_accounts` row |
| `account` | string, nullable | the account's own name — the SAME field name as on a `query_transactions` row and the same kind of value: display text, not a key. 🔴 It is typed nullable here where the `list_accounts` row's `name` is not, and the null is **not reachable today**: the stored column is `NOT NULL` and this tool reports on exactly the accounts it is read from. The nullability is the producer's defensiveness rather than a state a consumer can meet, and it is recorded that way rather than given a reason it does not have. Identify the account by `account_id` beside it |
| `first_transaction_date` | string, nullable | as on a `list_accounts` row: null means NO TRANSACTION HAS EVER BEEN RECORDED, never "no activity" |
| `last_transaction_date` | string, nullable | as on a `list_accounts` row |
| `transaction_count` | integer | as on a `list_accounts` row; `0` is a real answer |
| `investment_transaction_count` | integer | as on a `list_accounts` row |
| `holdings_as_of` | string, nullable | as on a `list_accounts` row |
| `lifecycle` | string | as on a `list_accounts` row: `active`, `closed`, or `no_longer_reported` |
| `closed_date` | string, nullable | as on a `list_accounts` row |
| `last_seen_in_roster` | string, nullable | as on a `list_accounts` row |
| `roster_last_observed` | string, nullable | as on a `list_accounts` row |
| `median_interval_days` | number, nullable | this account's own posting cadence in days; null under two transactions, because no interval exists rather than because it posts daily. `0` is a real answer and means the opposite of null: the account posts more than once a day |
| `days_silent` | integer, nullable | days since the last recorded transaction; null when there is none |
| `silence_ratio` | number, nullable | `days_silent` against this account's own cadence, the divisor floored at one day. A NUMBER rather than a flag on purpose: 28 days silent on a 30-day cycle is genuinely borderline, and a boolean is what would hide that |
| `interior_gaps` | integer | Holes INSIDE this account's history — runs where the feed stopped and restarted — counted against its own cadence: longer than three cycles AND at least seven days. Both conditions, because the multiple alone flags a long weekend on a daily account and the floor alone flags every normal month on a monthly one. 🔴 `days_silent` cannot see these at all: it measures from the last transaction to today, so a three-month hole mid-history leaves it at 1 while a monthly total shows a collapse that never happened. Present and 0, never omitted. This is the count as MEASURED |
| `interior_gap_detail` | array | The widest of those gaps, each one an object of `from`, `to`, `days` and `ratio`, where `ratio` is the gap against this account's cadence. Numbers rather than a flag, for the same reason as `silence_ratio`. Capped per account, so a list shorter than `interior_gaps` means the remaining gaps were narrower than the ones shown |
| `from` | string | On an `interior_gap_detail` entry: the last date the feed posted before the gap |
| `to` | string | On an `interior_gap_detail` entry: the first date it posted after it |
| `days` | integer | On an `interior_gap_detail` entry: the gap's length. The evidence under `ratio`, so a caller can apply its own threshold rather than this one |
| `silence_exceeds_cadence` | boolean | a full posting cycle has been missed (ratio above 1) by an account still being reported. 🔴 Always false for a non-active account, whose silence is closure rather than a hole — `silence_ratio` beside it still carries the measurement, so nothing is hidden |
| `stranded_holds` | integer | authorisation holds on this account still unsettled past any ordinary hold lifetime. Present and `0`, never omitted. A hold this old usually means the merchant never captured it, so the money is neither spent nor available |
| `oldest_stranded_hold` | object, nullable | the worst of them, so the operator can go and look at it; null when there are none, and 🔴 also null for a non-active account, whose holds can never settle and can never be cleared. `stranded_holds` beside it still carries the count, so the measurement is not withheld — only the call to action nobody could answer |
| `source_breakdown` | object | this account's rows by provenance |
| `reconciliation_state` | string | whether AC-11.2's balance-to-transactions check could be RUN for this account, and why not when it could not: `reconciled`, `not_applicable_investment`, `insufficient_snapshots`, `no_balance_recorded`. 🔴 `reconciled` means the comparison was PERFORMED, not that it came back clean — the residual beside it carries the verdict. 🔴 Its own required field rather than an inference from a null residual: three of these states would otherwise share one null, and an UNRECONCILABLE account is not an UNRECONCILED one |
| `residual_minor_units` | integer, nullable | the net of (change in balance − sum of transactions) over every interval compared, in minor units, operator-signed. `0` is the expected value and the claim this product makes. Null EXACTLY when `reconciliation_state` is not `reconciled` |
| `intervals_compared` | integer | how many consecutive-snapshot intervals were actually compared. 🔴 The honest denominator: a residual of `0` over `0` intervals is green by vacuity, and this is what tells the two apart. Present and `0` whenever no comparison was made. 🔴 **This is the TOTAL and `unreconciled_intervals` is a SUBSET of it — they do not partition, so never add them:** `5` compared with `2` unreconciled means five were checked and two of those did not balance, never seven. Named for what the producer measures rather than paired with its own subset, because the paired spelling reads as a partition and is not one |
| `unreconciled_intervals` | integer | how many OF THOSE intervals have a nonzero residual, as MEASURED — a subset of `intervals_compared`, never a second category beside it. The list below is capped, so a shorter list means the rest were not enumerated |
| `unreconciled_detail` | array | those intervals, oldest first, each an object of `from_date`, `to_date`, `balance_change_minor_units`, `transactions_sum_minor_units`, `residual_minor_units`, `cause` and `currency`. Present and empty when the account reconciles |
| `balance_currency` | string, nullable | the unit `residual_minor_units` is denominated in; null when no balance was ever recorded. An aggregate over residuals groups by this, per this document's rule that a total over stored amounts carries a count and a signed magnitude per currency |

**Fields — `rows[].unreconciled_detail[]`** *(`get_coverage_report`)*. One interval whose balance movement the transactions recorded in it do not explain.

| Field | Type | Means |
|---|---|---|
| `from_date` | string | the opening snapshot's date. Transactions are counted over `(from_date, to_date]` — half-open, closed at the top: a transaction posted ON the opening date is already inside that snapshot's balance, so counting it again would double it |
| `to_date` | string | the closing snapshot's date, included for the mirror reason — its effect is in the closing balance and nowhere else |
| `balance_change_minor_units` | integer | `current` at `to_date` less `current` at `from_date`, operator-signed |
| `transactions_sum_minor_units` | integer | the sum of `amount_minor` over the interval. 🔴 POSTED rows only (`pending = 0`) and soft-deleted rows excluded — the aggregator's `current` is the settled balance, so both sides of this comparison exclude pending (`api-notes-plaid.md` §§27-28) |
| `residual_minor_units` | integer | `balance_change_minor_units` − `transactions_sum_minor_units`. Nonzero by construction on every entry in this list |
| `cause` | string | the narrowest true explanation: `window_truncated` (the interval begins before the aggregator's granted history, so those transactions were never fetchable), `coverage_gap` (the transactions feed does not reach this interval), or `unexplained` — which is the finding: money moved and nothing recorded it |
| `currency` | string | the unit both amounts are denominated in. Intervals pair only snapshots sharing one, so a subtraction never crosses units |

**Fields — `rows[].oldest_stranded_hold`** *(`get_coverage_report`)*.

| Field | Type | Means |
|---|---|---|
| `transaction_id` | integer | the hold's id, as `query_transactions` reports it — the id and the age travel together because the operator's next move is to go and look at the transaction |
| `posted_date` | string, nullable | the date the hold was placed, `YYYY-MM-DD`; null when the store holds none for it |
| `days_pending` | integer | how long it has been pending, in days |
| `amount_minor_units` | integer | the hold's amount, in minor units and operator-signed |
| `currency` | string | the currency that amount is in |

**Fields — `rows[].source_breakdown`** *(`get_coverage_report`)*.

🔴 **Provenance is exclusive and total** — every stored row has exactly one of these two sources, so
the two counts sum to a figure over the account's rows and neither is a subset of the other. AC-7.4
requires that manually imported rows stay distinguishable from aggregator-sourced ones in every
result; this block is where that survives onto the verification surface.

| Field | Type | Means |
|---|---|---|
| `aggregator` | integer | rows for this account that arrived through a connection's sync. Present and `0` rather than omitted, so `0` cannot be confused with unknown |
| `manual` | integer | rows for this account that were imported by the operator rather than fetched. Present and `0` on the same terms. 🔴 A non-zero count here on an account whose connection is silent is the case where `days_silent` is measuring the operator's habits rather than the institution's |

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
| `stale` | Last sync older than expected — of a contributing connection, or of ONE SYNC DOMAIN of one that is otherwise syncing. `detail` says which, and which scope's `last_success_at` in `get_pipeline_health` the figure is as of |
| `degraded` | A contributing connection is in error, or ONE SYNC DOMAIN of an otherwise healthy connection is. `detail` says which. 🔴 At domain scope the connection's own `status` is `active` and its `last_error_code` is null — the code lives in `rows[].domains[]`, and `connections reauth` repairs nothing |
| `gapped` | A known coverage hole in the queried window |
| `partial` | A contributing account has bounded history, no connection is enrolled, or ONE SYNC DOMAIN of a healthy connection has never landed in full. `detail` says which |
| `derivation_version_mismatch` | Derived rows in the store carry a derivation version other than this build's (AC-5.4), so some of what any answer reads may not be what this build's derivers would produce from the same archive. `detail` names the versions. Older rows name `bankmachine store rebuild` as the remedy; newer rows mean this server is older than the build that derived them. Store-wide, so it rides every answer like the kinds above; `coverage.derivation` on `get_pipeline_health` carries the same fact structured |
| `rule-applied` | Rows were excluded from this aggregate ON PURPOSE, so the figure will not reconcile against a raw sum over the same window. `detail` names which rows and why. On `list_holdings`, a position the store could not record is ABSENT from the rows and so from `totals`, and `detail` names its account, security and unit |
| `window_starts_before_coverage` | The window asked for reaches back past the first covered date |
| `window_extends_past_coverage` | The window asked for reaches past the covered end — today, or the last transaction when that is later |
| `rows_truncated` | The request matched more rows than the cap returned, and the answer holds only the newest of them |
| `counted_during_change` | A write landed between the row read and the count read, so the two describe moments a fraction apart |
| `accounts_without_coverage` | An account in the scope of THIS request has nothing recorded in ANY feed: no transaction, no investment trade, no captured position. `detail` separates two states. Where its connection has never completed a sync, an empty result means data not present, never no activity. Where it has, the feeds that would carry the account completed and returned nothing, and the store cannot tell an account with no activity from one whose institution does not report it, so the answer is *no recorded activity*, not missing data and not a sync fault |
| `activity_in_another_feed` | On `query_transactions` and `money_summary`, which answer from the transactions feed: an account in scope holds no transaction because its activity is recorded in the investments feed. `detail` names the accounts and routes to `query_investment_transactions` and `list_holdings`. Its absence from this answer is where the data lives, not missing data |
| `account_no_longer_active` | An account in the scope of THIS request is closed or is no longer listed by its institution, so its balance is frozen as of the date beside it and is not a fact about today |
| `positions_not_current` | A position in THIS answer is not a current value: its price is more than four calendar days older than the day it was captured, its price date is unknown, a newer investments pull of its connection listed no position for its account (which may hold none of it now), or the connection's investments feed has stopped: its last investments attempt brought no holdings reply back. `detail` keeps the four apart and names the accounts; a null price date is unknown, never recent |
| `includes_pending_rows` | This answer's rows include authorisation holds that have not settled, so a figure computed from it may change without any new activity |
| `roster_observed_empty` | A connection contributing to THIS request had its roster read successfully and it listed no accounts at all. Every account on that connection is separately marked `no_longer_reported`; this kind is the connection-level anomaly beside that account-level truth, and it is what distinguishes a whole household closing its accounts from a feed that returns success and no rows |
| `sign_convention_unverified` | This answer draws on a connection whose stored sign distribution was measured and found INVERTED relative to the operator-signed convention, so its amounts run the wrong way. 🔴 It does not fire for a merely unconfirmed connection — see the note below the table |
| `search_is_literal` | THIS request narrowed by `search`, which matches literally, so a transaction whose text abbreviates or respells the counterparty is not among the rows. It fires on every searched answer, empty or not, because a search that found some of what it looked for reads as complete |
| `balance_unreconciled` | An account in THIS request's scope has an interval between two balance snapshots whose change in balance is not equal to the sum of the transactions recorded in it, and no coverage gap or truncated window accounts for the difference. `detail` names the accounts, the interval count and the net magnitude, grouped by currency. 🔴 Unlike every other kind here, which describes data that is absent, late or narrowed, this one says the figures in the answer are INTERNALLY CONSISTENT and may still be wrong by the amount named |
| `reconciliation_not_applicable` | An account in THIS request's scope is an investment account, whose balance moves with the market rather than with recorded activity, so the balance-to-transactions check does not apply to it. Its absence from the residuals is by construction, not a gap. Deliberately not `rule-applied`: that kind is about rows excluded from a FIGURE, this one about an account that cannot be verified at all |

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
uncovered account. On `list_accounts` and `get_coverage_report` that is a listed account with
nothing in any feed. On `query_transactions(account_id=N)` it is the account asked about having no
transaction, and on `money_summary` any account in its scope having none.
🔴 **What "uncovered" means follows what the tool answers from, and that is the rule, not an
inconsistency.** A listing describes the account, so an investment account whose trades or positions
are recorded has data and is not named. `query_transactions` and `money_summary` can only return
the transactions feed, so for them the same account's empty answer really is data not present, and
naming it is still true (#107).

🔴 **`sign_convention_unverified` fires on a MEASURED inversion, not on the absence of a
verification — and its name is the weaker of the two readings.** The name was fixed by a discovery
pass before the check was designed, and the check that shipped is narrower than the name suggests:
it reports a connection whose stored sign distribution was measured and found inverted, not every
connection whose direction the operator has yet to confirm against a known deposit (AC-14.7). The
narrow reading was chosen and the name kept, for three reasons worth recording so the tension is not
rediscovered as a bug.

First, the broad reading is the `gapped` failure again. Until AC-14.7 is performed no connection has
been confirmed against a known inflow, so a warning on that condition would ride nearly every answer
this product gives, character-for-character identical — true, and useless for telling a caller
whether *this* answer is the affected one, which is the defect `envelope.py`'s two-tuple split exists
to prevent.

Second, the unconfirmed-but-not-inverted state **is** disclosed; it is disclosed on the right
surface. Every connection's verdict — `consistent`, `inverted`, or `undetermined` where there is too
little to judge — is published per connection on `get_pipeline_health`. That is the verification
domain, and "we have not established this yet" is a verification answer. A warning riding an analysis
aggregate is for something that changes how *this* figure should be read.

Third, the name still tells a consumer the right thing to do, which is what a `kind` is for: do not
trust this connection's direction. The measured specifics ride `detail`, and the guidance served at
`warnings://reference` states the inversion explicitly. **Renaming remains open and is cheap** — no
consumer exists, and new codes are additive under the evolution rule below — but it is a contract
decision rather than a cleanup, and it should be taken with AC-14.7's result in hand rather than
before it.

🔴 **This table is prose and `envelope.WARNING_KINDS` is the code, and since 2026-09-09 something
does hold them together:** `test_the_warning_vocabulary_is_closed.py::test_the_contract_table_lists_every_kind_the_vocabulary_defines`
reconciles the two in both directions. Before it existed the drift was one-directional and silent —
a kind added to the vocabulary but not to this table went unnoticed, which is how
`accounts_without_coverage` was missing here for a full work cycle after it shipped, and how these
three rows went stale within one commit of being written. The guard matches on the kind NAME only, so
a row's annotation is free to change as its kind acquires an emitter without the guard having an
opinion about it.

🔴 **The last three kinds were proposed by two separate discovery passes that could not see each
other, and each registered only its own.** They entered `envelope.REQUEST_SCOPED_KINDS` **as a set,
in one commit, before any of the three items was built** — because a kind emitted but not declared
does not degrade to an unrecognized warning, it takes the whole answer down through the schema
validator. Doing that up front is also what let the three items be built in parallel at all: one
closed tuple cannot absorb three simultaneous additions from agents that cannot see each other. All
three now have emitters. Sources:
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

#### The error codes

| Code | Means | Worth retrying? |
|---|---|---|
| `invalid_argument` | The call named an argument this tool does not take, or a value it cannot use | Yes, with a corrected call |
| `datastore_unservable` | This build cannot serve this datastore, and data is in it. The message names the state and the operator's remedy | Not until the operator acts |
| `internal_error` | Something failed and was logged. No detail crosses the boundary | No |

🔴 **`datastore_unservable` exists so a fixable state does not wear the label that means "retry is
pointless".** The distinction the vocabulary carries is whether the caller can do anything, and
folding this into `internal_error` — whose remedy sentence is *the failure has been logged* — buries
a state the operator clears with one command under one they cannot act on at all.

🔴 **A datastore that is simply NOT THERE is the one unhealthy state that answers rather than
refuses**, carrying zeroed coverage and a `partial` warning. AC-ARCH.3 asks for that state to be
*reported* — *"the MCP server starts successfully when the datastore is empty or missing, and
reports that state through `get_pipeline_health` rather than crashing"* — and a store with no data in
it has nothing to misreport. Every other unservable state means data exists and could not be read.

**Measured 2026-09-09, and the reason this section grew a table.** A store holding 14 accounts and
388 transactions, at schema version 2 against a build serving 4, answered *every* tool with
`isError: false`, `rows: []` and every coverage figure zero — because one line applied the
missing-store carve-out to all five states `connection.inspect` distinguishes. An agent that does
not parse `warnings` reported that the household owned nothing. Both halves are now pinned by go-red
cases: one breaks the carve-out, the other removes the refusal.

### CLI exit codes — a stable contract, already shipped

| Code | Name | Meaning |
|---|---|---|
| `0` | `EXIT_OK` | Success |
| `1` | `EXIT_UNHEALTHY` | 🔴 **Ran fine; the answer is "unhealthy."** `store status` on a missing or empty datastore |
| `2` | `EXIT_ERROR` | The command could not run — config error, keychain failure, connector failure, or an unexpected exception |
| `75` | `EXIT_RUN_AGAIN` | 🔴 **Ran fine, nothing is wrong, and work is still owed — come back.** `EX_TEMPFAIL`. `sync run` answers it for `NOT_READY`, for `INITIAL_UPDATE_COMPLETE`, and for a page run stopped at its ceiling |

🔴 **The 1/2 split is load-bearing and must not be collapsed.** launchd needs to distinguish *"the job
ran and found a problem"* from *"the job could not run."* Merging them would make a broken scheduler
indistinguishable from a degraded feed, which is the silent-staleness failure mode arriving through
the operational door.

🔴 **`75` is an addition to that vocabulary, ratified as an amendment in § Direction on 2026-09-10.**
It exists because the scheduled runner reads the exit code and nothing else: a first sync on a real
institution reaches `INITIAL_UPDATE_COMPLETE` — thirty of a granted 730 days — minutes to hours
before the rest lands, and under a bare `0` launchd cannot tell that from a whole history. `1`
outranks `75` on a run that produced both: the stuck connection needs a person, the arriving one
needs only the next run.

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

**MCP tools** — all `experimental` until the §7 verification gate passes. Eight of
the nine are implemented (`get_pipeline_health`, `list_accounts`, `list_holdings`,
`balance_history`, `query_transactions`, `query_investment_transactions`, `money_summary`,
`get_coverage_report`) and one is still
specification only;
see the amendment under the tool table above for what is descoped and why. `experimental` therefore
means two different things in this list, and the distinction is worth keeping in view: for the
shipped tools it means *this may break*, and for the rest it means *this does not exist yet*:

- `get_pipeline_health` — experimental
- `list_accounts` — experimental
- `query_transactions` — experimental
- `query_investment_transactions` — experimental
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
- `bankmachine store key export` — experimental
- `bankmachine store key verify` — experimental
- `bankmachine store key import` — experimental
- `bankmachine sync shell` — stable
- `bankmachine connector check` — experimental
- `bankmachine connector set-secret` — experimental
- `--config` — stable
- `--verbose` — stable
- exit codes `0` / `1` / `2` — stable
- exit code `75` — experimental, by the same *shipped and depended on* criterion that grades
  `store backup`: it was added on 2026-09-10 and its one intended consumer, build step 8's
  launchd agent, does not exist yet. The meaning is fixed by the § Direction amendment; the
  tier records that nothing has yet read it in anger

The `connector` commands are `experimental` because the connector is mid-build (step 2) and its
command shape may still move; `store init`/`status`/`rebuild` and `sync shell` shipped in step 1 and
are depended on. 🔴 **`store backup` is `experimental` despite being a `store` command**, because
the inventory's criterion is *shipped and depended on*, not *which noun it starts with* — it was
written on 2026-09-07 and nothing schedules it yet; its restore path was rehearsed against sandbox
on 2026-09-10 but never against production. The
`Retention:` rule defers removal of a `stable` member to a major, so grading a day-old command
`stable` would bind the surface to a shape nobody has used in anger.

🔴 **The three `store key` verbs are `experimental` by that same criterion, and one of them carries a
guarantee the tier does not weaken.** They shipped 2026-09-10 under FR-12 and no operator has yet
recovered a real machine with them. What is NOT provisional is `export`'s refusal to write to a
non-terminal stdout: it is what keeps AC-10.1 true now that the key can leave the keychain at all,
so it is a property of the contract rather than of this tier. A future revision may change the
verbs' names or output; it may not make the export quietly pipeable.

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
