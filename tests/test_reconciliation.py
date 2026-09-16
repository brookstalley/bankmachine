"""AC-11.2's residual: whether recorded transactions explain the balance. #70.

🔴 **The claim under test here has been asserted by two docstrings and a
`## Direction` norm since the schema was frozen, with nothing computing it.** The
identity is that over any interval between two consecutive balance snapshots, the
change in the balance equals the sum of the transactions recorded in that
interval. Its expected value is zero everywhere. Whether it IS zero is the
measurement this file makes reachable and `bankmachine status` will report.

🔴 **Every fixture here asserts it reached the subject before asserting about
it.** A reconciliation over zero paired intervals is green by vacuity, and a
green-by-vacuity reconciliation looks exactly like a clean one -- which is the
failure shape this whole cycle exists to correct one instance of.

The snapshots are written by driving real `/accounts/get` captures through the
shipped derivers at chosen instants, never by hand-inserting `balances_daily`
rows: the schema enforces provenance with a CHECK, and hand-built rows would
encode this file's assumptions about that constraint instead of exercising it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import insert, select

from bankmachine import envelope, query
from bankmachine.config import Config
from bankmachine.connector import ACCOUNTS_GET, TRANSACTIONS_SYNC
from bankmachine.derivers import ALL_DERIVERS
from bankmachine.store.derivation import apply_response
from bankmachine.store.engine import reader_connection, writer_connection
from bankmachine.store.schema import accounts, balances_daily, connections, institutions
from bankmachine.store.types import calendar_date, utc_instant

ITEM = "item-recon"
#: A fixed origin rather than "today". A reconciliation walks dated snapshots, so
#: a fixture anchored on the clock changes its own intervals at midnight and the
#: failure looks like a data bug rather than a test one.
ORIGIN = utc_instant(datetime(2026, 3, 1, 12, 0, tzinfo=UTC))

CHECKING = "acct-checking"
CARD = "acct-card"
BROKERAGE = "acct-brokerage"


def _account(source_id: str, account_type: str, subtype: str, current: str) -> dict[str, Any]:
    return {
        "account_id": source_id,
        "name": f"Plaid {subtype.title()}",
        "mask": "0000",
        "type": account_type,
        "subtype": subtype,
        "balances": {
            "current": current,
            "available": None,
            "limit": None,
            "iso_currency_code": "USD",
        },
    }


def _enroll(config: Config, *, granted_days: int | None = 730) -> None:
    with writer_connection(config) as conn:
        primary_key = conn.execute(
            insert(institutions).values(
                source_institution_id="ins_recon",
                name="First Platypus Bank",
                first_seen_at=ORIGIN,
                last_seen_at=ORIGIN,
            )
        ).inserted_primary_key
        assert primary_key is not None
        conn.execute(
            insert(connections).values(
                connection_id=1,
                institution_id=int(primary_key[0]),
                source_connection_id=ITEM,
                credential_ref=f"connection:sandbox:{ITEM}",
                capabilities="[]",
                requested_history_days=730,
                granted_history_days=granted_days,
                status="active",
                last_success_at=ORIGIN,
                enrolled_at=ORIGIN,
                created_at=ORIGIN,
                updated_at=ORIGIN,
            )
        )


def _capture(config: Config, listed: list[dict[str, Any]], *, day_offset: int) -> None:
    """One `/accounts/get` capture, which is what writes a balance snapshot."""
    with writer_connection(config) as conn:
        apply_response(
            conn,
            connection_id=1,
            endpoint=ACCOUNTS_GET.path,
            body=json.dumps(
                {"accounts": listed, "item": {"item_id": ITEM}, "request_id": "req-accounts"}
            ).encode(),
            received_at=utc_instant(ORIGIN + timedelta(days=day_offset)),
            derivers=ALL_DERIVERS,
            replay_passes=(),
        )


def _post(
    config: Config,
    movements: list[tuple[str, int, str]],
    *,
    pending: bool = False,
    cursor: str = "c",
) -> None:
    """Transactions through the real sync deriver. `movements` is (account, day offset, amount)."""
    added = [
        {
            "account_id": account,
            "transaction_id": f"{cursor}-{index}",
            "amount": amount,
            "iso_currency_code": "USD",
            "date": str(ORIGIN.date() + timedelta(days=offset)),
            "authorized_date": None,
            "pending": pending,
            "pending_transaction_id": None,
            "name": "Coffee",
            "merchant_name": None,
            "personal_finance_category": {
                "primary": "FOOD_AND_DRINK",
                "detailed": "FOOD_AND_DRINK",
            },
        }
        for index, (account, offset, amount) in enumerate(movements)
    ]
    with writer_connection(config) as conn:
        apply_response(
            conn,
            connection_id=1,
            endpoint=TRANSACTIONS_SYNC.path,
            body=json.dumps(
                {
                    "accounts": [],
                    "added": added,
                    "modified": [],
                    "removed": [],
                    "next_cursor": cursor,
                    "has_more": False,
                    "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE",
                    "request_id": f"req-sync-{cursor}",
                }
            ).encode(),
            received_at=ORIGIN,
            derivers=ALL_DERIVERS,
            replay_passes=(),
        )


def _reconciliation(config: Config) -> dict[int, query.AccountReconciliation]:
    with reader_connection(config) as conn:
        found = query._account_reconciliation(conn)
    assert found, "the producer returned nothing, so no assertion below is about anything"
    return found


def _by_source_id(config: Config) -> dict[str, int]:
    with reader_connection(config) as conn:
        mapping = {
            str(row[0]): int(row[1])
            for row in conn.execute(
                select(accounts.c.source_account_id, accounts.c.account_id)
            ).all()
        }
    assert mapping, "no account was derived, so nothing is under test"
    return mapping


def _snapshot_count(config: Config, account_id: int) -> int:
    with reader_connection(config) as conn:
        return len(
            conn.execute(
                select(balances_daily.c.as_of_date).where(balances_daily.c.account_id == account_id)
            ).all()
        )


@pytest.fixture
def one_connection(initialized_config: Config) -> Config:
    _enroll(initialized_config)
    return initialized_config


def test_the_fixture_actually_writes_two_snapshots_and_a_transaction(
    one_connection: Config,
) -> None:
    """The scaffolding's own control. Everything below is meaningless without it."""
    _capture(one_connection, [_account(CHECKING, "depository", "checking", "100.00")], day_offset=0)
    _capture(one_connection, [_account(CHECKING, "depository", "checking", "90.00")], day_offset=10)
    _post(one_connection, [(CHECKING, 5, "10.00")])
    account_id = _by_source_id(one_connection)[CHECKING]
    assert _snapshot_count(one_connection, account_id) == 2, (
        "two captures did not produce two balance snapshots, so no interval exists to reconcile "
        "and every reconciliation assertion in this file would pass by vacuity"
    )
    entry = _reconciliation(one_connection)[account_id]
    assert entry.intervals_compared == 1


