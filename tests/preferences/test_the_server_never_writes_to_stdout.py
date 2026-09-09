"""Nothing on the MCP server's path addresses the process's own stdout.

stdout *is* the transport. `serve()` speaks line-delimited JSON over it, so a
stray byte does not garble a log line -- it corrupts the frame the client is
part-way through reading, and the client's failure mode is the one `cmd_mcp`
already exists to prevent: the tool disappears rather than fails, and the
operator has no error to read.

The word itself cannot be the rule, because two of the three cases are correct.
`cli/` commands print to stdout as their whole job, and `mcp.py` writes the
protocol to stdout deliberately. Two relationships separate the three, and
neither is a list of blessed files:

**What the server reaches.** The scanned set is the transitive import closure of
`bankmachine.mcp`, derived from the import statements themselves. A module the
server stops depending on leaves the scope on the commit that removes the
import, and one it starts depending on joins it the same way -- which is why a
`cli/` module the server takes a constant from is scanned while the command
modules that print are not, without either being named here.

**Who chose the destination.** `serve()` takes its `stdout` as an argument, so
the transport writes to a handle its caller injected and a test can hand it a
buffer instead. The process's own stream may be *handed* to something -- that is
the injection -- and may never be addressed: no `sys.stdout.write`, no name
bound to it, no `from sys import stdout`. Since binding it is refused, a name
inside the closure can only be holding the real stream because a caller passed
it in, which is what makes the transport's own write legal by construction
rather than by exemption. `print` is refused outright, `file=` kwarg or not,
because the function name is the smell rather than its destination.

stderr is held to the same rule for a smaller reason: diagnostics on the
server's path belong to the logger, which the operator can redirect and which
already knows what to redact, rather than to whichever stream a line reached
for. Handing the stream to a log handler is the same injection and stays legal.

Import time gets a second scan and needs no closure at all. Every module runs
its own body when something imports it, and the server is imported before it
reads its first frame, so a module-level write reaches the client's stream ahead
of the handshake no matter which module it hides in. The `cli/` prints are
untouched by that scan because they live inside command functions, which run
when the operator runs a command.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
SOURCE_ROOT = REPO_ROOT / "src"
PACKAGE = "bankmachine"
SERVER_MODULE = f"{PACKAGE}.mcp"

#: The attributes of `sys` that name the process's own streams. These are the
#: destinations nothing in the closure may address; a file handle, a socket or
#: an injected buffer is not one of them, so `.write` on its own is not the
#: thing being hunted.
PROCESS_STREAMS = frozenset({"stdout", "stderr"})


def _package_files() -> list[Path]:
    root = SOURCE_ROOT / PACKAGE
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _module_file(module: str) -> Path | None:
    """Where a dotted module name lives, module or package."""
    relative = Path(*module.split("."))
    module_file = SOURCE_ROOT / relative.with_suffix(".py")
    package_file = SOURCE_ROOT / relative / "__init__.py"
    for candidate in (module_file, package_file):
        if candidate.exists():
            return candidate
    return None


def _imported_modules(tree: ast.Module) -> set[str]:
    """Every module of this package a file names in an import statement.

    `from x.y import z` records both `x.y` and `x.y.z`, because the name may be
    a submodule and the dependency is real either way; the one that does not
    resolve to a file drops out when the closure looks for it.
    """
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return {m for m in modules if m.split(".")[0] == PACKAGE}


def _server_closure() -> dict[str, Path]:
    """The package modules `bankmachine.mcp` reaches, transitively."""
    found: dict[str, Path] = {}
    pending = [SERVER_MODULE]
    while pending:
        module = pending.pop()
        if module in found:
            continue
        path = _module_file(module)
        if path is None:
            continue
        found[module] = path
        pending.extend(_imported_modules(_parse(path)) - found.keys())
    return found


def _import_time_nodes(tree: ast.Module) -> Iterator[ast.AST]:
    """Every node whose code runs when the module is imported.

    A class body runs at import and a function body does not, so the walk
    descends through the former and stops at the `def`.
    """
    pending: list[ast.AST] = list(tree.body)
    while pending:
        node = pending.pop()
        yield node
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        pending.extend(ast.iter_child_nodes(node))


def _print_call(node: ast.AST) -> ast.Call | None:
    """The node as a `print` call, if that is what it is.

    A `file=` kwarg does not make it something else -- the function name is the
    smell, whatever destination the call names today.
    """
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
        return node
    return None


def _names_the_process_stream(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
        and node.attr in PROCESS_STREAMS
    )


def _offences(nodes: list[ast.AST]) -> list[tuple[int, str]]:
    """Writes to the process's own stdout, and the constructs that hide one.

    The exemption is *argument position*, evaluated over the same nodes: a
    stream handed to a call is being injected into something that took it as a
    parameter, and a stream reached through any other syntax is being addressed.
    """
    handed_over = {
        id(argument)
        for node in nodes
        if isinstance(node, ast.Call)
        for argument in (*node.args, *(keyword.value for keyword in node.keywords))
    }
    found: list[tuple[int, str]] = []
    for node in nodes:
        printing = _print_call(node)
        if printing is not None:
            found.append((printing.lineno, "print(...)"))
        elif (
            isinstance(node, ast.Attribute)
            and _names_the_process_stream(node)
            and id(node) not in handed_over
        ):
            found.append((node.lineno, f"sys.{node.attr} addressed rather than handed over"))
        elif isinstance(node, ast.ImportFrom) and node.module == "sys":
            aliased = sorted(a.name for a in node.names if a.name in PROCESS_STREAMS)
            if aliased:
                found.append((node.lineno, f"from sys import {', '.join(aliased)}"))
    return sorted(found)


def test_nothing_the_server_reaches_addresses_the_process_stdout() -> None:
    offenders: list[str] = []
    for _, path in sorted(_server_closure().items()):
        for lineno, what in _offences(list(ast.walk(_parse(path)))):
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {what}")

    assert not offenders, (
        "the MCP server's path writes to the process's own stdout, which is the "
        "transport; a write there corrupts the frame a client is reading:\n  "
        + "\n  ".join(offenders)
    )


def test_no_module_writes_to_a_process_stream_while_being_imported() -> None:
    """Import runs before the handshake, so import-time output leads the stream."""
    offenders: list[str] = []
    for path in _package_files():
        for lineno, what in _offences(list(_import_time_nodes(_parse(path)))):
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {what}")

    assert not offenders, (
        "a module writes to a process stream as it is imported, which happens "
        "before the server reads its first frame:\n  " + "\n  ".join(offenders)
    )


def test_the_check_catches_the_evasions_and_lets_an_injected_handle_through() -> None:
    """The positive control -- a check that has never fired is not known to fire.

    The second half is the load-bearing one. The transport's own write has to
    survive the same scan that rejects the four constructs above it, and survive
    it because of how the handle is addressed rather than where the code lives.
    """
    addressed = ast.parse(
        "import sys\n"
        "print('a')\n"
        "print('b', file=sys.stderr)\n"
        "sys.stdout.write('c')\n"
        "elsewhere = sys.stdout\n"
    )
    assert len(_offences(list(ast.walk(addressed)))) == 4

    aliased = ast.parse("from sys import stdout\nstdout.write('d')\n")
    assert _offences(list(ast.walk(aliased)))

    injected = ast.parse(
        "import sys\n"
        "def _write(stdout, payload):\n"
        "    stdout.write(payload)\n"
        "    stdout.flush()\n"
        "def cmd(config):\n"
        "    return serve(config, stdin=sys.stdin, stdout=sys.stdout)\n"
    )
    assert _offences(list(ast.walk(injected))) == []


def test_the_closure_holds_the_server_and_stops_short_of_the_printing_commands() -> None:
    """A scan over zero files, or over every file, passes forever either way.

    The second assertion is the one that proves the scope discriminates: some
    module in this package prints, and it is outside what the server reaches.
    Found rather than named, so the check survives the module being renamed and
    fails if the printing ever moves onto the server's path.
    """
    closure = _server_closure()
    assert SERVER_MODULE in closure
    assert len(closure) > 5

    printing = [p for p in _package_files() if _offences(list(ast.walk(_parse(p))))]
    assert printing, "nothing in the package prints, so the scope has nothing to tell apart"
    assert set(printing) - set(closure.values()), (
        "every module that prints is inside the server's closure, so this guard "
        "is no longer distinguishing the transport from the command line"
    )


def test_the_scan_reaches_the_transports_own_write() -> None:
    """The construct the exemption exists for has to be inside what is scanned.

    A scan that passes because it never looked at `mcp.py` is the failure this
    rules out, and a server that no longer writes at all means the transport has
    moved and this guard needs rereading rather than rerunning.
    """
    server = _server_closure()[SERVER_MODULE]
    writes = [
        node
        for node in ast.walk(_parse(server))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "write"
    ]
    assert writes, "the server writes nothing, so there is no transport left to protect"
