"""`balance_history`: one series, read per account and as net worth.

🔴 **Balances are written by the sync's own accounts deriver from a recorded
capture**, never typed into `balances_daily` here. The sign a liability is
stored with, and the class an account is given, are the deriver's decisions,
and a row written by hand would carry this file's beliefs about both. Expected
figures are read back from the store the deriver wrote, so the tool is compared
against a second, independent reading of the same series.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, insert, select, update

from bankmachine import envelope, mcp, query, query_balances
from bankmachine.config import Config
from bankmachine.connector import ACCOUNTS_GET, INVESTMENTS_HOLDINGS_GET
from bankmachine.derivers import ALL_DERIVERS
from bankmachine.store.derivation import apply_response
from bankmachine.store.engine import reader_connection, writer_connection
from bankmachine.store.schema import (
    accounts,
    balances_daily,
    connections,
    holdings,
    institutions,
)
from bankmachine.store.types import CalendarDate, UtcInstant, calendar_date, utc_instant

FIXTURES = Path(__file__).parent / "connector" / "fixtures"

DAY1 = utc_instant(datetime(2026, 9, 1, 14, 0, tzinfo=UTC))


def _day(n: int) -> UtcInstant:
    """The capture instant `n` days after DAY1."""
    return utc_instant(DAY1 + timedelta(days=n))


#: The keys every row must carry, read off the published schema.
ROW_KEYS = frozenset(
    next(d for d in mcp._tool_definitions() if d["name"] == "balance_history")["outputSchema"][
        "properties"
    ]["rows"]["items"]["properties"]
)


def _recorded(subtype: str) -> dict[str, Any]:
    """One account from the recorded capture, chosen by subtype."""
    body: dict[str, Any] = json.loads((FIXTURES / "accounts_get.json").read_bytes())
    chosen: dict[str, Any] = copy.deepcopy(
        next(a for a in body["accounts"] if a["subtype"] == subtype)
    )
    return chosen


def _account(subtype: str, current: float, *, suffix: str) -> dict[str, Any]:
    """That account as a capture reports it, with its own balance and a distinct id."""
    entry = _recorded(subtype)
    entry["account_id"] = f"{entry['account_id']}-{suffix}"
    entry["balances"]["current"] = current
    return entry


def _enroll(config: Config, connection_id: int) -> None:
    item = f"item-history-{connection_id}"
    with writer_connection(config) as conn:
        primary_key = conn.execute(
            insert(institutions).values(
                source_institution_id=f"ins_history_{connection_id}",
                name=f"Example Bank {connection_id}",
                first_seen_at=DAY1,
                last_seen_at=DAY1,
            )
        ).inserted_primary_key
        assert primary_key is not None  # an INTEGER PRIMARY KEY insert always yields one
        conn.execute(
            insert(connections).values(
                connection_id=connection_id,
                institution_id=primary_key[0],
                source_connection_id=item,
                credential_ref=f"connection:sandbox:{item}",
                capabilities="[]",
                status="active",
                enrolled_at=DAY1,
                created_at=DAY1,
                updated_at=DAY1,
            )
        )


def _capture(
    config: Config, connection_id: int, at: UtcInstant, listed: list[dict[str, Any]]
) -> None:
    """One `/accounts/get` capture of a connection, through the shipped derivers."""
    body = {"accounts": listed, "item": {"item_id": f"item-history-{connection_id}"}}
    with writer_connection(config) as conn:
        apply_response(
            conn,
            connection_id=connection_id,
            endpoint=ACCOUNTS_GET.path,
            body=json.dumps(body).encode(),
            received_at=at,
            derivers=ALL_DERIVERS,
        )


def _first_connection(checking: float = 110.25, card: float = 410.0) -> list[dict[str, Any]]:
    """An asset and a liability on connection 1."""
    return [
        _account("checking", checking, suffix="c1"),
        _account("credit card", card, suffix="c1"),
    ]


def _second_connection(savings: float = 210.0) -> list[dict[str, Any]]:
    return [_account("savings", savings, suffix="c2")]


@pytest.fixture
def two_connections(initialized_config: Config) -> Config:
    _enroll(initialized_config, 1)
    _enroll(initialized_config, 2)
    return initialized_config


def _ids_on(config: Config, connection_id: int) -> set[int]:
    with reader_connection(config) as conn:
        ids = {
            int(row[0])
            for row in conn.execute(
                select(accounts.c.account_id).where(accounts.c.connection_id == connection_id)
            ).all()
        }
    assert ids, f"connection {connection_id} holds no account, so nothing is under test"
    return ids


def _stored(config: Config) -> list[tuple[int, str, int, str, str]]:
    """Every balance the deriver wrote: (account, day, signed minor units, currency, class)."""
    with reader_connection(config) as conn:
        found = [
            (int(r[0]), str(r[1]), int(r[2]), str(r[3]), str(r[4]))
            for r in conn.execute(
                select(
                    balances_daily.c.account_id,
                    balances_daily.c.as_of_date,
                    balances_daily.c.current_minor,
                    balances_daily.c.currency,
                    accounts.c.balance_class,
                ).select_from(balances_daily.join(accounts))
            ).all()
        ]
    assert found, "the capture wrote no balance, so nothing is under test"
    return found


def _net_worth(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The net-worth rows, by date."""
    return {row["date"]: row for row in rows if row["account_id"] is None}


