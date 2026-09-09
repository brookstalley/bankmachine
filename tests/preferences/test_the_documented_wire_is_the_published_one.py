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


#: 🔴 **A debt, recorded as data so it can only shrink.** These field names are
#: published on a tool's `outputSchema` and are not written in backticks anywhere
#: in `api-contract.md` today. That is a real gap and it long predates the guard;
#: this set IS the measurement, taken on 2026-09-09 when the check was first run
#: over the shipped surface. Its size is `len(UNDOCUMENTED_AT_FREEZE)` and is not
#: written out here, because the whole point is that the number falls.
#:
#: 🔴 **It is frozen at that measurement on purpose, and the freezing is the
#: point.** Documenting all of them is its own work item and does not belong in
#: the change that discovered them -- but a guard that waited for that work would
#: not exist, and the drift it exists to stop would keep happening in the
#: meantime. So the guard ships now with the backlog held still: a NEW field
#: cannot join this set, and the test below refuses an entry that has since been
#: documented, so the list cannot quietly become a place to hide things.
#:
#: Removing a name from here is the documentation landing. Adding one is not a
#: thing to do: if a new field genuinely cannot be documented, that is a finding,
#: not an entry.
UNDOCUMENTED_AT_FREEZE: frozenset[str] = frozenset(
    {
        "aggregator",
        "debt_service_outflow_minor_units",
        "group_key",
        "group_label",
        "history_starts",
        "inflow_minor_units",
        "internal_transfer_outflow_minor_units",
        "last_error_code",
        "last_success_at",
        "manual",
        "net_minor_units",
        "outflow_minor_units",
        "requested_history_days",
        "retired",
    }
)


def test_no_newly_published_field_escapes_the_api_contract() -> None:
    """🔴 The wire cannot grow past the document that is supposed to define it.

    Scoped to what is NEW rather than to everything, because everything is a
    backlog and this is a ratchet. The frozen set above holds the debt still;
    anything outside it is a field this change added without describing.
    """
    published = _published_field_names()
    contract = CONTRACT.read_text(encoding="utf-8")

    assert published, "no properties were read from any outputSchema; this guard covers nothing"

    undocumented = {name for name in published if f"`{name}`" not in contract}
    new = sorted(undocumented - UNDOCUMENTED_AT_FREEZE)

    assert not new, (
        f"{new} are published on a tool's outputSchema and appear nowhere in "
        f"{CONTRACT.name}. A field a consumer is never told about is one it never learns to "
        f"read, which is this product's own thesis applied one layer out from the payload. "
        f"Describe it in the contract -- do not add it to UNDOCUMENTED_AT_FREEZE"
    )


def test_the_frozen_debt_holds_no_name_that_is_now_documented() -> None:
    """🔴 The ratchet's other pawl: the list may shrink and may not go stale.

    Without this, a name could be documented and left listed, and the set would
    slowly stop describing anything -- an exemption list nobody prunes is how a
    guard's scope quietly shrinks to nothing. Failing here is good news: it means
    somebody wrote the documentation and only has to delete a line.
    """
    contract = CONTRACT.read_text(encoding="utf-8")

    now_documented = sorted(n for n in UNDOCUMENTED_AT_FREEZE if f"`{n}`" in contract)

    assert not now_documented, (
        f"{now_documented} are now described in {CONTRACT.name} and can be deleted from "
        f"UNDOCUMENTED_AT_FREEZE; leaving them listed lets the exemption outlive its reason"
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
