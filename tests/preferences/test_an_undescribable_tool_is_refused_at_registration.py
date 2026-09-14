"""The two registration guards that keep the tool surface strictly describable.

🔴 Both refuse for ONE reason: a surface that cannot be described strictly
should not be advertised at all. `api-contract.md` § Direction's fourth norm
draws a tool's boundary where the answer *shape* changes, and its first
guardrail — one strict row schema covering every parameter value, no optional
fields — is the half that is mechanically checkable. Checking it here is what
makes the norm self-enforcing rather than a sentence the next builder has to
remember, which is why it survives the thirtieth capability rather than the
sixth.

#30's A3 sits beside it because it fails the same way: a name that means two
types is a surface teaching a caller something false.

Each guard is asserted twice — that the shipped surface passes it, and that a
surface which violates it is REFUSED. The second half is the one that matters:
a check that has never refused anything is a claim, not a check, and it is the
half that fails silently in the direction of looking finished.
"""

from __future__ import annotations

import argparse
import copy
from typing import Any

import pytest

from bankmachine import mcp
from bankmachine.config import Config


def _definitions() -> list[dict[str, Any]]:
    return copy.deepcopy(mcp._tool_definitions())


#: These two cases never reach the datastore: `cmd_mcp` checks the tool surface
#: BEFORE it inspects the store, which is itself the ordering under test — a
#: malformed surface is a code defect with no answer worth serving, while an
#: unusable store is a data condition the server reports. The shared `config`
#: fixture supplies a valid Config whose store does not exist, which is exactly
#: the case that must still reach serving.


def test_the_shipped_surface_registers_without_refusal() -> None:
    """The guards run inside `_tool_definitions()`, so building it IS the check."""
    assert mcp._tool_definitions(), "the surface must register, and must not be empty"


def test_a_parameter_meaning_two_types_is_refused() -> None:
    """🔴 A3. The harm is at SELECTION, not at validation.

    An agent that learned `since` is a `YYYY-MM-DD` string carries that to the
    next tool. A surface where the same name is an integer somewhere else gets
    back a refusal that reads as the caller's mistake, and nothing in the error
    points at the two definitions that disagree.
    """
    definitions = _definitions()
    windowed = [d for d in definitions if "since" in d["inputSchema"].get("properties", {})]
    assert len(windowed) >= 2, "the collision needs two tools sharing a parameter name to exist"
    windowed[0]["inputSchema"]["properties"]["since"] = {"type": "integer"}

    with pytest.raises(mcp.ToolRegistrationError) as refusal:
        mcp._refuse_colliding_parameters(definitions)
    assert "'since'" in str(refusal.value)
    # Both offenders named: an error that says only "there is a collision"
    # leaves the reader grepping for which two tools disagree.
    assert windowed[0]["name"] in str(refusal.value)
    assert windowed[1]["name"] in str(refusal.value)


def test_two_tools_agreeing_on_a_parameters_type_is_not_a_collision() -> None:
    """The positive control. Sharing a name is the POINT of a vocabulary.

    Without this, a guard that refused every shared name would pass the case
    above while making the surface it protects impossible to build.
    """
    definitions = _definitions()
    shared = [d for d in definitions if "since" in d["inputSchema"].get("properties", {})]
    assert len(shared) >= 2
    assert {d["inputSchema"]["properties"]["since"]["type"] for d in shared} == {"string"}
    mcp._refuse_colliding_parameters(definitions)


def test_a_row_field_that_is_present_on_some_answers_is_refused() -> None:
    """🔴 Guardrail 1. This is the merge being made anyway, invisibly.

    A field present under one `group_by` and absent under another passes review
    because every individual answer looks fine. It is only wrong across the
    parameter space, which is exactly what a registration check can see and a
    reviewer cannot.
    """
    definitions = _definitions()
    aggregate = next(d for d in definitions if d["name"] == "money_summary")
    rows = aggregate["outputSchema"]["properties"]["rows"]["items"]
    rows["properties"]["only_sometimes"] = {"type": "string"}

    with pytest.raises(mcp.ToolRegistrationError) as refusal:
        mcp._refuse_optional_row_fields(definitions)
    assert "only_sometimes" in str(refusal.value)
    assert "money_summary" in str(refusal.value)


def test_a_nullable_row_field_is_permitted() -> None:
    """🔴 Nullable is fine; ABSENT is not — and the distinction is the guard.

    `["string", "null"]` is present and null, and a consumer reading it learns
    something. A guard that conflated the two would refuse the per-account
    coverage dates, whose null means NO TRANSACTION HAS EVER BEEN RECORDED —
    the most load-bearing null on this surface.
    """
    definitions = _definitions()
    accounts = next(d for d in definitions if d["name"] == "list_accounts")
    fields = accounts["outputSchema"]["properties"]["rows"]["items"]["properties"]
    assert fields["first_transaction_date"]["type"] == ["string", "null"]
    mcp._refuse_optional_row_fields(definitions)