def _details(answer: envelope.Answer, kind: str) -> list[str]:
    return [w.detail for w in answer.warnings if w.kind == kind]


# --------------------------------------------------------------------------
# The acceptance criteria
# --------------------------------------------------------------------------


def test_every_capture_comes_back_as_an_account_row_split_by_its_class(
    two_connections: Config,
) -> None:
    """AC: the per-account series, compared against the balances the deriver stored.

    Both classes are in the capture, so a split that ignored the class could not
    pass by coincidence; every row carries exactly the published keys.
    """
    _capture(two_connections, 1, _day(0), _first_connection())
    _capture(two_connections, 1, _day(1), _first_connection(checking=150.0, card=380.0))
    stored = _stored(two_connections)
    assert {entry[4] for entry in stored} == {"asset", "liability"}

    rows = query_balances.balance_history(two_connections).rows

    assert all(set(row) == ROW_KEYS for row in rows)
    assert sorted(
        (
            row["account_id"],
            row["date"],
            row["assets_minor_units"],
            row["liabilities_minor_units"],
            row["net_minor_units"],
            row["currency"],
        )
        for row in rows
        if row["account_id"] is not None
    ) == sorted(
        (
            account_id,
            day,
            current if balance_class == "asset" else 0,
            -current if balance_class == "liability" else 0,
            current,
            currency,
        )
        for account_id, day, current, currency, balance_class in stored
    )


def test_a_complete_days_net_worth_row_is_the_sum_of_its_account_rows(
    two_connections: Config,
) -> None:
    """AC: both readings come from one series and agree, day by day."""
    for n in (0, 1):
        _capture(two_connections, 1, _day(n), _first_connection(checking=100.0 + n))
        _capture(two_connections, 2, _day(n), _second_connection(savings=200.0 + n))

    rows = query_balances.balance_history(two_connections).rows
    net_worth = _net_worth(rows)

    assert set(net_worth) == {_day(0).date().isoformat(), _day(1).date().isoformat()}
    for day, total in net_worth.items():
        day_rows = [r for r in rows if r["date"] == day and r["account_id"] is not None]
        assert len(day_rows) == 3
        for field in ("assets_minor_units", "liabilities_minor_units", "net_minor_units"):
            assert total[field] == sum(r[field] for r in day_rows), (day, field)
        assert total["net_minor_units"] == (
            total["assets_minor_units"] - total["liabilities_minor_units"]
        )


def test_a_day_nobody_captured_is_absent_at_both_levels(two_connections: Config) -> None:
    """AC: a day with no capture is visible as absent, never smoothed across."""
    for n in (0, 2):
        _capture(two_connections, 1, _day(n), _first_connection())
        _capture(two_connections, 2, _day(n), _second_connection())

    answer = query_balances.balance_history(two_connections)

    assert _day(1).date().isoformat() not in {row["date"] for row in answer.rows}
    assert set(_net_worth(answer.rows)) == {
        _day(0).date().isoformat(),
        _day(2).date().isoformat(),
    }
    assert _details(answer, "rule-applied") == [], "a day nobody looked at was called withheld"


# --------------------------------------------------------------------------
# A net worth only for a complete day
# --------------------------------------------------------------------------


