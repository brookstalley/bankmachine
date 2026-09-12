"""Investment transactions (FR-3, AC-3.3): the rows, and the window they came in.

The oracle for the happy path is the response the aggregator actually sent --
`fixtures/investments_transactions_get.json`, one page recorded live from an Item
enrolled with the investments product -- and every hostile case is that same
shape with one field changed.

🔴 **Two things the recorded payload cannot exercise, both measured rather than
assumed** (`api-notes-plaid.md` §26):

- An **undenominable** row: every transaction in it is USD.
- A **removal**: the feed sends no removal signal at all, so the soft delete is
  driven by a row's absence from a window that came back complete. There is no
  payload that expresses that -- it is a property of a set of pages -- so the
  window cases below construct one and, crucially, also assert the case where
  the window was NOT complete and nothing may be concluded from it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert, select

from bankmachine.config import Config
from bankmachine.connector import ACCOUNTS_GET, INVESTMENTS_TRANSACTIONS_GET
from bankmachine.derivers import ALL_DERIVERS
from bankmachine.store.derivation import DerivationError, apply_response
from bankmachine.store.engine import reader_connection, transaction, writer_connection
from bankmachine.store.investments import (
    IncompleteWindowError,
    record_investment_transaction_window,
)
from bankmachine.store.schema import (
    INVESTMENTS_DOMAIN,
    accounts,
    connections,
    institutions,
    investment_transactions,
    manual_imports,
    securities,
    sync_state,
)
from bankmachine.store.types import UtcInstant, calendar_date, utc_instant

FIXTURES = Path(__file__).parent / "fixtures"

RECEIVED = utc_instant(datetime(2026, 9, 12, 14, 0, tzinfo=UTC))
LATER = utc_instant(datetime(2026, 9, 13, 14, 0, tzinfo=UTC))

CONNECTION_ID = 1

#: The window the recorded capture was fetched over, as the probe asked for it.
WINDOW_END = calendar_date(datetime(2026, 9, 12, tzinfo=UTC).date())
WINDOW_START = calendar_date(WINDOW_END - timedelta(days=730))


def recorded() -> dict[str, Any]:
    """The live capture, parsed the way the deriver parses it: numbers as text."""
    body: dict[str, Any] = json.loads(
        (FIXTURES / "investments_transactions_get.json").read_bytes(), parse_float=str
    )
    return body


def _body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode()


def _accounts_body(entries: list[dict[str, Any]]) -> bytes:
    return json.dumps(
        {"accounts": entries, "item": {"item_id": "item-x"}, "request_id": "req-accounts"}
    ).encode()


@pytest.fixture
def enrolled(initialized_config: Config) -> Config:
    """A connection with the recorded capture's own accounts already derived."""
    _seed_connection(initialized_config)
    _apply(initialized_config, ACCOUNTS_GET.path, _accounts_body(recorded()["accounts"]))
    return initialized_config


def _seed_connection(config: Config) -> None:
    with writer_connection(config) as conn:
        primary_key = conn.execute(
            insert(institutions).values(
                source_institution_id="ins_109511",
                name="Tattersall Federal Credit Union",
                first_seen_at=RECEIVED,
                last_seen_at=RECEIVED,
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
                enrolled_at=RECEIVED,
                created_at=RECEIVED,
                updated_at=RECEIVED,
            )
        )


def _apply(
    config: Config,
    endpoint: str,
    body: bytes,
    *,
    received_at: UtcInstant = RECEIVED,
    connection_id: int | None = CONNECTION_ID,
) -> int:
    """Archive and derive one body, returning its raw response id."""
    with writer_connection(config) as conn:
        response = apply_response(
            conn,
            connection_id=connection_id,
            endpoint=endpoint,
            body=body,
            received_at=received_at,
            derivers=ALL_DERIVERS,
            request_context=None,
        )
    return response.raw_response_id


