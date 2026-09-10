"""`bankmachine sync run` — the loop, and the trap that makes it look correct.

🔴 A `NOT_READY` reply carries `has_more: false` and an empty `next_cursor`
*(measured, `api-notes-plaid.md` §17)*. So `while has_more:` terminates on the
first sync of every new connection and records a successful run over data that
has not materialized. These tests exist mostly to hold that ordering.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from bankmachine.cli import run
from bankmachine.cli.sync_run import MAX_PAGINATION_RESTARTS
from bankmachine.config import Config
from bankmachine.connector import (
    ACCOUNTS_GET,
    TRANSACTIONS_SYNC,
    FetchedResponse,
    TransactionsPaginationRestartError,
    TransportError,
)
from bankmachine.derivers import ALL_DERIVERS
from bankmachine.secrets import (
    SecretsError,
    delete_access_token,
    delete_plaid_secret,
    set_access_token,
    set_plaid_secret,
)
from bankmachine.store.derivation import apply_response
from bankmachine.store.engine import reader_connection, writer_connection
from bankmachine.store.schema import (
    TRANSACTIONS_DOMAIN,
    connections,
    institutions,
    sync_state,
    transactions,
)
from bankmachine.store.types import now_utc

SOURCE_ACCOUNT = "acct-checking"
ITEM_ID = "item-sync-tests"
ACCESS_TOKEN = "access-sandbox-sync-fake"  # credential-shape: test vector


def _accounts_body() -> bytes:
    return json.dumps(
        {
            "accounts": [
                {
                    "account_id": SOURCE_ACCOUNT,
                    "name": "Plaid Checking",
                    "mask": "0000",
                    "type": "depository",
                    "subtype": "checking",
                    "balances": {
                        "current": "110.94",
                        "available": "100.00",
                        "limit": None,
                        "iso_currency_code": "USD",
                    },
                }
            ],
            "item": {"item_id": ITEM_ID},
            "request_id": "req-accounts",
        }
    ).encode()


def _txn(transaction_id: str, amount: str = "12.00") -> dict[str, Any]:
    return {
        "account_id": SOURCE_ACCOUNT,
        "transaction_id": transaction_id,
        "amount": amount,
        "iso_currency_code": "USD",
        "date": "2026-09-07",
        "authorized_date": None,
        "pending": False,
        "pending_transaction_id": None,
        "name": "SparkFun",
        "merchant_name": "FUN",
        "personal_finance_category": {"primary": "GENERAL_MERCHANDISE", "detailed": "X"},
    }


def _page(
    *,
    added: list[dict[str, Any]] | None = None,
    next_cursor: str = "",
    has_more: bool = False,
    status: str = "HISTORICAL_UPDATE_COMPLETE",
) -> bytes:
    return json.dumps(
        {
            "accounts": [],
            "added": added or [],
            "modified": [],
            "removed": [],
            "next_cursor": next_cursor,
            "has_more": has_more,
            "transactions_update_status": status,
            "request_id": "req-sync",
        }
    ).encode()


class FakeClient:
    """Answers `transactions_sync` from a scripted list of pages."""

    pages: list[bytes | Exception] = []
    calls: list[str | None] = []
    fail_with: Exception | None = None
    #: Fail only for this access token, so one connection can fail while another
    #: succeeds -- which is the only way to observe AC-4.1 at all.
    fail_for_token: str | None = None
    accounts_calls: int = 0

    def __init__(self, config: Config, secret: str, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def accounts_get(self, access_token: str, **kwargs: Any) -> FetchedResponse:
        FakeClient.accounts_calls += 1
        if FakeClient.fail_for_token is not None and access_token == FakeClient.fail_for_token:
            raise TransportError("the aggregator is unreachable", endpoint=ACCOUNTS_GET)
        return FetchedResponse(
            endpoint=ACCOUNTS_GET,
            body=_accounts_body(),
            received_at=now_utc(),
            request_context=None,
        )

    def transactions_sync(
        self, access_token: str, *, cursor: str | None, **kwargs: Any
    ) -> FetchedResponse:
        FakeClient.calls.append(cursor)
        if FakeClient.fail_with is not None:
            raise FakeClient.fail_with
        index = min(len(FakeClient.calls) - 1, len(FakeClient.pages) - 1)
        page = FakeClient.pages[index]
        # A scripted page may be a refusal. The aggregator interleaves them with
        # bodies -- a mid-pagination mutation arrives between two good pages --
        # and a fake that could only fail for the whole run could not express
        # the sequence the loop is supposed to survive.
        if isinstance(page, Exception):
            raise page
        return FetchedResponse(
            endpoint=TRANSACTIONS_SYNC,
            body=page,
            received_at=now_utc(),
            request_context=None,
        )


@pytest.fixture(autouse=True)
def _reset() -> None:
    FakeClient.pages = []
    FakeClient.calls = []
    FakeClient.fail_with = None
    FakeClient.fail_for_token = None
    FakeClient.accounts_calls = 0


@pytest.fixture
def cli_env(initialized_config: Config, monkeypatch: pytest.MonkeyPatch) -> Iterator[Config]:
    config = initialized_config
    for key, value in {
        "BANKMACHINE_DATASTORE_PATH": str(config.datastore_path),
        "BANKMACHINE_LOG_DIR": str(config.log_dir),
        "BANKMACHINE_KEYCHAIN_SERVICE": config.keychain_service,
        "BANKMACHINE_ENVIRONMENT": config.environment,
        "BANKMACHINE_CONFIG": str(config.datastore_path.parent / "absent.toml"),
        "BANKMACHINE_PLAID_CLIENT_ID": "test-client-id",
    }.items():
        monkeypatch.setenv(key, value)
    set_plaid_secret(config, "test-secret")
    credential_ref = config.connection_keychain_account(ITEM_ID)
    set_access_token(config, credential_ref, ACCESS_TOKEN)

    now = now_utc()
    with writer_connection(config) as conn:
        primary_key = conn.execute(
            institutions.insert().values(
                source_institution_id="ins_109508",
                name="First Platypus Bank",
                first_seen_at=now,
                last_seen_at=now,
            )
        ).inserted_primary_key
        assert primary_key is not None
        conn.execute(
            connections.insert().values(
                institution_id=primary_key[0],
                source_connection_id=ITEM_ID,
                credential_ref=credential_ref,
                capabilities="[]",
                requested_history_days=730,
                status="active",
                enrolled_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        apply_response(
            conn,
            connection_id=1,
            endpoint=ACCOUNTS_GET.path,
            body=_accounts_body(),
            received_at=now,
            derivers=ALL_DERIVERS,
        )
    monkeypatch.setattr("bankmachine.cli.sync_run.PlaidClient", FakeClient)
    monkeypatch.setattr("bankmachine.cli.sync_run.time.sleep", lambda _s: None)
    try:
        yield config
    finally:
        delete_plaid_secret(config)
        for ref in (credential_ref, config.connection_keychain_account("item-two")):
            with contextlib.suppress(SecretsError):
                delete_access_token(config, ref)


def _txn_rows(config: Config) -> list[Any]:
    with reader_connection(config) as conn:
        return [r._mapping for r in conn.execute(select(transactions)).all()]


def _cursor(config: Config) -> str | None:
    with reader_connection(config) as conn:
        row = conn.execute(
            select(sync_state.c.cursor).where(sync_state.c.domain == TRANSACTIONS_DOMAIN)
        ).one_or_none()
    return None if row is None else row[0]


def _connection_row(config: Config) -> Any:
    with reader_connection(config) as conn:
        return conn.execute(select(connections)).one()._mapping


# --------------------------------------------------------------------------
# AC-2.6 — the trap
# --------------------------------------------------------------------------


def test_a_not_ready_first_page_is_not_reported_as_a_successful_empty_sync(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 The failure a `while has_more:` loop produces, and nothing raises for.

    A NOT_READY reply carries `has_more: false`, so the naive loop exits at once
    and stamps a success over a connection whose history has not materialized.
    The operator sees an account with no activity and no reason to doubt it.
    """
    FakeClient.pages = [_page(status="NOT_READY")]

    assert run(["sync", "run", "--no-wait"]) == 0

    assert _txn_rows(cli_env) == []
    assert _cursor(cli_env) is None, "an empty cursor was persisted"
    assert _connection_row(cli_env)["last_success_at"] is None, (
        "a connection that fetched nothing was recorded as having synced"
    )
    assert "still being prepared" in capsys.readouterr().out


