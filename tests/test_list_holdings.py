"""`list_holdings`: positions, under one strict row shape.

🔴 **The store is built from the aggregator's recorded capture, through the
sync's own entry point and the shipped derivers.** A position row typed out here
would carry whatever this file believed a column meant, and the whole question
is whether what the deriver WROTE comes back out as the tool publishes it.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, insert, select, update

from bankmachine import envelope, mcp, query
from bankmachine.config import Config
from bankmachine.connector import ACCOUNTS_GET, INVESTMENTS_HOLDINGS_GET
from bankmachine.derivers import ALL_DERIVERS
from bankmachine.store.derivation import apply_response
from bankmachine.store.engine import reader_connection, writer_connection
from bankmachine.store.schema import (
    TRANSACTIONS_DOMAIN,
    connections,
    holdings,
    institutions,
    refused_holdings,
    sync_state,
)
from bankmachine.store.schema import accounts as accounts_table
from bankmachine.store.types import UtcInstant, utc_instant

FIXTURES = Path(__file__).parent / "connector" / "fixtures"

CAPTURED = utc_instant(datetime(2026, 9, 10, 14, 0, tzinfo=UTC))
CAPTURED_LATER = utc_instant(datetime(2026, 9, 12, 14, 0, tzinfo=UTC))
CAPTURED_SAME_DAY = utc_instant(datetime(2026, 9, 10, 20, 0, tzinfo=UTC))

#: The keys every row must carry, read off the published schema rather than
#: listed here, so a field added to the definition is a field this checks.
ROW_KEYS = frozenset(
    next(d for d in mcp._tool_definitions() if d["name"] == "list_holdings")["outputSchema"][
        "properties"
    ]["rows"]["items"]["properties"]
)


def recorded() -> dict[str, Any]:
    """The live capture, parsed the way the deriver parses it: numbers as text."""
    body: dict[str, Any] = json.loads(
        (FIXTURES / "investments_holdings_get.json").read_bytes(), parse_float=str
    )
    return body


def _enroll(config: Config) -> None:
    with writer_connection(config) as conn:
        primary_key = conn.execute(
            insert(institutions).values(
                source_institution_id="ins_holdings_test",
                name="Example Brokerage",
                first_seen_at=CAPTURED,
                last_seen_at=CAPTURED,
            )
        ).inserted_primary_key
        assert primary_key is not None  # an INTEGER PRIMARY KEY insert always yields one
        conn.execute(
            insert(connections).values(
                connection_id=1,
                institution_id=primary_key[0],
                source_connection_id="item-holdings",
                credential_ref="connection:sandbox:item-holdings",
                capabilities='["investments"]',
                status="active",
                enrolled_at=CAPTURED,
                created_at=CAPTURED,
                updated_at=CAPTURED,
            )
        )


def _apply(config: Config, endpoint: str, body: dict[str, Any], received_at: UtcInstant) -> None:
    with writer_connection(config) as conn:
        apply_response(
            conn,
            connection_id=1,
            endpoint=endpoint,
            body=json.dumps(body).encode(),
            received_at=received_at,
            derivers=ALL_DERIVERS,
        )


def _seed(config: Config, payload: dict[str, Any], received_at: UtcInstant = CAPTURED) -> None:
    """The roster the capture names, then the capture itself."""
    _apply(
        config,
        ACCOUNTS_GET.path,
        {"accounts": payload["accounts"], "item": {"item_id": "item-holdings"}},
        received_at,
    )
    _apply(config, INVESTMENTS_HOLDINGS_GET.path, payload, received_at)


@pytest.fixture
def enrolled(initialized_config: Config) -> Config:
    _enroll(initialized_config)
    return initialized_config


def _cents(value: str) -> int:
    """What a USD valuation becomes, computed here from the text the capture sent."""
    return int((Decimal(value) * 100).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


# --------------------------------------------------------------------------
# The acceptance criteria
# --------------------------------------------------------------------------


def test_every_recorded_position_comes_back_with_quantity_value_and_currency(
    enrolled: Config,
) -> None:
    """AC: every position in the sandbox investments accounts, with its figures.

    Compared as a multiset of (quantity, value, currency) against the capture, so
    a dropped, duplicated or mis-rounded position fails -- and every row carries
    exactly the published keys, so an absent field fails too.
    """
    payload = recorded()
    _seed(enrolled, payload)

    rows = query.list_holdings(enrolled).rows

    expected = sorted(
        (str(h["quantity"]), _cents(str(h["institution_value"])), h["iso_currency_code"])
        for h in payload["holdings"]
    )
    served = sorted(
        (row["quantity"], row["market_value_minor_units"], row["currency"]) for row in rows
    )
    assert served == expected
    for row in rows:
        assert set(row) == ROW_KEYS, "a row's keys disagree with the published schema"


def test_a_store_holding_no_positions_answers_empty_rather_than_erroring(
    initialized_config: Config,
) -> None:
    answer = query.list_holdings(initialized_config)

    assert answer.rows == []


def test_a_missing_datastore_answers_empty_rather_than_erroring(config: Config) -> None:
    """AC-ARCH.3's rule for every tool: an unreadable store is reported, not raised."""
    answer = query.list_holdings(config)

    assert answer.rows == []
    assert answer.warnings, "an unreadable store answered with no word about why it was empty"


