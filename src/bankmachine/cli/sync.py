"""`bankmachine sync shell` -- an authenticated SQL prompt on the encrypted datastore.

Page encryption breaks every ad-hoc SQL tool: the pages are ciphertext, so
stock `sqlite3` on this file reports it as corrupt rather than as encrypted.
Without this command the operator has no way to look at their own data, which
is why AC-ARCH.6 puts the shell in build step 1 rather than beside the rest of
the CLI -- it is the debugging affordance every later step is built over.

This is also the product's only surface that runs operator-supplied SQL, so the
two read-role norms stop being theoretical here. Each is held as a *property*
rather than as a list of statements to look for, because a list of forbidden
statements is an enumeration wearing a predicate's clothes -- it looks correct
the day it is written and fails silently the first time something reaches the
mechanism from outside the list:

* **The refusal to write lives in the file handle.** The shell asks
  `store.connection` for a read-role handle and does nothing else about writes.
  That handle is `mode=ro` at the file, so `PRAGMA query_only = OFF` -- which
  this prompt is the exact surface that can type -- turns off a second layer
  and reaches a first one that SQL cannot address.
* **No snapshot survives an idle moment.** A shell left open overnight is open
  during the nightly sync, and a read-role handle takes no writer lock, so
  nothing else serialises the two. `_release_snapshot` ends any transaction the
  last statement left open *before* the prompt comes back, and it decides that
  by asking the connection whether a transaction is open rather than by reading
  what was typed: `BEGIN` and `SAVEPOINT` both open one, and the next thing
  that does would not have been in the list.

There is no writer shell. The plan leaves one optional ("if offered at all"),
and it is declined on three grounds: the product's headline commitment is
read-only; a writer shell would hold the exclusive advisory lock for the whole
session, so the overnight prompt above would block the nightly sync outright
rather than merely starve its checkpointer; and rows typed in by hand have no
raw response behind them, which is precisely what `store rebuild`'s content
digest exists to catch. Nothing in build step 1 needs one. If one is ever
added, it takes the lock through `store.connection`'s writer factory like any
other writer -- there is no other way to obtain a handle that can write.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from typing import Final, Protocol

from bankmachine.cli import sync_run
from bankmachine.config import Config
from bankmachine.logging_setup import redact
from bankmachine.store import connection

with contextlib.suppress(ImportError):  # readline is absent on some platforms
    # Imported for the side effect: it backs `input()` with line editing and
    # history, which is most of what makes the prompt usable by hand.
    import readline  # noqa: F401


class InputStream(Protocol):
    """What the shell needs from stdin, which is less than a file.

    Stated as a protocol so a test can hold the shell at its prompt with an
    object that blocks on demand -- the only way to observe the shell *between*
    statements, which is the state the snapshot norm is about.
    """

    def isatty(self) -> bool: ...
    def readline(self) -> str: ...


class OutputStream(Protocol):
    """What the shell needs from stdout, which is somewhere to write."""

    def write(self, text: str, /) -> int: ...


PROMPT: Final = "bankmachine> "
CONTINUATION: Final = "         ...> "

#: What a NULL renders as. Spelled out rather than left blank, so an empty
#: string and a NULL are not the same thing on screen -- the shell exists to
#: answer questions about data, and that is one of them.
NULL_DISPLAY: Final = "NULL"

_HELP: Final = """\
  .tables            list the tables in this datastore
  .schema [table]    show the DDL, for one table or all of them
  .help              this list
  .quit / .exit      leave the shell (Ctrl-D does too)

