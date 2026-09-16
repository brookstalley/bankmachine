# Discovery — Checking the invariant the schema already claims

**Work cycle:** reconciliation and the data-sanity surface · medium · requirement
**Opened:** 2026-09-16
**Closes (proposed):** brookstalley/bankmachine #70, #96
**Branch:** `feature/reconciliation-and-status`

---

> These two items are one work cycle because they are one question asked at two altitudes.
> #70 computes the one *numeric* check the verification gate is missing; #96 is the surface an
> operator reads it from. Built apart, #70 lands as a figure nothing displays and #96 as a display
> with the figure missing from it.
>
> Three decisions in this document were **taken by the owner on 2026-09-16** and are not open:
> the residual's home, the tolerance, and where the expected inventory lives. They are marked
> **RULED** where they appear. Everything else marked *proposed* is vetoable.

---

## The finding that reframes both items

🔴 **AC-11.2's formula is already load-bearing in this codebase. Nothing has ever checked it.**

Two docstrings — one on the frozen core-schema migration, one on the balance deriver — cite the
same sentence as the *justification* for the product's single most depended-upon data decision:

> *"One convention rather than one per account type is what keeps two later things from needing a
> special case: net worth is a plain sum, and AC-11.2's reconciliation is 'change in balance
> equals sum of transactions' for every account."*
> — `src/bankmachine/store/migrations/core_schema.py:40`, echoed at
> `src/bankmachine/connector/plaid/derivers.py:1631`

So the one-sign convention, and the rule that `balance_class` partitions *reporting* and never
arithmetic, were both chosen **because** this reconciliation would hold. The reconciliation was
then never built. The product has been designing against an unverified premise since the core
schema was frozen.

That changes what #70 is. It is not a new check bolted onto the side of the gate — it is the
**first measurement of an invariant the store already asserts about itself**, and a systematically
nonzero residual would falsify a premise two design decisions rest on. That is worth knowing
before the release, which is the argument for doing this now rather than after.

It also sets the bar for what "done" means: a residual that comes back zero across the production
store is the evidence those two docstrings have never had.

---

## Premise verification — what the issues claimed, and what the tree says

### #70, claim 1 — "No command, query or tool computes this." **TRUE.**

`grep -rn -i reconcil src/` returns 30 hits and every one is prose or an unrelated mechanism
(window-vs-coverage reconciliation in `envelope.py`, the connector's *removal* reconciliation in
`plaid/window.py`). Nothing computes change-in-balance against sum-of-transactions. Confirmed
independently of the review that filed it.

### #70, claim 2 — the finding it says this catches. **TRUE, and understated.**

`review-finance.md` MEDIUM 10 says the residual independently catches HIGH 3's duplicate (by
exactly the duplicated amount), HIGH 4's inverted credit balance (by twice the balance), and
HIGH 8's re-link duplication (by the whole overlap). The magnitudes are right. What the review
does not say is the item above — that the same formula is the *stated reason* the sign convention
is uniform, so the check also guards its own premise.

### #70, claim 3 — "for every account." **FALSE for investment accounts, and already measured.**

`api-notes-plaid.md` §23 records a direct measurement against the aggregator's own canned data:

| Account | `balances.current` | Σ `institution_value` | Difference |
|---|---|---|---|
| Plaid IRA | 320.76 | 320.76 | 0 |
| Plaid 401k | 23631.9805 | 25125.63318 | **−1493.65268** |

and draws the conclusion explicitly: *"A reconciliation asserting Σ holdings = balance would go
red on the aggregator's own canned data, so it is not a test this product can write."* No
`margin_loan_amount` explains it — the IRA is the account carrying one, and the 401k's is null.

Investment balances also move with the market rather than with recorded activity, so neither
available formula reconciles them: not against holdings (§23), and not against transactions
(a position that appreciates produces no row). **Investment accounts are therefore out of scope
for AC-11.2** — and the two docstrings quoted above say "every account", which is an overclaim
this cycle must correct rather than inherit.

🔴 This matters right now, not theoretically: the second production institution is
investment-only. Scoped in, this feature's first act in production would be to report a permanent
false finding against a real account.

