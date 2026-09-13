"""The wire envelope every MCP answer carries, and the vocabulary it speaks.

🔴 **This module exists because of one norm.** `api-contract.md` § Direction:
*every response carries a freshness stamp, and incompleteness rides the success
path as a warning field rather than as an exception.* The dangerous case is not
an error -- it is a **successful** answer computed over incomplete data, because
nothing throws and the numbers simply stop being true. The consumer is an
analyst agent that **cannot see a caveat which is not in the payload**:
documentation, log files and a health tool it did not think to call are all
invisible at the moment of answering.

🔴 **Nothing here touches the datastore, and that is the boundary this module is
for.** These types describe an answer's shape; `query.py` fills them by reading,
which is why the constructors that need a connection stay there. The split is
what lets the wire contract be reviewed on its own instead of inside a thousand
lines of unrelated SQL -- and it is why a tool's own vocabulary, the groupings
and flow classes a single aggregate defines, deliberately stays beside the SQL
that produces it rather than collecting here.

The invariant is enforced rather than described:
`tests/preferences/test_the_envelope_reaches_no_datastore.py` refuses a
datastore import here, on the same reasoning that puts the tool-registration
guards inside `_tool_definitions()` rather than in a style guide. A convention
only a reviewer enforces is one edit from gone.

🔴 **The environment is part of the envelope, not just the launch flag.** A
server pointed at sandbox data and one pointed at real money look identical in
their answers unless the answer says which it is. A flag selects; the envelope
confesses.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from bankmachine.build_id import build_identity
from bankmachine.store.types import UtcInstant, calendar_date

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
    # 🔴 Request-scoped, and it MOVED here from the connection-scoped tuple when
    # it gained emitters. Which accounts a store cannot denominate is standing
    # state, so the kind sat with the standing ones while it had no producer --
    # but it fires only on an answer that computes a total, and only when that
    # answer's own scope holds such an account. A kind in the connection tuple
    # promises to ride EVERY response equally; this one cannot, and leaving it
    # there would break the guarantee both tuples exist to make. Once one member
    # of one set behaves like the other's, a caller can no longer read the
    # ABSENCE of any kind in either set as information.
    "rule-applied",
    # The row query and the count are separate snapshots on an autocommit
    # reader, so a write landing between them is a data condition rather than a
    # defect. It is reported instead of smoothed away: a consumer comparing two
    # calls seconds apart deserves to know a write landed between them.
    "counted_during_change",
    # 🔴 Request-scoped, and deliberately not connection-scoped even though "this
    # account has never had a transaction" is standing state of the store. A kind
    # riding every response equally is the defect the comment above records: the
    # `gapped` notice arrived character-for-character identical on four
    # unrelated questions, true and useless. This one fires only when THIS
    # request's scope actually holds an uncovered account, so an empty answer
    # about such an account carries it and an ordinary quiet window does not.
    "accounts_without_coverage",
    # An account in THIS request's scope is closed, or its institution has
    # stopped listing it. The balance beside it froze on the day it was last
    # reported and is not a fact about today, which is a wrong number with no
    # signal unless the answer carries one.
    "account_no_longer_active",
    # 🔴 A position in THIS answer is not a current value: its price is older than
    # the day it was captured by more than the threshold, its price date is
    # unknown, or its account's investments stopped landing while the rest of the
    # connection carried on. Request-scoped because it is about the rows this
    # answer returned: the aggregator's sandbox values every position at a
    # years-old price, and that belongs on the answer holding those positions --
    # riding every answer would be the "true and useless" failure above.
    "positions_not_current",
    # A hold is not a settled amount, and a total that mixes the two changes
    # without any new activity. Request-scoped because it is a property of the
    # rows THIS answer drew on, not of the pipeline.
    "includes_pending_rows",
    # 🔴 Fires on a MEASURED inversion, not on the absence of a verification.
    # The name is the weaker of the two readings and predates the check; the
    # contract records why it was kept and why the trigger is the narrow one.
    # Getting this backwards is not a wording slip: the broad reading is true of
    # every connection until the operator checks one against a known deposit, so
    # a warning on it would ride nearly every answer identically -- the "true and
    # useless" failure the two tuples above exist to prevent. The not-yet-checked
    # state is disclosed as a per-connection verdict on the verification surface,
    # which is where "we cannot tell yet" belongs.
    "sign_convention_unverified",
    # A connection's roster was read successfully and listed NO accounts. Every
    # account on it is separately marked absent, which is the account-level
    # truth; this kind is the connection-level anomaly beside it, and the pair
    # is the point. Fourteen frozen balances and a broken feed look identical
    # from the account rows alone, so suppressing either one to avoid publishing
    # that ambiguity leaves a one-account connection unable to report its only
    # account absent at all.
    #
    # 🔴 **Its scope is RELATIONAL, like `sign_convention_unverified`'s above,
    # and the two emitters are not the same shape.** On an answer surface it is
    # request-scoped: it fires only where THIS request's scope holds an account
    # on such a connection. On the verification surface it is a per-connection
    # finding, which reaches a case the request-scoped one never can -- a
    # connection holding NO accounts at all has nothing to bring it into scope,
    # and that connection is the one an operator most needs told about. Reading
    # this entry as a single firing rule and making the health emitter obey it
    # would delete exactly that case.
    "roster_observed_empty",
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
        return self.covered_bounds() is None

    def covered_bounds(self) -> tuple[date, date] | None:
        """The effective bounds when there are any, else `None`.

        🔴 The same question as `covers_nothing`, asked so the answer NARROWS.
        A property cannot narrow `date | None` for a caller, so every site
        needing the bounds was rewriting the null check by hand — two hand-written
        copies of a predicate this type already models is how the two stop
        agreeing. Returning the pair makes `covers_nothing` the derived one and
        gives callers bounds a type checker will hold them to.
        """
        if self.effective_since is None or self.effective_until is None:
            return None
        return (self.effective_since, self.effective_until)

    def to_wire(self) -> dict[str, Any]:
        return {
            "requested": {
                "since": iso_or_none(self.requested_since),
                "until": iso_or_none(self.requested_until),
            },
            "effective": {
                "since": iso_or_none(self.effective_since),
                "until": iso_or_none(self.effective_until),
            },
        }


def iso_or_none(value: date | None) -> str | None:
    """A calendar date as the wire spells it, or `None` passed through.

    Public deliberately: `query.AccountCoverage.to_wire` formats its two dates
    with this, so it crosses the module boundary. A private name reached across
    that boundary would say the split had drawn the line in the wrong place --
    the honest reading is that formatting a date FOR THE WIRE belongs to the
    envelope, and is therefore part of what the envelope offers.
    """
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
    # measured.
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


class MalformedCursorError(ValueError):
    """A `cursor` this server did not issue for the question it arrived with.

    🔴 Its own type so the MCP boundary can refuse it by name, the way a
    malformed date is refused. The alternative — reading an unusable cursor as
    "start from the newest row" — returns page one under the name of page two:
    a plausible complete answer to a question nobody asked, which is the shape
    this work cycle exists to remove rather than to reintroduce one field over.
    Rides the same path `UnknownAccountError` does, for the same reason.
    """


#: What a caller is told when a cursor cannot be used. One sentence for every
#: rejection path on purpose: WHICH way a cursor is wrong — truncated in
#: transit, forged, stale, or issued for a different question — is this server's
#: business, and the caller's correct response to all four is the same one.
_CURSOR_REFUSAL = (
    "cursor is not a cursor this server issued for this request. Pass back the "
    "`next_cursor` from a previous answer to the same question, unchanged, or omit it "
    "to start from the newest row"
)

#: The cursor payload's shape tag. A cursor is opaque, so its internals are free
#: to change — but the MCP server is a subprocess a client relaunches, so a
#: cursor issued by one build and handed back to another is ordinary traffic,
#: and a payload read under the wrong shape would resume at a position that
#: means something else. Tagged, a cursor from a shape this build does not know
#: is refused rather than misread.
_CURSOR_SCHEME = 2


def _request_fingerprint(*, since: date | None, until: date | None, account_id: int | None) -> str:
    """Which result set a cursor belongs to, short enough to travel inside one.

    🔴 A keyset position is only meaningful inside the query that produced it.
    Page two's cursor handed to a request with a different `account_id` selects
    real rows, in the right order, and answers a question the caller did not
    ask — no error, no warning, and a payload that reads as a continuation. So
    the predicate travels with the position and the boundary compares them.

    `limit` is deliberately not part of it: it chooses how much of a result set
    comes back per page, not which result set that is, and a caller changing it
    between pages is doing something ordinary.
    """
    material = json.dumps(
        [iso_or_none(since), iso_or_none(until), account_id], separators=(",", ":")
    )
    # Eight bytes, because this discriminates a caller's mistake and is not a
    # signature. A forged cursor reaches only rows the request's own filters
    # already admit, at a position `since` could have reached anyway.
    return hashlib.blake2s(material.encode(), digest_size=8).hexdigest()


@dataclass(frozen=True, slots=True)
class Cursor:
    """Where a page stopped, in the one total order transactions come back in.

    🔴 **A keyset, never an offset**, and the difference is this work cycle's
    own subject. `ORDER BY ledger_date DESC, transaction_id DESC` is already a
    total order, so "everything after this row" is a predicate rather than a
    count of rows to skip. An offset is not: a sync inserting a row between two
    pages shifts every later page by one, so a caller walking them sees one row
    twice and never sees another — a paged answer that reads as complete and is
    not.

    🔴 **The position is a `ledger_date`, and it has to be.** The window filter,
    the order clause and this predicate are three descriptions of one total
    order; a cursor naming a column the other two no longer sort on resumes at a
    position that means something else. `posted_date` moves when a hold settles,
    so a keyset built on it would shift under a walking caller precisely on the
    rows most likely to change between two pages — the offset failure above,
    reached through the fix that was supposed to remove it.

    Opaque on the wire. The encoding is reversible rather than signed because
    there is nothing here to protect — the position names a row the request's
    own filters already admit — but it is tagged and fingerprinted, so a cursor
    that does not belong to this question is refused instead of answered.
    """

    ledger_date: date
    transaction_id: int
    #: The fingerprint of the request this cursor was issued for. Compared when a
    #: cursor comes back; never used to select a row.
    request: str

    @classmethod
    def issued_for(
        cls,
        *,
        ledger_date: date,
        transaction_id: int,
        since: date | None,
        until: date | None,
        account_id: int | None,
    ) -> Cursor:
        """The only route that should build one, so the fingerprint cannot be forgotten."""
        return cls(
            ledger_date=ledger_date,
            transaction_id=transaction_id,
            request=_request_fingerprint(since=since, until=until, account_id=account_id),
        )

    def encode(self) -> str:
        """The wire form: URL-safe and unpadded.

        `=` is the character a shell, a query string or a log line is most
        likely to eat in transit, and a cursor that arrives one character short
        is refused rather than misread — a refusal the caller cannot act on.
        """
        payload = json.dumps(
            {
                "v": _CURSOR_SCHEME,
                "d": self.ledger_date.isoformat(),
                "t": self.transaction_id,
                "q": self.request,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return base64.urlsafe_b64encode(payload).decode().rstrip("=")

    @classmethod
    def decode(cls, text: str) -> Cursor:
        """A cursor off the wire, or a refusal — never a silent fall back to page one."""
        try:
            payload = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
        except (ValueError, RecursionError):
            # Base64, UTF-8 and JSON *syntax* failures all derive from
            # `ValueError`. 🔴 One does not: `json.loads` on a deeply nested
            # document exhausts the stack and raises `RecursionError`, which is
            # a `RuntimeError`. Caught only as `ValueError`, that cursor escapes
            # this refusal, escapes the boundary's narrowing, and lands in its
            # broad catch — so a caller who mistyped an argument is answered
            # "internal error" and told to check whether their datastore is
            # readable. A false statement about a caller mistake is the exact
            # outcome `InvertedWindowError` exists to prevent.
            #
            # `from None` because the cause is a decoder's internals, which
            # `api-contract.md` § Error Model keeps off the wire.
            raise MalformedCursorError(_CURSOR_REFUSAL) from None
        if not isinstance(payload, dict) or payload.get("v") != _CURSOR_SCHEME:
            raise MalformedCursorError(_CURSOR_REFUSAL)
        ledger, transaction_id, request = payload.get("d"), payload.get("t"), payload.get("q")
        # 🔴 `bool` is an `int` in Python and JSON `true` decodes to one, so the
        # bool check is not defensive noise: without it a payload carrying
        # `"t": true` would resume at transaction 1 rather than being refused.
        if (
            not isinstance(ledger, str)
            or isinstance(transaction_id, bool)
            or not isinstance(transaction_id, int)
            or not isinstance(request, str)
        ):
            raise MalformedCursorError(_CURSOR_REFUSAL)
        try:
            ledger_date = date.fromisoformat(ledger)
        except ValueError:
            raise MalformedCursorError(_CURSOR_REFUSAL) from None
        return cls(ledger_date=ledger_date, transaction_id=transaction_id, request=request)


def parse_cursor(
    text: str | None,
    *,
    since: date | None,
    until: date | None,
    account_id: int | None,
) -> Cursor | None:
    """A `cursor` argument, decoded and checked against the request carrying it.

    Both halves live here rather than at the boundary because the encoder lives
    here: a decoder written one module away from the encoder is the second
    description that stops matching the first, and the fingerprint check is
    meaningless unless it runs at the one site that can compare it against the
    request being answered.
    """
    if text is None:
        return None
    cursor = Cursor.decode(text)
    if cursor.request != _request_fingerprint(since=since, until=until, account_id=account_id):
        raise MalformedCursorError(_CURSOR_REFUSAL)
    return cursor


@dataclass(frozen=True, slots=True)
class Truncation:
    """How many rows matched, how many came back, and therefore whether the cap bit.

    🔴 **`truncated` is a property, not a field.** The invariant is *truncated
    iff returned < remaining*, and a stored flag is a third thing that can
    disagree with the two counts. Derived, it cannot: there is no assignment to
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
    #: How many rows the request still had ahead of it when this page began --
    #: the whole result set on an unpaged call, and what is left from the
    #: cursor's position onward on a resumed one. 🔴 This is the figure
    #: `truncated` is derived from, because it is the one that answers "did this
    #: page leave anything behind"; `matching` cannot, since it does not move as
    #: a caller pages.
    remaining: int
    #: 🔴 How many rows the WHOLE request selects, cursor or no cursor, so it
    #: reads the same on every page of a walk and agrees with
    #: `coverage.transactions_in_effective_window` beside it, which does not
    #: narrow by cursor either. A per-page count under this name would let an
    #: agent quoting the last page answer "90 transactions" about a year that
    #: holds 390, with nothing in the payload contradicting it.
    matching: int
    #: 🔴 The count came back BELOW the rows, which means the store changed
    #: between the two reads. No default: `over()` is the only route that should
    #: build one of these, and a silent `False` here would be a claim that the
    #: two numbers describe one moment when nobody checked.
    counted_during_change: bool
    #: The last row of this page, from which the next one resumes. 🔴 No default,
    #: for the reason `effective_window` has none one type down: a capped tool
    #: that forgot it would issue no cursor, and the only symptom would be
    #: truncation staying inescapable — silently, on the success path, which is
    #: the failure mode this whole surface is being corrected for. `None` means
    #: there is no page to resume from, and it has to be written.
    resume_from: Cursor | None

    @classmethod
    def over(
        cls, *, returned: int, remaining: int, matching: int, resume_from: Cursor | None
    ) -> Truncation:
        """The only route that should build one, because a count can lag `returned`.

        Two counts, and they are two different questions. `remaining` is what
        this request still had ahead of it when the page began and is what
        `truncated` turns on; `matching` is what the whole request selects and
        stays put across a walk. On an unpaged call they are the same number and
        the caller may pass it twice.

        🔴 **The row query and the count are two snapshots, not one.**
        `store/connection.py` opens the read handle in autocommit — "every
        statement is its own snapshot" — and `store/engine.py` records that
        SQLAlchemy's transaction control is inert over these handles. The
        scheduled sync writer soft-deletes transactions, and the MCP reader may
        be mid-query when it wakes. So a row counted in the first statement and
        removed before the second is entirely reachable, and it makes the count
        smaller than the rows already in hand.

        Treated as the data condition it is rather than as an impossibility.
        This module rides incompleteness on the success path *because a
        plausible wrong number is worse than a failure* — but that argument
        cuts against raising here, not for it: refusing to answer a perfectly
        good question because a nightly sync landed mid-query would turn a
        harmless skew into a failed tool call, which `api-contract.md` §
        Direction forbids in as many words.

        `remaining` is floored at `returned`, because those rows were observed to
        match: reporting fewer would contradict the payload they sit beside, and
        `truncated` would then read false for the right reason by accident.
        `matching` is floored at `remaining` for the same reason one level out --
        the whole request cannot select fewer rows than one page of it still had
        ahead of it. The skew itself is not smoothed away — it rides out as a
        caveat, since a consumer comparing two calls seconds apart deserves to
        know a write landed between them.
        """
        left = max(remaining, returned)
        return cls(
            returned=returned,
            remaining=left,
            matching=max(matching, left),
            counted_during_change=remaining < returned,
            resume_from=resume_from,
        )

    @property
    def truncated(self) -> bool:
        """Whether THIS page left rows behind, which is the caller's loop condition.

        🔴 Derived from `remaining`, never from `matching`. `matching` describes
        the whole request and does not fall as a caller pages, so `returned <
        matching` is still true on the last page of a walk -- a caller looping on
        that would ask forever for a page that does not exist.
        """
        return self.returned < self.remaining

    @property
    def next_cursor(self) -> str | None:
        """The wire cursor, present when and only when there is a next page to reach.

        🔴 Derived from the same two counts `truncated` is, so "a cursor iff the
        answer is truncated" is one expression rather than two assignments that
        can disagree. A stored cursor could outlive the condition that justified
        it, and a consumer following one on a complete answer would page past
        the end of an answer that already held everything.

        The one case where a truncated answer carries no cursor is a page with
        no rows at all: `returned` is zero, the count taken a moment later is
        not, and there is no last row to resume from. Rare — it needs a write to
        land between the two statements — and the `rows_truncated` caveat says
        so without offering a route the caller cannot take.

        🔴 The mirror of that skew ends a walk EARLY, and it is worth naming
        because it looks like a clean finish. When enough rows are removed
        between the two statements the count comes back below the rows in hand,
        `remaining` floors at `returned`, `truncated` reads false and no cursor
        is issued — correct for the numbers in this payload, and possibly short of
        the window. `counted_during_change` is what says so, which is why it
        rides out rather than being smoothed away.
        """
        if not self.truncated or self.resume_from is None:
            return None
        return self.resume_from.encode()

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
        # 🔴 The cursor leads, because it is the only remedy that reaches EVERY
        # missing row: narrowing the window and raising `limit` each move the
        # boundary, while paging removes it. The `limit` clause is still offered
        # where it can still do something, and still withheld at the ceiling,
        # where advising a caller to raise a number to the value it already
        # holds is the self-contradicting sentence this surface exists to stop
        # emitting.
        remedy = (
            f"The cap of {MAX_ROWS} is a contract term, so narrowing the window is the "
            f"only other route to the rest"
            if self.returned >= MAX_ROWS
            else f"Narrow the window, or raise `limit` (at most {MAX_ROWS})"
        )
        if self.next_cursor is not None:
            remedy = (
                f"Pass this answer's `next_cursor` back as `cursor`, with the same window "
                f"and account, to read the next page. {remedy}"
            )
        caveats.append(
            Caveat(
                kind="rows_truncated",
                detail=(
                    f"{self.matching} transactions match this request. This answer returns "
                    f"the newest {self.returned} of the {self.remaining} still unread, so "
                    f"{self.remaining - self.returned} of them are still missing. Summing or "
                    f"counting these rows describes only what came back, not the window you "
                    f"asked about. {remedy}"
                ),
            )
        )
        return caveats

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "returned": self.returned,
            "remaining": self.remaining,
            "matching": self.matching,
            "truncated": self.truncated,
        }
        # Absent on a complete answer, exactly as `truncation` itself is absent
        # on an uncapped tool: the key's presence is the statement that there is
        # more to read, so a consumer that pages while the key is there stops
        # when it is gone, without having to compare two numbers to know.
        if self.next_cursor is not None:
            wire["next_cursor"] = self.next_cursor
        return wire


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
    #: plain sight rather than merely forget a call. The unbuilt tools in
    #: `api-contract.md` § Surface Inventory include windowed ones, and a clamp
    #: each of them has to remember is a clamp that decays.
    effective_window: Window | None
    #: 🔴 No default, for the same reason and by the same mechanism as
    #: `effective_window` above. `None` means this tool returns every row it
    #: found, and a tool that caps its rows would have to write `None` in plain
    #: sight to hide it. Aggregates write `None` truthfully:
    #: `api-contract.md` fixes them as unpaginated, bounded by the grouping.
    truncation: Truncation | None
    #: 🔴 No default, and it sits before `coverage` for the same reason the two
    #: fields above do: a defaulted field here could be forgotten, and the
    #: forgetting would look exactly like a tool that truthfully has no totals.
    #: `None` means this tool answers with rows alone. An empty LIST means this
    #: tool carries a totals block and there was nothing in the window to put in
    #: it -- a distinction a consumer branching on the key's presence depends on,
    #: because `api-contract.md` fixes a key's absence as information.
    totals: list[dict[str, Any]] | None
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
            # Before `rows`, because it is what a reader should meet FIRST: the
            # headline this tool exists to correct is that a raw outflow total
            # can be several times the money that actually went out the door,
            # and a decomposition placed after several hundred rows is one
            # nobody reaches.
            **({} if self.totals is None else {"totals": self.totals}),
            "rows": self.rows,
        }
