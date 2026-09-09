"""The reference documents, and the derivations that keep them from being copies.

🔴 The whole risk in this surface is a second hand-written list. The warning
vocabulary already has consumers kept in agreement by machinery -- the published
`outputSchema`'s `kind` enum, the scan that refuses a `Caveat` naming a kind
outside it, and the guard that the handshake text names every kind -- and a
document restating the roster would be the copy none of them watch. So the tests
here check the derivation itself, not merely the text: the positive controls
below add a kind and add an envelope key, and fail if the documents were typed
rather than walked.

The protocol side -- `resources/list`, `resources/read`, an unknown URI, a
missing datastore -- is exercised over the real loop in `test_mcp.py`, beside
the tool tests that share its fixtures.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from bankmachine import envelope, mcp, mcp_resources

#: A kind heading as `_kind_section` writes it.
_KIND_HEADING = re.compile(r"^### `([^`]+)`$")

#: A scope heading. Anchored on the words that name the distinction; an anchor
#: that stops matching FAILS rather than passing quietly, because a guard cannot
#: check a section it can no longer find.
_CONNECTION_SCOPE = "Connection-scoped"
_REQUEST_SCOPE = "Request-scoped"

#: The three things a kind's guidance owes a reader. Named here because the
#: absence of the third is the failure mode: a kind can be defined and explained
#: and still leave an agent with nothing to DO about it.
_BULLETS = ("- **Means:**", "- **For this answer:**", "- **Do:**")


def _documents() -> dict[str, mcp_resources.Document]:
    return {d.uri: d for d in mcp_resources.documents(mcp._tool_definitions())}


def _warnings_text() -> str:
    return _documents()[mcp_resources.WARNINGS_URI].text


def _envelope_text() -> str:
    return _documents()[mcp_resources.ENVELOPE_URI].text


def _kind_sections(text: str) -> dict[str, list[str]]:
    """Each kind's own lines, keyed by kind, in the order the document names them."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        heading = _KIND_HEADING.match(line)
        if heading:
            current = heading.group(1)
            sections[current] = []
            continue
        if line.startswith("## "):
            current = None
            continue
        if current is not None:
            sections[current].append(line)
    return sections


def _scope_of(text: str) -> dict[str, str]:
    """Which scope heading each kind was filed under."""
    scopes: dict[str, str] = {}
    scope = ""
    for line in text.splitlines():
        if line.startswith("## "):
            scope = line
        heading = _KIND_HEADING.match(line)
        if heading:
            scopes[heading.group(1)] = scope
    return scopes


# --------------------------------------------------------------------------
# The warning vocabulary, derived rather than restated
# --------------------------------------------------------------------------


def test_the_warning_reference_names_every_kind_the_vocabulary_defines() -> None:
    """The same guard the handshake text carries, extended to its new destination.

    `instructions` is held to naming every kind because the agent reading it
    cannot act on a kind it was never given. A resource that carries the deep
    version of that text needs the same guard for the same reason, and needs it
    derived from the vocabulary rather than from a list written here.
    """
    sections = _kind_sections(_warnings_text())

    missing = sorted(kind for kind in envelope.WARNING_KINDS if kind not in sections)

    assert not missing, (
        f"the vocabulary defines {missing} but the reference never names them; an agent sent "
        f"here to learn what a warning means finds nothing about those"
    )


def test_the_warning_reference_names_no_kind_the_vocabulary_does_not() -> None:
    """The other direction, which is what makes this a derivation and not a superset.

    A document that names a kind the vocabulary has dropped tells an agent to
    branch on something no answer can carry -- and, worse, reads as evidence
    that the roster here is maintained by hand.
    """
    strays = sorted(set(_kind_sections(_warnings_text())) - set(envelope.WARNING_KINDS))

    assert not strays, f"the reference names {strays}, which the vocabulary does not define"


