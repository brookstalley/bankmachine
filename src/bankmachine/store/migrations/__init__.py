"""Forward-only migrations, each applied in exactly one transaction.

The runner is deliberately ~50 lines and owns its own transaction boundary
rather than delegating to a migration framework. That boundary is the whole
point: a migration's DDL and the row recording its version commit together or
not at all, so a process killed between them leaves a datastore that reports no
recognized schema version rather than one that reports healthy while missing the
table the version claims it has.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from bankmachine.config import Config
from bankmachine.logging_setup import get_logger
from bankmachine.store.connection import (
    Connection,
    StoreError,
    initializing_writer,
    read_schema_version,
    stamp_schema_version,
)
from bankmachine.store.migrations.account_last_seen import apply_account_last_seen
from bankmachine.store.migrations.core_schema import apply_core_schema
from bankmachine.store.migrations.derivation_indexes import apply_derivation_indexes
from bankmachine.store.migrations.holdings_price_as_of import apply_holdings_price_as_of
from bankmachine.store.migrations.item_consent import apply_item_consent
from bankmachine.store.migrations.ledger_date import apply_ledger_date
from bankmachine.store.migrations.nullable_account_currency import (
    apply_nullable_account_currency,
)
from bankmachine.store.migrations.refused_holdings import apply_refused_holdings
from bankmachine.store.migrations.roster_observed import apply_roster_observed
from bankmachine.store.migrations.transaction_lineage import apply_transaction_lineage
from bankmachine.store.migrations.transfer_pairs import apply_transfer_pairs

logger = get_logger("store.migrations")


@dataclass(frozen=True, slots=True)
class Migration:
    """One forward step. `apply` issues DDL only; the runner stamps the version."""

    version: int
    name: str
    apply: Callable[[Connection], None]
    #: 🔴 Whether this step DROPS a table that other tables reference by name.
    #: SQLite runs an implicit delete inside `DROP TABLE`, and an enforced
    #: foreign key refuses it -- while `PRAGMA foreign_keys` is ignored inside a
    #: transaction, so enforcement can only be suspended AROUND the step. That
    #: is a guarantee the runner may lift only where a step needs it and only
    #: where it puts it back, so the need is declared per step rather than
    #: inferred, and the check below is what stands in for enforcement while it
    #: is off.
    rebuilds_a_referenced_table: bool = False


def _create_schema_version(conn: Connection) -> None:
    """The table every later migration records itself in.

    One row per applied migration rather than one mutable row: the history is
    worth more than the byte it saves, and `MAX(version)` is the current state.
    """
    conn.execute(
        "CREATE TABLE schema_version (  version INTEGER PRIMARY KEY,  applied_at TEXT NOT NULL)"
    )


MIGRATIONS: Sequence[Migration] = (
    Migration(version=1, name="create schema_version", apply=_create_schema_version),
    Migration(version=2, name="create the core schema", apply=apply_core_schema),
    Migration(
        version=3,
        name="record when an account was last listed",
        apply=apply_account_last_seen,
    ),
    Migration(
        version=4,
        name="record when a connection's roster was observed",
        apply=apply_roster_observed,
    ),
    Migration(
        version=5,
        name="record the day a transaction's money was committed",
        apply=apply_ledger_date,
    ),
    Migration(
        version=6,
        name="let an account exist in a currency the aggregator has not stated",
        apply=apply_nullable_account_currency,
        rebuilds_a_referenced_table=True,
    ),
    Migration(
        version=7,
        name="record the aggregator Item a transaction was produced under",
        apply=apply_transaction_lineage,
    ),
    Migration(
        version=8,
        name="record the Item's consent expiry and standing error",
        apply=apply_item_consent,
    ),
    Migration(
        version=9,
        name="record which rows are the two legs of one transfer",
        apply=apply_transfer_pairs,
    ),
    Migration(
        version=10,
        name="record the date of the price a position is valued at",
        apply=apply_holdings_price_as_of,
    ),
    Migration(
        version=11,
        name="record the positions a capture listed and this build could not denominate",
        apply=apply_refused_holdings,
    ),
    Migration(
        version=12,
        name="index every derived table on the derivation version its rows carry",
        apply=apply_derivation_indexes,
    ),
)


def _refuse_dangling_references(conn: Connection) -> None:
    """Refuse a rebuilt table that left a child row pointing at nothing.

    🔴 What stands in for foreign-key enforcement while the runner has it off.
    A table rebuild copies the parent rows itself, so the one way it goes wrong
    is by copying fewer than it found -- and with enforcement suspended nothing
    would say so. The symptom afterwards is not an error but an account whose
    transactions belong to no account: a query returns nothing and the answer
    looks like a quiet month.

    Raised before the version is stamped, so the DDL and the stamp roll back
    together and the datastore reports the version it actually holds.
    """
    dangling = conn.execute("PRAGMA foreign_key_check").fetchall()
    if not dangling:
        return
    tables = sorted({str(row[0]) for row in dangling})
    raise StoreError(
        f"the migration left {len(dangling)} row(s) in {', '.join(tables)} referencing a row "
        f"that is no longer there, so the rebuilt table did not carry every row it replaced. "
        f"Rolled back: a datastore whose children point at nothing answers questions with "
        f"silence rather than with an error"
    )


def migrate(config: Config, *, migrations: Sequence[Migration] | None = None) -> list[int]:
    """Apply every pending migration, and return the versions applied.

    The migration runner is one of the two paths permitted to bring a datastore
    into existence; every other caller opens a file that must already be there.
    """
    steps = MIGRATIONS if migrations is None else migrations
    applied: list[int] = []
    with initializing_writer(config) as conn:
        current = read_schema_version(conn) or 0
        for step in steps:
            if step.version <= current:
                continue
            # 🔴 Outside the transaction, because SQLite ignores this pragma
            # inside one. Restored on both ways out below -- a step that raised
            # must not hand back a connection enforcing less than the one it was
            # given, and the next step in this same loop would run under it.
            if step.rebuilds_a_referenced_table:
                conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN IMMEDIATE")
            try:
                step.apply(conn)
                if step.rebuilds_a_referenced_table:
                    _refuse_dangling_references(conn)
                stamp_schema_version(conn, step.version)
            except BaseException:
                conn.execute("ROLLBACK")
                if step.rebuilds_a_referenced_table:
                    conn.execute("PRAGMA foreign_keys = ON")
                raise
            conn.execute("COMMIT")
            if step.rebuilds_a_referenced_table:
                conn.execute("PRAGMA foreign_keys = ON")
            logger.info("applied migration %d (%s)", step.version, step.name)
            applied.append(step.version)
    return applied