# --------------------------------------------------------------------------- #
# The identity itself, in both directions.
# --------------------------------------------------------------------------- #


def test_transactions_that_bridge_two_snapshots_exactly_leave_no_residual(
    one_connection: Config,
) -> None:
    """The expected answer everywhere, and the one the product has been claiming."""
    _capture(one_connection, [_account(CHECKING, "depository", "checking", "100.00")], day_offset=0)
    _capture(one_connection, [_account(CHECKING, "depository", "checking", "90.00")], day_offset=10)
    _post(one_connection, [(CHECKING, 5, "10.00")])

    entry = _reconciliation(one_connection)[_by_source_id(one_connection)[CHECKING]]
    assert entry.intervals_compared == 1, "no interval was compared; a zero below means nothing"
    assert entry.state == "reconciled"
    assert entry.residual_minor_units == 0
    assert entry.unreconciled_count == 0
    assert entry.unreconciled == ()


def test_a_balance_that_moved_with_no_transaction_is_reported_with_its_magnitude(
    one_connection: Config,
) -> None:
    """🔴 The finding. Money left the account and nothing in the store records it.

    The magnitude is asserted as well as the flag: `learnings.md` records that
    the magnitude is the load-bearing half, because a flag cannot tell a rounding
    artefact from a missing month.
    """
    _capture(one_connection, [_account(CHECKING, "depository", "checking", "100.00")], day_offset=0)
    _capture(one_connection, [_account(CHECKING, "depository", "checking", "75.00")], day_offset=10)
    _post(one_connection, [(CHECKING, 5, "10.00")])

    entry = _reconciliation(one_connection)[_by_source_id(one_connection)[CHECKING]]
    assert entry.intervals_compared == 1
    assert entry.state == "reconciled", "the comparison ran; `reconciled` is not a clean bill"
    # The balance fell 25.00 while the only transaction accounts for 10.00.
    assert entry.residual_minor_units == -1500
    assert entry.unreconciled_count == 1
    (interval,) = entry.unreconciled
    assert interval.balance_change_minor_units == -2500
    assert interval.transactions_sum_minor_units == -1000
    assert interval.residual_minor_units == -1500
    assert interval.currency == "USD"
    assert interval.from_date == calendar_date(ORIGIN.date())
    assert interval.to_date == calendar_date(ORIGIN.date() + timedelta(days=10))