def test_every_kind_says_what_it_means_and_what_to_do_about_it() -> None:
    """🔴 The drift alarm. Guidance prose has no source, so a new kind arrives without it.

    The roster is walked from the vocabulary, which means a kind added there
    reaches the document by itself -- correctly, since dropping it would leave a
    reader short of the vocabulary they came for. What cannot be derived is the
    guidance, and this is what refuses to let a kind ship with the placeholder
    standing in for it.
    """
    sections = _kind_sections(_warnings_text())

    unwritten = sorted(
        kind
        for kind, lines in sections.items()
        if not all(any(line.startswith(bullet) for line in lines) for bullet in _BULLETS)
    )

    assert not unwritten, (
        f"{unwritten} reach the reference without all of {list(_BULLETS)}; the roster derives "
        f"itself but the guidance does not, so these need writing"
    )
    assert mcp_resources._UNWRITTEN not in _warnings_text()


def test_each_kind_is_filed_under_the_scope_the_vocabulary_puts_it_in() -> None:
    """🔴 The scope is the reason the kinds exist in two tuples, so it is not re-decided here.

    A connection-scoped warning rides every answer alike and says nothing about
    THIS one; a request-scoped warning fires only when this request crossed the
    boundary it names, so its absence is information. An agent that read a kind
    under the wrong heading would draw the opposite conclusion from its
    presence, which is worse than not having read it at all.
    """
    scopes = _scope_of(_warnings_text())

    for kind in envelope.CONNECTION_SCOPED_KINDS:
        assert _CONNECTION_SCOPE in scopes[kind], f"{kind} is not filed as connection-scoped"
    for kind in envelope.REQUEST_SCOPED_KINDS:
        assert _REQUEST_SCOPE in scopes[kind], f"{kind} is not filed as request-scoped"