def rows(config: Config, table: Any) -> list[dict[str, Any]]:
    with reader_connection(config) as conn:
        return [dict(row) for row in conn.execute(select(table)).mappings()]


def stored_row(config: Config, source_id: str) -> dict[str, Any]:
    with reader_connection(config) as conn:
        matched = [
            dict(row)
            for row in conn.execute(
                select(investment_transactions).where(
                    investment_transactions.c.source_investment_transaction_id == source_id
                )
            ).mappings()
        ]
    assert len(matched) == 1, f"expected one stored transaction, found {len(matched)}"
    return matched[0]


def entry_named(payload: dict[str, Any], **fields: Any) -> dict[str, Any]:
    """The first recorded row matching every field, asserted to exist.

    🔴 Asserted rather than returned-or-None: a helper that quietly finds nothing
    makes every test built on it pass against a payload that cannot reach the
    behaviour under test.
    """
    for entry in payload["investment_transactions"]:
        if all(entry.get(key) == value for key, value in fields.items()):
            matched: dict[str, Any] = entry
            return matched
    raise AssertionError(f"the recorded capture carries no row matching {fields}")


def _finer_than_a_cent(value: object) -> bool:
    """Whether a number, as the aggregator SENT it, carries sub-cent precision.

    Read off the digits rather than off `Decimal.as_tuple().exponent`, which is
    typed as possibly being one of the special-value markers and so cannot be
    compared to an integer without a guard that says nothing about this test.
    """
    _, _, fraction = str(value).partition(".")
    return len(fraction) > 2


# --------------------------------------------------------------------------
# The recorded capture, end to end
# --------------------------------------------------------------------------


def test_the_recorded_capture_lands_as_securities_and_transactions(enrolled: Config) -> None:
    """AC-3.3's happy path, against the payload the aggregator actually sent."""
    payload = recorded()
    _apply(enrolled, INVESTMENTS_TRANSACTIONS_GET.path, _body(payload))

    stored = rows(enrolled, investment_transactions)
    assert len(stored) == len(payload["investment_transactions"])
    assert len(rows(enrolled, securities)) == len(payload["securities"])
    assert {row["source_investment_transaction_id"] for row in stored} == {
        entry["investment_transaction_id"] for entry in payload["investment_transactions"]
    }
    for row in stored:
        assert row["source"] == "aggregator"
        assert row["raw_response_id"] is not None
        assert row["manual_import_id"] is None
        assert row["removed_at"] is None
        assert row["first_seen_at"] == RECEIVED
        # 🔴 The feed has no settlement date (§26), so the column stays null
        # rather than being filled with the trade date -- which would
        # manufacture a settlement the institution never stated.
        assert row["settlement_date"] is None


def test_the_domain_records_its_success_on_the_investments_row(enrolled: Config) -> None:
    _apply(enrolled, INVESTMENTS_TRANSACTIONS_GET.path, _body(recorded()))

    with reader_connection(enrolled) as conn:
        row = (
            conn.execute(
                select(sync_state).where(
                    sync_state.c.connection_id == CONNECTION_ID,
                    sync_state.c.domain == INVESTMENTS_DOMAIN,
                )
            )
            .mappings()
            .one()
        )
    assert row["last_success_at"] == RECEIVED
    assert row["last_error_code"] is None


# --------------------------------------------------------------------------
# Amounts, quantities and the two that round differently
# --------------------------------------------------------------------------


