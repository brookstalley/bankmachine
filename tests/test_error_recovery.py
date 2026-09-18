"""A refusal is the next turn's input: the caller can build the corrected call from it.

🔴 **Every case here retries from the FIELDS ALONE, and asserts the retry
succeeds.** That is the whole requirement (`api-contract.md` § `invalid_argument`
carries the correction as FIELDS), and it is the only assertion shape that can
fail for the right reason. A test that merely asserted the keys are present
would pass on a block that names the wrong argument, offers a value the tool
still refuses, or points at a tool that lists nothing -- every one of which is a
refusal a caller cannot act on, which is the state this work exists to end.

The retry is built by a helper that is DENIED the sentence, so a field the code
forgot cannot be quietly supplied from the prose by a test author who knew the
answer.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from bankmachine import envelope, mcp, mcp_resources
from bankmachine.config import Config
from test_mcp import _call, _seed, _seed_investments


def _error(result: dict[str, Any]) -> dict[str, Any]:
    """The refusal's error object, asserted to be one, read from the STRUCTURED half."""
    assert result["isError"] is True, "the call was expected to be refused and was not"
    error: dict[str, Any] = result["structuredContent"]["error"]
    return error


def _corrected(arguments: dict[str, Any], error: dict[str, Any]) -> dict[str, Any]:
    """The caller's arguments, corrected using ONLY the recovery fields.

    🔴 The `message` is deliberately never read here. This function is the agent
    the fields are written for: if it cannot build a valid call, neither can one.

    The order of the branches is the order of specificity -- a named set of
    values beats a bound, a bound beats dropping the argument -- and dropping is
    last because it is the only correction that changes the QUESTION rather than
    fixing the call.
    """
    corrected = dict(arguments)
    named = error["arguments"]
    assert named, "the refusal named no argument to correct"
    signature = set(error["required"]) | set(error["optional"])

    for argument in named:
        if argument not in signature:
            # Not an argument of this tool at all. There is nothing to correct
            # it to, so the corrected call is the one without it.
            corrected.pop(argument, None)
            continue
        if "valid_values" in error:
            values = error["valid_values"]
            assert values, "a closed set with no members leaves no corrected call to build"
            corrected[argument] = values[0]
        elif "example" in error and argument in error["example"]:
            corrected[argument] = error["example"][argument]
        elif "minimum" in error and _below(corrected.get(argument), error["minimum"]):
            corrected[argument] = error["minimum"]
        elif "maximum" in error and _above(corrected.get(argument), error["maximum"]):
            corrected[argument] = error["maximum"]
        elif "max_length" in error and isinstance(corrected.get(argument), str):
            corrected[argument] = corrected[argument][: error["max_length"]]
        else:
            corrected.pop(argument, None)
    return corrected


