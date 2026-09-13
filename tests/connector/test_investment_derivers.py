"""The holdings deriver (FR-3, AC-3.2): securities, positions, and the day they land on.

The oracle for the happy path is the response the aggregator actually sent --
`fixtures/investments_holdings_get.json`, recorded live from an Item enrolled
with the investments product -- and every hostile case is that same shape with
one field changed. A fixture invented here could be written to agree with
whatever the deriver happens to do; a recorded one cannot.

🔴 The one thing the recorded payload CANNOT exercise is the undenominable row:
every security in it is USD *(`api-notes-plaid.md` §22)*. That case is
constructed, and this note is why the construction is not laziness.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert, select

from bankmachine.config import Config
from bankmachine.connector import ACCOUNTS_GET, INVESTMENTS_HOLDINGS_GET
from bankmachine.derivers import ALL_DERIVERS
from bankmachine.store.derivation import DerivationError, apply_response
from bankmachine.store.engine import reader_connection, writer_connection
from bankmachine.store.schema import (
    INVESTMENTS_DOMAIN,
    accounts,
    connections,
    holdings,
    institutions,
    manual_imports,
    refused_holdings,
    securities,
    sync_state,
)
from bankmachine.store.types import UtcInstant, calendar_date, utc_instant

FIXTURES = Path(__file__).parent / "fixtures"

#: Fixed instants, so a test that cares which capture won compares against
#: values it chose rather than against whatever the clock said.
RECEIVED = utc_instant(datetime(2026, 9, 12, 14, 0, tzinfo=UTC))
SAME_DAY_LATER = utc_instant(datetime(2026, 9, 12, 20, 30, tzinfo=UTC))
SAME_DAY_EARLIER = utc_instant(datetime(2026, 9, 12, 6, 15, tzinfo=UTC))

CONNECTION_ID = 1


def recorded() -> dict[str, Any]:
    """The live capture, parsed the way the deriver parses it: numbers as text."""
    body: dict[str, Any] = json.loads(
        (FIXTURES / "investments_holdings_get.json").read_bytes(), parse_float=str
    )
    return body


def _accounts_body(entries: list[dict[str, Any]]) -> bytes:
    """The roster a holdings reply hangs from, in `/accounts/get`'s own shape."""
    return json.dumps(
        {"accounts": entries, "item": {"item_id": "item-x"}, "request_id": "req-accounts"}
    ).encode()


def _roster_from(payload: dict[str, Any]) -> bytes:
    """The accounts the recorded holdings reply itself lists.

    Taken from the same capture rather than hand-written: the positions reference
    these ids, and a roster typed out beside them would be one transcription
    error away from a test that proves the refusal path instead.
    """
    return _accounts_body(payload["accounts"])


@pytest.fixture
def enrolled(initialized_config: Config) -> Config:
    """A connection with the recorded capture's own accounts already derived.

    Enrollment and `/accounts/get` are other build steps' work, so the rows are
    put in the way the sync path puts them: the accounts through their own
    deriver, the connection row directly.
    """
    _seed_connection(initialized_config)
    _apply(initialized_config, ACCOUNTS_GET.path, _roster_from(recorded()))
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
) -> None:
    with writer_connection(config) as conn:
        apply_response(
            conn,
            connection_id=connection_id,
            endpoint=endpoint,
            body=body,
            received_at=received_at,
            derivers=ALL_DERIVERS,
        )


def _holdings_body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode()


def rows(config: Config, table: Any) -> list[dict[str, Any]]:
    with reader_connection(config) as conn:
        return [dict(row) for row in conn.execute(select(table)).mappings()]


