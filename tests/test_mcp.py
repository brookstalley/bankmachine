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
import re
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import IO, Any, cast
from unittest import mock

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.engine import Engine

from bankmachine import build_id, envelope, mcp, mcp_resources, query, signs
from bankmachine.cli.exit_codes import EXIT_OK
from bankmachine.config import Config
from bankmachine.connector import (
    ACCOUNTS_GET,
    INVESTMENTS_HOLDINGS_GET,
    INVESTMENTS_TRANSACTIONS_GET,
    TRANSACTIONS_SYNC,
)
from bankmachine.derivers import ALL_DERIVERS
from bankmachine.store import derivation
from bankmachine.store.derivation import apply_response
from bankmachine.store.engine import writer_connection
from bankmachine.store.schema import connections, institutions, investment_transactions
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
    def txn(index: int, amount: str, name: str, category: str, detailed: str) -> dict[str, Any]:
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
            # 🔴 Both stated. They used to be the same string, which is not a
            # shape the aggregator ever sends -- and once the flow class began
            # reading the detailed column, that fixture was feeding the
            # classifier a value from the wrong vocabulary.
            "personal_finance_category": {"primary": category, "detailed": detailed},
        }

    return json.dumps(
        {
            "accounts": [],
            # Positive amounts, as the aggregator sends them: these are money
            # LEAVING. The last is negative — money arriving — so the spending
            # aggregate has something it must exclude.
            "added": [
                txn(
                    0,
                    "89.40",
                    "SparkFun",
                    "GENERAL_MERCHANDISE",
                    "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES",
                ),
                txn(1, "12.00", "McDonald's", "FOOD_AND_DRINK", "FOOD_AND_DRINK_RESTAURANT"),
                txn(2, "-250.00", "Payroll", "INCOME", "INCOME_WAGES"),
            ],
            "modified": [],
            "removed": [],
            "next_cursor": "cursor-1",
            "has_more": False,
            "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE",
            "request_id": "req-sync",
        }
    ).encode()


def _empty_sync_body() -> bytes:
    """A transactions backfill that completed and carried nothing.

    The shape an institution whose accounts post nothing to this feed sends: the
    status says the history is in, and there is no transaction and no cursor.
    """
    return json.dumps(
        {
            "accounts": [],
            "added": [],
            "modified": [],
            "removed": [],
            "next_cursor": "",
            "has_more": False,
            "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE",
            "request_id": "req-sync-empty",
        }
    ).encode()


def _seed(
    config: Config,
    *,
    degraded: bool = False,
    granted: int | None = 90,
    transactions: bool = True,
) -> None:
    """One institution, one account, three transactions — through the real derivers.

    `transactions=False` completes the backfill with nothing in it instead.

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
                last_error_code="AGGREGATOR_UNREACHABLE" if degraded else None,
                last_error_at=now if degraded else None,
                enrolled_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        for endpoint, body in (
            (ACCOUNTS_GET.path, _accounts_body()),
            (
                TRANSACTIONS_SYNC.path,
                _sync_body(str(now.date())) if transactions else _empty_sync_body(),
            ),
        ):
            apply_response(
                conn,
                connection_id=1,
                endpoint=endpoint,
                body=body,
                received_at=now,
                derivers=ALL_DERIVERS,
                replay_passes=(),
            )


def _frames(config: Config, frames: list[Any]) -> list[Any]:
    """Drive the real server loop over string buffers, one JSON value per line.

    Not a mock of the protocol: the same `serve` the CLI calls, reading the same
    line-delimited JSON a client writes.

    Typed loosely on both halves because a frame is not always an object -- a
    BATCH is an array going out and an array coming back, and a frame that is
    neither is exactly what the refusal cases send. `_converse` is the narrower
    door for the ordinary one-object-per-line case.
    """
    stdin = io.StringIO("\n".join(json.dumps(frame) for frame in frames) + "\n")
    stdout = io.StringIO()
    mcp.serve(config, stdin=stdin, stdout=stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]


def _converse(config: Config, requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One request object per line, one reply object per line."""
    replies: list[dict[str, Any]] = _frames(config, list(requests))
    return replies


def _every_tool() -> tuple[str, ...]:
    """Every built tool, DERIVED from the registry rather than listed here.

    🔴 Five "every tool" loops in this file named their tools as string
    literals, and `get_coverage_report` was added to none of them — so a tool
    built specifically to answer "what data exists" had no proof it answered at
    all against a store that cannot be read, and nothing failed to say so. A
    literal list is an enumeration of a set the code already owns, and it goes
    stale by SILENCE: the loop keeps passing over the tools it still names.

    Derived, the next tool cannot be omitted by forgetting. The extraction
    asserts it found something, because a derivation that returns empty is a
    loop that checks nothing while reporting green.
    """
    names = tuple(sorted(d["name"] for d in mcp._tool_definitions()))
    assert names, "the registry produced no tools, so every loop over this checks nothing"
    return names


def _tools_requiring(key: str) -> tuple[str, ...]:
    """The tools whose published schema REQUIRES an envelope key.

    🔴 Read from what each tool publishes, not listed here. `api-contract.md`
    fixes a key's absence as information — no `effective_window` means the tool
    takes no window, no `truncation` means it returns every row it found, no
    `totals` means it computes no total — and the tests below assert the
    WIRE against exactly that claim. Derived, they hold every tool to its own
    published contract and a new one is covered the day it registers; listed,
    they hold whichever tools someone remembered.

    Empty is a defect rather than a vacuous pass: every key this is asked about
    is one some tool carries, so a derivation returning nothing means the schema
    stopped saying what this file thinks it says.
    """
    names = tuple(
        str(d["name"])
        for d in mcp._tool_definitions()
        if key in d["outputSchema"].get("required", [])
    )
    assert names, f"no tool requires {key!r}, so every loop keyed on it checks nothing"
    return names


#: A window wider than the seeded data, so a windowed tool actually clamps.
_A_WINDOW: dict[str, Any] = {"since": "2024-01-01", "until": "2024-06-30"}


def _every_tool_except(*excluded: str) -> tuple[str, ...]:
    """The complement, for the claims that are true of one tool and false of the rest.

    Asserts the exclusions were actually present: naming a tool that does not
    exist would silently widen the loop to everything, which reads as a stricter
    test than it is.
    """
    names = _every_tool()
    missing = set(excluded) - set(names)
    assert not missing, f"excluded {sorted(missing)}, which the registry does not build"
    return tuple(name for name in names if name not in excluded)


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


def test_the_current_handshake_revision_is_not_downgraded(initialized_config: Config) -> None:
    """🔴 A recognized version that is missing from the tuple is worse than an unknown one.

    `2025-11-25` is the newest revision `initialize` can negotiate, so it is
    what a current client offers. Absent from the supported set it matches
    nothing and silently lands two revisions back on the fallback -- a session
    that connects, works, and quietly speaks an older protocol than both halves
    are capable of.
    """
    replies = _converse(
        initialized_config,
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": mcp.LATEST_HANDSHAKE_VERSION},
            }
        ],
    )

    assert replies[0]["result"]["protocolVersion"] == "2025-11-25"


def test_the_oldest_handshake_revision_is_answered_rather_than_out_offered(
    initialized_config: Config,
) -> None:
    """🔴 The fallback only rescues a client that can speak something NEWER.

    Counter-offering `2025-03-26` to a client pinned at `2024-11-05` names a
    revision it cannot speak, and the spec has such a client disconnect rather
    than downgrade -- so dropping the oldest handshake revision from the set
    does not degrade that session, it ends it.
    """
    replies = _converse(
        initialized_config,
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            }
        ],
    )

    assert replies[0]["result"]["protocolVersion"] == "2024-11-05"


def test_the_handshake_offers_only_revisions_it_can_reach(initialized_config: Config) -> None:
    """🔴 `2026-07-28` is not reachable through `initialize` at all.

    It is a per-request-envelope era entered by a `server/discover` probe, and
    `InitializeResult` is documented as removed there -- so echoing it back
    would be this server agreeing to a protocol it has no code for, at the one
    moment a client takes the agreement at face value. It is not enough to
    check the constant: what matters is that a client asking cannot be told yes.
    """
    assert "2026-07-28" not in mcp.SUPPORTED_PROTOCOL_VERSIONS

    replies = _converse(
        initialized_config,
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2026-07-28"},
            }
        ],
    )

    assert replies[0]["result"]["protocolVersion"] == mcp.FALLBACK_PROTOCOL_VERSION


#: How to prove a declared capability is actually served: the listing call a
#: client makes first for each. A capability with no entry here fails the test
#: below rather than passing unchecked -- the whole claim is that nothing is
#: declared unserved, and a capability nothing knows how to probe is exactly the
#: one that would slip through.
_CAPABILITY_PROBES = {"tools": "tools/list", "resources": "resources/list"}


def test_the_server_declares_only_capabilities_it_serves(initialized_config: Config) -> None:
    """Declaring one it does not would have the client offer the operator something that fails.

    🔴 Asserted by CALLING each declared capability rather than by comparing the
    set against a list written here. A second list agrees with the handshake
    until someone adds a capability to both and implements neither, which is the
    exact failure the declaration is supposed to prevent.
    """
    declared = _converse(
        initialized_config, [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}]
    )[0]["result"]["capabilities"]

    unprobeable = sorted(set(declared) - set(_CAPABILITY_PROBES))
    assert not unprobeable, (
        f"the handshake declares {unprobeable}, which this test does not know how to call; "
        f"a capability nothing probes is one that can be declared without being served"
    )

    for capability, method in _CAPABILITY_PROBES.items():
        reply = _converse(
            initialized_config,
            [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": method, "params": {}},
            ],
        )[1]
        served = "result" in reply
        if capability in declared:
            assert served, (
                f"the handshake declares {capability!r} but {method} answered "
                f"{reply.get('error')}, so the client offers the operator something that fails"
            )
        else:
            assert not served, (
                f"{method} answers, but the handshake does not declare {capability!r} -- a "
                f"client reading the handshake never offers it at all"
            )


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


def test_a_request_whose_id_is_explicitly_null_is_answered(initialized_config: Config) -> None:
    """🔴 An absent `id` and a null `id` are different messages, and only one is a notification.

    JSON-RPC 2.0 defines a Notification as a request object WITHOUT an `id`
    member. An `id` present and null is an ordinary request, and its response
    carries `"id": null`. Deciding notification-ness on the VALUE conflates the
    two and leaves this client waiting for a reply the server chose not to
    send -- the hang the read loop's other guards exist to rule out, arriving
    through the one door they do not cover.
    """
    replies = _converse(
        initialized_config,
        [
            {"jsonrpc": "2.0", "id": None, "method": "ping"},
            # A second, ordinary request, so a reply count of one cannot be read
            # as "it answered" when what happened is that it answered the wrong
            # message.
            {"jsonrpc": "2.0", "id": 7, "method": "ping"},
        ],
    )

    assert [reply["id"] for reply in replies] == [None, 7]
    assert replies[0]["result"] == {}


def test_an_unparseable_line_is_answered_rather_than_ignored(initialized_config: Config) -> None:
    """A client that sent something broken is waiting; silence looks like a hang."""
    stdin = io.StringIO("not json at all\n")
    stdout = io.StringIO()
    mcp.serve(initialized_config, stdin=stdin, stdout=stdout)

    reply = json.loads(stdout.getvalue())
    assert reply["error"]["code"] == -32700


def _undecodable() -> UnicodeDecodeError:
    """What a read raises when the bytes behind it are not UTF-8."""
    return UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")


