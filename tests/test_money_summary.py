"""The grouped money aggregate: both directions, every grouping, per currency.

🔴 The tool this replaces filtered `amount_minor < 0`, so an inflow had no row
to appear in — unreachable rather than unaggregated (#20). The grouping is a
parameter rather than four tools because all four answer ONE row shape, which is
what `api-contract.md` § Direction's fourth norm draws a tool boundary on.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from bankmachine.config import Config
from bankmachine.store.engine import writer_connection
from bankmachine.store.schema import transactions
from bankmachine.store.types import minor_units
from test_mcp import _call, _seed


def _rows(config: Config, **arguments: Any) -> list[dict[str, Any]]:
    wire: dict[str, Any] = _call(config, "money_summary", arguments)["structuredContent"]
    return list(wire["rows"])


def test_grouping_by_month_keys_rows_by_year_and_month(initialized_config: Config) -> None:
    """`posted_date` is `YYYY-MM-DD` text that sorts as a date, so the month is
    its first seven characters — no date arithmetic and no dialect function to
    disagree about.
    """
    _seed(initialized_config)
    rows = _rows(initialized_config, group_by="month")
    assert rows
    for row in rows:
        assert len(row["group_key"]) == 7 and row["group_key"][4] == "-", row["group_key"]
        assert row["group_label"] == row["group_key"]


def test_grouping_by_account_keys_on_the_id_and_labels_with_the_name(
    initialized_config: Config,
) -> None:
    """🔴 The key is what a caller can act on; the label is for reading.

    Both are present on every row of every grouping, which is what lets one
    strict schema cover all four — the merge would not have been permitted
    otherwise.
    """
    _seed(initialized_config)
    rows = _rows(initialized_config, group_by="account")
    assert rows
    for row in rows:
        assert row["group_key"].isdigit(), row["group_key"]
        assert row["group_label"] and not row["group_label"].isdigit()


def test_every_grouping_returns_the_same_row_shape(initialized_config: Config) -> None:
    """🔴 The property the norm's first guardrail requires, asserted directly.

    A merge is permitted only when one strict row schema covers every parameter
    value with no optional fields. If a grouping ever returns a different key
    set, the published `outputSchema` is wrong for it and a validating client
    rejects an honest answer.
    """
    _seed(initialized_config)
    shapes = {
        grouping: {frozenset(row) for row in _rows(initialized_config, group_by=grouping)}
        for grouping in ("category", "merchant", "account", "month")
    }
    assert all(len(s) == 1 for s in shapes.values()), "one grouping returned ragged rows"
    assert len({next(iter(s)) for s in shapes.values()}) == 1, shapes


def test_an_unknown_grouping_is_refused_by_name_rather_than_defaulted(
    initialized_config: Config,
) -> None:
    """🔴 Refused, never silently defaulted to `category`.

    A fallback would answer a different question from the one asked and the
    caller could not tell — the same failure as a misspelled `since` returning
    the all-time aggregate.
    """
    _seed(initialized_config)
    result = _call(initialized_config, "money_summary", {"group_by": "quarter"})
    assert result["isError"] is True
    message = json.dumps(result)
    assert "quarter" in message, "the refusal does not name what was rejected"
    assert "category" in message, "the refusal does not say what is accepted"


def test_a_grouping_of_the_wrong_type_is_refused_at_the_boundary(
    initialized_config: Config,
) -> None:
    """Narrowed before it can reach the query layer, so the caller gets a
    sentence naming the field rather than a type error from inside a dispatch.
    """
    _seed(initialized_config)
    result = _call(initialized_config, "money_summary", {"group_by": 3})
    assert result["isError"] is True
    assert "group_by" in json.dumps(result)


def test_the_advertised_groupings_are_the_ones_the_query_layer_accepts(
    initialized_config: Config,
) -> None:
    """🔴 Derived on both sides rather than restated here.

    A value advertised in the `enum` that the query layer refuses is a promise
    the surface cannot keep, and it would only surface when a caller tried it.
    """
    from bankmachine import mcp, query

    advertised = next(d for d in mcp._tool_definitions() if d["name"] == "money_summary")[
        "inputSchema"
    ]["properties"]["group_by"]["enum"]
    assert set(advertised) == set(query.GROUPINGS)
    for grouping in advertised:
        assert (
            _call(initialized_config, "money_summary", {"group_by": grouping}).get("isError")
            is not True
        ), grouping


def test_rows_are_reported_per_currency_and_never_summed_across_them(
    initialized_config: Config,
) -> None:
    """🔴 The owner's ruling, made once for every aggregate tool: carry currency,
    refuse to sum across it.

    Conversion is explicitly not chosen — it needs a rate source, a rate date
    policy and somewhere to record that decision, which is a different scope
    from carrying a field. So a window spanning two currencies returns two rows,
    and a caller who wants one number has to make the conversion decision
    themselves rather than having it made silently for them.
    """
    _seed(initialized_config)
    # The sandbox is single-currency, so the second currency is written directly
    # rather than derived. That limitation is recorded in the build plan's
    # assumptions; what is exercised here is the GROUPING, which is this repo's.
    with writer_connection(initialized_config) as conn:
        conn.execute(
            transactions.update()
            .where(transactions.c.transaction_id == 1)
            .values(currency="EUR", amount_minor=minor_units(-500))
        )
    rows = _rows(initialized_config, group_by="category")
    currencies = {row["currency"] for row in rows}
    assert currencies == {"USD", "EUR"}, currencies
    # 🔴 The same category in two currencies is two rows, not one summed row.
    eur = [r for r in rows if r["currency"] == "EUR"]
    assert len(eur) == 1 and eur[0]["outflow_minor_units"] == 500


def test_the_aggregate_still_carries_no_truncation_block(initialized_config: Config) -> None:
    """An aggregate is unpaginated by contract, bounded by its grouping rather
    than a row cap — so the absence of `truncation` is the statement that it
    returned everything it found.
    """
    _seed(initialized_config)
    wire = _call(initialized_config, "money_summary", {})["structuredContent"]
    assert "truncation" not in wire
    assert "effective_window" in wire, "a windowed tool must say what it covered"


@pytest.mark.parametrize("grouping", ["category", "merchant", "account", "month"])
def test_gross_and_net_agree_in_every_grouping(initialized_config: Config, grouping: str) -> None:
    """🔴 By construction, not by coincidence.

    `net` is the signed sum; `inflow` and `outflow` are its two halves as
    positive magnitudes. If they ever disagree, one of the three is unusable and
    a caller has no way to know which.
    """
    _seed(initialized_config)
    for row in _rows(initialized_config, group_by=grouping):
        assert row["net_minor_units"] == row["inflow_minor_units"] - row["outflow_minor_units"], row
