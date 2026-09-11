---
artifact: build-plan
version: 2
scope: duplicated-roster-double-count
branch: fix/duplicated-roster-double-count
depends_on:
  - artifact: data-model
  - artifact: api-contract
  - artifact: discovery-relink-account-identity
governed_by:
  - artifact: data-model
    dispositions:
      - "a transaction is never hard-deleted; removal is a soft delete carrying a removal timestamp → conforms, and this plan deletes nothing at all: supersession is a READ-PATH exclusion. Every row stays stored and stays reachable, which is the property `only_superseded` exists to expose"
      - "a source value is never overwritten in place; local interpretation lives in its own column → conforms; no column is written by this plan. The grouping is derived at read time from columns the roster deriver already fills"
      - "every stored amount is signed from the operator's point of view → inapplicable because this plan reads amounts and changes none"
      - "all monetary values are stored as integer minor units → inapplicable because this plan introduces no new amount"
      - "calendar dates and UTC instants are distinct types and never mix → conforms, and the distinction decided a design choice here: the generation order is built from two INSTANTS (`enrolled_at`, `accounts.created_at`) precisely because the calendar date that reads more naturally, `last_seen_date`, cannot separate two generations born on the same day. The calendar date is still read, by `_still_reported`, where it is compared only against another calendar date"
      - "the daily balance and holdings series are append-only → inapplicable because this plan touches neither series"
      - "every silver row is either aggregator-sourced or manually imported and carries its evidence → conforms; an operator-imported row carries no lineage, and `lineage.py`'s standing rule that a null lineage is never excluded keeps this plan away from it"
      - "a migration's DDL is frozen once written → inapplicable because this plan adds no migration; every column it reads already exists"
  - artifact: api-contract
    dispositions:
      - "every response carries a freshness stamp, and incompleteness rides the success path as a warning → conforms, and this plan is an instance of it: the exclusion is disclosed through the existing `rule-applied` caveat rather than applied silently. The wording is corrected because the current text asserts 'more than one connection', which is false on the converging path this plan fixes"
      - "a stored balance is reported with its lifecycle, and no total over balances is emitted without stating its treatment of non-active accounts → NOT departed from, and deliberately NOT extended. This plan changes transaction aggregates only. The duplicated generation's BALANCES stay included-and-flagged exactly as the norm requires; see 'What this plan does not fix'"
      - "the MCP surface is read-only → conforms; no tool gains a mutation and no tool is added"
      - "a tool's boundary is drawn where the answer shape changes → inapplicable because this plan adds no tool and changes no answer shape; only figures and one caveat's text change"
      - "the CLI's three-way exit code is a contract → inapplicable because this plan changes no exit code"
partition: serial — one chunk; the disclosure text and the mechanism must land together or the payload asserts something untrue
last_validated: 2026-09-11
---

# Build Plan: A Duplicated Roster Stops Doubling Every Total

Closes brookstalley/bankmachine#91.

## Problem

**Measured on the live sandbox store, 2026-09-11.** `money_summary` for 2026-08
returns `outflow_minor_units: 2229892`. The true figure is `1114946` — **exactly 2×**
— and every account appears twice in the rows with identical figures.
`list_accounts` returns 28 accounts for a 14-account institution.