# --------------------------------------------------------------------------
# The row shape
# --------------------------------------------------------------------------


def test_an_unknown_cost_basis_is_present_and_null_never_missing(enrolled: Config) -> None:
    """🔴 Nullable, not optional: the key is there and says the institution sent none."""
    payload = recorded()
    for entry in payload["holdings"]:
        entry["cost_basis"] = None
    _seed(enrolled, payload)

    rows = query.list_holdings(enrolled).rows

    assert rows
    assert all("cost_basis_minor_units" in row for row in rows)
    assert all(row["cost_basis_minor_units"] is None for row in rows)


def test_a_known_cost_basis_is_served_in_minor_units(enrolled: Config) -> None:
    """The positive control, so the null above is not simply what every row gets."""
    payload = recorded()
    _seed(enrolled, payload)

    served = sorted(row["cost_basis_minor_units"] for row in query.list_holdings(enrolled).rows)

    assert served == sorted(_cents(str(h["cost_basis"])) for h in payload["holdings"])


def test_a_quantity_reaches_the_wire_as_the_exact_text_the_capture_sent(enrolled: Config) -> None:
    """🔴 No float anywhere on the path, asserted on the bytes a client receives.

    The finest quantity in the capture has more significant digits than a
    careless decimal-to-float-to-text round trip keeps, and it is compared as
    text in the serialized reply rather than after parsing it back.
    """
    payload = recorded()
    _seed(enrolled, payload)
    finest = max((str(h["quantity"]) for h in payload["holdings"]), key=len)

    reply = mcp._handle(
        enrolled,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "list_holdings", "arguments": {}},
        },
    )

    assert reply is not None
    wire = json.dumps(reply["result"]["structuredContent"])
    assert f'"quantity": "{finest}"' in wire


def test_the_row_carries_the_price_date_beside_the_capture_date(enrolled: Config) -> None:
    """A position captured today at a price years old says both."""
    payload = recorded()
    _seed(enrolled, payload)

    rows = query.list_holdings(enrolled).rows

    assert {row["price_as_of"] for row in rows} == {
        h["institution_price_as_of"] for h in payload["holdings"]
    }
    assert {row["as_of_date"] for row in rows} == {CAPTURED.date().isoformat()}


def test_a_price_date_the_store_never_recorded_is_served_null_not_the_capture_day(
    enrolled: Config,
) -> None:
    """🔴 Never coalesced: the capture day is the one value a null must not become."""
    _seed(enrolled, recorded())
    with writer_connection(enrolled) as conn:
        conn.execute(update(holdings).values(price_as_of=None))

    rows = query.list_holdings(enrolled).rows

    assert rows
    assert all(row["price_as_of"] is None for row in rows)


def test_every_row_carries_its_accounts_lifecycle_as_list_accounts_reports_it(
    enrolled: Config,
) -> None:
    payload = recorded()
    _seed(enrolled, payload)
    accounts = {row["account_id"]: row for row in query.list_accounts(enrolled).rows}

    rows = query.list_holdings(enrolled).rows

    assert rows
    for row in rows:
        for key in ("lifecycle", "closed_date", "last_seen_in_roster", "roster_last_observed"):
            assert row[key] == accounts[row["account_id"]][key]


# --------------------------------------------------------------------------
# Which capture is read
# --------------------------------------------------------------------------


