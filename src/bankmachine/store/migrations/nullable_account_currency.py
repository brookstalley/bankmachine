"""Migration 006 -- `accounts.currency` becomes nullable, and the null means something.

**This DDL never changes.** The rule migrations 002 through 005 state applies
here unaltered: a migration is a record of what some datastore in the world
already ran, so a change to this table is migration 007.

🔴 **The idioms are re-declared here rather than imported from an earlier
migration**, for the reason `core_schema.py` names: sharing a constant means a
later tightening of it silently redefines what an older version meant for every
datastore created afterwards, with every test still green.

**What the null means, and what it must never be read as.** A null currency
means *the aggregator has not told us what unit this account is in* -- never
`USD`, never the unit of the operator's other accounts, never a default of any
kind. Both of the aggregator's currency fields are documented nullable, and an
account whose first balance names neither could not be created at all while this
column was NOT NULL: it was skipped with a warning, and every transaction on it
went on refusing to derive until some later sync happened to supply one. An
account that exists and is honest about its unknown unit is strictly better than
an account that is invisible.

🔴 **The four other NOT NULL currency columns stay NOT NULL.**
`transactions.currency` and the balance columns describe one amount each, and an
amount whose unit nothing stated is already refused row by row with a named
reason. Widening those as well would turn a narrow honesty fix into a store-wide
loosening, and the rows it would admit are ones no total could ever use.

🔴 **A table rebuild, and NO row changes value.** SQLite cannot drop NOT NULL in
place, so the replacement table is created, copied into, swapped for the
original, and its indexes recreated. That shape usually means data moved; here
it does not. Every account in every datastore today has a currency -- the column
would not have accepted one without -- so the copy is the identity and this is a
schema-only change. Worth saying outright, because a reader meeting a rebuild
will reasonably assume otherwise.

🔴 **The rebuild needs foreign keys disabled, which is why this step declares
`rebuilds_a_referenced_table`.** Four tables reference `accounts(account_id)`,
and `DROP TABLE` runs an implicit delete that those references refuse while
enforcement is on. SQLite ignores `PRAGMA foreign_keys` inside a transaction, so
the runner turns it off around this step and runs `PRAGMA foreign_key_check`
before committing -- a rebuild that lost or duplicated a parent row is caught
there rather than discovered later by a query that returns nothing.

🔴 **The column keeps its position.** `store/schema.py`'s Core metadata is
compared to this database column-by-column *in order*, so the replacement lists
the columns exactly as migration 002 created them, with migration 003's
`last_seen_date` last where `ALTER TABLE ... ADD COLUMN` left it.
"""

from __future__ import annotations

from typing import Final

from bankmachine.store.connection import Connection

#: Migration 006's own spelling of a calendar date and a UTC instant,
#: deliberately not shared with migration 002's, 003's, 004's or 005's. See the
#: module docstring.
_V6_DATE: Final = "GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"
_V6_INSTANT: Final = "LIKE '%+00:00'"

#: 🔴 The replacement table, the copy, the swap, and the indexes -- in that
#: order, and every constraint migrations 002 and 003 put on `accounts`
#: reproduced verbatim except the one this step exists to drop. A constraint
#: quietly lost in a rebuild is invisible until the row it would have refused
#: arrives, which on this table means an account with no name or a closed date
#: before the account was first seen.
NULLABLE_ACCOUNT_CURRENCY_DDL: Final[tuple[str, ...]] = (
    f"""
    CREATE TABLE accounts_migration_006 (
        account_id                   INTEGER PRIMARY KEY,
        institution_id               INTEGER NOT NULL REFERENCES institutions(institution_id),
        connection_id                INTEGER REFERENCES connections(connection_id),
        source_account_id            TEXT,
        source_persistent_account_id TEXT,
        name                         TEXT NOT NULL,
        official_name                TEXT,
        mask                         TEXT,
        account_type                 TEXT NOT NULL,
        account_subtype              TEXT,
        balance_class                TEXT NOT NULL
                                     CHECK (balance_class IN ('asset', 'liability')),
        currency                     TEXT,
        lifecycle_status             TEXT NOT NULL
                                     CHECK (lifecycle_status IN ('active', 'inactive')),
        opened_date                  TEXT CHECK (opened_date {_V6_DATE}),
        first_seen_date              TEXT NOT NULL CHECK (first_seen_date {_V6_DATE}),
        closed_date                  TEXT CHECK (closed_date {_V6_DATE}),
        source                       TEXT NOT NULL
                                     CHECK (source IN ('aggregator', 'manual')),
        created_at                   TEXT NOT NULL CHECK (created_at {_V6_INSTANT}),
        updated_at                   TEXT NOT NULL CHECK (updated_at {_V6_INSTANT}),
        last_seen_date               TEXT CHECK (last_seen_date {_V6_DATE}),
        CHECK ((source = 'aggregator') <= (source_account_id IS NOT NULL)),
        CHECK (closed_date IS NULL OR closed_date >= first_seen_date)
    )
    """,
    # 🔴 Columns named on both sides rather than `SELECT *`. A positional copy
    # is correct exactly as long as the two column lists agree, and the failure
    # when they stop agreeing is a silent transposition of two TEXT columns --
    # every row present, every value in the wrong place.
    """
    INSERT INTO accounts_migration_006 (
        account_id, institution_id, connection_id, source_account_id,
        source_persistent_account_id, name, official_name, mask, account_type,
        account_subtype, balance_class, currency, lifecycle_status, opened_date,
        first_seen_date, closed_date, source, created_at, updated_at, last_seen_date
    )
    SELECT
        account_id, institution_id, connection_id, source_account_id,
        source_persistent_account_id, name, official_name, mask, account_type,
        account_subtype, balance_class, currency, lifecycle_status, opened_date,
        first_seen_date, closed_date, source, created_at, updated_at, last_seen_date
    FROM accounts
    """,
    "DROP TABLE accounts",
    "ALTER TABLE accounts_migration_006 RENAME TO accounts",
    # Dropped with the old table, so both are recreated. Their definitions are
    # migration 002's, re-declared here for the same reason the idioms are.
    """
    CREATE UNIQUE INDEX accounts_source_identity
        ON accounts (connection_id, source_account_id) WHERE source_account_id IS NOT NULL
    """,
    """
    CREATE INDEX accounts_by_institution ON accounts (institution_id)
    """,
)


def apply_nullable_account_currency(conn: Connection) -> None:
    """Issue migration 006's DDL. The runner owns the transaction around it."""
    for statement in NULLABLE_ACCOUNT_CURRENCY_DDL:
        conn.execute(statement)
