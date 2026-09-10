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
to run. It was first written against migration 004 and moved to 005 when that
landed.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import insert, select

from bankmachine.config import Config
from bankmachine.connector import ITEM_GET, TRANSACTIONS_SYNC
from bankmachine.derivers import ALL_DERIVERS
from bankmachine.secrets import generate_datastore_key, set_datastore_key
from bankmachine.store import derivation
from bankmachine.store.connection import reader
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
SCHEMA_BEFORE_THE_LEDGER_DATE = 4
DERIVATION_BEFORE_THE_LEDGER_DATE = 3

#: What that derivation version meant, so the seeded `derivation_versions` row
#: says something true about the rows hanging off it rather than describing a
#: roster observation those rows predate.
DESCRIPTION_BEFORE_THE_LEDGER_DATE = (
    "institutions and accounts derived from the aggregator; balances signed from the "
    "operator's point of view; the roster observation recorded per account "
    "(`accounts.last_seen_date`) and per connection (`connections.roster_observed_date`)"
)

#: The migration the seeded store is missing, and the column it adds.
THE_PENDING_MIGRATION = SCHEMA_BEFORE_THE_LEDGER_DATE + 1
THE_COLUMN_IT_ADDS = "ledger_date"

#: The table that column lands on.
THE_TABLE_IT_LANDS_ON = transactions.name

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
    that build has, so this build's transaction deriver stamps `ledger_date` and
    cannot run against any store predating it. Seeding first and removing the
    column afterwards is what lets the rows be written by the shipped path --
    `apply_response`, the shipped registry, the shipped derivers -- which is the
    property this whole module rests on. A hand-written row would satisfy a
    migration a derived row would not.

    What comes out is a file shaped exactly like one the previous build left: the
    column absent, `schema_version` topping out one short, and every row carrying
    the derivation version that build stamped.

    🔴 The derivation version and its description are stood in during seeding
    rather than rewritten afterwards, because they are what the rows POINT AT.
    Rewriting them after the fact would leave the row and its description written
    by different hands, which is the drift this fixture exists to avoid.
    """
    set_datastore_key(config, generate_datastore_key())
    assert migrate(config) == [step.version for step in MIGRATIONS]

    with monkeypatch.context() as previous_build:
        previous_build.setattr(
            derivation, "DERIVATION_VERSION", DERIVATION_BEFORE_THE_LEDGER_DATE
        )
        previous_build.setattr(
            derivation, "DERIVATION_DESCRIPTION", DESCRIPTION_BEFORE_THE_LEDGER_DATE
        )
        _enroll(config)
        _archive_and_derive(config, ITEM_GET.path, _item_body())
        _archive_and_derive(config, TRANSACTIONS_SYNC.path, _sync_body())

    _rewind_past_the_newest_migration(config)

    seeded = dump_every_table(config)
    empty = [name for name in POPULATED_BY_THE_SEEDING if not seeded[name][1]]
    assert not empty, f"the derivers wrote nothing into {empty}, so nothing here is under test"
    assert THE_COLUMN_IT_ADDS not in seeded[THE_TABLE_IT_LANDS_ON][0], (
        "the rewind left the new column in place, so the migration under test has nothing to add "
        "and every assertion below would hold against a store that never moved"
    )
    return config


def _rewind_past_the_newest_migration(config: Config) -> None:
    """Take the seeded store back to the version before this build's last migration.

    Removes exactly what that migration added -- its column, and its row in
    `schema_version` -- so the file reports the older version and a reader of it
    cannot tell it from a store that never crossed the migration at all.

    🔴 The store is still at the current version while this runs, which is why an
    ordinary writer may open it. The rewind is what makes it unsupported, so it
    is the last thing done to the file.
    """
    with writer_connection(config) as conn:
        conn.exec_driver_sql(
            f"ALTER TABLE {THE_TABLE_IT_LANDS_ON} DROP COLUMN {THE_COLUMN_IT_ADDS}"
        )
        conn.exec_driver_sql(
            "DELETE FROM schema_version WHERE version = :v", {"v": THE_PENDING_MIGRATION}
        )


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


# --------------------------------------------------------------------------
# The upgrade itself.
# --------------------------------------------------------------------------


def test_migrating_a_populated_store_forward_keeps_every_row_it_already_held(
    populated_at_the_previous_version: Config,
) -> None:
    """The operation an operator performs, asserted row by row.

    A migration that dropped, duplicated or rewrote a row is invisible to every
    other test in this tree, because they all run the chain over a store with
    nothing in it. Here the whole file is read before and after, so a step that
    reached past its own DDL has nowhere to hide.

    `connections` is compared separately below, because it is the one table the
    migration is allowed to change the shape of.
    """
    before = dump_every_table(populated_at_the_previous_version)

    applied = migrate(populated_at_the_previous_version)

    assert applied == [THE_PENDING_MIGRATION]
    after = dump_every_table(populated_at_the_previous_version)
    assert set(after) == set(before), "the migration added or removed a table"
    for name in sorted(set(before) - {SCHEMA_VERSION_TABLE, THE_TABLE_IT_LANDS_ON}):
        assert after[name] == before[name], f"{name} did not survive the upgrade unchanged"
    assert after[SCHEMA_VERSION_TABLE][1][-1][0] == THE_PENDING_MIGRATION
    assert len(after[SCHEMA_VERSION_TABLE][1]) == len(before[SCHEMA_VERSION_TABLE][1]) + 1


def test_the_migration_leaves_its_new_column_empty_on_the_rows_it_found(
    populated_at_the_previous_version: Config,
) -> None:
    """🔴 The upgrade window, asserted over rows that predate the column.

    A null in `transactions.ledger_date` means *this row predates the split and
    the store has not been rebuilt*. It must never be read as "the money was
    committed on the posting date", because that is the very number the column
    exists to stop being wrong -- so the migration may not invent one, and a
    constant DEFAULT would be exactly that. There is nothing honest to backfill
    with in DDL: the correct value is `COALESCE(authorized_date, posted_date)`
    per row, which `ALTER TABLE ... ADD COLUMN` cannot compute.

    🔴 **What this pins that a column DEFAULT cannot.** A row inserted after the
    migration takes the DEFAULT; a row that was already in the table takes
    whatever the migration did to it. Only the second is the operator's
    situation, and only a row written before the column existed can tell the two
    apart.
    """
    columns_before, rows_before = dump_every_table(populated_at_the_previous_version)[
        THE_TABLE_IT_LANDS_ON
    ]
    assert THE_COLUMN_IT_ADDS not in columns_before
    assert rows_before, "the fixture seeded no rows, so this asserts nothing about an upgrade"

    migrate(populated_at_the_previous_version)

    columns_after, rows_after = dump_every_table(populated_at_the_previous_version)[
        THE_TABLE_IT_LANDS_ON
    ]
    assert columns_after == (*columns_before, THE_COLUMN_IT_ADDS), (
        "the column must land at the END of the table, where `ALTER TABLE ... ADD COLUMN` puts "
        "it and where store/schema.py's metadata declares it"
    )
    widened = sorted((row[: len(columns_before)] for row in rows_after), key=repr)
    assert widened == rows_before, "the migration rewrote a transaction row it was only widening"
    assert [row[-1] for row in rows_after] == [None] * len(rows_after), (
        "the migration stamped a ledger date it had no per-row expression to compute, so some "
        "row now claims a commitment date nothing derived"
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
    assert stamped == [DERIVATION_BEFORE_THE_LEDGER_DATE], (
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

    assert report.previous_derivation_versions == (DERIVATION_BEFORE_THE_LEDGER_DATE,)
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


def test_the_rebuild_stamps_the_column_the_migration_could_only_leave_empty(
    populated_at_the_previous_version: Config,
) -> None:
    """🔴 The remedy the nullable column is owed, asserted rather than prescribed.

    `ledger_date` lands nullable because no DDL can compute
    `COALESCE(authorized_date, posted_date)` per row, and every windowed total
    silently EXCLUDES a null one -- so between the migration and the rebuild this
    store answers every window over a subset of its transactions. That is
    tolerable only because the rebuild closes it, and only for as long as the
    rebuild is known to.

    So this is the half that makes the design honest: after the prescribed
    rebuild, no transaction is left unstamped, and the value is the authorized
    date where the source reported one -- not the posting date, which is the
    fallback the whole column exists to refuse.
    """
    migrate(populated_at_the_previous_version)

    with reader_connection(populated_at_the_previous_version) as conn:
        before = conn.execute(select(transactions.c.ledger_date)).scalars().all()
    assert before and all(value is None for value in before), (
        "the migration stamped a ledger date, so the rebuild below is not what fills it"
    )

    rebuild(populated_at_the_previous_version, derivers=ALL_DERIVERS)

    with reader_connection(populated_at_the_previous_version) as conn:
        stamped = conn.execute(
            select(transactions.c.source_transaction_id, transactions.c.ledger_date)
        ).all()
    assert stamped, "the rebuild removed the rows it was meant to restamp"
    assert all(row[1] is not None for row in stamped), (
        "the rebuild left a transaction with no ledger date, so a window over this store still "
        "excludes it and reports a floor as a measurement"
    )
    # The seeded bodies carry `authorized_date` a day before `date`, so a value
    # equal to the posting date would mean the coalesce fell through -- the exact
    # fallback that reinstates the defect.
    assert {row[1] for row in stamped} == {AUTHORIZED_DATE}, (
        "the rebuild stamped the posting date rather than the date the money was committed"
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
