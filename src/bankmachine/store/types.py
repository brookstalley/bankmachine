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

from collections.abc import Iterable, Mapping
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


class UnknownMinorDigitsError(MoneyError):
    """A currency whose minor-unit exponent this build does not know.

    🔴 **Named rather than defaulted, and that is the whole point of the table
    below.** An exponent guessed at 2 is right for most fiat and wrong for every
    cryptocurrency: `0.04217` in a currency whose real exponent is 8 becomes
    `0.04`, a 0.4% loss that every later total inherits and no reader can see.
    A unit this build cannot express exactly is a state, not a fallback -- the
    callers refuse the row and say which one, and the archive keeps the value so
    a build that knows the exponent can derive it later.
    """


#: Every currency this build can express in minor units, and how many minor
#: digits each has: ISO 4217's alphabetic codes with their published exponents.
#:
#: 🔴 **A lookup, not a default.** The table used to hold the non-2 exceptions
#: alone and answer 2 for everything else, on the argument that a missing
#: exception would store a JPY balance 100x too large while a missing 2-decimal
#: currency would only refuse an ordinary account. The argument is sound about
#: ISO's own codes and does not reach the ones the aggregator sends in
#: `unofficial_currency_code`, which are not ISO codes at all and for which 2 is
#: not a convention but a guess -- so the default silently rounded every
#: cryptocurrency amount to hundredths.
#:
#: 🔴 **A code this table does not carry has no exponent here, and the fix is to
#: add it** -- one line, on evidence of what the currency's minor unit actually
#: is. That is the "configured rather than guessed" half: an unofficial currency
#: whose exponent is known derives exactly and refuses nothing.
#:
#: The metals and fund codes (`XAU`, `XDR`, ...) are deliberately absent: ISO
#: publishes no minor unit for them, so there is nothing to add and a holding
#: denominated in one is refused, which is the honest answer.
_MINOR_DIGITS: Final[Mapping[str, int]] = {
    # Zero-decimal
    "BIF": 0, "CLP": 0, "DJF": 0, "GNF": 0, "ISK": 0, "JPY": 0, "KMF": 0,
    "KRW": 0, "PYG": 0, "RWF": 0, "UGX": 0, "UYI": 0, "VND": 0, "VUV": 0,
    "XAF": 0, "XOF": 0, "XPF": 0,
    # Three-decimal
    "BHD": 3, "IQD": 3, "JOD": 3, "KWD": 3, "LYD": 3, "OMR": 3, "TND": 3,
    # Four-decimal
    "CLF": 4, "UYW": 4,
    # Two-decimal -- the rest of ISO 4217, enumerated rather than defaulted.
    "AED": 2, "AFN": 2, "ALL": 2, "AMD": 2, "ANG": 2, "AOA": 2, "ARS": 2,
    "AUD": 2, "AWG": 2, "AZN": 2, "BAM": 2, "BBD": 2, "BDT": 2, "BGN": 2,
    "BMD": 2, "BND": 2, "BOB": 2, "BOV": 2, "BRL": 2, "BSD": 2, "BTN": 2,
    "BWP": 2, "BYN": 2, "BZD": 2, "CAD": 2, "CDF": 2, "CHE": 2, "CHF": 2,
    "CHW": 2, "CNY": 2, "COP": 2, "COU": 2, "CRC": 2, "CUP": 2, "CVE": 2,
    "CZK": 2, "DKK": 2, "DOP": 2, "DZD": 2, "EGP": 2, "ERN": 2, "ETB": 2,
    "EUR": 2, "FJD": 2, "FKP": 2, "GBP": 2, "GEL": 2, "GHS": 2, "GIP": 2,
    "GMD": 2, "GTQ": 2, "GYD": 2, "HKD": 2, "HNL": 2, "HTG": 2, "HUF": 2,
    "IDR": 2, "ILS": 2, "INR": 2, "IRR": 2, "JMD": 2, "KES": 2, "KGS": 2,
    "KHR": 2, "KPW": 2, "KYD": 2, "KZT": 2, "LAK": 2, "LBP": 2, "LKR": 2,
    "LRD": 2, "LSL": 2, "MAD": 2, "MDL": 2, "MGA": 2, "MKD": 2, "MMK": 2,
    "MNT": 2, "MOP": 2, "MRU": 2, "MUR": 2, "MVR": 2, "MWK": 2, "MXN": 2,
    "MXV": 2, "MYR": 2, "MZN": 2, "NAD": 2, "NGN": 2, "NIO": 2, "NOK": 2,
    "NPR": 2, "NZD": 2, "PAB": 2, "PEN": 2, "PGK": 2, "PHP": 2, "PKR": 2,
    "PLN": 2, "QAR": 2, "RON": 2, "RSD": 2, "RUB": 2, "SAR": 2, "SBD": 2,
    "SCR": 2, "SDG": 2, "SEK": 2, "SGD": 2, "SHP": 2, "SLE": 2, "SOS": 2,
    "SRD": 2, "SSP": 2, "STN": 2, "SVC": 2, "SYP": 2, "SZL": 2, "THB": 2,
    "TJS": 2, "TMT": 2, "TOP": 2, "TRY": 2, "TTD": 2, "TWD": 2, "TZS": 2,
    "UAH": 2, "USD": 2, "USN": 2, "UYU": 2, "UZS": 2, "VED": 2, "VES": 2,
    "WST": 2, "XCD": 2, "XCG": 2, "YER": 2, "ZAR": 2, "ZMW": 2, "ZWG": 2,
}  # fmt: skip


def minor_digits(currency: str) -> int:
    """How many minor digits a currency has, or a refusal naming it.

    Lives beside the money type rather than in the aggregator's package, because
    it is a property of the currency: the manual-import path and any second
    aggregator need the same answer, and a copy of this table is a second answer
    waiting to disagree with the first.
    """
    try:
        return int(_MINOR_DIGITS[currency.upper()])
    except KeyError:
        raise UnknownMinorDigitsError(
            f"this build does not know how many minor digits {currency!r} has, so no amount in "
            f"it can be stored exactly. Add the currency's exponent to `store/types.py` on "
            f"evidence of what its minor unit is; guessing at two would round a value that is "
            f"not in hundredths and every total computed from it would inherit the loss"
        ) from None


def has_minor_digits(currency: str | None) -> bool:
    """Whether an amount in `currency` can be expressed in minor units at all.

    The question the read path asks of a stored code, where a refusal would be
    the wrong shape: it decides whether an account's rows may enter a
    minor-units aggregate, and `None` -- a currency the aggregator has never
    stated -- answers it the same way an unknown exponent does.
    """
    return currency is not None and currency.upper() in _MINOR_DIGITS


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
