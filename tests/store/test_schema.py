"""The core schema (FR-6): thirteen tables, and the guarantees built into them.

This is the plan's lock-in chunk, so the tests are weighted toward the things
that are expensive to discover later: that the metadata and the frozen DDL still
describe the same database, that the typed columns survive a round trip through
SQLCipher, and that the requirements written into CHECK constraints and partial
unique indexes actually refuse the rows they are supposed to refuse.

The negative cases go through raw SQL on purpose. Every one of them is already
prevented by the typed column in front of it, and asserting only that would test
the type decorator twice and the database not at all -- while `sync shell` ships
an operator a prompt that reaches the file with no type decorator anywhere in
between.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import Connection as SAConnection
from sqlalchemy import Table, func, insert, select, text, update
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.exc import IntegrityError

from bankmachine.config import Config
from bankmachine.store.connection import (
    SUPPORTED_SCHEMA_VERSION,
    Connection,
    StoreError,
)
from bankmachine.store.engine import writer_engine
from bankmachine.store.migrations import MIGRATIONS, Migration, migrate
from bankmachine.store.migrations.core_schema import CORE_SCHEMA_DDL, CORE_SCHEMA_DDL_SHA256
from bankmachine.store.rebuild import derived_tables
from bankmachine.store.schema import (
    CORE_TABLES,
    LATER_TABLES,
    PROVENANCE_SOURCES,
    account_rules,
    accounts,
    balances_daily,
    connections,
    derivation_versions,
    holdings,
    institutions,
    investment_transactions,
    manual_imports,
    metadata,
    raw_responses,
    refused_holdings,
    securities,
    sync_state,
    transactions,
)
from bankmachine.store.types import calendar_date, minor_units, now_utc

_DIALECT = sqlite_dialect.dialect()

TODAY = calendar_date(date(2026, 9, 6))
YESTERDAY = calendar_date(date(2026, 9, 5))


@pytest.fixture
def writer(initialized_config: Config) -> Iterator[SAConnection]:
    """One writable handle for a test, taken through the factory that locks."""
    with writer_engine(initialized_config) as engine, engine.connect() as conn:
        yield conn


@dataclass(frozen=True, slots=True)
class Seed:
    """One row in each table a normalized row needs to point at."""

    derivation_version_id: int
    institution_id: int
    connection_id: int
    account_id: int
    raw_response_id: int
    manual_import_id: int
    security_id: int


def _one_id(conn: SAConnection, table: Table, **values: object) -> int:
    result = conn.execute(insert(table).values(**values))
    primary_key = result.inserted_primary_key
    assert primary_key is not None
    return int(primary_key[0])


def seed(conn: SAConnection) -> Seed:
    now = now_utc()
    derivation_version_id = _one_id(
        conn,
        derivation_versions,
        version=1,
        description="test derivation",
        first_used_at=now,
    )
    institution_id = _one_id(
        conn,
        institutions,
        source_institution_id="src-inst-1",
        name="An Institution",
        first_seen_at=now,
        last_seen_at=now,
    )
    connection_id = _one_id(
        conn,
        connections,
        institution_id=institution_id,
        source_connection_id="src-conn-1",
        credential_ref="connection:src-conn-1",
        capabilities='["transactions"]',
        requested_history_days=730,
        granted_history_days=365,
        status="active",
        last_success_at=now,
        enrolled_at=now,
        created_at=now,
        updated_at=now,
    )
    account_id = _one_id(
        conn,
        accounts,
        institution_id=institution_id,
        connection_id=connection_id,
        source_account_id="src-acct-1",
        source_persistent_account_id="persistent-1",
        name="An Account",
        mask="0000",
        account_type="depository",
        account_subtype="checking",
        balance_class="asset",
        currency="USD",
        lifecycle_status="active",
        first_seen_date=YESTERDAY,
        source="aggregator",
        created_at=now,
        updated_at=now,
    )
    raw_response_id = _one_id(
        conn,
        raw_responses,
        connection_id=connection_id,
        endpoint="/transactions/sync",
        received_at=now,
        body_gzip=b"\x1f\x8b not really gzip",
        body_sha256="0" * 64,
        body_bytes=17,
    )
    manual_import_id = _one_id(
        conn,
        manual_imports,
        account_id=account_id,
        adapter="csv-v1",
        source_name="export.csv",
        file_sha256="1" * 64,
        file_bytes=42,
        imported_at=now,
        rows_seen=3,
        rows_applied=3,
    )
    security_id = _one_id(
        conn,
        securities,
        source_security_id="src-sec-1",
        name="A Fund",
        ticker="AAAA",
        security_type="etf",
        currency="USD",
        close_price_minor=minor_units(10_150),
        close_price_as_of=TODAY,
        derivation_version_id=derivation_version_id,
        created_at=now,
        updated_at=now,
    )
    return Seed(
        derivation_version_id=derivation_version_id,
        institution_id=institution_id,
        connection_id=connection_id,
        account_id=account_id,
        raw_response_id=raw_response_id,
        manual_import_id=manual_import_id,
        security_id=security_id,
    )


def add_transaction(conn: SAConnection, s: Seed, **overrides: object) -> int:
    now = now_utc()
    values: dict[str, object] = {
        "account_id": s.account_id,
        "source_transaction_id": "src-txn-1",
        "pending": 0,
        "posted_date": TODAY,
        "amount_minor": minor_units(-1234),
        "currency": "USD",
        "description": "a purchase",
        "merchant_name": "A Merchant",
        "source_category_primary": "FOOD_AND_DRINK",
        "source_category_detailed": "FOOD_AND_DRINK_COFFEE",
        "source": "aggregator",
        "raw_response_id": s.raw_response_id,
        "derivation_version_id": s.derivation_version_id,
        "first_seen_at": now,
        "updated_at": now,
    }
    values.update(overrides)
    return _one_id(conn, transactions, **values)


# --------------------------------------------------------------------------
# The schema is what the migration built, and what the metadata describes
# --------------------------------------------------------------------------


def _database_columns(conn: SAConnection, table_name: str) -> list[tuple[str, str, bool, bool]]:
    rows = conn.execute(text(f"PRAGMA table_info('{table_name}')")).fetchall()
    return [
        (row.name, str(row.type).upper(), bool(row.notnull) or bool(row.pk), bool(row.pk))
        for row in rows
    ]


def _metadata_columns(table: Table) -> list[tuple[str, str, bool, bool]]:
    return [
        (
            column.name,
            column.type.compile(_DIALECT).upper(),
            (not column.nullable) or column.primary_key,
            column.primary_key,
        )
        for column in table.columns
    ]


def test_the_thirteen_core_tables_exist_at_the_declared_schema_version(
    writer: SAConnection,
) -> None:
    present = {
        row.name
        for row in writer.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'table'")
        ).fetchall()
    }
    assert set(CORE_TABLES) <= present
    assert len(CORE_TABLES) == 13
    assert set(LATER_TABLES) <= present
    version = writer.execute(text("SELECT MAX(version) FROM schema_version")).scalar_one()
    assert version == SUPPORTED_SCHEMA_VERSION


def test_the_metadata_names_exactly_the_tables_the_requirement_lists(writer: SAConnection) -> None:
    """A table added to the metadata alone is a contract widened without a migration.

    FR-6's list is a minimum, so a later migration may add a table beside it --
    and `LATER_TABLES` is where one is declared. A table neither literal names is
    still a failure here.
    """
    assert set(metadata.tables) == set(CORE_TABLES) | set(LATER_TABLES)


def test_the_metadata_matches_the_migrated_database(writer: SAConnection) -> None:
    """The drift guard, and the reason the DDL is written by hand twice.

    `store/schema.py` is what every query is built against; `core_schema.py` is
    what the file actually contains. Nothing generates one from the other, so
    they can disagree -- and this is where they are made to say so, column by
    column, rather than in a query that silently returns nothing six chunks
    from now.
    """
    for name in (*CORE_TABLES, *LATER_TABLES):
        assert _metadata_columns(metadata.tables[name]) == _database_columns(writer, name), (
            f"{name}: store/schema.py and the migrations describe different tables"
        )


#: (name, table, unique, partial predicate or None, columns)
IndexShape = tuple[str, str, bool, str | None, tuple[str, ...]]


def _normalized_predicate(predicate: str | None, table_name: str) -> str | None:
    """Both sides' spelling of the same condition, reduced to one form.

    The two are written independently -- the DDL says `retired_at IS NULL` and
    SQLAlchemy compiles `connections.retired_at IS NULL` -- so the comparison
    needs the qualifier, the whitespace and the case out of the way. Nothing else
    is normalized: an inverted or rewritten condition must still differ, because
    telling those apart is the entire point of comparing the text rather than
    asking whether a predicate exists.
    """
    if predicate is None:
        return None
    collapsed = " ".join(predicate.split()).lower()
    return collapsed.replace(f"{table_name.lower()}.", "").strip("() ")


def _database_indexes(conn: SAConnection) -> set[IndexShape]:
    found: set[IndexShape] = set()
    for table_name in (*CORE_TABLES, *LATER_TABLES):
        for index in conn.execute(text(f"PRAGMA index_list('{table_name}')")).fetchall():
            if index.origin != "c":  # 'u' and 'pk' are constraint autoindexes
                continue
            columns = tuple(
                str(part.name)
                for part in conn.execute(text(f"PRAGMA index_info('{index.name}')")).fetchall()
            )
            predicate = None
            if index.partial:
                # `PRAGMA index_list` says only *that* an index is partial. What
                # it is partial ON is in the statement that created it.
                sql = conn.execute(
                    text("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = :name"),
                    {"name": str(index.name)},
                ).scalar_one()
                predicate = re.split(r"\bWHERE\b", str(sql), maxsplit=1, flags=re.IGNORECASE)[1]
            found.add(
                (
                    str(index.name),
                    table_name,
                    bool(index.unique),
                    _normalized_predicate(predicate, table_name),
                    columns,
                )
            )
    return found


def _metadata_indexes() -> set[IndexShape]:
    shapes: set[IndexShape] = set()
    for table in metadata.tables.values():
        for index in table.indexes:
            where = index.dialect_kwargs.get("sqlite_where")
            predicate = (
                None
                if where is None
                else str(where.compile(dialect=_DIALECT, compile_kwargs={"literal_binds": True}))
            )
            shapes.add(
                (
                    str(index.name),
                    table.name,
                    bool(index.unique),
                    _normalized_predicate(predicate, table.name),
                    tuple(column.name for column in index.columns),
                )
            )
    return shapes


def test_the_metadata_declares_the_same_indexes_as_the_database(writer: SAConnection) -> None:
    """Names alone would not catch it.

    `connections_one_live_per_institution` declared on the wrong column, or
    without its `sqlite_where`, is a different index doing a different job under
    a name that still matches. Uniqueness and the partial predicate are where
    the schema's idempotency guarantees actually live, so they are compared too --
    and the predicate is compared by what it *says*, not by whether one exists.
    A condition inverted to `retired_at IS NOT NULL` keeps every other property of
    the index intact while making it enforce the opposite rule.
    """
    assert _metadata_indexes() == _database_indexes(writer)


def test_the_index_comparison_actually_reads_some_predicates(writer: SAConnection) -> None:
    """The positive control: a normalizer that returned None for everything would
    make the comparison above agree with itself forever."""
    predicates = {shape[3] for shape in _metadata_indexes() if shape[3] is not None}
    assert "retired_at is null" in predicates
    assert predicates == {shape[3] for shape in _database_indexes(writer) if shape[3] is not None}


def test_every_derived_table_is_indexed_on_its_derivation_version(writer: SAConnection) -> None:
    """🔴 AC-5.4's cost clause, held for tables that do not exist yet.

    Every MCP answer asks whether any derived row carries a version other than
    this build's, one seek per table. A derived table added without an index
    leading with `derivation_version_id` turns that seek into a scan on every
    answer, and nothing else would notice. Checked against the database as well
    as the metadata, because the comparison above only proves the two agree.
    """
    tables = derived_tables()
    assert tables, "no derived table was found, so this guard checks nothing"
    leading = {(shape[1], shape[4][0]) for shape in _database_indexes(writer) if shape[4]} & {
        (shape[1], shape[4][0]) for shape in _metadata_indexes() if shape[4]
    }
    missing = sorted(
        table.name for table in tables if (table.name, "derivation_version_id") not in leading
    )
    assert not missing, f"no index leads with derivation_version_id on {missing}"


def test_migrations_are_idempotent_when_re_run(initialized_config: Config) -> None:
    """`store init` on an already-current datastore applies nothing and breaks nothing."""
    assert migrate(initialized_config) == []
    assert migrate(initialized_config) == []


def test_a_table_rebuild_that_orphans_a_row_is_refused_and_rolled_back(
    initialized_config: Config,
) -> None:
    """🔴 What stands in for foreign keys while the runner has them off.

    A step that rebuilds a referenced table runs with enforcement suspended --
    SQLite refuses `DROP TABLE` otherwise, and it ignores the pragma inside a
    transaction, so it can only be lifted around the whole step. The failure
    that suspension admits is a rebuild that carries fewer rows across than it
    found, and it is silent: nothing errors, and afterwards a transaction simply
    belongs to no account, so a query returns nothing and the answer reads as a
    quiet month.

    The version must not be stamped either. A datastore that reports a version
    whose DDL was rolled back is one nothing can diagnose.
    """

    def lose_the_accounts(conn: Connection) -> None:
        conn.execute("DROP TABLE accounts")
        conn.execute("CREATE TABLE accounts (account_id INTEGER PRIMARY KEY)")

    _seed_one_transaction(initialized_config)
    with pytest.raises(StoreError, match="referencing a row that is no longer there"):
        migrate(
            initialized_config,
            migrations=(
                *MIGRATIONS,
                Migration(
                    version=len(MIGRATIONS) + 1,
                    name="a rebuild that loses its rows",
                    apply=lose_the_accounts,
                    rebuilds_a_referenced_table=True,
                ),
            ),
        )
    with writer_engine(initialized_config) as engine, engine.connect() as conn:
        stamped = conn.execute(text("SELECT MAX(version) FROM schema_version")).scalar_one()
        held = conn.execute(text("SELECT COUNT(*) FROM accounts")).scalar_one()
    assert stamped == SUPPORTED_SCHEMA_VERSION, "a rolled-back migration stamped its version"
    assert held == 1, "the rollback did not put the rebuilt table back"


def test_the_runner_puts_foreign_key_enforcement_back_after_a_rebuild(
    initialized_config: Config,
) -> None:
    """🔴 Suspended for one step, not for the run.

    Every step after a rebuild would otherwise run with enforcement off, and so
    would anything the same handle did afterwards -- a guarantee lost by
    accident rather than by decision, and one nothing downstream would report.
    """
    observed: list[object] = []

    def rebuild_nothing(conn: Connection) -> None:
        observed.append(conn.execute("PRAGMA foreign_keys").fetchone()[0])

    def look(conn: Connection) -> None:
        observed.append(conn.execute("PRAGMA foreign_keys").fetchone()[0])

    migrate(
        initialized_config,
        migrations=(
            *MIGRATIONS,
            Migration(
                version=len(MIGRATIONS) + 1,
                name="a rebuild",
                apply=rebuild_nothing,
                rebuilds_a_referenced_table=True,
            ),
            Migration(version=len(MIGRATIONS) + 2, name="the next step", apply=look),
        ),
    )
    assert observed == [0, 1], "enforcement was not suspended, or was not put back"


def _seed_one_transaction(config: Config) -> None:
    """One account with one transaction hanging off it, so an orphan is possible."""
    with writer_engine(config) as engine, engine.connect() as conn, conn.begin():
        add_transaction(conn, seed(conn))


# --------------------------------------------------------------------------
# Typed round trips
# --------------------------------------------------------------------------


def test_every_typed_column_survives_a_round_trip(writer: SAConnection) -> None:
    """Money comes back an int, a date comes back a date, an instant stays aware.

    SQLite has no date, time or decimal type, so every one of these is text or
    an integer at rest. The column types are what make the round trip lossless,
    and a silent regression in any of them is a whole class of wrong answer.
    """
    s = seed(writer)
    now = now_utc()
    transaction_id = add_transaction(writer, s, authorized_date=YESTERDAY, removed_at=now)

    row = writer.execute(
        select(transactions).where(transactions.c.transaction_id == transaction_id)
    ).one()
    assert row.amount_minor == -1234
    assert type(row.amount_minor) is int
    assert row.posted_date == date(2026, 9, 6)
    assert type(row.posted_date) is date
    assert row.authorized_date == date(2026, 9, 5)
    assert row.removed_at == now
    assert row.removed_at.tzinfo is not None
    assert row.first_seen_at.utcoffset() is not None

    raw = writer.execute(
        select(raw_responses).where(raw_responses.c.raw_response_id == s.raw_response_id)
    ).one()
    assert raw.body_gzip == b"\x1f\x8b not really gzip"
    assert raw.received_at.utcoffset() is not None

    security = writer.execute(
        select(securities).where(securities.c.security_id == s.security_id)
    ).one()
    assert security.close_price_minor == 10_150
    assert security.close_price_as_of == date(2026, 9, 6)


def test_the_remaining_tables_accept_and_return_their_rows(writer: SAConnection) -> None:
    """Balances, holdings, investment transactions, sync state and rules."""
    s = seed(writer)
    now = now_utc()

    writer.execute(
        insert(balances_daily).values(
            account_id=s.account_id,
            as_of_date=TODAY,
            current_minor=minor_units(250_000),
            available_minor=minor_units(240_000),
            currency="USD",
            captured_at=now,
            source="aggregator",
            raw_response_id=s.raw_response_id,
            derivation_version_id=s.derivation_version_id,
        )
    )
    balance = writer.execute(select(balances_daily)).one()
    assert balance.current_minor == 250_000
    assert balance.as_of_date == date(2026, 9, 6)

    writer.execute(
        insert(holdings).values(
            account_id=s.account_id,
            security_id=s.security_id,
            as_of_date=TODAY,
            quantity="12.3456789",
            cost_basis_minor=minor_units(100_000),
            market_value_minor=minor_units(125_251),
            currency="USD",
            captured_at=now,
            source="aggregator",
            raw_response_id=s.raw_response_id,
            derivation_version_id=s.derivation_version_id,
        )
    )
    holding = writer.execute(select(holdings)).one()
    assert holding.quantity == "12.3456789", "a fractional quantity must not pass through a float"
    assert holding.market_value_minor == 125_251

    writer.execute(
        insert(investment_transactions).values(
            account_id=s.account_id,
            security_id=s.security_id,
            source_investment_transaction_id="src-inv-1",
            trade_date=TODAY,
            investment_type="buy",
            quantity="1.5",
            price_minor=minor_units(10_150),
            fees_minor=minor_units(99),
            amount_minor=minor_units(-15_324),
            currency="USD",
            source="aggregator",
            raw_response_id=s.raw_response_id,
            derivation_version_id=s.derivation_version_id,
            first_seen_at=now,
            updated_at=now,
        )
    )
    investment = writer.execute(select(investment_transactions)).one()
    assert investment.amount_minor == -15_324
    assert investment.trade_date == date(2026, 9, 6)

    writer.execute(
        insert(sync_state).values(
            connection_id=s.connection_id,
            domain="transactions",
            cursor="opaque-cursor",
            history_start_date=YESTERDAY,
            last_attempt_at=now,
            last_success_at=now,
            updated_at=now,
        )
    )
    state = writer.execute(select(sync_state)).one()
    assert state.cursor == "opaque-cursor"
    assert state.history_start_date == date(2026, 9, 5)

    writer.execute(
        insert(account_rules).values(
            account_id=s.account_id,
            rule_type="contribution_only",
            parameters='{"contributing_account_ids": [1]}',
            active=1,
            created_at=now,
            updated_at=now,
        )
    )
    rule = writer.execute(select(account_rules)).one()
    assert rule.rule_type == "contribution_only"


# --------------------------------------------------------------------------
# The constraints that carry requirements (AC-6.2, AC-6.4, AC-7.4)
# --------------------------------------------------------------------------


def test_a_float_amount_is_refused_by_the_database_itself(writer: SAConnection) -> None:
    """AC-6.2. A column declared INTEGER holds a float happily; the CHECK does not."""
    s = seed(writer)
    transaction_id = add_transaction(writer, s)
    with pytest.raises(IntegrityError):
        writer.exec_driver_sql(
            "UPDATE transactions SET amount_minor = 12.34 WHERE transaction_id = ?",
            (transaction_id,),
        )


def test_a_naive_timestamp_is_refused_by_the_database_itself(writer: SAConnection) -> None:
    """AC-6.4. Without the zone the value is a wall-clock reading, not an instant."""
    s = seed(writer)
    transaction_id = add_transaction(writer, s)
    with pytest.raises(IntegrityError):
        writer.exec_driver_sql(
            "UPDATE transactions SET updated_at = '2026-09-06T12:00:00' WHERE transaction_id = ?",
            (transaction_id,),
        )


def test_an_instant_in_a_calendar_date_column_is_refused(writer: SAConnection) -> None:
    """AC-6.4, the other direction: a transaction date has no time and no zone."""
    s = seed(writer)
    transaction_id = add_transaction(writer, s)
    with pytest.raises(IntegrityError):
        writer.exec_driver_sql(
            "UPDATE transactions SET posted_date = '2026-09-06T00:00:00+00:00' "
            "WHERE transaction_id = ?",
            (transaction_id,),
        )


def test_a_manual_row_without_an_import_to_point_at_is_refused(writer: SAConnection) -> None:
    """AC-7.4: provenance is never lost, so a row cannot claim a source it has no link to."""
    s = seed(writer)
    with pytest.raises(IntegrityError):
        add_transaction(
            writer,
            s,
            source="manual",
            source_transaction_id=None,
            raw_response_id=None,
            manual_import_id=None,
            import_fingerprint=None,
        )


def test_an_aggregator_row_carrying_an_import_link_is_refused(writer: SAConnection) -> None:
    """The mirror case: a row is from one source or the other, never both."""
    s = seed(writer)
    with pytest.raises(IntegrityError):
        add_transaction(writer, s, manual_import_id=s.manual_import_id)


def test_a_transaction_must_point_at_an_account_that_exists(writer: SAConnection) -> None:
    """`PRAGMA foreign_keys = ON` is set on every handle; this is what it buys."""
    s = seed(writer)
    with pytest.raises(IntegrityError):
        add_transaction(writer, s, account_id=99_999)


# --------------------------------------------------------------------------
# Acceptance criteria
# --------------------------------------------------------------------------


def test_a_local_account_id_survives_re_enrollment_of_its_connection(
    writer: SAConnection,
) -> None:
    """AC-6.3. Re-linking an institution must not orphan its history.

    The aggregator's account id changes when a connection is removed and
    re-linked, which is exactly why no row of history references it. Everything
    points at the local `account_id`, and re-enrollment repoints the account at
    a new connection without touching a single transaction.
    """
    s = seed(writer)
    add_transaction(writer, s)
    now = now_utc()

    writer.execute(
        update(connections)
        .where(connections.c.connection_id == s.connection_id)
        .values(status="retired", retired_at=now, updated_at=now)
    )
    new_connection_id = _one_id(
        writer,
        connections,
        institution_id=s.institution_id,
        source_connection_id="src-conn-2",
        credential_ref="connection:src-conn-2",
        capabilities='["transactions"]',
        status="active",
        enrolled_at=now,
        created_at=now,
        updated_at=now,
    )
    writer.execute(
        update(accounts)
        .where(accounts.c.account_id == s.account_id)
        .values(
            connection_id=new_connection_id,
            source_account_id="src-acct-1-after-relink",
            updated_at=now,
        )
    )

    account = writer.execute(select(accounts)).one()
    assert account.account_id == s.account_id, "the local id moved; history is now orphaned"
    assert account.connection_id == new_connection_id
    surviving = writer.execute(
        select(transactions).where(transactions.c.account_id == s.account_id)
    ).all()
    assert len(surviving) == 1


def test_retiring_an_account_keeps_its_history_and_dates_its_closure(
    writer: SAConnection,
) -> None:
    """AC-6.5. A closed account's dormant period is closure, not a coverage gap."""
    s = seed(writer)
    add_transaction(writer, s)
    now = now_utc()

    writer.execute(
        update(accounts)
        .where(accounts.c.account_id == s.account_id)
        .values(lifecycle_status="inactive", closed_date=TODAY, updated_at=now)
    )

    account = writer.execute(select(accounts)).one()
    assert account.lifecycle_status == "inactive"
    assert account.closed_date == date(2026, 9, 6)
    assert len(writer.execute(select(transactions)).all()) == 1


