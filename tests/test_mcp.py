"""The MCP surface: the handshake, the tools, and the envelope every answer carries.

🔴 The handshake is the part most likely to be subtly wrong in a hand-rolled
server, and it fails at *connection* time rather than in a unit test — so it is
driven here through the real loop over string buffers, not mocked.
"""

from __future__ import annotations

import argparse
import inspect
import io
import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from bankmachine import build_id, mcp, query
from bankmachine.config import Config
from bankmachine.connector import ACCOUNTS_GET, TRANSACTIONS_SYNC
from bankmachine.derivers import ALL_DERIVERS
from bankmachine.store.derivation import apply_response
from bankmachine.store.engine import writer_connection
from bankmachine.store.schema import connections, institutions
from bankmachine.store.types import now_utc


def _accounts_body() -> bytes:
    return json.dumps(
        {
            "accounts": [
                {
                    "account_id": "acct-1",
                    "name": "Plaid Checking",
                    "mask": "0000",
                    "type": "depository",
                    "subtype": "checking",
                    "balances": {
                        "current": "110.94",
                        "available": "100.00",
                        "limit": None,
                        "iso_currency_code": "USD",
                    },
                }
            ],
            "item": {"item_id": "item-mcp"},
            "request_id": "req-accounts",
        }
    ).encode()


def _sync_body(today: str) -> bytes:
    def txn(index: int, amount: str, name: str, category: str) -> dict[str, Any]:
        return {
            "account_id": "acct-1",
            "transaction_id": f"t{index}",
            "amount": amount,
            "iso_currency_code": "USD",
            "date": today,
            "authorized_date": None,
            "pending": False,
            "pending_transaction_id": None,
            "name": name,
            "merchant_name": None,
            "personal_finance_category": {"primary": category, "detailed": category},
        }

    return json.dumps(
        {
            "accounts": [],
            # Positive amounts, as the aggregator sends them: these are money
            # LEAVING. The last is negative — money arriving — so the spending
            # aggregate has something it must exclude.
            "added": [
                txn(0, "89.40", "SparkFun", "GENERAL_MERCHANDISE"),
                txn(1, "12.00", "McDonald's", "FOOD_AND_DRINK"),
                txn(2, "-250.00", "Payroll", "INCOME"),
            ],
            "modified": [],
            "removed": [],
            "next_cursor": "cursor-1",
            "has_more": False,
            "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE",
            "request_id": "req-sync",
        }
    ).encode()


def _seed(config: Config, *, degraded: bool = False, granted: int | None = 90) -> None:
    """One institution, one account, three transactions — through the real derivers.

    🔴 Derived rather than hand-inserted. The schema enforces provenance with a
    CHECK, so hand-built rows either encode this test's assumptions about that
    constraint or fail on it — and a fixture that bypassed the derivers would
    also stop reflecting what the product actually writes.
    """
    now = now_utc()
    with writer_connection(config) as conn:
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
                source_connection_id="item-mcp",
                credential_ref="connection:sandbox:item-mcp",
                capabilities="[]",
                requested_history_days=730,
                granted_history_days=granted,
                status="degraded" if degraded else "active",
                last_success_at=None if degraded else now,
                last_error_code="TransportError" if degraded else None,
                last_error_at=now if degraded else None,
                enrolled_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        for endpoint, body in (
            (ACCOUNTS_GET.path, _accounts_body()),
            (TRANSACTIONS_SYNC.path, _sync_body(str(now.date()))),
        ):
            apply_response(
                conn,
                connection_id=1,
                endpoint=endpoint,
                body=body,
                received_at=now,
                derivers=ALL_DERIVERS,
            )


