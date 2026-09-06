"""The typed vocabulary: money that stays exact, and dates that stay apart.

These are the two requirements that fail silently (AC-6.2, AC-6.4), so they get
property tests rather than examples. An example test proves the case its author
thought of, which for money arithmetic is always the round number.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal, localcontext

import pytest
from hypothesis import given
from hypothesis import strategies as st

from bankmachine.store.types import (
    CalendarDate,
    MoneyError,
    TemporalError,
    UtcInstant,
    add,
    calendar_date,
    from_decimal_string,
    minor_units,
    negate,
    now_utc,
    to_decimal_string,
    total,
    utc_instant,
)

AMOUNTS = st.integers(min_value=-(10**15), max_value=10**15).map(minor_units)
EXPONENTS = st.integers(min_value=0, max_value=6)


def test_minor_units_refuses_a_bool() -> None:
    """`True` is an int to Python, and would post as one cent."""
    with pytest.raises(MoneyError):
        minor_units(True)


def test_minor_units_refuses_a_float() -> None:
    with pytest.raises(MoneyError):
        minor_units(12.34)  # type: ignore[arg-type]


@given(AMOUNTS, AMOUNTS)
def test_addition_stays_an_exact_integer(left: int, right: int) -> None:
    result = add(minor_units(left), minor_units(right))
    assert type(result) is int
    assert result == left + right


@given(st.lists(AMOUNTS, max_size=200))
def test_a_total_is_exactly_the_sum_and_never_a_float(amounts: list[int]) -> None:
    result = total(minor_units(a) for a in amounts)
    assert type(result) is int
    assert result == sum(amounts)


@given(AMOUNTS)
def test_negation_is_its_own_inverse(amount: int) -> None:
    assert negate(negate(minor_units(amount))) == amount


@given(AMOUNTS, EXPONENTS)
def test_decimal_text_round_trips_through_minor_units(amount: int, exponent: int) -> None:
    """The conversion an aggregator response forces on every single row."""
    rendered = to_decimal_string(minor_units(amount), exponent=exponent)
    assert from_decimal_string(rendered, exponent=exponent) == amount


@given(AMOUNTS, EXPONENTS)
def test_rendered_text_agrees_with_exact_decimal_arithmetic(amount: int, exponent: int) -> None:
    """Pin the rendering against Decimal, which is the reference for exactness."""
    rendered = to_decimal_string(minor_units(amount), exponent=exponent)
    assert Decimal(rendered) == Decimal(amount).scaleb(-exponent)


@given(st.decimals(allow_nan=False, allow_infinity=False, places=2), st.just(2))
def test_any_two_place_decimal_converts_exactly(value: Decimal, exponent: int) -> None:
    """The oracle needs a widened context; the code under test needs none.

    That asymmetry is the point. `Decimal.scaleb` under the default 28-digit
    context rounds a large amount silently -- hypothesis found
    `100000000000000000000000000.01` doing exactly that to this test's first
    oracle. `from_decimal_string` scales the digit tuple instead, so no context
    applies to it and no amount is too large to convert exactly.
    """
    with localcontext() as ctx:
        ctx.prec = len(value.as_tuple().digits) + exponent + 5
        expected = int(value.scaleb(exponent))
    assert from_decimal_string(str(value), exponent=exponent) == expected


def test_more_precision_than_the_currency_has_is_refused_not_rounded() -> None:
    """A tenth of a cent dropped on every row reconciles to nothing (AC-11.2)."""
    with pytest.raises(MoneyError, match="refusing to round"):
        from_decimal_string("10.005", exponent=2)


def test_a_large_amount_converts_exactly() -> None:
    """Digit-tuple scaling, so no Decimal context precision can round this."""
    assert from_decimal_string("123456789012345678901234567890.99", exponent=2) == (
        12345678901234567890123456789099
    )


@pytest.mark.parametrize("value", ["", "abc", "1.2.3", "NaN", "Infinity", "12,34"])
def test_text_that_is_not_an_amount_is_refused(value: str) -> None:
    with pytest.raises(MoneyError):
        from_decimal_string(value, exponent=2)


def test_a_datetime_is_not_a_calendar_date() -> None:
    """`datetime` subclasses `date`, so Python permits exactly what AC-6.4 forbids."""
    with pytest.raises(TemporalError, match="not a calendar date"):
        calendar_date(datetime(2026, 9, 6, 12, 0, tzinfo=UTC))


def test_a_calendar_date_is_a_calendar_date() -> None:
    assert calendar_date(date(2026, 9, 6)) == date(2026, 9, 6)


def test_a_naive_datetime_is_not_an_instant() -> None:
    with pytest.raises(TemporalError, match="naive"):
        utc_instant(datetime(2026, 9, 6, 12, 0))


def test_an_offset_instant_is_normalized_to_utc() -> None:
    """Stored as given, two instants an hour apart would sort as the same moment."""
    east = datetime(2026, 9, 6, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    normalized = utc_instant(east)
    assert normalized.tzinfo is UTC
    assert normalized.isoformat() == "2026-09-06T10:00:00+00:00"


def test_now_is_an_aware_utc_instant() -> None:
    assert now_utc().tzinfo is UTC


def test_the_two_kinds_are_not_interchangeable_at_runtime_either() -> None:
    """The type checker is the mechanism; this is the second layer under it.

    `tests/store/test_temporal_types_are_distinct.py` asserts that mypy refuses
    the assignment. This asserts that the constructors refuse the value, which
    is what protects the paths that reach them through `Any` -- a JSON payload,
    a database row, a `cast`.
    """
    an_instant: UtcInstant = now_utc()
    a_date: CalendarDate = calendar_date(date(2026, 9, 6))

    with pytest.raises(TemporalError):
        calendar_date(an_instant)
    with pytest.raises(TemporalError):
        utc_instant(a_date)  # type: ignore[arg-type]