def test_the_database_refuses_an_instant_in_the_last_listed_column(
    writer: SAConnection,
) -> None:
    """Migration 003's column keeps AC-6.4 the way migration 002's do.

    🔴 Through raw SQL on purpose, like every other negative case in this file:
    `CalendarDateColumn` already refuses this in Python, and asserting only that
    would test the type decorator twice and the database not at all — while
    `sync shell` ships an operator a prompt that reaches the file with no type
    decorator anywhere in between. `last_seen_date` arrived by `ALTER TABLE`,
    which is the one path where a constraint is easiest to leave off.
    """
    s = seed(writer)
    with pytest.raises(IntegrityError):
        writer.execute(
            text("UPDATE accounts SET last_seen_date = :value WHERE account_id = :id"),
            {"value": "2026-09-06T00:00:00+00:00", "id": s.account_id},
        )


def test_the_last_listed_date_is_the_maximum_side_twin_of_the_first(
    writer: SAConnection,
) -> None:
    """AC-12.4. Nullable, because migration 003 backfilled nothing.

    🔴 A null is not "never listed" — it is a row this build's migration reached
    before any sync did, and `query._account_lifecycle` reads it as exactly
    that: no roster observation is recorded for this account. It is never read
    as `first_seen_date`, which looks true for one account and fabricates
    closures across a connection.
    """
    s = seed(writer)

    account = writer.execute(select(accounts)).one()
    assert account.last_seen_date is None, "migration 003 must not have invented a date"

    writer.execute(
        update(accounts).where(accounts.c.account_id == s.account_id).values(last_seen_date=TODAY)
    )
    assert writer.execute(select(accounts.c.last_seen_date)).scalar_one() == date(2026, 9, 6)


