# MCP acceptance — round 4, half (a)

**Date:** 2026-09-08. **Tester:** independent acceptance session, no source access.
**Build exercised:** `{"version":"0.1.0","commit":"87570c3","dirty":false}` — verified
as a descendant of both `6532659` (build identity) and `97085df` (unknown account id).

This is the run that round 4's main record listed as UNRUN. The client session was
relaunched, so the stdio subprocess serves current code. Corroborated independently of
the `build` field: `limit:9999` now returns `limit must be at most 500, got 9999`, where
round 3 recorded a ceiling of 1000 — `8c92131` is live.

## What passed

**The date-window fix is numerically correct.** August 2026 was rebuilt from raw rows
and reconciled against the summary by hand: 16 rows, 8 categories, outflow total
1,114,946 minor units, every category matching to the unit. The two inflows in the
window (United Airlines +50,000 and INTRST PYMNT +422) are correctly excluded from
spend. Month-spanning arithmetic is exact — June, July and August each return the same
monthly cycle, and `2026-06-01..2026-08-31` returns exactly 3x every category amount and
every transaction count, so nothing is double-counted or dropped at a month boundary.
Both bounds are inclusive: `2026-08-07..2026-08-10` returns precisely GENERAL_MERCHANDISE
1/8,940, PERSONAL_CARE 1/7,850, FOOD_AND_DRINK 2/1,633, and a single-day window
`2026-08-27..2026-08-27` returns exactly transaction 12.

**Argument handling refuses cleanly and leaks nothing.** Every refusal names the
argument, the rule and the offending value; none carries an exception class, stack trace
or SQL.

| Call | Response |
|---|---|
| `since:"2026-08-31", until:"2026-08-01"` | `until (2026-08-01) is before since (2026-08-31), so the window selects nothing. Did the two get swapped?` (same on both tools) |
| `since:"08/01/2026"` | `since must be a calendar date in YYYY-MM-DD form, got '08/01/2026'` |
| `since:"2026-13-45"` | `since must be a calendar date in YYYY-MM-DD form, got '2026-13-45'` |
| `limit:0` | `limit must be at least 1, got 0` |
| `limit:-5` | `limit must be at least 1, got -5` |
| `limit:99999` | `limit must be at most 500, got 99999` — refused, not trimmed, as the contract promises |
| `untl:"2026-08-31"` | `query_transactions has no argument 'untl'. It accepts: account_id, limit, since, until` |
| `category:"FOOD_AND_DRINK"` | `query_transactions has no argument 'category'. It accepts: account_id, limit, since, until` |
| `account_id:0` | `account_id must be at least 1, got 0` |

**`97085df` works, and the control it had to preserve is intact.**
`query_transactions{account_id:9999, since:"2026-08-01", until:"2026-08-31"}` →
`account_id 9999 does not exist. list_accounts reports the ids that do.` And the control:
`query_transactions{account_id:1, since:"2026-08-28", until:"2026-08-31"}` → `rows: []`,
no refusal. An account that exists but is quiet in the window still answers honestly.
The fix did not over-reach.

**Known boundary confirmed, not a regression.**
`query_transactions{account_id:7, since:"2024-01-01", until:"2026-12-31", limit:5}` →
`rows: []`. Plaid 401k has no transaction feed at all, and its answer is byte-identical
to account 1's genuinely-empty window above. Nothing distinguishes "no coverage" from
"no activity". This is the third case the brief flagged as unfixed on purpose.

## Findings, ranked by how badly they would mislead the account holder

**A1 — `limit` truncates silently, and its default of 100 silently drops history from
answers that look complete.** This is the same failure shape `97085df` just fixed for
`account_id`, still live on the other axis, and it is the quiet kind.

Call, with no `limit` so the documented default of 100 applies:
`query_transactions{account_id:4, since:"2024-01-01", until:"2026-12-31"}`

Returns 100 rows, newest 2026-08-27, oldest `transaction_id:321` dated 2025-04-28 —
stopping *mid-month*, partway through April 2025's cycle. `warnings` carries only the
standing `gapped` entry. There is no truncation marker, no count of total matches, no
`partial`. The payload is shaped exactly like a complete answer.

It is provably incomplete: `query_transactions{since:"2024-01-01", until:"2024-10-01"}`
returns Plaid Credit Card rows `transaction_id` 361–365 dated 2024-09-19 through
2024-10-01. Roughly sixteen months of that account's history is missing from the
"whole range" answer with nothing saying so.

Why it outranks everything else here: a caller who asks for two years of card activity
and sums what comes back understates the total by about 40%, and every number derived
from it is precise, plausible and wrong. The ceiling being *refused* rather than trimmed
at 501+ makes this sharper, not softer — the contract is explicit that over-asking is an
error, yet under-asking silently is not.

**A2 — the `gapped` warning is a constant, so it cannot tell you whether *your* window
is the one that is gapped.** The identical warning object appears on every response in
this run — on windows wholly inside coverage and on windows wholly outside it.

`spending_summary{since:"2024-01-01", until:"2024-06-30"}` → `rows: []`, with the same
`gapped` warning as everything else. `coverage.earliest_transaction` is 2024-09-16, so
the first half of 2024 has no coverage at all: the honest answer is "unknown", and the
delivered one reads as "you spent nothing." Compare
`spending_summary{since:"2026-08-01", until:"2026-08-31"}`, which returns a full and
correct answer carrying that same warning character-for-character.

Because it never varies, the warning conveys nothing about the answer it rides on. Its
text — "anything older than that is absent rather than zero" — is exactly the right
sentence, attached to every response rather than to the ones it describes. A careful
reader can derive the truth by comparing their window against `coverage`, but nothing in
`warnings` flags the affected answer, and an empty summary is the most believable wrong
answer the surface can give.

**A3 — a window entirely in the future returns `rows: []` with no marker.**
`query_transactions{since:"2027-01-01", until:"2027-12-31"}` → `rows: []`, standing
`gapped` warning only, against `as_of: 2026-09-08`. Defensible — nothing has happened
yet — but the response is indistinguishable from a real quiet period, and no `partial`
or future-window signal appears. Lowest severity of the three; listed for completeness.

## Notes

`coverage` is internally consistent: `earliest_transaction` 2024-09-16 is exactly 722
days before `as_of` 2026-09-08, matching the 722-of-730 figure in the warning text.
`build` was present and identical on all sixteen successful calls.