def test_a_day_one_connection_missed_has_account_rows_and_no_net_worth_row(
    two_connections: Config,
) -> None:
    """🔴 The partial day: a net worth summed without the missed account is a wrong figure."""
    for n in (0, 1, 2):
        _capture(two_connections, 1, _day(n), _first_connection())
    for n in (0, 2):
        _capture(two_connections, 2, _day(n), _second_connection())
    missed = _day(1).date().isoformat()

    answer = query_balances.balance_history(two_connections)

    assert missed not in _net_worth(answer.rows)
    assert {r["account_id"] for r in answer.rows if r["date"] == missed} == _ids_on(
        two_connections, 1
    ), "the captured accounts' own rows for the partial day were dropped"
    details = _details(answer, "rule-applied")
    assert len(details) == 1
    for account_id in _ids_on(two_connections, 2):
        assert f"account {account_id} on 1 day(s) between {missed} and {missed}" in details[0]


def test_a_connection_that_stopped_syncing_withholds_every_net_worth_since(
    two_connections: Config,
) -> None:
    """🔴 An ACTIVE account's span has no end.

    Connection 2 last synced on day 0 and its accounts are still listed. Ending
    their span at that last capture would drop them out of days 1 and 2 and serve
    a net worth that is quietly short by their balance -- the failure the
    complete-day rule exists to refuse.
    """
    for n in (0, 1, 2):
        _capture(two_connections, 1, _day(n), _first_connection())
    _capture(two_connections, 2, _day(0), _second_connection())

    answer = query_balances.balance_history(two_connections)

    assert set(_net_worth(answer.rows)) == {_day(0).date().isoformat()}
    details = _details(answer, "rule-applied")
    assert len(details) == 1
    for account_id in _ids_on(two_connections, 2):
        assert (
            f"account {account_id} on 2 day(s) between {_day(1).date().isoformat()} and "
            f"{_day(2).date().isoformat()}"
        ) in details[0]


def _in_cad(entry: dict[str, Any]) -> dict[str, Any]:
    entry["balances"]["iso_currency_code"] = "CAD"
    entry["balances"]["unofficial_currency_code"] = None
    return entry


def test_a_currency_whose_every_account_missed_a_day_is_withheld_and_named(
    two_connections: Config,
) -> None:
    """🔴 Completeness is judged for every currency net worth counts, on every captured day.

    Connection 2 holds the only CAD account and missed day 1 while connection 1's
    USD accounts were captured. Judging only the (day, currency) pairs that hold a
    capture finds no CAD pair on day 1, and its net worth goes silently absent.
    """
    _capture(two_connections, 1, _day(0), _first_connection())
    _capture(two_connections, 2, _day(0), [_in_cad(_account("savings", 210.0, suffix="c2"))])
    _capture(two_connections, 1, _day(1), _first_connection())

    answer = query_balances.balance_history(two_connections)

    first, second = _day(0).date().isoformat(), _day(1).date().isoformat()
    assert sorted((r["date"], r["currency"]) for r in answer.rows if r["account_id"] is None) == [
        (first, "CAD"),
        (first, "USD"),
        (second, "USD"),
    ]
    details = _details(answer, "rule-applied")
    assert len(details) == 1, "the CAD net worth for the day it missed was not named"
    (cad,) = _ids_on(two_connections, 2)
    assert f"account {cad} on 1 day(s) between {second} and {second}" in details[0]


def test_an_account_first_captured_later_does_not_withhold_the_days_before_it(
    two_connections: Config,
) -> None:
    """An account counts from its first capture; before that it was not part of net worth."""
    for n in (0, 1):
        _capture(two_connections, 1, _day(n), _first_connection())
    _capture(two_connections, 2, _day(1), _second_connection())

    answer = query_balances.balance_history(two_connections)

    assert set(_net_worth(answer.rows)) == {
        _day(0).date().isoformat(),
        _day(1).date().isoformat(),
    }
    assert _details(answer, "rule-applied") == []


