# Discovery — The answer states its own scope

**Work cycle:** MCP AC-9.1 completion · large · bugfix-and-feature
**Opened:** 2026-09-08
**Closes:** brookstalley/bankmachine #16, #17, #18, #19, #20, #21

---

## The three questions

**What problem are we solving?** Six shipped MCP behaviours return a well-formed, plausible answer
that is silently scoped narrower or wider than the question asked, and no field in the response lets
the caller detect it.

**What does success look like?** For every one of the six axes below, a caller can tell from the
response alone whether the answer covers what they asked for — and where it does not, the response
says so in a machine-readable field rather than leaving the caller to infer it from `coverage`.

**What is out of scope?** Refund/purchase netting; currency conversion; `balance_history`,
`net_worth`, `find_recurring`, `list_holdings`; and the three preconditions that require production
data to exercise (see *Deliberately left filed*).

---

## The finding that shapes the work

The six issues are **not six bugs**. They are six instances of one defect — *a well-formed answer
that does not state its own scope* — each on a different axis:

| Issue | Axis | What the answer does not say |
|---|---|---|
| #16 | time | the window asked for vs. the window actually covered |
| #17 | rows | that the result was truncated, and that there is no way past the cap |
| #19 | accounts, fields | which accounts contributed; which fields are populated |
| #18 | classification | that "spending" includes the holder's own transfers and debt service |
| #20 | direction | that inflows are structurally invisible to the aggregate |
| #21 | units | which currency the integers are in |

**They were recognised as one class independently, three times, before this discovery.** Each of
these lives in a triage comment on its own issue, written without reference to the others:

- #17: adding a `truncated` flag alone "would leave the misleading field standing beside it."
- #19: distinguishing *no such account* / *no coverage* / *genuinely quiet* is "one design, not
  three fixes."
- #16: wants "one computation with two labels, deliberately not two mechanisms; the second would
  drift."

Three separate notes reaching the same conclusion from inside three separate issues is the evidence
for planning this whole rather than picking at it. Fixing them as six issues produces six fields
that do not compose, and — per #17's note — at least one new field standing next to an old one that
still lies.

---

## Prior art in this repo, which is most of the discovery

This work does **not** start from a blank page. Three existing sources already hold the analysis,
and reconciling them is the job here rather than re-deriving them:

1. **`docs/system-requirements.md` AC-9.1 / 9.3 / 9.4 / 9.5** — the ten-tool table, the warning
   requirement, the units-and-sign requirement, and the per-account coverage rule. AC-9.5 mandates
   #19's fix *verbatim*, written down before the finding existed.
2. **`.prawduct/artifacts/api-contract.md`** — the 2026-09-08 amendment recording exactly what the
   shipped four do not carry, and naming `get_pipeline_health` + `get_coverage_report` as "the
   verification surface, not the analysis surface."
3. **`.prawduct/artifacts/mcp-production-readiness.md`** — an independent black-box assessment
   listing seven preconditions, of which four were never filed as issues.

**Read #3 before building.** Its closing section is the most valuable thing in it: the tester states
that every finding across three rounds was found because a number was *visibly ridiculous*, and that
real data produces none of that — "the defects that survive into production are precisely the ones
that produce plausible output." That is the acceptance criterion for this whole work cycle.

---

## The entanglement that changes the build order

🔴 **`get_coverage_report` cannot be built correctly without account lifecycle status, and lifecycle
status is a specified-but-unimplemented field, not a new requirement.**

The evidence, all of it already in the repo:

- `store/schema.py` **already has the columns** — `accounts.lifecycle_status` and
  `accounts.closed_date` both exist and are populated.
- `data-model.md` § Account lifecycle says, in a 🔴 rule: *"A retired account's dormant period must
  not read as a permanent coverage gap. The coverage report reads `lifecycle_status` and
  `closed_date` so a post-closure silence is identified as closure, not reported as a hole."*
- AC-9.1's own table says `list_accounts` returns "Accounts with type, institution, mask, current
  balance, **lifecycle state**."
- `query.py:319` `list_accounts` **selects neither column**. The row dict has no lifecycle field.

So the coverage report is *specified* to read two columns that exist, and the tool that is supposed
to expose them does not. Building #19's per-account coverage without this produces precisely the
defect `data-model.md` warns against: every closed account reported as a permanent gap. This is
cheap — the columns are there — and it is a correctness precondition for C2, not a nice-to-have.

It is precondition 6 in `mcp-production-readiness.md`, which was never filed. It is now in scope.

---

## Rulings taken as input (owner, 2026-09-08)

These are decided, not open. Recorded on the issues as comments.

