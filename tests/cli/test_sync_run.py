"""`bankmachine sync run` — the loop, and the trap that makes it look correct.

🔴 A `NOT_READY` reply carries `has_more: false` and an empty `next_cursor`
*(measured, `api-notes-plaid.md` §17)*. So `while has_more:` terminates on the
first sync of every new connection and records a successful run over data that
has not materialized. These tests exist mostly to hold that ordering.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from bankmachine import query
from bankmachine.cli import build_parser, run, sync_run
from bankmachine.cli.sync_run import MAX_PAGINATION_RESTARTS
from bankmachine.config import Config
from bankmachine.connector import (
    ACCOUNTS_GET,
    INVESTMENTS_HOLDINGS_GET,
    INVESTMENTS_TRANSACTIONS_GET,
    TRANSACTIONS_SYNC,
    FetchedResponse,
    InstitutionUnavailableError,
    ReauthRequiredError,
    TransactionsPaginationRestartError,
    TransportError,
)
from bankmachine.connector.plaid.client import INVESTMENT_TRANSACTIONS_PAGE_SIZE
from bankmachine.connector.plaid.window import investment_window_context
from bankmachine.derivers import ALL_DERIVERS, all_replay_passes
from bankmachine.secrets import (
    SecretsError,
    delete_access_token,
    delete_plaid_secret,
    set_access_token,
    set_plaid_secret,
)
from bankmachine.store.connection import AnotherWriterRunningError
from bankmachine.store.derivation import apply_response
from bankmachine.store.engine import reader_connection, writer_connection
from bankmachine.store.rebuild import RebuildReport, content_digest, rebuild
from bankmachine.store.schema import (
    INVESTMENTS_DOMAIN,
    TRANSACTIONS_DOMAIN,
    accounts,
    connections,
    holdings,
    institutions,
    investment_transactions,
    sync_state,
    transactions,
)
from bankmachine.store.types import UtcInstant, calendar_date, now_utc, utc_instant
from conftest import use_cli_env

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


SECURITY_ID = "sec-index-fund"


def _holdings_body(*, quantity: str = "10", value: str = "1855.875") -> bytes:
    """One position, in the shape `api-notes-plaid.md` §22 recorded.

    The sub-cent default value is not decoration: four of thirteen positions in
    the live capture carry more precision than the cent, so a fake that only ever
    sent round numbers would exercise a path the aggregator does not use.
    """
    return json.dumps(
        {
            "accounts": [],
            "holdings": [
                {
                    "account_id": SOURCE_ACCOUNT,
                    "security_id": SECURITY_ID,
                    "quantity": quantity,
                    "institution_price": "24.5",
                    "institution_price_as_of": "2021-05-25",
                    "institution_value": value,
                    "cost_basis": "1666.5",
                    "iso_currency_code": "USD",
                    "unofficial_currency_code": None,
                }
            ],
            "securities": [
                {
                    "security_id": SECURITY_ID,
                    "name": "Dreyfus Index Fund",
                    "ticker_symbol": "DBLTX",
                    "cusip": None,
                    "isin": None,
                    "type": "mutual fund",
                    "close_price": None,
                    "close_price_as_of": None,
                    "iso_currency_code": "USD",
                    "unofficial_currency_code": None,
                }
            ],
            "item": {"item_id": ITEM_ID},
            "request_id": "req-holdings",
        }
    ).encode()


def _investment_transactions_body(
    entries: list[dict[str, Any]] | None = None,
    *,
    total: int | None = None,
    state_total: bool = True,
) -> bytes:
    """One page of investment transactions, in the shape §26 recorded.

    `total` defaults to the number of entries, which is the EXHAUSTED case --
    the window came back whole. A caller that wants the bounded case passes a
    larger total, which is what the live capture looked like: 100 of 1169.

    `state_total=False` omits `total_investment_transactions` altogether, which
    is the UNMEASURED window: nothing says how many rows the window holds, so
    nothing can be concluded about what is missing from it.
    """
    rows = [_investment_txn("inv-1")] if entries is None else entries
    return json.dumps(
        {
            "accounts": [],
            "securities": [
                {
                    "security_id": SECURITY_ID,
                    "name": "Dreyfus Index Fund",
                    "ticker_symbol": "DBLTX",
                    "type": "mutual fund",
                    "iso_currency_code": "USD",
                    "unofficial_currency_code": None,
                }
            ],
            "investment_transactions": rows,
            "item": {"item_id": ITEM_ID},
            "request_id": "req-investment-transactions",
        }
        | (
            {"total_investment_transactions": len(rows) if total is None else total}
            if state_total
            else {}
        )
    ).encode()


def _investment_txn(
    transaction_id: str, *, amount: str = "1.10", date: str = "2026-09-07"
) -> dict[str, Any]:
    """One investment transaction. Positive `amount` = cash debited (§26)."""
    return {
        "account_id": SOURCE_ACCOUNT,
        "investment_transaction_id": transaction_id,
        "security_id": SECURITY_ID,
        "date": date,
        "name": "BUY DREYFUS INDEX",
        "quantity": "0.520877874205698",
        "amount": amount,
        "price": "2.16",
        "fees": "7.99",
        "type": "buy",
        "subtype": "buy",
        "iso_currency_code": "USD",
        "unofficial_currency_code": None,
        "cancel_transaction_id": None,
        "transaction_datetime": None,
    }


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
    #: The tokens `/investments/holdings/get` was called with, in order. A list
    #: rather than a count because the gate's whole question is WHICH connections
    #: reached the endpoint, and a count cannot tell two connections apart.
    holdings_tokens: list[str] = []
    #: What the investments call raises, if anything. Its own switch rather than
    #: `fail_with`, because the whole question is whether ONE endpoint failing
    #: takes the others down with it -- a fake that could only fail everywhere
    #: could not express the case.
    holdings_error: Exception | None = None
    #: The offsets `/investments/transactions/get` was asked for, in order. The
    #: sequence IS the behaviour under test: this endpoint has no cursor, so a
    #: loop that failed to advance the offset would page forever against the
    #: same rows and a count could not tell that apart from working.
    investment_transaction_offsets: list[int] = []
    #: The pages it answers with, scripted like `pages`. The last one repeats,
    #: so a loop asking past the end gets a consistent answer rather than an
    #: IndexError that would read as a crash instead of a bug.
    investment_transaction_pages: list[bytes] = []
    investment_transactions_error: Exception | None = None
    #: The `(start_date, end_date)` pairs it was asked for, in order.
    investment_transaction_windows: list[tuple[date, date]] = []
    #: The instant each page is stamped with, parallel to the pages. Empty means
    #: "use the clock", which is what every test that does not care does.
    investment_transaction_received_ats: list[UtcInstant] = []
    #: How far through the CURRENT window the fake has served, reset by an
    #: `offset == 0` request. See `investments_transactions_get`.
    investment_page_cursor: int = 0

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

    def investments_holdings_get(self, access_token: str, **kwargs: Any) -> FetchedResponse:
        FakeClient.holdings_tokens.append(access_token)
        if FakeClient.holdings_error is not None:
            raise FakeClient.holdings_error
        return FetchedResponse(
            endpoint=INVESTMENTS_HOLDINGS_GET,
            body=_holdings_body(),
            received_at=now_utc(),
            request_context=None,
        )

    def investments_transactions_get(
        self,
        access_token: str,
        *,
        start_date: date,
        end_date: date,
        offset: int,
        count: int = INVESTMENT_TRANSACTIONS_PAGE_SIZE,
        **kwargs: Any,
    ) -> FetchedResponse:
        # 🔴 The window is NAMED rather than absorbed into `**kwargs`. AC-3.3 has
        # two clauses and this is the only place the gate can see the first one:
        # a fake that swallowed these dates would let "request the full
        # configured window" go untested, and the same pair becomes the bound on
        # which stored rows the reconciliation may retire.
        FakeClient.investment_transaction_windows.append((start_date, end_date))
        FakeClient.investment_transaction_offsets.append(offset)
        if FakeClient.investment_transactions_error is not None:
            raise FakeClient.investment_transactions_error
        # 🔴 Which page to serve is keyed on THIS window's progress, not on how
        # many times the fake has ever been called. `offset == 0` starts a
        # window, which is the real protocol's own boundary (there is no cursor
        # to mark one). Counting calls instead made a second sync inside one
        # test resume at the previous run's index and serve page two twice --
        # so a test that meant "two distinct pages, the last one closes the
        # window" silently exercised one page served twice.
        if offset == 0:
            FakeClient.investment_page_cursor = 0
        else:
            FakeClient.investment_page_cursor += 1
        page_index = min(
            FakeClient.investment_page_cursor,
            len(FakeClient.investment_transaction_pages) - 1,
        )
        page = FakeClient.investment_transaction_pages[page_index]
        # 🔴 The page's own instant, scriptable. `removed_at` is taken from the
        # ARCHIVE rather than the clock so a rebuild can reproduce it, and a
        # fake that always stamped `now_utc()` would make the clock and the
        # archive indistinguishable -- so nothing could tell the two sources
        # apart and restoring the clock would stay green.
        received = (
            FakeClient.investment_transaction_received_ats[
                min(page_index, len(FakeClient.investment_transaction_received_ats) - 1)
            ]
            if FakeClient.investment_transaction_received_ats
            else now_utc()
        )
        return FetchedResponse(
            endpoint=INVESTMENTS_TRANSACTIONS_GET,
            body=page,
            received_at=received,
            # 🔴 Built by the production formatter, not spelled out here. The
            # reply does not echo the window back (§26), so this string is the
            # only record of the question a page answered -- and `store rebuild`
            # reassembles every window from it. A fake that archived `None`, or
            # its own wording, would leave the whole replay path exercised by
            # nothing while every sync test stayed green.
            request_context=investment_window_context(
                start_date=start_date, end_date=end_date, offset=offset, count=count
            ),
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
    FakeClient.holdings_tokens = []
    FakeClient.holdings_error = None
    FakeClient.investment_transaction_offsets = []
    FakeClient.investment_transaction_pages = [_investment_transactions_body()]
    FakeClient.investment_transactions_error = None
    FakeClient.investment_transaction_windows = []
    FakeClient.investment_transaction_received_ats = []
    FakeClient.investment_page_cursor = 0


@pytest.fixture
def cli_env(initialized_config: Config, monkeypatch: pytest.MonkeyPatch) -> Iterator[Config]:
    config = use_cli_env(
        monkeypatch, initialized_config, BANKMACHINE_PLAID_CLIENT_ID="test-client-id"
    )
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

    `75`, not `0`: a scheduled runner reads the exit code and nothing else, and
    "nothing has arrived yet" has to reach it as *come back* rather than as
    *done*.
    """
    FakeClient.pages = [_page(status="NOT_READY")]

    assert run(["sync", "run", "--no-wait"]) == 75

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


