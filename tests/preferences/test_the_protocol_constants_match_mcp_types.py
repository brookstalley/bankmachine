"""The protocol facts `mcp.py` copies by hand are held to the types they were copied from.

🔴 **The class this closes.** The MCP server speaks JSON-RPC over stdio without
the SDK (`api-notes-plaid.md` §18), so every protocol constant in `mcp.py` is a
copy read from `mcp_types` by a person. One review pass found four defects in
that layer, and every one was a fact that lives in `mcp_types`: a revision
offered on a handshake path where it does not exist, a current revision missing
so clients were downgraded, a key hung off `serverInfo` that the client's parser
drops, and a JSON-RPC shape read wrongly. Each failed at connection time, which
is the one place no test was looking.

So `mcp-types` is a DEV dependency and these tests compare the copies with it.
Upgrading it in `uv.lock` is how a protocol change reaches this server's
attention: the comparison goes red and names the constant.

🔴 **What this does NOT do.** It does not make `mcp_types` a runtime import. The
constants stay written out in `mcp.py`, and the last test here keeps it that way
-- the runtime tree has no pydantic, and a test oracle that quietly became a
runtime dependency would be the decision §18 rejected, taken by accident.

🔴 **What it cannot see.** A wire fact `mcp.py` never copied into a constant --
the shape of a dict it builds inline -- is not compared by anything here. The
handshake tests in `tests/test_mcp.py` cover the shapes that have bitten.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import mcp_types
import mcp_types.jsonrpc
import mcp_types.version

from bankmachine import mcp

REPO_ROOT = Path(__file__).parents[2]
SOURCE_ROOT = REPO_ROOT / "src" / "bankmachine"


def test_the_newest_handshake_revision_is_the_one_mcp_types_names() -> None:
    """🔴 `LATEST_HANDSHAKE_VERSION`, never `LATEST_PROTOCOL_VERSION`.

    The latter is the newest revision in ANY era, and it is not reachable through
    `initialize`. Offering it there is the first of the four defects.
    """
    assert mcp.LATEST_HANDSHAKE_VERSION == mcp_types.version.LATEST_HANDSHAKE_VERSION


def test_the_fallback_is_the_revision_mcp_types_negotiates_by_default() -> None:
    assert mcp.FALLBACK_PROTOCOL_VERSION == mcp_types.DEFAULT_NEGOTIATED_VERSION


def test_every_handshake_revision_is_offered_and_nothing_else() -> None:
    """Compared as SEQUENCES, so a revision missing, extra or out of order all go red.

    Missing is the second of the four defects: a current client asked for a
    revision this server did not list and was counter-offered one two revisions
    older. Extra is the first, arriving from the other side.
    """
    assert mcp.SUPPORTED_PROTOCOL_VERSIONS == mcp_types.version.HANDSHAKE_PROTOCOL_VERSIONS


def test_the_json_rpc_error_codes_are_the_ones_mcp_types_defines() -> None:
    """A client classifies a refusal by its code, so a wrong number is a wrong classification."""
    copied = {
        "PARSE_ERROR": mcp._PARSE_ERROR,
        "INVALID_REQUEST": mcp._INVALID_REQUEST,
        "METHOD_NOT_FOUND": mcp._METHOD_NOT_FOUND,
        "INVALID_PARAMS": mcp._INVALID_PARAMS,
        "INTERNAL_ERROR": mcp._INTERNAL_ERROR,
    }
    oracle = {name: getattr(mcp_types.jsonrpc, name) for name in copied}
    assert copied == oracle


def test_the_tool_annotation_hints_are_ones_tool_annotations_declares() -> None:
    """🔴 These are field NAMES copied from `ToolAnnotations`, and a dropped one is silent.

    The wire base leaves pydantic's `extra="ignore"` in force, so a misspelled
    hint is discarded in the client's parser -- present in the bytes, gone by
    the time anything reads them. `destructiveHint` and `openWorldHint` both
    default to the alarming answer, so a hint that fails to arrive is read as
    the opposite of what is true here.

    A SUBSET, not equality: this server states four of the five and deliberately
    leaves `title` to the tool definitions.
    """
    declared = {
        field.alias or name for name, field in mcp_types.ToolAnnotations.model_fields.items()
    }
    assert set(mcp._READ_ONLY_ANNOTATIONS) <= declared, (
        f"{sorted(set(mcp._READ_ONLY_ANNOTATIONS) - declared)} are not fields of "
        f"ToolAnnotations, so a client would drop them before reading"
    )


def _runtime_dependency_names() -> set[str]:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    names: set[str] = set()
    for requirement in project["project"]["dependencies"]:
        # The name is everything before the first version, extra or marker character.
        name = requirement
        for stop in "<>=!~[; ":
            name = name.split(stop, 1)[0]
        names.add(name.strip().lower().replace("_", "-"))
    return names


def test_the_oracle_is_not_a_runtime_dependency() -> None:
    """🔴 The owner's ruling, 2026-09-16: a test oracle, not a runtime dependency."""
    leaked = _runtime_dependency_names() & {"mcp-types", "mcp", "pydantic"}
    assert not leaked, (
        f"{sorted(leaked)} reached [project] dependencies. `mcp-types` is a dev dependency "
        f"by ruling, so the runtime tree carries no pydantic; see api-notes-plaid.md §18"
    )


def test_no_runtime_module_imports_the_oracle() -> None:
    """The dependency declaration is half of it; an import is the other half.

    A dev dependency is installed in every environment a developer runs, so an
    import of it passes every test here and then fails on the one install that
    matters -- the operator's, which has no dev group.
    """
    modules = sorted(SOURCE_ROOT.rglob("*.py"))
    assert modules, f"no modules found under {SOURCE_ROOT}, so this scan checked nothing"
    offenders: list[str] = []
    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                roots = [node.module.split(".")[0]]
            else:
                continue
            if {"mcp_types", "mcp", "pydantic"} & set(roots):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not offenders, f"runtime modules import the test oracle: {offenders}"
