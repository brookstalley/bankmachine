"""The window resolver, over every boundary a request can cross.

🔴 Assertions here compare warning kinds by EQUALITY on the whole list, never by
`in` on a joined string. `window_starts_before_coverage` and
`window_extends_past_coverage` share a `window_` prefix, and `learnings.md` records
two separate occasions where substring containment let an assertion pass against
the value it was written to exclude. Comparing the list also pins the ABSENCE of
a warning, which is the half that catches a resolver firing on every call — the
exact defect these two kinds exist to fix.

The resolver is pure: it takes a coverage dict and an instant, so none of this
needs a datastore. That is deliberate, and it is why the matrix can be complete.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from bankmachine.query import InvertedWindowError, Window, resolve_window
from bankmachine.store.types import UtcInstant, utc_instant

#: Coverage running 2024-09-16 .. 2026-08-27, the shape the sandbox actually
#: holds. `latest_transaction` is deliberately EARLIER than `as_of`: the store
#: covers up to today even when nothing happened yesterday, so the effective
#: upper bound is today rather than the last transaction. A fixture where the
#: two coincided could not tell those two readings apart.
COVERAGE = {
    "connections": 1,
    "accounts": 14,
    "transactions": 388,
    "earliest_transaction": "2024-09-16",
    "latest_transaction": "2026-08-27",
}

EMPTY_COVERAGE = {
    "connections": 0,
    "accounts": 0,
    "transactions": 0,
    "earliest_transaction": None,
    "latest_transaction": None,
}

AS_OF = utc_instant(datetime(2026, 9, 8, 17, 41, 2, tzinfo=UTC))
TODAY = date(2026, 9, 8)
EARLIEST = date(2024, 9, 16)


def _resolve(since: date | None, until: date | None, coverage: object = None) -> Window:
    return resolve_window(
        since=since,
        until=until,
        coverage=COVERAGE if coverage is None else coverage,  # type: ignore[arg-type]
        as_of=AS_OF,
    )


def _kinds(window: Window) -> list[str]:
    return [c.kind for c in window.caveats]


# --------------------------------------------------------------------------
# The quiet case: a window the store can answer over says nothing extra
# --------------------------------------------------------------------------


def test_a_window_wholly_inside_coverage_raises_no_caveat() -> None:
    """The case that must stay silent, or the two new kinds are worth nothing.

    An acceptance round measured the existing `gapped` notice arriving
    character-for-character identical on every response, which is why it cannot
    tell a caller whether THIS answer is degraded. A window warning that fired
    here would repeat that mistake immediately.
    """
    window = _resolve(date(2026, 8, 1), date(2026, 8, 31))

    assert _kinds(window) == []
    assert window.effective_since == date(2026, 8, 1)
    assert window.effective_until == date(2026, 8, 31)
    assert window.covers_nothing is False


@pytest.mark.parametrize(
    ("since", "until", "case"),
    [
        (EARLIEST, TODAY, "both bounds exactly on the edges"),
        (EARLIEST, date(2026, 8, 27), "start on the edge, end at the last transaction"),
    ],
)
def test_a_window_touching_the_boundary_is_not_crossing_it(
    since: date, until: date, case: str
) -> None:
    """`<` and `>`, not `<=` and `>=`.

    An off-by-one here fires a warning on the single most ordinary request there
    is — "everything you have" — which is the fastest possible way to teach a
    reader to ignore the field.
    """
    window = _resolve(since, until)

    assert _kinds(window) == [], case
    assert window.effective_since == since
    assert window.effective_until == until


# --------------------------------------------------------------------------
# Each boundary, alone and together
# --------------------------------------------------------------------------


def test_a_start_before_coverage_is_named_and_clamped() -> None:
    window = _resolve(date(2024, 1, 1), date(2026, 8, 31))

    assert _kinds(window) == ["window_starts_before_coverage"]
    assert window.effective_since == EARLIEST
    assert window.effective_until == date(2026, 8, 31)
    # 🔴 Whole phrase, and the effective start is quoted rather than described:
    # a caller who reads only the sentence still learns which window answered.
    assert window.caveats[0].detail == (
        "you asked from 2024-01-01, but coverage begins 2024-09-16. Anything before "
        "that date is absent rather than zero, so this answer covers 2024-09-16 onward"
    )


def test_an_end_after_today_is_named_and_clamped() -> None:
    window = _resolve(date(2026, 8, 1), date(2027, 12, 31))

    assert _kinds(window) == ["window_extends_past_coverage"]
    assert window.effective_since == date(2026, 8, 1)
    assert window.effective_until == TODAY
    # Whole phrases. The loose forms ("after 2026-09-08") stayed green through a
    # rewording that changed what the sentence claimed, which is the containment
    # trap `learnings.md` records -- caught here by re-reading, not by the test.
    assert window.caveats[0].detail == (
        "you asked until 2027-12-31, but this store holds nothing after 2026-09-08, "
        "so this answer covers through 2026-09-08"
    )


def test_crossing_both_boundaries_names_both_and_in_a_fixed_order() -> None:
    """Both, because a caller asking "everything, ever" crosses both at once.

    Order is asserted so the payload is stable for a consumer that reads
    positionally; nothing in the product depends on it, but an unstable list is
    a thing a consumer will come to depend on by accident.
    """
    window = _resolve(date(2000, 1, 1), date(2099, 1, 1))

    assert _kinds(window) == [
        "window_starts_before_coverage",
        "window_extends_past_coverage",
    ]
    assert window.effective_since == EARLIEST
    assert window.effective_until == TODAY


# --------------------------------------------------------------------------
# The unbounded request, which is the one a caller most needs answered
# --------------------------------------------------------------------------


def test_an_unbounded_request_reports_the_covered_span_and_warns_about_nothing() -> None:
    """An unbounded ask means nothing until you know what it covered.

    No caveat, because the caller asked for no bound and therefore crossed none.
    The value is entirely in `effective`.
    """
    window = _resolve(None, None)

    assert _kinds(window) == []
    assert window.requested_since is None
    assert window.requested_until is None
    assert window.effective_since == EARLIEST
    assert window.effective_until == TODAY


@pytest.mark.parametrize(
    ("since", "until", "expected_since", "expected_until"),
    [
        (None, date(2026, 8, 31), EARLIEST, date(2026, 8, 31)),
        (date(2026, 8, 1), None, date(2026, 8, 1), TODAY),
    ],
)
def test_one_open_bound_is_filled_from_coverage(
    since: date | None,
    until: date | None,
    expected_since: date,
    expected_until: date,
) -> None:
    window = _resolve(since, until)

    assert _kinds(window) == []
    assert window.effective_since == expected_since
    assert window.effective_until == expected_until


# --------------------------------------------------------------------------
# No overlap at all — the measured case that reads as "you spent nothing"
# --------------------------------------------------------------------------


def test_a_window_entirely_before_coverage_reports_no_effective_window() -> None:
    """The exact call an acceptance round called the most believable wrong answer.

    `spending_summary{since:2024-01-01, until:2024-06-30}` returns no rows
    against coverage that begins 2024-09-16. "You spent nothing" and "this is
    not knowable" are the same payload today. A backwards effective window
    (start after end) would be worse than none, because it reads like a window.
    """
    window = _resolve(date(2024, 1, 1), date(2024, 6, 30))

    assert _kinds(window) == ["window_starts_before_coverage"]
    assert window.effective_since is None
    assert window.effective_until is None
    assert window.covers_nothing is True
    assert "no part of the window you asked for" in window.caveats[0].detail


def test_a_window_entirely_in_the_future_reports_no_effective_window() -> None:
    window = _resolve(date(2027, 1, 1), date(2027, 12, 31))

    assert _kinds(window) == ["window_extends_past_coverage"]
    assert window.covers_nothing is True


@pytest.mark.parametrize(
    ("since", "until"),
    [
        (date(2027, 1, 1), date(2027, 12, 31)),
        (date(2024, 1, 1), date(2024, 6, 30)),
    ],
)
def test_a_caveat_never_claims_coverage_the_effective_window_denies(
    since: date, until: date
) -> None:
    """🔴 The prose and the structured field are two carriers of one fact.

    Caught scrubbing this chunk before review: the future-window sentence read
    "this answer covers through 2026-09-08" while `effective` was null — a
    plausible sentence the payload beside it contradicts, which is the exact
    defect this work cycle exists to remove, sitting inside the fix for it. The
    earlier test asserted the KIND and `covers_nothing` and never read the
    sentence, so it passed.

    Asserted for both no-overlap directions, because a fix applied to one
    sentence and not its twin is how the pair stops agreeing.
    """
    window = _resolve(since, until)

    assert window.covers_nothing is True
    assert len(window.caveats) == 1
    detail = window.caveats[0].detail
    assert "no part of the window you asked for" in detail
    # The clamped bound must not be quoted as a covered range when nothing is
    # covered. Both phrasings are excluded: one per sentence.
    assert "covers through" not in detail
    assert "onward" not in detail


# --------------------------------------------------------------------------
# An empty store has no span to reconcile against
# --------------------------------------------------------------------------


def test_an_empty_store_reconciles_nothing_and_invents_no_caveat() -> None:
    """Zero coverage is already announced by `coverage`; saying it twice is noise."""
    window = _resolve(date(2024, 1, 1), date(2026, 1, 1), coverage=EMPTY_COVERAGE)

    assert _kinds(window) == []
    assert window.covers_nothing is True
    assert window.requested_since == date(2024, 1, 1)


def test_an_unparseable_coverage_bound_is_treated_as_absent_not_raised() -> None:
    """🔴 Incompleteness rides the success path here; it never becomes an exception.

    `api-contract.md` § Direction fixes that for the whole surface. A resolver
    that raised on a malformed coverage bound would convert a degraded answer
    into a failed call — invisible exactly when it matters.
    """
    window = _resolve(
        date(2024, 1, 1),
        date(2026, 1, 1),
        coverage={**COVERAGE, "earliest_transaction": "not-a-date"},
    )

    assert _kinds(window) == []
    assert window.covers_nothing is True


# --------------------------------------------------------------------------
# The wire shape
# --------------------------------------------------------------------------


def test_the_wire_form_carries_requested_beside_effective() -> None:
    """Both halves, side by side. Either alone is the defect being fixed.

    `effective` alone cannot be checked against what was asked; `requested`
    alone is what the surface already had.
    """
    wire = _resolve(date(2024, 1, 1), date(2027, 1, 1)).to_wire()

    assert wire == {
        "requested": {"since": "2024-01-01", "until": "2027-01-01"},
        "effective": {"since": "2024-09-16", "until": "2026-09-08"},
    }


def test_a_window_covering_nothing_renders_nulls_rather_than_omitting_the_keys() -> None:
    """A missing key and a null mean different things to a tolerant reader.

    Omitting `effective` would read as "this server does not report one"; null
    reads as "there is none", which is the true statement.
    """
    wire = _resolve(date(2024, 1, 1), date(2024, 6, 30)).to_wire()

    assert wire["effective"] == {"since": None, "until": None}
    assert wire["requested"] == {"since": "2024-01-01", "until": "2024-06-30"}


def test_the_caveats_field_has_no_default() -> None:
    """🔴 The structural half, pinned so a later edit cannot quietly add one.

    A `Window` that had not been reconciled against coverage would be
    constructible the moment `caveats` gained a default, and every call site
    would keep type-checking. `Answer.warnings` holds the same line for the same
    reason.
    """
    with pytest.raises(TypeError):
        Window(  # type: ignore[call-arg]
            requested_since=None,
            requested_until=None,
            effective_since=None,
            effective_until=None,
        )


def test_as_of_is_read_as_a_calendar_date_at_exactly_one_site() -> None:
    """The instant/date seam `data-model.md` keeps apart.

    Late-evening UTC is the reading that would slip a day if the conversion ever
    went through a local zone: 23:59 UTC is already tomorrow in Asia and still
    today in the Americas. The answer's own `as_of` is UTC, so the window's
    "today" is UTC too, and one clock is what makes them comparable.
    """
    window = resolve_window(
        since=None,
        until=date(2026, 9, 30),
        coverage=COVERAGE,
        as_of=UtcInstant(datetime(2026, 9, 8, 23, 59, 59, tzinfo=UTC)),
    )

    assert window.effective_until == date(2026, 9, 8)
    assert _kinds(window) == ["window_extends_past_coverage"]


# --------------------------------------------------------------------------
# The invariant behind the two bugs found while scrubbing this chunk
# --------------------------------------------------------------------------


@given(
    since=st.one_of(st.none(), st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 1, 1))),
    until=st.one_of(st.none(), st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 1, 1))),
)
def test_a_window_that_covers_nothing_always_says_why(
    since: date | None, until: date | None
) -> None:
    """🔴 An unexplained empty window is the defect, in its most general form.

    Two separate instances of it survived the hand-written matrix above, because
    that matrix was written from the same assumption the code was: the
    future-window sentence contradicting its own `effective`, and an open-ended
    `since` in the future producing a null window with no warning at all. Both
    were found by probing, not by a test.

    So this asserts the rule rather than a third instance: whenever the store
    HAS coverage and the answer covers nothing, at least one caveat explains it.
    A future instance of this class fails here without anyone having predicted
    its shape.

    The empty-store case is excluded on purpose — `coverage` already announces
    itself there, and a window caveat would be a second voice saying it.
    """
    if since is not None and until is not None and until < since:
        # An inverted window is refused, not explained — see the resolver. This
        # branch is asserted rather than filtered out of the strategy, because
        # filtering would hide the fourth instance of the class the property
        # found: a shape I had not considered, reaching an unexplained empty.
        with pytest.raises(InvertedWindowError, match="is before since"):
            _resolve(since, until)
        return

    window = _resolve(since, until)

    if window.covers_nothing:
        assert window.caveats, f"{since}..{until} covered nothing and said nothing"


@given(
    since=st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 1, 1)),
    until=st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 1, 1)),
)
def test_no_caveat_ever_quotes_a_bound_the_effective_window_denies(
    since: date, until: date
) -> None:
    """The prose and the structured field never contradict each other.

    The generalisation of the first bug: any sentence claiming a covered range
    must be accompanied by an effective window that has one.
    """
    if until < since:
        with pytest.raises(InvertedWindowError, match="is before since"):
            _resolve(since, until)
        return

    window = _resolve(since, until)
    claims_coverage = any(
        "onward" in c.detail or "covers through" in c.detail for c in window.caveats
    )

    if claims_coverage:
        assert not window.covers_nothing, (
            f"{since}..{until} quoted a covered range while covering nothing"
        )


# --------------------------------------------------------------------------
# A row dated after today — the case this fixture deliberately cannot express
# --------------------------------------------------------------------------

#: `latest_transaction` AFTER `as_of`. Nothing forbids it: an authorization can
#: post forward, and an institution a day ahead in local time posts a date this
#: UTC clock has not reached. The sandbox has never held one, so this shape is
#: reasoned from the query predicate rather than measured — which is exactly why
#: it needs a fixture of its own rather than trust.
COVERAGE_WITH_FUTURE_ROWS = {
    **COVERAGE,
    "latest_transaction": "2026-12-25",
}


def test_the_covered_end_follows_the_data_when_a_row_is_dated_after_today() -> None:
    """🔴 Every returned row must lie inside the window the answer claims.

    `list_transactions` filters on the caller's `until`, not on the effective
    bound. So with `until: 2026-12-31` a row dated 2026-12-25 IS returned — and
    if the effective end were bare `today`, the answer would report covering
    through 2026-09-08 while handing back a row from December. A row outside the
    window the answer claims is this work cycle's own defect wearing the fix's
    clothes.
    """
    window = _resolve(None, date(2026, 12, 31), coverage=COVERAGE_WITH_FUTURE_ROWS)

    assert window.effective_until == date(2026, 12, 25)
    assert _kinds(window) == ["window_extends_past_coverage"]
    assert window.caveats[0].detail == (
        "you asked until 2026-12-31, but this store holds nothing after 2026-12-25, "
        "so this answer covers through 2026-12-25"
    )


def test_a_window_inside_the_future_rows_warns_about_nothing() -> None:
    """Asking exactly up to the last held date crosses no boundary."""
    window = _resolve(date(2026, 10, 1), date(2026, 12, 25), coverage=COVERAGE_WITH_FUTURE_ROWS)

    assert _kinds(window) == []
    assert window.effective_until == date(2026, 12, 25)


@given(
    since=st.one_of(st.none(), st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 1, 1))),
    until=st.one_of(st.none(), st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 1, 1))),
    latest=st.dates(min_value=date(2024, 9, 16), max_value=date(2030, 1, 1)),
)
def test_every_row_the_predicate_can_return_lies_inside_the_effective_window(
    since: date | None, until: date | None, latest: date
) -> None:
    """The invariant that makes `effective_window` a guarantee, not a description.

    `list_transactions` returns rows satisfying `posted_date >= since` (when
    given), `posted_date <= until` (when given), and — by definition of the
    store — `posted_date <= latest_transaction`. This asserts that the widest
    such row still falls inside the effective bounds, over a `latest` that
    ranges freely on both sides of `as_of`.

    🔴 **Scope of what this proves.** The resolver is pure, so this pins the
    guarantee against ONE coverage reading. In the running product the rows and
    the coverage are separate statements on a handle that holds no read snapshot,
    so a soft delete of a boundary row between them can still move the reported
    bound — reachable, rare, and tracked as #27. No test here can close that,
    because the gap is between two statements rather than inside either.
    """
    if since is not None and until is not None and until < since:
        return  # refused, not resolved — pinned by its own test above

    window = _resolve(since, until, coverage={**COVERAGE, "latest_transaction": latest.isoformat()})
    if window.covers_nothing:
        return

    assert window.effective_since is not None
    assert window.effective_until is not None
    # The latest date the predicate can admit.
    widest = latest if until is None else min(until, latest)
    if widest >= window.effective_since:
        assert widest <= window.effective_until, (
            f"a row dated {widest} passes the predicate but sits outside "
            f"{window.effective_since}..{window.effective_until}"
        )


def test_an_inverted_window_is_refused_rather_than_answered_with_an_empty() -> None:
    """🔴 The one empty window this function cannot explain, so it refuses it.

    There is no boundary crossed and no coverage fact to report — "nothing" is
    simply what an inverted window selects, which makes it indistinguishable
    from a real quiet period. The MCP boundary already refuses it by name while
    the caller's own words are in hand; this keeps the resolver's own invariant
    total rather than relying on that boundary staying in front of it.
    """
    with pytest.raises(InvertedWindowError, match="is before since"):
        _resolve(date(2025, 1, 2), date(2025, 1, 1))
