# Discovery — Where a tool's boundary is drawn

**Work cycle:** MCP tool-surface factoring · medium · requirement
**Opened:** 2026-09-09
**Closes:** brookstalley/bankmachine #30

---

## The three questions

**What problem are we solving?** The owner directed that the MCP surface follow *"minimize tools,
use actions to cover related capabilities"*. Applied literally that directive destroys a property
this repo shipped one wave ago: MCP allows one `outputSchema` per tool, and chunk 06 of
`build-plan-mcp-alignment.md` made schemas per-tool **precisely so that a key's ABSENCE is
information** — an unwindowed tool's schema forbids `effective_window` rather than merely not
requiring it. Behind an action parameter one schema must cover every action's envelope, so it
degrades to the loosest common shape and that property dies. #30 exists to settle which half
survives before the remaining tools are designed, rather than discovering it during the build.

**What does success look like?** A norm in `api-contract.md` § Direction that decides where a tool
boundary goes, stated so that a future builder adding capability number thirty can apply it without
re-litigating this, and so that the `outputSchema` property survives that growth rather than being
defended against it case by case.

**What is out of scope?** No code. The surface this norm implies is built by the AC-9.1 cluster
(`discovery-mcp-answer-scope.md` chunks C2–C4), which this document re-points but does not replace.

---

## The owner's framing, which set the bar

Three options were put to the owner — keep per-tool schemas and stay wide; consolidate behind
actions and lose them; or merge only where it happened to be free. All three were rejected in favour
of a question:

> *"What's the correct long term answer? This project is young. We should not accept any tech debt
> at this stage."*

That reframes the decision. The question is not which horn costs less **today** at four tools going
to ten; it is which rule is still right at thirty capabilities, when this surface has a deprecation
policy and breaking it is expensive.

---

## The finding: the conflict #30 states is false, because both horns share a wrong premise

Both horns assume a tool boundary tracks **the question asked**. Tool-per-question and
action-enum are the same assumption at two extremes — one tool per question, or one tool for all of
them. The premise is what fails, and neither horn can fix it.

Every answer this server sends is one envelope plus `rows`. What differs between tools is **the row
shape** and which envelope blocks apply. Across the ten specified tools there are seven distinct row
entities, not ten — and capabilities grow far faster than row entities do. A budget report is an
aggregate row; a forecast is a time series; a tax breakdown is an aggregate row. The surface's
*shape* count is close to stationary while its *capability* count is not.

### The norm this produces

> **A tool's boundary is drawn where the answer shape changes — never where the question changes.**

Two guardrails, each found by stress-testing the rule rather than shipped alongside it, because
the bare rule is not sufficient:

1. 🔴 **A merge is permitted only when ONE strict row schema covers every parameter value with no
   optional fields.** Nullable is fine — this codebase already carries `["string", "null"]` and
   states that a null field is *present and null, never dropped*. **Absent is not.** This is
   mechanically checkable at registration, the same family as #30's own A3 collision guard, so the
   rule enforces itself instead of resting on the judgment of whoever is adding the tool.
2. 🔴 **Within one shape, merge only across questions that share a domain.** Otherwise one tool's
   description becomes a grab-bag and the surface loses at *selection* exactly what it won at
   *schema* — which is the failure mode the whole consolidation argument was trying to avoid.

### Why this is the no-debt answer, stated against the alternatives

**Tool-per-question accrues debt silently.** It is fine at six tools and hits the near-twin wall
somewhere in the twenties. By then the surface is published under a deprecation policy, so the
correction is a breaking re-factor of a live API. The bill arrives late and larger.

**Action-enum takes the debt immediately.** One schema across every action means the loosest common
shape; absence stops being meaningful; envelope drift returns to being silent. On a product whose
defining risk is *a plausible wrong number with no signal* — the thesis in `api-contract.md`
§ Direction's second norm — that is the single worst field to surrender.

**Shape-factoring carries no debt, because the schema property is a CONSEQUENCE of the rule rather
than something the rule has to protect.** A tool defined by its answer shape has exactly one answer
shape by construction, so a strict per-tool `outputSchema` is always expressible. And two tools can
never become near-twins: if they were, they would share a row shape and the rule would already have
made them one tool. The mechanism recorded in `learnings.md` as the real revisit trigger — near-twin
crowding, not byte count — is not deferred by this rule, it is made structurally unreachable.

