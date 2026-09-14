"""Migration 007 -- `transactions.lineage_id`, the Item a row was produced under.

**This DDL never changes.** The rule migrations 002 through 006 state applies
here unaltered: a migration is a record of what some datastore in the world
already ran, so a change to this column is migration 008.

🔴 **The idioms are re-declared here rather than imported from an earlier
migration**, for the reason `core_schema.py` names: sharing a constant means a
later tightening of it silently redefines what an older version meant for every
datastore created afterwards, with every test still green.

**What the column is for.** Removing a connection and linking it again yields a
new Item at the aggregator, and the new Item re-issues every account id AND
every transaction id. The account converges -- `source_persistent_account_id`
names the same underlying account across the break -- but the transactions
cannot: the whole granted history arrives again as rows this store has never
seen, under an account it now recognises, with nothing for a unique index to
collide with. Summed naively, a year of spending doubles.

🔴 **This is not a new concept in the system; it is the Item boundary finally
given a name in this table.** The sync cursor and the measured granted window
are already Item-scoped and are already reset by a re-link. `connections` is
this store's row per Item -- `source_connection_id` holds the Item id and is
unique -- so the lineage a transaction belongs to is the connection whose
response produced it, and the column is a plain reference to it.

🔴 **Every row is KEPT.** The rejected alternative was a natural key over
`(account_id, ledger_date, amount_minor, name)`, which is what most systems do
and is wrong for this one: two genuinely distinct real transactions with the
same merchant, amount and day -- two $5 coffees, two identical fares -- collapse
into one and the total goes DOWN with nothing to say so. An overcount gets
questioned; an undercount gets believed. A rule that can silently delete real
money is on the wrong side of that, so nothing is deleted here and nothing is
deduped. How a read counts rows that cover the same range is `store/lineage.py`'s
to say, not this migration's.

🔴 **Nullable, and no backfill, which is the runner's contract and also what is
honest.** `apply` issues DDL only, and there is no constant that is correct: the
value is a per-row fact about which response produced the row. So a null means
**"this row predates the split and has not been rebuilt"** -- never "belongs to
the current Item", which is the reading that would let a rebuilt store exclude
rows it should have counted. A row with no lineage is therefore never excluded
by the read path: it is counted, because the alternative is deleting money on
the strength of a column that was never filled.

🔴 **`bankmachine store rebuild` is what fills it**, the way it fills migration
005's column: `transactions` carries `derivation_version_id` and points at the
response it came from, so replaying the archive restamps every row with the
connection that response was fetched for. `DERIVATION_VERSION` moves with this
migration so the replay is a recorded change rather than a silent one.
"""

from __future__ import annotations

from typing import Final

from bankmachine.store.connection import Connection

#: 🔴 The column lands at the END of `transactions`, because that is where
#: SQLite's `ALTER TABLE ... ADD COLUMN` puts it and `store/schema.py`'s Core
#: metadata is compared to this database column-by-column *in order*
#: (`tests/store/test_schema.py::test_the_metadata_matches_the_migrated_database`).
#: Declaring it beside `raw_response_id`, where the provenance it completes
#: would be obvious, would make two correct descriptions of one correct
#: database disagree.
#:
#: 🔴 The `REFERENCES` clause is permitted here only because the new column
#: defaults to NULL -- SQLite refuses `ALTER TABLE ... ADD COLUMN` with a
#: foreign key and any other default. That is the same nullability the column
#: needs anyway, so the constraint and the design agree.
TRANSACTION_LINEAGE_DDL: Final[tuple[str, ...]] = (
    "ALTER TABLE transactions ADD COLUMN lineage_id INTEGER REFERENCES connections(connection_id)",
)


def apply_transaction_lineage(conn: Connection) -> None:
    """Issue migration 007's DDL. The runner owns the transaction around it."""
    for statement in TRANSACTION_LINEAGE_DDL:
        conn.execute(statement)
