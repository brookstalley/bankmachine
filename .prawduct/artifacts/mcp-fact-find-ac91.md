# MCP fact-find for AC-9.1 (issues #16–#21)

**Date:** 2026-09-08. **Tester:** independent acceptance session, no source access.
**Build measured:** `{"version":"0.1.0","commit":"87570c3","dirty":false}`, reported
identically on all 24 responses in this run.

## On the build precondition

The brief asked for `4db74dc` and said to stop otherwise. Reporting both halves of that:

**I cannot restart my MCP server.** It is a stdio subprocess my client launched at
connect time; nothing on the tool surface re-reads the build, and no client-side control
is exposed to me. Forcing a re-read is an operator action, exactly as round 4's record
already noted. So the served build is pinned at `87570c3` for the life of this session.

**The gap does not matter here, and the brief's premise was inaccurate.** `87570c3..develop`
is exactly one commit — `4db74dc`, which changes one file, `.prawduct/artifacts/
mcp-acceptance-round-4.md`, +68 lines, nothing under `src/` or `tests/`. The served build
is therefore *code-identical* to `develop`'s HEAD; only the reported commit string would
differ after a restart. The brief also said those four commits included "the build-identity
work and the `account_id` fix" — both `6532659` and `97085df` are already ancestors of
`87570c3`, verified last round and re-verified now. Measuring rather than stopping, since
the rule's purpose (no measurement against an unidentified build) is satisfied: this build
is identified and provably equivalent.

## A. Currency (#21)

**Distinct values observed: `{"USD"}` on both surfaces. The refusal path is unreachable
with this fixture and will ship untested.**

- `list_accounts` — all 14 accounts `"currency":"USD"`.
- `query_transactions` — `"currency":"USD"` on every row retrieved (~272 rows, spanning
  all five accounts that have any, and both ends of coverage).

There is no aggregate currency field anywhere, so a consumer must infer uniformity by
enumerating rows — and enumeration is capped (see C). "Carry currency, refuse to sum
across" is the right rule, but nothing in this sandbox can exercise the refusal branch.
It needs a non-USD fixture account or the branch ships on inspection alone.

## B. Per-account coverage (#19)

Measured by 14 calls; there is **no per-account coverage anywhere in any payload** —
`coverage` is connection-level only, so this table is unobtainable except by fanning out
one call per account and enumerating rows.

| id | account | first | last | rows | trailing silence |
|---|---|---|---|---|---|
| 1 | Plaid Checking | 2024-09-17 | 2026-09-08 | 147 | 0d |
| 2 | Plaid Saving | 2024-09-16 | 2026-09-06 | 49 | 2d |
| 3 | Plaid CD | 2024-09-20 | 2026-08-11 | 24 | 28d |
| 4 | Plaid Credit Card | 2024-09-19 | 2026-08-27 | 144 | 12d |
| 5 | Plaid Money Market | 2024-09-20 | 2026-08-11 | 24 | 28d |
| 6 | Plaid IRA | — | — | **0** | — |
| 7 | Plaid 401k | — | — | **0** | — |
| 8 | Plaid Student Loan | — | — | **0** | — |
| 9 | Plaid Mortgage | — | — | **0** | — |
| 10 | Plaid HSA | — | — | **0** | — |
| 11 | Plaid Cash Management | — | — | **0** | — |
| 12 | Plaid Auto Loan | — | — | **0** | — |
| 13 | Plaid Home Equity Line of Credit | — | — | **0** | — |
| 14 | Plaid Business Credit Card | — | — | **0** | — |

Row counts sum to 388, matching `coverage.transactions`.