def _converse(config: Config, requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drive the real server loop over string buffers.

    Not a mock of the protocol: the same `serve` the CLI calls, reading the same
    line-delimited JSON a client writes.
    """
    stdin = io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n")
    stdout = io.StringIO()
    mcp.serve(config, stdin=stdin, stdout=stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]


def _call(config: Config, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    replies = _converse(
        config,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            },
        ],
    )
    result: dict[str, Any] = replies[-1]["result"]
    return result


# --------------------------------------------------------------------------
# The handshake
# --------------------------------------------------------------------------


def test_the_clients_protocol_version_is_honoured_when_recognized(
    initialized_config: Config,
) -> None:
    """🔴 The client is the half that cannot adapt.

    A server that always asserted its newest version would break every client
    pinned to an older one — and the failure lands at connection time, where the
    operator sees a tool that simply does not appear.
    """
    replies = _converse(
        initialized_config,
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }
        ],
    )

    assert replies[0]["result"]["protocolVersion"] == "2025-06-18"


def test_an_unknown_protocol_version_falls_back_rather_than_failing(
    initialized_config: Config,
) -> None:
    """A version this server has never heard of is not a reason to refuse the session.

    It answers with the SDK's own default negotiated version, which any client
    speaking something newer can also speak.
    """
    replies = _converse(
        initialized_config,
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "1999-01-01"},
            }
        ],
    )

    assert replies[0]["result"]["protocolVersion"] == mcp.FALLBACK_PROTOCOL_VERSION


def test_the_server_declares_only_the_capability_it_serves(initialized_config: Config) -> None:
    """Declaring one it does not would have the client offer the operator something that fails."""
    result = _converse(
        initialized_config, [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}]
    )[0]["result"]

    assert set(result["capabilities"]) == {"tools"}


def test_a_notification_is_not_answered(initialized_config: Config) -> None:
    """Replying to a notification is a protocol error on this side.

    `notifications/initialized` is the one every client sends immediately after
    the handshake, so getting this wrong breaks the very first exchange.
    """
    replies = _converse(
        initialized_config,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ],
    )

    assert len(replies) == 1


def test_an_unparseable_line_is_answered_rather_than_ignored(initialized_config: Config) -> None:
    """A client that sent something broken is waiting; silence looks like a hang."""
    stdin = io.StringIO("not json at all\n")
    stdout = io.StringIO()
    mcp.serve(initialized_config, stdin=stdin, stdout=stdout)

    reply = json.loads(stdout.getvalue())
    assert reply["error"]["code"] == -32700


def test_an_unknown_method_is_a_method_not_found(initialized_config: Config) -> None:
    replies = _converse(
        initialized_config, [{"jsonrpc": "2.0", "id": 1, "method": "resources/list"}]
    )

    assert replies[0]["error"]["code"] == -32601


# --------------------------------------------------------------------------
# 🔴 Read-only, structurally
# --------------------------------------------------------------------------


def test_no_tool_mutates_anything(initialized_config: Config) -> None:
    """🔴 The ratified norm: no mutation tools, and adding one is not open.

    Asserted over the tool list itself rather than over the four names, so a
    fifth tool that wrote would have to be named in this test to pass — the
    same construction that makes the endpoint-properties test hold.
    """
    tools = mcp._tool_definitions()
    assert {t["name"] for t in tools} == {
        "list_accounts",
        "query_transactions",
        "spending_summary",
        "get_pipeline_health",
    }, "the shipped subset of the api-contract tool surface"
    forbidden = ("create", "update", "delete", "remove", "write", "set_", "transfer", "pay")
    for tool in tools:
        assert not any(word in tool["name"] for word in forbidden), tool["name"]


def test_a_write_through_the_read_handle_is_refused(initialized_config: Config) -> None:
    """The refusal lives at the file handle, not in a rule a tool author remembers."""
    from sqlalchemy.exc import OperationalError

    from bankmachine.store.engine import reader_connection

    with reader_connection(initialized_config) as conn, pytest.raises(OperationalError):
        conn.exec_driver_sql("CREATE TABLE mutation_attempt (x INTEGER)")


# --------------------------------------------------------------------------
# 🔴 The envelope
# --------------------------------------------------------------------------


def test_every_answer_names_the_environment_it_came_from(initialized_config: Config) -> None:
    """🔴 A flag selects; the envelope confesses.

    A server pointed at sandbox and one pointed at real money produce identical
    answers unless the answer says which. The launch flag is invisible by the
    time a consumer reads a number.
    """
    _seed(initialized_config)

    for name in ("list_accounts", "query_transactions", "spending_summary", "get_pipeline_health"):
        wire = _call(initialized_config, name)["structuredContent"]
        assert wire["environment"] == "sandbox", name
        assert wire["as_of"], name


def test_a_shortfall_rides_the_success_path_as_a_warning(initialized_config: Config) -> None:
    """🔴 Incompleteness is a field, not an exception.

    The dangerous case is a well-formed answer computed over data that stops
    earlier than the question assumes. `gapped` says the missing history is
    ABSENT rather than zero — the difference between "I spent nothing" and "I
    have no record".
    """
    _seed(initialized_config, granted=90)

    wire = _call(initialized_config, "spending_summary")["structuredContent"]

    gapped = [w for w in wire["warnings"] if w["kind"] == "gapped"]
    assert gapped, "a 90-against-730 shortfall was reported as a complete answer"
    assert "absent rather than zero" in gapped[0]["detail"]
    assert gapped[0]["institution"] == "First Platypus Bank"


def test_an_unmeasured_window_is_reported_differently_from_no_shortfall(
    initialized_config: Config,
) -> None:
    """🔴 Null granted means NOT YET KNOWN, and AC-1.3a forbids reading it as complete.

    Reporting nothing here would let a consumer conclude the full requested
    window is present, which is the exact inference the requirement exists to
    prevent.
    """
    _seed(initialized_config, granted=None)

    wire = _call(initialized_config, "get_pipeline_health")["structuredContent"]

    assert wire["rows"][0]["granted_history_days"] is None
    kinds = {w["kind"] for w in wire["warnings"]}
    assert "partial" in kinds
    assert "gapped" not in kinds, "an unknown window was reported as a measured shortfall"


def test_a_degraded_connection_warns_on_every_answer(initialized_config: Config) -> None:
    """Not only on the health tool — a consumer asking about spending must be told too.

    A caveat only available from a tool nobody thought to call is not in the
    payload, which is the whole point of the norm.
    """
    _seed(initialized_config, degraded=True)

    wire = _call(initialized_config, "query_transactions")["structuredContent"]

    assert any(w["kind"] == "degraded" for w in wire["warnings"])


def test_an_empty_datastore_says_so_rather_than_answering_zero(
    initialized_config: Config,
) -> None:
    """🔴 "No transactions" and "no connections enrolled" are different answers.

    Without this, an unconfigured install reports zero spending and looks like a
    frugal month.
    """
    wire = _call(initialized_config, "spending_summary")["structuredContent"]

    assert wire["rows"] == []
    assert any("no connections are enrolled" in w["detail"] for w in wire["warnings"])
    assert wire["coverage"]["transactions"] == 0


# --------------------------------------------------------------------------
# The answers themselves
# --------------------------------------------------------------------------


def test_spending_sums_outflow_only_and_reports_magnitudes(initialized_config: Config) -> None:
    """Refunds and income are excluded, because "spending" asks about outflow.

    The sign convention is what makes that a filter rather than a per-account
    special case.
    """
    _seed(initialized_config)

    rows = _call(initialized_config, "spending_summary")["structuredContent"]["rows"]

    by_category = {r["category"]: r["spent_minor_units"] for r in rows}
    assert by_category == {"GENERAL_MERCHANDISE": 8940, "FOOD_AND_DRINK": 1200}
    assert "INCOME" not in by_category, "a deposit was counted as spending"
    assert all(r["spent_minor_units"] > 0 for r in rows), "magnitudes, not signed totals"


def test_amount_fields_say_they_are_minor_units(initialized_config: Config) -> None:
    """🔴 A consumer dividing by 100 without knowing would be wrong by two orders
    of magnitude, and the number would still look plausible."""
    _seed(initialized_config)

    rows = _call(initialized_config, "query_transactions")["structuredContent"]["rows"]

    assert "amount_minor_units" in rows[0]
    assert all("amount" not in key or "minor_units" in key for key in rows[0])


def test_a_failing_tool_reports_an_error_without_closing_the_session(
    initialized_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 An unhandled exception would close the pipe mid-session.

    The operator would see their tool "disappear" rather than fail, which is
    indistinguishable from a broken install. `isError` is the channel for this.

    🔴 And the exception itself does not cross the boundary. This test used to
    assert that it DID -- `"the datastore went away" in text` -- which pinned
    the behaviour `api-contract.md` § Error Model forbids: no stack traces and
    no internal identifiers. It matters most for the failure it was written
    against: a SQLAlchemy error stringifies to the failing SELECT and its bound
    parameters, so the leak is the schema plus the operator's own money.
    """

    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("SELECT secret FROM vault -- the datastore went away")

    monkeypatch.setattr(query, "list_accounts", explode)

    replies = _converse(
        initialized_config,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "list_accounts", "arguments": {}},
            },
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        ],
    )

    result = replies[1]["result"]
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "the datastore went away" not in text, "the raw exception message crossed the boundary"
    assert "SELECT" not in text, "the failing query crossed the boundary"
    assert "RuntimeError" not in text, "an internal identifier crossed the boundary"
    # Still a usable failure: a stable code to branch on and a remedy to act on.
    assert result["structuredContent"]["error"]["code"] == "internal_error"
    assert "store status" in text
    # The session survived: the client can still use the server.
    assert "tools" in replies[2]["result"]


def test_an_unknown_tool_is_refused_by_name(initialized_config: Config) -> None:
    replies = _converse(
        initialized_config,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "delete_everything", "arguments": {}},
            },
        ],
    )

    assert replies[1]["error"]["code"] == -32601


def test_both_content_forms_are_sent(initialized_config: Config) -> None:
    """A client that only renders text would otherwise show an empty result."""
    _seed(initialized_config)

    result = _call(initialized_config, "list_accounts")

    assert result["structuredContent"]["rows"]
    assert json.loads(result["content"][0]["text"])["rows"]


# --------------------------------------------------------------------------
# AC-ARCH.3 — starts against an empty or missing datastore, and REPORTS it
# --------------------------------------------------------------------------


def test_the_server_starts_and_answers_when_the_datastore_is_missing(
    config: Config,
) -> None:
    """🔴 AC-ARCH.3, which an earlier version of this server inverted.

    It refused to start — using `inspect()` to do it, whose own docstring says it
    exists so the server can *report* that state. The requirement reads this way
    because a client launches this as a subprocess: a server that exits on
    startup appears as a tool that silently does not show up, and the operator
    has no way to ask why. One that starts and answers "there is no datastore"
    can be asked.
    """
    assert not config.datastore_path.exists()

    replies = _converse(
        config,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "get_pipeline_health", "arguments": {}},
            },
        ],
    )

    assert "serverInfo" in replies[0]["result"], "the server refused to start"
    result = replies[1]["result"]
    assert result["isError"] is False, "a missing datastore reached the client as a failed call"
    wire = result["structuredContent"]
    assert wire["rows"] == []
    assert any("not readable" in w["detail"] for w in wire["warnings"])
    assert wire["coverage"]["transactions"] == 0


def test_every_tool_answers_against_a_missing_datastore(config: Config) -> None:
    """Not only the health tool.

    A consumer that called `list_accounts` first would otherwise see a crash
    where the health tool would have explained itself.
    """
    for name in ("list_accounts", "query_transactions", "spending_summary", "get_pipeline_health"):
        wire = _call(config, name)["structuredContent"]
        assert wire["rows"] == [], name
        assert any(w["kind"] == "partial" for w in wire["warnings"]), name


