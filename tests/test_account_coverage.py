"""Per-account coverage: telling "no data" apart from "no activity". #19, #35, AC-9.5.

🔴 The bug this file exists to keep closed is the most plausible-looking wrong
answer this surface can give. Nine of fourteen sandbox accounts have never had a
transaction -- 82% of the balance sheet by magnitude -- and
`query_transactions(account_id=9)` answered `[]` with nothing saying so. "Am I
paying down my mortgage?" came back "no payments found", which is honest-looking
and false. Nothing about an empty list looks wrong, which is why it needs a test
rather than a reader.

The three-way distinction is one design, not three fixes, so the three cases are
asserted against ONE fixture: an account that does not exist, an account that
exists with no transactions ever, and an account with transactions that happens
to be quiet in the window asked about.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from bankmachine import query
from bankmachine.config import Config
from bankmachine.connector import ACCOUNTS_GET, TRANSACTIONS_SYNC
from bankmachine.derivers import ALL_DERIVERS
from bankmachine.store.derivation import apply_response
from bankmachine.store.engine import writer_connection
from bankmachine.store.schema import (
    PROVENANCE_SOURCES,
    connections,
    institutions,
    transactions,
)
from bankmachine.store.types import now_utc

# 🔴 Imported rather than copied. `_call` drives the REAL server loop over
# string buffers; a second copy here would be a second description of the
# protocol, and the two would drift exactly where a protocol change matters.
from test_mcp import _call

COVERED = "acct-covered"
EMPTY = "acct-empty"


def _accounts_body() -> bytes:
    """Two accounts, and only one of them will ever receive a transaction."""

    def account(source_id: str, name: str, subtype: str) -> dict[str, Any]:
        return {
            "account_id": source_id,
            "name": name,
            "mask": "0000",
            "type": "depository",
            "subtype": subtype,
            "balances": {
                "current": "110.94",
                "available": "100.00",
                "limit": None,
                "iso_currency_code": "USD",
            },
        }

    return json.dumps(
        {
            "accounts": [
                account(COVERED, "Plaid Checking", "checking"),
                # 🔴 The account the whole file is about. It has a balance and a
                # name and looks exactly like the other one everywhere except in
                # its transaction coverage -- which is the point: a caller
                # reading `list_accounts` had no way to tell them apart.
                account(EMPTY, "Plaid Mortgage", "savings"),
            ],
            "item": {"item_id": "item-cov"},
            "request_id": "req-accounts",
        }
    ).encode()


def _sync_body(dates: list[str]) -> bytes:
    def txn(index: int, day: str) -> dict[str, Any]:
        return {
            "account_id": COVERED,
            "transaction_id": f"c{index}",
            "amount": "10.00",
            "iso_currency_code": "USD",
            "date": day,
            "authorized_date": None,
            "pending": False,
            "pending_transaction_id": None,
            "name": "Coffee",
            "merchant_name": None,
            "personal_finance_category": {
                "primary": "FOOD_AND_DRINK",
                "detailed": "FOOD_AND_DRINK",
            },
        }

    return json.dumps(
        {
            "accounts": [],
            "added": [txn(i, day) for i, day in enumerate(dates)],
            "modified": [],
            "removed": [],
            "next_cursor": "cursor-1",
            "has_more": False,
            "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE",
            "request_id": "req-sync",
        }
    ).encode()


def _seed(
    config: Config, *, spacing_days: int = 30, count: int = 4, silent_days: int = 0
) -> list[str]:
    """Two accounts through the real derivers; one gets a regular cadence.

    🔴 Derived rather than hand-inserted, like every other fixture here: the
    schema enforces provenance with a CHECK, so hand-built rows would encode
    this test's assumptions about that constraint instead of exercising it.

    `spacing_days=0` puts every row on ONE date, which is the busy-feed shape
    whose median interval is genuinely 0 -- unreachable from the sandbox, which
    is entirely monthly. `silent_days` then moves the whole run into the past,
    because a zero cadence only says something when the feed has since gone
    quiet: the two together are the case where "posts many times a day" meets
    "has posted nothing for two months".

    Returns the dates written, newest last, so a test can assert against the
    cadence it asked for rather than against a number copied from here.
    """
    now = now_utc()
    days = [
        str(now.date() - timedelta(days=spacing_days * offset + silent_days))
        for offset in reversed(range(count))
    ]
    with writer_connection(config) as conn:
        institution_pk = conn.execute(
            institutions.insert().values(
                source_institution_id="ins_cov",
                name="First Platypus Bank",
                first_seen_at=now,
                last_seen_at=now,
            )
        ).inserted_primary_key
        assert institution_pk is not None
        conn.execute(
            connections.insert().values(
                institution_id=int(institution_pk[0]),
                source_connection_id="item-cov",
                credential_ref="connection:sandbox:item-cov",
                capabilities="[]",
                requested_history_days=730,
                granted_history_days=730,
                status="active",
                last_success_at=now,
                enrolled_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        for endpoint, body in (
            (ACCOUNTS_GET.path, _accounts_body()),
            (TRANSACTIONS_SYNC.path, _sync_body(days)),
        ):
            apply_response(
                conn,
                connection_id=1,
                endpoint=endpoint,
                body=body,
                received_at=now,
                derivers=ALL_DERIVERS,
            )
    return days


def _rows_by_account(wire: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["name"]: row for row in wire["rows"]}


def _kinds(wire: dict[str, Any]) -> set[str]:
    return {caveat["kind"] for caveat in wire["warnings"]}


# --------------------------------------------------------------------------
# The three-way distinction, against one fixture
# --------------------------------------------------------------------------


def test_an_account_that_never_had_a_transaction_says_so_on_its_own_row(
    initialized_config: Config,
) -> None:
    """#19's fix, stated as a property of the row rather than of a warning.

    🔴 On EVERY row and behind no parameter, because the failure is an agent
    that never thought to ask the verification surface. A caller who has to opt
    in is a caller who still gets the misleading answer.
    """
    _seed(initialized_config)
    rows = _rows_by_account(_call(initialized_config, "list_accounts", {})["structuredContent"])

    empty = rows["Plaid Mortgage"]
    assert empty["transaction_count"] == 0
    assert empty["first_transaction_date"] is None
    assert empty["last_transaction_date"] is None

    covered = rows["Plaid Checking"]
    assert covered["transaction_count"] == 4
    assert covered["first_transaction_date"] is not None
    assert covered["last_transaction_date"] is not None
    # The two accounts are otherwise indistinguishable, which is the bug: both
    # carry a balance, a name and a mask. If this ever fails because the empty
    # account stopped having a balance, the fixture has stopped reproducing #19.
    assert empty["current_minor_units"] == covered["current_minor_units"]


def test_listing_accounts_warns_when_one_of_them_has_no_coverage(
    initialized_config: Config,
) -> None:
    _seed(initialized_config)
    wire = _call(initialized_config, "list_accounts", {})["structuredContent"]
    assert "accounts_without_coverage" in _kinds(wire)
    detail = next(c["detail"] for c in wire["warnings"] if c["kind"] == "accounts_without_coverage")
    # Names the id, because "some accounts have no data" is a warning whose
    # reader cannot act on it.
    assert "2" in detail


def test_querying_an_uncovered_account_warns_instead_of_answering_a_bare_empty(
    initialized_config: Config,
) -> None:
    """🔴 #19's own repro, and the call where the notice has to arrive.

    `query_transactions(account_id=<the empty one>)` returns `[]` either way.
    What changes is that the empty list now travels with a warning saying the
    emptiness means DATA NOT PRESENT.
    """
    _seed(initialized_config)
    wire = _call(
        initialized_config,
        "query_transactions",
        {"account_id": 2, "since": "2020-01-01", "until": "2030-12-31"},
    )["structuredContent"]

    assert wire["rows"] == []
    assert "accounts_without_coverage" in _kinds(wire)


def test_an_account_quiet_in_the_window_is_not_reported_as_uncovered(
    initialized_config: Config,
) -> None:
    """The third case, and the one that keeps the warning worth reading.

    🔴 A quiet window is an ORDINARY result. If this warning fired here too it
    would arrive on almost every question, which is the exact defect the
    connection-scoped kinds already demonstrated: a notice that appears
    character-for-character on every answer teaches its reader to skip it.
    """
    _seed(initialized_config)
    wire = _call(
        initialized_config,
        "query_transactions",
        # A window the covered account has no rows in -- far in the past, and
        # long before anything this fixture wrote.
        {"account_id": 1, "since": "2001-01-01", "until": "2001-12-31"},
    )["structuredContent"]

    assert wire["rows"] == []
    assert "accounts_without_coverage" not in _kinds(wire)


def test_an_account_that_does_not_exist_is_still_refused_by_name(
    initialized_config: Config,
) -> None:
    """The first case, unchanged: refused rather than answered empty or warned.

    Asserted here beside the other two because the three are one design. A
    later change that turned this refusal into a warning would pass both tests
    above and still destroy the distinction.
    """
    _seed(initialized_config)
    result = _call(initialized_config, "query_transactions", {"account_id": 999})
    assert result.get("isError") is True
    assert "999" in json.dumps(result)


# --------------------------------------------------------------------------
# One producer, two readers
# --------------------------------------------------------------------------


def test_the_two_tools_never_disagree_about_an_account(initialized_config: Config) -> None:
    """🔴 #35's second acceptance criterion, asserted rather than trusted.

    Both tools read `_account_coverage`. Built twice they could drift, and a
    verification surface that contradicts the analysis surface is worse than one
    that is missing -- a caller has no way to tell which half is lying.
    """
    _seed(initialized_config)
    listed = {
        row["account_id"]: row
        for row in _call(initialized_config, "list_accounts", {})["structuredContent"]["rows"]
    }
    reported = {
        row["account_id"]: row
        for row in _call(initialized_config, "get_coverage_report", {})["structuredContent"]["rows"]
    }

    assert set(listed) == set(reported), "the two tools disagree about which accounts exist"
    for account_id, row in listed.items():
        for field in ("first_transaction_date", "last_transaction_date", "transaction_count"):
            assert row[field] == reported[account_id][field], (account_id, field)


# --------------------------------------------------------------------------
# Cadence and trailing silence
# --------------------------------------------------------------------------


def test_the_cadence_is_the_accounts_own_median_interval(initialized_config: Config) -> None:
    """The ruled instrument: per account, from its own intervals.

    A fixed threshold was falsified by measurement -- every account in the real
    store is monthly, so "gaps > 7 days" fired on 100% of two accounts' intervals
    and produced ~146 findings with no signal.
    """
    _seed(initialized_config, spacing_days=30, count=4)
    rows = {
        row["account_id"]: row
        for row in _call(initialized_config, "get_coverage_report", {})["structuredContent"]["rows"]
    }
    assert rows[1]["median_interval_days"] == pytest.approx(30.0)


def test_an_account_with_one_transaction_has_no_cadence_rather_than_a_zero(
    initialized_config: Config,
) -> None:
    """🔴 Null, never 0. A zero would read as "posts every day", which makes
    every subsequent silence a finding — the noise the ruling removed, reached
    by a different route.
    """
    _seed(initialized_config, count=1)
    rows = {
        row["account_id"]: row
        for row in _call(initialized_config, "get_coverage_report", {})["structuredContent"]["rows"]
    }
    assert rows[1]["median_interval_days"] is None
    assert rows[1]["silence_ratio"] is None
    assert rows[1]["silence_exceeds_cadence"] is False


def test_silence_is_reported_as_a_ratio_and_not_only_as_a_flag(
    initialized_config: Config,
) -> None:
    """🔴 The borderline case the ruling singled out, and what a boolean destroys.

    Real CD and Money Market accounts sit 28 days silent on a 30-day cycle --
    "genuinely borderline, and the only thing in this dataset a coverage report
    should be drawing attention to". They do NOT exceed a full cycle, so the flag
    is false; the ratio is what surfaces them. A design that shipped only the
    flag would hide exactly the finding the report exists for.
    """
    _seed(initialized_config, spacing_days=30, count=4)
    row = next(
        r
        for r in _call(initialized_config, "get_coverage_report", {})["structuredContent"]["rows"]
        if r["account_id"] == 1
    )
    # The newest transaction is today, so this account is not silent at all --
    # the assertion that matters is that the quantity is REPORTED, in a form
    # that can be near 1 without being over it.
    assert row["days_silent"] == 0
    assert row["silence_ratio"] == pytest.approx(0.0)
    assert row["silence_exceeds_cadence"] is False
    assert isinstance(row["silence_ratio"], float)


def test_a_full_missed_cycle_raises_the_flag(initialized_config: Config) -> None:
    """One cycle is the threshold because it is the only non-arbitrary unit.

    🔴 Silence of EXACTLY one cadence does not flag, and that is deliberate: at
    one interval the next transaction is due, not missed. Removing a single row
    here leaves exactly that boundary and the flag stays false — which is how
    this test was first written, and it failed. Two rows removed is what puts
    the account a full cycle past due.
    """
    # Cadence of 10 days across four transactions at 30/20/10/0 days ago;
    # dropping the newest two leaves the last one 20 days back — two cadences.
    _seed(initialized_config, spacing_days=10, count=4)
    with writer_connection(initialized_config) as conn:
        conn.execute(transactions.delete().where(transactions.c.transaction_id.in_((3, 4))))
    row = next(
        r
        for r in _call(initialized_config, "get_coverage_report", {})["structuredContent"]["rows"]
        if r["account_id"] == 1
    )
    assert row["silence_exceeds_cadence"] is True
    assert row["silence_ratio"] > 1.0


# --------------------------------------------------------------------------
# What coverage counts
# --------------------------------------------------------------------------


def test_a_soft_deleted_row_is_not_counted_as_coverage(initialized_config: Config) -> None:
    """🔴 A coverage report counting rows the analysis surface cannot return
    would promise data no query can produce — the inverse of #19 and just as
    wrong.
    """
    _seed(initialized_config, count=4)
    with writer_connection(initialized_config) as conn:
        conn.execute(
            transactions.update()
            .where(transactions.c.transaction_id == 1)
            .values(removed_at=now_utc())
        )
    row = next(
        r
        for r in _call(initialized_config, "get_coverage_report", {})["structuredContent"]["rows"]
        if r["account_id"] == 1
    )
    assert row["transaction_count"] == 3


def test_the_source_breakdown_carries_a_zero_for_a_source_with_no_rows(
    initialized_config: Config,
) -> None:
    """Present and 0, never omitted: a missing key makes a consumer guess
    whether it meant zero or unknown, and those are different answers.
    """
    _seed(initialized_config)
    row = next(
        r
        for r in _call(initialized_config, "get_coverage_report", {})["structuredContent"]["rows"]
        if r["account_id"] == 1
    )
    assert set(row["source_breakdown"]) == set(PROVENANCE_SOURCES)
    assert row["source_breakdown"]["aggregator"] == 4
    assert row["source_breakdown"]["manual"] == 0


def test_the_report_answers_against_a_store_with_no_transactions_at_all(
    initialized_config: Config,
) -> None:
    """Every account uncovered is a legitimate state, not an empty payload."""
    _seed(initialized_config, count=0)
    wire = _call(initialized_config, "get_coverage_report", {})["structuredContent"]
    assert len(wire["rows"]) == 2
    assert all(row["transaction_count"] == 0 for row in wire["rows"])
    assert "accounts_without_coverage" in _kinds(wire)


def test_the_coverage_walk_reaches_accounts_with_no_transactions(
    initialized_config: Config,
) -> None:
    """🔴 The positive control for the join, not a restatement of the tests above.

    `removed_at IS NULL` belongs in the outer join's ON clause; in a WHERE it
    filters away the null side and every uncovered account vanishes from the
    result. That failure looks exactly like an account having coverage, so it is
    asserted at the producer rather than only through a tool.
    """
    _seed(initialized_config)
    with writer_connection(initialized_config) as conn:
        coverage = query._account_coverage(conn)
    assert set(coverage) == {1, 2}, "an account fell out of the coverage walk"
    assert coverage[2].uncovered is True
    assert coverage[1].uncovered is False


def test_an_account_that_posts_many_times_a_day_can_still_go_silent(
    initialized_config: Config,
) -> None:
    """🔴 The busiest feeds are the ones most likely to stop, and they answered `false`.

    An account whose rows cluster on the same dates has a median interval of
    genuinely 0 days. Dividing by it is undefined, so the guard returned a null
    ratio and `silence_exceeds_cadence: false` — permanently, however long the
    feed had been dead. A card or checking feed is exactly that shape, and it is
    the account class whose silence matters most, so the tool whose stated job
    is to notice trailing silence was blind on its most important case.

    Unreachable from the sandbox, which is entirely monthly. That is why this
    fixture puts three rows on one date and then walks the whole run 60 days
    into the past.
    """
    _seed(initialized_config, spacing_days=0, count=3, silent_days=60)
    row = {
        r["account_id"]: r
        for r in _call(initialized_config, "get_coverage_report", {})["structuredContent"]["rows"]
    }[1]

    # The measured cadence rides out as measured: 0 means "posts more than once
    # a day", which is the opposite of null's "no interval exists".
    assert row["median_interval_days"] == 0
    assert row["days_silent"] == 60
    # 🔴 The divisor is floored at a day, so 60 days of silence against a
    # daily-or-better cadence is 60 missed cycles rather than an unanswerable
    # question.
    assert row["silence_ratio"] == 60.0
    assert row["silence_exceeds_cadence"] is True


def test_a_busy_account_that_posted_today_is_not_reported_silent(
    initialized_config: Config,
) -> None:
    """The positive control for the floor, without which it would flag everything.

    Flooring the divisor makes silence easy to exceed, so the case that must
    still read `false` is asserted beside the one that must read `true` — a floor
    that flagged a live feed would trade a blind spot for a false alarm on every
    busy account in the store.
    """
    _seed(initialized_config, spacing_days=0, count=3, silent_days=0)
    row = {
        r["account_id"]: r
        for r in _call(initialized_config, "get_coverage_report", {})["structuredContent"]["rows"]
    }[1]
    assert row["median_interval_days"] == 0
    assert row["days_silent"] == 0
    assert row["silence_ratio"] == 0.0
    assert row["silence_exceeds_cadence"] is False
