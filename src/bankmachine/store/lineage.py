"""Which rows a total counts when one real account carries two Items' worth of history.

Linking an institution again yields a new Item at the aggregator. The new Item
re-issues every account id AND every transaction id, and the transactions cannot
converge: the whole granted history arrives again as rows this store has never
seen, with nothing for a unique index to collide with. Summed naively, a year of
spending doubles, and the only signal is a count of accounts that are no longer
active.

**Where the second generation LANDS depends on two independent things**, and
neither is this store's choice: whether the ACCOUNT converged (does the
aggregator publish `source_persistent_account_id` here?) and whether the
CONNECTION was replaced (did the re-link retire its connection row, or update it
in place?). Four combinations, and they do not all leave the same evidence:

* **Account converged, connection replaced** -- one `account_id`, two
  `lineage_id`s. Separated by enrollment.
* **Account split, connection replaced** -- two `account_id`s, two `lineage_id`s.
* **Account split, connection updated in place** -- two `account_id`s, ONE
  `lineage_id`, because `lineage_id` IS the `connection_id` and that row
  survived. This is the common production shape, since the persistent id is null
  outside three institutions.
* 🔴 **Account converged, connection updated in place -- one `account_id` and one
  `lineage_id`, and this module CANNOT SEE IT.** Both generations fall in a
  single `_coverage` group, there is no second span to overlap, and the total
  doubles with no caveat. Telling the two generations apart there needs
  transaction-level identity, which is the dedupe rejected below. It is reachable
  at the three institutions that publish a persistent id, via `enroll --relink`.

So neither `account_id` nor `lineage_id` alone separates a generation, and a rule
keyed on either one in isolation silently answers "nothing is superseded" for the
cases it cannot see. The comparison is partitioned on **account identity**
instead, and ordered by **generation**; both are defined below. That covers the
first three shapes and not the fourth.

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
names -- the account, and the range each older generation no longer answers for.

🔴 **Measured on `ledger_date`, never `posted_date`.** The posting date moves
when a hold settles, so a row measured on it could cross a lineage boundary
between two syncs and change which total counts it, with nothing to say so.

🔴 **A row with no lineage is never excluded.** A null means "predates migration
007 and has not been rebuilt" or "came from an operator file, which no Item
produced" -- never "belongs to the current Item". Excluding one would delete
money on the strength of a column nothing ever filled.

🔴 **The identity tuple stands in for a relationship the store does not record,
and is meant to be replaced.** Matching on four institution-supplied strings is
a rule keyed on NAMES where it means "these two rows are the same real account".
That substitution is taken knowingly, because no column records the
relationship: `source_persistent_account_id` is the field that would, and it is
null wherever this problem actually bites. Two things keep it honest -- the
exclusion is DISCLOSED and every excluded row stays reachable, so a wrong
grouping is visible and correctable rather than baked into stored data; and
grouping alone never excludes anything, because supersession additionally
requires the generation evidence below. When the store records account identity
across a re-link, that column should take this tuple's place.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import ColumnElement, and_, false, func, not_, or_, select, true
from sqlalchemy import Connection as SAConnection

from bankmachine.store.schema import accounts, connections, transactions
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


#: What two spans must share before either can supersede the other. Opaque by
#: design: `_group` is the only thing that builds one and nothing reads inside it.
#:
#: 🔴 **`str | int`, never `object`, and the narrowness is the point.** These
#: tuples are SORTED, so a `None` reaching one does not mis-group a total -- it
#: raises `TypeError` out of every read that touches `superseded_spans`, for
#: questions that never mentioned the account. Several columns an identity is
#: naturally built from (`mask`, `account_subtype`) are nullable, so the next one
#: added here has to be a type error rather than a production traceback.
_Group = tuple[str | int, ...]

#: Where a span sits in its partition's order, oldest first: the enrollment of the
#: Item that produced the rows, then when this store first wrote the account row,
#: then the account id.
#:
#: 🔴 **Ordering only. It never decides that something is superseded** -- that is
#: `_Coverage.still_reported`'s job, for a reason worth stating: a total order over
#: two live accounts would happily declare one of them older, and the rule would
#: then delete money that was really spent.
#:
#: 🔴 The account's `created_at` is an INSTANT on purpose, where the obvious choice
#: -- when the institution last listed it -- is a calendar date. Two generations
#: born on the same day tie on any date, and a re-link's whole point is that it
#: happens right after the sync it replaces.
_Generation = tuple[datetime, datetime, int]


@dataclass(frozen=True, slots=True)
class _Coverage:
    """One lineage's own reach on one account, and where it sits in the order."""

    account_id: int
    lineage_id: int
    start: CalendarDate
    end: CalendarDate
    #: What this row is a generation OF: the partition inside which two spans can
    #: supersede one another. Never compared across partitions.
    #:
    #: 🔴 Scoped to the institution, never globally and never to the connection.
    #: Global would let a four-digit mask collision between two banks merge two
    #: real accounts, which is the worst outcome available here. Connection-scoped
    #: would miss the re-link that retires its connection and enrols a
    #: replacement, where the second generation lands under a NEW connection_id --
    #: institution scope is what covers that path and the converging one together.
    #:
    #: 🔴 An account whose mask is null partitions with ITSELF ALONE. A null means
    #: the aggregator did not state the mask, never that two accounts agree on it,
    #: and grouping on an absence is how a rule that excludes money gets it wrong
    #: in the direction that gets believed.
    group: _Group
    #: Ordering key WITHIN the partition, oldest first. Total, so the order does
    #: not depend on the order rows came back and a replay cannot reshuffle it.
    #:
    #: 🔴 Both leading halves are load-bearing. Enrollment separates two lineages
    #: on ONE account row -- the converged re-link, where a single account carries
    #: two Items. The account's creation instant separates two account ROWS under
    #: one lineage -- the re-link that converged its connection in place, where
    #: enrollment is identical because the connection row was updated rather than
    #: replaced. A key carrying only one of them is blind to the other's case.
    generation: _Generation
    #: Whether the institution still lists this account on the roster this store
    #: last read for it -- the evidence that a generation was REPLACED rather than
    #: merely being older than something.
    #:
    #: 🔴 This, not the order, is what licenses an exclusion across two account
    #: rows. Ordering alone cannot tell a superseded generation from a second live
    #: account that merely resembles the first; only the institution having stopped
    #: listing one of them can.
    #:
    #: 🔴 Derived the same way `query._account_lifecycle` derives
    #: `no_longer_reported`, and held to it by test rather than by memory. Two
    #: derivations of one fact that disagree are worse than either alone: the
    #: aggregate would exclude an account the verification surface calls active.
    #:
    #: 🔴 **Day granularity is inherited, not chosen.** `last_seen_date` is a
    #: calendar date (AC-12.4), so a re-link on the same day as the sync it
    #: replaces leaves the old generation looking listed, and its rows go on being
    #: counted. That is the pre-existing resolution of the observation this reads,
    #: not a further loss: `list_accounts` calls such an account `active` too. The
    #: failure stays in the visible direction -- a duplicate an operator can see.
    still_reported: bool


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