def test_the_database_refuses_an_instant_in_the_roster_observation_column(
    writer: SAConnection,
) -> None:
    """Migration 004's column keeps AC-6.4 the way migrations 002 and 003 do.

    🔴 `roster_observed_at` was the rejected spelling and an instant is the
    rejected type: this column's only use is a comparison against
    `accounts.last_seen_date`, which is a calendar date, and a comparison across
    the two types is a defect rather than a conversion. The CHECK is what makes
    that a property of the file rather than of the code in front of it —
    `sync shell` ships an operator a prompt that reaches the database with no
    type decorator anywhere in between, and `ALTER TABLE` is the one path where
    a constraint is easiest to leave off.
    """
    s = seed(writer)
    with pytest.raises(IntegrityError):
        writer.execute(
            text("UPDATE connections SET roster_observed_date = :value WHERE connection_id = :id"),
            {"value": "2026-09-06T00:00:00+00:00", "id": s.connection_id},
        )


def test_the_roster_observation_is_nullable_and_migration_004_invented_none(
    writer: SAConnection,
) -> None:
    """AC-12.4's per-connection half, and the null that carries the upgrade window.

    🔴 There is nothing honest to backfill with: the correct value is when this
    connection's roster was last read, which is the record nothing ever kept —
    that absence is the whole reason the column exists. So the null MEANS "never
    observed", and `query._account_lifecycle` marks nothing on such a connection
    absent. Between this migration and a connection's next successful sync,
    which for a failing connection never arrives, no account can be called
    closed.
    """
    s = seed(writer)

    connection = writer.execute(select(connections)).one()
    assert connection.roster_observed_date is None, "migration 004 must not have invented a date"

    writer.execute(
        update(connections)
        .where(connections.c.connection_id == s.connection_id)
        .values(roster_observed_date=TODAY)
    )
    assert writer.execute(select(connections.c.roster_observed_date)).scalar_one() == date(
        2026, 9, 6
    )


