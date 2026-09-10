"""A datastore holding rows, brought forward from an older schema version.

Every other migration test starts from an empty store or from one already at the
version this build serves, so none of them asserts the operation an operator
actually performs: a datastore holding real rows crosses a schema version, and
the rows are still there and still correct on the other side. That upgrade is
the only thing that ever runs over the balance series, which
`operational-spec.md` § RPO/RTO records as the one thing no re-sync can rebuild.

🔴 **The rows are seeded through the ordinary derivers, never by hand.** A
hand-written row carries whatever the test thought a column meant, so it can
satisfy a migration that a derived row would not -- and the shape being upgraded
is the whole subject here.

**Standing in the previous build is what makes that possible.** A store at the
older schema version was written by a build that served that version and stamped
its rows with the derivation version shipping alongside it, so both constants are
stood in while the store is seeded; the ordinary writer and the shipped derivers
then run against the file exactly as they did then. The alternative -- reaching
for the one handle permitted to open an unsupported version and writing SQL
through it -- seeds rows no deriver ever produced.

🔴 **`/accounts/get` is deliberately absent from the seeding.** It is the roster
call, and a store seeded from it would carry a roster observation, which makes
the last test below -- the rebuild must not invent one from a sync body -- assert
nothing. `/transactions/sync` carries the same accounts array and derives the
same account and balance rows through the same code, without the observation.

🔴 **This module tracks the NEWEST migration, and re-pointing it is the
maintenance it asks for.** It models the upgrade an operator's own datastore
performs next, so when a migration lands, the constants move to it. They cannot
be written relatively -- see the note on them -- so the move is by hand, and the
alternative is a module that quietly stops testing the migration anyone is about
to run. It was first written against migration 004 and has moved with each one
since; it is pointed at 007 now.

🔴 **007 adds `transactions.lineage_id`, and the sharp question is what that
column holds on rows that predate it.** It must be EMPTY: no `ALTER TABLE ...
ADD COLUMN` can name the Item that produced each row, and the read path reads a
null as *this row predates the split* and counts the row. A migration that put
anything there would make that reading false and drop rows out of totals in
silence. The remedy is `store rebuild`, and the test at the bottom is what says
the remedy works rather than merely being prescribed.

🔴 **006's fixture is KEPT rather than retired with the re-point, and that is
deliberate.** It is the only TABLE REBUILD in this store's history: it creates a
replacement `accounts`, copies every row across, drops the original and swaps the
new one in -- with foreign-key enforcement suspended, because four tables
reference the table being dropped. Nothing about that is visible in the column
list afterwards, and the ways it goes wrong are silent: a row not copied, two
TEXT columns transposed, a CHECK or an index lost with the table it hung from, a
child left pointing at a parent that is no longer there. A populated store is the
only place any of that is reachable, so retiring those assertions to make the
re-point tidy would leave the operation proved by nothing. They ride
`populated_before_the_nullable_currency` instead.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import insert, select, update

from bankmachine.config import Config
from bankmachine.connector import ITEM_GET, TRANSACTIONS_SYNC
from bankmachine.derivers import ALL_DERIVERS
from bankmachine.secrets import generate_datastore_key, set_datastore_key
from bankmachine.store import derivation
from bankmachine.store.connection import reader, writer
from bankmachine.store.derivation import apply_response
from bankmachine.store.engine import reader_connection, writer_connection
from bankmachine.store.migrations import MIGRATIONS, migrate
from bankmachine.store.rebuild import rebuild
from bankmachine.store.schema import (
    accounts,
    balances_daily,
    connections,
    derivation_versions,
    institutions,
    transactions,
)
from bankmachine.store.types import UtcInstant, utc_instant

#: The schema version the seeded store stops at, and the derivation version the
#: build that served it stamped its rows with -- one migration behind this build,
#: which is the upgrade an operator's own datastore performs next.
#:
#: 🔴 Fixed historical numbers, deliberately NOT written as
#: `SUPPORTED_SCHEMA_VERSION - 1`. Written relatively they move with the
#: constants, so the seeded store and the running build could never be at
#: different versions and every assertion below would hold whether or not the
#: migration did anything at all.
SCHEMA_BEFORE_THE_TRANSFER_PAIRS = 8
DERIVATION_BEFORE_THE_TRANSFER_PAIRS = 7

#: What that derivation version meant, so the seeded `derivation_versions` row
#: says something true about the rows hanging off it rather than describing a
#: convergence rule those rows predate.
DESCRIPTION_BEFORE_THE_TRANSFER_PAIRS = (
    "institutions and accounts derived from the aggregator; balances signed from the "
    "operator's point of view; the roster observation recorded per account "
    "(`accounts.last_seen_date`) and per connection (`connections.roster_observed_date`); "
    "the day a transaction's money was committed stamped once as "
    "`transactions.ledger_date` and never moved by settlement; an account created with no "
    "currency where the aggregator has stated none, and any row whose currency has no known "
    "minor-unit exponent refused rather than rounded; an account matched on the persistent "
    "identity the aggregator gives it, scoped to its institution, so a re-link converges on "
    "the row its history already hangs from; and every transaction stamped with the "
    "aggregator Item that produced it (`transactions.lineage_id`); and the Item's consent "
    "expiry and its standing error recorded on the connection"
)

#: The migration the seeded store is missing, the table it adds columns to, and
#: the columns themselves. Migration 008 adds TWO, which is why this is a tuple
#: where its predecessors were a single name.
THE_PENDING_MIGRATION = SCHEMA_BEFORE_THE_TRANSFER_PAIRS + 1
THE_TABLE_IT_EXTENDS = transactions.name
THE_COLUMNS_IT_ADDS = ("transfer_pair_id",)

#: The migrations BETWEEN the older fixture and this build, each named with the
#: version it IS rather than as an offset from the newest.
#:
#: 🔴 Offsets were how two rewinds silently deleted the same `schema_version`
#: row and left another standing: `THE_PENDING_MIGRATION - 1` reads the same in
#: two functions and means the same thing in only one of them. A version is a
#: fixed historical number, so it is written as one -- the same rule the fixture
#: constants above already follow, applied to the steps between them.
THE_LINEAGE_MIGRATION = 7
THE_COLUMN_THE_LINEAGE_MIGRATION_ADDS = "lineage_id"

THE_ITEM_STANDING_MIGRATION = 8
THE_COLUMNS_THE_ITEM_MIGRATION_ADDS = ("consent_expires_at", "source_error_code")

#: 🔴 **The version before THAT, kept rather than retired with the re-point.**
#: Migration 006 is the only table rebuild in this store's history, and the ways
#: a rebuild goes wrong -- a row not copied, two TEXT columns transposed, a CHECK
#: or an index lost with the table it hung from -- are invisible in a column list
#: and are only ever exercised over a store that HOLDS rows. Re-pointing the
#: module at 007 and letting these go would leave that operation proved by
#: nothing, so the older store gets its own fixture and keeps its own assertions.
SCHEMA_BEFORE_THE_NULLABLE_CURRENCY = 5
DERIVATION_BEFORE_THE_NULLABLE_CURRENCY = 4

DESCRIPTION_BEFORE_THE_NULLABLE_CURRENCY = (
    "institutions and accounts derived from the aggregator; balances signed from the "
    "operator's point of view; the roster observation recorded per account "
    "(`accounts.last_seen_date`) and per connection (`connections.roster_observed_date`); "
    "the day a transaction's money was committed stamped once as "
    "`transactions.ledger_date` and never moved by settlement"
)

#: The two migrations that store is missing, and the column whose NOT NULL the
#: first of them drops.
THE_PENDING_MIGRATIONS = [
    SCHEMA_BEFORE_THE_NULLABLE_CURRENCY + 1,
    THE_LINEAGE_MIGRATION,
    THE_ITEM_STANDING_MIGRATION,
    THE_PENDING_MIGRATION,
]
THE_COLUMN_IT_WIDENS = "currency"

#: The table it rebuilds to do that -- SQLite cannot drop NOT NULL in place.
THE_TABLE_IT_REBUILDS = accounts.name

#: The table every migration records itself in. Its rows are the one thing the
#: upgrade is meant to change, so they are compared on their own.
SCHEMA_VERSION_TABLE = "schema_version"

#: The tables that must be holding rows for any of this to assert anything. An
#: upgrade over an empty store is what the rest of this tree already covers.
POPULATED_BY_THE_SEEDING = (
    institutions.name,
    connections.name,
    accounts.name,
    balances_daily.name,
    transactions.name,
)

SOURCE_INSTITUTION = "ins_109508"
SOURCE_CONNECTION = "item-under-upgrade"
SOURCE_ACCOUNT = "acct-checking"

#: Fixed instants, so a stored timestamp is compared against a value this file
#: chose rather than against whatever the clock said.
ENROLLED: UtcInstant = utc_instant(datetime(2026, 9, 5, 9, 0, tzinfo=UTC))
FETCHED: UtcInstant = utc_instant(datetime(2026, 9, 7, 12, 30, tzinfo=UTC))

#: The balance and the two amounts the seeded bodies carry, as the aggregator
#: spells them, beside what this product stores. A purchase arrives POSITIVE and
#: money leaving an account is stored negative, so the expectations below are
#: written from the response rather than copied from what the deriver produced.
BALANCE_AS_SENT = "110.94"
BALANCE_AS_STORED = 11094
PURCHASE_AS_SENT = "12.34"
PURCHASE_AS_STORED = -1234
REFUND_AS_SENT = "-5.00"
REFUND_AS_STORED = 500

POSTED_DATE = date(2026, 9, 7)

#: A day BEFORE the posting date, so a `ledger_date` that fell back to
#: `posted_date` is distinguishable from one that took the authorization.
AUTHORIZED_DATE = date(2026, 9, 6)


# --------------------------------------------------------------------------
# The store an operator is upgrading from.
# --------------------------------------------------------------------------


def _account_entry() -> dict[str, Any]:
    """One account, in the shape `/transactions/sync` sends it."""
    return {
        "account_id": SOURCE_ACCOUNT,
        "name": "Plaid Checking",
        "official_name": "Plaid Gold Checking",
        "mask": "0000",
        "type": "depository",
        "subtype": "checking",
        "balances": {
            "current": BALANCE_AS_SENT,
            "available": "100.00",
            "limit": None,
            "iso_currency_code": "USD",
        },
    }


def _transaction(transaction_id: str, amount: str) -> dict[str, Any]:
    return {
        "account_id": SOURCE_ACCOUNT,
        "transaction_id": transaction_id,
        "amount": amount,
        "iso_currency_code": "USD",
        "date": POSTED_DATE.isoformat(),
        "authorized_date": AUTHORIZED_DATE.isoformat(),
        "pending": False,
        "pending_transaction_id": None,
        "name": "SparkFun",
        "merchant_name": "FUN",
        "personal_finance_category": {
            "primary": "GENERAL_MERCHANDISE",
            "detailed": "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES",
            "confidence_level": "LOW",
        },
    }


def _item_body() -> bytes:
    return json.dumps(
        {
            "item": {
                "item_id": SOURCE_CONNECTION,
                "institution_id": SOURCE_INSTITUTION,
                "institution_name": "First Platypus Bank",
                "products": ["transactions"],
                "available_products": ["balance"],
            },
            "request_id": "req-item",
        }
    ).encode()


def _sync_body() -> bytes:
    return json.dumps(
        {
            "accounts": [_account_entry()],
            "added": [
                _transaction("txn-purchase", PURCHASE_AS_SENT),
                _transaction("txn-refund", REFUND_AS_SENT),
            ],
            "modified": [],
            "removed": [],
            "next_cursor": "cursor-1",
            "has_more": False,
            "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE",
            "request_id": "req-sync",
        }
    ).encode()


def _enroll(config: Config) -> None:
    """The institution and the connection an operator linked.

    Enrollment is operator state rather than derivation -- no response carries it
    and no deriver writes it -- so it is inserted, and the accounts deriver then
    reads the connection to learn which institution an account belongs to.
    """
    with writer_connection(config) as conn:
        seeded = conn.execute(
            insert(institutions).values(
                source_institution_id=SOURCE_INSTITUTION,
                name="First Platypus Bank",
                first_seen_at=ENROLLED,
                last_seen_at=ENROLLED,
            )
        ).inserted_primary_key
        assert seeded is not None  # an INTEGER PRIMARY KEY insert always yields one
        conn.execute(
            insert(connections).values(
                institution_id=seeded[0],
                source_connection_id=SOURCE_CONNECTION,
                credential_ref="connection:sandbox:item-under-upgrade",
                capabilities='["transactions"]',
                requested_history_days=730,
                status="active",
                enrolled_at=ENROLLED,
                created_at=ENROLLED,
                updated_at=ENROLLED,
            )
        )


def _archive_and_derive(config: Config, endpoint: str, body: bytes) -> None:
    """One response through the sync path's own entry point, derivers and all."""
    with writer_connection(config) as conn:
        apply_response(
            conn,
            connection_id=1,
            endpoint=endpoint,
            body=body,
            received_at=FETCHED,
            derivers=ALL_DERIVERS,
        )


