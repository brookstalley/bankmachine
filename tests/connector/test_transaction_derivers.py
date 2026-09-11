"""Transactions: added, modified, removed, and the pending that becomes posted.

FR-2's acceptance criteria at the row level. The bodies here follow the live
shape recorded in `api-notes-plaid.md` §17 — including the one that decides the
largest correctness surface in this file: **a merchant purchase arrives with a
POSITIVE amount**, and this product stores money leaving an account as negative.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, insert, select, update

from bankmachine.config import Config
from bankmachine.connector import ACCOUNTS_GET, TRANSACTIONS_SYNC
from bankmachine.derivers import ALL_DERIVERS
from bankmachine.store import types as store_types
from bankmachine.store.derivation import DerivationError, apply_response
from bankmachine.store.engine import reader_connection, writer_connection
from bankmachine.store.lineage import counts_once, only_superseded, superseded_spans
from bankmachine.store.schema import (
    TRANSACTIONS_DOMAIN,
    accounts,
    connections,
    institutions,
    sync_state,
    transactions,
)
from bankmachine.store.types import now_utc

SOURCE_ACCOUNT = "acct-checking"
CONNECTION_ID = 1


def _account_entry(
    account_id: str = SOURCE_ACCOUNT,
    *,
    name: str = "Plaid Checking",
    persistent_account_id: str | None = None,
    mask: str | None = "0000",
    subtype: str = "checking",
) -> Any:
    """One account in the shape both `/accounts/get` and `/transactions/sync` send it.

    `mask` and `subtype` are parameters because the lineage rule partitions on
    them: they are two of the four fields that decide whether two account rows
    describe the same real account. A fixture that could not vary them could only
    exercise the case where they happen to agree.
    """
    return {
        "account_id": account_id,
        **(
            {}
            if persistent_account_id is None
            else {"persistent_account_id": persistent_account_id}
        ),
        "name": name,
        "official_name": "Plaid Gold Checking",
        "mask": mask,
        "type": "depository",
        "subtype": subtype,
        "balances": {
            "current": "110.94",
            "available": "100.00",
            "limit": None,
            "iso_currency_code": "USD",
        },
    }


def _accounts_body(entries: list[Any] | None = None) -> bytes:
    return json.dumps(
        {
            "accounts": entries if entries is not None else [_account_entry()],
            "item": {"item_id": "item-x"},
            "request_id": "req-accounts",
        }
    ).encode()


def _txn(
    *,
    account_id: str = SOURCE_ACCOUNT,
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
        "account_id": account_id,
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
    accounts_listed: list[Any] | None = None,
    added: list[dict[str, Any]] | None = None,
    modified: list[dict[str, Any]] | None = None,
    removed: list[dict[str, Any]] | None = None,
    next_cursor: str = "cursor-1",
) -> bytes:
    return json.dumps(
        {
            "accounts": accounts_listed or [],
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


def _apply(
    config: Config,
    endpoint: str,
    body: bytes,
    *,
    connection_id: int = CONNECTION_ID,
    received_at: datetime | None = None,
) -> None:
    """Record one response, optionally as of a stated instant.

    `received_at` is a parameter because the roster observation date is derived
    from it, and "this account is no longer listed" is expressed as an account's
    last-seen date falling BEHIND its connection's latest observation. A test
    that could only record everything at one instant could not build that state
    at all -- every account would look current.
    """
    with writer_connection(config) as conn:
        apply_response(
            conn,
            connection_id=connection_id,
            endpoint=endpoint,
            body=body,
            received_at=received_at if received_at is not None else now_utc(),
            derivers=ALL_DERIVERS,
        )


def _connection_row(config: Config) -> Any:
    with reader_connection(config) as conn:
        return conn.execute(select(connections)).one()._mapping


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


def test_a_modification_naming_a_hold_that_has_already_posted_inserts_nothing(
    synced: Config,
) -> None:
    """🔴 The same purchase counted twice, with nothing on either row to say so.

    Once the posting has been applied, the merged row answers to the POSTED id --
    its `source_transaction_id` was overwritten by the transition. A later
    `modified` entry naming the hold's id therefore finds nothing under its own
    identity, and it carries no `pending_transaction_id` of its own for the
    second lookup to use, so the hold is inserted a second time as a live row.
    The purchase is then in the ledger twice, disclosed as an ordinary pending
    row rather than as a duplicate.

    `source_pending_transaction_id` is the column that still holds the hold's id
    on the merged row, and it is what the third lookup reads.
    """
    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(added=[_txn(transaction_id="pend-1", amount="2000.00", pending=True)]),
    )
    merged_row_id = _rows(synced)[0]["transaction_id"]
    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(
            added=[
                _txn(
                    transaction_id="post-1",
                    amount="2145.00",
                    pending_transaction_id="pend-1",
                )
            ]
        ),
    )

    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(
            modified=[_txn(transaction_id="pend-1", amount="2000.00", pending=True)],
        ),
    )

    rows = _rows(synced)
    live = [row for row in rows if row["removed_at"] is None]
    assert len(live) == 1, "one purchase, two live rows"
    assert live[0]["transaction_id"] == merged_row_id
    assert live[0]["source_pending_transaction_id"] == "pend-1"
    # 🔴 The posting is not undone by a change to the hold it absorbed. A row
    # sent back to `pending` under the hold's identity would also be the row a
    # later `removed: pend-1` soft-deleted -- and the whole purchase would leave
    # the ledger.
    assert live[0]["source_transaction_id"] == "post-1"
    assert live[0]["pending"] == 0
    assert live[0]["amount_minor"] == -214500


def test_the_hold_being_retired_after_that_does_not_take_the_purchase_with_it(
    synced: Config,
) -> None:
    """The step after the merge, which is where getting the identity wrong shows.

    The aggregator retires a hold once its posting has settled. If a modification
    of that hold had been allowed to move the merged row back under the hold's
    id, this removal would find it and soft-delete the purchase itself -- the
    $2,145 leaves every total, and the row that says why is stamped `removed`.
    """
    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(added=[_txn(transaction_id="pend-1", amount="2000.00", pending=True)]),
    )
    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(
            added=[
                _txn(
                    transaction_id="post-1",
                    amount="2145.00",
                    pending_transaction_id="pend-1",
                )
            ]
        ),
    )
    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(modified=[_txn(transaction_id="pend-1", amount="2000.00", pending=True)]),
    )

    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(removed=[{"transaction_id": "pend-1", "account_id": SOURCE_ACCOUNT}]),
    )

    live = [row for row in _rows(synced) if row["removed_at"] is None]
    assert len(live) == 1, "retiring the hold removed the purchase it had become"
    assert live[0]["source_transaction_id"] == "post-1"


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


def test_a_page_carrying_changes_but_no_cursor_applies_them_and_says_so(
    synced: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """🔴 The rows go in first; only the cursor write is skipped.

    The empty-cursor guard is right -- a `NOT_READY` reply carries one, and
    storing it would mean "start from the beginning" -- but it stood BEFORE the
    change lists were applied, so any page carrying entries and no cursor had its
    rows discarded with no error and no log line. If such a page also said
    `has_more`, the loop re-fetched the identical page to its ceiling and then
    reported that it had stopped short: an operator reading "run again to
    continue" from a run that never can.

    The rows are written, the cursor stays where it was, and the combination is
    recorded, because a page shaped this way is one nothing has ever observed.
    """
    _apply(synced, TRANSACTIONS_SYNC.path, _sync_body(next_cursor="cursor-1"))

    with caplog.at_level(logging.WARNING, logger="bankmachine"):
        _apply(
            synced,
            TRANSACTIONS_SYNC.path,
            _sync_body(added=[_txn(transaction_id="t1", amount="12.00")], next_cursor=""),
        )

    assert [row["source_transaction_id"] for row in _rows(synced)] == ["t1"]
    with reader_connection(synced) as conn:
        cursor = conn.execute(
            select(sync_state.c.cursor).where(sync_state.c.domain == TRANSACTIONS_DOMAIN)
        ).scalar_one()
    assert cursor == "cursor-1", "a page with no cursor moved the cursor"
    assert any("no cursor" in record.getMessage() for record in caplog.records), (
        "a page carrying changes and no cursor left no trace"
    )


def test_a_transaction_in_a_currency_with_no_known_exponent_costs_only_its_own_row(
    synced: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """🔴 The row is refused; the PAGE still applies, and so does the row beside it.

    The aggregator sets `iso_currency_code: null` and populates
    `unofficial_currency_code` for cryptocurrencies and other non-ISO
    instruments, and this build knows no minor unit for those codes -- so an
    amount in one cannot be converted exactly. It is not converted
    approximately: a ledger amount is exact or refused, and `12.00` in a
    currency whose real scale might be eight digits is not the same number as
    `12.00` in one with two.

    🔴 The refusal is scoped to the ROW. Letting it escape would abort the page,
    leave the cursor where it was, and re-fetch and re-refuse the identical body
    on every run afterwards -- so one unrepresentable transaction would take the
    institution's whole history offline. The raw response keeps the refused row,
    so a build that knows the currency's minor unit derives it on the next
    rebuild and nothing is lost.
    """
    unofficial = _txn(transaction_id="t1", amount="12.00")
    unofficial["iso_currency_code"] = None
    unofficial["unofficial_currency_code"] = "BTC"
    ordinary = _txn(transaction_id="t2", amount="5.00")

    with caplog.at_level(logging.WARNING, logger="bankmachine"):
        _apply(synced, TRANSACTIONS_SYNC.path, _sync_body(added=[unofficial, ordinary]))

    derived = {row["source_transaction_id"]: row for row in _rows(synced)}
    assert set(derived) == {"t2"}, "an unrepresentable amount was stored, or cost the page"
    assert derived["t2"]["amount_minor"] == -500
    assert any("cannot express in minor units" in r.getMessage() for r in caplog.records), (
        "a row was dropped with nothing recording that it happened"
    )


def test_the_same_transaction_derives_exactly_once_the_currency_has_an_exponent(
    synced: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The remedy, asserted: a configured exponent is not a guess.

    What is refused is an unknown scale, not an unofficial currency -- and the
    code is stored as the aggregator sent it, so it groups separately in every
    total rather than being folded in with the ISO ones.
    """
    monkeypatch.setitem(store_types._MINOR_DIGITS, "BTC", 8)
    entry = _txn(transaction_id="t1", amount="0.04217")
    entry["iso_currency_code"] = None
    entry["unofficial_currency_code"] = "BTC"

    _apply(synced, TRANSACTIONS_SYNC.path, _sync_body(added=[entry]))

    row = _rows(synced)[0]
    assert row["currency"] == "BTC"
    assert row["amount_minor"] == -4217000