class _ScriptedStream:
    """A stream serving a fixed sequence of reads, raising where the script says.

    🔴 A stub rather than a real `TextIOWrapper`, because the property under test
    is *the session continues*, and whether a real decoder can advance past bad
    bytes is CPython's business rather than this server's. The last test in this
    group covers the real decoder for the half that does reach an operator.

    Deliberately not a `StringIO` subclass: the read loop calls `readline` and
    nothing else, so the smallest object that can be wrong in the right way is a
    plain one, and inheriting would let a method nobody named answer for it.
    """

    def __init__(self, *script: str | UnicodeDecodeError) -> None:
        self._script = list(script)

    def readline(self) -> str:
        if not self._script:
            return ""
        item = self._script.pop(0)
        if isinstance(item, UnicodeDecodeError):
            raise item
        return item


class _NeverDecodes:
    """A stream that never advances and never ends — the case the ceiling exists for."""

    def readline(self) -> str:
        raise _undecodable()


def _as_stream(stub: _ScriptedStream | _NeverDecodes) -> IO[str]:
    """The stub, at the type `serve` declares. `readline` is the whole surface used."""
    return cast("IO[str]", stub)


def _ping(message_id: int) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": message_id, "method": "ping", "params": {}}) + "\n"


def _replies(stdout: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]


def test_a_frame_that_is_not_utf8_is_answered_and_the_session_survives(
    initialized_config: Config,
) -> None:
    """🔴 The decode failure one step EARLIER than the parse, through the adjacent door.

    The bytes are decoded by the READ, before any JSON is looked at, so the
    parse guards cannot reach it — and an uncaught `UnicodeDecodeError` ends
    `serve()` exactly as the nested-frame case did, with the operator's tool
    vanishing mid-session and nothing said.

    The second reply is the assertion that carries the weight: a loop that
    reported the bad frame and then stopped would satisfy the first half, and
    the symptom a person reports is the disappearance rather than the refusal.
    """
    stdout = io.StringIO()

    mcp.serve(
        initialized_config,
        stdin=_as_stream(_ScriptedStream(_undecodable(), _ping(2))),
        stdout=stdout,
    )

    replies = _replies(stdout)
    assert len(replies) == 2, "the session ended instead of carrying on past the bad frame"
    assert replies[0]["error"]["code"] == mcp._PARSE_ERROR
    assert replies[0]["error"]["message"] == "could not read a message: it is not valid UTF-8"
    assert replies[1]["id"] == 2 and replies[1]["result"] == {}


def test_the_give_up_ceiling_counts_consecutive_failures_not_lifetime_ones(
    initialized_config: Config,
) -> None:
    """🔴 The word "consecutive" in the ceiling, which nothing else pins.

    A counter that never reset would end a long-lived session on its third bad
    frame ever — three failures spread over hours, each one recovered from — and
    the operator would see the tool disappear for no reason they could connect
    to anything. The ceiling exists to stop a spin, not to ration a session.

    🔴 The reset is what nothing else in this group pins: every other case here
    recovers at most once, so all of them pass whether the counter resets or
    counts a lifetime. This one recovers repeatedly, which is the only shape
    that can tell the two apart.
    """
    stdout = io.StringIO()
    script: list[str | UnicodeDecodeError] = []
    for message_id in range(mcp._MAX_UNDECODABLE_FRAMES + 2):
        script += [_undecodable(), _ping(message_id)]

    mcp.serve(initialized_config, stdin=_as_stream(_ScriptedStream(*script)), stdout=stdout)

    replies = _replies(stdout)
    answered = [r["id"] for r in replies if "result" in r]
    assert answered == list(range(mcp._MAX_UNDECODABLE_FRAMES + 2)), (
        "the session gave up part-way, so the failure counter is counting a lifetime "
        "rather than a run"
    )


def test_a_stream_that_never_decodes_is_given_up_on_rather_than_spun_on(
    initialized_config: Config,
) -> None:
    """🔴 The one outcome worse than the bug above: a server that hangs.

    Reporting and carrying on is right only while the stream advances. Measured
    behaviour is that a real one reports EOF after a decode failure, so the loop
    ends on its own — but a stream that neither advanced nor ended would be spun
    on forever, and a hung server is less diagnosable than a dead one.
    """
    stdout = io.StringIO()

    mcp.serve(initialized_config, stdin=_as_stream(_NeverDecodes()), stdout=stdout)

    replies = _replies(stdout)
    assert len(replies) == mcp._MAX_UNDECODABLE_FRAMES
    assert all(r["error"]["code"] == mcp._PARSE_ERROR for r in replies)


def test_a_real_stream_of_invalid_bytes_does_not_take_the_server_down(
    initialized_config: Config,
) -> None:
    """The same case against the real decoder rather than the stubs above.

    The stubs prove the loop takes a recovery when one is offered; this proves
    the thing that actually reaches an operator — invalid bytes on a real
    `TextIOWrapper` — is answered rather than raised. What that decoder does
    with the frames BEHIND the bad one is CPython's business, so nothing here
    asserts it.
    """
    stdout = io.StringIO()

    mcp.serve(
        initialized_config,
        stdin=io.TextIOWrapper(io.BytesIO(b"\xff\xfe not utf-8\n"), encoding="utf-8"),
        stdout=stdout,
    )

    replies = _replies(stdout)
    assert replies, "the server raised instead of answering"
    assert replies[0]["error"]["message"] == "could not read a message: it is not valid UTF-8"


def test_a_frame_nested_too_deeply_is_answered_and_the_session_survives(
    initialized_config: Config,
) -> None:
    """🔴 The parse failure that is NOT a `JSONDecodeError`, and it used to kill the server.

    `json.loads` raises `RecursionError` on a deeply nested document — a
    `RuntimeError`, sharing no base with `JSONDecodeError` beyond `Exception`.
    Uncaught it escaped the read loop and ended `serve()`, so the operator's
    tool vanished mid-session with nothing said: the outcome `cmd_mcp` exists to
    prevent, arriving one frame in rather than at startup.

    The second request is the assertion that matters. A server that answered the
    bad frame and then died would satisfy the first half of this test, and the
    symptom a person actually reports is the disappearance rather than the
    refusal.

    The reply carries this server's own sentence, not the decoder's, whose text
    names the stack size it blew.
    """
    stdin = io.StringIO(
        "[" * 100_000
        + "]" * 100_000
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}})
        + "\n"
    )
    stdout = io.StringIO()

    mcp.serve(initialized_config, stdin=stdin, stdout=stdout)

    replies = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    assert len(replies) == 2, "the session ended instead of answering both frames"
    assert replies[0]["error"]["code"] == mcp._PARSE_ERROR
    assert replies[0]["error"]["message"] == "could not parse a message: it is nested too deeply"
    assert "Stack overflow" not in replies[0]["error"]["message"]
    assert replies[1]["id"] == 2 and replies[1]["result"] == {}


def test_an_unknown_method_is_a_method_not_found(initialized_config: Config) -> None:
    """`prompts/list` is a real protocol method this server does not serve.

    A method nobody has heard of would prove only that the fallback branch
    exists. This is the case that actually happens: a client trying a surface
    the handshake did not declare, which has to be refused in a way it can tell
    apart from a failure of a surface that was declared.
    """
    replies = _converse(initialized_config, [{"jsonrpc": "2.0", "id": 1, "method": "prompts/list"}])

    assert replies[0]["error"]["code"] == -32601


# --------------------------------------------------------------------------
# Batches
# --------------------------------------------------------------------------


def test_every_id_in_a_batch_gets_exactly_one_reply(initialized_config: Config) -> None:
    """🔴 The harm is a HANG, not a refusal.

    Batching is base JSON-RPC 2.0 and is mandatory in the two oldest revisions
    `SUPPORTED_PROTOCOL_VERSIONS` offers, so a conformant client may send an
    array at any time. Answering the whole array with one `id: null` error
    settles no promise the client is holding: every id inside it waits forever,
    and the operator sees the tool stop responding rather than fail.

    So the assertion is on the IDS, not on the absence of an error. A fix that
    refuses more politely still hangs the client; only a reply per id does not.
    """
    sent = [1, 2, 3]
    frames = _frames(
        initialized_config,
        [
            {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
            [
                {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "list_accounts", "arguments": {}},
                },
            ],
        ],
    )

    assert len(frames) == 2, "the batch was not answered as a single frame"
    batch = frames[1]
    assert isinstance(batch, list), "a batch is answered with an array, not one object"
    assert sorted(reply["id"] for reply in batch) == sent, "an id in the batch went unanswered"
    # Dispatched for real rather than acknowledged: each element carries the
    # answer its own method produces, so a batch is not a second, weaker surface.
    by_id = {reply["id"]: reply for reply in batch}
    assert by_id[1]["result"] == {}
    assert by_id[2]["result"]["tools"]
    assert by_id[3]["result"]["isError"] is False


def test_an_empty_batch_is_refused_as_one_object_rather_than_an_empty_array(
    initialized_config: Config,
) -> None:
    """`[]` is itself an Invalid Request, and the reply is NOT an array.

    Answering an empty batch with `[]` is the shape the spec singles out as
    wrong, and it is what a naive `[handle(m) for m in batch]` produces.
    """
    frames = _frames(initialized_config, [[]])

    assert len(frames) == 1
    reply = frames[0]
    assert not isinstance(reply, list), "an empty batch is refused with one object, not an array"
    assert reply["id"] is None
    assert reply["error"]["code"] == mcp._INVALID_REQUEST


def test_a_batch_of_only_notifications_is_answered_with_silence(
    initialized_config: Config,
) -> None:
    """No response AT ALL -- not an empty array.

    A notification carries no promise, so an empty array back is a frame the
    client never asked for, arriving where its parser expects nothing.
    """
    frames = _frames(
        initialized_config,
        [
            [
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}},
            ]
        ],
    )

    assert frames == [], "a batch of notifications was answered"


