"""Migration 010 -- `holdings.price_as_of`, the date of the price a position is valued at.

**This DDL never changes.** The rule migrations 002 through 009 state applies
here unaltered: a change to this column is migration 011.

🔴 **The idioms are re-declared here rather than imported from an earlier
migration**, for the reason `core_schema.py` names: two migrations that must
never move together do not share a constant.

**What the column is for.** A position carries no date of its own, so
`holdings.as_of_date` is this system's capture day -- always the day a sync ran.
The only date the aggregator puts on a position is the date of the PRICE it was
valued at, and that can be far older than the capture: the aggregator's own
sandbox values every position at a price from 2021 *(`api-notes-plaid.md` §22)*.
Without this column a position captured today at a years-old price reads as
today's value, and nothing in the row can say otherwise.

🔴 **Nullable, and no backfill -- and a null means one of exactly two things.**
Either the row was derived before this column existed and the store has not been
rebuilt since, or the aggregator sent no price date for that position. It is
NEVER "priced on the capture day", so the read path serves the null as it is and
does not coalesce it to `as_of_date`: doing so would put the one claim this
column exists to question back into the answer. `bankmachine store rebuild`
fills it from the archived responses, which is why `DERIVATION_VERSION` moves
with this migration.

**The append rule is unchanged.** The first capture of a day still keeps its
row, price date included; a later capture the same day does not rewrite it.
"""

from __future__ import annotations

from typing import Final

from bankmachine.store.connection import Connection

#: Migration 010's own spelling of a calendar date. See the module docstring.
_V10_DATE: Final = "GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"

#: 🔴 The column lands at the END of `holdings`, because that is where SQLite's
#: `ALTER TABLE ... ADD COLUMN` puts it and `store/schema.py`'s Core metadata is
#: compared to this database column-by-column *in order*.
#:
#: The CHECK is on the column itself, so `sync shell` -- which reaches the file
#: with no type decorator in between -- cannot write an instant where a calendar
#: date belongs.
HOLDINGS_PRICE_AS_OF_DDL: Final[tuple[str, ...]] = (
    f"ALTER TABLE holdings ADD COLUMN price_as_of TEXT CHECK (price_as_of {_V10_DATE})",
)


def apply_holdings_price_as_of(conn: Connection) -> None:
    """Issue migration 010's DDL. The runner owns the transaction around it."""
    for statement in HOLDINGS_PRICE_AS_OF_DDL:
        conn.execute(statement)