def test_a_liability_account_reconciles_under_the_same_arithmetic(
    one_connection: Config,
) -> None:
    """🔴 The operator-signed norm's own Why, under test for the first time.

    `data-model.md` states that every stored amount is operator-signed so that
    ONE subtraction works for every account class. A liability's balance is
    stored negated relative to the aggregator's "amount owed", so if that
    normalization were wrong this interval would come back at twice the
    magnitude rather than at zero -- which is exactly the failure the norm claims
    to prevent and nothing has ever checked.
    """
    _capture(one_connection, [_account(CARD, "credit", "credit card", "100.00")], day_offset=0)
    _capture(one_connection, [_account(CARD, "credit", "credit card", "130.00")], day_offset=10)
    # A 30.00 purchase on a card: the debt grows by 30.00.
    _post(one_connection, [(CARD, 5, "30.00")])

    entry = _reconciliation(one_connection)[_by_source_id(one_connection)[CARD]]
    assert entry.intervals_compared == 1, "no interval compared; the norm is not under test"
    assert entry.state == "reconciled"
    assert entry.residual_minor_units == 0, (
        "a liability did not reconcile under the same subtraction an asset does, which is the "
        "consequence `data-model.md`'s operator-signed norm exists to guarantee"
    )


def test_a_pending_row_is_not_counted_on_either_side(one_connection: Config) -> None:
    """🔴 `api-notes-plaid.md` §§27-28: `current` is the SETTLED balance.

    Both sides of the comparison exclude pending, so an unsettled authorization
    must move neither. If pending rows were summed, this interval would come back
    non-zero and the product would report a finding on an account that is
    behaving exactly as documented.
    """
    _capture(one_connection, [_account(CHECKING, "depository", "checking", "100.00")], day_offset=0)
    _capture(one_connection, [_account(CHECKING, "depository", "checking", "90.00")], day_offset=10)
    _post(one_connection, [(CHECKING, 5, "10.00")], cursor="posted")
    _post(one_connection, [(CHECKING, 6, "40.00")], pending=True, cursor="held")

    account_id = _by_source_id(one_connection)[CHECKING]
    with reader_connection(one_connection) as conn:
        from bankmachine.store.schema import transactions as txn_table

        held = conn.execute(
            select(txn_table.c.transaction_id).where(
                txn_table.c.account_id == account_id, txn_table.c.pending == 1
            )
        ).all()
    assert held, "no pending row was written, so the exclusion below is not under test"

    entry = _reconciliation(one_connection)[account_id]
    assert entry.residual_minor_units == 0, (
        "a pending authorization moved the residual. `current` excludes pending, so the "
        "interval sum must too -- otherwise every open hold reads as a finding"
    )


# --------------------------------------------------------------------------- #
# The four states, each separable from the other three.
# --------------------------------------------------------------------------- #
#
# 🔴 The three non-`reconciled` states must be distinguishable from ONE ANOTHER,
# not merely from `reconciled`. `learnings.md` § *A carve-out reaches every state
# that shares its return type*: when one function collapses distinguishable
# states into one value, the collapse is the defect -- and all three of these
# produce a null residual, so a single null is precisely that collapse. These are
# the tests that would catch it.


def test_an_investment_account_is_named_not_applicable_rather_than_reconciled(
    one_connection: Config,
) -> None:
    """🔴 Excluded, NAMED, and never silently dropped.

    `api-notes-plaid.md` §23 measured why: positions and the reported balance
    need not add up, so a check asserting they do fails on correct data.
    """
    _capture(
        one_connection, [_account(BROKERAGE, "investment", "brokerage", "5000.00")], day_offset=0
    )
    _capture(
        one_connection, [_account(BROKERAGE, "investment", "brokerage", "5400.00")], day_offset=10
    )
    account_id = _by_source_id(one_connection)[BROKERAGE]
    assert _snapshot_count(one_connection, account_id) == 2, (
        "the investment account has fewer than two snapshots, so this test would pass for "
        "`insufficient_snapshots` and prove nothing about the investment exclusion"
    )

    entry = _reconciliation(one_connection)[account_id]
    assert entry.state == "not_applicable_investment"
    assert entry.residual_minor_units is None
    assert entry.intervals_compared == 0


def test_a_single_snapshot_is_insufficient_rather_than_reconciled(
    one_connection: Config,
) -> None:
    """One capture yields no INTERVAL, which is not the same as reconciling."""
    _capture(one_connection, [_account(CHECKING, "depository", "checking", "100.00")], day_offset=0)
    account_id = _by_source_id(one_connection)[CHECKING]
    assert _snapshot_count(one_connection, account_id) == 1

    entry = _reconciliation(one_connection)[account_id]
    assert entry.state == "insufficient_snapshots"
    assert entry.residual_minor_units is None
    assert entry.intervals_compared == 0