def test_a_bad_element_rides_inside_the_batch_rather_than_failing_it(
    initialized_config: Config,
) -> None:
    """One malformed element does not cost its siblings their answers.

    The per-element error is an object INSIDE the array under `id: null`, which
    is the only place it can go: the bad element has no id to answer under, and
    the good ones are still owed theirs.
    """
    frames = _frames(
        initialized_config,
        [
            {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
            [
                {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                42,
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 4, "method": "prompts/list"},
            ],
        ],
    )

    batch = frames[1]
    assert isinstance(batch, list)
    # Three replies for four elements: the notification is owed none.
    assert len(batch) == 3
    by_id = {reply["id"]: reply for reply in batch}
    assert by_id[1]["result"] == {}
    assert by_id[None]["error"]["code"] == mcp._INVALID_REQUEST
    assert by_id[4]["error"]["code"] == mcp._METHOD_NOT_FOUND


def test_a_lone_request_is_still_answered_as_one_object(initialized_config: Config) -> None:
    """A non-batch reply is never wrapped in an array.

    The mirror of the batch cases: a client that sent one object is parsing for
    one object, and wrapping every reply uniformly would break every existing
    client to serve the new shape.
    """
    frames = _frames(initialized_config, [{"jsonrpc": "2.0", "id": 1, "method": "ping"}])

    assert frames == [{"jsonrpc": "2.0", "id": 1, "result": {}}]


def test_a_frame_that_is_neither_an_object_nor_a_batch_is_refused(
    initialized_config: Config,
) -> None:
    """A bare scalar is still an Invalid Request, and the session survives it.

    Reading a frame's shape moved out of the read loop so a batch could be told
    apart from a malformed request; this holds the case that move could have
    dropped -- a line that parses as JSON and is not a request at all.
    """
    frames = _frames(
        initialized_config,
        ["not a request", {"jsonrpc": "2.0", "id": 2, "method": "ping"}],
    )

    assert len(frames) == 2, "the session ended instead of answering both frames"
    assert frames[0]["id"] is None
    assert frames[0]["error"]["code"] == mcp._INVALID_REQUEST
    assert frames[1]["id"] == 2, "a refused frame took the session with it"


def test_a_failing_element_does_not_cost_the_batch_its_other_answers(
    initialized_config: Config,
) -> None:
    """An exception under one element is contained to that element's reply.

    The boundary catch sits per-message rather than per-frame for this reason:
    around the frame, one unexpected failure would swallow every sibling's
    answer and hand the client back the same silence a refused batch does.
    """
    real_handle = mcp._handle

    def _explode_on_ping(config: Config, message: dict[str, Any]) -> dict[str, Any] | None:
        if message.get("method") == "ping":
            raise RuntimeError("the reply could not be assembled")
        return real_handle(config, message)

    with mock.patch.object(mcp, "_handle", _explode_on_ping):
        frames = _frames(
            initialized_config,
            [
                [
                    {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                    {"jsonrpc": "2.0", "id": 2, "method": "prompts/list"},
                ]
            ],
        )

    batch = frames[0]
    assert isinstance(batch, list)
    by_id = {reply["id"]: reply for reply in batch}
    assert by_id[1]["error"]["code"] == mcp._INTERNAL_ERROR
    assert "RuntimeError" not in json.dumps(by_id[1]), "the exception crossed the boundary"
    assert by_id[2]["error"]["code"] == mcp._METHOD_NOT_FOUND


# --------------------------------------------------------------------------
# 🔴 Read-only, structurally
# --------------------------------------------------------------------------


def test_no_tool_mutates_anything(initialized_config: Config) -> None:
    """🔴 The ratified norm: no mutation tools, and adding one is not open.

    Asserted over the tool list itself rather than over a roster of names, so a
    new tool that wrote would have to be named in this test to pass — the same
    construction that makes the endpoint-properties test hold.
    """
    tools = mcp._tool_definitions()
    assert {t["name"] for t in tools} == {
        "list_accounts",
        "list_holdings",
        "balance_history",
        "query_transactions",
        "money_summary",
        "get_pipeline_health",
        "get_coverage_report",
    }, "the shipped subset of the api-contract tool surface"
    forbidden = ("create", "update", "delete", "remove", "write", "set_", "transfer", "pay")
    for tool in tools:
        assert not any(word in tool["name"] for word in forbidden), tool["name"]


def _seed_investments(config: Config) -> None:
    """The recorded holdings and trades captures, derived onto the seeded connection.

    Taken from the live sandbox captures rather than hand-written, and the roster
    they hang from is the captures' own accounts added to the seeded one, so the
    positions and trades reference ids this store actually holds.
    """
    fixtures = Path(__file__).parent / "connector" / "fixtures"
    captures = {
        endpoint: json.loads((fixtures / name).read_bytes(), parse_float=str)
        for endpoint, name in (
            (INVESTMENTS_HOLDINGS_GET.path, "investments_holdings_get.json"),
            (INVESTMENTS_TRANSACTIONS_GET.path, "investments_transactions_get.json"),
        )
    }
    roster = json.loads(_accounts_body())
    known = {entry["account_id"] for entry in roster["accounts"]}
    for capture in captures.values():
        for entry in capture["accounts"]:
            if entry["account_id"] not in known:
                roster["accounts"].append(entry)
                known.add(entry["account_id"])
    now = now_utc()
    with writer_connection(config) as conn:
        for endpoint, body in ((ACCOUNTS_GET.path, roster), *captures.items()):
            apply_response(
                conn,
                connection_id=1,
                endpoint=endpoint,
                body=json.dumps(body).encode(),
                received_at=now,
                derivers=ALL_DERIVERS,
                replay_passes=(),
            )


def _cannot_answer_text() -> str:
    documents = mcp_resources.documents(mcp._tool_definitions())
    text = next(d.text for d in documents if d.uri == mcp_resources.ENVELOPE_URI)
    start = text.index("## What this server cannot answer")
    end = text.find("\n## ", start + 1)
    return text[start:] if end == -1 else text[start:end]


def test_the_unserved_trades_claim_holds_against_what_every_tool_reads(
    initialized_config: Config,
) -> None:
    """🔴 The reference says trades are counted but served as rows by no tool; held to the SQL.

    Checked against the statements every registered tool actually executes, not
    against a list of files: a tool's query already runs through store helpers
    beyond `query.py`, and the next one to read trades could arrive through any of
    them. The store holds real trades first, because a query that reads them only
    when an investment account exists would issue nothing over a store without one.

    🔴 Counting a trade is not serving one. The coverage rows count each account's
    trades so an investment account is not reported as holding no data, and that
    count needs only the account a trade belongs to and whether it was removed.
    Serving a trade means reading what it IS -- its amount, date, security or
    description -- so the claim is held against the trade's CONTENT columns,
    derived from the table rather than listed here, so a column a migration adds
    is content until someone says otherwise.
    """
    _seed(initialized_config)
    _seed_investments(initialized_config)
    with writer_connection(initialized_config) as conn:
        stored = conn.execute(select(func.count()).select_from(investment_transactions)).scalar()
    assert stored, "no trade reached the store, so a tool that reads trades had nothing to read"

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
        for name in _every_tool():
            assert not _call(initialized_config, name).get("isError"), name
    finally:
        event.remove(Engine, "before_cursor_execute", _capture)

    # Positive control: the capture reaches the tools' queries at all. A listener
    # on an engine the server never uses would record nothing and pass forever.
    assert any(re.search(r"\btransactions\b", s) for s in statements), (
        "no tool's query was captured, so this cannot tell a tool that reads trades from one "
        "that does not"
    )
    # The columns a count may touch. Everything else on a trade is what it IS.
    counted_by = {"investment_transaction_id", "account_id", "removed_at"}
    content = [c.name for c in investment_transactions.columns if c.name not in counted_by]
    assert content, "the trade table has no content columns, so this checks nothing"
    # Positive control for the second half: the coverage rows DO count trades, so
    # the column check below judges statements that reach the table at all.
    assert any("investment_transactions" in s for s in statements), (
        "no tool's query reaches the trade table, so a tool that serves trades cannot be told "
        "from one that only counts them"
    )
    serves_trades = any(
        f"investment_transactions.{column}" in s for s in statements for column in content
    )
    text = _cannot_answer_text()
    says_unserved = "served as rows by no tool" in text

    assert says_unserved != serves_trades, (
        "the cannot-answer list says no tool serves trades, and a tool now reads what one is"
        if says_unserved
        else "no tool serves trades, and the list an agent is sent to no longer says so"
    )
    assert "investment_transaction_count" in text, (
        "trades are counted on the coverage rows, and the list that says they are not served "
        "does not point at where they ARE counted"
    )


def test_every_tool_says_on_the_wire_that_it_only_reads(initialized_config: Config) -> None:
    """The ratified norm, in the field the protocol provides for stating it.

    Read-only was true of this surface before it was sayable, and a client had
    no way to ask. Derived from `_tool_definitions()` rather than checked
    against a list of four names, because the property is "every tool", and a
    fifth added tomorrow is exactly the one that would be missed.

    Delivered through the real handshake as well as read off the definitions:
    the annotations are attached after the list is built, and a construction
    step is the kind of thing that can be right in the function and absent from
    the reply.
    """
    advertised = {tool["name"]: tool["annotations"] for tool in mcp._tool_definitions()}
    replies = _converse(
        initialized_config,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ],
    )
    on_the_wire = {tool["name"]: tool.get("annotations") for tool in replies[1]["result"]["tools"]}

    assert on_the_wire == advertised
    for name, hints in on_the_wire.items():
        # An exact dict, not a subset: `destructiveHint` and `openWorldHint`
        # both DEFAULT to the alarming answer, so a payload that dropped one
        # would have a client assume the opposite of what is true here.
        assert hints == {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }, name


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

    for name in _every_tool():
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

    wire = _call(initialized_config, "money_summary")["structuredContent"]

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
    assert wire["rows"][0]["granted_history_status"] == "not_yet_measured"
    kinds = {w["kind"] for w in wire["warnings"]}
    assert "partial" in kinds
    assert "gapped" not in kinds, "an unknown window was reported as a measured shortfall"


def test_a_measured_window_says_it_was_measured(initialized_config: Config) -> None:
    """The status names which kind of number `granted_history_days` is."""
    _seed(initialized_config, granted=90)

    wire = _call(initialized_config, "get_pipeline_health")["structuredContent"]

    assert wire["rows"][0]["granted_history_status"] == "measured"


def test_a_complete_backfill_with_no_transactions_is_not_reported_as_unmeasured(
    initialized_config: Config,
) -> None:
    """🔴 A connection whose accounts post nothing to the transactions feed.

    Its backfill completed and carried no transaction, so the measurement that
    fills `granted_history_days` has nothing to count from and never runs. The
    caveat said the window "is measured when the initial backfill completes" --
    on every answer, forever, about a backfill that had completed. No answer
    from this connection can be short on transactions, because it has none that
    a grant could have cut, so the status says that instead and no `partial`
    rides the answer.
    """
    _seed(initialized_config, granted=None, transactions=False)

    wire = _call(initialized_config, "get_pipeline_health")["structuredContent"]

    row = wire["rows"][0]
    assert row["granted_history_days"] is None, "a window nobody measured was given a number"
    assert row["granted_history_status"] == "no_transactions_to_measure"
    unmeasured = [
        w for w in wire["warnings"] if w["kind"] == "partial" and "not yet known" in w["detail"]
    ]
    assert not unmeasured, unmeasured


def test_a_connection_that_never_completed_a_backfill_is_still_not_yet_measured(
    initialized_config: Config,
) -> None:
    """The case the new status must not swallow: no transactions YET is not none at all.

    `last_success_at` is stamped only once the aggregator says the history is in,
    so a connection without it has told us nothing about what its feed holds.
    """
    _seed(initialized_config, degraded=True, granted=None, transactions=False)

    wire = _call(initialized_config, "get_pipeline_health")["structuredContent"]

    assert wire["rows"][0]["granted_history_status"] == "not_yet_measured"


def test_a_degraded_connection_warns_on_every_answer(initialized_config: Config) -> None:
    """Not only on the health tool — a consumer asking about spending must be told too.

    A caveat only available from a tool nobody thought to call is not in the
    payload, which is the whole point of the norm.
    """
    _seed(initialized_config, degraded=True)

    wire = _call(initialized_config, "query_transactions")["structuredContent"]

    assert any(w["kind"] == "degraded" for w in wire["warnings"])


def test_rows_derived_by_another_version_warn_on_every_answer(
    initialized_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 AC-5.4 says EVERY answer, and the tools reach it by two paths.

    `get_pipeline_health` hands the warnings a reading it has already taken; every
    other tool leaves `_pipeline_warnings` to take its own. A test through the
    health tool alone cannot see the second path break.
    """
    with monkeypatch.context() as previous_build:
        previous_build.setattr(derivation, "DERIVATION_VERSION", derivation.DERIVATION_VERSION - 1)
        _seed(initialized_config)

    for name in _every_tool():
        wire = _call(initialized_config, name)["structuredContent"]
        assert any(w["kind"] == "derivation_version_mismatch" for w in wire["warnings"]), name


def test_an_empty_datastore_says_so_rather_than_answering_zero(
    initialized_config: Config,
) -> None:
    """🔴 "No transactions" and "no connections enrolled" are different answers.

    Without this, an unconfigured install reports zero spending and looks like a
    frugal month.
    """
    wire = _call(initialized_config, "money_summary")["structuredContent"]

    assert wire["rows"] == []
    assert any("no connections are enrolled" in w["detail"] for w in wire["warnings"])
    assert wire["coverage"]["transactions"] == 0


# --------------------------------------------------------------------------
# The answers themselves
# --------------------------------------------------------------------------


def test_the_aggregate_reports_both_directions_as_magnitudes(initialized_config: Config) -> None:
    """🔴 Inflow is REACHABLE, and outflow is still separable from it (#20).

    This tool's predecessor filtered to `amount_minor < 0`, so income had no row
    to appear in at all — a consumer could not recover it by any question, which
    is why the fix had to be a column rather than a second tool. The outflow
    figures below are the same ones that behaviour produced; what is new is that
    `INCOME` is now present rather than absent, and that every row says which
    direction its money went.

    Both are positive magnitudes and `net_minor_units` carries the sign, which
    is the one row where `data-model.md`'s operator-signed convention and the
    reporting convention meet.
    """
    _seed(initialized_config)

    rows = _call(initialized_config, "money_summary")["structuredContent"]["rows"]
    by_category = {r["group_label"]: r for r in rows}

    assert by_category["GENERAL_MERCHANDISE"]["outflow_minor_units"] == 8940
    assert by_category["FOOD_AND_DRINK"]["outflow_minor_units"] == 1200
    assert by_category["INCOME"]["inflow_minor_units"] == 25000, (
        "a deposit is still unreachable, which is the defect #20 records"
    )

    assert all(r["outflow_minor_units"] >= 0 for r in rows), "magnitudes, not signed totals"
    assert all(r["inflow_minor_units"] >= 0 for r in rows), "magnitudes, not signed totals"
    # 🔴 Direction lives in the field name; the SIGN lives here, and only here.
    assert by_category["INCOME"]["net_minor_units"] == 25000
    assert by_category["FOOD_AND_DRINK"]["net_minor_units"] == -1200


def test_a_category_that_nets_to_nothing_says_so(initialized_config: Config) -> None:
    """🔴 The failure this tool exists to make impossible.

    A category of offsetting charges and credits reported its GROSS as though
    that were the cost — measured at $12,000 against a true net of $0, by two
    independent acceptance passes reaching the same figure by different routes.
    Nothing in the old payload could reveal the credits, because the rows they
    would have appeared in were filtered away before grouping.
    """
    _seed(initialized_config)
    rows = _call(initialized_config, "money_summary")["structuredContent"]["rows"]
    totals = {r["group_label"]: r for r in rows}

    gross_out = sum(r["outflow_minor_units"] for r in rows)
    gross_in = sum(r["inflow_minor_units"] for r in rows)
    net = sum(r["net_minor_units"] for r in rows)
    assert net == gross_in - gross_out, "gross and net disagree, so one of them is unusable"
    assert totals["INCOME"]["outflow_minor_units"] == 0


def test_amount_fields_say_they_are_minor_units(initialized_config: Config) -> None:
    """🔴 A consumer dividing by 100 without knowing would be wrong by two orders
    of magnitude, and the number would still look plausible."""
    _seed(initialized_config)

    rows = _call(initialized_config, "query_transactions")["structuredContent"]["rows"]

    assert "amount_minor_units" in rows[0]
    assert all("amount" not in key or "minor_units" in key for key in rows[0])


def test_a_transaction_row_carries_the_id_of_the_account_it_is_on(
    initialized_config: Config,
) -> None:
    """🔴 `account` is a display name, and two accounts can share one.

    Every other tool keys on `account_id`, and the tool descriptions send a
    caller from a suspicious row to `get_coverage_report` or to a narrowed
    `query_transactions` — both of which take the id. Without it on the row the
    caller has a name that may match two accounts and no way to tell them apart,
    so it either guesses or reports the wrong account. Real households hold two
    accounts called "Checking", and `mask` is nullable.

    Asserted as a join that actually completes, not as key presence: the id has
    to be the one `list_accounts` publishes and the one the argument accepts.
    """
    _seed(initialized_config)

    rows = _call(initialized_config, "query_transactions")["structuredContent"]["rows"]
    accounts = _call(initialized_config, "list_accounts")["structuredContent"]["rows"]

    ids = {row["account_id"] for row in rows}
    assert ids, "the fixture returned no rows, so this checks nothing"
    assert ids <= {account["account_id"] for account in accounts}, (
        "a transaction names an account id list_accounts does not publish"
    )
    for row in rows:
        narrowed = _call(
            initialized_config, "query_transactions", {"account_id": row["account_id"]}
        )["structuredContent"]["rows"]
        assert row["transaction_id"] in {r["transaction_id"] for r in narrowed}, (
            "the id on the row does not select the row when passed back as the argument"
        )


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


def test_a_failure_serialising_the_answer_is_reported_without_closing_the_session(
    initialized_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 The half of the boundary the tool `try` used to sit inside of.

    Rendering the answer is part of answering the call, so a failure there is
    the same event as a failure in the query: the client is waiting, and an
    exception escaping instead ends `serve()` and takes the session with it.
    The operator sees their tool disappear on one particular question, which is
    the one outcome this module names as worse than any wrong answer.

    Reachable without a bug in this product: SQLite's dynamic typing lets a BLOB
    sit in a TEXT column, and a `bytes` in `currency` or `description` makes
    `json.dumps` raise. Driven here through the answer's own renderer, which is
    the first step outside the query and the step the query result cannot
    protect.
    """

    class _UnrenderableAnswer:
        def to_wire(self) -> dict[str, Any]:
            raise TypeError("Object of type bytes is not JSON serializable")

    monkeypatch.setattr(
        query, "list_accounts", lambda *args, **kwargs: cast(Any, _UnrenderableAnswer())
    )

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
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        ],
    )

    assert len(replies) == 3, "the request or the session after it went unanswered"
    result = replies[1]["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "internal_error"
    text = result["content"][0]["text"]
    assert "TypeError" not in text and "bytes" not in text, "the exception crossed the boundary"
    assert replies[2]["id"] == 3 and replies[2]["result"] == {}, (
        "the pipe did not survive the failure, which is the tool disappearing mid-session"
    )


def test_a_failure_outside_a_tool_call_is_answered_rather_than_ending_the_session(
    initialized_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tool `try` covers one method; the read loop covers every other one.

    `initialize`, `tools/list` and the resource methods each assemble a reply
    from this process's own state, and an exception in any of them escapes to
    the loop. Answered as an internal error so the client's promise settles and
    the next request is still served.
    """

    def explode() -> list[dict[str, Any]]:
        raise RuntimeError("the tool surface fell over")

    monkeypatch.setattr(mcp, "_tool_definitions", explode)

    replies = _converse(
        initialized_config,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        ],
    )

    assert len(replies) == 2, "the session ended instead of answering both frames"
    assert replies[0]["id"] == 1
    assert replies[0]["error"]["code"] == mcp._INTERNAL_ERROR
    assert "fell over" not in replies[0]["error"]["message"], "the exception crossed the boundary"
    assert replies[1]["id"] == 2 and replies[1]["result"] == {}


def test_a_notification_that_fails_is_still_not_replied_to(
    initialized_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 The last-resort catch must not turn a notification into a reply.

    A JSON-RPC notification carries no `id` and takes no response at all, so a
    failure while handling one is logged and dropped. Answering it would put a
    frame on the wire the client has no promise waiting for, which is a protocol
    error on this side and the reason the loop asks whether an `id` is present
    rather than whether it is null.
    """

    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("handling the notification fell over")

    monkeypatch.setattr(mcp, "_handle", explode)

    replies = _converse(
        initialized_config,
        [
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        ],
    )

    assert [reply["id"] for reply in replies] == [1], (
        "a notification was answered, or the session ended before the next request"
    )


def test_a_closed_pipe_ends_the_session_cleanly(initialized_config: Config) -> None:
    """🔴 The client going away is how a session ends, not a crash to report.

    A client that exits between reading a request and reading its answer leaves
    the write end broken. Unhandled, the traceback is the last thing in the
    operator's log and the exit code says the server failed; caught, the loop
    stops and reports the same success a clean end-of-stream reports.
    """

    class _ClosedPipe(io.StringIO):
        def write(self, _text: str) -> int:
            raise BrokenPipeError(32, "Broken pipe")

    stdin = io.StringIO(
        "\n".join(
            json.dumps(request)
            for request in (
                {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            )
        )
        + "\n"
    )

    assert mcp.serve(initialized_config, stdin=stdin, stdout=_ClosedPipe()) == EXIT_OK


def test_an_unknown_tool_is_refused_as_a_bad_parameter(initialized_config: Config) -> None:
    """🔴 The code changed from `-32601`, and the name it refuses did not.

    `-32601` says the METHOD is not implemented, so a client classifying by code
    concludes this server does not support `tools/call` at all and stops calling
    it. The tool name is a parameter of a method this server does serve, and the
    specification's own tools example answers an unknown one with `-32602`.
    """
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

    assert replies[1]["error"]["code"] == -32602
    assert "delete_everything" in replies[1]["error"]["message"], (
        "the refusal no longer names the tool it rejected"
    )


@pytest.mark.parametrize("arguments", [["since"], [], 0, False, ""])
def test_a_non_object_arguments_member_is_refused_as_a_bad_parameter(
    initialized_config: Config, arguments: object
) -> None:
    """`arguments` of the wrong type is a params problem, not a malformed request.

    `-32600` describes the request OBJECT — a frame that is not a valid JSON-RPC
    request at all. This frame is one, and the fault is in what it carries, so a
    client is told to correct its parameters rather than its framing.

    The falsy shapes are in the list on purpose: an `or {}` default would let
    `[]`, `0`, `false` and `""` run the tool over defaults instead of being
    refused, and only `null` and absence mean "no arguments".
    """
    replies = _converse(
        initialized_config,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "list_accounts", "arguments": arguments},
            },
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        ],
    )

    assert replies[1]["error"]["code"] == -32602
    assert replies[2]["id"] == 3, "the session ended rather than answering the next request"


def test_the_text_form_of_an_answer_is_compact_json(initialized_config: Config) -> None:
    """🔴 Every answer is sent twice, so the second copy is paid for twice over.

    Most clients put both `structuredContent` and the text block into the
    model's context, and no human reads the text one — measured, a full page of
    rows cost ~27% more as pretty-printed JSON than as compact, on a payload
    already large enough to crowd out the question it was answering.

    Asserted against the two renderings of THIS answer rather than against a
    byte count, which would pin a fixture rather than the separator choice. A
    substring sweep for `", "` cannot do it either: warning details are English
    sentences and carry the sequence honestly.
    """
    _seed(initialized_config)

    result = _call(initialized_config, "query_transactions")

    text = result["content"][0]["text"]
    wire = result["structuredContent"]
    assert json.loads(text) == wire, "the two forms of one answer stopped agreeing"
    assert "\n" not in text, "the text form is still pretty-printed"
    assert len(text) == len(json.dumps(wire, separators=(",", ":"))), (
        "the text form is padded, so it is not the compact rendering"
    )
    assert len(text) < len(json.dumps(wire, indent=2)), (
        "the text form costs as much as the indented one it replaced"
    )


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
    for name in _every_tool():
        wire = _call(config, name)["structuredContent"]
        assert wire["rows"] == [], name
        assert any(w["kind"] == "partial" for w in wire["warnings"]), name


@pytest.mark.parametrize(
    ("tool", "arguments", "expects_window"),
    [
        (
            name,
            _A_WINDOW if name in _tools_requiring("effective_window") else {},
            name in _tools_requiring("effective_window"),
        )
        for name in _every_tool()
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
    wire = _call(config, "money_summary")["structuredContent"]

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


def test_the_health_row_declares_what_the_sign_check_concluded(
    initialized_config: Config,
) -> None:
    """🔴 AC-14.3, at the wire. The verdict reaches a consumer with its evidence.

    The shared fixture writes two rows in the judged categories, which is below
    the sample floor -- so the honest answer is `undetermined`, and asserting
    exactly that is the point: a check that answered `consistent` over two rows
    would be manufacturing confidence out of a small store, which is the failure
    the floor exists to prevent. The counts ride along because a verdict with no
    evidence under it is a claim rather than a measurement.
    """
    _seed(initialized_config)

    wire = _call(initialized_config, "get_pipeline_health")["structuredContent"]
    row = wire["rows"][0]

    assert row["sign_convention"] == "undetermined"
    assert row["sign_convention_rows_judged"] == 2
    assert row["sign_convention_rows_positive"] == 0
    assert not [w for w in wire["warnings"] if w["kind"] == "sign_convention_unverified"]


def test_the_published_health_schema_states_the_checks_category_set_and_threshold(
    initialized_config: Config,
) -> None:
    """🔴 AC-14.3 requires the check to DECLARE its category set and threshold.

    Declared where the consumer of the verdict actually reads -- the published
    `outputSchema`, which `api-contract.md` fixes as the statement nothing thins
    out -- rather than only in a comment in the module. A verdict whose basis is
    documented somewhere the agent cannot see is a bare assertion at the moment
    it matters.

    Read from `signs` rather than spelled out here, so adding a category with
    its measurement moves this test by construction and adding one without
    updating the schema fails it.
    """
    definition = next(d for d in mcp._tool_definitions() if d["name"] == "get_pipeline_health")
    published = definition["outputSchema"]["properties"]["rows"]["items"]["properties"]
    declared = published["sign_convention"]["description"]

    for category in signs.NEVER_INFLOW_CATEGORIES:
        assert category in declared, f"the published schema does not name {category}"
    assert f"{signs.INVERTED_ABOVE_SHARE:.0%}" in declared
    assert str(signs.MINIMUM_JUDGEABLE_ROWS) in declared
    assert published["sign_convention"]["enum"] == list(signs.VERDICTS)


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
        "money_summary",
        {"since": str(today - timedelta(days=1)), "until": str(today + timedelta(days=1))},
    )
    before = _call(
        initialized_config,
        "money_summary",
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

    result = _call(initialized_config, "money_summary", {"since": "August 2024"})

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
from bankmachine import envelope, query
from bankmachine.config import Config


def dispatch(config: Config, arguments: dict[str, object]) -> envelope.Answer:
    return query.list_transactions(config, since=arguments.get("since"))
"""

_NARROWED = """
from datetime import date

from bankmachine import envelope, query
from bankmachine.config import Config


def dispatch(config: Config, arguments: dict[str, object]) -> envelope.Answer:
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

    result = _call(initialized_config, "money_summary", {"sinceX": "2024-09-09"})

    assert result["isError"] is True
    message = result["content"][0]["text"]
    assert "sinceX" in message, "the refusal does not name the argument it rejected"
    # 🔴 An exact set, not `"since" in message`: the refusal already contains
    # 'sinceX', so that substring can never fail. When one valid value contains
    # another as text, `in` cannot tell them apart.
    _, _, accepted = message.partition("It accepts: ")
    assert accepted, f"the refusal does not say what is accepted: {message!r}"
    assert set(accepted.strip().split(", ")) == {"group_by", "since", "until"}, (
        f"the refusal offered {accepted.strip()!r}"
    )
    assert result["structuredContent"]["error"]["code"] == "invalid_argument"


def _delivered_guidance(config: Config) -> str:
    """Everything a client can put in front of the model: the primer AND the resources.

    🔴 The union, because the primer alone is no longer the whole statement and
    holding it to the whole vocabulary would force the vocabulary back into a
    text a client TRUNCATES. Measured: one client delivered 2,045 of 6,673
    characters and cut mid-table, so what a longer primer buys is not coverage
    but the appearance of it. The resources are served by URI and arrive whole
    when they are asked for, so a field or a kind that lives there is reachable
    in a way a cut paragraph is not — and the union is what nothing may fall out
    of.
    """
    return "\n".join(
        [mcp._instructions(config), *(document.text for document in mcp._reference_documents())]
    )


def test_what_the_server_delivers_names_every_field_the_envelope_actually_carries(
    initialized_config: Config,
) -> None:
    """🔴 A closed list in prose is one that stops matching the payload it describes.

    What a consuming agent can read about this envelope is the handshake primer
    plus the reference documents this server serves by URI. Adding `build` to
    the wire and to none of them would leave every text the agent can reach
    actively denying the field exists -- which is exactly how a stale-build
    round happens again, since `build` is what would have prevented the last one.

    Asserted against the real envelope rather than a second hand-written list,
    because a second list is one that stops matching the first. A name counts as
    delivered when it appears as its own leaf -- how the primer writes it -- or
    as the dotted path the envelope reference renders.

    🔴 **Over the UNION of every tool's envelope, and one level into it, never
    one sample.** A tool that carries neither `effective_window` nor
    `truncation` cannot discriminate a rule about them, and a scan of top-level
    keys alone cannot see a key nested inside a block — so a guard written
    either way passes while the text a consuming agent reads denies a field
    exists. A check that samples one instance of the thing it generalises over
    is a check whose bad news never arrives, which is the trap `learnings.md`
    records twice.
    """
    _seed(initialized_config)
    delivered = _delivered_guidance(initialized_config)

    envelope: set[str] = set()
    for definition in mcp._tool_definitions():
        wire = _call(initialized_config, definition["name"])["structuredContent"]
        for key, value in wire.items():
            envelope.add(key)
            # 🔴 One level down as well. A key nested inside a block is invisible
            # to a scan of the top level, and that is not hypothetical: the
            # window-scoped coverage sibling shipped and stayed unnamed here
            # while this guard passed, because it lives inside `coverage`. The
            # blocks are exactly where the numbers a consumer sums live.
            if isinstance(value, dict):
                envelope |= {f"{key}.{nested}" for nested in value}
    assert {
        "effective_window",
        "truncation",
        "coverage.transactions_in_effective_window",
    } <= envelope, "the union lost the keys this guard exists for, so it is back to sampling"

    missing = sorted(
        key
        for key in envelope
        if f"`{key}`" not in delivered and f"`{key.rsplit('.', 1)[-1]}`" not in delivered
    )

    assert not missing, (
        f"the envelope carries {missing} and nothing this server delivers names them; "
        f"an agent reading the primer and both resources does not know they exist"
    )


def test_what_the_server_delivers_names_every_warning_kind_the_vocabulary_defines(
    initialized_config: Config,
) -> None:
    """🔴 The kinds are the half an agent is told to branch on, and nothing pinned them.

    The envelope guard above pins FIELDS. This one pins KINDS, over the same
    union: a kind the vocabulary declares and no delivered text names is one an
    agent is told to branch on and was never given. Derived from the vocabulary
    rather than from a second list here, for the reason the vocabulary exists at
    all.
    """
    delivered = _delivered_guidance(initialized_config)

    missing = sorted(k for k in envelope.WARNING_KINDS if f"`{k}`" not in delivered)

    assert not missing, (
        f"the vocabulary defines {missing} and nothing this server delivers names them; "
        f"an agent told to read `warnings` cannot act on a kind it was never given"
    )


def test_the_primer_fits_inside_what_a_client_actually_delivers(
    initialized_config: Config,
) -> None:
    """🔴 The measurement this budget exists for: a client cut it, and said nothing.

    One client handed the model 2,045 of 6,673 characters and stopped mid-table.
    Everything past the cut — six of the warning kinds, the whole envelope table,
    and the pointer telling the agent the reference resources exist — was never
    read, and the surviving text reads complete. A primer that fits is the only
    version of this text that is actually delivered.

    🔴 The two resource URIs are asserted to be in the FIRST lines rather than
    merely present, because a pointer that would be cut is a pointer that does
    not exist — and it is the pointer that makes everything else reachable.
    """
    primer = mcp._instructions(initialized_config)

    assert len(primer) <= mcp.INSTRUCTIONS_BUDGET, (
        f"the primer is {len(primer)} characters against a budget of "
        f"{mcp.INSTRUCTIONS_BUDGET}; a client that trims will hand the model a prefix of it"
    )
    opening = "\n".join(primer.splitlines()[:3])
    for uri in (mcp_resources.ENVELOPE_URI, mcp_resources.WARNINGS_URI):
        assert uri in opening, f"{uri} is not in the first three lines, so it can be cut"


#: What `Implementation` -- the type of `serverInfo` -- declares, read from
#: `mcp_types` 2.2.0. Written out rather than imported because this product has
#: no `mcp` dependency and is not acquiring one to run a test; the list is the
#: fact the test needs, and it moves only when the SDK's type does.
_IMPLEMENTATION_FIELDS = frozenset(
    {"name", "title", "version", "description", "websiteUrl", "icons"}
)


def test_the_handshake_reports_the_running_build(initialized_config: Config) -> None:
    """🔴 The handshake is what a client shows a human BEFORE any tool is called.

    All three keys were untested, including `version` -- which stopped being the
    literal "0.1.0" and became package metadata, so it now reports "unknown"
    for a source tree that was never installed. An untested handshake is how a
    client-facing identity drifts from the code that serves it.

    🔴 Read from `_meta`, and `serverInfo` is checked for the ABSENCE of the
    same keys, because that is where the identity is actually readable.
    `Implementation` declares six fields and the SDK's wire base leaves
    pydantic's `extra="ignore"` in force, so a `commit` on `serverInfo` is
    dropped in the client's parser -- present in the bytes, gone by the time
    anything reads them, which no assertion over the raw reply would catch.
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
    result = replies[0]["result"]
    server_info = result["serverInfo"]
    build = result["_meta"][mcp.BUILD_META_KEY]
    identity = build_id.build_identity()

    # Nothing rides `serverInfo` that a strict reading of `Implementation` would
    # discard -- the test of survival, since a dropped key looks identical to a
    # delivered one from this side of the pipe.
    assert set(server_info) <= _IMPLEMENTATION_FIELDS, (
        f"{sorted(set(server_info) - _IMPLEMENTATION_FIELDS)} would be dropped before any "
        f"SDK-based client could read them"
    )
    assert server_info["version"] == identity.version
    assert build == {
        "version": identity.version,
        "commit": identity.commit,
        "dirty": identity.dirty,
    }
    # The handshake and the envelope must not be able to disagree about the
    # build: two readings of one process are one fact, and a client that showed
    # a human one commit while an agent read another would be unfalsifiable.
    envelope = _call(initialized_config, "list_accounts")["structuredContent"]["build"]
    assert envelope == build


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
    """🔴 It said "no tool named 'money_summary'" — a false statement about a real tool.

    The unknown-tool guard used to wrap the handler CALL, so any `KeyError`
    raised inside the query layer surfaced as JSON-RPC -32601. A consumer acting
    on that would stop calling a tool that exists and works.
    """

    def explode(*args: Any, **kwargs: Any) -> None:
        raise KeyError("a column the deriver expected")

    monkeypatch.setattr(query, "money_summary", explode)

    result = _call(initialized_config, "money_summary")

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "internal_error"


def test_every_unrecognized_argument_is_named_at_once(initialized_config: Config) -> None:
    """Two typos should cost one round trip, not two."""
    _seed(initialized_config)

    result = _call(initialized_config, "money_summary", {"sinceX": "x", "untilX": "y"})

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

    for value in (0, -5, envelope.MAX_ROWS + 1):
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

    for tool in ("money_summary", "query_transactions"):
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

    for tool in _every_tool():
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
    assert envelope.MAX_ROWS == 500


def test_the_advertised_bounds_match_the_enforced_ones() -> None:
    """A caller should learn the bounds from the schema, not by being refused."""
    limit = next(
        d["inputSchema"]["properties"]["limit"]
        for d in mcp._tool_definitions()
        if d["name"] == "query_transactions"
    )

    assert limit["minimum"] == 1
    assert limit["maximum"] == envelope.MAX_ROWS


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
    request_scoped = set(envelope.REQUEST_SCOPED_KINDS)
    assert request_scoped, "the request-scoped kinds vanished from the vocabulary"
    return [w["kind"] for w in wire["warnings"] if w["kind"] in request_scoped]


@pytest.mark.parametrize("tool", ["query_transactions", "money_summary"])
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


@pytest.mark.parametrize("tool", ["query_transactions", "money_summary"])
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

    `money_summary` over a window that precedes coverage returns no rows.
    "You spent nothing" and "this is not knowable" were the same payload; the
    effective window plus its warning are what separate them.
    """
    _seed(initialized_config)

    wire = _call(
        initialized_config,
        "money_summary",
        {"since": "2024-01-01", "until": "2024-06-30"},
    )["structuredContent"]

    assert wire["rows"] == []
    assert _request_kinds(wire) == ["window_starts_before_coverage"]
    assert wire["effective_window"]["effective"] == {"since": None, "until": None}


@pytest.mark.parametrize("tool", _every_tool_except(*_tools_requiring("effective_window")))
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
        if d["name"] in {"query_transactions", "money_summary"}
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
            replay_passes=(),
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
    assert wire["truncation"]["remaining"] == 133
    assert wire["truncation"]["truncated"] is True
    assert len(wire["rows"]) == 10, "the block disagrees with the rows beside it"
    assert _request_kinds(wire) == ["rows_truncated"]
    detail = next(w["detail"] for w in wire["warnings"] if w["kind"] == "rows_truncated")
    assert "133 transactions match this request" in detail
    assert "123 of them are still missing" in detail


def test_a_complete_answer_says_it_is_complete(initialized_config: Config) -> None:
    """The reverse half. A block that read `truncated: true` always would say nothing."""
    _seed(initialized_config)

    wire = _call(initialized_config, "query_transactions")["structuredContent"]

    assert wire["truncation"] == {
        "returned": 3,
        "remaining": 3,
        "matching": 3,
        "truncated": False,
    }
    assert _request_kinds(wire) == []


def test_the_aggregate_says_whether_its_group_list_was_cut(
    initialized_config: Config,
) -> None:
    """🔴 A CONTRACT CHANGE: the aggregate used to say this by SAYING NOTHING.

    Absence meant "unpaginated by construction, bounded by the grouping". The
    grouping stopped bounding anything once the merchant key could fall back to
    a per-transaction description -- the group count approaches the transaction
    count, and the payload grew without limit while nothing on it said so.

    An absence meaning "nothing was cut" is worth having. An absence meaning
    "nobody checked" is what this replaces.
    """
    _seed_many(initialized_config, 130)

    wire = _call(initialized_config, "money_summary")["structuredContent"]

    assert wire["truncation"]["truncated"] is False, "this store holds fewer groups than the cap"
    assert wire["truncation"]["returned"] == wire["truncation"]["matching"]
    assert _request_kinds(wire) == []


@pytest.mark.parametrize("tool", _every_tool_except(*_tools_requiring("truncation")))
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
        {"since": str(today - timedelta(days=4)), "limit": envelope.MAX_ROWS},
    )["structuredContent"]

    assert wire["coverage"]["transactions"] == 133
    assert wire["coverage"]["transactions_in_effective_window"] == 8
    assert wire["truncation"]["matching"] == 8


def test_the_capped_tool_describes_its_cap_and_the_aggregate_does_not() -> None:
    """AC-9.4: a tool description states its conventions.

    The note belongs to the two PAGED tools alone — `query_transactions` and
    `balance_history` — and saying it on the aggregate would describe a cursor
    that tool does not issue.
    """
    described = {d["name"]: d["description"] for d in mcp._tool_definitions()}
    paged = ("query_transactions", "balance_history")

    for name in paged:
        assert mcp._TRUNCATION_NOTE in described[name], name
    for name in _every_tool_except(*paged):
        assert mcp._TRUNCATION_NOTE not in described[name], name


@pytest.mark.parametrize(
    ("tool", "expects_truncation"),
    [("query_transactions", True), ("money_summary", True), ("list_accounts", False)],
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

    assert wire["truncation"] == {
        "returned": 0,
        "remaining": 0,
        "matching": 0,
        "truncated": False,
    }
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


def _walk_the_series(config: Config, *, limit: int) -> tuple[list[dict[str, Any]], int]:
    """Page `balance_history` over stdio until it stops offering a next page."""
    rows: list[dict[str, Any]] = []
    pages = 0
    cursor: str | None = None
    while True:
        sent: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            sent["cursor"] = cursor
        result = _call(config, "balance_history", sent)
        assert result["isError"] is False, result["content"][0]["text"]
        wire = result["structuredContent"]
        pages += 1
        rows.extend(wire["rows"])
        cursor = wire["truncation"].get("next_cursor")
        if cursor is None:
            break
        # A cursor that fails to advance hangs the test rather than reddening it.
        assert pages <= 40, "the walk did not terminate"
    return rows, pages


def test_a_balance_history_walk_over_the_wire_reaches_every_row_exactly_once(
    initialized_config: Config,
) -> None:
    """🔴 The series cursor, sent back through the JSON-RPC boundary as a consumer sends it.

    Every other paging test for this tool calls the query layer, so a dispatch
    that decoded the cursor and never handed it on would leave them all green
    while every caller re-read page one forever.
    """
    _seed(initialized_config)
    _seed_investments(initialized_config)
    whole = _call(initialized_config, "balance_history", {"limit": 500})["structuredContent"]
    assert len(whole["rows"]) > 6, "too few rows to page, so the walk proves nothing"

    rows, pages = _walk_the_series(initialized_config, limit=3)

    assert rows == whole["rows"], "the walk did not reassemble the series, each row once"
    assert pages == -(-len(whole["rows"]) // 3)


def test_each_paged_tool_refuses_the_other_tools_cursor_at_the_boundary(
    initialized_config: Config,
) -> None:
    """A page position from one series never resumes a walk over the other."""
    _seed_many(initialized_config, 30)
    _seed_investments(initialized_config)
    issued = {
        tool: _call(initialized_config, tool, {"limit": 1})["structuredContent"]["truncation"][
            "next_cursor"
        ]
        for tool in ("query_transactions", "balance_history")
    }

    for tool, foreign in (
        ("balance_history", issued["query_transactions"]),
        ("query_transactions", issued["balance_history"]),
    ):
        result = _call(initialized_config, tool, {"limit": 1, "cursor": foreign})
        assert result["isError"] is True, f"{tool} answered with the other tool's cursor"
        assert "cursor" in result["content"][0]["text"], tool


def test_the_cursor_is_advertised_on_the_capped_tool_and_nowhere_else() -> None:
    """A caller learns the argument from the schema, and a misspelling is refused by name.

    `additionalProperties: False` plus `_permitted_arguments` means an argument
    that is not advertised cannot be sent — so an unadvertised `cursor` would
    make the escape route unreachable to a caller reading the tool definition,
    which is the only thing an agent reads.
    """
    paged = ("query_transactions", "balance_history")
    for name in paged:
        assert "cursor" in mcp._permitted_arguments(name), name
    for name in _every_tool_except(*paged):
        assert "cursor" not in mcp._permitted_arguments(name), name


def test_the_way_past_the_cap_is_named_everywhere_an_agent_might_look(
    initialized_config: Config,
) -> None:
    """🔴 `next_cursor` is nested inside `truncation`, so the envelope guard cannot see it.

    That guard walks the TOP-LEVEL keys of each tool's payload; a field one
    level down is invisible to it, and a field that appears only on a truncated
    answer is invisible to a call it makes with no arguments. Both gaps point the
    same way — the one field that gets a caller past the cap could go unmentioned
    on every surface an agent reads.

    The primer has to name the field, because paging is the instruction it gives.
    What to pass it back AS is detail, so it is asserted over the union the
    server delivers rather than forced into a text a client trims.
    """
    primer = mcp._instructions(initialized_config)
    delivered = _delivered_guidance(initialized_config)

    assert "`next_cursor`" in primer
    assert "`cursor`" in delivered
    assert mcp._TRUNCATION_NOTE.count("`next_cursor`") >= 1


# --------------------------------------------------------------------------
# The answer's shape, published (`outputSchema`)
# --------------------------------------------------------------------------


#: The JSON Schema keywords `_violations` below understands, and therefore the
#: whole of what a published schema may use. A validator that skips a keyword it
#: does not recognize reports success over a payload nothing checked, and the
#: green is what stops anyone looking -- so the schemas are held to this set by a
#: test rather than the validator being trusted to have kept up with them.
_IMPLEMENTED_KEYWORDS = frozenset(
    {"type", "properties", "required", "additionalProperties", "items", "enum", "description"}
)

_PRIMITIVES: dict[str, type] = {"object": dict, "array": list, "string": str, "null": type(None)}


def _is_of_type(value: Any, name: str) -> bool:
    """One JSON Schema primitive, with Python's bool/int overlap taken back out.

    `True` is an `int` in Python and `1` is a boolean nowhere on the wire, so a
    check leaning on `isinstance` alone would accept `truncated: 1` and
    `pending: 3` -- the class of thing a published schema exists to refuse.
    """
    if name == "boolean":
        return isinstance(value, bool)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    return isinstance(value, _PRIMITIVES[name])


def _violations(value: Any, schema: dict[str, Any], path: str = "") -> list[str]:
    """Every place a payload disagrees with a schema, in the subset the schemas use.

    🔴 Written here rather than reached for. `jsonschema` would be a sixth direct
    dependency against a product that guards that number, and what a test needs
    is these six keywords -- the guard above is what keeps the shortcut honest
    by failing the moment a schema uses a seventh.

    Returns the disagreements rather than raising, so a payload that is wrong in
    four places names all four instead of one at a time.
    """
    where = path or "<root>"
    declared = schema.get("type")
    if declared is not None:
        names = declared if isinstance(declared, list) else [declared]
        if not any(_is_of_type(value, name) for name in names):
            # Returned rather than accumulated: nothing below can say anything
            # true about a value that is not even the right shape.
            return [f"{where}: expected {declared}, got {type(value).__name__}"]
    errors: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{where}: {value!r} is not one of {schema['enum']}")
    if isinstance(value, dict):
        properties: dict[str, Any] = schema.get("properties", {})
        errors += [
            f"{where}.{key}: required, and absent"
            for key in schema.get("required", [])
            if key not in value
        ]
        if schema.get("additionalProperties") is False:
            errors += [
                f"{where}.{key}: not permitted here" for key in value if key not in properties
            ]
        for key, subschema in properties.items():
            if key in value:
                errors += _violations(value[key], subschema, f"{where}.{key}")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors += _violations(item, schema["items"], f"{where}[{index}]")
    return errors


def _keywords(schema: dict[str, Any]) -> set[str]:
    """Every keyword the schema uses, at every depth."""
    used = set(schema)
    for subschema in schema.get("properties", {}).values():
        used |= _keywords(subschema)
    if "items" in schema:
        used |= _keywords(schema["items"])
    return used


#: One call per tool that reaches the parts of the envelope a schema can get
#: wrong. The window is deliberately wider than the seeded data, so the clamp
#: fires and `effective_window` carries real bounds beside a window caveat; and
#: `_seed` grants less history than it requests, so a `gapped` warning populates
#: the two optional keys a caveat can carry. A validation run over payloads
#: whose every optional half is absent checks only the half that cannot fail.
_LIVE_CALLS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("list_accounts", {}),
    ("list_holdings", {}),
    ("balance_history", {"since": "2020-01-01", "until": "2030-12-31"}),
    ("query_transactions", {"since": "2020-01-01", "until": "2030-12-31"}),
    ("money_summary", {"since": "2020-01-01", "until": "2030-12-31"}),
    ("get_pipeline_health", {}),
    ("get_coverage_report", {}),
)


def test_every_tool_publishes_the_shape_of_the_answer_it_returns(
    initialized_config: Config,
) -> None:
    """🔴 The envelope was described to an agent only in prose, and prose is what gets trimmed.

    `outputSchema` is where the protocol takes the same statement in a form a
    client can check rather than read, so it survives a context budget that the
    handshake text does not. Delivered through the real `tools/list` as well as
    read off the definitions: the schema is assembled per definition, and an
    assembly step can be right in the function and absent from the reply.

    Root `type: "object"` because that is what the protocol restricts an output
    schema to, and a root of any other type is dropped by a client that parses
    the field into its declared type.
    """
    advertised = {tool["name"]: tool.get("outputSchema") for tool in mcp._tool_definitions()}
    replies = _converse(
        initialized_config,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ],
    )
    on_the_wire = {tool["name"]: tool.get("outputSchema") for tool in replies[1]["result"]["tools"]}

    assert on_the_wire == advertised
    assert on_the_wire, "no tools were advertised, so this checked nothing"
    for name, schema in on_the_wire.items():
        assert schema is not None, f"{name} publishes no output schema"
        assert schema["type"] == "object", name


def test_the_published_schemas_use_only_keywords_the_checking_here_implements() -> None:
    """🔴 A keyword nothing implements is a rule that silently stops being enforced.

    The validation below is this repo's own, so its blind spots are this repo's
    to notice: an unrecognized keyword is not an error there, it is simply
    skipped, and every payload then passes the rule it states. This is what
    makes the shortcut safe to keep -- a schema that reaches for a seventh
    keyword fails here rather than passing everywhere.
    """
    used: set[str] = set()
    for definition in mcp._tool_definitions():
        used |= _keywords(definition["outputSchema"])

    assert used, "no keywords were collected, so the walk found nothing"
    assert used <= _IMPLEMENTED_KEYWORDS, (
        f"the schemas use {sorted(used - _IMPLEMENTED_KEYWORDS)}, which nothing here checks; "
        f"a payload violating those keywords would validate"
    )


def test_a_real_answer_from_every_tool_validates_against_its_own_schema(
    initialized_config: Config,
) -> None:
    """🔴 The real payload, from the real loop, against the schema the same session published.

    A schema checked against a fixture shaped like an answer agrees with the
    fixture. What a validating client actually does is take the tool's published
    `outputSchema` and hold `structuredContent` to it -- so that is what happens
    here, because a schema that has drifted from its payload is worse than no
    schema at all: the client rejects a good answer.
    """
    _seed(initialized_config)
    # Positions, so `list_holdings` answers with rows its row schema can check.
    _seed_investments(initialized_config)
    schemas = {d["name"]: d["outputSchema"] for d in mcp._tool_definitions()}
    seen: list[dict[str, Any]] = []

    for tool, arguments in _LIVE_CALLS:
        wire = _call(initialized_config, tool, arguments)["structuredContent"]
        seen.append(wire)

        assert wire["rows"], f"{tool} answered with no rows, so the row schema checked nothing"
        assert _violations(wire, schemas[tool]) == [], tool

    assert {tool for tool, _ in _LIVE_CALLS} == set(schemas), "a tool went unchecked"
    # The optional halves have to have been reached, or this run proved only
    # that the required keys are required.
    caveats = [caveat for wire in seen for caveat in wire["warnings"]]
    assert any("connection_id" in caveat for caveat in caveats), (
        "no warning named its connection, so the optional keys of a caveat went unchecked"
    )
    assert any(wire.get("effective_window", {}).get("effective", {}).get("since") for wire in seen)


def test_an_answer_from_an_unreadable_store_validates_too(config: Config) -> None:
    """🔴 AC-ARCH.3's answer is still an answer, and a client validates it like any other.

    The missing-datastore path is the one place the envelope is assembled by a
    different function, and it is the path a consumer meets before anything else
    has gone right. A schema that only described the healthy shape would have
    the client reject the one answer that explains why the rest are empty.
    """
    assert not config.datastore_path.exists()
    schemas = {d["name"]: d["outputSchema"] for d in mcp._tool_definitions()}

    for tool, arguments in _LIVE_CALLS:
        wire = _call(config, tool, arguments)["structuredContent"]

        assert wire["rows"] == [], tool
        assert _violations(wire, schemas[tool]) == [], tool


def test_a_truncated_page_validates_with_the_cursor_it_carries(
    initialized_config: Config,
) -> None:
    """The one optional key inside `truncation`, which no complete answer emits.

    `next_cursor` is present when and only when there is another page, so every
    other check here runs over payloads that do not have it -- and a schema is
    only ever wrong about the key that is absent from the sample.
    """
    _seed_many(initialized_config, 130)
    schema = next(
        d["outputSchema"] for d in mcp._tool_definitions() if d["name"] == "query_transactions"
    )

    wire = _call(initialized_config, "query_transactions", {"limit": 10})["structuredContent"]

    assert wire["truncation"]["next_cursor"], "the page carried no cursor, so this checked nothing"
    assert _violations(wire, schema) == []


def test_the_conditional_keys_are_published_exactly_where_an_answer_carries_them(
    initialized_config: Config,
) -> None:
    """🔴 A key's presence in the schema is a statement about the TOOL, and it has to be true.

    `api-contract.md` fixes an absent `effective_window` as "this tool takes no
    window" and an absent `truncation` as "this tool returns every row it
    found". A single schema permitting both on every tool would publish the
    opposite of that for three of these four, and a consumer reading the schema
    to decide whether to pass a window would get no answer from it.

    The expectation is read off the payload rather than from a table written
    here, because a table is a second description of the same fact and it is the
    one that stops matching. The schema is hand-written in `mcp` and the payload
    is assembled in `query`: two independent statements, compared.
    """
    _seed(initialized_config)
    schemas = {d["name"]: d["outputSchema"] for d in mcp._tool_definitions()}
    carried: dict[str, set[str]] = {"effective_window": set(), "truncation": set()}

    for tool, arguments in _LIVE_CALLS:
        wire = _call(initialized_config, tool, arguments)["structuredContent"]
        schema = schemas[tool]
        for key in carried:
            if key in wire:
                carried[key].add(tool)
            assert (key in schema["properties"]) is (key in wire), (
                f"{tool}'s schema and its answer disagree about whether it carries {key}"
            )
            assert (key in schema["required"]) is (key in wire), (
                f"{tool} may omit {key}, which makes its presence say nothing"
            )
        sibling = "transactions_in_effective_window"
        assert (sibling in schema["properties"]["coverage"]["properties"]) is (
            sibling in wire["coverage"]
        ), f"{tool}'s coverage block and its schema disagree about {sibling}"

    # Both keys have to have been seen on some tools and not others, or the
    # comparison above agreed with itself over an empty distinction.
    for key, tools in carried.items():
        assert tools, f"no tool carried {key}"
        assert tools != set(schemas), f"every tool carried {key}"


def test_the_checking_rejects_the_shapes_the_schemas_forbid(initialized_config: Config) -> None:
    """🔴 Validation that cannot fail is a test that cannot fail.

    Every other check here reports success, and none of them can tell "the
    payload matches" from "the checking is a no-op". So each rule the schemas
    lean on is broken on purpose, one key at a time, against a REAL answer --
    including the two that carry this chunk's argument: an unwindowed tool's
    schema must refuse a window, and a windowed tool's must refuse an answer
    without one.
    """
    _seed(initialized_config)
    schemas = {d["name"]: d["outputSchema"] for d in mcp._tool_definitions()}
    windowed = _call(initialized_config, "query_transactions")["structuredContent"]
    unwindowed = _call(initialized_config, "list_accounts")["structuredContent"]
    assert _violations(windowed, schemas["query_transactions"]) == []
    assert _violations(unwindowed, schemas["list_accounts"]) == []

    mutants: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
        (
            "a windowed answer with its window dropped",
            {key: value for key, value in windowed.items() if key != "effective_window"},
            schemas["query_transactions"],
        ),
        (
            "an unwindowed tool answering with a window",
            {**unwindowed, "effective_window": windowed["effective_window"]},
            schemas["list_accounts"],
        ),
        (
            "an uncapped tool answering with a truncation block",
            {**unwindowed, "truncation": windowed["truncation"]},
            schemas["list_accounts"],
        ),
        (
            "an envelope key nobody published",
            {**unwindowed, "spending_limit": 100},
            schemas["list_accounts"],
        ),
        (
            "a warning kind outside the vocabulary",
            {**unwindowed, "warnings": [{"kind": "probably_fine", "detail": "a made-up kind"}]},
            schemas["list_accounts"],
        ),
        (
            "a count where a flag belongs",
            {**windowed, "truncation": {**windowed["truncation"], "truncated": 1}},
            schemas["query_transactions"],
        ),
        (
            "a row missing one of its fields",
            {
                **unwindowed,
                "rows": [
                    {k: v for k, v in unwindowed["rows"][0].items() if k != "current_minor_units"}
                ],
            },
            schemas["list_accounts"],
        ),
        (
            "a coverage bound that came back as a number",
            {**unwindowed, "coverage": {**unwindowed["coverage"], "transactions": "seven"}},
            schemas["list_accounts"],
        ),
    ]

    for label, payload, schema in mutants:
        assert _violations(payload, schema), f"{label} was accepted"


# --------------------------------------------------------------------------
# The reference surface — addressable content, read by URI rather than by turn
# --------------------------------------------------------------------------


def _list_resources(config: Config) -> list[dict[str, Any]]:
    replies = _converse(
        config,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "resources/list", "params": {}},
        ],
    )
    resources: list[dict[str, Any]] = replies[1]["result"]["resources"]
    return resources


