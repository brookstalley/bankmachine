# MCP sandbox server — acceptance testing, round 3

Date: 2026-09-08. Tester: acceptance session (black-box only — tool responses and
tool descriptions, no source read, nothing fixed). Reported to the build session;
the operator decides anything that changes what a tool means.

Server restarted, so this round tests the build that carries the round-2 fixes.
Dataset unchanged: 1 connection (Tartan Bank), 14 accounts, 388 transactions,
coverage 2024-09-16 → 2026-09-08 (722 of 730 days granted).

## The four re-checks — all four pass

| # | Re-check | Evidence | Verdict |
|---|----------|----------|---------|
| 1 | `limit: 0` and negatives refused | `limit must be at least 1, got 0` / `got -5` | Pass |
| 2 | Transposed window refused on **both** tools, swap named | identical on both: `until (2026-07-01) is before since (2026-07-31), so the window selects nothing. Did the two get swapped?` | Pass |
| 3 | `account_id` below 1 refused | `account_id must be at least 1, got 0` / `got -1` | Pass |
| 4 | Ceiling refused rather than trimmed, ceiling quoted | `limit must be at most 1000, got 9999` and `got 1001`; `limit:1000` and `limit:1` both accepted | Pass |

Every refusal names the argument, the rule, and the offending value, with no
exception class, SQL, or bound parameters. NEW-1 is closed; the surviving half of
NEW-2 is closed *structurally* — because an over-ceiling request is refused rather
than trimmed, any accepted request is now known not to have been trimmed by the
cap. That is a better fix than declaring the cap would have been.

Date validation re-probed on two fresh cases and still strong:
`2026-06-31` (non-calendar day in a 30-day month) and `2026-07-01T00:00:00`
(right prefix, wrong shape) both refused. Unadvertised-key refusal survives in
the singular: `spending_summary has no argument 'account_id'. It accepts: since, until`.

## The July reconciliation — intact, and all-time re-verified

This was the regression I was most concerned about, since the boundary work
touches the argument path feeding `spending_summary`.

**July 2026:** 8 categories, 14 outflow rows, every category sum and count
identical between the two tools. Zero drift.

**All-time (stronger check, re-run):** all 8 categories agree exactly, counts
included, totalling **26,769,277** — the same figure round 2 recorded.

| Category | `spending_summary` | Sum of negatives in `query_transactions` |
|---|---:|---:|
| TRANSFER_OUT | 16,440,000 (48) | 16,440,000 (48) |
| LOAN_PAYMENTS | 5,048,400 (48) | 5,048,400 (48) |
| FOOD_AND_DRINK | 1,240,825 (74) | 1,240,825 (74) |
| TRANSPORTATION | 1,228,152 (72) | 1,228,152 (72) |
| RENT_AND_UTILITIES | 1,200,000 (24) | 1,200,000 (24) |
| TRAVEL | 1,200,000 (24) | 1,200,000 (24) |
| GENERAL_MERCHANDISE | 223,500 (25) | 223,500 (25) |
| PERSONAL_CARE | 188,400 (24) | 188,400 (24) |

Also re-verified across all 388 rows: newest-first ordering holds strictly,
currency uniformly USD, `pending` and `category_is_override` uniformly false
(the recorded dataset gaps, unchanged), and truncation is deterministic — the
same `limit:1` call repeated returned the same row.

## NEW-7's provenance wording — it works, and here is the size of the change

The build session asked whether the new `query_transactions` description actually
changes how a merchant-rollup question gets answered. It does, and the delta is
larger than either of us assumed.

**Answering "who are my top merchants?" by `merchant`** — what the old
description invited:

| Merchant | Outflow | Rows |
|---|---:|---:|
| **(null)** | **$240,768.00** | 168 |
| KFC | $12,000.00 | 24 |
| United Airlines | $12,000.00 | 24 |
| **FUN** | **$2,235.00** | 25 |
| McDonald's | $300.00 | 25 |
| Uber | $281.52 | 48 |
| Starbucks | $108.25 | 25 |

**By `description`** — what the new wording steers to:

GUSTO PAY $140,400.00 · AUTOMATIC PAYMENT $49,884.00 · CD DEPOSIT $24,000.00 ·
KFC $12,000.00 · Madison Bicycle Shop $12,000.00 · Tectra Inc $12,000.00 ·
United Airlines $12,000.00 · SparkFun $2,235.00 · Touchstone Climbing $1,884.00 ·
CREDIT CARD 3333 PAYMENT $600.00 · McDonald's $300.00 · Uber 072515 $151.92 ·
Uber 063015 $129.60 · Starbucks $108.25 — summing to $267,692.77, the exact
all-time outflow, with nothing unattributed.

