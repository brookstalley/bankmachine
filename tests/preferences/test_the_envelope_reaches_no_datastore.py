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

#: 🔴 **An allowlist, not a ban list, and the difference is the whole guard.**
#: The tempting form enumerates the modules that hand out a handle --
#: `store.connection`, `store.engine`, `store.schema`. That form is already
#: wrong: `store.derivation`, `store.raw` and `store.rebuild` reach the database
#: too and would pass it, and the next module added to `store/` passes it by
#: default. A guarantee defined by an enumeration decays; one defined by what is
#: PERMITTED has no case to fall outside of.
#:
#: `store.types` is on the list and is the one entry worth explaining. It imports
#: SQLAlchemy to declare column types, so the envelope depends on SQLAlchemy
#: transitively -- but it opens nothing and executes nothing. The property being
#: guarded is that the envelope cannot READ, not that the name `sqlalchemy` is
#: absent from its import graph, and stating it as an allowlist is what keeps
#: those two from being confused.
ALLOWED_FIRST_PARTY = {
    "bankmachine.build_id",
    "bankmachine.store.types",
}

#: Direct execution machinery. Separate from the allowlist because it catches
#: the other direction -- importing SQLAlchemy straight into the envelope rather
#: than reaching the database through one of this project's own modules.
FORBIDDEN_THIRD_PARTY = {"sqlalchemy"}

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


def _matches(module: str, prefixes: set[str]) -> bool:
    """Whether a module is one of `prefixes`, or lives beneath one.

    `sqlalchemy.engine` counts as `sqlalchemy`: an import one level in is the
    same dependency wearing a longer name.
    """
    return any(module == p or module.startswith(p + ".") for p in prefixes)


def test_the_envelope_reaches_only_what_it_is_allowed_to() -> None:
    modules = _imported_modules(ENVELOPE.read_text(encoding="utf-8"), str(ENVELOPE))
    first_party = {m for m in modules if m == "bankmachine" or m.startswith("bankmachine.")}
    unlisted = {m for m in first_party if not _matches(m, ALLOWED_FIRST_PARTY)}
    assert not unlisted, (
        f"{ENVELOPE.relative_to(REPO_ROOT)} imports {sorted(unlisted)}, which is not on this "
        "module's allowlist. The envelope describes what an answer IS; anything that reads a "
        "datastore to fill one belongs in query.py beside the reading it depends on. Move the "
        "code back, or widen ALLOWED_FIRST_PARTY deliberately and say why the addition cannot "
        "read."
    )


def test_the_envelope_does_not_import_the_execution_layer() -> None:
    """The other direction: reaching the database without going through us.

    The allowlist above governs first-party imports. This catches SQLAlchemy
    imported straight into the envelope, which would let it build and run a
    statement without touching any `bankmachine.store` module at all.
    """
    modules = _imported_modules(ENVELOPE.read_text(encoding="utf-8"), str(ENVELOPE))
    direct = {m for m in modules if _matches(m, FORBIDDEN_THIRD_PARTY)}
    assert not direct, (
        f"{ENVELOPE.relative_to(REPO_ROOT)} imports {sorted(direct)} directly, so it can build "
        "and execute a statement. The envelope is a description of an answer's shape and has no "
        "reason to hold one."
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