### #96, claim 1 — "Nothing answers *is my data sane?* in one read." **TRUE.**

`bankmachine store status` exists and is verified, but reports only the datastore *file*:
environment, path, presence, journal mode, schema version, writer lock, healthy, problem
(`src/bankmachine/cli/store.py:334`). It says nothing about the data inside. `connections list`
reports connections and slot usage. Neither answers the question, and together they do not either.

### #96, claim 2 — "§§ 3.7, 4.2, 4.3 and 4.5 collapse into invoking it." **Three of four.**

- **3.7** (`connections list` + `store status`) — collapses. ✅
- **4.3** (every account is USD) — collapses; currency mix is a plain read. ✅
- **4.5** (run `sync run` twice, look for duplicates) — **the check collapses, the action does
  not.** The runbook's failure signature is *"a total that moved while nothing happened"*, which
  is exactly a nonzero residual from #70. The operator still has to run the second sync; what
  they no longer have to do is eyeball `sync shell` for a doubled row. Record it as partial, not
  as collapsed.
- **4.2** (accounts you know you have vs. `list_accounts`) — **cannot collapse as filed.** The
  runbook says so in terms: *"An account that was never received is invisible. There is no field
  that can report it and no query can detect it."* It is undetectable from inside the store by
  construction. The only path is AC-11.6's operator-supplied expected inventory, and **no
  machine-readable inventory exists** — `deployment/` holds `data-sources.md`,
  `deployment-requirements.md`, `history-audit.md` and three token files, none of them an
  inventory a command could read.

### #96, claim 3 — the inventory is unspecified. **FALSE — it is specified, just not built.**

`deployment/deployment-requirements.md` DAC-5.2 requires a written post-enrollment inventory
(institution, type, mask, current balance, first and last transaction date) and **DAC-5.3** says
in terms: *"This inventory is the operator-supplied expected inventory that system AC-11.6
reconciles against. Recollection stays out of the source-of-truth path."*

So the requirement exists and has a home. What is missing is a **machine-readable form** of it.
That is the gap this cycle closes, and it is smaller than #96 implies.

---

## Decisions taken

### RULED — the residual lives on `get_coverage_report`, and `status` renders it

Owner ruling, 2026-09-16. The residual becomes a field on `get_coverage_report`, computed by a
producer shared with the CLI; `bankmachine status` renders the same figures for the operator.

