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

🔴 **`/accounts/get` cannot be seeded this way and is deliberately absent.** Its
deriver writes `connections.roster_observed_date`, and adding that column is what
the pending migration *is*, so this build's accounts deriver cannot run against
any store predating it. `/transactions/sync` carries the same accounts array and
derives the same account and balance rows through the same code, without the
roster observation -- which is the honest shape anyway, because a store that
crossed this migration holds no record of a roster having been read.
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
from bankmachine.store import connection as store_connection
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
#: build that served it stamped its rows with.
#:
#: 🔴 Fixed historical numbers, deliberately NOT written as
#: `SUPPORTED_SCHEMA_VERSION - 1`. Written relatively they move with the
#: constants, so the seeded store and the running build could never be at
#: different versions and every assertion below would hold whether or not the
#: migration did anything at all.
SCHEMA_BEFORE_THE_ROSTER_OBSERVATION = 3
DERIVATION_BEFORE_THE_ROSTER_OBSERVATION = 2

#: What that derivation version meant, so the seeded `derivation_versions` row
#: says something true about the rows hanging off it rather than describing a
#: roster observation those rows predate.
DESCRIPTION_BEFORE_THE_ROSTER_OBSERVATION = (
    "institutions and accounts derived from the aggregator; balances signed from the "
    "operator's point of view"
)

#: The migration the seeded store is missing, and the column it adds.
THE_PENDING_MIGRATION = SCHEMA_BEFORE_THE_ROSTER_OBSERVATION + 1
THE_COLUMN_IT_ADDS = "roster_observed_date"

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
        "authorized_date": "2026-09-06",
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

    The two constants stood in are the whole of what "the previous build" means
    here: which schema version its writer would open, and which derivation
    version it stamped rows with. Every row below is written by the shipped path
    -- `apply_response`, the shipped registry, the shipped derivers -- so what
    lands in the file is what that build really wrote.
    """
    set_datastore_key(config, generate_datastore_key())
    pending = MIGRATIONS[:SCHEMA_BEFORE_THE_ROSTER_OBSERVATION]
    assert migrate(config, migrations=pending) == [step.version for step in pending]

    with monkeypatch.context() as previous_build:
        previous_build.setattr(
            store_connection, "SUPPORTED_SCHEMA_VERSION", SCHEMA_BEFORE_THE_ROSTER_OBSERVATION
        )
        previous_build.setattr(
            derivation, "DERIVATION_VERSION", DERIVATION_BEFORE_THE_ROSTER_OBSERVATION
        )
        previous_build.setattr(
            derivation, "DERIVATION_DESCRIPTION", DESCRIPTION_BEFORE_THE_ROSTER_OBSERVATION
        )
        _enroll(config)
        _archive_and_derive(config, ITEM_GET.path, _item_body())
        _archive_and_derive(config, TRANSACTIONS_SYNC.path, _sync_body())

    seeded = dump_every_table(config)
    empty = [name for name in POPULATED_BY_THE_SEEDING if not seeded[name][1]]
    assert not empty, f"the derivers wrote nothing into {empty}, so nothing here is under test"
    return config


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
    for name in sorted(set(before) - {SCHEMA_VERSION_TABLE, connections.name}):
        assert after[name] == before[name], f"{name} did not survive the upgrade unchanged"
    assert after[SCHEMA_VERSION_TABLE][1][-1][0] == THE_PENDING_MIGRATION
    assert len(after[SCHEMA_VERSION_TABLE][1]) == len(before[SCHEMA_VERSION_TABLE][1]) + 1


def test_the_migration_leaves_its_new_column_empty_on_the_rows_it_found(
    populated_at_the_previous_version: Config,
) -> None:
    """🔴 The upgrade window, asserted over a connection row that predates the column.

    A null in `connections.roster_observed_date` means *this connection's roster
    has never been observed*, and `query._account_lifecycle` reads it as exactly
    that -- so a migration that invented a date would mark accounts absent that
    nobody ever failed to see. There is nothing honest to backfill with: the
    correct value is when the roster was last read, which is the record nothing
    ever kept.

    🔴 **What this pins that a column DEFAULT cannot.** A row inserted after the
    migration takes the DEFAULT; a row that was already in the table takes
    whatever the migration did to it. Only the second is the operator's
    situation, and only a connection row written before the column existed can
    tell the two apart.
    """
    columns_before, rows_before = dump_every_table(populated_at_the_previous_version)[
        connections.name
    ]
    assert THE_COLUMN_IT_ADDS not in columns_before

    migrate(populated_at_the_previous_version)

    columns_after, rows_after = dump_every_table(populated_at_the_previous_version)[
        connections.name
    ]
    assert columns_after == (*columns_before, THE_COLUMN_IT_ADDS), (
        "the column must land at the END of the table, where `ALTER TABLE ... ADD COLUMN` puts "
        "it and where store/schema.py's metadata declares it"
    )
    widened = sorted((row[: len(columns_before)] for row in rows_after), key=repr)
    assert widened == rows_before, "the migration rewrote a connection row it was only widening"
    assert [row[-1] for row in rows_after] == [None] * len(rows_after), (
        "the migration invented a roster observation for a connection whose roster nobody read"
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
    assert stamped == [DERIVATION_BEFORE_THE_ROSTER_OBSERVATION], (
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

    assert report.previous_derivation_versions == (DERIVATION_BEFORE_THE_ROSTER_OBSERVATION,)
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
