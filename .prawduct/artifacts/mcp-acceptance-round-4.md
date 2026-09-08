# MCP acceptance — round 4 (partial)

**Date:** 2026-09-08. **Tester:** independent acceptance session, no source access.
**Build exercised:** the round-3 build, NOT `f6e81f2`. See "Why this round is partial".

## Why this round is partial

The tester's MCP server is a stdio subprocess launched at client connect time. That
session connected at roughly 10:51; `7a000c3` (the `fix/mcp-date-window` merge) landed
on `develop` at 11:32. The server therefore serves pre-merge code, and the tester
refused the half of the brief that would have verified the merge rather than produce a
pass that meant nothing.

It fingerprinted the running build against round 3's recorded strings to establish this
rather than asserting it: `limit:9999` → `limit must be at most 1000, got 9999` (the
ceiling `8c92131` claims to have changed is still 1000); the transposed-window refusal
is character-identical to round 3's quote; July `spending_summary` returns the same 8
categories and $11,149.46.

🔴 **Two numbers below depend on that build and are marked.** Everything else is dataset
shape, which the merge did not touch.

**Half (a) — re-verification of the date-window fix and argument handling on the merged
build — remains UNRUN.** It needs the tester's client session relaunched, which is an
operator action.

A brief error worth not repeating: the round-4 brief told the tester that date-windowed
questions "failed outright in round 3." Round 3's record says the opposite. The brief
was written from the fix's framing rather than from the record.

## Findings, ranked by how badly they would mislead the account holder

**F1 — "Am I paying down debt?" gets a precise, plausible, entirely wrong answer, and
the true answer is unobtainable.** `LOAN_PAYMENTS` over 24 months is $50,484.00 —
$49,884.00 of `AUTOMATIC PAYMENT - THANK` on the credit card plus $600.00 of
`CREDIT CARD 3333 PAYMENT` from savings. 100% of it is credit-card repayment. Nothing
appears against the mortgage ($56,302.06), student loan ($65,262.00), auto loan
($23,211.33) or HELOC ($13,500.50) — $158,275.89 of debt with no servicing visible.
"You've paid $50,484 toward loans" is believable, precise, and wrong about which debt.
This is #19's consequence and the sharpest quiet error in the dataset.

**F2 — every account that has transactions has a balance its transactions cannot
produce, and one is directionally impossible.** `query_transactions{account_id:5,
limit:1000}`: Plaid Money Market has 24 rows, every one an outflow, 24 × -$5,850.00 =
-$140,400.00, zero inflows ever — against a balance of +$43,200.00. Bridging it needs
$183,600 of unrecorded inflow. Same shape on the CD: 24 × -$1,000.00 `CD DEPOSIT
.INITIAL.` leaving the account, balance +$1,000.00. Round 2 flagged Checking's $9k gap
and explained it as a missing opening balance; the general form is worse. No account
reconciles, one is impossible, and no field in any payload says balances and
transactions are not expected to reconcile — so a user checking the system's arithmetic
against itself finds it broken everywhere with nothing to say that is expected.

**F3 — "What are my recurring charges?" leads with three things that are not charges.**
All 16 recurring rows are perfectly monthly, totalling $11,149.46/month. The top three
are GUSTO PAY $5,850.00 (payroll), AUTOMATIC PAYMENT $2,078.50 (card payoff) and CD
DEPOSIT $1,000.00 (saving) — $8,928.50, 80.1% of the monthly total, none of them a
charge. Genuine recurring spend is the tail: United $500, Tectra $500, KFC $500, Madison
Bicycle $500, Touchstone $78.50, SparkFun $89.40, McDonald's $12.00, Uber $6.33 + $5.40,
Starbucks $4.33. *(Depends on the round-3 build.)*

