"""The typed vocabulary the schema is written in.

Three of this product's requirements are about *types* rather than behaviour,
and all three fail silently when they fail. Money that becomes a float loses
fractions of a cent nobody notices (AC-6.2). A calendar date used as an instant
moves a transaction across a day boundary in whichever direction the local
timezone leans, and an instant used as a date does the same in reverse
(AC-6.4). Neither crashes; both produce analysis that is wrong and internally
consistent, which is the failure this product exists to prevent.

So the three are made distinct here rather than remembered at each call site.
`MinorUnits`, `CalendarDate` and `UtcInstant` are distinct to mypy, and the
column types beside them are the only sanctioned way any of the three reaches
the database.

The runtime constructors matter as much as the annotations, because `datetime`
is a subclass of `date` in Python -- the substitution AC-6.4 forbids is one the
language permits and the type checker cannot see through a cast. `calendar_date`
refuses a datetime; `utc_instant` refuses a naive one.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final, NewType

from sqlalchemy import Integer, Text, TypeDecorator
from sqlalchemy.engine.interfaces import Dialect

#: An amount in a currency's smallest indivisible unit -- cents, pence, yen.
#: Never a float, never a Decimal at rest. AC-6.2.
MinorUnits = NewType("MinorUnits", int)

#: A day on a calendar, with no time and no zone. What a transaction is dated.
CalendarDate = NewType("CalendarDate", date)

#: A timezone-aware instant, normalized to UTC. What sync metadata is stamped.
UtcInstant = NewType("UtcInstant", datetime)

#: The wire form of a UTC instant, and the suffix the schema's CHECK constraints
#: look for. `datetime.isoformat()` on a UTC-aware value ends in exactly this.
UTC_SUFFIX: Final = "+00:00"


class MoneyError(ValueError):
    """A monetary value could not be represented exactly in minor units."""


class TemporalError(ValueError):
    """A date or an instant was not of the kind the schema requires."""


def minor_units(value: int) -> MinorUnits:
    """Narrow an int to an amount. Rejects bool, which int would otherwise accept."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise MoneyError(f"minor units must be an int, got {type(value).__name__}")
    return MinorUnits(value)


def add(left: MinorUnits, right: MinorUnits) -> MinorUnits:
    """Sum of two amounts. Integer addition, so the result is exact by construction."""
    return MinorUnits(int(left) + int(right))


def negate(amount: MinorUnits) -> MinorUnits:
    """The same magnitude, other sign. Used wherever a sign convention is inverted."""
    return MinorUnits(-int(amount))


def total(amounts: Iterable[MinorUnits]) -> MinorUnits:
    """Sum of any number of amounts, including none, which is zero."""
    return MinorUnits(sum(int(amount) for amount in amounts))


def from_decimal_string(value: str, *, exponent: int) -> MinorUnits:
    """Parse a source's decimal string into minor units, exactly or not at all.

    This is the one place a float could enter the system: every aggregator and
    every exported statement states amounts as decimal text, and the obvious
    `round(float(value) * 100)` is wrong in a way that survives every test
    written with round numbers.

    The scaling is done on the digit tuple rather than by Decimal arithmetic, so
    no context precision applies and no value is too large to convert exactly.
    A value carrying more precision than the currency has minor digits raises
    rather than rounding: silent rounding is how money disappears, and a
    fraction of a cent that vanishes on every row is exactly the discrepancy
    AC-11.2 asks to be itemized rather than averaged away.
    """
    if exponent < 0:
        raise MoneyError(f"exponent must not be negative, got {exponent}")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise MoneyError(f"{value!r} is not a decimal number") from exc
    if not parsed.is_finite():
        raise MoneyError(f"{value!r} is not a finite amount")

    sign, digits, digit_exponent = parsed.as_tuple()
    assert isinstance(digit_exponent, int)  # is_finite() rules out 'n', 'N' and 'F'
    magnitude = int("".join(str(digit) for digit in digits) or "0")
    shift = digit_exponent + exponent
    if shift >= 0:
        scaled = magnitude * 10**shift
    else:
        divisor = 10**-shift
        if magnitude % divisor:
            raise MoneyError(
                f"{value!r} carries more precision than {exponent} minor digit(s); refusing to "
                f"round, because a fraction lost on every row reconciles to nothing"
            )
        scaled = magnitude // divisor
    return MinorUnits(-scaled if sign else scaled)


