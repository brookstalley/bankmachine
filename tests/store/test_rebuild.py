"""Rebuild (AC-5.2, AC-11.5): the normalized tables, reconstructed from the archive.

There is no aggregator yet, so these tests bring their own derivers. That is not
a workaround: the derivers are the seam build step 2 plugs into, and exercising
the rebuild through a stand-in for them is the only way to test the machinery
before the real ones exist. They are deliberately the narrowest pair that
touches every shape the schema has -- an identity table the rebuild must not
delete (`accounts`), an append-only series (`balances_daily`), and a fact table
it rebuilds outright (`transactions`).

The claim under test is not "the rebuild ran". It is that a rebuild reproduces
what it replaced, that it refuses to commit when it cannot, and that it never
deletes a row the archive could not recreate.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Connection as SAConnection
from sqlalchemy import insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from bankmachine.config import DEFAULT_CONNECTION_CAP, MAX_HISTORY_DAYS, Config
from bankmachine.secrets import delete_datastore_key, generate_datastore_key, set_datastore_key
from bankmachine.store import derivation
from bankmachine.store.derivation import (
    DerivationContext,
    Deriver,
    UnknownEndpointError,
    apply_response,
)
from bankmachine.store.engine import transaction, writer_engine
from bankmachine.store.migrations import migrate
from bankmachine.store.raw import RawResponse, record_response
from bankmachine.store.rebuild import (
    RebuildNotReproducibleError,
    UnhashableValueError,
    content_digest,
    derived_dimension_tables,
    derived_tables,
    no_replay_passes,
    rebuild,
    rebuildable_tables,
)
from bankmachine.store.schema import (
    accounts,
    balances_daily,
    connections,
    derivation_versions,
    institutions,
    manual_imports,
    raw_responses,
    transactions,
)
from bankmachine.store.types import UtcInstant, calendar_date, minor_units, utc_instant

ACCOUNTS_ENDPOINT = "/test/accounts"
TRANSACTIONS_ENDPOINT = "/test/transactions"

DAY_ONE = date(2026, 9, 1)


def instant(offset_days: int) -> UtcInstant:
    return utc_instant(datetime(2026, 9, 1, 9, 0, tzinfo=UTC) + timedelta(days=offset_days))


# --------------------------------------------------------------------------
# The stand-in derivers.
# --------------------------------------------------------------------------


def derive_accounts(conn: SAConnection, response: RawResponse, context: DerivationContext) -> None:
    """An `/accounts` response: refresh the accounts, append today's balance.

    Accounts are upserted rather than inserted, because a rebuild does not
    delete them -- their local ids are what every row of history points at
    (AC-6.3), and the archive cannot recreate an id.
    """
    payload = json.loads(response.body)
    institution_id = conn.execute(
        select(connections.c.institution_id).where(
            connections.c.connection_id == response.connection_id
        )
    ).scalar_one()
    for entry in payload["accounts"]:
        account_id = _upsert_account(conn, response, int(institution_id), entry)
        # Append-only (AC-3.1): the schema rejects a second capture on a day it
        # already holds, so the first reading of a day is the one that stands.
        conn.execute(
            sqlite_insert(balances_daily)
            .values(
                account_id=account_id,
                as_of_date=calendar_date(date.fromisoformat(entry["as_of"])),
                current_minor=minor_units(entry["current_minor"]),
                currency=entry["currency"],
                captured_at=response.received_at,
                source="aggregator",
                raw_response_id=response.raw_response_id,
                derivation_version_id=context.derivation_version_id,
            )
            .on_conflict_do_nothing()
        )


def _upsert_account(
    conn: SAConnection, response: RawResponse, institution_id: int, entry: Mapping[str, Any]
) -> int:
    existing = conn.execute(
        select(accounts.c.account_id).where(
            accounts.c.connection_id == response.connection_id,
            accounts.c.source_account_id == entry["id"],
        )
    ).one_or_none()
    if existing is not None:
        conn.execute(
            update(accounts)
            .where(accounts.c.account_id == existing[0])
            .values(name=entry["name"], updated_at=response.received_at)
        )
        return int(existing[0])
    result = conn.execute(
        insert(accounts).values(
            institution_id=institution_id,
            connection_id=response.connection_id,
            source_account_id=entry["id"],
            name=entry["name"],
            account_type="depository",
            balance_class="asset",
            currency=entry["currency"],
            lifecycle_status="active",
            first_seen_date=calendar_date(date.fromisoformat(entry["as_of"])),
            source="aggregator",
            created_at=response.received_at,
            updated_at=response.received_at,
        )
    )
    primary_key = result.inserted_primary_key
    assert primary_key is not None
    return int(primary_key[0])


def derive_transactions(
    conn: SAConnection, response: RawResponse, context: DerivationContext
) -> None:
    """A `/transactions` response, upserted on the source's own id."""
    _derive_transactions(conn, response, context, describe=str)


