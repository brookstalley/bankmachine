"""The filters on `query_transactions`: effective category and a signed amount range. AC-9.6.

🔴 Every expected row set is derived from the table by an oracle written against
the columns in Python, never through `_transaction_filters` or
`_effective_category`. An oracle that shares the builder it checks agrees with it
by construction and detects nothing, which `learnings.md` records more than once.

Every case asserts its oracle found rows before comparing, because an empty
expectation and an empty answer agree about everything.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from bankmachine import envelope, query
from bankmachine.config import Config
from bankmachine.connector import ACCOUNTS_GET, TRANSACTIONS_SYNC
from bankmachine.derivers import ALL_DERIVERS
from bankmachine.envelope import MAX_ROWS, BadFilterError, Cursor, TransactionFilter
from bankmachine.store.derivation import apply_response
from bankmachine.store.engine import reader_connection, writer_connection
from bankmachine.store.schema import connections, institutions, transactions
from bankmachine.store.types import now_utc
from test_mcp import _call

#: (source id, account, aggregator amount, description, merchant, source category, days ago).
#: 🔴 Aggregator amounts are POSITIVE for money out. The deriver flips them, so
#: every stored and served amount is the negation of the string here.
_Row = tuple[str, int, str, str, str | None, str | None, int]

_FIXED: list[_Row] = [
    ("t-uber", 1, "12.00", "UBER 063015 SF", None, "TRANSPORTATION", 1),
    ("t-walmart", 2, "55.10", "WALMART STORE 1234", "Walmart", "GENERAL_MERCHANDISE", 2),
    ("t-walmart-return", 2, "-55.10", "WALMART RETURN 1234", "Walmart", "GENERAL_MERCHANDISE", 3),
    ("t-payroll", 1, "-2500.00", "PAYROLL ACME", None, "INCOME", 4),
    ("t-flight", 1, "400.00", "DELTA AIR 0062", "Delta", "TRAVEL", 5),
    ("t-flight-refund", 1, "-400.00", "DELTA AIR REFUND", "Delta", "TRAVEL", 6),
    ("t-coffee", 2, "4.50", "BLUE BOTTLE", "Blue Bottle", "FOOD_AND_DRINK", 7),
    ("t-check", 1, "100.00", "CHECK 1042", None, None, 8),
    ("t-concert", 2, "80.00", "TICKETS", None, "ENTERTAINMENT", 9),
]

#: Enough TRAVEL rows to page at every size below, two per day so the keyset's
#: tie-break carries positions, and one in four a refund so a signed bound has
#: something on each side of zero within one category.
_HOTELS: list[_Row] = [
    (
        f"t-hotel-{n}",
        (n % 2) + 1,
        f"{'-' if n % 4 == 0 else ''}{(n % 3 + 1) * 10}.00",
        f"HOTEL NIGHT {n}",
        None,
        "TRAVEL",
        10 + n // 2,
    )
    for n in range(24)
]

#: Removed after it lands, so the only row carrying its category is soft-deleted.
_REMOVED = "t-concert"
#: Overridden by the operator, so its source category is carried by nothing live.
_OVERRIDDEN = ("t-coffee", "COFFEE_SHOPS")


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
            "item": {"item_id": "item-filters"},
            "request_id": "req-accounts",
        }
    ).encode()


def _sync_body(today: date, added: list[_Row], removed: list[str], next_cursor: str) -> bytes:
    return json.dumps(
        {
            "accounts": [],
            "added": [
                {
                    "account_id": f"acct-{account}",
                    "transaction_id": source_id,
                    "amount": amount,
                    "iso_currency_code": "USD",
                    "date": str(today - timedelta(days=days_ago)),
                    "authorized_date": None,
                    "pending": False,
                    "pending_transaction_id": None,
                    "name": description,
                    "merchant_name": merchant,
                    "personal_finance_category": (
                        None if category is None else {"primary": category, "detailed": category}
                    ),
                }
                for source_id, account, amount, description, merchant, category, days_ago in added
            ],
            "modified": [],
            "removed": [
                {"transaction_id": source_id, "account_id": f"acct-{account}"}
                for source_id, account, *_ in _FIXED + _HOTELS
                if source_id in removed
            ],
            "next_cursor": next_cursor,
            "has_more": False,
            "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE",
            "request_id": f"req-{next_cursor}",
        }
    ).encode()


@pytest.fixture
def filtered_config(initialized_config: Config) -> Config:
    """A store whose categories and amounts give every filter something to include and exclude.

    Derived through the real derivers rather than hand-inserted, for the reason
    `tests/test_query_truncation.py` gives: the schema enforces provenance with a
    CHECK, and hand-built rows stop reflecting what the product writes.
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
                source_connection_id="item-filters",
                credential_ref="connection:sandbox:item-filters",
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
            (TRANSACTIONS_SYNC.path, _sync_body(now.date(), _FIXED + _HOTELS, [], "cursor-1")),
            (TRANSACTIONS_SYNC.path, _sync_body(now.date(), [], [_REMOVED], "cursor-2")),
        ):
            apply_response(
                conn,
                connection_id=1,
                endpoint=endpoint,
                body=body,
                received_at=now,
                derivers=ALL_DERIVERS,
            )
        source_id, override = _OVERRIDDEN
        conn.execute(
            transactions.update()
            .where(transactions.c.source_transaction_id == source_id)
            .values(category_override=override)
        )
    # 🔴 The fixture's two special rows are asserted to BE special, so a deriver
    # change that stopped removing or overriding them fails here, loudly, instead
    # of turning the refusal tests below into checks of nothing.
    with reader_connection(initialized_config) as conn:
        state = {
            row.source_transaction_id: (row.removed_at, row.category_override)
            for row in conn.execute(
                select(
                    transactions.c.source_transaction_id,
                    transactions.c.removed_at,
                    transactions.c.category_override,
                )
            )
        }
    assert len(state) == len(_FIXED) + len(_HOTELS)
    assert state[_REMOVED][0] is not None, "the removal did not land"
    assert state[_OVERRIDDEN[0]][1] == _OVERRIDDEN[1], "the override did not land"
    return initialized_config