def test_a_purchase_is_signed_from_the_operators_point_of_view(enrolled: Config) -> None:
    """🔴 The aggregator debits cash as a POSITIVE amount; the operator lost it.

    Measured (§26): a `buy` arrives positive because cash left the account, and
    a `cash`/`contribution` arrives negative because cash entered it. Storing
    either at face value would be wrong by twice the amount, silently, with
    every sum completing.
    """
    payload = recorded()
    purchase = entry_named(payload, type="buy")
    contribution = entry_named(payload, type="cash", subtype="contribution")
    _apply(enrolled, INVESTMENTS_TRANSACTIONS_GET.path, _body(payload))

    bought = stored_row(enrolled, purchase["investment_transaction_id"])
    assert Decimal(purchase["amount"]) > 0, "the recorded buy is not the case this asserts"
    assert bought["amount_minor"] < 0, "a purchase left the account and must store negative"

    added = stored_row(enrolled, contribution["investment_transaction_id"])
    assert Decimal(contribution["amount"]) < 0, "the recorded contribution is not the case"
    assert added["amount_minor"] > 0, "a contribution entered the account and must store positive"


def test_a_quantity_keeps_every_digit_the_aggregator_sent(enrolled: Config) -> None:
    """🔴 A quantity is not money: exact text, never a float, never minor units.

    The recorded capture carries 17 significant digits (§26). A float would have
    dropped some before anyone could look, and a scaled integer would need a
    scale this product would have to guess.
    """
    payload = recorded()
    fine = max(
        payload["investment_transactions"],
        key=lambda entry: len(str(entry.get("quantity", ""))),
    )
    assert len(str(fine["quantity"])) > 10, "no recorded quantity is long enough to test this"
    _apply(enrolled, INVESTMENTS_TRANSACTIONS_GET.path, _body(payload))

    assert stored_row(enrolled, fine["investment_transaction_id"])["quantity"] == str(
        fine["quantity"]
    )


def test_a_sale_keeps_its_negative_quantity_unflipped(enrolled: Config) -> None:
    """A quantity is already the operator's point of view: shares left."""
    payload = recorded()
    sale = entry_named(payload, type="sell")
    assert Decimal(str(sale["quantity"])) < 0, "the recorded sell has no negative quantity"
    _apply(enrolled, INVESTMENTS_TRANSACTIONS_GET.path, _body(payload))

    assert stored_row(enrolled, sale["investment_transaction_id"])["quantity"] == str(
        sale["quantity"]
    )


def test_a_sub_cent_unit_price_is_rounded_half_even_rather_than_refused(
    enrolled: Config,
) -> None:
    """🔴 A unit price is a RATE, so it rounds; a ledger amount would be refused.

    12 of the 100 recorded prices carry more precision than the cent (§26), so
    this is a third of a real page rather than an edge case.
    """
    payload = recorded()
    priced = next(
        entry for entry in payload["investment_transactions"] if _finer_than_a_cent(entry["price"])
    )
    _apply(enrolled, INVESTMENTS_TRANSACTIONS_GET.path, _body(payload))

    expected = int(
        Decimal(str(priced["price"])).scaleb(2).to_integral_value(rounding="ROUND_HALF_EVEN")
    )
    assert stored_row(enrolled, priced["investment_transaction_id"])["price_minor"] == expected


def test_a_fee_is_stored_operator_signed_like_every_other_amount(enrolled: Config) -> None:
    """A fee is money leaving, so it stores negative even though the feed sends a magnitude."""
    payload = recorded()
    charged = next(
        entry
        for entry in payload["investment_transactions"]
        if entry.get("fees") is not None and Decimal(str(entry["fees"])) > 0
    )
    _apply(enrolled, INVESTMENTS_TRANSACTIONS_GET.path, _body(payload))

    assert stored_row(enrolled, charged["investment_transaction_id"])["fees_minor"] < 0


def test_trade_date_is_a_calendar_date_against_an_instant_capture(enrolled: Config) -> None:
    """A trade date is a calendar fact from the institution; `first_seen_at` is ours."""
    payload = recorded()
    entry = payload["investment_transactions"][0]
    _apply(enrolled, INVESTMENTS_TRANSACTIONS_GET.path, _body(payload))

    row = stored_row(enrolled, entry["investment_transaction_id"])
    assert row["trade_date"] == calendar_date(
        datetime.fromisoformat(entry["date"]).replace(tzinfo=UTC).date()
    )
    assert row["first_seen_at"] == RECEIVED


