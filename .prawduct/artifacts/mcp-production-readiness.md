# MCP server — production go/no-go

Date: 2026-09-08. Written by the acceptance session, black-box only: tool
responses and tool descriptions, no source read, nothing fixed. Requested as
input to a real decision about real accounts.

Basis: acceptance rounds 2 and 3 against this build, plus six probes re-run
today to confirm the claims below still hold rather than quoting my own reports
back. Every figure here was either measured today or is cited to the round it
came from.

---

## Verdict

**No. Not today.**

Not "no, and here is a list of anxieties" — no, and here is a short list of
things that have to be true first, most of which are already filed and one of
which is not.

The reason is one sentence: **this surface's characteristic failure is a
plausible wrong number with no signal in the payload that anything is wrong,
and the only reason three rounds of testing caught any of them is that the
sandbox fixture was absurd enough to trip me.** Real data will not be absurd.
Every number will look like it could be true.

That is the whole argument. The rest of this document is the supporting detail.

### What keeps this from being a harder no

The surface is read-only and narrow: four tools, no mutating argument on any of
them, no write path exposed. A wrong answer here produces **bad advice**, not an
unauthorized transaction. That is the difference between "fix these seven things
first" and "do not connect this to a bank." I want the operator to hear the
distinction — the risk is a household making a real decision on a confidently
wrong figure, not losing money directly.

---

## The four filed issues

Your instinct was #16 and #19 block, #17 and #18 don't. **I agree on #16 and
#19, disagree on #17, and land close to you on #18 with one caveat that
matters.**

| Issue | Your call | Mine | Why |
|---|---|---|---|
| #16 window clamp | block | **block** | agreed, no argument to add |
| #19 account coverage | block | **block** | agreed, and it is structural, not a fixture artifact — worse in production |
| #17 truncation | ship | **block** | severity is a function of dataset size; sandbox is 20× too small to have shown it |
| #18 transfers/debt | ship | **ship, with two conditions** | it is a loud error at the total level and a quiet one at the category level |

### #16 — blocking. Agreed.

Re-confirmed today. `spending_summary(2024-08-01 .. 2024-08-31)`, entirely
before coverage begins, returns `rows: []` with a `warnings` array **byte-identical**
to a fully covered query. The consumer has nothing to discriminate on. An
operator enrolling accounts in production gets a coverage start date of *today*
for anything the institution does not backfill, so this is not an edge case at
enrollment — for the first 24 months it is the common case, and "what did I
spend last spring" answers $0.

### #19 — blocking. Agreed, and stronger than filed.

This is the one I would not compromise on, because it is **structural rather
than a property of the fixture**. Only `/transactions/sync` is pulled;
investments and loans have their own endpoints that are not built. So the
accounts with balances and no transactions in production will be the same
classes as in sandbox: mortgage, student loan, auto loan, HELOC, 401k, IRA, HSA.

Those are not incidental accounts. They are the ones a household asks the
important questions about — am I paying down the mortgage, am I on track for
retirement. In sandbox they are 82% of the balance sheet by absolute magnitude
(round 2). In a real household carrying a mortgage and retirement accounts the
share will be similar or higher.

Confirmed today: `list_accounts` has **no per-account coverage field of any
kind**, and `query_transactions(account_id=9)` — a real account holding
-$56,302.06 — returns `rows: []` indistinguishable from a quiet month.

### #17 — I think this blocks, and this is my main disagreement

The sandbox could not show you the severity of #17, because severity here is
almost purely a function of row count, and the fixture has 388 rows.

- 388 transactions / 24 months / 1 institution ≈ **16 per month**. At that
  density the default `limit: 100` covers about six months, and most ordinary
  queries never touch it. This is also exactly why round 2's NEW-2 went wrong in
  the first place: `limit: 9999` returned "everything" only because everything
  was 388 rows.
- A real household across several institutions with normal card use runs
  **an order of magnitude higher** — I am estimating, not measuring, but a few
  hundred transactions a month over two years puts the dataset in the thousands.
  At that density the default limit returns roughly the **newest few weeks**,
  and it does so on essentially every unbounded query rather than rarely.

Three facts compound, and it is the combination that makes me call this
blocking rather than annoying:

1. **The truncation is silent.** Confirmed today: `limit: 3` returns 3 rows and
   the response carries no `returned`, `matching`, or `truncated`.
2. **The one field a caller would self-check against is not a completeness
   signal** (round 3, NEW-9). `coverage.transactions` reported **388** on every
   probe today regardless of window or limit — on the 3-row call, on the 0-row
   call, on all of them. It counts the datastore, not the query. So it flags
   phantom truncation on correct answers and stays silent on real ones.
