"""The aggregator's vocabulary stays inside `connector/plaid/`, and the connector
never reaches the datastore.

`system-requirements.md` §9.2 asked how hard to draw this boundary. The answer
taken on 2026-09-06: one aggregator in v1, contained so a second is a new module
rather than a rewrite. Containment is only a boundary if something checks it --
an interface with one implementation would encode that implementation and call
itself a contract, whereas this test states the two properties that actually
matter and fails when either stops holding.

The second property is the load-bearing one. `store.raw.record_response` puts a
response in the archive *before* anything normalizes it (AC-5.1), and that
ordering is guaranteed by the connector having no way to write at all: it
returns bytes, and its caller archives them. A connector that could open a
datastore handle could also write a normalized row directly, and the guarantee
would go back to being a thing every future author has to remember.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
SOURCE_ROOT = REPO_ROOT / "src" / "bankmachine"
CONNECTOR_ROOT = SOURCE_ROOT / "connector"
PLAID_PACKAGE = CONNECTOR_ROOT / "plaid"

#: The aggregator SDK's top-level module. Matched as an imported name rather
#: than as the string "plaid" anywhere in the file, so that a comment, a
#: docstring or a keychain account name discussing the aggregator does not read
#: as a violation -- the rule is about what the module *depends on*.
AGGREGATOR_SDK = "plaid"

#: Modules that hand out a datastore handle. Importing one of these is the
#: relationship the norm is about; matching on a file's name would miss a
#: handle obtained through an alias and flag a module that only mentions one.
DATASTORE_HANDLE_MODULES = {
    "bankmachine.store.connection",
    "bankmachine.store.engine",
}


def _python_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def _imported_modules(source: str, filename: str) -> set[str]:
    """Every module a file imports, by full dotted path.

    `from x.y import z` records `x.y`, since that is the dependency; whether `z`
    is a name or a submodule does not change what the file rests on.
    """
    tree = ast.parse(source, filename=filename)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def _top_level(modules: set[str]) -> set[str]:
    return {module.split(".")[0] for module in modules}


def test_only_the_plaid_package_imports_the_aggregator_sdk() -> None:
    scanned = 0
    offenders: list[str] = []
    for path in _python_files(SOURCE_ROOT):
        if path.is_relative_to(PLAID_PACKAGE):
            continue
        scanned += 1
        modules = _imported_modules(path.read_text(encoding="utf-8"), str(path))
        if AGGREGATOR_SDK in _top_level(modules):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert scanned, f"scanned no files outside {PLAID_PACKAGE}; the scan is not testing anything"
    assert not offenders, (
        "the aggregator SDK is imported outside connector/plaid/:\n  " + "\n  ".join(offenders)
    )


def test_the_plaid_package_is_not_empty() -> None:
    """The positive control for the test above.

    A containment scan passes trivially when there is nothing to contain, and it
    would keep passing if the package were renamed or deleted. This asserts the
    thing being contained actually exists and does import the SDK -- so a green
    run above means "contained", never "absent".
    """
    importers = [
        path
        for path in _python_files(PLAID_PACKAGE)
        if AGGREGATOR_SDK
        in _top_level(_imported_modules(path.read_text(encoding="utf-8"), str(path)))
    ]
    assert importers, (
        f"no module under {PLAID_PACKAGE.relative_to(REPO_ROOT)} imports {AGGREGATOR_SDK!r}; "
        "the containment test above is passing because there is nothing to contain"
    )


def test_the_connector_cannot_reach_the_datastore() -> None:
    scanned = 0
    offenders: list[str] = []
    for path in _python_files(CONNECTOR_ROOT):
        scanned += 1
        modules = _imported_modules(path.read_text(encoding="utf-8"), str(path))
        reachable = modules & DATASTORE_HANDLE_MODULES
        if reachable:
            offenders.append(f"{path.relative_to(REPO_ROOT)} imports {sorted(reachable)}")
    assert scanned, f"scanned no files under {CONNECTOR_ROOT}; the scan is not testing anything"
    assert not offenders, (
        "the connector can obtain a datastore handle, so AC-5.1's archive-before-normalize "
        "ordering is no longer structural:\n  " + "\n  ".join(offenders)
    )
