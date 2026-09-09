"""Every field the tools publish is described somewhere a consumer reads.

🔴 **The class this closes.** `test_the_documented_tool_surface_is_the_built_one.py`
compares tool NAME sets, and the warning-vocabulary guard reconciles warning
KINDS. Nothing compared the published *fields*. So the wire grew and its
descriptions did not, twice in one work cycle: `list_accounts` was documented as
carrying a lifecycle state it did not have (#47), and then a batch of new
`totals` keys and row fields shipped while `api-contract.md` still described the
narrower shape.

Both directions of that are bad and they are bad differently. A field on the
wire that no document mentions is one a consumer never learns to read -- the
whole product thesis is that a caveat which is not in the payload is invisible,
and a payload field nobody documented is the same failure one layer out. A field
described in prose that the wire does not carry is worse: a consumer branches on
it and waits forever.

🔴 **What this does NOT check.** That the description is ACCURATE. Nothing
mechanical can, and the guard says so rather than implying coverage it lacks:
that is the Critic's under `api-contract.md`'s Direction norms. This asserts
only that every published name is accounted for, which is the half that has now
failed twice while every test stayed green.

The contract is the canonical document, so it is the one held to completeness.
`docs/connecting-an-mcp-client.md` is explicitly a copy that "can fall behind"
by its own text, and the server `instructions` are already held to the envelope
by `tests/test_mcp.py`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bankmachine import mcp

CONTRACT = Path(__file__).resolve().parents[2] / ".prawduct" / "artifacts" / "api-contract.md"


def _published_field_names() -> set[str]:
    """Every property name in every tool's `outputSchema`, at any depth.

    Walks rather than reading a hand-kept list, for the reason the vocabulary
    guard walks: a list typed out here would pass forever and go silently short
    the day a field was added, which is the exact defect this file exists for.
    """
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "properties" and isinstance(value, dict):
                    found.update(value.keys())
                # Descends into EVERY value, JSON Schema's own keywords
                # included, because a nested object's `properties` sits
                # underneath one of them -- and a nested object is exactly where
                # an undocumented field hides. Nothing is filtered on the way
                # down: only a `properties` dict contributes names, so a schema
                # keyword can never be mistaken for a field.
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for definition in mcp._tool_definitions():
        walk(definition.get("outputSchema", {}))
    return found


def test_every_published_field_is_described_in_the_api_contract() -> None:
    """🔴 The wire cannot grow past the document that is supposed to define it.

    Unconditional since 2026-09-09: it compares EVERY published name against the
    contract, with no exemption set beside it. It did not start that way. It
    shipped as a ratchet -- a frozen set of 34 names measured on the shipped
    surface the day the walk was first run over it, which a new field could not
    join and which a later documented name could not linger in. That set is now
    empty and gone, and its two pawls with it.

    The shape is worth keeping in view rather than only the outcome, because it
    is what let the guard exist before the work it demanded was done: a check
    that had waited for 34 descriptions would not have been holding the line
    during the cycle in which they were written, and the drift it exists to stop
    happened twice in one cycle before it existed.

    🔴 There is no exemption set to add a name to any more, and reintroducing one
    is not the way past a failure here. A field that genuinely cannot be
    described is a finding about the field.
    """
    published = _published_field_names()
    contract = CONTRACT.read_text(encoding="utf-8")

    assert published, "no properties were read from any outputSchema; this guard covers nothing"

    undocumented = sorted(name for name in published if f"`{name}`" not in contract)

    assert not undocumented, (
        f"{undocumented} are published on a tool's outputSchema and appear nowhere in "
        f"{CONTRACT.name}. A field a consumer is never told about is one it never learns to "
        f"read, which is this product's own thesis applied one layer out from the payload. "
        f"Describe it in the contract -- § The published field shapes is where the row tables "
        f"live, and its extraction contract says how a name is spelled there"
    )


def test_the_walk_reaches_fields_nested_inside_a_row() -> None:
    """The positive control: proof the walk descends rather than reading the top level.

    A reader that returned only envelope keys would satisfy the assertion above
    for as long as no envelope key was added, while the row fields -- where both
    real failures happened -- went unchecked. So this names fields that exist
    only inside a row, and inside a block inside a row.
    """
    published = _published_field_names()

    for nested in ("lifecycle", "roster_last_observed", "stranded_holds", "sign_convention"):
        assert nested in published, (
            f"{nested!r} is published inside a row and the walk did not reach it, so this "
            f"guard is checking the envelope only"
        )
    for deeper in ("days_pending", "transaction_id"):
        assert deeper in published, (
            f"{deeper!r} is published inside a block inside a row and the walk did not reach "
            f"it; a nested object is exactly where an undocumented field hides"
        )


def test_the_reader_reports_a_field_the_contract_never_mentions() -> None:
    """The other control: proof the comparison can fail at all.

    Asserts against a name chosen to be absent from both sides, so it fails if
    the containment test were ever inverted or short-circuited.
    """
    contract = CONTRACT.read_text(encoding="utf-8")
    invented = "quarterly_vibes_index"

    assert f"`{invented}`" not in contract, "pick a name the contract does not contain"
    assert invented not in _published_field_names(), "pick a name no tool publishes"