def _coverage(conn: SAConnection) -> list[_Coverage]:
    """Each (account, lineage) pair's covered range, newest generation first.

    Removed rows are left out: a soft-deleted transaction is not money the
    account holder committed, so a lineage whose rows were all retired covers
    nothing and must not supersede the lineage that replaced it.
    """
    # 🔴 Two connections are in play and they are not the same row. `lineage` is
    # the connection that PRODUCED these transactions; `holder` is the one the
    # ACCOUNT currently hangs off, which is what says whether the institution
    # still lists it. A re-link that retires its connection leaves the older
    # generation pointing at the retired row for both -- and a re-link that
    # converges in place leaves every generation sharing one row for both. Only
    # asking the two questions separately answers both shapes.
    #
    # The account's connection is OUTER-joined: the FR-7 import path leaves it
    # null, and such an account is on no roster to be dropped from.
    lineage = connections.alias("lineage_connection")
    holder = connections.alias("holder_connection")
    statement = (
        select(
            transactions.c.account_id,
            transactions.c.lineage_id,
            func.min(transactions.c.ledger_date).label("start"),
            func.max(transactions.c.ledger_date).label("end"),
            lineage.c.enrolled_at.label("enrolled_at"),
            accounts.c.institution_id.label("institution_id"),
            accounts.c.mask.label("mask"),
            accounts.c.name.label("name"),
            accounts.c.account_type.label("account_type"),
            accounts.c.account_subtype.label("account_subtype"),
            accounts.c.created_at.label("created_at"),
            accounts.c.last_seen_date.label("last_seen_date"),
            holder.c.roster_observed_date.label("roster_observed_date"),
            holder.c.retired_at.label("retired_at"),
        )
        .select_from(
            transactions.join(lineage, lineage.c.connection_id == transactions.c.lineage_id)
            .join(accounts, accounts.c.account_id == transactions.c.account_id)
            .outerjoin(holder, holder.c.connection_id == accounts.c.connection_id)
        )
        .where(
            transactions.c.lineage_id.is_not(None),
            transactions.c.ledger_date.is_not(None),
            transactions.c.removed_at.is_(None),
        )
        # The account and connection columns are bare here rather than repeated in
        # the grouping: each is functionally dependent on a column that IS grouped
        # -- the account columns on `account_id`, `enrolled_at` on `lineage_id` --
        # so every row in a group carries the same value and there is nothing for
        # an arbitrary pick to vary.
        .group_by(transactions.c.account_id, transactions.c.lineage_id)
    )
    # 🔴 Read BY NAME. Every column here is labelled and every one is reached
    # through that label, because this select carries fourteen of them and a
    # positional read silently re-points every field after any column inserted
    # above it -- landing a mask in the name's place and an enrollment instant in
    # the creation one, with no error anywhere and a total that is merely wrong.
    found = [
        _Coverage(
            account_id=int(row.account_id),
            lineage_id=int(row.lineage_id),
            start=_day(row.start),
            end=_day(row.end),
            group=_group(
                account_id=int(row.account_id),
                institution_id=int(row.institution_id),
                mask=row.mask,
                name=row.name,
                account_type=row.account_type,
                account_subtype=row.account_subtype,
            ),
            generation=(row.enrolled_at, row.created_at, int(row.account_id)),
            still_reported=_still_reported(
                last_seen=row.last_seen_date,
                roster_observed=row.roster_observed_date,
                retired_at=row.retired_at,
            ),
        )
        for row in conn.execute(statement).all()
    ]
    found.sort(key=lambda item: (item.group, item.generation), reverse=True)
    return found