@pytest.fixture
def populated_at_the_previous_version(config: Config, monkeypatch: pytest.MonkeyPatch) -> Config:
    """A datastore stopped one migration short of this build, holding derived rows.

    🔴 **Seeded at the CURRENT version and then rewound, rather than seeded at the
    old one -- and that order is forced.** A build's derivers write the columns
    that build has, so this build's transaction deriver stamps `lineage_id` and
    cannot run against any store predating it. Seeding first and removing the
    column afterwards is what lets the rows be written by the shipped path --
    `apply_response`, the shipped registry, the shipped derivers -- which is the
    property this whole module rests on. A hand-written row would satisfy a
    migration a derived row would not.

    What comes out is a file shaped exactly like one the previous build left:
    no `transactions.lineage_id`, `schema_version` topping out one short, and
    every row carrying the derivation version that build stamped.

    🔴 The derivation version and its description are stood in during seeding
    rather than rewritten afterwards, because they are what the rows POINT AT.
    Rewriting them after the fact would leave the row and its description written
    by different hands, which is the drift this fixture exists to avoid.
    """
    _seed(
        config,
        monkeypatch,
        DERIVATION_BEFORE_THE_TRANSFER_PAIRS,
        DESCRIPTION_BEFORE_THE_TRANSFER_PAIRS,
    )
    _rewind_past_the_transfer_pairs(config)

    _refuse_a_fixture_with_nothing_in_it(config)
    assert not set(THE_COLUMNS_IT_ADDS) & set(dump_every_table(config)[THE_TABLE_IT_EXTENDS][0]), (
        "the rewind left the column already there, so the migration under test has nothing to "
        "add and every assertion below would hold against a store that never moved"
    )
    return config


