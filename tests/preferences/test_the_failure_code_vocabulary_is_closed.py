"""The codes this product records in `last_error_code` are a closed, published set.

🔴 `get_pipeline_health` publishes `last_error_code`, and a consumer reads it to
decide what to tell the operator. A code invented at one call site, or a class
name leaking back in, reaches that consumer as a value nothing documents -- and
unlike a warning kind there is no schema enum to refuse it, so nothing fails.
These checks stand in for the constraint the column's `TEXT` type cannot express.

Four properties, each driven from the thing it protects rather than from a list
kept beside it:

- the published table in `api-contract.md` IS the set the code resolves to;
- the set is disjoint from every aggregator code this build knows, so a reader
  can tell whose code they are looking at;
- every failure type defined under `src/bankmachine` resolves to a member, found
  by walking the hierarchy rather than by naming the types someone remembered;
- every place `sync_run.py` records a code asks `failure_code` for it.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
import re
from pathlib import Path

import bankmachine
from bankmachine.cli.failure_codes import LOCAL_FAILURE_CODES, local_failure_code
from bankmachine.connector import ConnectorError
from bankmachine.connector.plaid.errors import (
    CODE_TO_ERROR,
    KNOWN_UNCLASSIFIED_CODES,
    TYPE_TO_ERROR,
)
from bankmachine.secrets import SecretsError
from bankmachine.store.connection import StoreError

REPO = Path(__file__).resolve().parents[2]
CONTRACT = REPO / ".prawduct" / "artifacts" / "api-contract.md"
SYNC_RUN = REPO / "src" / "bankmachine" / "cli" / "sync_run.py"

_TABLE_HEADING = "**Codes this product records in `last_error_code`**"
_ROW = re.compile(r"^\| `([A-Z][A-Z_]*)` \|")


def _published_codes() -> set[str]:
    lines = CONTRACT.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(_TABLE_HEADING))
    codes: set[str] = set()
    in_table = False
    for line in lines[start + 1 :]:
        if line.startswith("|"):
            in_table = True
            match = _ROW.match(line)
            if match:
                codes.add(match.group(1))
        elif in_table:
            break
    return codes


def _every_failure_type() -> set[type[Exception]]:
    """Every subclass of the three failure bases defined in this product's source.

    Imports every module first, because `__subclasses__` only knows classes whose
    module has run -- a type in a module no test happened to import would
    otherwise be invisible, which is the one this guard most needs to see.
    """
    for module in pkgutil.walk_packages(bankmachine.__path__, prefix="bankmachine."):
        if not module.name.endswith("__main__"):
            importlib.import_module(module.name)
    found: set[type[Exception]] = set()
    pending: list[type[Exception]] = [ConnectorError, StoreError, SecretsError]
    while pending:
        kind = pending.pop()
        if kind in found or not kind.__module__.startswith("bankmachine"):
            continue
        found.add(kind)
        pending.extend(kind.__subclasses__())
    return found


def test_the_published_table_is_the_set_the_code_records() -> None:
    published = _published_codes()
    assert published, f"no code table found under {_TABLE_HEADING} in {CONTRACT.name}"
    assert published == set(LOCAL_FAILURE_CODES), (
        f"published but never recorded: {sorted(published - LOCAL_FAILURE_CODES)}; "
        f"recorded but not published: {sorted(LOCAL_FAILURE_CODES - published)}"
    )


def test_no_local_code_could_be_mistaken_for_the_aggregators() -> None:
    aggregator = set(CODE_TO_ERROR) | set(TYPE_TO_ERROR) | set(KNOWN_UNCLASSIFIED_CODES)
    assert not LOCAL_FAILURE_CODES & aggregator, sorted(LOCAL_FAILURE_CODES & aggregator)


def test_every_failure_type_in_the_source_resolves_to_a_published_code() -> None:
    types = _every_failure_type()
    # Positive control: the walk has to reach types that live in modules the
    # imports above did not name, or it proves nothing about the ones nobody named.
    names = {kind.__name__ for kind in types}
    assert {
        "UnreadableWindowError",
        "AnotherWriterRunningError",
        "UndenominableAmountError",
    } <= names

    unresolved = {
        kind.__qualname__: local_failure_code(kind)
        for kind in types
        if local_failure_code(kind) not in LOCAL_FAILURE_CODES
    }
    assert not unresolved, unresolved


def test_every_recorded_code_in_sync_run_comes_from_failure_code() -> None:
    """🔴 Driven over the source, because the defect was a call site building its
    own code -- `type(exc).__name__` in three places and a literal in a fourth --
    and a test of the resolver alone passes while a site ignores it."""
    tree = ast.parse(SYNC_RUN.read_text(encoding="utf-8"))
    recorded: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_degrade":
            recorded.append(node.args[2])
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Attribute) and target.attr == "investments_error_code"
            for target in node.targets
        ):
            recorded.append(node.value)
    assert len(recorded) >= 4, "the scan no longer finds the recording sites it was written for"
    offenders = [
        f"line {expr.lineno}: {ast.unparse(expr)}"
        for expr in recorded
        if not (isinstance(expr, ast.Call) and getattr(expr.func, "id", None) == "failure_code")
    ]
    assert not offenders, offenders
