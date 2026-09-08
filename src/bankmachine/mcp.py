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
from datetime import date
from typing import IO, Any

from bankmachine import query
from bankmachine.build_id import build_identity
from bankmachine.cli.exit_codes import EXIT_OK
from bankmachine.config import Config
from bankmachine.logging_setup import get_logger
from bankmachine.store.connection import inspect

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


#: What every windowed tool says about its own window. Written once and shared
#: because two tools describing one mechanism in two sentences is how the two
#: sentences stop agreeing -- and this text is the only place a caller is told
#: the field exists before they have seen one.
_WINDOW_NOTE = (
    "The window you ask for is CLAMPED to what the store can answer over, and the result "
    "says so: `effective_window` carries the window requested beside the window actually "
    "covered, and a `window_starts_before_coverage` or `window_extends_past_coverage` warning "
    "names the boundary crossed. Absent those warnings, the window you asked for is the "
    "window you got. Read it before treating an empty result as a zero -- outside coverage, "
    "data is ABSENT rather than zero."
)

#: 🔴 On `query_transactions` alone. `spending_summary` is an aggregate, fixed
#: unpaginated by `api-contract.md` and bounded by its grouping, so saying this
#: there would describe a cap it does not have.
_TRUNCATION_NOTE = (
    "This tool is CAPPED. `truncation` carries `matching` (how many rows the request "
    "selects), `returned` (how many came back) and `truncated`. 🔴 When `truncated` is true "
    "the rows are the NEWEST ones only, so summing or counting them describes what came "
    "back rather than the window you asked about -- a `rows_truncated` warning says by how "
    "much. To read the rest, pass the answer's `next_cursor` straight back as `cursor` with "
    "the SAME window and account, and keep going until `truncated` is false -- that is the "
    "only route that reaches every matching row. Narrowing the window or raising `limit` "
    "moves the cap; paging removes it."
)


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
            "name": "query_transactions",
            "title": "List transactions",
            "description": (
                "Transactions in a date window, newest first. Amounts are INTEGER MINOR "
                "UNITS and signed from the account holder's point of view: money leaving is "
                "negative, money arriving is positive. Removed transactions are excluded. "
                "🔴 `description` is the institution's own string and is authoritative; "
                "`merchant` is the AGGREGATOR'S guess at a merchant name, unvalidated and "
                "often absent or wrong -- it reads 'FUN' for a purchase whose description is "
                "'SparkFun'. Do not roll up or match on `merchant` without saying it may be "
                "wrong, and prefer `description` when the two disagree. "
                + _WINDOW_NOTE
                + " "
                + _TRUNCATION_NOTE
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "since": {"type": "string", "description": "inclusive start, YYYY-MM-DD"},
                    "until": {"type": "string", "description": "inclusive end, YYYY-MM-DD"},
                    "account_id": {"type": "integer", "minimum": 1},
                    "limit": {
                        "type": "integer",
                        "default": 100,
                        "minimum": 1,
                        "maximum": query.MAX_ROWS,
                        "description": (
                            "rows returned, at most "
                            f"{query.MAX_ROWS}; asking for more is refused, not trimmed"
                        ),
                    },
                    "cursor": {
                        "type": "string",
                        "description": (
                            "resume a paged walk: pass back the `next_cursor` from a "
                            "previous answer, unchanged, with the same window and account. "
                            "OPAQUE -- do not read it, build one, or edit one; a cursor "
                            "this server did not issue for this request is refused"
                        ),
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "spending_summary",
            "title": "Spending by category",
            "description": (
                "Total outflow per category in a date window. Sums only money leaving, "
                "reported as positive magnitudes in INTEGER MINOR UNITS -- refunds and "
                "income are excluded, because 'spending' is a question about outflow. "
                + _WINDOW_NOTE
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
            "name": "get_pipeline_health",
            "title": "Pipeline health",
            "description": (
                "Every connection, when it last synced, and what is wrong with it — including "
                "when there is no datastore at all. 🔴 Call "
                "this before trusting a total that looks surprising: a granted history "
                "window of null means NOT YET MEASURED, never 'no shortfall'."
            ),
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    ]


def _tool_names() -> frozenset[str]:
    """The advertised tool names, derived once from the one definition list."""
    return frozenset(definition["name"] for definition in _tool_definitions())


def _permitted_arguments(name: str) -> frozenset[str]:
    """The keys one tool advertises. Derived, never restated.

    `additionalProperties: False` is published on every tool, so a caller is
    entitled to be told when it sends a key that is not there. Read back off
    `_tool_definitions()` rather than listed again here, because a second list
    is one that stops matching the first.
    """
    for definition in _tool_definitions():
        if definition["name"] == name:
            schema: dict[str, Any] = definition["inputSchema"]
            properties: dict[str, Any] = schema.get("properties", {})
            return frozenset(properties)
    return frozenset()


class BadArgumentError(ValueError):
    """A tool argument the caller can correct, reported so that it can.

    Not a JSON-RPC error: the schema declares `since` a *string*, and
    "August 2024" is one -- what it violates is the YYYY-MM-DD form the
    description asks for, which no schema here states. So this is the tool
    reporting on its input, and it rides `isError` where a model will read it
    and try again, rather than a protocol code a client tends to surface as a
    hard failure.
    """


def _calendar_date(arguments: dict[str, object], field: str) -> date | None:
    """One JSON argument, narrowed to the type the query layer accepts.

    🔴 JSON has no date. Every date crossing this boundary arrives as text and
    must be parsed HERE -- the query layer binds it to a `CalendarDate` column
    that refuses anything else, and AC-6.4 keeps dates and instants apart on
    purpose, so a datetime string is refused rather than silently truncated.
    """
    raw = arguments.get(field)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise BadArgumentError(
            f"{field} must be a date as a YYYY-MM-DD string, got {type(raw).__name__}"
        )
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise BadArgumentError(
            f"{field} must be a calendar date in YYYY-MM-DD form, got {raw!r}"
        ) from None


def _whole_number(
    arguments: dict[str, object],
    field: str,
    default: int | None,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None:
    """Same narrowing for the integer arguments, and for the same reason.

    `int(...)` on whatever arrived would turn a caller's mistake into a
    `ValueError` from deep inside the dispatch table, which reaches the client
    as a stack-shaped string instead of a sentence naming the field.
    """
    raw = arguments.get(field)
    if raw is None:
        return default
    # bool is an int in Python, and `true` is a JSON value a caller can send.
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise BadArgumentError(f"{field} must be a whole number, got {type(raw).__name__}")
    if minimum is not None and raw < minimum:
        # 🔴 Refused, not clamped. A silent clamp answers a question nobody
        # asked: `limit: 0` served one row, which reads as a plausible complete
        # answer to a narrow question -- worse than an obviously wrong hundred.
        raise BadArgumentError(f"{field} must be at least {minimum}, got {raw}")
    if maximum is not None and raw > maximum:
        raise BadArgumentError(f"{field} must be at most {maximum}, got {raw}")
    return raw


def _window(arguments: dict[str, object]) -> tuple[date | None, date | None]:
    """Both bounds, refused together if they contradict each other.

    🔴 An `until` before its `since` selects nothing, and "nothing" is a
    believable answer to a spending question -- so a transposed pair, which is
    an ordinary slip, returns a confident zero rather than a complaint. There is
    no window it could mean, so there is nothing to guess at.
    """
    since = _calendar_date(arguments, "since")
    until = _calendar_date(arguments, "until")
    if since is not None and until is not None and until < since:
        raise BadArgumentError(
            f"until ({until.isoformat()}) is before since ({since.isoformat()}), "
            f"so the window selects nothing. Did the two get swapped?"
        )
    return since, until


def _cursor(
    arguments: dict[str, object],
    *,
    since: date | None,
    until: date | None,
    account_id: int | None,
) -> query.Cursor | None:
    """The `cursor` argument, narrowed to the position type the query layer accepts.

    🔴 Narrowed HERE, like every other argument, so no raw string reaches the
    query layer. The decoding and the fingerprint check live in `query` beside
    the encoder that produced them -- a decoder written one module from its
    encoder is the second description that stops matching the first -- and the
    refusal it raises rides the same boundary path `UnknownAccountError` does,
    because what a caller gets told is this boundary's to decide.
    """
    raw = arguments.get("cursor")
    if raw is not None and not isinstance(raw, str):
        raise BadArgumentError(
            f"cursor must be the `next_cursor` string from a previous answer, "
            f"got {type(raw).__name__}"
        )
    return query.parse_cursor(raw, since=since, until=until, account_id=account_id)


def _dispatch_tool(config: Config, name: str, arguments: dict[str, object]) -> query.Answer:
    """🔴 `arguments` is `dict[str, object]`, not `dict[str, Any]`, and that is load-bearing.

    Under `Any` every value here flows into the query layer unchallenged and
    mypy strict says nothing -- which is exactly how raw JSON strings reached a
    `CalendarDate` column and broke every windowed question the server could be
    asked. Typed as `object`, a value that has not been narrowed cannot be
    passed at all, so the checker refuses the mistake rather than a test having
    to notice it. Do not widen this back.
    """
    unknown = sorted(set(arguments) - _permitted_arguments(name))
    if unknown:
        # 🔴 Silently dropping one is the dangerous outcome, not a strict one:
        # a misspelled `since` returns the ALL-TIME aggregate, which is
        # indistinguishable from the window that was asked for.
        raise BadArgumentError(
            f"{name} has no argument {', '.join(repr(key) for key in unknown)}. It accepts: "
            f"{', '.join(sorted(_permitted_arguments(name))) or 'no arguments'}"
        )
    # Narrowed once, ahead of the handler table: referenced inside the lambdas
    # these would re-parse on every call, and a refusal would be raised twice.
    since, until = _window(arguments)
    limit = _whole_number(arguments, "limit", 100, minimum=1, maximum=query.MAX_ROWS)
    account_id = _whole_number(arguments, "account_id", None, minimum=1)
    # 🔴 After the window and the account, because a cursor is only meaningful
    # against the request it accompanies and this is the call that compares the
    # two. A cursor narrowed first would have nothing to be checked against.
    cursor = _cursor(arguments, since=since, until=until, account_id=account_id)
    handlers: dict[str, Callable[..., query.Answer]] = {
        "list_accounts": lambda: query.list_accounts(config),
        "query_transactions": lambda: query.list_transactions(
            config,
            since=since,
            until=until,
            account_id=account_id,
            limit=limit if limit is not None else 100,
            after=cursor,
        ),
        "spending_summary": lambda: query.spending_by_category(config, since=since, until=until),
        "get_pipeline_health": lambda: query.pipeline_health(config),
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
        # Read from package metadata rather than restated here: a literal
        # version is one that stops matching `pyproject.toml` the first time
        # either moves without the other.
        "version": build_identity().version,
        # 🔴 The handshake carries it too, so a client can show which build it
        # connected to before any tool is called. `null` means the build could
        # not be identified -- it is never guessed at.
        "commit": build_identity().commit,
        "dirty": build_identity().dirty,
    }


def _instructions(config: Config) -> str:
    return (
        f"This server reads a local {config.environment} finance datastore. It is READ-ONLY "
        f"and never moves money.\n\n"
        f"Every response carries `environment`, `as_of`, `build`, `coverage`, `warnings` and "
        f"`rows`. `build` says which code answered you -- this server is a subprocess launched "
        f"at connect time, so it runs whatever existed then, and `commit` is captured once at "
        f"start rather than re-read (a null `commit` means the build could not be identified, "
        f"and `dirty` is then null too, never false).\n\n"
        f"🔴 A WINDOWED tool also carries `effective_window` (the window you asked for beside "
        f"the one the data could answer over), and a CAPPED tool also carries `truncation` "
        f"(`matching`, `returned`, `truncated`). Absence of either key means that tool has no "
        f"window, or returns every row it finds. 🔴 **If `truncated` is true the rows are the "
        f"NEWEST ones only, so summing or counting them describes what came back rather than "
        f"the window you asked about.** A truncated answer also carries "
        f"a `next_cursor` inside `truncation`: pass it straight back as the tool's `cursor` "
        f"argument, "
        f"with the same window and account, to read the next page, and keep going until "
        f"`truncated` is false. The cursor is OPAQUE -- never build or edit one -- and it is "
        f"present when and only when there is more to read.\n\n"
        f"🔴 Read `warnings` before drawing a conclusion: an answer can be perfectly "
        f"well-formed and still be computed over incomplete data. Some warnings describe the "
        f"PIPELINE and ride every response: `stale` means a connection has not synced "
        f"recently; `degraded` means one is failing; `gapped` means the institution granted "
        f"less history than was asked for, so older data is ABSENT rather than zero; "
        f"`partial` means something is not yet known; `rule-applied` means an account rule "
        f"filtered rows out of an aggregate, so the total excludes them on purpose. The rest "
        f"describe THIS REQUEST and "
        f"appear only when it crosses the boundary they name, so their absence is information "
        f"too: `window_starts_before_coverage` and `window_extends_past_coverage` mean the "
        f"window you asked for reaches outside what the store holds; `rows_truncated` means "
        f"rows were left behind; `counted_during_change` means a write landed while the "
        f"answer was being assembled.\n\n"
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
        if name not in _tool_names():
            # Resolved BEFORE the call, so that a `KeyError` raised anywhere
            # BENEATH the query layer is not answered "no tool named
            # 'spending_summary'" -- which is a false statement about a tool
            # that exists, delivered as a protocol error nobody can act on.
            return _error(message_id, _METHOD_NOT_FOUND, f"no tool named {name!r}")
        try:
            answer = _dispatch_tool(config, name, arguments)
        except (
            BadArgumentError,
            query.UnknownAccountError,
            query.InvertedWindowError,
            query.MalformedCursorError,
        ) as exc:
            # Ahead of the broad catch. The message is the caller's to act on,
            # so it is rendered without the exception class name -- and it is
            # safe to send verbatim because this product wrote every word of it.
            #
            # `UnknownAccountError` rides the same path from the query layer:
            # only a datastore read can know an id names nothing, but what the
            # caller gets told is this boundary's to decide, and the answer is
            # the same one an unadvertised argument gets -- correct your call.
            logger.info("tool %s refused an argument: %s", name, exc)
            return _tool_error(message_id, "invalid_argument", str(exc))
        except Exception:  # prawduct:allow prawduct/broad-except -- see below
            # 🔴 Broad, because this is the boundary between this product and a
            # client that must not be left hanging: an unhandled exception here
            # would close the pipe mid-session and the operator would see their
            # tool "disappear" rather than fail. Reported as a tool error on the
            # success channel, which is what `isError` is for.
            #
            # 🔴 The exception NEVER crosses the boundary. `api-contract.md`
            # § Error Model: no stack traces and no internal identifiers. A
            # SQLAlchemy error stringifies to the failing SELECT and its bound
            # parameters -- which is the schema, and the operator's own money,
            # handed to whatever is reading. The detail goes to the log, where
            # redaction applies; the caller gets a code and a remedy.
            logger.exception("tool %s failed", name)
            return _tool_error(
                message_id,
                "internal_error",
                f"{name} could not be answered. The failure has been logged; "
                f"`bankmachine store status` reports whether the datastore is readable.",
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


def _tool_error(message_id: Any, code: str, remedy: str) -> dict[str, Any]:
    """A tool failure, as `api-contract.md` § Error Model specifies it.

    A stable code so a consumer can branch -- `invalid_argument` is worth
    retrying with a corrected call, `internal_error` is not -- and a remedy
    sentence for the human. Both forms, because a client that renders only text
    would otherwise show an empty failure.
    """
    return _result(
        message_id,
        {
            "content": [{"type": "text", "text": remedy}],
            "structuredContent": {"error": {"code": code, "message": remedy}},
            "isError": True,
        },
    )


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
    """🔴 Starts even when the datastore is empty or missing. AC-ARCH.3.

    An earlier version refused, which inverted the requirement — and used
    `inspect()` to do it, whose own docstring says it exists so the server can
    *report* that state. The reason the AC reads this way is that a client
    launches this as a subprocess: a server that exits on startup shows up as a
    tool that silently does not appear, and the operator has no way to ask why.
    A server that starts and answers `get_pipeline_health` with "there is no
    datastore" can be asked.

    So the unhealthy state is logged and carried into every answer as a warning
    rather than raised.
    """
    status = inspect(config)
    if not status.healthy:
        logger.warning(
            "serving %s with an unusable datastore at %s (%s); tools will report this rather "
            "than fail",
            config.environment,
            status.path,
            status.problem or "unknown problem",
        )
    else:
        logger.info("serving %s datastore over stdio", config.environment)
    return serve(config, stdin=sys.stdin, stdout=sys.stdout)
