"""A rebuild with investments in it (AC-5.2, AC-2.4), through the real derivers.

`test_rebuild.py` tests the machinery against stand-in derivers, which is what
lets it state the shape of the guarantee. This file tests the one feed whose
guarantee the machinery alone does not deliver.

🔴 **The whole file exists for one direction of failure.** A removal on
`/investments/transactions/get` is a row's ABSENCE from a window that came back
whole *(`api-notes-plaid.md` §26)*, and a deriver sees one page. Replaying the
archive through the derivers re-upserts every row that ever appeared and CLEARS
`removed_at` on each -- so a rebuild would report success over a store holding
transactions the source had dropped, and every other rebuild assertion in this
repo would pass straight through it. `connector.plaid.window.InvestmentWindowReplay`
is what closes that, and the tests below are written from the resurrection's
point of view rather than from the fix's.

The rows are small and synthetic rather than the recorded 100-row capture,
because what is under test is which pages constituted which window -- and one
test does replay the recorded capture, so the real shape is exercised too.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import insert, select

from bankmachine.config import DEFAULT_CONNECTION_CAP, MAX_HISTORY_DAYS, Config
from bankmachine.connector import (
    ACCOUNTS_GET,
    INVESTMENTS_HOLDINGS_GET,
    INVESTMENTS_TRANSACTIONS_GET,
)
from bankmachine.connector.plaid.client import INVESTMENT_TRANSACTIONS_PAGE_SIZE
from bankmachine.connector.plaid.window import (
    InvestmentWindowReplay,
    UnreadableWindowError,
    investment_window_context,
)
from bankmachine.derivers import ALL_DERIVERS, all_replay_passes
from bankmachine.secrets import delete_datastore_key, generate_datastore_key, set_datastore_key
from bankmachine.store.derivation import apply_response
from bankmachine.store.engine import reader_connection, transaction, writer_connection
from bankmachine.store.investments import record_investment_transaction_window
from bankmachine.store.migrations import migrate
from bankmachine.store.raw import load_response
from bankmachine.store.rebuild import content_digest, rebuild
from bankmachine.store.schema import (
    INVESTMENTS_DOMAIN,
    accounts,
    connections,
    holdings,
    institutions,
    investment_transactions,
    manual_imports,
    raw_responses,
    securities,
)
from bankmachine.store.sync_domains import record_domain_history_start
from bankmachine.store.types import UtcInstant, calendar_date, utc_instant

FIXTURES = Path(__file__).parents[1] / "connector" / "fixtures"

CONNECTION_ID = 1
ACCOUNT = "brokerage-1"
SECURITY = "sec-1"

WINDOW_END = date(2026, 9, 12)
WINDOW_START = WINDOW_END - timedelta(days=730)


def instant(hours: int) -> UtcInstant:
    return utc_instant(datetime(2026, 9, 12, 0, 0, tzinfo=UTC) + timedelta(hours=hours))


# --------------------------------------------------------------------------
# Bodies, in the shape §22-26 recorded
# --------------------------------------------------------------------------


def _account_entry() -> dict[str, Any]:
    """The brokerage account, in full.

    Written out once because every investments body carries the roster too, and
    the deriver derives it from whichever body it arrives in -- a stub entry in
    one of them refuses the whole page rather than the account.
    """
    return {
        "account_id": ACCOUNT,
        "name": "Brokerage",
        "type": "investment",
        "subtype": "brokerage",
        "balances": {"current": 1000, "iso_currency_code": "USD"},
    }


def _accounts_body() -> bytes:
    return json.dumps(
        {
            "accounts": [_account_entry()],
            "item": {"item_id": "item-investments"},
            "request_id": "req-accounts",
        }
    ).encode()


def _security(security_id: str = SECURITY) -> dict[str, Any]:
    return {
        "security_id": security_id,
        "name": "Cambiar International Equity",
        "ticker_symbol": "CAMB",
        "type": "mutual fund",
        "iso_currency_code": "USD",
        "close_price": 25,
        "close_price_as_of": "2026-09-11",
    }


def _txn(transaction_id: str, *, day: int = 0, amount: str = "-0.22") -> dict[str, Any]:
    """One investment transaction. `day` counts back from the window's end."""
    return {
        "investment_transaction_id": transaction_id,
        "account_id": ACCOUNT,
        "security_id": SECURITY,
        "date": (WINDOW_END - timedelta(days=day)).isoformat(),
        "type": "sell",
        "subtype": "sell",
        "quantity": "-0.0089",
        "price": "25",
        "fees": "5",
        "amount": amount,
        "iso_currency_code": "USD",
        "name": "SELL Cambiar International Equity",
    }