def _still_reported(*, last_seen: object, roster_observed: object, retired_at: object) -> bool:
    """Whether the institution still lists this account on the roster last read.

    🔴 Mirrors `query._account_lifecycle`'s derivation of `no_longer_reported`
    clause for clause, and `tests/store/test_lineage_agrees_with_lifecycle.py`
    holds the two to each other. The rule that excludes rows from a total and the
    field that tells an operator the account is stale are the same claim; if they
    can disagree, an aggregate drops an account the verification surface is
    calling active and nothing in either answer explains it.

    A retired connection is not still reporting anything. Its last roster read is
    frozen at whatever it was, so comparing the two dates would call every account
    on it current -- and the re-link that retires its connection is exactly the
    shape that most needs the opposite answer.
    """
    if retired_at is not None:
        return False
    if roster_observed is None:
        # This connection's roster has never been observed, so nothing on it can
        # be called absent -- the same reading `_account_lifecycle` takes, and the
        # reason a null is not filled in with a guess.
        return True
    if last_seen is None:
        return False
    return _day(last_seen) >= _day(roster_observed)


def _group(
    *,
    account_id: int,
    institution_id: int,
    mask: str | None,
    name: str,
    account_type: str,
    account_subtype: str | None,
) -> _Group:
    """The partition this account's spans can be superseded within.

    Two rows share a partition when the institution describes them the same way
    -- which is what a re-issued roster leaves behind, because the aggregator
    mints new ids but re-reports the same mask, name, type and subtype.

    🔴 **Every component must be STATED.** `mask` and `account_subtype` are both
    nullable, and a null means the aggregator did not say -- never that two
    accounts agree. An account missing either gets a partition of one, named by
    itself, so it can still be superseded by ANOTHER LINEAGE ON ITSELF (the
    converged re-link) while never being matched against a DIFFERENT account row
    on the strength of a field nobody filled in.

    🔴 That rule is also what keeps this key sortable. `superseded_spans` orders
    by the partition, and a tuple holding `None` in a position another tuple
    holds a string in raises `TypeError` on comparison -- so admitting one null
    would not mis-group a total, it would take every query over the store down.
    """
    if mask is None or account_subtype is None:
        return ("account", account_id)
    return ("identity", institution_id, mask, name, account_type, account_subtype)