def test_an_account_whose_balance_was_never_recorded_says_so(one_connection: Config) -> None:
    """A null `current` writes NO snapshot, which is a third distinct state.

    The deriver's `reported is None` path leaves no `balances_daily` row at all,
    so this account has never had a balance -- as opposed to having had one.
    """
    listed = _account(CHECKING, "depository", "checking", "100.00")
    listed["balances"]["current"] = None
    _capture(one_connection, [listed], day_offset=0)
    account_id = _by_source_id(one_connection)[CHECKING]
    assert _snapshot_count(one_connection, account_id) == 0, (
        "a null `current` still wrote a balance row, so this test is exercising "
        "`insufficient_snapshots` rather than `no_balance_recorded`"
    )

    entry = _reconciliation(one_connection)[account_id]
    assert entry.state == "no_balance_recorded"
    assert entry.residual_minor_units is None
    assert entry.currency is None


def test_the_four_states_are_reachable_together_and_none_shadows_another(
    one_connection: Config,
) -> None:
    """🔴 All four in ONE store, so the mapping cannot be right by coincidence.

    Asserted together rather than only apart: a producer keyed on the wrong thing
    can answer each isolated case correctly and still assign the wrong state when
    several accounts are present, which is the only shape this surface ever runs in.
    """
    never = _account("acct-never", "depository", "savings", "100.00")
    never["balances"]["current"] = None
    _capture(
        one_connection,
        [
            _account(CHECKING, "depository", "checking", "100.00"),
            _account("acct-one-shot", "depository", "savings", "50.00"),
            _account(BROKERAGE, "investment", "brokerage", "5000.00"),
            never,
        ],
        day_offset=0,
    )
    # Only the checking account is captured a second time, so only it gets an interval.
    _capture(one_connection, [_account(CHECKING, "depository", "checking", "90.00")], day_offset=10)
    _post(one_connection, [(CHECKING, 5, "10.00")])

    ids = _by_source_id(one_connection)
    found = _reconciliation(one_connection)
    states = {source: found[ids[source]].state for source in ids}
    assert states == {
        CHECKING: "reconciled",
        "acct-one-shot": "insufficient_snapshots",
        BROKERAGE: "not_applicable_investment",
        "acct-never": "no_balance_recorded",
    }
    # 🔴 And the null is NOT what distinguishes them: three of these share it.
    nulls = {source for source in ids if found[ids[source]].residual_minor_units is None}
    assert nulls == {"acct-one-shot", BROKERAGE, "acct-never"}, (
        "the three non-reconciled states must all carry a null residual -- if one of them "
        "carried a number, the state field would be inferable from the null and the collapse "
        "this design prevents would not be prevented by it"
    )


def test_a_brokerage_typed_account_is_excluded_like_an_investment_one(
    one_connection: Config,
) -> None:
    """🔴 `brokerage` is the pre-2018-05-22 spelling of the same account class.

    Omitting it would reconcile a brokerage account as though its balance were
    cash and attribute every market move to a missing transaction -- a residual
    indistinguishable from a real finding, on an account behaving correctly.
    """
    _capture(
        one_connection, [_account("acct-old", "brokerage", "brokerage", "5000.00")], day_offset=0
    )
    _capture(
        one_connection, [_account("acct-old", "brokerage", "brokerage", "5400.00")], day_offset=10
    )
    account_id = _by_source_id(one_connection)["acct-old"]
    assert _snapshot_count(one_connection, account_id) == 2

    entry = _reconciliation(one_connection)[account_id]
    assert entry.state == "not_applicable_investment"


# --------------------------------------------------------------------------- #
# Cause attribution: every cause reaches its own branch, and the order holds.
# --------------------------------------------------------------------------- #


def _set_history_start(config: Config, *, day_offset: int) -> None:
    """Where the aggregator granted this connection's transaction history from."""
    from bankmachine.store.schema import TRANSACTIONS_DOMAIN
    from bankmachine.store.sync_domains import record_domain_history_start

    with writer_connection(config) as conn:
        record_domain_history_start(
            conn,
            connection_id=1,
            domain=TRANSACTIONS_DOMAIN,
            start=calendar_date(ORIGIN.date() + timedelta(days=day_offset)),
            at=ORIGIN,
        )


