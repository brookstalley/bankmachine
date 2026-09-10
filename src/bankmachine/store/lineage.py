"""Which rows a total counts when one account carries two Items' worth of history.

Removing a connection and linking it again yields a new Item at the aggregator.
The new Item re-issues every account id AND every transaction id. The account
converges -- `source_persistent_account_id` names the same underlying account
across the break -- but the transactions cannot: the whole granted history
arrives again as rows this store has never seen, under an account it now
recognises, with nothing for a unique index to collide with. Summed naively, a
year of spending doubles, and the only signal is a count of accounts that are no
longer active.

**Every row is kept; aggregates count one lineage.** For a range covered by more
than one Item, the NEWEST Item answers, and older ones answer only outside it.
The rows an older lineage loses are retained, still queryable by explicit
request, and never deleted -- the same rule `removed_at` already follows.

🔴 **Rejected: natural-key dedupe** on `(account_id, ledger_date, amount_minor,
normalized name)`. It is what most systems do and it is wrong for this one: two
genuinely distinct real transactions with the same merchant, amount and day --
two $5 coffees, two identical transit fares -- collapse into one, and the total
goes DOWN with nothing to signal it. This product's stated asymmetry is that an
overcount gets questioned and an undercount gets believed, so a rule that can
silently delete real money is on the wrong side of it. The design here fails the
other way: a boundary computed wrongly leaves a VISIBLE duplicate or a DISCLOSED
exclusion, and an operator can see and report both.

🔴 **The overlap is disclosed, and that is not decoration.** A silent correct
total and a silent wrong total look identical to whoever reads them; only the
disclosure tells them apart. `superseded_spans` returns what the disclosure
names -- the account, and the range each older lineage no longer answers for.

🔴 **Measured on `ledger_date`, never `posted_date`.** The posting date moves
when a hold settles, so a row measured on it could cross a lineage boundary
between two syncs and change which total counts it, with nothing to say so.

🔴 **A row with no lineage is never excluded.** A null means "predates migration
007 and has not been rebuilt" or "came from an operator file, which no Item
produced" -- never "belongs to the current Item". Excluding one would delete
money on the strength of a column nothing ever filled.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import ColumnElement, and_, false, func, not_, or_, select, true
from sqlalchemy import Connection as SAConnection

from bankmachine.store.schema import connections, transactions
from bankmachine.store.types import CalendarDate, calendar_date


@dataclass(frozen=True, slots=True)
class SupersededSpan:
    """One older lineage's rows on one account, over the range a newer one covers.

    Both ends are inclusive calendar dates on `ledger_date`. A span is what the
    read path excludes from a total and what the `rule-applied` disclosure names,
    so it carries the account rather than only the lineage: an operator reading
    "some rows were excluded" learns nothing they can check.
    """

    account_id: int
    lineage_id: int
    start: CalendarDate
    end: CalendarDate


@dataclass(frozen=True, slots=True)
class _Coverage:
    """One lineage's own reach on one account, and where it sits in the order."""

    account_id: int
    lineage_id: int
    start: CalendarDate
    end: CalendarDate
    #: Ordering key: the connection's enrollment, then its local id. Enrollment
    #: is the fact that makes one Item newer than another -- a re-link enrols
    #: after the connection it replaces -- and the id breaks a tie so the order
    #: is total and a replay cannot reshuffle it.
    enrolled: tuple[datetime, int]


def _day(value: object) -> CalendarDate:
    """An aggregate's calendar date, whichever form the driver handed back.

    `func.min` over a TypeDecorator column is not guaranteed to carry that
    decorator's result processing, so the text form is converted here rather
    than trusted to have been. Wrong would be silent: a string compares against
    a date as unequal rather than raising, and every span would come back empty.
    """
    if isinstance(value, datetime):
        return calendar_date(value.date())
    if isinstance(value, date):
        return calendar_date(value)
    return calendar_date(date.fromisoformat(str(value)))


