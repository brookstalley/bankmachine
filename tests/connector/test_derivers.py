"""The institutions and accounts derivers (FR-5, FR-6, AC-3.1, AC-6.2, AC-6.3).

Oracles here are written by hand from the recorded responses, never produced by
the deriver under test: a fixture generated from the code it checks agrees with
that code forever, including about whatever it gets wrong.

Every fixture body is real. `institutions_get.json`, `item_get.json` and
`accounts_get.json` were recorded from live sandbox calls; the hostile ones are
those same shapes with a field removed or nulled, so they test what the
aggregator can actually send rather than what would be convenient to send.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Connection as SAConnection
from sqlalchemy import delete, insert, select, update

from bankmachine.config import Config
from bankmachine.connector import ACCOUNTS_GET, INSTITUTIONS_GET, ITEM_GET, Endpoint
from bankmachine.connector.plaid.derivers import (
    PLAID_DERIVERS,
    balance_class_of,
    to_minor,
)
from bankmachine.store import types as store_types
from bankmachine.store.derivation import (
    DerivationContext,
    DerivationError,
    apply_response,
    ensure_derivation_version,
)
from bankmachine.store.engine import transaction, writer_connection
from bankmachine.store.raw import RawResponse
from bankmachine.store.rebuild import content_digest, rebuild
from bankmachine.store.schema import (
    accounts,
    balances_daily,
    connections,
    institutions,
    manual_imports,
)
from bankmachine.store.types import (
    UnknownMinorDigitsError,
    UtcInstant,
    has_minor_digits,
    minor_digits,
    utc_instant,
)

FIXTURES = Path(__file__).parent / "fixtures"

#: A fixed instant, so a test that cares about a stored timestamp compares
#: against a value it chose rather than against whatever the clock said.
RECEIVED = utc_instant(datetime(2026, 9, 7, 12, 30, tzinfo=UTC))
LATER = utc_instant(datetime(2026, 9, 9, 8, 0, tzinfo=UTC))
EARLIER = utc_instant(datetime(2026, 9, 5, 6, 0, tzinfo=UTC))

#: The institution the `store` fixture enrolls a connection against, named once
#: so a test filtering it out is not carrying a copy of the fixture's value.
SEEDED_INSTITUTION = "ins_109508"


def fixture(name: str) -> bytes:
    return (FIXTURES / f"{name}.json").read_bytes()


def _with(body: bytes, mutate: Any) -> bytes:
    """One recorded body with a field changed, so hostile cases stay real shapes."""
    payload = json.loads(body)
    mutate(payload)
    return json.dumps(payload).encode()


@pytest.fixture
def store(initialized_config: Config) -> Config:
    """A datastore with one institution and one connection already enrolled.

    Enrollment is build step 3's job, so the rows are inserted directly here --
    the accounts deriver reads the connection to learn which institution an
    account belongs to, and standing in for enrollment is honest about what this
    chunk does and does not build.

    Yields the *config* rather than an open handle: the writer lock is exclusive
    and does not wait, so a fixture holding a connection open would make
    `rebuild` -- which opens its own -- untestable from here.
    """
    with writer_connection(initialized_config) as conn, transaction(conn):
        seeded = conn.execute(
            insert(institutions).values(
                source_institution_id=SEEDED_INSTITUTION,
                name="First Platypus Bank",
                first_seen_at=RECEIVED,
                last_seen_at=RECEIVED,
            )
        ).inserted_primary_key
        assert seeded is not None  # an INTEGER PRIMARY KEY insert always yields one
        institution_id = seeded[0]
        conn.execute(
            insert(connections).values(
                connection_id=1,
                institution_id=institution_id,
                source_connection_id="item-under-test",
                credential_ref="plaid:sandbox",
                status="active",
                enrolled_at=RECEIVED,
                created_at=RECEIVED,
                updated_at=RECEIVED,
            )
        )
    return initialized_config


@contextmanager
def writing(config: Config) -> Iterator[SAConnection]:
    with writer_connection(config) as conn:
        yield conn


def derive(
    config: Config,
    endpoint: str,
    body: bytes,
    *,
    connection_id: int | None = 1,
    received_at: UtcInstant = RECEIVED,
) -> RawResponse:
    """Archive one response and derive from it, the way the sync path will."""
    with writing(config) as conn:
        return apply_response(
            conn,
            connection_id=connection_id,
            endpoint=endpoint,
            body=body,
            received_at=received_at,
            derivers=PLAID_DERIVERS,
        )


def rows(config: Config, table: Any) -> list[Any]:
    with writing(config) as conn:
        return [dict(row) for row in conn.execute(select(table)).mappings()]


# --------------------------------------------------------------------------
# Money (AC-6.2) — integer minor units, and never a float
# --------------------------------------------------------------------------


def test_a_balance_is_stored_as_integer_minor_units(store: Config) -> None:
    """The schema stores integers, and the conversion is exact or it refuses."""
    body = _with(
        fixture("accounts_get"),
        lambda p: p["accounts"].__setitem__(
            0,
            {
                **p["accounts"][0],
                "balances": {
                    "current": 110.23,
                    "available": 100.0,
                    "limit": None,
                    "iso_currency_code": "USD",
                    "unofficial_currency_code": None,
                },
            },
        ),
    )
    derive(store, str(ACCOUNTS_GET), body)

    balance = rows(store, balances_daily)[0]
    assert balance["current_minor"] == 11023
    assert isinstance(balance["current_minor"], int)
    assert not isinstance(balance["current_minor"], float)
    assert balance["available_minor"] == 10000


def test_an_amount_a_float_would_have_mangled_survives_exactly(store: Config) -> None:
    """🔴 The reason `json.loads` is called with `parse_float=str`.

    `round(70.07 * 100)` is 7007 on this interpreter, but the class of error is
    real and silent: the float nearest 70.07 is not 70.07, and the discrepancy
    lands on whichever value happens to fall the wrong side of a rounding
    boundary. Reading the number as the digits the aggregator sent removes the
    question rather than betting on it.
    """
    awkward = ["70.07", "1234567890123.45", "0.01", "8.29", "1.005"]
    for index, amount in enumerate(awkward):
        body = _with(
            fixture("accounts_get"),
            lambda p, a=amount, i=index: p.__setitem__(
                "accounts",
                [
                    {
                        **p["accounts"][0],
                        "account_id": f"acct-{i}",
                        "balances": {
                            "current": json.loads(a),
                            "available": None,
                            "limit": None,
                            "iso_currency_code": "USD",
                            "unofficial_currency_code": None,
                        },
                    }
                ],
            ),
        )
        derive(
            store,
            str(ACCOUNTS_GET),
            body,
            received_at=utc_instant(datetime(2026, 9, 7, 12, 30 + index, tzinfo=UTC)),
        )

    stored = {r["current_minor"] for r in rows(store, balances_daily)}
    assert stored == {7007, 123456789012345, 1, 829, None} - {None} | {100}, stored


def test_a_sub_cent_valuation_is_rounded_half_even_and_says_so(
    store: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """🔴 The one place this build rounds, and it is a valuation rather than a ledger amount.

    Plaid's own sandbox institution returns a 401k balance of `23631.9805` USD --
    canned data, so the aggregator is deliberately exercising the case and
    sub-cent valuations are a production shape. An investment `current` is price
    times quantity, and no brokerage statement reports hundredths of a cent.

    Half-even because it applies to every valuation on every sync: half-up would
    bias a portfolio's recorded value upward a fraction of a cent at a time, in
    one direction, forever. Logged because rounding nobody can see is the silent
    loss the convention exists to avoid.
    """
    with caplog.at_level(logging.INFO, logger="bankmachine"):
        derive(store, str(ACCOUNTS_GET), fixture("accounts_get"))

    stored = {r["current_minor"] for r in rows(store, balances_daily)}
    assert 2363198 in stored, "the four-decimal valuation did not round to the cent"
    assert any("rounded to" in record.getMessage() for record in caplog.records), (
        "the balance was rounded with nothing recording that it happened"
    )

    # Half-even, asserted on the boundary the rule is about: .005 goes to the
    # even cent in both directions, so repeated rounding does not drift.
    for reported, expected in (("1.005", 100), ("1.015", 102), ("1.025", 102)):
        assert _one_balance(store, reported) == expected, reported


def _one_balance(conn: Config, reported: str) -> int:
    """Derive a single account with the given `current`, and read back what was stored."""
    account = _account(current=json.loads(reported))
    account["account_id"] = f"acct-{reported}"
    body = json.dumps({"accounts": [account], "item": {}, "request_id": "r"}).encode()
    received = utc_instant(datetime(2026, 9, 7, 12, 30, len(reported), tzinfo=UTC))
    derive(conn, str(ACCOUNTS_GET), body, received_at=received)
    with writing(conn) as handle:
        stored = (
            handle.execute(
                select(balances_daily.c.current_minor).where(
                    balances_daily.c.as_of_date == received.date()
                )
            )
            .scalars()
            .all()
        )
    return int(stored[-1])


def test_a_value_the_currency_can_represent_is_never_rounded(
    store: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """The control: rounding is reached only by values that need it.

    Without this, a conversion that quantized everything would pass every
    assertion above while quietly becoming approximate across the board.
    """
    with caplog.at_level(logging.INFO, logger="bankmachine"):
        _derive_account(store, _account(current=110.23))
    assert rows(store, balances_daily)[0]["current_minor"] == 11023
    assert not [r for r in caplog.records if "rounded to" in r.getMessage()]


def test_an_amount_that_arrives_as_a_float_is_refused_rather_than_rounded() -> None:
    """Rounding a float would launder a loss that already happened.

    The valuation rounding above is a recorded approximation of a number this
    build read exactly. A `float` is a number it did not: whatever the binary
    representation dropped is gone before any rounding decision, and quantizing
    it produces a value that looks exact and is not.

    Asserted against `to_minor` directly, because `_payload` parses with
    `parse_float=str` and no float can reach it through the ordinary path -- which
    is the point, and is why this guard is the second layer rather than the first.
    """
    response = RawResponse(
        raw_response_id=1,
        connection_id=1,
        endpoint=str(ACCOUNTS_GET),
        received_at=RECEIVED,
        body=b"{}",
        body_sha256="",
        request_context=None,
    )
    with pytest.raises(DerivationError, match="float has already lost"):
        to_minor(110.23, "USD", "a current balance", response)
    # Positive control: the same value as text converts exactly, so the refusal
    # is about the type rather than about the number.
    assert to_minor("110.23", "USD", "a current balance", response) == 11023


def test_a_currency_with_a_different_minor_unit_is_not_scaled_by_a_hundred() -> None:
    """A JPY balance stored with two minor digits is 100x too large, and plausible."""
    assert minor_digits("JPY") == 0
    assert minor_digits("KWD") == 3
    assert minor_digits("USD") == 2
    # Lowercase too: the field is the aggregator's, not ours.
    assert minor_digits("jpy") == 0


def test_a_currency_this_build_does_not_know_has_no_minor_digits_at_all() -> None:
    """🔴 The exponent is a LOOKUP, and an absent entry is a state rather than a 2.

    The table used to answer 2 for anything it did not recognize, which is the
    ISO convention and is not a fact about `BTC` -- so `0.04217` became `0.04`,
    a 0.4% loss that every total computed from it inherited. There is no default
    to fall back to now, and `has_minor_digits` is how the read path asks the
    same question without catching an exception.
    """
    with pytest.raises(UnknownMinorDigitsError, match="BTC"):
        minor_digits("BTC")
    assert not has_minor_digits("BTC")
    assert not has_minor_digits(None)
    assert has_minor_digits("usd")
    # An ISO currency that is neither an exception nor the reference one: the
    # table enumerates the two-decimal codes rather than defaulting to them, so
    # an ordinary account in one still derives.
    assert minor_digits("EUR") == 2
    assert minor_digits("SEK") == 2


def test_an_account_in_no_stated_currency_is_created_with_a_null_one(
    store: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """🔴 The account EXISTS, with `currency = NULL`, and the balance does not.

    Both of the aggregator's currency fields are documented nullable, and while
    `accounts.currency` was NOT NULL an account of that shape could not be
    written at all -- so it was skipped, it was invisible to `list_accounts`,
    and every transaction on it went on refusing to derive until some later sync
    happened to state a unit. The transactions never needed the account's
    currency: they carry their own, stated per row.

    A null here means *the aggregator has not told us yet* -- never `USD`, never
    the unit of the operator's other accounts. Inventing the unit of every
    amount on an account is the largest version of the mistake this product
    exists to refuse.

    The BALANCE is still not written, and that asymmetry is deliberate: one
    amount with no stated unit is unusable, while an account with no unit yet is
    merely incompletely known.
    """
    body = _with(
        fixture("accounts_get"),
        lambda p: p.__setitem__(
            "accounts",
            [
                {
                    **p["accounts"][0],
                    "account_id": "acct-no-currency",
                    "balances": {
                        "current": 1.0,
                        "available": None,
                        "limit": None,
                        "iso_currency_code": None,
                        "unofficial_currency_code": None,
                    },
                },
                {**p["accounts"][1], "account_id": "acct-ordinary"},
            ],
        ),
    )

    with caplog.at_level(logging.WARNING, logger="bankmachine"):
        derive(store, str(ACCOUNTS_GET), body)

    derived = {row["source_account_id"]: row for row in rows(store, accounts)}
    assert set(derived) == {"acct-no-currency", "acct-ordinary"}, (
        "an account whose unit the aggregator has not stated is invisible rather than honest"
    )
    assert derived["acct-no-currency"]["currency"] is None, (
        "a unit nothing stated was filled in from somewhere"
    )
    assert derived["acct-ordinary"]["currency"] == "USD"
    assert [row["account_id"] for row in rows(store, balances_daily)] == [
        derived["acct-ordinary"]["account_id"]
    ], "a balance was stored under a unit the response did not state"
    assert any("no stated currency" in record.getMessage() for record in caplog.records), (
        "a balance was dropped with nothing recording that it happened"
    )


def test_a_balance_in_a_currency_with_no_known_exponent_is_refused_not_rounded(
    store: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """🔴 `0.04217` is not `0.04`, and a rounded amount is a changed amount.

    `to_minor` used to default an unrecognized code to two minor digits, which is
    ISO's convention and is not a fact about a cryptocurrency: the 0.4% this
    rounding lost was disclosed only in a log line, which no caller of the MCP
    surface or the CLI ever sees. A value this system cannot represent exactly is
    not stored as an approximation -- the row is refused, the raw body keeps it,
    and a build that knows the currency's minor unit derives it later.

    🔴 Refused per ROW, never per connection. `/accounts/get` runs before a
    single page is fetched, so one unrepresentable holding escaping here would
    take the whole institution's history offline on this run and on every run
    after it.
    """
    body = _with(
        fixture("accounts_get"),
        lambda p: p.__setitem__(
            "accounts",
            [
                {
                    **p["accounts"][0],
                    "account_id": "acct-unofficial",
                    "balances": {
                        "current": "0.04217",
                        "available": None,
                        "limit": None,
                        "iso_currency_code": None,
                        "unofficial_currency_code": "BTC",
                    },
                },
                {**p["accounts"][1], "account_id": "acct-ordinary"},
            ],
        ),
    )

    with caplog.at_level(logging.WARNING, logger="bankmachine"):
        derive(store, str(ACCOUNTS_GET), body)

    derived = {row["source_account_id"]: row for row in rows(store, accounts)}
    assert set(derived) == {"acct-unofficial", "acct-ordinary"}, (
        "one unrepresentable balance cost the roster the rest of its accounts"
    )
    assert derived["acct-unofficial"]["currency"] == "BTC", (
        "the unit IS known; it is the scale that is not, and the account should say so"
    )
    assert [row["account_id"] for row in rows(store, balances_daily)] == [
        derived["acct-ordinary"]["account_id"]
    ], "a balance this build cannot express exactly was stored anyway"
    assert any("cannot express in minor units" in r.getMessage() for r in caplog.records)


def test_the_same_balance_derives_exactly_once_the_currency_has_an_exponent(
    store: Config, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half: an exponent that is CONFIGURED is not a guess.

    Adding the currency's minor digits to the lookup is the whole remedy -- the
    amount then converts exactly, nothing is refused, and nothing is warned
    about. What the previous test refuses is an unknown scale, not an unofficial
    currency.
    """
    monkeypatch.setitem(store_types._MINOR_DIGITS, "BTC", 8)
    body = _with(
        fixture("accounts_get"),
        lambda p: p.__setitem__(
            "accounts",
            [
                {
                    **p["accounts"][0],
                    "account_id": "acct-unofficial",
                    "balances": {
                        "current": "0.04217",
                        "available": None,
                        "limit": None,
                        "iso_currency_code": None,
                        "unofficial_currency_code": "BTC",
                    },
                }
            ],
        ),
    )

    with caplog.at_level(logging.WARNING, logger="bankmachine"):
        derive(store, str(ACCOUNTS_GET), body)

    balance = rows(store, balances_daily)[0]
    assert balance["current_minor"] == 4217000, "the amount was scaled to the wrong exponent"
    assert balance["currency"] == "BTC"
    assert not caplog.records, "an exactly-derived amount warned about something"