def _oracle_ids(
    config: Config, *, narrowed_by: TransactionFilter, account_id: int | None = None
) -> list[int]:
    """Every matching transaction id, newest first, filtered in Python off the raw columns."""
    t = transactions.c
    with reader_connection(config) as conn:
        rows = conn.execute(
            select(
                t.transaction_id,
                t.account_id,
                t.amount_minor,
                t.category_override,
                t.source_category_primary,
                t.removed_at,
            ).order_by(t.ledger_date.desc(), t.transaction_id.desc())
        ).all()
    kept: list[int] = []
    for row in rows:
        if row.removed_at is not None:
            continue
        if account_id is not None and row.account_id != account_id:
            continue
        if row.category_override is not None:
            effective = row.category_override
        elif row.source_category_primary is not None:
            effective = row.source_category_primary
        else:
            effective = "UNCATEGORIZED"
        if narrowed_by.category is not None and effective != narrowed_by.category:
            continue
        low, high = narrowed_by.min_amount_minor, narrowed_by.max_amount_minor
        if low is not None and row.amount_minor < low:
            continue
        if high is not None and row.amount_minor > high:
            continue
        kept.append(int(row.transaction_id))
    return kept


def _ids(answer: envelope.Answer) -> list[int]:
    return [int(row["transaction_id"]) for row in answer.rows]


def _described(answer: envelope.Answer, *fields: str) -> list[tuple[Any, ...]]:
    return [tuple(row[field] for field in fields) for row in answer.rows]


# --------------------------------------------------------------------------
# Category
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "category", ["TRAVEL", "GENERAL_MERCHANDISE", "INCOME", "COFFEE_SHOPS", "UNCATEGORIZED"]
)
def test_a_category_selects_exactly_the_rows_whose_effective_category_it_names(
    filtered_config: Config, category: str
) -> None:
    narrowed = TransactionFilter(category=category)
    expected = _oracle_ids(filtered_config, narrowed_by=narrowed)
    assert expected, f"the fixture holds no live {category} row, so this case compares nothing"

    answer = query.list_transactions(filtered_config, limit=MAX_ROWS, narrowed_by=narrowed)

    assert _ids(answer) == expected


