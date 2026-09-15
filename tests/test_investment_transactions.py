"""`query_investment_transactions`: every stored trade, windowed, paged and totalled.

🔴 **The store is built from the aggregator's recorded capture, through the shipped
derivers**, for `test_list_holdings.py`'s reason: the question is whether what the deriver
WROTE comes back out as the tool publishes it, and a hand-typed trade row would carry this
file's beliefs about its columns instead.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert, select, update

from bankmachine import envelope, mcp, query, query_investments
from bankmachine.config import Config
from bankmachine.connector import ACCOUNTS_GET, INVESTMENTS_TRANSACTIONS_GET
from bankmachine.derivers import ALL_DERIVERS
from bankmachine.store.derivation import apply_response
from bankmachine.store.engine import writer_connection
from bankmachine.store.schema import connections, institutions, investment_transactions
from bankmachine.store.types import utc_instant

FIXTURES = Path(__file__).parent / "connector" / "fixtures"
CAPTURED = utc_instant(datetime(2026, 9, 12, 14, 0, tzinfo=UTC))

#: The keys every row must carry, read off the published schema rather than listed here.
ROW_KEYS = frozenset(
    next(d for d in mcp._tool_definitions() if d["name"] == "query_investment_transactions")[
        "outputSchema"
    ]["properties"]["rows"]["items"]["properties"]
)


def recorded() -> dict[str, Any]:
    body: dict[str, Any] = json.loads(
        (FIXTURES / "investments_transactions_get.json").read_bytes(), parse_float=str
    )
    return body


@pytest.fixture
def seeded(initialized_config: Config) -> Config:
    """One connection, the capture's roster, and the capture's trades."""
    payload = recorded()
    with writer_connection(initialized_config) as conn:
        primary_key = conn.execute(
            insert(institutions).values(
                source_institution_id="ins_trades_test",
                name="Example Brokerage",
                first_seen_at=CAPTURED,
                last_seen_at=CAPTURED,
            )
        ).inserted_primary_key
        assert primary_key is not None
        conn.execute(
            insert(connections).values(
                connection_id=1,
                institution_id=primary_key[0],
                source_connection_id="item-trades",
                credential_ref="connection:sandbox:item-trades",
                capabilities='["investments"]',
                status="active",
                enrolled_at=CAPTURED,
                created_at=CAPTURED,
                updated_at=CAPTURED,
            )
        )
        for endpoint, body in (
            (
                ACCOUNTS_GET.path,
                {"accounts": payload["accounts"], "item": {"item_id": "item-trades"}},
            ),
            (INVESTMENTS_TRANSACTIONS_GET.path, payload),
        ):
            apply_response(
                conn,
                connection_id=1,
                endpoint=endpoint,
                body=json.dumps(body).encode(),
                received_at=CAPTURED,
                derivers=ALL_DERIVERS,
                replay_passes=(),
            )
    return initialized_config


def _walk(config: Config, limit: int, **scope: Any) -> tuple[list[dict[str, Any]], envelope.Answer]:
    """Every page of one request, following `next_cursor` until `truncated` is false."""
    rows: list[dict[str, Any]] = []
    after = None
    for _ in range(1000):
        answer = query_investments.query_investment_transactions(
            config, limit=limit, after=after, **scope
        )
        rows.extend(answer.rows)
        assert answer.truncation is not None
        if not answer.truncation.truncated:
            return rows, answer
        assert answer.truncation.resume_from is not None, "a truncated page issued no cursor"
        after = envelope.parse_trade_cursor(
            answer.truncation.resume_from.encode(), **scope_for_cursor(scope)
        )
    raise AssertionError("the walk never ended")


def scope_for_cursor(scope: dict[str, Any]) -> dict[str, Any]:
    return {
        "since": scope.get("since"),
        "until": scope.get("until"),
        "account_id": scope.get("account_id"),
        "investment_type": scope.get("investment_type"),
    }


def test_every_recorded_trade_comes_back_with_the_published_keys(seeded: Config) -> None:
    """Every trade in the capture, as a multiset of (date, type, amount), each row strict."""
    payload = recorded()
    rows, _ = _walk(seeded, 500)

    assert rows, "the capture derived no trade, so nothing here checks the tool"
    assert all(set(row) == ROW_KEYS for row in rows)
    assert len(rows) == len(payload["investment_transactions"])
    assert sorted((r["trade_date"], r["investment_type"]) for r in rows) == sorted(
        (t["date"], t["type"]) for t in payload["investment_transactions"]
    )


def test_a_walk_at_any_page_size_reads_every_trade_exactly_once_and_matches_the_totals(
    seeded: Config,
) -> None:
    """🔴 The invariant, over several page sizes rather than the one a fixture happened to fit.

    Every page's rows lie inside `effective_window`; the ids never repeat; the walk's length is
    `matching`; and the totals, which cover the whole request, count and sum exactly the rows
    the walk read.
    """
    for limit in (1, 3, 7, 40, 500):
        rows, last = _walk(seeded, limit)
        ids = [r["investment_transaction_id"] for r in rows]
        assert len(ids) == len(set(ids)), f"limit={limit}: a trade came back twice"
        assert last.truncation is not None
        assert len(rows) == last.truncation.matching, f"limit={limit}"
        assert last.effective_window is not None
        low, high = last.effective_window.effective_since, last.effective_window.effective_until
        assert low is not None and high is not None
        assert all(low.isoformat() <= r["trade_date"] <= high.isoformat() for r in rows)
        assert last.totals is not None
        assert sum(t["transactions"] for t in last.totals) == len(rows)
        per_key: dict[tuple[Any, ...], int] = {}
        for r in rows:
            key = (r["currency"], r["investment_type"], r["investment_subtype"])
            per_key[key] = per_key.get(key, 0) + r["amount_minor_units"]
        assert {
            (t["currency"], t["investment_type"], t["investment_subtype"]): t["amount_minor_units"]
            for t in last.totals
        } == per_key, f"limit={limit}"


def test_the_totals_cover_the_whole_request_not_the_page(seeded: Config) -> None:
    answer = query_investments.query_investment_transactions(seeded, limit=5)

    assert answer.truncation is not None and answer.truncation.truncated
    assert answer.totals is not None
    assert sum(t["transactions"] for t in answer.totals) == answer.truncation.matching
    assert answer.truncation.matching > len(answer.rows)


def test_a_removed_trade_is_in_no_row_count_or_total(seeded: Config) -> None:
    with writer_connection(seeded) as conn:
        victim = conn.execute(
            select(investment_transactions.c.investment_transaction_id).limit(1)
        ).scalar_one()
        conn.execute(
            update(investment_transactions)
            .where(investment_transactions.c.investment_transaction_id == victim)
            .values(removed_at=CAPTURED)
        )

    rows, last = _walk(seeded, 500)

    assert victim not in {r["investment_transaction_id"] for r in rows}
    assert last.truncation is not None
    assert last.truncation.matching == len(recorded()["investment_transactions"]) - 1
    assert last.totals is not None
    assert sum(t["transactions"] for t in last.totals) == last.truncation.matching


def test_the_window_is_clamped_to_the_trades_own_span(seeded: Config) -> None:
    """Reconciled against the days trades were recorded, not the transactions feed's span."""
    earliest = min(t["date"] for t in recorded()["investment_transactions"])

    answer = query_investments.query_investment_transactions(seeded, since=date(2000, 1, 1))

    assert answer.effective_window is not None
    assert answer.effective_window.effective_since == date.fromisoformat(earliest)
    assert "window_starts_before_coverage" in [w.kind for w in answer.warnings]