def test_a_closed_date_before_the_account_was_first_seen_is_refused(
    writer: SAConnection,
) -> None:
    """A closure that precedes the account turns every coverage window negative."""
    s = seed(writer)
    with pytest.raises(IntegrityError):
        writer.execute(
            update(accounts)
            .where(accounts.c.account_id == s.account_id)
            .values(lifecycle_status="inactive", closed_date=calendar_date(date(2020, 1, 1)))
        )


def test_the_source_category_is_kept_when_an_override_is_written(writer: SAConnection) -> None:
    """AC-6.1: the override is a separate column, so the source value is never lost."""
    s = seed(writer)
    transaction_id = add_transaction(writer, s)

    writer.execute(
        update(transactions)
        .where(transactions.c.transaction_id == transaction_id)
        .values(category_override="COFFEE", updated_at=now_utc())
    )

    row = writer.execute(select(transactions)).one()
    assert row.category_override == "COFFEE"
    assert row.source_category_primary == "FOOD_AND_DRINK"
    assert row.source_category_detailed == "FOOD_AND_DRINK_COFFEE"


def test_an_institution_holds_one_live_connection_at_a_time(writer: SAConnection) -> None:
    """AC-1.4: re-enrolling updates rather than duplicating.

    Two live connections to one institution would double every balance it
    reports, which is the kind of wrong answer that looks entirely plausible.
    """
    s = seed(writer)
    now = now_utc()
    second = {
        "institution_id": s.institution_id,
        "source_connection_id": "src-conn-2",
        "credential_ref": "connection:src-conn-2",
        "capabilities": "[]",
        "status": "active",
        "enrolled_at": now,
        "created_at": now,
        "updated_at": now,
    }

    with pytest.raises(IntegrityError):
        writer.execute(insert(connections).values(**second))

    writer.execute(
        update(connections)
        .where(connections.c.connection_id == s.connection_id)
        .values(status="retired", retired_at=now, updated_at=now)
    )
    writer.execute(insert(connections).values(**second))
    live = writer.execute(select(connections).where(connections.c.retired_at.is_(None))).all()
    assert len(live) == 1