def _enroll_a_second_connection(config: Config, *, capabilities: str = "[]") -> str:
    """A second live connection, and the access token that reaches it.

    AC-4.1 is only observable across two connections: with one, "the loop
    continued" is indistinguishable from "there was nothing left to do".
    """
    now = now_utc()
    second_ref = config.connection_keychain_account("item-two")
    second_token = "access-sandbox-second-fake"  # credential-shape: test vector
    set_access_token(config, second_ref, second_token)
    with writer_connection(config) as conn:
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
                capabilities=capabilities,
                requested_history_days=730,
                status="active",
                enrolled_at=now,
                created_at=now,
                updated_at=now,
            )
        )
    return second_token


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
    assert row["last_error_code"] == "AGGREGATOR_UNREACHABLE"
    assert row["last_error_at"] is not None
    assert "could not be synced" in capsys.readouterr().out


def test_an_expired_login_is_reported_with_the_repair_that_keeps_the_history(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 AC-4.3 reaching the one surface an operator actually reads.

    The move an operator makes when told their login expired is to enrol the
    institution again, and that mints a SECOND item whose roster re-issues every
    account and transaction id -- doubling every total with no warning naming it.
    The run is where they learn it, so the run is where the repair is named, with
    the connection id already filled in.
    """
    FakeClient.fail_with = ReauthRequiredError(
        "the login for this item has expired",
        endpoint=TRANSACTIONS_SYNC,
        error_code="ITEM_LOGIN_REQUIRED",
    )

    assert run(["sync", "run"]) == 1

    # AC-4.2: the aggregator's own code, verbatim -- not the class it was filed under.
    assert _connection_row(cli_env)["last_error_code"] == "ITEM_LOGIN_REQUIRED"
    out = capsys.readouterr().out
    assert "connections reauth 1" in out
    # The reason the line exists at all: without it the remedy an operator
    # reaches for is the one that duplicates.
    assert "duplicate" in out


def test_a_failure_that_is_not_an_expired_login_offers_no_repair(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """The negative control, and it is the half that decays.

    An unreachable aggregator is not repaired by re-authenticating, and a line
    printed under every degradation is one an operator learns to skip -- which
    costs exactly the case above, where reading it is the whole point. Same
    reasoning as `enroll`'s signpost, which is absent when nothing is live.
    """
    FakeClient.fail_with = TransportError(
        "the aggregator is unreachable", endpoint=TRANSACTIONS_SYNC
    )

    assert run(["sync", "run"]) == 1

    assert "connections reauth" not in capsys.readouterr().out


def test_one_connection_failing_does_not_stop_the_others(
    cli_env: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4.1 with two connections, which is the only way to observe it.

    A single-connection test cannot tell "the loop continued" from "there was
    nothing left to do".
    """
    second_token = _enroll_a_second_connection(cli_env)
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


def test_a_datastore_failure_on_one_connection_leaves_the_others_to_sync(
    cli_env: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 AC-4.1's hole: a locked datastore, not an aggregator refusal.

    `_persist` takes the exclusive writer lock per page and does not wait, so an
    ordinary `store backup` running beside a sync raises `AnotherWriterRunningError`
    -- a `StoreError`, which is neither a `ConnectorError` nor a `DerivationError`.
    It escaped the per-connection catch, escaped the run, and was reported as a
    command that could not run at all, skipping every connection after it. The
    requirement is that one broken connection never aborts another, and the
    datastore was the counterexample.
    """
    second_token = _enroll_a_second_connection(cli_env)
    FakeClient.pages = [_page(added=[_txn("t1")], next_cursor="cursor-1")]
    real_persist = sync_run._persist

    def persist(config: Config, fetched: FetchedResponse, connection_id: int) -> None:
        if connection_id == 1:
            raise AnotherWriterRunningError("another writer holds the datastore")
        real_persist(config, fetched, connection_id)

    monkeypatch.setattr(sync_run, "_persist", persist)

    assert run(["sync", "run"]) == 1, "a locked datastore was reported as could-not-run"

    with reader_connection(cli_env) as conn:
        rows = {
            int(r._mapping["connection_id"]): r._mapping["status"]
            for r in conn.execute(select(connections)).all()
        }
    assert rows[1] == "degraded"
    assert rows[2] == "active", "the second connection never got its turn"
    assert second_token  # the second connection is the one that had to succeed
    with reader_connection(cli_env) as conn:
        code = conn.execute(
            select(connections.c.last_error_code).where(connections.c.connection_id == 1)
        ).scalar_one()
    # No aggregator was involved, so the code is this product's -- and it has to
    # say "wait it out", which a class name never did.
    assert code == "DATASTORE_LOCKED"
    assert len(_txn_rows(cli_env)) == 1


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
        "the data changed while the page run was in flight",
        endpoint=TRANSACTIONS_SYNC,
        error_code="TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION",
    )

    assert run(["sync", "run"]) == 1

    row = _connection_row(cli_env)
    assert row["status"] == "degraded"
    assert row["last_error_code"] == "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"
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
    one continues exactly here. `75` is exactly that sentence in the one channel
    a scheduled runner can read.
    """
    monkeypatch.setattr("bankmachine.cli.sync_run.MAX_PAGES_PER_RUN", 2)
    FakeClient.pages = [
        _page(added=[_txn("t1")], next_cursor="c1", has_more=True),
        _page(added=[_txn("t2")], next_cursor="c2", has_more=True),
        _page(added=[_txn("t3")], next_cursor="c3", has_more=True),
    ]

    assert run(["sync", "run"]) == 75

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
    assert run(["sync", "run"]) == 75
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

    assert run(["sync", "run"]) == 75

    assert _granted(cli_env) is None, "a window was measured against a backfill in flight"


def test_the_granted_window_is_recorded_once_history_is_complete(cli_env: Config) -> None:
    """AC-1.3a: null stops meaning "not yet known" only at this point.

    🔴 The oldest transaction is dated RELATIVE to today and the granted window
    asserted against that same offset. A fixed date against an absolute
    day-count bound is a test with an expiry date: the previous form asserted
    `360 <= granted <= 372` for a row dated `2025-09-08`, which would have gone
    red of its own accord days after it was written and blocked every commit
    until somebody widened the numbers.
    """
    oldest = now_utc().date() - timedelta(days=365)
    entry = _txn("t1")
    entry["date"] = oldest.isoformat()
    FakeClient.pages = [_page(added=[entry], next_cursor="c1", status="HISTORICAL_UPDATE_COMPLETE")]

    assert run(["sync", "run"]) == 0

    granted = _granted(cli_env)
    assert granted is not None
    # That offset exactly -- the window is not rounded and does not drift. 366
    # is admitted for the one case that is not drift: a run straddling UTC
    # midnight between this fixture's `now` and the code's.
    assert granted in (365, 366), granted
    with reader_connection(cli_env) as conn:
        start = conn.execute(select(sync_state.c.history_start_date)).scalar_one()
    assert str(start) == oldest.isoformat()


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

    assert run(["sync", "run"]) == 75

    row = _connection_row(cli_env)
    assert row["last_success_at"] is None, "a run that stopped short claimed to be up to date"
    assert row["status"] == "active", "the run succeeded at what it did do"


def test_a_complete_run_does_stamp_the_success(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """The mirror half, without which the rule above is satisfied by never stamping.

    It is also the mirror of the two rules below it: a run that reached
    `HISTORICAL_UPDATE_COMPLETE` exits `0` and says nothing about history still
    arriving, so neither the gate nor the new sentence can be satisfied by
    applying them to every run.
    """
    FakeClient.pages = [_page(added=[_txn("t1")], next_cursor="c1")]

    assert run(["sync", "run"]) == 0

    assert _connection_row(cli_env)["last_success_at"] is not None
    assert "still arriving" not in capsys.readouterr().out, (
        "a finished backfill was reported as still arriving"
    )


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


# --------------------------------------------------------------------------
# The initial backfill: applied pages are not a finished history
# --------------------------------------------------------------------------


def test_a_run_whose_history_is_still_arriving_asks_to_be_run_again(cli_env: Config) -> None:
    """🔴 `INITIAL_UPDATE_COMPLETE` is ~30 days of a 730-day grant, and it exits.

    In production the rest follows minutes to hours later. A `0` here tells the
    scheduled runner the backfill landed, and the runner has no other channel to
    learn otherwise — it does not read the terminal, and the MCP envelope's
    `partial` warning is on a surface it never calls. `75` (`EX_TEMPFAIL`) is the
    one thing it can act on: come back.
    """
    FakeClient.pages = [
        _page(added=[_txn("t1")], next_cursor="c1", status="INITIAL_UPDATE_COMPLETE")
    ]

    assert run(["sync", "run"]) == 75

    assert len(_txn_rows(cli_env)) == 1, "the pages that DID arrive were not applied"


def test_a_run_whose_history_is_still_arriving_names_what_is_not_yet_known(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 "1 page applied" is true and reads as finished, which is the defect.

    The line has to say the history is still arriving AND name what that costs:
    `granted_history_days` is null, so there is no window to report, and the
    oldest transaction on hand is the oldest *so far* rather than the oldest that
    exists. An operator who reads only "1 page applied" has no way to know either.
    """
    FakeClient.pages = [
        _page(added=[_txn("t1")], next_cursor="c1", status="INITIAL_UPDATE_COMPLETE")
    ]

    code = run(["sync", "run"])

    # Collapsed to one line first: the contract is the sentence, not where the
    # report chose to wrap it, and asserting on the line breaks would make every
    # rewording of the layout look like a change of meaning.
    out = " ".join(capsys.readouterr().out.split())
    assert "1 page applied" in out, "the pages that arrived went unreported"
    assert "still arriving" in out, "a partial backfill was reported as a finished one"
    assert "granted window is not yet known" in out, (
        "the report did not say the window is unmeasured"
    )
    assert "not the oldest that exists" in out, (
        "the report let the oldest transaction on hand pass for the oldest there is"
    )
    assert code == 75


def test_a_partial_backfill_is_not_stamped_as_a_successful_sync(cli_env: Config) -> None:
    """🔴 The half of this that an agent reads instead of a terminal.

    `last_success_at` is what the freshness warning and `get_pipeline_health`
    answer from, and to both of them a successful sync means the backfill is in.
    A connection at `INITIAL_UPDATE_COMPLETE` is behind by 700 of its 730 days
    and was being reported as current — a well-formed, plausible, wrong answer
    on the surface with no terminal to look at.

    Asserted on the stamped column rather than on the gate's argument, so it
    cannot pass by reading a word that was renamed.
    """
    FakeClient.pages = [
        _page(added=[_txn("t1")], next_cursor="c1", status="INITIAL_UPDATE_COMPLETE")
    ]

    code = run(["sync", "run"])

    row = _connection_row(cli_env)
    assert row["last_success_at"] is None, (
        "a connection holding thirty of its 730 days was recorded as up to date"
    )
    assert row["status"] == "active", "the run succeeded at what it did do"
    assert row["last_error_code"] is None
    assert code == 75


def test_the_cli_and_the_read_path_say_the_same_thing_about_a_partial_backfill(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 Two surfaces over one datastore, and they were disagreeing.

    The read path has always put this state on every answer as a `partial`
    warning; the CLI printed "N pages applied" and exited 0. Driven end to end
    against the same datastore rather than compared by eye, because agreement
    read side by side in two files is agreement nothing enforces.
    """
    from bankmachine.query import list_accounts

    FakeClient.pages = [
        _page(added=[_txn("t1")], next_cursor="c1", status="INITIAL_UPDATE_COMPLETE")
    ]

    code = run(["sync", "run"])
    assert "still arriving" in " ".join(capsys.readouterr().out.split()), (
        "the CLI called the datastore finished while the read path called it partial"
    )

    warnings = list_accounts(cli_env).warnings
    assert any(w.kind == "partial" for w in warnings), (
        f"the read path called the same datastore complete: {[w.kind for w in warnings]}"
    )
    assert code == 75


def test_a_degraded_connection_outranks_one_that_is_still_arriving(
    cli_env: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`75` says come back; `1` says look at this. A run that is both needs the operator.

    The scheduled runner would come back tomorrow regardless, so `75` costs the
    stuck connection a day of nobody noticing. Pinned because it is a decision,
    not a consequence: nothing else in the suite fixes the order of the two.
    """
    second_token = _enroll_a_second_connection(cli_env)
    FakeClient.pages = [
        _page(added=[_txn("t1")], next_cursor="c1", status="INITIAL_UPDATE_COMPLETE")
    ]
    FakeClient.fail_for_token = second_token

    assert run(["sync", "run"]) == 1

    with reader_connection(cli_env) as conn:
        rows = {
            int(r._mapping["connection_id"]): r._mapping["status"]
            for r in conn.execute(select(connections)).all()
        }
    assert rows[1] == "active", "the still-arriving connection was not synced"
    assert rows[2] == "degraded"


# --- `--until-ready`: the loop the runbook used to ask a person to be ---------
#
# 🔴 Every test here drives the real command through `run()` and lets the page
# script advance between attempts. `FakeClient.calls` accumulates across runs,
# so attempt N sees page N -- which is the point: a fake that returned the same
# page forever would pass a loop that never re-fetched anything.
#
# `--retry-delay 0` rather than an injected clock, and `--no-wait` so each
# attempt makes exactly one call instead of spending `NOT_READY_DELAYS` inside
# the run.


def test_until_ready_keeps_running_while_history_is_still_owed(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 0 only when nothing is owed -- reached by re-running, not by waiting."""
    FakeClient.pages = [
        _page(status="NOT_READY"),
        _page(status="NOT_READY"),
        _page(added=[_txn("t1")], next_cursor="cursor-1"),
    ]

    assert run(["sync", "run", "--until-ready", "--no-wait", "--retry-delay", "0"]) == 0

    assert len(FakeClient.calls) == 3, "the loop did not re-fetch between attempts"
    assert len(_txn_rows(cli_env)) == 1
    assert "still owed after attempt 1" in capsys.readouterr().out


def test_until_ready_returns_a_problem_unchanged_rather_than_retrying_it(
    cli_env: Config,
) -> None:
    """🔴 `1` outranks `75`. A stuck connection needs a person, and another
    twelve attempts would bury the one signal that says so."""
    FakeClient.pages = [
        _page(status="NOT_READY"),
        TransportError("the aggregator is unreachable", endpoint=TRANSACTIONS_SYNC),
    ]

    assert run(["sync", "run", "--until-ready", "--no-wait", "--retry-delay", "0"]) == 1

    assert len(FakeClient.calls) == 2, "the loop kept going after a problem"


def test_until_ready_stops_at_the_attempt_cap_and_still_reports_unfinished(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """A backfill that never lands must not spin forever -- and giving up
    waiting is not the same event as finishing, so the code stays 75."""
    FakeClient.pages = [_page(status="NOT_READY")]

    code = run(
        ["sync", "run", "--until-ready", "--no-wait", "--retry-delay", "0", "--max-attempts", "3"]
    )

    assert code == 75
    assert len(FakeClient.calls) == 3, "the cap was not honoured"
    assert "still owed after 3 attempts" in capsys.readouterr().err


def test_without_the_flag_a_run_is_still_one_run(cli_env: Config) -> None:
    """The default path is untouched: 75 is returned, not retried."""
    FakeClient.pages = [_page(status="NOT_READY")]

    assert run(["sync", "run", "--no-wait"]) == 75

    assert len(FakeClient.calls) == 1


@pytest.mark.parametrize(
    "argv",
    [
        ["sync", "run", "--until-ready", "--max-attempts", "0"],
        ["sync", "run", "--until-ready", "--retry-delay", "-1"],
        # 🔴 The same bounds without the flag. They used to hold only on the
        # `--until-ready` branch, so `--retry-delay -1` was refused on one path
        # and accepted on the other -- a bound that holds only where somebody
        # remembered it. They are argparse converters now, so both paths agree.
        ["sync", "run", "--max-attempts", "0"],
        ["sync", "run", "--retry-delay", "-1"],
        # Neither is caught by `< 0`: `nan < 0` and `inf < 0` are both False.
        # `sleep(nan)` raises from inside the loop and `sleep(inf)` waits
        # forever, which is what the bounded cap exists to prevent.
        ["sync", "run", "--until-ready", "--retry-delay", "nan"],
        ["sync", "run", "--until-ready", "--retry-delay", "inf"],
    ],
)
def test_a_bound_on_the_loop_is_refused_rather_than_clamped_on_every_path(
    cli_env: Config, argv: list[str]
) -> None:
    """Zero attempts would exit 75 having done nothing, which is exactly what a
    backfill that never landed looks like."""
    FakeClient.pages = [_page(status="NOT_READY")]

    with pytest.raises(SystemExit) as raised:
        run(argv)

    assert raised.value.code == 2
    assert FakeClient.calls == [], "a refused bound still ran the command"


@pytest.mark.parametrize("flag", [["--max-attempts", "20"], ["--retry-delay", "5"]])
def test_tuning_the_loop_without_enabling_it_is_a_usage_error(
    cli_env: Config, capsys: pytest.CaptureFixture[str], flag: list[str]
) -> None:
    """🔴 Not a silent no-op. `sync run --max-attempts 20` used to make exactly
    one attempt and exit 75 saying nothing, which an operator who believed they
    had enabled the loop would read as the loop giving up."""
    FakeClient.pages = [_page(status="NOT_READY")]

    assert run(["sync", "run", *flag]) == 2

    assert f"bankmachine: {flag[0]} only applies with --until-ready" in capsys.readouterr().err
    assert FakeClient.calls == []


@pytest.mark.parametrize("flag", [["--max-attempts", "20"], ["--retry-delay", "5"]])
def test_a_refused_tuning_flag_leaves_its_refusal_in_the_log(
    cli_env: Config, flag: list[str]
) -> None:
    """🔴 The refusal an unattended run most needs recorded.

    It used to leave by `SystemExit`, past the handler that logs, so a scheduled
    run that was refused left the same log as one that found nothing to do.
    """
    FakeClient.pages = [_page(status="NOT_READY")]

    assert run(["sync", "run", *flag]) == 2

    logging.shutdown()
    text = (cli_env.log_dir / "bankmachine.log").read_text(encoding="utf-8")
    assert re.search(
        rf"WARNING.*command sync refused: {re.escape(flag[0])} only applies with --until-ready",
        text,
    ), text


def test_until_ready_says_each_wait_once_on_the_terminal(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """The printed line is the terminal's copy and the log record is the file's.

    Both used to reach the terminal, so every wait and the give-up appeared twice,
    once as the sentence and once as a timestamped log line under it. The log
    file keeping them is held by the test below.
    """
    FakeClient.pages = [_page(status="NOT_READY")]

    code = run(
        ["sync", "run", "--until-ready", "--no-wait", "--retry-delay", "0", "--max-attempts", "2"]
    )
    assert code == 75

    captured = capsys.readouterr()
    terminal = captured.out + captured.err
    assert terminal.count("still owed after attempt 1 of 2") == 1, terminal
    assert terminal.count("history is still owed") == 1, terminal


def test_until_ready_leaves_its_waiting_in_the_log_not_only_on_the_terminal(
    cli_env: Config,
) -> None:
    """🔴 The flag exists for the run nobody is watching, so stdout is the wrong
    channel to prove.

    Without this the three `logger` calls are invisible to the suite and can be
    deleted green, which would leave an hour of polling indistinguishable in the
    durable record from one ordinary exit 75 -- the consequence
    `test_run_failures_are_logged.py` already exists to hold for failures.
    """
    FakeClient.pages = [_page(status="NOT_READY")]

    code = run(
        ["sync", "run", "--until-ready", "--no-wait", "--retry-delay", "0", "--max-attempts", "2"]
    )
    assert code == 75

    logging.shutdown()
    log_file = cli_env.log_dir / "bankmachine.log"
    text = log_file.read_text(encoding="utf-8") if log_file.exists() else ""

    assert "attempt 1 of 2" in text, "the retry was not recorded in the log"
    # 🔴 The level and the sentence on ONE line. A bare `"WARNING" in text`
    # cannot fail: every CLI run writes the environment banner at WARNING into
    # this same file, so the assertion would pass with the loop logging nothing
    # at all -- which is the failure this test exists to catch.
    assert re.search(r"WARNING.*gave up waiting", text), (
        "giving up waiting was not recorded at WARNING"
    )


def test_until_ready_records_the_attempt_it_finished_on(cli_env: Config) -> None:
    """A run that took three attempts and one that took one are different events."""
    FakeClient.pages = [
        _page(status="NOT_READY"),
        _page(added=[_txn("t1")], next_cursor="cursor-1"),
    ]

    assert run(["sync", "run", "--until-ready", "--no-wait", "--retry-delay", "0"]) == 0

    logging.shutdown()
    text = (cli_env.log_dir / "bankmachine.log").read_text(encoding="utf-8")
    assert "finished at attempt 2" in text


def test_until_ready_waits_the_configured_delay_between_attempts(cli_env: Config) -> None:
    """The delay is real. Asserted through the injected clock because a test
    that actually waited five minutes would be deleted by the first person who
    ran the suite."""
    FakeClient.pages = [_page(status="NOT_READY")]
    slept: list[float] = []

    args = build_parser().parse_args(
        ["sync", "run", "--until-ready", "--no-wait", "--retry-delay", "90", "--max-attempts", "3"]
    )
    code = sync_run.cmd_sync_run(cli_env, args, sleep=slept.append)

    assert code == 75
    # Two waits for three attempts: the loop does not sleep after the last one,
    # which would be a delay nobody is waiting through.
    assert slept == [90.0, 90.0]


# --------------------------------------------------------------------------
# AC-3.2 — the capability gate, which is about the connection and never the bank
# --------------------------------------------------------------------------


#: One connection's heading in the run report: two spaces, its id, two spaces,
#: the institution. Matched rather than sliced at a fixed width, which a
#: two-digit id already outgrows.
_CONNECTION_HEADING = re.compile(r"^ {2}(\d+) {2}\S")


def _per_connection(report: str) -> dict[str, str]:
    """The report split into one block per connection, continuations included.

    Every investments line is a continuation under the connection it belongs to,
    so a test that read only the numbered line would pass whatever those said.

    🔴 **The run-level summary belongs to no connection, and dropping it is what
    makes a block's contents attributable.** It prints at column 0 and OPENS
    WITH A COUNT -- "2 of 2 connections could not be synced" -- so a splitter
    that took any leading digit would file it under connection 2 whenever two
    connections ran, and an assertion written about that connection's own line
    would be satisfied by a sentence about the run. Continuations are the lines
    the renderer indents past the heading; anything less indented ends the block.
    """
    blocks: dict[str, str] = {}
    current = ""
    for line in report.splitlines():
        heading = _CONNECTION_HEADING.match(line)
        if heading:
            current = heading.group(1)
        elif not line.startswith("   "):
            current = ""
        if current:
            blocks[current] = blocks.get(current, "") + line + "\n"
    return blocks


def _capable_connection(config: Config) -> str:
    """A second connection that reports it can serve investments."""
    return _enroll_a_second_connection(config, capabilities='["investments", "transactions"]')


def test_investments_are_pulled_for_the_connection_that_reports_them_and_no_other(
    cli_env: Config,
) -> None:
    """🔴 Both directions in one run, because either alone proves nothing.

    A test with only the capable connection cannot tell a working gate from no
    gate at all; one with only the incapable connection cannot tell a working
    gate from a broken client. The discrimination is the assertion.
    """
    capable_token = _capable_connection(cli_env)
    FakeClient.pages = [_page()]

    assert run(["sync", "run", "--no-wait"]) == 0

    assert FakeClient.holdings_tokens == [capable_token], (
        "the endpoint was reached for exactly the connection whose recorded "
        "capabilities name the product"
    )
    stored = _holdings_rows(cli_env)
    assert len(stored) == 1
    assert {row["account_id"] for row in stored} == {_account_of(cli_env, "item-two")}


def test_a_capability_that_merely_contains_the_product_name_does_not_open_the_gate(
    cli_env: Config,
) -> None:
    """🔴 Whole values, never a substring.

    The aggregator's product list holds `investments_auth` beside `investments`,
    and a substring test would pull holdings for a connection that reports only
    the former -- the same shape of error that once recorded a connection's
    capabilities from `available_products` alone and inverted AC-3.2's criterion
    for exactly the connections that have investments.
    """
    _enroll_a_second_connection(cli_env, capabilities='["investments_auth"]')
    FakeClient.pages = [_page()]

    assert run(["sync", "run", "--no-wait"]) == 0

    assert FakeClient.holdings_tokens == []
    assert _holdings_rows(cli_env) == []


def test_a_capable_connection_records_its_investments_domain_and_says_so(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """The run's own report distinguishes a connection that pulled positions.

    Without the line, a connection that cannot serve investments and one whose
    capabilities could not be read look identical to one that pulled them and
    found an empty portfolio.
    """
    _capable_connection(cli_env)
    FakeClient.pages = [_page()]

    assert run(["sync", "run", "--no-wait"]) == 0

    out = capsys.readouterr().out
    assert "positions recorded" in out
    with reader_connection(cli_env) as conn:
        domains = conn.execute(
            select(sync_state.c.connection_id, sync_state.c.domain).where(
                sync_state.c.domain == INVESTMENTS_DOMAIN
            )
        ).all()
    assert [row[0] for row in domains] == [2]


def test_a_connection_that_cannot_serve_investments_is_not_said_to_have_recorded_positions(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other direction of the same line, which the positive one cannot prove.

    A report that appended `positions recorded` to every connection would pass
    the capable case exactly as a working one does, and the operator reading it
    would conclude a connection had positions when nothing ever asked for them.
    Both connections run here so the discrimination is what is asserted.
    """
    _capable_connection(cli_env)
    FakeClient.pages = [_page()]

    assert run(["sync", "run", "--no-wait"]) == 0

    reported = _per_connection(capsys.readouterr().out)
    assert "positions recorded" not in reported["1"]
    assert "positions recorded" in reported["2"]


def test_a_second_run_the_same_day_leaves_the_positions_alone(cli_env: Config) -> None:
    """AC-2.4 for the holdings series: the first capture of the day is the one kept."""
    _capable_connection(cli_env)
    FakeClient.pages = [_page()]
    assert run(["sync", "run", "--no-wait"]) == 0
    before = _holdings_rows(cli_env)

    FakeClient.pages = [_page(next_cursor="cursor-2")]
    assert run(["sync", "run", "--no-wait"]) == 0

    assert _holdings_rows(cli_env) == before


def _investment_transaction_rows(config: Config) -> list[dict[str, Any]]:
    with reader_connection(config) as conn:
        return [dict(row) for row in conn.execute(select(investment_transactions)).mappings()]


def test_an_investment_transaction_window_is_paged_to_exhaustion_by_offset(
    cli_env: Config,
) -> None:
    """AC-3.3: the whole window, fetched by advancing the offset (§26).

    The offsets are the assertion. This endpoint has no cursor, so a loop that
    failed to advance would re-read page one forever -- and against a fake that
    answers every call the same way, a row count alone would look identical to
    a loop that worked.
    """
    _capable_connection(cli_env)
    FakeClient.pages = [_page()]
    FakeClient.investment_transaction_pages = [
        _investment_transactions_body([_investment_txn("inv-1")], total=3),
        _investment_transactions_body([_investment_txn("inv-2")], total=3),
        _investment_transactions_body([_investment_txn("inv-3")], total=3),
    ]

    assert run(["sync", "run", "--no-wait"]) == 0

    assert FakeClient.investment_transaction_offsets == [0, 1, 2]
    stored = {
        row["source_investment_transaction_id"] for row in _investment_transaction_rows(cli_env)
    }
    assert stored == {"inv-1", "inv-2", "inv-3"}

    # 🔴 AC-3.3's FIRST clause: the full configured window is what was asked
    # for, on every page. Measured against the configuration rather than a
    # literal, so the assertion follows `history_days` instead of restating it
    # -- and relatively rather than against a fixed date, so it cannot go red on
    # a calendar boundary.
    assert FakeClient.investment_transaction_windows, "the endpoint was never called"
    for asked_start, asked_end in FakeClient.investment_transaction_windows:
        assert (asked_end - asked_start).days == cli_env.history_days
        assert asked_end == now_utc().date()


def test_a_second_consecutive_run_produces_zero_net_investment_changes(
    cli_env: Config,
) -> None:
    """AC-2.4 for the investments window, across two whole runs."""
    _capable_connection(cli_env)
    FakeClient.pages = [_page()]
    assert run(["sync", "run", "--no-wait"]) == 0
    before = _investment_transaction_rows(cli_env)
    assert before, "the first run stored nothing, so the second proves nothing"

    FakeClient.pages = [_page(next_cursor="cursor-2")]
    assert run(["sync", "run", "--no-wait"]) == 0
    after = _investment_transaction_rows(cli_env)

    assert len(after) == len(before)
    assert {row["source_investment_transaction_id"] for row in after} == {
        row["source_investment_transaction_id"] for row in before
    }
    assert all(row["removed_at"] is None for row in after), (
        "a second identical window must not retire the rows the first one stored"
    )


def test_a_row_missing_from_a_later_complete_window_is_retired_and_reported(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """The soft delete, end to end, and the line that makes it visible."""
    _capable_connection(cli_env)
    FakeClient.pages = [_page()]
    FakeClient.investment_transaction_pages = [
        _investment_transactions_body([_investment_txn("inv-1"), _investment_txn("inv-2")])
    ]
    assert run(["sync", "run", "--no-wait"]) == 0
    assert len(_investment_transaction_rows(cli_env)) == 2

    FakeClient.pages = [_page(next_cursor="cursor-2")]
    FakeClient.investment_transaction_pages = [
        _investment_transactions_body([_investment_txn("inv-1")])
    ]
    assert run(["sync", "run", "--no-wait"]) == 0

    rows_by_id = {
        row["source_investment_transaction_id"]: row
        for row in _investment_transaction_rows(cli_env)
    }
    assert rows_by_id["inv-1"]["removed_at"] is None
    assert rows_by_id["inv-2"]["removed_at"] is not None
    assert "no longer reported" in capsys.readouterr().out, (
        "a soft delete leaves no trace in a page count, so the run has to name it"
    )


def test_a_window_stopped_at_the_page_ceiling_retires_nothing(
    cli_env: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 The invariant that stands between a bounded run and bulk data loss.

    A run that saw a PREFIX of the window is missing every row it never reached,
    and concluding removal from that would soft-delete real history while
    looking exactly as it should. Asserted as the invariant rather than as one
    of its causes: what reaches the reconciliation is a short row count,
    whatever stopped the run.
    """
    _capable_connection(cli_env)
    FakeClient.pages = [_page()]
    FakeClient.investment_transaction_pages = [
        _investment_transactions_body([_investment_txn("inv-1"), _investment_txn("inv-2")])
    ]
    assert run(["sync", "run", "--no-wait"]) == 0
    assert len(_investment_transaction_rows(cli_env)) == 2

    # The window now claims far more than the bounded run can fetch, and the
    # ceiling stops it after one page that carries neither stored row.
    monkeypatch.setattr("bankmachine.cli.sync_run.MAX_PAGES_PER_RUN", 1)
    FakeClient.pages = [_page(next_cursor="cursor-2")]
    FakeClient.investment_transaction_pages = [
        _investment_transactions_body([_investment_txn("inv-3")], total=99)
    ]

    # 75: the run found nothing wrong and still owes work.
    assert run(["sync", "run", "--no-wait"]) == 75

    assert all(row["removed_at"] is None for row in _investment_transaction_rows(cli_env)), (
        "a window seen only in part must retire nothing"
    )


def test_a_window_stopped_short_records_no_range(
    cli_env: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unmeasured range stays null rather than being invented from a prefix."""
    _capable_connection(cli_env)
    monkeypatch.setattr("bankmachine.cli.sync_run.MAX_PAGES_PER_RUN", 1)
    FakeClient.pages = [_page()]
    FakeClient.investment_transaction_pages = [
        _investment_transactions_body([_investment_txn("inv-1")], total=99)
    ]

    assert run(["sync", "run", "--no-wait"]) == 75

    with reader_connection(cli_env) as conn:
        recorded = conn.execute(
            select(sync_state.c.history_start_date).where(sync_state.c.domain == INVESTMENTS_DOMAIN)
        ).scalar_one()
    assert recorded is None


def test_a_complete_window_records_the_range_that_came_back(cli_env: Config) -> None:
    """AC-3.3: the range the rows actually carried, not the one asked for."""
    _capable_connection(cli_env)
    FakeClient.pages = [_page()]
    # Dated relative to today and INSIDE the configured window, so the rows stay
    # eligible whatever day this runs. Literal dates would fall out of the
    # 730-day bound on their own and take the assertion with them.
    # Derived from the configured window rather than a literal, so the row is
    # inside it by construction however `history_days` is set.
    oldest = now_utc().date() - timedelta(days=cli_env.history_days // 2)
    newest = now_utc().date() - timedelta(days=5)
    FakeClient.investment_transaction_pages = [
        _investment_transactions_body(
            [
                _investment_txn("inv-old", date=oldest.isoformat()),
                _investment_txn("inv-new", date=newest.isoformat()),
            ]
        )
    ]

    assert run(["sync", "run", "--no-wait"]) == 0

    with reader_connection(cli_env) as conn:
        recorded = conn.execute(
            select(sync_state.c.history_start_date).where(sync_state.c.domain == INVESTMENTS_DOMAIN)
        ).scalar_one()
    assert recorded == calendar_date(oldest)


def test_an_empty_page_against_a_stated_total_reports_work_still_owed(
    cli_env: Config,
) -> None:
    """🔴 A prefix that never reaches the page ceiling, and still owes the rest.

    The loop exits on an empty page, so this run never touches the ceiling
    branch — and reporting it as finished would tell the operator there is
    nothing left to fetch while the range stayed unmeasured and nothing was
    reconciled. 75 is "ran, found nothing wrong, still owes work", which is
    exactly this.
    """
    _capable_connection(cli_env)
    FakeClient.pages = [_page()]
    FakeClient.investment_transaction_pages = [
        _investment_transactions_body([_investment_txn("inv-1")])
    ]
    assert run(["sync", "run", "--no-wait"]) == 0
    assert len(_investment_transaction_rows(cli_env)) == 1

    # The window now claims five rows and answers with none of them.
    FakeClient.pages = [_page(next_cursor="cursor-2")]
    FakeClient.investment_transaction_pages = [_investment_transactions_body([], total=5)]

    assert run(["sync", "run", "--no-wait"]) == 75, (
        "an empty page against a stated total of five is a window seen in part"
    )
    assert all(row["removed_at"] is None for row in _investment_transaction_rows(cli_env)), (
        "a window that answered with nothing must not retire what it did not contradict"
    )


def test_a_window_that_states_no_total_reports_work_still_owed(cli_env: Config) -> None:
    """An unmeasured window cannot be exhausted, so the run still owes it."""
    _capable_connection(cli_env)
    FakeClient.pages = [_page()]
    FakeClient.investment_transaction_pages = [
        _investment_transactions_body([_investment_txn("inv-1")], state_total=False)
    ]

    assert run(["sync", "run", "--no-wait"]) == 75

    # The rows still land — what is withheld is the CONCLUSION, not the data.
    assert len(_investment_transaction_rows(cli_env)) == 1
    with reader_connection(cli_env) as conn:
        recorded = conn.execute(
            select(sync_state.c.history_start_date).where(sync_state.c.domain == INVESTMENTS_DOMAIN)
        ).scalar_one()
    assert recorded is None, "a window with no stated total measured no range"


def test_a_removal_is_stamped_from_the_archive_and_not_the_clock(cli_env: Config) -> None:
    """🔴 AC-5.2: a soft delete a rebuild can reproduce.

    `removed_at` comes from the LAST page of the complete window rather than
    from `now_utc()`, so replaying the same archived pages concludes the same
    removal at the same instant. Two pages carry two distinct instants, so this
    pins which one is used as well as where it came from — and both are far
    enough from today that the clock could not produce either.
    """
    first_capture = utc_instant(datetime(2026, 9, 3, 11, 0, tzinfo=UTC))
    second_page_one = utc_instant(datetime(2026, 9, 5, 9, 0, tzinfo=UTC))
    second_page_two = utc_instant(datetime(2026, 9, 6, 17, 30, tzinfo=UTC))

    _capable_connection(cli_env)
    FakeClient.pages = [_page()]
    FakeClient.investment_transaction_pages = [
        _investment_transactions_body(
            [_investment_txn(name) for name in ("inv-1", "inv-2", "inv-3")]
        )
    ]
    FakeClient.investment_transaction_received_ats = [first_capture]
    assert run(["sync", "run", "--no-wait"]) == 0
    assert len(_investment_transaction_rows(cli_env)) == 3

    # The window comes back complete over two pages and no longer carries
    # `inv-3`, so that row is retired — at the instant of the page that closed
    # the window, which is the second one.
    FakeClient.pages = [_page(next_cursor="cursor-2")]
    FakeClient.investment_transaction_pages = [
        _investment_transactions_body([_investment_txn("inv-1")], total=2),
        _investment_transactions_body([_investment_txn("inv-2")], total=2),
    ]
    FakeClient.investment_transaction_received_ats = [second_page_one, second_page_two]

    assert run(["sync", "run", "--no-wait"]) == 0

    rows_by_id = {
        row["source_investment_transaction_id"]: row
        for row in _investment_transaction_rows(cli_env)
    }
    assert rows_by_id["inv-1"]["removed_at"] is None, (
        "page one's row came back, so nothing about it was contradicted"
    )
    assert rows_by_id["inv-2"]["removed_at"] is None, "page two's row came back"
    retired = rows_by_id["inv-3"]
    assert retired["removed_at"] == second_page_two, (
        "the removal must carry the closing page's archived instant, so a rebuild "
        "replaying the same pages reaches the same stamp"
    )
    assert retired["removed_at"] != second_page_one
    assert retired["removed_at"].date() != now_utc().date(), (
        "a stamp on today's date means the clock was read instead of the archive"
    )


def test_an_investments_transactions_failure_does_not_cost_the_transactions(
    cli_env: Config,
) -> None:
    """The carried-failure rule, on the second investments endpoint.

    The capability gate opens for products an Item has never initialized (§25),
    so this call can fail for a connection whose transactions are healthy.
    """
    _capable_connection(cli_env)
    # A page that actually carries a transaction, or "the transactions survived"
    # would be asserted over a store that was always going to be empty.
    FakeClient.pages = [_page(added=[_txn("txn-survives")])]
    FakeClient.investment_transactions_error = TransportError(
        "the aggregator is unreachable", endpoint=INVESTMENTS_TRANSACTIONS_GET
    )

    assert run(["sync", "run", "--no-wait"]) == 1

    with reader_connection(cli_env) as conn:
        stored = (
            conn.execute(
                select(transactions.c.source_transaction_id).where(
                    transactions.c.source_transaction_id == "txn-survives"
                )
            )
            .scalars()
            .all()
        )
    assert stored, "the investments failure took the connection's transactions with it"


def test_an_unreadable_capability_record_is_reported_rather_than_read_as_incapable(
    cli_env: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """A connection that quietly stops pulling investments looks exactly like one
    whose institution never offered them, so the difference is logged and the
    domains every connection has still run."""
    _enroll_a_second_connection(cli_env, capabilities="not json at all")
    FakeClient.pages = [_page(added=[_txn("t-capability")], next_cursor="cursor-1")]

    with caplog.at_level(logging.WARNING, logger="bankmachine.cli.sync_run"):
        assert run(["sync", "run", "--no-wait"]) == 0

    assert FakeClient.holdings_tokens == []
    assert any("unreadable capabilities" in record.getMessage() for record in caplog.records)
    assert len(_txn_rows(cli_env)) == 2, "the domains it has no choice about still ran"


def _holdings_rows(config: Config) -> list[dict[str, Any]]:
    with reader_connection(config) as conn:
        return [dict(row) for row in conn.execute(select(holdings)).mappings()]


def _account_of(config: Config, source_connection_id: str) -> int:
    with reader_connection(config) as conn:
        return int(
            conn.execute(
                select(accounts.c.account_id)
                .join(connections, connections.c.connection_id == accounts.c.connection_id)
                .where(connections.c.source_connection_id == source_connection_id)
            ).scalar_one()
        )


def _domain_row(config: Config, connection_id: int, domain: str) -> Any:
    with reader_connection(config) as conn:
        return (
            conn.execute(
                select(sync_state).where(
                    sync_state.c.connection_id == connection_id, sync_state.c.domain == domain
                )
            )
            .mappings()
            .one_or_none()
        )


def test_an_investments_failure_does_not_cost_the_connection_its_transactions(
    cli_env: Config,
) -> None:
    """🔴 The new feature must not be able to break the old one.

    The gate opens for any connection whose recorded capabilities name the
    product, and those include products the Item has never initialized — so this
    call can fail for a connection whose transactions are entirely healthy.
    Raising from the pull would abandon the page loop before it started, on this
    run and on every run after it.
    """
    _capable_connection(cli_env)
    FakeClient.holdings_error = TransportError(
        "the aggregator is unreachable", endpoint=INVESTMENTS_HOLDINGS_GET
    )
    FakeClient.pages = [_page(added=[_txn("t-despite-investments")], next_cursor="cursor-1")]

    assert run(["sync", "run", "--no-wait"]) == 1

    assert FakeClient.holdings_tokens, "the gate still opened; the failure is the endpoint's"
    assert _holdings_rows(cli_env) == []
    assert _domain_row(cli_env, 2, TRANSACTIONS_DOMAIN)["cursor"] == "cursor-1", (
        "the transactions paged to the end despite the other failure"
    )


def test_a_product_failure_is_recorded_against_the_domain_not_the_connection(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 The recorded decision, made structural.

    `connections.status` means credential health. A product the Item never
    initialized failing is not a statement about the login, and marking the
    connection degraded for it sends the operator to `connections reauth` —
    which cannot fix it, and which mints a second Item if they reach for
    `enroll` instead.
    """
    _capable_connection(cli_env)
    FakeClient.holdings_error = TransportError(
        "the aggregator is unreachable", endpoint=INVESTMENTS_HOLDINGS_GET
    )
    FakeClient.pages = [_page(added=[_txn("t-domain-scoped")], next_cursor="cursor-1")]

    # 🔴 Still `1`. The connection ran and found a problem, which is what that
    # code means — collapsing a domain failure into `0` would leave a scheduled
    # runner's only machine-readable signal saying nothing is wrong.
    assert run(["sync", "run", "--no-wait"]) == 1

    with reader_connection(cli_env) as conn:
        status, code = conn.execute(
            select(connections.c.status, connections.c.last_error_code).where(
                connections.c.connection_id == 2
            )
        ).one()
    assert status == "active", "a product error must not send the operator to reauth"
    assert code is None

    investments = _domain_row(cli_env, 2, INVESTMENTS_DOMAIN)
    assert investments["last_error_code"] == "AGGREGATOR_UNREACHABLE"
    assert investments["last_error_at"] is not None
    assert investments["last_success_at"] is None

    transactions_domain = _domain_row(cli_env, 2, TRANSACTIONS_DOMAIN)
    assert transactions_domain["last_error_code"] is None
    assert transactions_domain["last_success_at"] is not None, (
        "the transactions domain's own success must survive the other domain failing"
    )

    out = capsys.readouterr().out
    assert "investments:" in out
    assert "no re-authentication" in out
    assert "1 of 2 connections synced with their investments domain failing" in out, out
    assert "could not be synced" not in out, out


def test_an_investments_failure_reaches_a_column_on_a_first_sync(
    cli_env: Config,
) -> None:
    """🔴 The residue the carried failure left, closed by recording at the catch.

    A connection still materializing its history returns from inside the page
    loop. A failure carried past that loop to be applied afterwards therefore
    reached no column at all — on exactly the run an operator most needs to see
    it, because a first sync is when an uninitialized product fails.
    """
    _capable_connection(cli_env)
    FakeClient.holdings_error = TransportError(
        "the aggregator is unreachable", endpoint=INVESTMENTS_HOLDINGS_GET
    )
    # 🔴 The status string the loop actually branches on. A near-miss spelling
    # here reaches no early return at all, and the test then passes over the
    # ordinary path while claiming to cover this one.
    FakeClient.pages = [_page(status=sync_run.NOT_READY, next_cursor="")]

    assert run(["sync", "run", "--no-wait"]) == 1

    with reader_connection(cli_env) as conn:
        assert (
            conn.execute(
                select(transactions.c.transaction_id).where(
                    transactions.c.account_id.in_(
                        select(accounts.c.account_id).where(accounts.c.connection_id == 2)
                    )
                )
            ).first()
            is None
        ), "the page loop has to have returned early, or this covers the ordinary path"

    investments = _domain_row(cli_env, 2, INVESTMENTS_DOMAIN)
    assert investments is not None, (
        "the page loop returned before the failure was applied, so it reached nothing"
    )
    assert investments["last_error_code"] == "AGGREGATOR_UNREACHABLE"


def test_a_capable_connection_that_has_never_run_a_domain_has_no_row_for_it(
    cli_env: Config,
) -> None:
    """AC-4.5's two absences, kept apart.

    A domain with no row has never been TRIED; a row with a null
    `last_success_at` has been tried and has never landed. Reading the first as
    the second would report a hole where there is only a connection that cannot
    serve the domain.
    """
    FakeClient.pages = [_page()]

    assert run(["sync", "run", "--no-wait"]) == 0

    assert _domain_row(cli_env, 1, INVESTMENTS_DOMAIN) is None
    assert _domain_row(cli_env, 1, TRANSACTIONS_DOMAIN) is not None


def test_a_window_seen_in_part_does_not_make_the_investments_domain_current(
    cli_env: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 Two feeds, one domain key, one freshness claim — so both must be in.

    Positions and the investment-transaction window share
    `sync_state.domain = 'investments'`. Stamping the domain current when the
    positions landed would report a portfolio as fresh while most of its
    transaction history was still missing, which is the silent staleness this
    product exists to refuse.
    """
    _capable_connection(cli_env)
    monkeypatch.setattr("bankmachine.cli.sync_run.MAX_PAGES_PER_RUN", 1)
    FakeClient.pages = [_page()]
    FakeClient.investment_transaction_pages = [
        _investment_transactions_body([_investment_txn("inv-1")], total=99)
    ]

    # 75: nothing is wrong, and work is still owed.
    assert run(["sync", "run", "--no-wait"]) == 75

    assert _holdings_rows(cli_env), "the positions did land; the window is what fell short"
    investments = _domain_row(cli_env, 2, INVESTMENTS_DOMAIN)
    assert investments["last_success_at"] is None, (
        "the positions arriving is half the pull, and half a pull is not a fresh domain"
    )
    assert investments["last_error_code"] is None, (
        "a short window is work still owed, not a failure to record"
    )


def test_a_complete_investments_pull_stamps_the_domain_current(cli_env: Config) -> None:
    """The other side of the same rule: both feeds in, so the domain is current."""
    _capable_connection(cli_env)
    FakeClient.pages = [_page()]

    assert run(["sync", "run", "--no-wait"]) == 0

    investments = _domain_row(cli_env, 2, INVESTMENTS_DOMAIN)
    assert investments["last_success_at"] is not None
    assert investments["last_error_code"] is None


def test_an_investments_failure_clears_once_the_domain_succeeds_again(
    cli_env: Config,
) -> None:
    """A repaired domain stops being reported as failing.

    Multi-hop, because a failure recorded and never cleared is a health surface
    that goes permanently red on a transient error — indistinguishable from one
    that is right.
    """
    _capable_connection(cli_env)
    FakeClient.holdings_error = TransportError(
        "the aggregator is unreachable", endpoint=INVESTMENTS_HOLDINGS_GET
    )
    FakeClient.pages = [_page()]
    assert run(["sync", "run", "--no-wait"]) == 1
    assert (
        _domain_row(cli_env, 2, INVESTMENTS_DOMAIN)["last_error_code"] == "AGGREGATOR_UNREACHABLE"
    )

    FakeClient.holdings_error = None
    FakeClient.pages = [_page(next_cursor="cursor-2")]
    assert run(["sync", "run", "--no-wait"]) == 0

    investments = _domain_row(cli_env, 2, INVESTMENTS_DOMAIN)
    assert investments["last_error_code"] is None
    assert investments["last_error_at"] is None
    assert investments["last_success_at"] is not None


def test_one_connection_s_investments_failure_does_not_stop_another_s_sync(
    cli_env: Config,
) -> None:
    """AC-4.1, for a failure that is now the domain's rather than the connection's."""
    _capable_connection(cli_env)
    FakeClient.holdings_error = TransportError(
        "the aggregator is unreachable", endpoint=INVESTMENTS_HOLDINGS_GET
    )
    FakeClient.pages = [_page(added=[_txn("t-both")], next_cursor="cursor-1")]

    assert run(["sync", "run", "--no-wait"]) == 1

    # Both connections paged; the failing domain belongs to the second alone.
    assert _domain_row(cli_env, 1, TRANSACTIONS_DOMAIN)["cursor"] == "cursor-1"
    assert _domain_row(cli_env, 2, TRANSACTIONS_DOMAIN)["cursor"] == "cursor-1"
    assert _domain_row(cli_env, 1, INVESTMENTS_DOMAIN) is None


def test_an_expired_login_found_through_the_investments_call_still_offers_the_repair(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 A credential error IS about the login, whichever call happens to meet it.

    Swallowing it with the other investments failures would degrade the
    connection with no repair printed — and the operator's next move would be to
    enrol again, which mints a second Item and doubles the history.
    """
    _capable_connection(cli_env)
    FakeClient.holdings_error = ReauthRequiredError(
        "the login expired", endpoint=INVESTMENTS_HOLDINGS_GET
    )
    FakeClient.pages = [_page()]

    assert run(["sync", "run", "--no-wait"]) == 1

    assert "connections reauth 2" in capsys.readouterr().out


# --- The health surface, once a connection is more than one stream ------------


def _health_rows(config: Config) -> list[dict[str, Any]]:
    return [dict(row) for row in query.pipeline_health(config).to_wire()["rows"]]


def _domains_of(row: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["domain"]: entry for entry in row["domains"]}


def _age_domain(config: Config, connection_id: int, domain: str, *, hours: int) -> UtcInstant:
    """Move one domain's last success into the past, leaving the connection alone.

    The whole case under test is a connection whose own freshness is fine while
    one domain's is not, and a fake clock would move both together.
    """
    aged = utc_instant(now_utc() - timedelta(hours=hours))
    with writer_connection(config) as conn:
        conn.execute(
            sync_state.update()
            .where(sync_state.c.connection_id == connection_id, sync_state.c.domain == domain)
            .values(last_success_at=aged, updated_at=aged)
        )
    return aged


def test_the_health_surface_reports_each_domain_a_connection_has(cli_env: Config) -> None:
    """AC-4.4 for a pipeline with two domains in it.

    Every field on a health row above `domains` reads the connection, and a
    connection is no longer one stream: it can be `active` and current while one
    of its domains has not landed in weeks.
    """
    _capable_connection(cli_env)
    FakeClient.pages = [_page(next_cursor="cursor-1")]

    assert run(["sync", "run", "--no-wait"]) == 0

    rows = {row["connection_id"]: row for row in _health_rows(cli_env)}
    assert set(_domains_of(rows[2])) == {TRANSACTIONS_DOMAIN, INVESTMENTS_DOMAIN}
    assert set(_domains_of(rows[1])) == {TRANSACTIONS_DOMAIN}, (
        "a connection that cannot serve investments must not be reported as owing them"
    )
    for entry in rows[2]["domains"]:
        assert entry["last_success_at"] is not None
        assert entry["last_error_code"] is None


def test_a_failing_domain_is_reported_beside_a_healthy_one(cli_env: Config) -> None:
    """The chunk's acceptance criterion, on the surface that has to carry it."""
    _capable_connection(cli_env)
    FakeClient.holdings_error = TransportError(
        "the aggregator is unreachable", endpoint=INVESTMENTS_HOLDINGS_GET
    )
    FakeClient.pages = [_page(next_cursor="cursor-1")]

    assert run(["sync", "run", "--no-wait"]) == 1

    row = next(r for r in _health_rows(cli_env) if r["connection_id"] == 2)
    assert row["status"] == "active", "a product error is not a statement about the login"
    domains = _domains_of(row)
    assert domains[INVESTMENTS_DOMAIN]["last_error_code"] == "AGGREGATOR_UNREACHABLE"
    assert domains[INVESTMENTS_DOMAIN]["last_error_at"] is not None
    assert domains[INVESTMENTS_DOMAIN]["last_success_at"] is None
    assert domains[TRANSACTIONS_DOMAIN]["last_error_code"] is None
    assert domains[TRANSACTIONS_DOMAIN]["last_success_at"] is not None, (
        "the domain beside the failing one must be untouched"
    )

    # 🔴 ONE caveat for one condition. A domain whose first attempt failed is
    # both failing and never-landed, and two warnings describing that single
    # fact -- on every answer, since these kinds ride all of them -- is what
    # teaches a reader to skip the pair.
    about_the_domain = [
        w
        for w in query.pipeline_health(cli_env).to_wire()["warnings"]
        if w.get("connection_id") == 2 and INVESTMENTS_DOMAIN in w["detail"]
    ]
    assert len(about_the_domain) == 1, about_the_domain
    assert "AGGREGATOR_UNREACHABLE" in about_the_domain[0]["detail"]
    assert "never landed in full" in about_the_domain[0]["detail"], (
        "the one caveat has to carry the size of the hole the other would have named"
    )


def test_a_domain_that_has_never_landed_is_told_apart_from_one_that_has_gone_stale(
    cli_env: Config,
) -> None:
    """🔴 AC-4.5: the size of the hole must be computable rather than guessed.

    A domain that has never landed and one that landed in July are both "not
    current", and the operator's next move differs entirely — so the two must
    not arrive as the same value. The never case is a null `last_success_at`
    against a present `last_attempt_at`; the stale case is a date to subtract.
    """
    _capable_connection(cli_env)
    FakeClient.holdings_error = TransportError(
        "the aggregator is unreachable", endpoint=INVESTMENTS_HOLDINGS_GET
    )
    FakeClient.pages = [_page(next_cursor="cursor-1")]
    assert run(["sync", "run", "--no-wait"]) == 1

    never = _domains_of(next(r for r in _health_rows(cli_env) if r["connection_id"] == 2))
    assert never[INVESTMENTS_DOMAIN]["last_success_at"] is None
    assert never[INVESTMENTS_DOMAIN]["last_attempt_at"] is not None, (
        "null on both would be indistinguishable from a domain nobody ever tried"
    )

    FakeClient.holdings_error = None
    FakeClient.pages = [_page(next_cursor="cursor-2")]
    assert run(["sync", "run", "--no-wait"]) == 0
    _age_domain(cli_env, 2, INVESTMENTS_DOMAIN, hours=72)

    stale = _domains_of(next(r for r in _health_rows(cli_env) if r["connection_id"] == 2))
    landed = stale[INVESTMENTS_DOMAIN]["last_success_at"]
    assert landed is not None
    # The whole point of the distinction: this case yields a number of hours to
    # act on, and the never case yields no arithmetic at all.
    assert now_utc() - utc_instant(datetime.fromisoformat(landed)) >= timedelta(hours=71)


def test_a_stale_domain_warns_while_the_connection_itself_reads_healthy(
    cli_env: Config,
) -> None:
    """🔴 Silent staleness with a new cause, said out loud on the success path.

    The connection syncs nightly, its login works, and its positions have not
    returned since last week. Every connection-level field reads healthy, so the
    warning has to come from the domain or it does not come at all (AC-4.4).
    """
    _capable_connection(cli_env)
    FakeClient.pages = [_page(next_cursor="cursor-1")]
    assert run(["sync", "run", "--no-wait"]) == 0
    _age_domain(cli_env, 2, INVESTMENTS_DOMAIN, hours=72)

    answer = query.pipeline_health(cli_env).to_wire()
    stale = [
        w
        for w in answer["warnings"]
        if w["kind"] == "stale"
        and w.get("connection_id") == 2
        and INVESTMENTS_DOMAIN in w["detail"]
    ]
    assert stale, "a domain three days behind a healthy connection said nothing"


def test_a_healthy_second_domain_adds_no_warning_of_its_own(cli_env: Config) -> None:
    """The negative control. A kind that rides every answer teaches a reader to
    skip it, so the absence of one has to be information."""
    _capable_connection(cli_env)
    FakeClient.pages = [_page(next_cursor="cursor-1")]

    assert run(["sync", "run", "--no-wait"]) == 0

    warnings = query.pipeline_health(cli_env).to_wire()["warnings"]
    assert not [w for w in warnings if INVESTMENTS_DOMAIN in w["detail"]]


def test_a_transactions_failure_leaves_the_investments_domain_s_success_alone(
    cli_env: Config,
) -> None:
    """The other direction of per-domain recording.

    The positions are pulled before the page loop, so a connection whose
    transactions then fail has a genuinely current investments domain — and
    writing the failure across both domains would report a hole where there is
    none.
    """
    _capable_connection(cli_env)
    FakeClient.pages = [_page(next_cursor="cursor-1")]
    FakeClient.fail_with = TransportError(
        "the aggregator is unreachable", endpoint=TRANSACTIONS_SYNC
    )

    assert run(["sync", "run", "--no-wait"]) == 1

    investments = _domain_row(cli_env, 2, INVESTMENTS_DOMAIN)
    assert investments["last_success_at"] is not None
    assert investments["last_error_code"] is None
    with reader_connection(cli_env) as conn:
        status = conn.execute(
            select(connections.c.status).where(connections.c.connection_id == 2)
        ).scalar_one()
    assert status == "degraded", "the transactions failure IS the connection's"


def test_a_connection_that_lost_both_is_not_also_reported_as_having_synced(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 The two roll-up lines must never describe the same connection.

    "could not be synced" sends an operator to the login; "synced with their
    investments domain failing" tells them it is not the login and needs no
    reauth. A connection that lost its investments AND then its transactions
    satisfies both counts on the raw fields, and counting it in both tells the
    operator two contradictory things about one institution.
    """
    _capable_connection(cli_env)
    FakeClient.holdings_error = TransportError(
        "investments are unreachable", endpoint=INVESTMENTS_HOLDINGS_GET
    )
    # The page loop then fails for both connections, so the capable one carries a
    # recorded domain failure AND a degraded connection.
    FakeClient.fail_with = TransportError(
        "the aggregator is unreachable", endpoint=TRANSACTIONS_SYNC
    )

    assert run(["sync", "run", "--no-wait"]) == 1

    assert (
        _domain_row(cli_env, 2, INVESTMENTS_DOMAIN)["last_error_code"] == "AGGREGATOR_UNREACHABLE"
    ), "the domain failure has to be recorded, or the roll-up has nothing to get wrong"
    out = capsys.readouterr().out
    assert "2 of 2 connections could not be synced" in out, out
    assert "investments domain failing" not in out, out


def test_a_second_domain_does_not_double_any_count(cli_env: Config) -> None:
    """🔴 The regression the pinned domain filters were written against.

    `sync_state` is keyed on `(connection, domain)`, so an unfiltered join
    returns a row per domain — and every count over the result comes back
    multiplied, silently and in the direction of looking like more coverage.
    """
    _capable_connection(cli_env)
    FakeClient.pages = [_page(added=[_txn("t-count")])]

    assert run(["sync", "run", "--no-wait"]) == 0

    with reader_connection(cli_env) as conn:
        domain_rows = conn.execute(select(sync_state.c.connection_id)).all()
    assert len(domain_rows) == 3, "two connections, three domains between them"

    health = _health_rows(cli_env)
    assert [row["connection_id"] for row in health] == [1, 2]

    accounts_wire = query.list_accounts(cli_env).to_wire()["rows"]
    assert len(accounts_wire) == len({row["account_id"] for row in accounts_wire})
    coverage = query.coverage_report(cli_env).to_wire()["rows"]
    assert len(coverage) == len({row["account_id"] for row in coverage})
    assert sum(int(row["transaction_count"]) for row in coverage) == len(_txn_rows(cli_env))


# --------------------------------------------------------------------------
# The rebuild, over a store a real run produced (AC-5.2, AC-2.4)
# --------------------------------------------------------------------------


def _rebuild(config: Config) -> RebuildReport:
    return rebuild(config, derivers=ALL_DERIVERS, replay_passes=all_replay_passes)


def test_a_rebuild_reproduces_a_store_a_whole_run_produced(cli_env: Config) -> None:
    """🔴 AC-5.2 against the state a SYNC leaves, not the state a fixture builds.

    A run writes two kinds of thing the archive cannot: the rows a replay
    reproduces, and the progress `sync_state` records about the run itself --
    stamped from the clock, after the window concluded. A replay that recorded
    either would rewind a freshness claim to the archive's instant, and the
    digest would refuse a rebuild that had reproduced every row correctly.
    """
    _capable_connection(cli_env)
    FakeClient.pages = [_page()]
    assert run(["sync", "run", "--no-wait"]) == 0
    assert _investment_transaction_rows(cli_env), "the run stored nothing to reproduce"
    with reader_connection(cli_env) as conn:
        before = content_digest(conn)

    report = _rebuild(cli_env)

    assert not report.content_changed
    with reader_connection(cli_env) as conn:
        assert content_digest(conn) == before


def test_a_rebuild_after_a_soft_delete_does_not_bring_the_row_back(cli_env: Config) -> None:
    """🔴 End to end: the resurrection, through the command an operator runs.

    `operational-spec.md` tells the operator to rebuild once this build step
    lands. That instruction is a claim, and this is the test that asserts it:
    the row the second window retired stays retired, at the instant the window
    concluded.
    """
    _capable_connection(cli_env)
    FakeClient.pages = [_page()]
    FakeClient.investment_transaction_pages = [
        _investment_transactions_body([_investment_txn("inv-1"), _investment_txn("inv-2")])
    ]
    assert run(["sync", "run", "--no-wait"]) == 0

    FakeClient.pages = [_page(next_cursor="cursor-2")]
    FakeClient.investment_transaction_pages = [
        _investment_transactions_body([_investment_txn("inv-1")])
    ]
    assert run(["sync", "run", "--no-wait"]) == 0
    retired = {
        row["source_investment_transaction_id"]: row["removed_at"]
        for row in _investment_transaction_rows(cli_env)
    }
    assert retired["inv-2"] is not None, "the second window retired nothing, so nothing is at risk"

    report = _rebuild(cli_env)

    assert not report.content_changed
    after = {
        row["source_investment_transaction_id"]: row["removed_at"]
        for row in _investment_transaction_rows(cli_env)
    }
    assert after == retired


def test_a_sync_after_a_rebuild_changes_no_investment_row(cli_env: Config) -> None:
    """AC-2.4 across the pair: rebuild, sync again, and nothing about the rows moves.

    The provenance does move -- a second run archives new pages and every row
    points at the page that last carried it -- so what is asserted is what the
    rows mean: which transactions are there, what they are worth, and which of
    them are retired.
    """
    _capable_connection(cli_env)
    FakeClient.pages = [_page()]
    assert run(["sync", "run", "--no-wait"]) == 0
    _rebuild(cli_env)
    before = _investment_facts(cli_env)
    assert before, "the run stored nothing, so a second one proves nothing"

    FakeClient.pages = [_page(next_cursor="cursor-2")]
    assert run(["sync", "run", "--no-wait"]) == 0

    assert _investment_facts(cli_env) == before


def _investment_facts(config: Config) -> dict[str, tuple[Any, ...]]:
    """What a transaction says about the world, without the provenance of the page."""
    return {
        row["source_investment_transaction_id"]: (
            row["account_id"],
            row["trade_date"],
            row["amount_minor"],
            row["quantity"],
            row["currency"],
            row["removed_at"],
        )
        for row in _investment_transaction_rows(config)
    }


def test_a_window_with_no_stated_total_is_asked_for_once(cli_env: Config) -> None:
    """🔴 A window with no stated size has nothing to page against.

    There is no offset at which the loop could learn it had finished, so asking
    again spends the whole page ceiling in aggregator calls to reach the
    conclusion the first page already supports. The offsets are the assertion: a
    row count would look identical either way, because every extra call lands on
    the same row.
    """
    _capable_connection(cli_env)
    FakeClient.pages = [_page()]
    FakeClient.investment_transaction_pages = [
        _investment_transactions_body([_investment_txn("inv-1")], state_total=False)
    ]

    assert run(["sync", "run", "--no-wait"]) == 75

    assert FakeClient.investment_transaction_offsets == [0], (
        "an unmeasured window was paged past its first reply"
    )


def test_a_short_investments_window_does_not_withhold_the_connection_s_freshness(
    cli_env: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 The domain owes more; the CONNECTION's history is still in.

    `connections.last_success_at` is what every freshness surface reads, and it
    answers "is this connection's transaction backfill in". Routing an
    investments shortfall through the pager's own flag withheld it — so the
    connection then read stale, `_domain_caveats` suppressed the per-domain
    caveat precisely BECAUSE the connection read stale too, and the operator was
    sent to look at a connection for something belonging to one domain.
    """
    _capable_connection(cli_env)
    # One page against a stated 99, so the window is seen in part and the loop
    # stops where a real one would: at its ceiling, with more to fetch.
    monkeypatch.setattr("bankmachine.cli.sync_run.MAX_PAGES_PER_RUN", 1)
    FakeClient.pages = [_page()]
    FakeClient.investment_transaction_pages = [
        _investment_transactions_body([_investment_txn("inv-1")], total=99)
    ]

    assert run(["sync", "run", "--no-wait"]) == 75, "the run still owes the window"

    with reader_connection(cli_env) as conn:
        stamped = conn.execute(
            select(connections.c.last_success_at).where(connections.c.connection_id == 2)
        ).scalar_one()
    assert stamped is not None, (
        "an investments shortfall withheld the connection's own freshness stamp"
    )
    assert _domain_row(cli_env, 2, INVESTMENTS_DOMAIN)["last_success_at"] is None, (
        "the DOMAIN is not current, which is the fact that must survive"
    )


def test_an_attempt_that_came_back_short_retires_the_error_it_did_not_repeat(
    cli_env: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 `last_error_code` is what the LAST attempt failed with, as published.

    Writing nothing for an attempt that neither failed nor finished left the
    previous run's code standing on a row whose last attempt reached the
    aggregator and came back clean — and three surfaces then disagreed about
    whether the domain was currently failing.
    """
    _capable_connection(cli_env)
    FakeClient.pages = [_page()]
    FakeClient.holdings_error = TransportError(
        "the aggregator is unreachable", endpoint=INVESTMENTS_HOLDINGS_GET
    )
    assert run(["sync", "run", "--no-wait"]) == 1
    assert (
        _domain_row(cli_env, 2, INVESTMENTS_DOMAIN)["last_error_code"] == "AGGREGATOR_UNREACHABLE"
    )

    FakeClient.holdings_error = None
    monkeypatch.setattr("bankmachine.cli.sync_run.MAX_PAGES_PER_RUN", 1)
    FakeClient.pages = [_page(next_cursor="cursor-2")]
    FakeClient.investment_transaction_pages = [
        _investment_transactions_body([_investment_txn("inv-1")], total=99)
    ]

    assert run(["sync", "run", "--no-wait"]) == 75

    domain = _domain_row(cli_env, 2, INVESTMENTS_DOMAIN)
    assert domain["last_error_code"] is None, (
        "the domain reports a failure that is not the last thing that happened to it"
    )
    assert domain["last_error_at"] is None
    assert domain["last_success_at"] is None, "a short window did not make the domain current"


def test_a_connection_that_lost_its_login_is_not_told_no_reauth_is_needed(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 Both failures in one run, and the two lines contradicted each other.

    The investments call fails with a product error — recorded against the
    domain, connection left alone — and the page loop afterwards hits an expired
    login. The domain line's "no re-authentication is needed" then printed two
    lines under the instruction to re-authenticate.
    """
    _capable_connection(cli_env)
    FakeClient.holdings_error = TransportError(
        "the aggregator is unreachable", endpoint=INVESTMENTS_HOLDINGS_GET
    )
    FakeClient.fail_with = ReauthRequiredError(
        "the login expired", endpoint=TRANSACTIONS_SYNC, error_code="ITEM_LOGIN_REQUIRED"
    )

    assert run(["sync", "run", "--no-wait"]) == 1

    out = capsys.readouterr().out
    assert "connections reauth" in out, "the repair instruction is the line that matters"
    assert "no re-authentication is needed" not in out, out
    assert (
        _domain_row(cli_env, 2, INVESTMENTS_DOMAIN)["last_error_code"] == "AGGREGATOR_UNREACHABLE"
    ), "the domain failure is still recorded; only the contradicting sentence is withheld"


def test_a_complete_and_empty_window_retires_the_whole_window_and_says_so(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 The highest-consequence branch the reconciliation has.

    A reply stating `total_investment_transactions: 0` beside no rows IS an
    exhausted window, so every aggregator row inside it has gone away and is
    retired. That is the feed's only removal signal applied at its widest, and
    the thing that makes it safe rather than reckless is that exhaustion is
    derived from the aggregator's own count rather than taken as a caller's
    promise. It is reported, because a bulk soft delete otherwise leaves no trace
    at all — the rows simply stop appearing in every total.
    """
    _capable_connection(cli_env)
    FakeClient.pages = [_page()]
    FakeClient.investment_transaction_pages = [
        _investment_transactions_body([_investment_txn("inv-1"), _investment_txn("inv-2")])
    ]
    assert run(["sync", "run", "--no-wait"]) == 0
    assert len(_investment_transaction_rows(cli_env)) == 2
    capsys.readouterr()

    FakeClient.pages = [_page(next_cursor="cursor-2")]
    FakeClient.investment_transaction_pages = [_investment_transactions_body([], total=0)]

    assert run(["sync", "run", "--no-wait"]) == 0

    rows = _investment_transaction_rows(cli_env)
    assert len(rows) == 2, "a removal is a soft delete; no row is dropped"
    assert all(row["removed_at"] is not None for row in rows), (
        "a window that came back complete and empty says every row in it has gone"
    )
    assert (
        "2 transaction(s) are no longer reported" in _per_connection(capsys.readouterr().out)["2"]
    )


@pytest.mark.parametrize(
    ("branch", "marker", "code"),
    [
        ("materializing", "still being prepared", 75),
        ("degraded", "the aggregator is unreachable", 1),
    ],
)
def test_a_retired_investment_transaction_is_reported_whatever_branch_the_pager_takes(
    cli_env: Config, capsys: pytest.CaptureFixture[str], branch: str, marker: str, code: int
) -> None:
    """🔴 The count has to survive the branch the pager takes afterwards.

    `_pull_investments` runs BEFORE the page loop, so a window that reconciled
    cleanly sits behind a connection the pager then reports as still preparing
    its history, or as degraded. A soft delete leaves no trace in any total, so a
    report that dropped the count in exactly those runs would leave the one
    surface that names it silent whenever something else about the connection
    also went wrong.

    Both branches, because one of them passes against a report that suppresses
    the other: the lines sit at the loop's level and nothing in the code
    distinguishes the two, which is a claim a single case cannot make.
    """
    _capable_connection(cli_env)
    FakeClient.pages = [_page()]
    FakeClient.investment_transaction_pages = [
        _investment_transactions_body([_investment_txn("inv-1"), _investment_txn("inv-2")])
    ]
    assert run(["sync", "run", "--no-wait"]) == 0
    capsys.readouterr()

    FakeClient.investment_transaction_pages = [_investment_transactions_body([], total=0)]
    if branch == "materializing":
        FakeClient.pages = [_page(status="NOT_READY")]
    else:
        FakeClient.fail_with = TransportError(
            "the aggregator is unreachable", endpoint=TRANSACTIONS_SYNC
        )

    assert run(["sync", "run", "--no-wait"]) == code

    reported = _per_connection(capsys.readouterr().out)
    assert marker in reported["2"], "the branch this test needs was not taken"
    assert "2 transaction(s) are no longer reported" in reported["2"]
    assert "positions recorded" in reported["2"]


def test_a_domain_refusal_records_the_aggregators_own_code(cli_env: Config) -> None:
    """AC-4.2 at domain scope: the code on `sync_state` is the aggregator's, verbatim."""
    _capable_connection(cli_env)
    FakeClient.holdings_error = InstitutionUnavailableError(
        "the institution is down",
        endpoint=INVESTMENTS_HOLDINGS_GET,
        error_code="INSTITUTION_DOWN",
    )
    FakeClient.pages = [_page(next_cursor="cursor-1")]

    assert run(["sync", "run", "--no-wait"]) == 1

    assert _domain_row(cli_env, 2, INVESTMENTS_DOMAIN)["last_error_code"] == "INSTITUTION_DOWN"


def test_a_complete_and_empty_transactions_feed_is_not_reported_as_never_landed(
    cli_env: Config,
) -> None:
    """🔴 An institution with no cash accounts finishes its transactions feed empty.

    The aggregator answers `HISTORICAL_UPDATE_COMPLETE` with no changes and an
    empty cursor. The run is complete, and the health surface has to agree with
    it: a `partial` caveat saying the feed "never landed in full" rides every
    answer and tells an agent to distrust data that is all there.

    Multi-hop, because the empty cursor is not stored and the next run starts the
    feed from nothing again -- which must land it again rather than undo it.
    """
    FakeClient.pages = [_page()]
    assert run(["sync", "run"]) == 0

    for _ in range(2):
        domain = _domain_row(cli_env, 1, TRANSACTIONS_DOMAIN)
        assert domain is not None and domain["last_success_at"] is not None
        assert domain["cursor"] is None
        never_landed = [
            w
            for w in query.pipeline_health(cli_env).to_wire()["warnings"]
            if "never landed" in w["detail"]
        ]
        assert never_landed == [], never_landed

        FakeClient.pages = [_page()]
        assert run(["sync", "run"]) == 0
