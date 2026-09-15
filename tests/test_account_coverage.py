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
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, update

from bankmachine import query
from bankmachine.config import Config
from bankmachine.connector import (
    ACCOUNTS_GET,
    INVESTMENTS_HOLDINGS_GET,
    INVESTMENTS_TRANSACTIONS_GET,
    TRANSACTIONS_SYNC,
)
from bankmachine.derivers import ALL_DERIVERS
from bankmachine.store.derivation import apply_response
from bankmachine.store.engine import writer_connection
from bankmachine.store.schema import (
    PROVENANCE_SOURCES,
    accounts,
    connections,
    institutions,
    investment_transactions,
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
                replay_passes=(),
            )
    return days


def _cover_the_empty_account(config: Config) -> None:
    """Give the second account one transaction, through the real deriver.

    The store this fixture builds has exactly one uncovered account, so removing
    its uncoveredness is the only way to reach the negative case — and it has to
    be reached, because a warning asserted only where it fires says nothing
    about whether it ever stops.
    """
    now = now_utc()
    body = json.dumps(
        {
            "accounts": [],
            "added": [
                {
                    "account_id": EMPTY,
                    "transaction_id": "e0",
                    "amount": "10.00",
                    "iso_currency_code": "USD",
                    "date": str(now.date()),
                    "authorized_date": None,
                    "pending": False,
                    "pending_transaction_id": None,
                    "name": "Mortgage payment",
                    "merchant_name": None,
                    "personal_finance_category": {
                        "primary": "LOAN_PAYMENTS",
                        "detailed": "LOAN_PAYMENTS_MORTGAGE_PAYMENT",
                    },
                }
            ],
            "modified": [],
            "removed": [],
            "next_cursor": "cursor-2",
            "has_more": False,
            "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE",
            "request_id": "req-sync-2",
        }
    ).encode()
    with writer_connection(config) as conn:
        apply_response(
            conn,
            connection_id=1,
            endpoint=TRANSACTIONS_SYNC.path,
            body=body,
            received_at=now,
            derivers=ALL_DERIVERS,
            replay_passes=(),
        )


FIXTURES = Path(__file__).parent / "connector" / "fixtures"


def _capture(name: str) -> dict[str, Any]:
    """A recorded sandbox capture, parsed the way the deriver parses it: numbers as text."""
    body: dict[str, Any] = json.loads((FIXTURES / name).read_bytes(), parse_float=str)
    return body