def test_a_transaction_in_no_stated_currency_at_all_is_still_refused(synced: Config) -> None:
    """The control: the fallback widens what counts as stated, not what counts as known.

    An amount whose unit nothing named is how a total silently mixes two of them,
    and that refusal is the same one the balance path makes.
    """
    entry = _txn(transaction_id="t1", amount="12.00")
    entry["iso_currency_code"] = None
    entry["unofficial_currency_code"] = None

    with pytest.raises(DerivationError, match="currency"):
        _apply(synced, TRANSACTIONS_SYNC.path, _sync_body(added=[entry]))


def test_an_account_the_sync_response_names_is_derived_before_its_transactions(
    synced: Config,
) -> None:
    """🔴 The permanent wedge, and the roster that was riding the same body.

    An account closed, de-selected in Account Select, or no longer shared drops
    out of `/accounts/get` while `/transactions/sync` keeps emitting deltas for
    its rows. The page then failed forever: the raw body committed, the
    derivation rolled back, the cursor never moved, and the next run re-fetched
    and re-archived the identical page. The archive itself became unrebuildable,
    so the one recovery tool failed on the same response.

    The aggregator is telling you which accounts these transactions belong to,
    in the same body -- so the roster it carries is derived before the change
    lists that depend on it.
    """
    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(
            accounts_listed=[_account_entry(), _account_entry("acct-savings", name="Savings")],
            added=[_txn(transaction_id="t1", amount="12.00") | {"account_id": "acct-savings"}],
            next_cursor="cursor-2",
        ),
    )

    with reader_connection(synced) as conn:
        derived = set(conn.execute(select(accounts.c.source_account_id)).scalars())
        cursor = conn.execute(
            select(sync_state.c.cursor).where(sync_state.c.domain == TRANSACTIONS_DOMAIN)
        ).scalar_one()
    assert derived == {SOURCE_ACCOUNT, "acct-savings"}
    assert len(_rows(synced)) == 1
    assert cursor == "cursor-2", "the cursor did not advance past the page it applied"