def _coverage(conn: SAConnection, account_ids: Iterable[int] | None) -> list[_Coverage]:
    """Each (account, lineage) pair's covered range, newest lineage first.

    Removed rows are left out: a soft-deleted transaction is not money the
    account holder committed, so a lineage whose rows were all retired covers
    nothing and must not supersede the lineage that replaced it.
    """
    statement = (
        select(
            transactions.c.account_id,
            transactions.c.lineage_id,
            func.min(transactions.c.ledger_date),
            func.max(transactions.c.ledger_date),
            connections.c.enrolled_at,
        )
        .join(connections, connections.c.connection_id == transactions.c.lineage_id)
        .where(
            transactions.c.lineage_id.is_not(None),
            transactions.c.ledger_date.is_not(None),
            transactions.c.removed_at.is_(None),
        )
        .group_by(transactions.c.account_id, transactions.c.lineage_id)
    )
    if account_ids is not None:
        wanted = list(account_ids)
        if not wanted:
            return []
        statement = statement.where(transactions.c.account_id.in_(wanted))
    found = [
        _Coverage(
            account_id=int(row[0]),
            lineage_id=int(row[1]),
            start=_day(row[2]),
            end=_day(row[3]),
            enrolled=(row[4], int(row[1])),
        )
        for row in conn.execute(statement).all()
    ]
    found.sort(key=lambda item: (item.account_id, item.enrolled), reverse=True)
    return found


def superseded_spans(
    conn: SAConnection, *, account_ids: Iterable[int] | None = None
) -> tuple[SupersededSpan, ...]:
    """Where an older lineage no longer answers, because a newer one covers it.

    A property of the store rather than of any one question: the same rows are
    superseded whatever window is asked about, so a caller may compute this once
    and both filter and disclose from it.

    Restricted to `account_ids` where the caller has them, because an account
    nobody asked about contributes an exclusion nobody can act on -- and a
    `rule-applied` naming it would be a warning about somebody else's data.
    """
    spans: list[SupersededSpan] = []
    newer: dict[int, list[tuple[CalendarDate, CalendarDate]]] = {}
    for item in _coverage(conn, account_ids):
        covered = newer.setdefault(item.account_id, [])
        overlaps = [
            (max(item.start, start), min(item.end, end))
            for start, end in covered
            if max(item.start, start) <= min(item.end, end)
        ]
        spans.extend(
            SupersededSpan(
                account_id=item.account_id, lineage_id=item.lineage_id, start=start, end=end
            )
            for start, end in _merged(overlaps)
        )
        covered.append((item.start, item.end))
    spans.sort(key=lambda span: (span.account_id, span.lineage_id, span.start))
    return tuple(spans)


def _merged(
    ranges: Sequence[tuple[CalendarDate, CalendarDate]],
) -> list[tuple[CalendarDate, CalendarDate]]:
    """Overlapping ranges joined into the fewest that cover the same days.

    Three lineages on one account give an older one two overlaps that may
    themselves overlap, and naming both in a disclosure would tell an operator
    the same days twice while implying they were excluded twice.
    """
    joined: list[tuple[CalendarDate, CalendarDate]] = []
    for start, end in sorted(ranges):
        if joined and start <= joined[-1][1]:
            joined[-1] = (joined[-1][0], max(joined[-1][1], end))
        else:
            joined.append((start, end))
    return joined


def counts_once(spans: Sequence[SupersededSpan]) -> ColumnElement[bool]:
    """A predicate over `transactions` keeping one lineage's row per covered day.

    True for every row no span names, which is every row when nothing is
    superseded -- so a caller adds this unconditionally rather than branching,
    and a store that has never seen a re-link is filtered by a constant.

    🔴 Each span is refused as a conjunction of all three of account, lineage and
    range. Dropping the account from it would exclude an unrelated account's rows
    that happen to share a lineage, and dropping the lineage would exclude the
    NEWER rows as well -- which is the undercount this whole design exists to
    refuse, arriving inside the fix.
    """
    if not spans:
        return true()
    return and_(
        *(
            not_(
                and_(
                    transactions.c.account_id == span.account_id,
                    transactions.c.lineage_id == span.lineage_id,
                    transactions.c.ledger_date.between(span.start, span.end),
                )
            )
            for span in spans
        )
    )


def only_superseded(spans: Sequence[SupersededSpan]) -> ColumnElement[bool]:
    """The mirror of `counts_once`: exactly the rows an aggregate left out.

    What answers "show me what was excluded". The rows are still in the store and
    still reachable -- that is the whole difference between this design and a
    dedupe -- and a caller with no way to ask for them could not tell.
    """
    if not spans:
        return false()
    return or_(
        *(
            and_(
                transactions.c.account_id == span.account_id,
                transactions.c.lineage_id == span.lineage_id,
                transactions.c.ledger_date.between(span.start, span.end),
            )
            for span in spans
        )
    )
