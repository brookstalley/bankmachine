"""Migration 005 -- `transactions.ledger_date`, the day the money was committed.

**This DDL never changes.** The rule migrations 002, 003 and 004 state applies
here unaltered: a migration is a record of what some datastore in the world
already ran, so a change to this column is migration 006.

🔴 **The idioms are re-declared here rather than imported from an earlier
migration**, for the reason `core_schema.py` names: sharing a constant means a
later tightening of it silently redefines what an older version meant for every
datastore created afterwards, with every test still green.

**What the column is for, and why `posted_date` could not stay the only date.**
The aggregator's `date` is the transaction date while a charge is pending and
the POSTING date once it settles, and the deriver mirrors that faithfully -- so
a hold authorised on 06-28 and posted on 07-02 silently moves from June to July
between two syncs. Every window measured on `posted_date` therefore reports a
different June depending on when it is asked, with nothing on the answer to say
so. `ledger_date` is the household-economic date: *the day the money was
committed from the account holder's point of view*.

- **On insert:** `COALESCE(authorized_date, posted_date)`.
- **On every later derivation of the same row: it is left alone.** Settlement
  never moves it. For an institution that reports `authorized_date` recomputing
  would be harmless, because that field does not change; for one that does not,
  recomputing on settlement is precisely the defect. One rule covers both.

`posted_date` keeps tracking the source and still moves on settlement, which is
correct -- it is the aggregator's own `date`, and mirroring it is this store's
job. The split is what makes both honest: economic questions (windows, monthly
grouping, hold tallies, how long an authorisation has been outstanding) read
`ledger_date`; delivery questions (coverage, cadence, gaps, staleness, *when did
this arrive*) read `posted_date`.

🔴 **Measuring windows on `authorized_date` itself was rejected and must not be
reintroduced.** It is documented nullable and is null for institutions that do
not report it, so a window measured on it would mean different things on
different rows of the same total -- some counted by when money was committed,
others by when it cleared -- and no caller could tell which. That is the failure
this product exists to prevent, arriving inside the fix.

🔴 **Nullable, and no backfill, which is the runner's contract and also the only
honest option.** `apply` issues DDL only. There is no constant default that is
correct here, because the value is a per-row expression; and `ALTER TABLE ... ADD
COLUMN` cannot compute one. So a null means **"this row predates the split and
has not been rebuilt"** -- never "committed on the posting date", which is the
very reading that would reinstate the defect.

🔴 **`bankmachine store rebuild` is what fills it, and unlike migration 004's
column there is something real to fill it from.** `transactions` carries
`derivation_version_id` and is rebuildable, so replaying the archived responses
restamps every row with a `ledger_date` derived from the same raw body that
produced it. `DERIVATION_VERSION` moves with this migration so the replay is a
recorded change rather than a silent one, and the upgrade procedure in
`operational-spec.md` prescribes the rebuild.

🔴 **Until that rebuild runs, the read path must not coalesce the null away.**
A window that silently fell back to `posted_date` would answer with exactly the
number this column exists to stop being wrong, and would do it invisibly. Rows
with no `ledger_date` are disclosed, not defaulted.
"""

from __future__ import annotations

from typing import Final

from bankmachine.store.connection import Connection

#: Migration 005's own spelling of a calendar date, deliberately not shared with
#: migration 002's, 003's or 004's. See the module docstring.
_V5_DATE: Final = "GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"

#: 🔴 The column lands at the END of `transactions`, because that is where
#: SQLite's `ALTER TABLE ... ADD COLUMN` puts it and `store/schema.py`'s Core
#: metadata is compared to this database column-by-column *in order*
#: (`tests/store/test_schema.py::test_the_metadata_matches_the_migrated_database`).
#: Declaring it beside `posted_date` in the metadata, where it reads far better
#: and where the split it makes would be obvious, would make two correct
#: descriptions of one correct database disagree.
LEDGER_DATE_DDL: Final[tuple[str, ...]] = (
    f"ALTER TABLE transactions ADD COLUMN ledger_date TEXT CHECK (ledger_date {_V5_DATE})",
)


def apply_ledger_date(conn: Connection) -> None:
    """Issue migration 005's DDL. The runner owns the transaction around it."""
    for statement in LEDGER_DATE_DDL:
        conn.execute(statement)