def _seed_investment_activity(config: Config) -> dict[str, Any]:
    """Investment activity for four accounts, each holding a different mix of the feeds.

    Built on `_seed`'s store, through the real derivers, from the recorded holdings
    and trades captures. Those two captures name DIFFERENT accounts, which is what
    lets one store hold every combination #107 turns on:

    * ``trades_only`` -- an investment account with trades and no positions;
    * ``positions_only`` -- one with positions and no trades;
    * ``covered`` -- the checking account `_seed` already gave four transactions,
      handed the other trades account's trades too, so one account holds rows in
      BOTH tables and a count taken through a widened join is visibly multiplied;
    * ``no_data`` -- the account those trades were taken from, still on the roster
      and now holding nothing in any feed.

    The captures' rosters also list accounts neither capture gives anything to
    (their checking, loan and card accounts), and those join the roster as further
    accounts with no data in any feed.

    Returns each account's store id, the trade counts read off the captures, and
    the store ids of every roster account and of the ones this fixture gave data,
    so a test asserts against the fixture rather than a number copied from it.
    """
    holdings = _capture("investments_holdings_get.json")
    trades = _capture("investments_transactions_get.json")
    holding_accounts = sorted({h["account_id"] for h in holdings["holdings"]})
    trade_accounts = sorted({t["account_id"] for t in trades["investment_transactions"]})
    assert holding_accounts and len(trade_accounts) >= 2, "the captures stopped separating feeds"
    assert not set(holding_accounts) & set(trade_accounts), (
        "an account now holds both positions and trades in the captures, so no account here is "
        "trades-only or positions-only any more"
    )
    trades_only, taken_from = trade_accounts[0], trade_accounts[1]
    for trade in trades["investment_transactions"]:
        if trade["account_id"] == taken_from:
            trade["account_id"] = COVERED

    roster = json.loads(_accounts_body())
    known = {entry["account_id"] for entry in roster["accounts"]}
    for capture in (holdings, trades):
        for entry in capture["accounts"]:
            if entry["account_id"] not in known:
                roster["accounts"].append(entry)
                known.add(entry["account_id"])
    now = now_utc()
    with writer_connection(config) as conn:
        for endpoint, body in (
            (ACCOUNTS_GET.path, roster),
            (INVESTMENTS_HOLDINGS_GET.path, holdings),
            (INVESTMENTS_TRANSACTIONS_GET.path, trades),
        ):
            apply_response(
                conn,
                connection_id=1,
                endpoint=endpoint,
                body=json.dumps(body).encode(),
                received_at=now,
                derivers=ALL_DERIVERS,
                replay_passes=(),
            )
        ids = dict(
            conn.execute(select(accounts.c.source_account_id, accounts.c.account_id)).tuples().all()
        )
    per_account = {
        source: sum(1 for t in trades["investment_transactions"] if t["account_id"] == source)
        for source in (trades_only, COVERED)
    }
    return {
        "trades_only": ids[trades_only],
        "positions_only": ids[holding_accounts[0]],
        "covered": ids[COVERED],
        "no_data": ids[taken_from],
        "empty": ids[EMPTY],
        "trades_only_trades": per_account[trades_only],
        "covered_trades": per_account[COVERED],
        "roster": {ids[entry["account_id"]] for entry in roster["accounts"]},
        # Read off what this fixture wrote, never off the store: `_seed` gave the
        # covered account transactions, and the captures gave trades and positions.
        "with_data": {ids[source] for source in (COVERED, trades_only, *holding_accounts)},
    }


def _named(wire: dict[str, Any]) -> set[int]:
    """The account ids `accounts_without_coverage` names on this answer; empty when it is absent."""
    named: set[int] = set()
    for caveat in wire["warnings"]:
        if caveat["kind"] == "accounts_without_coverage":
            match = re.search(r"account\(s\) ([\d, ]+)", caveat["detail"])
            assert match, f"the warning names no accounts: {caveat['detail']}"
            named |= {int(part) for part in match.group(1).replace(" ", "").split(",") if part}
    return named


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


def test_summarising_money_warns_when_an_account_in_scope_has_no_coverage(
    initialized_config: Config,
) -> None:
    """🔴 The aggregate is the answer an agent is told to QUOTE, and it said nothing.

    `money_summary` takes no `account_id`, so every account in the store is in
    its scope — including one that has never had a transaction recorded. Its
    contribution to every group is nothing, and without this warning that
    nothing is indistinguishable from an account that was quiet. The three tools
    beside it already say so; the one whose figure gets quoted did not.
    """
    _seed(initialized_config)

    wire = _call(initialized_config, "money_summary", {})["structuredContent"]

    assert "accounts_without_coverage" in _kinds(wire)
    detail = next(c["detail"] for c in wire["warnings"] if c["kind"] == "accounts_without_coverage")
    assert "2" in detail, "the warning does not name the account a reader would have to exclude"


def test_summarising_money_over_a_fully_covered_store_raises_no_coverage_warning(
    initialized_config: Config,
) -> None:
    """The reverse half, without which the warning would ride every aggregate alike.

    A kind that fires on every answer teaches its reader to skip it, which is the
    defect the connection-scoped kinds already measured.
    """
    _seed(initialized_config)
    _cover_the_empty_account(initialized_config)

    wire = _call(initialized_config, "money_summary", {})["structuredContent"]

    assert "accounts_without_coverage" not in _kinds(wire)


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
# #107: an investment account's activity is data, and the listings say so
# --------------------------------------------------------------------------


