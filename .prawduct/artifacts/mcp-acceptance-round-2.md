# MCP sandbox server — acceptance testing, round 2

Date: 2026-09-08. Tester: acceptance session (black-box only — tool responses,
no source read, nothing fixed). Reported to the build session; the operator decides
anything that changes what a tool means.

**Triaged by the build session same day.** NEW-1 and NEW-3 fixed and committed;
NEW-2 corrected and fixed; NEW-4 and NEW-6 filed to the backlog; NEW-5 and NEW-7
adjudicated as faithful passthrough of aggregator data, not defects here. Fixes
are NOT yet verified — the MCP server must be restarted to load them. Corrections
are marked inline below; nothing was deleted.

Dataset under test: 1 connection (Tartan Bank), 14 accounts, 388 transactions,
coverage 2024-09-16 → 2026-09-08 (722 of 730 days granted).

## Fixes verified

| # | Fix | Verdict |
|---|-----|---------|
| 1 | `since`/`until` no longer raise `TemporalError` | Confirmed |
| 2 | Unadvertised argument refused, all keys named, accepted set listed | Confirmed |
| 3 | No exception class / SQL / bound parameters in failures | Confirmed *as far as visible* — see caveat |
| 4 | `KeyError` no longer surfaces as "no tool named 'spending_summary'" | Confirmed |
| 5 | `limit: 0` no longer silently means 100 | **Half fixed** — now silently means 1 (NEW-1) |

Fix 2 evidence — `spending_summary{since,until,sinceX,account_id}`:
`spending_summary has no argument 'account_id', 'sinceX'. It accepts: since, until`

Fix 3 caveat: only the error *string* is visible through an MCP client. The
absence of leakage and the remedy sentence are confirmed; that
`structuredContent.error.code` is populated and discriminates
`invalid_argument` from `internal_error` is NOT — it needs a unit test.

Date validation is strong: `2026-02-30` rejected as a non-calendar date (not
merely regex-shaped), `2026-7-1` rejected for padding, `''` rejected.

## Known-broken, re-confirmed

- Coverage clamp — still present (2019 → `[]`; 2027 → `[]`; Sept 2026 reads as a whole month on day 8).
- `query_transactions` truncation — still present; NEW-2 is its other half.
- `spending_summary` counting transfers and debt service — still present; NEW-4/5/6 quantify it.

## New findings

**NEW-1 — `limit: 0` silently means 1.** July 2026 has 16 rows. `limit:0` → 1,
`limit:-5` → 1, no limit → 16. Silent clamp-to-minimum replaced silent
clamp-to-100. One row reads as a plausible complete answer. Per fix 2's own
philosophy these should be `invalid_argument`.

**NEW-2 — Undeclared limit cap.** *Partly wrong as first written — corrected.*
I reported "no cap at all" on the evidence that `limit:9999` returned all 388
rows / 93,447 characters. The build session showed a cap did exist —
`max(1, min(limit, 1000))` — and 9999 returned everything only because just 388
rows exist. My test could not distinguish the two and I over-claimed.
(That formula also explains NEW-1 exactly: `max(1, min(0, 1000))` = 1.)
The surviving half: a cap nothing declares is, from the caller's side, the same
defect — a caller cannot tell a complete answer from a trimmed one. Now a named
`MAX_ROWS` enforced at the boundary, with requests above it refused and the
ceiling quoted rather than silently trimmed.

**NEW-3 — Four failure states share one indistinguishable `{"rows":[]}`:**
nonexistent `account_id:999`; real-but-empty `account_id:9` (Mortgage,
-$56,302.06); nonsensical `account_id:-1`/`0`; and a transposed window
(`since:2026-07-31, until:2026-07-01`). The transposed window is the one a real
person hits, and "$0 spent" is a believable answer. Same defect as the `sinceX`
bug in different clothes.

**NEW-4 — 9 of 14 accounts have zero transactions and nothing warns you.**
Only Checking, Saving, CD, Credit Card, Money Market appear in all 388 rows.

| Account | Balance | Txns |
|---|---:|---:|
| Plaid Student Loan | -$65,262.00 | 0 |
| Plaid Mortgage | -$56,302.06 | 0 |
| Plaid Auto Loan | -$23,211.33 | 0 |
| Plaid HELOC | -$13,500.50 | 0 |
| Plaid Business Credit Card | -$5,020.00 | 0 |
| Plaid 401k | +$23,631.98 | 0 |
| Plaid Cash Management | +$12,060.00 | 0 |
| Plaid HSA | +$6,009.00 | 0 |
| Plaid IRA | +$320.76 | 0 |

