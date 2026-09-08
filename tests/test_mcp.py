"""The MCP surface: the handshake, the tools, and the envelope every answer carries.

🔴 The handshake is the part most likely to be subtly wrong in a hand-rolled
server, and it fails at *connection* time rather than in a unit test — so it is
driven here through the real loop over string buffers, not mocked.
"""

from __future__ import annotations

import io
import json
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
    """

    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the datastore went away")

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

    assert replies[1]["result"]["isError"] is True
    assert "the datastore went away" in replies[1]["result"]["content"][0]["text"]
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
