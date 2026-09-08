"""The read surface, and the envelope every answer carries.

🔴 **This module exists because of one norm.** `api-contract.md` § Direction:
*every response carries a freshness stamp, and incompleteness rides the success
path as a warning field rather than as an exception.* The dangerous case is not
an error — it is a **successful** answer computed over incomplete data, because
nothing throws and the numbers simply stop being true. The consumer is an
analyst agent that **cannot see a caveat which is not in the payload**:
documentation, log files and a health tool it did not think to call are all
invisible at the moment of answering.

So every function here returns an `Answer`, and an `Answer` cannot be built
without its warnings — they are computed from the datastore in the same call
that computes the rows.

🔴 **The environment is part of the envelope, not just the launch flag.** A
server pointed at sandbox data and one pointed at real money look identical in
their answers unless the answer says which it is. A flag selects; the envelope
confesses.

Read-role only: every handle here opens `mode=ro` at the file, so no query can
write whatever SQL reaches it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Connection as SAConnection

from bankmachine.build_id import build_identity
from bankmachine.config import Config
from bankmachine.store.connection import inspect
from bankmachine.store.engine import reader_connection
from bankmachine.store.schema import (
    TRANSACTIONS_DOMAIN,
    accounts,
    balances_daily,
    connections,
    institutions,
    sync_state,
    transactions,
)
from bankmachine.store.types import UtcInstant, calendar_date, now_utc

#: How long since a connection's last successful sync before its data is called
#: stale. A day and a half: the scheduled job runs nightly, so one missed run is
#: not yet a story and two consecutive ones are.
STALE_AFTER = timedelta(hours=36)

#: The most rows any single query returns. 🔴 **A contract term, not a tuning
#: knob** -- `api-contract.md` fixes it at ~500 under AC-9.1,
#: `nonfunctional-requirements.md` makes it what keeps the sub-second target
#: reachable, and `security-model.md` names it as the mitigation for
#: unrestricted resource consumption (OWASP API4). Raising it is an amendment to
#: all three, not an edit here.
MAX_ROWS = 500

#: Warnings about the standing state of the pipeline. These ride EVERY response
#: equally, because they describe the connection rather than the question: an
#: acceptance round measured the `gapped` notice arriving character-for-character
#: identical on a window wholly inside coverage, a window wholly outside it, a
#: future window, and a query for an account that does not exist. True, and
#: useless for telling a caller whether THIS answer is the degraded one.
CONNECTION_SCOPED_KINDS: tuple[str, ...] = (
    "stale",
    "degraded",
    "gapped",
    "partial",
    "rule-applied",
)

#: 🔴 Warnings about THIS request, which fire only when this request actually
#: crosses the boundary they name -- so their presence is information and **so is
#: their absence**. That is the whole reason they exist, and it is why the
#: distinction is a structure here rather than a comment: a test asking "did this
#: request warn about itself" has to be able to name the set, and deriving it
#: from a shared spelling (every kind starting `window_`) would silently exempt
#: the first request-scoped kind that is not about a window -- which is exactly
#: what `rows_truncated` is.
#:
#: Additive by `api-contract.md`'s own evolution rule: new warning codes need no
#: version bump, and consumers are required to tolerate a kind they do not
#: recognize. AC-9.3's list is a minimum, so it needs no amendment.
REQUEST_SCOPED_KINDS: tuple[str, ...] = (
    "window_starts_before_coverage",
    # Named for the COVERED END rather than for today, because that is the bound
    # it actually reports: the covered end is today, or the last transaction when
    # that is later. Mirrors `window_starts_before_coverage`, so both kinds name
    # one boundary concept from their two ends.
    "window_extends_past_coverage",
    "rows_truncated",
    # The row query and the count are separate snapshots on an autocommit
    # reader, so a write landing between them is a data condition rather than a
    # defect. It is reported instead of smoothed away: a consumer comparing two
    # calls seconds apart deserves to know a write landed between them.
    "counted_during_change",
)

#: The warning vocabulary the API contract fixes. Named here as a tuple rather
#: than left to string literals at each site, because a warning nobody spells the
#: same way twice is a warning a consumer cannot branch on. Composed from the two
#: scopes above rather than re-listed, so a kind cannot join the vocabulary
#: without declaring which of the two it is.
WARNING_KINDS: tuple[str, ...] = CONNECTION_SCOPED_KINDS + REQUEST_SCOPED_KINDS


@dataclass(frozen=True, slots=True)
class Caveat:
    """One reason an answer is less complete than it looks.

    Named `Caveat` rather than `Warning` because the builtin of that name is a
    different thing entirely, and a module that shadows it makes every later
    reader check which one they are looking at.

    Carries the connection it is about where there is one, because an operator
    with ten institutions needs to know which of them went quiet — a bare
    "some data is stale" is a warning they cannot act on.
    """

    kind: str
    detail: str
    connection_id: int | None = None
    institution: str | None = None

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"kind": self.kind, "detail": self.detail}
        if self.connection_id is not None:
            wire["connection_id"] = self.connection_id
        if self.institution is not None:
            wire["institution"] = self.institution
        return wire


@dataclass(frozen=True, slots=True)
class Window:
    """The window a caller asked for, beside the one the data could answer over.

    🔴 `caveats` has no default, for exactly the reason `Answer.warnings` has
    none: a window that has not been reconciled against coverage cannot be
    constructed, so the reconciliation is structural rather than remembered.

    The clamp this records is **reportorial, not selective.** Nothing here
    narrows a SQL predicate -- clamping `since` up to the first covered date can
    only exclude rows that do not exist, and clamping `until` down to today can
    only exclude rows that have not happened. So an effective window never
    changes a returned figure; it says what the figure was always computed over.

    `operational-spec.md` refuses an out-of-range ENROLLMENT window rather than
    clamping it, "because a clamp would enroll at a window the operator never
    chose and never told them about". That reason is about not being told, and a
    query is the case where it points the other way: refusing "show me 2024"
    against a store that starts in September 2024 refuses an ordinary question,
    and enrollment's cost -- history that cannot be bought back -- has no
    analogue in a read. Ruled 2026-09-08: clamp, and say so. This type is the
    saying so.
    """

    requested_since: date | None
    requested_until: date | None
    effective_since: date | None
    effective_until: date | None
    caveats: list[Caveat]

    @property
    def covers_nothing(self) -> bool:
        """True when the request and the covered span do not overlap at all."""
        return self.effective_since is None or self.effective_until is None

    def to_wire(self) -> dict[str, Any]:
        return {
            "requested": {
                "since": _iso_or_none(self.requested_since),
                "until": _iso_or_none(self.requested_until),
            },
            "effective": {
                "since": _iso_or_none(self.effective_since),
                "until": _iso_or_none(self.effective_until),
            },
        }


def _iso_or_none(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _parse_coverage_date(value: Any) -> date | None:
    """A coverage bound off the wire dict, back to a date.

    `_coverage` renders its bounds with `str()` for the payload, so the resolver
    reads them back rather than re-querying. An unparseable value is treated as
    absent: a window that cannot be reconciled must not raise on the success
    path, because incompleteness rides the answer here and never the error
    channel (`api-contract.md` § Direction).
    """
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


class InvertedWindowError(ValueError):
    """A window whose end precedes its start.

    🔴 Its own type, not a bare `ValueError`, so the MCP boundary can render it
    as a refusal the caller can act on. Reached only by a windowed tool that did
    not narrow its arguments first -- `mcp._window` refuses the transposed pair
    ahead of the query layer today, and the caller's own words are still in hand
    there, so that remains the better place to catch it. But a future windowed
    tool that forgot would otherwise land in the boundary's broad catch and get
    back "internal error", which is a false statement about a caller mistake.
    Rides the same path `UnknownAccountError` does, for the same reason.
    """


def resolve_window(
    *,
    since: date | None,
    until: date | None,
    coverage: dict[str, Any],
    as_of: UtcInstant,
) -> Window:
    """Reconcile a requested window against what the store actually holds.

    The effective window is the requested one intersected with
    `[earliest transaction, today]`. An unbounded request reports that span,
    which is the case where a caller most needs the answer: "all of it" means
    nothing until you know what "all" covers.
    """
    # 🔴 A transposed window is a CALLER error, not a data condition, and it is
    # the one empty window this function cannot explain: there is no boundary
    # crossed and no coverage fact to report -- "nothing" is simply what an
    # inverted window selects. The MCP boundary already refuses it by name
    # ("until X is before since Y ... Did the two get swapped?"), which is the
    # right treatment and belongs there, where the caller's own words are still
    # in hand. Refusing it here too is what makes this function's own invariant
    # total: every window it RETURNS that covers nothing carries a caveat
    # saying why. Found by the hypothesis property below, which had no idea the
    # boundary refuses this shape -- exactly why it was worth writing.
    if since is not None and until is not None and until < since:
        raise InvertedWindowError(
            f"until ({until.isoformat()}) is before since ({since.isoformat()}); "
            f"an inverted window selects nothing and there is no coverage fact that "
            f"explains it. Refuse it at the boundary that has the caller's own words."
        )

    # 🔴 The one place a UTC instant becomes a calendar date. `data-model.md`
    # keeps the two apart because an instant carries a zone and a transaction
    # date does not, so the conversion is a decision made at a named site rather
    # than an implicit coercion. Today is UTC today, matching the `as_of` the
    # same answer is stamped with -- a caller comparing the two reads one clock.
    today = calendar_date(as_of.date())
    earliest = _parse_coverage_date(coverage.get("earliest_transaction"))
    latest = _parse_coverage_date(coverage.get("latest_transaction"))

    # 🔴 The covered span ends at TODAY, or at the last transaction when that is
    # later. `today` alone is wrong the moment a row is dated ahead of it, and
    # nothing stops one: an authorization can post forward, and an institution a
    # day ahead in local time posts a date this UTC clock has not reached. With
    # `today` as the bound, such a row is RETURNED (the predicate uses the
    # caller's `until`) while `effective_until` says the answer stopped at
    # today -- a row outside the window the answer claims, which is this work
    # cycle's own defect wearing the fix's clothes. Taking the later of the two
    # makes "every returned row lies inside `effective_window`" hold against the
    # coverage this resolver was handed: a returned row is at or before `until`
    # AND at or before `latest`, so it is at or before the minimum of `until` and
    # this bound.
    #
    # 🔴 **Against that coverage, not against the rows** -- and the difference is
    # not pedantry. `coverage` is read by a statement of its own, after the rows,
    # on a handle that holds no read snapshot (`store/connection.py`: "every
    # statement is its own snapshot"). So a soft delete of the store's OLDEST row
    # landing between the two reads moves `earliest` forward and can push
    # `effective_since` past a row already in hand. Rare -- it needs the boundary
    # row itself removed inside a sub-millisecond gap, which ordinary sync
    # traffic does not do, since removals are recent pending rows rather than the
    # oldest row of a two-year store -- and the cost is a reported bound off by a
    # little, never a wrong figure or a wrong row. Stated at the strength the
    # mechanism actually holds because the stronger claim was written here first
    # and was wrong; a single read snapshot per answer is what would earn it
    # (#27).
    #
    # The sandbox cannot express it -- its last transaction is deliberately
    # earlier than `as_of` -- so this is reasoned from the predicate, not
    # measured. Found by review.
    covered_end = today if latest is None or latest < today else latest

    caveats: list[Caveat] = []
    if earliest is None:
        # Nothing is covered, so there is no span to intersect with. The empty
        # store already announces itself through `coverage`, and inventing a
        # window caveat here would say the same thing in a second voice.
        return Window(
            requested_since=since,
            requested_until=until,
            effective_since=None,
            effective_until=None,
            caveats=caveats,
        )

    effective_since = earliest if since is None else max(since, earliest)
    effective_until = covered_end if until is None else min(until, covered_end)
    overlaps = effective_since <= effective_until

    # 🔴 Both sentences quote the effective bound, so both must fall back to the
    # same phrase when there is no effective window to quote. Saying "this
    # answer covers through <date>" beside an `effective` of null is exactly the
    # defect this whole work cycle exists to remove -- a plausible sentence the
    # structured field contradicts -- and it would be read, because a consumer
    # reads the warning precisely when the numbers look wrong.
    nothing_covered = "no part of the window you asked for"

    # 🔴 EITHER bound can put the request before coverage, and keying on `since`
    # alone missed the second case: `{until: "2020-01-01"}` with no `since` --
    # "everything up to 2020" against a store beginning 2024 -- covered nothing
    # and said nothing. Found by the property test below, after two hand-written
    # instances of this class had already slipped through a matrix written from
    # the same assumption as the code. The bound named in the sentence is
    # whichever one is the evidence.
    before_coverage = (
        ("from", since)
        if since is not None and since < earliest
        else ("until", until)
        if until is not None and until < earliest
        else None
    )
    if before_coverage is not None:
        preposition, bound = before_coverage
        covered = f"{effective_since.isoformat()} onward" if overlaps else nothing_covered
        caveats.append(
            Caveat(
                kind="window_starts_before_coverage",
                detail=(
                    f"you asked {preposition} {bound.isoformat()}, but coverage begins "
                    f"{earliest.isoformat()}. Anything before that date is absent rather "
                    f"than zero, so this answer covers {covered}"
                ),
            )
        )
    # 🔴 Either bound reaching past today counts, not just `until`. An
    # open-ended `since` in the future -- `{since: "2027-01-01"}` with no
    # `until`, an entirely ordinary shape -- clamps the end to today and leaves
    # the window backwards, so it covers nothing. Keyed on `until` alone that
    # case produced a null effective window with NO warning at all: a silent
    # null, which is the defect this chunk exists to remove, reachable from
    # ordinary input. Found by probing the reachable inputs rather than by a
    # test, because the test matrix was written from the same assumption the
    # code was.
    beyond_coverage = (
        ("until", until)
        if until is not None and until > covered_end
        else ("from", since)
        if since is not None and since > covered_end
        else None
    )
    if beyond_coverage is not None:
        preposition, bound = beyond_coverage
        covered = f"through {effective_until.isoformat()}" if overlaps else nothing_covered
        caveats.append(
            Caveat(
                kind="window_extends_past_coverage",
                detail=(
                    f"you asked {preposition} {bound.isoformat()}, but this store holds "
                    f"nothing after {covered_end.isoformat()}, so this answer covers "
                    f"{covered}"
                ),
            )
        )

    if not overlaps:
        # The request and the covered span do not overlap. Reporting a backwards
        # window would be worse than reporting none: it reads as a real window.
        return Window(
            requested_since=since,
            requested_until=until,
            effective_since=None,
            effective_until=None,
            caveats=caveats,
        )
    return Window(
        requested_since=since,
        requested_until=until,
        effective_since=effective_since,
        effective_until=effective_until,
        caveats=caveats,
    )


@dataclass(frozen=True, slots=True)
class Truncation:
    """How many rows matched, how many came back, and therefore whether the cap bit.

    🔴 **`truncated` is a property, not a field.** The invariant is *truncated
    iff returned < matching*, and a stored third count is a third thing that can
    disagree with the other two. Derived, it cannot: there is no assignment to
    get wrong. `Window.caveats` is a stored field because reconciling a window
    needs coverage facts from outside it; every caveat here follows from the
    values already on the instance, so they are derived too.

    Why this exists at all: an answer that hit the cap and one that returned
    everything are the same payload. Measurement found the documented default of
    100 silently dropping ~16 months of one account's history, and a caller
    summing a two-year card total understating it by roughly 40% -- with nothing
    in the response saying so. `rows` alone cannot say it, because "100 rows" is
    a believable complete answer.
    """

    returned: int
    matching: int
    #: 🔴 The count came back BELOW the rows, which means the store changed
    #: between the two reads. No default: `over()` is the only route that should
    #: build one of these, and a silent `False` here would be a claim that the
    #: two numbers describe one moment when nobody checked.
    counted_during_change: bool

    @classmethod
    def over(cls, *, returned: int, counted: int) -> Truncation:
        """The only route that should build one, because `counted` can lag `returned`.

        🔴 **The row query and the count are two snapshots, not one.**
        `store/connection.py` opens the read handle in autocommit — "every
        statement is its own snapshot" — and `store/engine.py` records that
        SQLAlchemy's transaction control is inert over these handles. The
        scheduled sync writer soft-deletes transactions, and the MCP reader may
        be mid-query when it wakes. So a row counted in the first statement and
        removed before the second is entirely reachable, and it makes `counted`
        smaller than the rows already in hand.

        Treated as the data condition it is rather than as an impossibility.
        This module rides incompleteness on the success path *because a
        plausible wrong number is worse than a failure* — but that argument
        cuts against raising here, not for it: refusing to answer a perfectly
        good question because a nightly sync landed mid-query would turn a
        harmless skew into a failed tool call, which `api-contract.md` §
        Direction forbids in as many words.

        `matching` is floored at `returned`, because those rows were observed to
        match: reporting fewer would contradict the payload they sit beside, and
        `truncated` would then read false for the right reason by accident. The
        skew itself is not smoothed away — it rides out as a caveat, since a
        consumer comparing two calls seconds apart deserves to know a write
        landed between them.
        """
        return cls(
            returned=returned,
            matching=max(counted, returned),
            counted_during_change=counted < returned,
        )

    @property
    def truncated(self) -> bool:
        return self.returned < self.matching

    @property
    def caveats(self) -> list[Caveat]:
        """The warning a truncated answer carries, derived from the same two numbers.

        🔴 The block alone is not enough. `api-contract.md` § Direction fixes
        that *incompleteness rides the success path as a warning field*, and a
        truncated answer is the largest incompleteness this surface produces --
        larger than any window clamp, because the clamp removes rows that do not
        exist while this one removes rows that do. A consumer reads `warnings`
        precisely when the numbers look wrong, so the number being wrong has to
        appear there.

        The remedy names `limit` and derives its ceiling from `MAX_ROWS` rather
        than quoting a figure: `learnings.md` records a ceiling written into a
        fixture being falsified within a day by a commit that moved it.

        🔴 **A remedy the caller cannot follow is worse than none**, so which
        remedy is offered depends on whether `limit` has anything left to give.
        At the cap, offering to raise `limit` sits beside a `returned` already
        equal to that ceiling and tells the caller to raise a number to the
        value it already holds -- a plausible sentence its own payload
        contradicts, which is the defect this work cycle exists to remove rather
        than to reintroduce one field over. The cap is a contract term, so at
        that point narrowing the window is genuinely the only thing that helps,
        and the sentence says so.
        """
        caveats: list[Caveat] = []
        if self.counted_during_change:
            caveats.append(
                Caveat(
                    kind="counted_during_change",
                    detail=(
                        f"the datastore changed while this answer was being assembled — the "
                        f"row count came back below the {self.returned} rows already read, so "
                        f"a transaction was removed between the two reads. The rows are "
                        f"accurate as of the `as_of` stamp on this answer; ask again for a "
                        f"count taken after the change"
                    ),
                )
            )
        if not self.truncated:
            return caveats
        remedy = (
            f"`limit` is already at its ceiling of {MAX_ROWS} and the cap is fixed, so narrow "
            f"the window to reach the rest"
            if self.returned >= MAX_ROWS
            else f"Narrow the window, or raise `limit` (at most {MAX_ROWS})"
        )
        caveats.append(
            Caveat(
                kind="rows_truncated",
                detail=(
                    f"{self.matching} transactions match this request and the newest "
                    f"{self.returned} are returned, so {self.matching - self.returned} are "
                    f"missing from this answer. Summing or counting these rows describes "
                    f"only what came back, not the window you asked about. {remedy}"
                ),
            )
        )
        return caveats

    def to_wire(self) -> dict[str, Any]:
        return {
            "returned": self.returned,
            "matching": self.matching,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class Answer:
    """Rows, and everything a consumer needs to know about how far to trust them.

    🔴 `warnings` is not optional and has no default. A caller cannot construct
    an answer without having considered incompleteness, which is the difference
    between the norm being enforced and being remembered.
    """

    rows: list[dict[str, Any]]
    warnings: list[Caveat]
    environment: str
    as_of: UtcInstant
    #: 🔴 No default, and that is the mechanism rather than a style choice. It
    #: sits BEFORE `coverage` so it cannot acquire one by drifting after a
    #: defaulted field. Every construction site must say whether its answer was
    #: computed over a window: an unwindowed tool writes `None` on purpose, and
    #: a windowed tool that skipped the clamp would have to write `None` in
    #: plain sight rather than merely forget a call. Three more windowed tools
    #: are specified against this (`get_coverage_report`, `cashflow_summary`),
    #: and a clamp each of them has to remember is a clamp that decays.
    effective_window: Window | None
    #: 🔴 No default, for the same reason and by the same mechanism as
    #: `effective_window` above. `None` means this tool returns every row it
    #: found, and a tool that caps its rows would have to write `None` in plain
    #: sight to hide it. Aggregates write `None` truthfully:
    #: `api-contract.md` fixes them as unpaginated, bounded by the grouping.
    truncation: Truncation | None
    coverage: dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return {
            # 🔴 The environment leads. It is the difference between an answer
            # about someone's money and an answer about a fixture, and a reader
            # skimming a payload should not have to look for it.
            "environment": self.environment,
            "as_of": self.as_of.isoformat(),
            # 🔴 Provenance of the ANSWER, so it rides beside `as_of` rather than
            # living only in the handshake: a client is free not to surface
            # `serverInfo` to whatever is reading, and an agent that cannot see
            # which build replied cannot tell a stale server from a current one.
            # The server is a subprocess launched at connect time, so that
            # distinction is not hypothetical -- it cost a full acceptance round.
            "build": build_identity().to_wire(),
            "warnings": [w.to_wire() for w in self.warnings],
            "coverage": self.coverage,
            **(
                {}
                if self.effective_window is None
                else {"effective_window": self.effective_window.to_wire()}
            ),
            # Absence says "this tool returns everything it found", exactly as an
            # absent `effective_window` says "this tool takes no window". A
            # consumer branching on the key gets a true answer either way.
            **({} if self.truncation is None else {"truncation": self.truncation.to_wire()}),
            "rows": self.rows,
        }


def _is_short(granted: Any, requested: Any) -> bool:
    """Whether the aggregator granted less history than was asked for.

    A named predicate rather than an inline conjunction, because the null case is
    the one that matters: null granted is NOT a shortfall of zero, it is an
    unmeasured window, and the caller reports the two differently (AC-1.3a).
    """
    return granted is not None and requested is not None and int(granted) < int(requested)


def _pipeline_warnings(conn: SAConnection, now: UtcInstant) -> list[Caveat]:
    """Everything wrong with the data underneath any answer.

    Computed per call rather than cached: an answer's warnings describe the
    datastore at the moment it was read, and a cache would make them describe
    some earlier moment while the rows described this one.
    """
    warnings: list[Caveat] = []
    rows = conn.execute(
        select(
            connections.c.connection_id,
            institutions.c.name,
            connections.c.status,
            connections.c.last_success_at,
            connections.c.last_error_code,
            connections.c.requested_history_days,
            connections.c.granted_history_days,
        )
        .select_from(connections.join(institutions))
        .where(connections.c.retired_at.is_(None))
    ).all()

    if not rows:
        warnings.append(
            Caveat(
                kind="partial",
                detail=(
                    "no connections are enrolled, so this answer is computed over an empty "
                    "datastore rather than over an absence of activity"
                ),
            )
        )

    for row in rows:
        connection_id, name = int(row[0]), str(row[1])
        status, last_success = str(row[2]), row[3]
        requested, granted = row[5], row[6]

        if status == "degraded":
            warnings.append(
                Caveat(
                    kind="degraded",
                    detail=(
                        f"{name} last failed to sync with {row[4] or 'an unrecorded error'}; "
                        f"its data stops at the last successful run"
                    ),
                    connection_id=connection_id,
                    institution=name,
                )
            )
        if last_success is None:
            warnings.append(
                Caveat(
                    kind="partial",
                    detail=f"{name} has never completed a sync, so it contributes no data yet",
                    connection_id=connection_id,
                    institution=name,
                )
            )
        elif now - last_success > STALE_AFTER:
            hours = int((now - last_success).total_seconds() // 3600)
            warnings.append(
                Caveat(
                    kind="stale",
                    detail=f"{name} has not synced successfully for {hours} hours",
                    connection_id=connection_id,
                    institution=name,
                )
            )
        # 🔴 The shortfall AC-11.8 exists to surface. Null granted is NOT no
        # shortfall -- it is not yet known -- and the two are reported
        # differently, because reading the first as the second is the exact
        # inference AC-1.3a forbids.
        if granted is None and last_success is not None:
            warnings.append(
                Caveat(
                    kind="partial",
                    detail=(
                        f"{name}'s granted history window is not yet known; it is measured "
                        f"when the initial backfill completes"
                    ),
                    connection_id=connection_id,
                    institution=name,
                )
            )
        elif _is_short(granted, requested):
            warnings.append(
                Caveat(
                    kind="gapped",
                    detail=(
                        f"{name} granted {int(granted)} days of history against "
                        f"{int(requested)} requested, so anything older than that is absent "
                        f"rather than zero"
                    ),
                    connection_id=connection_id,
                    institution=name,
                )
            )
    return warnings


def _coverage(conn: SAConnection) -> dict[str, Any]:
    """What the datastore actually holds, so an empty answer can be told from an empty world."""
    span = conn.execute(
        select(func.min(transactions.c.posted_date), func.max(transactions.c.posted_date)).where(
            transactions.c.removed_at.is_(None)
        )
    ).one()
    return {
        "connections": conn.execute(
            select(func.count()).select_from(connections).where(connections.c.retired_at.is_(None))
        ).scalar_one(),
        "accounts": conn.execute(select(func.count()).select_from(accounts)).scalar_one(),
        "transactions": conn.execute(
            select(func.count())
            .select_from(transactions)
            .where(transactions.c.removed_at.is_(None))
        ).scalar_one(),
        "earliest_transaction": None if span[0] is None else str(span[0]),
        "latest_transaction": None if span[1] is None else str(span[1]),
    }


def _covered_rows(conn: SAConnection, *, since: date, until: date) -> int:
    """How many transactions lie inside the window this answer actually covered.

    🔴 Store-wide, never narrowed by `account_id`. Per-account coverage is its
    own issue (#19) with its own shape; reporting a half of it here would leave
    that work amending a field this chunk just shipped.

    Counted over the EFFECTIVE bounds, which is what makes it a coverage fact
    rather than a restatement of `matching`: it answers "how much data does this
    window hold", against which a caller can read the account-scoped `matching`
    beside it. The effective bounds select the same rows the requested ones would
    -- the clamp only ever removes dates the store has no rows for -- so the
    choice costs nothing and ties the number to the window the answer names.

    🔴 **Takes two `date`s, never the `Window`.** A window covering nothing has
    null bounds, and SQLAlchemy's comparison operators accept `Any` -- so passing
    the window in would let a `None` bound reach the predicate with mypy raising
    nothing, and the guard against it would rest on a comment. Demanding `date`
    here moves that guard into the signature: the caller cannot omit the null
    check, because omitting it is a type error rather than a runtime surprise.
    The empty case is answered at the call site, where the zero belongs and where
    the caveat explaining it is already being assembled.
    """
    return int(
        conn.execute(
            select(func.count())
            .select_from(transactions)
            .where(
                transactions.c.removed_at.is_(None),
                transactions.c.posted_date >= since,
                transactions.c.posted_date <= until,
            )
        ).scalar_one()
    )


def _answer(
    config: Config,
    conn: SAConnection,
    rows: list[dict[str, Any]],
    *,
    requested_window: tuple[date | None, date | None] | None,
    truncation: Truncation | None,
) -> Answer:
    """One answer, and the one place a window is reconciled against coverage.

    `requested_window` is a required keyword with no default: `None` means this
    tool is not windowed, and it has to be written. Resolution lives here rather
    than in each query function because this is where `_coverage` is already
    computed -- one read, one reconciliation, and no second mechanism for a
    later windowed tool to drift from.
    """
    now = now_utc()
    coverage = _coverage(conn)
    window = (
        None
        if requested_window is None
        else resolve_window(
            since=requested_window[0],
            until=requested_window[1],
            coverage=coverage,
            as_of=now,
        )
    )
    covered_since = None if window is None else window.effective_since
    covered_until = None if window is None else window.effective_until
    if window is not None:
        # 🔴 A SIBLING, never a narrowing of `coverage["transactions"]`, which
        # keeps its store-wide meaning. `api-contract.md` forbids repurposing a
        # field in red for the reason that bites here: a consumer still reading
        # the old key would get a wrong number rather than an error, which is
        # this work cycle's own defect introduced by its own fix.
        coverage = {
            **coverage,
            # A window covering nothing counts nothing, and that zero is not a
            # silent one: the caveat that made the bounds null is already in
            # `window.caveats` and rides the warnings below.
            "transactions_in_effective_window": (
                0
                if covered_since is None or covered_until is None
                else _covered_rows(conn, since=covered_since, until=covered_until)
            ),
        }
    return Answer(
        rows=rows,
        # 🔴 Connection-scoped warnings first, then this request's own. A
        # consumer reading top-down meets the standing state of the pipeline
        # before the thing that is specific to what they just asked.
        warnings=(
            _pipeline_warnings(conn, now)
            + ([] if window is None else window.caveats)
            + ([] if truncation is None else truncation.caveats)
        ),
        environment=config.environment,
        as_of=now,
        effective_window=window,
        truncation=truncation,
        coverage=coverage,
    )


def _unusable(
    config: Config,
    problem: str,
    *,
    requested_window: tuple[date | None, date | None] | None,
    truncation: Truncation | None,
) -> Answer:
    """🔴 An answer about a datastore that cannot be read. AC-ARCH.3.

    Zero rows and a warning, never an exception: the requirement is that the
    server reports this state rather than crashing, and a tool that raised would
    reach the client as a failed call rather than as an answer they can read.
    The coverage block is present and zero so a consumer sees the shape it
    expects, and the warning is what tells them the zeroes mean "nothing to read"
    rather than "nothing happened".
    """
    return Answer(
        rows=[],
        # 🔴 A windowed tool still reports a window here, with null effective
        # bounds. The contract fixes ABSENCE of the key as "this tool takes no
        # window", so omitting it on `query_transactions` would say something
        # false about the tool rather than about the store -- and a consumer
        # branching on the key's presence would take the wrong branch precisely
        # when the datastore is unreadable. Nulls say "your window and this
        # store do not overlap", which is true of a store that cannot be read.
        effective_window=(
            None
            if requested_window is None
            else Window(
                requested_since=requested_window[0],
                requested_until=requested_window[1],
                effective_since=None,
                effective_until=None,
                caveats=[],
            )
        ),
        # 🔴 Present with zeroes for a capped tool, matching what `coverage`
        # does two fields down and for the same reason: the consumer sees the
        # shape it expects, and the `partial` warning is what tells them the
        # zeroes mean "nothing could be read". Every number here is true --
        # nothing was returned, nothing was readable to match, and the answer was
        # not truncated. It is empty for a reason the warning states.
        truncation=truncation,
        warnings=[
            Caveat(
                kind="partial",
                detail=(
                    f"the {config.environment} datastore is not readable ({problem}), so this "
                    f"answer is empty because nothing could be read — not because there is "
                    f"nothing to report. Run `bankmachine store init` to create it"
                ),
            )
        ],
        environment=config.environment,
        as_of=now_utc(),
        coverage={
            "connections": 0,
            "accounts": 0,
            "transactions": 0,
            "earliest_transaction": None,
            "latest_transaction": None,
            # 🔴 Keyed off the same condition that decides `effective_window`
            # above, so the two cannot disagree about whether this answer is
            # windowed. A windowed answer that carried `effective_window` but
            # dropped this sibling would move the wire shape precisely when the
            # store is unreadable, and a consumer branching on the key would
            # take the "unwindowed tool" branch for a tool that has a window.
            **({} if requested_window is None else {"transactions_in_effective_window": 0}),
        },
    )


def _readable(config: Config) -> str | None:
    """The reason the datastore cannot be read, or None when it can.

    Checked per call rather than once at startup: a datastore can be created,
    moved or corrupted while a long-lived server is running, and an answer must
    describe the store as it is at the moment of answering.
    """
    status = inspect(config)
    return None if status.healthy else (status.problem or "it is missing or unreadable")


def list_accounts(config: Config) -> Answer:
    """Every account, with its latest recorded balance."""
    problem = _readable(config)
    if problem is not None:
        return _unusable(config, problem, requested_window=None, truncation=None)
    with reader_connection(config) as conn:
        latest = (
            select(
                balances_daily.c.account_id,
                func.max(balances_daily.c.as_of_date).label("as_of_date"),
            )
            .group_by(balances_daily.c.account_id)
            .subquery()
        )
        result = conn.execute(
            select(
                accounts.c.account_id,
                institutions.c.name.label("institution"),
                accounts.c.name,
                accounts.c.mask,
                accounts.c.account_type,
                accounts.c.account_subtype,
                accounts.c.balance_class,
                balances_daily.c.current_minor,
                balances_daily.c.currency,
                latest.c.as_of_date,
            )
            .select_from(
                accounts.join(institutions)
                .outerjoin(latest, latest.c.account_id == accounts.c.account_id)
                .outerjoin(
                    balances_daily,
                    (balances_daily.c.account_id == accounts.c.account_id)
                    & (balances_daily.c.as_of_date == latest.c.as_of_date),
                )
            )
            .order_by(institutions.c.name, accounts.c.name)
        ).all()
        rows = [
            {
                "account_id": int(r[0]),
                "institution": r[1],
                "name": r[2],
                "mask": r[3],
                "type": r[4],
                "subtype": r[5],
                "balance_class": r[6],
                # 🔴 Minor units, and the field says so. A consumer that divided
                # by 100 without knowing would be wrong by two orders of
                # magnitude, and the number would still look plausible.
                "current_minor_units": None if r[7] is None else int(r[7]),
                "currency": r[8],
                "balance_as_of": None if r[9] is None else str(r[9]),
            }
            for r in result
        ]
        return _answer(config, conn, rows, requested_window=None, truncation=None)


class UnknownAccountError(ValueError):
    """An `account_id` naming no account, refused rather than answered empty.

    🔴 An id that names nothing and an account that was simply quiet in the
    window both select no rows, and `rows: []` cannot tell them apart. "No
    transactions" is an entirely ordinary thing for an account to have, so the
    wrong answer invites no second look -- which makes the typo that produces it
    more dangerous than a typo in an argument NAME, and that one is already
    refused by name.

    Raised from the query layer because only a datastore read knows which ids
    exist; rendered at the MCP boundary, which owns how a caller is told.
    """


def _account_exists(conn: SAConnection, account_id: int) -> bool:
    found = conn.execute(
        select(accounts.c.account_id).where(accounts.c.account_id == account_id).limit(1)
    ).first()
    return found is not None


def _transaction_filters(
    *,
    since: date | None,
    until: date | None,
    account_id: int | None,
) -> list[Any]:
    """🔴 The predicates of a transaction query, built once for both statements.

    The row query and the count MUST select the same set, and "remember to
    update both" is the enumeration failure this repo has already been bitten
    by: the two would drift apart silently, and the symptom would be a
    `matching` that contradicts the rows beside it -- a precise wrong number,
    which is worse than the vague one this chunk exists to remove. Built here,
    a filter added later reaches both by construction because there is only one
    place to add it.
    """
    filters: list[Any] = [transactions.c.removed_at.is_(None)]
    if since is not None:
        filters.append(transactions.c.posted_date >= since)
    if until is not None:
        filters.append(transactions.c.posted_date <= until)
    if account_id is not None:
        filters.append(transactions.c.account_id == account_id)
    return filters


def list_transactions(
    config: Config,
    *,
    since: date | None = None,
    until: date | None = None,
    account_id: int | None = None,
    limit: int = 100,
) -> Answer:
    """Transactions in a window, newest first. Soft-deleted rows are excluded."""
    problem = _readable(config)
    if problem is not None:
        return _unusable(
            config,
            problem,
            requested_window=(since, until),
            # Zero returned of zero matching: nothing was readable, so nothing
            # matched and nothing was dropped. The key stays present because its
            # absence would say this tool returns everything it finds.
            truncation=Truncation.over(returned=0, counted=0),
        )
    with reader_connection(config) as conn:
        # 🔴 Ordered AFTER the readability check on purpose: an unreadable store
        # knows nothing about which accounts exist, and "that account does not
        # exist" is a claim about the data rather than about the connection.
        if account_id is not None and not _account_exists(conn, account_id):
            raise UnknownAccountError(
                f"account_id {account_id} does not exist. list_accounts reports the ids that do."
            )
        filters = _transaction_filters(since=since, until=until, account_id=account_id)
        # 🔴 Both statements take the SAME from-clause as well as the same
        # filters. The join to `accounts` is part of what selects a row -- an
        # inner join drops a transaction whose account is absent -- so a count
        # taken over the bare table would exceed the rows and report a
        # truncation that never happened.
        source = transactions.join(accounts)
        statement = (
            select(
                transactions.c.transaction_id,
                accounts.c.name.label("account"),
                transactions.c.posted_date,
                transactions.c.description,
                transactions.c.merchant_name,
                transactions.c.amount_minor,
                transactions.c.currency,
                transactions.c.pending,
                transactions.c.source_category_primary,
                transactions.c.category_override,
            )
            .select_from(source)
            .where(*filters)
            .order_by(transactions.c.posted_date.desc(), transactions.c.transaction_id.desc())
            .limit(max(1, min(limit, MAX_ROWS)))
        )
        rows = [
            {
                "transaction_id": int(r[0]),
                "account": r[1],
                "date": str(r[2]),
                "description": r[3],
                "merchant": r[4],
                "amount_minor_units": int(r[5]),
                "currency": r[6],
                "pending": bool(r[7]),
                "category": r[9] or r[8],
                "category_is_override": r[9] is not None,
            }
            for r in conn.execute(statement).all()
        ]
        matching = conn.execute(
            select(func.count()).select_from(source).where(*filters)
        ).scalar_one()
        return _answer(
            config,
            conn,
            rows,
            requested_window=(since, until),
            # `returned` is derived from the rows themselves rather than from
            # `limit`, so it cannot claim a count the payload does not contain.
            truncation=Truncation.over(returned=len(rows), counted=matching),
        )


def spending_by_category(
    config: Config, *, since: date | None = None, until: date | None = None
) -> Answer:
    """🔴 An aggregate, which is the shape AC-4.2 asks the tool surface to prefer.

    Money *out* only: this sums negative amounts and reports them as positive
    magnitudes, because "spending" is a question about outflow and mixing
    refunds in would answer a different one. The sign convention is what makes
    that a filter rather than a per-account special case.
    """
    problem = _readable(config)
    if problem is not None:
        # An aggregate is unpaginated by contract, bounded by the grouping
        # rather than by a row cap, so there is no truncation to report.
        return _unusable(config, problem, requested_window=(since, until), truncation=None)
    with reader_connection(config) as conn:
        statement = (
            select(
                func.coalesce(
                    transactions.c.category_override,
                    transactions.c.source_category_primary,
                    "UNCATEGORIZED",
                ).label("category"),
                func.count().label("count"),
                func.sum(transactions.c.amount_minor).label("total"),
            )
            .where(
                transactions.c.removed_at.is_(None),
                transactions.c.amount_minor < 0,
            )
            .group_by("category")
            .order_by(func.sum(transactions.c.amount_minor))
        )
        if since is not None:
            statement = statement.where(transactions.c.posted_date >= since)
        if until is not None:
            statement = statement.where(transactions.c.posted_date <= until)
        rows = [
            {
                "category": r[0],
                "transactions": int(r[1]),
                "spent_minor_units": abs(int(r[2])),
            }
            for r in conn.execute(statement).all()
        ]
        return _answer(config, conn, rows, requested_window=(since, until), truncation=None)


def pipeline_health(config: Config) -> Answer:
    """Every connection and what is wrong with it — the question AC-ARCH.3 names.

    Returns rows even when everything is fine, because "healthy" is an answer
    and an empty result would be indistinguishable from a broken query.
    """
    problem = _readable(config)
    if problem is not None:
        return _unusable(config, problem, requested_window=None, truncation=None)
    with reader_connection(config) as conn:
        result = conn.execute(
            select(
                connections.c.connection_id,
                institutions.c.name,
                connections.c.status,
                connections.c.last_success_at,
                connections.c.last_error_code,
                connections.c.requested_history_days,
                connections.c.granted_history_days,
                connections.c.retired_at,
                sync_state.c.history_start_date,
            )
            .select_from(
                connections.join(institutions).outerjoin(
                    sync_state,
                    (sync_state.c.connection_id == connections.c.connection_id)
                    # 🔴 Filtered on domain, like every other read of this table.
                    # `sync_state` is keyed on (connection, domain) and balances
                    # and holdings advance on their own schedules -- so the day a
                    # second domain lands, an unfiltered join silently returns
                    # one health row per domain and a consumer counts each
                    # connection twice.
                    & (sync_state.c.domain == TRANSACTIONS_DOMAIN),
                )
            )
            .order_by(connections.c.connection_id)
        ).all()
        rows = [
            {
                "connection_id": int(r[0]),
                "institution": r[1],
                "status": r[2],
                "last_success_at": None if r[3] is None else r[3].isoformat(),
                "last_error_code": r[4],
                "requested_history_days": None if r[5] is None else int(r[5]),
                # 🔴 Null is reported as null, never as zero or as "complete".
                "granted_history_days": None if r[6] is None else int(r[6]),
                "history_starts": None if r[8] is None else str(r[8]),
                "retired": r[7] is not None,
            }
            for r in result
        ]
        return _answer(config, conn, rows, requested_window=None, truncation=None)