**Why this over a CLI-only `store reconcile`:** the product's own thesis, recorded as an
`api-contract.md` § Direction norm, is *"a consumer cannot see a caveat that is not in the
payload."* An operator-only reconciliation leaves the analyst agent answering questions
confidently over data whose balance does not reconcile — which is verbatim the failure §7 says
the gate exists to prevent (*"the failure mode being guarded against is confident analysis of
quietly incomplete data"*). `coverage_report`'s own docstring already frames it as *"the
verification surface's second half"* and as existing *"so an analyst agent can establish
completeness BEFORE answering."* The residual belongs there.

🔴 **One producer, two readers — non-negotiable, and for a reason this file already states twice.**
`_account_coverage`'s docstring: *"Computed twice they can disagree, and a verification surface
that contradicts the analysis surface is worse than one that is missing."* The CLI must call the
same function the tool does, not a second implementation over the same tables.

### RULED — the tolerance is zero, and every residual is itemized with an attributed cause

Owner ruling, 2026-09-16. AC-11.2's *"within a documented tolerance"* reads as **exact equality in
integer minor units**, with each nonzero residual reported alongside what explains it.

**Why:** amounts are integer minor units by norm, so exact equality is meaningful arithmetic
rather than a floating-point aspiration. And AC-11.2's own next sentence — *"Discrepancies are
itemized, not averaged away"* — is in tension with a numeric band: a threshold that forgives
residuals under it is the averaging-away the clause forbids, wearing a different name. A residual
of 300 that is an open hold and a residual of 300 that is a duplicated transaction are not
distinguished by their magnitude, so a magnitude threshold cannot be the discriminator. The cause
is.

**The attributable causes, proposed** (each must be *computed*, never assumed):

| Cause | How it is established | Residual is |
|---|---|---|
| `coverage_gap` | a known hole between the two snapshots (`gapped`'s own producer) | explained |
| `pending_holds` | open holds in the interval; the residual equals their sum | explained |
| `window_truncated` | the interval reaches back past `history_starts` | explained |
| `unexplained` | none of the above accounts for it | 🔴 **a finding** |

An `unexplained` residual is the output this feature exists to produce. The other three exist so
that it means something.

### RULED — the expected inventory is a gitignored file under `deployment/`

Owner ruling, 2026-09-16. Matches what the repo already does: the roster is configuration,
configuration is gitignored (`.gitignore:41`, `deployment/`), and `deployment/` is where DAC-5.2's
inventory was already specified to live.

🔴 **A missing inventory file reports "not supplied", never "passed."** An absent oracle that
reads as a green check is worse than no check — it is the absent-bad-news shape this project's
`learnings.md` already records against itself.

### Decided by me — one branch, two chunks, #70 first

#96 renders #70's output, so #70 first. Both land before the release, per the owner's sequencing.
Stated here rather than asked because it changes no requirement.

---

## Requirements — proposed

Proposed for `docs/system-requirements.md`. AC-11.2 already exists and is **amended**, not
replaced; the rest are new and belong with FR-9/FR-10 in § 4 or beside § 7.

**AC-11.2 (amended)** — For each **non-investment** account, the change in `current_minor` between
consecutive balance snapshots equals the sum of `amount_minor` over transactions posted in the
interval `(d1, d2]`. Tolerance is **zero**. Every nonzero residual is reported per account per
interval with its magnitude and an attributed cause; a residual with no attributable cause is a
finding. Investment accounts are **excluded and named as excluded** — their balances move with the
market, and §23 of `api-notes-plaid.md` measured that neither holdings nor transactions reconcile
them.

**AC-11.2a** — **Four** sites assert the reconciliation holds "for every account" and are corrected
to state the exclusion. *A requirement, not a cleanup: they are the recorded justification for the
sign convention, and leaving them overclaiming preserves the exact confusion this cycle exists to
resolve.*

🔴 **They do not all have the same status, and the difference decides how each is changed** — norms
bind, descriptions track (`/prawduct:methodology norms`):

| Site | Status | How it changes |
|---|---|---|
| `data-model.md:57` — the operator-signed norm's **Why** | **normative** | a recorded **amendment**, with statement / why / retroactivity and a `[DECISION: …]` |
| `data-model.md:776` — § Sign convention prose | descriptive | tracks the norm; corrected to match |
| `src/bankmachine/store/migrations/core_schema.py:40` | descriptive | module docstring; **not** the frozen DDL |
| `src/bankmachine/connector/plaid/derivers.py:1631` | descriptive | `_balance_row` docstring |

Counted by `grep -rn "change in balance equals sum"` rather than from memory — the first pass of
this document said three and missed `data-model.md:776`, which is exactly the decay a durable
claim about a count suffers when it is written from recall.

**AC-11.6a** — The expected account inventory is operator-supplied, read from a gitignored file
under `deployment/`. Each entry identifies an account the operator expects the store to hold. The
surface reports three states, kept distinct: **matched**, **unrecognized** (in the store, not in
the inventory — a finding per AC-11.6), and **expected but never received** (in the inventory, not
in the store). 🔴 When no inventory file is present the surface reports **not supplied**, which is
neither a pass nor a failure.

**AC-18.1** — One command reports, per connection: connection status,
granted vs. requested history window, account count, transaction count, the date span they cover,
currency mix, accounts with zero activity, what the most recent sync changed, the reconciliation
residuals of AC-11.2, and the inventory states of AC-11.6a — with no SQL and no `sync shell`.

**AC-18.2** — A suspected account duplication is **stated in words**, not left inferable from a
count. Two active accounts on one connection sharing `(mask, name, type, subtype)`, or an account
count that rose with no corresponding retire. 🔴 Per #96's own acceptance, **the false-positive
bar is written down before it ships** — a joint account legitimately appearing on two connections
is the case that must not cry wolf.

**AC-18.3** — Every account is USD, or the command says which are not and that multi-currency
totals are undefined (runbook 4.3).

---

## Design — proposed

### The residual row

Per account, per consecutive-snapshot interval. Snapshots are **sparse by design** — the deriver
records no row for a day the aggregator reported a null `current`
(`plaid/derivers.py`, the `reported is None` path), so an interval may span many days and a
missing day is not itself a defect. The pairing walks `balances_daily` per account in `as_of_date`
order and pairs each row with its predecessor.

| Field | Meaning |
|---|---|
| `account_id` | the store's own id |
| `from_date` / `to_date` | the interval's bounds; transactions counted over `(from, to]` |
| `balance_change_minor_units` | `current_minor(to) − current_minor(from)`, operator-signed |
| `transactions_sum_minor_units` | Σ `amount_minor` over the interval, soft-deleted rows excluded |
| `residual_minor_units` | the difference; `0` is the expected value |
| `cause` | one of the four above; `unexplained` is the finding |
| `reconciliation_state` | `reconciled` \| `not_applicable_investment` \| `insufficient_snapshots` \| `no_balance_recorded` |

🔴 **`reconciliation_state` is a separate required field and is never inferred from a null
residual.** Three distinguishable states would otherwise share one null — an investment account, an
account with only one snapshot so far, and an account with no balance ever recorded — and a
consumer could not tell an *unreconcilable* account from an *unreconciled* one. `learnings.md`
§ *A carve-out reaches every state that shares its return type*: when one function collapses
distinguishable states into one value, the collapse is the defect.

### Two new warning kinds, for `api-contract.md` § *The warning vocabulary*

| `kind` | Meaning |
|---|---|
| `balance_unreconciled` | An account in the scope of THIS request has an interval whose balance movement is not explained by the transactions recorded in it, and no coverage gap, open hold or truncated window accounts for the difference. `detail` names the account, the interval and the magnitude. 🔴 The figures in this answer are internally consistent and may still be wrong by that amount |
| `reconciliation_not_applicable` | An account in scope is an investment account, whose balance moves with the market rather than with recorded activity, so AC-11.2 does not apply to it. Its absence from the residuals is by construction, not a gap. `detail` names the accounts |

`reconciliation_not_applicable` is a warning rather than a silent omission for the same reason
`activity_in_another_feed` is: a consumer that cannot see why an account is missing from a
verification surface reads the surface as having checked it.

### Why not a `rule-applied` warning for the investment exclusion

`rule-applied` already means *"rows were excluded from this aggregate ON PURPOSE."* It was
considered and rejected: it is about rows excluded from a *figure*, and reusing it here would put
"this account cannot be checked" and "these rows were left out of a total" under one kind, so a
consumer could not tell a scoping decision from an unverifiable account. Kept apart deliberately.

### `bankmachine status` — new verb, not an extension of `store status`

`store status` answers *"can this build serve this datastore?"* and exits `1` for unhealthy.
`status` answers *"is the data in it sane?"* Collapsing them would put a data finding behind an
exit code launchd reads as a datastore fault, and the `0`/`1`/`2` split is a contract norm that
`api-contract.md` § Direction records as **not collapsible**.

**Exit codes, proposed:** `0` every check passed or was not supplied · `1` a finding — an
unexplained residual, an unrecognized account, a suspected duplication, a non-USD account ·
`2` could not run. This reuses `EXIT_UNHEALTHY`'s established meaning (*"ran fine; the answer is
unhealthy"*) rather than inventing a code.

---

## Assumptions, vetoable

- `[ASSUMPTION: the reconciliation pairs CONSECUTIVE snapshots rather than reconciling against a
  caller-supplied period | MED impact | user can correct]` — consecutive pairing needs no
  parameter and localizes a discrepancy to the interval that produced it. A period-based variant
  would aggregate intervals and lose that. Reversible later; the row shape above already carries
  explicit bounds.
- `[ASSUMPTION: a pending row counts toward the transaction sum for the interval it is posted in |
  HIGH impact | user can correct]` — 🔴 the highest-impact assumption here and the one most likely
  to be wrong on day one. Whether the aggregator's `current` includes authorization holds is
  **not recorded in `api-notes-plaid.md`**, which documents the `balances` object's fields but not
  that semantic. If `current` excludes holds and this sum includes them, every account with an
  open hold shows a residual equal to the hold. The design mitigates rather than guesses: the
  `pending_holds` cause is computed, so the mismatch would surface as an *explained* residual
  whose magnitude equals the open holds — which is itself the measurement that settles the
  question. This is the same unverified semantic that **#22 / VRF-038** has been re-raised five
  times waiting on, and this feature is the instrument that would finally read it.
- `[ASSUMPTION: the inventory file is TOML, consistent with `~/.config/bankmachine/config.toml` |
  LOW impact | user can correct]` — format is cosmetic; the states it must express are the
  requirement.
- `[ASSUMPTION: `status` reports across all connections with no argument, and takes an optional
  connection filter | LOW impact | user can correct]`

---

## Requirements confidence

1. **What problem does this solve?** An operator and an analyst agent both have to decide whether
   the stored data is sane, and today neither can — the one numeric check that would tell them is
   unimplemented, and the eight checks that stand in for it are prose in a 492-line runbook.
2. **What does success look like?** `get_coverage_report` carries a per-account residual whose
   expected value is zero and whose exceptions are itemized with attributed causes; one CLI command
   reports per-connection sanity without SQL; both read the same producer. The production store
   returns zero unexplained residuals — or it does not, and the product learns that before the
   release rather than after.
3. **What is out of scope?** The **fix** for any duplication found (#95). Redefining
   `account_no_longer_active`. Reconciling investment accounts (measured impossible, §23).
   AC-11.1, 11.3–11.5 and 11.7–11.8 — this cycle implements 11.2 and 11.6 only, and does not
   claim the gate as a whole. Scheduling (build step 8) and file import (step 10).

**Rigor calibration** — stakes: high (financial data, live production store, and a check whose
output will be believed). Knowledge confidence: high on the data model and the envelope
conventions, which are unusually well documented here; **low on one foreign-API semantic** (does
`current` include holds), which is why that one is an assumption with a computed mitigation rather
than a design decision. Volatility: low — the schema is frozen, and the aggregator's behaviour is
recorded from measurement in `api-notes-plaid.md` rather than from recall.

**Foreign API:** Plaid `/accounts/get` `balances.current` — pending-inclusion semantics
unverified. Flagged for the Critic's `verify-api` check on the chunk that consumes it.

---

## Shared-artifact deltas for the build

1. `docs/system-requirements.md` — amend AC-11.2; add AC-11.2a, AC-11.6a, AC-18.1–18.3. Amend §7's
   AC-11.2 clause to state the investment exclusion, in the same voice as the AC-11.1 and AC-11.3
   amendment notes already there.
2. `.prawduct/artifacts/api-contract.md` — two warning kinds; the `get_coverage_report` row-shape
   table; `bankmachine status` in the CLI verb table; the tool/verb counts the contract test reads.
3. `.prawduct/artifacts/data-model.md` — state that the reconciliation excludes investment accounts
   where the sign convention's rationale is recorded.
4. `src/bankmachine/store/migrations/core_schema.py:40` and
   `src/bankmachine/connector/plaid/derivers.py:1631` — AC-11.2a's correction. 🔴 The migration's
   DDL is frozen; this edits a **module docstring**, not the DDL, so `CORE_SCHEMA_DDL_SHA256` is
   untouched. Confirm against `tests/store/test_schema.py` before editing.
5. `docs/first-production-connection.md` — §§ 3.7, 4.3 collapse into `bankmachine status`; 4.5
   partially (the check, not the second sync); 4.2 becomes "maintain the inventory file, then run
   `status`". § 4's preamble — *"Everything above can be verified by the machine. Nothing below
   can"* — is falsified by this cycle for three of its eight items and must be rewritten.
6. `deployment/deployment-requirements.md` — DAC-5.2/5.3 gain the file's location and format.
   🔴 Gitignored; the edit is local and never reaches a remote.
7. `.prawduct/artifacts/operational-spec.md` — the new verb.
8. Backlog #70, #96 — stage transitions and `refs:` to this document.