def _only_cause(config: Config, source_id: str) -> str:
    entry = _reconciliation(config)[_by_source_id(config)[source_id]]
    assert entry.unreconciled_count == 1, (
        f"expected exactly one unreconciled interval to attribute, got "
        f"{entry.unreconciled_count}; the cause asserted below would be arbitrary"
    )
    return entry.unreconciled[0].cause


def test_an_interval_the_feed_never_reached_is_a_coverage_gap(one_connection: Config) -> None:
    """The balance moved and the transactions feed holds nothing for the account."""
    _capture(one_connection, [_account(CHECKING, "depository", "checking", "100.00")], day_offset=0)
    _capture(one_connection, [_account(CHECKING, "depository", "checking", "75.00")], day_offset=10)

    assert _only_cause(one_connection, CHECKING) == "coverage_gap"


def test_an_interval_beginning_before_the_granted_history_is_window_truncated(
    one_connection: Config,
) -> None:
    """🔴 The narrower explanation wins over `coverage_gap`, and that is the point.

    Both describe transactions the store does not hold. `window_truncated` names
    the REASON -- the aggregator never granted history that far back, so the rows
    were never fetchable. Testing the general cause first would absorb every
    truncated interval into `coverage_gap` and this distinction would never once
    appear in an answer, which is what makes a cause vocabulary worthless.
    """
    _capture(one_connection, [_account(CHECKING, "depository", "checking", "100.00")], day_offset=0)
    _capture(one_connection, [_account(CHECKING, "depository", "checking", "75.00")], day_offset=10)
    # Granted from day 5: the interval opens two days before the feed could reach.
    _set_history_start(one_connection, day_offset=5)

    assert _only_cause(one_connection, CHECKING) == "window_truncated", (
        "an interval opening before the granted history was attributed to the general cause; "
        "the specific one must be tried first or it can never fire"
    )


def test_a_residual_inside_full_coverage_is_unexplained(one_connection: Config) -> None:
    """🔴 The finding this whole surface exists to report.

    The interval sits wholly inside the account's recorded transaction span and
    inside the granted history, so nothing about coverage explains it: money
    moved and the store holds no record of it.
    """
    _capture(one_connection, [_account(CHECKING, "depository", "checking", "100.00")], day_offset=0)
    _capture(one_connection, [_account(CHECKING, "depository", "checking", "75.00")], day_offset=10)
    # Transactions on both sides of the closing snapshot, so the feed demonstrably
    # covers the interval -- the residual cannot be blamed on its reach.
    _post(one_connection, [(CHECKING, 5, "10.00"), (CHECKING, 12, "5.00")])
    _set_history_start(one_connection, day_offset=0)

    assert _only_cause(one_connection, CHECKING) == "unexplained"


def test_every_declared_cause_is_one_the_producer_can_actually_attribute(
    one_connection: Config,
) -> None:
    """The positive control over the vocabulary. A cause nothing emits is a lie.

    🔴 This is the guard that would have caught `pending_holds`: it was declared
    as a cause while no code path could reach it, and a declared-but-unreachable
    cause is indistinguishable from one that is merely rare.
    """
    reachable: set[str] = set()

    # `coverage_gap` -- balance moves, feed holds nothing.
    _capture(one_connection, [_account(CHECKING, "depository", "checking", "100.00")], day_offset=0)
    _capture(one_connection, [_account(CHECKING, "depository", "checking", "75.00")], day_offset=10)
    reachable.add(_only_cause(one_connection, CHECKING))

    # `window_truncated` -- the same interval, now opening before the grant.
    _set_history_start(one_connection, day_offset=5)
    reachable.add(_only_cause(one_connection, CHECKING))

    # `unexplained` -- covered on both sides, granted from the start.
    _set_history_start(one_connection, day_offset=0)
    _post(one_connection, [(CHECKING, 5, "10.00"), (CHECKING, 12, "5.00")])
    reachable.add(_only_cause(one_connection, CHECKING))

    assert reachable == set(query.RESIDUAL_CAUSES), (
        f"the declared cause vocabulary is {sorted(query.RESIDUAL_CAUSES)} but only "
        f"{sorted(reachable)} were reached. A cause no branch can attribute is one a consumer "
        f"will never see and cannot be told apart from a rare one"
    )


# --------------------------------------------------------------------------- #
# Sparse snapshots, and the invariant itself.
# --------------------------------------------------------------------------- #


