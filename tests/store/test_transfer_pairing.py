"""The pairing rule, over the properties a classification has to have.

`query._flow_class` reads `transactions.transfer_pair_id` and nothing else to
decide whether money stayed inside the household, so every guarantee the class
rests on is a guarantee about this module. None of them is visible in an
aggregate's output: a pairing that stole a leg, or that changed under replay,
produces a well-formed total that is simply wrong.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import Connection as SAConnection
from sqlalchemy import insert, select

from bankmachine.config import Config
from bankmachine.store import transfers
from bankmachine.store.engine import reader_connection, writer_connection
from bankmachine.store.schema import (
    accounts,
    connections,
    derivation_versions,
    institutions,
    raw_responses,
    transactions,
)
from bankmachine.store.types import calendar_date, now_utc

LEDGER = date(2026, 6, 10)


def _enrol(conn: SAConnection, *, count: int) -> None:
    """`count` enrolled accounts on one connection, plus one import-only account."""
    now = now_utc()
    conn.execute(
        insert(institutions).values(
            source_institution_id="ins",
            name="A Bank",
            first_seen_at=now,
            last_seen_at=now,
        )
    )
    conn.execute(
        insert(connections).values(
            institution_id=1,
            source_connection_id="item",
            credential_ref="ref",
            status="active",
            enrolled_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    for index in range(count):
        conn.execute(
            insert(accounts).values(
                institution_id=1,
                connection_id=1,
                source_account_id=f"acct-{index}",
                name=f"Account {index}",
                account_type="depository",
                balance_class="asset",
                currency="USD",
                lifecycle_status="active",
                first_seen_date=calendar_date(LEDGER),
                source="aggregator",
                created_at=now,
                updated_at=now,
            )
        )


def _row(
    conn: SAConnection,
    *,
    account_id: int,
    amount: int,
    day: int = 0,
    detailed: str = "TRANSFER_OUT_ACCOUNT_TRANSFER",
    currency: str = "USD",
) -> None:
    now = now_utc()
    ledger = calendar_date(LEDGER + timedelta(days=day))
    conn.execute(
        insert(transactions).values(
            account_id=account_id,
            source_transaction_id=f"t-{account_id}-{amount}-{day}-{detailed}",
            pending=0,
            posted_date=ledger,
            ledger_date=ledger,
            amount_minor=amount,
            currency=currency,
            description="a movement",
            source_category_detailed=detailed,
            source="aggregator",
            raw_response_id=1,
            derivation_version_id=1,
            first_seen_at=now,
            updated_at=now,
        )
    )


@pytest.fixture
def store(initialized_config: Config) -> Config:
    with writer_connection(initialized_config) as conn:
        conn.execute(
            insert(derivation_versions).values(
                version=1, description="test", first_used_at=now_utc()
            )
        )
        # 🔴 A real archive row, because the provenance CHECK requires one: an
        # aggregator row that cannot name the response it came from is a row
        # this store refuses to hold, and seeding around that would test a
        # shape the product never produces.
        conn.execute(
            insert(raw_responses).values(
                connection_id=None,
                endpoint="/transactions/sync",
                received_at=now_utc(),
                body_gzip=b"",
                body_sha256="0" * 64,
                body_bytes=0,
            )
        )
        _enrol(conn, count=3)
    return initialized_config


def _pairs(config: Config) -> dict[int, int | None]:
    with reader_connection(config) as conn:
        return {
            int(row[0]): None if row[1] is None else int(row[1])
            for row in conn.execute(
                select(transactions.c.transaction_id, transactions.c.transfer_pair_id)
            ).all()
        }


def test_two_opposite_legs_on_two_enrolled_accounts_are_paired(store: Config) -> None:
    """The ordinary case, and the only one that makes a row `internal_transfer`."""
    with writer_connection(store) as conn:
        _row(conn, account_id=1, amount=-50_00)
        _row(conn, account_id=2, amount=50_00, detailed="TRANSFER_IN_ACCOUNT_TRANSFER")
        transfers.pair_transfers(conn)

    pairs = _pairs(store)
    assert len(set(pairs.values())) == 1, "the two legs did not take the same pair id"
    assert None not in pairs.values()


def test_a_leg_matches_at_most_once_so_one_deposit_cannot_absorb_three_withdrawals(
    store: Config,
) -> None:
    """🔴 A PAIRING, not a lookup, and the difference is money.

    Under a lookup, one $500 deposit would answer three separate $500
    withdrawals, all three would classify as internal transfers, and the month
    would understate spending by $1,000 -- silently, because every row is real
    and every arithmetic step succeeds.
    """
    with writer_connection(store) as conn:
        _row(conn, account_id=1, amount=-500_00, day=0)
        _row(conn, account_id=1, amount=-500_00, day=1)
        _row(conn, account_id=1, amount=-500_00, day=2)
        _row(conn, account_id=2, amount=500_00, day=0, detailed="TRANSFER_IN_ACCOUNT_TRANSFER")
        transfers.pair_transfers(conn)

    pairs = _pairs(store)
    paired = [pid for pid in pairs.values() if pid is not None]
    assert len(paired) == 2, f"expected exactly one pair (two legs), saw {pairs}"


def test_the_nearest_date_wins_so_a_replay_reproduces_the_same_pairing(store: Config) -> None:
    """🔴 Contention resolved deterministically, or `store rebuild` disagrees with itself.

    Two candidates can answer one leg. If the winner depended on scan order, the
    same archive would produce different exclusions on different runs, and a
    classification nobody can reproduce is one nobody can audit.
    """
    with writer_connection(store) as conn:
        _row(conn, account_id=1, amount=-75_00, day=0)
        _row(conn, account_id=2, amount=75_00, day=3, detailed="TRANSFER_IN_ACCOUNT_TRANSFER")
        _row(conn, account_id=3, amount=75_00, day=1, detailed="TRANSFER_IN_ACCOUNT_TRANSFER")
        transfers.pair_transfers(conn)

    with reader_connection(store) as conn:
        by_account = {
            int(row[0]): row[1]
            for row in conn.execute(
                select(transactions.c.account_id, transactions.c.transfer_pair_id)
            ).all()
        }
    assert by_account[3] is not None, "the nearer counterparty did not win"
    assert by_account[2] is None, "the further counterparty took the leg"


def test_pairing_is_idempotent_and_recomputes_rather_than_accumulating(store: Config) -> None:
    """🔴 Run twice, same answer -- and the second run RE-pairs rather than skipping.

    Pairing only unpaired rows would let a leg matched on one page keep a worse
    counterparty forever, so an incremental sync and a rebuild over the same
    archive would disagree and the rebuild's digest guard would report a routine
    rebuild as content-changing.
    """
    with writer_connection(store) as conn:
        _row(conn, account_id=1, amount=-20_00)
        _row(conn, account_id=2, amount=20_00, detailed="TRANSFER_IN_ACCOUNT_TRANSFER")
        transfers.pair_transfers(conn)
    once = _pairs(store)

    with writer_connection(store) as conn:
        transfers.pair_transfers(conn)
    assert _pairs(store) == once


def test_a_later_page_can_move_a_leg_onto_its_nearer_counterparty(store: Config) -> None:
    """The property the recompute buys, stated as the sequence that produces it.

    The far counterparty arrives first and pairs. Then the near one lands. A
    pairing that only considered unpaired rows would leave the first match
    standing; the rebuild, which sees both at once, would choose the second --
    and the two would disagree about the same archive.
    """
    with writer_connection(store) as conn:
        _row(conn, account_id=1, amount=-30_00, day=0)
        _row(conn, account_id=2, amount=30_00, day=3, detailed="TRANSFER_IN_ACCOUNT_TRANSFER")
        transfers.pair_transfers(conn)
    with writer_connection(store) as conn:
        _row(conn, account_id=3, amount=30_00, day=1, detailed="TRANSFER_IN_ACCOUNT_TRANSFER")
        transfers.pair_transfers(conn)

    with reader_connection(store) as conn:
        by_account = {
            int(row[0]): row[1]
            for row in conn.execute(
                select(transactions.c.account_id, transactions.c.transfer_pair_id)
            ).all()
        }
    assert by_account[3] is not None, "the nearer counterparty that arrived later did not win"
    assert by_account[2] is None


def test_a_leg_outside_the_window_is_not_paired(store: Config) -> None:
    """Beyond the settlement window, two equal opposite rows are not one movement."""
    with writer_connection(store) as conn:
        _row(conn, account_id=1, amount=-40_00, day=0)
        _row(
            conn,
            account_id=2,
            amount=40_00,
            day=transfers.TRANSFER_WINDOW_DAYS + 1,
            detailed="TRANSFER_IN_ACCOUNT_TRANSFER",
        )
        transfers.pair_transfers(conn)

    assert all(pid is None for pid in _pairs(store).values())


def test_two_rows_on_one_account_are_never_paired_with_each_other(store: Config) -> None:
    """A correction against one account is not money moving between two.

    Pairing it would exclude a real reversal from every total.
    """
    with writer_connection(store) as conn:
        _row(conn, account_id=1, amount=-60_00)
        _row(conn, account_id=1, amount=60_00, detailed="TRANSFER_IN_ACCOUNT_TRANSFER")
        transfers.pair_transfers(conn)

    assert all(pid is None for pid in _pairs(store).values())


def test_a_row_a_pair_could_never_help_is_not_allowed_to_consume_one(store: Config) -> None:
    """🔴 The restriction is correctness before it is cost.

    A purchase and a refund are opposite, equal and days apart. Unrestricted
    they would take each other -- and because a leg matches at most once, a
    genuine transfer of the same amount on the same day would then find no
    counterparty and be reported as money that left the household.
    """
    with writer_connection(store) as conn:
        _row(conn, account_id=1, amount=-25_00, detailed="GENERAL_MERCHANDISE_ONLINE_MARKETPLACES")
        _row(conn, account_id=2, amount=25_00, detailed="GENERAL_MERCHANDISE_ONLINE_MARKETPLACES")
        _row(conn, account_id=1, amount=-25_00, day=1)
        _row(conn, account_id=3, amount=25_00, day=1, detailed="TRANSFER_IN_ACCOUNT_TRANSFER")
        transfers.pair_transfers(conn)

    with reader_connection(store) as conn:
        rows = conn.execute(
            select(transactions.c.source_category_detailed, transactions.c.transfer_pair_id)
        ).all()
    spend = [row[1] for row in rows if row[0].startswith("GENERAL_MERCHANDISE")]
    moved = [row[1] for row in rows if row[0].startswith("TRANSFER_")]
    assert all(pid is None for pid in spend), "a purchase/refund pair consumed a pairing"
    assert all(pid is not None for pid in moved), "the real transfer was left unpaired"


def test_a_different_currency_is_not_the_other_leg(store: Config) -> None:
    """Equal magnitudes in two units are not one movement, whatever they look like."""
    with writer_connection(store) as conn:
        _row(conn, account_id=1, amount=-10_00)
        _row(
            conn,
            account_id=2,
            amount=10_00,
            detailed="TRANSFER_IN_ACCOUNT_TRANSFER",
            currency="EUR",
        )
        transfers.pair_transfers(conn)

    assert all(pid is None for pid in _pairs(store).values())