@pytest.mark.parametrize(
    ("tool", "arguments", "expects_window"),
    [
        ("query_transactions", {"since": "2024-01-01", "until": "2024-06-30"}, True),
        ("spending_summary", {"since": "2024-01-01", "until": "2024-06-30"}, True),
        ("list_accounts", {}, False),
        ("get_pipeline_health", {}, False),
    ],
)
def test_an_unreadable_store_still_reports_whether_the_tool_takes_a_window(
    config: Config, tool: str, arguments: dict[str, Any], expects_window: bool
) -> None:
    """🔴 The key's PRESENCE is a fact about the tool, not about the store.

    The contract fixes absence as "this tool takes no window". So a windowed
    tool must still emit the key when the datastore cannot be read, with null
    effective bounds — the true statement being "your window and this store do
    not overlap", which is exactly the state of a store that cannot be read.
    Omitting it here would say something false about the TOOL at precisely the
    moment a consumer branching on the key would take the wrong branch.

    Pinned because nothing did: every other `effective_window` assertion in this
    file runs against an initialized store, so the old shape (key omitted) and
    the new one passed the AC-ARCH.3 tests identically. `_unusable` is also the
    one construction site that hand-builds a `Window` with no caveats, which
    makes it the only place left that can emit an unexplained empty window.
    """
    assert not config.datastore_path.exists()

    wire = _call(config, tool, arguments)["structuredContent"]

    if not expects_window:
        assert "effective_window" not in wire, tool
        return

    assert wire["effective_window"] == {
        "requested": {"since": "2024-01-01", "until": "2024-06-30"},
        "effective": {"since": None, "until": None},
    }, tool
    # The `partial` warning is what explains the emptiness here; a window caveat
    # would be a second voice saying the same thing about a different subject.
    assert _request_kinds(wire) == [], tool
    assert any(w["kind"] == "partial" for w in wire["warnings"]), tool


def test_the_missing_datastore_warning_says_the_zeroes_mean_nothing_read(
    config: Config,
) -> None:
    """🔴 Zero and unreadable are different answers.

    "You spent nothing" and "I could not read anything" are the same payload
    unless the warning distinguishes them, and only one of them is a fact about
    the operator's money.
    """
    wire = _call(config, "spending_summary")["structuredContent"]

    detail = " ".join(w["detail"] for w in wire["warnings"])
    assert "nothing could be read" in detail
    assert "store init" in detail, "the operator is not told how to fix it"