def test_an_override_moves_a_row_into_its_new_category_and_out_of_its_old_one(
    filtered_config: Config,
) -> None:
    """The operator's correction is what a row counts under, for the filter as for the aggregate.

    The source category is carried by nothing live once the only row with it is
    overridden, so asking for it is refused rather than answered empty.
    """
    answer = query.list_transactions(
        filtered_config, narrowed_by=TransactionFilter(category="COFFEE_SHOPS")
    )

    assert _described(answer, "description", "category", "category_is_override") == [
        ("BLUE BOTTLE", "COFFEE_SHOPS", True)
    ]
    with pytest.raises(query.UnknownCategoryError):
        query.list_transactions(
            filtered_config, narrowed_by=TransactionFilter(category="FOOD_AND_DRINK")
        )


def test_uncategorized_selects_the_rows_whose_category_is_null(filtered_config: Config) -> None:
    answer = query.list_transactions(
        filtered_config, narrowed_by=TransactionFilter(category="UNCATEGORIZED")
    )

    assert _described(answer, "description", "category") == [("CHECK 1042", None)]


def test_every_group_key_money_summary_reports_selects_the_rows_that_group_counted(
    filtered_config: Config,
) -> None:
    """🔴 The drill-down the filter exists for: a category row, then the rows behind it.

    If the aggregate and the filter spelled the effective category differently,
    a `group_key` passed back would select nothing, or something else, and the
    caller would have a total with no rows that add up to it.
    """
    summary = query.money_summary(filtered_config, group_by="category")
    counted: dict[str, int] = {}
    for row in summary.rows:
        key = str(row["group_key"])
        counted[key] = counted.get(key, 0) + int(row["transactions"])
    assert counted, "money_summary reported no category, so nothing was compared"

    for key, count in counted.items():
        answer = query.list_transactions(
            filtered_config, limit=MAX_ROWS, narrowed_by=TransactionFilter(category=key)
        )
        assert answer.truncation is not None
        assert answer.truncation.matching == count, key


def test_a_category_no_transaction_carries_is_refused_naming_the_ones_that_do(
    filtered_config: Config,
) -> None:
    """`TRAVL` would otherwise read as "no travel", which invites no second look.

    The list excludes the category only a removed row carries and the source
    category an override replaced: both would select nothing, so offering either
    as a correction would send the caller straight back to an empty answer.
    """
    with pytest.raises(query.UnknownCategoryError) as refused:
        query.list_transactions(filtered_config, narrowed_by=TransactionFilter(category="TRAVL"))

    assert str(refused.value) == (
        "category 'TRAVL' is carried by no transaction in this store. The categories it holds: "
        "COFFEE_SHOPS, GENERAL_MERCHANDISE, INCOME, TRANSPORTATION, TRAVEL, UNCATEGORIZED"
    )


def test_a_category_carried_only_by_a_removed_transaction_is_refused(
    filtered_config: Config,
) -> None:
    with pytest.raises(query.UnknownCategoryError):
        query.list_transactions(
            filtered_config, narrowed_by=TransactionFilter(category="ENTERTAINMENT")
        )


def test_a_category_is_matched_exactly_and_the_refusal_offers_the_right_spelling(
    filtered_config: Config,
) -> None:
    with pytest.raises(query.UnknownCategoryError) as refused:
        query.list_transactions(filtered_config, narrowed_by=TransactionFilter(category="travel"))

    _, _, offered = str(refused.value).partition("The categories it holds: ")
    assert offered, f"the refusal names no categories: {refused.value}"
    assert "TRAVEL" in offered.split(", ")


def test_a_category_in_a_window_that_holds_none_of_it_is_an_empty_answer_not_a_refusal(
    filtered_config: Config,
) -> None:
    """Existence is checked against the store, never the window.

    A category with no rows THIS week is an ordinary answer; refusing it would
    tell the caller a real category does not exist.
    """
    today = now_utc().date()

    answer = query.list_transactions(
        filtered_config,
        since=today - timedelta(days=2),
        until=today,
        narrowed_by=TransactionFilter(category="INCOME"),
    )

    assert answer.rows == []