def test_a_sync_page_does_not_claim_the_roster_was_observed(synced: Config) -> None:
    """🔴 A sync body's `accounts` array is not a roster read.

    It names the accounts these transactions belong to, which is not the same
    statement as *this is every account this connection has*. Recording it as an
    observation would make AC-12.5's absence test compare accounts against a
    date no roster read produced -- and a connection whose sync page landed after
    midnight would report every account of its own last roster as behind it.
    """
    before = _connection_row(synced)["roster_observed_date"]

    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(
            accounts_listed=[_account_entry("savings", name="Savings")],
            next_cursor="cursor-2",
        ),
    )

    assert _connection_row(synced)["roster_observed_date"] == before
    with reader_connection(synced) as conn:
        seen = conn.execute(
            select(accounts.c.last_seen_date).where(accounts.c.source_account_id == "savings")
        ).scalar_one()
    assert seen is None, "an account learned from a sync page claims a roster observation"


def test_a_removal_naming_an_account_this_system_does_not_have_is_a_no_op(
    synced: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """The docstring `_mark_removed` already had, made true.

    A soft delete of a row this system does not hold has nothing to do -- and
    refusing instead stopped the cursor, so the connection stayed at that page
    for good. A rebuild replaying removals whose original page predates the
    archive meets the same shape.
    """
    with caplog.at_level(logging.INFO, logger="bankmachine"):
        _apply(
            synced,
            TRANSACTIONS_SYNC.path,
            _sync_body(
                removed=[{"transaction_id": "gone", "account_id": "acct-never-derived"}],
                next_cursor="cursor-2",
            ),
        )

    with reader_connection(synced) as conn:
        cursor = conn.execute(
            select(sync_state.c.cursor).where(sync_state.c.domain == TRANSACTIONS_DOMAIN)
        ).scalar_one()
    assert cursor == "cursor-2", "a removal for an unknown account stopped the cursor"
    assert any("no row for" in record.getMessage() for record in caplog.records), (
        "a removal was skipped with nothing recording that it happened"
    )


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


# --------------------------------------------------------------------------
# Lineage: two Items' worth of history on one account (#68, Part 2)
# --------------------------------------------------------------------------

#: The aggregator's own statement that two differently-numbered accounts are the
#: same underlying account, and the id the second Item issues for it.
PERSISTENT = "persistent-checking"
RELINKED_ACCOUNT = "acct-checking-relinked"

#: Two purchases, months apart, so a lineage's covered range is wider than a day
#: and an older lineage has somewhere outside it to still answer from.
SPRING = "2026-03-10"
SUMMER = "2026-06-15"

#: A purchase from before the second Item's granted window -- the tail only the
#: older lineage holds, and the one an over-eager exclusion would delete.
LAST_AUTUMN = "2025-11-02"


def _relink(config: Config) -> int:
    """Retire the enrolled connection and enrol its replacement at the same bank.

    🔴 The retirement is not decoration. `connections_one_live_per_institution`
    refuses two live connections at one institution, so this is the state
    `connections remove` followed by `enroll` actually produces -- and a fixture
    built any other way would test a shape the product cannot reach.
    """
    later = now_utc()
    with writer_connection(config) as conn:
        institution_id = conn.execute(
            select(institutions.c.institution_id).where(
                institutions.c.source_institution_id == "ins_109508"
            )
        ).scalar_one()
        conn.execute(
            update(connections)
            .where(connections.c.connection_id == CONNECTION_ID)
            .values(status="retired", retired_at=later, updated_at=later)
        )
        created = conn.execute(
            insert(connections).values(
                institution_id=institution_id,
                source_connection_id="item-after-relink",
                credential_ref="connection:sandbox:item-after-relink",
                capabilities="[]",
                requested_history_days=730,
                status="active",
                enrolled_at=later,
                created_at=later,
                updated_at=later,
            )
        ).inserted_primary_key
        assert created is not None  # an INTEGER PRIMARY KEY insert always yields one
        return int(created[0])


def _purchase(account: str, transaction_id: str, amount: str, day: str) -> dict[str, Any]:
    """One purchase whose two dates agree, so `ledger_date` is unambiguous."""
    return _txn(
        account_id=account,
        transaction_id=transaction_id,
        amount=amount,
        date=day,
        authorized_date=day,
    )


def _relinked_history(config: Config, *, older_tail: bool = False) -> int:
    """One account synced under two Items, the second re-delivering the first's rows.

    What a remove-and-re-link actually produces: the account converges on its
    persistent identity, and the whole granted history arrives again under new
    transaction ids that collide with nothing.
    """
    _apply(
        config,
        ACCOUNTS_GET.path,
        _accounts_body([_account_entry(persistent_account_id=PERSISTENT)]),
    )
    first = [
        _purchase(SOURCE_ACCOUNT, "t-spring", "10.00", SPRING),
        _purchase(SOURCE_ACCOUNT, "t-summer", "20.00", SUMMER),
    ]
    if older_tail:
        first.append(_purchase(SOURCE_ACCOUNT, "t-autumn", "30.00", LAST_AUTUMN))
    _apply(config, TRANSACTIONS_SYNC.path, _sync_body(added=first))

    relinked = _relink(config)
    _apply(
        config,
        ACCOUNTS_GET.path,
        _accounts_body([_account_entry(RELINKED_ACCOUNT, persistent_account_id=PERSISTENT)]),
        connection_id=relinked,
    )
    _apply(
        config,
        TRANSACTIONS_SYNC.path,
        _sync_body(
            added=[
                _purchase(RELINKED_ACCOUNT, "t-spring-again", "10.00", SPRING),
                _purchase(RELINKED_ACCOUNT, "t-summer-again", "20.00", SUMMER),
            ],
            next_cursor="cursor-relinked",
        ),
        connection_id=relinked,
    )
    return relinked


#: The account id the aggregator mints for the SAME real account behind a new
#: Item. Distinct from `RELINKED_ACCOUNT` only so a test reading both fixtures can
#: tell which path produced a row.
REISSUED_ACCOUNT = "acct-checking-reissued"

#: A re-link is a repair for something that broke, so it happens after the sync it
#: replaces -- and the roster read that stops listing the old account is what makes
#: it detectable. A day is the resolution `last_seen_date` records.
_A_DAY = timedelta(days=1)


def _converging_relink_history(
    config: Config,
    *,
    older_tail: bool = False,
    mask: str | None = "0000",
) -> None:
    """One real account synced under two Items WITHOUT converging on one row.

    🔴 The difference from `_relinked_history` is the whole point, and it is the
    case production actually produces. There, the account carries a
    `persistent_account_id`, the roster matches on it, and both generations land
    on ONE account row. Here nothing carries one -- which is the rule outside the
    three banks that publish the field -- so the roster INSERTS a second account
    row, and the connection is converged in place rather than retired, so both
    generations also share one lineage.

    Neither `account_id` nor `lineage_id` separates the two generations. That is
    exactly the state in which the aggregates used to report every figure twice.

    🔴 The re-link is recorded a day AFTER the sync it replaces, and the offset is
    load-bearing rather than cosmetic. `last_seen_date` is a monotone maximum over
    roster reads, so the evidence that the institution stopped listing the first
    account is that account's date falling BEHIND the connection's latest
    observation. Recording both at one instant leaves the two indistinguishable --
    which is also true in production, where a re-link inside the same day as the
    previous sync is below the resolution `last_seen_date` records.
    """
    _apply(config, ACCOUNTS_GET.path, _accounts_body([_account_entry(mask=mask)]))
    first = [
        _purchase(SOURCE_ACCOUNT, "t-spring", "10.00", SPRING),
        _purchase(SOURCE_ACCOUNT, "t-summer", "20.00", SUMMER),
    ]
    if older_tail:
        first.append(_purchase(SOURCE_ACCOUNT, "t-autumn", "30.00", LAST_AUTUMN))
    _apply(config, TRANSACTIONS_SYNC.path, _sync_body(added=first))

    # The re-link. The connection row is NOT retired and NOT replaced, so
    # `connection_id` -- and with it `lineage_id` -- is unchanged, and this roster
    # no longer lists the account the first one did.
    relinked_at = now_utc() + _A_DAY
    _apply(
        config,
        ACCOUNTS_GET.path,
        _accounts_body([_account_entry(REISSUED_ACCOUNT, mask=mask)]),
        received_at=relinked_at,
    )
    _apply(
        config,
        TRANSACTIONS_SYNC.path,
        _sync_body(
            added=[
                _purchase(REISSUED_ACCOUNT, "t-spring-reissued", "10.00", SPRING),
                _purchase(REISSUED_ACCOUNT, "t-summer-reissued", "20.00", SUMMER),
            ],
            next_cursor="cursor-reissued",
        ),
        received_at=relinked_at,
    )


def _total(config: Config, *, superseded: bool = False, rule: bool = True) -> int:
    """What a total over this store comes to, with the lineage rule on or off.

    `rule=False` is the naive sum -- the figure this store reports today -- and it
    is asserted alongside the corrected one so the test states the defect rather
    than only the fix.
    """
    with reader_connection(config) as conn:
        spans = superseded_spans(conn)
        if not rule:
            where = counts_once(())
        else:
            where = only_superseded(spans) if superseded else counts_once(spans)
        return int(
            conn.execute(
                select(func.coalesce(func.sum(transactions.c.amount_minor), 0)).where(
                    transactions.c.removed_at.is_(None), where
                )
            ).scalar_one()
        )


def test_a_transaction_records_the_aggregator_item_that_produced_it(synced: Config) -> None:
    """The column the whole convergence rests on, filled from the archived response.

    A lineage is not a new concept in this system: the sync cursor and the
    measured granted window are already Item-scoped and are already reset by a
    re-link. This is that same boundary, recorded on the rows so a total can tell
    two Items' copies of one month apart.
    """
    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(added=[_txn(transaction_id="t1", amount="89.40")]),
    )

    assert [row["lineage_id"] for row in _rows(synced)] == [CONNECTION_ID]