def test_an_account_no_longer_listed_counts_only_through_its_last_capture_and_is_named(
    two_connections: Config,
) -> None:
    """A non-active account stops counting after its last capture, and the answer says so."""
    _capture(two_connections, 1, _day(0), _first_connection())
    _capture(two_connections, 1, _day(1), _first_connection()[:1])
    with reader_connection(two_connections) as conn:
        dropped = int(
            conn.execute(
                select(accounts.c.account_id).where(
                    accounts.c.source_account_id == _first_connection()[1]["account_id"]
                )
            ).scalar_one()
        )

    answer = query_balances.balance_history(two_connections)

    second = _day(1).date().isoformat()
    assert second in _net_worth(answer.rows), "a no-longer-listed account withheld a net worth"
    only = [r for r in answer.rows if r["date"] == second and r["account_id"] is not None]
    assert _net_worth(answer.rows)[second]["net_minor_units"] == only[0]["net_minor_units"]
    assert _details(answer, "rule-applied") == []
    named = _details(answer, "account_no_longer_active")
    assert len(named) == 1 and f"account(s) {dropped} " in named[0]
    # 🔴 The magnitude, not only the flag: the day it stopped counting and the
    # balance that stopped, read back from what the deriver stored.
    first = _day(0).date().isoformat()
    (last,) = [minor for held, day, minor, _, _ in _stored(two_connections) if held == dropped]
    assert f"account {dropped} through {first}, last balance {last} USD" in named[0]
    assert f"1 account(s) whose last balances sum to {last} USD" in named[0], (
        "what stopped counting went unstated"
    )
    assert answer.coverage["not_active_balance_minor_units"] == [
        {"currency": "USD", "current_minor_units": last}
    ], "the figure the warning names and the envelope's figure disagree"


# --------------------------------------------------------------------------
# The split is by class, never by sign
# --------------------------------------------------------------------------


def test_a_balance_is_split_by_its_accounts_class_never_by_its_sign(
    two_connections: Config,
) -> None:
    """🔴 An overdraft is negative ASSETS; a card in credit is negative LIABILITIES."""
    _capture(two_connections, 1, _day(0), _first_connection(checking=-20.0, card=-50.0))
    stored = {entry[4]: entry for entry in _stored(two_connections)}
    assert stored["asset"][2] < 0 and stored["liability"][2] > 0, (
        "the capture did not produce an overdraft and a card in credit, so nothing is under test"
    )

    rows = {
        r["account_id"]: r
        for r in query_balances.balance_history(two_connections).rows
        if r["account_id"] is not None
    }

    overdraft, in_credit = rows[stored["asset"][0]], rows[stored["liability"][0]]
    assert (overdraft["assets_minor_units"], overdraft["liabilities_minor_units"]) == (
        stored["asset"][2],
        0,
    )
    assert (in_credit["assets_minor_units"], in_credit["liabilities_minor_units"]) == (
        0,
        -stored["liability"][2],
    )


_CLASSES = st.sampled_from(["asset", "liability"])
_CURRENCIES = st.sampled_from(["USD", "EUR"])
_START = date(2026, 9, 1)


@given(data=st.data())
def test_both_readings_agree_over_any_series(data: st.DataObject) -> None:
    """🔴 The invariants, over generated series rather than instances this file thought of.

    For every row `net = assets - liabilities`, and an account row's split is
    degenerate. A net-worth row exists for a (day, currency) exactly when every
    account counted in it that day was captured, and then equals the sum of that
    day's account rows; otherwise the day is withheld naming exactly the missing
    accounts. Completeness is re-derived here from the definition, not from the
    code's expression of it.
    """
    shape = data.draw(
        st.lists(st.tuples(_CLASSES, _CURRENCIES, st.booleans()), min_size=1, max_size=5)
    )
    captures: list[query_balances.BalanceCapture] = []
    for account_id, (balance_class, currency, _active) in enumerate(shape, start=1):
        for offset in sorted(data.draw(st.sets(st.integers(0, 6), max_size=7))):
            captures.append(
                query_balances.BalanceCapture(
                    account_id=account_id,
                    day=calendar_date(_START + timedelta(days=offset)),
                    current_minor=data.draw(st.integers(-(10**9), 10**9)),
                    currency=currency,
                    balance_class=balance_class,
                )
            )
    counted: dict[tuple[int, str], tuple[CalendarDate, CalendarDate | None]] = {}
    for account_id, (_class, currency, active) in enumerate(shape, start=1):
        days = [c.day for c in captures if c.account_id == account_id]
        if days:
            counted[(account_id, currency)] = (min(days), None if active else max(days))

    rows, withheld = query_balances.compose_balance_series(
        captures, counted=counted, aggregate=True
    )

    positions = [position for position, _ in rows]
    assert positions == sorted(positions) and len(set(positions)) == len(positions)
    wire = [row for _, row in rows]
    for row in wire:
        assert row["net_minor_units"] == row["assets_minor_units"] - row["liabilities_minor_units"]
    account_rows = [row for row in wire if row["account_id"] is not None]
    assert len(account_rows) == len(captures)
    assert all(
        r["assets_minor_units"] == 0 or r["liabilities_minor_units"] == 0 for r in account_rows
    )
    withheld_at = {(w.day.isoformat(), w.currency): set(w.missing) for w in withheld}
    for day, currency in {(c.day, c.currency) for c in captures}:
        present = {c.account_id for c in captures if c.day == day and c.currency == currency}
        expected_missing = {
            account_id
            for (account_id, counted_in), (first, last) in counted.items()
            if counted_in == currency and first <= day <= (last or date.max)
        } - present
        totals = [
            r
            for r in wire
            if r["account_id"] is None
            and r["date"] == day.isoformat()
            and r["currency"] == currency
        ]
        if expected_missing:
            assert totals == [] and withheld_at[(day.isoformat(), currency)] == expected_missing
            continue
        assert len(totals) == 1 and (day.isoformat(), currency) not in withheld_at
        same_day = [
            r for r in account_rows if r["date"] == day.isoformat() and r["currency"] == currency
        ]
        for field in ("assets_minor_units", "liabilities_minor_units", "net_minor_units"):
            assert totals[0][field] == sum(r[field] for r in same_day)
    assert len(wire) == len(captures) + sum(1 for r in wire if r["account_id"] is None), (
        "a net-worth row appeared for a day with no capture in its currency"
    )