def _transactions_body(entries: Sequence[dict[str, Any]], *, total: int | None) -> bytes:
    payload: dict[str, Any] = {
        "accounts": [_account_entry()],
        "securities": [_security()],
        "investment_transactions": list(entries),
        "item": {"item_id": "item-investments"},
        "request_id": "req-investment-transactions",
    }
    if total is not None:
        payload["total_investment_transactions"] = total
    return json.dumps(payload).encode()


def _holdings_body(*, quantity: str, value: str, carry_accounts: bool = True) -> bytes:
    """One position. `carry_accounts` omits the roster array the body rides with.

    An absent array is not an empty one -- the deriver leaves the roster alone
    -- which is what lets a test say something about the holdings series without
    also saying it about the accounts the body happened to name.
    """
    payload: dict[str, Any] = {
        "securities": [_security()],
        "holdings": [
            {
                "account_id": ACCOUNT,
                "security_id": SECURITY,
                "quantity": quantity,
                "institution_price": "25",
                "institution_value": value,
                "cost_basis": "100.5",
                "iso_currency_code": "USD",
            }
        ],
        "item": {"item_id": "item-investments"},
        "request_id": "req-holdings",
    }
    if carry_accounts:
        payload["accounts"] = [_account_entry()]
    return json.dumps(payload).encode()


# --------------------------------------------------------------------------
# A store that has synced, so a rebuild has something to reproduce
# --------------------------------------------------------------------------


@pytest.fixture
def enrolled(initialized_config: Config) -> Config:
    """One capable connection with its brokerage account already derived."""
    with writer_connection(initialized_config) as conn:
        primary_key = conn.execute(
            insert(institutions).values(
                source_institution_id="ins_109511",
                name="Tattersall Federal Credit Union",
                first_seen_at=instant(0),
                last_seen_at=instant(0),
            )
        ).inserted_primary_key
        assert primary_key is not None  # an INTEGER PRIMARY KEY insert always yields one
        conn.execute(
            insert(connections).values(
                connection_id=CONNECTION_ID,
                institution_id=primary_key[0],
                source_connection_id="item-investments",
                credential_ref="connection:sandbox:item-investments",
                capabilities='["investments"]',
                status="active",
                enrolled_at=instant(0),
                created_at=instant(0),
                updated_at=instant(0),
            )
        )
    _archive(initialized_config, ACCOUNTS_GET.path, _accounts_body(), at=instant(0))
    return initialized_config


def _archive(
    config: Config,
    endpoint: str,
    body: bytes,
    *,
    at: UtcInstant,
    request_context: str | None = None,
) -> int:
    with writer_connection(config) as conn:
        response = apply_response(
            conn,
            connection_id=CONNECTION_ID,
            endpoint=endpoint,
            body=body,
            received_at=at,
            derivers=ALL_DERIVERS,
            request_context=request_context,
        )
    return response.raw_response_id


