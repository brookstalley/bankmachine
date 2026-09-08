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

import pytest

from bankmachine import mcp, query
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


def test_a_limit_of_zero_is_not_silently_a_hundred(initialized_config: Config) -> None:
    """`or 100` is truthiness on an int, and 0 is a number a caller can send.

    The query layer clamps with `max(1, min(limit, 1000))`, so 0 means one row.
    Reading it as "unset" instead hands back a hundred -- a different answer to
    the question that was asked, with nothing saying so.
    """
    _seed(initialized_config)

    rows = _call(initialized_config, "query_transactions", {"limit": 0})["structuredContent"][
        "rows"
    ]

    assert len(rows) == 1, "limit=0 was read as unset and served the default page"