$205,317.63 by absolute magnitude — 82% of the balance sheet — has no
transaction coverage, while `coverage.accounts: 14` implies otherwise,
`get_pipeline_health` reports `active` / `last_error_code: null`, and the only
warning concerns history *depth*. The agreed coverage-clamp fix addresses the
time axis; this is the missing account axis. Fold into the same design.

**NEW-5 — A payroll deposit is recorded as an outflow.**
`"ACH Electronic CreditGUSTO PAY 123456"`, Plaid Money Market, `-585000`,
`TRANSFER_OUT`, 24×, -$140,400.00 total. Description says *Credit* and names a
payroll provider; sign and category say money leaving. The row contradicts
itself and is the largest line in every `spending_summary` window.

*Adjudicated — not a defect in this codebase.* The build session read the
archived Plaid body: Plaid sent `amount: 5850` POSITIVE, which in Plaid's
convention means money LEAVING, and Plaid assigned `TRANSFER_OUT` itself. The
`-585000` stored here is the correct normalization to the operator convention.
The contradiction is between Plaid's own description string and Plaid's own
amount and category; all three are stored faithfully.

The consequence survives as a fact about the fixture, not a bug: total inflow
across 24 months is $12,105.50 — this sandbox household really does look like it
earns ~$504/month while carrying a $56k mortgage. Affordability and savings-rate
answers computed from this dataset remain unusable, and nothing in the payload
tells a consumer why.

**NEW-6 — TRAVEL nets to exactly zero; `spending_summary` reports $12,000.**
-$500.00 "United Airlines" on Credit Card 24× (late month) against +$500.00
"United Airlines" on Checking 24× (mid month). The tool follows its documented
outflow-only contract and is off by infinity. Cleanest available justification
for the `flow_class` proposal.

**NEW-7 — Merchant extraction mangles names.** `"SparkFun"` → merchant `"FUN"`
(25 rows, $2,235.00). `"Madison Bicycle Shop"`, `"Touchstone Climbing"`,
`"Tectra Inc"` → `null`. KFC / Starbucks / McDonald's / Uber / United correct.
Any merchant rollup invents a phantom merchant.

*Adjudicated — not a defect in this codebase.* Plaid sends `name: "SparkFun"`
with `merchant_name: "FUN"`, and `null` for Madison Bicycle Shop. The value is
stored as received. Overriding an aggregator's own field is a product decision,
not a bug fix. Still destroys user trust on sight; left open as a product
question rather than closed.

## Answers a user would get, with confidence

- **Net worth** → -$77,164.15 (assets $86,541.74 − liabilities $163,705.89).
  High confidence in arithmetic, low that it is true: 9 of 14 balances are
  uncheckable (NEW-4) and the checkable ones don't reconcile — Checking's
  balance is $110.00 against a 24-month transaction net of +$9,075.23. Expected
  (no opening balance) but no field says so.
- **Monthly spend** → refuse a single number. As reported ~$11,295/mo; less
  `TRANSFER_OUT` ~$4,359; less `LOAN_PAYMENTS` too ~$2,228; less refunded
  TRAVEL ~$1,722. A 6.6× spread with only the top figure surfaced.
- **Income** → decline. $504/month is not credible (NEW-5).
- **Loan paydown** → decline. `[]` is not evidence here (NEW-4). `LOAN_PAYMENTS`
  is 100% credit-card repayment ($49,884.00 + $600.00); not one dollar of
  servicing appears for four loan accounts carrying $158,275.89.

## Solid — verified, no defect

- `spending_summary` reconciles with `query_transactions` **exactly**: July 2026
  category by category (8 categories, 14 outflow rows) and all-time (all
  categories sum to 26,769,277 = the exact sum of every negative amount across
  388 rows). Zero drift.
- `list_accounts` and `get_pipeline_health` never disagreed on any call.
- `account_id` filtering is correct where the account has data (`account_id:4`,
  July → exactly the 6 Credit Card rows).
- Inclusive on both boundaries; `since`-only and `until`-only both behave.
- No duplicates (date+account+amount+description across 388), no dates outside
  stated coverage, currency uniformly USD.

## Untestable with this dataset — silence is not a pass

`pending` is `false` on all 388 rows · `category_is_override` false on all 388 ·
only 1 connection, so the two zero-arg tools cannot be made to diverge · no
retired connection, and `list_accounts` has no per-account retired/closed field
at all (itself a gap) · single currency · `structuredContent.error.code`
invisible to a client. Covering these needs new sandbox rows.

## Near-misses worth keeping

- `limit:0 → 1 row` first read as coincidence; only `limit:-5` (also 1) against
  the true count of 16 revealed a clamp. One call earlier and this was "fixed".
- TRAVEL nearly reported as a straightforward $12,000. The offsetting inflow
  sits on a different account 17 days earlier each month and is invisible until
  the whole category is grouped by sign.
