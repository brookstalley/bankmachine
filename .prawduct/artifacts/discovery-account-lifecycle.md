# Discovery — A balance that is no longer a fact about today

**Work cycle:** account lifecycle on the analysis surface · medium · requirement
**Opened:** 2026-09-09
**Closes (proposed):** brookstalley/bankmachine #40
**Precondition:** item 6 of `.prawduct/artifacts/mcp-production-readiness.md` § "What has to be true
before yes"

---

> 🔴 **Everything in this document is PROPOSED and awaiting the owner's ratification — the AC-12.x
> criteria included.** It was drafted by a delegate working from the tree and the existing artifacts,
> not from an owner interview. Nothing here is settled contract, nothing here amends a shared
> artifact (every change it wants is a proposal in the final section), and no requirement here binds
> a builder until the owner ratifies it. The one product ruling this item turns on is left **visibly
> open** with a recommendation and its trade-offs; it is not decided here. Read the confidence
> section before treating any claim as load-bearing — the measured findings and the proposals are
> deliberately separable, and only the findings are facts.

---

## The questions this item has to answer

**What problem are we solving?** `list_accounts` reports a balance for every account and says nothing
about whether the account still exists. A closed credit card and a paid-off loan keep contributing
their last recorded balance to any net-worth sum a consumer builds, indefinitely, with no field in
the payload marking them as retired. This is the readiness document's named failure class arriving on
the balance sheet: a plausible number, well-formed, with no signal that anything is wrong.

**What does success look like?** A consumer reading one `list_accounts` response can tell a live
account from a retired one, can tell *how* the system knows, and cannot build a net-worth total that
silently includes a frozen balance without being told it did.

**What is out of scope?** Retiring a *connection* (already built — `bankmachine connections retire`);
investment position lifecycle (build step 5, unstarted); and any inference about *why* an account
disappeared beyond what the aggregator actually reports.

### 🔴 The question this item does not answer, and must not answer silently

#40 scopes it out in one sentence — *"what net worth should **do** with a closed account — exclude,
or include and flag — is a product ruling the owner owes."* That framing is right and it is repeated
here rather than resolved, because resolving it inside a requirements pass is exactly the failure the
scope-out was guarding against. It is put to the owner in § *The ruling the owner owes*, with a
recommendation, the argument against the recommendation, and the two ACs written so **either** answer
is adoptable without rework.

---

## Premise verification — what the issue claimed, and what the tree says

#40 is a polished issue with a Problem / Actual / Expected / Key-fact structure, and the previous work
cycle established that this shape is *more* likely to be believed rather than less: #39's central
premise about the code turned out to be false and the polish is part of why nobody checked. So every
claim was checked. Measured against `develop` at `2aa36ac` on 2026-09-09.

| # | Claim in #40 | Verdict |
|---|---|---|
| 1 | `store/schema.py:108` — `Column("lifecycle_status", Text, nullable=False)` | **Holds, exactly, line and all** |
| 2 | `store/migrations/core_schema.py:174` — `CHECK (lifecycle_status IN ('active','inactive'))` | **Holds, exactly, lines 174–175** |
| 3 | the same table carries `closed_date` | **Holds** — `schema.py:111`, DDL line 178, with `CHECK (closed_date IS NULL OR closed_date >= first_seen_date)` |
| 4 | `derivers.py` treats `lifecycle_status` as operator-owned via `_OPERATOR_OWNED` | **Holds** — `connector/plaid/derivers.py:95`, and an update deliberately excludes it |
| 5 | `query.list_accounts` at `src/bankmachine/query.py:1332` emits no lifecycle column | **Half holds.** The behaviour is exactly as described; the address is stale |
| 6 | "a read-path change plus a field on the published `outputSchema` — not a data-model change and not a migration" | 🔴 **FALSE, and it is the load-bearing claim** |

### Claim 5 — the address, stated precisely so it is not mistaken for a bigger error