def test_the_latest_capture_is_read_per_account_and_not_one_day_store_wide(
    enrolled: Config,
) -> None:
    """🔴 An account not captured on the newest day still answers, from its own newest day.

    The later capture carries ONE account's positions, minus one of them. That
    account must answer from the later day without the position it no longer
    holds; every other account must still answer from the earlier day rather than
    reading as holding nothing.
    """
    earlier = recorded()
    _seed(enrolled, earlier)
    by_account: dict[str, list[dict[str, Any]]] = {}
    for entry in earlier["holdings"]:
        by_account.setdefault(entry["account_id"], []).append(entry)
    assert len(by_account) >= 2, "the capture needs two accounts holding positions"
    moved, *_ = (source for source, entries in by_account.items() if len(entries) >= 2)

    later = copy.deepcopy(earlier)
    later["holdings"] = [h for h in later["holdings"] if h["account_id"] == moved][1:]
    _seed(enrolled, later, CAPTURED_LATER)

    rows = query.list_holdings(enrolled).rows

    later_day = CAPTURED_LATER.date().isoformat()
    earlier_day = CAPTURED.date().isoformat()
    on_later = [row for row in rows if row["as_of_date"] == later_day]
    on_earlier = [row for row in rows if row["as_of_date"] == earlier_day]
    assert len(on_later) == len(later["holdings"])
    assert len(on_earlier) == len(earlier["holdings"]) - len(by_account[moved])
    assert {row["account_id"] for row in on_later}.isdisjoint(
        {row["account_id"] for row in on_earlier}
    ), "an account answered from two capture days at once"


def test_a_capture_day_before_today_is_served_rather_than_nothing(enrolled: Config) -> None:
    """A position is what it was on the day it was captured; today's absence is not zero."""
    long_ago = utc_instant(datetime.now(UTC) - timedelta(days=40))
    _seed(enrolled, recorded(), long_ago)

    rows = query.list_holdings(enrolled).rows

    assert rows
    assert {row["as_of_date"] for row in rows} == {long_ago.date().isoformat()}
    assert date.fromisoformat(rows[0]["as_of_date"]) < datetime.now(UTC).date()


# --------------------------------------------------------------------------
# What an answer discloses about itself
# --------------------------------------------------------------------------


def _warnings(config: Config, kind: str) -> list[str]:
    """The `detail` of every warning of one kind on a `list_holdings` answer."""
    return [w.detail for w in query.list_holdings(config).warnings if w.kind == kind]


def _priced(payload: dict[str, Any], day: date) -> dict[str, Any]:
    """The capture with every position priced as of `day`."""
    for entry in payload["holdings"]:
        entry["institution_price_as_of"] = day.isoformat()
    return payload


def _transactions_landed(config: Config, at: UtcInstant) -> None:
    """The connection's TRANSACTIONS domain, last landed at `at`."""
    with writer_connection(config) as conn:
        conn.execute(
            insert(sync_state).values(
                connection_id=1,
                domain=TRANSACTIONS_DOMAIN,
                last_attempt_at=at,
                last_success_at=at,
                updated_at=at,
            )
        )


def _close(config: Config, account_id: int) -> None:
    """The operator's own closure, recorded the way the lifecycle tests record one."""
    with writer_connection(config) as conn:
        first_seen = conn.execute(
            select(accounts_table.c.first_seen_date).where(
                accounts_table.c.account_id == account_id
            )
        ).scalar_one()
        conn.execute(
            update(accounts_table)
            .where(accounts_table.c.account_id == account_id)
            .values(lifecycle_status="inactive", closed_date=first_seen)
        )


def test_the_new_kind_is_about_this_request_and_not_the_pipeline() -> None:
    """Its absence has to read as information, and only a request-scoped kind's does."""
    assert "positions_not_current" in envelope.REQUEST_SCOPED_KINDS
    assert "positions_not_current" not in envelope.CONNECTION_SCOPED_KINDS


def test_the_recorded_capture_says_its_prices_are_older_than_the_day_it_was_captured(
    enrolled: Config,
) -> None:
    """🔴 The sandbox's own shape: a 2021 price on a capture taken years later.

    Every field on those rows is well-formed, so the warning is the only thing in
    the answer saying the values are not current -- and the rows still come back.
    """
    payload = recorded()
    _seed(enrolled, payload)

    answer = query.list_holdings(enrolled)

    assert answer.rows, "the warning replaced the answer rather than qualifying it"
    details = [w.detail for w in answer.warnings if w.kind == "positions_not_current"]
    assert len(details) == 1
    counts: dict[int, int] = {}
    for row in answer.rows:
        counts[row["account_id"]] = counts.get(row["account_id"], 0) + 1
    for account_id, count in counts.items():
        assert f"{account_id} ({count} position(s), priced as early as" in details[0]
    oldest = min(h["institution_price_as_of"] for h in payload["holdings"])
    assert f"priced as early as {oldest}" in details[0]