def to_decimal_string(amount: MinorUnits, *, exponent: int) -> str:
    """Render minor units as decimal text. The inverse of `from_decimal_string`."""
    if exponent < 0:
        raise MoneyError(f"exponent must not be negative, got {exponent}")
    sign = "-" if amount < 0 else ""
    digits = str(abs(int(amount))).rjust(exponent + 1, "0")
    if exponent == 0:
        return f"{sign}{digits}"
    return f"{sign}{digits[:-exponent]}.{digits[-exponent:]}"


def calendar_date(value: date) -> CalendarDate:
    """Narrow a date to a calendar date, refusing the datetime that subclasses it."""
    if isinstance(value, datetime):
        raise TemporalError(
            f"{value!r} is a datetime, not a calendar date. AC-6.4 keeps the two apart because "
            f"an instant carries a zone and a transaction date does not; converting one to the "
            f"other is a decision, and it belongs at the boundary that knows which zone applies"
        )
    if not isinstance(value, date):
        raise TemporalError(f"a calendar date must be a date, got {type(value).__name__}")
    return CalendarDate(value)


def utc_instant(value: datetime) -> UtcInstant:
    """Narrow a datetime to a UTC instant, refusing a naive one.

    A naive datetime is not an instant: it is a wall-clock reading whose meaning
    depends on where the reader is standing. Stored as one, it silently shifts
    every comparison against a real instant by the local offset.
    """
    if not isinstance(value, datetime):
        raise TemporalError(f"a UTC instant must be a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise TemporalError(
            f"{value!r} is naive; an instant without a zone is a wall-clock reading, and storing "
            f"one shifts every later comparison by whatever offset the reader happens to be at"
        )
    return UtcInstant(value.astimezone(UTC))


def now_utc() -> UtcInstant:
    """The current instant. The only sanctioned source of `updated_at` values."""
    return UtcInstant(datetime.now(UTC))


class MinorUnitsColumn(TypeDecorator[MinorUnits]):
    """An INTEGER column carrying minor units, and nothing that is not an int."""

    impl = Integer
    cache_ok = True

    def process_bind_param(self, value: MinorUnits | None, dialect: Dialect) -> int | None:
        if value is None:
            return None
        return int(minor_units(value))

    def process_result_value(self, value: Any, dialect: Dialect) -> MinorUnits | None:
        if value is None:
            return None
        return minor_units(int(value))


class CalendarDateColumn(TypeDecorator[CalendarDate]):
    """A TEXT column holding `YYYY-MM-DD`, which sorts and compares as a date does."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: CalendarDate | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return calendar_date(value).isoformat()

    def process_result_value(self, value: Any, dialect: Dialect) -> CalendarDate | None:
        if value is None:
            return None
        return calendar_date(date.fromisoformat(str(value)))


class UtcInstantColumn(TypeDecorator[UtcInstant]):
    """A TEXT column holding an ISO-8601 instant that always ends `+00:00`.

    The suffix is not cosmetic: it is what the schema's CHECK constraints test,
    so an instant written by any path that bypasses this type is rejected by the
    database rather than stored as an ambiguous local reading.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: UtcInstant | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return utc_instant(value).isoformat()

    def process_result_value(self, value: Any, dialect: Dialect) -> UtcInstant | None:
        if value is None:
            return None
        return utc_instant(datetime.fromisoformat(str(value)))