def sync_a_window(
    config: Config,
    pages: Sequence[Sequence[dict[str, Any]]],
    *,
    total: int | None,
    at: UtcInstant,
) -> None:
    """Archive one window's pages and reconcile it, exactly as `sync run` does.

    🔴 **The `request_context` is built by the production formatter**, because
    the reply does not echo the window back (§26) and that string is the only
    thing a rebuild can reassemble a window from. A fixture that spelled it out
    here would keep passing after the formatter changed, and the replay it is
    meant to exercise would silently stop finding any window at all.
    """
    page_ids: list[int] = []
    rows_seen = 0
    for page in pages:
        page_ids.append(
            _archive(
                config,
                INVESTMENTS_TRANSACTIONS_GET.path,
                _transactions_body(page, total=total),
                at=at,
                request_context=investment_window_context(
                    start_date=WINDOW_START,
                    end_date=WINDOW_END,
                    offset=rows_seen,
                    count=INVESTMENT_TRANSACTIONS_PAGE_SIZE,
                ),
            )
        )
        rows_seen += len(page)
    with writer_connection(config) as conn, transaction(conn):
        outcome = record_investment_transaction_window(
            conn,
            connection_id=CONNECTION_ID,
            window_start=calendar_date(WINDOW_START),
            window_end=calendar_date(WINDOW_END),
            page_response_ids=page_ids,
            rows_seen=rows_seen,
            stated_total=total,
            at=at,
        )
        if outcome.history_start_date is not None:
            record_domain_history_start(
                conn,
                connection_id=CONNECTION_ID,
                domain=INVESTMENTS_DOMAIN,
                start=outcome.history_start_date,
                at=at,
            )


def stored(config: Config, table: Any) -> list[dict[str, Any]]:
    with reader_connection(config) as conn:
        return [dict(row) for row in conn.execute(select(table)).mappings()]


def by_source_id(config: Config) -> dict[str, dict[str, Any]]:
    return {
        row["source_investment_transaction_id"]: row
        for row in stored(config, investment_transactions)
    }


def digest_of(config: Config) -> str:
    with reader_connection(config) as conn:
        return content_digest(conn)


def rebuilt(config: Config) -> Any:
    return rebuild(config, derivers=ALL_DERIVERS, replay_passes=all_replay_passes)


# --------------------------------------------------------------------------
# The resurrection, and the property that stops it
# --------------------------------------------------------------------------


def test_a_rebuild_reproduces_a_soft_delete_rather_than_undoing_it(enrolled: Config) -> None:
    """🔴 The assertion every other rebuild test passes straight through.

    The replay re-upserts every row that ever appeared and clears `removed_at`
    as it goes, so without the window replay the rebuilt store holds a
    transaction the synced store had retired -- and says the rebuild succeeded.
    """
    sync_a_window(enrolled, [[_txn("inv-1"), _txn("inv-2", day=1)]], total=2, at=instant(1))
    sync_a_window(enrolled, [[_txn("inv-1")]], total=1, at=instant(2))
    assert by_source_id(enrolled)["inv-2"]["removed_at"] == instant(2)
    before = digest_of(enrolled)

    report = rebuilt(enrolled)

    assert not report.content_changed
    assert digest_of(enrolled) == before
    retired = by_source_id(enrolled)["inv-2"]
    assert retired["removed_at"] == instant(2), (
        "the replay resurrected a transaction the source had dropped"
    )


def test_a_rebuild_reproduces_a_window_that_retired_everything_in_it(
    enrolled: Config,
) -> None:
    """🔴 The widest removal this feed can express, replayed.

    A reply stating `total_investment_transactions: 0` beside no rows is an
    EXHAUSTED window, so every aggregator row inside it has gone away. That is
    one archived body retiring a connection's whole window, and the replay has to
    reach the same conclusion from the same body -- a rebuild that quietly
    resurrected the lot would be the largest version of the defect this file
    exists for.
    """
    sync_a_window(enrolled, [[_txn("inv-1"), _txn("inv-2", day=1)]], total=2, at=instant(1))
    sync_a_window(enrolled, [[]], total=0, at=instant(2))
    assert all(
        row["removed_at"] == instant(2) for row in stored(enrolled, investment_transactions)
    ), "the empty window retired nothing, so the rebuild has nothing to reproduce"
    before = digest_of(enrolled)

    report = rebuilt(enrolled)

    assert not report.content_changed
    assert digest_of(enrolled) == before
    assert all(row["removed_at"] == instant(2) for row in stored(enrolled, investment_transactions))


