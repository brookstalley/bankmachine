"""Migration 003 -- `accounts.last_seen_date`, the maximum-side twin of `first_seen_date`.

**This DDL never changes either.** The rule `core_schema.py` states for migration
002 is a rule about forward-only migrations, not about that one file: a migration
is a record of what some datastore in the world already ran, and editing one
rewrites history for every store that ran it. Changing this column is migration
004.

🔴 **The idioms are re-declared here rather than imported from `core_schema.py`,
and that is deliberate.** `core_schema.py`'s own docstring names the trap: the
obvious move is to reuse `_V2_DATE`, and doing so means a later tightening of one
constant silently redefines what version 2 means for every datastore created
afterwards, with every test still green. Two migrations that must never move
together do not share a constant.

**What the column is for.** AC-12.4: each successful roster derivation records,
per account, the date that account was last listed -- as a *monotone maximum*,
mirroring `first_seen_date`'s minimum. A min at one end and a max at the other is
what makes an archive replay order-independent, which is the specific care
`connector/plaid/derivers.py` recorded as the reason account retirement was
deferred rather than half-built. Without a recorded observation there is no
non-`active` state for the read path to read at all.

**Nullable, and no backfill.** The runner's contract is that a migration's
`apply` issues DDL only, and a backfill of existing rows is DML. Rather than
widen that convention for one column, the read path carries a transitional rule:
a null `last_seen_date` is read as `first_seen_date`, which is true by
construction -- the account was listed at least once, on that date. The rule
retires itself once every live account has been observed by one post-migration
sync; a null surviving after that belongs to an account no roster has listed
since the migration, which is exactly what the read path is looking for.
`query._account_lifecycle` is where the rule lives.
"""

from __future__ import annotations

from typing import Final

from bankmachine.store.connection import Connection

#: Migration 003's own spelling of a calendar date, deliberately not shared with
#: migration 002's. See the module docstring.
_V3_DATE: Final = "GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"

#: 🔴 The column lands at the END of `accounts`, because that is where SQLite's
#: `ALTER TABLE ... ADD COLUMN` puts it and `store/schema.py`'s Core metadata is
#: compared to this database column-by-column *in order*
#: (`tests/store/test_schema.py::test_the_metadata_matches_the_migrated_database`).
#: Declaring it next to `first_seen_date` in the metadata, where it reads better,
#: would make the two descriptions disagree about a database that is correct.
#:
#: No table-level CHECK pairs it with `first_seen_date` the way `closed_date` has
#: one: `ALTER TABLE` cannot add a table constraint, and a monotone maximum that
#: starts at the same value the minimum did cannot fall below it without the
#: deriver being wrong in a way a CHECK here would not be the honest place to
#: catch. `tests/connector/test_derivers.py` is.
ACCOUNT_LAST_SEEN_DDL: Final[tuple[str, ...]] = (
    f"ALTER TABLE accounts ADD COLUMN last_seen_date TEXT CHECK (last_seen_date {_V3_DATE})",
)


def apply_account_last_seen(conn: Connection) -> None:
    """Issue migration 003's DDL. The runner owns the transaction around it."""
    for statement in ACCOUNT_LAST_SEEN_DDL:
        conn.execute(statement)