def _read_resource(config: Config, uri: str) -> dict[str, Any]:
    replies = _converse(
        config,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": uri}},
        ],
    )
    reply: dict[str, Any] = replies[1]
    return reply


def test_everything_the_server_lists_is_something_it_can_read(
    initialized_config: Config,
) -> None:
    """🔴 A listing is a promise, and `resources/read` is where it is kept.

    Two tables — one to list from and one to read from — is how a client comes
    to offer the operator a document that then fails to open. Driven off the
    listing itself rather than off URIs written here, because a URI written here
    would prove only that this test and the server agree about one document.
    """
    listed = _list_resources(initialized_config)

    assert listed, "the capability is declared and nothing is served"
    for entry in listed:
        reply = _read_resource(initialized_config, entry["uri"])
        contents = reply["result"]["contents"]

        assert len(contents) == 1, entry["uri"]
        assert contents[0]["uri"] == entry["uri"]
        assert contents[0]["mimeType"] == entry["mimeType"]
        assert contents[0]["text"].strip(), f"{entry['uri']} is served empty"


def test_the_listing_says_what_it_would_cost_a_host_to_read_it(
    initialized_config: Config,
) -> None:
    """`size` is the field a host reads to estimate context before it fetches.

    Which is the whole argument for serving this material as resources rather
    than as prose in the handshake, so a wrong number here undoes the reason it
    is here at all. Measured in bytes of the text, as the type specifies.
    """
    for entry in _list_resources(initialized_config):
        text = _read_resource(initialized_config, entry["uri"])["result"]["contents"][0]["text"]

        assert entry["size"] == len(text.encode("utf-8")), entry["uri"]
        assert entry["name"] and entry["title"] and entry["description"], entry["uri"]


