"""Nothing outside `store/` opens a connection, and nothing outside
`store/connection.py` constructs one.

That single rule is what makes the four architecture norms enforceable by a
structural test rather than by review. Both role norms are properties of *how a
handle was constructed*; if a second construction site exists, neither norm is
a guarantee any more -- it is a convention every future author has to remember,
which is the shape of failure this project has already had twice.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
SOURCE_ROOT = REPO_ROOT / "src" / "bankmachine"

CONNECTION_MODULE = SOURCE_ROOT / "store" / "connection.py"
ENGINE_MODULE = SOURCE_ROOT / "store" / "engine.py"

#: DBAPI modules that can open a database file.
DRIVER_MODULES = {"sqlcipher3", "sqlite3", "pysqlite3"}


def _source_files() -> list[Path]:
    return [p for p in SOURCE_ROOT.rglob("*.py") if "__pycache__" not in p.parts]


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def test_only_the_store_layer_imports_a_database_driver() -> None:
    offenders: list[str] = []
    for path in _source_files():
        if path.parent.name == "store" or path.parent.parent.name == "store":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        leaked = _imported_modules(tree) & DRIVER_MODULES
        if leaked:
            offenders.append(f"{path.relative_to(REPO_ROOT)} imports {sorted(leaked)}")
    assert not offenders, "a database driver is reachable outside store/:\n  " + "\n  ".join(
        offenders
    )


def test_only_connection_py_calls_connect() -> None:
    """`engine.py` may name the driver -- it hands it to SQLAlchemy -- but not call it."""
    offenders: list[str] = []
    for path in _source_files():
        if path == CONNECTION_MODULE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name == "connect":
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not offenders, (
        "a connection is constructed outside store/connection.py, so role membership "
        "is no longer a property of construction:\n  " + "\n  ".join(offenders)
    )


def test_only_engine_py_builds_a_sqlalchemy_engine() -> None:
    """SQLAlchemy must never open a connection itself; it is always handed a `creator=`."""
    offenders: list[str] = []
    for path in _source_files():
        if path == ENGINE_MODULE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name == "create_engine":
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not offenders, "an engine is built outside store/engine.py:\n  " + "\n  ".join(offenders)


def test_every_create_engine_call_passes_a_creator() -> None:
    tree = ast.parse(ENGINE_MODULE.read_text(encoding="utf-8"), filename=str(ENGINE_MODULE))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        )
        == "create_engine"
    ]
    assert calls, "engine.py builds no engine -- this test is checking nothing"
    for call in calls:
        keywords = {kw.arg for kw in call.keywords}
        assert "creator" in keywords, (
            f"{ENGINE_MODULE.name}:{call.lineno} builds an engine without a creator=, "
            f"so SQLAlchemy would open its own unkeyed connection"
        )


def test_the_probe_finds_the_construction_site_it_permits() -> None:
    """The positive control: connection.py really does contain the only connect()."""
    tree = ast.parse(CONNECTION_MODULE.read_text(encoding="utf-8"), filename=str(CONNECTION_MODULE))
    connects = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        )
        == "connect"
    ]
    assert connects, "connection.py constructs nothing -- the scan is looking for the wrong shape"


def test_only_the_migration_runner_reaches_the_initializing_writer() -> None:
    """The creating handle has one caller, and that is the whole of norm 3's first half.

    `connect()` and `create_engine()` both have caller-restriction tests above;
    without this one, the *creating* writer -- the only handle in the codebase
    that can bring a datastore into existence -- was the least restricted of the
    three. `store init` reaches it through `migrate()`, not directly.
    """
    permitted = {CONNECTION_MODULE, SOURCE_ROOT / "store" / "migrations" / "__init__.py"}
    offenders: list[str] = []
    for path in _source_files():
        if path in permitted:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Attribute):
                name = node.attr
            else:
                continue
            if name == "initializing_writer":
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not offenders, (
        "the creating writer is reachable outside the migration runner; only an explicit "
        "`store init` and the runner may bring a datastore into existence:\n  "
        + "\n  ".join(offenders)
    )


def test_the_initializing_writer_probe_finds_its_permitted_caller() -> None:
    """The positive control: the scan is looking for a name that is really there."""
    runner = (SOURCE_ROOT / "store" / "migrations" / "__init__.py").read_text(encoding="utf-8")
    assert "initializing_writer" in runner
