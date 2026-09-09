"""Transactions: added, modified, removed, and the pending that becomes posted.

FR-2's acceptance criteria at the row level. The bodies here follow the live
shape recorded in `api-notes-plaid.md` §17 — including the one that decides the
largest correctness surface in this file: **a merchant purchase arrives with a
POSITIVE amount**, and this product stores money leaving an account as negative.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import select

from bankmachine.config import Config
from bankmachine.connector import ACCOUNTS_GET, TRANSACTIONS_SYNC
from bankmachine.derivers import ALL_DERIVERS
from bankmachine.store.derivation import DerivationError, apply_response
from bankmachine.store.engine import reader_connection, writer_connection
from bankmachine.store.schema import connections, institutions, transactions
from bankmachine.store.types import now_utc

SOURCE_ACCOUNT = "acct-checking"
CONNECTION_ID = 1


def _accounts_body() -> bytes:
    return json.dumps(
        {
            "accounts": [
                {
                    "account_id": SOURCE_ACCOUNT,
                    "name": "Plaid Checking",
                    "official_name": "Plaid Gold Checking",
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
            "item": {"item_id": "item-x"},
            "request_id": "req-accounts",
        }
    ).encode()


def _txn(
    *,
    transaction_id: str,
    amount: str,
    pending: bool = False,
    pending_transaction_id: str | None = None,
    date: str = "2026-09-07",
    authorized_date: str | None = "2026-09-06",
    name: str = "SparkFun",
    merchant_name: str | None = "FUN",
) -> dict[str, Any]:
    return {
        "account_id": SOURCE_ACCOUNT,
        "transaction_id": transaction_id,
        "amount": amount,
        "iso_currency_code": "USD",
        "date": date,
        "authorized_date": authorized_date,
        "pending": pending,
        "pending_transaction_id": pending_transaction_id,
        "name": name,
        "merchant_name": merchant_name,
        "personal_finance_category": {
            "primary": "GENERAL_MERCHANDISE",
            "detailed": "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES",
            "confidence_level": "LOW",
        },
    }


def _sync_body(
    *,
    added: list[dict[str, Any]] | None = None,
    modified: list[dict[str, Any]] | None = None,
    removed: list[dict[str, Any]] | None = None,
    next_cursor: str = "cursor-1",
) -> bytes:
    return json.dumps(
        {
            "accounts": [],
            "added": added or [],
            "modified": modified or [],
            "removed": removed or [],
            "next_cursor": next_cursor,
            "has_more": False,
            "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE",
            "request_id": "req-sync",
        }
    ).encode()


@pytest.fixture
def synced(initialized_config: Config) -> Config:
    """A connection with one derived account, which transactions hang from."""
    now = now_utc()
    with writer_connection(initialized_config) as conn:
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
                source_connection_id="item-x",
                credential_ref="connection:sandbox:item-x",
                capabilities="[]",
                requested_history_days=730,
                status="active",
                enrolled_at=now,
                created_at=now,
                updated_at=now,
            )
        )
    _apply(initialized_config, ACCOUNTS_GET.path, _accounts_body())
    return initialized_config


def _apply(config: Config, endpoint: str, body: bytes) -> None:
    with writer_connection(config) as conn:
        apply_response(
            conn,
            connection_id=CONNECTION_ID,
            endpoint=endpoint,
            body=body,
            received_at=now_utc(),
            derivers=ALL_DERIVERS,
        )


def _rows(config: Config) -> list[Any]:
    with reader_connection(config) as conn:
        return [
            row._mapping
            for row in conn.execute(
                select(transactions).order_by(transactions.c.transaction_id)
            ).all()
        ]


# --------------------------------------------------------------------------
# The sign convention — the largest correctness surface here
# --------------------------------------------------------------------------


def test_a_purchase_reported_positive_is_stored_negative(synced: Config) -> None:
    """🔴 Wrong here is wrong by TWICE the amount, silently.

    *Measured* (§17): a merchant purchase on a depository account arrives as
    `amount: 89.4` — money leaving. `data-model.md` § Direction stores money
    moving *into* an account as positive, so this is negative. Taking the
    aggregator's sign at face value produces well-formed numbers whose every sum
    completes and whose every total is wrong.
    """
    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(added=[_txn(transaction_id="t1", amount="89.40")]),
    )

    assert _rows(synced)[0]["amount_minor"] == -8940


def test_a_deposit_reported_negative_is_stored_positive(synced: Config) -> None:
    """The mirror half, which is what makes the negation a convention rather than a sign flip.

    A refund or deposit arrives negative from the aggregator — money arriving —
    and is stored positive. Without this assertion the rule above would be
    satisfied by code that simply made every amount negative.
    """
    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(added=[_txn(transaction_id="t1", amount="-250.00")]),
    )

    assert _rows(synced)[0]["amount_minor"] == 25000


def test_an_amount_a_float_would_have_mangled_survives_exactly(synced: Config) -> None:
    """A ledger amount is converted exactly or refused; it is never rounded.

    `to_minor` rounds and exists for investment valuations, which are price times
    quantity. A transaction is money that moved, and a fraction of a cent lost on
    every row reconciles to nothing.
    """
    _apply(
        synced, TRANSACTIONS_SYNC.path, _sync_body(added=[_txn(transaction_id="t1", amount="8.87")])
    )

    assert _rows(synced)[0]["amount_minor"] == -887


def test_an_amount_with_sub_cent_precision_is_refused_not_rounded(synced: Config) -> None:
    """The refusal is the point: a ledger amount this system cannot represent exactly
    must not be recorded as though it could."""
    with pytest.raises(DerivationError):
        _apply(
            synced,
            TRANSACTIONS_SYNC.path,
            _sync_body(added=[_txn(transaction_id="t1", amount="10.005")]),
        )


# --------------------------------------------------------------------------
# AC-2.2 — added, modified, removed
# --------------------------------------------------------------------------


def test_added_transactions_are_inserted_with_their_source_fields(synced: Config) -> None:
    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(added=[_txn(transaction_id="t1", amount="12.00")]),
    )

    row = _rows(synced)[0]
    assert row["source_transaction_id"] == "t1"
    assert row["description"] == "SparkFun"
    assert row["merchant_name"] == "FUN"
    assert row["source_category_primary"] == "GENERAL_MERCHANDISE"
    assert row["source_category_detailed"] == "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES"
    assert row["source"] == "aggregator"
    assert row["raw_response_id"] is not None
    assert row["derivation_version_id"] is not None
    assert row["removed_at"] is None


def test_a_modified_transaction_updates_in_place(synced: Config) -> None:
    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(added=[_txn(transaction_id="t1", amount="12.00")]),
    )
    first_id = _rows(synced)[0]["transaction_id"]

    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(modified=[_txn(transaction_id="t1", amount="14.50", name="SparkFun Corrected")]),
    )

    rows = _rows(synced)
    assert len(rows) == 1, "a modification must not duplicate the row"
    assert rows[0]["transaction_id"] == first_id, "the local id is what other rows point at"
    assert rows[0]["amount_minor"] == -1450
    assert rows[0]["description"] == "SparkFun Corrected"


def test_a_removed_transaction_is_soft_deleted(synced: Config) -> None:
    """🔴 AC-2.2: retained with a removal timestamp, never hard-deleted.

    A transaction that vanished is a fact about the source. A row that vanished
    with it leaves nothing to reconcile against — and no re-sync can bring it back
    to say what was there.
    """
    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(added=[_txn(transaction_id="t1", amount="12.00")]),
    )

    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(removed=[{"transaction_id": "t1", "account_id": SOURCE_ACCOUNT}]),
    )

    rows = _rows(synced)
    assert len(rows) == 1, "the row was deleted rather than marked removed"
    assert rows[0]["removed_at"] is not None


def test_a_transaction_removed_then_sent_again_is_present_again(synced: Config) -> None:
    """A stale removal stamp would keep a live row out of every sum.

    The row would say it exists while every query that respects `removed_at`
    ignored it — the disagreement being invisible because both halves are
    individually well-formed.
    """
    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(added=[_txn(transaction_id="t1", amount="12.00")]),
    )
    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(removed=[{"transaction_id": "t1", "account_id": SOURCE_ACCOUNT}]),
    )

    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(added=[_txn(transaction_id="t1", amount="12.00")]),
    )

    assert _rows(synced)[0]["removed_at"] is None


# --------------------------------------------------------------------------
# AC-2.3 — pending becomes posted
# --------------------------------------------------------------------------


def test_a_posting_transaction_updates_the_pending_row(synced: Config) -> None:
    """🔴 AC-2.3, and the shape that makes it easy to get wrong.

    The posting transaction arrives with a NEW `transaction_id` and the pending
    row's id in `pending_transaction_id`. Matching on the transaction id alone
    would insert, leaving the same purchase in the ledger twice — and both rows
    would look correct.
    """
    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(added=[_txn(transaction_id="pending-1", amount="89.40", pending=True)]),
    )
    pending_row_id = _rows(synced)[0]["transaction_id"]

    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(
            added=[
                _txn(
                    transaction_id="posted-1",
                    amount="89.40",
                    pending=False,
                    pending_transaction_id="pending-1",
                )
            ]
        ),
    )

    rows = _rows(synced)
    assert len(rows) == 1, "the pending row was duplicated by its own posting"
    assert rows[0]["transaction_id"] == pending_row_id, "the local id must survive the transition"
    assert rows[0]["source_transaction_id"] == "posted-1"
    assert rows[0]["pending"] == 0
    assert rows[0]["source_pending_transaction_id"] == "pending-1"


def test_replaying_a_posting_transaction_does_not_insert_a_second_row(synced: Config) -> None:
    """The replay case the pending match makes possible.

    Once applied, the row answers to the POSTED id. A second delivery of the same
    page must find it there rather than matching the pending id again and
    inserting beside it — which is what a rebuild does by construction.
    """
    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(added=[_txn(transaction_id="pending-1", amount="89.40", pending=True)]),
    )
    posting = _sync_body(
        added=[
            _txn(
                transaction_id="posted-1",
                amount="89.40",
                pending_transaction_id="pending-1",
            )
        ]
    )
    _apply(synced, TRANSACTIONS_SYNC.path, posting)

    _apply(synced, TRANSACTIONS_SYNC.path, posting)

    assert len(_rows(synced)) == 1


def test_a_settlement_that_changes_the_amount_updates_it_in_place(synced: Config) -> None:
    """🔴 AC-13.2 — the ORDINARY settlement, and the one nothing here reached.

    Both cases above send `89.40` on the hold and `89.40` on the posting, so they
    would pass over an UPDATE that copied every column except the amount. A tip,
    a fuel hold and a hotel incidental all settle at a different figure from the
    one they were authorised for, which makes the changing amount the common case
    rather than the exotic one.

    🔴 The hold's figure is NOT retained as a second amount. There is one row and
    one amount, and it is the settled one: keeping the authorised figure beside
    it would give every consumer two numbers and no rule for which is money.
    """
    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(added=[_txn(transaction_id="pending-1", amount="89.40", pending=True)]),
    )
    pending_row_id = _rows(synced)[0]["transaction_id"]
    assert _rows(synced)[0]["amount_minor"] == -8940

    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(
            added=[
                _txn(
                    transaction_id="posted-1",
                    amount="103.20",
                    pending=False,
                    pending_transaction_id="pending-1",
                )
            ],
            next_cursor="cursor-2",
        ),
    )

    rows = _rows(synced)
    assert len(rows) == 1, "a settlement at a new amount inserted a second row"
    assert rows[0]["transaction_id"] == pending_row_id, "the local id must survive the transition"
    assert rows[0]["amount_minor"] == -10320, (
        "the settled amount must replace the hold's; a row still holding -8940 means the "
        "UPDATE carried the identity across and left the figure behind"
    )
    assert rows[0]["pending"] == 0


def test_a_hold_removed_in_the_same_page_as_its_posting_leaves_one_row(synced: Config) -> None:
    """🔴 AC-13.3 — the hold and its own posting arrive together.

    The aggregator may report the settlement as an `added` posting and the hold's
    disappearance as a `removed` entry in the SAME page. Applied in the wrong
    order that is a row that posts and is then soft-deleted, so the purchase
    vanishes from every total — an undercount, which is the direction that gets
    believed.
    """
    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(added=[_txn(transaction_id="pending-1", amount="89.40", pending=True)]),
    )

    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(
            added=[
                _txn(
                    transaction_id="posted-1",
                    amount="103.20",
                    pending_transaction_id="pending-1",
                )
            ],
            removed=[{"account_id": SOURCE_ACCOUNT, "transaction_id": "pending-1"}],
            next_cursor="cursor-2",
        ),
    )

    live = [row for row in _rows(synced) if row["removed_at"] is None]
    assert len(live) == 1, (
        "the hold's removal took its own settlement with it: the row that posted was "
        "soft-deleted, so the purchase left every total"
    )
    assert live[0]["source_transaction_id"] == "posted-1"
    assert live[0]["amount_minor"] == -10320


@pytest.mark.parametrize("removal_first", [True, False])
def test_a_hold_and_its_posting_resolve_to_one_row_whichever_page_lands_first(
    synced: Config, removal_first: bool
) -> None:
    """🔴 AC-13.3 across PAGES, both orders.

    The three change lists are applied added → modified → removed within a page,
    so page boundaries are the only place their relative order can vary — and it
    varies in both directions: a hold's removal can be paged before its posting
    or after it. Either way exactly one non-removed row must survive, carrying
    the settled amount.
    """
    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(added=[_txn(transaction_id="pending-1", amount="89.40", pending=True)]),
    )
    removal = _sync_body(
        removed=[{"account_id": SOURCE_ACCOUNT, "transaction_id": "pending-1"}],
        next_cursor="cursor-removal",
    )
    posting = _sync_body(
        added=[
            _txn(transaction_id="posted-1", amount="103.20", pending_transaction_id="pending-1")
        ],
        next_cursor="cursor-posting",
    )

    for body in (removal, posting) if removal_first else (posting, removal):
        _apply(synced, TRANSACTIONS_SYNC.path, body)

    live = [row for row in _rows(synced) if row["removed_at"] is None]
    assert len(live) == 1, f"removal_first={removal_first} left {len(live)} live rows, not one"
    assert live[0]["source_transaction_id"] == "posted-1"
    assert live[0]["amount_minor"] == -10320
    assert live[0]["pending"] == 0


def test_a_hold_that_expires_without_posting_is_retained_as_a_removed_pending_row(
    synced: Config,
) -> None:
    """🔴 AC-13.4's datastore half — the disappearance that is not a duplication.

    A hold can simply expire. The row is soft-deleted like any withdrawal
    (AC-2.2, unchanged), and what makes it ATTRIBUTABLE later is that it is still
    marked `pending`: a hold that settled had its own row updated, so it leaves
    as `pending = 0`. The read path tells the two apart on exactly that, and it
    is the only thing distinguishing "the number fell because a hold went away"
    from "the number fell because rows are missing".
    """
    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(added=[_txn(transaction_id="pending-1", amount="89.40", pending=True)]),
    )

    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(
            removed=[{"account_id": SOURCE_ACCOUNT, "transaction_id": "pending-1"}],
            next_cursor="cursor-2",
        ),
    )

    rows = _rows(synced)
    assert len(rows) == 1, "the expired hold was hard-deleted"
    assert rows[0]["removed_at"] is not None
    assert rows[0]["pending"] == 1, (
        "an expired hold must stay marked pending; flipped to 0 it is indistinguishable "
        "from a settled row that was later withdrawn"
    )


# --------------------------------------------------------------------------
# AC-2.4 — idempotency
# --------------------------------------------------------------------------


def test_a_second_identical_page_produces_zero_net_changes(synced: Config) -> None:
    """AC-2.4, compared as a full digest rather than a row count.

    Counting rows would miss a page that rewrote every column while adding none,
    which is the failure a re-run is most likely to produce.
    """
    page = _sync_body(
        added=[
            _txn(transaction_id="t1", amount="12.00"),
            _txn(transaction_id="t2", amount="-99.99", name="Payroll", merchant_name=None),
        ]
    )
    _apply(synced, TRANSACTIONS_SYNC.path, page)
    before = [dict(row) for row in _rows(synced)]

    _apply(synced, TRANSACTIONS_SYNC.path, page)
    after = [dict(row) for row in _rows(synced)]

    # 🔴 Two columns legitimately move and the rest must not. `updated_at` and
    # `raw_response_id` record WHICH response last asserted the row, and a replay
    # is a later response asserting the same thing -- the accounts deriver treats
    # provenance the same way. What AC-2.4 forbids is a second run changing what
    # the row SAYS, or adding one, and that is what this compares.
    for row in before + after:
        row.pop("updated_at")
        row.pop("raw_response_id")
    assert before == after
    assert len(after) == 2, "a re-run inserted rows it should have converged onto"


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def test_a_transaction_for_an_unknown_account_is_refused(synced: Config) -> None:
    """🔴 Refused rather than skipped, because the cursor advances on success.

    A dropped row would never be offered again: the cursor moves past it and the
    aggregator has no reason to resend it.
    """
    entry = _txn(transaction_id="t1", amount="12.00")
    entry["account_id"] = "acct-never-derived"

    with pytest.raises(DerivationError) as raised:
        _apply(synced, TRANSACTIONS_SYNC.path, _sync_body(added=[entry]))

    assert "no row for" in str(raised.value)


def test_a_malformed_change_list_is_refused_not_read_as_empty(synced: Config) -> None:
    """A page with a broken list would otherwise look like a page with no changes.

    And a sync that applied nothing would still advance its cursor past the
    transactions it skipped.
    """
    body = json.loads(_sync_body().decode())
    body["added"] = "not-a-list"

    with pytest.raises(DerivationError) as raised:
        _apply(synced, TRANSACTIONS_SYNC.path, json.dumps(body).encode())

    assert "not a list" in str(raised.value)


def test_dates_are_calendar_dates_not_instants(synced: Config) -> None:
    """The off-by-one this prevents lands at period boundaries.

    Which is exactly where period-over-period comparison lives — the product's
    headline query.
    """
    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(added=[_txn(transaction_id="t1", amount="12.00", date="2026-01-01")]),
    )

    row = _rows(synced)[0]
    assert str(row["posted_date"]) == "2026-01-01"
    assert str(row["authorized_date"]) == "2026-09-06"


def test_a_whole_dollar_amount_arrives_as_an_integer_and_is_still_exact(
    synced: Config,
) -> None:
    """🔴 The case every fixture in this file used to miss.

    `parse_float=str` keeps decimals as text but leaves JSON *integers* as `int`,
    so a whole-dollar transaction arrives as `500`, not `"500.00"`. Every fixture
    here used a fractional amount, so the whole suite passed while the first real
    sandbox sync refused every whole-dollar transaction it fetched. An int is
    exact, so converting it loses nothing — what was missing was noticing it
    could be one.
    """
    entry = _txn(transaction_id="t1", amount="0")
    entry["amount"] = 500  # as `json.loads(..., parse_float=str)` yields it

    _apply(synced, TRANSACTIONS_SYNC.path, _sync_body(added=[entry]))

    assert _rows(synced)[0]["amount_minor"] == -50000


def test_a_boolean_amount_is_refused_rather_than_read_as_a_number(synced: Config) -> None:
    """`bool` is an `int` in Python, and `str(True)` is not a number.

    Without the exclusion this would become the string "True" and fail somewhere
    less obvious than here.
    """
    entry = _txn(transaction_id="t1", amount="0")
    entry["amount"] = True

    with pytest.raises(DerivationError):
        _apply(synced, TRANSACTIONS_SYNC.path, _sync_body(added=[entry]))
