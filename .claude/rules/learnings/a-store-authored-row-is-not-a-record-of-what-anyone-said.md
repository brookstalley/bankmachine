---
paths:
  - "tests/store/**"
---

# A store-authored row is not a record of what anyone said

**When a preservation norm — never hard-delete, never overwrite in place — meets a row the product
itself authored, rule that the norm does not reach it, because those norms protect *evidence of what
an outside party told us*, and a row we wrote ourselves is a current belief instead.**

`data-model.md` § Direction carries two preservation norms, and both are about the aggregator. *A
source value is never overwritten in place* exists so provider drift stays distinguishable from a
local decision. *A transaction is never hard-deleted* exists because a removal is evidence of a
reversal or a correction, and because a rebuild from the archive can reproduce a removal but not a
row nobody kept. Neither why survives contact with an annotation: nobody outside said it, the
archive does not contain it, and no rebuild can reproduce it or lose it.

**Applied mechanically, both norms would have made an annotation permanent.** An agent's note is its
belief at a moment; a belief that can be added and never withdrawn becomes *permanently wrong* the
first time the agent learns better, and it keeps being returned beside the row as though the store
stood behind it. Preserving it would protect nothing and mislead every later reader — the exact
inversion of what the norms were written to do.

**The category, so the next case at this edge is pre-decided:** a preservation norm reaches rows
whose author is outside this product. Rows this product's own operator or agents authored — the
annotation table, and anything later built on the same footing — are governed by their own
requirement, and their requirement may say *delete means delete*.

**Instances:**

- *2026-09-10, FR-11 · Agent annotations.* AC-16.7 requires that an annotation be rewritable and
  deletable with no tombstone. Recorded as a ruling on both norms rather than as a silent exemption,
  and linked from each entry's `Rulings:` line. Note the asymmetry it does **not** license:
  `transactions.category_override` is operator-authored but sits on an aggregator-authored row, and
  § 5's 2026-09-10 amendment keeps it out of an agent's reach for that reason. Authorship of the
  **row** is the test, not authorship of the value.

**How to apply:** at any preservation norm's edge, ask who wrote the row — not who wrote the field,
and not whether the field looks derived. If the answer is "we did", the norm's why does not reach it
and the ruling is already made here; record the instance and move on.