`list_accounts` is at **`src/bankmachine/query.py:536`** today; the file is 1305 lines, so line 1332
does not exist. The number was **correct when the issue was written**: at `fd1e921` the file was 2101
lines and `list_accounts` sat at exactly 1332. The envelope-module refactor (`68d8f55`, merged as
PR #45) moved it. So this is line drift across one merge, not a wrong claim — and it is the planning
guide's *line-number scoping* trap arriving in an issue body rather than a build plan. The behaviour
the line was pointing at is unchanged and verified: the `SELECT` names `account_id`, `institution`,
`name`, `mask`, `account_type`, `account_subtype`, `balance_class`, `current_minor`, `currency` and
the latest `as_of_date`, and the row dict adds #19's three coverage fields. **No lifecycle column is
selected and none is emitted.**

### 🔴 Claim 6 — the issue's central inference is wrong, and the correction is the whole item

#40 reasons: the columns exist, `_OPERATOR_OWNED` names one of them, therefore *"only the read is
missing."* Every step is individually checkable and the conclusion does not follow. **Nothing in this
product has ever written `inactive` to `lifecycle_status`, and no path exists by which anything
could.** Three independent confirmations:

1. **The deriver hardcodes it.** `derivers.py:815` — `"lifecycle_status": "active"` — is the only
   assignment in `src/`. On update it is excluded from the `values` dict, so the column is
   *write-once at insert, to the constant `active`*.
2. **The code says so, in the comment immediately above `_OPERATOR_OWNED`** (`derivers.py:86–94`):
   🔴 *"Nothing retires one yet, and that is a real gap rather than an oversight this list closes.
   An account that stops appearing in `/accounts/get` stays `active` with a frozen balance."* The
   same obligation is recorded a second time as unfinished business in
   `build-plan-connector-v1.md` § obligation 3.
3. **There is no operator path.** The CLI's subcommands are `connector`, `connections`, `enroll`,
   `store`, `sync` (`src/bankmachine/cli/`). There is no `accounts` command and no other write
   surface — the MCP server is read-only by norm and by file handle. `connections retire` sets
   `connections.status='retired'` and `retired_at` (`cli/connections.py:353`) and **does not touch
   `accounts` at all**, so retiring a connection leaves all of its accounts `active` with frozen
   balances. `sync shell` can issue raw SQL, which is a debugging affordance, not a product path.

So `_OPERATOR_OWNED` does not mean the operator owns it *in practice*; it means derivation has
agreed not to clobber a value **that nothing can set**. The set is `{"balance_class",
"lifecycle_status"}` and only `balance_class` has a real owner today.

**Consequence for scope.** `lifecycle_status` is `'active'` for every row in every datastore this
product has ever produced. Reading it onto the wire would ship a field that is a constant — which is
worse than shipping nothing, because a constant-`active` lifecycle field is an *affirmative claim*
that every account is live, and consumers would rightly trust it. #40's own repro step ("set an
account's `lifecycle_status` to `inactive`") is only performable by hand-editing the database.
**This item's design must cover how the state gets there, or it makes the product more confidently
wrong rather than less.**

### Two further findings the issue does not contain

🔴 **`get_coverage_report` shipped without the lifecycle read that `data-model.md` requires of it.**
`data-model.md` § Account lifecycle carries a 🔴 rule: *"A retired account's dormant period must not
read as a permanent coverage gap. The coverage report reads `lifecycle_status` and `closed_date` so a
post-closure silence is identified as closure, not reported as a hole."* `query.coverage_report`
(`query.py:855–965`) reads neither column. It computes `days_silent` and `silence_ratio` from
`last_transaction_date` against today for every account alike, so a closed account will report
`silence_exceeds_cadence: true` forever — the precise defect the rule was written to prevent, on the
tool whose whole job is to be believed at the moment someone decides whether to trust an answer.
`discovery-mcp-answer-scope.md` § *The entanglement that changes the build order* declared this a
correctness precondition of chunk C2 and folded it into scope; **C2 shipped and the entanglement was
not discharged.** That is not a criticism of the shipped work, which closed #19 as specified — it is
the reason #40 is larger than filed.

🔴 **No guard could have caught this, and that is a finding in its own right.** Both requirements
documents already promise the field: `docs/system-requirements.md` §5's AC-9.1 table says
`list_accounts` returns *"type, institution, mask, current balance, **lifecycle state**"*, and
`api-contract.md`'s tool table says the same words. `tests/preferences/test_the_documented_tool_surface_is_the_built_one.py`
compares the *specified tool-name set* against the *built tool-name set* and asserts built ⊆
specified. **It says nothing about row fields.** So a documented row field that never reached the
wire is invisible to every mechanism in this repo, which is exactly how a field promised in two
contracts stayed unbuilt across two work cycles without anything going red.

---

## What the connector can actually know, which is less than "closed"

Account lifecycle is **not reported by this aggregator**, and this was verified against the pinned
SDK rather than inferred from documentation.

`plaid/model/account_base.py` in the pinned SDK declares exactly: `account_id`, `balances`, `mask`,
`name`, `official_name`, `type`, `subtype`, `verification_status`, `verification_name`,
`verification_insights`, `persistent_account_id`, `apy`, `holder_category`. **There is no `closed`,
no `status`, no `closed_at`, no lifecycle field of any kind.** `api-notes-plaid.md` §14 recorded the
same list from the live sandbox response. The SDK ships `new_accounts_available_webhook` and
`user_account_revoked_webhook`; there is no account-closed webhook, and this product consumes no
webhooks at all.

**The only signal that exists is absence: an account stops being listed.** Three facts make that
signal usable, and one makes it ambiguous.

- **The observation is taken on every run.** `cli/sync_run.py:244` calls `accounts_get` first, every
  connection, every sync — the comment says why: *"accounts open, close and are renamed, and a sync
  that never looked again would derive transactions against a roster frozen on the day the operator
  linked the institution."* The full roster per connection is therefore archived on every run.
- **A failed roster fetch produces no partial list.** `/accounts/get` on an item in error fails
  wholesale, so the connection contributes no observation rather than an empty one. Absence can
  therefore be measured *only against a successful observation*, and that is enforceable.
- **The archive makes it replayable.** Every roster response is in `raw_responses`, so the signal is
  reconstructible by a rebuild rather than being a one-shot event that must be caught live.
- ⚠️ **Absence is evidence of closure, not closure.** An account also stops being listed when the
  operator de-selects it in Link's account-select update mode, when the institution changes what it
  shares, or when the aggregator's own coverage of that product changes. These are indistinguishable
  from a genuine closure at this layer, and no amount of care makes them distinguishable.

That ambiguity is the single most important input to the design. **A field named `closed` that is
computed from absence is a wrong number that looks right** — the exact class this whole surface
exists to refuse. The vocabulary below names the observation, not the conclusion.

---

## The ruling the owner owes

**What is being asked:** when a total is computed over account balances — a net worth an agent builds
today by summing `list_accounts`, or the `net_worth` half of the specified-but-unbuilt
`balance_history` — what does a retired account contribute?

**Option A — exclude from the total, and state the exclusion in the payload.**
**Option B — include in the total, and flag the account in the row.**

**Recommendation: Option A, with the exclusion stated as a figure rather than merely as a flag.**
The argument is not preference; it is that a retired account's last balance is **arithmetically wrong
in both directions**, and the direction depends on the account's class:

- A closed **liability** — a paid-off auto loan, a cancelled card — carries a last recorded balance
  that is a debt the household does not owe. Including it *overstates* debt and understates net worth.
- A closed **asset** — a matured CD, a rolled-over 401k — held money that went somewhere, and in a
  household with several enrolled institutions it usually went into another enrolled account.
  Including it *double-counts* the same money.

So "include and flag" is not the conservative option it looks like. It preserves a number that has no
referent: `balance_as_of` freezes on the day the account stopped being reported, and the balance
stops being a fact about today at that moment. Option B's real content is *"the consumer will apply
the correction"*, and the consumer is an analyst agent that this product's own thesis says cannot be
relied on to apply a correction it was not handed.

**The argument against the recommendation, which is real.** #18 was ruled the other way — *classify,
do not filter* — on the grounds that an overcount gets questioned and an undercount gets believed.
Excluding a closed account makes net worth **drop** with no visible cause, and a silently smaller
number is the quiet error #18's ruling was avoiding. That objection is answered by the second half of
the recommendation and not by the first: exclusion must be **stated as a magnitude**, on the
`total_external_spend` precedent — the payload says what was excluded and how much it came to, so the
correction is visible and reversible by the reader. Exclusion without that figure is Option A's
failure mode and should not be ratified.

**Why the ruling is not urgent, and why it still has to be made.** Nothing in `src/` computes a net
worth today — `grep` finds the phrase only in comments and DDL rationale. `balance_history` /
`net_worth` is specified and unbuilt. So the *only* net worth that exists is one an agent builds
itself from `list_accounts` rows, which means today the ruling lands in the tool description and the
contract, not in arithmetic. It still has to be made now, because the field vocabulary that makes
either ruling expressible is what this item ships, and choosing the vocabulary after the ruling is
cheaper than choosing it twice.

**How the requirements stay ruling-independent.** AC-12.1 through AC-12.7 and AC-12.9 are identical
under both options — they concern the state, its evidence, and how it is populated. **AC-12.8 is the
only criterion the ruling changes**, and it is written as an obligation to *state the treatment*
rather than as a mandate to exclude. Under Option A its clause reads "excluded, with the excluded
magnitude stated"; under Option B, "included, with the included magnitude stated". Either is a
one-clause edit to one criterion, and no other requirement, field name, or vocabulary value moves.

---

## Requirements — proposed, awaiting ratification

House style: one bolded id, one clause, the reason where the clause alone would not survive a
reader who disagrees. Ids come from the reserved block **AC-12.1 – AC-12.9** and are used in full;
see § *Shared-artifact deltas* for where the integrator may wish to place them, which is not
necessarily as a new group.

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

**AC-12.4 · The roster observation is recorded, so the state is reachable at all.** Each successful
roster derivation records, per account, the date that account was last listed, as a **monotone
maximum** — the mirror of `first_seen_date`'s minimum. *Why:* this is the requirement #40 says does
not exist. Without a recorded observation there is no non-`active` state for the read path to read,
and a min/max pair is what makes the record order-independent under archive replay, which
`derivers.py:86–94` names as the reason retirement was deferred rather than half-built.

**AC-12.5 · Absence is measured within one connection, against a successful observation, and never
against silence.** A connection whose roster could not be fetched marks nothing absent. A connection
whose *entire* roster is absent marks nothing absent. An account with no connection — the FR-7
import-only path — is never marked absent. *Why:* each clause removes a way for a pipeline failure to
be reported as a household event. The whole-roster clause is the important one: fourteen simultaneous
closures is not a thing that happens, and the connection-level failure it really is already has a
home in `get_pipeline_health`.

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

**AC-12.8 · No total over account balances is emitted without stating its treatment of non-active
accounts.** 🔴 *This is the criterion the owner's ruling settles* (§ *The ruling the owner owes*).
Whichever treatment is ruled, a response carrying a count or a sum over accounts states which
treatment it applied and the magnitude that treatment moved, and the envelope's own `coverage.accounts`
count — today an unfiltered `COUNT(*)` over `accounts` (`query.py:184`) — is covered by this clause.
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

## Design — proposed

### The row: four fields, on the lifecycle axis of the row #19 gave the coverage axis

#19 shipped three always-present coverage fields on every `list_accounts` row, produced once by
`query._account_coverage` and consumed by both `list_accounts` and `get_coverage_report`, published
through one shared schema fragment `mcp._coverage_row_fields()`. **This item is the same shape on the
lifecycle axis and should be built the same way**, down to the shared fragment — a
`_lifecycle_row_fields()` beside `_coverage_row_fields()`, and an `_account_lifecycle` producer beside
`_account_coverage`. Proposed fields, in this order, immediately after the coverage block:

| Field | Type | Meaning |
|---|---|---|
| `lifecycle` | `string`, enum | The verdict. Vocabulary below |
| `closed_date` | `["string","null"]` | The stored `accounts.closed_date`; null when none is recorded |
| `last_seen_in_roster` | `["string","null"]` | The date the institution last listed this account; null for an import-only account with no connection |
| `roster_last_observed` | `["string","null"]` | The date this account's connection's roster was last successfully observed; null for an import-only account |

**Vocabulary — three values, all reachable:**

- **`active`** — declared active, and listed on this connection's most recent successful roster
  observation. The ordinary case.
- **`closed`** — `accounts.lifecycle_status` is `inactive`. This value is an **operator declaration**,
  never a derivation, which is what lets it be trusted; `closed_date` carries the date where one was
  recorded.
- **`no_longer_reported`** — still declared active, but the institution's most recent successful
  roster observation did not list it. 🔴 The description on the schema field says what this does and
  does not mean: *consistent with closure, and equally consistent with the account being de-selected
  from sharing or the institution changing what it shares; the balance beside it is frozen as of
  `last_seen_in_roster` and is not a fact about today.*

**A fourth value (`unknown`) was considered and rejected as unreachable.** An aggregator account
exists only because a roster listed it, so `roster_last_observed` is never null for one; an
import-only account is fully operator-owned and is honestly `active` until the operator says
otherwise. An unreachable enum member is a liability on this surface — the multi-currency refusal path
is already this repo's cautionary case — so the null on `roster_last_observed` carries "no roster
basis" instead, which is a fact the row can state rather than a state it has to invent.

**`no_longer_reported` is derived, not stored**, and the derivation is one comparison:
`last_seen_in_roster < roster_last_observed`. Both operands ride the row, so the verdict is auditable
from the payload alone — the `silence_ratio` rule applied again.

### `outputSchema`

`mcp._refuse_optional_row_fields` (`api-contract.md` § Direction's fourth norm, guardrail 1, enforced
inside `_tool_definitions()`) requires every declared row property to be **required**; nullable is
fine, absent is not. All four fields are therefore required, and three are declared
`["string","null"]`. `lifecycle` is a plain `string` with an `enum`, and 🔴 **the enum must be taken
from the vocabulary constant rather than retyped in `mcp.py`** — the precedent is `FLOW_CLASSES` and
`GROUPINGS`, which `mcp.py` reads from `query.py` for exactly this reason: a copy would start refusing
answers this server sends the first time a fourth value is classified.

**No envelope key is added.** `mcp._output_schema`'s `windowed` / `capped` / `totals` flags are the
three conditional-key declarations that `learnings.md` § *A repeated declaration…* ruled must not
become a fourth without collapsing them into a model. This design deliberately keeps lifecycle on the
row, so that trigger is not tripped.

### The unknown case

An account whose lifecycle cannot be established does not get a value that pretends otherwise. Two
shapes, both covered above: the import-only account (no connection, no roster, `roster_last_observed`
and `last_seen_in_roster` both null, `lifecycle: active` on the operator's own authority) and the
pre-migration account not yet re-observed (§ *Population path*, transitional rule). Neither produces
an absent field.

### Population path — the part #40 says does not exist

**One new column, one new migration.** `accounts` gains `last_seen_date` (calendar date, nullable),
the maximum-side twin of the existing `first_seen_date`. `_upsert_account` writes it on both the
insert and the update arms, taking `max(existing, seen_date)`, exactly as the update arm already takes
`min(first_seen_date, seen_date)`. Monotone aggregates on both ends are what make the archive replay
order-independent, which is the specific care `derivers.py:86–94` says retirement was waiting for.

**Then nothing else needs to be stored.** `roster_last_observed` for a connection is
`max(last_seen_date)` over that connection's accounts, which is a read-time aggregate needing no
column and no second writer. It also gives AC-12.5's whole-roster clause **by construction rather
than by a guard**: if every account of a connection vanishes at once, the maximum moves with them and
nothing is ever older than it, so nothing is marked absent. That is the property worth having, and it
is the reason to prefer this over a `connections.roster_observed_at` column, which would have to have
the same case written into it by hand.

**The alternative that avoids the migration was considered and is rejected.** `balances_daily` gets a
row per account per day from every roster derivation, so `max(as_of_date)` per account is a working
proxy for "last listed" today, with no schema change at all. It is rejected because it is a
coincidence of the current implementation rather than a mechanism: it depends on `_derive_one_account`
always reaching `_write_balance`, it is polluted by FR-7 manual-import rows which write into the same
table with no roster behind them, and it makes a lifecycle claim rest on a balance-capture side
effect that nothing pins. `learnings.md` § *Guarantees by construction* is directly against it — the
rule would be matching on a **name** where it means a **relationship**, which is the shape that
already nearly wiped this repo's archive once.

**Migration mechanics the build inherits, recorded so they are not rediscovered:**

- The DDL is frozen. `core_schema.py` is migration 2 and must not change; `last_seen_date` arrives as
  **migration 3**, and `store/schema.py`'s Core metadata gains the column independently.
  `tests/store/test_schema.py::test_the_metadata_matches_the_migrated_database` is the comparison,
  and `learnings.md` § *Two descriptions, compared* is why neither side may be generated from the
  other.
- ⚠️ **The runner's docstring says a migration's `apply` "issues DDL only".** A backfill of existing
  rows is DML, so the build must either widen that convention deliberately or avoid the backfill. The
  cheap avoidance is a **transitional read rule**: treat a null `last_seen_date` as `first_seen_date`,
  which is true by construction (the account was listed at least once, on that date) and retires
  itself after the first post-migration sync repopulates every live account. Recommended, with the
  retirement condition stated in the build plan rather than left to be noticed.
- 🔴 **A migration means older readers refuse to serve** (`architecture.md` § Direction). The MCP
  server and the CLI must be upgraded with the datastore. This is the norm working, not a problem, but
  it turns #40 from "a read-path change" into a change with an operational step, and the operational
  spec should say so.

**And an operator path, because a derived signal is not enough.** AC-12.2 is emphatic that
`no_longer_reported` is not a closure claim; the only thing that can make a closure claim is the
operator. `lifecycle_status` has been operator-owned in name since the connector was built and
unreachable in fact for just as long. Closing that needs one command — a `bankmachine accounts retire
<id>` in the shape `connections retire` already established, writing `lifecycle_status='inactive'` and
`closed_date`, and a way back for the mistake. 🔴 **This is the piece most likely to be dropped under
schedule pressure, and dropping it leaves the item half-built**: without it the surface can report
`no_longer_reported` and can never report `closed`, so the operator has no way to confirm what the
system suspects, and the ambiguity AC-12.2 honestly reports never resolves for any account.

### The warning, and the guidance beside it

A request-scoped warning kind — proposed name `account_no_longer_active` — fires on `list_accounts`
when the listing holds a non-`active` account, and on `query_transactions(account_id=N)` when the
account asked about is one. It names the ids, exactly as `accounts_without_coverage` does. This is the
mechanism by which AC-12.8's "state the treatment" reaches a consumer that read only the envelope.
🔴 It is **request-scoped, not connection-scoped**, for the reason `api-contract.md` records against
`accounts_without_coverage`: a fifth kind riding every response equally reproduces the `gapped`
defect, where a warning true on every answer told a caller nothing about *this* one.

Adding a kind touches three places and `learnings.md` records that nothing holds them together:
`envelope.WARNING_KINDS`, the vocabulary table in `api-contract.md` § *The warning vocabulary*, and a
`_Guidance` entry in `mcp_resources.py` — which is the client guide's *means / for this answer / act*
triple, and where the "do not sum a frozen balance into net worth" instruction actually lands for the
consuming agent.

### Interaction with #19's coverage fields, stated because the two will be read together

- **A closed account with `transaction_count: 0`** carries both `accounts_without_coverage` and the
  new kind. Both are true and they say different things; neither suppresses the other.
- 🔴 **`silence_exceeds_cadence` must not fire for a non-active account.** This is AC-12.7's whole
  content on the coverage surface, and it is where the two axes actually collide: today a closed
  account's `silence_ratio` grows without bound and its flag is permanently true, which is
  `data-model.md`'s named defect. Whether the flag goes false or the ratio goes null is a build
  decision; that it must stop asserting a coverage failure is not.
- **`last_transaction_date` and `last_seen_in_roster` are different facts and will often differ.** An
  account can be listed on a roster for months after its last transaction. Neither may be derived
  from the other, and the client guide should say which question each answers.
- **One producer, two readers, twice over.** `_account_coverage` already holds that discipline for
  #19; `_account_lifecycle` must hold it for AC-12.7, and for the same recorded reason.

---

## Assumptions, vetoable

- `[ASSUMPTION: absence from a successful roster observation is worth surfacing at all, given it is
  ambiguous between closure, de-selection and an aggregator coverage change | HIGH impact | user can
  override]` — the alternative is to surface only the operator's declaration and build no derived
  signal, which is simpler and strictly weaker: an account the operator has not yet noticed is gone
  stays indistinguishable from a live one, which is #40 unfixed for exactly the case that motivated it.
- `[ASSUMPTION: the operator wants a CLI command to retire an account, rather than the derived signal
  alone | MED impact | user can defer]` — deferring it is coherent but leaves `closed` unreachable and
  the ambiguity permanent; if deferred, that should be recorded on the issue rather than discovered.
- `[ASSUMPTION: a new column plus migration 3 is acceptable, against #40's premise that this needs
  neither | MED impact | user can override toward the balances_daily proxy]` — the proxy avoids the
  migration and the operational upgrade step, at the cost of resting the lifecycle claim on a
  balance-capture side effect. The trade is stated in full under § *Population path*.
- `[ASSUMPTION: four new row fields on the most-called tool is acceptable schema weight on top of
  #19's three | LOW impact | user can correct toward three by dropping roster_last_observed]` —
  dropping it makes the verdict non-auditable from the payload, which is the property the
  `silence_ratio` ruling was about.
- `[ASSUMPTION: no version bump or deprecation window is owed | LOW impact | user can correct]` —
  every MCP tool is `experimental` in `api-contract.md` § Surface Inventory, the change is additive to
  a row, and there is one consumer.
- `[ASSUMPTION: the cost of the lifecycle read on list_accounts is negligible | MED impact — see
  below]` — **this is an expectation, not a measurement, and it is recorded as one.**
  `mcp-coverage-latency-2026-09-09.md` measured #19's per-account walk at 3.0ms against an 18.2ms
  end-to-end `list_accounts`, but `learnings.md` § *A cited number measures what its study measured*
  is precisely against reusing that figure here: the lifecycle read is a different walk. The build
  owes its own measurement in its Done-when, in the shape chunk 02 used.

---

## Requirements confidence

**High** on the premise verification and on the population finding. Every claim in § *Premise
verification* was read off the tree at `2aa36ac`, the negative claim ("nothing writes `inactive`") was
established three independent ways, and the aggregator's silence on account status was read from the
pinned SDK's own model rather than from documentation or memory.

**High** on AC-12.1 through AC-12.7 and AC-12.9. They restate obligations this repo already wrote down
— AC-6.5, AC-9.1's own tool table, AC-9.5, AC-11.1, and `data-model.md` § Account lifecycle's 🔴 rule —
and add the population path those obligations silently assumed. The requirements predate the finding,
which is the same position #19 was in.

**Medium** on the field vocabulary. `no_longer_reported` is the honest name for the observation and it
is longer and less familiar than `closed`; a consumer skimming may collapse the two, which is the
failure the name exists to prevent, arriving through the reader instead of the payload. The mitigation
is the schema description and the client-guide entry, neither of which has been tested against a real
consumer.

**Open, and deliberately** — AC-12.8's treatment clause. § *The ruling the owner owes* recommends
exclude-and-state-the-magnitude and argues the case against it. It is not decided here, and the
remaining eight criteria do not move whichever way it goes.

**Out of scope, unchanged:** #22 (pending semantics) and #23 (sign on a real inflow) remain deferred
for the reason `discovery-mcp-answer-scope.md` records — neither can be exercised against the sandbox.
🔴 **#40 is different on exactly that axis**, which is the argument for building it now: a shrinking
roster is two archived responses replayed, so this precondition can be closed before production data
exists, while those two cannot.

---

## For the audit against #22 and #23, which are being written in parallel

Three points of contact, offered so the two sets can be checked for contradiction:

1. **Retained-but-excluded is one pattern, and it must be spelled the same way twice.** #22 rests on
   `transactions.removed_at` — soft delete, never a hard delete, and every read filters
   `removed_at IS NULL`. This item deliberately does **not** filter: a retired account stays in the
   `list_accounts` listing and is flagged. The two are consistent — a removed transaction is a row the
   institution says never happened, while a closed account is a row that is still true about the past
   — but they read as contradictory if either is stated as a general rule. Neither should be.
2. **Net worth is where #23 and this item meet.** `derivers.py:868` and `core_schema.py:39` both say
   the operator-signed convention exists so *"net worth is a plain sum"*. #23 questions whether the
   sign is right in the direction production will run; AC-12.8 questions what belongs in the sum at
   all. **If both land, "a plain sum" stops being literally true in two independent ways**, and the
   two documents should not each claim to be the one exception. Whoever integrates should make the
   plain-sum sentence name both.
3. **The deferral reason differs, and the difference is load-bearing.** #22 and #23 carry the shared
   deferral reason *"cannot be exercised against the sandbox"*. #40 does not qualify for it: a
   shrinking roster is two archived responses replayed, so its transition is testable in-repo today
   (AC-12.9). If a common "production preconditions" framing is written across the three, this one
   should be excluded from it by name, or the readiness document's own claim that #40 is the buildable
   one is quietly contradicted.

---

## Shared-artifact deltas for the integrator

Nothing below was applied. Every entry is a proposal, and the placement questions are flagged rather
than resolved — the integrator owns placement and conflict resolution.

### 1. `docs/system-requirements.md` — where the AC-12.x block goes

🔴 **A placement judgment for the integrator, stated rather than taken.** The reserved id block is
AC-12.x and this document uses it in full, but the criteria's natural homes are spread across three
existing groups: AC-12.1–12.3 and 12.8 extend §5's AC-9.x (the MCP surface), AC-12.4–12.6 extend
FR-6's **AC-6.5** (the schema's account lifecycle), and **AC-12.7 is most naturally an amendment note
under §7's AC-11.1**, which already carries the clause *"a retired account's post-closure period is
not a gap"* and is the gate `get_coverage_report` is audited against. Two options:

- **(a)** A new `### FR-9 · Account lifecycle, made visible` after FR-8 (§4, ending line 344),
  holding AC-12.1–12.9, with pointer notes added under AC-6.5, AC-9.1 and AC-11.1.
- **(b)** Distribute the criteria into the three groups above, which contradicts the reserved-block
  constraint this document was written under and needs the owner's sign-off on renumbering.

**(a) is the safe default and preserves the parallel-agent id contract.** The criteria text is § *Requirements*
above, verbatim. `tests/preferences/test_requirement_ids_unique.py` greps `**AC-` headers; AC-12 is
unused today, verified.

**Anchor — under AC-6.5** (line 320-ish, immediately after the existing paragraph), append:

> **Amendment (proposed, 2026-09-09).** This lifecycle has never had a transition. `lifecycle_status`
> is written once, to `active`, by the accounts deriver, and no code path and no operator command has
> ever set it to `inactive`; the coverage report that AC-11.1 audits reads neither column. AC-12.4–12.6
> supply the transition. See `.prawduct/artifacts/discovery-account-lifecycle.md`.

**Anchor — under the AC-9.1 tool table's `list_accounts` row** (line 359), append after the build-status
parenthetical:

> 🔴 **`list_accounts`'s "lifecycle state" has never been on the wire.** It is named in this table and
> in `api-contract.md`'s, and `test_the_documented_tool_surface_is_the_built_one.py` compares tool
> *names* only, so a documented row field that was never built is invisible to every guard in this
> repo. AC-12.1–12.3 build it. Recorded here because the gap, not the field, is the reusable lesson.

**Anchor — under AC-11.1's existing amendment block** (line ~462), append:

> **Note (proposed, 2026-09-09).** This clause's *"a retired account's post-closure period is not a
> gap"* is **not met by the shipped `get_coverage_report`**, which reads no lifecycle column and
> reports a closed account's silence as an unbounded `silence_ratio` with `silence_exceeds_cadence`
> permanently true. AC-12.7 closes it.

### 2. `.prawduct/artifacts/api-contract.md`

**2a — new subsection**, immediately after § *Coverage is reported per account, never per institution
(AC-9.5)* (§ heading at line 476; it ends just before § *Provenance survives into every result* at line 515):

> ### Lifecycle rides the same row as coverage
>
> 🔴 **Every `list_accounts` row carries `lifecycle`, `closed_date`, `last_seen_in_roster` and
> `roster_last_observed` — always, not behind a parameter.** Coverage says whether an account has
> data; lifecycle says whether the account still exists. A closed card's last balance is not a fact
> about today, and until this shipped nothing in any payload said so.
>
> 🔴 **The vocabulary names the observation, not the conclusion.** `active`, `closed`, and
> `no_longer_reported` — and the third is *not* a synonym for the second. The aggregator reports no
> account-closure signal of any kind (verified against the pinned SDK's `AccountBase`: no `closed`,
> no `status`, no `closed_at`), so the only available evidence is that an account stopped being
> listed, which is equally consistent with de-selection from sharing and with the institution changing
> what it shares. `closed` is an operator declaration and nothing else may set it.
>
> **The evidence dates ride beside the verdict** so a consumer can re-derive it:
> `no_longer_reported` is exactly `last_seen_in_roster < roster_last_observed`. Same rule as
> `silence_ratio` — report the quantity, not only the flag.
>
> **One producer feeds `list_accounts` and `get_coverage_report`**, as `_account_coverage` already
> does for the coverage axis. A retired account's silence is identified as closure rather than
> reported as a hole (`data-model.md` § Account lifecycle; AC-11.1).

**2b — § The warning vocabulary** (table at line ~553), add a row below `accounts_without_coverage`:

> | `account_no_longer_active` | An account in the scope of THIS request is closed or is no longer listed by its institution, so its balance is frozen as of the date beside it and is not a fact about today |

and append to the paragraph that follows the table: *"`account_no_longer_active` is request-scoped for
the same reason `accounts_without_coverage` is: it fires on `list_accounts` when the listing holds one
and on `query_transactions(account_id=N)` when the account asked about is one."*

**2c — § MCP tool surface, the descope amendment** (the *"What the shipped tools do not yet carry"*
paragraph, line ~175), add a sentence: *"and `list_accounts` is specified with lifecycle state — in
this table and in `docs/system-requirements.md` §5 — and does not yet carry it; the columns exist in
the schema, no code has ever written a non-`active` value to them, and no guard covers a documented
row field."*

**2d — § Direction, a candidate fifth norm.** 🔴 **Proposed for the owner, not drafted as ratified.**
Text if ratified: *"A stored balance is reported with its lifecycle, and no total over balances is
emitted without stating its treatment of non-active accounts."* Why: the same thesis as the second
norm applied to the balance sheet — the frozen balance of a closed account is a plausible wrong number
whose wrongness has no signal, and the direction of the error depends on `balance_class`. **This norm
should not be born until AC-12.8's treatment is ruled**, since its second clause is what the ruling
settles.

### 3. `.prawduct/artifacts/data-model.md`

**3a — the `accounts` field table** (line ~254), add after the `first_seen_date` row:

> | `last_seen_date` | calendar date | nullable | The date the institution last listed this account. A **maximum**, the mirror of `first_seen_date`'s minimum — which is what makes an archive replay order-independent |

**3b — § Account lifecycle (AC-6.5)** (line ~473), replace the diagram and rule with:

> ```
>   active ──── operator retires ────> inactive   (closed_date set)
>      │
>      └──── absent from a successful roster observation ────> reported as `no_longer_reported`
>                                                              (derived at read time; the stored
>                                                               column is NOT changed)
> ```
>
> 🔴 **A retired account's dormant period must not read as a permanent coverage gap.** The coverage
> report reads `lifecycle_status` and `closed_date` so a post-closure silence is *identified as
> closure*, not reported as a hole.
>
> ⚠️ **Verified unmet on 2026-09-09 and recorded rather than left to be found again.**
> `query.coverage_report` reads neither column, so every closed account will report an unbounded
> `silence_ratio` and a permanently-true `silence_exceeds_cadence`. AC-12.7 closes it.
>
> 🔴 **Declared and derived are two different facts and the schema keeps them apart.**
> `lifecycle_status` is the **operator's declaration** and is authoritative; absence from a roster is
> an **observation** and is not a closure claim, because the aggregator reports no closure signal at
> all. Absence is measured within one connection, against a successful roster observation only, and
> never when a connection's entire roster is absent.

### 4. `.prawduct/artifacts/mcp-production-readiness.md` — a correction to its own tracking note

The 2026-09-09 tracking block under § *What has to be true before yes* asserts *"**#40 is the one that
is buildable now**, and cheaper than this list knew: `accounts` already carries `lifecycle_status` and
`closed_date`, and `list_accounts` simply does not read them."* 🔴 **The first clause holds and the
second does not.** Proposed appended correction:

> **Correction (2026-09-09, from `discovery-account-lifecycle.md`).** "`list_accounts` simply does not
> read them" was checked and is wrong in the way that matters: the columns exist and **nothing has
> ever written a non-`active` value to either.** The accounts deriver hardcodes `"active"` at insert
> and excludes the column on update; no CLI command and no MCP tool can set it; `connections retire`
> does not touch `accounts`. So item 6 is a **population path plus a read path plus a migration**,
> not a read path. The "buildable now" half stands, and stands more firmly than the rest of this list
> knew: unlike #22 and #23, this one is exercisable in-repo by replaying two archived roster
> observations, so it can be closed before production data exists.

### 5. `.prawduct/artifacts/discovery-mcp-answer-scope.md` — C2's undischarged entanglement

That document's § *The entanglement that changes the build order* folded account lifecycle into chunk
C2 as a correctness precondition, and C2's own description opens *"Account lifecycle onto
`list_accounts`."* C2 shipped and closed #19; the lifecycle half did not ship. Proposed note under the
C2 bullet:

> **Not discharged (recorded 2026-09-09).** C2's lifecycle half did not ship — `list_accounts` and
> `get_coverage_report` both read no lifecycle column — and the entanglement's stated consequence
> stands: a closed account reports as a permanent coverage gap. Nothing failed, because no guard
> covers a documented row field. Carried by #40; see `discovery-account-lifecycle.md`.

### 6. `.prawduct/backlog.md` and issue #40 — for the integrator, who owns the stage transition

Not edited, per brief. Two changes proposed to the issue body: correct `src/bankmachine/query.py:1332`
to `:536` (the address was accurate at `fd1e921` and moved in PR #45), and replace the § *Key fact*
conclusion — *"this is a read-path change plus a field on the published `outputSchema` — not a
data-model change and not a migration"* — since it is false. Suggested replacement: *"the columns
exist and nothing has ever written a non-`active` value to them, so this is a population path, a read
path and one migration; see `.prawduct/artifacts/discovery-account-lifecycle.md`."* The
`effort:S` label is understated on the corrected scope and is probably `effort:M`.

### 7. `.prawduct/artifacts/project-preferences.md`

**No row proposed yet.** A norm row would follow the candidate norm in delta 2d, and 2d should not be
born before AC-12.8 is ruled. Flagged so the integrator does not read the absence as an oversight.

### 8. `.prawduct/artifacts/operational-spec.md`

If the migration is adopted (§ *Population path*), one line is owed: migration 3 means an older MCP
server or CLI **refuses to serve** against the migrated datastore, per `architecture.md` § Direction.
That is the norm working, but it makes #40 an upgrade with an ordering step rather than a pure code
change, and the operational spec is where an operator would look for it.