@pytest.mark.parametrize(("days_before", "named"), [(4, False), (5, True)])
def test_the_price_threshold_is_more_than_four_calendar_days(
    enrolled: Config, days_before: int, named: bool
) -> None:
    """The boundary from both sides: four days clears a long weekend, five does not.

    The four-day side is also the kind's absence meaning something: a fresh
    capture raises nothing at all.
    """
    _seed(enrolled, _priced(recorded(), CAPTURED.date() - timedelta(days=days_before)))

    assert bool(_warnings(enrolled, "positions_not_current")) is named


def test_an_unknown_price_date_is_named_unknown_rather_than_treated_as_fresh(
    enrolled: Config,
) -> None:
    """🔴 A null price date is not a recent one: nothing vouches for the value."""
    _seed(enrolled, recorded())
    with writer_connection(enrolled) as conn:
        conn.execute(update(holdings).values(price_as_of=None))

    details = _warnings(enrolled, "positions_not_current")

    assert len(details) == 1
    assert "UNKNOWN" in details[0]
    assert "priced as early as" not in details[0], "an unknown date was read as a known one"


@pytest.mark.parametrize(("landed", "named"), [(CAPTURED, False), (CAPTURED_LATER, True)])
def test_a_capture_older_than_its_connections_transactions_is_named(
    enrolled: Config, landed: UtcInstant, named: bool
) -> None:
    """🔴 The investments pull stopped while the rest of the connection carried on.

    Prices are fresh here, so only the capture's age can raise the warning. The
    same-day case is the control: a capture on the day the transactions landed is
    as current as the connection is.
    """
    _seed(enrolled, _priced(recorded(), CAPTURED.date()))
    _transactions_landed(enrolled, landed)

    details = _warnings(enrolled, "positions_not_current")

    assert bool(details) is named
    if named:
        assert f"landed {CAPTURED_LATER.date().isoformat()}" in details[0]


def test_a_refused_position_is_named_under_rule_applied_and_absent_from_the_rows(
    enrolled: Config,
) -> None:
    """🔴 The account holds more than its rows show, and the answer says which position.

    Constructed rather than recorded: every position in the live capture is USD.
    """
    payload = _priced(recorded(), CAPTURED.date())
    odd = payload["holdings"][0]
    odd["iso_currency_code"] = None
    odd["unofficial_currency_code"] = "ZZZ"
    _seed(enrolled, payload)

    answer = query.list_holdings(enrolled)

    assert len(answer.rows) == len(payload["holdings"]) - 1
    with reader_connection(enrolled) as conn:
        account_id, security_id = conn.execute(
            select(refused_holdings.c.account_id, refused_holdings.c.security_id)
        ).one()
    assert not any(
        (row["account_id"], row["security_id"]) == (account_id, security_id) for row in answer.rows
    )
    details = [w.detail for w in answer.warnings if w.kind == "rule-applied"]
    assert len(details) == 1
    assert f"account {account_id}: security {security_id} (ZZZ)" in details[0]


def test_a_capture_with_nothing_refused_carries_no_rule_applied(enrolled: Config) -> None:
    """The absence is the information: every position the capture listed is a row."""
    _seed(enrolled, recorded())

    assert _warnings(enrolled, "rule-applied") == []


def test_an_account_whose_newest_capture_refused_every_position_answers_from_that_day(
    enrolled: Config,
) -> None:
    """🔴 Not from the day before, which would serve what it may no longer hold as current.

    The later capture lists one account's positions, every one in a unit this
    build cannot denominate. That account answers with no rows and names the
    refusals; every other account still answers from the earlier day.
    """
    earlier = recorded()
    _seed(enrolled, earlier)
    moved = earlier["holdings"][0]["account_id"]
    later = copy.deepcopy(earlier)
    later["holdings"] = [h for h in later["holdings"] if h["account_id"] == moved]
    for entry in later["holdings"]:
        entry["iso_currency_code"] = None
        entry["unofficial_currency_code"] = "ZZZ"
    _seed(enrolled, later, CAPTURED_LATER)

    answer = query.list_holdings(enrolled)

    with reader_connection(enrolled) as conn:
        moved_id = conn.execute(
            select(accounts_table.c.account_id).where(accounts_table.c.source_account_id == moved)
        ).scalar_one()
    assert all(row["account_id"] != moved_id for row in answer.rows), (
        "the account answered from a capture older than its newest one"
    )
    assert len(answer.rows) == sum(1 for h in earlier["holdings"] if h["account_id"] != moved)
    details = [w.detail for w in answer.warnings if w.kind == "rule-applied"]
    assert len(details) == 1
    assert f"account {moved_id}:" in details[0]