# --------------------------------------------------------------------------
# The signed amount range
# --------------------------------------------------------------------------

_BOUNDS: list[tuple[int | None, int | None]] = [
    (None, -10000),  # every outflow of $100 or more
    (40000, None),  # every inflow of $400 or more, with the flight refund ON the bound
    (-5510, -5510),  # one exact amount
    (0, None),  # money in
    (None, -1),  # money out
    (-3000, 3000),  # small movements either way
]


@pytest.mark.parametrize(("low", "high"), _BOUNDS)
def test_an_amount_range_selects_exactly_the_signed_amounts_inside_it(
    filtered_config: Config, low: int | None, high: int | None
) -> None:
    narrowed = TransactionFilter(min_amount_minor=low, max_amount_minor=high)
    expected = _oracle_ids(filtered_config, narrowed_by=narrowed)
    assert expected, f"no fixture row lies in [{low}, {high}], so this case compares nothing"

    answer = query.list_transactions(filtered_config, limit=MAX_ROWS, narrowed_by=narrowed)

    assert _ids(answer) == expected
    # The check a reader can make with the payload alone, which is why the bound
    # is signed the way the rows are.
    for row in answer.rows:
        amount = int(row["amount_minor_units"])
        assert (low is None or amount >= low) and (high is None or amount <= high), row


def test_both_bounds_are_inclusive(filtered_config: Config) -> None:
    """`<` for `<=` would drop exactly the row whose amount the caller typed."""
    refund = query.list_transactions(
        filtered_config,
        narrowed_by=TransactionFilter(min_amount_minor=40000, max_amount_minor=40000),
    )
    check = query.list_transactions(
        filtered_config,
        narrowed_by=TransactionFilter(min_amount_minor=-10000, max_amount_minor=-10000),
    )

    assert _described(refund, "description", "amount_minor_units") == [("DELTA AIR REFUND", 40000)]
    assert _described(check, "description", "amount_minor_units") == [("CHECK 1042", -10000)]


def test_a_range_whose_bounds_are_transposed_is_refused_rather_than_answered_empty() -> None:
    with pytest.raises(BadFilterError) as refused:
        TransactionFilter(min_amount_minor=1, max_amount_minor=0)

    assert str(refused.value) == (
        "min_amount_minor_units (1) is above max_amount_minor_units (0), so the range selects "
        "nothing. Amounts are signed and money out is negative: 'spent $100 or more' is "
        "max_amount_minor_units=-10000"
    )


def test_equal_bounds_are_a_range_rather_than_a_contradiction() -> None:
    assert TransactionFilter(min_amount_minor=5, max_amount_minor=5).narrows


def test_filters_compose_with_each_other_and_with_the_account(filtered_config: Config) -> None:
    narrowed = TransactionFilter(category="TRAVEL", min_amount_minor=1)
    expected = _oracle_ids(filtered_config, narrowed_by=narrowed, account_id=1)
    assert expected, "no TRAVEL inflow on account 1, so this case compares nothing"

    answer = query.list_transactions(
        filtered_config, account_id=1, limit=MAX_ROWS, narrowed_by=narrowed
    )

    assert _ids(answer) == expected


# --------------------------------------------------------------------------
# What a filter narrows, and what it must not
# --------------------------------------------------------------------------


def test_a_filter_narrows_matching_and_leaves_the_window_scoped_coverage_alone(
    filtered_config: Config,
) -> None:
    """🔴 `transactions_in_effective_window` counts the WINDOW, and rides beside `matching`.

    A caller compares the two to see how much of the window its filter set
    aside. A filter that leaked into the coverage count would make them agree
    and erase exactly that, while repurposing a shipped field.
    """
    since = now_utc().date() - timedelta(days=60)
    narrowed = TransactionFilter(category="TRAVEL")

    whole = query.list_transactions(filtered_config, since=since, limit=MAX_ROWS)
    travel = query.list_transactions(
        filtered_config, since=since, limit=MAX_ROWS, narrowed_by=narrowed
    )

    assert whole.truncation is not None and travel.truncation is not None
    assert travel.truncation.matching == len(_oracle_ids(filtered_config, narrowed_by=narrowed))
    assert travel.truncation.matching < whole.truncation.matching
    assert (
        travel.coverage["transactions_in_effective_window"]
        == whole.coverage["transactions_in_effective_window"]
    )
    assert travel.coverage["transactions"] == whole.coverage["transactions"]