Anything else is SQL, terminated by a semicolon. Statements may span lines.
This handle is read-only at the file, so writes are refused whatever the
session PRAGMAs say. Access tokens and account numbers are redacted on the way
out (AC-10.3); account masks are left intact, since they are what the redaction
exists to preserve."""


def add_arguments(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    sync = subparsers.add_parser("sync", help="talk to the datastore's contents")
    commands = sync.add_subparsers(dest="sync_command", required=True)

    shell = commands.add_parser(
        "shell",
        help="open an authenticated SQL prompt on the datastore (AC-ARCH.6)",
        description=(
            "An authenticated SQL prompt on the encrypted datastore. Page encryption "
            "breaks ad-hoc SQL tooling -- stock sqlite3 reads this file as corrupt -- so "
            "this is how the datastore gets inspected. The handle is read-only at the "
            "file: writes are refused, and no PRAGMA typed at this prompt changes that. "
            "Output is redacted for access tokens and account numbers."
        ),
    )
    shell.set_defaults(handler=cmd_shell)

    sync_run.add_arguments(commands)


def cmd_shell(config: Config, _args: argparse.Namespace) -> int:
    return run_shell(config)


def run_shell(
    config: Config,
    *,
    stdin: InputStream | None = None,
    stdout: OutputStream | None = None,
) -> int:
    """Open the read-role handle and run the prompt over it until end of input.

    The streams are parameters so the prompt can be driven by something other
    than a terminal -- a piped script, and the tests that hold the shell at its
    prompt while a writer checkpoints underneath it.
    """
    in_stream = sys.stdin if stdin is None else stdin
    out_stream = sys.stdout if stdout is None else stdout
    interactive = in_stream.isatty()

    with connection.reader(config) as conn:
        _print_banner(config, out_stream)
        return _repl(conn, in_stream, out_stream, interactive=interactive)


def _print_banner(config: Config, out: OutputStream) -> None:
    print(f"bankmachine sync shell -- environment {config.environment.upper()}", file=out)
    print(f"datastore: {config.datastore_path}", file=out)
    print(
        "role:      read-only at the file; writes are refused and no PRAGMA changes that",
        file=out,
    )
    print("output:    access tokens and account numbers redacted (AC-10.3)", file=out)
    print("type .help for the command list, .quit to leave", file=out)


def _repl(
    conn: connection.Connection, stdin: InputStream, out: OutputStream, *, interactive: bool
) -> int:
    """Read a statement, run it, release whatever it held, repeat."""
    buffered = ""
    while True:
        try:
            line = _read_line(CONTINUATION if buffered else PROMPT, stdin, out, interactive)
        except EOFError:
            if interactive:
                print(file=out)
            if buffered.strip():
                print("input ended mid-statement; it was not run", file=out)
            return 0
        except KeyboardInterrupt:
            # Ctrl-C abandons the statement being typed. It does not end the
            # session: losing a session to a mistyped line means re-opening and
            # re-authenticating, which is the opposite of usable under pressure.
            print("\n(statement abandoned)", file=out)
            buffered = ""
            continue

        if not buffered:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("."):
                if _meta_command(conn, stripped, out):
                    return 0
                continue

        buffered = f"{buffered}\n{line}" if buffered else line
        if not connection.statement_is_complete(buffered):
            continue
        _execute(conn, buffered, out)
        buffered = ""


def _read_line(prompt: str, stdin: InputStream, out: OutputStream, interactive: bool) -> str:
    """One line of input, from a terminal or from whatever is feeding the shell.

    `input()` is used only for a terminal, because that is what carries the
    readline editing and history an operator expects at a prompt. A piped or
    scripted stdin is read directly, so the shell can be driven by something
    that is not a tty without the prompt text landing in the middle of it.
    """
    if interactive:
        return input(prompt)
    print(prompt, end="", file=out)
    line = stdin.readline()
    if not line:
        print(file=out)
        raise EOFError
    # Echo what was read. A terminal echoes typed input on its own; a pipe does
    # not, and a transcript that shows results without the statements that
    # produced them is not a record of a session. The operator-verification
    # entry for this command is exactly such a transcript.
    #
    # The echo is redacted like everything else the shell writes. It is output
    # on the stream AC-10.3 governs, and a transcript is the most likely thing
    # here to be committed or pasted into a bug report -- a token typed into a
    # query is still a token once it has been written down.
    typed = line.rstrip("\n")
    print(redact(typed), file=out)
    return typed


def _meta_command(conn: connection.Connection, line: str, out: OutputStream) -> bool:
    """Run a dot-command. Returns whether the shell should exit."""
    parts = line.split()
    name = parts[0]
    if name in (".quit", ".exit"):
        return True
    if name == ".help":
        print(_HELP, file=out)
    elif name == ".tables":
        _execute(
            conn,
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name;",
            out,
        )
    elif name == ".schema":
        _print_schema(conn, parts[1] if len(parts) > 1 else None, out)
    else:
        print(f"unknown command {name} -- .help lists them", file=out)
    return False


def _print_schema(conn: connection.Connection, table: str | None, out: OutputStream) -> None:
    """The stored DDL, printed as DDL rather than squeezed into a table cell.

    Schema text is **not** a redaction surface, and that is a property rather
    than an exemption: everything in `sqlite_master` here was authored by this
    repo's migrations, and AC-6.6 -- enforced by
    `tests/preferences/test_no_provider_identity.py` -- is that no institution,
    account or product identity is encoded in the schema. There is nothing in a
    CREATE statement for AC-10.3 to protect.

    Running the value rule over it destroys it instead. `_OPAQUE` blanks any
    32-plus character run, and this schema's identifiers are longer than that:
    `source_investment_transaction_id` is exactly 32, so the column name comes
    out as `[REDACTED]`, and index names like
    `connections_one_live_per_institution` go the same way. Eight lines of the
    real schema were unreadable before this split.
    """
    try:
        if table is None:
            rows = conn.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
            ).fetchall()
        else:
            # `tbl_name`, not `name`: an index has its own name, and the
            # indexes are half of what makes a table's shape legible -- the
            # partial unique index on `connections` is where AC-1.4 lives.
            rows = conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE sql IS NOT NULL AND tbl_name = ? ORDER BY name",
                (table,),
            ).fetchall()
    except connection.DriverError as exc:
        print(f"error: {redact(str(exc))}", file=out)
        return
    finally:
        _release_snapshot(conn, out)

    if not rows:
        print(f"no such table: {table}" if table else "(this datastore has no schema)", file=out)
        return
    for (sql,) in rows:
        # `sqlite_master` stores the statement as written, indentation and
        # trailing newline included, so the terminator needs the strip to land
        # on the last line rather than a line of its own.
        print(f"{sql.strip()};", file=out)


def _execute(conn: connection.Connection, statement: str, out: OutputStream) -> None:
    """Run one statement and render whatever it produced.

    A failure prints a sentence and the prompt comes back. The shell is the
    thing an operator reaches for when something is already wrong, so a typo
    must not cost them the session -- but the error is always shown, because
    silence is the one outcome this project does not allow.
    """
    try:
        cursor = conn.execute(statement)
        if cursor.description is None:
            # Not the same thing as a result set that came back empty, and the
            # difference is the whole answer when a PRAGMA is what was typed.
            print("(no result set)", file=out)
        else:
            _print_table([str(column[0]) for column in cursor.description], cursor.fetchall(), out)
    except connection.DriverError as exc:
        # The message is redacted too: SQLite quotes the offending value back in
        # several of its errors, and a mistyped token is still a token.
        print(f"error: {redact(str(exc))}", file=out)
    finally:
        _release_snapshot(conn, out)


def _release_snapshot(conn: connection.Connection, out: OutputStream) -> None:
    """End any transaction the last statement left open, before the prompt returns.

    This is the norm this command exists to get right. A read snapshot that
    outlives the statement that opened it pins WAL frames, and a shell sitting
    at its prompt overnight is sitting there during the nightly sync -- with no
    writer lock between them, since this handle never takes one. The measured
    cost of getting it wrong is a checkpoint that moves 0 frames of 93 instead
    of all of them.

    What is asked is whether the connection has a transaction open, not what the
    operator typed: `BEGIN` opens one and so does `SAVEPOINT`, and a list of the
    statements that do is exactly the kind of enumeration that goes quietly
    stale. Rolling back is the whole recovery -- this handle cannot write, so a
    transaction on it has nothing in it worth keeping.
    """
    if not conn.in_transaction:
        return
    conn.execute("ROLLBACK")
    print(
        "note: that statement left a transaction open; it was rolled back so the prompt "
        "holds no snapshot. Nothing was lost -- this handle cannot write.",
        file=out,
    )


def _print_table(columns: list[str], rows: list[tuple[object, ...]], out: OutputStream) -> None:
    if not rows:
        print("(no rows)", file=out)
        return
    cells = [[_render(value) for value in row] for row in rows]
    widths = [len(column) for column in columns]
    for row in cells:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    print(_row(columns, widths), file=out)
    print(_row(["-" * width for width in widths], widths), file=out)
    for row in cells:
        print(_row(row, widths), file=out)
    print(f"({len(rows)} row{'' if len(rows) == 1 else 's'})", file=out)


def _row(cells: list[str], widths: list[int]) -> str:
    return "  ".join(cell.ljust(width) for cell, width in zip(cells, widths, strict=True)).rstrip()


def _render(value: object) -> str:
    """One cell, as text an operator can read, with AC-10.3 applied.

    Redaction runs over text values and not over numbers, and the reason is a
    property of this schema rather than a guess: money is an INTEGER of minor
    units here, and an account number is not an arithmetic quantity, so it is
    TEXT -- `accounts.mask` is. Redacting integers would blank a six-figure
    balance, which is the number the operator most often opened the shell to
    read, while protecting nothing that an account number is actually stored
    as. A four-digit mask passes through, which is what AC-10.3 asks for.

    A blob is summarised rather than printed. `raw_responses.body_gzip` is the
    one that comes up, and a terminal full of gzip is not a debugging
    affordance.

    Row values keep the full rule, bare-length matching included, and that is a
    deliberate choice with a real cost: `raw_responses.body_sha256` is 64 hex
    characters and comes out `[REDACTED]`. Dropping bare-length matching would
    read better and would let an unlabelled token through, and the archive's
    "no credential is persisted verbatim" clause is a recorded decision rather
    than a mechanism yet -- so over-redaction is the direction to be wrong in
    here. Correlating a row with its response does not need the digest: the
    integer `raw_response_id` foreign key is the join, and it is not redacted.
    Schema text is the surface where this rule is wrong, and `_print_schema`
    is where that is handled.
    """
    if value is None:
        return NULL_DISPLAY
    if isinstance(value, bytes):
        return f"<blob, {len(value)} bytes>"
    if isinstance(value, str):
        return redact(value)
    return str(value)
