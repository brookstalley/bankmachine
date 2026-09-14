"""Migration 009 -- `transactions.transfer_pair_id`, the other leg of a transfer.

**This DDL never changes.** The rule migrations 002 through 008 state applies
here unaltered: a change to this column is migration 010.

🔴 **The idioms are re-declared here rather than imported from an earlier
migration**, for the reason `core_schema.py` names.

**What the column is for.** A transfer between two accounts the household holds
is not spending: the money is still there, and counting the outgoing leg as
spend overstates what left. The classifier used to decide that from the
aggregator's category ALONE -- so an ATM withdrawal, a Zelle to a person, ACH
rent and a mortgage payment to a lender this store does not hold were all
excluded from spending as though the money had merely moved. It had not; it was
gone.

Deciding it needs the OTHER leg: a matching, opposite row on a different account
this store also holds. This column records which row that is, both legs carrying
the same pair id.

🔴 **Recorded, not recomputed at query time**, for three reasons that each stand
alone. An operator can audit WHY a row was excluded from spending, rather than
being told it was. `store rebuild` reproduces the same pairing from the same
archive, so the classification is derivable rather than incidental. And the read
path stays a read -- a query that had to search for a counterparty on every row
would be doing derivation inside an answer.

🔴 **Nullable, and no backfill.** The runner's contract is DDL only, and there is
nothing to backfill with: no pairing has ever been computed. A null means **no
counterparty leg was found**, which is the ordinary case for most rows and is
NEVER "not checked". `bankmachine store rebuild` computes it over the whole
archive; an ordinary sync computes it for the rows it adds.

🔴 **A null is what makes an unmatched transfer-shaped row spending**, which is
the conservative direction this classifier already takes elsewhere: an overcount
gets questioned and an undercount gets believed, so a row that LOOKS like a
transfer and has no counterparty here counts as money leaving.
"""

from __future__ import annotations

from typing import Final

from bankmachine.store.connection import Connection

#: 🔴 The column lands at the END of `transactions`, because that is where
#: SQLite's `ALTER TABLE ... ADD COLUMN` puts it and `store/schema.py`'s Core
#: metadata is compared to this database column-by-column *in order*.
#:
#: No foreign key: a pair id is a shared token, not a pointer at a row. Making it
#: reference `transactions.transaction_id` would say the pair IS one of its legs,
#: and the two legs are symmetric -- neither is the parent.
TRANSFER_PAIRS_DDL: Final[tuple[str, ...]] = (
    "ALTER TABLE transactions ADD COLUMN transfer_pair_id INTEGER",
)


def apply_transfer_pairs(conn: Connection) -> None:
    """Issue migration 009's DDL. The runner owns the transaction around it."""
    for statement in TRANSFER_PAIRS_DDL:
        conn.execute(statement)