def test_a_settlement_leaves_a_transaction_in_the_lineage_that_produced_it(
    synced: Config,
) -> None:
    """Stamped once, like `ledger_date`, and for the same reason.

    A hold posting is the one event that rewrites almost every column of a row,
    and where a row CAME FROM is not something a later fact about it can change.
    A settlement that re-homed a row would move it across a lineage boundary, so
    which total counted it would depend on when the question was asked.
    """
    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(added=[_txn(transaction_id="hold", amount="12.00", pending=True)]),
    )
    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(
            added=[
                _txn(
                    transaction_id="posted",
                    amount="12.50",
                    pending_transaction_id="hold",
                    date="2026-09-09",
                )
            ]
        ),
    )

    held = _rows(synced)
    assert len(held) == 1, "the posting did not merge into the hold, so this proves nothing"
    assert held[0]["lineage_id"] == CONNECTION_ID


def test_a_relink_does_not_double_the_annual_total(synced: Config) -> None:
    """🔴 The headline defect: the same money counted twice, silently.

    A full re-link re-issues every transaction id, so the whole granted history
    arrives again as rows this store has never seen, against an account it now
    recognises. Nothing collides, nothing warns, and the year's spending is
    double. The naive figure is asserted here beside the corrected one, because
    a fix that merely produced a plausible number would be indistinguishable
    from the defect it replaces.

    🔴 Every row is KEPT. The rejected alternative -- deduping on merchant,
    amount and day -- would have collapsed two genuinely distinct $5 coffees into
    one, and an undercount is the direction that gets believed.
    """
    _relinked_history(synced)

    with reader_connection(synced) as conn:
        listed = conn.execute(select(accounts.c.account_id)).scalars().all()
    assert len(listed) == 1, "the re-link split the account, so its history is in two places"
    assert len(_rows(synced)) == 4, "a row was deleted or deduped; every row is meant to be kept"
    assert _total(synced, rule=False) == -6000, (
        "the store no longer holds the doubled history this test exists to correct"
    )
    assert _total(synced) == -3000, (
        "the year is still counted twice, which is the number the operator reads"
    )