# --------------------------------------------------------------------------
# Refusals, at the scope each belongs to
# --------------------------------------------------------------------------


def test_a_cash_movement_with_no_security_is_stored_rather_than_refused(
    enrolled: Config,
) -> None:
    """A contribution of cash is not a trade in an instrument; the column is nullable."""
    payload = recorded()
    entry = dict(entry_named(payload, type="cash", subtype="contribution"))
    entry["security_id"] = None
    payload["investment_transactions"] = [entry]
    _apply(enrolled, INVESTMENTS_TRANSACTIONS_GET.path, _body(payload))

    assert stored_row(enrolled, entry["investment_transaction_id"])["security_id"] is None


def test_a_transaction_in_a_security_the_body_did_not_list_refuses_the_page(
    enrolled: Config,
) -> None:
    """The instrument a trade is in is not something this system can supply."""
    payload = recorded()
    entry = dict(payload["investment_transactions"][0])
    entry["security_id"] = "sec-nobody-listed"
    payload["investment_transactions"] = [entry]

    with pytest.raises(DerivationError, match="securities list"):
        _apply(enrolled, INVESTMENTS_TRANSACTIONS_GET.path, _body(payload))


def test_a_transaction_in_no_stated_currency_refuses_the_page(enrolled: Config) -> None:
    payload = recorded()
    entry = dict(payload["investment_transactions"][0])
    entry["iso_currency_code"] = None
    entry["unofficial_currency_code"] = None
    payload["investment_transactions"] = [entry]

    with pytest.raises(DerivationError, match="no stated currency"):
        _apply(enrolled, INVESTMENTS_TRANSACTIONS_GET.path, _body(payload))