def derive_transactions_recategorized(
    conn: SAConnection, response: RawResponse, context: DerivationContext
) -> None:
    """The same deriver after a change nobody bumped the derivation version for."""
    _derive_transactions(conn, response, context, describe=str.upper)


def derive_transactions_reidentified(
    conn: SAConnection, response: RawResponse, context: DerivationContext
) -> None:
    """A deriver that no longer emits the rows under the identities it used to.

    A real reason for it: the source identity was being taken from the wrong
    field, and a later build corrected it. The replay then produces rows that
    are not the ones the operator corrected, which is the case where a preserved
    column has nowhere to land.
    """
    _derive_transactions(conn, response, context, describe=str, identify="{}-v2".format)


def _derive_transactions(
    conn: SAConnection,
    response: RawResponse,
    context: DerivationContext,
    *,
    describe: Callable[[str], str],
    identify: Callable[[str], str] = str,
) -> None:
    payload = json.loads(response.body)
    account_id = int(
        conn.execute(
            select(accounts.c.account_id).where(
                accounts.c.connection_id == response.connection_id,
                accounts.c.source_account_id == payload["account"],
            )
        ).scalar_one()
    )
    for entry in payload["transactions"]:
        values = {
            "account_id": account_id,
            "source_transaction_id": identify(entry["id"]),
            "pending": 0,
            "posted_date": calendar_date(date.fromisoformat(entry["posted"])),
            "amount_minor": minor_units(entry["amount_minor"]),
            "currency": "USD",
            "description": describe(entry["description"]),
            "source": "aggregator",
            "raw_response_id": response.raw_response_id,
            "derivation_version_id": context.derivation_version_id,
            "first_seen_at": response.received_at,
            "updated_at": response.received_at,
        }
        statement = sqlite_insert(transactions).values(**values)
        conn.execute(
            statement.on_conflict_do_update(
                index_elements=[transactions.c.account_id, transactions.c.source_transaction_id],
                index_where=transactions.c.source_transaction_id.is_not(None),
                set_={
                    name: values[name]
                    for name in (
                        "posted_date",
                        "amount_minor",
                        "description",
                        "raw_response_id",
                        "derivation_version_id",
                        "updated_at",
                    )
                },
            )
        )


DERIVERS: Mapping[str, Deriver] = {
    ACCOUNTS_ENDPOINT: derive_accounts,
    TRANSACTIONS_ENDPOINT: derive_transactions,
}

RECATEGORIZED: Mapping[str, Deriver] = {
    ACCOUNTS_ENDPOINT: derive_accounts,
    TRANSACTIONS_ENDPOINT: derive_transactions_recategorized,
}

REIDENTIFIED: Mapping[str, Deriver] = {
    ACCOUNTS_ENDPOINT: derive_accounts,
    TRANSACTIONS_ENDPOINT: derive_transactions_reidentified,
}


# --------------------------------------------------------------------------
# Corpus and store helpers.
# --------------------------------------------------------------------------

Corpus = Sequence[tuple[str, dict[str, Any], UtcInstant]]


def accounts_response(
    day: int, current_minor: int, name: str = "Everyday"
) -> tuple[str, dict[str, Any], UtcInstant]:
    return (
        ACCOUNTS_ENDPOINT,
        {
            "accounts": [
                {
                    "id": "acct-1",
                    "name": name,
                    "currency": "USD",
                    "as_of": (DAY_ONE + timedelta(days=day)).isoformat(),
                    "current_minor": current_minor,
                }
            ]
        },
        instant(day),
    )


def transactions_response(
    day: int, entries: Sequence[Mapping[str, Any]]
) -> tuple[str, dict[str, Any], UtcInstant]:
    return (
        TRANSACTIONS_ENDPOINT,
        {"account": "acct-1", "transactions": list(entries)},
        instant(day),
    )


def entry(identifier: str, day: int, amount_minor: int, description: str) -> dict[str, Any]:
    return {
        "id": identifier,
        "posted": (DAY_ONE + timedelta(days=day)).isoformat(),
        "amount_minor": amount_minor,
        "description": description,
    }