def test_a_degraded_connection_must_carry_an_error_code(writer: SAConnection) -> None:
    """AC-4.2/AC-4.5: degraded with nothing recorded is a state nobody can act on."""
    s = seed(writer)
    with pytest.raises(IntegrityError):
        writer.execute(
            update(connections)
            .where(connections.c.connection_id == s.connection_id)
            .values(status="degraded", updated_at=now_utc())
        )


def test_a_days_balance_is_written_once_and_never_overwritten(writer: SAConnection) -> None:
    """AC-3.1. No aggregator backfills a balance series, so a day is captured once.

    Rejecting the second write rather than accepting it keeps the series from
    depending on what time of day the sync happened to run, and makes a re-run a
    no-op (AC-2.4).
    """
    s = seed(writer)
    now = now_utc()
    row = {
        "account_id": s.account_id,
        "as_of_date": TODAY,
        "current_minor": minor_units(250_000),
        "currency": "USD",
        "captured_at": now,
        "source": "aggregator",
        "raw_response_id": s.raw_response_id,
        "derivation_version_id": s.derivation_version_id,
    }
    writer.execute(insert(balances_daily).values(**row))
    with pytest.raises(IntegrityError):
        writer.execute(insert(balances_daily).values(**{**row, "current_minor": minor_units(1)}))

    assert writer.execute(select(balances_daily.c.current_minor)).scalar_one() == 250_000