def test_cmd_mcp_itself_starts_against_a_missing_datastore(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 The function that did the refusing, which nothing exercised.

    Every other AC-ARCH.3 test here goes through `_converse`, whose body is
    `mcp.serve(...)` — and `serve` never carried the refusal. `cmd_mcp` raised
    *before* calling it. So the sibling test whose docstring says "the server
    refused to start" passes identically against the unfixed tree, and re-adding
    the raise today would leave the whole suite green.

    This calls `cmd_mcp` directly with `serve` replaced by a sentinel, so the
    assertion is that the handler REACHED serving — which is the thing AC-ARCH.3
    is about and the thing the inversion broke.
    """
    served: list[Config] = []

    def record(cfg: Config, **_: Any) -> int:
        served.append(cfg)
        return 0

    monkeypatch.setattr(mcp, "serve", record)
    assert not config.datastore_path.exists()

    exit_code = mcp.cmd_mcp(config, argparse.Namespace())

    assert exit_code == 0
    assert served == [config], (
        "cmd_mcp refused to serve a missing datastore, which inverts AC-ARCH.3: a client "
        "launches this as a subprocess, so refusing shows up as a tool that silently does "
        "not appear"
    )


def test_cmd_mcp_serves_a_healthy_datastore_too(
    initialized_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mirror half, without which "always serve" is satisfied by never checking."""
    served: list[Config] = []

    def record(cfg: Config, **_: Any) -> int:
        served.append(cfg)
        return 0

    monkeypatch.setattr(mcp, "serve", record)

    assert mcp.cmd_mcp(initialized_config, argparse.Namespace()) == 0
    assert served == [initialized_config]


def test_the_domain_filter_keeps_one_health_row_per_connection(
    initialized_config: Config,
) -> None:
    """A second sync domain would otherwise duplicate every connection.

    `sync_state` is keyed on (connection, domain), and balances and holdings will
    advance on their own schedules. Seeded here rather than waited for, so the
    join is asserted now instead of the day the second domain lands.
    """
    from bankmachine.store.schema import sync_state

    _seed(initialized_config)
    with writer_connection(initialized_config) as conn:
        conn.execute(
            sync_state.insert().values(
                connection_id=1, domain="balances", cursor="b1", updated_at=now_utc()
            )
        )

    rows = _call(initialized_config, "get_pipeline_health")["structuredContent"]["rows"]

    assert len(rows) == 1, "a second sync domain duplicated the connection"


# --------------------------------------------------------------------------
# The date window — JSON has no date, so this boundary is where one is made
# --------------------------------------------------------------------------


def test_a_date_window_filters_rather_than_failing(initialized_config: Config) -> None:
    """🔴 The regression. Every windowed question was unanswerable without this.

    `since` and `until` arrive as JSON strings and get bound to a `CalendarDate`
    column that refuses anything but a `date`, so for a while every window --
    every question this server exists to answer -- came back as a
    `StatementError` carrying a SQL fragment. Nothing caught it because every
    other test here calls these tools with NO arguments at all.

    Asserted as *discrimination* rather than as "it did not error": a window
    that excludes the data must come back empty while one that includes it comes
    back full. A parse that silently produced the wrong date would satisfy the
    weaker check and fail this one.
    """
    _seed(initialized_config)
    today = now_utc().date()

    inside = _call(
        initialized_config,
        "spending_summary",
        {"since": str(today - timedelta(days=1)), "until": str(today + timedelta(days=1))},
    )
    before = _call(
        initialized_config,
        "spending_summary",
        {"since": "2020-01-01", "until": "2020-12-31"},
    )

    assert inside["isError"] is False
    assert before["isError"] is False
    assert inside["structuredContent"]["rows"], "a window containing the data returned nothing"
    assert before["structuredContent"]["rows"] == [], (
        "a window ending in 2020 returned rows dated today, so the bound never reached the query"
    )


def test_a_transaction_window_filters_rather_than_failing(initialized_config: Config) -> None:
    """The same boundary on the other windowed tool, which has its own call site."""
    _seed(initialized_config)
    today = now_utc().date()

    inside = _call(initialized_config, "query_transactions", {"since": str(today)})
    before = _call(initialized_config, "query_transactions", {"until": "2020-12-31"})

    assert inside["isError"] is False
    assert inside["structuredContent"]["rows"], "today's transactions were filtered out"
    assert before["structuredContent"]["rows"] == [], "an `until` bound in 2020 returned rows"


def test_a_malformed_date_is_refused_with_a_sentence_a_caller_can_act_on(
    initialized_config: Config,
) -> None:
    """🔴 The caller is the one who can fix this, so the message is written to them.

    A model asking about "August 2024" must be told the form to use. What it got
    instead was `StatementError: (bankmachine.store.types.TemporalError) ...`
    followed by the SELECT -- which names no argument, suggests no correction,
    and leaks the schema to whoever is reading.
    """
    _seed(initialized_config)

    result = _call(initialized_config, "spending_summary", {"since": "August 2024"})

    assert result["isError"] is True
    message = result["content"][0]["text"]
    assert "since" in message, "the message does not say which argument was wrong"
    assert "YYYY-MM-DD" in message, "the message does not say what form to use"
    assert "SELECT" not in message, "the refusal leaked the query"
    assert not message.startswith("BadArgumentError"), (
        "the exception class name is noise to the caller being asked to fix its call"
    )


@pytest.mark.parametrize(
    ("arguments", "field"),
    [
        ({"since": 20240801}, "since"),
        ({"until": ["2024-08-01"]}, "until"),
        ({"limit": "lots"}, "limit"),
        ({"limit": True}, "limit"),
        ({"account_id": "1"}, "account_id"),
        # A cursor is a string on the wire, so a caller sending the whole
        # `truncation` block back — an ordinary slip — must be told which part
        # of it to send instead.
        ({"cursor": 7}, "cursor"),
        ({"cursor": {"next_cursor": "x"}}, "cursor"),
    ],
)
def test_an_argument_of_the_wrong_json_type_is_refused_by_name(
    initialized_config: Config, arguments: dict[str, Any], field: str
) -> None:
    """Every argument crossing this boundary, not only the two that broke.

    `limit` reached `int(...)` unchecked and `account_id` reached the query
    unchecked; both had the same defect as the dates and neither had been tried.
    `True` is included because a bool IS an int in Python, so a caller sending
    JSON `true` for a count would otherwise get a limit of 1.
    """
    _seed(initialized_config)

    result = _call(initialized_config, "query_transactions", arguments)

    assert result["isError"] is True
    assert field in result["content"][0]["text"]


# --------------------------------------------------------------------------
# The typing that makes the boundary refuse an unparsed value at all
# --------------------------------------------------------------------------

_UNNARROWED = """
from bankmachine import query
from bankmachine.config import Config


def dispatch(config: Config, arguments: dict[str, object]) -> query.Answer:
    return query.list_transactions(config, since=arguments.get("since"))
"""

_NARROWED = """
from datetime import date

from bankmachine import query
from bankmachine.config import Config


def dispatch(config: Config, arguments: dict[str, object]) -> query.Answer:
    raw = arguments.get("since")
    since = date.fromisoformat(raw) if isinstance(raw, str) else None
    return query.list_transactions(config, since=since)
"""


@pytest.fixture(scope="module")
def dispatch_report(tmp_path_factory: pytest.TempPathFactory) -> str:
    """One mypy run over both snippets, because mypy is the slow part.

    What this pins is `query.list_transactions`'s own signature: it takes
    `date | None`, so an `object` cannot reach it. That is the half a runtime
    assertion cannot check, because the call never happens -- the checker stops
    it first.

    🔴 It does NOT pin `_dispatch_tool`'s `dict[str, object]`, and the two are
    easy to conflate: these snippets declare their own signature, so widening
    the real one back to `dict[str, Any]` leaves this test green. The annotation
    itself is pinned separately, below.

    🔴 The two snippet filenames deliberately share no substring. Naming them
    `narrowed.py` and `unnarrowed.py` made the positive control match the
    NEGATIVE file -- `in` cannot tell a name from a name that contains it, which
    is the containment trap recorded in `learnings.md`.
    """
    workspace = tmp_path_factory.mktemp("dispatch")
    (workspace / "forwards_raw.py").write_text(_UNNARROWED, encoding="utf-8")
    (workspace / "parses_first.py").write_text(_NARROWED, encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "--no-incremental",
            "--no-error-summary",
            "--cache-dir",
            str(workspace / ".mypy_cache"),
            str(workspace / "forwards_raw.py"),
            str(workspace / "parses_first.py"),
        ],
        cwd=Path(__file__).parent.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, (
        "mypy accepted a dispatch that forwards an unnarrowed JSON value into the query "
        f"layer. That is the whole mechanism:\n{result.stdout}{result.stderr}"
    )
    return result.stdout


def test_an_unnarrowed_argument_cannot_reach_the_query_layer(dispatch_report: str) -> None:
    """🔴 The property, not the instance: no call site can forward raw JSON."""
    errors = [line for line in dispatch_report.splitlines() if "forwards_raw.py" in line]
    assert any("since" in line for line in errors), (
        f"mypy rejected the snippet, but not for the unnarrowed argument:\n{errors}"
    )


def test_mypy_accepts_the_dispatch_that_parses_first(dispatch_report: str) -> None:
    """The positive control.

    Without it this passes just as happily when mypy failed for an unrelated
    reason -- a broken import, say -- which is the one way a type-check
    assertion goes quietly green.
    """
    assert not [line for line in dispatch_report.splitlines() if "parses_first.py" in line], (
        f"mypy rejected the correct dispatch:\n{dispatch_report}"
    )


def test_the_dispatch_bag_is_typed_object_so_narrowing_cannot_be_skipped() -> None:
    """🔴 The annotation is the mechanism, so the annotation is what gets asserted.

    `dict[str, Any]` lets every value flow into the query layer unchallenged
    with mypy strict silent -- which is precisely how raw JSON strings reached a
    `CalendarDate` column and made every windowed question unanswerable while
    553 tests passed. Typed `object`, an unnarrowed value cannot be passed at
    all and the checker refuses it.

    Asserted on the annotation rather than through mypy because the snippets
    that run the checker declare their own signature and so cannot see this one.
    """
    annotation = inspect.signature(mcp._dispatch_tool).parameters["arguments"].annotation

    assert annotation == "dict[str, object]", (
        f"the dispatch bag is annotated {annotation!r}. Widened to Any, an unparsed JSON "
        f"value reaches the query layer and mypy strict says nothing."
    )


def test_an_argument_the_tool_does_not_advertise_is_refused(initialized_config: Config) -> None:
    """🔴 A misspelled bound must not quietly become no bound at all.

    Every tool publishes `additionalProperties: False`, and until this was
    enforced an unknown key was dropped: `{"sinceX": "2024-09-09"}` returned the
    ALL-TIME aggregate, which is byte-identical to the windowed answer the
    caller believed it had asked for. Nothing in the response said which it was.
    """
    _seed(initialized_config)

    result = _call(initialized_config, "spending_summary", {"sinceX": "2024-09-09"})

    assert result["isError"] is True
    message = result["content"][0]["text"]
    assert "sinceX" in message, "the refusal does not name the argument it rejected"
    # 🔴 An exact set, not `"since" in message`: the refusal already contains
    # 'sinceX', so that substring can never fail. When one valid value contains
    # another as text, `in` cannot tell them apart.
    _, _, accepted = message.partition("It accepts: ")
    assert accepted, f"the refusal does not say what is accepted: {message!r}"
    assert set(accepted.strip().split(", ")) == {"since", "until"}, (
        f"the refusal offered {accepted.strip()!r}"
    )
    assert result["structuredContent"]["error"]["code"] == "invalid_argument"


def test_the_instructions_name_every_field_the_envelope_actually_carries(
    initialized_config: Config,
) -> None:
    """🔴 A closed list in prose is one that stops matching the payload it describes.

    The instructions are the ONE text a consuming agent reads before it calls
    anything, and they enumerated the envelope as a closed set. Adding `build`
    to the wire without adding it here would leave the only document the agent
    sees actively denying the field exists -- which is exactly how a stale-build
    round happens again, since `build` is what would have prevented the last one.

    Asserted against the real envelope rather than a second hand-written list,
    because a second list is one that stops matching the first.

    🔴 **Over the UNION of every tool's envelope, never one sample.** This read
    `list_accounts` alone, which is the one tool carrying neither
    `effective_window` nor `truncation` — so two whole chunks added two envelope
    keys and four warning kinds and this guard passed unchanged, while the only
    text a consuming agent reads at handshake actively denied they existed. A
    check that samples one instance of the thing it generalises over is a check
    whose bad news never arrives, which is the trap `learnings.md` records twice.
    """
    _seed(initialized_config)
    instructions = mcp._instructions(initialized_config)

    envelope: set[str] = set()
    for definition in mcp._tool_definitions():
        envelope |= set(_call(initialized_config, definition["name"])["structuredContent"])
    assert {"effective_window", "truncation"} <= envelope, (
        "the union lost the windowed/capped keys, so this guard is back to sampling"
    )

    missing = sorted(key for key in envelope if f"`{key}`" not in instructions)

    assert not missing, (
        f"the envelope carries {missing} but the instructions never name them; "
        f"an agent reading only the instructions does not know they exist"
    )


def test_the_instructions_name_every_warning_kind_the_vocabulary_defines(
    initialized_config: Config,
) -> None:
    """🔴 The kinds are the half an agent is told to branch on, and they were short by four.

    The envelope guard above pins FIELDS. Nothing pinned KINDS, so the four
    request-scoped kinds this cycle added were absent from the handshake text
    while `warnings` was the thing that text tells the reader to check first.
    Derived from the vocabulary rather than from a second list here, for the
    reason the vocabulary exists at all.
    """
    instructions = mcp._instructions(initialized_config)

    missing = sorted(k for k in query.WARNING_KINDS if f"`{k}`" not in instructions)

    assert not missing, (
        f"the vocabulary defines {missing} but the instructions never name them; "
        f"an agent told to read `warnings` cannot act on a kind it was never given"
    )


def test_the_handshake_reports_the_running_build(initialized_config: Config) -> None:
    """🔴 `serverInfo` is what a client shows a human BEFORE any tool is called.

    All three keys were untested, including `version` -- which stopped being the
    literal "0.1.0" and became package metadata, so it now reports "unknown"
    for a source tree that was never installed. An untested handshake is how a
    client-facing identity drifts from the code that serves it.
    """
    _seed(initialized_config)
    replies = _converse(
        initialized_config,
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"},
                },
            }
        ],
    )
    server_info = replies[0]["result"]["serverInfo"]
    identity = build_id.build_identity()

    assert server_info["version"] == identity.version
    assert server_info["commit"] == identity.commit
    assert server_info["dirty"] == identity.dirty
    # The handshake and the envelope must not be able to disagree about the
    # build: two readings of one process are one fact, and a client that showed
    # a human one commit while an agent read another would be unfalsifiable.
    envelope = _call(initialized_config, "list_accounts")["structuredContent"]["build"]
    assert envelope == {
        "version": server_info["version"],
        "commit": server_info["commit"],
        "dirty": server_info["dirty"],
    }