The doubling is not disclosed. The four caveats returned are `gapped`,
`accounts_without_coverage`, `partial` and `account_no_longer_active`; the last is
the only one in the neighbourhood and it speaks **solely about balances** ("Any total
over balances INCLUDES them on purpose"). Nothing in the payload names transactions
being counted twice, which is where the 2× lands.

## Root cause

`store/lineage.py` exists to prevent precisely this, and cannot fire.

A converging re-enroll updates the `connections` row in place, so `connection_id` —
and therefore `lineage_id`, which is set from it
(`connector/plaid/derivers.py:871`) — is **unchanged**. The new Item nonetheless
re-issues every `source_account_id`, and `source_persistent_account_id` is NULL at
every institution outside the three TAN banks, so `_match_account`
(`connector/plaid/derivers.py:1289`) matches nothing and **inserts a second
generation of account rows**. The re-fetched history lands on those rows.

`_coverage` (`store/lineage.py:127`) groups by `(account_id, lineage_id)` and
`superseded_spans` compares overlaps **within one `account_id`**. The twins have
*different* `account_id`s and the *same* `lineage_id`, so no overlap is ever
detected, `spans` comes back empty, and `query.py:1950` faithfully applies a
`counts_once` that constrains nothing.

`lineage.py`'s own module docstring records the assumption that fails: "The account
converges — `source_persistent_account_id` names the same underlying account across
the break." Production evidence says absence is the rule, not the exception
(`discovery-relink-account-identity.md`).

## The fix

Group the overlap comparison on **account identity** rather than on `account_id`.

`SupersededSpan`, `counts_once` and `only_superseded` keep their shape, so **every
reader is fixed by construction** — `query_transactions`, `money_summary` and the
coverage paths all route through the single `superseded_spans` chokepoint already.

### [DECISION] The identity key is `(institution_id, mask, name, account_type, account_subtype)`

**Scoped to the institution, never globally and never to the connection.** Global
would let a mask collision between two banks silently merge two real accounts —
the outcome `_match_account` already names as the worst available. Connection-scoped
would miss the remove-and-re-link path (#68), where the duplication lands under a
*new* `connection_id`; institution scope covers both duplication paths with one rule.

**A NULL `mask` never groups.** A null means the aggregator did not state it, not
that two accounts agree. Grouping on absence is how a rule that excludes money gets
it wrong in the believable direction.

Owner ruling, 2026-09-11: the tuple is acceptable **for the read path**, where an
exclusion is disclosed and reversible, and #95 stays parked at `stage: research` for
the write path, where a merge would permanently fuse two real accounts' history.

### [DECISION] Supersession requires the older side to have stopped being reported

Within an identity group, a newer generation supersedes an older one **only when the
older account is no longer being listed by its institution** — its `last_seen_date`
is behind its connection's `roster_observed_date`, or its connection is retired.

**Two concurrently-reported accounts that share an identity tuple are never
superseded.** This is the safety property that keeps #95's production case apart: a
checking account and its overdraft line share a four-digit mask at the one real
institution, and both are live on every roster. They would never be superseded even
if the tuple did group them — and it does not, because they differ on
`account_type` (depository vs loan) *and* `account_subtype` (checking vs line of
credit), which is two independent discriminators.

### [DECISION] Generation order is `(enrolled_at, created_at, account_id)`, newest first

Three keys, because no one of them separates every shape.

`enrolled_at` separates two lineages on ONE account row — the converged re-link,
where a single account carries two Items. It cannot separate the converging twins:
the connection row is updated in place, so both generations carry the same
enrollment instant and the same `lineage_id`.

`accounts.created_at` separates two account ROWS under one lineage, which is that
case. **It is an instant, and the obvious alternative — `last_seen_date`, when the
institution last listed the account — is a calendar date that ties.** A re-link
happens right after the sync it replaces, so a same-day re-link would leave both
generations indistinguishable on any date-grained key. `account_id` is the final
tiebreak, so the order is total and a replay cannot reshuffle it.

🔴 **The order NEVER decides that something is superseded.** A total order over two
live accounts would happily declare one of them older, and the rule would then
delete money that was really spent. Ordering is necessary and, across two account
rows, not sufficient — the decision belongs to the evidence above.

## What this plan does not fix, stated explicitly

- **Balances still double-count nothing and still include the stale generation.**
  That is the `api-contract` norm working as ruled (include-and-flag, with the
  magnitude beside the total), not a gap this plan leaves. `list_accounts` will go
  on returning both generations, flagged. Changing that is a separate decision
  about `account_no_longer_active`, and it is not taken here.
- **#95 stays parked.** Nothing in this plan gives the write path an identity
  fallback; the local `account_id` still does not survive a re-link, and the
  duplicate account rows are still created.
- **The already-duplicated sandbox store is not repaired.** It does not need to be:
  the fix is a read-path rule, so the correct totals appear on the next query with
  no data migration. The stale rows stay queryable, which is the design's point.
- **[ASSUMPTION] Every duplicated pair in a real store shares all four tuple
  components.** Held because the aggregator re-issues ids but re-reports the same
  institution-supplied mask, name, type and subtype. If an institution renames an
  account across the break, that pair will not group and will go on doubling —
  visibly, as two rows, which is the failure direction this design chooses.

- **A converged account under a converged connection is still undetectable, and
  still doubles.** Where the account converges (the aggregator publishes a
  persistent id) AND the connection is updated in place rather than replaced, both
  generations land on ONE `account_id` under ONE `lineage_id`. There is no second
  span for the overlap comparison to find, so the total doubles with no
  `rule-applied` caveat — the same silent 2x this plan fixes, by a route it does
  not reach. Reachable at the three institutions that publish a persistent id, via
  `enroll --relink`. Separating those two generations needs transaction-level
  identity, which is the dedupe `lineage.py` rejects on the record, so this is
  FILED rather than fixed here. The module docstring now enumerates all four
  shapes and marks this one as uncovered, so a maintainer triaging a doubled total
  is not sent looking in the wrong place.

- **[RESIDUAL RISK] A reused mask on a closed-and-replaced account can
  over-supersede.** If an institution closes an account and later issues a new one
  reusing the same last-four AND the same name, type and subtype, and the two
  overlap in time, the older one's overlapping rows are excluded — real history,
  gone from the totals. Bounded by needing all four components to match and the
  spans to overlap, and it is DISCLOSED: the `rule-applied` caveat names the
  account and the exact range, so an operator can see and report it. The
  tightening that would close it — requiring the newer generation to start at or
  before the older's — was rejected because it breaks the case a re-link most
  often produces: a new Item granting LESS history than the store already holds,
  where the newer generation legitimately starts later. Transaction-level matching
  would settle it and is the dedupe this module rejects on the record.

## The learning this design runs against, recorded rather than skirted

`learnings.md` § *Guarantees by construction* names the decay shape: **"an
enumeration wearing a predicate's clothes — a rule matching on a NAME where it
means a RELATIONSHIP."** This plan's identity tuple does exactly that. It matches on
four institution-supplied strings where what it means is *these two rows are the
same real account*.

It is taken knowingly, because the relationship it stands in for **is not recorded
anywhere in the store** — that absence is #95, and #95's own evidence forbids
inventing it on the write path. Two things keep the substitution honest:

- the exclusion is **disclosed** and the rows stay reachable, so a wrong grouping is
  visible and correctable, unlike a write-path merge;
- the grouping alone never excludes anything. It takes the *relationship* signal —
  one generation stopped being reported while another continued — to supersede, and
  that signal is a recorded observation rather than a name.

When #95 resolves, the tuple should give way to whatever identity the store then
records. This is a stand-in with a named successor, not a steady state.

## Chunk 01 — Identity-grouped supersession, disclosed

**Delivers.** `superseded_spans` detects a duplicated roster generation and the
aggregates stop doubling; the `rule-applied` caveat names it truthfully.

**Files.** `src/bankmachine/store/lineage.py` (grouping, ordering, module
docstring), `src/bankmachine/query.py` (caveat wording),
`tests/connector/test_transaction_derivers.py` (the converging-path regression the
existing lineage test does not build).

**Tests.**

1. **The converging re-enroll no longer doubles.** Build the path the existing
   fixture does not: re-enroll against a live connection with **no**
   `persistent_account_id`, so a second account row is inserted under the *same*
   connection and the same lineage. Assert the total counts the window once. The
   existing test at `test_transaction_derivers.py:1040` builds its second lineage by
   RETIRING the connection and inserting a new row, which yields a new
   `connection_id` and exercises only the path that already worked.
2. **The older generation's tail survives.** Where the older generation covers days
   the newer one does not, those rows still answer. This is the undercount the whole
   design refuses and it must be asserted, not assumed.
3. **Two live accounts sharing an identity tuple are never superseded.** The safety
   property from the second decision above, asserted directly rather than inferred
   from the tuple happening to differ.
4. **A NULL mask never groups.** Two accounts at one institution with null masks and
   matching name/type/subtype stay separate.
5. **The retire-then-re-enroll path still works.** The existing test must keep
   passing unchanged — it is the contract for the path this plan must not regress.
6. **The caveat fires and names the account and range** on the converging path.

**Done when.** All six pass, the full suite is green, `money_summary` over the live
sandbox store reports `1114946` rather than `2229892` for 2026-08 and carries a
`rule-applied` caveat, and `/prawduct:critic` has been run and its findings
dispositioned.

## Status

- [x] Chunk 01 — Identity-grouped supersession, disclosed

## Context

Chunk 01 landed. Verified on the live sandbox store: `money_summary` for 2026-08
now reports `outflow_minor_units: 1114946` where it reported `2229892`, each account
appears once, and a `rule-applied` caveat names accounts 1-5 with the exact ranges
superseded. Suite green; `mypy` reports the same 13 pre-existing errors as the
merge-base, none in the changed files.

Two things the build changed from the plan, both recorded above in place:

1. **The generation key is `(enrolled_at, created_at, account_id)`, not
   `(enrolled_at, last_seen_date, ...)`.** `last_seen_date` is a calendar date, so
   two generations born on the same day tie -- and a re-link happens right after
   the sync it replaces. The account's creation instant discriminates; the
   last-seen date moved to `still_reported`, where it belongs.
2. **Supersession is gated on `still_reported`, not on the order alone.** A total
   order over two live accounts would declare one of them older and delete money
   really spent. The evidence that a generation was REPLACED is the institution
   having stopped listing it.

Branched from `develop`, not from `fix/environment-guard-and-until-ready`: the two
scopes are independent (that branch touches `store/connection.py` and `secrets.py`;
this one touches `store/lineage.py` and `query.py`) and should merge separately.