**Scope — "envelope + two tools."** Fix the scope-reporting defect across the four shipped tools,
and build `get_coverage_report` (#19's own named fix) and `cashflow_summary` (#20's). Do **not**
build `balance_history`, `net_worth`, `find_recurring`, `list_holdings` — they are, in
`api-contract.md`'s words, "analysis conveniences whose data is already in the datastore" and add no
correctness, and `list_holdings` is blocked on build step 5 regardless.

**#20 — reachability only, no netting.** Make inflows visible and searchable; build no
refund-matching heuristic. *Why:* every answer to "matched on what, within what window, at what
acceptable false-match rate" is a heuristic that **understates** spending when wrong — the same
argument that decided #18 the other way. An overcount gets questioned; an undercount gets believed.
Netting is descoped, not dropped: whoever picks it up still owes the open questions.

**#21 — carry currency, refuse to sum across.** Every aggregate row carries `currency` and groups by
it; a multi-currency window returns per-currency rows rather than one summed integer. Conversion
needs a rate source, a rate date policy, and somewhere to store that decision — different scope.
**Ruled once for all aggregate tools**, because four more are about to be written.

**#18 — classify, do not filter** (ruled earlier, unchanged). Every row kept; each gains a
`flow_class`; the response gains `total_external_spend`.

---

## Measured evidence

Measured 2026-09-08 by an independent session driving the MCP surface with no source access, on
build `87570c3` — code-identical to `develop` (the only intervening commit touches one artifact
file, nothing under `src/` or `tests/`). Full record and every call:
`.prawduct/artifacts/mcp-fact-find-ac91.md`.

### 🔴 AC-9.1's "gaps > 7 days" is the wrong instrument, and this is a spec defect

`get_coverage_report` is specified as *"Per account: first and last transaction date, gaps >7 days,
source breakdown."* Measured against real cadence, a fixed 7-day threshold produces **~146 findings
and zero signal**, because this data is monthly by nature:

| Account | Intervals | Over 7 days | Min interval |
|---|---|---|---|
| CD, Money Market | 23 each | 100% | 30 days |
| Saving | 48 | 50% | 5 days |
| Checking, Credit Card | — | ~100 more from intra-cycle gaps | 11–14 days |

A monthly account is *silent for 30 days by design*. Reporting that as a coverage gap trains the
reader to ignore the field — the same failure `warnings` already has (#16), arriving by a different
route.

**The informative quantity is trailing silence measured against the account's own cadence**, not
against a constant. On the same data that yields: Checking 0d, Saving 2d, Credit Card 12d, CD 28d,
Money Market 28d. The last two are 28 days silent on a 30-day cycle — genuinely borderline, and the
only thing in the whole fixture worth surfacing.

**Ruled 2026-09-08 (owner): measure against the account's own cadence.** `get_coverage_report`
derives a per-account threshold from the account's median interval and reports trailing silence
against it. The amendment is written into `docs/system-requirements.md` under both **AC-9.1** and
**AC-11.1** — the gate and the tool must hold the same threshold, or the tool's output is
unauditable against the gate that checks it. `api-contract.md`'s tool table is amended to match.

Worth keeping: AC-11.1 already carried the principle the constant violated — *"genuine no-activity
periods are fine but must be identified as such."* A monthly account's 30-day silence is exactly
that, and the 7-day rule was reporting it as a gap. The requirement contradicted itself, and only
measurement made that visible.

### 🔴 `cashflow_summary` has no fixture for its most important branch

There is **no income in this dataset**, and the one payroll-shaped row is signed as an outflow:
`ACH Electronic CreditGUSTO PAY 123456`, description says *Credit*, amount **−585,000**, category
`TRANSFER_OUT`, 24 occurrences, $140,400 — the largest single flow in the sandbox, recorded as money
leaving.

Measured inflows over full coverage are only: TRAVEL 24 txns / $12,000 (United Airlines refunds) and
`TRANSFER_IN` 25 txns / $105.50 (interest). Against outflow of 339 txns / $267,692.77.

Three consequences that C4 must be designed around:

1. **A sign-keyed split reports income $12,105.50 against spending $267,692.77** — precise,
   plausible, and a pure fixture artefact.
2. **Its largest "income" line would be airline refunds**, so any sign-keyed split labels refunds as
   earnings. That is the same class as #18's `LOAN_PAYMENTS` problem: the sign is right and the
   meaning is not.
3. **Real income is zero rows**, so the branch that matters most ships untested — the same shape as
   #21's unreachable refusal path.

This is the strongest argument yet for the no-netting ruling: a heuristic built and "verified"
against this fixture would be verified against nothing.

### The rest, confirmed or corrected

- **#21 — every account and all ~272 retrieved rows are USD.** The refusal path is **unreachable**
  with this fixture; it ships on inspection alone. Assumption above is confirmed, not resolved.
- **#19 account axis — nine of fourteen accounts return zero rows (64%).** Per-account counts sum to
  388, matching `coverage.transactions`. There is no per-account coverage in any payload, so
  establishing this took one call per account.
- **#19 field axis — merchant null confirmed at 89.94% of outflow by value**, and the correction
  worth carrying: **49.74% by row count**. The row figure is the flattering one, the one a naive
  check produces, and it is nearly half the value figure — because the null-merchant rows are the
  big ones (GUSTO 585,000; AUTOPAY 207,850; CD DEPOSIT 100,000). **Report both bases or the field is
  misleading in the safe direction.**
- **Category null is 0.00%** on both bases — zero of 388. The field axis for `category` needs no
  work; scope removed from C2. (`category_is_override` is false on all 388, so that path has no
  fixture either — a separate gap, not this cycle's.)
- **#17 — measured truncation at the default limit:** 4-day window 0% dropped, 31-day 0%, 365-day
  **49.0%** (100 of 196), full coverage **74.2%** (100 of 388). And the decisive part: a consumer
  **cannot detect any of it**. The peer recovered the true counts only by exploiting
  `spending_summary` being uncapped and solving two mixed categories uniquely — and that route
  cannot reach inflows at all, since `spending_summary` filters `amount_minor < 0`. **#17 must ship
  a total or a cursor; a larger default would not fix it.**

---

## Chunks

Ordered by dependency. Each ends with `/prawduct:critic chunk`.

> **Amendment (2026-09-09, #30).** `api-contract.md` § Direction gained a fourth norm after C1
> shipped: **a tool's boundary is drawn where the answer shape changes, never where the question
> changes.** C2 is unaffected — it already splits `list_accounts` from `get_coverage_report`, and the
> norm's second guardrail independently confirms that split, since verification and analysis are
> different domains. **C3 and C4 are re-pointed rather than rescoped:**
>
> - **C4 no longer builds `cashflow_summary` as its own tool.** Cashflow-by-month is the grouped
>   aggregate shape with inflows kept, so it and `spending_summary` become one tool whose rows are
>   `{group_key, group_label, currency, transactions, inflow_minor, outflow_minor, net_minor}` and
>   whose `group_by` selects `category | merchant | account | month`. #20's unreachable inflows and
>   #21's currency land as fields of that one row rather than as two changes to two tools.
> - **C3's `flow_class` lands on the merged tool's row and becomes a grouping**, not a field bolted
>   onto `spending_summary` alone. The chunk's substance — the three-way `external_spend` /
>   `internal_transfer` / `debt_service` distinction and `total_external_spend`, on the
>   `balance_class` precedent — is unchanged and still ruled.
>
> 🔴 **C4 also inherits the claim-site sweep.** It is the commit that rewrites the contract's tool
> table from ten rows to eight, and every count spelled across the README, the client guide and
> `api-contract.md` moves with it; `test_the_documented_tool_surface_is_the_built_one.py` names each
> site. The derivation is `discovery-mcp-tool-surface.md`.

**C1 — the time and row axes.** `effective_window` (the requested window beside the one the data
could answer over) with its two request-scoped warning kinds, window-scoped transaction counts,
`returned` / `matching` / `truncated`, and cursor pagination. → closes #16, #17.
🔴 **Shipped 2026-09-08, and `api-contract.md` is now the authority on its particulars** — this line
deliberately does not restate the kind names or the clamp bounds, because C2 and C4 add windowed
tools against this same mechanism and both moved while C1 was being built: the end bound is the
covered end (today, *or* the last transaction when that is later) rather than `as_of`, and one kind
was renamed. A builder planning against a restatement writes a retired name into a tool description.

**C2 — the account and field axes.** Account lifecycle onto `list_accounts` (see the entanglement
above), per-account coverage, `get_coverage_report` built, the three-way distinction between *no
such account* / *account with no coverage* / *account genuinely quiet in this window*, and
merchant-population figures on **both** bases (by row and by value — measurement shows they differ
by nearly 2×, and the row figure is the flattering one).
The gap threshold is **per-account, from the median interval** (ruled; AC-9.1 and AC-11.1 amended),
so C2 is unblocked. Category population is out: measured 0.00% null. → closes #19.
🔴 **C2's lifecycle half did not ship. Verified 2026-09-09.** C2 closed #19 and delivered the
per-account coverage axis; "Account lifecycle onto `list_accounts`" did not land, and neither
`list_accounts` nor `get_coverage_report` reads `lifecycle_status` or `closed_date` today. The
entanglement's stated consequence therefore still stands: a retired account's silence reports as a
permanent coverage gap. Nothing failed at the time, because no guard covers a *row field* — the
surface guard compares tool **names** only. Carried by #40; see
`.prawduct/artifacts/discovery-account-lifecycle.md`.

**C3 — classification.** `flow_class` of `external_spend` / `internal_transfer` / `debt_service` on
every `spending_summary` row, plus `total_external_spend`. Precedent to follow: `balance_class` on
`list_accounts`, which the acceptance tester named as the single field that let it state net worth
with no hedging. → closes #18.

**C4 — direction and units.** `cashflow_summary` built; `query_transactions` gains merchant/text,
category and amount-range filters; `currency` on every aggregate row with grouping. → closes #20
(as ruled), #21.

**Trap to carry into C3, recorded so it is not rediscovered:** shipping C3 does **not** fix the
TRAVEL case, and TRAVEL is the most tempting thing to spot-check afterwards. `spending_summary`
filters `amount_minor < 0`, so the 24 offsetting +$500 credits are inflows it never sees — there is
no row for `flow_class` to reach. After C3, TRAVEL still reports $12,000 against a true $0. That is
gross-vs-net, and it is C4. Two independent acceptance passes have arrived at that $12,000 by
different routes; treat a post-C3 TRAVEL check as confirming the linkage, not as evidence the fix
failed.

---

## Deliberately left filed, with the reason

Three concerns from `mcp-production-readiness.md` are **not** in this work cycle, because none of
them can be exercised against the sandbox — they need production data or a real settlement cycle.
Building against the fixture would produce code whose only test is a fixture that cannot express the
failure.

⚠️ **Two of the three are numbered preconditions (5 and 7); the third is not.** Multi-connection
warnings comes from the readiness doc's "Where my confidence comes from" prose, not its numbered
list — and precondition **6** (closed/retired accounts) is *in* scope, folded into C2 by the
entanglement above. The numbered list and the prose disagree about how many preconditions that
document holds, so cite them by name rather than by number.

- **Pending transactions.** `pending` is `false` on all 388 sandbox rows, so the field's behaviour
  has never run. Two untested risks: a pending row counted, then counted again when it posts under a
  new id; and a pending row that vanishes without trace. Needs real settlement.
- **Sign convention on a real inflow.** The fixture's only payroll row arrives pre-inverted from the
  aggregator, so the normalization has never been watched in the direction it will actually run.
- **Multi-connection warnings.** With one connection the two zero-arg tools cannot be made to
  disagree. `warnings` is already the weak part of the payload; with five connections a consumer
  cannot tell which institution a gap is in.

Filed 2026-09-08 as **#22** (pending-transaction semantics), **#23** (sign convention on a real
inflow) and **#24** (warnings cannot name which connection is degraded), each carrying the shared
deferral reason so a later reader does not mistake deferral for oversight. An assessment artifact is
not a tracker, and this one had already carried four unfiled preconditions once.

---

## Assumptions, vetoable

- `[ASSUMPTION: cursor pagination is opaque-cursor as api-contract.md § Pagination and caps already
  specifies, not offset | MED impact | user can override]` — the contract term exists and was never
  implemented; implementing it as specified is cheaper than amending the contract.
- `[ASSUMPTION: the refusal path for multi-currency aggregation ships untested against real
  multi-currency data, because the sandbox is single-currency | MED impact | user can defer #21
  until a non-USD account exists]` — the peer is measuring whether the sandbox can exercise it at
  all. If it cannot, that limitation goes in the C4 record rather than being discovered later.
- `[ASSUMPTION: window-scoping coverage.transactions is a breaking change no deployed consumer
  depends on | LOW impact | user can correct]` — single-user product, one consumer, `experimental`
  tier permits it.

---

## Requirements confidence

**High** on C1, C2, C3 — the requirements predate the findings (AC-9.1, AC-9.3, AC-9.5,
`data-model.md`), the rulings are recorded, and the evidence is measured rather than inferred.

**Medium** on C4 — `cashflow_summary` is specified only as "income vs. outflow by month," and what
counts as income is exactly the question #20 declined to answer. Measurement made this worse rather
than better: the fixture has zero income, its one payroll-shaped row is signed as an outflow, and
its largest inflow is airline refunds. **A sign-keyed split is therefore both the obvious
implementation and demonstrably wrong**, and no test against this fixture would catch it. The C4
build plan must state its own interpretation of income explicitly, and say what it cannot verify.

C2's one open requirement — the gap threshold — was ruled and written into the requirements
document, so no chunk is blocked on a decision.
