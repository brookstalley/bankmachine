"""The read path, exercised against rows that are authorisation holds. AC-13.1–13.7.

🔴 **This file exists because no pending row had ever reached `query.py`.** The
write path has been implemented and tested since FR-2 — a posting entry finds its
hold, updates it in place and keeps the local id — but every read-path fixture in
the suite hardcoded `pending: False`, so no assertion anywhere could tell correct
handling of a hold from no handling at all. `money_summary` folded authorisation
holds into a spending total and said nothing, which is this product's own defect
class: a well-formed answer that does not state its own scope.

Every fixture here goes through the real derivers, like every other fixture in
this suite. A hand-inserted row would encode this file's assumptions about the
provenance CHECK rather than what the product actually writes — and for holds
specifically it would let a test assert a `pending` value the write path could
never produce.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

from bankmachine import query
from bankmachine.config import Config
from bankmachine.connector import TRANSACTIONS_SYNC
from bankmachine.derivers import ALL_DERIVERS
from bankmachine.store.derivation import apply_response
from bankmachine.store.engine import reader_connection, writer_connection
from bankmachine.store.schema import accounts
from bankmachine.store.types import calendar_date, now_utc
from test_mcp import _call, _seed

SOURCE_ACCOUNT = "acct-1"


def _entry(
    *,
    transaction_id: str,
    amount: str,
    days_ago: int = 0,
    pending: bool = False,
    pending_transaction_id: str | None = None,
    category: str = "GENERAL_MERCHANDISE",
) -> dict[str, Any]:
    """One aggregator change entry, in the live shape `api-notes-plaid.md` §17 records."""
    return {
        "account_id": SOURCE_ACCOUNT,
        "transaction_id": transaction_id,
        "amount": amount,
        "iso_currency_code": "USD",
        "date": str(now_utc().date() - timedelta(days=days_ago)),
        "authorized_date": None,
        "pending": pending,
        "pending_transaction_id": pending_transaction_id,
        "name": f"Merchant {transaction_id}",
        "merchant_name": None,
        "personal_finance_category": {"primary": category, "detailed": category},
    }


def _page(
    config: Config,
    *,
    added: list[dict[str, Any]] | None = None,
    removed: list[dict[str, Any]] | None = None,
    cursor: str,
) -> None:
    body = json.dumps(
        {
            "accounts": [],
            "added": added or [],
            "modified": [],
            "removed": removed or [],
            "next_cursor": cursor,
            "has_more": False,
            "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE",
            "request_id": f"req-{cursor}",
        }
    ).encode()
    with writer_connection(config) as conn:
        apply_response(
            conn,
            connection_id=1,
            endpoint=TRANSACTIONS_SYNC.path,
            body=body,
            received_at=now_utc(),
            derivers=ALL_DERIVERS,
        )


def _removal(transaction_id: str) -> dict[str, Any]:
    return {"account_id": SOURCE_ACCOUNT, "transaction_id": transaction_id}


def _hold(config: Config, transaction_id: str, amount: str, *, days_ago: int = 0) -> None:
    """One unsettled authorisation hold, written the way the aggregator delivers one."""
    _page(
        config,
        added=[
            _entry(transaction_id=transaction_id, amount=amount, days_ago=days_ago, pending=True)
        ],
        cursor=f"cursor-{transaction_id}",
    )


def _totals(wire: dict[str, Any], currency: str = "USD") -> dict[str, Any]:
    entries = [entry for entry in wire["totals"] if entry["currency"] == currency]
    assert len(entries) == 1, f"expected exactly one {currency} totals entry, got {wire['totals']}"
    result: dict[str, Any] = entries[0]
    return result


def _kinds(wire: dict[str, Any]) -> list[str]:
    return [str(warning["kind"]) for warning in wire["warnings"]]


def _warning(wire: dict[str, Any], kind: str) -> dict[str, Any]:
    found = [warning for warning in wire["warnings"] if warning["kind"] == kind]
    assert len(found) == 1, f"expected exactly one {kind!r} warning, got {_kinds(wire)}"
    result: dict[str, Any] = found[0]
    return result


# --------------------------------------------------------------------------
# AC-13.1 — the disclosure is always present, and it is a real number
# --------------------------------------------------------------------------


def test_a_summary_over_settled_rows_reports_zero_pending_rather_than_omitting_it(
    initialized_config: Config,
) -> None:
    """🔴 Present and ZERO, never absent — the half of AC-13.1 that is easy to skip.

    A key that appeared only when something was pending would leave a reader
    unable to tell "none of this is a hold" from "this tool does not say", and
    the second is exactly what the surface looked like before this work. The zero
    is what makes every other answer's number readable.
    """
    _seed(initialized_config)
    wire = _call(initialized_config, "money_summary", {})["structuredContent"]

    assert _totals(wire)["pending_transactions"] == 0
    assert _totals(wire)["pending_net_minor_units"] == 0
    for row in wire["rows"]:
        assert row["pending_transactions"] == 0, row
        assert row["pending_net_minor_units"] == 0, row


def test_a_summary_over_settled_rows_raises_no_pending_warning(
    initialized_config: Config,
) -> None:
    """🔴 The ABSENCE of a request-scoped warning is information.

    `includes_pending_rows` fires only when this answer actually drew on a hold,
    so a total arriving without it can be read as a total of settled money. A
    kind that rode every response equally would be the `gapped` defect the
    envelope's own comment records — true, and useless for telling a caller
    whether THIS answer is the compromised one.
    """
    _seed(initialized_config)
    wire = _call(initialized_config, "money_summary", {})["structuredContent"]

    assert "includes_pending_rows" not in _kinds(wire)


def test_a_hold_is_counted_and_its_magnitude_stated(initialized_config: Config) -> None:
    """🔴 AC-13.1 — the count AND the signed magnitude, on the row and in the totals.

    A count alone cannot be acted on: "one row is pending" leaves a reader unable
    to tell a $4 coffee hold from a $2,000 hotel authorisation, and the whole
    question is whether the figure beside it can move enough to matter.
    """
    _seed(initialized_config)
    _hold(initialized_config, "hold-1", "25.00")

    wire = _call(initialized_config, "money_summary", {})["structuredContent"]
    totals = _totals(wire)

    assert totals["pending_transactions"] == 1
    # Signed as stored: a hold on a purchase is money on its way OUT.
    assert totals["pending_net_minor_units"] == -2500
    pending_rows = [row for row in wire["rows"] if row["pending_transactions"]]
    assert len(pending_rows) == 1, "the hold reached no group row"
    assert pending_rows[0]["pending_net_minor_units"] == -2500


def test_the_pending_part_of_a_group_is_a_part_of_that_groups_own_net(
    initialized_config: Config,
) -> None:
    """🔴 The disclosure qualifies the figure it rides beside, not some other figure.

    Both are computed in one pass over one predicate set, so `pending_net` is by
    construction a component of `net` rather than a second count taken a moment
    later — which could disagree with the very number it is supposed to qualify,
    and would be believed because it sits on the same row.
    """
    _seed(initialized_config)
    _hold(initialized_config, "hold-1", "25.00")

    wire = _call(initialized_config, "money_summary", {"group_by": "flow_class"})[
        "structuredContent"
    ]
    totals = _totals(wire)

    # Contained: a group cannot hold more holds than it holds rows.
    for row in wire["rows"]:
        assert 0 <= row["pending_transactions"] <= row["transactions"], row
    # And the totals entry is the sum of its own rows, not a second count.
    assert totals["pending_transactions"] == sum(
        row["pending_transactions"] for row in wire["rows"] if row["currency"] == "USD"
    )
    assert totals["pending_net_minor_units"] == sum(
        row["pending_net_minor_units"] for row in wire["rows"] if row["currency"] == "USD"
    )
    assert totals["pending_transactions"] == 1


def test_a_summary_over_a_hold_warns_and_names_the_magnitude(
    initialized_config: Config,
) -> None:
    """The warning carries what a consumer has to say out loud, not just a flag."""
    _seed(initialized_config)
    _hold(initialized_config, "hold-1", "25.00")

    wire = _call(initialized_config, "money_summary", {})["structuredContent"]
    detail = _warning(wire, "includes_pending_rows")["detail"]

    assert "-2500" in detail, detail
    assert "USD" in detail, detail


# --------------------------------------------------------------------------
# AC-13.7 — the row surface, which had never seen a pending row either
# --------------------------------------------------------------------------


def test_a_transaction_page_holding_a_hold_says_so(initialized_config: Config) -> None:
    """🔴 The row tool discloses too, because a caller can sum the rows themselves.

    `query_transactions` publishes `pending` per row, which is necessary and not
    sufficient: an agent adding up a page has already produced the conflated
    figure by the time it reads the field. The warning is what arrives before the
    arithmetic.
    """
    _seed(initialized_config)
    _hold(initialized_config, "hold-1", "25.00")

    wire = _call(initialized_config, "query_transactions", {})["structuredContent"]

    assert any(row["pending"] for row in wire["rows"]), "the fixture wrote no pending row"
    assert "includes_pending_rows" in _kinds(wire)
    assert "-2500" in _warning(wire, "includes_pending_rows")["detail"]


def test_a_transaction_page_holding_no_hold_stays_quiet(initialized_config: Config) -> None:
    _seed(initialized_config)

    wire = _call(initialized_config, "query_transactions", {})["structuredContent"]

    assert not any(row["pending"] for row in wire["rows"])
    assert "includes_pending_rows" not in _kinds(wire)


def test_the_row_tools_notice_counts_the_rows_it_returned_not_the_ones_it_matched(
    initialized_config: Config,
) -> None:
    """🔴 The caveat describes THIS PAGE, because that is what a caller can add up.

    A walk's later page may hold no holds at all. A notice counting the whole
    result set would tell that page it mixes in holds it does not contain — a
    precise false statement about the payload beside it, which is worse than the
    vague one this disclosure removes.
    """
    _seed(initialized_config)
    _hold(initialized_config, "hold-old", "25.00", days_ago=9)

    # The newest rows first, and the hold is the oldest, so a one-row page cannot
    # contain it.
    first = _call(initialized_config, "query_transactions", {"limit": 1})["structuredContent"]
    assert first["truncation"]["truncated"], "the fixture did not truncate, so nothing is tested"
    assert not any(row["pending"] for row in first["rows"])
    assert "includes_pending_rows" not in _kinds(first)

    every = _call(initialized_config, "query_transactions", {})["structuredContent"]
    assert "includes_pending_rows" in _kinds(every)


# --------------------------------------------------------------------------
# AC-13.4 — a hold that expired is attributable rather than silent
# --------------------------------------------------------------------------


def test_an_expired_hold_is_reported_as_the_reason_a_total_shrank(
    initialized_config: Config,
) -> None:
    """🔴 AC-13.4 — the one way a total moves that re-asking will not explain.

    The row is soft-deleted, so every figure here excludes it by construction and
    no arithmetic over the returned rows could reach it. Without this field a
    consumer comparing two answers sees a number fall and has exactly two
    candidate explanations — a hold went away, or the data is incomplete — with
    nothing in the payload to choose between them. The second is what the
    coverage warnings already describe, so the first has to be said too.
    """
    _seed(initialized_config)
    _hold(initialized_config, "hold-gone", "25.00")
    _page(initialized_config, removed=[_removal("hold-gone")], cursor="cursor-expiry")

    wire = _call(initialized_config, "money_summary", {})["structuredContent"]
    totals = _totals(wire)

    assert totals["expired_holds"] == 1
    assert totals["expired_holds_net_minor_units"] == -2500
    # It left the answer: nothing pending remains, and no figure counts it.
    assert totals["pending_transactions"] == 0
    assert "includes_pending_rows" not in _kinds(wire)


def test_a_settled_row_that_was_later_withdrawn_is_not_reported_as_an_expired_hold(
    initialized_config: Config,
) -> None:
    """🔴 The discrimination the field rests on, and the one a count would blur.

    `pending = 1 AND removed` is a hold that never became anything. A row that
    settled and was withdrawn afterwards left as `pending = 0` — an ordinary
    withdrawal, not an authorisation that expired. Counting every removed row
    would report the second as the first and tell a consumer a hold dropped off
    when a real transaction was retracted.
    """
    _seed(initialized_config)
    _hold(initialized_config, "hold-1", "25.00")
    _page(
        initialized_config,
        added=[_entry(transaction_id="posted-1", amount="25.00", pending_transaction_id="hold-1")],
        cursor="cursor-settle",
    )
    _page(initialized_config, removed=[_removal("posted-1")], cursor="cursor-retract")

    totals = _totals(_call(initialized_config, "money_summary", {})["structuredContent"])

    assert totals["expired_holds"] == 0, (
        "a settled row that was later retracted was counted as an expired hold"
    )
    assert totals["expired_holds_net_minor_units"] == 0


def test_a_row_that_settled_out_of_a_hold_is_disclosed_as_one(
    initialized_config: Config,
) -> None:
    """🔴 AC-13.4's other half — the amount that CHANGED rather than left.

    A settlement replaces the authorised figure in place, so a total computed
    before it and one computed after it differ with no new transaction anywhere.
    A consumer watching that drift needs to know how much of the window arrived
    by replacement, and this is the only field that says so.
    """
    _seed(initialized_config)
    _hold(initialized_config, "hold-1", "25.00")
    _page(
        initialized_config,
        added=[_entry(transaction_id="posted-1", amount="31.40", pending_transaction_id="hold-1")],
        cursor="cursor-settle",
    )

    totals = _totals(_call(initialized_config, "money_summary", {})["structuredContent"])

    assert totals["settled_from_hold"] == 1
    assert totals["settled_from_hold_net_minor_units"] == -3140, (
        "the settled figure must be the one reported, not the hold's"
    )
    assert totals["pending_transactions"] == 0, "the settled row is no longer a hold"


def test_a_currency_present_only_in_an_expired_hold_still_gets_a_totals_entry(
    initialized_config: Config,
) -> None:
    """🔴 The entry cannot be built from the returned rows, so it must be added.

    Every row of that currency is excluded from the answer, so summing what came
    back finds no such currency at all — and dropping the entry would hide the
    one fact explaining why an earlier answer's figure is gone.
    """
    _seed(initialized_config)
    _hold(initialized_config, "hold-gone", "25.00")
    _page(initialized_config, removed=[_removal("hold-gone")], cursor="cursor-expiry")

    wire = _call(initialized_config, "money_summary", {})["structuredContent"]
    usd = _totals(wire)

    assert usd["expired_holds"] == 1
    assert [entry["currency"] for entry in wire["totals"]] == ["USD"]


# --------------------------------------------------------------------------
# AC-13.5 — a hold too old to be an ordinary authorisation
# --------------------------------------------------------------------------


def test_a_hold_older_than_the_declared_threshold_is_reported(
    initialized_config: Config,
) -> None:
    """🔴 AC-13.5 — past the outer end of any ordinary authorisation lifetime.

    Named with its id and its age, because "an account has a stranded hold" is a
    finding whose next move is to look at the transaction; a number nobody can
    act on is the AC-11.1 outcome this threshold's derivation is written against.

    🔴 **Asserted on `get_coverage_report`, and it was moved there.** It first
    asserted the same fact in `money_summary`'s pending detail, which is the
    analysis surface -- and AC-13.5 puts a stranded hold on the VERIFICATION
    surface, where `api-contract.md` rules per-account findings belong. The
    assertion is unchanged in force; only the surface it reads moved, and it
    moved because the analysis placement produced two defects: a cross-scope
    count on a truncated walk, and an escape from the non-active suppression.
    """
    _seed(initialized_config)
    _hold(
        initialized_config,
        "hold-stale",
        "25.00",
        days_ago=query.STRANDED_HOLD_AFTER_DAYS + 15,
    )

    wire = _call(initialized_config, "get_coverage_report", {})["structuredContent"]
    holding = [row for row in wire["rows"] if row["stranded_holds"]]

    assert len(holding) == 1, f"expected one account holding a stranded hold: {wire['rows']}"
    worst = holding[0]["oldest_stranded_hold"]
    assert worst is not None, "the count was reported with nothing to go look at"
    assert worst["days_pending"] > query.STRANDED_HOLD_AFTER_DAYS, worst
    assert worst["transaction_id"], "the finding has to name the transaction to look at"


def test_a_hold_inside_the_threshold_is_disclosed_without_being_called_stranded(
    initialized_config: Config,
) -> None:
    """🔴 The threshold has to be able to NOT fire, or it is not a threshold.

    A hold placed yesterday is entirely ordinary. Reporting it would reproduce
    the ~146-findings-and-no-signal outcome the constant's derivation exists to
    avoid — and it is the reason the constant sits at the outer end of the
    ordinary range rather than the typical one.
    """
    _seed(initialized_config)
    _hold(initialized_config, "hold-fresh", "25.00", days_ago=1)

    coverage = _call(initialized_config, "get_coverage_report", {})["structuredContent"]
    summary = _call(initialized_config, "money_summary", {})["structuredContent"]

    for row in coverage["rows"]:
        assert row["stranded_holds"] == 0, f"an ordinary overnight hold was called stranded: {row}"
        assert row["oldest_stranded_hold"] is None, row
    assert _totals(summary)["pending_transactions"] == 1, (
        "the hold still has to be disclosed as pending; not-stranded is not not-pending"
    )


def test_the_stranded_threshold_is_measured_from_one_boundary(
    initialized_config: Config,
) -> None:
    """🔴 One definition of "too old", evaluated in SQL and in Python.

    `_stranded_cutoff` is the single place the boundary is expressed. Two
    hand-written spellings of "older than the threshold" is how a producer and
    its caller start disagreeing about the same row, which this module already
    carries a rule about.
    """
    _seed(initialized_config)
    _hold(initialized_config, "hold-a", "25.00", days_ago=query.STRANDED_HOLD_AFTER_DAYS + 1)
    _hold(initialized_config, "hold-b", "25.00", days_ago=query.STRANDED_HOLD_AFTER_DAYS - 1)

    today = calendar_date(now_utc().date())
    with reader_connection(initialized_config) as conn:
        stranded = query._stranded_holds(conn, today=today)

    assert [hold.days_pending for hold in stranded] == [query.STRANDED_HOLD_AFTER_DAYS + 1]
    assert stranded[0].posted_date < query._stranded_cutoff(today)


# --------------------------------------------------------------------------
# AC-13.6 — the pending link serves a query that exists
# --------------------------------------------------------------------------


@pytest.fixture
def captured_statements() -> Iterator[list[tuple[str, Any]]]:
    """Every statement the engine ran, with the parameters it ran them with.

    🔴 Captured from the real execution rather than rebuilt here. A test that
    constructed the statement itself would explain its own SQL to SQLite and
    agree forever, which tells you nothing about the SQL the product emits.
    """
    seen: list[tuple[str, Any]] = []

    def _capture(
        _conn: Any,
        _cursor: Any,
        statement: str,
        parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        seen.append((statement, parameters))

    event.listen(Engine, "before_cursor_execute", _capture)
    try:
        yield seen
    finally:
        event.remove(Engine, "before_cursor_execute", _capture)


def test_the_pending_link_index_serves_the_reader_that_exists(
    initialized_config: Config, captured_statements: list[tuple[str, Any]]
) -> None:
    """🔴 AC-13.6, settled by measurement rather than by assertion.

    `transactions_pending_link` was maintained on every write and read by
    nothing, and `data-model.md` justified it with a query that did not exist:
    AC-2.3's match is served by `transactions_source_identity`, because a hold
    answers to its OWN id until it posts. The criterion allows either making the
    index serve a real query or dropping it. It now serves one — the reverse
    lookup, "which rows in this window arrived by replacing a hold" — and this
    asserts SQLite actually reaches for it, because an index nothing plans
    against is the same dead weight under a new justification.
    """
    _seed(initialized_config)
    _hold(initialized_config, "hold-1", "25.00")
    _page(
        initialized_config,
        added=[_entry(transaction_id="posted-1", amount="31.40", pending_transaction_id="hold-1")],
        cursor="cursor-settle",
    )
    captured_statements.clear()

    query.money_summary(initialized_config)

    settled = [
        (statement, parameters)
        for statement, parameters in captured_statements
        if "source_pending_transaction_id IS NOT NULL" in statement
    ]
    assert len(settled) == 1, (
        f"expected exactly one reader of the pending link; saw {len(settled)}. If it is 0 the "
        f"index has no reader again and AC-13.6 is unresolved"
    )
    statement, parameters = settled[0]
    with reader_connection(initialized_config) as conn:
        plan = conn.exec_driver_sql(f"EXPLAIN QUERY PLAN {statement}", parameters).all()
    detail = " | ".join(str(step[-1]) for step in plan)

    assert "transactions_pending_link" in detail, (
        f"SQLite planned the pending-link reader without the index that exists for it: {detail}"
    )


def test_a_settlement_is_found_by_the_hold_it_replaced(initialized_config: Config) -> None:
    """The column the index is on is the one that records which hold this row was.

    Written on every settlement and, before this chunk, read by nothing anywhere
    in `src/`. This is the read that makes the write worth making.
    """
    _seed(initialized_config)
    _hold(initialized_config, "hold-1", "25.00")
    _page(
        initialized_config,
        added=[_entry(transaction_id="posted-1", amount="31.40", pending_transaction_id="hold-1")],
        cursor="cursor-settle",
    )

    with reader_connection(initialized_config) as conn:
        transitions = query._hold_transitions(conn, since=None, until=None)

    assert transitions.settled_for("USD").transactions == 1
    assert transitions.settled_for("USD").net_minor == -3140
    assert transitions.expired_for("USD").transactions == 0


# --------------------------------------------------------------------------
# AC-13.5, the half that crossed a delegation boundary — the finding reaching
# the VERIFICATION surface.
#
# 🔴 `api-contract.md` rules that a per-account finding belongs on
# `get_coverage_report` rather than on an analysis answer, and the producer and
# that tool were built by two agents who could not see each other. The wiring
# between them was made by neither, which makes this the criterion most likely
# to be reported as delivered while a consumer can still never see it. So it is
# pinned from the TOOL's side, over the wire.
# --------------------------------------------------------------------------


def test_the_coverage_report_names_a_stranded_hold_on_the_account_holding_it(
    initialized_config: Config,
) -> None:
    """AC-13.5: a stranded hold is surfaced on the verification surface."""
    _seed(initialized_config)
    _hold(
        initialized_config,
        "hold-stranded-cov",
        "42.00",
        days_ago=query.STRANDED_HOLD_AFTER_DAYS + 20,
    )

    wire = _call(initialized_config, "get_coverage_report", {})["structuredContent"]
    holding = [row for row in wire["rows"] if row["stranded_holds"]]

    assert len(holding) == 1, (
        f"expected exactly one account reported as holding a stranded hold, got "
        f"{[(r['account_id'], r['stranded_holds']) for r in wire['rows']]}"
    )
    worst = holding[0]["oldest_stranded_hold"]
    assert worst is not None, "the count was reported with nothing to look at"
    assert worst["days_pending"] >= query.STRANDED_HOLD_AFTER_DAYS, worst
    assert worst["amount_minor_units"] == -4200, worst


def test_every_coverage_row_states_its_stranded_count_even_when_it_is_zero(
    initialized_config: Config,
) -> None:
    """🔴 Present and zero, never absent — and the null is a null, not a gap.

    An account with no stranded hold is stating a fact. A missing key would
    leave a consumer deciding for itself whether it meant zero or unknown, and
    on a verification surface that guess is the whole failure this product
    exists to refuse.
    """
    _seed(initialized_config)

    wire = _call(initialized_config, "get_coverage_report", {})["structuredContent"]

    assert wire["rows"], "the fixture produced no rows, so this asserts nothing"
    for row in wire["rows"]:
        assert row["stranded_holds"] == 0, row
        assert "oldest_stranded_hold" in row, f"the key was dropped rather than nulled: {row}"
        assert row["oldest_stranded_hold"] is None, row


def test_the_coverage_report_derives_its_calendar_day_once(
    initialized_config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 One `today` for the whole report, proved with a clock that MOVES.

    `coverage_report` measures stranded holds against a cutoff and its coverage
    rows against a `today`, and it used to read the clock separately for each --
    thirty-two lines apart, under a comment asserting that one `today` covers
    every row. A report assembled across midnight then answered about two
    different days in one payload, with nothing in it to say so.

    🔴 A frozen clock cannot see this: two reads of a stopped clock agree, so
    such a test passes whether the code reads once or twice. This clock advances
    a full day per read, which is what makes a second read observable at all.

    **Two reads are correct and the count is the discriminator.** One derives the
    report's calendar day; one is `_answer`'s `as_of`, the instant the envelope
    was produced -- a different fact, and not a calendar day. The defect made
    three. The change that flips this test: derive a calendar day from the clock
    a second time anywhere in `coverage_report` instead of passing the one
    `today` down.
    """
    _seed(initialized_config)
    # Old enough to stay stranded on any day this clock reports, so a
    # disagreement between reads shows up as a moved cutoff and never as the
    # hold dropping out of the answer entirely.
    _hold(initialized_config, "hold-ancient", "25.00", days_ago=120)

    # Reached through the module rather than the symbol, because monkeypatching must
    # replace what `query` itself calls, not this test's own reference to it.
    real = query.now_utc  # type: ignore[attr-defined]
    reads: list[Any] = []

    def a_clock_that_advances_one_day_per_read() -> Any:
        reads.append(None)
        return real() + timedelta(days=len(reads) - 1)

    monkeypatch.setattr(query, "now_utc", a_clock_that_advances_one_day_per_read)

    coverage = _call(initialized_config, "get_coverage_report", {})["structuredContent"]

    assert len(reads) == 2, (
        f"`coverage_report` read the clock {len(reads)} times; exactly two are accounted for -- "
        f"the report's own calendar day, and `_answer`'s `as_of` instant. Every extra read can "
        f"land on a different day, so the report answers about two days at once, which is what "
        f"its own comment says must not happen. Hoist the one `today` above its first reader."
    )
    stranded = [row for row in coverage["rows"] if row["stranded_holds"]]
    assert stranded, "the fixture stopped reaching the stranded path; this guard now proves nothing"