def test_the_superseded_rows_are_retained_and_reachable(synced: Config) -> None:
    """🔴 Excluded from a total is not removed from the store.

    `data-model.md` § Direction already says a transaction that goes away is soft
    deleted rather than dropped, and the same rule governs here: an operator who
    is told a range was superseded must be able to look at what was left out and
    judge it. A design that could only be trusted is the design that was
    rejected.
    """
    older = CONNECTION_ID
    _relinked_history(synced)

    with reader_connection(synced) as conn:
        spans = superseded_spans(conn)
    assert [(span.lineage_id, span.start.isoformat(), span.end.isoformat()) for span in spans] == [
        (older, SPRING, SUMMER)
    ], "the disclosure does not name the lineage and range the total left out"
    assert spans[0].account_id == _rows(synced)[0]["account_id"]
    assert _total(synced, superseded=True) == -3000, (
        "the superseded rows cannot be asked for, so an operator is told a range was excluded "
        "and handed nothing to check it against"
    )
    assert all(row["removed_at"] is None for row in _rows(synced)), (
        "a superseded row was soft-deleted; exclusion is a reading of the rows, not a change "
        "to them"
    )


def test_an_older_lineage_still_answers_outside_the_range_the_newer_one_covers(
    synced: Config,
) -> None:
    """🔴 The tail only the old Item holds, and the undercount that would eat it.

    A re-link resets the measured granted window, and the new Item routinely
    grants less history than the store already has. Excluding an older lineage
    outright -- rather than only where a newer one covers it -- would delete
    every month before the new grant begins, and the loss would look exactly like
    a household that spent nothing.
    """
    _relinked_history(synced, older_tail=True)

    assert len(_rows(synced)) == 5
    assert _total(synced) == -6000, (
        "the purchase from before the new Item's window was dropped with the lineage it "
        "belongs to, so the store now reports months it holds as empty"
    )
    assert _total(synced, superseded=True) == -3000, (
        "the tail was counted as superseded, so the exclusion reaches past the overlap"
    )