def test_a_build_that_is_not_a_git_checkout_still_serves(initialized_config: Config) -> None:
    """AC 5, through a real tool call rather than only at the unit level.

    An installed copy outside a checkout has no commit to report, and the
    requirement is that it answers anyway -- reporting the absence rather than
    failing or guessing. Verified end to end because the failure mode this
    guards is the server refusing to start, which a unit test cannot see.
    """
    _seed(initialized_config)
    build_id.build_identity.cache_clear()
    try:
        with mock.patch.object(build_id, "_git", return_value=None):
            result = _call(initialized_config, "list_accounts")

            assert result.get("isError") is not True, (
                f"a non-checkout build refused to serve: {result}"
            )
            assert result["structuredContent"]["build"]["commit"] is None
            assert result["structuredContent"]["build"]["dirty"] is None
            assert result["structuredContent"]["rows"], "it reported no data, not just no commit"
    finally:
        build_id.build_identity.cache_clear()


def test_the_build_is_captured_at_import_not_on_first_call() -> None:
    """🔴 A cache that fills on first CALL leaves open the window this closes.

    Process starts, operator merges, first request then reports the merged
    commit while the process is serving pre-merge code -- the exact defect, one
    step later. `lru_cache` alone gives precisely that; the module-level capture
    is what removes it, and only a fresh interpreter can observe the difference
    because this session imported the module long ago.
    """
    probe = (
        "from bankmachine import build_id\n"
        # Broken AFTER import. Irrelevant if capture already happened; fatal if
        # the first call is what reaches for git.
        "def boom(*a):\n"
        "    raise AssertionError('git ran after import')\n"
        "build_id._git = boom\n"
        "build_id.build_identity()\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60
    )

    # The discriminator is the exit code, and it holds whether or not this
    # happens to be a git checkout: if capture were lazy, the call would reach
    # the raiser either way.
    assert completed.returncode == 0, (
        f"the build identity was captured lazily, not at import: {completed.stderr[-600:]}"
    )


def test_the_permitted_arguments_are_read_from_the_advertised_schema() -> None:
    """Derived, not restated — a second list is one that stops matching the first."""
    for definition in mcp._tool_definitions():
        advertised = frozenset(definition["inputSchema"].get("properties", {}))

        assert mcp._permitted_arguments(definition["name"]) == advertised