def test_a_position_on_a_closed_account_says_its_positions_froze(enrolled: Config) -> None:
    """The existing kind, emitted here: a position on a closed account is not today's."""
    _seed(enrolled, recorded())
    held = query.list_holdings(enrolled).rows[0]["account_id"]
    _close(enrolled, held)

    details = _warnings(enrolled, "account_no_longer_active")

    assert len(details) == 1
    assert f"account(s) {held} are declared closed" in details[0]
    assert "positions froze" in details[0]


def test_a_closed_account_holding_no_position_is_not_this_answers_to_name(
    enrolled: Config,
) -> None:
    """Request-scoped: an account this answer is not about raises nothing on it."""
    _seed(enrolled, recorded())
    held = {row["account_id"] for row in query.list_holdings(enrolled).rows}
    with reader_connection(enrolled) as conn:
        idle = [
            int(account_id)
            for account_id in conn.execute(select(accounts_table.c.account_id)).scalars()
            if int(account_id) not in held
        ]
    assert idle, "the capture needs an account that holds no position"
    _close(enrolled, idle[0])

    assert _warnings(enrolled, "account_no_longer_active") == []


def _refusing_first_position(payload: dict[str, Any]) -> dict[str, Any]:
    """The capture with its first position in a unit this build cannot denominate."""
    payload["holdings"][0]["iso_currency_code"] = None
    payload["holdings"][0]["unofficial_currency_code"] = "ZZZ"
    return payload


def _the_disputed_key(config: Config) -> tuple[int, int]:
    """The one refused key, asserted to be held by BOTH tables -- the state under test."""
    with reader_connection(config) as conn:
        account_id, security_id = conn.execute(
            select(refused_holdings.c.account_id, refused_holdings.c.security_id)
        ).one()
        held = conn.execute(
            select(func.count())
            .select_from(holdings)
            .where(holdings.c.account_id == account_id, holdings.c.security_id == security_id)
        ).scalar_one()
    assert held == 1, "the captures did not leave the key in both tables, so nothing is under test"
    return int(account_id), int(security_id)


def test_a_days_first_capture_refusing_a_position_keeps_it_out_of_the_rows(
    enrolled: Config,
) -> None:
    """🔴 Two captures on one day disagree about a position's unit; the FIRST decides.

    The deriver leaves a row in each table -- each keeps its own first capture --
    so this is the state the real producer leaves. The answer must not serve the
    position while its disclosure says the position is absent.
    """
    _seed(enrolled, _refusing_first_position(_priced(recorded(), CAPTURED.date())))
    _seed(enrolled, _priced(recorded(), CAPTURED.date()), CAPTURED_SAME_DAY)
    account_id, security_id = _the_disputed_key(enrolled)

    answer = query.list_holdings(enrolled)

    assert not any(
        (row["account_id"], row["security_id"]) == (account_id, security_id) for row in answer.rows
    ), "a position the day's first capture refused was served as a row"
    details = [w.detail for w in answer.warnings if w.kind == "rule-applied"]
    assert len(details) == 1
    assert f"account {account_id}: security {security_id} (ZZZ)" in details[0]


def test_a_days_first_capture_recording_a_position_is_not_named_absent(enrolled: Config) -> None:
    """The other arrival order: recorded first, refused later the same day."""
    _seed(enrolled, _priced(recorded(), CAPTURED.date()))
    _seed(
        enrolled, _refusing_first_position(_priced(recorded(), CAPTURED.date())), CAPTURED_SAME_DAY
    )
    account_id, security_id = _the_disputed_key(enrolled)

    answer = query.list_holdings(enrolled)

    assert any(
        (row["account_id"], row["security_id"]) == (account_id, security_id) for row in answer.rows
    ), "a position the day's first capture recorded was dropped"
    assert [w.detail for w in answer.warnings if w.kind == "rule-applied"] == [], (
        "the answer named a position absent while serving it"
    )