**F4 — no balance history exists, so the entire class of trend questions is
unanswerable, and nothing says so.** `list_accounts` returns one current snapshot per
account; there is no historical-balance tool and no `as_of` parameter. "Is my net worth
going up", "am I saving more than last year", "is the mortgage shrinking" cannot be
answered at all — and for the nine accounts with no transactions there is no second
route either. The surface answers the nearest wrong question (F1) without signalling the
substitution.

**F5 — refunds cannot be matched to their charges even with raw rows.** The only
refund-shaped rows are United Airlines +$500.00 monthly on Checking against -$500.00
monthly on the Credit Card, ~17 days apart: different account, different date, no shared
id, no linkage field, no `pending`. #20 covers refunds being unreachable through
`spending_summary`; this is the harder half — even the raw-row workaround cannot pair
them without guessing. Total measured inflow is $504.22/month ($500.00 United + $4.22
interest), which is why income and savings-rate answers stay unusable.

**F6 — balances were 3h53m stale and nothing marked them.** `get_pipeline_health`:
`last_success_at: 2026-09-08T13:48:35Z` against `as_of: 17:41:02Z`, status `active`,
`last_error_code: null`, and the only warning is the 8-day `gapped` notice about history
depth. No staleness threshold appears to fire. Benign in sandbox; in production a
nearly-four-hour-old balance presented as current under a reassuring `active` is a
real-money problem. *(Depends on the round-3 build.)*

**F7 — `retired` exists on connections but not on accounts.** `get_pipeline_health` rows
carry `retired: false`; `list_accounts` rows have no status field at all. A retired
*connection* is expressible and a closed *account* is not. This sharpens the carried
precondition: it is not a missing concept, it is a concept that stops one level short of
where it is needed.

**F8 — #16 re-confirmed open.** `query_transactions{since:2026-10-01, until:2026-10-31}`
(entirely future) → `rows: []`, warnings byte-identical to a covered query.

## Net worth

**-$77,164.15** (assets $86,541.74 − liabilities $163,705.89), arithmetic confirmed
against `list_accounts`, unchanged from round 2. The tester's own gloss: state the
number, then refuse to let anyone act on it — 9 of 14 balances have no transaction
behind them, the 5 that do cannot be reconciled (F2), it is a single snapshot with no
history (F4), and it was ~4 hours stale when read (F6). There is no `net_worth` tool, so
every consumer sums `current_minor_units` by hand and re-implements the asset/liability
sign convention independently.

## The gap between the question and the surface

Six ordinary questions; the tools answer one and a half. Net worth: derivable by hand.
Which accounts lack history: only via 14 separate `query_transactions` calls, where a
nonexistent id returns the same `[]` as a real quiet account. Debt paydown: answered
wrongly (F1). Recurring charges: derivable but led by non-charges (F3). Refunds: not
matchable (F5). Trends: impossible (F4). That maps closely onto the unbuilt AC-9.1
tools — a fourth independent round arriving at the shape of a spec written before the
code.

## Argument handling on the round-3 build

Clean, and reported as such: transposed windows refused on both tools with the swap
named, `limit` above the ceiling refused with the ceiling quoted, refusals naming
argument, rule and offending value with no exception class or SQL leaked. This says
nothing about the merged build.

---

# Round 4, half (a) — the merged build

**Date:** 2026-09-08, after the operator relaunched the tester against `f6e81f2`.
**Fingerprint:** `limit: 9999` → `limit must be at most 500, got 9999`. Ceiling 500, not
the 1000 the pre-merge build served, and the tool schema advertises `maximum 500` in
agreement with the runtime. New build confirmed before any testing began.

## The merge is verified. The numbers are right, not merely present.

- **Single month** (2026-07): `spending_summary` reconciles EXACTLY against the 16 raw
  rows across all 8 categories, counts and magnitudes both. The +50000 United Airlines
  inflow row is correctly excluded from TRAVEL.
- **Range spanning months** (2026-06-01..2026-08-31): exactly 3× the July figures in all
  8 categories, which is right for a strictly monthly-periodic fixture.