def stored_rows(config: Config, entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Every stored position matching one payload entry's account AND security.

    🔴 Both halves, because the security alone does not identify a position: the
    recorded capture holds one instrument in two different accounts, and a lookup
    by security returns two rows that a test reading only the first would compare
    against the wrong one.
    """
    with reader_connection(config) as conn:
        result = conn.execute(
            select(holdings)
            .join(securities, securities.c.security_id == holdings.c.security_id)
            .join(accounts, accounts.c.account_id == holdings.c.account_id)
            .where(
                securities.c.source_security_id == entry["security_id"],
                accounts.c.source_account_id == entry["account_id"],
            )
        )
        return [dict(row) for row in result.mappings()]


def stored_row(config: Config, entry: dict[str, Any]) -> dict[str, Any]:
    matched = stored_rows(config, entry)
    assert len(matched) == 1, f"expected one stored position, found {len(matched)}"
    return matched[0]


def refusals(config: Config) -> list[tuple[Any, ...]]:
    """Every recorded refusal, named by the capture's own ids rather than local ones.

    `(account, security, currency, capture day, captured_at)` -- the capture's ids,
    so an assertion compares against the payload entry it altered rather than
    against whatever local id the store happened to allocate.
    """
    with reader_connection(config) as conn:
        result = conn.execute(
            select(
                accounts.c.source_account_id,
                securities.c.source_security_id,
                refused_holdings.c.currency,
                refused_holdings.c.as_of_date,
                refused_holdings.c.captured_at,
            )
            .select_from(
                refused_holdings.join(
                    accounts, accounts.c.account_id == refused_holdings.c.account_id
                ).join(securities, securities.c.security_id == refused_holdings.c.security_id)
            )
            .order_by(accounts.c.source_account_id, securities.c.source_security_id)
        )
        return [tuple(row) for row in result.all()]


def _in_unit(code: str) -> dict[str, Any]:
    """The recorded capture with its first position moved into `code`."""
    payload = recorded()
    payload["holdings"][0]["iso_currency_code"] = None
    payload["holdings"][0]["unofficial_currency_code"] = code
    return payload


# --------------------------------------------------------------------------
# The recorded capture, end to end
# --------------------------------------------------------------------------


def test_the_recorded_capture_lands_as_securities_and_positions(enrolled: Config) -> None:
    """AC-3.2's happy path, against the payload the aggregator actually sent."""
    payload = recorded()
    _apply(enrolled, INVESTMENTS_HOLDINGS_GET.path, _holdings_body(payload))

    stored_securities = rows(enrolled, securities)
    stored_holdings = rows(enrolled, holdings)
    assert len(stored_securities) == len(payload["securities"])
    assert len(stored_holdings) == len(payload["holdings"])
    assert {row["source_security_id"] for row in stored_securities} == {
        entry["security_id"] for entry in payload["securities"]
    }
    for row in stored_holdings:
        assert row["source"] == "aggregator"
        assert row["raw_response_id"] is not None
        assert row["manual_import_id"] is None
        # 🔴 The capture's own day, not the price's. The recorded prices are
        # `2021-05-25`; filing today's observation under that date would leave
        # the series with a hole on every day the sync actually ran.
        assert row["as_of_date"] == calendar_date(RECEIVED.date())
        assert row["captured_at"] == RECEIVED


def test_a_position_records_the_value_the_capture_states(enrolled: Config) -> None:
    """One row, checked against the payload rather than against the deriver."""
    payload = recorded()
    entry = max(payload["holdings"], key=lambda h: Decimal(h["institution_value"]))
    _apply(enrolled, INVESTMENTS_HOLDINGS_GET.path, _holdings_body(payload))

    row = stored_row(enrolled, entry)

    assert row["quantity"] == entry["quantity"]
    assert row["market_value_minor"] == int(
        (Decimal(entry["institution_value"]) * 100).to_integral_value()
    )
    assert row["currency"] == entry["iso_currency_code"]


def test_a_derived_capture_does_not_claim_the_investments_domain_is_current(
    enrolled: Config,
) -> None:
    """🔴 One body is half the pull, and half a pull is not a fresh domain.

    Positions and the investment-transaction window share one
    `sync_state.domain`, so a holdings capture landing says nothing about whether
    the window did. Stamping `last_success_at` here would report a portfolio as
    current while most of its transaction history was still missing -- which is
    the silent staleness this product exists to refuse, produced by its own
    bookkeeping. The sync command stamps it once both feeds are in
    (`store/sync_domains.py`), and a rebuild replaying an archived body therefore
    cannot forge a freshness claim out of a year-old capture either.
    """
    _apply(enrolled, INVESTMENTS_HOLDINGS_GET.path, _holdings_body(recorded()))

    with reader_connection(enrolled) as conn:
        row = (
            conn.execute(select(sync_state).where(sync_state.c.domain == INVESTMENTS_DOMAIN))
            .mappings()
            .one_or_none()
        )
    assert row is None or row["last_success_at"] is None


def test_a_second_capture_of_the_same_payload_changes_nothing(enrolled: Config) -> None:
    """AC-2.4, at the level the operator meets it: running twice is a no-op."""
    payload = recorded()
    _apply(enrolled, INVESTMENTS_HOLDINGS_GET.path, _holdings_body(payload))
    before = rows(enrolled, holdings)

    _apply(enrolled, INVESTMENTS_HOLDINGS_GET.path, _holdings_body(payload), received_at=RECEIVED)

    assert rows(enrolled, holdings) == before


# --------------------------------------------------------------------------
# Securities: one row per instrument, converged
# --------------------------------------------------------------------------


def test_a_security_seen_twice_is_one_row(enrolled: Config) -> None:
    """Upserted on the aggregator's id, because positions reference the local one."""
    payload = recorded()
    _apply(enrolled, INVESTMENTS_HOLDINGS_GET.path, _holdings_body(payload))
    first = rows(enrolled, securities)

    renamed = recorded()
    renamed["securities"][0]["name"] = "Renamed Fund"
    _apply(
        enrolled,
        INVESTMENTS_HOLDINGS_GET.path,
        _holdings_body(renamed),
        received_at=SAME_DAY_LATER,
    )
    second = rows(enrolled, securities)

    assert len(second) == len(first)
    assert {row["security_id"] for row in second} == {row["security_id"] for row in first}
    by_source = {row["source_security_id"]: row for row in second}
    assert by_source[renamed["securities"][0]["security_id"]]["name"] == "Renamed Fund"


def test_an_older_capture_does_not_reinstate_a_stale_security_name(enrolled: Config) -> None:
    """🔴 Replay order must not decide what a security is called.

    A rebuild replays the archive, and an older response applied after a newer
    one would otherwise overwrite the current name with the superseded one --
    reported as content the rebuild could not reproduce, days from the cause.
    """
    current = recorded()
    current["securities"][0]["name"] = "Current Name"
    _apply(enrolled, INVESTMENTS_HOLDINGS_GET.path, _holdings_body(current))

    stale = recorded()
    stale["securities"][0]["name"] = "Stale Name"
    _apply(
        enrolled,
        INVESTMENTS_HOLDINGS_GET.path,
        _holdings_body(stale),
        received_at=SAME_DAY_EARLIER,
    )

    by_source = {row["source_security_id"]: row for row in rows(enrolled, securities)}
    assert by_source[current["securities"][0]["security_id"]]["name"] == "Current Name"


def test_a_position_in_a_security_the_body_never_listed_is_refused(enrolled: Config) -> None:
    """The instrument a position is in is not something this system can supply."""
    payload = recorded()
    payload["securities"] = [
        entry
        for entry in payload["securities"]
        if entry["security_id"] != payload["holdings"][0]["security_id"]
    ]

    with pytest.raises(DerivationError, match="securities list does not carry"):
        _apply(enrolled, INVESTMENTS_HOLDINGS_GET.path, _holdings_body(payload))


# --------------------------------------------------------------------------
# Money and quantity (AC-6.2, and the norm a quantity is NOT money)
# --------------------------------------------------------------------------


def test_a_quantity_keeps_every_digit_a_float_would_have_dropped(enrolled: Config) -> None:
    """🔴 Fractional shares are routine and a float is lossy before anyone looks.

    The value here round-trips through `float` to a different number, so a
    deriver that had parsed the body a second way fails this rather than passing
    by a rounding accident.
    """
    lossy = "0.1234567890123456789"
    assert str(float(lossy)) != lossy

    payload = recorded()
    payload["holdings"][0]["quantity"] = lossy
    _apply(enrolled, INVESTMENTS_HOLDINGS_GET.path, _holdings_body(payload))

    stored = stored_row(enrolled, payload["holdings"][0])["quantity"]
    assert stored == lossy
    assert Decimal(stored) == Decimal(lossy)


def test_a_quantity_that_is_not_a_number_is_refused(enrolled: Config) -> None:
    payload = recorded()
    payload["holdings"][0]["quantity"] = "a handful"

    with pytest.raises(DerivationError, match="not a number"):
        _apply(enrolled, INVESTMENTS_HOLDINGS_GET.path, _holdings_body(payload))


def test_a_sub_cent_valuation_is_rounded_half_even_and_said_so(
    enrolled: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """A valuation is price times quantity, so it arrives finer than the cent.

    Rounded rather than refused -- that is the ledger/valuation split the data
    model draws -- and logged every time, because rounding nobody can see is the
    silent loss the convention exists to prevent.
    """
    payload = recorded()
    entry = payload["holdings"][0]
    entry["institution_value"] = "23631.9805"

    with caplog.at_level(logging.INFO, logger="bankmachine.connector.plaid.derivers"):
        _apply(enrolled, INVESTMENTS_HOLDINGS_GET.path, _holdings_body(payload))

    stored = stored_row(enrolled, entry)["market_value_minor"]
    # 23631.9805 -> half-even on the half cent lands on the even digit: 2363198.
    assert stored == 2363198
    assert any("rounded to" in record.getMessage() for record in caplog.records)


def test_a_position_in_an_unknown_unit_costs_that_position_and_no_other(
    enrolled: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """🔴 A row's refusal, never the account's.

    A portfolio holding one instrument in a unit this build has no exponent for
    would otherwise report nothing at all, and the rest of it is perfectly
    denominable. Constructed rather than recorded because every security in the
    live capture is USD.
    """
    payload = recorded()
    odd = payload["holdings"][0]
    odd["iso_currency_code"] = None
    odd["unofficial_currency_code"] = "ZZZ"

    with caplog.at_level(logging.WARNING, logger="bankmachine.connector.plaid.derivers"):
        _apply(enrolled, INVESTMENTS_HOLDINGS_GET.path, _holdings_body(payload))

    assert len(rows(enrolled, holdings)) == len(payload["holdings"]) - 1
    assert stored_rows(enrolled, odd) == []
    assert any("cannot express in minor units" in r.getMessage() for r in caplog.records)
    # 🔴 And the refusal is recorded where a read can name it. A log line is seen
    # by nobody who calls a tool, so without this the account's other positions
    # are served as though they were all of them.
    assert refusals(enrolled) == [
        (odd["account_id"], odd["security_id"], "ZZZ", calendar_date(RECEIVED.date()), RECEIVED)
    ], "the refused position left no record a read could name it from"


def test_a_position_with_no_stated_currency_is_not_recorded_under_a_borrowed_one(
    enrolled: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """`holdings.currency` is NOT NULL, and a unit taken from a neighbour is how
    a total silently mixes two of them."""
    payload = recorded()
    entry = payload["holdings"][0]
    entry["iso_currency_code"] = None
    entry["unofficial_currency_code"] = None

    with caplog.at_level(logging.WARNING, logger="bankmachine.connector.plaid.derivers"):
        _apply(enrolled, INVESTMENTS_HOLDINGS_GET.path, _holdings_body(payload))

    assert len(rows(enrolled, holdings)) == len(payload["holdings"]) - 1
    assert any("states no currency" in r.getMessage() for r in caplog.records)
    assert refusals(enrolled) == [
        (entry["account_id"], entry["security_id"], None, calendar_date(RECEIVED.date()), RECEIVED)
    ], "a position with no stated unit left no record, or recorded a unit nobody stated"


def test_a_capture_whose_every_position_derives_records_no_refusal(enrolled: Config) -> None:
    """The record's ABSENCE is what a read takes to mean nothing was left out."""
    _apply(enrolled, INVESTMENTS_HOLDINGS_GET.path, _holdings_body(recorded()))

    assert refusals(enrolled) == []


def test_a_refusal_keeps_the_first_capture_of_its_day_as_a_position_does(enrolled: Config) -> None:
    """🔴 The append rule a position obeys, applied to the record standing in for one.

    A later capture the same day leaves the day's record alone, so what a read
    names does not depend on what time anyone happened to sync.
    """
    _apply(enrolled, INVESTMENTS_HOLDINGS_GET.path, _holdings_body(_in_unit("ZZZ")))
    _apply(
        enrolled,
        INVESTMENTS_HOLDINGS_GET.path,
        _holdings_body(_in_unit("QQQ")),
        received_at=SAME_DAY_LATER,
    )

    assert [(r[2], r[4]) for r in refusals(enrolled)] == [("ZZZ", RECEIVED)]


def test_an_earlier_archived_refusal_replaces_the_record_a_later_capture_wrote(
    enrolled: Config,
) -> None:
    """Replay order must not decide which refusal a day keeps, or a rebuild
    reports content it could not reproduce."""
    _apply(
        enrolled,
        INVESTMENTS_HOLDINGS_GET.path,
        _holdings_body(_in_unit("QQQ")),
        received_at=SAME_DAY_LATER,
    )
    _apply(
        enrolled,
        INVESTMENTS_HOLDINGS_GET.path,
        _holdings_body(_in_unit("ZZZ")),
        received_at=SAME_DAY_EARLIER,
    )

    assert [(r[2], r[4]) for r in refusals(enrolled)] == [("ZZZ", SAME_DAY_EARLIER)]


def test_a_position_of_unstated_value_is_refused_rather_than_zeroed(enrolled: Config) -> None:
    payload = recorded()
    payload["holdings"][0]["institution_value"] = None

    with pytest.raises(DerivationError, match="NOT NULL"):
        _apply(enrolled, INVESTMENTS_HOLDINGS_GET.path, _holdings_body(payload))


def test_a_cost_basis_the_capture_omits_is_stored_as_absent(enrolled: Config) -> None:
    """The aggregator documents it nullable, and absent is not zero."""
    payload = recorded()
    entry = payload["holdings"][0]
    entry["cost_basis"] = None
    _apply(enrolled, INVESTMENTS_HOLDINGS_GET.path, _holdings_body(payload))

    assert stored_row(enrolled, entry)["cost_basis_minor"] is None


def test_a_position_records_the_date_of_the_price_it_was_valued_at(enrolled: Config) -> None:
    """🔴 The only date the aggregator puts on a position, kept beside the capture day.

    The recorded capture values every position at a price years older than the
    day it was captured, so this compares against the capture's own field and
    asserts the two dates differ -- a deriver that stamped the capture day would
    otherwise agree with itself.
    """
    payload = recorded()
    entry = payload["holdings"][0]
    _apply(enrolled, INVESTMENTS_HOLDINGS_GET.path, _holdings_body(payload))

    row = stored_row(enrolled, entry)
    assert row["price_as_of"] == calendar_date(date.fromisoformat(entry["institution_price_as_of"]))
    assert row["price_as_of"] != row["as_of_date"], (
        "the capture's price date equals its capture day, so this cannot tell the two apart"
    )


def test_a_price_date_the_capture_omits_is_stored_as_absent_not_as_the_capture_day(
    enrolled: Config,
) -> None:
    """Null means unknown. The capture day is the one value it must never be filled with."""
    payload = recorded()
    entry = payload["holdings"][0]
    del entry["institution_price_as_of"]
    _apply(enrolled, INVESTMENTS_HOLDINGS_GET.path, _holdings_body(payload))

    assert stored_row(enrolled, entry)["price_as_of"] is None


def test_a_price_date_that_is_not_a_date_is_refused(enrolled: Config) -> None:
    payload = recorded()
    payload["holdings"][0]["institution_price_as_of"] = "last Tuesday"

    with pytest.raises(DerivationError, match="not a calendar date"):
        _apply(enrolled, INVESTMENTS_HOLDINGS_GET.path, _holdings_body(payload))


# --------------------------------------------------------------------------
# The append rule, shared with the balance series
# --------------------------------------------------------------------------


def test_the_first_capture_of_a_day_is_the_one_the_day_keeps(enrolled: Config) -> None:
    """🔴 Not the last, so the series does not depend on what time anyone looked."""
    first = recorded()
    first["holdings"][0]["institution_value"] = "100.00"
    _apply(enrolled, INVESTMENTS_HOLDINGS_GET.path, _holdings_body(first))

    later = recorded()
    later["holdings"][0]["institution_value"] = "999.00"
    _apply(
        enrolled,
        INVESTMENTS_HOLDINGS_GET.path,
        _holdings_body(later),
        received_at=SAME_DAY_LATER,
    )

    stored = stored_row(enrolled, first["holdings"][0])
    assert stored["market_value_minor"] == 10000
    assert stored["captured_at"] == RECEIVED


def test_an_earlier_archived_capture_replaces_the_row_a_later_one_wrote(
    enrolled: Config,
) -> None:
    """Replay must land on the earliest capture whatever order it runs in.

    Otherwise a rebuild's result depends on the order rows happened to be written
    the first time, which is not a property anything could check afterwards.
    """
    later = recorded()
    later["holdings"][0]["institution_value"] = "999.00"
    _apply(
        enrolled,
        INVESTMENTS_HOLDINGS_GET.path,
        _holdings_body(later),
        received_at=SAME_DAY_LATER,
    )

    earlier = recorded()
    earlier["holdings"][0]["institution_value"] = "100.00"
    _apply(
        enrolled,
        INVESTMENTS_HOLDINGS_GET.path,
        _holdings_body(earlier),
        received_at=SAME_DAY_EARLIER,
    )

    stored = stored_row(enrolled, earlier["holdings"][0])
    assert stored["market_value_minor"] == 10000
    assert stored["captured_at"] == SAME_DAY_EARLIER


def test_a_row_the_operator_imported_is_never_replaced_by_a_capture(enrolled: Config) -> None:
    """🔴 A rebuild deletes exactly the rows carrying a `raw_response_id`.

    Overwriting a manual row with an aggregator one would make the operator's own
    statement disappear a rebuild later, with nothing connecting the loss to the
    sync that caused it.
    """
    payload = recorded()
    entry = payload["holdings"][0]
    # A position references a security, so the imported row is seeded after a
    # capture on a DIFFERENT day -- which is also what gives the account and
    # security their local ids.
    _apply(
        enrolled,
        INVESTMENTS_HOLDINGS_GET.path,
        _holdings_body(payload),
        received_at=utc_instant(datetime(2026, 9, 11, 9, 0, tzinfo=UTC)),
    )
    seeded = stored_row(enrolled, entry)
    account_id = seeded["account_id"]
    security_id = seeded["security_id"]
    as_of = calendar_date(RECEIVED.date())

    with writer_connection(enrolled) as conn:
        import_key = conn.execute(
            insert(manual_imports).values(
                account_id=account_id,
                adapter="csv",
                source_name="a brokerage statement",
                file_sha256="0" * 64,
                file_bytes=1024,
                imported_at=RECEIVED,
                rows_seen=1,
                rows_applied=1,
            )
        ).inserted_primary_key
        assert import_key is not None  # an INTEGER PRIMARY KEY insert always yields one
        conn.execute(
            insert(holdings).values(
                account_id=account_id,
                security_id=security_id,
                as_of_date=as_of,
                quantity="1",
                market_value_minor=4242,
                currency="USD",
                captured_at=SAME_DAY_LATER,
                source="manual",
                raw_response_id=None,
                manual_import_id=import_key[0],
                derivation_version_id=seeded["derivation_version_id"],
            )
        )

    _apply(enrolled, INVESTMENTS_HOLDINGS_GET.path, _holdings_body(payload), received_at=RECEIVED)

    with reader_connection(enrolled) as conn:
        kept = conn.execute(
            select(holdings.c.market_value_minor, holdings.c.source).where(
                holdings.c.account_id == account_id,
                holdings.c.security_id == security_id,
                holdings.c.as_of_date == as_of,
            )
        ).one()
    assert kept[0] == 4242
    assert kept[1] == "manual"


# --------------------------------------------------------------------------
# What a capture cannot hang from
# --------------------------------------------------------------------------


def test_a_position_for_an_account_this_connection_has_no_row_for_is_refused(
    enrolled: Config,
) -> None:
    """A portfolio hanging from nothing is worse than a refusal that says so."""
    payload = recorded()
    payload["holdings"][0]["account_id"] = "an-account-nobody-derived"

    with pytest.raises(DerivationError, match="no row for"):
        _apply(enrolled, INVESTMENTS_HOLDINGS_GET.path, _holdings_body(payload))


def test_a_capture_archived_without_a_connection_is_refused(enrolled: Config) -> None:
    """Positions belong to one connection's accounts and nothing else says which."""
    with pytest.raises(DerivationError, match="without a connection"):
        _apply(
            enrolled,
            INVESTMENTS_HOLDINGS_GET.path,
            _holdings_body(recorded()),
            connection_id=None,
        )


def test_a_position_for_an_account_the_roster_no_longer_lists_still_derives(
    initialized_config: Config,
) -> None:
    """🔴 The failure this avoids is permanent, which is what makes it worth avoiding.

    An account that has closed, been de-selected in Account Select, or stopped
    being shared drops out of `/accounts/get` while the investments reply keeps
    reporting its positions. Refusing the response for it would roll back every
    OTHER position too — on this run, on every run after it, and on every rebuild
    of the archive, because the same body is replayed each time.

    The aggregator names the account in the same body, so the roster it carries
    is derived first and the position has somewhere to hang.
    """
    _seed_connection(initialized_config)
    payload = recorded()
    held = {entry["account_id"] for entry in payload["holdings"]}
    # A roster from before those accounts went missing: everything except them.
    partial = [account for account in payload["accounts"] if account["account_id"] not in held]
    assert partial and len(partial) < len(payload["accounts"]), "the roster must really differ"
    _apply(initialized_config, ACCOUNTS_GET.path, _accounts_body(partial))

    _apply(initialized_config, INVESTMENTS_HOLDINGS_GET.path, _holdings_body(payload))

    assert len(rows(initialized_config, holdings)) == len(payload["holdings"])


def test_one_position_listed_twice_in_a_body_lands_once(enrolled: Config) -> None:
    """A body repeating a position is a duplicate key, not a second position.

    The day-claim rule answers it without a special case — what is already there
    was written by this same response, so it stands — and that is worth a test
    because the alternative is an integrity error escaping the derivation as a
    class no caller catches.
    """
    payload = recorded()
    payload["holdings"].append(dict(payload["holdings"][0]))

    _apply(enrolled, INVESTMENTS_HOLDINGS_GET.path, _holdings_body(payload))

    assert len(rows(enrolled, holdings)) == len(payload["holdings"]) - 1