It also satisfies the owner's original directive **in substance**: it does minimize tools, and it
does use parameters to cover related capabilities. It differs only in drawing the line on a testable
property instead of on "relatedness", which is not checkable by anything.

---

## The rule applied to the ten specified tools

| Row entity | Specified tools | Verdict | Decided by |
|---|---|---|---|
| connection | `get_pipeline_health` | alone | no other tool shares the shape |
| account (verification) | `get_coverage_report` | alone | guardrail 2 — see below |
| account (analysis) | `list_accounts` | alone, **gains coverage fields** | guardrail 2 |
| transaction | `query_transactions` | alone | only windowed **and** capped tool |
| group aggregate | `spending_summary`, `cashflow_summary` | **MERGE** | one strict row shape exists |
| time-series point | `balance_history`, `net_worth` | **MERGE** | one strict row shape exists |
| position | `list_holdings` | alone | blocked on build step 5 regardless |
| recurrence | `find_recurring` | alone | no other tool shares the shape |

**Ten specified tools land at eight, with zero `outputSchema` loss** and both near-twin pairs
eliminated. Every merge below was tested against guardrail 1 before being accepted.

### The two merges, with the row shape that permits each

**Group aggregate — `spending_summary` + `cashflow_summary`.** Cashflow-by-month is spending grouped
by month with inflows kept rather than filtered. One row covers both:

`{group_key, group_label, currency, transactions, inflow_minor, outflow_minor, net_minor}`

Every field present for every grouping; `group_by` selects `category | merchant | account | month`.
This merge also **absorbs three cluster fixes at no extra cost**: #20's unreachable inflows become
`inflow_minor` (today `spending_summary` filters `amount_minor < 0` and cannot see them at all),
#21's currency becomes a grouped field per the owner's 2026-09-08 ruling, and #18's `flow_class`
becomes a row field and a grouping rather than a bolt-on to one tool.

**Time series — `balance_history` + `net_worth`.** This one **failed guardrail 1 as specified** and
was only admitted after a unifying shape was found, which is the guardrail working rather than being
worked around. Per-account rows are `{date, balance}`; aggregate rows are `{date, assets,
liabilities, net}`; those do not unify. The shape that does:

`{date, account_id, assets_minor, liabilities_minor, net_minor, currency}`

`account_id` is nullable — null means the aggregate row — and `data-model.md`'s operator-signed
convention makes the split derivable at **both** levels: for one account on one day the balance is
either an asset or a liability, so its split is degenerate rather than absent, and
`net = assets - liabilities` holds identically at both levels. `balances_daily` is keyed
`(account_id, as_of_date)`, so both readings come from one series.

⚠️ **Naming is unresolved and is a guardrail-2 question, not a shape question.** Under the merge the
tool answering *"what is my net worth over time"* would be reached as `balance_history`, which is
worse at selection than `net_worth` is. The shape rule fixes where the split goes and says nothing
about the name. Left to the build with the constraint recorded.

### The one split the rule was expected to merge, and did not

`list_accounts` and `get_coverage_report` are both one row per account, so guardrail 1 permits a
merge — a single always-fat account row is strict, and `gaps` as an always-present (possibly empty)
array is strict too. **Guardrail 2 refuses it**: `api-contract.md` already declares
`get_pipeline_health` and `get_coverage_report` "the verification surface, not the analysis surface",
existing so an agent can *establish completeness before answering*. Verification and analysis are
different domains by this document's own prior declaration, and merging across them is what
guardrail 2 exists to stop.

🔴 **The split is only sound because the coverage signal crosses it.** #19's actual bug is that an
agent listing accounts never learns nine of fourteen have no transactions — and an agent that never
thought to call the verification tool is exactly the failure `list_accounts` has to survive. So:

