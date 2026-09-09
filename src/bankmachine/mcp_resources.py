"""The reference documents this server serves by URI rather than by tool call.

Tools are imperative: the agent decides to call one, and pays a turn for it. A
resource is addressable content a client reads by URI, so detail that lives here
costs a session nothing until something asks for it — which is why the deep
reference belongs here rather than in the handshake `instructions`, where every
session pays for it whether or not the question in front of it needs it.

🔴 **The list of warning kinds is DERIVED here, never restated.**
`query.WARNING_KINDS` already has consumers kept in agreement by machinery: the
published `outputSchema` closes `warnings[].kind` to it, a scan refuses any
`Caveat` naming a kind outside it, and a guard holds the handshake text to
naming every one. A hand-written list in this module would be the copy none of
that watches. So the warning document WALKS the two scope tuples and looks
guidance up by kind — a kind with no guidance still reaches the reader, named
and marked as unwritten, and the test beside this module fails until someone
writes it. The per-kind guidance is the new thing; the roster of kinds is not
this module's to invent.

The envelope document is derived the same way, from the `outputSchema` each tool
publishes: a field that reaches the wire reaches this document without anyone
remembering to add it, because the schema is already held to the payload.

🔴 **Nothing here reads the datastore.** These documents are assembled from the
vocabulary and the published schemas alone, so the reference surface answers
under AC-ARCH.3 for the same reason it answers at all — there is nothing for a
missing store to fail at.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import Any

from bankmachine import query

#: The scheme is this product's own. A resource URI is an identifier rather than
#: a location -- the client hands it straight back to `resources/read` -- and a
#: `file://` or `https://` URI would invite a client to try fetching it itself,
#: which would reach something other than this server or nothing at all.
_SCHEME = "bankmachine://reference"

WARNINGS_URI = f"{_SCHEME}/warnings"
ENVELOPE_URI = f"{_SCHEME}/envelope"

#: Markdown rather than plain text: these are read by a model, and the headings
#: are what let it find one kind without carrying all of them.
_MIME_TYPE = "text/markdown"

#: The one key of the envelope this document does NOT descend into. Row fields
#: are per tool -- `merchant` exists on transactions and nowhere else -- so they
#: are published per tool, in that tool's own `outputSchema`, and a single
#: "envelope" section merging four row shapes would describe none of them.
_ROWS_KEY = "rows"


@dataclass(frozen=True, slots=True)
class Document:
    """One reference document: what a client lists, and what it reads.

    Metadata and text in one object because `resources/list` and
    `resources/read` are two views of one registry -- a client that lists a URI
    it cannot then read is the failure the capability declaration exists to
    prevent, and two separate tables is how that happens.
    """

    uri: str
    name: str
    title: str
    description: str
    text: str
    mime_type: str = _MIME_TYPE


@dataclass(frozen=True, slots=True)
class _Guidance:
    """What one warning kind means, what it does to the answer, and what to do.

    Three fields rather than one paragraph because the third is the one an agent
    acts on, and a paragraph is where it gets skimmed past.
    """

    means: str
    for_this_answer: str
    act: str


#: Guidance per kind, keyed rather than listed: the roster comes from
#: `query.WARNING_KINDS`, and this map only answers for a kind that roster
#: already named. A kind missing from here is reported to the reader, not
#: dropped.
_GUIDANCE: dict[str, _Guidance] = {
    "stale": _Guidance(
        means=(
            "a contributing connection has not completed a successful sync recently; `detail` "
            "names the institution and how long it has been"
        ),
        for_this_answer=(
            "everything that institution contributes is as of its last successful sync rather "
            "than as of now, so a window ending today can read as a quiet stretch that is "
            "really an absence of syncing"
        ),
        act=(
            "read `last_success_at` for that connection from `get_pipeline_health`, and report "
            "the figure as of that moment rather than as of today. Do not read the recent end "
            "of a window as low activity until you have."
        ),
    ),
    "degraded": _Guidance(
        means=(
            "a contributing connection is failing; `detail` names the institution and the "
            "error it last failed with"
        ),
        for_this_answer=(
            "that institution's data stopped advancing at its last successful run and will not "
            "catch up until the connection is repaired, so any total spanning it is an "
            "undercount of a size nothing here can tell you"
        ),
        act=(
            "name the failing institution when you report the number, and treat a drop against "
            "an earlier period as unexplained rather than as a change in the operator's "
            "behaviour. `get_pipeline_health` carries `last_error_code`, which is what an "
            "operator needs in order to fix it."
        ),
    ),
    "gapped": _Guidance(
        means=(
            "an institution granted less history than was asked for; `detail` names the days "
            "granted against the days requested"
        ),
        for_this_answer=(
            "🔴 data older than the granted window is ABSENT, not zero. A question reaching "
            "into the gap comes back as a confident small number, and nothing in the number "
            "itself says so"
        ),
        act=(
            "compare your window against `granted_history_days` and `history_starts` from "
            "`get_pipeline_health` before treating an older period as quiet. A null "
            "`granted_history_days` means NOT YET MEASURED, never 'no shortfall'."
        ),
    ),
    "partial": _Guidance(
        means=(
            "something a complete answer rests on is not known yet: no connection is enrolled, "
            "an enrolled one has never completed a sync, its granted history window has not "
            "been measured, or the datastore itself could not be read"
        ),
        for_this_answer=(
            "the zeroes and the empty `rows` are the shape of an unanswered question rather "
            "than a measured absence -- 'nothing could be read' and 'nothing happened' are the "
            "same payload without this warning"
        ),
        act=(
            "read `detail`, which says which of those it is, and where the datastore is "
            "unreadable also carries the command that fixes it. Report which one you mean "
            "rather than reporting the zero."
        ),
    ),
    "rule-applied": _Guidance(
        means="an account rule filtered rows out of an aggregate, so the total excludes them",
        for_this_answer=(
            "the figure is smaller than the raw sum over the same window, and deliberately so; "
            "it will not reconcile against a total computed without the rule"
        ),
        act=(
            "🔴 say the exclusion out loud when you report the total. An exclusion silently "
            "forgotten during analysis is the outcome this kind exists to prevent."
        ),
    ),
    "window_starts_before_coverage": _Guidance(
        means=(
            "the window you asked for reaches back past the first date the store covers; "
            "`detail` names where coverage begins and what this answer covered instead"
        ),
        for_this_answer=(
            "the figure is computed over the covered part of your window only. "
            "`effective_window.effective` says which part that was, beside the `requested` "
            "bounds you sent"
        ),
        act=(
            "report the effective window rather than the one you asked for, and never divide "
            "by the requested span -- a monthly average over a window that was clamped to a "
            "week is wrong by a ratio nothing in the number reveals."
        ),
    ),
    "window_extends_past_coverage": _Guidance(
        means=(
            "the window reaches past the covered end -- today, or the last transaction when "
            "that is later; `detail` names it"
        ),
        for_this_answer=(
            "the same clamp from the other end: the tail of your window contributed nothing "
            "because there is nothing there yet, not because nothing happened in it"
        ),
        act=(
            "read `effective_window.effective.until` and say which period you actually "
            "reported on. A window running past today is the ordinary case for a "
            "'this month' question, and it is not an error."
        ),
    ),
    "rows_truncated": _Guidance(
        means=(
            "the request matched more rows than the cap returns; `detail` names how many "
            "matched, how many came back, and how many are missing"
        ),
        for_this_answer=(
            "🔴 the rows are the NEWEST ones only, so summing or counting them describes what "
            "came back rather than the window you asked about"
        ),
        act=(
            "page. Pass the answer's `truncation.next_cursor` straight back as `cursor` with "
            "the SAME window and account, and keep going until `truncation.truncated` is "
            "false -- that is the only route that reaches every matching row. Narrowing the "
            "window or raising `limit` moves the cap; paging removes it."
        ),
    ),
    "accounts_without_coverage": _Guidance(
        means=(
            "an account inside the scope of this request has NEVER had a transaction "
            "recorded -- not none in this window, none at all, ever"
        ),
        for_this_answer=(
            "any row count, total or empty result touching that account describes ABSENT "
            "DATA rather than absent activity. An answer of 'no payments found' about it is "
            "false, and it is the most plausible-looking false answer this surface can give: "
            "nothing about an empty list looks wrong"
        ),
        act=(
            "never report zero activity for a named account carrying this warning. Say the "
            "account has no transaction data at all, and call `get_coverage_report` for the "
            "per-account picture -- `transaction_count`, the first and last transaction, and "
            "how long it has been silent against its own cadence."
        ),
    ),
    "counted_during_change": _Guidance(
        means=(
            "a write landed between the row read and the count read, so the two describe "
            "moments a fraction apart"
        ),
        for_this_answer=(
            "the rows are accurate as of this answer's `as_of` stamp; the count beside them "
            "was taken across a change and need not agree with them"
        ),
        act=(
            "trust the rows and the `as_of` stamp, and ask again if you need a count taken "
            "after the change. This is a data condition rather than a failure, and repeating "
            "the call resolves it."
        ),
    ),
}


#: Said once per scope rather than once per kind, because it is one fact about
#: the scope and nine copies of it is how the copies stop agreeing.
_CONNECTION_SCOPE_NOTE = (
    "🔴 These describe the standing state of the PIPELINE, so they ride every answer alike. "
    "Measurement found the `gapped` notice arriving character-for-character identical on a "
    "window wholly inside coverage, a window wholly outside it, a future window, and a query "
    "for an account that does not exist — true every time, and useless for telling you whether "
    "THIS answer is the affected one. Read them as a qualification on the pipeline, and read "
    "the request-scoped kinds below to learn what happened to this request."
)

_REQUEST_SCOPE_NOTE = (
    "🔴 These fire only when the request carrying them actually crosses the boundary they "
    "name, so their presence is information and SO IS THEIR ABSENCE. None of them present "
    "means this request stayed inside what the store can answer over."
)

_WARNINGS_INTRO = (
    "🔴 Read `warnings` before drawing a conclusion: an answer can be perfectly well-formed "
    "and still be computed over incomplete data — that is the failure this field exists to "
    "make impossible to miss.\n\n"
    "Every kind this server can send is below, with what it means, what it implies about the "
    "answer carrying it, and what to do about it. Each warning also carries a `detail` "
    "sentence written for the case in hand, and a warning about one connection carries "
    "`connection_id` and `institution` so you can name which institution went quiet.\n\n"
    "The kinds below are the vocabulary itself: every tool's published `outputSchema` closes "
    "`warnings[].kind` to exactly this set. Kinds are added additively and need no version "
    "bump, so treat an unfamiliar one as a warning you have not been briefed on rather than "
    "as an error."
)

_UNWRITTEN = (
    "the vocabulary declares this kind and no guidance for it has been written yet — treat it "
    "as a warning you have not been briefed on, and read its `detail`"
)


def _kind_section(kind: str) -> str:
    """One kind, rendered. Never omitted for want of guidance.

    A kind the vocabulary declares is one an answer can carry, so dropping it
    here would leave a reader who came for the whole vocabulary quietly short of
    it. The unwritten case is stated instead, which is a true sentence and a
    visible one.
    """
    guidance = _GUIDANCE.get(kind)
    if guidance is None:
        return f"### `{kind}`\n\n- **Means:** {_UNWRITTEN}\n"
    return (
        f"### `{kind}`\n\n"
        f"- **Means:** {guidance.means}\n"
        f"- **For this answer:** {guidance.for_this_answer}\n"
        f"- **Do:** {guidance.act}\n"
    )


def _warning_reference() -> str:
    """The vocabulary in full, walked from the two scope tuples that define it."""
    parts = [
        "# Warning kinds",
        "",
        _WARNINGS_INTRO,
        "",
        "## Connection-scoped — about the pipeline",
        "",
        _CONNECTION_SCOPE_NOTE,
        "",
    ]
    parts += [_kind_section(kind) for kind in query.CONNECTION_SCOPED_KINDS]
    parts += [
        "## Request-scoped — about this request",
        "",
        _REQUEST_SCOPE_NOTE,
        "",
    ]
    parts += [_kind_section(kind) for kind in query.REQUEST_SCOPED_KINDS]
    return "\n".join(parts).rstrip() + "\n"


@dataclass(frozen=True, slots=True)
class _Field:
    """One key of the envelope, and which tools carry it."""

    path: tuple[str, ...]
    declared_type: str
    description: str | None
    tools: tuple[str, ...]


def _declared_type(spec: dict[str, Any]) -> str:
    declared = spec.get("type")
    if isinstance(declared, list):
        return " or ".join(str(entry) for entry in declared)
    return str(declared) if declared is not None else "any"


def _schema_fields(
    schema: dict[str, Any], prefix: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], str, str | None]]:
    """Every key one published schema declares, depth first, in declared order.

    Descends through objects and through the items of an array, so a key nested
    inside a block is named here — a scan of the top level alone cannot see
    `truncation.matching`, and the blocks are exactly where the numbers a
    consumer sums live.
    """
    properties: dict[str, Any] = schema.get("properties", {})
    for name, spec in properties.items():
        path = prefix + (name,)
        yield path, _declared_type(spec), spec.get("description")
        if spec.get("type") == "object":
            yield from _schema_fields(spec, path)
        elif spec.get("type") == "array" and path != (_ROWS_KEY,):
            items: dict[str, Any] = spec.get("items", {})
            if items.get("type") == "object":
                yield from _schema_fields(items, (*path, "[]"))


def _envelope_fields(tool_definitions: list[dict[str, Any]]) -> list[_Field]:
    """The union of every tool's envelope, each key knowing which tools carry it.

    The union rather than one sample: a tool that carries neither
    `effective_window` nor `truncation` cannot describe them, and the contract
    fixes their ABSENCE as information — so which tools carry a key is part of
    what this document has to say.
    """
    fields: dict[tuple[str, ...], _Field] = {}
    for definition in tool_definitions:
        name = str(definition["name"])
        for path, declared, description in _schema_fields(definition["outputSchema"]):
            seen = fields.get(path)
            if seen is None:
                fields[path] = _Field(path, declared, description, (name,))
            else:
                fields[path] = replace(seen, tools=(*seen.tools, name))
    return list(fields.values())


def _label(path: tuple[str, ...]) -> str:
    """A path as a consumer reads it off the payload: `truncation.matching`."""
    rendered = ""
    for part in path:
        if part == "[]":
            rendered += "[]"
        elif rendered:
            rendered += f".{part}"
        else:
            rendered = part
    return rendered


def _depth(path: tuple[str, ...], present: set[tuple[str, ...]]) -> int:
    """How far to indent, counting only ancestors this section actually renders.

    A key whose parent is in the other section -- the window-scoped coverage
    count, whose `coverage` block is on every answer -- would otherwise be
    indented under a bullet that is not there, which reads as a child of
    whatever bullet happens to precede it.
    """
    return sum(1 for cut in range(1, len(path)) if path[:cut] in present)


def _field_lines(fields: list[_Field], *, name_carriers: bool) -> list[str]:
    """The fields as an indented list, naming carriers only where they change.

    Repeating the same tool names under every leaf of a block says nothing the
    block's own line did not, and buries the one place the carriers narrow.
    """
    by_path = {field.path: field for field in fields}
    lines: list[str] = []
    for field in fields:
        indent = "  " * _depth(field.path, set(by_path))
        rendered = f"{indent}- `{_label(field.path)}` ({field.declared_type})"
        if field.description:
            rendered += f" — {field.description}"
        parent = by_path.get(field.path[:-1]) or by_path.get(field.path[:-2])
        if name_carriers and (parent is None or parent.tools != field.tools):
            carriers = ", ".join(f"`{tool}`" for tool in field.tools)
            rendered += f"\n{indent}  Carried by: {carriers}."
        lines.append(rendered)
    return lines


_ENVELOPE_INTRO = (
    "Every tool on this server answers with the same envelope around its `rows`. What follows "
    "is read off the `outputSchema` each tool publishes, so it describes the answer a "
    "validating client checks rather than a second account of it.\n\n"
    "Row fields are per tool — `merchant` exists on transactions and nowhere else — and are "
    "published in each tool's own `outputSchema` rather than here."
)

_ENVELOPE_ALWAYS = (
    "## Carried by every answer\n\n"
    "These are required on every tool, including an answer assembled when the datastore could "
    "not be read at all: an empty answer that still carries `coverage` and `warnings` is how "
    "'nothing was found' is told apart from 'nothing could be read'."
)

_ENVELOPE_SOMETIMES = (
    "## Carried only where they are true of the tool\n\n"
    "🔴 Absence is information. No `effective_window` means that tool takes no window; no "
    "`truncation` means it returns every row it found. A key is never omitted because the "
    "answer was empty — a windowed tool that could read nothing still carries the key, with "
    "null effective bounds, because 'your window and this store do not overlap' is the true "
    "statement about a store that cannot be read."
)

_ENVELOPE_NOTES = (
    "## Reading the envelope\n\n"
    "- **Amounts are integer minor units** (cents for USD) and signed from the account "
    "holder's point of view: negative is money out, positive is money in. A credit card "
    "balance is negative. The field name says `minor_units` wherever this applies.\n"
    "- **`build` says which code answered.** This server is a subprocess the client launches "
    "at connect time, so it runs whatever existed then; `commit` is captured once at start "
    "rather than re-read per call. A null `commit` means the build could not be identified, "
    "and `dirty` is then null too rather than a false claim that the tree was clean.\n"
    "- **`coverage.transactions` is always store-wide** and never narrows with the question "
    "asked. A windowed answer adds `transactions_in_effective_window`, which is the count to "
    "read against a windowed question — it is not narrowed by `account_id` either, so it is a "
    "fact about the window rather than about your filters. `truncation.matching` is the one "
    "that reflects your filters, so compare the two rather than either alone.\n"
    "- **`truncation.next_cursor` is OPAQUE.** Pass it back unchanged as `cursor` with the "
    "same window and account; never read one, build one, or edit one. It is present when and "
    "only when there is another page to read.\n"
    f"- **Warnings have a vocabulary of their own**, with what each kind means and what to do "
    f"about it, at `{WARNINGS_URI}`."
)


def _envelope_reference(tool_definitions: list[dict[str, Any]]) -> str:
    """The envelope, derived from what each tool publishes about its own answer."""
    fields = _envelope_fields(tool_definitions)
    universal = [field for field in fields if len(field.tools) == len(tool_definitions)]
    conditional = [field for field in fields if len(field.tools) < len(tool_definitions)]
    parts = ["# The answer envelope", "", _ENVELOPE_INTRO, "", _ENVELOPE_ALWAYS, ""]
    parts += _field_lines(universal, name_carriers=False)
    if conditional:
        parts += ["", _ENVELOPE_SOMETIMES, ""]
        parts += _field_lines(conditional, name_carriers=True)
    parts += ["", _ENVELOPE_NOTES]
    return "\n".join(parts).rstrip() + "\n"


def documents(tool_definitions: list[dict[str, Any]]) -> list[Document]:
    """Every reference document this server serves, in listing order.

    The tool definitions arrive as an argument rather than being imported: the
    envelope document is derived from what they publish, and the module that
    owns the protocol is the one that owns them.
    """
    return [
        Document(
            uri=WARNINGS_URI,
            name="warning-kinds",
            title="Warning kinds, and what to do about each",
            description=(
                "Every kind a `warnings` entry can name: what it means, what it implies about "
                "the answer carrying it, and what to do about it. Read this before treating "
                "any answer as complete."
            ),
            text=_warning_reference(),
        ),
        Document(
            uri=ENVELOPE_URI,
            name="answer-envelope",
            title="The envelope every answer carries",
            description=(
                "Every field of the envelope wrapped around each tool's rows, which tools "
                "carry each one, and how to read them. Derived from the schema each tool "
                "publishes for its own answer."
            ),
            text=_envelope_reference(tool_definitions),
        ),
    ]