def test_a_relink_that_splits_the_account_does_not_double_the_total(
    synced: Config,
) -> None:
    """🔴 The same defect as the headline one, by the route production takes.

    `test_a_relink_does_not_double_the_annual_total` builds its second generation
    by RETIRING the connection and matching the account on its persistent
    identity. Both of those are the lucky case: a new connection gives a new
    lineage, and a matched account keeps one row. Production supplies neither --
    the field is null outside three institutions, and a re-link converges the
    connection in place -- so the store ends up with two account rows under ONE
    lineage, which is the shape no ordering over `(account_id, lineage_id)` can
    see.

    Measured on the live sandbox store before this rule existed: `money_summary`
    reported 2229892 for a month whose true figure is 1114946.
    """
    _converging_relink_history(synced)

    with reader_connection(synced) as conn:
        listed = conn.execute(select(accounts.c.account_id)).scalars().all()
        lineages = {row["lineage_id"] for row in _rows(synced)}
    assert len(listed) == 2, (
        "the roster converged the account onto one row, so this fixture is building the "
        "path that already worked rather than the one that did not"
    )
    assert lineages == {CONNECTION_ID}, (
        "the re-link minted a second lineage, so this passes for a reason the production "
        "path does not supply"
    )
    assert len(_rows(synced)) == 4, "a row was deleted or deduped; every row is meant to be kept"
    assert _total(synced, rule=False) == -6000, (
        "the store no longer holds the doubled history this test exists to correct"
    )
    assert _total(synced) == -3000, (
        "the re-issued generation is still counted beside the one that replaced it, so "
        "every figure over this store reads double"
    )


