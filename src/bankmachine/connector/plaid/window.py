"""An investment-transaction window, as it is asked for and as it is read back.

`/investments/transactions/get` is a windowed read rather than a delta, and the
reply does not echo the window back *(measured, `api-notes-plaid.md` §26)*. So
the question a page answered survives in exactly one place: the
`request_context` archived beside it. Everything here exists because that string
is load-bearing.

🔴 **Why a replay needs it at all.** A removal on this feed is a row's ABSENCE
from a window that came back whole (`store.investments`), and a deriver sees one
page. Replaying the archive through the derivers alone therefore re-upserts
every row that ever appeared and clears every soft delete with it -- the rebuilt
store would hold rows the synced store had retired, and the rebuild would report
success. `InvestmentWindowReplay` is what stops that: it reassembles each
window from the archive and concludes it at the page that closed it. It is also
the pass the sync runs as each page lands, inside that page's derivation
transaction, so the two paths conclude every window with one piece of code at
one response.

🔴 **Reassembled at the closing page, not once at the end of the replay.** A
removal is stamped with the instant its window closed, and the evidence for it
is the rows that existed at that moment. Running every window's reconciliation
over the finished tables would judge the first window against rows that only
arrived in the third, and soft-delete each of them -- a conclusion no run ever
reached. So the replay reproduces the SEQUENCE of window conclusions, which is
the only thing that reproduces the store.

**One spelling of the context, written and read here.** The client formats it
and the replay parses it; two spellings a paragraph apart is how they come to
disagree, and the disagreement would surface as a rebuild that cannot reproduce
its own content rather than as anything a reader could trace back to here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Final

from sqlalchemy import Connection as SAConnection

from bankmachine.connector import (
    INVESTMENTS_TRANSACTIONS_GET,
    ConnectorError,
    parse_response_body,
)
from bankmachine.logging_setup import get_logger
from bankmachine.store.investments import (
    WindowOutcome,
    record_investment_transaction_window,
    window_is_exhausted,
)
from bankmachine.store.raw import RawResponse
from bankmachine.store.types import CalendarDate, calendar_date

_log = get_logger(__name__)


class UnreadableWindowError(ConnectorError):
    """An archived investments page does not say what window it answered.

    Raised rather than skipped, and that direction is the whole point. A page
    whose window cannot be read is a page a replay cannot attribute to a window
    -- so the reconciliation that produced this connection's soft deletes would
    silently not run, every retired row would come back, and the rebuild would
    report success over a store holding transactions the source had dropped.
    Refusing is loud; the alternative is the silent incompleteness that still
    adds up.
    """

    #: Nothing was asked of the aggregator and nothing would be. The archived
    #: row does not say what it answered, and it will not say it on a second
    #: reading.
    retryable = False


def investment_window_context(*, start_date: date, end_date: date, offset: int, count: int) -> str:
    """The question one page of a window answered, in the form the archive keeps.

    Read back by `read_investment_transaction_page`. It carries no credential
    and no token, as `store.raw` requires of every `request_context`: a window,
    a position in it, and a page size.
    """
    return f"window={start_date}..{end_date} offset={offset} count={count}"


_CONTEXT: Final = re.compile(
    r"window=(?P<start>\d{4}-\d{2}-\d{2})\.\.(?P<end>\d{4}-\d{2}-\d{2}) "
    r"offset=(?P<offset>\d+) count=(?P<count>\d+)"
)


@dataclass(frozen=True, slots=True)
class ArchivedWindowPage:
    """One archived page, read as what it says about its window."""

    window_start: CalendarDate
    window_end: CalendarDate
    offset: int
    rows: int
    stated_total: int | None

    @property
    def rows_through_this_page(self) -> int:
        """How many rows the window has returned once this page is in.

        🔴 The offset the page was fetched at IS the count of rows that came
        before it -- the loop pages by `offset=rows_seen` -- so this is the same
        number the live loop accumulates, arrived at from the archive instead of
        from a running total. That is what lets a replay and a sync reach the
        same exhaustion verdict without either trusting the other's arithmetic.
        """
        return self.offset + self.rows


def read_investment_transaction_page(response: RawResponse) -> ArchivedWindowPage:
    """What one archived page of an investment-transaction window says about it.

    The one reader, used by the sync loop and by the replay, so the two cannot
    disagree about how many rows a page carried or how many the window claims to
    hold. A total that is absent or not an integer comes back `None`, which
    `record_investment_transaction_window` treats as an UNMEASURED window --
    nothing can be concluded about what is missing from it, so nothing is
    soft-deleted on the strength of it.
    """
    match = _CONTEXT.fullmatch(response.request_context or "")
    if match is None:
        raise UnreadableWindowError(
            f"raw response {response.raw_response_id} ({response.endpoint}) does not record "
            f"the window it answered, so the pages of that window cannot be assembled and "
            f"its removals cannot be reproduced"
        )
    payload = parse_response_body(response.body, what="an investment-transaction page")
    entries = payload.get("investment_transactions") if isinstance(payload, dict) else None
    total = payload.get("total_investment_transactions") if isinstance(payload, dict) else None
    return ArchivedWindowPage(
        window_start=calendar_date(date.fromisoformat(match["start"])),
        window_end=calendar_date(date.fromisoformat(match["end"])),
        offset=int(match["offset"]),
        rows=len(entries) if isinstance(entries, list) else 0,
        # `bool` is an `int` subclass and JSON `true` would otherwise read as a
        # total of 1, which is a number a window could be exhausted against.
        stated_total=total if isinstance(total, int) and not isinstance(total, bool) else None,
    )


@dataclass(slots=True)
class _WindowInProgress:
    """The pages of one connection's current window, and whether it has a start.

    `opened` is tracked rather than inferred from the pages in hand, because the
    two states a replay has to tell apart look identical from a count: a window
    whose first page is the one at offset 0, and a window whose earlier pages
    the archive no longer holds. Only the first may be reconciled.
    """

    opened: bool = False
    page_response_ids: list[int] = field(default_factory=list)


@dataclass(slots=True)
class InvestmentWindowReplay:
    """Re-runs each complete window's reconciliation as the replay reaches its last page.

    Stateful across the responses it is shown, and the state is small: the
    archived pages of the window each connection currently has in progress. A
    page fetched at offset 0 starts a window -- the protocol's own boundary,
    since there is no cursor to mark one -- and the page that carries the window
    past its stated total closes it.

    🔴 **A window whose opening page the replay never saw concludes nothing.**
    Reconciling on a partial set of pages would soft-delete every row that
    happened to sit on the pages the archive no longer holds, which is the same
    bulk deletion `store.investments` refuses a short run for. The rebuild's
    content digest then reports the removal it could not reproduce, which is a
    refusal an operator can act on rather than a deletion nobody sees.

    🔴 **The live sync runs this same pass**, inside each page's derivation
    transaction (`store.derivation.apply_response`). A window is then concluded in
    the transaction of the page that closed it, and a rebuild replaying those
    pages concludes it at the same response and the same archived instant.
    `last_conclusion` is how the sync reads back what that commit established.
    """

    _in_progress: dict[int, _WindowInProgress] = field(default_factory=dict)
    #: The last window this pass concluded: the closing page's raw response id and
    #: what the reconciliation established. One entry rather than a history, so a
    #: rebuild over years of archive holds one outcome, not one per window.
    last_conclusion: tuple[int, WindowOutcome] | None = None

    def observe(self, conn: SAConnection, response: RawResponse) -> None:
        if response.endpoint != str(INVESTMENTS_TRANSACTIONS_GET):
            return
        if response.connection_id is None:
            # The deriver refuses this response outright, for the same reason:
            # an investments reply belongs to exactly one connection, and a
            # window is that connection's.
            return
        page = read_investment_transaction_page(response)
        window = self._in_progress.setdefault(response.connection_id, _WindowInProgress())
        if page.offset == 0:
            window.opened = True
            window.page_response_ids.clear()
        window.page_response_ids.append(response.raw_response_id)
        if not window_is_exhausted(page.rows_through_this_page, page.stated_total):
            return
        if not window.opened:
            _log.warning(
                "the archive holds connection %d's investment-transaction window %s..%s from "
                "offset %d onwards but not the pages before it, so the window cannot be "
                "assembled and its removals are not reproduced",
                response.connection_id,
                page.window_start,
                page.window_end,
                page.offset,
            )
            self._in_progress[response.connection_id] = _WindowInProgress()
            return
        concluded = record_investment_transaction_window(
            conn,
            connection_id=response.connection_id,
            window_start=page.window_start,
            window_end=page.window_end,
            page_response_ids=list(window.page_response_ids),
            rows_seen=page.rows_through_this_page,
            stated_total=page.stated_total,
            # 🔴 The archived instant, which is what the sync stamped its
            # removals with. The clock would make every replay produce a
            # different `removed_at`, and AC-5.2 false for every soft delete.
            at=response.received_at,
        )
        self.last_conclusion = (response.raw_response_id, concluded)
        self._in_progress[response.connection_id] = _WindowInProgress()