def test_a_not_ready_connection_is_retried_rather_than_failed(cli_env: Config) -> None:
    """AC-2.6: backoff-and-retry, because a multi-year backfill takes time.

    It is not an error and must not degrade the connection — the aggregator is
    working exactly as intended.
    """
    FakeClient.pages = [
        _page(status="NOT_READY"),
        _page(status="NOT_READY"),
        _page(added=[_txn("t1")], next_cursor="cursor-1"),
    ]

    assert run(["sync", "run"]) == 0

    assert len(FakeClient.calls) == 3, "the run gave up instead of backing off"
    assert len(_txn_rows(cli_env)) == 1
    assert _connection_row(cli_env)["status"] == "active"


# --------------------------------------------------------------------------
# AC-2.1 — the page loop and its cursor
# --------------------------------------------------------------------------


def test_the_loop_pages_until_has_more_is_false(cli_env: Config) -> None:
    FakeClient.pages = [
        _page(added=[_txn("t1")], next_cursor="cursor-1", has_more=True),
        _page(added=[_txn("t2")], next_cursor="cursor-2", has_more=True),
        _page(added=[_txn("t3")], next_cursor="cursor-3"),
    ]

    assert run(["sync", "run"]) == 0

    assert len(_txn_rows(cli_env)) == 3
    assert _cursor(cli_env) == "cursor-3"