def test_the_same_file_cannot_be_imported_into_an_account_twice(writer: SAConnection) -> None:
    """AC-7.5, first layer: the file's hash is its identity, so renaming it changes nothing."""
    s = seed(writer)
    with pytest.raises(IntegrityError):
        writer.execute(
            insert(manual_imports).values(
                account_id=s.account_id,
                adapter="csv-v1",
                source_name="export-renamed.csv",
                file_sha256="1" * 64,
                file_bytes=42,
                imported_at=now_utc(),
                rows_seen=3,
                rows_applied=3,
            )
        )


def test_overlapping_imports_cannot_duplicate_a_row(writer: SAConnection) -> None:
    """AC-7.5, second layer: two statement exports sharing a boundary week.

    The files differ, so the first layer lets both in. The rows they share are
    the same rows, and the fingerprint is what says so.
    """
    s = seed(writer)
    now = now_utc()
    second_import_id = _one_id(
        writer,
        manual_imports,
        account_id=s.account_id,
        adapter="csv-v1",
        source_name="export-next-month.csv",
        file_sha256="2" * 64,
        file_bytes=44,
        imported_at=now,
        rows_seen=3,
        rows_applied=3,
    )
    imported = {
        "source": "manual",
        "source_transaction_id": None,
        "raw_response_id": None,
        "manual_import_id": s.manual_import_id,
        "import_fingerprint": "sha-of-the-row",
    }
    add_transaction(writer, s, **imported)

    with pytest.raises(IntegrityError):
        add_transaction(writer, s, **{**imported, "manual_import_id": second_import_id})


