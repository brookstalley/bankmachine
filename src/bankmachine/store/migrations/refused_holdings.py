"""Migration 011 -- `refused_holdings`, the positions this build could not record.

**This DDL never changes.** The rule migrations 002 through 010 state applies
here unaltered: a change to this table is migration 012.

🔴 **The idioms are re-declared here rather than imported from an earlier
migration**, for the reason `core_schema.py` names: two migrations that must
never move together do not share a constant.

**What the table is for.** A position whose currency has no minor-unit exponent
this build knows -- or that states no currency at all -- is refused per row:
`holdings.market_value_minor` is NOT NULL, and a value at an unknown scale cannot
be written there, so the position is skipped and the account's other positions
are kept. Until this table the refusal reached a log line and nothing else, so
`list_holdings` served an account holding a coin position as though its other
positions were all of it: a smaller portfolio with no signal. `data-model.md`'s
valuation norm requires the refusal to be NAMED under `rule-applied`, and a read
can only name what the store records.

🔴 **A record of a refusal, not a position with its value missing.** Relaxing
`holdings.market_value_minor` to nullable would put a row in the one table every
holdings total reads, carrying no figure -- the shape a later sum treats as zero.
Kept apart, a position is always a value and a refusal is never one.

🔴 **A raw response is the only provenance, so there is no `source` column.**
Nothing refuses a position except deriving an archived capture, so
`raw_response_id` is NOT NULL and the exclusive-provenance rule every silver row
obeys reduces to that one column. `store rebuild` classifies the table as
rebuildable by that foreign key, with nothing to register.

**The holdings append rule applies unchanged**: one record per account, security
and capture day, and the first capture of the day keeps it. A capture later the
same day that DOES record the position writes its holding beside this record
rather than removing it -- each table keeps its own first capture, which is what
lets a replay land on the same rows in either order.

🔴 **Empty on arrival, with no backfill.** A migration cannot know which archived
positions a build refused, so `bankmachine store rebuild` fills the table from
the archived captures, which is why `DERIVATION_VERSION` moves with this
migration.
"""

from __future__ import annotations

from typing import Final

from bankmachine.store.connection import Connection

#: Migration 011's own spelling of a calendar date. See the module docstring.
_V11_DATE: Final = "GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"

#: Migration 011's own spelling of a UTC instant.
_V11_INSTANT: Final = "LIKE '%+00:00'"

#: The CHECKs are on the columns themselves, so `sync shell` -- which reaches the
#: file with no type decorator in between -- cannot write an instant where a
#: calendar date belongs, or the reverse.
REFUSED_HOLDINGS_DDL: Final[tuple[str, ...]] = (
    f"""
    CREATE TABLE refused_holdings (
        account_id            INTEGER NOT NULL REFERENCES accounts(account_id),
        security_id           INTEGER NOT NULL REFERENCES securities(security_id),
        as_of_date            TEXT NOT NULL CHECK (as_of_date {_V11_DATE}),
        currency              TEXT,
        captured_at           TEXT NOT NULL CHECK (captured_at {_V11_INSTANT}),
        raw_response_id       INTEGER NOT NULL REFERENCES raw_responses(raw_response_id),
        derivation_version_id INTEGER NOT NULL
                              REFERENCES derivation_versions(derivation_version_id),
        PRIMARY KEY (account_id, security_id, as_of_date)
    )
    """,
)


def apply_refused_holdings(conn: Connection) -> None:
    """Issue migration 011's DDL. The runner owns the transaction around it."""
    for statement in REFUSED_HOLDINGS_DDL:
        conn.execute(statement)