A_CORPUS: Corpus = (
    accounts_response(0, 500_00),
    transactions_response(0, [entry("t-1", 0, -12_50, "coffee"), entry("t-2", 0, 2_000_00, "pay")]),
    accounts_response(1, 2_487_50),
    transactions_response(1, [entry("t-3", 1, -40_00, "groceries")]),
    # The same transaction seen again, corrected. The rebuild must land on the
    # later reading, which it can only do by replaying in received order.
    transactions_response(2, [entry("t-1", 0, -13_75, "coffee")]),
)


def enroll(conn: SAConnection) -> int:
    """An institution and a live connection: enrollment state, not derived state."""
    moment = instant(0)
    institution = conn.execute(
        insert(institutions).values(
            source_institution_id="src-inst-1",
            name="An Institution",
            first_seen_at=moment,
            last_seen_at=moment,
        )
    ).inserted_primary_key
    assert institution is not None
    result = conn.execute(
        insert(connections).values(
            institution_id=int(institution[0]),
            source_connection_id="src-conn-1",
            credential_ref="connection:src-conn-1",
            status="active",
            enrolled_at=moment,
            created_at=moment,
            updated_at=moment,
        )
    ).inserted_primary_key
    assert result is not None
    return int(result[0])


def apply_corpus(
    config: Config, corpus: Corpus, *, derivers: Mapping[str, Deriver] = DERIVERS
) -> int:
    """Enroll, then feed the corpus through the live path the sync writer will use."""
    with writer_engine(config) as engine, engine.connect() as conn:
        with transaction(conn):
            connection_id = enroll(conn)
        for endpoint, payload, received_at in corpus:
            apply_response(
                conn,
                connection_id=connection_id,
                endpoint=endpoint,
                body=json.dumps(payload).encode(),
                received_at=received_at,
                derivers=derivers,
            )
    return connection_id


@contextmanager
def reading(config: Config) -> Iterator[SAConnection]:
    with writer_engine(config) as engine, engine.connect() as conn:
        yield conn


def digest_of(config: Config) -> str:
    with reading(config) as conn:
        return content_digest(conn)