def test_each_page_resumes_from_the_cursor_the_last_one_stored(cli_env: Config) -> None:
    """🔴 Re-read from the datastore, not carried in memory.

    Carrying it would let the loop's idea of progress and the datastore's
    disagree — and the datastore is the one that survives a crash, so it is the
    one that decides where the next page starts.
    """
    FakeClient.pages = [
        _page(added=[_txn("t1")], next_cursor="cursor-1", has_more=True),
        _page(added=[_txn("t2")], next_cursor="cursor-2"),
    ]

    assert run(["sync", "run"]) == 0

    assert FakeClient.calls == [None, "cursor-1"], (
        "the second page did not resume from the cursor the first one committed"
    )


def test_a_second_run_is_a_no_op(cli_env: Config) -> None:
    """AC-2.4 at the command level: the cursor is what makes it cheap as well as safe."""
    FakeClient.pages = [_page(added=[_txn("t1")], next_cursor="cursor-1")]
    assert run(["sync", "run"]) == 0
    before = [dict(r) for r in _txn_rows(cli_env)]

    FakeClient.pages = [_page(next_cursor="cursor-1")]
    assert run(["sync", "run"]) == 0

    after = [dict(r) for r in _txn_rows(cli_env)]
    assert before == after
    assert FakeClient.calls[-1] == "cursor-1", "the re-run refetched from the beginning"


# --------------------------------------------------------------------------
# AC-4.1 — one failure never aborts another
# --------------------------------------------------------------------------


