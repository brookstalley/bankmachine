# bankmachine — financial modelling & money arithmetic review

Read-only review, 2026-09-09, against `develop` @ 4b78550. Every claim below was
verified against the code; the ones marked **measured** were reproduced by running the
shipped derivers or by calling the shipped MCP tools against the sandbox store.

Failure mode under review: a **silently wrong number**, not a crash.

---

## BLOCKER 1 — `flow_class` is a relabelling of Plaid's category, but the payload states it as a fact about where the money went; the headline spending figure UNDERSTATES spending

**Where:** `src/bankmachine/query.py:2069-2077` (`_INTERNAL_TRANSFER_CATEGORIES`,
`_DEBT_SERVICE_CATEGORIES`), `src/bankmachine/query.py:2112-2147` (`_flow_class`),
`src/bankmachine/mcp.py:368-377` (the `totals` description), `src/bankmachine/mcp.py:800-807`.

**Evidence.** The entire classification is:

```python
_INTERNAL_TRANSFER_CATEGORIES = frozenset({"TRANSFER_IN", "TRANSFER_OUT"})
_DEBT_SERVICE_CATEGORIES = frozenset({"LOAN_PAYMENTS"})
...
return case(
    (transactions.c.source_category_primary.in_(sorted(_INTERNAL_TRANSFER_CATEGORIES)), "internal_transfer"),
    (transactions.c.source_category_primary.in_(sorted(_DEBT_SERVICE_CATEGORIES)), "debt_service"),
    else_="external_spend",
)
```

Nothing matches the two sides of a transfer against each other, and nothing checks whether
a counterparty account is enrolled. Yet the wire says (mcp.py:368-377):

> "what actually left the household, what only moved between the holder's own accounts, and
> what serviced a debt. Only `external_spend_outflow_minor_units` is spending — an internal
> transfer never left, and debt service settles purchases already counted under the
> categories they were spent in"

and the tool description tells the agent: *"Read `totals` before quoting any spending
figure, and quote `external_spend_outflow_minor_units` from it."*

**Four ways that instruction produces a wrong answer, all in the understating direction —
the direction this repo's own principle (`api-contract.md`: "an overcount gets questioned
and an undercount gets believed") says is the dangerous one:**

1. **Real income is called an internal transfer. Measured in this store.**
   `mcp__bankmachine-sandbox__money_summary` returns
   `{"group_key":"TRANSFER_IN","flow_class":"internal_transfer","inflow_minor_units":10550}`.
   `docs/system-requirements.md` AC-14.6 and `signs.py:31-34` both record that the sandbox's
   *payroll* row is categorised `TRANSFER_OUT`/`TRANSFER_IN` in both structured fields. So
   the project has already **measured** that Plaid's PFC labels a paycheque as a transfer —
   and `_flow_class` then declares that paycheque "money that only moved between the
   holder's own accounts."
2. **Loan payments that are genuine household cost are removed from spending.** Plaid's
   `LOAN_PAYMENTS` primary covers `LOAN_PAYMENTS_MORTGAGE_PAYMENT`, `_CAR_PAYMENT`,
   `_STUDENT_LOAN_PAYMENT`, `_PERSONAL_LOAN_PAYMENT` as well as
   `_CREDIT_CARD_PAYMENT`. Only the last is the double count the payload describes. The
   sandbox roster this build already syncs holds `Plaid Mortgage`, `Plaid Student Loan`,
   `Plaid Auto Loan` and `Plaid Home Equity Line of Credit` (measured via `list_accounts`),
   so production will have exactly these rows.
3. **Third-party outflows are called internal.** `TRANSFER_OUT` includes
   `TRANSFER_OUT_WITHDRAWAL` (an ATM cash withdrawal), `TRANSFER_OUT_ACCOUNT_TRANSFER`
   (which is where Zelle/Venmo/most P2P land) and `TRANSFER_OUT_OTHER_TRANSFER_OUT` (rent
   paid by ACH). Every one of them left the household and every one is excluded from
   `external_spend`.
4. **The double-count claim is only true if the card is enrolled.** If the operator links
   checking but not one of their cards, the card payment is the *only* record of that
   spending, and it is classified `debt_service` and told to the agent as "already counted
   under the categories they were spent in."