def _by_id(wire: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {row["account_id"]: row for row in wire["rows"]}


def test_an_investment_account_carries_its_trades_and_positions_on_its_own_row(
    initialized_config: Config,
) -> None:
    """🔴 The row says where an investment account's activity is, not only what it lacks.

    `transaction_count` 0 on its own sent a reader to conclude the account was
    empty. The trade count and the day its positions were last captured sit beside
    it, so the same row says what the store DOES hold. The holdings day is compared
    with `list_holdings`' own `as_of_date` for the account: two tools naming two
    different days for one capture is the disagreement a verification surface
    exists to rule out.
    """
    _seed(initialized_config)
    ids = _seed_investment_activity(initialized_config)
    rows = _by_id(_call(initialized_config, "list_accounts", {})["structuredContent"])
    positions = _call(initialized_config, "list_holdings", {})["structuredContent"]["rows"]
    captured = {row["account_id"]: row["as_of_date"] for row in positions}

    trades_only = rows[ids["trades_only"]]
    assert trades_only["transaction_count"] == 0
    assert trades_only["investment_transaction_count"] == ids["trades_only_trades"]
    assert trades_only["holdings_as_of"] is None

    positions_only = rows[ids["positions_only"]]
    assert positions_only["investment_transaction_count"] == 0
    assert positions_only["holdings_as_of"] is not None
    assert positions_only["holdings_as_of"] == captured[ids["positions_only"]]

    # Present and zero rather than absent on an account no feed has anything for.
    nothing = rows[ids["no_data"]]
    assert nothing["investment_transaction_count"] == 0
    assert nothing["holdings_as_of"] is None


def test_neither_listing_names_an_account_whose_data_is_investments(
    initialized_config: Config,
) -> None:
    """🔴 #107: the warning told an agent to distrust an account whose data IS in the store.

    Both listings describe the ACCOUNT, so both now name only the accounts nothing
    has ever been recorded for in any feed. Asserted as the exact set on both
    tools, derived from what the fixture wrote rather than from the store: a set
    that still holds an investment account is the bug, and a set that lost a
    genuinely empty account is the warning gone quiet -- the failure #19 was.
    """
    _seed(initialized_config)
    ids = _seed_investment_activity(initialized_config)
    expected = ids["roster"] - ids["with_data"]
    assert {ids["empty"], ids["no_data"]} <= expected, "the fixture lost its empty accounts"
    assert {ids["trades_only"], ids["positions_only"], ids["covered"]} <= ids["with_data"]

    for tool in ("list_accounts", "get_coverage_report"):
        named = _named(_call(initialized_config, tool, {})["structuredContent"])
        assert named == expected, (tool, sorted(named ^ expected))


def _routed(wire: dict[str, Any]) -> set[int]:
    """The account ids `activity_in_another_feed` names on this answer; empty when it is absent."""
    routed: set[int] = set()
    for caveat in wire["warnings"]:
        if caveat["kind"] == "activity_in_another_feed":
            head = caveat["detail"].split(" hold no transaction")[0]
            assert head.startswith("account(s) "), caveat["detail"]
            routed |= {int(part) for part in head.removeprefix("account(s) ").split(", ")}
    return routed


def test_querying_an_investment_only_account_for_transactions_routes_to_its_feed(
    initialized_config: Config,
) -> None:
    """🔴 Named, and routed rather than called absent: its activity IS in the store.

    `query_transactions` cannot return a trade, so an empty answer about an
    investment-only account needs a notice -- "no transactions found" must not pass
    as "nothing happened". But "data not present" is false of it too, and an agent
    told that reported a fault. The notice says where the activity lives.
    """
    _seed(initialized_config)
    ids = _seed_investment_activity(initialized_config)
    wire = _call(
        initialized_config,
        "query_transactions",
        {"account_id": ids["trades_only"], "since": "2020-01-01", "until": "2030-12-31"},
    )["structuredContent"]

    assert wire["rows"] == []
    assert _named(wire) == set(), "an account whose trades are stored was called data not present"
    assert _routed(wire) == {ids["trades_only"]}
    routed = next(w for w in wire["warnings"] if w["kind"] == "activity_in_another_feed")
    assert "`query_investment_transactions`" in routed["detail"]


def test_summarising_money_routes_investment_accounts_and_names_the_empty_ones(
    initialized_config: Config,
) -> None:
    """Every account the aggregate leaves out is named once, under the kind that is true of it.

    An investment account adds nothing to a total over the transactions feed, and
    that is still said -- but as where its activity lives, not as missing data. An
    account with nothing in any feed keeps `accounts_without_coverage`. Asserted as
    exact sets, so an account named under both, or dropped from both, fails.
    """
    _seed(initialized_config)
    ids = _seed_investment_activity(initialized_config)
    wire = _call(initialized_config, "money_summary", {})["structuredContent"]

    routed, named = _routed(wire), _named(wire)
    # Every account the fixture gave data to except the one it also gave transactions:
    # the holdings capture can name more than one positions-only account.
    assert {ids["trades_only"], ids["positions_only"]} <= routed
    assert routed == ids["with_data"] - {ids["covered"]}
    assert named == ids["roster"] - ids["with_data"]
    assert not routed & named


def test_an_account_with_nothing_recorded_on_a_completed_connection_is_not_called_missing(
    initialized_config: Config,
) -> None:
    """🔴 A completed sync that returned nothing for an account is not a fault.

    The aggregator gives no signal that tells a quiet account from one whose
    institution does not report it, so the store says it cannot tell -- never
    "DATA NOT PRESENT", which an agent reported to the operator as a problem.
    """
    _seed(initialized_config)

    wire = _call(initialized_config, "list_accounts", {})["structuredContent"]

    details = [w["detail"] for w in wire["warnings"] if w["kind"] == "accounts_without_coverage"]
    assert details, "the empty account is no longer named at all"
    assert all("DATA NOT PRESENT" not in d for d in details), details
    assert any("cannot tell" in d and "not that a sync failed" in d for d in details), details


def test_an_account_with_nothing_recorded_on_an_unfinished_connection_is_data_not_present(
    initialized_config: Config,
) -> None:
    """The other state: a connection that never completed a sync has told us nothing yet."""
    _seed(initialized_config)
    with writer_connection(initialized_config) as conn:
        conn.execute(update(connections).values(last_success_at=None))

    wire = _call(initialized_config, "list_accounts", {})["structuredContent"]

    details = [w["detail"] for w in wire["warnings"] if w["kind"] == "accounts_without_coverage"]
    assert details and all("DATA NOT PRESENT" in d for d in details), details
    assert all("cannot tell" not in d for d in details), details


def test_each_feed_is_counted_on_its_own_and_never_multiplied_by_the_other(
    initialized_config: Config,
) -> None:
    """🔴 One account with rows in BOTH tables reports each count exactly.

    The coverage walk already outer-joins `transactions`. Joining the trade table
    beside it multiplies every count by the other -- four transactions and twenty
    trades would both read eighty -- and nothing about eighty looks wrong.
    """
    _seed(initialized_config)
    ids = _seed_investment_activity(initialized_config)
    assert ids["covered_trades"] > 1, "the fixture no longer puts several trades on one account"

    for tool in ("list_accounts", "get_coverage_report"):
        row = _by_id(_call(initialized_config, tool, {})["structuredContent"])[ids["covered"]]
        assert row["transaction_count"] == 4, tool
        assert row["investment_transaction_count"] == ids["covered_trades"], tool


def test_a_removed_trade_is_not_counted(initialized_config: Config) -> None:
    """A trade the feed stopped returning is soft-deleted; counting it promises data.

    The `removed_at` stamp is written directly, standing in for the window
    reconciliation that writes it in a sync: that stamp is the whole of what the
    reconciliation leaves on the row, and it is what the count must honour.
    """
    _seed(initialized_config)
    ids = _seed_investment_activity(initialized_config)
    with writer_connection(initialized_config) as conn:
        first = conn.execute(
            select(func.min(investment_transactions.c.investment_transaction_id)).where(
                investment_transactions.c.account_id == ids["trades_only"]
            )
        ).scalar_one()
        conn.execute(
            update(investment_transactions)
            .where(investment_transactions.c.investment_transaction_id == first)
            .values(removed_at=now_utc())
        )

    row = _by_id(_call(initialized_config, "list_accounts", {})["structuredContent"])[
        ids["trades_only"]
    ]
    assert row["investment_transaction_count"] == ids["trades_only_trades"] - 1


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
    # Investment activity too, so the two investment fields are compared at values
    # other than their zero and null defaults.
    _seed_investment_activity(initialized_config)
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
        for field in (
            "first_transaction_date",
            "last_transaction_date",
            "transaction_count",
            "investment_transaction_count",
            "holdings_as_of",
        ):
            assert row[field] == reported[account_id][field], (account_id, field)
    assert any(row["investment_transaction_count"] for row in listed.values()), (
        "no account carries a trade count, so the comparison above never left zero"
    )


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


# --------------------------------------------------------------------------
# Holes in the middle of a history, which trailing silence cannot see
# --------------------------------------------------------------------------


def _gaps(cadence: float | None, *day_offsets: int) -> list[dict[str, Any]]:
    """Interior gaps over a history given as day offsets from an arbitrary start."""
    from bankmachine.store.types import calendar_date

    base = date(2026, 1, 1)
    dates = [calendar_date(base + timedelta(days=offset)) for offset in day_offsets]
    return query._interior_gaps(dates, cadence)


def test_a_hole_in_the_middle_of_a_history_is_found_where_trailing_silence_is_blind() -> None:
    """🔴 The defect, stated as the measurement that misses it.

    `days_silent` runs from the last transaction to today, so an account that
    went quiet for three months and then resumed reports 1 day silent and
    `silence_exceeds_cadence` false -- while `money_summary` with
    `group_by=month` shows a spending collapse for those months that never
    happened. The feed stopped; the spending did not. Nothing in the report
    could distinguish those two until this.
    """
    # Daily-ish account: posts every day, then a 90-day hole, then daily again.
    offsets = [0, 1, 2, 3, 93, 94, 95, 96]
    gaps = _gaps(1.0, *offsets)

    assert len(gaps) == 1, f"expected the one hole, saw {gaps}"
    assert gaps[0]["days"] == 90
    assert gaps[0]["from"] == "2026-01-04"
    assert gaps[0]["to"] == "2026-04-04"
    assert gaps[0]["ratio"] == 90.0


def test_a_long_weekend_on_a_daily_account_is_not_a_gap() -> None:
    """The floor's whole job. Without it, cadence 1 makes any four quiet days a finding.

    An account posting daily is the busiest class there is, and it is the class
    a bare cadence multiple turns into noise -- which is how a report ends up
    with findings nobody reads.
    """
    assert _gaps(1.0, 0, 1, 2, 6, 7) == []


def test_an_ordinary_month_on_a_monthly_account_is_not_a_gap() -> None:
    """The multiple's whole job, and the mirror of the case above.

    A seven-day floor alone would flag every single cycle of an account that
    posts once a month -- noise on the quietest accounts instead of the busiest.
    Both conditions exist because either alone misfires, in opposite directions.
    """
    assert _gaps(30.0, 0, 30, 61, 91, 122) == []


def test_a_missed_quarter_on_a_monthly_account_is_a_gap() -> None:
    """And the monthly account's real failure is still caught."""
    gaps = _gaps(30.0, 0, 30, 130, 160)
    assert len(gaps) == 1
    assert gaps[0]["days"] == 100


def test_an_account_with_no_cadence_reports_no_gaps_rather_than_guessing() -> None:
    """Fewer than two transactions is no interval, and no interval is not zero.

    Zero would read as "posts every day" and make every subsequent quiet week a
    finding on an account nobody has any rhythm for.
    """
    assert _gaps(None, 0, 500) == []
    assert _gaps(1.0, 0) == []


def test_the_widest_gaps_survive_the_cap_and_the_count_does_not_shrink_with_the_list() -> None:
    """🔴 The count is as MEASURED; only the enumeration is capped.

    Capping inside the measurement would make the count agree with the truncated
    list, and the truncation would stop being visible -- an account with forty
    holes would report ten, which is the undercount this report exists to
    surface. So this asserts the two are allowed to disagree, and that what
    survives is the widest rather than the earliest.
    """
    # Twelve gaps of increasing width on a daily account.
    offsets = [0]
    for width in range(10, 22):
        offsets.append(offsets[-1] + width)
    gaps = _gaps(1.0, *offsets)

    assert len(gaps) == 12, "the measurement itself must not be capped"
    capped = gaps[: query.MAX_INTERIOR_GAPS_PER_ACCOUNT]
    assert len(capped) == query.MAX_INTERIOR_GAPS_PER_ACCOUNT
    assert [gap["days"] for gap in capped] == sorted((gap["days"] for gap in gaps), reverse=True)[
        : query.MAX_INTERIOR_GAPS_PER_ACCOUNT
    ], "the cap kept the earliest gaps, not the widest"


# --------------------------------------------------------------------------
# A true zero, and history the grant cut off, told apart
# --------------------------------------------------------------------------


def _coverage(first: date | None, starts: date | None, count: int = 5) -> query.AccountCoverage:
    from bankmachine.store.types import calendar_date

    return query.AccountCoverage(
        account_id=1,
        first_transaction_date=None if first is None else calendar_date(first),
        last_transaction_date=None if first is None else calendar_date(first),
        transaction_count=count,
        investment_transaction_count=0,
        holdings_as_of=None,
        history_starts=None if starts is None else calendar_date(starts),
    )


def test_an_account_opened_well_after_the_grant_really_does_begin_there() -> None:
    """🔴 A TRUE zero, which may be reported as one.

    The account's first transaction sits months after its connection's granted
    start, so the store genuinely looked at that earlier period and there was
    nothing in it. A zero for those months is a fact about the household.
    """
    assert not _coverage(date(2026, 6, 1), date(2024, 9, 16)).truncated_by_the_grant


def test_an_account_starting_at_the_grant_boundary_was_cut_rather_than_opened() -> None:
    """🔴 ABSENT, not zero — and indistinguishable from the case above without this.

    `first_transaction_date` alone reads identically in both: a date with
    nothing before it. Only the comparison against the connection's granted
    start says whether the store LOOKED earlier and found nothing, or never
    looked at all. Reporting this one as $0 is the failure the product exists
    to refuse.
    """
    assert _coverage(date(2024, 9, 18), date(2024, 9, 16)).truncated_by_the_grant


def test_the_boundary_margin_resolves_toward_truncation() -> None:
    """The tie goes to "absent", and the direction is the decision.

    A grant boundary rarely lands exactly on an account's first posting day, so
    a few days of slack is ordinary. Inside the margin this reports truncation,
    because calling absent data a true zero is the error that gets believed
    rather than questioned.
    """
    boundary = date(2024, 9, 16)
    inside = boundary + timedelta(days=query.GRANT_BOUNDARY_DAYS)
    outside = boundary + timedelta(days=query.GRANT_BOUNDARY_DAYS + 1)

    assert _coverage(inside, boundary).truncated_by_the_grant, "the margin must include its edge"
    assert not _coverage(outside, boundary).truncated_by_the_grant


def test_an_unmeasured_grant_is_neither_case_and_says_so() -> None:
    """🔴 The third state. Defaulting it to either branch would invent the answer.

    A connection whose granted window has not been measured has no start to
    compare against. That is common in the first hours of a real connection,
    and it is not "covered" — it is unanswered.
    """
    entry = _coverage(date(2026, 1, 1), None)
    assert entry.unmeasured
    assert not entry.truncated_by_the_grant


def test_an_aggregate_names_the_accounts_whose_history_the_window_reaches_past() -> None:
    """🔴 The defect: one store-wide min() made a thinly-covered account invisible.

    `coverage.earliest_transaction` is a single minimum over every account, so
    one long-history account makes the whole store look well covered, and a
    window over an account whose own data starts inside it returns $0 for the
    uncovered months with no warning at all.

    The notice names WHICH account and from WHICH date. A count is not
    actionable — with a store-wide minimum there was nothing to go and look at.
    """
    cut = _coverage(date(2024, 9, 18), date(2024, 9, 16))
    caveats = query._window_coverage_caveat([cut], date(2024, 1, 1))

    assert len(caveats) == 1
    assert caveats[0].kind == "accounts_without_coverage"
    assert "2024-09-16" in caveats[0].detail, "the notice must say from when the account IS covered"
    assert "absent" in caveats[0].detail


def test_an_aggregate_inside_every_accounts_coverage_says_nothing() -> None:
    """The absence of this kind has to stay information.

    A window that reaches past nothing raises nothing, so a caller can read the
    quiet as coverage rather than as an unasked question.
    """
    cut = _coverage(date(2024, 9, 18), date(2024, 9, 16))
    assert query._window_coverage_caveat([cut], date(2025, 1, 1)) == []


def test_an_unmeasured_account_is_not_named_a_second_time_by_the_aggregate() -> None:
    """🔴 One condition, one notice.

    A connection whose grant is unmeasured already raises `partial` once, from
    the pipeline warnings. Emitting it per ACCOUNT here as well would put a
    second notice about one fact on nearly every answer during the hours after
    a real connection is made — and two kinds describing one condition is how a
    caller ends up reconciling two lists.
    """
    unmeasured = _coverage(date(2026, 1, 1), None)
    assert unmeasured.unmeasured
    assert query._window_coverage_caveat([unmeasured], date(2020, 1, 1)) == []


# --------------------------------------------------------------------------
# A connection about to lose its authorisation, said BEFORE it does
# --------------------------------------------------------------------------


def _consent(days_from_now: int) -> list[Any]:
    """The caveats a connection expiring that many days out would raise."""
    from bankmachine.store.types import now_utc

    now = now_utc()
    return query._consent_caveats(
        name="An Institution",
        connection_id=1,
        expires_at=now + timedelta(days=days_from_now),
        now=now,
    )


def test_a_consent_expiring_soon_is_reported_before_it_lapses() -> None:
    """🔴 The defect: the pipeline is poll-only, so expiry surfaced as a failed run.

    A connection whose consent lapses next week answered *healthy* and stayed
    healthy right up to the sync that failed. The date to say otherwise was
    archived on every poll and read by nothing, which made the one surface that
    could have given advance notice the one that stayed quiet.
    """
    caveats = _consent(7)

    assert len(caveats) == 1
    assert caveats[0].kind == "partial"
    assert "7 day(s)" in caveats[0].detail
    assert "re-link" in caveats[0].detail


def test_an_approaching_expiry_is_partial_rather_than_stale() -> None:
    """🔴 The decision, not a wording choice.

    `stale` means "has not synced recently" — a claim about the PAST. An
    expiring consent is a claim about the future, and a reader acts on the two
    differently. `partial` already means *something is not yet known, never read
    it as no shortfall*, and a consent about to lapse is exactly a known future
    gap in what will be known.
    """
    assert [c.kind for c in _consent(3)] == ["partial"]


def test_an_expiry_already_passed_is_degraded_because_it_is_no_longer_a_warning() -> None:
    """It has stopped being about the future. The connection has not failed soon — it has failed."""
    caveats = _consent(-1)

    assert len(caveats) == 1
    assert caveats[0].kind == "degraded"
    assert "EXPIRED" in caveats[0].detail


def test_an_expiry_far_out_is_not_permanently_lit() -> None:
    """A notice present on every answer for months is one a reader learns to skip.

    That is the same "true and useless" failure the two warning tuples exist to
    prevent, and it is why the threshold is a threshold rather than a flag on
    the presence of a date.
    """
    assert _consent(query.CONSENT_EXPIRY_WARNING_DAYS + 1) == []


def test_a_connection_never_polled_for_consent_raises_nothing_here() -> None:
    """🔴 Silence, and it is honest silence.

    A null means the Item has not been fetched since the column existed — which
    is NOT "consent does not expire". Inventing a warning from the absence would
    fire on every connection in a store that has not re-synced; inventing
    reassurance would be worse. The connection's unmeasured state is already
    reported by its own `partial`.
    """
    from bankmachine.store.types import now_utc

    assert (
        query._consent_caveats(
            name="An Institution", connection_id=1, expires_at=None, now=now_utc()
        )
        == []
    )