# --------------------------------------------------------------------------
# Scope, window and pages
# --------------------------------------------------------------------------


def test_narrowed_to_one_account_the_answer_is_its_series_and_no_net_worth_row(
    two_connections: Config,
) -> None:
    for n in (0, 1):
        _capture(two_connections, 1, _day(n), _first_connection())
    _capture(two_connections, 2, _day(0), _second_connection())
    chosen = min(_ids_on(two_connections, 1))

    answer = query_balances.balance_history(two_connections, account_id=chosen)

    assert {r["account_id"] for r in answer.rows} == {chosen}
    assert len(answer.rows) == 2
    assert _details(answer, "rule-applied") == []


def test_an_account_that_does_not_exist_is_refused(two_connections: Config) -> None:
    with pytest.raises(query.UnknownAccountError, match="account_id 999"):
        query_balances.balance_history(two_connections, account_id=999)


def test_the_window_is_clamped_to_the_days_balances_were_captured(
    two_connections: Config,
) -> None:
    """🔴 Against the BALANCE series' span, never the transactions'.

    This store holds no transaction at all, so a window reconciled against
    transactions would cover nothing; reconciled against balances it begins on
    the first capture and says so.
    """
    first = _day(0).date().isoformat()
    for n in (0, 1):
        _capture(two_connections, 1, _day(n), _first_connection())

    answer = query_balances.balance_history(
        two_connections, since=date(2026, 1, 1), until=_day(1).date()
    )
    wire = answer.to_wire()

    assert wire["effective_window"]["effective"] == {
        "since": first,
        "until": _day(1).date().isoformat(),
    }
    starts = _details(answer, "window_starts_before_coverage")
    assert len(starts) == 1 and f"coverage begins {first}" in starts[0]
    assert "transactions_in_effective_window" not in wire["coverage"]


def test_a_window_selects_only_the_days_inside_it_and_still_judges_completeness_store_wide(
    two_connections: Config,
) -> None:
    """An account captured before the window but not inside it still counts in its net worth."""
    for n in (0, 1, 2):
        _capture(two_connections, 1, _day(n), _first_connection())
    _capture(two_connections, 2, _day(0), _second_connection())

    answer = query_balances.balance_history(
        two_connections, since=_day(1).date(), until=_day(2).date()
    )

    assert {r["date"] for r in answer.rows} == {
        _day(1).date().isoformat(),
        _day(2).date().isoformat(),
    }
    assert _net_worth(answer.rows) == {}, "a net worth was served without an account it counts"