def test_a_keyerror_beneath_the_query_layer_is_not_reported_as_an_unknown_tool(
    initialized_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 It said "no tool named 'spending_summary'" — a false statement about a real tool.

    The unknown-tool guard used to wrap the handler CALL, so any `KeyError`
    raised inside the query layer surfaced as JSON-RPC -32601. A consumer acting
    on that would stop calling a tool that exists and works.
    """

    def explode(*args: Any, **kwargs: Any) -> None:
        raise KeyError("a column the deriver expected")

    monkeypatch.setattr(query, "spending_by_category", explode)

    result = _call(initialized_config, "spending_summary")

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "internal_error"


def test_every_unrecognized_argument_is_named_at_once(initialized_config: Config) -> None:
    """Two typos should cost one round trip, not two."""
    _seed(initialized_config)

    result = _call(initialized_config, "spending_summary", {"sinceX": "x", "untilX": "y"})

    assert result["isError"] is True
    message = result["content"][0]["text"]
    assert "sinceX" in message and "untilX" in message


def test_a_limit_outside_the_servable_range_is_refused_not_clamped(
    initialized_config: Config,
) -> None:
    """🔴 A clamp answers a question nobody asked, and says nothing about it.

    `limit: 0` served one row, which reads as a plausible complete answer to a
    narrow question — worse than the obviously-wrong hundred it replaced. Above
    the ceiling is the same problem pointed the other way: a caller asking for
    everything gets a page and no word that it is one.
    """
    _seed(initialized_config)

    for value in (0, -5, query.MAX_ROWS + 1):
        result = _call(initialized_config, "query_transactions", {"limit": value})

        assert result["isError"] is True, f"limit={value} was answered rather than refused"
        assert result["structuredContent"]["error"]["code"] == "invalid_argument"
        assert "limit" in result["content"][0]["text"]


def test_a_window_whose_end_precedes_its_start_is_refused(initialized_config: Config) -> None:
    """🔴 The slip a person actually makes, whose wrong answer is believable.

    Swapping the two bounds selects nothing, and "you spent nothing" is an
    entirely ordinary thing for a month to be. There is no window a transposed
    pair could mean, so there is nothing to guess at and nothing to clamp to.
    """
    _seed(initialized_config)

    for tool in ("spending_summary", "query_transactions"):
        result = _call(initialized_config, tool, {"since": "2026-07-31", "until": "2026-07-01"})

        assert result["isError"] is True, f"{tool} answered a backwards window"
        assert result["structuredContent"]["error"]["code"] == "invalid_argument"
        assert "swapped" in result["content"][0]["text"]


def test_an_account_id_below_one_is_refused(initialized_config: Config) -> None:
    """Row ids start at 1, so 0 and negatives name nothing and can only be a mistake."""
    _seed(initialized_config)

    result = _call(initialized_config, "query_transactions", {"account_id": 0})

    assert result["isError"] is True
    assert "account_id" in result["content"][0]["text"]


def test_an_account_id_that_names_no_account_is_refused_not_answered_empty(
    initialized_config: Config,
) -> None:
    """🔴 A typo in an argument name was loud; a typo in an account id was silent.

    `{"account_id": 999}` returned `rows: []` with no warning -- byte-identical
    to a real account that happened to be quiet in the window. "No transactions"
    is an ordinary thing for an account to have, so nothing about the answer
    invited a second look. The refusal for an unadvertised argument NAME already
    existed; this is the same mistake one field over, and the quieter one.
    """
    _seed(initialized_config)

    result = _call(initialized_config, "query_transactions", {"account_id": 999})

    assert result["isError"] is True, "an account id naming nothing was answered"
    assert result["structuredContent"]["error"]["code"] == "invalid_argument"
    message = result["content"][0]["text"]
    # 🔴 The whole phrase, not `"999" in message`: an id is a bare integer and
    # would match inside any longer number a future refusal happened to carry.
    assert "account_id 999 does not exist" in message, message
    assert "SELECT" not in message, "the refusal leaked the query"


def test_an_account_that_exists_but_is_quiet_in_the_window_still_answers_empty(
    initialized_config: Config,
) -> None:
    """🔴 The control. Refusing an id that names nothing must not eat the honest empty.

    An account that exists and simply had no activity in the window has a
    correct answer, and it is `rows: []`. Without this test, narrowing the
    existence check by one character -- or applying it to the window rather than
    to the id -- turns a true answer into a refusal and nothing notices.
    """
    _seed(initialized_config)

    result = _call(
        initialized_config,
        "query_transactions",
        {"account_id": 1, "since": "2019-01-01", "until": "2019-12-31"},
    )

    assert result.get("isError") is not True, f"a real account was refused: {result}"
    assert result["structuredContent"]["rows"] == []


def test_every_answer_says_which_build_produced_it(initialized_config: Config) -> None:
    """🔴 The server is a subprocess launched at connect time, so this is not cosmetic.

    A client holds whatever code existed when it connected. Without a stamp in
    the response, a session testing a stale build produces a clean pass and
    reads it as confirmation -- which is what happened on 2026-09-08, caught
    only because a refusal string had changed between the two builds. A fix that
    changed no observable string would not have been caught at all.
    """
    _seed(initialized_config)

    for tool in ("list_accounts", "query_transactions", "spending_summary", "get_pipeline_health"):
        build = _call(initialized_config, tool)["structuredContent"]["build"]

        # An exact set: a missing key and a null value are different answers, and
        # `in` on a subset would accept a payload that dropped one of the three.
        assert set(build) == {"version", "commit", "dirty"}, f"{tool} reported {build}"
        assert build["version"], f"{tool} reported no version"


def test_the_running_build_is_captured_once_and_not_re_read_per_request(
    initialized_config: Config,
) -> None:
    """🔴 The load-bearing requirement, not an optimization.

    A hash read per request reports the REPOSITORY's current HEAD. A server left
    running across a merge would then answer with the merged commit while
    serving pre-merge code -- reporting itself current at exactly the moment it
    is not, which is the whole defect this stamp exists to expose. Re-reading it
    would build that bug into the instrument meant to catch it.
    """
    _seed(initialized_config)
    first = _call(initialized_config, "list_accounts")["structuredContent"]["build"]

    # 🔴 No `cache_clear()` here, deliberately. Clearing it would couple this
    # test to `lru_cache` as the MECHANISM: remove the decorator and the test
    # dies on `AttributeError` at its setup line, reporting a different defect
    # than the one it names and never reaching the assertion below. What is
    # under test is the behaviour -- after the first answer, no request touches
    # git -- which stays true however the capture is implemented.
    with mock.patch.object(build_id, "_git", side_effect=AssertionError("git ran per request")):
        result = _call(initialized_config, "query_transactions")

    assert result.get("isError") is not True, (
        f"a second request re-read the build identity instead of reusing it: {result}"
    )
    assert result["structuredContent"]["build"] == first, (
        "the build stamp changed between two calls to one process"
    )


def test_an_unidentifiable_build_reports_null_rather_than_claiming_it_is_clean() -> None:
    """🔴 `dirty: false` is a CLAIM, and an unknown build supports no such claim.

    Reporting a reassuring default where there is no evidence is the exact
    failure this product exists to prevent, one level up: an answer that looks
    trustworthy because nothing said otherwise. Absence is reported as absence.
    """
    build_id.build_identity.cache_clear()
    try:
        with mock.patch.object(build_id, "_git", return_value=None):
            identity = build_id.build_identity()

        assert identity.commit is None
        assert identity.dirty is None, "an unidentifiable build claimed its tree was clean"
        assert identity.version, "the version is known even when the commit is not"
    finally:
        build_id.build_identity.cache_clear()


def test_a_modified_working_tree_is_reported_dirty() -> None:
    """A hash alone would be a lie by omission about code that is running.

    Uncommitted edits are what the operator is most likely to be testing, and a
    bare commit says the process is running that commit when it is not.
    """
    build_id.build_identity.cache_clear()
    try:
        with mock.patch.object(build_id, "_git", side_effect=["abc1234", " M src/x.py"]):
            assert build_id.build_identity().dirty is True
        build_id.build_identity.cache_clear()
        with mock.patch.object(build_id, "_git", side_effect=["abc1234", ""]):
            assert build_id.build_identity().dirty is False
    finally:
        build_id.build_identity.cache_clear()


def test_the_merchant_field_is_declared_as_the_aggregators_guess() -> None:
    """🔴 A value a consumer cannot tell is unvalidated is one it will trust.

    `merchant` sits beside `description` in every row with nothing to say that
    one is the institution's own string and the other is a third-party guess
    which reads "FUN" for "SparkFun". Marking the provenance settles nothing
    about what a merchant *is* -- that is a larger decision -- but it stops the
    field being presented as though it had been checked.
    """
    described = {d["name"]: d["description"] for d in mcp._tool_definitions()}

    assert "merchant" in described["query_transactions"]
    assert "unvalidated" in described["query_transactions"]


def test_the_row_ceiling_is_the_contracted_one() -> None:
    """🔴 The cap is a contract term, and three documents fix it at ~500.

    `api-contract.md` calls it "a contract term, not a tuning knob" under
    AC-9.1; `nonfunctional-requirements.md` makes it what keeps the sub-second
    target reachable; and `security-model.md` names it as the mitigation for
    unrestricted resource consumption (OWASP API4). A cap raised in code alone
    silently withdraws a declared security control, which is why this asserts
    the number rather than merely that some ceiling exists.
    """
    assert query.MAX_ROWS == 500


def test_the_advertised_bounds_match_the_enforced_ones() -> None:
    """A caller should learn the bounds from the schema, not by being refused."""
    limit = next(
        d["inputSchema"]["properties"]["limit"]
        for d in mcp._tool_definitions()
        if d["name"] == "query_transactions"
    )

    assert limit["minimum"] == 1
    assert limit["maximum"] == query.MAX_ROWS


# --------------------------------------------------------------------------
# The window the answer actually covered (#16)
# --------------------------------------------------------------------------


def _request_kinds(wire: dict[str, Any]) -> list[str]:
    """🔴 The request-scoped warnings only, compared as a whole list.

    Never a substring test: `window_starts_before_coverage` and
    `window_extends_past_coverage` share a prefix, and `learnings.md` records two
    occasions where `in` passed against the value the assertion was written to
    exclude. Filtering to the request-scoped kinds and comparing the list also
    pins ABSENCE, which is the half that catches a warning firing on every
    response — the defect these kinds exist to fix.

    🔴 Keyed on what the vocabulary DECLARES request-scoped, not on how the kinds
    are spelled. Deriving the set from a shared `window_` prefix worked only
    while every request-scoped kind was about a window: `rows_truncated` is
    request-scoped and carries no such prefix, so a prefix filter would have
    silently exempted it from every absence assertion below — and absence is the
    half these assertions exist for.
    """
    request_scoped = set(query.REQUEST_SCOPED_KINDS)
    assert request_scoped, "the request-scoped kinds vanished from the vocabulary"
    return [w["kind"] for w in wire["warnings"] if w["kind"] in request_scoped]


@pytest.mark.parametrize("tool", ["query_transactions", "spending_summary"])
def test_a_windowed_answer_states_the_window_it_covered(
    initialized_config: Config, tool: str
) -> None:
    """Both windowed tools, because one that forgot would be invisible.

    The fixture's transactions are all dated today, so a request reaching back
    to 2024 crosses the coverage boundary by nearly two years.
    """
    _seed(initialized_config)
    today = str(now_utc().date())

    wire = _call(initialized_config, tool, {"since": "2024-01-01", "until": today})[
        "structuredContent"
    ]

    assert _request_kinds(wire) == ["window_starts_before_coverage"], tool
    assert wire["effective_window"]["requested"]["since"] == "2024-01-01"
    assert wire["effective_window"]["effective"]["since"] == today
    assert wire["effective_window"]["effective"]["until"] == today


@pytest.mark.parametrize("tool", ["query_transactions", "spending_summary"])
def test_a_window_inside_coverage_carries_no_window_warning(
    initialized_config: Config, tool: str
) -> None:
    """The silence that makes the warning worth reading.

    An acceptance round measured the connection-scoped `gapped` notice arriving
    identically on a covered window, an uncovered one and a future one, which is
    why it says nothing about any of them. If these fired here too they would
    inherit that uselessness on the day they shipped.
    """
    _seed(initialized_config)
    today = str(now_utc().date())

    wire = _call(initialized_config, tool, {"since": today, "until": today})["structuredContent"]

    assert _request_kinds(wire) == [], tool
    assert wire["effective_window"]["effective"] == {"since": today, "until": today}


def test_a_future_window_says_so_rather_than_reading_as_a_quiet_period(
    initialized_config: Config,
) -> None:
    """Measured: a 2027 window returned `rows: []` indistinguishable from real quiet."""
    _seed(initialized_config)

    wire = _call(
        initialized_config,
        "query_transactions",
        {"since": "2027-01-01", "until": "2027-12-31"},
    )["structuredContent"]

    assert wire["rows"] == []
    assert _request_kinds(wire) == ["window_extends_past_coverage"]
    assert wire["effective_window"]["effective"] == {"since": None, "until": None}


def test_an_empty_answer_outside_coverage_is_told_apart_from_a_zero(
    initialized_config: Config,
) -> None:
    """🔴 The single most believable wrong answer this surface can produce.

    `spending_summary` over a window that precedes coverage returns no rows.
    "You spent nothing" and "this is not knowable" were the same payload; the
    effective window plus its warning are what separate them.
    """
    _seed(initialized_config)

    wire = _call(
        initialized_config,
        "spending_summary",
        {"since": "2024-01-01", "until": "2024-06-30"},
    )["structuredContent"]

    assert wire["rows"] == []
    assert _request_kinds(wire) == ["window_starts_before_coverage"]
    assert wire["effective_window"]["effective"] == {"since": None, "until": None}


@pytest.mark.parametrize("tool", ["list_accounts", "get_pipeline_health"])
def test_an_unwindowed_tool_reports_no_window_at_all(initialized_config: Config, tool: str) -> None:
    """The key is ABSENT, not null.

    A null `effective_window` on a tool that takes no window would invite a
    consumer to reconcile one that does not exist. Absence is the honest shape,
    and it is what `requested_window=None` at the construction site produces.
    """
    _seed(initialized_config)

    wire = _call(initialized_config, tool)["structuredContent"]

    assert "effective_window" not in wire, tool
    assert _request_kinds(wire) == [], tool


def test_the_two_windowed_tools_describe_the_window_in_one_shared_sentence() -> None:
    """One mechanism, one description — the drift #16's own triage note predicted.

    Two tools explaining one behaviour in two sentences is how the sentences
    stop agreeing. Pinning the shared text is what makes a divergence a test
    failure rather than a slow documentation rot.
    """
    described = {
        d["name"]: d["description"]
        for d in mcp._tool_definitions()
        if d["name"] in {"query_transactions", "spending_summary"}
    }

    assert len(described) == 2
    for name, text in described.items():
        assert mcp._WINDOW_NOTE in text, name
    for name, text in described.items():
        assert "ABSENT rather than zero" in text, name


# --------------------------------------------------------------------------
# Truncation, read as a consumer reads it — over the wire, not off the object
# --------------------------------------------------------------------------


def _seed_many(config: Config, count: int) -> None:
    """`count` transactions on distinct dates, so the cap has something to hide.

    The shared `_seed` above writes three, which cannot truncate under any limit
    a caller is allowed to send — so the case this chunk exists to fix is
    unreachable from it.
    """
    now = now_utc()
    _seed(config)
    added = [
        {
            "account_id": "acct-1",
            "transaction_id": f"bulk-{index}",
            "amount": "5.00",
            "iso_currency_code": "USD",
            "date": str(now.date() - timedelta(days=index)),
            "authorized_date": None,
            "pending": False,
            "pending_transaction_id": None,
            "name": f"Bulk {index}",
            "merchant_name": None,
            "personal_finance_category": {
                "primary": "GENERAL_MERCHANDISE",
                "detailed": "GENERAL_MERCHANDISE",
            },
        }
        for index in range(count)
    ]
    with writer_connection(config) as conn:
        apply_response(
            conn,
            connection_id=1,
            endpoint=TRANSACTIONS_SYNC.path,
            body=json.dumps(
                {
                    "accounts": [],
                    "added": added,
                    "modified": [],
                    "removed": [],
                    "next_cursor": "cursor-2",
                    "has_more": False,
                    "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE",
                    "request_id": "req-bulk",
                }
            ).encode(),
            received_at=now,
            derivers=ALL_DERIVERS,
        )


def test_a_capped_answer_says_so_in_the_payload_a_consumer_reads(
    initialized_config: Config,
) -> None:
    """🔴 The defect, over the wire: an answer that hit the cap must stop reading as complete.

    Measured harm — the documented default of 100 silently dropped ~16 months of
    one account's history, and summing what came back understated a two-year
    total by roughly 40% with nothing in the response saying so. A consumer sees
    only this payload, so the correction has to be in it.
    """
    _seed_many(initialized_config, 130)

    wire = _call(initialized_config, "query_transactions", {"limit": 10})["structuredContent"]

    assert wire["truncation"]["returned"] == 10
    assert wire["truncation"]["matching"] == 133
    assert wire["truncation"]["truncated"] is True
    assert len(wire["rows"]) == 10, "the block disagrees with the rows beside it"
    assert _request_kinds(wire) == ["rows_truncated"]
    detail = next(w["detail"] for w in wire["warnings"] if w["kind"] == "rows_truncated")
    assert "123 are missing" in detail


def test_a_complete_answer_says_it_is_complete(initialized_config: Config) -> None:
    """The reverse half. A block that read `truncated: true` always would say nothing."""
    _seed(initialized_config)

    wire = _call(initialized_config, "query_transactions")["structuredContent"]

    assert wire["truncation"] == {"returned": 3, "matching": 3, "truncated": False}
    assert _request_kinds(wire) == []


def test_the_aggregate_carries_no_truncation_block_over_the_wire(
    initialized_config: Config,
) -> None:
    """🔴 `api-contract.md` fixes aggregates as unpaginated, and absence is how that is said."""
    _seed_many(initialized_config, 130)

    wire = _call(initialized_config, "spending_summary")["structuredContent"]

    assert "truncation" not in wire
    assert _request_kinds(wire) == []


@pytest.mark.parametrize("tool", ["list_accounts", "get_pipeline_health"])
def test_an_uncapped_tool_carries_no_truncation_block_over_the_wire(
    initialized_config: Config, tool: str
) -> None:
    """Absence says "this tool returns everything it found"."""
    _seed(initialized_config)

    wire = _call(initialized_config, tool)["structuredContent"]

    assert "truncation" not in wire, tool


def test_the_window_scoped_count_rides_beside_the_store_wide_one(
    initialized_config: Config,
) -> None:
    """🔴 A new coverage key, never the old one narrowed.

    `coverage.transactions` is store-wide, and a consumer still reading it after
    a silent window-scoping would get a wrong answer rather than an error — the
    repurpose `api-contract.md` forbids in red.
    """
    _seed_many(initialized_config, 130)
    today = now_utc().date()

    wire = _call(
        initialized_config,
        "query_transactions",
        {"since": str(today - timedelta(days=4)), "limit": query.MAX_ROWS},
    )["structuredContent"]

    assert wire["coverage"]["transactions"] == 133
    assert wire["coverage"]["transactions_in_effective_window"] == 8
    assert wire["truncation"]["matching"] == 8


def test_the_capped_tool_describes_its_cap_and_the_aggregate_does_not() -> None:
    """AC-9.4: a tool description states its conventions.

    The note belongs to `query_transactions` alone — saying it on the aggregate
    would describe a cap that tool does not have.
    """
    described = {d["name"]: d["description"] for d in mcp._tool_definitions()}

    assert mcp._TRUNCATION_NOTE in described["query_transactions"]
    for name in ("spending_summary", "list_accounts", "get_pipeline_health"):
        assert mcp._TRUNCATION_NOTE not in described[name], name


@pytest.mark.parametrize(
    ("tool", "expects_truncation"),
    [("query_transactions", True), ("spending_summary", False), ("list_accounts", False)],
)
def test_an_unreadable_store_still_reports_whether_the_tool_is_capped(
    config: Config, tool: str, expects_truncation: bool
) -> None:
    """🔴 The capped-tool key, pinned over the wire and not only on the object.

    The sibling of the `effective_window` test above, and it exists because that
    one had to be written after the fact: the old shape and the new one passed
    every AC-ARCH.3 assertion identically, since each of them runs against an
    initialized store. `_unusable` is the construction site where a key is
    easiest to drop, and dropping this one would tell a consumer that
    `query_transactions` returns everything it finds — a false statement about
    the tool, made precisely when the datastore cannot be read.
    """
    assert not config.datastore_path.exists()

    wire = _call(config, tool)["structuredContent"]

    if not expects_truncation:
        assert "truncation" not in wire, tool
        return

    assert wire["truncation"] == {"returned": 0, "matching": 0, "truncated": False}
    assert _request_kinds(wire) == [], tool
    assert any(w["kind"] == "partial" for w in wire["warnings"]), tool


# --------------------------------------------------------------------------
# The cursor, read as a consumer reads it — a paged walk over the wire (#17)
# --------------------------------------------------------------------------


def _walk_the_wire(
    config: Config, arguments: dict[str, Any], *, limit: int
) -> tuple[list[int], list[dict[str, Any]]]:
    """Page `query_transactions` over stdio until it stops offering a next page.

    🔴 The loop a consuming agent actually writes, driven through the JSON-RPC
    surface rather than against the query function: the fields it branches on
    are the ones in `structuredContent`, and a cursor that never reached the
    payload would leave an object-level test passing while every consumer was
    stranded at the cap.
    """
    seen: list[int] = []
    pages: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        sent = {**arguments, "limit": limit}
        if cursor is not None:
            sent["cursor"] = cursor
        result = _call(config, "query_transactions", sent)
        assert result["isError"] is False, result["content"][0]["text"]
        wire = result["structuredContent"]
        pages.append(wire)
        seen.extend(int(row["transaction_id"]) for row in wire["rows"])
        cursor = wire["truncation"].get("next_cursor")
        if cursor is None:
            break
        # A cursor that fails to advance hangs the test rather than reddening
        # it, so the walk carries its own ceiling.
        assert len(pages) <= 40, "the walk did not terminate"
    return seen, pages


def test_a_paged_walk_reassembles_every_row_the_first_page_left_behind(
    initialized_config: Config,
) -> None:
    """🔴 The other half of #17: visibility without a route past the cap is not enough.

    Chunks 01 and 02 made a truncated answer stop reading as a complete one.
    This is the part that lets a caller do something about it — the measured
    harm was a two-year card total understated by roughly 40%, and knowing the
    figure is wrong does not make it right.

    Asserted against `matching` from the FIRST page, which is the number the
    caller is given up front and the one they would check the walk against.
    """
    _seed_many(initialized_config, 130)

    seen, pages = _walk_the_wire(initialized_config, {}, limit=10)

    assert pages[0]["truncation"]["matching"] == 133
    assert len(seen) == 133, "the walk did not reassemble the answer"
    assert len(set(seen)) == 133, "a row came back on more than one page"
    assert len(pages) == 14
    assert sum(len(page["rows"]) for page in pages) == 133


def test_the_last_page_of_a_walk_says_it_is_the_last_one(initialized_config: Config) -> None:
    """The terminating condition, over the wire and stated in both fields.

    A consumer stops when `next_cursor` is absent; one that branches on
    `truncated` instead must reach the same conclusion, or the two halves of the
    block disagree about whether the answer is complete.
    """
    _seed_many(initialized_config, 130)

    _, pages = _walk_the_wire(initialized_config, {}, limit=50)

    for page in pages[:-1]:
        assert page["truncation"]["truncated"] is True
        assert isinstance(page["truncation"]["next_cursor"], str)

    last = pages[-1]["truncation"]
    assert last["truncated"] is False
    assert "next_cursor" not in last
    assert _request_kinds(pages[-1]) == []


def test_a_page_states_the_same_window_every_other_page_states(
    initialized_config: Config,
) -> None:
    """🔴 Paging moves the rows, never the window the answer claims to cover.

    `effective_window` is a statement about the question, and the cursor narrows
    only which rows of the answer are in hand. A window that crept forward page
    by page would report each page's own span as the window the caller asked
    about — the precise wrong number this work cycle exists to remove.
    """
    _seed_many(initialized_config, 130)

    _, pages = _walk_the_wire(initialized_config, {}, limit=50)

    windows = {json.dumps(page["effective_window"], sort_keys=True) for page in pages}
    assert len(pages) > 1
    assert len(windows) == 1, "the effective window moved while the caller was paging"


@pytest.mark.parametrize(
    ("presented", "why"),
    [
        ("", "empty"),
        ("MTIzNA", "base64 of something that is not a cursor"),
        ("not a cursor", "not base64 at all"),
    ],
)
def test_a_cursor_this_server_did_not_issue_is_refused_by_name(
    initialized_config: Config, presented: str, why: str
) -> None:
    """🔴 Refused, not read as "start again from the newest row".

    The silent fallback returns page one under the name of page two: a
    well-formed, plausible, complete-looking answer to a question nobody asked.
    The refusal names the argument, offers the route back, and — like every other
    refusal on this boundary — carries no exception class for a caller to puzzle
    over.
    """
    _seed(initialized_config)

    result = _call(initialized_config, "query_transactions", {"cursor": presented})

    assert result["isError"] is True, why
    assert result["structuredContent"]["error"]["code"] == "invalid_argument", why
    message = result["content"][0]["text"]
    assert "cursor" in message, why
    assert "`next_cursor`" in message, why
    assert "Error" not in message, why


def test_a_cursor_from_a_different_question_is_refused_rather_than_answered(
    initialized_config: Config,
) -> None:
    """🔴 The reachable foreign cursor: the caller's own, against a changed request.

    It would select real rows in the right order and answer a question the
    caller did not ask — no error, no warning, and a payload that reads as a
    continuation of the walk they thought they were on. So a cursor carries the
    request it was issued for, and the boundary compares them.
    """
    _seed_many(initialized_config, 130)
    first = _call(initialized_config, "query_transactions", {"limit": 10})
    issued = first["structuredContent"]["truncation"]["next_cursor"]

    same = _call(initialized_config, "query_transactions", {"limit": 10, "cursor": issued})
    assert same["isError"] is False, same["content"][0]["text"]

    changed = _call(
        initialized_config,
        "query_transactions",
        {"limit": 10, "cursor": issued, "account_id": 1},
    )

    assert changed["isError"] is True, "a cursor from another question was answered"
    assert "cursor" in changed["content"][0]["text"]


def test_the_cursor_is_advertised_on_the_capped_tool_and_nowhere_else() -> None:
    """A caller learns the argument from the schema, and a misspelling is refused by name.

    `additionalProperties: False` plus `_permitted_arguments` means an argument
    that is not advertised cannot be sent — so an unadvertised `cursor` would
    make the escape route unreachable to a caller reading the tool definition,
    which is the only thing an agent reads.
    """
    assert "cursor" in mcp._permitted_arguments("query_transactions")
    for name in ("spending_summary", "list_accounts", "get_pipeline_health"):
        assert "cursor" not in mcp._permitted_arguments(name), name


def test_the_instructions_say_how_to_reach_what_a_truncated_answer_left_behind(
    initialized_config: Config,
) -> None:
    """🔴 `next_cursor` is nested inside `truncation`, so the envelope guard cannot see it.

    That guard walks the TOP-LEVEL keys of each tool's payload; a field one
    level down is invisible to it, and a field that appears only on a truncated
    answer is invisible to a call it makes with no arguments. Both gaps point the
    same way — the only text a consuming agent reads before it calls anything
    would not mention the one field that gets it past the cap.
    """
    instructions = mcp._instructions(initialized_config)

    assert "`next_cursor`" in instructions
    assert "`cursor`" in instructions
    assert mcp._TRUNCATION_NOTE.count("`next_cursor`") >= 1
