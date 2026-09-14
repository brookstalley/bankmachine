"""Migration 008 -- what the aggregator says about the Item itself.

**This DDL never changes.** The rule migrations 002 through 007 state applies
here unaltered: a migration is a record of what some datastore in the world
already ran, so a change to these columns is migration 009.

🔴 **The idioms are re-declared here rather than imported from an earlier
migration**, for the reason `core_schema.py` names: sharing a constant means a
later tightening of it silently redefines what an older version meant for every
datastore created afterwards, with every test still green.

**What the columns are for.** `/item/get` answers with two facts about the
connection that no other call carries, and both were archived and read by
nothing:

* `consent_expiration_time` -- the day the operator's authorisation lapses. A
  connection whose consent expires next week answers *healthy* and stays healthy
  right up to the sync that fails. The pipeline is poll-only, so expiry
  otherwise surfaces as a failure on the NEXT run rather than in advance, and a
  health surface holding the date and not saying it is the one place that could
  have made it advance notice.
* `item.error` -- the aggregator's own STANDING complaint about the Item, which
  is not the same fact as `last_error_code`. That column records the last sync
  attempt failing; this one records the aggregator saying the Item is unwell
  whether or not the last attempt happened to succeed. Folding them together
  would let a successful sync erase a complaint nobody resolved.

🔴 **An INSTANT and a CODE, and the names say which.** `consent_expires_at` is a
UTC instant because the aggregator states one; `data-model.md` § *Calendar dates
and UTC instants never mix* makes the choice a type rather than a convention.
`source_error_code` keeps the `source_` prefix every column carrying the
aggregator's own vocabulary has, so a reader never mistakes it for a code this
product defined.

🔴 **Nullable, and no backfill.** The runner's contract is DDL only, and there is
nothing honest to backfill with: nothing ever read these, so no datastore holds
them. A null means *this connection's Item has not been fetched since the
columns existed* -- never *consent does not expire* and never *the aggregator
reports no error*. The read path must not read either null as reassurance, which
is the whole failure mode this migration exists to end.

`bankmachine sync run` fills them on the next `/item/get`, which every run makes.
"""

from __future__ import annotations

from typing import Final

from bankmachine.store.connection import Connection

#: Migration 008's own spelling of a UTC instant, deliberately not shared with
#: any earlier migration's. See the module docstring.
_V8_INSTANT: Final = (
    "GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]*'"
)

#: 🔴 Both columns land at the END of `connections`, because that is where
#: SQLite's `ALTER TABLE ... ADD COLUMN` puts them and `store/schema.py`'s Core
#: metadata is compared to this database column-by-column *in order*. Declaring
#: them beside `last_error_code`, where they read better, would make two correct
#: descriptions of one correct database disagree.
ITEM_CONSENT_DDL: Final[tuple[str, ...]] = (
    "ALTER TABLE connections ADD COLUMN consent_expires_at TEXT "
    f"CHECK (consent_expires_at {_V8_INSTANT})",
    "ALTER TABLE connections ADD COLUMN source_error_code TEXT",
)


def apply_item_consent(conn: Connection) -> None:
    """Issue migration 008's DDL. The runner owns the transaction around it."""
    for statement in ITEM_CONSENT_DDL:
        conn.execute(statement)