def test_the_type_filter_narrows_rows_counts_and_totals(seeded: Config) -> None:
    rows, last = _walk(seeded, 500, investment_type="buy")

    assert rows and {r["investment_type"] for r in rows} == {"buy"}
    assert last.totals is not None and {t["investment_type"] for t in last.totals} == {"buy"}


def test_an_unknown_type_is_refused_naming_the_types_that_exist(seeded: Config) -> None:
    with pytest.raises(query_investments.UnknownInvestmentTypeError, match="buy"):
        query_investments.query_investment_transactions(seeded, investment_type="purchase")


def test_an_unknown_account_is_refused(seeded: Config) -> None:
    with pytest.raises(query.UnknownAccountError):
        query_investments.query_investment_transactions(seeded, account_id=9999)


def test_a_cursor_from_another_tool_or_another_request_is_refused() -> None:
    """Each decoder refuses the others' scheme tags, and a fingerprint names one request."""
    trade = envelope.TradeCursor.issued_for(
        trade_date=date(2026, 9, 1),
        investment_transaction_id=5,
        since=None,
        until=None,
        account_id=None,
        investment_type=None,
    ).encode()
    transaction = envelope.Cursor.issued_for(
        ledger_date=date(2026, 9, 1), transaction_id=5, since=None, until=None, account_id=None
    ).encode()
    with pytest.raises(envelope.MalformedCursorError):
        envelope.parse_trade_cursor(
            transaction, since=None, until=None, account_id=None, investment_type=None
        )
    with pytest.raises(envelope.MalformedCursorError):
        envelope.parse_cursor(trade, since=None, until=None, account_id=None)
    with pytest.raises(envelope.MalformedCursorError):
        envelope.parse_series_cursor(trade, since=None, until=None, account_id=None)
    with pytest.raises(envelope.MalformedCursorError):
        envelope.parse_trade_cursor(
            trade, since=None, until=None, account_id=None, investment_type="buy"
        )
    assert (
        envelope.parse_trade_cursor(
            trade, since=None, until=None, account_id=None, investment_type=None
        )
        is not None
    )


def test_a_missing_datastore_answers_with_every_key_and_nothing_in_it(config: Config) -> None:
    """AC-ARCH.3: an empty answer that still says it is windowed, capped and totalled."""
    answer = query_investments.query_investment_transactions(config)

    assert answer.rows == []
    assert answer.totals == []
    assert answer.truncation is not None and answer.truncation.matching == 0
    assert answer.effective_window is not None
    assert "partial" in [w.kind for w in answer.warnings]


def test_the_tool_answers_on_the_wire_within_its_published_schema(seeded: Config) -> None:
    """Through the real server, with a cursor round-trip, so the dispatch and schema agree."""
    stdin_frames = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "query_investment_transactions", "arguments": {"limit": 2}},
        },
    ]
    import io

    out = io.StringIO()
    mcp.serve(
        seeded, stdin=io.StringIO("\n".join(map(json.dumps, stdin_frames)) + "\n"), stdout=out
    )
    replies = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    first = replies[1]["result"]
    assert not first.get("isError"), first
    wire = first["structuredContent"]
    assert len(wire["rows"]) == 2 and wire["truncation"]["truncated"]
    cursor = wire["truncation"]["next_cursor"]

    frames = stdin_frames[:1] + [
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "query_investment_transactions",
                "arguments": {"limit": 2, "cursor": cursor},
            },
        }
    ]
    out = io.StringIO()
    mcp.serve(seeded, stdin=io.StringIO("\n".join(map(json.dumps, frames)) + "\n"), stdout=out)
    second = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()][1]["result"]
    assert not second.get("isError"), second
    page_two = {r["investment_transaction_id"] for r in second["structuredContent"]["rows"]}
    assert page_two and not page_two & {r["investment_transaction_id"] for r in wire["rows"]}