def test_an_unknown_resource_uri_is_refused_with_the_route_to_a_real_one(
    initialized_config: Config,
) -> None:
    """Refused the way an unknown tool is: a sentence the caller can act on.

    🔴 Not a stack-shaped string and not an internal identifier — the same rule
    `api-contract.md` § Error Model puts on every other refusal. The reply names
    what is actually served, so the caller's next call can be the right one.
    """
    reply = _read_resource(initialized_config, "bankmachine://reference/nothing-here")

    assert "result" not in reply, "an unknown URI was answered with a document"
    message = reply["error"]["message"]
    assert reply["error"]["code"] == mcp._INVALID_PARAMS
    assert "bankmachine://reference/nothing-here" in message
    for served in mcp_resources.documents(mcp._tool_definitions()):
        assert served.uri in message, "the refusal does not say what the caller could read"
    assert "Traceback" not in message and "Error" not in message


def test_a_read_that_names_no_uri_is_refused_rather_than_guessed_at(
    initialized_config: Config,
) -> None:
    """Serving the first document to a caller who named none answers a question nobody asked."""
    replies = _converse(
        initialized_config,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {}},
        ],
    )

    assert replies[1]["error"]["code"] == mcp._INVALID_PARAMS


def test_the_resource_surface_answers_against_a_missing_datastore(config: Config) -> None:
    """🔴 AC-ARCH.3 reaches this surface too, and for the same reason.

    Every tool answers when the datastore is missing rather than failing, so the
    operator can ask why instead of watching their tool vanish. A resource that
    raised on the same connection would be a fresh way to lose the session — and
    the connection where the store is unreadable is exactly the one where an
    agent most needs to be told what a `partial` warning means.
    """
    assert not config.datastore_path.exists()

    listed = _list_resources(config)

    assert listed
    for entry in listed:
        contents = _read_resource(config, entry["uri"])["result"]["contents"]
        assert contents[0]["text"].strip(), entry["uri"]