def test_a_split_relink_discloses_the_generation_it_excluded(synced: Config) -> None:
    """🔴 A correct total and a wrong one look identical unless the answer says.

    The exclusion is the reason the figure is right, so the span must name the
    account and the range -- an operator told "some rows were left out" has been
    handed nothing to check.
    """
    _converging_relink_history(synced)

    with reader_connection(synced) as conn:
        spans = superseded_spans(conn)
        superseded_account = conn.execute(
            select(accounts.c.account_id).where(accounts.c.source_account_id == SOURCE_ACCOUNT)
        ).scalar_one()
    assert [(span.account_id, span.start.isoformat(), span.end.isoformat()) for span in spans] == [
        (superseded_account, SPRING, SUMMER)
    ], "the disclosure does not name the account and range the total left out"
    assert _total(synced, superseded=True) == -3000, (
        "the superseded rows cannot be asked for, so an operator is told a range was excluded "
        "and handed nothing to check it against"
    )
    assert all(row["removed_at"] is None for row in _rows(synced)), (
        "a superseded row was soft-deleted; exclusion is a reading of the rows, not a change "
        "to them"
    )


def test_a_split_relink_keeps_the_tail_the_older_generation_alone_holds(
    synced: Config,
) -> None:
    """🔴 The undercount arriving inside the fix for the overcount.

    A new Item routinely grants less history than the store already holds.
    Excluding the older generation outright -- rather than only where the newer
    one covers it -- would delete every month before the new grant begins, and
    the loss would look exactly like a household that spent nothing.
    """
    _converging_relink_history(synced, older_tail=True)

    assert len(_rows(synced)) == 5
    assert _total(synced) == -6000, (
        "the purchase from before the new Item's window was dropped with the generation it "
        "belongs to, so the store now reports months it holds as empty"
    )
    assert _total(synced, superseded=True) == -3000, (
        "the tail was counted as superseded, so the exclusion reaches past the overlap"
    )