def test_a_failing_connection_is_recorded_and_the_run_reports_one(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 `1` — it ran and found a problem. `2` would mean it could not run.

    The api-contract norm calls that split non-collapsible because the scheduled
    job reads it: `2` here would make a degraded feed indistinguishable from a
    broken install, which is this product's primary failure mode arriving through
    the operational door.
    """
    FakeClient.fail_with = TransportError(
        "the aggregator is unreachable", endpoint=TRANSACTIONS_SYNC
    )

    assert run(["sync", "run"]) == 1

    row = _connection_row(cli_env)
    assert row["status"] == "degraded"
    assert row["last_error_code"] == "TransportError"
    assert row["last_error_at"] is not None
    assert "could not be synced" in capsys.readouterr().out


def test_one_connection_failing_does_not_stop_the_others(
    cli_env: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4.1 with two connections, which is the only way to observe it.

    A single-connection test cannot tell "the loop continued" from "there was
    nothing left to do".
    """
    now = now_utc()
    second_ref = cli_env.connection_keychain_account("item-two")
    second_token = "access-sandbox-second-fake"  # credential-shape: test vector
    set_access_token(cli_env, second_ref, second_token)
    with writer_connection(cli_env) as conn:
        primary_key = conn.execute(
            institutions.insert().values(
                source_institution_id="ins_second",
                name="Second Bank",
                first_seen_at=now,
                last_seen_at=now,
            )
        ).inserted_primary_key
        assert primary_key is not None
        conn.execute(
            connections.insert().values(
                institution_id=primary_key[0],
                source_connection_id="item-two",
                credential_ref=second_ref,
                capabilities="[]",
                requested_history_days=730,
                status="active",
                enrolled_at=now,
                created_at=now,
                updated_at=now,
            )
        )
    FakeClient.pages = [_page(added=[_txn("t1")], next_cursor="cursor-1")]
    # 🔴 An AGGREGATOR failure, not a missing credential. Both degrade the
    # connection, but they leave by different paths -- and a test that took the
    # credential path would say nothing about whether an aggregator error is
    # contained, which is what AC-4.1 is about.
    FakeClient.fail_for_token = second_token

    assert run(["sync", "run"]) == 1

    # The first connection synced; the second failed at the aggregator.
    assert len(_txn_rows(cli_env)) == 1
    with reader_connection(cli_env) as conn:
        rows = {
            int(r._mapping["connection_id"]): r._mapping["status"]
            for r in conn.execute(select(connections)).all()
        }
    assert rows[1] == "active"
    assert rows[2] == "degraded"


def test_a_mid_pagination_mutation_restarts_the_page_run_rather_than_degrading_it(
    cli_env: Config,
) -> None:
    """🔴 Ordinary on a long backfill, and invisible in sandbox.

    The aggregator raises this when the underlying data changes while a page run
    is in flight, and documents the remedy as starting again from the last
    cursor that was stored. Sandbox backfills are tiny and static, so tomorrow's
    first production sync is the first time this can happen at all -- and left
    unclassified it degrades the connection for the rest of the run, stopping
    that institution's history where the mutation happened.

    The loop is already built to do the right thing: it re-reads the cursor from
    the datastore before every request, so retrying after page one has committed
    IS the documented restart. Only the classification was missing.
    """
    FakeClient.pages = [
        _page(added=[_txn("t1")], next_cursor="cursor-1", has_more=True),
        TransactionsPaginationRestartError(
            "the data changed while the page run was in flight", endpoint=TRANSACTIONS_SYNC
        ),
        _page(added=[_txn("t2")], next_cursor="cursor-2"),
    ]

    assert run(["sync", "run"]) == 0

    assert _connection_row(cli_env)["status"] == "active"
    assert {r["source_transaction_id"] for r in _txn_rows(cli_env)} == {"t1", "t2"}
    assert FakeClient.calls == [None, "cursor-1", "cursor-1"], (
        "the restarted page run did not resume from the cursor page one committed"
    )
    assert _cursor(cli_env) == "cursor-2"


def test_a_page_run_that_never_stops_restarting_is_degraded_rather_than_hung(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """The bound that keeps "restart" from meaning "loop forever".

    A restart does not advance the page count, so a connection whose data never
    settles would spin against the aggregator with nothing ever reported. A
    nightly sync that never returns is a worse failure than one that says a
    connection is degraded, because nothing downstream can tell it from a
    machine that is merely slow.
    """
    FakeClient.fail_with = TransactionsPaginationRestartError(
        "the data changed while the page run was in flight", endpoint=TRANSACTIONS_SYNC
    )

    assert run(["sync", "run"]) == 1

    row = _connection_row(cli_env)
    assert row["status"] == "degraded"
    assert row["last_error_code"] == "TransactionsPaginationRestartError"
    assert len(FakeClient.calls) == MAX_PAGINATION_RESTARTS + 1, (
        "the restart budget is not what bounded the run"
    )
    assert "could not be synced" in capsys.readouterr().out


def test_a_recovered_connection_stops_being_degraded(cli_env: Config) -> None:
    """A status that only ever goes one way is a status nobody can trust."""
    FakeClient.fail_with = TransportError("unreachable", endpoint=TRANSACTIONS_SYNC)
    assert run(["sync", "run"]) == 1
    assert _connection_row(cli_env)["status"] == "degraded"

    FakeClient.fail_with = None
    FakeClient.pages = [_page(added=[_txn("t1")], next_cursor="cursor-1")]
    assert run(["sync", "run"]) == 0

    row = _connection_row(cli_env)
    assert row["status"] == "active"
    assert row["last_error_code"] is None
    assert row["last_success_at"] is not None


def test_an_absent_datastore_exits_two(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """`2` — could not run, as distinct from ran and found a problem."""
    for key, value in {
        "BANKMACHINE_DATASTORE_PATH": str(config.datastore_path),
        "BANKMACHINE_LOG_DIR": str(config.log_dir),
        "BANKMACHINE_KEYCHAIN_SERVICE": config.keychain_service,
        "BANKMACHINE_CONFIG": str(config.datastore_path.parent / "absent.toml"),
    }.items():
        monkeypatch.setenv(key, value)

    assert run(["sync", "run"]) == 2


def test_syncing_with_no_connections_is_not_a_problem(
    initialized_config: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing enrolled is a state, not a failure — and it says what to do about it."""
    for key, value in {
        "BANKMACHINE_DATASTORE_PATH": str(initialized_config.datastore_path),
        "BANKMACHINE_LOG_DIR": str(initialized_config.log_dir),
        "BANKMACHINE_KEYCHAIN_SERVICE": initialized_config.keychain_service,
        "BANKMACHINE_CONFIG": str(initialized_config.datastore_path.parent / "absent.toml"),
    }.items():
        monkeypatch.setenv(key, value)

    assert run(["sync", "run"]) == 0
    assert "enroll" in capsys.readouterr().out


def test_a_retired_connection_is_not_synced(cli_env: Config) -> None:
    """Retirement means it stops syncing; that is what the operator asked for."""
    with writer_connection(cli_env) as conn:
        # Both columns, because the schema enforces their agreement:
        # CHECK ((status = 'retired') = (retired_at IS NOT NULL)).
        conn.execute(connections.update().values(status="retired", retired_at=now_utc()))
    FakeClient.pages = [_page(added=[_txn("t1")], next_cursor="cursor-1")]

    assert run(["sync", "run"]) == 0

    assert FakeClient.calls == []
    assert _txn_rows(cli_env) == []


def test_every_run_refreshes_accounts_before_paging_transactions(cli_env: Config) -> None:
    """🔴 Nothing else in the product ever called `accounts_get`.

    The transactions deriver refuses a row whose account it has no record of --
    on purpose, because skipping one would let the cursor advance past a
    transaction that is then never offered again. So without this fetch, the
    first real sync after a real enrollment would refuse every transaction it
    received, and the product would look like it had no data rather than like it
    had a bug.

    Re-fetched every run rather than once at enrollment: accounts open, close and
    get renamed, and a roster frozen on linking day would silently rot.
    """
    FakeClient.pages = [_page(added=[_txn("t1")], next_cursor="cursor-1")]

    assert run(["sync", "run"]) == 0
    assert FakeClient.accounts_calls == 1

    assert run(["sync", "run"]) == 0
    assert FakeClient.accounts_calls == 2, "a later run stopped refreshing the account roster"


def test_a_transaction_for_a_newly_opened_account_is_applied_not_refused(
    cli_env: Config,
) -> None:
    """The case the per-run refresh exists for.

    An account opened since the last sync arrives in the same run as its first
    transactions. Fetching accounts only at enrollment would refuse them, and the
    refusal would look like an aggregator problem rather than a stale roster.
    """
    # Balances first: they carry a foreign key to accounts, and the schema
    # refuses to orphan them. Simulating "this account did not exist last run"
    # has to respect that, which is the constraint doing its job.
    with writer_connection(cli_env) as conn:
        conn.execute(transactions.delete())
        conn.exec_driver_sql("DELETE FROM balances_daily")
        conn.exec_driver_sql("DELETE FROM accounts")

    FakeClient.pages = [_page(added=[_txn("t1")], next_cursor="cursor-1")]

    assert run(["sync", "run"]) == 0
    assert len(_txn_rows(cli_env)) == 1


def test_syncing_an_unknown_connection_is_reported_not_silently_fine(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 It used to fall through to "no live connections" and exit 0.

    Asking for a connection that is retired or mistyped and being told everything
    is fine is how an operator concludes a connection is syncing when it is not —
    and a scheduled job reading exit codes would agree with them.
    """
    assert run(["sync", "run", "--connection", "999"]) == 1

    err = capsys.readouterr().err
    assert "no live connection 999" in err
    assert FakeClient.calls == []


def test_syncing_one_connection_leaves_the_others_alone(cli_env: Config) -> None:
    """`--connection` narrows the run, which is the whole point of the flag."""
    now = now_utc()
    second_ref = cli_env.connection_keychain_account("item-two")
    set_access_token(cli_env, second_ref, "access-sandbox-second")  # credential-shape: test vector
    with writer_connection(cli_env) as conn:
        primary_key = conn.execute(
            institutions.insert().values(
                source_institution_id="ins_second",
                name="Second Bank",
                first_seen_at=now,
                last_seen_at=now,
            )
        ).inserted_primary_key
        assert primary_key is not None
        conn.execute(
            connections.insert().values(
                institution_id=primary_key[0],
                source_connection_id="item-two",
                credential_ref=second_ref,
                capabilities="[]",
                requested_history_days=730,
                status="active",
                enrolled_at=now,
                created_at=now,
                updated_at=now,
            )
        )
    FakeClient.pages = [_page(added=[_txn("t1")], next_cursor="cursor-1")]

    assert run(["sync", "run", "--connection", "1"]) == 0

    assert FakeClient.accounts_calls == 1, "the narrowed run touched a second connection"


def test_a_run_that_hits_the_page_ceiling_says_it_stopped_short(
    cli_env: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 "500 pages applied" with no further word reads as finished.

    And the operator would have no reason to run again — while the cursor sat
    mid-history. Not degraded: nothing is wrong, the run is bounded, and the next
    one continues exactly here.
    """
    monkeypatch.setattr("bankmachine.cli.sync_run.MAX_PAGES_PER_RUN", 2)
    FakeClient.pages = [
        _page(added=[_txn("t1")], next_cursor="c1", has_more=True),
        _page(added=[_txn("t2")], next_cursor="c2", has_more=True),
        _page(added=[_txn("t3")], next_cursor="c3", has_more=True),
    ]

    assert run(["sync", "run"]) == 0

    out = capsys.readouterr().out
    assert "stopped at the page ceiling" in out
    assert "run again to continue" in out
    assert _cursor(cli_env) == "c2", "the cursor must sit where the bounded run stopped"


def test_a_bounded_run_resumes_from_where_it_stopped(
    cli_env: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Which is what makes the ceiling a bound rather than data loss."""
    monkeypatch.setattr("bankmachine.cli.sync_run.MAX_PAGES_PER_RUN", 2)
    FakeClient.pages = [
        _page(added=[_txn("t1")], next_cursor="c1", has_more=True),
        _page(added=[_txn("t2")], next_cursor="c2", has_more=True),
    ]
    assert run(["sync", "run"]) == 0
    assert len(_txn_rows(cli_env)) == 2

    FakeClient.calls = []
    FakeClient.pages = [_page(added=[_txn("t3")], next_cursor="c3")]
    assert run(["sync", "run"]) == 0

    assert FakeClient.calls[0] == "c2", "the resumed run refetched from the beginning"
    assert len(_txn_rows(cli_env)) == 3


# --------------------------------------------------------------------------
# AC-1.3a / AC-11.8 — the granted window, measurable for the first time
# --------------------------------------------------------------------------


def _granted(config: Config) -> int | None:
    with reader_connection(config) as conn:
        value = conn.execute(select(connections.c.granted_history_days)).scalar_one()
    return None if value is None else int(value)


def test_the_granted_window_is_not_computed_before_the_backfill_completes(
    cli_env: Config,
) -> None:
    """🔴 The confidently-wrong number this whole gate exists to prevent.

    At INITIAL_UPDATE_COMPLETE the backfill is still arriving, so the oldest
    transaction present is the oldest one *so far*. Measuring there records a
    shortfall that does not exist — well-formed, plausible, and wrong — and
    AC-11.8 would then report a gap against history the operator actually has.
    """
    FakeClient.pages = [
        _page(added=[_txn("t1")], next_cursor="c1", status="INITIAL_UPDATE_COMPLETE")
    ]

    assert run(["sync", "run"]) == 0

    assert _granted(cli_env) is None, "a window was measured against a backfill in flight"


def test_the_granted_window_is_recorded_once_history_is_complete(cli_env: Config) -> None:
    """AC-1.3a: null stops meaning "not yet known" only at this point."""
    old = "2025-09-08"
    entry = _txn("t1")
    entry["date"] = old
    FakeClient.pages = [_page(added=[entry], next_cursor="c1", status="HISTORICAL_UPDATE_COMPLETE")]

    assert run(["sync", "run"]) == 0

    granted = _granted(cli_env)
    assert granted is not None
    # A year of history, give or take the day the test runs on.
    assert 360 <= granted <= 372, granted
    with reader_connection(cli_env) as conn:
        start = conn.execute(select(sync_state.c.history_start_date)).scalar_one()
    assert str(start) == old


def test_a_shortfall_against_the_requested_window_is_reported(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 AC-11.8: a shortfall is a recorded gap, never a returned window read as complete.

    The connection asked for 730 days and got about one year. That difference is
    the operator's one chance to act — widening it means re-linking (AC-1.2), and
    they will not get another prompt.
    """
    entry = _txn("t1")
    entry["date"] = "2025-09-08"
    FakeClient.pages = [_page(added=[entry], next_cursor="c1", status="HISTORICAL_UPDATE_COMPLETE")]

    assert run(["sync", "run"]) == 0

    out = capsys.readouterr().out
    # The rendered phrase, not the bare word "gap": that substring survived the
    # line reading `a 8-day gap`, which is wrong for every shortfall of 8, 11, 18
    # or 80 days. Matched as a pattern rather than a literal because the granted
    # window is measured against the real clock, so the number moves by a day.
    assert re.search(r"a gap of \d+ days", out), out
    assert "re-linking" in out


def test_a_connection_with_no_transactions_leaves_the_window_unmeasured(
    cli_env: Config,
) -> None:
    """Zero would claim a measurement nobody made.

    Nothing was granted that can be counted, and a zero here would show as a
    730-day shortfall on an account that simply has no activity.
    """
    FakeClient.pages = [_page(next_cursor="c1", status="HISTORICAL_UPDATE_COMPLETE")]

    assert run(["sync", "run"]) == 0

    assert _granted(cli_env) is None


def test_a_bounded_run_does_not_claim_the_connection_is_up_to_date(
    cli_env: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 `last_success_at` is what the freshness warning reads.

    A run stopped by the page ceiling has NOT finished — `has_more` was still
    true — so advancing that stamp would tell every later reader the connection
    is current while it sits mid-history. The error state IS cleared, because the
    run really did fetch successfully; the two halves are separate for that
    reason.
    """
    monkeypatch.setattr("bankmachine.cli.sync_run.MAX_PAGES_PER_RUN", 1)
    FakeClient.pages = [_page(added=[_txn("t1")], next_cursor="c1", has_more=True)]

    assert run(["sync", "run"]) == 0

    row = _connection_row(cli_env)
    assert row["last_success_at"] is None, "a run that stopped short claimed to be up to date"
    assert row["status"] == "active", "the run succeeded at what it did do"


def test_a_complete_run_does_stamp_the_success(cli_env: Config) -> None:
    """The mirror half, without which the rule above is satisfied by never stamping."""
    FakeClient.pages = [_page(added=[_txn("t1")], next_cursor="c1")]

    assert run(["sync", "run"]) == 0

    assert _connection_row(cli_env)["last_success_at"] is not None


def test_the_granted_window_is_measured_once_and_never_re_measured(
    cli_env: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 `HISTORICAL_UPDATE_COMPLETE` is a persistent STATE, not an event.

    Every later sync reports it too. Re-measuring oldest-held-to-today grows the
    window by a day per day, so the AC-11.8 shortfall shrinks to nothing on its
    own and the `gapped` warning quietly stops being emitted while the missing
    history stays missing — the silent-staleness failure this product exists to
    prevent, produced by its own bookkeeping.
    """
    entry = _txn("t1")
    entry["date"] = "2025-09-08"
    FakeClient.pages = [_page(added=[entry], next_cursor="c1", status="HISTORICAL_UPDATE_COMPLETE")]
    assert run(["sync", "run"]) == 0
    first = _granted(cli_env)
    assert first is not None

    # 🔴 A year later. The drift is TIME-based, not data-based: the window is
    # measured as (today - oldest transaction), so it grows by a day per day
    # while the oldest transaction stays exactly where it is. A test that only
    # changed the DATA would not see this — I wrote that one first and it passed
    # against the unguarded code.
    # `timedelta`, not `.replace(year=...)`: the latter raises on Feb 29, so the
    # test would fail once every four years for a reason unrelated to what it asserts.
    a_year_on = now_utc() + timedelta(days=365)
    monkeypatch.setattr("bankmachine.cli.sync_run.now_utc", lambda: a_year_on)
    FakeClient.pages = [
        _page(added=[_txn("t2")], next_cursor="c2", status="HISTORICAL_UPDATE_COMPLETE")
    ]
    assert run(["sync", "run"]) == 0

    assert _granted(cli_env) == first, (
        "the granted window was re-measured, so the recorded shortfall drifts and "
        "eventually disappears while the missing history stays missing"
    )


def test_a_later_run_still_reports_the_shortfall_it_did_not_measure(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 The gap is still true on the runs that did not find it.

    Measuring once is right; going quiet about it is not. The summary line read a
    field set only on the measuring run, so the shortfall vanished from every
    later report while the missing history stayed missing — the same
    silent-staleness shape as the drift the measure-once guard fixed, arriving
    from the other side.
    """
    entry = _txn("t1")
    entry["date"] = "2025-09-08"
    FakeClient.pages = [_page(added=[entry], next_cursor="c1", status="HISTORICAL_UPDATE_COMPLETE")]
    assert run(["sync", "run"]) == 0
    assert "gap" in capsys.readouterr().out

    FakeClient.pages = [_page(next_cursor="c2", status="HISTORICAL_UPDATE_COMPLETE")]
    assert run(["sync", "run"]) == 0

    assert "gap" in capsys.readouterr().out, "a later run went quiet about a gap that still exists"