def test_a_long_series_pages_to_every_row_exactly_once(two_connections: Config) -> None:
    """The cap, the cursor and the count, walked to the end."""
    for n in range(5):
        _capture(two_connections, 1, _day(n), _first_connection(checking=100.0 + n))
    whole = query_balances.balance_history(two_connections, limit=500)
    assert whole.truncation is not None and not whole.truncation.truncated
    assert len(whole.rows) == 15

    walked: list[dict[str, Any]] = []
    after: envelope.SeriesCursor | None = None
    for _ in range(10):
        page = query_balances.balance_history(two_connections, limit=4, after=after)
        assert page.truncation is not None
        assert page.truncation.matching == 15
        walked.extend(page.rows)
        if page.truncation.next_cursor is None:
            break
        truncated = _details(page, "rows_truncated")
        assert len(truncated) == 1 and "15 balance series rows match this request" in truncated[0]
        after = envelope.parse_series_cursor(
            page.truncation.next_cursor, since=None, until=None, account_id=None
        )

    assert walked == whole.rows
    newest = whole.rows[0]
    assert newest["date"] == _day(4).date().isoformat() and newest["account_id"] is None, (
        "the newest day's net-worth row does not lead"
    )


def test_a_two_currency_series_pages_to_every_row_exactly_once(two_connections: Config) -> None:
    """The cursor resumes on the ONE order the query sorts by, currency included.

    Two net-worth rows share each day and differ only by currency, so a page
    boundary falls between them -- the case a single-currency walk never reaches.
    """
    for n in range(3):
        _capture(two_connections, 1, _day(n), _first_connection(checking=100.0 + n))
        _capture(
            two_connections, 2, _day(n), [_in_cad(_account("savings", 210.0 + n, suffix="c2"))]
        )
    whole = query_balances.balance_history(two_connections, limit=500)
    assert {r["currency"] for r in whole.rows if r["account_id"] is None} == {"CAD", "USD"}

    for limit in (1, 2, 3):
        walked: list[dict[str, Any]] = []
        after: envelope.SeriesCursor | None = None
        for _ in range(len(whole.rows) + 1):
            page = query_balances.balance_history(two_connections, limit=limit, after=after)
            assert page.truncation is not None
            walked.extend(page.rows)
            if page.truncation.next_cursor is None:
                break
            after = envelope.parse_series_cursor(
                page.truncation.next_cursor, since=None, until=None, account_id=None
            )
        assert walked == whole.rows, f"a walk at limit {limit} did not reassemble the series"


# --------------------------------------------------------------------------
# Net worth never adds the positions that decompose a balance
# --------------------------------------------------------------------------


def _holdings_capture() -> dict[str, Any]:
    """The recorded holdings capture, with its accounts, numbers kept as text."""
    capture: dict[str, Any] = json.loads(
        (FIXTURES / "investments_holdings_get.json").read_bytes(), parse_float=str
    )
    return capture


def _positions_land(config: Config, capture: dict[str, Any], at: UtcInstant) -> None:
    with writer_connection(config) as conn:
        apply_response(
            conn,
            connection_id=1,
            endpoint=INVESTMENTS_HOLDINGS_GET.path,
            body=json.dumps(capture).encode(),
            received_at=at,
            derivers=ALL_DERIVERS,
        )


def _positions_stored(config: Config) -> int:
    with reader_connection(config) as conn:
        return int(conn.execute(select(func.count()).select_from(holdings)).scalar_one())


def test_net_worth_reads_the_balance_series_and_never_adds_the_positions_that_decompose_it(
    two_connections: Config,
) -> None:
    """🔴 The double-count guard.

    An investment account's value is already in its balance: the institution
    reports a brokerage account's `current` like any other account's. Positions
    DECOMPOSE that balance. This project once shipped an exactly-2x total with
    nothing in the payload naming it, so net worth over a store holding both is
    held to the signed sum of the balances alone, and unmoved by the positions.
    """
    capture = _holdings_capture()
    _capture(two_connections, 1, _day(0), capture["accounts"])
    before = query_balances.balance_history(two_connections).rows
    _positions_land(two_connections, capture, _day(0))
    assert _positions_stored(two_connections) == len(capture["holdings"]), (
        "the positions did not land, so nothing is under test"
    )

    after = query_balances.balance_history(two_connections).rows

    assert after == before, "positions moved a balance series they only decompose"
    (net,) = _net_worth(after).values()
    assert net["net_minor_units"] == sum(minor for _, _, minor, _, _ in _stored(two_connections))