def test_an_undenominable_row_costs_that_row_and_not_the_page(
    enrolled: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """🔴 One unknown unit must not take the rest of the window offline.

    Constructed rather than recorded, because every transaction the aggregator
    sent is USD (§26) -- and the construction is valid in every respect but the
    currency, so it reaches the refusal under test rather than an earlier guard.
    """
    payload = recorded()
    good, bad = (dict(e) for e in payload["investment_transactions"][:2])
    bad["iso_currency_code"] = None
    bad["unofficial_currency_code"] = "XTS"
    payload["investment_transactions"] = [good, bad]

    _apply(enrolled, INVESTMENTS_TRANSACTIONS_GET.path, _body(payload))

    stored = {
        row["source_investment_transaction_id"] for row in rows(enrolled, investment_transactions)
    }
    assert good["investment_transaction_id"] in stored
    assert bad["investment_transaction_id"] not in stored


def test_a_transaction_for_an_account_nothing_names_refuses_the_page(
    enrolled: Config,
) -> None:
    """A row with nowhere to hang refuses rather than being dropped.

    🔴 The body's own `accounts` array is derived first, so a transaction for an
    account missing from the ROSTER still lands -- that is the case Chunk 01's
    review forced, and it is why this test has to strip the account from the
    carried array too. Only then is the refusal reachable, and a test that
    skipped that step would pass while proving nothing.
    """
    payload = recorded()
    entry = dict(payload["investment_transactions"][0])
    entry["account_id"] = "acct-nobody-names"
    payload["investment_transactions"] = [entry]

    with pytest.raises(DerivationError, match="which this connection has no"):
        _apply(enrolled, INVESTMENTS_TRANSACTIONS_GET.path, _body(payload))


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


def test_applying_the_same_window_twice_changes_nothing(enrolled: Config) -> None:
    """AC-2.4, at the deriver: the second pass converges on the same rows."""
    payload = recorded()
    _apply(enrolled, INVESTMENTS_TRANSACTIONS_GET.path, _body(payload))
    first = rows(enrolled, investment_transactions)

    _apply(enrolled, INVESTMENTS_TRANSACTIONS_GET.path, _body(payload), received_at=LATER)
    second = rows(enrolled, investment_transactions)

    assert len(second) == len(first)
    assert {row["source_investment_transaction_id"] for row in second} == {
        row["source_investment_transaction_id"] for row in first
    }
    for row in second:
        # The identity is stable and the row was not duplicated under a second
        # local id; only the provenance of the latest capture moves.
        assert row["first_seen_at"] == RECEIVED
        assert row["updated_at"] == LATER


# --------------------------------------------------------------------------
# The window: what a COMPLETE one concludes, and what an incomplete one may not
# --------------------------------------------------------------------------


def _reconcile(
    config: Config,
    *,
    page_response_ids: list[int],
    rows_seen: int,
    stated_total: int | None,
) -> Any:
    with writer_connection(config) as conn, transaction(conn):
        return record_investment_transaction_window(
            conn,
            connection_id=CONNECTION_ID,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            page_response_ids=page_response_ids,
            rows_seen=rows_seen,
            stated_total=stated_total,
            at=LATER,
        )


def test_a_row_absent_from_a_complete_window_is_soft_deleted(enrolled: Config) -> None:
    """The only removal signal this feed has: absence from a window seen whole."""
    payload = recorded()
    first_id = _apply(enrolled, INVESTMENTS_TRANSACTIONS_GET.path, _body(payload))
    gone = payload["investment_transactions"][0]["investment_transaction_id"]

    # The window comes back again without that row, and complete.
    second = recorded()
    second["investment_transactions"] = second["investment_transactions"][1:]
    second["total_investment_transactions"] = len(second["investment_transactions"])
    second_id = _apply(
        enrolled, INVESTMENTS_TRANSACTIONS_GET.path, _body(second), received_at=LATER
    )
    assert second_id != first_id

    outcome = _reconcile(
        enrolled,
        page_response_ids=[second_id],
        rows_seen=len(second["investment_transactions"]),
        stated_total=len(second["investment_transactions"]),
    )

    assert outcome.exhausted is True
    assert outcome.removed == 1
    assert stored_row(enrolled, gone)["removed_at"] == LATER
    # 🔴 Soft, never hard: the row is still there to reconcile against.
    assert stored_row(enrolled, gone)["source_investment_transaction_id"] == gone


def test_an_incomplete_window_soft_deletes_nothing(enrolled: Config) -> None:
    """🔴 The guard that stands between a bounded run and bulk data loss.

    Asserted as the invariant rather than as one of its causes: whatever stopped
    the run -- page ceiling, transport failure, a kill -- what reaches here is a
    row count short of the stated total, and nothing may be concluded from it.
    """
    payload = recorded()
    page_id = _apply(enrolled, INVESTMENTS_TRANSACTIONS_GET.path, _body(payload))
    seen = len(payload["investment_transactions"])

    outcome = _reconcile(
        enrolled,
        page_response_ids=[page_id],
        rows_seen=seen,
        # The window holds far more than this one page returned, which is
        # exactly the shape the live capture had: 100 of 1169.
        stated_total=seen + 1,
    )

    assert outcome.exhausted is False
    assert outcome.removed == 0
    assert all(row["removed_at"] is None for row in rows(enrolled, investment_transactions))
    assert outcome.history_start_date is None


def test_a_window_with_no_stated_total_concludes_nothing(enrolled: Config) -> None:
    """An unmeasured window cannot be exhausted, so it removes nothing."""
    payload = recorded()
    page_id = _apply(enrolled, INVESTMENTS_TRANSACTIONS_GET.path, _body(payload))

    outcome = _reconcile(
        enrolled,
        page_response_ids=[page_id],
        rows_seen=len(payload["investment_transactions"]),
        stated_total=None,
    )

    assert outcome.exhausted is False
    assert all(row["removed_at"] is None for row in rows(enrolled, investment_transactions))


def test_a_window_claiming_completeness_with_no_pages_refuses(enrolled: Config) -> None:
    """🔴 `NOT IN ()` is true of every row, so this would retire the whole window."""
    _apply(enrolled, INVESTMENTS_TRANSACTIONS_GET.path, _body(recorded()))

    with pytest.raises(IncompleteWindowError, match="no archived pages"):
        _reconcile(enrolled, page_response_ids=[], rows_seen=3, stated_total=3)

    assert all(row["removed_at"] is None for row in rows(enrolled, investment_transactions))


def test_a_complete_window_records_the_range_it_returned(enrolled: Config) -> None:
    """AC-3.3: the range that ACTUALLY returned, computed from the rows (§26)."""
    payload = recorded()
    page_id = _apply(enrolled, INVESTMENTS_TRANSACTIONS_GET.path, _body(payload))
    total = len(payload["investment_transactions"])
    earliest = min(entry["date"] for entry in payload["investment_transactions"])

    outcome = _reconcile(enrolled, page_response_ids=[page_id], rows_seen=total, stated_total=total)

    expected = calendar_date(datetime.fromisoformat(earliest).replace(tzinfo=UTC).date())
    assert outcome.history_start_date == expected
    with reader_connection(enrolled) as conn:
        recorded_start = conn.execute(
            select(sync_state.c.history_start_date).where(
                sync_state.c.connection_id == CONNECTION_ID,
                sync_state.c.domain == INVESTMENTS_DOMAIN,
            )
        ).scalar_one()
    assert recorded_start == expected


def test_a_soft_deleted_row_does_not_set_the_recorded_range(enrolled: Config) -> None:
    """The range is measured after the removals, or a retired row would set it."""
    payload = recorded()
    entries = sorted(payload["investment_transactions"], key=lambda entry: entry["date"])
    oldest, rest = entries[0], entries[1:]
    payload["investment_transactions"] = entries
    _apply(enrolled, INVESTMENTS_TRANSACTIONS_GET.path, _body(payload))

    without_oldest = recorded()
    without_oldest["investment_transactions"] = rest
    page_id = _apply(
        enrolled,
        INVESTMENTS_TRANSACTIONS_GET.path,
        _body(without_oldest),
        received_at=LATER,
    )

    outcome = _reconcile(
        enrolled, page_response_ids=[page_id], rows_seen=len(rest), stated_total=len(rest)
    )

    assert outcome.removed == 1
    assert stored_row(enrolled, oldest["investment_transaction_id"])["removed_at"] == LATER
    assert outcome.history_start_date == calendar_date(
        datetime.fromisoformat(min(e["date"] for e in rest)).replace(tzinfo=UTC).date()
    )


def test_a_row_that_comes_back_has_its_removal_retired(enrolled: Config) -> None:
    """A window disagreeing with a removal is the evidence that retires it."""
    payload = recorded()
    one = payload["investment_transactions"][0]
    shortened = recorded()
    shortened["investment_transactions"] = shortened["investment_transactions"][1:]

    _apply(enrolled, INVESTMENTS_TRANSACTIONS_GET.path, _body(payload))
    absent_id = _apply(
        enrolled, INVESTMENTS_TRANSACTIONS_GET.path, _body(shortened), received_at=LATER
    )
    _reconcile(
        enrolled,
        page_response_ids=[absent_id],
        rows_seen=len(shortened["investment_transactions"]),
        stated_total=len(shortened["investment_transactions"]),
    )
    assert stored_row(enrolled, one["investment_transaction_id"])["removed_at"] is not None

    _apply(enrolled, INVESTMENTS_TRANSACTIONS_GET.path, _body(payload), received_at=LATER)

    assert stored_row(enrolled, one["investment_transaction_id"])["removed_at"] is None


def test_a_manually_imported_transaction_is_never_retired_by_an_aggregator_window(
    enrolled: Config,
) -> None:
    """🔴 Authorship of the ROW is the test: the aggregator never had this one.

    A manual row is absent from every window because it was never the
    aggregator's to send, so its absence is not evidence of anything.
    """
    page_id = _apply(enrolled, INVESTMENTS_TRANSACTIONS_GET.path, _body(recorded()))
    account_id = rows(enrolled, accounts)[0]["account_id"]
    with writer_connection(enrolled) as conn:
        import_key = conn.execute(
            insert(manual_imports).values(
                account_id=account_id,
                adapter="csv",
                source_name="a brokerage CSV",
                file_sha256="0" * 64,
                file_bytes=1024,
                imported_at=RECEIVED,
                rows_seen=1,
                rows_applied=1,
            )
        ).inserted_primary_key
        assert import_key is not None
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
                first_seen_at=RECEIVED,
                updated_at=RECEIVED,
            )
        )

    total = len(recorded()["investment_transactions"])
    _reconcile(enrolled, page_response_ids=[page_id], rows_seen=total, stated_total=total)

    with reader_connection(enrolled) as conn:
        manual = (
            conn.execute(
                select(investment_transactions).where(
                    investment_transactions.c.import_fingerprint == "csv-row-1"
                )
            )
            .mappings()
            .one()
        )
    assert manual["removed_at"] is None


