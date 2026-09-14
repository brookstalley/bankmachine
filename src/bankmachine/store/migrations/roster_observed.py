"""Migration 004 -- `connections.roster_observed_date`, the record of *we looked*.

**This DDL never changes.** The rule migrations 002 and 003 state applies here
unaltered: a migration is a record of what some datastore in the world already
ran, so a change to this column is migration 005.

🔴 **The idioms are re-declared here rather than imported from an earlier
migration**, for the reason `core_schema.py` names: sharing a constant means a
later tightening of it silently redefines what an older version meant for every
datastore created afterwards, with every test still green. Two migrations that
must never move together do not share a constant.

**What the column is for.** AC-12.4 records the roster observation per
CONNECTION as well as per account, and the two say different things:
`accounts.last_seen_date` says *this is what we found*, and this column says
*we looked*. Deriving the second from the first -- taking the maximum of a
connection's own accounts' last-listed dates -- is the design this replaces, and
it collapses the pair. A maximum over the accounts that were listed moves with
them, so a roster that comes back listing nothing leaves nothing behind it and
"the roster was observed, and this account was not in it" becomes inexpressible
exactly when it is true. At a connection holding one account that is the
ordinary case rather than an exotic one.

🔴 **A CALENDAR DATE, and the name says so.** `roster_observed_at` was the
obvious spelling and is wrong here: this value's only use is a comparison
against `accounts.last_seen_date`, which is a calendar date, and
`data-model.md` § *Calendar dates and UTC instants never mix* makes a comparison
across the two types a defect rather than a conversion.

**`connections.last_success_at` is not this fact and cannot stand in for it.**
It records a sync attempt succeeding, not a roster being read, and the two
diverge whenever a sync succeeds without a roster call.

**Nullable, and no backfill.** The runner's contract is that a migration's
`apply` issues DDL only, and a backfill is DML -- but the deeper reason is that
there is nothing honest to backfill with. Nothing ever recorded when a roster
was read, which is the whole reason this column exists.

🔴 **So a null means "this connection's roster has never been observed", and
`query._account_lifecycle` reads it as exactly that -- never as a date.** A
connection with no observation marks nothing absent (AC-12.5's first clause).

🔴 **That is correct and it is not free, and calling the window "safe" would
overstate it.** The design this replaces derived the observation from the
accounts themselves, so a store already running it reported some accounts
absent. Those accounts read `active` again from this migration until their
connection's next successful sync -- a frozen balance with no warning, which is
the failure FR-9 exists to remove. The window closes on the next sync for a
healthy connection and NOT AT ALL for one that never succeeds again, which is
the connection whose absent accounts matter most. `bankmachine store rebuild`
closes it deliberately by replaying the archived roster responses; the upgrade
procedure in `operational-spec.md` says when to run it.
"""

from __future__ import annotations

from typing import Final

from bankmachine.store.connection import Connection

#: Migration 004's own spelling of a calendar date, deliberately not shared with
#: migration 002's or 003's. See the module docstring.
_V4_DATE: Final = "GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"

#: 🔴 The column lands at the END of `connections`, because that is where
#: SQLite's `ALTER TABLE ... ADD COLUMN` puts it and `store/schema.py`'s Core
#: metadata is compared to this database column-by-column *in order*
#: (`tests/store/test_schema.py::test_the_metadata_matches_the_migrated_database`).
#: Declaring it beside `last_success_at` in the metadata, where it reads better,
#: would make two correct descriptions of one correct database disagree.
ROSTER_OBSERVED_DDL: Final[tuple[str, ...]] = (
    "ALTER TABLE connections ADD COLUMN roster_observed_date TEXT "
    f"CHECK (roster_observed_date {_V4_DATE})",
)


def apply_roster_observed(conn: Connection) -> None:
    """Issue migration 004's DDL. The runner owns the transaction around it."""
    for statement in ROSTER_OBSERVED_DDL:
        conn.execute(statement)