Three things follow:

1. **The phantom is gone.** "FUN, $2,235" is replaced by "SparkFun, $2,235".
   This was the whole point and it lands.
2. **The wording also surfaces a bigger truth by accident.** Grouping by
   `description` exposes the three largest outflow lines as *not merchants at
   all* — payroll, card autopay, and a CD deposit, $214,284 between them. The
   `merchant` view hid all three inside `(null)`. This is the same money #18 is
   about, arriving by a different route.
3. **The stated cost is real but small.** "Prefer `description`" fragments Uber
   into two rows, because its description carries a per-trip stamp. That is the
   only fragmentation in 388 rows — five of six merchants are 1:1 with their
   description.

So: the third option was the right call, and I would now decline to publish a
`merchant` rollup and say why. That is the correct answer rather than a
capability, which is the honest place to land.

## New findings

**NEW-8 — `merchant` is absent on half the rows and 90% of the money, and
nothing in the payload says so.** 193 of 388 rows (49.7%) have `merchant: null`,
and those rows carry $240,768.00 of $267,692.77 total outflow — **89.9% by
value**. The tool description now warns the field is "often absent or wrong,"
which is qualitatively true and was the right fix for trust; but a consumer
cannot learn *how* absent without pulling all 388 rows and counting, which the
default `limit` prevents. `coverage` reports connections, accounts,
transactions, and dates — nothing about field population. This is the same
defect family as the two already filed: #16 is the time axis, NEW-4 is the
account axis, this is the field-population axis. A rollup that quietly drops 90%
of value into an unlabelled bucket is a confident wrong number by the same
mechanism.

**NEW-9 — `coverage.transactions` looks like a completeness signal and is not
one.** It reports 388 — the whole datastore — on *every* call regardless of
window or limit. July returns 16 rows alongside `transactions: 388`; a
`since:2026-09-01` query returns 4 rows alongside `transactions: 388`. So the
one field a caller would reach for to check "did I get everything?" is
window-independent, and comparing it against `rows.length` suggests truncation on
every correctly-complete answer while saying nothing on a genuinely truncated
one. This is why #17 is hard to work around from outside rather than merely
undesirable.

**NEW-3 is half-closed, and the surviving half is the quieter one.** The
transposed-window arm is fixed. The account arm is not: `account_id: 999`
(nonexistent) and `account_id: 9` (Plaid Mortgage, real, -$56,302.06, zero
transactions) still return byte-identical `{"rows":[]}`. The new range check
catches 0 and -1 — below the floor — but nothing catches above the ceiling,
because the ceiling is data, not a constant. A caller who mistypes an account id
gets "no transactions" rather than "no such account," and both read as $0 spent.

## Observations, not defects

- **The `limit:0` argument applies verbatim to accepted limits.** `limit:0` is
  now refused on the reasoning that a silently-clamped answer is
  indistinguishable from a complete one. `limit:5` on July returns 5 of 16 rows
  with exactly that indistinguishability, and `spending_summary` over the same
  window reports the full $11,149.46 against the $3,584.83 those 5 rows sum to.
  The fix closed the invalid-input door; #17 is the same door on the valid side.
  Worth quoting the build session's own reasoning into #17.
- **The `gapped` warning fires on every window**, including windows entirely
  inside the granted range — it appeared on a single-day July 2026 query where
  no history older than the grant is in scope. Connection-scoped rather than
  query-scoped is defensible, but it trains a consumer to ignore the field.
- **`spending_summary` takes no `account_id`**, correctly per its contract — but
  that means per-account category spend is unobtainable except by pulling
  transactions and aggregating, which walks straight back into the truncation
  trap above. Capability gap, not a defect; worth knowing before someone asks
  "what do I spend on the credit card?"

## Not re-derived, per instruction

NEW-4 and NEW-6 are filed and were not re-measured. NEW-5 and NEW-7 stay closed
as aggregator passthrough; NEW-7's product half is answered above. The coverage
clamp (#16) was re-confirmed only incidentally — a 2027 window still returns `[]`.

## Still untestable with this dataset — silence is still not a pass

No pending rows · no category overrides · one connection, so the two zero-arg
tools cannot be made to diverge · no retired accounts · single currency ·
`structuredContent.error.code` invisible to a client, so the
`invalid_argument` / `internal_error` discrimination still needs a unit test.
Recorded as untested-not-passing; no effort spent trying to reach them.