def test_a_kind_added_to_the_vocabulary_reaches_the_reference_by_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The positive control: proof the roster is WALKED rather than typed out.

    A document that had the nine kinds written into it would pass every check
    above forever, and would go silently short the day a tenth was added. This
    adds one and reads the document back -- it appears, marked as having no
    guidance yet, which is the state the guard above then refuses to ship.
    """
    invented = "rate_limited"
    assert invented not in envelope.WARNING_KINDS, "pick a kind the vocabulary does not define"
    monkeypatch.setattr(
        envelope, "CONNECTION_SCOPED_KINDS", (*envelope.CONNECTION_SCOPED_KINDS, invented)
    )

    sections = _kind_sections(_warnings_text())

    assert invented in sections, "the reference did not follow the vocabulary, so it is a copy"
    assert any(mcp_resources._UNWRITTEN in line for line in sections[invented]), (
        "a kind with no guidance has to say so; silently rendering an empty section would "
        "read as 'nothing to know about this one'"
    )


# --------------------------------------------------------------------------
# The envelope, derived from what each tool publishes about its own answer
# --------------------------------------------------------------------------


def _published_paths() -> set[str]:
    return {
        mcp_resources._label(field.path)
        for field in mcp_resources._envelope_fields(mcp._tool_definitions())
    }


def test_the_envelope_reference_names_every_key_the_schemas_publish() -> None:
    """Every published key, including the ones nested inside a block.

    A scan of the top level cannot see `truncation.matching`, and the blocks are
    exactly where the numbers a consumer sums live.
    """
    text = _envelope_text()

    missing = sorted(path for path in _published_paths() if f"`{path}`" not in text)

    assert not missing, f"the schemas publish {missing} and the envelope reference omits them"
    assert {"truncation.matching", "coverage.transactions_in_effective_window"} <= (
        _published_paths()
    ), "the walk lost the nested keys it exists for, so it is back to reading the top level"


def test_the_row_fields_are_left_to_the_tool_that_owns_them() -> None:
    """🔴 `rows` is per tool, so one merged account of four row shapes describes none of them.

    Named as a key, deliberately not descended into: the row schema is published
    per tool, and the reference says where to look rather than inventing a
    fifth, tool-less version of it.
    """
    text = _envelope_text()

    assert "`rows`" in text
    assert not any(path.startswith("rows") and path != "rows" for path in _published_paths())
    assert "outputSchema" in text, "the reference does not say where row fields are"


def test_a_key_carried_by_only_some_tools_says_which() -> None:
    """🔴 The contract fixes ABSENCE as information, so absence has to be legible.

    No `effective_window` means the tool takes no window; no `truncation` means
    it returns every row it found. A reference that listed both without saying
    which tools carry them would publish the opposite: that any tool might carry
    either.
    """
    lines = _envelope_text().splitlines()
    windowed = [
        definition["name"]
        for definition in mcp._tool_definitions()
        if "effective_window" in definition["outputSchema"]["properties"]
    ]
    unwindowed = [
        definition["name"]
        for definition in mcp._tool_definitions()
        if "effective_window" not in definition["outputSchema"]["properties"]
    ]

    assert windowed and unwindowed, "every tool is the same shape, so this test checks nothing"
    at = next(i for i, line in enumerate(lines) if line.strip().startswith("- `effective_window`"))
    carriers = lines[at + 1]

    assert carriers.strip().startswith("Carried by:"), carriers
    for name in windowed:
        assert f"`{name}`" in carriers, f"{name} carries `effective_window` and is not named"
    for name in unwindowed:
        assert f"`{name}`" not in carriers, f"{name} does not carry `effective_window`"


def test_a_key_added_to_a_published_schema_reaches_the_reference_by_itself() -> None:
    """The positive control for the envelope, and the reason it takes an argument.

    `documents()` is handed the tool definitions rather than importing them, so
    a synthetic tool can be put through the same renderer the server uses. A
    hand-written envelope section would pass every other check here and go stale
    the first time a field was added to the wire.
    """
    invented: dict[str, Any] = {
        "name": "invented_tool",
        "outputSchema": {
            "type": "object",
            "properties": {
                "settlement_lag_days": {
                    "type": "integer",
                    "description": "how far behind the institution posts",
                }
            },
        },
    }

    text = next(
        document.text
        for document in mcp_resources.documents([*mcp._tool_definitions(), invented])
        if document.uri == mcp_resources.ENVELOPE_URI
    )

    assert "`settlement_lag_days`" in text, "the reference did not follow the schemas"
    assert "how far behind the institution posts" in text, (
        "the published description did not travel with the key, so the reference is a second "
        "account of the same field rather than a rendering of the first"
    )


def test_no_key_is_described_two_different_ways_across_the_tools() -> None:
    """One key, one account of it — checked rather than assumed by the renderer.

    The renderer keeps the first description it meets for a path, which is right
    while every tool builds its envelope from one shared function. If two ever
    disagreed, the reference would quietly report one of them and hide the
    other; this is what notices instead.
    """
    accounts: dict[str, set[tuple[str, str | None]]] = {}
    for definition in mcp._tool_definitions():
        for path, declared, description in mcp_resources._schema_fields(definition["outputSchema"]):
            accounts.setdefault(mcp_resources._label(path), set()).add((declared, description))

    disagreements = sorted(path for path, seen in accounts.items() if len(seen) > 1)

    assert not disagreements, (
        f"{disagreements} are published differently by different tools, so the envelope "
        f"reference can only report one of the accounts"
    )


# --------------------------------------------------------------------------
# The registry itself
# --------------------------------------------------------------------------


def test_every_document_is_listed_once_and_carries_what_a_host_lists() -> None:
    """A URI collision would have one document silently shadow the other."""
    documents = mcp_resources.documents(mcp._tool_definitions())

    assert len({document.uri for document in documents}) == len(documents)
    assert len({document.name for document in documents}) == len(documents)
    for document in documents:
        assert document.uri.startswith("bankmachine://"), document.uri
        assert document.title and document.description, document.uri
        assert document.mime_type == "text/markdown", document.uri
        assert document.text.endswith("\n"), document.uri


def test_the_documents_are_assembled_without_reading_anything() -> None:
    """🔴 AC-ARCH.3, held where it is cheapest to hold: this module cannot reach a store.

    Every tool answers against a missing or unreadable datastore, and a resource
    that raised instead would be a fresh way for the operator's tool to vanish
    on the one connection where they most need to ask why. `test_mcp.py` proves
    the wire answers; this is the reason it will keep answering — nothing here
    imports the configuration or the store, so there is nothing to open and
    nothing to fail.
    """
    import ast
    import inspect

    assert list(inspect.signature(mcp_resources.documents).parameters) == ["tool_definitions"]

    imported: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(mcp_resources))):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported |= {f"{node.module}.{alias.name}" for alias in node.names}

    reaches_a_store = sorted(
        name for name in imported if "config" in name or "store" in name or "engine" in name
    )
    assert not reaches_a_store, (
        f"this module imports {reaches_a_store}; a reference document that can touch the "
        f"datastore is one that can fail when the datastore cannot be read"
    )
