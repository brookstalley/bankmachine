"""What a capped answer says about the rows it did not return.

🔴 **The invariants come first in this file, and that ordering is the point.**
`learnings.md` records the window resolver surviving nine mutations against a
hand-built boundary matrix while three real bugs stayed live: a matrix is
written by the same mind, at the same sitting, from the same mental model as the
code, so it reproduces the code's blind spot. The escape is to assert the RULE
rather than enumerate instances — `truncated` iff `returned < matching` cannot be
written from a mental model of which requests truncate, so it does not inherit
one.

The matrix below the invariants is still worth having; it is just not the part
that catches the case nobody thought of.

Assertions compare warning kinds by equality on the whole list, never by `in` on
a joined string: `rows_truncated` sits in a vocabulary beside two kinds sharing a
`window_` prefix, and substring containment cannot tell near-neighbours apart.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import and_, event, func, or_, select
from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect
from sqlalchemy.engine import Engine

from bankmachine import envelope, query
from bankmachine.config import Config
from bankmachine.connector import ACCOUNTS_GET, TRANSACTIONS_SYNC
from bankmachine.derivers import ALL_DERIVERS
from bankmachine.envelope import MAX_ROWS, Cursor, Truncation
from bankmachine.store.derivation import apply_response
from bankmachine.store.engine import reader_connection, writer_connection
from bankmachine.store.schema import connections, institutions, transactions
from bankmachine.store.types import now_utc

# --------------------------------------------------------------------------
# The invariants — written before the matrix, and the reason for the ordering
# --------------------------------------------------------------------------

#: Any two row counts where the answer is possible at all. `returned > matching`
#: is excluded here because it is not a truncation, it is a contradiction, and it
#: has its own test one function down.
_POSSIBLE = st.integers(min_value=0, max_value=10_000).flatmap(
    lambda matching: st.tuples(st.integers(min_value=0, max_value=matching), st.just(matching))
)

#: 🔴 Every invariant below is drawn over BOTH resume states, because the block
#: now has two: a page with a row to continue from and a page without one. A
#: property asserted only at `resume_from=None` would say nothing about the
#: branch that actually ships on a truncated answer — and the remedy sentence,
#: which one of these properties exists to police, is exactly the thing that
#: changes between them.
_A_CURSOR = Cursor.issued_for(
    posted_date=date(2026, 5, 4),
    transaction_id=7,
    since=None,
    until=None,
    account_id=None,
)
_RESUME = st.sampled_from([None, _A_CURSOR])


@given(counts=_POSSIBLE, resume_from=_RESUME)
def test_truncated_is_true_exactly_when_rows_are_missing(
    counts: tuple[int, int], resume_from: Cursor | None
) -> None:
    """The whole contract of the block, as a rule rather than as examples.

    A caller branches on `truncated` and never re-derives it, so a `truncated`
    that disagreed with its own two numbers would be believed over them.
    """
    returned, matching = counts
    truncation = Truncation.over(returned=returned, counted=matching, resume_from=resume_from)

    assert truncation.truncated == (returned < matching)
    assert truncation.returned <= truncation.matching


@given(counts=_POSSIBLE, resume_from=_RESUME)
def test_a_caveat_rides_every_truncated_answer_and_no_complete_one(
    counts: tuple[int, int], resume_from: Cursor | None
) -> None:
    """🔴 The generalisation of this work cycle's defect: a missing row that says nothing.

    Stated as an iff in both directions on purpose. The forward half is the
    defect being fixed — rows silently absent. The reverse half is the defect the
    FIX would introduce: a caveat on a complete answer fires on every response,
    and measurement already showed what that does to a reader. A warning that is
    always there is one nobody reads, which is how the `gapped` notice became
    invisible.
    """
    returned, matching = counts
    truncation = Truncation.over(returned=returned, counted=matching, resume_from=resume_from)

    kinds = [c.kind for c in truncation.caveats]

    assert kinds == (["rows_truncated"] if truncation.truncated else [])


@given(counts=_POSSIBLE, resume_from=_RESUME)
def test_the_caveat_never_quotes_a_figure_the_block_beside_it_denies(
    counts: tuple[int, int], resume_from: Cursor | None
) -> None:
    """The prose and the structured field cannot contradict each other.

    The window resolver's own bug was a sentence claiming coverage beside an
    `effective` of null. This is the same class one field over: the sentence
    quotes three numbers, and a consumer reads the sentence precisely when the
    numbers look wrong.
    """
    returned, matching = counts
    truncation = Truncation.over(returned=returned, counted=matching, resume_from=resume_from)

    for caveat in truncation.caveats:
        assert f"{matching} transactions match" in caveat.detail
        assert f"newest {returned} are returned" in caveat.detail
        assert f"{matching - returned} are missing" in caveat.detail


@given(
    returned=st.integers(min_value=1, max_value=1000),
    removed=st.integers(min_value=1, max_value=1000),
)
def test_a_count_that_lags_the_rows_is_reconciled_rather_than_refused(
    returned: int, removed: int
) -> None:
    """🔴 A write landing mid-answer is a data condition, not an impossible one.

    The read handle is opened in autocommit — `store/connection.py`: "every
    statement is its own snapshot" — so the row query and the count are two
    snapshots, and the scheduled sync writer soft-deletes transactions while the
    MCP reader may be mid-query. A row counted in the first statement and removed
    before the second makes the count come back *below* the rows already in hand.

    An earlier version of this code called that impossible and raised. It is
    reachable, and raising would have refused a perfectly good question because a
    nightly sync landed mid-query — turning a harmless skew into a failed tool
    call, which is exactly what `api-contract.md` § Direction forbids.

    `matching` floors at `returned` because those rows were observed to match;
    reporting fewer would contradict the payload beside it. Nothing is hidden:
    the skew rides out as its own caveat.
    """
    counted = max(0, returned - removed)
    truncation = Truncation.over(returned=returned, counted=counted, resume_from=None)

    assert truncation.matching == returned, "a count below the rows contradicts the payload"
    assert truncation.truncated is False, "no rows are being hidden when the count lags"
    assert truncation.counted_during_change is True
    assert [c.kind for c in truncation.caveats] == ["counted_during_change"]


def test_a_count_that_matches_or_exceeds_the_rows_reports_no_change() -> None:
    """The reverse half: the ordinary case must not raise the skew warning.

    A caveat that fired on every answer would be the invariant `gapped` notice
    all over again — measurement already showed what that does to a reader.
    """
    for returned, counted in ((0, 0), (3, 3), (100, 144), (500, 500)):
        truncation = Truncation.over(returned=returned, counted=counted, resume_from=None)

        assert truncation.counted_during_change is False, (returned, counted)
        assert "counted_during_change" not in [c.kind for c in truncation.caveats]


@given(counts=_POSSIBLE, resume_from=_RESUME)
def test_the_remedy_is_one_the_caller_can_actually_follow(
    counts: tuple[int, int], resume_from: Cursor | None
) -> None:
    """🔴 A sentence that tells a caller to raise `limit` past a cap it already hit.

    Found by hand-probing reachable inputs, not by a mutation: at the ceiling the
    caveat read "the newest 500 are returned ... raise `limit` (at most 500)",
    which is unusable advice sitting beside a payload that contradicts it. That
    is precisely the shape this work cycle exists to remove — a well-formed,
    plausible sentence whose own numbers deny it — and every test passed while it
    was there.

    Stated as a rule over the whole space rather than at the two boundary values,
    because the boundary is the thing most likely to move.
    """
    returned, matching = counts
    truncation = Truncation.over(returned=returned, counted=matching, resume_from=resume_from)

    for caveat in truncation.caveats:
        offers_a_bigger_limit = "raise `limit`" in caveat.detail
        assert offers_a_bigger_limit == (returned < MAX_ROWS), (
            f"returned={returned} against a ceiling of {MAX_ROWS}: "
            f"{'offered' if offers_a_bigger_limit else 'withheld'} a limit the caller "
            f"{'cannot' if offers_a_bigger_limit else 'could'} use"
        )


def test_the_remedy_names_the_ceiling_the_code_enforces() -> None:
    """A ceiling written into a sentence decays; this one is derived.

    `learnings.md` records a recorded fingerprint (`limit:9999` → "at most
    1000") falsified within a day by a commit moving the ceiling to 500. The
    caveat tells a caller what to raise `limit` to, so it must read the same
    constant the query clamps against.
    """
    detail = Truncation.over(returned=1, counted=2, resume_from=None).caveats[0].detail

    assert f"at most {MAX_ROWS}" in detail


# --------------------------------------------------------------------------
# The guard: the row query and the count are built from ONE predicate list
# --------------------------------------------------------------------------


@pytest.fixture
def captured_sql() -> Iterator[list[str]]:
    """Every statement the engine actually executes, in order.

    🔴 Captured from the real execution rather than rebuilt in the test. A guard
    that constructed the two statements itself would compare the test's idea of
    the predicates with itself and agree forever — `learnings.md` § *Two
    descriptions, compared*: an oracle derived from the thing it checks teaches
    nothing.
    """
    statements: list[str] = []

    def _capture(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(Engine, "before_cursor_execute", _capture)
    try:
        yield statements
    finally:
        event.remove(Engine, "before_cursor_execute", _capture)


def _normalized(statement: str) -> str:
    """One line, single-spaced.

    🔴 Normalize BEFORE splitting, never after. SQLAlchemy compiles with
    newlines — `count_1 \nFROM transactions \nWHERE ...` — so partitioning the
    raw text on `" WHERE "` matches nothing and silently yields an empty string
    for every input. That is how the first version of this guard passed every
    mutation applied to it: it compared "" with "" and agreed. A parse that can
    fail open is worse than no parse, which is why both extractors below refuse
    to return an empty result.
    """
    return " ".join(statement.split())


def _where_of(statement: str) -> str:
    _, marker, tail = _normalized(statement).partition(" WHERE ")
    assert marker, f"no WHERE clause found in: {statement}"
    # `ORDER BY`/`LIMIT` belong to the row query alone and are not predicates.
    for terminator in (" ORDER BY ", " LIMIT "):
        tail = tail.partition(terminator)[0]
    assert tail, f"empty WHERE clause parsed from: {statement}"
    return tail


def _from_of(statement: str) -> str:
    head, _, _ = _normalized(statement).partition(" WHERE ")
    _, marker, tail = head.partition(" FROM ")
    assert marker and tail, f"no FROM clause found in: {statement}"
    return tail


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"since": date(2026, 1, 1)},
        {"until": date(2026, 6, 30)},
        {"account_id": 1},
        {"since": date(2026, 1, 1), "until": date(2026, 6, 30), "account_id": 1},
        # 🔴 The keyset predicate is in this list too, and it is the one whose
        # absence from the count would be invisible: a count taken over the whole
        # result set behind every page reports `truncated` true on the last page
        # forever, so a caller paging until it goes false never stops.
        {"after": _A_CURSOR},
        {"since": date(2026, 1, 1), "account_id": 1, "after": _A_CURSOR},
    ],
)
def test_the_count_and_the_row_query_select_from_the_same_predicates(
    seeded_config: Config, captured_sql: list[str], arguments: dict[str, Any]
) -> None:
    """🔴 The failure this chunk is most likely to ship, and it would be silent.

    Two statements that must agree, edited by different hands at different times:
    a filter added to the rows and forgotten in the count reports a `matching`
    that is too large and a `truncated` that is a lie — a precise wrong number,
    which is worse than the vague one this chunk removes. This asserts they were
    built from one list by reading what the engine actually ran.

    Parametrized across the filter combinations because a single unfiltered call
    cannot discriminate the rule: with no `WHERE` beyond the soft-delete clause,
    a divergent builder and a correct one emit the same SQL.
    """
    query.list_transactions(seeded_config, **arguments)

    # 🔴 The count is taken as the first one AFTER the row query, not the first
    # in the run. `_coverage` issues its own counts over `transactions` a moment
    # later, and one of them carries only the soft-delete clause — which happens
    # to equal the row query's WHERE when no filter is set, so a looser search
    # could pass here by comparing the wrong statement against itself.
    rows_at = next(i for i, sql in enumerate(captured_sql) if "transactions.description" in sql)
    count_at = next(i for i, sql in enumerate(captured_sql) if i > rows_at and "count(*)" in sql)
    rows_sql, count_sql = captured_sql[rows_at], captured_sql[count_at]

    if "after" in arguments:
        # 🔴 The comparison below fails OPEN if the cursor reached NEITHER
        # statement: two identical predicate lists agree whether or not they
        # carry the clause this case exists to check. `learnings.md` records
        # exactly this shape — an extraction that can quietly find nothing turns
        # a strict equality into a tautology — so the operand is checked before
        # it is compared.
        assert "posted_date < ?" in _where_of(rows_sql), (
            "the keyset predicate never reached the row query, so the comparison "
            "below would agree about a filter neither statement has"
        )

    assert _where_of(count_sql) == _where_of(rows_sql), (
        "the count and the row query were built from different predicates"
    )
    assert _from_of(count_sql) == _from_of(rows_sql), (
        "the count and the row query read different tables, so one can admit a row the other drops"
    )


def _shared_predicate_texts(
    *, since: date, until: date, include_removed: bool = False
) -> list[str]:
    """The predicate fragments every reader must carry, read off the one list itself.

    Derived from `_transaction_filters` rather than retyped, so a filter added
    there joins this assertion without anyone remembering to widen it — the same
    reason the production readers compose from it.
    """
    # 🔴 Compiled against the SQLite dialect, so the fragments carry the same `?`
    # placeholders the engine emits. Compiling with literal binds produces
    # `posted_date >= '2026-08-04'`, which matches no executed statement and would
    # make every assertion below fail for the wrong reason — or, had the
    # comparison been laxer, pass for one.
    return [
        str(clause.compile(dialect=sqlite_dialect()))
        for clause in query._transaction_filters(
            since=since, until=until, account_id=None, after=None, include_removed=include_removed
        )
    ]


def test_every_reader_of_transactions_shares_the_one_predicate_list(
    seeded_config: Config, captured_sql: list[str]
) -> None:
    """🔴 The promise is "one place to add a filter", so every reader must use it.

    `_transaction_filters` was introduced for `list_transactions`'s two
    statements, and for a while that was all it covered: `money_summary`
    and `_covered_rows` each rebuilt the soft-delete and window clauses by hand,
    so there were three places to add a filter and the docstring's promise held
    only for the pair it was written about. A maintainer adding a predicate at
    the named single place would get it in the rows and the count and silently
    not in the other two — two tools disagreeing about which rows exist, which is
    a precise wrong number rather than an error.

    Asserted by reading the SQL the engine actually ran, across the READERS
    rather than across one tool's argument shapes.

    🔴 The window is chosen INSIDE the fixture's data on purpose. A window the
    store cannot cover makes `_covered_rows` short-circuit before it runs, so the
    reader this test exists to catch would never appear in the captured SQL and
    the test would pass while checking one fewer reader than it names.

    🔴 **There are exactly TWO admissible treatments of the soft delete, and a
    reader must take one of them explicitly** (AC-13.4). Almost every reader
    excludes removed rows; the expired-hold tally deliberately reads only removed
    rows, because a hold that was withdrawn without ever posting left the totals
    by being soft-deleted and its exit has to stay attributable. That reader
    still composes from `_transaction_filters` — with `include_removed=True`, so
    a filter added at the one named place reaches it too — and the assertion
    below names both treatments rather than admitting anything. A reader that
    simply forgot the soft delete carries NEITHER fragment and still fails, which
    is the case this test was written for.
    """
    today = now_utc().date()
    since, until = today - timedelta(days=_DAYS // 2), today
    fragments = _shared_predicate_texts(since=since, until=until)
    shared = _shared_predicate_texts(since=since, until=until, include_removed=True)
    # The one fragment the two lists differ by, derived rather than typed: it is
    # what tells a reader that excludes removed rows from one that reads them.
    (excludes_removed,) = [fragment for fragment in fragments if fragment not in shared]
    reads_removed = excludes_removed.replace("IS NULL", "IS NOT NULL")
    assert reads_removed != excludes_removed, (
        f"the soft-delete predicate no longer spells IS NULL ({excludes_removed!r}), so the "
        f"inversion below names a clause no reader can emit and would admit any reader"
    )

    query.list_transactions(seeded_config, since=since, until=until, limit=MAX_ROWS)
    query.money_summary(seeded_config, since=since, until=until)

    windowed = [
        sql
        for sql in captured_sql
        if "transactions" in sql and "posted_date >=" in sql and "posted_date <=" in sql
    ]
    # The row query, its count, one coverage count per windowed call, the
    # aggregate, and the two hold tallies. Asserted as a floor so an added reader
    # cannot slip past.
    assert len(windowed) >= 5, (
        f"expected at least 5 windowed reads of `transactions` across the two calls; "
        f"saw {len(windowed)} — a reader is missing, or one short-circuited"
    )
    for sql in windowed:
        where = _where_of(sql)
        for fragment in shared:
            assert fragment in where, (
                f"a reader rebuilt its predicates by hand: {fragment!r} missing from {where}"
            )
        assert excludes_removed in where or reads_removed in where, (
            f"a reader took no position on the soft delete: neither {excludes_removed!r} nor "
            f"{reads_removed!r} in {where}"
        )


# --------------------------------------------------------------------------
# The store, and the invariant asserted over every request it can answer
# --------------------------------------------------------------------------

#: Two accounts, so an `account_id` filter has something to exclude — a fixture
#: holding one instance of a shape cannot discriminate a rule about the shape.
#: 140 transactions, so the DOCUMENTED DEFAULT of 100 truncates: that default is
#: the measured harm this chunk exists to remove, and a fixture smaller than it
#: could not reproduce it.
_DAYS = 70
_PER_DAY = 2
_TOTAL = _DAYS * _PER_DAY


def _accounts_body() -> bytes:
    return json.dumps(
        {
            "accounts": [
                {
                    "account_id": f"acct-{n}",
                    "name": name,
                    "mask": f"000{n}",
                    "type": "depository" if n == 1 else "credit",
                    "subtype": "checking" if n == 1 else "credit card",
                    "balances": {
                        "current": "110.94",
                        "available": "100.00",
                        "limit": None,
                        "iso_currency_code": "USD",
                    },
                }
                for n, name in ((1, "Platypus Checking"), (2, "Platypus Card"))
            ],
            "item": {"item_id": "item-truncation"},
            "request_id": "req-accounts",
        }
    ).encode()


def _sync_body(last_day: date) -> bytes:
    """`_TOTAL` transactions spread backwards over `_DAYS` days, alternating accounts.

    Dates are spread rather than piled on one day so that a window bounds a
    COUNT rather than selecting all or nothing — a fixture dated entirely today
    cannot tell a window-scoped count from a store-wide one.
    """
    added = []
    for index in range(_TOTAL):
        day = last_day - timedelta(days=index // _PER_DAY)
        added.append(
            {
                "account_id": f"acct-{(index % 2) + 1}",
                "transaction_id": f"t{index}",
                "amount": "10.00",
                "iso_currency_code": "USD",
                "date": str(day),
                "authorized_date": None,
                "pending": False,
                "pending_transaction_id": None,
                "name": f"Merchant {index}",
                "merchant_name": None,
                "personal_finance_category": {
                    "primary": "GENERAL_MERCHANDISE",
                    "detailed": "GENERAL_MERCHANDISE",
                },
            }
        )
    return json.dumps(
        {
            "accounts": [],
            "added": added,
            "modified": [],
            "removed": [],
            "next_cursor": "cursor-1",
            "has_more": False,
            "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE",
            "request_id": "req-sync",
        }
    ).encode()


@pytest.fixture
def seeded_config(initialized_config: Config) -> Config:
    """A store with more transactions than the default limit returns.

    🔴 Derived through the real derivers rather than hand-inserted, for the
    reason `tests/test_mcp.py` records: the schema enforces provenance with a
    CHECK, so hand-built rows either encode an assumption about that constraint
    or fail on it, and either way stop reflecting what the product writes.
    """
    now = now_utc()
    with writer_connection(initialized_config) as conn:
        institution_pk = conn.execute(
            institutions.insert().values(
                source_institution_id="ins_109508",
                name="First Platypus Bank",
                first_seen_at=now,
                last_seen_at=now,
            )
        ).inserted_primary_key
        assert institution_pk is not None
        conn.execute(
            connections.insert().values(
                institution_id=int(institution_pk[0]),
                source_connection_id="item-truncation",
                credential_ref="connection:sandbox:item-truncation",
                capabilities="[]",
                requested_history_days=730,
                granted_history_days=730,
                status="active",
                last_success_at=now,
                enrolled_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        for endpoint, body in (
            (ACCOUNTS_GET.path, _accounts_body()),
            (TRANSACTIONS_SYNC.path, _sync_body(now.date())),
        ):
            apply_response(
                conn,
                connection_id=1,
                endpoint=endpoint,
                body=body,
                received_at=now,
                derivers=ALL_DERIVERS,
            )
    return initialized_config


def _oracle_count(
    config: Config,
    *,
    since: date | None,
    until: date | None,
    account_id: int | None,
    after: Cursor | None,
) -> int:
    """The matching count, derived independently of the code under test.

    🔴 Written against the table directly rather than through
    `_transaction_filters`. Sharing the builder would make this agree with the
    implementation by construction and detect nothing, which is precisely the
    self-derived oracle `learnings.md` warns produces a test that "would have
    agreed with itself forever".

    The keyset half is spelled out here for the same reason, and it is the half
    most worth writing twice: "everything after this row" has two orderings that
    look alike — strictly older, or the same day and further down the tie-break —
    and an oracle that borrowed the implementation's version could not tell them
    apart.
    """
    clauses: list[Any] = [transactions.c.removed_at.is_(None)]
    if since is not None:
        clauses.append(transactions.c.posted_date >= since)
    if until is not None:
        clauses.append(transactions.c.posted_date <= until)
    if account_id is not None:
        clauses.append(transactions.c.account_id == account_id)
    if after is not None:
        clauses.append(
            or_(
                transactions.c.posted_date < after.posted_date,
                and_(
                    transactions.c.posted_date == after.posted_date,
                    transactions.c.transaction_id < after.transaction_id,
                ),
            )
        )
    with reader_connection(config) as conn:
        return int(
            conn.execute(
                select(func.count()).select_from(transactions).where(*clauses)
            ).scalar_one()
        )


def _resume_midway(since: date | None, until: date | None, account_id: int | None) -> Cursor:
    """A cursor landing partway through the fixture, issued for the request given.

    Built directly rather than read off a first page: a cursor taken from the
    implementation's own answer would place the grid's second half wherever the
    implementation chose to place it, and the invariants would then be checked
    at a position the code picked for itself. A position of `transaction_id`
    `_TOTAL // 2` on a mid-fixture date also sits INSIDE a day the fixture wrote
    two rows on, so the tie-break carries it.
    """
    return Cursor.issued_for(
        posted_date=now_utc().date() - timedelta(days=_DAYS // 3),
        transaction_id=_TOTAL // 2,
        since=since,
        until=until,
        account_id=account_id,
    )


def test_the_truncation_invariant_holds_over_every_request_this_store_can_answer(
    seeded_config: Config,
) -> None:
    """🔴 The rule, over the whole reachable input space rather than over chosen cases.

    The grid supplies the inputs; the assertions are invariants, so a
    combination nobody predicted fails here without anyone having predicted it.
    Four rules, each of which has a way to be violated silently:

    - `returned` equals the rows actually in the payload (not the limit asked for)
    - `matching` equals an independently derived count
    - `truncated` iff rows are missing
    - a `rows_truncated` warning rides every truncated answer and no complete one
    """
    today = now_utc().date()
    boundaries = [
        None,
        today,
        today - timedelta(days=_DAYS // 2),
        today - timedelta(days=_DAYS * 2),
    ]
    # Around the cap, at the documented default, at the contract ceiling, and
    # 🔴 on both sides of the clamp `list_transactions` applies to `limit`
    # itself. Without a limit ABOVE `MAX_ROWS`, `returned` read off the caller's
    # `limit` rather than off the rows is indistinguishable from the truth —
    # every value inside the clamp's range makes the two agree.
    limits = [0, 1, 2, _PER_DAY, 100, _TOTAL - 1, _TOTAL, _TOTAL + 1, MAX_ROWS, MAX_ROWS + 50]

    checked = 0
    for since in boundaries:
        for until in boundaries:
            if since is not None and until is not None and until < since:
                continue
            for account_id in (None, 1, 2):
                # 🔴 The cursor is a dimension of the grid, not a separate test.
                # It narrows the request the way `since` does, so every invariant
                # above has to hold on page two as well — and a grid that only
                # ever asked for page one would still be exercising the whole
                # input space it was written for, while the function had grown a
                # second half.
                for after in (None, _resume_midway(since, until, account_id)):
                    for limit in limits:
                        answer = query.list_transactions(
                            seeded_config,
                            since=since,
                            until=until,
                            account_id=account_id,
                            limit=limit,
                            after=after,
                        )
                        where = (
                            f"since={since} until={until} account={account_id} "
                            f"limit={limit} after={after}"
                        )
                        truncation = answer.truncation
                        assert truncation is not None, where

                        assert truncation.returned == len(answer.rows), where
                        assert truncation.matching == _oracle_count(
                            seeded_config,
                            since=since,
                            until=until,
                            account_id=account_id,
                            after=after,
                        ), where
                        assert truncation.truncated == (len(answer.rows) < truncation.matching), (
                            where
                        )

                        kinds = [
                            w.kind
                            for w in answer.warnings
                            if w.kind in envelope.REQUEST_SCOPED_KINDS
                        ]
                        assert ("rows_truncated" in kinds) == truncation.truncated, where
                        # A cursor is offered exactly when there is a next page
                        # to reach, which is the loop condition every consumer
                        # writes against.
                        assert (truncation.next_cursor is not None) == (
                            truncation.truncated and bool(answer.rows)
                        ), where
                        checked += 1

    assert checked > 100, "the grid collapsed; it is no longer exercising the invariant"


def test_a_capped_answer_returns_the_limit_and_says_how_many_it_left_behind(
    seeded_config: Config,
) -> None:
    """The measured harm, stated as the acceptance criterion states it.

    An acceptance round found the documented default of 100 silently dropping
    ~16 months of one account's history, with a caller summing the result
    understating a two-year total by roughly 40%.
    """
    answer = query.list_transactions(seeded_config, limit=100)
    assert answer.truncation is not None

    assert answer.truncation.returned == 100
    assert answer.truncation.matching == _TOTAL
    assert answer.truncation.truncated is True
    assert answer.truncation.matching > answer.truncation.returned


def test_an_untruncated_answer_reports_false_and_equal_counts(seeded_config: Config) -> None:
    """The regression the plan names: a complete answer must read as complete."""
    answer = query.list_transactions(seeded_config, limit=MAX_ROWS)
    assert answer.truncation is not None

    assert answer.truncation.truncated is False
    assert answer.truncation.returned == answer.truncation.matching == _TOTAL
    assert [w.kind for w in answer.warnings if w.kind in envelope.REQUEST_SCOPED_KINDS] == []


def test_a_narrow_window_stops_truncating_what_a_wide_one_truncated(
    seeded_config: Config,
) -> None:
    """🔴 `truncated` responds to the WINDOW, not only to `limit`.

    The same limit against two windows must give different answers, or the count
    is being taken over the store rather than over the request — which is the
    bug that would make `matching` a restatement of `coverage.transactions`.
    """
    today = now_utc().date()

    wide = query.list_transactions(seeded_config, limit=20)
    narrow = query.list_transactions(seeded_config, since=today - timedelta(days=4), limit=20)

    assert wide.truncation is not None and narrow.truncation is not None
    assert wide.truncation.truncated is True
    assert narrow.truncation.truncated is False
    assert narrow.truncation.matching == 5 * _PER_DAY


def test_the_two_counts_agree_when_nothing_narrows_them(seeded_config: Config) -> None:
    """🔴 `matching` and the window-scoped coverage figure must not silently disagree.

    They are computed by different statements over different from-clauses —
    `matching` joins `accounts`, the coverage count reads `transactions` alone —
    so an unfiltered request is where a divergence would show as two numbers a
    consumer cannot reconcile. They agree because `transactions.account_id` is a
    NOT NULL foreign key, which means the inner join can never drop a row. That
    reasoning rests on the schema, so it is pinned here rather than trusted: a
    migration making the column nullable breaks this test rather than the
    payload.
    """
    answer = query.list_transactions(seeded_config, limit=MAX_ROWS)

    assert answer.truncation is not None
    assert answer.truncation.matching == answer.coverage["transactions_in_effective_window"]


def test_an_account_filter_narrows_matching_rather_than_only_the_rows(
    seeded_config: Config,
) -> None:
    """A count taken before the account filter would report the store's total."""
    both = query.list_transactions(seeded_config, limit=MAX_ROWS)
    one = query.list_transactions(seeded_config, account_id=1, limit=MAX_ROWS)

    assert both.truncation is not None and one.truncation is not None
    assert one.truncation.matching == _TOTAL // 2
    assert one.truncation.matching < both.truncation.matching


# --------------------------------------------------------------------------
# The coverage sibling — a new key, never a narrowing of the old one
# --------------------------------------------------------------------------


def test_the_window_scoped_count_is_a_sibling_and_leaves_the_store_wide_one_alone(
    seeded_config: Config,
) -> None:
    """🔴 The single easiest mistake in this plan, pinned.

    `api-contract.md` forbids repurposing a field in red: a consumer still
    reading `coverage.transactions` after it silently became window-scoped gets a
    wrong answer rather than an error. So the window-scoped figure is a NEW key,
    and this asserts the two are different numbers for a window that excludes
    part of the store.
    """
    today = now_utc().date()

    answer = query.list_transactions(seeded_config, since=today - timedelta(days=4), limit=MAX_ROWS)

    assert answer.coverage["transactions"] == _TOTAL, (
        "the store-wide count was narrowed to the window — the repurpose the contract forbids"
    )
    assert answer.coverage["transactions_in_effective_window"] == 5 * _PER_DAY


def test_the_window_scoped_count_ignores_the_account_filter(seeded_config: Config) -> None:
    """Coverage is a fact about the store, and per-account coverage is its own issue (#19).

    Half of it shipped here would leave that work amending a field this chunk
    just added.
    """
    answer = query.list_transactions(seeded_config, account_id=1, limit=MAX_ROWS)

    assert answer.coverage["transactions_in_effective_window"] == _TOTAL
    assert answer.truncation is not None
    assert answer.truncation.matching == _TOTAL // 2


def test_an_unwindowed_tool_reports_no_window_scoped_count(seeded_config: Config) -> None:
    """The key's absence means "this tool takes no window", exactly as `effective_window`'s does."""
    answer = query.list_accounts(seeded_config)

    assert answer.effective_window is None
    assert "transactions_in_effective_window" not in answer.coverage
    assert answer.coverage["transactions"] == _TOTAL


def test_a_window_covering_nothing_counts_nothing_and_says_why(seeded_config: Config) -> None:
    """A zero that is explained, which is the whole subject of this work cycle.

    The count is genuinely zero, and the caveat beside it is what stops the zero
    reading as "you had no transactions".
    """
    answer = query.list_transactions(
        seeded_config, since=date(2020, 1, 1), until=date(2020, 12, 31)
    )

    assert answer.effective_window is not None
    assert answer.effective_window.covers_nothing
    assert answer.coverage["transactions_in_effective_window"] == 0
    assert [w.kind for w in answer.warnings if w.kind in envelope.REQUEST_SCOPED_KINDS] == [
        "window_starts_before_coverage"
    ]


# --------------------------------------------------------------------------
# Where the block must NOT appear
# --------------------------------------------------------------------------


def test_an_aggregate_carries_no_truncation_block(seeded_config: Config) -> None:
    """🔴 `api-contract.md` fixes aggregates as unpaginated, bounded by the grouping.

    A truncation block on a spending summary would describe a cap it does not
    have, and `truncated: false` on every response is the invariant warning
    measurement already showed a reader learns to skip.
    """
    answer = query.money_summary(seeded_config)

    assert answer.truncation is None
    assert "truncation" not in answer.to_wire()


@pytest.mark.parametrize("tool", ["list_accounts", "pipeline_health"])
def test_an_uncapped_tool_carries_no_truncation_block(seeded_config: Config, tool: str) -> None:
    """Absence says "this tool returns everything it found", and here that is true."""
    answer = getattr(query, tool)(seeded_config)

    assert answer.truncation is None
    assert "truncation" not in answer.to_wire()


@pytest.mark.parametrize("tool", ["list_transactions", "money_summary"])
def test_an_unreadable_store_does_not_move_the_windowed_wire_shape(
    config: Config, tool: str
) -> None:
    """🔴 The key set a consumer branches on must not depend on the store's health.

    🔴 The unreadable-store path is where a key is easiest to drop and hardest
    to notice, because every other assertion in this file runs against an
    initialized store. A consumer branching on the key would otherwise read
    "this tool takes no window" precisely when the datastore cannot be read —
    the one moment the shape must not move.
    """
    answer = getattr(query, tool)(config)

    assert answer.effective_window is not None, tool
    assert answer.coverage["transactions_in_effective_window"] == 0, tool
    assert answer.coverage["transactions"] == 0, tool


def test_an_unreadable_store_reports_no_window_sibling_for_an_unwindowed_tool(
    config: Config,
) -> None:
    """The other half: absence still means "this tool takes no window"."""
    answer = query.list_accounts(config)

    assert answer.effective_window is None
    assert "transactions_in_effective_window" not in answer.coverage


def test_a_capped_tool_still_reports_the_block_when_the_store_cannot_be_read(
    config: Config,
) -> None:
    """🔴 The key's presence is a fact about the TOOL, not about the store.

    Omitting it here would say `query_transactions` returns everything it finds —
    a false statement about the tool — and a consumer branching on the key would
    take the wrong branch precisely when the datastore is unreadable. The zeroes
    are true, and the `partial` warning says what they mean.
    """
    answer = query.list_transactions(config)

    assert answer.truncation is not None
    assert answer.truncation.returned == 0
    assert answer.truncation.matching == 0
    assert answer.truncation.truncated is False
    assert [w.kind for w in answer.warnings] == ["partial"]


def test_a_row_removed_between_the_two_reads_answers_rather_than_failing(
    seeded_config: Config,
) -> None:
    """🔴 The concurrency R-1 named, forced through the real query path.

    Not a unit test of `Truncation.over` — those are above. This drives
    `list_transactions` while a writer commits a soft-delete *between* the row
    query and the count, which is the interleaving the architecture actually
    permits: the read handle is autocommit, so the two statements are two
    snapshots, and the scheduled sync soft-deletes rows while the MCP reader may
    be mid-query.

    An earlier version raised here, on a docstring claiming the case could not
    happen. It can, and the answer a caller gets must still be an answer.
    """
    removed: list[int] = []

    def _remove_one_row_mid_answer(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        # Fire once, immediately after the row query and before the count.
        if removed or "transactions.description" not in statement:
            return
        with writer_connection(seeded_config) as writer:
            target = writer.execute(
                select(transactions.c.transaction_id)
                .where(transactions.c.removed_at.is_(None))
                .order_by(transactions.c.posted_date.desc())
                .limit(1)
            ).scalar_one()
            writer.execute(
                transactions.update()
                .where(transactions.c.transaction_id == target)
                .values(removed_at=now_utc())
            )
        removed.append(int(target))

    event.listen(Engine, "after_cursor_execute", _remove_one_row_mid_answer)
    try:
        answer = query.list_transactions(seeded_config, limit=MAX_ROWS)
    finally:
        event.remove(Engine, "after_cursor_execute", _remove_one_row_mid_answer)

    assert removed, "the interleaving never happened, so this test proved nothing"
    assert answer.truncation is not None

    # The answer exists at all — the point of the finding.
    assert answer.truncation.returned == len(answer.rows) == _TOTAL
    # The count came back one short; `matching` floors at the rows observed.
    assert answer.truncation.matching == _TOTAL
    assert answer.truncation.truncated is False, "no rows are hidden when the count lags"
    assert answer.truncation.counted_during_change is True
    assert "counted_during_change" in [w.kind for w in answer.warnings], (
        "the skew was smoothed away instead of announced"
    )


# --------------------------------------------------------------------------
# The cursor — the route past the cap, and the invariants of a paged walk
# --------------------------------------------------------------------------


@given(
    posted=st.dates(),
    transaction_id=st.integers(min_value=1, max_value=2**53),
    since=st.one_of(st.none(), st.dates()),
    until=st.one_of(st.none(), st.dates()),
    account_id=st.one_of(st.none(), st.integers(min_value=1, max_value=10_000)),
)
def test_a_cursor_survives_its_wire_form_unchanged(
    posted: date,
    transaction_id: int,
    since: date | None,
    until: date | None,
    account_id: int | None,
) -> None:
    """The codec's whole contract, as a rule rather than at one sample value.

    A cursor that came back subtly different — a date off by a day, an id
    truncated by a float round trip — would resume at the wrong row, and the
    walk would silently skip or repeat. That is this work cycle's own defect
    arriving through the fix's door, so it is asserted over the space rather
    than at one point.
    """
    cursor = Cursor.issued_for(
        posted_date=posted,
        transaction_id=transaction_id,
        since=since,
        until=until,
        account_id=account_id,
    )

    assert Cursor.decode(cursor.encode()) == cursor


#: The fingerprint of the unfiltered request every forged payload below claims
#: to belong to. 🔴 Real, not a placeholder: with a WRONG fingerprint each of
#: those payloads is refused by the fingerprint check whatever else is broken in
#: it, so the branch a case exists to exercise is never reached. Removing the
#: bool guard and removing the scheme check both left this test green until the
#: fingerprints were made to match — a check that cannot fail hides every
#: problem in its blast radius, not one.
_UNFILTERED = envelope._request_fingerprint(since=None, until=None, account_id=None)


def _forged(payload: object) -> str:
    """A cursor-shaped string this server never issued."""
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def _forged_depth(depth: int) -> str:
    """A cursor payload nested deeper than the JSON decoder's stack.

    🔴 The one decode failure that is NOT a `ValueError`: `json.loads` raises
    `RecursionError` here, a `RuntimeError`, so a decoder catching only
    `ValueError` lets this cursor escape the refusal path into the boundary's
    broad catch — and the caller is told "internal error, check whether your
    datastore is readable" for a mistake in their own argument.

    The case survives a future decoder that parses this iteratively: the payload
    is an array, so it would then be refused for not being an object. What it
    cannot survive is the guard narrowing back to `ValueError` alone.
    """
    return base64.urlsafe_b64encode(("[" * depth + "]" * depth).encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    ("presented", "why"),
    [
        ("", "empty"),
        (_forged_depth(50_000), "nested past the JSON decoder's recursion limit"),
        ("not a cursor at all", "not base64"),
        (_forged([2026, 1, 1]), "a JSON array rather than an object"),
        (_forged("a string"), "a JSON string rather than an object"),
        (_forged({"d": "2026-01-01", "t": 1, "q": _UNFILTERED}), "no scheme tag"),
        (
            _forged({"v": 2, "d": "2026-01-01", "t": 1, "q": _UNFILTERED}),
            "a scheme this build has never issued",
        ),
        (
            _forged({"v": 1, "d": "2026-02-30", "t": 1, "q": _UNFILTERED}),
            "a date that does not exist",
        ),
        (
            _forged({"v": 1, "d": "2026-01-01", "t": True, "q": _UNFILTERED}),
            "JSON true, which is an int in Python",
        ),
        (_forged({"v": 1, "d": "2026-01-01", "t": "7", "q": _UNFILTERED}), "an id as text"),
        (_forged({"v": 1, "d": "2026-01-01", "t": 1}), "no fingerprint"),
    ],
)
def test_a_cursor_this_server_did_not_issue_is_refused_rather_than_read(
    presented: str, why: str
) -> None:
    """🔴 Refused, never quietly treated as "start from the newest row".

    The silent fallback is the dangerous branch: it returns page one under the
    name of page two, which is a well-formed, plausible, complete-looking answer
    to a question nobody asked — the exact shape this work cycle exists to
    remove. Every rejection path is listed because each is a separate `raise`,
    and a missed one fails by ANSWERING rather than by erroring.

    The refusal names the argument and carries no exception class, module or
    decoder fragment: `api-contract.md` § Error Model keeps internals off the
    wire, and a decoder's internals are the least actionable thing a caller
    could be handed.
    """
    with pytest.raises(envelope.MalformedCursorError) as caught:
        envelope.parse_cursor(presented, since=None, until=None, account_id=None)

    detail = str(caught.value)
    assert detail.startswith("cursor "), f"{why}: the refusal does not name the argument"
    assert "`next_cursor`" in detail, f"{why}: the refusal offers no route back"
    for leak in ("Error", "Traceback", "binascii", "json", "b64", "Cursor("):
        assert leak not in detail, f"{why}: the refusal leaks {leak!r}"


def test_an_absent_cursor_is_the_first_page_rather_than_a_refusal() -> None:
    """The reverse half. A parser that refused `None` would make `cursor` mandatory."""
    assert envelope.parse_cursor(None, since=None, until=None, account_id=None) is None


#: Four requests selecting four different result sets. A cursor is a position
#: INSIDE one of them, so it means nothing in the other three.
_REQUESTS: list[tuple[date | None, date | None, int | None]] = [
    (None, None, None),
    (None, None, 1),
    (date(2026, 1, 1), None, None),
    (date(2026, 1, 1), date(2026, 6, 30), 2),
]


@pytest.mark.parametrize("issued_for", _REQUESTS)
@pytest.mark.parametrize("presented_with", _REQUESTS)
def test_a_cursor_is_usable_only_against_the_request_that_issued_it(
    issued_for: tuple[date | None, date | None, int | None],
    presented_with: tuple[date | None, date | None, int | None],
) -> None:
    """🔴 The foreign cursor that is actually reachable — not a forged one, a stale one.

    Page two's cursor sent with a different `account_id` selects real rows, in
    the right order, and answers a question the caller did not ask: no error, no
    warning, and a payload that reads as a continuation. So the predicate travels
    inside the cursor and is compared here.

    Asserted over the whole cross product rather than over chosen mismatches, so
    the accepting case and the refusing case are decided by one rule instead of
    by two lists written from the same mental model as the code.
    """
    wire = Cursor.issued_for(
        posted_date=date(2026, 5, 4),
        transaction_id=7,
        since=issued_for[0],
        until=issued_for[1],
        account_id=issued_for[2],
    ).encode()

    def _resume() -> Cursor | None:
        return envelope.parse_cursor(
            wire,
            since=presented_with[0],
            until=presented_with[1],
            account_id=presented_with[2],
        )

    if presented_with == issued_for:
        resumed = _resume()
        assert resumed is not None
        assert (resumed.posted_date, resumed.transaction_id) == (date(2026, 5, 4), 7)
        return

    with pytest.raises(envelope.MalformedCursorError):
        _resume()


@given(counts=_POSSIBLE, resume_from=_RESUME)
def test_a_cursor_rides_exactly_the_answers_that_have_a_next_page(
    counts: tuple[int, int], resume_from: Cursor | None
) -> None:
    """🔴 The block's new invariant, and the one a caller's loop condition rests on.

    A consumer pages while the key is there and stops when it is gone. A cursor
    on a complete answer would page past the end of an answer that already held
    everything; a truncated answer without one strands the caller at the cap,
    which is the defect this chunk closes.

    The one honest exception is a page that returned no rows at all — there is
    nothing to resume from — and it is stated as part of the rule rather than
    excused beside it.
    """
    returned, matching = counts
    truncation = Truncation.over(returned=returned, counted=matching, resume_from=resume_from)

    assert (truncation.next_cursor is not None) == (
        truncation.truncated and resume_from is not None
    )
    assert ("next_cursor" in truncation.to_wire()) == (truncation.next_cursor is not None)


def _oracle_ids(
    config: Config,
    *,
    since: date | None,
    until: date | None,
    account_id: int | None,
) -> list[int]:
    """Every matching transaction id, newest first, derived independently.

    🔴 Written against the table rather than through `_transaction_filters`, for
    the reason `_oracle_count` above gives: an oracle sharing the builder it
    checks agrees with itself forever.
    """
    clauses: list[Any] = [transactions.c.removed_at.is_(None)]
    if since is not None:
        clauses.append(transactions.c.posted_date >= since)
    if until is not None:
        clauses.append(transactions.c.posted_date <= until)
    if account_id is not None:
        clauses.append(transactions.c.account_id == account_id)
    with reader_connection(config) as conn:
        return [
            int(row[0])
            for row in conn.execute(
                select(transactions.c.transaction_id)
                .where(*clauses)
                .order_by(
                    transactions.c.posted_date.desc(),
                    transactions.c.transaction_id.desc(),
                )
            ).all()
        ]


def _walk(
    config: Config,
    *,
    since: date | None = None,
    until: date | None = None,
    account_id: int | None = None,
    page_size: int,
) -> tuple[list[int], list[envelope.Answer]]:
    """Page until the answer stops offering a next one, the way a consumer would.

    🔴 Drives the loop a caller actually writes: read `next_cursor` off the WIRE
    form, hand it straight back, stop when the key is absent. A walk that read
    the cursor off the object would exercise a route no consumer has, and would
    keep passing if the key never reached the payload.
    """
    seen: list[int] = []
    pages: list[envelope.Answer] = []
    cursor: Cursor | None = None
    while True:
        answer = query.list_transactions(
            config,
            since=since,
            until=until,
            account_id=account_id,
            limit=page_size,
            after=cursor,
        )
        pages.append(answer)
        seen.extend(int(row["transaction_id"]) for row in answer.rows)
        assert answer.truncation is not None
        wire = answer.truncation.to_wire()
        if "next_cursor" not in wire:
            break
        cursor = envelope.parse_cursor(
            wire["next_cursor"], since=since, until=until, account_id=account_id
        )
        # A cursor that fails to advance produces a hung test rather than a red
        # one, so the walk carries its own ceiling.
        assert len(pages) <= _TOTAL + 2, "the walk did not terminate"
    return seen, pages


# 🔴 Every page size here must page BOTH row sets: the store holds `_TOTAL` and
# one account holds half that, so a size above the smaller one would silently
# make an unpaged answer stand in for a walk.
@pytest.mark.parametrize("page_size", [1, 3, 7, 50])
@pytest.mark.parametrize("account_id", [None, 1])
def test_a_paged_walk_returns_every_row_exactly_once_and_in_order(
    seeded_config: Config, page_size: int, account_id: int | None
) -> None:
    """🔴 The chunk's central invariant, stated over the walk rather than over a page.

    Every row exactly once, in the order one unpaged answer would have given
    them, with nothing repeated and nothing skipped. Duplication and omission are
    the two ways keyset paging fails, and both produce a payload that reads
    correctly on every individual page — which is why this is asserted across the
    walk and cannot be asserted inside it.

    The odd page sizes are load-bearing: the fixture writes two transactions per
    day, so a page boundary at 1, 3 or 7 falls INSIDE a day and the tie-break on
    `transaction_id` is the thing carrying it. A page size that always landed on
    a day boundary would pass with no tie-break at all.
    """
    expected = _oracle_ids(seeded_config, since=None, until=None, account_id=account_id)
    assert len(expected) > page_size, "this case is one page; it cannot show paging"

    seen, pages = _walk(seeded_config, account_id=account_id, page_size=page_size)

    assert len(seen) == len(set(seen)), "a row came back on more than one page"
    assert seen == expected, "the walk skipped, repeated or reordered a row"
    assert len(pages) == -(-len(expected) // page_size)


@pytest.mark.parametrize("page_size", [1, 7, 100])
def test_only_the_last_page_of_a_walk_reads_as_complete(
    seeded_config: Config, page_size: int
) -> None:
    """The terminating condition, which is the half a caller's loop depends on.

    An earlier page reading complete stops the walk with rows still unread; a
    last page reading truncated leaves the caller paging against an answer that
    has nothing more to give.
    """
    _, pages = _walk(seeded_config, page_size=page_size)
    blocks = [page.truncation for page in pages]
    assert all(block is not None for block in blocks)

    truncated = [block.truncated for block in blocks if block is not None]
    assert truncated == [True] * (len(pages) - 1) + [False]

    carried = ["next_cursor" in block.to_wire() for block in blocks if block is not None]
    assert carried == truncated, "a cursor and a next page have to arrive together"

    assert [w.kind for w in pages[-1].warnings if w.kind in envelope.REQUEST_SCOPED_KINDS] == []


def test_a_cursor_narrows_matching_to_the_rows_still_ahead(seeded_config: Config) -> None:
    """🔴 What `matching` means on page two, which is the decision the walk rests on.

    Counting the whole result set behind every page would leave `truncated` true
    on the final page forever, so a caller paging until it went false would never
    stop and would ask for a page that does not exist. The cursor narrows the
    count exactly as `since` does, and the two numbers stay a statement about
    THIS request.
    """
    first = query.list_transactions(seeded_config, limit=10)
    assert first.truncation is not None
    assert first.truncation.matching == _TOTAL
    assert first.truncation.next_cursor is not None

    second = query.list_transactions(
        seeded_config,
        limit=10,
        after=envelope.parse_cursor(
            first.truncation.next_cursor, since=None, until=None, account_id=None
        ),
    )

    assert second.truncation is not None
    assert second.truncation.matching == _TOTAL - 10
    assert second.truncation.returned == 10


def test_the_window_scoped_coverage_does_not_shrink_as_a_caller_pages(
    seeded_config: Config,
) -> None:
    """🔴 `transactions_in_effective_window` describes the WINDOW, not the page.

    It is a coverage fact — how much data the answered window holds — so it is
    counted with the cursor deliberately left off. A figure that fell page by
    page would describe the walk instead of the store, and a caller comparing it
    against `matching` would read the difference as data going missing.
    """
    _, pages = _walk(seeded_config, page_size=40)

    assert len(pages) > 1
    assert {page.coverage["transactions_in_effective_window"] for page in pages} == {_TOTAL}


def test_the_remedy_on_a_truncated_page_names_the_route_that_reaches_every_row(
    seeded_config: Config,
) -> None:
    """The caveat is where a consumer looks when the numbers seem wrong.

    Before this chunk the sentence offered only remedies that MOVE the cap —
    narrow the window, raise `limit` — and at the ceiling it offered narrowing
    alone. Paging is the one route that reaches every matching row, so it leads.
    """
    answer = query.list_transactions(seeded_config, limit=10)
    assert answer.truncation is not None

    detail = next(c.detail for c in answer.truncation.caveats if c.kind == "rows_truncated")

    assert "`next_cursor`" in detail
    assert "`cursor`" in detail


def test_a_complete_answer_offers_no_cursor_to_follow(seeded_config: Config) -> None:
    """The reverse half: nothing left to read means nothing telling a caller to read on."""
    answer = query.list_transactions(seeded_config, limit=MAX_ROWS)

    assert answer.truncation is not None
    assert answer.truncation.truncated is False
    assert answer.truncation.next_cursor is None
    assert "next_cursor" not in answer.truncation.to_wire()