**Numeric failure scenario.** Checking + one card linked; a $2,400 mortgage payment, a $300
ATM withdrawal, a $1,200 rent ACH, $5,000 of card purchases, an $1,800 card payment, and a
$6,000 paycheque, in one month.
- Truth: money that left the household ≈ $2,400 + $300 + $1,200 + $5,000 = $8,900; income $6,000.
- `money_summary` totals: `external_spend_outflow = 5,000`, `internal_transfer_outflow = 1,500`,
  `debt_service_outflow = 4,200`. The agent, following the tool's own instruction, answers
  **"you spent $5,000"** — 44% of the truth — and, asked about income, reads a
  `TRANSFER_IN`/`internal_transfer` row and reports **$0 of income** (or reports the $6,000
  as "money you moved between your own accounts").

Every number is well-formed, every sum completes, and the payload actively asserts the wrong
interpretation. `source_category_detailed` — the column that could separate most of these —
is stored and has **zero readers** (`grep -rn source_category_detailed src/` → only the
schema and the deriver).

**Fix.**
- Split the classification on `source_category_detailed`, not `source_category_primary`:
  `TRANSFER_OUT_WITHDRAWAL`, `TRANSFER_*_THIRD_PARTY`/`OTHER_*` → `external_spend`;
  `LOAN_PAYMENTS_CREDIT_CARD_PAYMENT` → `debt_service`, the other `LOAN_PAYMENTS_*` →
  `external_spend` (or a fourth class named for what it is, e.g. `debt_principal_and_interest`).
- A transfer may only be called *internal* when the other leg is found: same amount, opposite
  sign, within ±3 days, on another enrolled account. Where no counterpart is found, emit a
  distinct class (`transfer_counterparty_unmatched`) rather than asserting `internal_transfer`.
- Change the `totals` and tool text so it stops asserting "never left" / "already counted"
  for rows where that was inferred from a label rather than observed.
- **Tests to pin it:** (a) a fixture with `TRANSFER_OUT_WITHDRAWAL` asserting it lands in
  `external_spend`; (b) a fixture with `LOAN_PAYMENTS_MORTGAGE_PAYMENT` asserting it is not
  reported under a class the payload describes as already-counted; (c) a `TRANSFER_OUT` with
  no matching leg on any enrolled account asserting it is not called `internal_transfer`;
  (d) the payroll row from `api-notes-plaid.md` §17 asserting it is not classified as an
  internal transfer.

---

## HIGH 2 — A settlement moves the transaction's DATE, silently moving money between periods and out of an already-answered window with nothing to attribute it to

**Where:** `src/bankmachine/connector/plaid/derivers.py:543` (`"posted_date": _parse_calendar(entry.get("date"), ...)` inside the update path), `src/bankmachine/query.py:1518-1547` (`HoldTransitions`).

**Evidence — measured.** Replaying two real-shaped `/transactions/sync` pages through the
shipped deriver:

```
after hold    [(1, 'pend-1', pending=1, -200000, 2026-06-30, removed=None)]
after settle  [(1, 'post-1', pending=0, -214500, 2026-07-02, removed=None)]
```

Plaid's `date` is the transaction date while pending and the **posting date** once posted, so
the row legitimately changes date on settlement. `_write_transaction` writes the new date over
the old one with no record that it moved.

`HoldTransitions`' own docstring claims the complete set of ways a figure moves:

> "it changes in exactly two ways: a hold expires and its whole amount leaves the total, or a
> hold settles and its amount is replaced by the settled one."

There is a third, and `_hold_transitions` cannot see it: both of its tallies are filtered by
`posted_date` inside the window, so a row that settled *out* of the window appears in neither.

**Numeric failure scenario.** $2,000 hotel authorised 2026-06-30; posts 2026-07-02 at $2,145.
- `money_summary(since=2026-06-01, until=2026-06-30)` asked on 7-01 → June outflow includes
  $2,000, with `includes_pending_rows`.
- The same call on 7-03 → June outflow is $2,000 lower, `expired_holds: 0`,
  `settled_from_hold: 0`, no pending warning. **Nothing in the payload explains the change.**
  A quarter-boundary version of this moves money between Q2 and Q3 — which is precisely the
  period-over-period question §0 names as the product's success condition.

**Fix.** Keep the pending row's original `posted_date` as evidence (a nullable
`hold_date`/`authorized_date` already exists and is unused — see LOW 14), and add a third
`totals` figure: rows in this window that settled *out* of it (matchable as
`source_pending_transaction_id IS NOT NULL AND <prior posted_date in window> AND <current
posted_date outside>`), or at minimum stop `HoldTransitions` claiming its two figures are
exhaustive. **Test:** extend
`test_a_settlement_that_changes_the_amount_updates_it_in_place` with a settlement whose
`date` also moves across a month boundary, and assert the June answer either still accounts
for it or names the movement.

---

## HIGH 3 — A `modified` entry naming a pending id after that hold has posted inserts a SECOND live row: the purchase is counted twice