3. **There is no pagination.** Confirmed from the tool schema: `query_transactions`
   accepts `account_id`, `limit`, `since`, `until` — no cursor, no offset. With
   the ceiling at 1000, a window containing more than 1000 rows **cannot be
   retrieved in full at all.** The workaround is bisecting by date, which
   requires knowing you were truncated, which is fact 1.

So in production the tool routinely returns a partial history, cannot say it
did, and cannot be made to return the rest. Any agent doing its own aggregation
over `query_transactions` — which the round-3 merchant work shows is exactly
what a consumer is steered toward — silently computes over the newest slice.

I would not ship that against real money. I would accept it as non-blocking
**only** if `spending_summary` were the sole sanctioned path for any totalling
question and `query_transactions` were documented as a browse-only tool that
must never be summed. That is a real option, and cheaper than pagination — but
it needs to be a stated contract, not an assumption.

### #18 — ships, with two conditions. And the accepted fix has a hole.

I land where you do, and here is the reasoning, because it also gives the
operator a rule for everything else.

**The discriminator: does the defect produce an implausible number or a
plausible one?** An overcount that reads as $267,693 of annual household
spending gets questioned by any human who sees it — #18's own ruling makes this
argument for choosing classify-over-filter, and it is correct. #16, #17 and #19
produce numbers that look completely ordinary. Loud errors are self-limiting;
quiet ones are believed. That is why #18 sits on the other side of the line from
the other three.

Two conditions on shipping it, though:

**First — the distortion is not loud at category level.** Re-measured today,
July 2026: total $11,149.46, of which `TRANSFER_OUT` $6,850.00 and
`LOAN_PAYMENTS` $2,103.50 — **80.3% of the reported month is the holder moving
their own money and paying their own card.** The headline total is absurd enough
to catch. "You spent $500 on travel in July" is not; it is completely believable
and the true net is $0. So a warning in the tool *description* is not enough —
`spending_summary` needs the distortion stated in the **payload**, per response,
or a consumer that quotes one category will never know.

**Second, and please check this before taking #18 to the operator: the accepted
fix does not fix the case #18 leads with.** #18 lists three distortions —
internal transfers, double-counting, and gross-vs-net. The ruled fix adds
`flow_class` of `external_spend` / `internal_transfer` / `debt_service` plus
`total_external_spend`. That resolves the first two. It does **not** resolve
TRAVEL: the offsetting +$500 on checking is a refund, not a transfer, and
`spending_summary` is outflow-only by contract, so it is not in the summary at
all and has no row to classify. **After #18 ships as ruled, TRAVEL still reports
$12,000 against a true $0** — the single sharpest example in the issue's own
text. Either the scope needs to grow to cover refund-netting, or the issue
should say plainly that gross-vs-net is out of scope and file it separately. I
would not want that discovered after the fix lands and the case is presumed
closed.

---

## What has to be true before yes