def test_an_account_whose_currency_this_datastore_knows_survives_a_response_that_omits_it(
    store: Config,
) -> None:
    """The unit is the aggregator's own earlier word about the same account.

    Keeping the account row is what lets its transactions keep deriving; the
    balance is still not written, because storing an amount under a unit taken
    from a different response is the mixing this refusal exists to prevent.
    """
    derive(store, str(ACCOUNTS_GET), _one_account_body("acct-1", current=100.00))

    without_currency = _account(current=250.00)
    without_currency["account_id"] = "acct-1"
    without_currency["balances"]["iso_currency_code"] = None
    derive(
        store,
        str(ACCOUNTS_GET),
        json.dumps({"accounts": [without_currency], "item": {}, "request_id": "r"}).encode(),
        received_at=LATER,
    )

    account = next(row for row in rows(store, accounts) if row["source_account_id"] == "acct-1")
    assert account["currency"] == "USD"
    assert account["last_seen_date"] == LATER.date(), "the account stopped being observed"
    assert [row["as_of_date"] for row in rows(store, balances_daily)] == [RECEIVED.date()], (
        "a balance was stored under a currency the response did not state"
    )


def test_a_null_current_balance_records_an_absent_balance_rather_than_refusing(
    store: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """🔴 `current` is documented nullable, and null means ABSENT, not zero.

    The refusal cost far more than the balance: `/accounts/get` runs before any
    page is fetched, so one account anywhere on the connection with a null
    `current` meant no transactions were fetched for that institution at all,
    ever, and the same body was archived again on every run. The account keeps
    its row -- which is what its transactions hang from -- and the day simply has
    no balance, because `current_minor` is NOT NULL and a zero would be this
    system inventing a figure and recording it as the source's.
    """
    absent = _account(current=None)
    absent["account_id"] = "acct-absent"
    present = _account(current=110.23)
    present["account_id"] = "acct-present"

    with caplog.at_level(logging.WARNING, logger="bankmachine"):
        derive(
            store,
            str(ACCOUNTS_GET),
            json.dumps({"accounts": [absent, present], "item": {}, "request_id": "r"}).encode(),
        )

    assert {row["source_account_id"] for row in rows(store, accounts)} == {
        "acct-absent",
        "acct-present",
    }
    balances = {row["account_id"]: row["current_minor"] for row in rows(store, balances_daily)}
    assert list(balances.values()) == [11023], "a balance was invented for the account with none"
    assert any("no current balance" in record.getMessage() for record in caplog.records)
    # AC-12.4: the roster was still observed, so the accounts do not start
    # reading as no-longer-reported because one of them had no balance.
    observed = rows(store, connections)[0]["roster_observed_date"]
    assert observed == RECEIVED.date()


def test_a_second_capture_on_a_recorded_day_is_rejected_not_merged(store: Config) -> None:
    """🔴 AC-3.1: never overwrite; append. Three records say a second capture is rejected.

    The series must not depend on what time of day anyone happened to look, and
    no aggregator backfills a balance series — so a day already recorded is a day
    already answered. The tempting reading -- that AC-3.1 is "about the series",
    so a same-day refinement is not an overwrite -- is a reinterpretation of a
    ratified norm, and a comment in a deriver is not where one gets made.
    """
    first = derive(
        store,
        str(ACCOUNTS_GET),
        _one_account_body("acct-1", current=100.00),
        received_at=utc_instant(datetime(2026, 9, 7, 9, 0, tzinfo=UTC)),
    )
    derive(
        store,
        str(ACCOUNTS_GET),
        _one_account_body("acct-1", current=999.99),
        received_at=utc_instant(datetime(2026, 9, 7, 21, 0, tzinfo=UTC)),
    )

    balances = rows(store, balances_daily)
    assert len(balances) == 1, "a second capture on the same day added a row"
    assert balances[0]["current_minor"] == 10000, "the evening capture overwrote the morning's"
    assert balances[0]["raw_response_id"] == first.raw_response_id


def test_an_earlier_capture_replayed_later_still_wins(store: Config) -> None:
    """ "First" is decided by comparing captures, not by arriving first.

    Replay order gives the same answer today and stops doing so the moment two
    responses share a `received_at`, which the archive explicitly allows. Without
    this, a rebuild's result would depend on the order rows happened to be
    written in originally.
    """
    derive(
        store,
        str(ACCOUNTS_GET),
        _one_account_body("acct-1", current=999.99),
        received_at=utc_instant(datetime(2026, 9, 7, 21, 0, tzinfo=UTC)),
    )
    derive(
        store,
        str(ACCOUNTS_GET),
        _one_account_body("acct-1", current=100.00),
        received_at=utc_instant(datetime(2026, 9, 7, 9, 0, tzinfo=UTC)),
    )

    balances = rows(store, balances_daily)
    assert len(balances) == 1
    assert balances[0]["current_minor"] == 10000, "the earlier capture did not win on replay"


def test_re_deriving_one_response_changes_no_balance_column(store: Config) -> None:
    """AC-2.4: a re-run changes nothing — including the columns nobody looks at.

    The idempotence test that existed compared `accounts` and `institutions`, so
    it could not see the one table where it failed: an upsert rewrote
    `raw_response_id` and `captured_at` on every derivation.
    """
    body = _one_account_body("acct-1", current=100.00)
    derive(store, str(ACCOUNTS_GET), body)
    once = rows(store, balances_daily)
    derive(store, str(ACCOUNTS_GET), body)

    assert rows(store, balances_daily) == once


def test_a_manual_import_row_is_not_turned_into_an_aggregator_row(store: Config) -> None:
    """🔴 The sharpest case, and the one an upsert got wrong silently.

    A manual-import row carries `manual_import_id` and no `raw_response_id`.
    Overwritten into an aggregator row it becomes something the *next* rebuild
    deletes — because a rebuild empties exactly the rows carrying a
    `raw_response_id` — so the operator's hand-entered balance disappears one
    rebuild later, with nothing connecting the loss to the sync that caused it.
    """
    derive(store, str(ACCOUNTS_GET), _one_account_body("acct-1", current=100.00))
    account_id = rows(store, accounts)[0]["account_id"]
    with writing(store) as conn, transaction(conn):
        conn.execute(delete(balances_daily))
        import_id = conn.execute(
            insert(manual_imports).values(
                account_id=account_id,
                adapter="csv",
                source_name="a statement the operator typed in",
                file_sha256="0" * 64,
                file_bytes=1,
                imported_at=RECEIVED,
                rows_seen=1,
                rows_applied=1,
            )
        ).inserted_primary_key
        assert import_id is not None
        conn.execute(
            insert(balances_daily).values(
                account_id=account_id,
                as_of_date=RECEIVED.date(),
                current_minor=777,
                currency="USD",
                captured_at=RECEIVED,
                source="manual",
                raw_response_id=None,
                manual_import_id=import_id[0],
                derivation_version_id=1,
            )
        )

    derive(store, str(ACCOUNTS_GET), _one_account_body("acct-1", current=100.00))

    balance = rows(store, balances_daily)[0]
    assert balance["current_minor"] == 777, "the operator's own row was overwritten"
    assert balance["source"] == "manual"
    assert balance["raw_response_id"] is None, (
        "the row now carries a raw_response_id, so the next rebuild will delete it"
    )


def test_a_manual_row_survives_an_older_response_replayed_over_it(store: Config) -> None:
    """🔴 The branch the neighbouring test cannot reach, and the case it exists for.

    That test seeds the manual row at the same instant it replays, so the
    capture comparison returns first and the guard below it never runs. This one
    replays an *older* archived response over a later manual row -- which is
    exactly the path that used to delete the operator's hand-entered balance and
    reinsert it as an aggregator row, for the next rebuild to remove.

    Written because the guard was added with nothing asserting it: reverting it
    turned no test red, which is the same silence the finding it came from named.
    """
    derive(store, str(ACCOUNTS_GET), _one_account_body("acct-1", current=100.00))
    account_id = rows(store, accounts)[0]["account_id"]
    with writing(store) as conn, transaction(conn):
        conn.execute(delete(balances_daily))
        import_id = conn.execute(
            insert(manual_imports).values(
                account_id=account_id,
                adapter="csv",
                source_name="a statement the operator typed in",
                file_sha256="0" * 64,
                file_bytes=1,
                imported_at=LATER,
                rows_seen=1,
                rows_applied=1,
            )
        ).inserted_primary_key
        assert import_id is not None
        conn.execute(
            insert(balances_daily).values(
                account_id=account_id,
                as_of_date=EARLIER.date(),
                current_minor=777,
                currency="USD",
                # Later than the response replayed below, so the capture
                # comparison falls through and the guard is what has to hold.
                captured_at=LATER,
                source="manual",
                raw_response_id=None,
                manual_import_id=import_id[0],
                derivation_version_id=1,
            )
        )

    derive(
        store,
        str(ACCOUNTS_GET),
        _one_account_body("acct-1", current=100.00),
        received_at=EARLIER,
    )

    balance = [r for r in rows(store, balances_daily) if r["as_of_date"] == EARLIER.date()][0]
    assert balance["current_minor"] == 777, "an older response overwrote the operator's own row"
    assert balance["source"] == "manual"
    assert balance["raw_response_id"] is None, (
        "the row now carries a raw_response_id, so the next rebuild will delete it"
    )


def _one_account_body(account_id: str, *, current: float) -> bytes:
    """One account, in the shape `/accounts/get` sends."""
    account = _account(current=current)
    account["account_id"] = account_id
    return json.dumps({"accounts": [account], "item": {}, "request_id": "r"}).encode()


# --------------------------------------------------------------------------
# The sign convention (data-model.md Direction; backlog #9)
# --------------------------------------------------------------------------


def _account(**balances: Any) -> dict[str, Any]:
    return {
        "account_id": "acct-signs",
        "name": "Test Account",
        "official_name": None,
        "mask": "0000",
        "type": balances.pop("type", "depository"),
        "subtype": "checking",
        "balances": {
            "current": balances.get("current"),
            "available": balances.get("available"),
            "limit": balances.get("limit"),
            "iso_currency_code": "USD",
            "unofficial_currency_code": None,
        },
    }


def _derive_account(conn: Config, account: dict[str, Any]) -> Any:
    body = json.dumps({"accounts": [account], "item": {}, "request_id": "r"}).encode()
    derive(conn, str(ACCOUNTS_GET), body)
    return rows(conn, balances_daily)[0]


def test_a_liability_reported_positive_is_stored_negative(store: Config) -> None:
    """🔴 Aggregators disagree, and several report a card balance as an amount owed.

    A consumer taking that at face value is wrong **by twice the debt** --
    silently, and plausibly: the number is well-formed, the sum completes, and
    the answer is confidently wrong. One convention is what lets net worth be a
    plain sum instead of a per-type special case.
    """
    balance = _derive_account(store, _account(type="credit", current=250.00, limit=1000.00))
    assert balance["current_minor"] == -25000


def test_an_asset_reported_positive_stays_positive(store: Config) -> None:
    """The control that makes the flip a discrimination rather than a negation."""
    balance = _derive_account(store, _account(type="depository", current=250.00))
    assert balance["current_minor"] == 25000


def test_available_and_limit_keep_the_magnitudes_the_source_reported(
    store: Config,
) -> None:
    """The documented exceptions, asserted so a later 'fix' cannot flip them.

    Neither participates in net worth, and "available credit" is not a negative
    quantity from anyone's point of view. Without this assertion a change that
    signed every column alike would look like a tidy-up and pass.
    """
    balance = _derive_account(
        store, _account(type="credit", current=250.00, available=750.00, limit=1000.00)
    )
    assert balance["current_minor"] == -25000
    assert balance["available_minor"] == 75000
    assert balance["limit_minor"] == 100000


def test_a_card_in_credit_is_stored_as_value_held_rather_than_as_debt(
    store: Config,
) -> None:
    """🔴 The aggregator's negative `current` on a credit account means it owes YOU.

    A refund on a paid-off card, or an overpayment, leaves an ordinary card in
    credit, and this aggregator documents that state as a negative `current` --
    the mirror of the positive `current` that means money owed. A rule written
    as "make liabilities negative" maps both to the same stored value, so a $250
    credit and a $250 debt become the same row and net worth is understated by
    $500 with nothing on the row to tell them apart.

    The rule is therefore "a liability's stored sign is the negation of the
    aggregator's", applied unconditionally. This replaces the idempotence
    argument the conditional rested on -- that a second aggregator might sign
    liabilities the other way -- because that argument put a hypothetical feed's
    convention inside the connector built for the one feed whose convention is
    documented. A second aggregator gets its own connector, which declares its
    own normalization. Recorded as a decision in
    `.prawduct/artifacts/build-plan-production-cutover-hardening.md` § Decisions.
    """
    balance = _derive_account(store, _account(type="credit", current=-250.00))
    assert balance["current_minor"] == 25000


def test_an_account_type_this_build_cannot_classify_is_refused(store: Config) -> None:
    """Guessing "asset" on an unrecognized liability reports a debt as savings."""
    with pytest.raises(DerivationError, match="asset or a liability"):
        _derive_account(store, _account(type="a_type_invented_for_this_test", current=1.0))


def test_the_classification_reads_the_account_type_and_nothing_about_an_institution() -> None:
    """AC-3.2's rule reaches here too: no arithmetic may turn on a roster identity."""
    response = RawResponse(
        raw_response_id=1,
        connection_id=1,
        endpoint=str(ACCOUNTS_GET),
        received_at=RECEIVED,
        body=b"{}",
        body_sha256="",
        request_context=None,
    )
    assert balance_class_of("credit", response) == "liability"
    assert balance_class_of("loan", response) == "liability"
    assert balance_class_of("depository", response) == "asset"
    assert balance_class_of("investment", response) == "asset"


# --------------------------------------------------------------------------
# AC-6.3 — history references the local id, never the aggregator's
# --------------------------------------------------------------------------


def test_a_balance_references_the_local_account_id(store: Config) -> None:
    """The aggregator's id changes when a connection is removed and re-linked.

    History pointing at it would detach on re-enrollment -- which is exactly the
    moment an operator is least able to notice a gap appearing.
    """
    derive(store, str(ACCOUNTS_GET), fixture("accounts_get"))

    account = rows(store, accounts)[0]
    balance = rows(store, balances_daily)[0]
    assert balance["account_id"] == account["account_id"]
    assert isinstance(balance["account_id"], int)
    # The source's own id lives on the account row, where re-enrollment can
    # replace it without touching anything that points at the account.
    assert isinstance(account["source_account_id"], str)
    # 🔴 The structural half, and the one that survives a careless later change:
    # `balances_daily` has no column that could hold the aggregator's id at all,
    # so history cannot reference it even by mistake. Comparing the two values
    # instead would assert only that an int is not a str.
    assert not [c.name for c in balances_daily.c if c.name.startswith("source_")]


def test_every_derived_balance_carries_its_provenance(store: Config) -> None:
    """AC-5.3: losslessness is only well-defined against a recorded version."""
    response = derive(store, str(ACCOUNTS_GET), fixture("accounts_get"))
    for balance in rows(store, balances_daily):
        assert balance["raw_response_id"] == response.raw_response_id
        assert balance["derivation_version_id"] is not None
        assert balance["source"] == "aggregator"


# --------------------------------------------------------------------------
# Purity — the same response, twice, in any order
# --------------------------------------------------------------------------


def test_deriving_the_same_response_twice_writes_the_same_rows(store: Config) -> None:
    """The property the rebuild's self-check rests on.

    `institutions` and `accounts` are never emptied by a rebuild, so a plain
    insert would raise on the replay's second pass -- and the failure would read
    as a purity bug rather than as the missing upsert it is.
    """
    derive(store, str(INSTITUTIONS_GET), fixture("institutions_get"))
    derive(store, str(ACCOUNTS_GET), fixture("accounts_get"))
    once = [dict(r) for r in rows(store, accounts)] + [dict(r) for r in rows(store, institutions)]

    derive(store, str(INSTITUTIONS_GET), fixture("institutions_get"))
    derive(store, str(ACCOUNTS_GET), fixture("accounts_get"))
    twice = [dict(r) for r in rows(store, accounts)] + [dict(r) for r in rows(store, institutions)]

    assert once == twice
    assert len(rows(store, accounts)) == len(json.loads(fixture("accounts_get"))["accounts"])


@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(order=st.permutations([EARLIER, RECEIVED, LATER]))
def test_replaying_in_any_order_converges_on_the_same_identity_rows(
    store: Config, order: Sequence[UtcInstant]
) -> None:
    """`first_seen_at` is a minimum and `last_seen_at` a maximum, so order cannot matter.

    Stated as a property rather than as one shuffled case, because the orders
    that would break it are the ones nobody thinks to write down -- and a rebuild
    replays by `received_at`, which two responses can share.
    """
    for received_at in order:
        derive(store, str(ITEM_GET), fixture("item_get"), received_at=received_at)

    derived = [
        r for r in rows(store, institutions) if r["source_institution_id"] == SEEDED_INSTITUTION
    ]
    assert derived, "the fixture derived no institution, so this proves nothing"
    for row in derived:
        assert row["first_seen_at"] == EARLIER
        assert row["last_seen_at"] == LATER


@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(order=st.permutations([EARLIER, RECEIVED, LATER]))
def test_when_an_account_was_last_listed_is_a_maximum_so_order_cannot_matter(
    store: Config, order: Sequence[UtcInstant]
) -> None:
    """🔴 AC-12.4, and the property retirement was actually waiting on.

    `first_seen_date` is a minimum and `last_seen_date` a maximum over the roster
    observations that mention an account, so replaying the archive in ANY order
    converges on the same pair. That is what makes "this account was last listed
    on X" a fact about the responses rather than about the order they were
    replayed in — and it is the specific care this deriver's own note recorded as
    the reason the removal case was deferred rather than half-built.

    A property rather than one shuffled case, on the precedent above: the orders
    that break it are the ones nobody thinks to write down, and a rebuild replays
    by `received_at`, which two responses can share.
    """
    for received_at in order:
        derive(store, str(ACCOUNTS_GET), fixture("accounts_get"), received_at=received_at)

    derived = rows(store, accounts)
    assert derived, "the fixture derived no account, so this proves nothing"
    for row in derived:
        assert row["first_seen_date"] == EARLIER.date()
        assert row["last_seen_date"] == LATER.date()


def test_reading_a_roster_records_when_the_connection_was_observed(
    store: Config,
) -> None:
    """🔴 AC-12.4's per-connection half: the record that says *we looked*.

    Without it there is nothing to measure absence against except a maximum
    derived over the accounts that were listed, which moves with them -- so the
    one fact AC-12.5 needs is inexpressible exactly when it is true.
    """
    derive(store, str(ACCOUNTS_GET), fixture("accounts_get"), received_at=RECEIVED)

    observed = [row["roster_observed_date"] for row in rows(store, connections)]

    assert observed == [RECEIVED.date()]


def test_a_roster_that_lists_nothing_still_records_the_observation(
    store: Config,
) -> None:
    """🔴 AC-12.5a. The state that must not exist is a successful read of nothing.

    An `/accounts/get` that comes back with an empty list type-checks, runs the
    derivation loop zero times, and -- if the observation were recorded per
    account, or recorded only where the loop wrote something -- would leave no
    record that anyone looked. The two facts it must be possible to tell apart
    are "the operator de-selected every account" and "the feed broke and still
    returned success", and neither can be reported at all if the read left no
    trace.

    The two observations DIFFER, so the assertion cannot be satisfied by the
    first one alone: an empty roster read three days later must MOVE the date.
    """
    derive(store, str(ACCOUNTS_GET), fixture("accounts_get"), received_at=EARLIER)
    derive(
        store,
        str(ACCOUNTS_GET),
        _with(fixture("accounts_get"), lambda p: p.__setitem__("accounts", [])),
        received_at=LATER,
    )

    observed = [row["roster_observed_date"] for row in rows(store, connections)]
    seen = {row["source_account_id"]: row["last_seen_date"] for row in rows(store, accounts)}

    assert observed == [LATER.date()], (
        "a roster read that listed no accounts left the observation where it was, so the "
        "connection is indistinguishable from one that was never read"
    )
    assert set(seen.values()) == {EARLIER.date()}, (
        "an empty roster must not move any account's own last-listed date; only the "
        "observation moves, which is what makes every account behind it"
    )


@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(order=st.permutations([EARLIER, RECEIVED, LATER]))
def test_the_roster_observation_is_a_maximum_so_a_replay_cannot_move_it_back(
    store: Config, order: Sequence[UtcInstant]
) -> None:
    """🔴 AC-12.4, on the connection half, for the reason the account half is one.

    A repaired connection, a backfill and a manual re-derive all hand the
    deriver responses in whatever order they were archived. An observation that
    moved BACKWARDS on a replay would put accounts a later roster had listed
    behind it and report them absent -- a fabricated closure produced by replay
    order alone.
    """
    for received_at in order:
        derive(store, str(ACCOUNTS_GET), fixture("accounts_get"), received_at=received_at)

    observed = [row["roster_observed_date"] for row in rows(store, connections)]

    assert observed == [LATER.date()]


def test_the_first_roster_observation_records_both_ends_at_the_same_date(
    store: Config,
) -> None:
    """The insert arm. The pair only diverges once a later roster is seen."""
    derive(store, str(ACCOUNTS_GET), fixture("accounts_get"), received_at=RECEIVED)

    derived = rows(store, accounts)
    assert derived
    for row in derived:
        assert row["first_seen_date"] == RECEIVED.date()
        assert row["last_seen_date"] == RECEIVED.date()


def test_an_account_a_later_roster_does_not_list_keeps_the_date_it_had(
    store: Config,
) -> None:
    """🔴 The whole population half of #40, at the deriver.

    An institution that stops listing an account says so by not saying it, so
    the mechanism is that nothing writes the row — and a `last_seen_date` that
    the second observation moved anyway would make every account permanently
    current, which is exactly the state #40 reported.
    """
    both = fixture("accounts_get")
    payload = json.loads(both)
    assert len(payload["accounts"]) > 1, "the fixture lists one account, so nothing can drop out"
    kept = payload["accounts"][0]["account_id"]
    dropped = payload["accounts"][1]["account_id"]

    derive(store, str(ACCOUNTS_GET), both, received_at=EARLIER)
    derive(
        store,
        str(ACCOUNTS_GET),
        _with(both, lambda p: p.__setitem__("accounts", p["accounts"][:1])),
        received_at=LATER,
    )

    seen = {row["source_account_id"]: row["last_seen_date"] for row in rows(store, accounts)}
    assert seen[kept] == LATER.date()
    assert seen[dropped] == EARLIER.date()


def test_no_deriver_reads_the_clock(store: Config) -> None:
    """🔴 The seam's one rule with teeth, asserted on what reaches the columns.

    A `now()` anywhere makes replay produce rows the rebuild cannot reproduce,
    which disarms the check the whole seam exists for. Every stamp below has to
    be the response's own `received_at` -- checked against an instant deliberately
    far from the present, so a clock call would be unmistakable.
    """
    derive(store, str(ITEM_GET), fixture("item_get"), received_at=EARLIER)
    derive(store, str(ACCOUNTS_GET), fixture("accounts_get"), received_at=EARLIER)

    # The item deriver writes the seeded institution's row rather than a new one,
    # so this asserts the *update* path's stamps. Skipping it as "the fixture's"
    # would leave the loop with nothing in it -- which is what it had, silently,
    # once the roster stopped being grown from the catalogue.
    institution = [
        r for r in rows(store, institutions) if r["source_institution_id"] == SEEDED_INSTITUTION
    ]
    assert institution, "no institution row, so the assertions below prove nothing"
    row = institution[0]
    # The response is older than the row the fixture seeded, so the minimum moves
    # and the maximum does not. Both values come from data -- the response, or
    # what was already stored -- and neither is anywhere near now, which is what
    # a clock call would produce.
    assert row["first_seen_at"] == EARLIER
    assert row["last_seen_at"] == RECEIVED
    for row in rows(store, accounts):
        assert row["created_at"] == EARLIER
        assert row["updated_at"] == EARLIER
        assert row["first_seen_date"] == EARLIER.date()
    for row in rows(store, balances_daily):
        assert row["captured_at"] == EARLIER
        assert row["as_of_date"] == EARLIER.date()


# --------------------------------------------------------------------------
# Hostile shapes — derive, or fail loudly; never a silent partial row
# --------------------------------------------------------------------------


def test_an_account_with_no_mask_derives_because_the_column_is_nullable(
    store: Config,
) -> None:
    """Plenty of real accounts have none, which is why the column allows it."""
    body = _with(
        fixture("accounts_get"),
        lambda p: p.__setitem__("accounts", [{**p["accounts"][0], "mask": None}]),
    )
    derive(store, str(ACCOUNTS_GET), body)
    assert rows(store, accounts)[0]["mask"] is None


def test_optional_fields_that_are_null_derive_as_null(store: Config) -> None:
    """Sandbox responses are tidy; production ones are full of nulls."""
    body = _with(
        fixture("accounts_get"),
        lambda p: p.__setitem__(
            "accounts",
            [
                {
                    **p["accounts"][0],
                    "official_name": None,
                    "subtype": None,
                    "persistent_account_id": None,
                    "balances": {**p["accounts"][0]["balances"], "available": None, "limit": None},
                }
            ],
        ),
    )
    derive(store, str(ACCOUNTS_GET), body)
    account = rows(store, accounts)[0]
    assert account["official_name"] is None
    assert account["account_subtype"] is None
    balance = rows(store, balances_daily)[0]
    assert balance["available_minor"] is None
    assert balance["limit_minor"] is None


@pytest.mark.parametrize(
    ("what", "mutate"),
    [
        ("no name", lambda p: p["accounts"][0].update({"name": None})),
        ("no account id", lambda p: p["accounts"][0].update({"account_id": None})),
        ("no type", lambda p: p["accounts"][0].update({"type": None})),
        ("no balances", lambda p: p["accounts"][0].update({"balances": None})),
        ("no accounts list", lambda p: p.pop("accounts")),
        ("accounts is not a list", lambda p: p.update({"accounts": "none"})),
    ],
)
def test_a_missing_required_field_fails_loudly_rather_than_writing_a_partial_row(
    store: Config, what: str, mutate: Any
) -> None:
    """A placeholder here would be this system inventing a fact and recording it.

    The alternative -- deriving what is understood and skipping the rest -- gives
    a dataset that is incomplete and still adds up, which is the failure this
    whole layer exists to make impossible.
    """
    with pytest.raises(DerivationError):
        derive(store, str(ACCOUNTS_GET), _with(fixture("accounts_get"), mutate))
    assert not rows(store, accounts), f"a partial row survived the {what} case"


def test_an_item_missing_its_institution_is_refused_rather_than_left_unlinked(
    store: Config,
) -> None:
    """`accounts.institution_id` is NOT NULL, so there is no half-derived answer.

    A placeholder institution would be this system inventing a fact and recording
    it as one the aggregator supplied -- and it would sit in the roster beside the
    operator's real institutions with nothing to tell them apart.
    """
    for mutate in (
        lambda p: p["item"].update({"institution_id": None}),
        lambda p: p["item"].update({"institution_name": None}),
        lambda p: p.update({"item": None}),
    ):
        with pytest.raises(DerivationError):
            derive(store, str(ITEM_GET), _with(fixture("item_get"), mutate))


def test_fields_this_build_does_not_read_do_not_stop_it_deriving(store: Config) -> None:
    """The aggregator adds fields; a deriver that read them all would break on each one.

    The archive keeps the whole body, so a field this build ignores today is
    still there for a rebuild that learns to read it.
    """
    body = _with(
        fixture("item_get"),
        lambda p: p["item"].update({"a_field_invented_after_this_build": {"nested": [1, 2]}}),
    )
    derive(store, str(ITEM_GET), body)
    assert any(r["source_institution_id"] == SEEDED_INSTITUTION for r in rows(store, institutions))


def test_a_catalogue_page_derives_no_rows_at_all(store: Config) -> None:
    """🔴 `/institutions/get` serves the aggregator's *production* catalogue.

    10,083 US institutions, with real names and routing numbers. `institutions`
    is the roster -- the table `connections` hangs off -- so deriving a page into
    it would put banks the operator never linked beside the ones they did, with
    nothing to tell them apart and nothing that removes them, because a rebuild
    never empties this table.

    Registered as deriving nothing rather than left unregistered: "archived and
    implies no rows" is a real answer and has to be stated, or it is
    indistinguishable from the endpoint nobody got round to.
    """
    before = rows(store, institutions)
    derive(store, str(INSTITUTIONS_GET), fixture("institutions_get"), connection_id=None)

    assert rows(store, institutions) == before
    # Positive control: the page really does carry institutions, so the empty
    # result is a decision rather than an empty page.
    assert len(json.loads(fixture("institutions_get"))["institutions"]) >= 1


def test_an_accounts_response_archived_against_no_connection_is_refused(
    store: Config,
) -> None:
    """The connection comes from the archived row, not from the body.

    Derived against no connection, the accounts would be indistinguishable from
    the manual-import path and the source-identity index would stop preventing
    duplicates -- so a second sync would double every account, silently.
    """
    with pytest.raises(DerivationError, match="without a connection"):
        derive(store, str(ACCOUNTS_GET), fixture("accounts_get"), connection_id=None)


def test_a_response_naming_a_connection_that_is_not_here_is_refused(
    store: Config,
) -> None:
    """Defence in depth, tested where it is actually reachable.

    `raw_responses.connection_id` is a foreign key, so an unknown connection
    cannot reach the archive at all -- `apply_response` fails first. The guard
    still earns its place for the path that calls a deriver directly, and a test
    routed through `apply_response` would have asserted the database's
    constraint while appearing to assert the deriver's.
    """
    ghost = RawResponse(
        raw_response_id=1,
        connection_id=999,
        endpoint=str(ACCOUNTS_GET),
        received_at=RECEIVED,
        body=fixture("accounts_get"),
        body_sha256="",
        request_context=None,
    )
    with writing(store) as conn, transaction(conn):
        context = DerivationContext(derivation_version_id=ensure_derivation_version(conn))
        with pytest.raises(DerivationError, match="not in this datastore"):
            PLAID_DERIVERS[str(ACCOUNTS_GET)](conn, ghost, context)


def test_a_body_that_is_not_json_is_refused(store: Config) -> None:
    with pytest.raises(DerivationError, match="not JSON"):
        derive(store, str(ACCOUNTS_GET), b"<html>a proxy replied instead</html>")


# --------------------------------------------------------------------------
# The acceptance criterion — rebuild over a real archive
# --------------------------------------------------------------------------


def test_a_rebuild_over_a_real_archive_reproduces_the_tables(store: Config) -> None:
    """🔴 The chunk's acceptance criterion, over responses the aggregator really sent.

    The rebuild hashes the datastore's content before and after and refuses to
    commit one that did not reproduce what it replaced. That check is only
    meaningful if the derivers are pure and idempotent, so this is the test that
    fails if either property is quietly lost.
    """
    derive(store, str(INSTITUTIONS_GET), fixture("institutions_get"))
    derive(store, str(ACCOUNTS_GET), fixture("accounts_get"))
    with writing(store) as conn:
        before = content_digest(conn)
    assert rows(store, balances_daily), "nothing was derived, so the rebuild proves nothing"

    report = rebuild(store, derivers=PLAID_DERIVERS)

    assert not report.content_changed, (
        "the rebuild did not reproduce what it replaced; a deriver is reading a clock, "
        "or is not idempotent over the identity tables"
    )
    with writing(store) as conn:
        assert content_digest(conn) == before


def test_every_archivable_endpoint_has_a_deriver() -> None:
    """🔴 Derived from what can be archived, never written out by hand.

    A rebuild refuses an endpoint it has no deriver for -- correctly, since
    stepping over a response it cannot interpret would report success on an
    incomplete dataset. But that refusal lands on the *archive*, months later and
    on a row that cannot be removed, rather than at the call that introduced the
    endpoint.

    The first version of this test asserted a hand-written pair, which is why
    adding `item_get` to the client did not turn it red. The expected set is now
    every endpoint that can produce a `FetchedResponse` -- the ones declared
    without `issues_credential` -- so the next archivable endpoint fails here, in
    the commit that adds it.
    """
    import bankmachine.connector as boundary

    archivable = {
        str(value)
        for value in vars(boundary).values()
        if isinstance(value, Endpoint) and not value.issues_credential
    }
    assert archivable, "no endpoints were found, so this test is proving nothing"
    assert set(PLAID_DERIVERS) == archivable, (
        "an endpoint can be archived with no deriver registered for it; the first caller to "
        "archive one makes every later `store rebuild` refuse the whole archive"
    )


def test_the_composed_registry_is_what_the_rebuild_command_uses() -> None:
    """The registry lives above both layers, and `store` must not import `connector`.

    Holding a registry in `store.derivation` instead would pull the aggregator SDK
    into every process that opens the datastore -- the read-only query surface
    included, which must never load the network layer at all.
    """
    from bankmachine.derivers import ALL_DERIVERS

    assert dict(ALL_DERIVERS) == dict(PLAID_DERIVERS)


# --------------------------------------------------------------------------
# Convergence across a re-link (#68, Part 1)
# --------------------------------------------------------------------------

#: When the operator re-linked. Later than `RECEIVED`, so the connection the
#: re-link enrolled is unambiguously the newer of the two.
RELINKED = utc_instant(datetime(2026, 9, 11, 9, 0, tzinfo=UTC))

#: The aggregator's own statement that two differently-numbered accounts are the
#: same underlying account. Named once so a test reading it is not carrying a
#: second copy of the value under test.
PERSISTENT = "persistent-checking"


def _one_account(
    *, account_id: str, persistent_account_id: str | None, name: str = "Plaid Checking"
) -> bytes:
    """A recorded `/accounts/get` narrowed to one account, so a match is unambiguous."""
    return _with(
        fixture("accounts_get"),
        lambda p: p.__setitem__(
            "accounts",
            [
                {
                    **p["accounts"][0],
                    "account_id": account_id,
                    "name": name,
                    **(
                        {}
                        if persistent_account_id is None
                        else {"persistent_account_id": persistent_account_id}
                    ),
                }
            ],
        ),
    )


def _second_institution(config: Config, source_institution_id: str) -> int:
    """Another bank, enrolled -- what makes a global persistent match dangerous."""
    with writer_connection(config) as conn, transaction(conn):
        seeded = conn.execute(
            insert(institutions).values(
                source_institution_id=source_institution_id,
                name="Second Platypus Bank",
                first_seen_at=RECEIVED,
                last_seen_at=RECEIVED,
            )
        ).inserted_primary_key
        assert seeded is not None  # an INTEGER PRIMARY KEY insert always yields one
        created = conn.execute(
            insert(connections).values(
                institution_id=seeded[0],
                source_connection_id=f"item-{source_institution_id}",
                credential_ref="plaid:sandbox",
                status="active",
                enrolled_at=RECEIVED,
                created_at=RECEIVED,
                updated_at=RECEIVED,
            )
        ).inserted_primary_key
        assert created is not None
        return int(created[0])


def _relink(config: Config) -> int:
    """Retire the enrolled connection and enrol a new one at the same institution.

    🔴 The retirement is not decoration. `connections_one_live_per_institution`
    refuses two live connections at one bank, so this is the state
    `connections remove` followed by `enroll` actually produces -- and building
    the fixture any other way would test a shape the product cannot reach.
    """
    with writer_connection(config) as conn, transaction(conn):
        institution_id = conn.execute(
            select(institutions.c.institution_id).where(
                institutions.c.source_institution_id == SEEDED_INSTITUTION
            )
        ).scalar_one()
        conn.execute(
            update(connections)
            .where(connections.c.connection_id == 1)
            .values(status="retired", retired_at=RELINKED, updated_at=RELINKED)
        )
        created = conn.execute(
            insert(connections).values(
                institution_id=institution_id,
                source_connection_id="item-after-relink",
                credential_ref="plaid:sandbox",
                status="active",
                enrolled_at=RELINKED,
                created_at=RELINKED,
                updated_at=RELINKED,
            )
        ).inserted_primary_key
        assert created is not None  # an INTEGER PRIMARY KEY insert always yields one
        return int(created[0])


def test_a_relink_converges_on_the_account_its_history_already_hangs_from(
    store: Config,
) -> None:
    """🔴 The defect this item exists for, at the account level.

    A full re-link issues a new Item, and the new Item issues a NEW id for every
    account. Matched on `(connection_id, source_account_id)` alone the same real
    account appears a second time, the whole granted history is re-fetched
    against it, and annual spending doubles -- signalled by nothing louder than a
    count of accounts that are no longer active.

    The local `account_id` is what every row of history references (AC-6.3), so
    keeping it is the whole point: the rows already in the store stay attached
    to the account they were about.
    """
    derive(
        store,
        str(ACCOUNTS_GET),
        _one_account(account_id="acct-old", persistent_account_id=PERSISTENT),
    )
    before = rows(store, accounts)
    assert len(before) == 1, "the fixture listed more than one account, so a match is ambiguous"

    relinked = _relink(store)
    derive(
        store,
        str(ACCOUNTS_GET),
        _one_account(account_id="acct-new", persistent_account_id=PERSISTENT),
        connection_id=relinked,
        received_at=RELINKED,
    )

    after = rows(store, accounts)
    assert len(after) == 1, "the re-link left a second account row, so its history is split"
    assert after[0]["account_id"] == before[0]["account_id"], (
        "the account was renumbered, so every transaction and balance already stored points "
        "at a row nothing lists"
    )
    assert after[0]["source_account_id"] == "acct-new"
    assert after[0]["connection_id"] == relinked, (
        "the converged account still points at the retired connection, so the re-fetched "
        "pages have no account to hang from and the whole sync refuses"
    )
    assert after[0]["first_seen_date"] == RECEIVED.date(), (
        "the converged row forgot when the account was first seen"
    )


def test_a_persistent_identity_shared_across_institutions_is_never_merged(
    store: Config,
) -> None:
    """🔴 Scoped to the institution, because a global match's failure is unrecoverable.

    The field is documented stable for the same underlying account, but nothing
    makes it unique across banks. A global match turns a collision between two
    institutions into a silent merge of two real accounts into one: every total
    over either is then wrong, and the store keeps no record that two things were
    joined.

    **Red against a global match, not against the pre-fix code** -- which had no
    persistent match at all and so also kept the two apart. This is the guard on
    the fix, and it is stated rather than left to look like a regression test.
    """
    derive(
        store,
        str(ACCOUNTS_GET),
        _one_account(account_id="acct-a", persistent_account_id=PERSISTENT),
    )
    elsewhere = _second_institution(store, "ins_999999")

    derive(
        store,
        str(ACCOUNTS_GET),
        _one_account(account_id="acct-b", persistent_account_id=PERSISTENT, name="Other Checking"),
        connection_id=elsewhere,
        received_at=LATER,
    )

    held = rows(store, accounts)
    assert len(held) == 2, "two banks' accounts were merged on an identity neither bank shares"
    assert {row["institution_id"] for row in held} == {1, 2}


def test_an_account_the_aggregator_gives_no_persistent_identity_falls_back_to_todays_key(
    store: Config,
) -> None:
    """Absence is ORDINARY, and what it costs is stated rather than claimed fixed.

    The aggregator populates `persistent_account_id` for select institutions
    only, so a body without one is not an error and must still converge on an
    ordinary re-sync. What it cannot do is survive a re-link: with no identity
    that outlives the Item, the second Item's account is a new account as far as
    anything here can tell. 🔴 That case is NOT fixed by this match, and the
    remedy is re-authorising in place -- the same Item, so no second lineage at
    all.
    """
    body = _one_account(account_id="acct-old", persistent_account_id=None)
    derive(store, str(ACCOUNTS_GET), body)
    derive(store, str(ACCOUNTS_GET), body, received_at=LATER)
    assert len(rows(store, accounts)) == 1, "an ordinary re-sync duplicated the account"

    relinked = _relink(store)
    derive(
        store,
        str(ACCOUNTS_GET),
        _one_account(account_id="acct-new", persistent_account_id=None),
        connection_id=relinked,
        received_at=RELINKED,
    )

    assert len(rows(store, accounts)) == 2, (
        "the re-link converged with no identity to converge on, so something guessed"
    )


def test_a_persistent_identity_already_recorded_survives_a_body_that_omits_it(
    store: Config,
) -> None:
    """🔴 The one key convergence has must not be erasable by an ordinary sync.

    The aggregator populates this field on some responses and not others. An
    unconditional write of what the current body says would blank the column on
    the first response that omits it -- and nothing would look wrong until the
    next re-link, which would then duplicate exactly as it does today.
    """
    derive(
        store,
        str(ACCOUNTS_GET),
        _one_account(account_id="acct-old", persistent_account_id=PERSISTENT),
    )

    derive(
        store,
        str(ACCOUNTS_GET),
        _one_account(account_id="acct-old", persistent_account_id=None),
        received_at=LATER,
    )

    assert rows(store, accounts)[0]["source_persistent_account_id"] == PERSISTENT


def test_two_rows_already_sharing_a_persistent_identity_are_left_where_they_are(
    store: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """🔴 What a re-link performed BEFORE this match existed left behind.

    Such a store holds two rows carrying one persistent identity. Re-pointing the
    older onto the newer one's `(connection_id, source_account_id)` would violate
    the identity index and take the connection down on every run afterwards, and
    merging their history is a repair with its own requirement rather than
    something a deriver does on the way past. So both are kept, neither moves,
    and the pair is named in the log rather than left for a total to disagree
    about silently.
    """
    derive(
        store,
        str(ACCOUNTS_GET),
        _one_account(account_id="acct-old", persistent_account_id=PERSISTENT),
    )
    relinked = _relink(store)
    # The duplicate this store would already be holding: derived under the new
    # connection before the aggregator stated an identity for it.
    derive(
        store,
        str(ACCOUNTS_GET),
        _one_account(account_id="acct-new", persistent_account_id=None),
        connection_id=relinked,
        received_at=RELINKED,
    )
    assert len(rows(store, accounts)) == 2, "the duplicate this test is about was never created"

    with caplog.at_level(logging.WARNING):
        derive(
            store,
            str(ACCOUNTS_GET),
            _one_account(account_id="acct-new", persistent_account_id=PERSISTENT),
            connection_id=relinked,
            received_at=RELINKED,
        )
        derive(
            store,
            str(ACCOUNTS_GET),
            _one_account(account_id="acct-new", persistent_account_id=PERSISTENT),
            connection_id=relinked,
            received_at=LATER,
        )

    held = rows(store, accounts)
    assert len(held) == 2, "the collision was forced through and one row absorbed the other"
    assert {row["source_account_id"] for row in held} == {"acct-old", "acct-new"}
    assert any("collide" in record.message for record in caplog.records), (
        "the store holds two rows for one account and says nothing about it"
    )


def test_a_rebuild_over_a_relinked_archive_converges_the_same_way_twice(store: Config) -> None:
    """🔴 Convergence has to be a function of the archive, not of what ran first.

    The account row a re-link converges on is one a rebuild never deletes -- its
    id is what every transaction and balance references -- so the replay meets a
    row already carrying the NEW Item's identity and has to land back on exactly
    the same state. The failure is not subtle but it is late: the rebuild's own
    digest guard rolls it back at an unchanged derivation version, so the remedy
    the upgrade procedure prescribes stops working on precisely the stores that
    most need it.
    """
    derive(
        store,
        str(ACCOUNTS_GET),
        _one_account(account_id="acct-old", persistent_account_id=PERSISTENT),
    )
    relinked = _relink(store)
    derive(
        store,
        str(ACCOUNTS_GET),
        _one_account(account_id="acct-new", persistent_account_id=PERSISTENT),
        connection_id=relinked,
        received_at=RELINKED,
    )
    converged = rows(store, accounts)
    assert len(converged) == 1, "nothing converged, so the replay below has nothing to reproduce"
    with writing(store) as conn:
        before = content_digest(conn)

    report = rebuild(store, derivers=PLAID_DERIVERS)

    assert not report.content_changed, (
        "replaying a re-linked archive landed somewhere else, so the account this store "
        "converged on depends on the order the responses were first applied"
    )
    with writing(store) as conn:
        assert content_digest(conn) == before
    assert rows(store, accounts) == converged