**Where:** `src/bankmachine/connector/plaid/derivers.py:587-616` (`_existing_transaction`).

**Evidence — measured.** Continuing the probe above with a third page carrying the pending
transaction in `modified`:

```
after modified pend-1  [(1, 'post-1', pending=0, -214500, 2026-07-02, None),
                        (2, 'pend-1', pending=1, -200000, 2026-06-30, None)]
```

Two live, non-removed rows for one $2,145 purchase. Cause: once the posting is applied, the
row answers to the *posted* id — its `source_transaction_id` has been overwritten
(`derivers.py:574-584`) — so `_existing_transaction` looking up `pend-1` finds nothing, the
`pending_source_id` branch is not taken (a `modified` pending entry carries no
`pending_transaction_id`), and it inserts. The unique index
`transactions_source_identity (account_id, source_transaction_id)` does not stop it, because
the two ids differ. `source_pending_transaction_id` — the column that *does* still hold
`pend-1` — is never consulted in the lookup.

**Numeric failure scenario.** As measured: a $2,145 hotel is reported as $4,145 of outflow
in a `money_summary`, with `includes_pending_rows` present (so it looks like a properly
disclosed hold, not a duplicate). AC-11.3 ("zero duplicate transactions") is not held over
this ordering; `test_a_hold_and_its_posting_resolve_to_one_row_whichever_page_lands_first`
covers `added`+`removed` orderings only.

