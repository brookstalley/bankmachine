"""The wire envelope describes an answer's shape and never reads one.

`envelope.py` holds the types every MCP answer is built from -- `Answer`,
`Caveat`, `Window`, `Truncation`, `Cursor` and the warning vocabulary. It was
split out of `query.py` because one module owning both the wire contract and
every tool's SQL meant an envelope change was reviewed inside a thousand lines
of unrelated statements, and each new tool made the seam more expensive.

A split is only a boundary if something checks it. The property that makes this
one worth having is **direction**: the envelope must not learn to read. The
moment it can open a handle, "how an answer is shaped" and "how this store
answers" are back in one place and the next author has no structural reason to
keep them apart -- the split would survive as a file listing and nothing else.

The constructors that DO need a connection stay in `query.py` deliberately.
`_answer` reconciles a requested window against coverage it reads in the same
call, which is how *this store* fills an envelope rather than what an envelope
*is*. That asymmetry is the boundary, so this test states it as one.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
SOURCE_ROOT = REPO_ROOT / "src" / "bankmachine"
ENVELOPE = SOURCE_ROOT / "envelope.py"

#: Modules that hand out a datastore handle or build a statement to run through
#: one. Matched as imported module paths rather than as text anywhere in the
#: file, so that a docstring discussing a query does not read as a violation --
#: the rule is about what the module *depends on*.
DATASTORE_MODULES = {
    "sqlalchemy",
    "bankmachine.store.connection",
    "bankmachine.store.engine",
    "bankmachine.store.schema",
}

#: The types whose presence makes a green run mean "contained" rather than
#: "absent". Every one is part of the wire contract a client reads.
ENVELOPE_TYPES = {"Answer", "Caveat", "Window", "Truncation", "Cursor"}


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


def _reaches_datastore(modules: set[str]) -> set[str]:
    """Datastore dependencies, matching a package by its prefix.

    `sqlalchemy.engine` counts as `sqlalchemy`: the rule is about reaching the
    database layer at all, and an import one level in is the same dependency
    wearing a longer name.
    """
    hits: set[str] = set()
    for module in modules:
        for banned in DATASTORE_MODULES:
            if module == banned or module.startswith(banned + "."):
                hits.add(module)
    return hits


def test_the_envelope_cannot_reach_the_datastore() -> None:
    reachable = _reaches_datastore(
        _imported_modules(ENVELOPE.read_text(encoding="utf-8"), str(ENVELOPE))
    )
    assert not reachable, (
        f"{ENVELOPE.relative_to(REPO_ROOT)} imports {sorted(reachable)}, so the wire envelope "
        "can now read the datastore. The split that separates the answer's SHAPE from how this "
        "store fills it is no longer structural -- move the reading code back to query.py, or "
        "record the decision to collapse the boundary."
    )


def test_the_envelope_still_defines_the_wire_types() -> None:
    """The positive control for the test above.

    A containment scan passes trivially when there is nothing to contain, and
    this one would keep passing if `envelope.py` were emptied, renamed, or
    reduced to re-exports -- reporting "the envelope reaches no datastore"
    about a file that no longer holds an envelope. This repo has already
    shipped one guard that was green because the thing it watched could not be
    reached, so the control is not hypothetical.
    """
    tree = ast.parse(ENVELOPE.read_text(encoding="utf-8"), filename=str(ENVELOPE))
    defined = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
    missing = ENVELOPE_TYPES - defined
    assert not missing, (
        f"{ENVELOPE.relative_to(REPO_ROOT)} no longer defines {sorted(missing)}; the containment "
        "test above is passing because there is nothing left to contain"
    )
