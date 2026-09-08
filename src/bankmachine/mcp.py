"""The MCP server: JSON-RPC over stdio, read-only, no SDK.

🔴 **Read-only, and structurally so.** `api-contract.md` § Direction: *the MCP
surface is read-only; there are no mutation tools, and adding one is not a
decision this norm leaves open.* Every tool here goes through `query.py`, which
opens `mode=ro` at the file — so the refusal lives in the file handle rather
than in a rule a future tool author has to remember. Vetting a comparable server
surfaced 19 mutation tools including `delete_transaction` with no undo, on a
datastore holding this class of data.

🔴 **Every answer carries its own caveats and its own environment.** The
consumer is an analyst agent that cannot see a caveat which is not in the
payload. `query.Answer` cannot be constructed without warnings, and the
environment leads the envelope — a flag selects sandbox or production, the
envelope is what confesses which one answered.

**Why there is no `mcp` dependency:** the official SDK resolves to 29 packages
including `uvicorn`, `starlette` and `httpx2` — an HTTP server and client stack —
against this product's five direct dependencies, and its ratified norm that the
aggregator's API is the only network destination. This server speaks stdio, uses
none of those transports, and the wire format below was read from the SDK's own
type definitions rather than recalled (`api-notes-plaid.md` §18).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Iterator
from typing import IO, Any

from bankmachine import query
from bankmachine.cli.exit_codes import EXIT_OK
from bankmachine.config import Config
from bankmachine.logging_setup import get_logger
from bankmachine.store.connection import DatastoreMissingError, inspect

logger = get_logger("mcp")

#: The newest protocol version this server has been written against, read from
#: `mcp.types.LATEST_PROTOCOL_VERSION` at 2.2.0 rather than remembered.
LATEST_PROTOCOL_VERSION = "2026-07-28"

#: What to answer a client that asks for a version this server does not know.
#: The SDK's own `DEFAULT_NEGOTIATED_VERSION`, and the conservative choice: a
#: client that speaks something newer can still speak this.
FALLBACK_PROTOCOL_VERSION = "2025-03-26"

#: Versions this server will echo back verbatim when a client asks for one.
#: 🔴 The CLIENT's version is honoured when recognized rather than the server's
#: newest being asserted, because the client is the half that cannot adapt.
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = (
    LATEST_PROTOCOL_VERSION,
    "2025-06-18",
    FALLBACK_PROTOCOL_VERSION,
)

_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INTERNAL_ERROR = -32603


def _tool_definitions() -> list[dict[str, Any]]:
    """The tool surface. 🔴 Every one of them reads; none of them writes."""
    return [
        {
            "name": "list_accounts",
            "title": "List accounts",
            "description": (
                "Every account across every enrolled institution, with its most recent "
                "recorded balance. Amounts are INTEGER MINOR UNITS (cents for USD) and the "
                "field name says so. Signed from the account holder's point of view: a "
                "positive balance is value held, a negative one is value owed, so a credit "
                "card balance is negative."
            ),
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "list_transactions",
            "title": "List transactions",
            "description": (
                "Transactions in a date window, newest first. Amounts are INTEGER MINOR "
                "UNITS and signed from the account holder's point of view: money leaving is "
                "negative, money arriving is positive. Removed transactions are excluded."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "since": {"type": "string", "description": "inclusive start, YYYY-MM-DD"},
                    "until": {"type": "string", "description": "inclusive end, YYYY-MM-DD"},
                    "account_id": {"type": "integer"},
                    "limit": {"type": "integer", "default": 100},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "spending_by_category",
            "title": "Spending by category",
            "description": (
                "Total outflow per category in a date window. Sums only money leaving, "
                "reported as positive magnitudes in INTEGER MINOR UNITS -- refunds and "
                "income are excluded, because 'spending' is a question about outflow."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "since": {"type": "string", "description": "inclusive start, YYYY-MM-DD"},
                    "until": {"type": "string", "description": "inclusive end, YYYY-MM-DD"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "pipeline_health",
            "title": "Pipeline health",
            "description": (
                "Every connection, when it last synced, and what is wrong with it. 🔴 Call "
                "this before trusting a total that looks surprising: a granted history "
                "window of null means NOT YET MEASURED, never 'no shortfall'."
            ),
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    ]


def _dispatch_tool(config: Config, name: str, arguments: dict[str, Any]) -> query.Answer:
    handlers: dict[str, Callable[..., query.Answer]] = {
        "list_accounts": lambda: query.list_accounts(config),
        "list_transactions": lambda: query.list_transactions(
            config,
            since=arguments.get("since"),
            until=arguments.get("until"),
            account_id=arguments.get("account_id"),
            limit=int(arguments.get("limit", 100)),
        ),
        "spending_by_category": lambda: query.spending_by_category(
            config, since=arguments.get("since"), until=arguments.get("until")
        ),
        "pipeline_health": lambda: query.pipeline_health(config),
    }
    handler = handlers.get(name)
    if handler is None:
        raise KeyError(name)
    return handler()


def _server_info(config: Config) -> dict[str, Any]:
    return {
        "name": "bankmachine",
        # 🔴 The environment is in the server's own identity as well as in every
        # answer. A client listing two configured servers should be able to tell
        # the sandbox one from the real one without calling a tool.
        "title": f"bankmachine ({config.environment})",
        "version": "0.1.0",
    }


def _instructions(config: Config) -> str:
    return (
        f"This server reads a local {config.environment} finance datastore. It is READ-ONLY "
        f"and never moves money.\n\n"
        f"Every response carries `environment`, `as_of`, `coverage` and `warnings`. 🔴 Read "
        f"`warnings` before drawing a conclusion: an answer can be perfectly well-formed and "
        f"still be computed over incomplete data. `stale` means a connection has not synced "
        f"recently; `degraded` means one is failing; `gapped` means the institution granted "
        f"less history than was asked for, so older data is ABSENT rather than zero; "
        f"`partial` means something is not yet known.\n\n"
        f"All amounts are integer minor units (cents for USD) and signed from the account "
        f"holder's point of view: negative is money out, positive is money in."
    )


def _handle(config: Config, message: dict[str, Any]) -> dict[str, Any] | None:
    """One JSON-RPC request. Returns None for a notification, which takes no reply."""
    method = message.get("method")
    message_id = message.get("id")
    params = message.get("params") or {}

    if message_id is None:
        # A notification. `notifications/initialized` is the expected one; any
        # other is ignored rather than answered, because replying to a
        # notification is a protocol error on this side.
        return None

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = (
            requested
            if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS
            else FALLBACK_PROTOCOL_VERSION
        )
        return _result(
            message_id,
            {
                "protocolVersion": version,
                # Only `tools`. Declaring a capability this server does not serve
                # would have the client offer the operator something that fails.
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": _server_info(config),
                "instructions": _instructions(config),
            },
        )

    if method == "tools/list":
        return _result(message_id, {"tools": _tool_definitions()})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return _error(message_id, _INVALID_REQUEST, "tools/call needs a name and arguments")
        try:
            answer = _dispatch_tool(config, name, arguments)
        except KeyError:
            return _error(message_id, _METHOD_NOT_FOUND, f"no tool named {name!r}")
        except Exception as exc:  # prawduct:allow prawduct/broad-except -- see below
            # 🔴 Broad, because this is the boundary between this product and a
            # client that must not be left hanging: an unhandled exception here
            # would close the pipe mid-session and the operator would see their
            # tool "disappear" rather than fail. Reported as a tool error on the
            # success channel, which is what `isError` is for, and logged in full.
            logger.exception("tool %s failed", name)
            return _result(
                message_id,
                {
                    "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                    "isError": True,
                },
            )
        wire = answer.to_wire()
        return _result(
            message_id,
            {
                # Both forms: `structuredContent` is what a client parses, and
                # `content` is what one that only renders text will show. Sending
                # only the first leaves older clients with an empty result.
                "content": [{"type": "text", "text": json.dumps(wire, indent=2)}],
                "structuredContent": wire,
                "isError": False,
            },
        )

    if method in ("ping",):
        return _result(message_id, {})

    return _error(message_id, _METHOD_NOT_FOUND, f"unsupported method {method!r}")


def _result(message_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _error(message_id: Any, code: int, detail: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": detail}}


def serve(config: Config, *, stdin: IO[str], stdout: IO[str]) -> int:
    """Read requests until the client closes the pipe.

    Line-delimited JSON, which is what the stdio transport is. `stdin`/`stdout`
    are arguments rather than the module globals so the whole loop is testable
    without a subprocess -- the handshake is the part most likely to be subtly
    wrong, and it should be exercised by something that runs on every commit.
    """
    for message in _read_messages(stdin, stdout):
        reply = _handle(config, message)
        if reply is not None:
            _write(stdout, reply)
    return EXIT_OK


def _read_messages(stdin: IO[str], stdout: IO[str]) -> Iterator[dict[str, Any]]:
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            # Answered rather than ignored: a client that sent something
            # unparseable is waiting, and silence would look like a hang.
            _write(stdout, _error(None, _PARSE_ERROR, f"could not parse a message: {exc}"))
            continue
        if not isinstance(message, dict):
            _write(stdout, _error(None, _INVALID_REQUEST, "a message must be an object"))
            continue
        yield message


def _write(stdout: IO[str], payload: dict[str, Any]) -> None:
    stdout.write(json.dumps(payload) + "\n")
    stdout.flush()


def add_arguments(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "mcp",
        help="run the read-only MCP server on stdio",
        description=(
            "Serves the datastore to an MCP client over stdin/stdout. Read-only: there are "
            "no mutation tools, and every answer carries the environment it came from."
        ),
    )
    parser.set_defaults(handler=cmd_mcp)


def cmd_mcp(config: Config, _args: argparse.Namespace) -> int:
    status = inspect(config)
    if not status.healthy:
        raise DatastoreMissingError(
            f"datastore at {status.path} is not ready ({status.problem or 'unknown problem'}); "
            f"run `bankmachine store init` before serving it"
        )
    logger.info("serving %s datastore over stdio", config.environment)
    return serve(config, stdin=sys.stdin, stdout=sys.stdout)