**Fix.** In `_existing_transaction`, after the two existing lookups, fall back to
`source_pending_transaction_id == source_transaction_id` on the same account — the row that
already absorbed this hold. **Test:** the exact three-page sequence above, asserting one live
row (this repo's `verify_norms_go_red` discipline applies: the assertion fails today).

---

## HIGH 4 — A credit-card balance in credit is stored as debt: wrong by twice the balance, and indistinguishable from money owed

**Where:** `src/bankmachine/connector/plaid/derivers.py:949-951`.

```python
current = to_minor(balances.get("current"), currency, "a current balance", response)
if balance_class == "liability" and current > 0:
    current = negate(current)
```

**Evidence — measured**, deriving two credit accounts through the shipped deriver:

```
('Card owes bank',            'liability', current_minor=-25000, available=75000)
('Card in credit (refund)',   'liability', current_minor=-25000, available=125000)
```

Plaid documents that for credit-type accounts a **positive** `current` is the amount owed and
a **negative** `current` means the lender owes the account holder (a credit balance — the
ordinary result of a refund on a paid-off card, or an overpayment). The conditional negation
maps both `+250.00` and `-250.00` to `-25000`, so the two opposite states are the same row.

The behaviour is **pinned by a test**:
`tests/connector/test_derivers.py:601-611`
`test_a_liability_already_reported_negative_is_not_flipped_twice`, justified as
"aggregators disagree with each other, so normalization has to be idempotent." That defence
is about a hypothetical second aggregator; applied to the one aggregator this build has, and
whose semantics are documented, it inverts a real state. Per `CLAUDE.md` ("tests are
contracts — fix the code, never weaken a test") this needs an explicit ruling, not a quiet
edit.

**Numeric failure scenario.** Card with a $250 credit balance after a return. `list_accounts`
reports `current_minor_units: -25000`; net worth is understated by **$500**. Nothing on the
row distinguishes it from a $250 debt (`available_minor` exceeding `limit_minor` is the only
residual hint and no code reads it).

**Fix.** Make the rule "a liability's stored sign is the negation of the aggregator's" —
`current = negate(current)` unconditionally for `balance_class == "liability"` — and move the
idempotence concern to where it belongs: a per-connector normalization declaration, not a
sign-sniffing conditional. **Test:** a credit account with `current: -250.00` asserting
`current_minor == +25000`, plus the existing `+250.00 → -25000` case, so the pair pins the
mapping rather than the magnitude.

---

## HIGH 5 — A retired connection's balances freeze and read as `active`, with no staleness warning anywhere

**Where:** `src/bankmachine/query.py:82-101` (`_pipeline_warnings` filters
`connections.c.retired_at.is_(None)`), `src/bankmachine/cli/connections.py:353-358`
(`_mark_retired` touches only `connections`), `src/bankmachine/query.py:1223-1310`
(`list_accounts` does not filter on the connection).

**Evidence.** `_pipeline_warnings` reads only live connections, so a retired connection can
produce neither `stale`, `degraded`, nor `partial`. `_mark_retired` writes
`status='retired', retired_at=now` and nothing else: its accounts keep
`lifecycle_status='active'`, and since no further roster is ever observed,
`accounts.last_seen_date == connections.roster_observed_date` still holds, so
`_account_lifecycle` (`query.py:565-580`) evaluates them `active`. Their balances and
transactions continue to be returned by `list_accounts`, `money_summary` and
`query_transactions` with nothing marking them.

**Numeric failure scenario.** The operator hits the connection cap; `explain_cap`
(`connections.py:339-352`) tells them to run `bankmachine connections retire <id>` — the
product's own instruction. Six months later they ask for net worth. The retired institution's
$60,000 savings balance is still summed as a current balance, `lifecycle: "active"`,
`warnings: []`. This is AC-4.4's named primary failure mode ("a connection that quietly
stopped three weeks ago produces confidently wrong analysis") arriving through the retire path.

**Fix.** Either mark the accounts (`lifecycle_status='inactive'` at retire time — AC-12.6
makes the stored value authoritative) or emit a `degraded`/`partial` caveat for retired
connections that still have accounts carrying balances or rows. **Test:** retire a connection
whose account has a balance, then assert `list_accounts` neither reports it `active` nor
returns an empty `warnings`.

---

## HIGH 6 — Window/coverage reconciliation is store-wide; an account with shorter history is silently absent from every aggregate

**Where:** `src/bankmachine/query.py:183-241` (`_coverage`: `min/max(posted_date)` over the
whole store), `src/bankmachine/envelope.py:312-398` (`resolve_window` intersects against that
one span), `src/bankmachine/query.py:140-168` (`gapped` computed per *connection*),
`src/bankmachine/cli/sync_run.py:355-370` (`granted = today − oldest transaction on the connection`).

**Evidence.** `earliest_transaction` is a single `func.min(transactions.c.posted_date)` with
no account or connection scoping, and `window_starts_before_coverage` fires only when the
request starts before *that* date. Per-account coverage exists
(`AccountCoverage`, `query.py:294-393`) but rides only `list_accounts` rows and
`get_coverage_report` — never the aggregate. AC-9.5 says coverage is reported per account,
never per institution; the *window* reconciliation is neither.

**Numeric failure scenario.** Checking linked 2024-09 (24 months of history), a credit card
linked 2026-08 (3 weeks of history). `money_summary(since="2025-01-01", until="2025-12-31")`
returns $0 of card spending. `effective_window.effective` reads
`2025-01-01 → 2025-12-31` (fully inside store-wide coverage), no
`window_starts_before_coverage`, no `gapped` unless that connection's own
`granted < requested`. The agent reports "you spent nothing on that card in 2025" — a
plausible, false answer, which is exactly the #19 repro this repo already fixed for
`query_transactions(account_id=...)` and did not fix for the aggregate.

Two secondary defects in the same measurement: `granted_history_days` is measured as
`today − oldest transaction anywhere on the connection`, so (a) a connection whose accounts
are all young reports a false shortfall and a permanent `gapped`, and (b) one old account
masks every young account on the same connection.

**Fix.** Compute the effective window against the coverage of the accounts actually in scope
(all accounts, for an unscoped aggregate: take `max(per-account first_transaction_date)` as a
second bound and warn when the request reaches before it), or attach
`accounts_without_coverage`-style per-account bounds to the aggregate's envelope. **Test:**
two accounts with disjoint coverage; a window inside the older one's span and before the
newer one's, asserting the answer carries a caveat naming the account whose data does not
reach back that far.

---

## HIGH 7 — `money_summary` carries no lifecycle or coverage caveat, so an account that stops being reported mid-window silently drops out of every period total

**Where:** `src/bankmachine/query.py:2412-2426` (`money_summary`'s `extra_caveats` is
`_pending_caveat(...) + signs.caveats(...)` and nothing else; `lifecycle` is not passed to
`_answer`).

**Evidence.** `list_accounts` (`query.py:1301-1309`), `list_transactions`
(`query.py:1795-1801`) and `coverage_report` (`query.py:2041-2048`) all emit
`_uncovered_caveat` / `_not_active_caveat` / `_roster_observed_empty_caveat`.
`money_summary` — the one tool whose output an agent is told to quote as a figure — emits
neither. The envelope still carries `coverage.accounts_not_active`, but no warning kind, and
the MCP instructions teach the agent to branch on `warnings`.

AC-12.8 says a response carrying a count or sum over accounts "also carries
`account_no_longer_active`"; `money_summary`'s envelope carries `coverage.accounts` and
`coverage.accounts_not_active`.

**Numeric failure scenario.** The operator de-selects a card from sharing (or the institution
stops listing it) on 2026-05-01. `money_summary(group_by="month")` for Jan–Jun then shows
$1,800/month of card spending through April and ~$0 in May and June, with
`warnings: [ ]` apart from standing pipeline kinds. The agent reports "your spending dropped
by 40% in May." The store knows why — that account's `lifecycle` is `no_longer_reported` —
and does not say so on the answer that needs it.

**Fix.** Pass the already-computed lifecycle into `_answer` and emit `_not_active_caveat` /
`_uncovered_caveat` for the accounts that contributed rows to the window (or that would have,
by their coverage bounds). **Test:** an account marked non-active plus a windowed
`money_summary` spanning the date it went quiet, asserting `account_no_longer_active` is in
`warnings`.

---

## HIGH 8 — A remove-and-re-link duplicates the whole history, and `persistent_account_id` is stored but never used to prevent it

**Where:** `src/bankmachine/connector/plaid/derivers.py:824-850` (`_upsert_account` matches on
`(connection_id, source_account_id)`), `derivers.py:856`
(`source_persistent_account_id` is written and never read),
`src/bankmachine/cli/enroll.py:611-700` (the *connection* converges on the institution — good
— but the accounts do not converge on anything stable).

**Evidence.** `grep -rn persistent_account_id src/` returns three hits: the schema column, the
DDL, and the deriver writing it. Nothing reads it. Plaid re-issues fresh `account_id`s and
fresh `transaction_id`s when an Item is removed and re-linked (update-mode re-auth keeps
them; a full re-link does not). `_upsert_account` finds no existing row for the new
`source_account_id` and inserts a second account under the same `connection_id`; the fresh
cursor then re-fetches the entire granted history against it. The unique index
`transactions_source_identity (account_id, source_transaction_id)` does not collide because
both halves differ.

This is not an exotic path: AC-1.2 states the history window "cannot be raised after
enrollment without removing and re-linking the connection", so the documented remedy for the
single highest-stakes parameter in the system is the operation that duplicates the ledger.

**Numeric failure scenario.** Operator enrolls with 180 days, realises the config was wrong,
removes and re-links at 730 days. `money_summary` now reports every transaction in the
overlapping 180 days **twice** — annual spending reported as, say, $104,000 instead of
$52,000. The only signal is `coverage.accounts_not_active: 14` in the envelope's coverage
block, and (per HIGH 7) `money_summary` emits no warning about it at all. AC-6.3 ("stable
local ID that survives re-enrollment") is unmet in the case it was written for.

**Fix.** Match accounts on `persistent_account_id` first, falling back to
`source_account_id`, so a re-link converges onto the existing local `account_id`; where the
aggregator supplies no persistent id, refuse to silently create a parallel account on a
connection that already has accounts and surface it as a `get_pipeline_health` finding.
**Test:** two `/accounts/get` responses on one connection with different `account_id`s and the
same `persistent_account_id`, asserting one `accounts` row and one local `account_id`.

---

## HIGH 9 — `get_coverage_report` never enumerates interior gaps; only trailing silence is measured

**Where:** `src/bankmachine/query.py:1863-2050`. The row carries `first_transaction_date`,
`last_transaction_date`, `median_interval_days`, `days_silent`, `silence_ratio`,
`silence_exceeds_cadence` — every one of them derived from the **last** transaction or from
the median. There is no per-interval scan.

**Evidence.** The docstring says so plainly: *"Trailing silence, not an enumeration of gaps
between transactions."* But AC-11.1 asks for "all gaps **against the account's own cadence**
enumerated and explained"; the 2026-09-08 amendment changed the *threshold* (from a fixed 7
days to the account's cadence), not the enumeration. §7 calls this gate "the deliverable".

**Numeric failure scenario.** An institution stops delivering one account's rows from
2026-02-01 to 2026-04-30 and then resumes (a re-auth that succeeded without backfilling, a
Plaid outage window, an account de-selected and re-selected). Coverage row:
`first_transaction_date: 2024-09-16`, `last_transaction_date: 2026-09-08`,
`days_silent: 1`, `silence_exceeds_cadence: false`, `transaction_count` merely smaller than
it should be. The verification gate reports the account clean; `money_summary(group_by=month)`
reports February–April spending near zero and the agent reports a three-month drop in
spending that never happened.

**Fix.** Add an interior-gap pass: for each account, the intervals between consecutive
`posted_date`s exceeding `k × median_interval` (k declared with its derivation, as
`_STRANDED_AFTER` is), reported as `{from, to, days, ratio}` rows on that account, suppressed
for non-active accounts per AC-12.7. **Test:** an account with a monthly cadence and one
90-day hole in the middle, asserting the hole is enumerated and that a genuinely monthly
account with no hole reports none.

---

## MEDIUM 10 — AC-11.2 reconciliation (balance vs sum of transactions) is not implemented anywhere

**Where:** absent. `grep -rn -i reconcil src/` returns only unrelated prose in
`query.py:927`, `mcp.py:1381`.

**Evidence.** §7 AC-11.2: "For each account, the balance derived by summing transactions
matches the reported current balance within a documented tolerance. Discrepancies are
itemized." No CLI command, no query function, no MCP tool computes this.

**Why it matters here.** This is the one check that is *numeric* rather than structural, and
it independently catches BLOCKER 1's misclassification (no), HIGH 3's duplicate (yes, by
exactly the duplicated amount), HIGH 4's inverted credit balance (yes, by twice the balance),
and HIGH 8's re-link duplication (yes, by the whole overlap). Shipping to production accounts
without it means every one of the above fails silently in exactly the way §7 exists to prevent.

**Fix.** A `bankmachine store reconcile` command (and/or a `get_pipeline_health` field): for
each account with ≥2 balance snapshots, assert
`current_minor(d2) - current_minor(d1) == sum(amount_minor) over (d1, d2]`, itemising each
account that fails and by how much. **Test:** an account with two balance snapshots and
transactions that do not bridge them, asserting the discrepancy is reported with its magnitude.

---

## MEDIUM 11 — A rebuild at a changed derivation version silently discards every `category_override`

**Where:** `src/bankmachine/store/rebuild.py:255-260` (deletes every row of every
rebuildable table where `raw_response_id IS NOT NULL`), `store/schema.py:209`
(`category_override` lives on `transactions`, a rebuildable table),
`connector/plaid/derivers.py:538-559` (the replay never writes `category_override`),
`rebuild.py:104-113` (`change_was_expected`).

**Evidence.** `transactions` carries both `derivation_version_id` and a FK to
`raw_responses`, so it is classified rebuildable and emptied. The digest guard normally
catches the loss — but:

```python
@property
def change_was_expected(self) -> bool:
    return set(self.previous_derivation_versions) != {self.derivation_version}
```

When the derivation version has changed — which is the *usual* reason to run a rebuild — the
content change is "expected", the guard does not fire, and the rebuild commits with every
operator override gone.

**Numeric failure scenario.** The operator re-categorises 40 rows out of `TRAVEL` into a
personal category. A later build bumps `DERIVATION_VERSION`; `store rebuild` reports success.
`money_summary(group_by="category")` now attributes those rows to `TRAVEL` again — a changed
answer with no error and no record. (AC-6.1 requires the override column precisely so a source
value is never overwritten; nothing protects the override itself.)

**Fix.** Preserve operator-owned columns across a rebuild the way `_OPERATOR_OWNED` protects
them on `accounts`: capture `(account_id, source_transaction_id) → category_override` before
the delete and re-apply after the replay, or move overrides to their own non-rebuildable
table keyed on source identity. **Test:** set an override, bump the derivation version,
rebuild, assert the override survives.

---

## MEDIUM 12 — A transaction in an unofficial currency aborts the whole page, while a balance in one is accepted

**Where:** `connector/plaid/derivers.py:534`
(`currency = _required(entry.get("iso_currency_code"), "a transaction currency", response)`)
versus `derivers.py:794-796` (balances fall back to `unofficial_currency_code`).

**Evidence.** Plaid sets `iso_currency_code: null` and populates
`unofficial_currency_code` for cryptocurrencies and some non-ISO instruments. A transaction
row of that shape raises `DerivationError`, which fails the whole `/transactions/sync` page —
so the connection's cursor never advances and the connection degrades. Loud, not silent,
but it takes the whole connection down and the inconsistency with the balance path means an
account can exist while none of its transactions can be derived.

**Fix.** Use the same `iso_currency_code or unofficial_currency_code` fallback for
transactions. **Test:** a transaction body with only `unofficial_currency_code`, asserting it
derives and that its currency groups separately in `money_summary`.

---

## MEDIUM 13 — An unknown or unofficial currency silently gets 2 minor digits

**Where:** `src/bankmachine/store/types.py:62-84` (`_MINOR_DIGITS`, `DEFAULT_MINOR_DIGITS = 2`),
`connector/plaid/derivers.py:148-206` (`to_minor` rounds half-even to that exponent).

**Evidence.** The table correctly enumerates the 0-, 3- and 4-decimal ISO currencies (JPY,
KWD, CLF, …) and defaults the rest to 2, with a good rationale for that direction. But the
default also catches **unofficial** codes, which balances accept. `to_minor` then rounds to
two places and logs at `info`.

**Numeric failure scenario.** A crypto sub-account with `unofficial_currency_code: "BTC"` and
`current: 0.04217` is stored as `4` minor units, i.e. `0.04` — the account's value is
destroyed to a rounding artefact, disclosed only in a log line that no MCP consumer sees.

**Fix.** Refuse an unknown currency for a *valuation* rather than rounding it (mirroring
`from_decimal_string`'s exact-or-refuse rule for ledger amounts), or carry an explicit
per-currency exponent for unofficial codes. **Test:** a balance in an unofficial currency with
more than two decimals, asserting refusal or exact storage rather than a silent round.

---

## MEDIUM 14 — "Ask this for income (read `inflow`)" points at a figure with no protection, and `totals` decomposes only outflow

**Where:** `src/bankmachine/mcp.py:797-799` (tool description),
`src/bankmachine/query.py:2148-2215` (`_flow_class_totals` builds
`{flow}_outflow_minor_units` only).

**Evidence.** The spending path has a headline, protected figure the agent is told to quote
(`external_spend_outflow_minor_units`). The income path has none: `totals` has no inflow keys
at all, so an agent asked "what was my income" must sum `inflow_minor_units` across rows
itself — and per BLOCKER 1 the paycheque is sitting in an `internal_transfer` row while
refunds sit in `external_spend`.

**Numeric failure scenario.** Measured on the sandbox store: `TRAVEL`/`external_spend`
carries `inflow_minor_units: 1,250,000` (24 refunds) and `TRANSFER_IN`/`internal_transfer`
carries the payroll. An agent reading `inflow` on the external rows reports **$12,500 of
income** for a period whose real income was the payroll it just discarded.

**Fix.** Either add `{flow}_inflow_minor_units` to `totals` (symmetry), or — better, and
dependent on BLOCKER 1 — introduce an `income` flow class read from
`source_category_primary = 'INCOME'` plus the `TRANSFER_IN_DEPOSIT`/payroll detailed values,
and change the description so it stops implying the data can answer "income" when what it
holds is inflows. **Test:** the `KNOWN_SOURCE_CATEGORIES` guard already exists in
`tests/test_money_summary.py`; extend it to assert that whatever class carries an inflow the
description calls "income" is reachable and does not contain refunds.

---

## LOW 15 — `authorized_date` has zero readers, and no surface states which date a window uses

**Where:** `connector/plaid/derivers.py:544-548` writes it; `store/schema.py:202` holds it;
nothing reads it (`grep -rn authorized_date src/`). `mcp.py:735` labels the wire field simply
`"date"`, and `data-model.md:359` documents the column with an empty description.

Every window (`_transaction_filters`, `_covered_rows`, `_stranded_holds`, `_hold_transitions`,
`coverage_report`) filters on `posted_date` = Plaid's `date`. That is a defensible convention,
but a consumer cannot tell whether a purchase made on 30 June and posted on 2 July belongs to
June or July, and the answer changes when the row settles (HIGH 2).

**Fix.** State the convention in the `query_transactions` and `money_summary` descriptions
("windows are measured on the POSTING date"), and put `authorized_date` on the wire so the
other reading is available. **Test:** the documented-surface guard
(`tests/preferences/test_the_documented_tool_surface_is_the_built_one.py`) is name-only today;
a row-field-shape guard would catch both this and the `lifecycle` gap §5 already records.

## LOW 16 — "today" is UTC, so calendar-day answers can be a day ahead of the operator's

**Where:** `envelope.py:311` (`today = calendar_date(as_of.date())`), `query.py:1918`
(`coverage_report`), `query.py:1427` (`_stranded_cutoff`),
`connector/plaid/derivers.py:223-225` (`_as_of` stamps `balances_daily.as_of_date` from
`received_at`).

For a US-Pacific operator, everything after 17:00 local is stamped the next calendar day, and
`effective_window.effective.until` can name a date the operator has not reached. `days_silent`
and `silence_ratio` inherit the same one-day skew, and a balance captured at 18:00 local and
another at 08:00 the next morning land on two different `as_of_date`s. Nothing here is wrong
by more than a day, and the UTC choice is documented — but it is not stated on the wire.

**Fix.** Name the zone in the `as_of`/window descriptions, or make the calendar-day boundary a
configured local zone. **Test:** freeze `as_of` at 2026-06-01T04:00Z and assert the reported
`effective.until` is described in the terms the operator asked in.

---

# Verified SOUND

- **Float→int conversion.** `store/types.py:109-147` scales on the Decimal digit tuple, not by
  arithmetic, so no context precision applies and no `0.1+0.2` error is reachable.
  `_payload` parses with `parse_float=str` (`derivers.py:118`), so a float never exists.
  Ledger amounts are exact-or-refuse; valuations round half-even and log
  (`derivers.py:148-206`). The measured 401k case (`23631.9805 → 2363198`) is correct.
- **Currency exponents.** The 0-, 3- and 4-decimal ISO currencies are enumerated
  (`types.py:62-71`) with the safe default direction argued correctly. JPY and KWD are handled.
- **No cross-currency summation.** Every aggregate groups on `transactions.c.currency` —
  `money_summary` rows, `_flow_class_totals`, `_hold_transitions`,
  `_not_active_balances`. Verified there is no path that sums across currencies.
- **Transaction sign for depository and credit.** `_operator_signed_amount` negates
  unconditionally, which is correct for Plaid's `amount` on both depository and credit
  accounts (purchases positive = money out). The convention is applied in exactly one place
  (`derivers.py:375-419`) for transactions and one (`derivers.py:949-951`) for balances.
- **Liability balance sign for the ordinary case.** A credit account reporting `current:
  +250.00` is stored `-25000`, so `list_accounts` presents a card balance as money owed
  (measured). Only the credit-balance case (HIGH 4) is wrong.
- **Sign-convention population check.** `signs.py` is a careful piece of work: per-connection,
  whole-history, proportion-based, sample floor derived rather than picked, `undetermined`
  kept distinct from `consistent`, category set read from the source column only, reports and
  never corrects (AC-14.4), and the caveat is scoped to connections that actually contributed
  to the window.
- **Soft deletes.** `removed_at IS NULL` is applied in every reader —
  `_transaction_filters`, `_account_coverage`, `_coverage`, `signs.measure`,
  `_stranded_holds`, `coverage_report`'s cadence and source walks. An expired hold is
  correctly excluded from sums (the money never left) and its exit is tallied as
  `expired_holds`.
- **Pending→posted on the ordinary paths.** Merge-on-`pending_transaction_id`, local
  `transaction_id` preserved, amount replaced in place (AC-13.2), `removed_at` cleared on
  re-send, both page orders resolving to one row (AC-13.3), and a `removed`-in-the-same-page
  case. All covered by tests and reproduced.
- **Uniqueness.** `transactions_source_identity` is a real partial unique index, so an ordinary
  re-sync cannot duplicate a row.
- **Rebuild determinism.** Replay order is total (`raw.py:229-231`), derivers take every
  timestamp from `response.received_at` with no clock, `first_seen`/`last_seen` are a min/max
  pair so replay order does not matter, the content digest excludes only rowid surrogates and
  rejects floats outright, and a content change at an unchanged derivation version rolls the
  whole rebuild back. Dimension tables (`accounts`, `institutions`) are never emptied, so local
  ids survive (AC-6.3 within one Item).
- **`balances_daily`.** Point-in-time, stamped with `as_of_date` and `captured_at`,
  append-only with a first-wins rule decided by comparing captures rather than arrival order,
  and it refuses to overwrite an import-path row.
- **Per-account coverage on rows.** `AccountCoverage`/`AccountLifecycle` are one producer with
  two readers, always on every row rather than behind a parameter, with the evidence dates
  beside the verdict — genuinely good design, and the outer-join-in-the-join-condition detail
  is right.
- **Lifecycle derivation.** Measured against the recorded `roster_observed_date` rather than a
  derived maximum; a null observation marks nothing absent; the empty-roster case yields both
  the account-level truth and the connection-level anomaly. The reasoning in
  `_account_lifecycle`'s comments about why a null `last_seen_date` must not be backfilled
  from `first_seen_date` is correct and worth keeping.
- **Window clamping.** `resolve_window` handles both bounds on both sides, refuses an inverted
  window, takes `covered_end = max(today, latest)` so a forward-dated row cannot fall outside
  the window the answer claims, and never lets a null bound reach a predicate.
- **Truncation and paging.** The keyset predicate is in the shared filter list, so `matching`
  and the rows agree and `truncated` terminates; the cursor is built from the statement's own
  column values, is signed, and is refused when it does not match the question.
- **Types.** `CalendarDate`/`UtcInstant`/`MinorUnits` are genuinely distinct at runtime, not
  just to mypy; `calendar_date` refuses the `datetime` that subclasses `date`; the MCP
  boundary refuses a datetime string (verified on Python 3.14).
- **Warning vocabulary.** Closed, AST-checked, split into connection-scoped and
  request-scoped with the "absence is information" property argued and enforced.