def test_a_rebuild_judges_each_window_against_the_rows_that_existed_when_it_closed(
    enrolled: Config,
) -> None:
    """🔴 Why the reconciliation is re-run DURING the replay, not after it.

    Once every page is replayed, each surviving row carries the id of the LAST
    page it appeared on -- so running the first window's reconciliation over the
    finished tables would find the rows that only arrived in the second window
    absent from the first, and retire every one of them. No run ever reached
    that conclusion, and the store it produces is not the store that was synced.
    """
    sync_a_window(enrolled, [[_txn("inv-a"), _txn("inv-b", day=1)]], total=2, at=instant(1))
    sync_a_window(enrolled, [[_txn("inv-a"), _txn("inv-c", day=2)]], total=2, at=instant(2))

    rebuilt(enrolled)

    rows = by_source_id(enrolled)
    assert rows["inv-b"]["removed_at"] == instant(2)
    assert rows["inv-a"]["removed_at"] is None
    assert rows["inv-c"]["removed_at"] is None, (
        "a row the second window returned was retired by the first window's replay"
    )


def test_a_rebuild_reproduces_the_recorded_capture_exactly(enrolled: Config) -> None:
    """The real shape, not a synthetic one: the live capture, replayed.

    One page of 1169, so the window is NOT exhausted and nothing is reconciled
    -- which is the ordinary state of a first sync and has to rebuild too.
    """
    payload = json.loads((FIXTURES / "investments_transactions_get.json").read_bytes())
    _archive(enrolled, ACCOUNTS_GET.path, _accounts_body_from(payload), at=instant(0))
    _archive(
        enrolled,
        INVESTMENTS_TRANSACTIONS_GET.path,
        (FIXTURES / "investments_transactions_get.json").read_bytes(),
        at=instant(1),
        request_context=investment_window_context(
            start_date=WINDOW_START,
            end_date=WINDOW_END,
            offset=0,
            count=INVESTMENT_TRANSACTIONS_PAGE_SIZE,
        ),
    )
    before = digest_of(enrolled)
    assert len(stored(enrolled, investment_transactions)) == 100

    report = rebuilt(enrolled)

    assert not report.content_changed
    assert digest_of(enrolled) == before


def _accounts_body_from(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        {
            "accounts": payload["accounts"],
            "item": {"item_id": "item-investments"},
            "request_id": "req-accounts",
        }
    ).encode()


def test_a_rebuild_reproduces_holdings_and_the_day_they_were_captured(
    enrolled: Config,
) -> None:
    """AC-5.2 for the positions half: the append-only series comes back whole."""
    _archive(
        enrolled,
        INVESTMENTS_HOLDINGS_GET.path,
        _holdings_body(quantity="1.5", value="37.5"),
        at=instant(1),
    )
    before = digest_of(enrolled)
    positions = stored(enrolled, holdings)
    assert len(positions) == 1

    report = rebuilt(enrolled)

    assert not report.content_changed
    assert digest_of(enrolled) == before
    assert stored(enrolled, holdings) == positions


def test_a_rebuild_keeps_the_first_capture_of_a_holdings_day_whatever_order_it_replays(
    enrolled: Config,
) -> None:
    """🔴 A sync archives in arrival order; a replay orders by `received_at`.

    So the two orders can disagree, and the day-keyed series has to land on the
    same capture either way -- which it does because "first" is decided by
    COMPARING captures rather than by arriving first. Here the store is given
    the 14:00 capture before the 09:00 one, and the replay meets them the other
    way round: if arrival decided it, the live row would be 09:00's and the
    rebuilt one 14:00's.

    The bodies carry no `accounts` array, so what this says about ordering is
    about the holdings series alone.
    """
    _archive(
        enrolled,
        INVESTMENTS_HOLDINGS_GET.path,
        _holdings_body(quantity="9", value="225", carry_accounts=False),
        at=instant(14),
    )
    _archive(
        enrolled,
        INVESTMENTS_HOLDINGS_GET.path,
        _holdings_body(quantity="1", value="25", carry_accounts=False),
        at=instant(9),
    )
    kept = stored(enrolled, holdings)
    assert [row["quantity"] for row in kept] == ["1"]
    before = digest_of(enrolled)

    report = rebuilt(enrolled)

    assert not report.content_changed
    assert digest_of(enrolled) == before
    assert stored(enrolled, holdings) == kept