def test_a_stranded_hold_on_a_non_active_account_is_counted_but_not_asked_about(
    initialized_config: Config,
) -> None:
    """🔴 AC-12.7's reasoning applied to its sibling criterion.

    A closed account's hold can never settle and can never be cleared, so
    naming it as something to go look at is the finding no operator can ever
    action -- the exact shape AC-12.7 removed from `silence_exceeds_cadence`.
    What is withheld is only the call to action: the COUNT stays as measured,
    on the same reasoning that leaves `silence_ratio` measured while the flag
    beside it goes false. Both halves are asserted, because suppressing the
    count as well would hide the measurement, which is the opposite error and
    just as available.
    """
    _seed(initialized_config)
    _hold(
        initialized_config,
        "hold-on-closed",
        "17.00",
        days_ago=query.STRANDED_HOLD_AFTER_DAYS + 30,
    )

    before = _call(initialized_config, "get_coverage_report", {})["structuredContent"]
    holding = [row for row in before["rows"] if row["stranded_holds"]]
    assert len(holding) == 1, "the fixture did not produce the stranded hold it needs"
    account_id = holding[0]["account_id"]
    assert holding[0]["oldest_stranded_hold"] is not None, (
        "the account is still active here, so the call to action must be present"
    )

    with writer_connection(initialized_config) as conn:
        conn.execute(
            accounts.update()
            .where(accounts.c.account_id == account_id)
            .values(lifecycle_status="inactive")
        )

    after = _call(initialized_config, "get_coverage_report", {})["structuredContent"]
    row = next(r for r in after["rows"] if r["account_id"] == account_id)

    assert row["stranded_holds"] == 1, (
        "the count was suppressed along with the call to action; the measurement is "
        "evidence and hiding it is the opposite error"
    )
    assert row["oldest_stranded_hold"] is None, (
        "a closed account was handed a hold to go look at, which no operator can clear"
    )