@pytest.fixture
def populated_before_the_nullable_currency(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> Config:
    """The same store, two migrations short -- the one 006's table rebuild runs over.

    🔴 **Kept when this module re-pointed at 007.** Migration 006 is the only
    table rebuild in this store's history, and a rebuild's failures are silent
    and only reachable over a store that holds rows. The assertions that ride
    this fixture are 006's own, and retiring them with the re-point would have
    left the operation nothing proves.

    Its rows carry the derivation version the build serving schema 5 stamped,
    which is two behind this one -- so it says nothing about a single reverted
    version bump, and the fixture above is what covers that.
    """
    _seed(
        config,
        monkeypatch,
        DERIVATION_BEFORE_THE_NULLABLE_CURRENCY,
        DESCRIPTION_BEFORE_THE_NULLABLE_CURRENCY,
    )
    # 🔴 Oldest migration undone FIRST, which is the opposite of the order they
    # ran in. Each rewind opens an ordinary writer, and that handle refuses a
    # store at a version this build does not serve -- so the step that makes the
    # file unsupported has to be the last one taken against it, and every step
    # before it runs while the file still reports a version this build knows.
    _rewind_past_the_nullable_currency(config)
    _rewind_past_the_lineage_column(config)
    _rewind_past_the_item_standing(config)
    _rewind_past_the_transfer_pairs(config)

    _refuse_a_fixture_with_nothing_in_it(config)
    assert required_columns(config, THE_TABLE_IT_REBUILDS) >= {THE_COLUMN_IT_WIDENS}, (
        "the rewind left the column already nullable, so the migration under test has nothing "
        "to widen and every assertion below would hold against a store that never moved"
    )
    return config


def _seed(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
    derivation_version: int,
    description: str,
) -> None:
    """One store at the current version, holding rows the shipped derivers wrote."""
    set_datastore_key(config, generate_datastore_key())
    assert migrate(config) == [step.version for step in MIGRATIONS]

    with monkeypatch.context() as previous_build:
        previous_build.setattr(derivation, "DERIVATION_VERSION", derivation_version)
        previous_build.setattr(derivation, "DERIVATION_DESCRIPTION", description)
        _enroll(config)
        _archive_and_derive(config, ITEM_GET.path, _item_body())
        _archive_and_derive(config, TRANSACTIONS_SYNC.path, _sync_body())


def _refuse_a_fixture_with_nothing_in_it(config: Config) -> None:
    """An upgrade over an empty store is what the rest of this tree already covers."""
    seeded = dump_every_table(config)
    empty = [name for name in POPULATED_BY_THE_SEEDING if not seeded[name][1]]
    assert not empty, f"the derivers wrote nothing into {empty}, so nothing here is under test"


def _rewind_past_the_transfer_pairs(config: Config) -> None:
    """Take the seeded store back to the version before this build's last migration.

    Undoes exactly what migration 009 did -- one column on `transactions`, and
    its row in `schema_version` -- so the file reports the older version and a
    reader of it cannot tell it from a store that never crossed the migration.

    🔴 **`DROP COLUMN`, deliberately not the inverse of the shipped statement.**
    The migration adds the column with `ALTER TABLE ... ADD COLUMN`; reusing that
    statement's own text here would make the fixture and the code under test one
    implementation, and a mistake in the column's declaration would be made
    twice and compared against itself.

    🔴 The store is still at the current version while this runs, which is why an
    ordinary writer may open it. The rewind is what makes it unsupported, so the
    version row goes last.
    """
    with writer(config) as conn:
        for column in THE_COLUMNS_IT_ADDS:
            conn.execute(f"ALTER TABLE {THE_TABLE_IT_EXTENDS} DROP COLUMN {column}")
        conn.execute("DELETE FROM schema_version WHERE version = ?", (THE_PENDING_MIGRATION,))


def _rewind_past_the_item_standing(config: Config) -> None:
    """Take a store back past 008, the migration that recorded the Item's standing.

    🔴 Kept when the module re-pointed at 009, for the reason the lineage rewind
    below is: the older fixture crosses every migration from its own version
    forward, so it has to be able to arrive at that version.
    """
    with writer(config) as conn:
        for column in THE_COLUMNS_THE_ITEM_MIGRATION_ADDS:
            conn.execute(f"ALTER TABLE {connections.name} DROP COLUMN {column}")
        conn.execute("DELETE FROM schema_version WHERE version = ?", (THE_ITEM_STANDING_MIGRATION,))


def _rewind_past_the_lineage_column(config: Config) -> None:
    """Take a store back past 007, the migration that stamped each row's Item.

    🔴 **Kept rather than retired when the module re-pointed at 008.** The older
    fixture below crosses every migration from its own version forward, so it
    has to be able to arrive at that version -- and the rows it is asserting
    about survived 007 as well as 008. A rewind that stopped at the newest
    migration would leave the older store holding a column its version does not
    have, which is not a state any operator's datastore is ever in.
    """
    with writer(config) as conn:
        conn.execute(
            f"ALTER TABLE {transactions.name} DROP COLUMN {THE_COLUMN_THE_LINEAGE_MIGRATION_ADDS}"
        )
        conn.execute("DELETE FROM schema_version WHERE version = ?", (THE_LINEAGE_MIGRATION,))


def _rewind_past_the_nullable_currency(config: Config) -> None:
    """Take the seeded store back past 006, the migration that rebuilt `accounts`.

    Undoes exactly what that migration did -- the column's nullability, and its
    row in `schema_version`.

    🔴 **A rebuild in the other direction, and deliberately not a call to the
    migration's own DDL.** Reusing the shipped statements with `NOT NULL` spliced
    back in would make the fixture and the code under test one implementation:
    a rebuild that dropped a CHECK would drop it on both sides and the
    comparison would still pass. This is written out, so what the migration
    produces is compared against a table built independently of it.
    """
    # 🔴 The raw handle, not the SQLAlchemy one, and foreign keys off OUTSIDE any
    # transaction -- SQLite ignores that pragma inside one, and the `DROP TABLE`
    # below is refused while four tables reference the table being dropped. It
    # is the same constraint the migration runner works around for the migration
    # this rewind undoes.
    with writer(config) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(_ACCOUNTS_BEFORE_THE_MIGRATION)
        conn.execute(f"INSERT INTO accounts_before_006 SELECT {_ACCOUNT_COLUMNS} FROM accounts")
        conn.execute("DROP TABLE accounts")
        conn.execute("ALTER TABLE accounts_before_006 RENAME TO accounts")
        conn.execute(
            "CREATE UNIQUE INDEX accounts_source_identity ON accounts "
            "(connection_id, source_account_id) WHERE source_account_id IS NOT NULL"
        )
        conn.execute("CREATE INDEX accounts_by_institution ON accounts (institution_id)")
        conn.execute(
            "DELETE FROM schema_version WHERE version = ?",
            (SCHEMA_BEFORE_THE_NULLABLE_CURRENCY + 1,),
        )
        conn.execute("PRAGMA foreign_keys = ON")


#: The columns of `accounts`, in the order the file holds them. Named on both
#: sides of the copy above rather than left to `SELECT *`, for the reason the
#: migration names: a positional copy is correct until the two lists stop
#: agreeing, and then it transposes two TEXT columns in silence.
_ACCOUNT_COLUMNS = (
    "account_id, institution_id, connection_id, source_account_id, "
    "source_persistent_account_id, name, official_name, mask, account_type, "
    "account_subtype, balance_class, currency, lifecycle_status, opened_date, "
    "first_seen_date, closed_date, source, created_at, updated_at, last_seen_date"
)

#: `accounts` as the build before migration 006 declared it: `currency NOT NULL`,
#: and every other constraint migrations 002 and 003 put on the table.
_ACCOUNTS_BEFORE_THE_MIGRATION = """
    CREATE TABLE accounts_before_006 (
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
        currency                     TEXT NOT NULL,
        lifecycle_status             TEXT NOT NULL
                                     CHECK (lifecycle_status IN ('active', 'inactive')),
        opened_date                  TEXT CHECK (opened_date GLOB
                                     '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
        first_seen_date              TEXT NOT NULL CHECK (first_seen_date GLOB
                                     '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
        closed_date                  TEXT CHECK (closed_date GLOB
                                     '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
        source                       TEXT NOT NULL
                                     CHECK (source IN ('aggregator', 'manual')),
        created_at                   TEXT NOT NULL CHECK (created_at LIKE '%+00:00'),
        updated_at                   TEXT NOT NULL CHECK (updated_at LIKE '%+00:00'),
        last_seen_date               TEXT CHECK (last_seen_date GLOB
                                     '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
        CHECK ((source = 'aggregator') <= (source_account_id IS NOT NULL)),
        CHECK (closed_date IS NULL OR closed_date >= first_seen_date)
    )
"""


# --------------------------------------------------------------------------
# Reading the file at whatever version it happens to be at.
# --------------------------------------------------------------------------

#: One table as the file holds it: the column names the database itself reports,
#: and every row under them.
TableDump = tuple[tuple[str, ...], list[tuple[Any, ...]]]


def dump_every_table(config: Config) -> dict[str, TableDump]:
    """Every row of every table, read from the file rather than from the metadata.

    `store/schema.py` describes the version this build serves, so asking it what
    the columns are would ask about a different database than the one on disk.
    The claim under test is about the file, before and after the migration runs
    over it, which is why the shape comes from `PRAGMA table_info`.

    Rows come back sorted, because a migration is free to change the order
    SQLite hands them back and no criterion here is about that order.
    """
    dumped: dict[str, TableDump] = {}
    with reader(config, require_supported_schema=False) as conn:
        names = [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        for name in names:
            columns = tuple(
                str(row[1]) for row in conn.execute(f'PRAGMA table_info("{name}")').fetchall()
            )
            selected = ", ".join(f'"{column}"' for column in columns)
            rows = conn.execute(f'SELECT {selected} FROM "{name}"').fetchall()
            dumped[name] = (columns, sorted((tuple(row) for row in rows), key=repr))
    return dumped


def required_columns(config: Config, table_name: str) -> set[str]:
    """The columns the file itself refuses a NULL in, whatever the metadata says.

    Read from `PRAGMA table_info` for the same reason `dump_every_table` is: the
    claim under test is about the database on disk, and `store/schema.py`
    describes the version this build serves rather than the one the file is at.
    """
    with reader(config, require_supported_schema=False) as conn:
        return {
            str(row[1])
            for row in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
            if row[3]
        }


# --------------------------------------------------------------------------
# The upgrade itself.
# --------------------------------------------------------------------------


def _assert_only_the_new_column_moved(
    before: dict[str, TableDump],
    after: dict[str, TableDump],
    added: dict[str, tuple[str, ...]] | None = None,
) -> None:
    """Every table came through unchanged, but for the columns 008 appends.

    🔴 The extended table is compared value by value rather than skipped. A
    migration that reached past its own DDL is exactly what this module exists to
    catch, and exempting the table it touches would exempt the only place it
    could have happened. So the rows are compared with the new column stripped
    back off, and the new column is asserted EMPTY -- `ALTER TABLE ... ADD
    COLUMN` cannot compute a per-row value, so anything else there came from
    somewhere that had no business writing it.
    """
    # 🔴 Named per test rather than read off one module constant. A fixture that
    # crosses ONE migration widens one table; the older fixture crosses three and
    # widens two, and a helper that assumed the newest migration's table would
    # have exempted `transactions` from comparison entirely on that run -- which
    # is the one table those extra migrations could have damaged.
    widened = {THE_TABLE_IT_EXTENDS: THE_COLUMNS_IT_ADDS} if added is None else added
    assert set(after) == set(before), "the migration added or removed a table"
    for name in sorted(set(before) - {SCHEMA_VERSION_TABLE} - set(widened)):
        assert after[name] == before[name], f"{name} did not survive the upgrade unchanged"

    for name, columns in sorted(widened.items()):
        columns_before, rows_before = before[name]
        columns_after, rows_after = after[name]
        count = len(columns)
        assert columns_after == (*columns_before, *columns), (
            f"{name} did not gain exactly the columns declared for it, and the metadata drift "
            f"guard compares this database column-by-column in order"
        )
        assert sorted((row[:-count] for row in rows_after), key=repr) == rows_before, (
            f"a {name} row these migrations were only meant to widen came out different"
        )
        assert all(value is None for row in rows_after for value in row[-count:]), (
            f"a new {name} column arrived populated, which no ADD COLUMN can do -- so a null "
            f"there does not mean 'predates the migration' and the read path is wrong about it"
        )


def test_migrating_a_populated_store_forward_keeps_every_row_it_already_held(
    populated_at_the_previous_version: Config,
) -> None:
    """The operation an operator performs, asserted row by row.

    A migration that dropped, duplicated or rewrote a row is invisible to every
    other test in this tree, because they all run the chain over a store with
    nothing in it. Here the whole file is read before and after, so a step that
    reached past its own DDL has nowhere to hide.

    🔴 **The new column comes out EMPTY on every row, and that is the assertion
    the read path depends on.** `lineage_id` is nullable because no `ALTER TABLE
    ... ADD COLUMN` can name the Item that produced each row, and the read path
    reads a null as *this row predates the split* -- never as *it belongs to the
    current Item*. A migration that put anything there would make that reading
    false and would silently exclude rows from totals.
    """
    before = dump_every_table(populated_at_the_previous_version)

    applied = migrate(populated_at_the_previous_version)

    assert applied == [THE_PENDING_MIGRATION]
    after = dump_every_table(populated_at_the_previous_version)
    _assert_only_the_new_column_moved(before, after)
    assert after[SCHEMA_VERSION_TABLE][1][-1][0] == THE_PENDING_MIGRATION
    assert len(after[SCHEMA_VERSION_TABLE][1]) == len(before[SCHEMA_VERSION_TABLE][1]) + 1


# --------------------------------------------------------------------------
# The store two migrations back -- the one migration 006's table rebuild runs
# over, kept when this module re-pointed at 007.
# --------------------------------------------------------------------------


def test_migrating_a_populated_store_across_the_table_rebuild_keeps_every_row(
    populated_before_the_nullable_currency: Config,
) -> None:
    """The same claim over the migration that REPLACES a table rather than extending one.

    🔴 **Every table, `accounts` included, and that is the point on migration
    006.** It drops `accounts` and swaps a replacement in with foreign-key
    enforcement suspended, so the failures it can produce are a row not copied,
    two TEXT columns transposed, and a child row left pointing at a parent that
    is gone. None of those shows up anywhere else: the store this runs over is
    the only populated one that crosses that step.
    """
    before = dump_every_table(populated_before_the_nullable_currency)

    applied = migrate(populated_before_the_nullable_currency)

    assert applied == THE_PENDING_MIGRATIONS
    after = dump_every_table(populated_before_the_nullable_currency)
    _assert_only_the_new_column_moved(
        before,
        after,
        added={
            transactions.name: (
                THE_COLUMN_THE_LINEAGE_MIGRATION_ADDS,
                *THE_COLUMNS_IT_ADDS,
            ),
            connections.name: THE_COLUMNS_THE_ITEM_MIGRATION_ADDS,
        },
    )
    assert after[SCHEMA_VERSION_TABLE][1][-1][0] == THE_PENDING_MIGRATION
    assert len(after[SCHEMA_VERSION_TABLE][1]) == len(before[SCHEMA_VERSION_TABLE][1]) + len(
        THE_PENDING_MIGRATIONS
    )


def test_the_migration_widens_the_column_and_rewrites_no_value(
    populated_before_the_nullable_currency: Config,
) -> None:
    """🔴 The upgrade window, asserted over a table that was replaced wholesale.

    A rebuild is the shape of migration that usually means data moved. Here it
    must not: every account in every datastore today has a currency -- the NOT
    NULL would not have accepted one without -- so the copy is the identity and
    this is a schema-only change. What has to hold is that the file refuses one
    fewer thing than it did, and that nothing else about the table moved: same
    columns, same order, same values, same indexes.

    🔴 **Column ORDER, because `store/schema.py`'s metadata is compared to this
    database column-by-column in order.** A replacement table that listed the
    columns in a better arrangement would be a correct database that a correct
    description disagrees with, and the drift guard is where that surfaces --
    somewhere else entirely from the migration that caused it.
    """
    columns_before, rows_before = dump_every_table(populated_before_the_nullable_currency)[
        THE_TABLE_IT_REBUILDS
    ]
    assert rows_before, "the fixture seeded no rows, so this asserts nothing about an upgrade"
    assert THE_COLUMN_IT_WIDENS in required_columns(
        populated_before_the_nullable_currency, THE_TABLE_IT_REBUILDS
    )

    migrate(populated_before_the_nullable_currency)

    columns_after, rows_after = dump_every_table(populated_before_the_nullable_currency)[
        THE_TABLE_IT_REBUILDS
    ]
    assert columns_after == columns_before, (
        "the rebuilt table renamed or reordered a column, so the metadata drift guard now "
        "disagrees with a database that is otherwise correct"
    )
    assert rows_after == rows_before, "the rebuild rewrote a row it was only meant to carry across"
    still_required = required_columns(populated_before_the_nullable_currency, THE_TABLE_IT_REBUILDS)
    assert THE_COLUMN_IT_WIDENS not in still_required, "the column is still NOT NULL"
    assert still_required == {
        column for column in columns_before if column not in {THE_COLUMN_IT_WIDENS}
    } & (still_required | {THE_COLUMN_IT_WIDENS}), "a column stopped being required"


def test_the_rebuilt_table_keeps_the_indexes_and_checks_it_hung_from(
    populated_before_the_nullable_currency: Config,
) -> None:
    """🔴 What a table rebuild loses silently, asserted because nothing else would.

    Indexes and table CHECKs belong to the table, so `DROP TABLE` takes them
    with it and the replacement has to declare them again. Neither loss shows up
    in a column list or a row dump: a missing index makes queries slower and
    lets a duplicate account through, and a missing CHECK is invisible until the
    row it would have refused arrives -- an aggregator account with no source id,
    or a closed date before the account was first seen.
    """
    migrate(populated_before_the_nullable_currency)

    with reader_connection(populated_before_the_nullable_currency) as conn:
        indexes = {
            str(row[0])
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'accounts' "
                "AND sql IS NOT NULL"
            ).fetchall()
        }
    assert indexes == {"accounts_source_identity", "accounts_by_institution"}

    with (
        writer_connection(populated_before_the_nullable_currency) as conn,
        pytest.raises(Exception, match="CHECK constraint failed"),
    ):
        conn.exec_driver_sql(
            "INSERT INTO accounts (institution_id, name, account_type, balance_class, "
            "lifecycle_status, first_seen_date, closed_date, source, created_at, updated_at) "
            "VALUES (1, 'n', 'depository', 'asset', 'active', '2026-09-07', '2026-09-01', "
            "'manual', '2026-09-07T12:30:00+00:00', '2026-09-07T12:30:00+00:00')"
        )


def test_the_upgraded_store_holds_the_account_the_old_one_could_not(
    populated_before_the_nullable_currency: Config,
) -> None:
    """🔴 The behaviour the widening buys, asserted rather than inferred.

    An account whose first balance names neither currency field could not be
    written at all while the column was NOT NULL, so it was skipped -- invisible
    to `list_accounts`, with every transaction on it refusing to derive until
    some later sync happened to state a unit. After the upgrade the row exists
    and says what is true of it: the aggregator has not told us yet.

    Written through the ordinary writer rather than the deriver, because the
    claim here is about what the FILE will hold. What the deriver does with a
    body that states no currency is `tests/connector/test_derivers.py`'s.
    """
    assert THE_COLUMN_IT_WIDENS in required_columns(
        populated_before_the_nullable_currency, THE_TABLE_IT_REBUILDS
    ), "the column was already nullable, so the upgrade below is not what makes this possible"

    migrate(populated_before_the_nullable_currency)

    with writer_connection(populated_before_the_nullable_currency) as conn:
        conn.execute(
            insert(accounts).values(
                institution_id=1,
                connection_id=1,
                source_account_id="acct-no-currency",
                name="Unknown Unit",
                account_type="depository",
                balance_class="asset",
                currency=None,
                lifecycle_status="active",
                first_seen_date=POSTED_DATE,
                source="aggregator",
                created_at=FETCHED,
                updated_at=FETCHED,
            )
        )
    with reader_connection(populated_before_the_nullable_currency) as conn:
        held = {
            str(row[0]): row[1]
            for row in conn.execute(
                select(accounts.c.source_account_id, accounts.c.currency).where(
                    accounts.c.source_account_id == "acct-no-currency"
                )
            ).all()
        }
    assert held == {"acct-no-currency": None}, (
        "a unit nothing stated came back as something, so the null was defaulted somewhere"
    )


def test_an_upgraded_store_serves_the_values_its_derivers_wrote(
    populated_at_the_previous_version: Config,
) -> None:
    """The rows are still *correct*, not merely still present.

    Byte-equality across the migration says nothing about whether this build can
    still read what it finds: every typed column is text or an integer at rest,
    and the round trip through the type decorators is what turns it back into
    money and dates. So the store is read here through the ordinary reader, and
    the expectations come from the response bodies -- a purchase arrives POSITIVE
    and is stored negative -- rather than from what the deriver produced.
    """
    migrate(populated_at_the_previous_version)

    with reader_connection(populated_at_the_previous_version) as conn:
        held = {
            str(row.source_transaction_id): row.amount_minor
            for row in conn.execute(
                select(transactions.c.source_transaction_id, transactions.c.amount_minor)
            ).all()
        }
        posted = conn.execute(select(transactions.c.posted_date).distinct()).scalars().all()
        balance = conn.execute(
            select(balances_daily.c.current_minor, balances_daily.c.currency)
        ).one()
        account = conn.execute(
            select(accounts.c.source_account_id, accounts.c.currency, accounts.c.last_seen_date)
        ).one()
        stamped = conn.execute(select(derivation_versions.c.version)).scalars().all()

    assert held == {"txn-purchase": PURCHASE_AS_STORED, "txn-refund": REFUND_AS_STORED}
    assert posted == [POSTED_DATE]
    assert balance == (BALANCE_AS_STORED, "USD")
    assert account.source_account_id == SOURCE_ACCOUNT
    assert account.currency == "USD"
    assert account.last_seen_date is None, (
        "a sync body is not a roster read, so it must not leave a record that one happened"
    )
    assert stamped == [DERIVATION_BEFORE_THE_TRANSFER_PAIRS], (
        "the upgrade restamped rows it did not re-derive, so their provenance is now a claim "
        "about logic that never touched them"
    )


# --------------------------------------------------------------------------
# The remedy the upgrade procedure prescribes.
# --------------------------------------------------------------------------


def test_the_prescribed_rebuild_runs_on_the_store_the_upgrade_produced(
    populated_at_the_previous_version: Config,
) -> None:
    """🔴 `operational-spec.md` tells the operator to run this, so it is asserted.

    The upgrade procedure prescribes `bankmachine store rebuild` after the
    migration, because replaying the archive is what closes the window on a
    connection that will never sync again -- the one whose absent accounts matter
    most. **A documented remedy that fails is worse than none**: it spends the
    operator's trust on the way to failing, and this one has failed before,
    rolling back on this very shape of store because the replay moved the content
    digest at an unchanged derivation version.

    The store here is one a real upgrade produced rather than one assembled at
    the current version and edited to look upgraded, and its rows carry the older
    derivation version because the build that wrote them did. That is what makes
    the digest movement a recorded change instead of a refusal, and asserting the
    movement is what stops this test going quiet: a rebuild that changed nothing
    would satisfy the guard without ever reaching it.
    """
    migrate(populated_at_the_previous_version)

    report = rebuild(populated_at_the_previous_version, derivers=ALL_DERIVERS)

    assert report.previous_derivation_versions == (DERIVATION_BEFORE_THE_TRANSFER_PAIRS,)
    assert report.content_changed, (
        "the replay reproduced the upgraded store byte for byte, so the guard this test exists "
        "to exercise was never consulted"
    )
    assert report.change_was_expected, (
        "`store rebuild` would refuse and roll back on a store that just came across the "
        "migration, so the remedy the upgrade procedure prescribes does not run"
    )
    assert report.responses_replayed == 2
    with reader_connection(populated_at_the_previous_version) as conn:
        held = {
            str(row.source_transaction_id): row.amount_minor
            for row in conn.execute(
                select(transactions.c.source_transaction_id, transactions.c.amount_minor)
            ).all()
        }
        balance = conn.execute(select(balances_daily.c.current_minor)).scalar_one()
        stamps = (
            conn.execute(select(transactions.c.derivation_version_id).distinct()).scalars().all()
        )
    assert held == {"txn-purchase": PURCHASE_AS_STORED, "txn-refund": REFUND_AS_STORED}, (
        "the rebuild committed and the amounts moved"
    )
    assert balance == BALANCE_AS_STORED, "the rebuild committed and the balance series moved"
    assert stamps == [report.derivation_version_id]


def test_the_rebuild_stamps_the_ledger_dates_migration_005_could_only_leave_empty(
    populated_at_the_previous_version: Config,
) -> None:
    """🔴 The remedy `transactions.ledger_date` is owed, asserted rather than prescribed.

    That column landed NULLABLE because no `ALTER TABLE ... ADD COLUMN` can
    compute `COALESCE(authorized_date, posted_date)` per row, and every window
    predicate silently EXCLUDES a null one -- so between migration 005 and a
    rebuild, this store answers every windowed question over a subset of its
    transactions and reports the subset as the whole. That is tolerable only
    because the rebuild closes it, and only for as long as the rebuild is known
    to. This is the half that makes the design honest rather than merely
    documented.

    🔴 **The nulls are written here rather than reached through migration 005,
    and that is forced.** This build's transaction deriver stamps `ledger_date`,
    so it cannot write a store predating the column; and the fixture above
    already seeds past 005 on its way to 006. Nulling the column reproduces
    exactly the file state 005 leaves on a populated store -- the column present
    and empty on every row that predates it -- which is the state under test.
    """
    migrate(populated_at_the_previous_version)
    with writer_connection(populated_at_the_previous_version) as conn:
        conn.execute(update(transactions).values(ledger_date=None))

    with reader_connection(populated_at_the_previous_version) as conn:
        before = conn.execute(select(transactions.c.ledger_date)).scalars().all()
    assert before and all(value is None for value in before), (
        "the fixture holds no unstamped row, so the rebuild below has nothing to prove"
    )

    rebuild(populated_at_the_previous_version, derivers=ALL_DERIVERS)

    with reader_connection(populated_at_the_previous_version) as conn:
        stamped = conn.execute(select(transactions.c.ledger_date)).scalars().all()
    assert stamped, "the rebuild removed the rows it was meant to restamp"
    assert all(value is not None for value in stamped), (
        "the rebuild left a transaction with no ledger date, so a window over this store still "
        "excludes it and reports a floor as a measurement"
    )
    # 🔴 The seeded bodies carry `authorized_date` a day BEFORE `date`, so a
    # value equal to the posting date would mean the coalesce fell through --
    # the fallback the column exists to refuse, and the one a rebuild could
    # reintroduce without any test noticing.
    assert set(stamped) == {AUTHORIZED_DATE}, (
        "the rebuild stamped the posting date rather than the day the money was committed"
    )


def test_the_rebuild_pairs_the_transfers_migration_009_could_only_leave_empty(
    populated_at_the_previous_version: Config,
) -> None:
    """🔴 The remedy `transactions.transfer_pair_id` is owed, over a POPULATED store.

    Its two sibling columns each have this test and this one did not, which is
    the gap worth closing rather than the assertion worth adding: the pairing is
    what two of the three flow classes are read from, so a rebuild that failed
    to recompute it would leave every transfer on an upgraded store classified
    as money that left the household — a wrong spending figure on a store that
    had done nothing but upgrade.

    🔴 The seeded bodies carry no transfer pair, so what this proves is the
    weaker and more important half: the rebuild RUNS the pairing pass rather
    than skipping it, and leaves the column in the state the data supports
    rather than in whatever state the migration left.
    """
    migrate(populated_at_the_previous_version)
    with writer_connection(populated_at_the_previous_version) as conn:
        conn.execute(update(transactions).values(transfer_pair_id=999))

    with reader_connection(populated_at_the_previous_version) as conn:
        assert {row for row in conn.execute(select(transactions.c.transfer_pair_id)).scalars()} == {
            999
        }, "the fixture did not stand in a pairing for the rebuild to overwrite"

    rebuild(populated_at_the_previous_version, derivers=ALL_DERIVERS)

    with reader_connection(populated_at_the_previous_version) as conn:
        after = list(conn.execute(select(transactions.c.transfer_pair_id)).scalars())
    assert after, "the rebuild removed the rows it was meant to re-pair"
    assert all(value is None for value in after), (
        "the rebuild left a pairing the data does not support, so an upgraded store classifies "
        "money as an internal transfer on the strength of a value nothing recomputed"
    )


def test_the_rebuild_stamps_the_lineage_migration_007_could_only_leave_empty(
    populated_at_the_previous_version: Config,
) -> None:
    """🔴 The remedy `transactions.lineage_id` is owed, asserted rather than prescribed.

    That column landed NULLABLE because no `ALTER TABLE ... ADD COLUMN` can name
    the aggregator Item that produced each row. Until the rebuild runs, the store
    holds a history it cannot partition -- so if that store has ALREADY been
    through a remove-and-re-link, every duplicated row is counted, the annual
    total is double, and the column that exists to say so is empty on both
    copies. The migration is honest about that only for as long as the rebuild
    that closes it is known to work.

    🔴 **What the rebuild fills it from is the archive, not a guess.**
    `transactions` points at the response it came from and `raw_responses`
    records which connection that response was fetched for, so the Item a row
    belongs to is a fact the archive already holds. This asserts the replay
    recovers it -- and recovers the RIGHT one, which is the seeded connection
    rather than whatever id happened to be first.
    """
    migrate(populated_at_the_previous_version)
    # 🔴 The nulls are written here rather than reached through migration 007,
    # and that is forced: this module tracks the NEWEST migration, so its
    # fixture now seeds past 007 and the column arrives populated. Nulling it
    # reproduces exactly the file state 007 leaves on a populated store, which
    # is the state under test -- and it keeps this assertion working when the
    # module re-points again, rather than quietly becoming unreachable and
    # inviting the next reader to delete it.
    with writer_connection(populated_at_the_previous_version) as conn:
        conn.execute(update(transactions).values(lineage_id=None))

    with reader_connection(populated_at_the_previous_version) as conn:
        before = conn.execute(select(transactions.c.lineage_id)).scalars().all()
    assert before and all(value is None for value in before), (
        "the fixture holds no unstamped row, so the rebuild below has nothing to prove"
    )

    rebuild(populated_at_the_previous_version, derivers=ALL_DERIVERS)

    with reader_connection(populated_at_the_previous_version) as conn:
        stamped = conn.execute(select(transactions.c.lineage_id)).scalars().all()
        enrolled = conn.execute(select(connections.c.connection_id)).scalars().all()
    assert stamped, "the rebuild removed the rows it was meant to restamp"
    assert set(stamped) == set(enrolled), (
        "the rebuild left a transaction with no Item, or attributed one to a connection this "
        "store does not hold -- either way the read path cannot tell one lineage from another"
    )


def test_the_rebuild_does_not_invent_the_roster_observation_the_migration_left_empty(
    populated_at_the_previous_version: Config,
) -> None:
    """🔴 The remedy closes the window from the archive, and from nothing else.

    A rebuild fills `connections.roster_observed_date` by replaying an archived
    roster response. This connection has none -- its rows came from
    `/transactions/sync`, which answers *these are the accounts these
    transactions belong to*, not *this is what the connection has* -- so the
    honest outcome is that the observation stays empty and the connection marks
    nothing absent.

    Inventing one from a sync body is the failure this guards, and it is silent:
    the value would read as a successful observation, and every account the body
    happened not to name would be reported closed on the strength of a roster
    nobody ever read.
    """
    migrate(populated_at_the_previous_version)

    rebuild(populated_at_the_previous_version, derivers=ALL_DERIVERS)

    with reader_connection(populated_at_the_previous_version) as conn:
        observed = conn.execute(select(connections.c.roster_observed_date)).scalars().all()
        seen = conn.execute(select(accounts.c.last_seen_date)).scalars().all()
    assert observed == [None]
    assert seen == [None]