- **Partly outside coverage** (2024-01-01..2024-10-31 against coverage from 2024-09-16):
  30 rows, summary reconciles exactly against them.
- **Entirely future** (2027 Q1): `rows: []`, both tools, no crash.
- **Global cross-check:** 388 total rows − 339 outflow rows = 49, and exactly 49
  positive-amount rows enumerated independently. The summary is internally consistent
  with the row store.
- **Argument handling:** nine refusals, each naming argument, rule and offending value,
  with no exception class, stack trace or SQL leaked. The transposed-window refusal
  survived the merge on both tools with its wording intact.

## 🔴 The sign hypothesis is DISCONFIRMED. Read this before re-deriving it.

The tester, with no source access, proposed that a uniform negation applied under an
inverted convention would explain all five irreconcilable accounts at once, and named
the rows to check. The mechanism guess was correct and the conclusion was not.

`_operator_signed_amount` (`connector/plaid/derivers.py`) **does** negate every
aggregator amount, deliberately and documented: the aggregator sends positive for money
leaving a depository account, this datastore stores positive as money entering, so the
negation is what makes the two agree. Removing it would be wrong by twice the amount on
every spend row.

Queried directly from the datastore, with the aggregator's own category alongside:

| txn | acct | `amount_minor` | source category | description |
|---|---|---|---|---|
| 154 | 1 | **+50000** | TRAVEL / TRAVEL_FLIGHTS | United Airlines |
| 227 | 3 | −100000 | **TRANSFER_OUT** | CD DEPOSIT .INITIAL. |
| 360 | 4 | −50000 | TRAVEL / TRAVEL_FLIGHTS | United Airlines |
| 388 | 5 | −585000 | **TRANSFER_OUT** | ACH Electronic CreditGUSTO PAY 123456 |

🔴 **The amount and the aggregator's own category agree with each other and disagree
with the description text.** The GUSTO row is `TRANSFER_OUT` at the source; only the
word "Credit" in the free-text description says otherwise. Same for `CD DEPOSIT`. This
is the ruling already recorded in the handoff — sandbox description strings are
decorative, and we store what arrives.

**So finding [1]'s premise — that these rows are inflows misreported as spending — does
not hold.** Its number does, and it lands on a different and real defect; see below.

**Balance irreconcilability (finding [2]) has the same root:** the aggregator's sandbox
serves static balance fixtures unrelated to its synthetic transaction stream. The
tester's own census is the evidence that closes it — the misses do **not** share a
direction (four accounts short of inflows, Checking long by a spurious one), and
`TRANSFER_IN` exists and carries 25 correctly-positive rows, which falsifies any
"inflows are dropped" transform. Not a product defect. 🔴 **What remains a product
defect is that nothing on the surface says balances and transactions are not expected
to reconcile** — a holder checking the system's arithmetic against itself finds it
broken everywhere with nothing to say that is expected.

## What survives as product findings

**A1 — #18 now has its number, and it is bigger than anyone assumed.** `spending_summary`
filters `amount_minor < 0` and labels the result spending. Over the full coverage window
the top row is `TRANSFER_OUT`, 48 transactions, **$164,400.00 — 61% of the $267,692.77
two-year total, and the largest category by a factor of three.** Adding the 24
`AUTOMATIC PAYMENT` card-payoff rows inside `LOAN_PAYMENTS` reaches 72 rows and
$214,284.00, **80% of the reported total**, none of it discretionary spending. This is
exactly what the ruled classify-don't-filter fix removes. Nothing in `warnings`,
`coverage` or `as_of` contradicts the number, and "you spent $164,400 on transfers"
reads as an answer.