def test_a_rebuild_leaves_a_manually_imported_investment_transaction_alone(
    enrolled: Config,
) -> None:
    """A row no response could recreate is never deleted, and never retired.

    Two claims in one, because the rebuild reaches this row twice: the delete
    pass takes exactly the rows carrying a `raw_response_id`, and the replayed
    window's reconciliation takes exactly the rows the aggregator authored.
    """
    sync_a_window(enrolled, [[_txn("inv-1")]], total=1, at=instant(1))
    account_id = stored(enrolled, accounts)[0]["account_id"]
    with writer_connection(enrolled) as conn:
        import_key = conn.execute(
            insert(manual_imports).values(
                account_id=account_id,
                adapter="csv",
                source_name="a brokerage CSV",
                file_sha256="0" * 64,
                file_bytes=1024,
                imported_at=instant(0),
                rows_seen=1,
                rows_applied=1,
            )
        ).inserted_primary_key
        assert import_key is not None  # an INTEGER PRIMARY KEY insert always yields one
        version_id = (
            conn.execute(select(investment_transactions.c.derivation_version_id)).scalars().first()
        )
        conn.execute(
            insert(investment_transactions).values(
                account_id=account_id,
                source_investment_transaction_id=None,
                trade_date=calendar_date(WINDOW_END - timedelta(days=5)),
                investment_type="buy",
                amount_minor=-1000,
                currency="USD",
                source="manual",
                manual_import_id=import_key[0],
                raw_response_id=None,
                derivation_version_id=version_id,
                import_fingerprint="csv-row-1",
                first_seen_at=instant(0),
                updated_at=instant(0),
            )
        )
    before = digest_of(enrolled)

    report = rebuilt(enrolled)

    assert not report.content_changed
    assert digest_of(enrolled) == before
    manual = [row for row in stored(enrolled, investment_transactions) if row["source"] == "manual"]
    assert len(manual) == 1
    assert manual[0]["removed_at"] is None


def test_a_rebuild_refuses_an_investments_page_that_does_not_say_what_window_it_answered(
    enrolled: Config,
) -> None:
    """An unattributable page is a refusal, not a window quietly skipped.

    Skipping would leave the reconciliation unrun and every retired row back in
    the totals, under a rebuild that reported success -- the silent
    incompleteness that still adds up.
    """
    sync_a_window(enrolled, [[_txn("inv-1")]], total=1, at=instant(1))
    with writer_connection(enrolled) as conn, transaction(conn):
        conn.exec_driver_sql(
            "UPDATE raw_responses SET request_context = NULL WHERE endpoint = ?",
            (INVESTMENTS_TRANSACTIONS_GET.path,),
        )
    before = digest_of(enrolled)

    with pytest.raises(UnreadableWindowError, match="does not record the window it answered"):
        rebuilt(enrolled)

    assert digest_of(enrolled) == before, "the refused rebuild was not rolled back"