def test_a_reference_that_cannot_be_assembled_reports_rather_than_ending_the_session(
    initialized_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 The same boundary argument `tools/call` makes, on the surface next to it.

    An exception escaping the handler ends the read loop, and the operator sees
    their tool disappear mid-session rather than fail. A resource has no
    `isError` channel to report on — `ReadResourceResult` carries only
    `contents` — so the refusal is a protocol error, and the session goes on.
    """

    def boom(_definitions: list[dict[str, Any]]) -> list[mcp_resources.Document]:
        raise RuntimeError("SELECT * FROM transactions WHERE account_id = 7")

    monkeypatch.setattr(mcp_resources, "documents", boom)

    replies = _converse(
        initialized_config,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "resources/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}},
        ],
    )

    assert replies[1]["error"]["code"] == mcp._INTERNAL_ERROR
    message = replies[1]["error"]["message"]
    assert "SELECT" not in message, "the failure carried the operator's own data to the client"
    assert "RuntimeError" not in message
    assert replies[2]["id"] == 3, "the session ended, which is the tool disappearing mid-session"


def test_resource_templates_are_answered_rather_than_refused(
    initialized_config: Config,
) -> None:
    """Declaring `resources` is what invites this call, so it gets an answer.

    Every document here sits at a fixed URI, so the honest answer is that there
    are no templates — which is a different thing from the method not existing.
    """
    replies = _converse(
        initialized_config,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "resources/templates/list", "params": {}},
        ],
    )

    assert replies[1]["result"] == {"resourceTemplates": []}


def test_the_instructions_name_every_resource_the_server_serves(
    initialized_config: Config,
) -> None:
    """🔴 A document nobody is told about is one nobody reads.

    The handshake text is the one thing a consuming agent reads before it calls
    anything, and the reference documents exist so that text can stay short —
    which only works if what is left points at where the rest went. Derived from
    the registry, so trimming the prose cannot quietly orphan a document.
    """
    instructions = mcp._instructions(initialized_config)

    missing = sorted(
        document.uri
        for document in mcp_resources.documents(mcp._tool_definitions())
        if document.uri not in instructions
    )

    assert not missing, (
        f"{missing} are served and the instructions never mention them; an agent that reads "
        f"only the handshake never learns they exist"
    )


def _wire_paths(value: Any, prefix: tuple[str, ...] = ()) -> set[str]:
    """Every key a payload actually carries, as a consumer reads it off the payload.

    🔴 One level is not enough: `truncation.matching` is invisible to a scan of
    the top level, and the blocks are where the numbers a consumer sums live.
    `rows` is left alone because its shape is per tool and published per tool.
    """
    paths: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            path = (*prefix, key)
            paths.add(mcp_resources._label(path))
            if path != ("rows",):
                paths |= _wire_paths(nested, path)
    elif isinstance(value, list):
        for item in value:
            paths |= _wire_paths(item, (*prefix, "[]"))
    return paths


def test_the_envelope_reference_names_every_field_the_answers_actually_carry(
    initialized_config: Config,
) -> None:
    """🔴 Read off real payloads, over the union of every tool, never one sample.

    The reference is derived from the published `outputSchema`, and the schema is
    held to the payload elsewhere in this file — so this is the third side of
    that triangle, and the one that fails if either of the other two is quietly
    describing something the wire does not send.

    A tool carrying neither `effective_window` nor `truncation` cannot
    discriminate a rule about them, which is why the union is taken rather than
    one answer sampled.
    """
    _seed(initialized_config, degraded=True)
    envelope: set[str] = set()
    for definition in mcp._tool_definitions():
        wire = _call(initialized_config, definition["name"])["structuredContent"]
        envelope |= _wire_paths(wire)

    assert {
        "effective_window.effective.since",
        "truncation.matching",
        "coverage.transactions_in_effective_window",
        "warnings[].institution",
    } <= envelope, "the union lost the keys this guard exists for, so it is back to sampling"

    text = next(
        document.text
        for document in mcp_resources.documents(mcp._tool_definitions())
        if document.uri == mcp_resources.ENVELOPE_URI
    )
    missing = sorted(path for path in envelope if f"`{path}`" not in text)

    assert not missing, (
        f"the wire carries {missing} and the envelope reference never names them; an agent "
        f"sent there to learn the envelope is told the field does not exist"
    )


def test_by_position_params_are_refused_rather_than_ending_the_session(
    initialized_config: Config,
) -> None:
    """🔴 JSON-RPC 2.0 permits `params` as an ARRAY, and every branch here reads an object.

    An array is a conformant client's request, not a malformed one — MCP simply
    never sends it. Read as an object it raises out of the handler and out of
    `serve()`, which is the operator's tool disappearing mid-session rather than
    answering. Refused, and the session goes on.
    """
    replies = _converse(
        initialized_config,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": ["a-uri"]},
            {"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}},
        ],
    )

    assert replies[1]["error"]["code"] == -32600
    assert replies[2]["id"] == 3, "the session ended, which is the tool disappearing mid-session"


def _seed_second_connection(config: Config, *, degraded: bool) -> None:
    """A second institution and connection, so two warnings can be told apart.

    🔴 #24's defect is not that a warning lacks a field — it is that with one
    connection the field can never be shown to DO anything. A single-connection
    fixture attributes every warning correctly by having only one answer
    available, which is the shape `learnings.md` names: a setup that cannot
    trigger the thing it tests passes forever.
    """
    now = now_utc()
    with writer_connection(config) as conn:
        institution_pk = conn.execute(
            institutions.insert().values(
                source_institution_id="ins_222222",
                name="Second Wombat Credit Union",
                first_seen_at=now,
                last_seen_at=now,
            )
        ).inserted_primary_key
        assert institution_pk is not None
        conn.execute(
            connections.insert().values(
                institution_id=int(institution_pk[0]),
                source_connection_id="item-second",
                credential_ref="connection:sandbox:item-second",
                capabilities="[]",
                requested_history_days=730,
                granted_history_days=730,
                status="degraded" if degraded else "active",
                last_success_at=None if degraded else now,
                last_error_code="AGGREGATOR_UNREACHABLE" if degraded else None,
                last_error_at=now if degraded else None,
                enrolled_at=now,
                created_at=now,
                updated_at=now,
            )
        )


def test_a_warning_names_which_of_two_connections_it_describes(
    initialized_config: Config,
) -> None:
    """🔴 #24, and the reason it could not be closed by the field alone.

    The first connection is granted 90 of 730 requested days, so it warns
    `gapped`; the second is granted its full window and fails instead, so it
    warns `degraded`. A consumer facing five institutions has to answer "which
    one is the gap in" — with one connection that question has a right answer by
    default, which is why the field shipped unproven.

    The assertion is that the two warnings are ATTRIBUTED, not merely that each
    carries a key: same kind or not, they must name different connections, and
    the names must be the ones that own the conditions.
    """
    _seed(initialized_config)
    _seed_second_connection(initialized_config, degraded=True)

    warnings = _call(initialized_config, "list_accounts")["structuredContent"]["warnings"]
    # Grouped rather than keyed: one connection can be several things at once —
    # a connection that has never synced AND is failing is truthfully both — and
    # a dict keyed on institution would silently keep whichever came last.
    attributed: dict[str, list[dict[str, Any]]] = {}
    for warning in warnings:
        if "institution" in warning:
            attributed.setdefault(warning["institution"], []).append(warning)
    assert set(attributed) == {"First Platypus Bank", "Second Wombat Credit Union"}, warnings

    # The shortfall belongs to the connection that was short, and the failure to
    # the connection that failed — swapped attribution would send a reader to
    # the wrong institution with a plausible-looking sentence.
    assert "gapped" in {w["kind"] for w in attributed["First Platypus Bank"]}
    assert "degraded" in {w["kind"] for w in attributed["Second Wombat Credit Union"]}
    assert "gapped" not in {w["kind"] for w in attributed["Second Wombat Credit Union"]}
    assert {w["connection_id"] for w in attributed["First Platypus Bank"]} == {1}
    assert {w["connection_id"] for w in attributed["Second Wombat Credit Union"]} == {2}
    # 🔴 And no detail is byte-identical across the two, which is the failure
    # round 3 named: a warning repeated verbatim trains a consumer to ignore it.
    first = {w["detail"] for w in attributed["First Platypus Bank"]}
    second = {w["detail"] for w in attributed["Second Wombat Credit Union"]}
    assert not (first & second), "a detail reads the same for two institutions"


def test_two_connections_in_the_same_state_are_still_told_apart(
    initialized_config: Config,
) -> None:
    """The harder half: same kind, same condition, two institutions.

    Different kinds could be distinguished by kind alone, so a guard that only
    ever saw `gapped` beside `degraded` would pass while attribution was broken.
    Two connections failing the same way is where the identifying fields are the
    only thing that separates them.
    """
    _seed(initialized_config, degraded=True)
    _seed_second_connection(initialized_config, degraded=True)

    warnings = _call(initialized_config, "list_accounts")["structuredContent"]["warnings"]
    degraded = [w for w in warnings if w["kind"] == "degraded"]
    assert len(degraded) == 2, warnings
    assert {w["institution"] for w in degraded} == {
        "First Platypus Bank",
        "Second Wombat Credit Union",
    }
    assert {w["connection_id"] for w in degraded} == {1, 2}