**A2 — `warnings` is connection-level and INVARIANT; it never responds to the request.**
The identical single `gapped` notice appears verbatim on a window fully inside coverage,
a window 8 months of which precede coverage, a window entirely in the future, and a query
for an account that does not exist. It therefore carries zero information about whether
*this* answer is degraded. 🔴 **The sharpest framing of #16 yet: the warning's presence
actively suggests the system is already telling the caller what is wrong.**
`coverage.earliest_transaction` / `latest_transaction` are present and correct, so a
caller *can* work it out — but only if they think to.

**A3 — a nonexistent `account_id` is answered, not refused.** `query_transactions
{account_id: 999}` → `rows: []`, no warning. Compare an invented *argument name*, which
is refused by name with the valid ones listed. **A typo in an argument is loud; a typo in
an account id is silent** — and the silent one is the one that returns a confident empty
answer. Appears unfiled; belongs beside #19.

**A4 — nine of fourteen accounts report a balance with zero transaction history and
nothing says so.** IRA, 401k, Student Loan, Mortgage, HSA, Cash Management, Auto Loan,
HELOC, Business Credit Card, carrying real balances (Mortgage −5630206, Student Loan
−6526200, 401k +2363198). Nothing distinguishes "this account has no feed" from "this
account had no activity", so "have my mortgage payments been going out?" gets an empty
list that reads as no. This is the carried NEW-4, re-confirmed on the merged build.

**A5 — cosmetic.** `2026-02-30` is refused with "must be a calendar date in YYYY-MM-DD
form". The format *is* YYYY-MM-DD; the day does not exist. Defensible via the word
"calendar", but a user who typed a plausible date is told their format is wrong.

**Confirmed working, not a defect:** `merchant` is null on ~60% of rows and all 25
SparkFun rows carry merchant `FUN`, exactly as the tool description warns. The
description is honest and held up under an independent reader.

## A6 — the United Airlines sign flip: not a defect, and it already has a home

Raised by the tester as the one item its A-list did not cover, correctly — the
decorative-description ruling does not reach it. There the description was the only
evidence; here the source categories match on both rows and it is the **amount** that
differs.

- txn 154, acct 1 (Checking), **+50000**, TRAVEL/TRAVEL_FLIGHTS, "United Airlines", ×24
- txn 360, acct 4 (Credit Card), **−50000**, TRAVEL/TRAVEL_FLIGHTS, "United Airlines", ×24

**The pipeline is exonerated by construction.** `_operator_signed_amount` ends in a
single unconditional `return negate(exact)` — no branch on account, category, merchant
or sign. A stored +50000 therefore means the aggregator sent −50000. The two rows differ
because the source says they differ: a charge on the card and a credit on checking.

🔴 **The surface consequence is already filed, and was predicted with this exact
number.** `.prawduct/artifacts/mcp-production-readiness.md` §§145-159 records that
`spending_summary` filters `amount_minor < 0`, so the offsetting +$500 credits are
inflows it never sees — **"after #18 ships as ruled, TRAVEL still reports $12,000
against a true $0"**, called there the sharpest example in the issue's own text. That is
issue **#20** (gross-vs-net), linked both ways to #18 precisely so a builder shipping
#18 and then checking TRAVEL does not conclude the fix failed.

An independent reader with no source access and no sight of that document arrived at the
same rows and the same $12,000. That is third-party confirmation of #20's scope, not a
new finding — and it is the strongest evidence yet that the #18/#20 linkage was worth
writing down.

## 🔴 A reading rule for round 5, from the tester's own correction

On this surface, `description` is authoritative **against `merchant`** — the tool
description says so, and the `SparkFun`/`FUN` case proves it. It is **NOT** evidence
against `category` or `amount`. The tester read a `TRANSFER_OUT` category sitting in its
own quoted output and weighted the free-text "Credit" over it, which is how finding [1]
got its wrong premise. The structured field wins.

**This is narrower than what the tool description states**, and the tester got it wrong
in the permissive direction — which makes it a candidate defect in the *description*
rather than only a tester error. Worth deciding when the AC-9.1 work touches tool text.
