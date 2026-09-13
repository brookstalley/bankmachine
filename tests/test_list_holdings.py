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
from sqlalchemy import insert, update

from bankmachine import mcp, query
from bankmachine.config import Config
from bankmachine.connector import ACCOUNTS_GET, INVESTMENTS_HOLDINGS_GET
from bankmachine.derivers import ALL_DERIVERS
from bankmachine.store.derivation import apply_response
from bankmachine.store.engine import writer_connection
from bankmachine.store.schema import connections, holdings, institutions
from bankmachine.store.types import UtcInstant, utc_instant

FIXTURES = Path(__file__).parent / "connector" / "fixtures"

CAPTURED = utc_instant(datetime(2026, 9, 10, 14, 0, tzinfo=UTC))
CAPTURED_LATER = utc_instant(datetime(2026, 9, 12, 14, 0, tzinfo=UTC))

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