def test_a_row_outside_the_window_is_never_retired_by_it(enrolled: Config) -> None:
    """A window is evidence only about the range it asked for."""
    page_id = _apply(enrolled, INVESTMENTS_TRANSACTIONS_GET.path, _body(recorded()))
    account_id = rows(enrolled, accounts)[0]["account_id"]
    with writer_connection(enrolled) as conn:
        version_id = (
            conn.execute(select(investment_transactions.c.derivation_version_id)).scalars().first()
        )
        older_response = (
            conn.execute(select(investment_transactions.c.raw_response_id)).scalars().first()
        )
        conn.execute(
            insert(investment_transactions).values(
                account_id=account_id,
                source_investment_transaction_id="older-than-the-window",
                trade_date=calendar_date(WINDOW_START - timedelta(days=1)),
                investment_type="buy",
                amount_minor=-1000,
                currency="USD",
                source="aggregator",
                raw_response_id=older_response,
                derivation_version_id=version_id,
                first_seen_at=RECEIVED,
                updated_at=RECEIVED,
            )
        )

    total = len(recorded()["investment_transactions"])
    _reconcile(enrolled, page_response_ids=[page_id], rows_seen=total, stated_total=total)

    assert stored_row(enrolled, "older-than-the-window")["removed_at"] is None