> **Tracking, added 2026-09-09.** The remaining items carry the
> **`blocks:production`** label on `brookstalley/bankmachine`, so the open gate is one query rather
> than a re-read of this list: `gh issue list --label blocks:production`. 🔴 **This document stays
> the authority on WHY each one blocks** — the label is an index into it, not a replacement for it,
> and an item wearing the label without a numbered entry here would be "important" rather than
> "blocking", which is the distinction that makes the gate mean anything.
>
> **State on 2026-09-09:** items 1–4 are done — #16 and #17 shipped in `mcp-effective-window`, and
> #19 and #21 in `mcp-answer-scope-completion`. Items 5, 6 and 7 remain and are #22, **#40** and
> #23. Item 6 had never been filed until now, which is what this list warned about in its own
> preamble ("most of which are already filed and one of which is not").
>
> 🔴 **Two of the three remaining cannot be closed before production data, by their own
> definition** — #22 needs a real pending row watched across settlement and #23 needs a real
> deposit. They do not block *connecting* production data; they block *trusting particular answers*
> once it is connected, and § "Day one in production" below is how they get discharged. **#40 is
> the one that is buildable now**, and cheaper than this list knew: `accounts` already carries
> `lifecycle_status` and `closed_date`, and `list_accounts` simply does not read them.
>
> 🔴 **Correction, 2026-09-09 — all three items were checked against the source tree, and all
> three are misdescribed above.** Closability and buildability are separate axes and this block
> conflated them.
>
> **Item 6 (#40) — "cheaper than this list knew" is wrong; the closability claim stands.**
> The columns exist, but **nothing has ever written a non-`active` value to either.** The accounts
> deriver hardcodes `"active"` at insert and excludes the column on update; no CLI command and no
> tool can set it; `connections retire` does not touch `accounts`, and `closed_date` has zero
> readers and zero writers in `src/`. So item 6 is a population path **plus** a read path **plus** a
> migration, not a read-path change. It remains the only one of the three closable before
> production data — more firmly than this list knew, since a shrinking roster replays from the
> archive rather than needing a real closure.
>
> **Items 5 and 7 (#22, #23) — not closable before production data, but far from unbuildable.**
> Item 5's pending→posted contract is implemented and unit-tested; what has never run is the
> **read path**, which has never seen a pending row and does not disclose one. Item 7's
> normalization is an *unconditional* negation with no branch, already exercised inflow-wards on 49
> of the 388 sandbox rows; the genuinely blocked claim is narrower — the sandbox is
> **single-connection**, so a per-connection sign check cannot run at all. Both carry substantial
> criteria that gate on nothing and can ship as ordinary work; only a gating tail needs real data.
>
> Full derivation and proposed (**not ratified**) requirements:
> `.prawduct/artifacts/discovery-account-lifecycle.md` and
> `.prawduct/artifacts/discovery-production-data-semantics.md`.

Ordered by what I would do first. Items 1–3 are the filed blockers; 4–7 are not
currently filed and items 4 and 5 are the ones I would be most annoyed to
discover in production.

1. **#16** — `effective_window` plus the two warning kinds, as designed.
2. **#19** — per-account coverage signal, sharing #16's design. Fold in the
   surviving half of round 3's **NEW-3** while you are there: a nonexistent
   `account_id` must be refused, not answered with `[]`. Confirmed still open
   today — `account_id: 999` returns `{"rows": []}`. The range check catches
   values below the floor because the floor is a constant; nothing catches above
   it because the ceiling is data. The operator will hit this by typo in week
   one, and it reads as "$0 spent."
3. **#17 + NEW-9** — either `returned`/`matching`/`truncated` and a way past the
   1000 ceiling, **or** an explicit contract that `query_transactions` is
   browse-only and never to be summed. Whichever you choose,
   `coverage.transactions` needs to stop looking like a completeness signal.
4. **Multi-currency behaviour must be defined before a non-USD account is ever
   enrolled. This is not filed and I could not have found it before today.**
   `list_accounts` carries `currency` per account and `query_transactions`
   carries it per row — but I checked `spending_summary`'s rows today and they
   are `{category, transactions, spent_minor_units}` with **no currency field at
   all**. With one currency that is fine. With two it is unfixable from the
   consumer side: a bare `spent_minor_units` cannot be interpreted without
   knowing the currency, minor units are not even the same scale across
   currencies, and the payload has nowhere to say which it means. One foreign
   card, one travel account, and every category total silently becomes a
   meaningless integer. Cheapest correct answer is probably to refuse to
   aggregate across currencies and say so, rather than to build conversion.
5. **Pending transactions.** `pending` is `false` on all 388 sandbox rows, so
   the field's behaviour has never been exercised. In production pending rows
   are constant and dominate any "what did I spend this week" answer. Two
   specific risks, both untested: a pending row counted and then counted again
   when it posts under a new id, and a pending row that vanishes without a
   trace. This needs to be defined and tested against real settlement before
   anyone trusts a current-month figure.
6. **Closed and retired accounts.** `list_accounts` has no status field —
   confirmed again today. A closed card or paid-off loan keeps contributing its
   last balance to net worth indefinitely, with nothing marking it. Real
   households close accounts; this one never has.
7. **Sign convention on a real inflow.** Round 2's payroll-as-outflow was
   correctly adjudicated as faithful Plaid passthrough, and I am not reopening
   it. The point is different: **nobody has ever watched this pipeline handle a
   correctly-signed real deposit**, because the fixture's only payroll row
   arrives pre-inverted from the aggregator. The normalization is unverified in
   the direction it will actually run.

---

## Where my confidence comes from sandbox specifics that will not hold

You asked directly, so here is the honest answer, including the part that
undercuts my own reports.

**The fixture was my test oracle, and production has no oracle.** Every
significant finding across three rounds was found the same way: a number was
visibly ridiculous and I pulled the thread. $504/month income against a $56k
mortgage. $267,693 of household spending. A merchant named "FUN." A payroll
credit signed as an outflow. Those are not subtle. They are what let a black-box
tester with no ground truth detect anything at all.

Real data produces none of that. A real household's spending total is plausible.
Its income is plausible. Its merchants have real names. **The defects that
survive into production are precisely the ones that produce plausible output** —
which is the same set I am calling blocking. So my rounds-2-and-3 clean bill on
everything else should be read narrowly: I verified internal *consistency*, and
consistency is all a black-box tester can verify without ground truth.

The strongest thing I verified is a good example of both the strength and the
limit: `spending_summary` and `query_transactions` reconcile **exactly**, all
eight categories, sums and counts, totalling 26,769,277 minor units all-time,
identical in rounds 2 and 3. That is real and it is worth something — the two
tools cannot disagree. It says nothing about whether either matches the bank.

Specific sandbox properties my confidence rests on that production breaks:

- **One connection.** The two zero-arg tools have never been made to disagree,
  because they cannot be with one connection. In production the `warnings` array
  becomes multi-connection, and it is already the weak part of the payload: the
  `gapped` warning fired on **every one of my six probes today**, including on
  windows entirely inside the granted range. Round 3 called that "trains a
  consumer to ignore the field." With five connections it stops being noise and
  becomes ambiguity — a consumer cannot tell which institution the gap is in, or
  whether the account they asked about is the affected one.
- **One sync, no incremental round.** The cursor boundary between the initial
  full pull and later incremental syncs is where duplicates and dropped rows
  live, and a single-shot fixture never exercises it. My "no duplicates across
  388 rows" check tested a dataset that was written once.
- **Balances that cannot be checked.** Round 2 found Checking at $110.00 against
  a 24-month transaction net of +$9,075.23, and correctly explained it as a
  missing opening balance. The consequence for production is the important part:
  **there is no internal way to reconcile a balance against its transactions**,
  so a sync that silently drops rows looks exactly like the expected
  no-opening-balance discrepancy. Combined with #19, a partially-synced account
  and a genuinely quiet one are the same payload.
- **Scale**, as argued under #17.
- **`balance_as_of` is uniform.** Credit where it is due — the field exists and
  is the right shape. But every account reports the same timestamp here, so
  divergent per-connection staleness, which is normal in production, has never
  been seen.

---

## Day one in production: what the operator must check, because no test session can

These all require ground truth about a real household's actual finances. No
amount of black-box testing substitutes for any of them, and the first two are
the ones with **no internal signal whatsoever**.

1. **Reconcile every account balance against the institution's own app, same
   day.** This is the highest-value check available and it catches sync gaps,
   stale connections, and zombie accounts at once. Nothing inside the system can
   perform it.
2. **Confirm the account list is complete** — enumerate the accounts you know
   you have and compare against `list_accounts`. **An account that was never
   received is invisible.** There is no field that can report it and no query
   that can detect it. This is the single failure mode with zero internal
   signal, and net worth is wrong by exactly that account's balance.
3. **Check the sign of one known paycheck and one known bill.** Confirm the
   deposit is positive and the bill negative. See item 7 above — this has never
   been observed running in the correct direction.
4. **Check per-institution history depth against what you were actually
   granted.** Institutions differ sharply through Plaid — some give 24 months,
   some 90 days. `coverage.earliest_transaction` is a single global figure, so a
   short-history institution is averaged into a number that looks fine.
5. **Watch one pending transaction across settlement.** Note it while pending,
   re-query after it posts, confirm it appears exactly once and the amount is
   the settled one. This is item 5 above and it cannot be rehearsed here.
6. **Re-check for duplicates after the second sync, not the first.** The first
   sync is a full pull; the second is the incremental path, and that is where
   the cursor boundary can double or drop rows.
7. **Confirm every enrolled account is USD** — and if any is not, do not ask
   spending questions at all until item 4 above is resolved.
8. **Pick three questions you already know the answer to** — last month's rent,
   the current card balance, what you actually paid the mortgage — and ask them
   through the tools before asking anything you don't know. Establish the oracle
   deliberately, because that is what the sandbox was providing for free and
   production will not.

---

## What I verified today, so you can weigh this

Six probes against the running server on this build, all confirming the current
state rather than quoting prior rounds:

- `list_accounts` — 14 accounts, all USD, `balance_class` correct throughout, no
  status/retired field, no per-account coverage field.
- `query_transactions(account_id=999)` → `{"rows": []}`, no refusal. NEW-3's
  account arm still open.
- `spending_summary(2024-08-01..08-31)`, pre-coverage → `rows: []`, warnings
  identical to a covered call. #16 confirmed open.
- `query_transactions(limit=3)` → 3 rows, `coverage.transactions: 388`, no
  truncation field. #17 and NEW-9 confirmed open.
- `spending_summary(2026-07-01..07-31)` → 8 categories, $11,149.46, of which
  80.3% is `TRANSFER_OUT` + `LOAN_PAYMENTS`; row shape carries **no currency
  field**. #18 confirmed open, and the multi-currency gap found.
- Tool schemas read directly: four tools, all read-only, no mutating argument,
  and no cursor or offset on `query_transactions`.

I did not re-derive #16, #18 or #19's quantitative detail — rounds 2 and 3 have
it and re-measuring would only restate it.
