---
artifact: discovery
version: 1
scope: relink-account-identity
status: findings
governs:
  - brookstalley/bankmachine#67
  - brookstalley/bankmachine#91
  - brookstalley/bankmachine#68
---

# Discovery — account identity across a re-link

Raised 2026-09-10 after a converging re-enroll doubled every figure in the sandbox store. #91 was
staged `research` because the first move is an aggregator documentation question, not code. This is
the answer to it.

## The questions

1. When is `persistent_account_id` populated?
2. Does a new Item re-issue `account_id`?
3. Does a new Item re-issue `transaction_id`?
4. What does the aggregator itself say the recovery path is?

## Findings

**1. `persistent_account_id` exists at three institutions.** It is supported only for Items at
institutions using Tokenized Account Numbers — **three US banks, named in the aggregator's Accounts
API reference** — and only for depository accounts. 🔴 The names are deliberately not written here:
this repository is a general-purpose tool and the leak guard treats an institution name as roster
identity, correctly — the number and the constraint are what the finding rests on, and both survive
without them.

🔴 **This inverts the reading the code carries.** `derivers.py` says the aggregator "populates this
field for select institutions only", which reads as *present by default, absent occasionally*. The
truth is the reverse: **absent is the rule, present is the exception**, and the exception is three
banks. Every other institution — every credit union, every card issuer, every regional bank, and
every non-depository account even at those three — has NULL, and therefore has no stable identity
across Items.

**2. A new Item re-issues `account_id`.** The documented triggers are exactly ours: deleting an
access token and generating a new one from the same credentials yields different `account_id`s. (It
also changes when the aggregator cannot reconcile an account, e.g. a renamed account.)

**3. `transaction_id` is not stable across regenerated account data.** Where the underlying account
data is re-generated, transaction ids may change. The aggregator's own dedupe advice — compare
incoming `transaction_id` against stored ones and skip matches — therefore does not hold across a
re-link, which is the one case where dedupe matters most.

**4. The aggregator's documented answer to expired credentials is *update mode*, not remove-and-
re-link.** Update mode repairs the existing Item in place, so no second generation of ids is minted.
Its documentation notes that account ids should still be re-read afterwards rather than assumed.

## What this changes

🔴 **The duplication is not an edge case. It is the default outcome of a re-link at almost every
institution this product will ever meet.** At the time of this discovery the store's only recovery
from an expired login was `enroll`, which mints a new Item, which re-issues every id, which
duplicates every account and every transaction, and doubles every money figure with no warning
naming it. **#67 shipped `connections reauth` as the recovery and made `enroll` refuse the
unflagged re-link**, so that is no longer the path an operator is sent down — but it remains what
`enroll --relink` costs.

**#67 (reauth via update mode) is therefore the primary mitigation, not an ergonomic improvement.**
An earlier read in this session — that adding it later is graceful, and that the risk bites only at
institutions lacking `persistent_account_id` — was right about the mechanics and wrong about the
scope: it framed NULL as the exception when NULL is the rule. Update mode does not merely *avoid*
the cost of a re-link; it is the only path that avoids minting a second generation at all.

**Lineage exclusion (#68, shipped) cannot cover this.** `store/lineage.py::_coverage` groups by
`(account_id, lineage_id)`. Duplicated accounts have *different* `account_id`s, so two generations
of one real account read as two unrelated accounts and nothing is superseded. Lineage only helps
when the accounts converged — which requires `persistent_account_id` — so it is load-bearing at
three banks and inert everywhere else.

**"Retire first, then re-enroll" is not a workaround.** It takes the same path: new Item, new
account ids, duplicate rows. It differs only in marking the old accounts inactive.

## What is still open

- **Does update mode preserve `account_id` in practice?** The documentation says to re-read them
  afterwards rather than assume, which stops short of a guarantee. **#67 settled the half that is
  assertable from one call and no more:** `connections reauth` compares the `item_id` the aggregator
  reports after the session against the one the connection was enrolled with, and refuses if they
  differ — so a repair cannot silently become a re-link. Whether the *accounts* beneath an unchanged
  Item keep their ids is not answerable from that call, and **it cannot be answered in sandbox**; it
  is enqueued as an operator verification against production (`.prawduct/operator-verification.md`).
  If they are re-issued, #67 reduces the frequency of the duplication without eliminating it and the
  identity fallback below is needed regardless.
- **What identity should stand in where `persistent_account_id` is NULL?** `(mask, name, type,
  subtype)` scoped to the connection is the obvious candidate. `_match_account` deliberately refuses
  to guess today, and its stated reason is sound: a false merge of two real accounts is worse than a
  duplicate, because it is silent and unrecoverable while a duplicate is at least visible.
- **Should the product disclose a suspected duplication?** Today nothing does.
  `account_no_longer_active` is the only warning that fires in this state and it offers "closure or
  de-selection from sharing" as the explanation, never a re-link, and speaks only about balances.

## Sources

Aggregator documentation, read 2026-09-10: the Accounts API reference (`persistent_account_id`
support and `account_id` change triggers), the Transactions troubleshooting guide (id regeneration
and dedupe advice), and the Link update-mode guide.