def test_a_window_whose_opening_page_the_archive_lost_reconciles_nothing(
    enrolled: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """🔴 The fail-open this guard exists for, one layer in from the store's.

    `store.investments` refuses a window offered as complete with no pages at
    all. This is the case it cannot see: pages that ARE present, but not the
    ones the window opened with -- so every row that sat on the missing pages is
    absent from what the replay assembled, and reconciling would soft-delete the
    lot on the path that believes the window was whole. `inv-1` is exactly such
    a row: it came back on the opening page and on no other.
    """
    sync_a_window(enrolled, [[_txn("inv-1")], [_txn("inv-2", day=1)]], total=2, at=instant(1))
    with reader_connection(enrolled) as conn:
        second_page = conn.execute(
            select(raw_responses.c.raw_response_id).where(
                raw_responses.c.endpoint == INVESTMENTS_TRANSACTIONS_GET.path,
                raw_responses.c.request_context.like("%offset=1 %"),
            )
        ).scalar_one()

    replay = InvestmentWindowReplay()
    with writer_connection(enrolled) as conn, transaction(conn), caplog.at_level(logging.WARNING):
        replay.observe(conn, load_response(conn, second_page))

    assert by_source_id(enrolled)["inv-1"]["removed_at"] is None, (
        "a window assembled from only the pages that survived retired the rows that did not"
    )
    assert "cannot be assembled" in caplog.text


# --------------------------------------------------------------------------
# Losslessness as an invariant (project preferences: property-based)
# --------------------------------------------------------------------------


@st.composite
def _windows(draw: st.DrawFn) -> list[list[str]]:
    """A sequence of windows, each naming the transactions it returned.

    Ids are drawn from a small pool so that a row dropping out of a later window
    -- the only thing that produces a soft delete on this feed -- is a frequent
    case rather than one hypothesis has to stumble onto.
    """
    pool = [f"inv-{index}" for index in range(4)]
    return draw(
        st.lists(
            st.lists(st.sampled_from(pool), min_size=0, max_size=4, unique=True),
            min_size=1,
            max_size=4,
        )
    )


@given(windows=_windows(), pages_per_window=st.integers(min_value=1, max_value=2))
@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_a_rebuild_is_lossless_for_any_sequence_of_windows(
    tmp_path_factory: pytest.TempPathFactory,
    windows: list[list[str]],
    pages_per_window: int,
) -> None:
    """Losslessness is an invariant, so it is stated as one (per project preferences).

    Every generated corpus varies the two dimensions that decide what a replay
    has to reassemble: how many windows the connection has seen, and how many
    pages each of them came back in. A soft delete appears wherever a row leaves
    the pool between two windows, and the rebuild has to reproduce each one at
    the instant its own window concluded.
    """
    with temporary_store(tmp_path_factory.mktemp("rebuild-investments")) as config:
        _seed(config)
        for index, returned in enumerate(windows, start=1):
            entries = [_txn(name, day=int(name.rsplit("-", 1)[1])) for name in sorted(returned)]
            pages = _split(entries, pages_per_window)
            sync_a_window(config, pages, total=len(entries), at=instant(index))
        before = digest_of(config)
        rows_before = stored(config, investment_transactions)

        report = rebuild(config, derivers=ALL_DERIVERS, replay_passes=all_replay_passes)

        assert not report.content_changed
        assert digest_of(config) == before
        assert stored(config, investment_transactions) == rows_before


def _split(entries: list[dict[str, Any]], pages: int) -> list[list[dict[str, Any]]]:
    """The entries dealt into `pages` pages, the last ones possibly empty.

    An empty page is not padding: a window whose stated total is reached on an
    earlier page is exactly how the live loop exits, so the replay has to close
    the window on the page that crossed the total rather than on the last one.
    """
    if pages <= 1 or not entries:
        return [entries]
    size = max(1, (len(entries) + pages - 1) // pages)
    return [entries[start : start + size] for start in range(0, len(entries), size)] or [[]]


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


def _seed(config: Config) -> None:
    with writer_connection(config) as conn:
        primary_key = conn.execute(
            insert(institutions).values(
                source_institution_id="ins_109511",
                name="Tattersall Federal Credit Union",
                first_seen_at=instant(0),
                last_seen_at=instant(0),
            )
        ).inserted_primary_key
        assert primary_key is not None  # an INTEGER PRIMARY KEY insert always yields one
        conn.execute(
            insert(connections).values(
                connection_id=CONNECTION_ID,
                institution_id=primary_key[0],
                source_connection_id="item-investments",
                credential_ref="connection:sandbox:item-investments",
                capabilities='["investments"]',
                status="active",
                enrolled_at=instant(0),
                created_at=instant(0),
                updated_at=instant(0),
            )
        )
    _archive(config, ACCOUNTS_GET.path, _accounts_body(), at=instant(0))


def test_the_securities_dimension_survives_a_rebuild_that_does_not_delete_it(
    enrolled: Config,
) -> None:
    """A dimension row is upserted on its natural key, so the replay meets it again.

    Worth its own case because the failure is not a wrong value: a plain insert
    raises on the replay's second pass, and the rebuild would be reported as a
    deriver-purity bug rather than as the missing upsert it is.
    """
    sync_a_window(enrolled, [[_txn("inv-1")]], total=1, at=instant(1))
    _archive(
        enrolled,
        INVESTMENTS_HOLDINGS_GET.path,
        _holdings_body(quantity="1.5", value="37.5"),
        at=instant(2),
    )
    before = stored(enrolled, securities)
    assert len(before) == 1

    report = rebuilt(enrolled)

    assert not report.content_changed
    assert stored(enrolled, securities) == before