def _below(value: object, minimum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value < minimum


def _above(value: object, maximum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > maximum


def _retry(config: Config, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Refuse once, correct from the fields, and call again."""
    refusal = _call(config, tool, arguments)
    error = _error(refusal)
    assert error["code"] == "invalid_argument"
    answer = _call(config, tool, _corrected(arguments, error))
    assert answer["isError"] is False, (
        f"the corrected call was refused too: {answer['content'][0]['text']}"
    )
    return answer


# --------------------------------------------------------------------------
# One case per way a call can be wrong. Each RETRIES.
# --------------------------------------------------------------------------


def test_an_unknown_argument_is_corrected_by_dropping_it(initialized_config: Config) -> None:
    """The refusal names the key and the tool's signature, so the retry drops it.

    🔴 The dangerous version of this mistake is the one that is NOT refused: a
    misspelled `since` silently returns the all-time aggregate, which reads as
    the windowed answer that was asked for.
    """
    _seed(initialized_config)
    refusal = _call(initialized_config, "money_summary", {"sinceX": "2024-09-09"})
    error = _error(refusal)

    assert error["arguments"] == ["sinceX"]
    assert "sinceX" not in set(error["required"]) | set(error["optional"])
    assert set(error["optional"]) == {"group_by", "since", "until"}

    _retry(initialized_config, "money_summary", {"sinceX": "2024-09-09"})


def test_a_date_in_the_wrong_form_is_corrected_from_the_example(
    initialized_config: Config,
) -> None:
    _seed(initialized_config)
    refusal = _call(initialized_config, "money_summary", {"since": "August 2024"})
    error = _error(refusal)

    assert error["arguments"] == ["since"]
    assert error["example"] == {"since": mcp._DATE_FORM}

    _retry(initialized_config, "money_summary", {"since": "August 2024"})


def test_a_limit_past_the_cap_is_corrected_from_the_maximum(initialized_config: Config) -> None:
    _seed(initialized_config)
    refusal = _call(initialized_config, "query_transactions", {"limit": envelope.MAX_ROWS + 1})
    error = _error(refusal)

    assert error["arguments"] == ["limit"]
    assert error["maximum"] == envelope.MAX_ROWS
    assert error["minimum"] == 1

    answer = _retry(initialized_config, "query_transactions", {"limit": envelope.MAX_ROWS + 1})
    assert answer["structuredContent"]["rows"]


def test_a_limit_below_the_floor_is_corrected_from_the_minimum(initialized_config: Config) -> None:
    """🔴 `limit: 0` is refused rather than clamped, and the bound says what to send.

    A silent clamp served one row, which reads as a complete answer to a narrow
    question. The refusal is the point; the field is what makes it cheap.
    """
    _seed(initialized_config)
    refusal = _call(initialized_config, "query_transactions", {"limit": 0})
    assert _error(refusal)["minimum"] == 1

    _retry(initialized_config, "query_transactions", {"limit": 0})


def test_an_unknown_category_is_corrected_from_the_valid_values(
    initialized_config: Config,
) -> None:
    """The closed set is the store's own categories, so the retry picks one and works."""
    _seed(initialized_config)
    refusal = _call(initialized_config, "query_transactions", {"category": "TRAVL"})
    error = _error(refusal)

    assert error["arguments"] == ["category"]
    assert error["valid_values"], "the store holds categories, so the closed set cannot be empty"

    answer = _retry(initialized_config, "query_transactions", {"category": "TRAVL"})
    assert answer["structuredContent"]["rows"]


def test_an_unknown_investment_type_is_corrected_from_the_valid_values(
    initialized_config: Config,
) -> None:
    _seed(initialized_config)
    _seed_investments(initialized_config)
    refusal = _call(initialized_config, "query_investment_transactions", {"investment_type": "byu"})
    error = _error(refusal)

    assert error["arguments"] == ["investment_type"]
    assert error["valid_values"]

    _retry(initialized_config, "query_investment_transactions", {"investment_type": "byu"})


def test_an_unknown_account_is_corrected_by_calling_the_tool_the_refusal_names(
    initialized_config: Config,
) -> None:
    """🔴 The ids are the STORE's, so the refusal routes instead of listing.

    The retry follows the route: call the named tool, take an id it reports, ask
    again. Nothing here reads the sentence, and nothing here knows in advance
    which ids the fixture seeded.
    """
    _seed(initialized_config)
    refusal = _call(initialized_config, "query_transactions", {"account_id": 9999})
    error = _error(refusal)

    assert error["arguments"] == ["account_id"]
    assert error["valid_values_from"] == "list_accounts"
    assert "valid_values" not in error, (
        "the ids are data, not a published set; offering a list here would go stale"
    )

    listing = _call(initialized_config, error["valid_values_from"])
    assert listing["isError"] is False
    account_id = listing["structuredContent"]["rows"][0]["account_id"]

    answer = _call(initialized_config, "query_transactions", {"account_id": account_id})
    assert answer["isError"] is False


def test_a_transposed_window_names_both_bounds(initialized_config: Config) -> None:
    """Both, because either one may be the one the caller meant to change."""
    _seed(initialized_config)
    refusal = _call(
        initialized_config, "money_summary", {"since": "2024-06-30", "until": "2024-01-01"}
    )
    error = _error(refusal)

    assert set(error["arguments"]) == {"since", "until"}
    assert set(error["arguments"]) <= set(error["optional"])

    swapped = {"since": "2024-01-01", "until": "2024-06-30"}
    assert _call(initialized_config, "money_summary", swapped)["isError"] is False


def test_an_unusable_cursor_is_corrected_by_omitting_it(initialized_config: Config) -> None:
    """One correction for every way a cursor can be wrong, because there is only one."""
    _seed(initialized_config)
    arguments = {"cursor": "not-a-cursor-this-server-issued"}
    error = _error(_call(initialized_config, "query_transactions", arguments))

    assert error["arguments"] == ["cursor"]
    assert "cursor" in error["optional"]
    assert "valid_values" not in error

    _retry(initialized_config, "query_transactions", arguments)


def test_an_over_long_search_is_corrected_from_the_max_length(initialized_config: Config) -> None:
    _seed(initialized_config)
    arguments = {"search": "x" * (envelope.MAX_SEARCH_LENGTH + 1)}
    error = _error(_call(initialized_config, "query_transactions", arguments))

    assert error["arguments"] == ["search"]
    assert error["max_length"] == envelope.MAX_SEARCH_LENGTH

    _retry(initialized_config, "query_transactions", arguments)


def test_an_unknown_grouping_is_corrected_from_the_valid_values(
    initialized_config: Config,
) -> None:
    _seed(initialized_config)
    refusal = _call(initialized_config, "money_summary", {"group_by": "merchnat"})
    error = _error(refusal)

    assert error["arguments"] == ["group_by"]
    assert error["valid_values"]

    _retry(initialized_config, "money_summary", {"group_by": "merchnat"})


# --------------------------------------------------------------------------
# The block itself: what rides, where it rides, and what does not carry it
# --------------------------------------------------------------------------


def test_the_error_object_rides_the_text_a_client_forwards(initialized_config: Config) -> None:
    """🔴 MEASURED, not assumed: acceptance rounds 2 and 3 both recorded that a real
    client forwarded only the error TEXT, and that `structuredContent.error.code`
    never reached the model. Fields that ride only the structured half are fields
    the agent they were written for cannot read.

    The two halves are compared rather than each checked, because two copies of
    one payload is exactly the shape that drifts.
    """
    _seed(initialized_config)
    result = _call(initialized_config, "money_summary", {"sinceX": "2024-09-09"})

    from_text = json.loads(result["content"][0]["text"])
    assert from_text == result["structuredContent"]
    assert from_text["error"]["message"], "the sentence is still there for the human"
    assert from_text["error"]["arguments"] == ["sinceX"]


def test_a_refusal_points_at_the_document_that_teaches_recovery(
    initialized_config: Config,
) -> None:
    """The URI is machine-followable, and the server actually serves it."""
    _seed(initialized_config)
    error = _error(_call(initialized_config, "money_summary", {"sinceX": "2024-09-09"}))

    assert error["see"] == mcp_resources.REFUSALS_URI
    served = {document.uri for document in mcp_resources.documents(mcp._tool_definitions())}
    assert error["see"] in served


def test_a_failure_with_no_corrected_call_carries_no_recovery_fields(
    initialized_config: Config,
) -> None:
    """🔴 `internal_error` has nothing for the caller to correct, so it offers nothing.

    A block of empty fields on a failure nobody can act on is what teaches a
    reader to stop reading the block on the failures they CAN act on.
    """
    _seed(initialized_config)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            mcp, "_dispatch_tool", _raise(RuntimeError("something under the query layer"))
        )
        result = _call(initialized_config, "money_summary", {})

    error = _error(result)
    assert error["code"] == "internal_error"
    assert set(error) == {"code", "message"}


def _raise(exc: Exception) -> Any:
    def fail(*_args: object, **_kwargs: object) -> Any:
        raise exc

    return fail


def test_every_recovery_field_is_described_where_a_consumer_reads(
    initialized_config: Config,
) -> None:
    """🔴 Two descriptions, compared: the emitted block, the contract, and the document.

    A field added to `Recovery` and described nowhere would reach the wire
    undocumented -- the same defect the published-wire guard exists for, one
    surface over. Walked from what is actually EMITTED rather than from the
    dataclass, so a key the boundary adds by hand is covered too.
    """
    _seed(initialized_config)
    emitted: set[str] = set()
    for tool, arguments in (
        ("money_summary", {"sinceX": "1"}),
        ("money_summary", {"since": "August 2024"}),
        ("money_summary", {"group_by": "merchnat"}),
        ("query_transactions", {"limit": 0}),
        ("query_transactions", {"category": "TRAVL"}),
        ("query_transactions", {"account_id": 9999}),
        ("query_transactions", {"search": "x" * (envelope.MAX_SEARCH_LENGTH + 1)}),
    ):
        emitted |= set(_error(_call(initialized_config, tool, arguments)))
    emitted -= {"code", "message"}
    assert emitted, "no refusal emitted a recovery field, so this compared nothing"

    documented = {name for name, _ in mcp_resources._RECOVERY_FIELDS}
    assert emitted <= documented, f"{sorted(emitted - documented)} reach the wire undescribed"