def test_a_hold_that_expired_long_ago_is_not_reported_as_stranded(
    initialized_config: Config,
) -> None:
    """🔴 A withdrawn hold is finished, not stranded, and the two are opposites.

    An expired hold is retained with `removed_at` set and `pending` left at 1 --
    that retention is exactly what the `expired_holds` tally reads. So a
    stranded-hold query that omits the soft-delete exclusion reports every
    long-expired hold as one still outstanding, and the two surfaces then say
    contradictory things about the same transaction: `money_summary` publishes
    it as expired while `get_coverage_report` tells the operator to go look at
    money the institution already took back.

    The age here is past the stranded threshold on purpose. A hold that expires
    quickly could never reach the cutoff, so the only fixture that can catch
    this is one where the hold is BOTH old enough to be called stranded and
    already withdrawn.
    """
    _seed(initialized_config)
    _hold(
        initialized_config,
        "hold-expired-old",
        "31.00",
        days_ago=query.STRANDED_HOLD_AFTER_DAYS + 40,
    )
    _page(initialized_config, removed=[_removal("hold-expired-old")], cursor="cursor-expire")

    coverage = _call(initialized_config, "get_coverage_report", {})["structuredContent"]
    summary = _totals(_call(initialized_config, "money_summary", {})["structuredContent"])

    assert summary["expired_holds"] == 1, "the fixture did not produce the expired hold it needs"
    for row in coverage["rows"]:
        assert row["stranded_holds"] == 0, (
            f"a hold the institution withdrew is being reported as still outstanding: {row}"
        )
        assert row["oldest_stranded_hold"] is None, (
            f"the operator is being sent after money that was already taken back: {row}"
        )