def test_an_unreadable_store_answers_rather_than_calling_a_category_unknown(
    config: Config,
) -> None:
    """Which categories exist is a fact about the data, and this store has none to read."""
    answer = query.list_transactions(config, narrowed_by=TransactionFilter(category="TRAVEL"))

    assert answer.rows == []
    assert answer.truncation is not None
    assert answer.truncation.matching == 0


# --------------------------------------------------------------------------
# The cursor carries the filters
# --------------------------------------------------------------------------

_FILTERS = [
    TransactionFilter(),
    TransactionFilter(category="TRAVEL"),
    TransactionFilter(category="INCOME"),
    TransactionFilter(min_amount_minor=0),
    TransactionFilter(max_amount_minor=0),
    TransactionFilter(min_amount_minor=0, max_amount_minor=0),
]


@pytest.mark.parametrize("issued_for", _FILTERS, ids=repr)
@pytest.mark.parametrize("presented_with", _FILTERS, ids=repr)
def test_a_cursor_is_usable_only_against_the_filters_that_issued_it(
    issued_for: TransactionFilter, presented_with: TransactionFilter
) -> None:
    """🔴 Page two of a `TRAVEL` walk sent with `INCOME` would read as a continuation.

    Asserted over the whole cross product so accepting and refusing are decided
    by one rule. `min=0` and `max=0` are both present because a fingerprint that
    hashed the bound values without their positions could not tell them apart.
    """
    wire = Cursor.issued_for(
        ledger_date=date(2026, 5, 4),
        transaction_id=7,
        since=None,
        until=None,
        account_id=None,
        narrowed_by=issued_for,
    ).encode()

    def _resume() -> Cursor | None:
        return envelope.parse_cursor(
            wire, since=None, until=None, account_id=None, narrowed_by=presented_with
        )

    if presented_with == issued_for:
        assert _resume() is not None
        return
    with pytest.raises(envelope.MalformedCursorError):
        _resume()


def test_an_unfiltered_request_hashes_what_it_hashed_before_filters_existed() -> None:
    """A client relaunches this server, so a cursor from the previous build is ordinary traffic.

    Pinned against the pre-filter material spelled out here, rather than against
    anything read from the function under test.
    """
    material = json.dumps([None, "2026-01-01", 3], separators=(",", ":"))

    assert envelope._request_fingerprint(since=None, until=date(2026, 1, 1), account_id=3) == (
        hashlib.blake2s(material.encode(), digest_size=8).hexdigest()
    )


@pytest.mark.parametrize("page_size", [1, 4, 7])
def test_a_filtered_walk_returns_every_matching_row_exactly_once_and_ends(
    filtered_config: Config, page_size: int
) -> None:
    """Multi-hop: the cursor a filtered page issues must resume the same filtered set."""
    narrowed = TransactionFilter(category="TRAVEL", max_amount_minor=-1)
    expected = _oracle_ids(filtered_config, narrowed_by=narrowed)
    assert len(expected) > 7, "the walk must span several pages at every size"

    seen: list[int] = []
    matchings: set[int] = set()
    cursor: Cursor | None = None
    answer: envelope.Answer | None = None
    for _ in range(len(expected) + 2):
        answer = query.list_transactions(
            filtered_config, limit=page_size, after=cursor, narrowed_by=narrowed
        )
        seen.extend(_ids(answer))
        assert answer.truncation is not None
        matchings.add(answer.truncation.matching)
        wire = answer.truncation.to_wire()
        if "next_cursor" not in wire:
            break
        cursor = envelope.parse_cursor(
            wire["next_cursor"], since=None, until=None, account_id=None, narrowed_by=narrowed
        )
    else:
        pytest.fail("the walk did not terminate")

    assert seen == expected
    assert matchings == {len(expected)}
    assert answer is not None and answer.truncation is not None
    assert answer.truncation.truncated is False