def test_two_accounts_may_share_a_fingerprint(writer: SAConnection) -> None:
    """The dedup key is scoped per account, because an identical charge on two
    accounts is two charges."""
    s = seed(writer)
    now = now_utc()
    other_account_id = _one_id(
        writer,
        accounts,
        institution_id=s.institution_id,
        connection_id=s.connection_id,
        source_account_id="src-acct-2",
        name="Another Account",
        account_type="depository",
        balance_class="asset",
        currency="USD",
        lifecycle_status="active",
        first_seen_date=YESTERDAY,
        source="aggregator",
        created_at=now,
        updated_at=now,
    )
    other_import_id = _one_id(
        writer,
        manual_imports,
        account_id=other_account_id,
        adapter="csv-v1",
        source_name="other.csv",
        file_sha256="3" * 64,
        file_bytes=10,
        imported_at=now,
        rows_seen=1,
        rows_applied=1,
    )
    imported = {
        "source": "manual",
        "source_transaction_id": None,
        "raw_response_id": None,
        "import_fingerprint": "sha-of-the-row",
    }
    add_transaction(writer, s, manual_import_id=s.manual_import_id, **imported)
    add_transaction(
        writer,
        s,
        account_id=other_account_id,
        manual_import_id=other_import_id,
        **imported,
    )
    assert len(writer.execute(select(transactions)).all()) == 2


def test_an_account_is_classified_as_an_asset_or_a_liability(writer: SAConnection) -> None:
    """`net_worth` needs the partition, and `account_type` cannot supply it.

    The types are the source's vocabulary, they differ between sources, and an
    import-only account has none at all.
    """
    s = seed(writer)
    with pytest.raises(IntegrityError):
        writer.execute(
            update(accounts)
            .where(accounts.c.account_id == s.account_id)
            .values(balance_class="somewhere in between")
        )


def test_net_worth_is_a_plain_sum_because_liabilities_are_stored_negative(
    writer: SAConnection,
) -> None:
    """The whole reason for one sign convention instead of one per account type.

    A card balance stored as a positive amount owed would make net worth a
    type-dependent expression, and every consumer that forgot the special case
    would be wrong by twice the debt.
    """
    s = seed(writer)
    now = now_utc()
    card_account_id = _one_id(
        writer,
        accounts,
        institution_id=s.institution_id,
        connection_id=s.connection_id,
        source_account_id="src-acct-card",
        name="A Card",
        account_type="credit",
        account_subtype="credit card",
        balance_class="liability",
        currency="USD",
        lifecycle_status="active",
        first_seen_date=YESTERDAY,
        source="aggregator",
        created_at=now,
        updated_at=now,
    )
    for account_id, current in ((s.account_id, 250_000), (card_account_id, -40_000)):
        writer.execute(
            insert(balances_daily).values(
                account_id=account_id,
                as_of_date=TODAY,
                current_minor=minor_units(current),
                currency="USD",
                captured_at=now,
                source="aggregator",
                raw_response_id=s.raw_response_id,
                derivation_version_id=s.derivation_version_id,
            )
        )

    net_worth = writer.execute(
        select(func.sum(balances_daily.c.current_minor)).where(balances_daily.c.as_of_date == TODAY)
    ).scalar_one()
    assert net_worth == 210_000


