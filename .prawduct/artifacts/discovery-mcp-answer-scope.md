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

## Chunks

Ordered by dependency. Each ends with `/prawduct:critic chunk`.

**C1 — the time and row axes.** `effective_window` (start clamped to coverage, end clamped to
`as_of`), the two warning kinds `window_starts_before_coverage` / `window_extends_past_today`,
window-scoped transaction counts, `returned` / `matching` / `truncated`, and cursor pagination.
→ closes #16, #17.

**C2 — the account and field axes.** Account lifecycle onto `list_accounts` (see the entanglement
above), per-account coverage, `get_coverage_report` built, the three-way distinction between *no
such account* / *account with no coverage* / *account genuinely quiet in this window*, and
field-population figures for `merchant` and `category`. → closes #19.

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

Three of `mcp-production-readiness.md`'s preconditions are **not** in this work cycle, because none
of them can be exercised against the sandbox — they need production data or a real settlement cycle.
Building against the fixture would produce code whose only test is a fixture that cannot express the
failure.

- **Pending transactions.** `pending` is `false` on all 388 sandbox rows, so the field's behaviour
  has never run. Two untested risks: a pending row counted, then counted again when it posts under a
  new id; and a pending row that vanishes without trace. Needs real settlement.
- **Sign convention on a real inflow.** The fixture's only payroll row arrives pre-inverted from the
  aggregator, so the normalization has never been watched in the direction it will actually run.
- **Multi-connection warnings.** With one connection the two zero-arg tools cannot be made to
  disagree. `warnings` is already the weak part of the payload; with five connections a consumer
  cannot tell which institution a gap is in.

🔴 These must be **filed as backlog items**, not left in this document — an assessment artifact is
not a tracker, and the readiness doc has already carried four unfiled preconditions once.

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
counts as income is exactly the question #20 declined to answer. The C4 build plan must state its
own interpretation rather than inherit the phrase.