def test_an_interval_spanning_many_days_is_not_itself_a_finding(
    one_connection: Config,
) -> None:
    """🔴 Snapshots are sparse BY DESIGN and a missing day is not a defect.

    The deriver writes no `balances_daily` row for a day the aggregator reported
    a null `current`, so an interval can span weeks. A reader that assumed daily
    rows would report every sparse stretch as a finding -- and this store's real
    captures are far from daily.
    """
    _capture(one_connection, [_account(CHECKING, "depository", "checking", "100.00")], day_offset=0)
    unreported = _account(CHECKING, "depository", "checking", "100.00")
    unreported["balances"]["current"] = None
    for missing in range(1, 40):
        _capture(one_connection, [unreported], day_offset=missing)
    _capture(one_connection, [_account(CHECKING, "depository", "checking", "60.00")], day_offset=40)
    _post(one_connection, [(CHECKING, 20, "40.00")])

    account_id = _by_source_id(one_connection)[CHECKING]
    assert _snapshot_count(one_connection, account_id) == 2, (
        "the null-balance captures wrote rows after all, so this is not the sparse case"
    )
    entry = _reconciliation(one_connection)[account_id]
    assert entry.intervals_compared == 1, (
        "39 missing days broke the pairing into more than one interval; a run of days with no "
        "row must WIDEN an interval, never split one"
    )
    assert entry.residual_minor_units == 0
    assert entry.unreconciled_count == 0


def test_a_transaction_on_the_opening_date_is_not_counted_twice(
    one_connection: Config,
) -> None:
    """🔴 `(from, to]` -- half-open, closed at the top, and both edges matter.

    A transaction posted ON the opening snapshot's date is already inside that
    snapshot's balance. Counting it again would double it into the very interval
    its effect has already left, and the residual would be wrong by its amount in
    the direction that looks like a real finding.
    """
    _capture(one_connection, [_account(CHECKING, "depository", "checking", "100.00")], day_offset=0)
    _capture(one_connection, [_account(CHECKING, "depository", "checking", "90.00")], day_offset=10)
    # One on the opening date (already in the 100.00) and one inside the interval.
    _post(one_connection, [(CHECKING, 0, "25.00"), (CHECKING, 5, "10.00")])

    entry = _reconciliation(one_connection)[_by_source_id(one_connection)[CHECKING]]
    assert entry.intervals_compared == 1
    assert entry.residual_minor_units == 0, (
        "the opening date's transaction was counted inside the interval, so the sum is wrong "
        "by its amount -- `(from, to]` excludes it because the opening BALANCE already holds it"
    )


def test_a_transaction_on_the_closing_date_is_counted(one_connection: Config) -> None:
    """The mirror of the rule above: the closing date's effect is in the closing balance."""
    _capture(one_connection, [_account(CHECKING, "depository", "checking", "100.00")], day_offset=0)
    _capture(one_connection, [_account(CHECKING, "depository", "checking", "90.00")], day_offset=10)
    _post(one_connection, [(CHECKING, 10, "10.00")])

    entry = _reconciliation(one_connection)[_by_source_id(one_connection)[CHECKING]]
    assert entry.residual_minor_units == 0, (
        "a transaction posted ON the closing date was excluded, but the closing balance "
        "already reflects it, so the residual wrongly reports its whole amount"
    )