def test_an_optional_field_nested_inside_a_row_block_is_refused() -> None:
    """The recursion, asserted rather than assumed.

    `additionalProperties` on the outer row says nothing about the shape of a
    value inside it, so a block is exactly where a check that stopped at the top
    level would let the defect through — and blocks are where the numbers a
    consumer sums live.
    """
    definitions = _definitions()
    coverage = next(d for d in definitions if d["name"] == "get_coverage_report")
    block = coverage["outputSchema"]["properties"]["rows"]["items"]["properties"][
        "source_breakdown"
    ]
    assert block["type"] == "object", "this case needs a row that actually carries a block"
    block["properties"]["smuggled"] = {"type": "integer"}

    with pytest.raises(mcp.ToolRegistrationError) as refusal:
        mcp._refuse_optional_row_fields(definitions)
    assert "smuggled" in str(refusal.value)
    assert "source_breakdown" in str(refusal.value), "the error must name where the field is"


def test_a_row_that_leaves_additional_properties_open_is_refused() -> None:
    """An open row cannot claim its absences are information.

    A key that reaches the wire without reaching the published schema is the
    drift this surface's strictness exists to make impossible; leaving the door
    open is the same defect stated as a permission.
    """
    definitions = _definitions()
    aggregate = next(d for d in definitions if d["name"] == "money_summary")
    aggregate["outputSchema"]["properties"]["rows"]["items"]["additionalProperties"] = True

    with pytest.raises(mcp.ToolRegistrationError) as refusal:
        mcp._refuse_optional_row_fields(definitions)
    assert "additionalProperties" in str(refusal.value)


def test_no_path_can_obtain_an_unvalidated_definition() -> None:
    """🔴 The guards live in the producer, not beside `serve`.

    This server has no startup 'registration' event: `tools/list`, the derived
    reference documents and every test build the surface by calling
    `_tool_definitions()`. A check anywhere else would be one some caller routes
    around, so the refusal has to sit in the one function that hands out a
    definition.
    """
    import inspect

    source = inspect.getsource(mcp._tool_definitions)
    assert "_refuse_colliding_parameters(definitions)" in source
    assert "_refuse_optional_row_fields(definitions)" in source


def test_an_undescribable_surface_refuses_at_startup_rather_than_mid_session(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 The refusal needs a channel, and without this one it had none.

    `ToolRegistrationError` calls itself a startup failure, but nothing built
    the definitions at startup: the earliest either guard could fire was the
    client's first `tools/list`, which sits outside `_handle`'s `try` and
    outside anything `serve` wraps — so the exception escaped the read loop and
    ENDED THE SESSION. That is the operator's tool vanishing mid-session, the
    one outcome this module's own comments name as worse than any wrong answer,
    and it would have arrived with no log line saying which tool caused it.

    Building the surface in `cmd_mcp` is what makes the docstring's claim true.
    Asserted by never reaching `serve`, and by the exit code being `2` — "could
    not run" — rather than `1`, which the scheduler reads as a degraded feed.
    """
    from bankmachine import mcp as mcp_module
    from bankmachine.cli.exit_codes import EXIT_ERROR

    served: list[object] = []
    monkeypatch.setattr(
        mcp_module,
        "serve",
        lambda *a, **k: served.append(a) or 0,  # type: ignore[func-returns-value]
    )

    def undescribable() -> list[dict[str, Any]]:
        raise mcp_module.ToolRegistrationError("money_summary's rows[] declares ['x']")

    monkeypatch.setattr(mcp_module, "_tool_definitions", undescribable)

    exit_code = mcp_module.cmd_mcp(config, argparse.Namespace())

    assert exit_code == EXIT_ERROR, "a surface that cannot be described is 'could not run'"
    assert served == [], "the read loop was entered, so the refusal would reach a client mid-call"


def test_a_describable_surface_still_reaches_the_read_loop(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The positive control: a guard that refused everything would pass the case above."""
    from bankmachine import mcp as mcp_module

    served: list[object] = []
    monkeypatch.setattr(
        mcp_module,
        "serve",
        lambda *a, **k: served.append(a) or 0,  # type: ignore[func-returns-value]
    )
    assert mcp_module.cmd_mcp(config, argparse.Namespace()) == 0
    assert served, "the real surface registers, so cmd_mcp must reach serving"
