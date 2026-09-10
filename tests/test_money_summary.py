"""The grouped money aggregate: both directions, every grouping, per currency.

🔴 The tool this replaces filtered `amount_minor < 0`, so an inflow had no row
to appear in — unreachable rather than unaggregated (#20). The grouping is a
parameter rather than four tools because all four answer ONE row shape, which is
what `api-contract.md` § Direction's fourth norm draws a tool boundary on.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from bankmachine import mcp, query
from bankmachine.config import Config
from bankmachine.connector import TRANSACTIONS_SYNC
from bankmachine.derivers import ALL_DERIVERS
from bankmachine.store.derivation import apply_response
from bankmachine.store.engine import reader_connection, writer_connection
from bankmachine.store.schema import transactions
from bankmachine.store.types import minor_units, now_utc
from test_mcp import _call, _every_tool_except, _seed, _tools_requiring


def _rows(config: Config, **arguments: Any) -> list[dict[str, Any]]:
    wire: dict[str, Any] = _call(config, "money_summary", arguments)["structuredContent"]
    return list(wire["rows"])


def _wire(config: Config, **arguments: Any) -> dict[str, Any]:
    result: dict[str, Any] = _call(config, "money_summary", arguments)["structuredContent"]
    return result


def _seed_every_flow_class(config: Config) -> None:
    """The shared fixture plus one row of each class that is NOT external spend.

    🔴 The shared `_seed` writes three rows that are all `external_spend`, so
    every case about the other two classes would pass over a store where they
    are simply absent -- green because nothing was classified rather than
    because the classification works. These are the categories the mapping
    names, written through the real derivers like every other fixture here, so
    the CHECK on provenance is satisfied by the product rather than by the test.
    """
    now = now_utc()
    _seed(config)

    def txn(index: int, amount: str, name: str, category: str) -> dict[str, Any]:
        return {
            "account_id": "acct-1",
            "transaction_id": f"flow-{index}",
            "amount": amount,
            "iso_currency_code": "USD",
            "date": str(now.date()),
            "authorized_date": None,
            "pending": False,
            "pending_transaction_id": None,
            "name": name,
            "merchant_name": None,
            "personal_finance_category": {"primary": category, "detailed": category},
        }

    with writer_connection(config) as conn:
        apply_response(
            conn,
            connection_id=1,
            endpoint=TRANSACTIONS_SYNC.path,
            # Positive amounts leave, as the aggregator sends them.
            body=json.dumps(
                {
                    "accounts": [],
                    "added": [
                        txn(0, "400.00", "Transfer to Saving", "TRANSFER_OUT"),
                        txn(1, "-150.00", "Transfer from Saving", "TRANSFER_IN"),
                        txn(2, "300.00", "Card Payment", "LOAN_PAYMENTS"),
                    ],
                    "modified": [],
                    "removed": [],
                    "next_cursor": "cursor-flow",
                    "has_more": False,
                    "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE",
                    "request_id": "req-flow",
                }
            ).encode(),
            received_at=now,
            derivers=ALL_DERIVERS,
        )


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


def test_each_flow_class_is_reachable_over_rows_that_exercise_it(
    initialized_config: Config,
) -> None:
    """🔴 All three classes, over rows that are actually of that class.

    The mapping is the whole of #18: `LOAN_PAYMENTS` measured as ~100%
    credit-card payoff, which double-counts purchases already booked under the
    categories they were spent in, and `TRANSFER_IN`/`TRANSFER_OUT` measured as
    61% of the two-year total — the holder moving their own money. A test over
    the shared fixture alone would pass with the other two classes never
    produced, which is green for the wrong reason.
    """
    _seed_every_flow_class(initialized_config)
    by_class = {row["group_key"]: row for row in _rows(initialized_config, group_by="flow_class")}
    assert set(by_class) == {"external_spend", "internal_transfer", "debt_service"}
    # Money out under each, from the seeded amounts: 89.40 + 12.00 spent,
    # 400.00 transferred out, 300.00 paid to a card.
    assert by_class["external_spend"]["outflow_minor_units"] == 10140
    assert by_class["internal_transfer"]["outflow_minor_units"] == 40000
    assert by_class["debt_service"]["outflow_minor_units"] == 30000
    # And money in stays reachable within a class rather than being filtered
    # away: the payroll credit is external, the returned 150.00 is a transfer.
    assert by_class["external_spend"]["inflow_minor_units"] == 25000
    assert by_class["internal_transfer"]["inflow_minor_units"] == 15000


def test_a_re_categorisation_cannot_move_a_transfer_into_spending(
    initialized_config: Config,
) -> None:
    """🔴 The class reads the SOURCE column, never `category_override`.

    An override says what a transaction was *for*; the flow class says whose
    money moved and in which direction. If an override could reclassify, the
    61% overcount would come back through the back door — silently, and in the
    direction that inflates, which is the direction nobody questions.
    """
    _seed_every_flow_class(initialized_config)
    with writer_connection(initialized_config) as conn:
        conn.execute(
            transactions.update()
            .where(transactions.c.source_category_primary == "TRANSFER_OUT")
            .values(category_override="GROCERIES")
        )
    by_class = {row["group_key"]: row for row in _rows(initialized_config, group_by="flow_class")}
    assert by_class["internal_transfer"]["outflow_minor_units"] == 40000
    # The override still moves the CATEGORY, so the test proves the two axes are
    # independent rather than that the override was ignored everywhere.
    categories = {row["group_key"] for row in _rows(initialized_config, group_by="category")}
    assert "GROCERIES" in categories


def test_an_unrecognised_category_falls_to_external_spend(initialized_config: Config) -> None:
    """The conservative direction, on this surface's own principle.

    An overcount gets questioned; an undercount gets believed. So a category
    this mapping has never seen counts as money that left the household, and a
    future taxonomy addition shows up as spending rather than disappearing.
    """
    _seed_every_flow_class(initialized_config)
    with writer_connection(initialized_config) as conn:
        conn.execute(
            transactions.update()
            .where(transactions.c.source_category_primary == "LOAN_PAYMENTS")
            .values(source_category_primary="SOMETHING_INVENTED_LATER")
        )
    by_class = {row["group_key"]: row for row in _rows(initialized_config, group_by="flow_class")}
    assert "debt_service" not in by_class
    assert by_class["external_spend"]["outflow_minor_units"] == 10140 + 30000


@pytest.mark.parametrize("grouping", ["category", "merchant", "account", "month", "flow_class"])
def test_every_row_of_every_grouping_carries_a_real_flow_class(
    initialized_config: Config, grouping: str
) -> None:
    """🔴 Present and true under every `group_by`, never optional.

    `api-contract.md` § Direction's fourth norm merges tools only where one
    strict row schema covers every parameter value with no optional field. The
    class is not a function of the group — an account holds a transfer and a
    coffee — so it is a grouping dimension under all five values rather than a
    field that would be meaningful under one and a guess under the others.
    """
    _seed_every_flow_class(initialized_config)
    rows = _rows(initialized_config, group_by=grouping)
    assert rows
    for row in rows:
        assert row["flow_class"] in {"external_spend", "internal_transfer", "debt_service"}


def test_one_account_returns_a_row_per_class_rather_than_one_conflated_row(
    initialized_config: Config,
) -> None:
    """The cost of the finer grain, asserted as the behaviour it buys.

    Every seeded row is on one account, so before the split this account was a
    single row whose outflow read 803.40 — a figure that is 700.00 of money that
    never left. Three rows is what makes that readable, and a caller who sums
    them blindly gets back exactly the number the split exists to separate.
    """
    _seed_every_flow_class(initialized_config)
    rows = _rows(initialized_config, group_by="account")
    assert {row["group_key"] for row in rows} == {"1"}
    assert {row["flow_class"] for row in rows} == set(query.FLOW_CLASSES)


def test_the_three_totals_partition_the_windows_outflow(initialized_config: Config) -> None:
    """🔴 By construction, which is also the proof nothing was dropped.

    The classification KEEPS every row — the owner's ruling on #18 is classify,
    do not filter — so the three class totals must add to the total outflow over
    the same rows. If they ever did not, a row had been silently discarded, and
    an undercount is the failure this surface cannot afford because nobody
    questions it.
    """
    _seed_every_flow_class(initialized_config)
    wire = _wire(initialized_config, group_by="category")
    (totals,) = wire["totals"]
    assert totals["currency"] == "USD"
    assert sum(row["outflow_minor_units"] for row in wire["rows"]) == sum(
        totals[f"{flow}_outflow_minor_units"] for flow in query.FLOW_CLASSES
    )
    assert totals["external_spend_outflow_minor_units"] == 10140
    assert totals["internal_transfer_outflow_minor_units"] == 40000
    assert totals["debt_service_outflow_minor_units"] == 30000


def test_the_totals_carry_the_whole_windows_inflow_and_outflow(
    initialized_config: Config,
) -> None:
    """🔴 "How much went out" had no protected figure, so an agent had to sum rows.

    The three class outflows are a decomposition of a number that was never
    published, and the guidance beside them said to quote only the smallest of
    the three — so a question about money leaving the household got an answer
    that excluded a mortgage payment and an ATM withdrawal. The whole-window
    figure is the one that answers the question as asked, and the classes sit
    beside it saying what it is made of.

    The income side has the same hole one direction over: with no
    `inflow_minor_units` here, an agent asked what came in sums the rows itself
    and reads refunds as income.
    """
    _seed_every_flow_class(initialized_config)
    wire = _wire(initialized_config, group_by="category")
    (totals,) = wire["totals"]

    assert totals["outflow_minor_units"] == sum(row["outflow_minor_units"] for row in wire["rows"])
    assert totals["inflow_minor_units"] == sum(row["inflow_minor_units"] for row in wire["rows"])


def test_the_three_class_outflows_add_up_to_the_windows_outflow(
    initialized_config: Config,
) -> None:
    """🔴 The identity that proves the classes PARTITION the window rather than sample it.

    Asserted inside the block rather than across the block and the rows, because
    that is where a reader checks it: `external_spend + internal_transfer +
    debt_service` must be `outflow_minor_units` exactly, and a shortfall would
    mean a row was classified into nothing at all — an undercount, in the
    direction this surface says nobody questions.
    """
    _seed_every_flow_class(initialized_config)
    for entry in _wire(initialized_config)["totals"]:
        assert (
            sum(entry[f"{flow}_outflow_minor_units"] for flow in query.FLOW_CLASSES)
            == entry["outflow_minor_units"]
        ), entry


def test_the_payload_does_not_claim_a_transfer_never_left_or_a_debt_was_already_counted(
    initialized_config: Config,
) -> None:
    """🔴 The classifier reads one aggregator category and matches no counterparty.

    So `internal_transfer` means *the aggregator called it a transfer* — which in
    this very store includes the payroll deposit — and `debt_service` covers
    mortgage, auto and student-loan payments, which are money out rather than the
    settlement of purchases counted elsewhere. Text asserting otherwise is a
    claim about the household that the classification does not establish, and it
    understates spending: the direction the contract records as the one that gets
    believed.

    Asserted over every surface an agent can read, because the sentence was in
    all of them and fixing the one a reviewer names is what buys a second round.
    """
    _seed_every_flow_class(initialized_config)
    definition = next(d for d in mcp._tool_definitions() if d["name"] == "money_summary")
    schemas = json.dumps(definition["outputSchema"])
    surfaces = {
        "tool description": str(definition["description"]),
        "output schema": schemas,
        "instructions": mcp._instructions(initialized_config),
        "resources": "\n".join(doc.text for doc in mcp._reference_documents()),
        "client guide": (
            Path(__file__).resolve().parents[1] / "docs" / "connecting-an-mcp-client.md"
        ).read_text(encoding="utf-8"),
    }

    for name, text in surfaces.items():
        assert "never left" not in text, f"{name} still claims a transfer never left the household"
        assert "already counted" not in text, (
            f"{name} still claims debt service settles purchases already counted"
        )


def test_every_surface_says_what_the_flow_classes_do_and_do_not_establish(
    initialized_config: Config,
) -> None:
    """The positive half: removing the false claim must not leave silence.

    An agent that reads "internal_transfer" with nothing beside it supplies a
    meaning of its own, and the one it will reach for is the one the label
    suggests. So each surface has to say what the class is derived from — a
    category the aggregator assigned, with no counterparty leg matched — and
    which date a window is measured on, since a hold that settles later moves
    between periods.
    """
    definition = next(d for d in mcp._tool_definitions() if d["name"] == "money_summary")
    resources = "\n".join(doc.text for doc in mcp._reference_documents())

    assert "not verified against an enrolled counterparty" in resources
    assert "mortgage, auto or student-loan payment is money out" in resources
    assert "categorised as a transfer by the aggregator" in str(definition["description"])
    assert "falls back to `description`" in str(definition["description"]), (
        "the merchant rollup does not say when it is really rolling up by description"
    )
    assert "POSTING date" in str(definition["description"]), (
        "nothing says which date the window is measured on"
    )


def test_the_totals_are_identical_under_every_grouping(initialized_config: Config) -> None:
    """The same window is the same money however it is sliced.

    This is what makes the block quotable without a second call: an agent that
    asked by category and an agent that asked by month must be able to state the
    same spending figure, or the field is a trap that depends on how the
    question happened to be phrased.
    """
    _seed_every_flow_class(initialized_config)
    answers = [_wire(initialized_config, group_by=g)["totals"] for g in query.GROUPINGS]
    assert all(totals == answers[0] for totals in answers), answers


def test_totals_are_per_currency_and_never_summed_across_them(
    initialized_config: Config,
) -> None:
    """🔴 The owner's ruling reaches the totals block, not only the rows.

    One integer spanning two currencies is not a wrong number, it is not a
    number — and a headline figure is exactly where that mistake would be
    quoted, because it is the field an agent reads instead of the rows.
    """
    _seed_every_flow_class(initialized_config)
    with writer_connection(initialized_config) as conn:
        conn.execute(
            transactions.update()
            .where(transactions.c.transaction_id == 1)
            .values(currency="EUR", amount_minor=minor_units(-500))
        )
    totals = {entry["currency"]: entry for entry in _wire(initialized_config)["totals"]}
    assert set(totals) == {"USD", "EUR"}
    assert totals["EUR"]["external_spend_outflow_minor_units"] == 500
    # 🔴 Present and zero, not absent: "nothing serviced a debt in euros" is an
    # answer, and a missing key would be indistinguishable from a class this
    # tool forgot to compute.
    assert totals["EUR"]["debt_service_outflow_minor_units"] == 0


def test_only_the_aggregate_carries_a_totals_block(initialized_config: Config) -> None:
    """A key's ABSENCE is information, so it may not appear where it is untrue.

    `api-contract.md` fixes that for `effective_window` and `truncation`, and
    the same rule governs this one: a totals block on `list_accounts` would
    claim that tool classifies money, and its absence here says it does not.
    """
    _seed(initialized_config)
    assert "totals" in _wire(initialized_config)
    # Derived, so a tool added later is held to this claim without anyone
    # remembering to add it — the omission that left `get_coverage_report`
    # outside five of these loops at once.
    for tool in _every_tool_except(*_tools_requiring("totals")):
        wire = _call(initialized_config, tool, {})["structuredContent"]
        assert "totals" not in wire, tool


def test_an_unreadable_store_still_carries_an_empty_totals_block(config: Config) -> None:
    """🔴 Present and empty, never dropped — the rule the coverage zeroes follow.

    Losing the key exactly when the store cannot be read moves the wire shape at
    the moment a consumer is trying to work out what went wrong, and one
    branching on the key would conclude this tool does not classify money at
    all.
    """
    wire = _wire(config)
    assert wire["totals"] == []
    assert [w["kind"] for w in wire["warnings"]] == ["partial"]


def test_a_window_that_holds_no_money_carries_an_empty_totals_block(
    initialized_config: Config,
) -> None:
    """The empty-window path is a different one from the unreadable-store path.

    🔴 An empty list rather than three zeroes, because with no rows there is no
    CURRENCY to report them in — and inventing a USD zero would be this surface
    asserting a fact about a store it just told you it read nothing from. The
    key is still present: a caller branching on it learns the tool classifies
    money, and the `window_starts_before_coverage` warning beside it is what
    says why there is none.
    """
    _seed_every_flow_class(initialized_config)
    wire = _wire(initialized_config, since="2001-01-01", until="2001-12-31")
    assert wire["rows"] == []
    assert wire["totals"] == []
    assert wire["warnings"], "an empty answer must say why it is empty"


def test_the_classified_categories_are_ones_the_taxonomy_actually_contains() -> None:
    """🔴 A typo in either set classifies nothing, and looks exactly like a rule.

    `_DEBT_SERVICE_CATEGORIES = {"LOAN_PAYMENT"}` — singular — would send every
    card payoff to `external_spend` and inflate the one figure this tool tells
    an agent to quote, with every other test still green because they seed the
    same misspelling from the same mental model. Held against the recorded
    vocabulary instead, which is the shape `PROVENANCE_SOURCES` already uses one
    file away.
    """
    classified = query._INTERNAL_TRANSFER_CATEGORIES | query._DEBT_SERVICE_CATEGORIES
    unknown = classified - set(query.KNOWN_SOURCE_CATEGORIES)
    assert not unknown, f"these are classified but are not in the taxonomy: {sorted(unknown)}"
    # The two classes are exclusive: a value in both would have its class decided
    # by the order of the CASE arms rather than by a decision anyone recorded.
    assert not (query._INTERNAL_TRANSFER_CATEGORIES & query._DEBT_SERVICE_CATEGORIES)


def test_no_category_reaches_external_spend_without_a_decision(
    initialized_config: Config,
) -> None:
    """🔴 The `else_` branch is silent by construction — this is what breaks the silence.

    A category nobody classified is indistinguishable from one deliberately left
    as spending: both arrive as `external_spend`, with no warning, no log and no
    test going red. So the vocabulary is held against what the store actually
    contains, and a value entering the data without a decision being made about
    it fails here instead of quietly inflating
    `external_spend_outflow_minor_units`.
    """
    _seed_every_flow_class(initialized_config)
    with reader_connection(initialized_config) as conn:
        present = {
            str(row[0])
            for row in conn.execute(select(transactions.c.source_category_primary).distinct()).all()
            if row[0] is not None
        }
    assert present, "the store held no categories, so this checked nothing"
    undecided = present - set(query.KNOWN_SOURCE_CATEGORIES)
    assert not undecided, (
        f"{sorted(undecided)} reach `external_spend` through the fallback rather than through "
        f"a decision; classify them or record them in KNOWN_SOURCE_CATEGORIES"
    )