@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    movements=st.lists(
        st.tuples(
            st.integers(min_value=1, max_value=29),
            # Both directions, and never zero: a zero movement cannot tell a
            # correct sum from one that dropped the row.
            st.integers(min_value=1, max_value=50_000).flatmap(
                lambda cents: st.sampled_from([cents, -cents])
            ),
        ),
        min_size=1,
        max_size=8,
    ),
    liability=st.booleans(),
)
def test_the_residual_is_zero_whenever_the_transactions_are_the_balance_movement(
    one_connection: Config, movements: list[tuple[int, int]], liability: bool
) -> None:
    """🔴 The invariant, not enumerated cases. The invariant IS the feature.

    `learnings.md` § *Guarantees by construction*: assert the invariant rather
    than instances -- the window-resolver's property test found a bug nine
    enumerated mutations missed, and this is the same shape. For ANY account
    class, ANY signs, and ANY number of movements, an interval whose transactions
    are exactly its balance movement must reconcile to zero.

    The balance is DERIVED from the movements rather than stated alongside them,
    so the premise cannot drift from the assertion: whatever hypothesis generates,
    the closing balance is by construction the opening balance plus the sum.
    """
    account_type, subtype = ("credit", "credit card") if liability else ("depository", "checking")
    # 🔴 A fresh account per example, and this is not cosmetic. Hypothesis reuses
    # one function-scoped fixture across every example it generates, so a shared
    # account would accumulate the previous examples' transactions and the
    # property would be asserted against a ledger it never generated. The first
    # run of this test failed exactly that way -- a one-cent movement reporting a
    # sum of 30,608 -- which is the accumulation, not the arithmetic.
    source_id = f"acct-pbt-{uuid4().hex[:12]}"
    opening_cents = 100_000
    # 🔴 The two account classes move in OPPOSITE directions as the aggregator
    # reports them, and that is exactly what the operator-signed norm exists to
    # absorb. `amount` is positive for money out. An asset's reported balance
    # FALLS by that; a liability's reported balance is an amount OWED, so it
    # RISES by it. The deriver negates the liability on the way in, after which a
    # single subtraction serves both -- which is the norm's stated Why, and this
    # is the assertion that holds it to it. The premise is written here in the
    # aggregator's terms so the test cannot silently adopt the producer's.
    total_out = sum(cents for _, cents in movements)
    closing_cents = opening_cents + total_out if liability else opening_cents - total_out

    _capture(
        one_connection,
        [_account(source_id, account_type, subtype, f"{opening_cents / 100:.2f}")],
        day_offset=0,
    )
    _capture(
        one_connection,
        [_account(source_id, account_type, subtype, f"{closing_cents / 100:.2f}")],
        day_offset=30,
    )
    _post(
        one_connection,
        [(source_id, day, f"{cents / 100:.2f}") for day, cents in movements],
        # Unique per example for the reason the account is: a repeated
        # `transaction_id` is an UPDATE to the sync deriver, not a second row.
        cursor=source_id,
    )

    account_id = _by_source_id(one_connection)[source_id]
    assert _snapshot_count(one_connection, account_id) == 2, "no interval; the property is vacuous"
    entry = _reconciliation(one_connection)[account_id]
    assert entry.intervals_compared == 1
    assert entry.residual_minor_units == 0, (
        f"transactions that ARE the balance movement left a residual of "
        f"{entry.residual_minor_units}; the identity AC-11.2 asserts does not hold"
    )


# --------------------------------------------------------------------------- #
# The wire: `get_coverage_report`, its warnings, and its row schema.
# --------------------------------------------------------------------------- #


def _details(answer: envelope.Answer, kind: str) -> list[str]:
    return [w.detail for w in answer.warnings if w.kind == kind]


def test_the_coverage_report_carries_a_residual_per_account_with_its_cause(
    one_connection: Config,
) -> None:
    """The vertical slice, end to end: two tables through the producer to the row."""
    _capture(
        one_connection,
        [
            _account(CHECKING, "depository", "checking", "100.00"),
            _account(BROKERAGE, "investment", "brokerage", "5000.00"),
        ],
        day_offset=0,
    )
    _capture(
        one_connection,
        [
            _account(CHECKING, "depository", "checking", "75.00"),
            _account(BROKERAGE, "investment", "brokerage", "5400.00"),
        ],
        day_offset=10,
    )
    _post(one_connection, [(CHECKING, 5, "10.00"), (CHECKING, 12, "5.00")])
    _set_history_start(one_connection, day_offset=0)

    answer = query.coverage_report(one_connection)
    rows = {int(row["account_id"]): row for row in answer.rows}
    ids = _by_source_id(one_connection)
    assert set(ids.values()) <= set(rows), "an account is missing from the report entirely"

    checking = rows[ids[CHECKING]]
    assert checking["reconciliation_state"] == "reconciled"
    assert checking["residual_minor_units"] == -1500
    assert checking["reconciled_intervals"] == 1
    assert checking["unreconciled_intervals"] == 1
    assert checking["balance_currency"] == "USD"
    (detail,) = checking["unreconciled_detail"]
    assert detail["cause"] == "unexplained"
    assert detail["residual_minor_units"] == -1500
    assert detail["balance_change_minor_units"] == -2500
    assert detail["transactions_sum_minor_units"] == -1000

    # 🔴 Present and null beside a state that says why -- never absent.
    brokerage = rows[ids[BROKERAGE]]
    assert brokerage["reconciliation_state"] == "not_applicable_investment"
    assert brokerage["residual_minor_units"] is None
    assert brokerage["unreconciled_detail"] == []


def test_an_unexplained_residual_raises_the_warning_with_its_magnitude(
    one_connection: Config,
) -> None:
    """🔴 The answer is internally consistent and wrong by the amount named.

    Nothing else on any surface can say that, which is why the kind exists at all
    rather than reusing one of the absence-shaped ones.
    """
    _capture(one_connection, [_account(CHECKING, "depository", "checking", "100.00")], day_offset=0)
    _capture(one_connection, [_account(CHECKING, "depository", "checking", "75.00")], day_offset=10)
    _post(one_connection, [(CHECKING, 5, "10.00"), (CHECKING, 12, "5.00")])

    answer = query.coverage_report(one_connection)
    (detail,) = _details(answer, "balance_unreconciled")
    assert "USD" in detail, "the warning does not name a currency, so its magnitude has no unit"
    assert "-1500" in detail, (
        "the warning omits the magnitude. `learnings.md`: the magnitude is the load-bearing "
        "half, not the flag -- a count alone cannot tell a rounding artefact from a lost month"
    )