def test_a_balance_and_a_holding_carry_a_transactions_provenance_rule(
    writer: SAConnection,
) -> None:
    """AC-7.4 is a property of every normalized row, not of the transactions table.

    `holdings.source` accepted `'manual'` before it had a column naming the
    import it came from — a provenance marker with nothing behind it, which
    reads as provenance right up until someone tries to follow it.
    """
    s = seed(writer)
    now = now_utc()
    balance = {
        "account_id": s.account_id,
        "as_of_date": TODAY,
        "current_minor": minor_units(250_000),
        "currency": "USD",
        "captured_at": now,
        "derivation_version_id": s.derivation_version_id,
    }
    holding = {
        "account_id": s.account_id,
        "security_id": s.security_id,
        "as_of_date": TODAY,
        "quantity": "1",
        "market_value_minor": minor_units(10_150),
        "currency": "USD",
        "captured_at": now,
        "derivation_version_id": s.derivation_version_id,
    }

    with pytest.raises(IntegrityError):
        writer.execute(insert(balances_daily).values(**balance, source="manual"))
    with pytest.raises(IntegrityError):
        writer.execute(insert(holdings).values(**holding, source="manual"))

    writer.execute(
        insert(balances_daily).values(
            **balance, source="manual", manual_import_id=s.manual_import_id
        )
    )
    writer.execute(
        insert(holdings).values(**holding, source="manual", manual_import_id=s.manual_import_id)
    )
    assert writer.execute(select(balances_daily.c.manual_import_id)).scalar_one() is not None
    assert writer.execute(select(holdings.c.manual_import_id)).scalar_one() is not None


def test_migration_002_ddl_is_frozen(writer: SAConnection) -> None:
    """A forward-only migration is a record of what some datastore already ran.

    The rendered DDL is compared against a hash recorded beside it — two
    independent descriptions, so the disagreement is available. Without it,
    "frozen" rested on nobody editing the two shared constraint idioms, and
    migration 003 tightening one of them would silently redefine what version 2
    means for every datastore created afterwards.
    """
    rendered = sha256("\n".join(CORE_SCHEMA_DDL).encode("utf-8")).hexdigest()
    assert rendered == CORE_SCHEMA_DDL_SHA256, (
        "migration 002's DDL changed. If this was deliberate, it belongs in a new migration: "
        "editing 002 makes this build disagree with every datastore that already ran it"
    )


def test_an_aggregator_row_must_name_the_response_it_came_from(writer: SAConnection) -> None:
    """AC-7.4 asks where a row came from, and silence is not an answer.

    The rebuild is written against this constraint. A row whose origin is
    `aggregator` with no raw response behind it is a row nothing can trace, and
    it would look identical to one that had been traced.
    """
    s = seed(writer)
    with pytest.raises(IntegrityError):
        add_transaction(writer, s, raw_response_id=None)
    with pytest.raises(IntegrityError):
        writer.execute(
            insert(balances_daily).values(
                account_id=s.account_id,
                as_of_date=TODAY,
                current_minor=minor_units(1),
                currency="USD",
                captured_at=now_utc(),
                source="aggregator",
                derivation_version_id=s.derivation_version_id,
            )
        )


def test_a_refused_position_points_at_the_capture_that_refused_it(writer: SAConnection) -> None:
    """Migration 011's guarantees, asserted through the file rather than the type.

    A refusal is only ever derived from an archived capture, so one with no raw
    response behind it is a claim nothing can check. And `sync shell` reaches
    this table with no type decorator between the operator and the file, so the
    calendar-date CHECK has to hold on its own.
    """
    s = seed(writer)
    refusal = {
        "account_id": s.account_id,
        "security_id": s.security_id,
        "currency": "ZZZ",
        "captured_at": now_utc(),
        "derivation_version_id": s.derivation_version_id,
    }

    with pytest.raises(IntegrityError):
        writer.execute(insert(refused_holdings).values(**refusal, as_of_date=TODAY))
    with pytest.raises(IntegrityError):
        writer.execute(
            text(
                "INSERT INTO refused_holdings (account_id, security_id, as_of_date, currency, "
                "captured_at, raw_response_id, derivation_version_id) VALUES (:account, "
                ":security, '2026-09-12T00:00:00+00:00', 'ZZZ', '2026-09-12T00:00:00+00:00', "
                ":raw, :version)"
            ),
            {
                "account": s.account_id,
                "security": s.security_id,
                "raw": s.raw_response_id,
                "version": s.derivation_version_id,
            },
        )

    writer.execute(
        insert(refused_holdings).values(
            **refusal, as_of_date=TODAY, raw_response_id=s.raw_response_id
        )
    )
    assert writer.execute(select(func.count()).select_from(refused_holdings)).scalar_one() == 1


def test_the_provenance_constant_still_agrees_with_the_ddl() -> None:
    """🔴 `PROVENANCE_SOURCES` is an enumeration, so it is checked, not trusted.

    The read surface reports a breakdown by `source` and must emit a zero for a
    source with no rows — which needs the full set spelled in Python, while the
    authority lives in the frozen DDL's `CHECK (source IN (...))`. Two homes for
    one fact is exactly the drift `learnings.md` records: a rule that matched on
    a name where it meant a relationship, and a list that was already wrong on
    the day it was written.

    A third provenance arriving in the DDL without this tuple noticing would
    make every coverage report silently omit a whole class of row, and the
    payload would look complete. This test is the only thing that would catch
    it.
    """
    ddl = (
        Path(__file__).parents[2]
        / "src"
        / "bankmachine"
        / "store"
        / "migrations"
        / "core_schema.py"
    ).read_text(encoding="utf-8")
    declared = set(re.findall(r"CHECK \(source IN \(([^)]*)\)\)", ddl))
    assert declared, "the CHECK this test reads is no longer in the DDL, so it checks nothing"
    for clause in declared:
        assert set(re.findall(r"'([a-z]+)'", clause)) == set(PROVENANCE_SOURCES), clause
