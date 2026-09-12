"""What a COMPLETE investment-transaction window establishes, once its pages are in.

🔴 **Why this is not a deriver.** Every other normalization in this build reads
one archived body and writes the rows it contains. The two facts recorded here
are properties of a whole window instead, and neither is visible from a single
page:

- **A removal.** `/investments/transactions/get` sends no removal signal of any
  kind -- no `removed` list, no tombstone, no flag *(measured,
  `api-notes-plaid.md` §26)*. Unlike `/transactions/sync`, it is not a delta: it
  answers with the whole window every time. So the only thing that says a
  transaction went away is its ABSENCE from a window that came back complete,
  and absence cannot be read off page four of twelve.
- **The range that actually returned** (AC-3.3). The rows arrive newest-first,
  so the earliest date in the window is on the LAST page and nothing states it
  *(§26)*.

🔴 **Both refuse to act on a window that was not seen whole, and that refusal is
the only thing standing between this module and silent data loss.** A run that
stopped at its page ceiling, lost its connection, or was killed has fetched a
PREFIX of the window; every row it never reached is absent from what it saw.
Reconciling on that prefix would soft-delete real transactions in bulk, and the
store would look exactly as it should if the institution had genuinely dropped
them. So exhaustion is not a caller's promise to keep -- it is derived here, from
the evidence the caller passes, once, for every caller.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from sqlalchemy import Connection as SAConnection
from sqlalchemy import func, select, update

from bankmachine.logging_setup import get_logger
from bankmachine.store.connection import StoreError
from bankmachine.store.schema import (
    INVESTMENTS_DOMAIN,
    accounts,
    investment_transactions,
    sync_state,
)
from bankmachine.store.types import CalendarDate, UtcInstant

_log = get_logger(__name__)


class IncompleteWindowError(StoreError):
    """A window was offered as complete while its own evidence says otherwise.

    Raised rather than absorbed: a caller reaching this point with no pages has
    a defect in its page loop, and absorbing it would turn that defect into the
    quiet no-op that looks identical to a window with nothing to remove.

    🔴 **A `StoreError`, so that it costs ONE connection rather than the run.**
    The sync path catches `(ConnectorError, StoreError)` per connection, and an
    exception outside both escapes to end the run and skip every connection
    queued behind it -- which AC-4.1 forbids, and which `_page`'s own docstring
    records having happened once already. A defect in one connection's page loop
    is still a defect; it is not a reason for the institutions behind it to go
    unsynced.
    """


@dataclass(frozen=True, slots=True)
class WindowOutcome:
    """What the completed window changed, for the caller's report.

    `exhausted` is False for every run that stopped short, and the other two
    fields are then meaningless by construction -- nothing was concluded, so
    nothing was written.
    """

    exhausted: bool
    removed: int
    history_start_date: CalendarDate | None


def record_investment_transaction_window(
    conn: SAConnection,
    *,
    connection_id: int,
    window_start: CalendarDate,
    window_end: CalendarDate,
    page_response_ids: Collection[int],
    rows_seen: int,
    stated_total: int | None,
    at: UtcInstant,
) -> WindowOutcome:
    """Soft-delete what the window did not return, and record the range it did.

    Runs inside the caller's transaction, so the removals and the range they were
    concluded from commit together or not at all. A store that had soft-deleted
    rows while still reporting the previous window's range would be claiming a
    measurement its own rows contradict.

    `stated_total` is the aggregator's `total_investment_transactions`, and
    `rows_seen` the number of rows the pages actually carried. Exhaustion is
    `rows_seen >= stated_total`, computed here rather than passed as a flag: a
    boolean argument is a claim each caller makes separately, and the cost of one
    caller getting it wrong is paid in deleted history.

    A null `stated_total` is an UNMEASURED window -- the response did not state a
    total, so nothing can be concluded about what is missing from it -- and is
    treated exactly as a short run is.
    """
    if stated_total is None or rows_seen < stated_total:
        # Not a failure. The ordinary state of a run bounded by its page ceiling,
        # which AC's exit-code contract reports as work still owed rather than as
        # something wrong.
        _log.info(
            "connection %d saw %d of %s investment transactions in its window, so the "
            "window is incomplete: nothing is soft-deleted and no range is recorded",
            connection_id,
            rows_seen,
            "an unstated number of" if stated_total is None else stated_total,
        )
        return WindowOutcome(exhausted=False, removed=0, history_start_date=None)

    if not page_response_ids:
        # 🔴 The fail-open this guard exists for. `NOT IN ()` is true of every
        # row, so an empty set of pages would soft-delete the connection's ENTIRE
        # window -- and it would do it on the code path that believes the window
        # was complete. A window cannot be exhausted by fetching nothing.
        raise IncompleteWindowError(
            f"connection {connection_id} reports {rows_seen} of {stated_total} investment "
            f"transactions over {window_start}..{window_end} with no archived pages to "
            f"attribute them to. Reconciling here would soft-delete the whole window"
        )

    connection_accounts = select(accounts.c.account_id).where(
        accounts.c.connection_id == connection_id
    )
    in_window = (
        investment_transactions.c.account_id.in_(connection_accounts),
        investment_transactions.c.trade_date >= window_start,
        investment_transactions.c.trade_date <= window_end,
    )

    removed = conn.execute(
        update(investment_transactions)
        .where(
            *in_window,
            investment_transactions.c.removed_at.is_(None),
            # 🔴 An aggregator window is evidence about aggregator rows and about
            # nothing else. A manually imported transaction is absent from every
            # window because the aggregator never had it, so the row's own author
            # is what decides whether its absence means anything -- the test is
            # authorship of the ROW, not of the value in it.
            #
            # `raw_response_id` is null on a manual row, and `NOT IN` is null
            # rather than true for it, so that column alone would already exclude
            # these. Both are written: one of them is a SQL three-valued-logic
            # subtlety and the other says what is meant.
            investment_transactions.c.source == "aggregator",
            investment_transactions.c.raw_response_id.not_in(page_response_ids),
        )
        .values(removed_at=at, updated_at=at)
    ).rowcount

    # Measured AFTER the removals, so a transaction this window retired cannot
    # set the range the window is recorded as having returned.
    start = conn.execute(
        select(func.min(investment_transactions.c.trade_date)).where(
            *in_window,
            investment_transactions.c.removed_at.is_(None),
            investment_transactions.c.source == "aggregator",
        )
    ).scalar_one_or_none()

    if start is not None:
        conn.execute(
            update(sync_state)
            .where(
                sync_state.c.connection_id == connection_id,
                sync_state.c.domain == INVESTMENTS_DOMAIN,
            )
            .values(history_start_date=start, updated_at=at)
        )
    else:
        # 🔴 An exhausted window that returned nothing. The column is left as it
        # was rather than nulled: null means NOBODY HAS EVER MEASURED, and
        # overwriting a range a previous run did measure would destroy a fact to
        # record the absence of one. A connection whose investment accounts are
        # simply quiet is not a connection whose history shrank.
        _log.info(
            "connection %d returned a complete investment-transaction window holding no "
            "rows, so the recorded range is left as it was",
            connection_id,
        )

    if removed:
        _log.info(
            "connection %d: %d investment transaction(s) inside %s..%s did not come back in "
            "a complete window and are soft-deleted",
            connection_id,
            removed,
            window_start,
            window_end,
        )
    return WindowOutcome(exhausted=True, removed=removed, history_start_date=start)