def test_page_two_of_one_filter_cannot_resume_another(filtered_config: Config) -> None:
    first = query.list_transactions(
        filtered_config, limit=2, narrowed_by=TransactionFilter(category="TRAVEL")
    )
    assert first.truncation is not None
    wire = first.truncation.to_wire()
    assert "next_cursor" in wire, "the first TRAVEL page was not truncated, so nothing is resumed"

    with pytest.raises(envelope.MalformedCursorError):
        envelope.parse_cursor(
            wire["next_cursor"],
            since=None,
            until=None,
            account_id=None,
            narrowed_by=TransactionFilter(category="INCOME"),
        )


# --------------------------------------------------------------------------
# The boundary: what a client sends, and what it is told
# --------------------------------------------------------------------------


def test_the_tool_takes_the_filters_and_answers_with_them(filtered_config: Config) -> None:
    narrowed = TransactionFilter(category="TRAVEL", min_amount_minor=1)
    expected = _oracle_ids(filtered_config, narrowed_by=narrowed)
    assert expected, "no TRAVEL inflow in the fixture, so this case compares nothing"

    result = _call(
        filtered_config,
        "query_transactions",
        {"category": "TRAVEL", "min_amount_minor_units": 1, "limit": MAX_ROWS},
    )

    assert result["isError"] is False, result["content"][0]["text"]
    assert [int(row["transaction_id"]) for row in result["structuredContent"]["rows"]] == expected


@pytest.mark.parametrize(
    ("arguments", "named"),
    [
        ({"min_amount_minor_units": 1.5}, "min_amount_minor_units"),
        ({"max_amount_minor_units": True}, "max_amount_minor_units"),
        ({"min_amount_minor_units": 2**63}, "min_amount_minor_units"),
        ({"category": 7}, "category"),
    ],
)
def test_a_malformed_filter_argument_is_refused_naming_it(
    filtered_config: Config, arguments: dict[str, object], named: str
) -> None:
    """Each case is valid in every respect but the one argument it exercises.

    `2**63` is a valid JSON integer that SQLite cannot bind: unrefused, it reaches
    the caller as an internal error for a mistake in their own argument.
    """
    result = _call(filtered_config, "query_transactions", arguments)

    assert result["isError"] is True, f"{arguments} was answered rather than refused"
    assert result["structuredContent"]["error"]["code"] == "invalid_argument"
    assert named in result["content"][0]["text"]


def test_a_transposed_range_is_refused_at_the_boundary(filtered_config: Config) -> None:
    result = _call(
        filtered_config,
        "query_transactions",
        {"min_amount_minor_units": 1, "max_amount_minor_units": 0},
    )

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "invalid_argument"
    assert "selects nothing" in result["content"][0]["text"]


def test_an_unknown_category_is_refused_at_the_boundary(filtered_config: Config) -> None:
    result = _call(filtered_config, "query_transactions", {"category": "TRAVL"})

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "invalid_argument"
    _, _, offered = result["content"][0]["text"].partition("The categories it holds: ")
    assert "TRAVEL" in offered.split(", ")


def test_a_cursor_sent_back_with_different_filters_is_refused_at_the_boundary(
    filtered_config: Config,
) -> None:
    """Multi-hop through the wire: the same filters resume, different ones are refused."""
    first = _call(filtered_config, "query_transactions", {"category": "TRAVEL", "limit": 2})
    cursor = first["structuredContent"]["truncation"]["next_cursor"]

    same = _call(
        filtered_config, "query_transactions", {"category": "TRAVEL", "limit": 2, "cursor": cursor}
    )
    other = _call(
        filtered_config, "query_transactions", {"category": "INCOME", "limit": 2, "cursor": cursor}
    )

    assert same["isError"] is False, same["content"][0]["text"]
    assert other["isError"] is True, "a TRAVEL cursor resumed an INCOME request"
    assert other["structuredContent"]["error"]["code"] == "invalid_argument"