def _left_behind_by_a_newer_capture(
    config: Config, *, transactions_landed: UtcInstant = CAPTURED_LATER
) -> set[int]:
    """Two captures of one connection, the newer listing only one account's positions.

    Prices are fresh on both days and, by default, the transactions land with the
    newer capture, so the only thing old about the other account is that the
    newer capture listed nothing for it. Returns that account's ids.
    """
    earlier = _priced(recorded(), CAPTURED.date())
    _seed(config, earlier)
    moved = earlier["holdings"][0]["account_id"]
    later = _priced(copy.deepcopy(earlier), CAPTURED_LATER.date())
    later["holdings"] = [h for h in later["holdings"] if h["account_id"] == moved]
    _seed(config, later, CAPTURED_LATER)
    _transactions_landed(config, transactions_landed)
    left = {
        row["account_id"]
        for row in query.list_holdings(config).rows
        if row["as_of_date"] == CAPTURED.date().isoformat()
    }
    assert left, "the capture needs a second account for the newer capture to leave out"
    return left


def test_an_account_left_out_of_a_newer_capture_is_not_blamed_on_the_feed(
    enrolled: Config,
) -> None:
    """🔴 The connection's newer capture listed no position for it, and its feed works.

    The account may simply hold nothing now. Naming that as a stopped feed sends a
    reader to repair a sync that is working.
    """
    left = _left_behind_by_a_newer_capture(enrolled)

    details = _warnings(enrolled, "positions_not_current")

    assert len(details) == 1
    for account_id in left:
        assert (
            f"{account_id} (positions from {CAPTURED.date().isoformat()}; its connection's newest "
            f"capture, {CAPTURED_LATER.date().isoformat()}, listed none for it)"
        ) in details[0]
    assert "stopped arriving" not in details[0], "a working feed was named as a stopped one"


def test_an_account_left_out_of_a_newer_capture_that_is_itself_behind_is_named_for_both(
    enrolled: Config,
) -> None:
    """🔴 Left out of a newer capture, on a connection whose newest capture is ITSELF behind.

    Both facts hold. Saying only that a newer capture listed nothing for it --
    "the feed is working" -- is false here: the transactions landed after every
    capture, so the investments feed has stopped as well, and the account's
    positions may be gone OR merely unseen.
    """
    landed = utc_instant(CAPTURED_LATER + timedelta(days=2))
    left = _left_behind_by_a_newer_capture(enrolled, transactions_landed=landed)

    details = _warnings(enrolled, "positions_not_current")

    assert len(details) == 1
    for account_id in left:
        assert (
            f"{account_id} (positions from {CAPTURED.date().isoformat()}; its connection's newest "
            f"capture, {CAPTURED_LATER.date().isoformat()}, listed none for it)"
        ) in details[0]
        assert (
            f"{account_id} (captured {CAPTURED.date().isoformat()}, its connection's transactions "
            f"landed {landed.date().isoformat()})"
        ) in details[0], "the stopped feed went unsaid for an account a newer capture left out"
    assert "The feed is working" not in details[0], "a stopped feed was called a working one"


def test_a_closed_account_left_out_of_a_newer_capture_is_named_only_as_inactive(
    enrolled: Config,
) -> None:
    """Its captures stopped because it closed, which `account_no_longer_active` already says."""
    for account_id in _left_behind_by_a_newer_capture(enrolled):
        _close(enrolled, account_id)

    assert _warnings(enrolled, "positions_not_current") == []
    assert _warnings(enrolled, "account_no_longer_active"), "the closure itself went unsaid"


# --------------------------------------------------------------------------
# The surface around the tool
# --------------------------------------------------------------------------


def test_the_strict_row_guard_refuses_an_optional_field_on_list_holdings() -> None:
    """The guard sees THIS definition: a field made optional here is a registration failure."""
    definitions = copy.deepcopy(mcp._tool_definitions())
    holdings_tool = next(d for d in definitions if d["name"] == "list_holdings")
    row = holdings_tool["outputSchema"]["properties"]["rows"]["items"]
    row["required"].remove("price_as_of")

    with pytest.raises(mcp.ToolRegistrationError, match="price_as_of"):
        mcp._refuse_optional_row_fields(definitions)


def test_the_primer_no_longer_calls_positions_unanswerable(initialized_config: Config) -> None:
    """🔴 The unforced half: a primer telling an agent positions cannot be answered
    while `list_holdings` serves them sends the agent away from the tool."""
    primer = mcp._instructions(initialized_config)
    cannot = primer[primer.index("THIS SERVER CANNOT ANSWER") :]

    assert "holdings" not in cannot and "positions" not in cannot
    assert "`list_holdings`" not in cannot