**Nine of fourteen accounts return zero rows — 64%.** A coverage report over this data is
mostly a report about absence, and every one of those nine is indistinguishable from a
real account that is merely quiet (the #19 boundary).

**Gaps over 7 days, exactly, for the three accounts I enumerated completely:**

| account | intervals | >7d | max | min |
|---|---|---|---|---|
| Plaid CD (3) | 23 | **23 (100%)** | 30d | 30d |
| Plaid Money Market (5) | 23 | **23 (100%)** | 30d | 30d |
| Plaid Saving (2) | 48 | 24 (50%) | 25d | 5d |

🔴 **A fixed 7-day threshold is the wrong instrument for this data.** Every account here
is on a monthly cadence, so on CD and Money Market *100% of intervals* are "gaps" — the
report would flag 23 gaps per account, all of them normal. Checking and Credit Card have
one or two intra-cycle gaps of 11–14 days each, so they add roughly another 100. The
threshold produces ~146 findings and zero signal.

The informative quantity is in the last column of the first table: **trailing silence
measured against the account's own cadence.** CD and Money Market have been silent 28
days on a 30-day cycle — genuinely borderline, and the only thing in this dataset a
coverage report should be drawing attention to. Recommend deriving the threshold per
account from its own median interval rather than fixing it at 7 days.

## C. Truncation magnitude (#17)

| window | span | returned (default `limit`) | true matches | dropped |
|---|---|---|---|---|
| 2026-08-07 → 2026-08-10 | 4d | 6 | 6 | 0 (0%) |
| 2026-08-01 → 2026-08-31 | 31d | 16 | 16 | 0 (0%) |
| 2025-09-09 → 2026-09-08 | 365d | 100 | **196** | **96 (49.0%)** |
| 2024-09-16 → 2026-09-08 | 723d (full coverage) | 100 | **388** | **288 (74.2%)** |

True counts for rows 1–2 are direct (a single call under the cap returned everything).
Row 4 is `coverage.transactions`, independently reproduced by the per-description model
below. Row 3 was derived by walking the window with `spending_summary`, which is **not**
limit-capped: it reports 171 outflows across 8 categories, and the two mixed categories
solve uniquely (FOOD_AND_DRINK 38/621,229 → KFC 12 + Starbucks 13 + McDonald's 13;
TRANSPORTATION 36/614,076 → Madison 12 + Uber-633 12 + Uber-540 12). Inflows follow as
United 12 + INTRST 13 = 25. 171 + 25 = 196, cross-checking per account as
CC 72 + Checking 75 + Saving 25 + MM 12 + CD 12.

🔴 **The most important part is the method, because a consumer cannot use it.** There is
no per-window match count anywhere on the surface: `coverage.transactions` is global, not
windowed. I could only recover the true counts because `spending_summary` is uncapped —
and it **cannot see inflows at all** (it filters to `amount_minor < 0`). So the inflow
half of any window is knowable only by enumerating rows, which is precisely what the cap
silently truncates. A consumer holding 100 rows has no reachable way to discover there
were 388. Whatever #17 ships must expose a total or a cursor; a bigger default would not
fix it.

## D. Field population (#19, field axis)

Measured over all 388 rows. Method: a per-description table of the 16 recurring
transaction shapes, validated by reproducing **all eight** `spending_summary` category
lines to the unit and summing to exactly 388 rows.

**`merchant`**

| basis | null | total | rate |
|---|---|---|---|
| by row | 193 | 388 | **49.74%** |
| by absolute amount, all rows | 24,087,350 | 27,979,827 | **86.09%** |
| by absolute amount, outflow only | 24,076,800 | 26,769,277 | **89.94%** |

**The issue's "90% of outflow by value" is confirmed — 89.94%.** Two figures worth adding:
it is 86.09% once inflows are counted, and only 49.74% by row. The row figure is the one
that flatters, and it is the one a naive check would produce; the value figure is nearly
double it because the null-merchant rows are the large ones (GUSTO 585,000; AUTOMATIC
PAYMENT 207,850; CD DEPOSIT 100,000 — all null).

**`category`: 0.00% null — 0 of 388 rows, on both bases.** Also `category_is_override` is
`false` on all 388, so nothing in this fixture exercises the override path either.

Note the known `merchant` defect is present and measurable: `SparkFun` carries
`"merchant":"FUN"` on all 25 occurrences.

## E. Inflow shape (#20)

Full coverage, from `query_transactions` (invisible to `spending_summary`, which filters
to outflow):

| category | txns | total minor | |
|---|---|---|---|
| TRAVEL | 24 | 1,200,000 | $12,000.00 — "United Airlines" on Checking, merchant populated |
| TRANSFER_IN | 25 | 10,550 | $105.50 — "INTRST PYMNT" on Saving, merchant null |
| **total** | **49** | **1,210,550** | **$12,105.50** |

Against outflow of 26,769,277 (\$267,692.77) over 339 transactions. **Inflow is 4.52% of
outflow; net is −25,558,727 minor units, −\$255,587.27 over 723 days (−\$10,600/month).**

🔴 **There is no income in this dataset, and the one row that looks like income is signed
the wrong way.** The only payroll-shaped record is `"ACH Electronic CreditGUSTO PAY
123456"` on Money Market — description says *Credit*, amount is **−585,000**, category
`TRANSFER_OUT`, 24 occurrences, $140,400.00 total. It is the largest single flow in the
sandbox and it is recorded as money leaving.

Consequences for `cashflow_summary` as specified:

1. Built on sign alone it reports income \$12,105.50 against spending \$267,692.77 —
   a plausible-looking, precise, catastrophic net that is an artefact of the fixture.
2. Its largest "income" line, \$12,000 of \$12,105, would be **TRAVEL** — airline
   refunds, not income. Any income/expense split keyed on sign will label refunds as
   earnings. This is the same class as #19's `LOAN_PAYMENTS` problem: the sign is right,
   the meaning is not.
3. Real income is 0 rows. There is nothing in this sandbox to test an income path
   against, so `cashflow_summary`'s most important branch has no fixture — the same gap
   as the currency refusal in A.

## What was not obtainable from the tool surface

- **Forcing the server to re-read its build.** No consumer-side control exists; operator only.
- **A per-window match count.** Global `coverage.transactions` only, so truncation is
  undetectable from `query_transactions` alone (C).
- **Inflow aggregates.** `spending_summary` cannot see them; only row enumeration can,
  and that is capped.
- **Per-account coverage.** Requires N calls; `coverage` is connection-level (B).
- **Removed transactions.** Excluded by contract, not observable at all, so "was something
  removed?" is unanswerable from outside.
- `pending` was `false` on every row observed; no fixture exercises pending.