- `list_accounts` carries **always-present** minimal coverage — `first_transaction_date`,
  `last_transaction_date`, `transaction_count`, dates nullable where the account has no rows. One
  indexed group-by over `transactions_by_account_date`. This closes #19 **by construction** rather
  than by an agent remembering to check, which is the same argument as § Direction's existing norm
  that incompleteness rides the success path rather than a channel nobody reads.
- `get_coverage_report` carries those same facts **plus** the verification detail: per-account gaps
  against the account's own median interval (ruled in `discovery-mcp-answer-scope.md`), and the
  source breakdown.
- 🔴 **One producer computes the per-account coverage facts; both tools consume it.** This is #35's
  second acceptance criterion and the reason #35 and #19 were told to be scoped together — built
  separately they can disagree, and a verification surface that disagrees with the analysis surface
  is worse than one that is missing.

---

## What this costs, unmeasured and flagged as such

Adding always-present coverage to `list_accounts` puts a per-account aggregate on the most-called
tool. `transactions_by_account_date` is the index that walk reads in and
`mcp-count-latency-2026-09-08.md` prices comparable grouped reads at ~17ms at the expected 10k-row
volume, so the expectation is *small* — but **that is an expectation, not a measurement, and it is
recorded here as one.**

`learnings.md` § *A cited number measures what its study measured* was written after this project
proposed three mitigations against a cost whose 78% was somewhere else. The build owes a
measurement of the `list_accounts` walk in its Done-when, in the same shape chunk 02 used to price
`matching`, rather than inheriting a number measured for a different statement.

---

## What the build owes, carried forward from #30

#30 asks for three things beyond the ruling itself, and two of them are registration-time
guards rather than prose. All three bind the AC-9.1 cluster, not this document:

- **A2 — flatten the parameters.** A merged tool exposes `group_by`, `since`, `until` as top-level
  keyword arguments. It does **not** expose `params={...}`. Carried over from hallucinote's
  consolidation regardless of which half of #30's conflict survived, and the merges above are
  exactly where it becomes reachable.
- **A3 — refuse parameter collisions at registration.** Two parameters sharing a name with
  different types are a startup failure, not a runtime surprise.
- 🔴 **Guardrail 1, enforced the same way.** A merged tool whose row schema needs an optional field
  to cover every parameter value is refused at registration. This is what makes the norm
  self-enforcing rather than a sentence someone has to remember, and it is the reason the norm
  survives the thirtieth capability. It belongs beside A3, in the same check, because they fail for
  the same reason: a surface that cannot be described strictly should not be advertised at all.

Each is a guard with a go-red case, in the shape `verify_norms_go_red.py` already uses — and per
`learnings.md`, the anchor must be asserted to have applied before a green run is believed.

## Assumptions, vetoable

- `[ASSUMPTION: merging spending_summary into a grouped aggregate tool needs no version bump or
  deprecation window | LOW impact | user can override]` — `api-contract.md` § Surface Inventory
  grades every MCP tool `experimental` and states "removing one is the policy working, not a
  violation". Single consumer, single user.
- `[ASSUMPTION: the merged time-series tool keeps one of the two specified names rather than gaining
  a third | LOW impact | user can correct]` — a new name costs the contract's tool table and guard
  02b's ten pinned claim sites; the build decides on selection grounds.
- `[ASSUMPTION: eight tools is inside the band where selection accuracy holds, so no further
  consolidation is owed | MED impact | user can override]` — the cited evidence (Speakeasy 10
  perfect / 20 near-perfect / 107 total failure) puts the wall near twenty, and the near-twin
  mechanism is now structurally excluded rather than merely absent.

---

## Requirements confidence

**High** on the norm and both guardrails: the decision is derived from a property this repo already
built and measured, the alternatives were priced against the owner's stated no-debt bar, and the
rule was stress-tested until it refused a merge that looked obvious.

**Medium** on the time-series merge — the unifying row shape is sound against `data-model.md` but
has not been written against the actual `balances_daily` read path, and the naming question is
open.

**Out of scope, unchanged:** `list_holdings` still waits on build step 5 (investment sync), and
nothing here moves `mcp-production-readiness.md`'s go/no-go blockers (#22, #23, and the
multi-currency, pending-settlement and closed-account preconditions). This norm orders the work; it
does not change the verdict on real accounts.
