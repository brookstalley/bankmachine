"""Migration 012 -- an index on `derivation_version_id` for every derived table.

**This DDL never changes.** The rule migrations 002 through 011 state applies
here unaltered: a change to these indexes is migration 013.

**What the indexes are for.** AC-5.4 asks every MCP answer whether any derived
row carries a derivation version other than this build's. A version can move
without a migration, and until `store rebuild` replays the archive the rows
already stored hold what the older logic produced -- nothing in a row says it
would come out differently now. The question rides EVERY answer, so it cannot
cost a scan of the transactions table: it is asked as "does any row carry
version *v*" for each recorded version, and each of those is one seek into one
of these indexes.

🔴 **Every table that points at `derivation_versions`, not only the ones a
rebuild empties.** `securities` is a dimension -- a replay upserts it rather than
deleting it -- and a check that skipped it would miss exactly the rows a rebuild
is least certain to re-stamp.

🔴 **The table list is written out, not derived from the metadata**, for the
reason `core_schema.py` names: a migration is what one datastore actually had
done to it, and a list read off the code at run time would change under a store
that already ran this step. A derived table added later carries its index in its
own migration, and `tests/store/test_schema.py` fails on one that does not.

**No rows move and nothing is backfilled.** An index is derived from the rows it
covers, so a populated store comes out of this step holding exactly what it held.
"""

from __future__ import annotations

from typing import Final

from bankmachine.store.connection import Connection

#: Migration 012's own list of the tables whose rows carry a derivation version.
_V12_DERIVED_TABLES: Final[tuple[str, ...]] = (
    "transactions",
    "balances_daily",
    "securities",
    "holdings",
    "refused_holdings",
    "investment_transactions",
)

DERIVATION_INDEXES_DDL: Final[tuple[str, ...]] = tuple(
    f"CREATE INDEX {table}_by_derivation_version ON {table} (derivation_version_id)"
    for table in _V12_DERIVED_TABLES
)


def apply_derivation_indexes(conn: Connection) -> None:
    """Issue migration 012's DDL. The runner owns the transaction around it."""
    for statement in DERIVATION_INDEXES_DDL:
        conn.execute(statement)