def test_an_account_that_reconciles_raises_no_unreconciled_warning(
    one_connection: Config,
) -> None:
    """The negative control. A warning asserted only where it fires says nothing
    about whether it ever stops."""
    _capture(one_connection, [_account(CHECKING, "depository", "checking", "100.00")], day_offset=0)
    _capture(one_connection, [_account(CHECKING, "depository", "checking", "90.00")], day_offset=10)
    _post(one_connection, [(CHECKING, 5, "10.00")])

    answer = query.coverage_report(one_connection)
    assert (
        _reconciliation(one_connection)[_by_source_id(one_connection)[CHECKING]].intervals_compared
        == 1
    ), "nothing was compared, so the silence below is vacuous"
    assert not _details(answer, "balance_unreconciled")


def test_an_investment_account_is_named_in_a_warning_rather_than_silently_missing(
    one_connection: Config,
) -> None:
    """🔴 A consumer that cannot see WHY an account is missing from a verification
    surface reads the surface as having checked it."""
    _capture(
        one_connection, [_account(BROKERAGE, "investment", "brokerage", "5000.00")], day_offset=0
    )
    _capture(
        one_connection, [_account(BROKERAGE, "investment", "brokerage", "5400.00")], day_offset=10
    )

    answer = query.coverage_report(one_connection)
    (detail,) = _details(answer, "reconciliation_not_applicable")
    account_id = _by_source_id(one_connection)[BROKERAGE]
    assert str(account_id) in detail, "the warning does not name the account it is about"
    # Deliberately NOT `rule-applied`: that kind is about rows excluded from a
    # figure, this one about an account that cannot be verified at all.
    assert not _details(answer, "rule-applied")


def test_both_new_kinds_are_declared_request_scoped(one_connection: Config) -> None:
    """🔴 A kind emitted but not declared is refused by the schema validator,
    which takes the WHOLE answer down rather than degrading it.

    Request-scoped for both, because each fires only when this request's scope
    holds such an account -- so their absence is information, which is the entire
    guarantee the two tuples exist to make.
    """
    for kind in ("balance_unreconciled", "reconciliation_not_applicable"):
        assert kind in envelope.WARNING_KINDS, f"{kind} is emitted but not in the vocabulary"
        assert kind in envelope.REQUEST_SCOPED_KINDS, (
            f"{kind} is declared connection-scoped, which promises it rides EVERY response "
            f"equally. It cannot, so a caller could no longer read the ABSENCE of any kind "
            f"in either set as information"
        )
        assert kind not in envelope.CONNECTION_SCOPED_KINDS


def test_each_transaction_lands_in_exactly_one_of_several_consecutive_intervals(
    one_connection: Config,
) -> None:
    """🔴 Multi-interval, because that is the only place a cursor bug can hide.

    The producer walks each account's movements ONCE across all of its intervals
    rather than re-filtering the list per interval. A cursor left behind folds an
    earlier interval's transactions into a later one's sum, and a cursor advanced
    too far drops them: either way both intervals are wrong and their residuals
    are equal and opposite, which looks like a real finding on both. A
    single-interval test cannot see any of it.

    Each interval here is independently exact, so a leak in either direction
    shows up as a nonzero residual on the pair it moved between.
    """
    balances = ["100.00", "90.00", "65.00", "60.00", "20.00"]
    for index, amount in enumerate(balances):
        _capture(
            one_connection,
            [_account(CHECKING, "depository", "checking", amount)],
            day_offset=index * 10,
        )
    # One movement inside each of the four intervals, exactly matching it, and
    # two of them ON an interval boundary -- the edge `(from, to]` decides.
    _post(
        one_connection,
        [
            (CHECKING, 5, "10.00"),
            (CHECKING, 20, "25.00"),
            (CHECKING, 25, "5.00"),
            (CHECKING, 40, "40.00"),
        ],
    )

    account_id = _by_source_id(one_connection)[CHECKING]
    assert _snapshot_count(one_connection, account_id) == len(balances)
    entry = _reconciliation(one_connection)[account_id]
    assert entry.intervals_compared == len(balances) - 1, (
        "the pairing did not produce one interval per consecutive snapshot pair"
    )
    assert entry.unreconciled_count == 0, (
        f"a transaction was counted into the wrong interval: {entry.unreconciled}"
    )
    assert entry.residual_minor_units == 0