def dump(conn: SAConnection, table: str) -> list[tuple[object, ...]]:
    """Every column of every row, ids included, in a stable order."""
    rows = conn.exec_driver_sql(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
    return [tuple(row) for row in rows]


@contextmanager
def temporary_store(base: Path) -> Iterator[Config]:
    """A fresh encrypted datastore, for a property test that needs one per example."""
    config = Config(
        environment="sandbox",
        datastore_path=base / "store.db",
        log_dir=base / "logs",
        keychain_service=f"bankmachine-test-{uuid.uuid4()}",
        busy_timeout_ms=200,
        history_days=MAX_HISTORY_DAYS,
        connection_cap=DEFAULT_CONNECTION_CAP,
        plaid_client_id=None,
        config_path=None,
    )
    set_datastore_key(config, generate_datastore_key())
    try:
        migrate(config)
        yield config
    finally:
        delete_datastore_key(config)


# --------------------------------------------------------------------------
# The tests.
# --------------------------------------------------------------------------


def test_the_rebuildable_tables_are_the_ones_carrying_raw_provenance() -> None:
    # Derived from the schema rather than listed, so a table added later with a
    # `raw_response_id` is rebuilt without an edit here -- and one without it can
    # never be deleted by a rebuild, which is what protects imported rows.
    assert {table.name for table in rebuildable_tables()} == {
        "transactions",
        "balances_daily",
        "holdings",
        "investment_transactions",
    }


def test_a_derived_table_is_either_rebuildable_or_a_dimension_and_never_both() -> None:
    # The partition is what a step-2 deriver needs before it writes a row: a
    # rebuildable table is emptied and re-derived, a dimension is met again with
    # its rows still there and must be upserted. Asserted rather than described,
    # because the cost of getting it wrong lands on a replay months from now.
    derived = {table.name for table in derived_tables()}
    rebuildable = {table.name for table in rebuildable_tables()}
    dimensions = {table.name for table in derived_dimension_tables()}

    assert rebuildable | dimensions == derived
    assert not rebuildable & dimensions
    assert dimensions == {"securities"}


def test_neither_registry_is_classified_as_derived_from_itself() -> None:
    # `raw_responses` names its own primary key `raw_response_id`, and
    # `derivation_versions` names its own `derivation_version_id`. A rule matching
    # on either column name puts the registry in the set of tables a rebuild
    # rewrites -- which for the archive means emptying it and then replaying
    # nothing at all.
    classified = {table.name for table in derived_tables()}
    assert "raw_responses" not in classified
    assert "derivation_versions" not in classified


def test_rebuild_reproduces_the_normalized_tables_from_the_archive_alone(
    initialized_config: Config,
) -> None:
    apply_corpus(initialized_config, A_CORPUS)
    before = digest_of(initialized_config)
    with reading(initialized_config) as conn:
        accounts_before = dump(conn, "accounts")
        transactions_before = dump(conn, "transactions")
    assert len(transactions_before) == 3

    report = rebuild(initialized_config, derivers=DERIVERS, replay_passes=no_replay_passes)

    assert report.responses_replayed == len(A_CORPUS)
    assert report.rows_deleted["transactions"] == 3
    assert not report.content_changed
    assert digest_of(initialized_config) == before
    with reading(initialized_config) as conn:
        assert dump(conn, "accounts") == accounts_before
        assert dump(conn, "transactions") == transactions_before


def test_rebuild_replays_in_received_order_so_a_correction_still_wins(
    initialized_config: Config,
) -> None:
    # `t-1` is corrected by a later response. If a rebuild replayed in any other
    # order the older reading would win and the datastore would silently revert.
    apply_corpus(initialized_config, A_CORPUS)
    rebuild(initialized_config, derivers=DERIVERS, replay_passes=no_replay_passes)

    with reading(initialized_config) as conn:
        amount = conn.execute(
            select(transactions.c.amount_minor).where(transactions.c.source_transaction_id == "t-1")
        ).scalar_one()
    assert amount == -13_75


def test_a_rebuild_that_cannot_reproduce_its_input_is_rolled_back(
    initialized_config: Config,
) -> None:
    apply_corpus(initialized_config, A_CORPUS)
    before = digest_of(initialized_config)

    with pytest.raises(RebuildNotReproducibleError) as caught:
        rebuild(initialized_config, derivers=RECATEGORIZED, replay_passes=no_replay_passes)

    assert "unchanged derivation version" in str(caught.value)
    assert digest_of(initialized_config) == before


def test_an_unreproducible_rebuild_can_be_accepted_deliberately(
    initialized_config: Config,
) -> None:
    # The honest use is an archive that was deliberately pruned. It takes a flag
    # because the same symptom is produced by a deriver that reads the clock,
    # and that one must not be committed by a command that shrugged.
    apply_corpus(initialized_config, A_CORPUS)
    before = digest_of(initialized_config)

    report = rebuild(
        initialized_config,
        derivers=RECATEGORIZED,
        replay_passes=no_replay_passes,
        accept_content_change=True,
    )

    assert report.content_changed
    assert not report.change_was_expected
    assert digest_of(initialized_config) != before


def test_a_changed_derivation_version_makes_the_difference_a_recorded_one(
    initialized_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Read from the constant rather than written as literals: these are the
    # build's derivation version and its successor, and a copy of a number that
    # moves under you fails for a reason that says nothing about the behaviour
    # under test -- which is exactly what happened when the first derivers
    # landed and the version went to 2.
    shipped = derivation.DERIVATION_VERSION
    apply_corpus(initialized_config, A_CORPUS)
    before = digest_of(initialized_config)
    monkeypatch.setattr(derivation, "DERIVATION_VERSION", shipped + 1)

    report = rebuild(initialized_config, derivers=RECATEGORIZED, replay_passes=no_replay_passes)

    assert report.content_changed
    assert report.change_was_expected
    assert report.previous_derivation_versions == (shipped,)
    assert digest_of(initialized_config) != before
    with reading(initialized_config) as conn:
        recorded = conn.execute(
            select(derivation_versions.c.version).order_by(derivation_versions.c.version)
        ).scalars()
        assert list(recorded) == [shipped, shipped + 1]
        stamps = conn.execute(select(transactions.c.derivation_version_id).distinct()).scalars()
        assert list(stamps) == [report.derivation_version_id]


def test_a_rebuild_preserves_an_operator_override_the_archive_cannot_recreate(
    initialized_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 The one column the archive was never going to bring back.

    `transactions` carries a `raw_response_id`, so a rebuild empties it -- and
    `category_override` is the operator's own correction, which no deriver
    writes and no response contains. The digest guard would normally catch the
    loss, but a changed derivation version is the usual REASON to rebuild, and
    that is exactly the state in which the guard treats a content change as
    expected. So the rebuild reported success and every override was gone, with
    `money_summary(group_by="category")` quietly answering with the source's
    categories again.
    """
    apply_corpus(initialized_config, A_CORPUS)
    with writer_engine(initialized_config) as engine, engine.connect() as conn, transaction(conn):
        conn.execute(
            update(transactions)
            .where(transactions.c.source_transaction_id == "t-1")
            .values(category_override="Sabbatical")
        )
    monkeypatch.setattr(derivation, "DERIVATION_VERSION", derivation.DERIVATION_VERSION + 1)

    report = rebuild(initialized_config, derivers=RECATEGORIZED, replay_passes=no_replay_passes)

    assert report.change_was_expected
    with reading(initialized_config) as conn:
        overrides = {
            str(row[0]): row[1]
            for row in conn.execute(
                select(transactions.c.source_transaction_id, transactions.c.category_override)
            ).all()
        }
    assert overrides["t-1"] == "Sabbatical", "the rebuild reported success and lost the override"
    assert overrides["t-2"] is None, "an override was applied to a row that never had one"


def test_an_override_whose_row_the_replay_cannot_recreate_is_reported_not_dropped(
    initialized_config: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """A correction with nowhere to land is a loss, and losses are said out loud.

    The row it belonged to is not in the rebuilt table -- the response that
    produced it was pruned, or a changed deriver no longer emits it -- so there
    is nothing to carry the override onto. Restoring it against some other row
    would be worse than losing it; saying which correction was lost is what lets
    the operator put it back.
    """
    apply_corpus(initialized_config, A_CORPUS)
    with writer_engine(initialized_config) as engine, engine.connect() as conn, transaction(conn):
        conn.execute(
            update(transactions)
            .where(transactions.c.source_transaction_id == "t-1")
            .values(category_override="Sabbatical")
        )

    with caplog.at_level(logging.WARNING, logger="bankmachine"):
        rebuild(
            initialized_config,
            derivers=REIDENTIFIED,
            replay_passes=no_replay_passes,
            accept_content_change=True,
        )

    assert any("category_override" in record.getMessage() for record in caplog.records), (
        "an operator correction was dropped with nothing recording that it happened"
    )


def test_a_response_no_deriver_understands_stops_the_whole_rebuild(
    initialized_config: Config,
) -> None:
    # Skipping it would report success over a dataset missing whatever that
    # endpoint carried -- and every number in it would still add up.
    connection_id = apply_corpus(initialized_config, A_CORPUS)
    with writer_engine(initialized_config) as engine, engine.connect() as conn, transaction(conn):
        record_response(
            conn,
            connection_id=connection_id,
            endpoint="/test/holdings",
            body=b"{}",
            received_at=instant(3),
        )
    # Taken after the response lands, because the archive is part of what the
    # rebuild must leave alone.
    before = digest_of(initialized_config)

    with pytest.raises(UnknownEndpointError) as caught:
        rebuild(initialized_config, derivers=DERIVERS, replay_passes=no_replay_passes)

    assert "/test/holdings" in str(caught.value)
    with reading(initialized_config) as conn:
        # The archive keeps the response it could not interpret; only the
        # derivation was rolled back.
        assert (
            conn.execute(select(raw_responses.c.endpoint)).scalars().all().count("/test/holdings")
            == 1
        )
    assert digest_of(initialized_config) == before


def test_a_rebuild_does_not_delete_rows_the_archive_could_not_recreate(
    initialized_config: Config,
) -> None:
    # An imported row names a file, not a response. Nothing in the archive could
    # bring it back, so a rebuild that deleted it would be a deletion wearing a
    # rebuild's name.
    apply_corpus(initialized_config, A_CORPUS)
    with writer_engine(initialized_config) as engine, engine.connect() as conn, transaction(conn):
        account_id = conn.execute(select(accounts.c.account_id)).scalar_one()
        version_id = conn.execute(select(derivation_versions.c.derivation_version_id)).scalar_one()
        import_id = conn.execute(
            insert(manual_imports).values(
                account_id=account_id,
                adapter="csv",
                source_name="export.csv",
                file_sha256="0" * 64,
                file_bytes=100,
                imported_at=instant(2),
                rows_seen=1,
                rows_applied=1,
            )
        ).inserted_primary_key
        assert import_id is not None
        conn.execute(
            insert(transactions).values(
                account_id=account_id,
                pending=0,
                posted_date=calendar_date(DAY_ONE),
                amount_minor=minor_units(-99_00),
                currency="USD",
                description="from a statement export",
                source="manual",
                manual_import_id=int(import_id[0]),
                import_fingerprint="fp-1",
                derivation_version_id=int(version_id),
                first_seen_at=instant(2),
                updated_at=instant(2),
            )
        )
    with reading(initialized_config) as conn:
        imported_before = [row for row in dump(conn, "transactions") if "fp-1" in row]
    assert len(imported_before) == 1

    report = rebuild(initialized_config, derivers=DERIVERS, replay_passes=no_replay_passes)

    assert not report.content_changed
    with reading(initialized_config) as conn:
        assert [row for row in dump(conn, "transactions") if "fp-1" in row] == imported_before


def test_local_account_ids_survive_a_rebuild(initialized_config: Config) -> None:
    # AC-6.3 from the other direction: history points at `account_id`, so a
    # rebuild that renumbered accounts would orphan every imported row.
    apply_corpus(initialized_config, A_CORPUS)
    with reading(initialized_config) as conn:
        ids_before = dump(conn, "accounts")

    rebuild(initialized_config, derivers=DERIVERS, replay_passes=no_replay_passes)

    with reading(initialized_config) as conn:
        assert dump(conn, "accounts") == ids_before


def test_rebuilding_an_untouched_datastore_changes_nothing(initialized_config: Config) -> None:
    # The shipped path today: no derivers are registered, and an empty archive
    # gives them nothing to do.
    report = rebuild(initialized_config, derivers=DERIVERS, replay_passes=no_replay_passes)

    assert report.responses_replayed == 0
    assert not report.content_changed
    assert sum(report.rows_deleted.values()) == 0


def test_the_content_digest_refuses_a_value_the_schema_cannot_hold(
    initialized_config: Config,
) -> None:
    # SQLite stores a float wherever the column has no affinity to convert it,
    # and money that has been through a float has already lost fractions of a
    # cent. A digest that hashed it would certify the loss (AC-6.2).
    apply_corpus(initialized_config, A_CORPUS)
    with writer_engine(initialized_config) as engine, engine.connect() as conn, transaction(conn):
        conn.exec_driver_sql("UPDATE raw_responses SET body_gzip = 1.5")

    with reading(initialized_config) as conn, pytest.raises(UnhashableValueError):
        content_digest(conn)


_ENTRIES = st.lists(
    st.tuples(
        st.integers(min_value=0, max_value=30),
        st.integers(min_value=-1_000_000, max_value=1_000_000),
        st.text(max_size=30),
    ),
    max_size=6,
)


@given(
    batches=st.lists(_ENTRIES, max_size=4),
    balances=st.lists(st.integers(min_value=-(10**12), max_value=10**12), max_size=3),
)
@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_rebuild_is_lossless_for_any_corpus(
    tmp_path_factory: pytest.TempPathFactory,
    batches: Sequence[Sequence[tuple[int, int, str]]],
    balances: Sequence[int],
) -> None:
    """Losslessness is an invariant, so it is stated as one (per project preferences).

    The generated corpus varies in the dimensions a real one does: how many
    responses, how many rows each carries, which days they fall on, and whether
    the same transaction is seen more than once. Ids are assigned by position so
    a repeat is a deliberate case rather than a collision the test has to dodge.
    """
    corpus: list[tuple[str, dict[str, Any], UtcInstant]] = [accounts_response(0, 0)]
    for day, amount in enumerate(balances, start=1):
        corpus.append(accounts_response(day, amount))
    for index, batch in enumerate(batches):
        corpus.append(
            transactions_response(
                index,
                [
                    entry(f"t-{index}-{position}", day, amount, description)
                    for position, (day, amount, description) in enumerate(batch)
                ],
            )
        )

    with temporary_store(tmp_path_factory.mktemp("rebuild")) as config:
        apply_corpus(config, corpus)
        before = digest_of(config)
        with reading(config) as conn:
            rows_before = {name: dump(conn, name) for name in ("transactions", "balances_daily")}

        report = rebuild(config, derivers=DERIVERS, replay_passes=no_replay_passes)

        assert not report.content_changed
        assert report.responses_replayed == len(corpus)
        assert digest_of(config) == before
        with reading(config) as conn:
            for name, rows in rows_before.items():
                assert dump(conn, name) == rows