def test_an_exhausted_but_empty_window_leaves_a_measured_range_alone(
    enrolled: Config,
) -> None:
    """🔴 Null means nobody measured, so an empty window must not write one.

    Overwriting a range an earlier run did measure would destroy a fact in order
    to record the absence of one -- and a quiet brokerage is not a connection
    whose history shrank.
    """
    payload = recorded()
    page_id = _apply(enrolled, INVESTMENTS_TRANSACTIONS_GET.path, _body(payload))
    total = len(payload["investment_transactions"])
    first = _reconcile(enrolled, page_response_ids=[page_id], rows_seen=total, stated_total=total)
    assert first.history_start_date is not None

    empty = recorded()
    empty["investment_transactions"] = []
    empty_id = _apply(enrolled, INVESTMENTS_TRANSACTIONS_GET.path, _body(empty), received_at=LATER)
    outcome = _reconcile(enrolled, page_response_ids=[empty_id], rows_seen=0, stated_total=0)

    assert outcome.exhausted is True
    assert outcome.history_start_date is None
    with reader_connection(enrolled) as conn:
        still = conn.execute(
            select(sync_state.c.history_start_date).where(
                sync_state.c.connection_id == CONNECTION_ID,
                sync_state.c.domain == INVESTMENTS_DOMAIN,
            )
        ).scalar_one()
    assert still == first.history_start_date