def test_two_accounts_the_institution_still_lists_are_never_superseded(
    synced: Config,
) -> None:
    """🔴 The exclusion that would delete money really spent.

    A household can hold a checking account and an overdraft line that share a
    four-digit mask, and at the one real institution measured here it does. They
    resemble each other on every field this rule partitions by, and both are on
    every roster. Resemblance is therefore NOT what licenses an exclusion; the
    institution having STOPPED listing one of them is.

    Without that guard this is the failure mode: one live account silently
    stops being counted, and an undercount is the direction that gets believed.
    """
    _apply(
        synced,
        ACCOUNTS_GET.path,
        _accounts_body([_account_entry(), _account_entry("acct-overdraft")]),
    )
    _apply(
        synced,
        TRANSACTIONS_SYNC.path,
        _sync_body(
            added=[
                _purchase(SOURCE_ACCOUNT, "t-spring", "10.00", SPRING),
                _purchase("acct-overdraft", "t-spring-overdraft", "20.00", SPRING),
            ]
        ),
    )

    with reader_connection(synced) as conn:
        assert superseded_spans(conn) == (), (
            "one of two accounts the institution still lists was superseded by the other, so "
            "money that was really spent has left the totals"
        )
    assert _total(synced) == -3000


def test_an_account_with_no_mask_is_never_matched_to_another_row(
    synced: Config,
) -> None:
    """🔴 A null mask means the aggregator did not state one, never that two agree.

    Grouping on an absence would let every unmasked account at one institution
    fall into a single partition, where the first roster read to drop any of them
    would supersede it against an unrelated account's history.
    """
    _converging_relink_history(synced, mask=None)

    with reader_connection(synced) as conn:
        assert superseded_spans(conn) == (), (
            "two accounts were matched on a mask neither of them has"
        )
    assert _total(synced, rule=False) == -6000
    assert _total(synced) == -6000, (
        "the rule excluded rows on the strength of a field nobody stated; the doubling is "
        "meant to stay VISIBLE here rather than be silently half-corrected"
    )


def test_a_transaction_with_no_lineage_is_counted_rather_than_excluded(
    synced: Config,
) -> None:
    """🔴 A null means "predates the split", never "belongs to the current Item".

    Between migration 007 and `store rebuild` every row already in the store has
    an empty lineage. Reading that as anything but "unknown" would exclude rows
    from totals on the strength of a column nothing ever filled -- an invisible
    undercount, arriving inside the fix for an overcount. So the rows stay in,
    the total stays doubled until the rebuild runs, and the doubling is the
    visible failure the rebuild then closes.
    """
    _relinked_history(synced)
    with writer_connection(config=synced) as conn:
        conn.execute(
            update(transactions)
            .where(transactions.c.lineage_id == CONNECTION_ID)
            .values(lineage_id=None)
        )

    with reader_connection(synced) as conn:
        assert superseded_spans(conn) == (), "a row with no lineage supersedes or is superseded"
    assert _total(synced) == -6000