@settings(
    max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    values=st.lists(
        st.decimals(min_value=0, max_value=10_000_000, places=2), min_size=1, max_size=13
    ),
    offset=st.integers(min_value=0, max_value=30),
)
def test_net_worth_is_the_sum_of_balances_however_many_positions_decompose_them(
    two_connections: Config, values: list[Decimal], offset: int
) -> None:
    """Each day's net worth is the signed sum of that day's balances, over any positions.

    The store accumulates across examples: positions on many days, at any values,
    for any number of securities. A holdings capture carries its accounts'
    balances as well, and those land in the series like any capture's -- so a
    capture on a new day adds that day's net worth. What must never land in it
    is the positions: every net-worth row is held to the balances stored for its
    own day, read back independently.
    """
    capture = _holdings_capture()
    with reader_connection(two_connections) as conn:
        captured = conn.execute(select(func.count()).select_from(balances_daily)).scalar_one()
    if not captured:
        _capture(two_connections, 1, _day(0), capture["accounts"])
    capture["holdings"] = capture["holdings"][: len(values)]
    for entry, value in zip(capture["holdings"], values, strict=True):
        entry["institution_value"] = str(value)

    _positions_land(two_connections, capture, _day(offset))

    stored = _stored(two_connections)
    net = _net_worth(query_balances.balance_history(two_connections).rows)
    assert net, "no net worth was served, so nothing was checked"
    for day, row in net.items():
        assert row["net_minor_units"] == sum(m for _, on, m, _, _ in stored if on == day), day


def test_a_cursor_from_the_other_series_or_another_request_is_refused() -> None:
    """Each cursor type refuses the other's, and each refuses a request it was not issued for."""
    series = envelope.SeriesCursor.issued_for(
        day=date(2026, 9, 1),
        row_account_id=None,
        currency="USD",
        since=None,
        until=None,
        account_id=None,
    ).encode()
    transactions = envelope.Cursor.issued_for(
        ledger_date=date(2026, 9, 1), transaction_id=7, since=None, until=None, account_id=None
    ).encode()

    assert envelope.parse_series_cursor(series, since=None, until=None, account_id=None)
    with pytest.raises(envelope.MalformedCursorError):
        envelope.parse_series_cursor(transactions, since=None, until=None, account_id=None)
    with pytest.raises(envelope.MalformedCursorError):
        envelope.parse_cursor(series, since=None, until=None, account_id=None)
    with pytest.raises(envelope.MalformedCursorError):
        envelope.parse_series_cursor(series, since=date(2026, 1, 1), until=None, account_id=None)


# --------------------------------------------------------------------------
# What the answer discloses, and the surface around the tool
# --------------------------------------------------------------------------


def test_an_account_in_no_known_currency_is_named_as_excluded(two_connections: Config) -> None:
    _capture(two_connections, 1, _day(0), _first_connection())
    chosen = min(_ids_on(two_connections, 1))
    with writer_connection(two_connections) as conn:
        conn.execute(update(accounts).where(accounts.c.account_id == chosen).values(currency=None))

    details = _details(query_balances.balance_history(two_connections), "rule-applied")

    assert len(details) == 1 and f"account(s) {chosen} are in no currency" in details[0]


def test_an_unreadable_store_answers_empty_with_the_windowed_capped_shape(config: Config) -> None:
    wire = query_balances.balance_history(config, since=date(2026, 1, 1)).to_wire()

    assert wire["rows"] == []
    assert [w["kind"] for w in wire["warnings"]] == ["partial"]
    assert wire["effective_window"]["effective"] == {"since": None, "until": None}
    assert wire["truncation"]["returned"] == 0
    assert "transactions_in_effective_window" not in wire["coverage"]


def test_the_strict_row_guard_refuses_an_optional_field_on_balance_history() -> None:
    definitions = copy.deepcopy(mcp._tool_definitions())
    tool = next(d for d in definitions if d["name"] == "balance_history")
    tool["outputSchema"]["properties"]["rows"]["items"]["required"].remove("account_id")

    with pytest.raises(mcp.ToolRegistrationError, match="account_id"):
        mcp._refuse_optional_row_fields(definitions)


def test_the_primer_no_longer_calls_a_balance_history_unanswerable(
    initialized_config: Config,
) -> None:
    """🔴 The unforced half: calling net worth over time unanswerable sends an agent away."""
    primer = mcp._instructions(initialized_config)
    cannot = primer[primer.index("THIS SERVER CANNOT ANSWER") :]

    assert "balance" not in cannot and "net worth" not in cannot
    assert "`balance_history`" not in cannot