def superseded_spans(conn: SAConnection) -> tuple[SupersededSpan, ...]:
    """Where an older generation no longer answers, because a newer one covers it.

    A property of the store rather than of any one question: the same rows are
    superseded whatever window is asked about, so a caller may compute this once
    and both filter and disclose from it.

    🔴 **Always over the WHOLE store, and there is deliberately no way to ask for
    less.** Narrowing to a set of accounts would narrow what can be COMPARED, not
    only what is reported -- and the two generations of a re-issued roster are
    different accounts, so passing either one alone leaves it looking
    unsuperseded. An aggregate built on that would present a partial exclusion as
    a total. The parameter that allowed it is gone rather than documented,
    because a foot-gun a caller has to read a docstring to avoid is the more
    expensive of the two.
    """
    spans: list[SupersededSpan] = []
    newer: dict[_Group, list[_Coverage]] = {}
    for item in _coverage(conn):
        covered = newer.setdefault(item.group, [])
        overlaps = [
            (max(item.start, span.start), min(item.end, span.end))
            for span in covered
            if _supersedes(span, item) and max(item.start, span.start) <= min(item.end, span.end)
        ]
        spans.extend(
            SupersededSpan(
                account_id=item.account_id, lineage_id=item.lineage_id, start=start, end=end
            )
            for start, end in _merged(overlaps)
        )
        covered.append(item)
    spans.sort(key=lambda span: (span.account_id, span.lineage_id, span.start))
    return tuple(spans)


def _supersedes(newer: _Coverage, older: _Coverage) -> bool:
    """Whether `newer` is entitled to answer for days `older` also covers.

    Being newer is necessary and, across two account rows, NOT sufficient.

    🔴 **On one account row, order is the whole test.** Two lineages on a single
    account are two Items' copies of one account's history; the later Item is the
    one that re-fetched, and there is no second real account for the rule to
    confuse it with.

    🔴 **Across two account rows, the older one must have STOPPED BEING LISTED.**
    Two rows that merely resemble each other are the ordinary case -- a household
    can hold a checking account and an overdraft line that share a four-digit
    mask, and at the one institution where this was measured it does. Both are on
    every roster. Excluding either would delete money that was really spent,
    which is the undercount this module exists to refuse; so resemblance alone
    licenses nothing, and the institution having dropped one of them is the
    evidence that makes it a replaced generation rather than a neighbour.
    """
    if newer.generation <= older.generation:
        return False
    return newer.account_id == older.account_id or not older.still_reported


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
