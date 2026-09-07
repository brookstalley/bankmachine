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
from sqlalchemy import insert, select

from bankmachine.config import Config
from bankmachine.connector import ACCOUNTS_GET, INSTITUTIONS_GET
from bankmachine.connector.plaid.derivers import (
    DEFAULT_MINOR_DIGITS,
    PLAID_DERIVERS,
    balance_class_of,
    minor_digits,
    to_minor,
)
from bankmachine.store.derivation import (
    DerivationContext,
    DerivationError,
    apply_response,
    ensure_derivation_version,
)
from bankmachine.store.engine import transaction, writer_connection
from bankmachine.store.raw import RawResponse
from bankmachine.store.rebuild import content_digest, rebuild
from bankmachine.store.schema import accounts, balances_daily, connections, institutions
from bankmachine.store.types import UtcInstant, utc_instant

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
    assert minor_digits("USD") == DEFAULT_MINOR_DIGITS == 2
    # Lowercase too: the field is the aggregator's, not ours.
    assert minor_digits("jpy") == 0


def test_a_balance_in_no_stated_currency_is_refused(store: Config) -> None:
    """An amount whose unit is unknown is how a total silently mixes two of them."""
    body = _with(
        fixture("accounts_get"),
        lambda p: p.__setitem__(
            "accounts",
            [
                {
                    **p["accounts"][0],
                    "balances": {
                        "current": 1.0,
                        "available": None,
                        "limit": None,
                        "iso_currency_code": None,
                        "unofficial_currency_code": None,
                    },
                }
            ],
        ),
    )
    with pytest.raises(DerivationError, match="currency"):
        derive(store, str(ACCOUNTS_GET), body)


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


def test_a_liability_already_reported_negative_is_not_flipped_twice(
    store: Config,
) -> None:
    """Aggregators disagree with each other, so normalization has to be idempotent.

    A rule written as "negate liabilities" rather than "make liabilities
    negative" turns a correctly-signed source into a positive debt -- the same
    error as the one it was written to prevent, arriving from the other side.
    """
    balance = _derive_account(store, _account(type="credit", current=-250.00))
    assert balance["current_minor"] == -25000


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
    # The fixture's own institution is referenced by its connection, so it stays.
    # Excluded by name rather than by a snapshot taken here: hypothesis reuses a
    # function-scoped fixture across examples, so a snapshot would grow to
    # include the previous example's rows and leave nothing to assert on.
    for received_at in order:
        derive(store, str(INSTITUTIONS_GET), fixture("institutions_get"), received_at=received_at)

    derived = [
        r for r in rows(store, institutions) if r["source_institution_id"] != SEEDED_INSTITUTION
    ]
    assert derived, "the fixture derived no institution, so this proves nothing"
    for row in derived:
        assert row["first_seen_at"] == EARLIER
        assert row["last_seen_at"] == LATER


def test_no_deriver_reads_the_clock(store: Config) -> None:
    """🔴 The seam's one rule with teeth, asserted on what reaches the columns.

    A `now()` anywhere makes replay produce rows the rebuild cannot reproduce,
    which disarms the check the whole seam exists for. Every stamp below has to
    be the response's own `received_at` -- checked against an instant deliberately
    far from the present, so a clock call would be unmistakable.
    """
    derive(store, str(INSTITUTIONS_GET), fixture("institutions_get"), received_at=EARLIER)
    derive(store, str(ACCOUNTS_GET), fixture("accounts_get"), received_at=EARLIER)

    for row in rows(store, institutions):
        if row["source_institution_id"] == SEEDED_INSTITUTION:
            continue  # the fixture connection's institution, inserted by the test
        assert row["first_seen_at"] == EARLIER
        assert row["last_seen_at"] == EARLIER
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


def test_an_institution_with_no_logo_derives_because_nothing_reads_a_logo(
    store: Config,
) -> None:
    """The columns this build stores are the ones it can promise something about."""
    body = _with(
        fixture("institutions_get"),
        lambda p: [
            entry.pop("logo", None) or entry.update({"logo": None}) for entry in p["institutions"]
        ],
    )
    derive(store, str(INSTITUTIONS_GET), body)
    assert len(rows(store, institutions)) > 1


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


def test_the_registry_covers_the_endpoints_that_are_archived(
    initialized_config: Config,
) -> None:
    """A rebuild refuses an endpoint it has no deriver for, rather than skipping it.

    Asserted here because the pairing is easy to break from either side: an
    endpoint added to the client with no deriver makes every later rebuild fail,
    and it fails on the archive rather than at the call that introduced it.
    """
    assert set(PLAID_DERIVERS) == {str(INSTITUTIONS_GET), str(ACCOUNTS_GET)}


def test_the_composed_registry_is_what_the_rebuild_command_uses() -> None:
    """The registry lives above both layers, and `store` must not import `connector`.

    Populating `store.derivation.DERIVERS` instead would pull the aggregator SDK
    into every process that opens the datastore -- the read-only query surface
    included, which must never load the network layer at all.
    """
    from bankmachine.derivers import ALL_DERIVERS
    from bankmachine.store.derivation import DERIVERS

    assert dict(ALL_DERIVERS) == dict(PLAID_DERIVERS)
    assert not DERIVERS, "the seam's default registry should stay empty; see its docstring"
