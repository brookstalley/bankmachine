"""The grouped money aggregate: both directions, every grouping, per currency.

🔴 The tool this replaces filtered `amount_minor < 0`, so an inflow had no row
to appear in — unreachable rather than unaggregated (#20). The grouping is a
parameter rather than four tools because all four answer ONE row shape, which is
what `api-contract.md` § Direction's fourth norm draws a tool boundary on.
"""

from __future__ import annotations

import json
import re
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
from bankmachine.store.schema import accounts, transactions
from bankmachine.store.types import minor_units, now_utc
from test_mcp import _call, _every_tool_except, _seed, _tools_requiring


def _rows(config: Config, **arguments: Any) -> list[dict[str, Any]]:
    wire: dict[str, Any] = _call(config, "money_summary", arguments)["structuredContent"]
    return list(wire["rows"])


def _wire(config: Config, **arguments: Any) -> dict[str, Any]:
    result: dict[str, Any] = _call(config, "money_summary", arguments)["structuredContent"]
    return result


def _seed_every_flow_class(config: Config) -> None:
    """The shared fixture plus a real example of each class that is NOT spending.

    🔴 The shared `_seed` writes three rows that are all `external_spend`, so
    every case about the other two classes would pass over a store where they
    are simply absent -- green because nothing was classified rather than
    because the classification works.

    🔴 **Two ACCOUNTS, and two legs each, because that is what those classes now
    mean.** A row is `internal_transfer` only when the money is found on the
    other side, and `debt_service` only when the liability is one this store
    holds. A single transfer-shaped row with no counterparty is money that left
    the household, and seeding one would assert the opposite of the rule. So the
    savings account and the card are enrolled here, and each movement is written
    as the pair the aggregator really sends: money out of checking, money into
    the account that received it.

    Written through the real derivers like every other fixture here, so the
    pairing under test is the product's own rather than the test's.
    """
    now = now_utc()
    today = str(now.date())
    _seed(config)

    def account(source_id: str, name: str, kind: str, subtype: str) -> dict[str, Any]:
        return {
            "account_id": source_id,
            "name": name,
            "mask": "0000",
            "type": kind,
            "subtype": subtype,
            "balances": {
                "current": "0.00",
                "available": None,
                "limit": None,
                "iso_currency_code": "USD",
            },
        }

    def txn(
        index: int, source_account: str, amount: str, name: str, primary: str, detailed: str
    ) -> dict[str, Any]:
        return {
            "account_id": source_account,
            "transaction_id": f"flow-{index}",
            "amount": amount,
            "iso_currency_code": "USD",
            "date": today,
            "authorized_date": None,
            "pending": False,
            "pending_transaction_id": None,
            "name": name,
            "merchant_name": None,
            # 🔴 Both stated, never one derived from the other. The
            # aggregator sends both and they are not related by string surgery:
            # deriving `primary` here produced categories that exist in no
            # taxonomy and slipped past the guard that holds this tuple against
            # what the store contains.
            "personal_finance_category": {"primary": primary, "detailed": detailed},
        }

    with writer_connection(config) as conn:
        apply_response(
            conn,
            connection_id=1,
            endpoint=TRANSACTIONS_SYNC.path,
            # Positive amounts leave, as the aggregator sends them -- so the two
            # legs of one movement arrive with opposite signs.
            body=json.dumps(
                {
                    "accounts": [
                        account("acct-savings", "Plaid Saving", "depository", "savings"),
                        account("acct-card", "Plaid Card", "credit", "credit card"),
                    ],
                    "added": [
                        txn(
                            0,
                            "acct-1",
                            "400.00",
                            "Transfer to Saving",
                            "TRANSFER_OUT",
                            "TRANSFER_OUT_ACCOUNT_TRANSFER",
                        ),
                        txn(
                            1,
                            "acct-savings",
                            "-400.00",
                            "Transfer from Checking",
                            "TRANSFER_IN",
                            "TRANSFER_IN_ACCOUNT_TRANSFER",
                        ),
                        txn(
                            2,
                            "acct-1",
                            "300.00",
                            "Card Payment",
                            "LOAN_PAYMENTS",
                            "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
                        ),
                        txn(
                            3,
                            "acct-card",
                            "-300.00",
                            "Payment Received",
                            "LOAN_PAYMENTS",
                            "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
                        ),
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


def test_the_aggregate_reports_truncation_rather_than_leaving_it_unsaid(
    initialized_config: Config,
) -> None:
    """🔴 A CONTRACT CHANGE, and the reason the old contract could not hold.

    This tool used to carry no `truncation` block at all, on the reasoning that
    an aggregate is unpaginated by construction — bounded by its grouping rather
    than by a row cap, so the absence of the block was itself the statement that
    everything found was returned.

    That held only while the grouping bounded anything. Keyed on a merchant
    string that falls back to a per-transaction description, the group count
    approaches the TRANSACTION count: the payload grows without limit and
    `capped` reads false the whole way. An absence that means "nothing was cut"
    is worth having; an absence that means "nobody checked" is the shape this
    surface exists to refuse.

    So the block is present and truthful. On a small store nothing is cut and
    the block says so — which is strictly more than the old silence said,
    because it distinguishes a complete answer from an unexamined one.
    """
    _seed(initialized_config)
    wire = _call(initialized_config, "money_summary", {})["structuredContent"]
    assert "truncation" in wire, "the aggregate must say whether its group list was cut"
    assert wire["truncation"]["truncated"] is False, (
        "this store holds far fewer groups than the cap, so nothing was cut"
    )
    assert wire["truncation"]["returned"] == wire["truncation"]["matching"]
    assert "effective_window" in wire, "a windowed tool must say what it covered"


def test_a_capped_group_list_does_not_shrink_the_totals_beside_it(
    initialized_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 The hazard the cap creates, and the reason it is applied to the payload only.

    `totals` is summed from the group rows BY CONSTRUCTION — that identity is
    what makes the three flow classes add to the window's outflow, and it is
    also the proof the classification partitions the rows rather than dropping
    some. Cap the rows in SQL, or sum the totals from the capped list, and every
    total silently shrinks to the visible groups while the answer still looks
    complete.

    Driven by lowering the cap rather than by seeding hundreds of groups: the
    subject is what the cap does to the arithmetic, and a fixture large enough
    to trip the real cap would take far longer to say the same thing.
    """
    _seed(initialized_config)
    full = _call(initialized_config, "money_summary", {})["structuredContent"]
    assert len(full["rows"]) > 1, "the fixture must produce more than one group to cap"

    monkeypatch.setattr(query, "MAX_GROUPS", 1)
    capped = _call(initialized_config, "money_summary", {})["structuredContent"]

    assert len(capped["rows"]) == 1, "the cap did not bound the payload"
    assert capped["truncation"]["truncated"] is True
    assert capped["truncation"]["matching"] == full["truncation"]["matching"], (
        "the cap changed how many groups the answer says it found"
    )
    assert capped["totals"] == full["totals"], (
        "the totals shrank with the visible rows, so a capped answer under-reports the window "
        "while still reading as complete"
    )


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
    # 400.00 moved to savings, 300.00 paid to the enrolled card.
    assert by_class["external_spend"]["outflow_minor_units"] == 10140
    assert by_class["internal_transfer"]["outflow_minor_units"] == 40000
    assert by_class["debt_service"]["outflow_minor_units"] == 30000
    # 🔴 And money IN under the same class, which is the half a matched pair
    # makes true: both legs of one movement classify together, so the 400.00
    # arriving in savings is the same transfer as the 400.00 leaving checking
    # and the class nets to zero. That netting is the proof the money never
    # left the household -- under the old classifier the two legs could land in
    # different classes and the net said nothing.
    assert by_class["external_spend"]["inflow_minor_units"] == 25000
    assert by_class["internal_transfer"]["inflow_minor_units"] == 40000
    assert by_class["internal_transfer"]["net_minor_units"] == 0, (
        "a matched transfer must net to zero; a non-zero net means one leg was classified "
        "and the other was not, which is the overcount this class exists to remove"
    )
    assert by_class["debt_service"]["inflow_minor_units"] == 30000


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
        # 🔴 The DETAILED column, because that is the one the class is read from
        # now. Rewriting the primary category leaves the classification
        # untouched, so a test that mutated it would pass while proving nothing
        # about the fallback it names.
        conn.execute(
            transactions.update()
            .where(transactions.c.source_category_detailed == "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
            .values(source_category_detailed="SOMETHING_INVENTED_LATER")
        )
    by_class = {row["group_key"]: row for row in _rows(initialized_config, group_by="flow_class")}
    assert "debt_service" not in by_class
    # The card payment's OUT leg joins spending; its IN leg on the card joins
    # inflow, which is what an unclassified pair looks like from both sides.
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

    The checking account carries a row of every class, so without the split it
    was a single row whose outflow read 803.40 — a figure that is 700.00 of
    money that never left the household. Three rows is what makes that readable,
    and a caller who sums them blindly gets back exactly the number the split
    exists to separate.

    🔴 Scoped to that account rather than asserting it is the only one. The
    other two exist because a transfer needs somewhere to go: the class is only
    `internal_transfer` when the counterparty is enrolled, so a fixture with one
    account could not produce the row this test is about.
    """
    _seed_every_flow_class(initialized_config)
    rows = [row for row in _rows(initialized_config, group_by="account") if row["group_key"] == "1"]
    assert rows, "the checking account produced no row at all"
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


def test_no_surface_still_disclaims_what_the_classifier_now_establishes(
    initialized_config: Config,
) -> None:
    """🔴 The classifier matches a counterparty leg, so the old disclaimers are false.

    `internal_transfer` and `debt_service` both require the other side to be an
    account this store holds, and the window is measured on `ledger_date`. Text
    still saying the class "matches no counterparty leg", that it is "not
    verified against an enrolled counterparty", or that a window follows the
    posting date is now a claim BELOW what the classification establishes — and
    it understates, which the contract records as the direction that gets
    believed. An agent reading it discounts a figure that is sound.

    Asserted over every surface an agent can read, because the sentence lived in
    all of them and fixing the one a reviewer names is what buys a second round.
    🔴 That is not hypothetical here: the first pass at this corrected four
    surfaces, missed a fifth, and the guard added beside it was exact-case while
    the survivor was lowercase — so it passed over its own subject.
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
        # 🔴 INVERTED, and the inversion is the contract change. These two claims
        # were forbidden because the classifier could not support them: it read
        # the aggregator's label alone, so "never left the household" was false
        # of an ATM withdrawal and "already counted" was false of a mortgage.
        # Both are now true BY CONSTRUCTION -- each class requires a matched
        # counterparty leg on an enrolled account -- so what has to be absent is
        # the old disclaimer, which now understates a verified figure in the
        # direction this surface records as the one that gets believed.
        assert "matches no counterparty leg" not in text, (
            f"{name} still tells a client the class matches no counterparty leg, which is the "
            f"classifier this build replaced"
        )
        assert "not verified against an enrolled counterparty" not in text, (
            f"{name} still tells a client the class is unverified, so an agent will discount a "
            f"figure that is now established"
        )
        # 🔴 Case-INSENSITIVE, and the reason is a miss this guard already had:
        # it was written as an exact-case check, the surviving copy in the client
        # guide was lowercase, and the assertion passed over the very instance it
        # was added to catch. A guard that reads green against its own subject is
        # worse than no guard.
        # 🔴 The CLAIM, not the phrase. "posting date" appears legitimately --
        # `date` on a row IS the posting date and surfaces have to say so -- and
        # a blunt substring check would have forced that true sentence out to
        # stay green. What must be absent is the claim about the WINDOW.
        window_claim = re.compile(r"window is measured on the posting date", re.IGNORECASE)
        assert not window_claim.search(text), (
            f"{name} still says the window is measured on the posting date; it is measured on "
            f"`ledger_date`, which settlement does not move"
        )
        # And the positive, on the surfaces that EXPLAIN the window rather than
        # pointing at one that does: removing a false claim must not leave
        # silence, because an agent will supply the meaning the old prose
        # asserted. The output schema is a field list for an aggregate whose
        # rows carry no date column, and the handshake instructions deliberately
        # point at the reference documents instead of restating them -- neither
        # has anything to say here, so neither is asked to.
        if name in {"tool description", "resources", "client guide"}:
            assert "ledger_date" in text, (
                f"{name} no longer says the window is measured on the posting date and does not "
                f"say what it IS measured on"
            )


def test_every_surface_says_what_the_flow_classes_do_and_do_not_establish(
    initialized_config: Config,
) -> None:
    """Each surface says what the class now establishes, and what it still cannot.

    An agent that reads "internal_transfer" with nothing beside it supplies a
    meaning of its own, and the one it will reach for is the one the label
    suggests. So each surface has to say what the class now establishes — that
    the counterparty is an account this store holds — and, just as importantly,
    what it still cannot: an unmatched transfer-shaped row counts as spending,
    which is a fallback rather than a finding.
    """
    definition = next(d for d in mcp._tool_definitions() if d["name"] == "money_summary")
    resources = "\n".join(doc.text for doc in mcp._reference_documents())
    described = str(definition["description"])

    assert "THIS STORE HOLDS" in resources, (
        "the reference does not say the counterparty has to be an account this store holds, "
        "which is the whole of what the class now establishes"
    )
    assert "nobody enrolled is" in resources, (
        "the reference does not say a loan payment to an unenrolled lender is external spend"
    )
    assert "HOUSEHOLD BOUNDARY" in described, (
        "the tool does not say what question the class answers"
    )
    assert "partial" in described, (
        "the tool does not tell a caller how to see that the classifier fell back rather than "
        "concluded"
    )
    assert "falls back to `description`" in described, (
        "the merchant rollup does not say when it is really rolling up by description"
    )
    assert "ledger_date" in resources, "nothing says which date the window is measured on"


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
    with reader_connection(initialized_config) as conn:
        detailed = {
            str(row[0])
            for row in conn.execute(
                select(transactions.c.source_category_detailed).distinct()
            ).all()
            if row[0] is not None
        }
    # 🔴 The DETAILED vocabulary is checked too, and it is the one that matters
    # now: the class is read from that column, so a detailed name nobody
    # classified is the value that reaches `external_spend` unexamined. Checking
    # only the primary tuple would leave the deciding column unguarded.
    assert detailed, "the store held no detailed categories, so this checked nothing"
    undecided_detailed = detailed - set(query.KNOWN_SOURCE_CATEGORIES_DETAILED)
    assert not undecided_detailed, (
        f"{sorted(undecided_detailed)} reach `external_spend` through the fallback rather than "
        f"through a decision; classify them or record them in KNOWN_SOURCE_CATEGORIES_DETAILED"
    )
    undecided = present - set(query.KNOWN_SOURCE_CATEGORIES)
    assert not undecided, (
        f"{sorted(undecided)} reach `external_spend` through the fallback rather than through "
        f"a decision; classify them or record them in KNOWN_SOURCE_CATEGORIES"
    )


# --------------------------------------------------------------------------
# Accounts this store cannot denominate — excluded, and the exclusion named
# --------------------------------------------------------------------------


def _second_account(
    config: Config, *, balances: dict[str, Any], iso: str | None, unofficial: str | None = None
) -> None:
    """A second account on the same connection, plus one transaction on it.

    Written through `/transactions/sync`, which carries an `accounts` array and
    derives it with the same deriver `/accounts/get` uses. One body is therefore
    both halves of the case: the account whose unit is in question, and a row on
    it that a total would otherwise pick up.
    """
    now = now_utc()
    entry: dict[str, Any] = {
        "account_id": "acct-2",
        "transaction_id": "second-1",
        "amount": "77.00",
        "iso_currency_code": iso,
        "unofficial_currency_code": unofficial,
        "date": str(now.date()),
        "authorized_date": None,
        "pending": False,
        "pending_transaction_id": None,
        "name": "Second Account Purchase",
        "merchant_name": None,
        "personal_finance_category": {
            "primary": "GENERAL_MERCHANDISE",
            "detailed": "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES",
        },
    }
    with writer_connection(config) as conn:
        apply_response(
            conn,
            connection_id=1,
            endpoint=TRANSACTIONS_SYNC.path,
            body=json.dumps(
                {
                    "accounts": [
                        {
                            "account_id": "acct-2",
                            "name": "Unknown Unit",
                            "mask": "2222",
                            "type": "depository",
                            "subtype": "checking",
                            "balances": balances,
                        }
                    ],
                    "added": [entry],
                    "modified": [],
                    "removed": [],
                    "next_cursor": "cursor-second",
                    "has_more": False,
                    "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE",
                    "request_id": "req-second",
                }
            ).encode(),
            received_at=now,
            derivers=ALL_DERIVERS,
        )


def _rule_applied(wire: dict[str, Any]) -> list[dict[str, Any]]:
    return [warning for warning in wire["warnings"] if warning["kind"] == "rule-applied"]


def test_an_ordinary_store_carries_no_exclusion_warning_at_all(
    initialized_config: Config,
) -> None:
    """🔴 The control, and the reason the warning below is worth reading.

    `rule-applied` says rows were left out of THIS aggregate on purpose. A store
    where every account's unit is known excludes nothing, so it says nothing --
    and a reader can take the absence as the statement that the figures cover
    every account there is.
    """
    _seed(initialized_config)
    assert _rule_applied(_wire(initialized_config)) == []


def test_an_account_in_no_stated_currency_is_left_out_of_the_totals_and_named(
    initialized_config: Config,
) -> None:
    """🔴 Its rows derive, they are queryable, and no minor-units figure includes them.

    The account exists with `currency = NULL` -- the aggregator has not told us
    what unit it is in -- and its transactions derive normally, because they
    carry their own currency stated per row. What cannot happen is those amounts
    entering a total: an amount on an account whose unit is unknown cannot be
    added to a figure in a unit that is known, because the arithmetic would
    succeed and the result would mean nothing.

    So the rows are excluded and the answer SAYS SO, naming the account rather
    than a count -- the reader's next move is to go and look at that account.
    """
    _seed(initialized_config)
    baseline = _wire(initialized_config)
    _second_account(
        initialized_config,
        balances={
            "current": "50.00",
            "available": None,
            "limit": None,
            "iso_currency_code": None,
            "unofficial_currency_code": None,
        },
        iso="USD",
    )

    with reader_connection(initialized_config) as conn:
        held = {
            int(row[0]): row[1]
            for row in conn.execute(select(accounts.c.account_id, accounts.c.currency)).all()
        }
    excluded = [account_id for account_id, currency in held.items() if currency is None]
    assert len(excluded) == 1, "the account was skipped rather than created with a null unit"

    # It derived, and it is answerable — the cascade this fix ends.
    rows = _call(initialized_config, "query_transactions", {})["structuredContent"]["rows"]
    assert any(row["account_id"] == excluded[0] for row in rows), (
        "the account's transactions did not derive, so the exclusion below hides nothing"
    )

    wire = _wire(initialized_config)
    assert wire["totals"] == baseline["totals"], (
        "an account whose unit is unknown moved a minor-units total"
    )
    assert excluded[0] not in {row["group_key"] for row in _rows(initialized_config)}
    assert str(excluded[0]) not in {
        row["group_key"] for row in _rows(initialized_config, group_by="account")
    }
    warnings = _rule_applied(wire)
    assert len(warnings) == 1
    assert str(excluded[0]) in warnings[0]["detail"]
    assert "never stated one" in warnings[0]["detail"]


def test_an_account_whose_currency_has_no_known_exponent_is_named_with_its_code(
    initialized_config: Config,
) -> None:
    """🔴 The unit is known and the SCALE is not, which is the same exclusion.

    `0.04217` in a currency whose minor unit this build does not know cannot be
    stored exactly, and it is not stored approximately -- so the balance is
    refused and so is every transaction on the account. That leaves the account
    with no rows at all, which is precisely why the warning is computed from
    `accounts` rather than from the rows the answer returned: a scan of the rows
    would find nothing excluded and report nothing, and the operator would see
    an account that simply never appears in any figure.

    The code rides the detail, because "this account is in an unknown unit" and
    "this account is in BTC and we do not know its scale" send an operator to
    two different places.
    """
    _seed(initialized_config)
    baseline = _wire(initialized_config)
    _second_account(
        initialized_config,
        balances={
            "current": "0.04217",
            "available": None,
            "limit": None,
            "iso_currency_code": None,
            "unofficial_currency_code": "BTC",
        },
        iso=None,
        unofficial="BTC",
    )

    with reader_connection(initialized_config) as conn:
        held = {
            int(row[0]): row[1]
            for row in conn.execute(select(accounts.c.account_id, accounts.c.currency)).all()
        }
    excluded = [account_id for account_id, currency in held.items() if currency == "BTC"]
    assert len(excluded) == 1, "the account was skipped rather than kept with its stated unit"

    wire = _wire(initialized_config)
    assert wire["totals"] == baseline["totals"]
    warnings = _rule_applied(wire)
    assert len(warnings) == 1
    assert f"{excluded[0]} (BTC)" in warnings[0]["detail"]
    assert "minor unit this build does not know" in warnings[0]["detail"]


# --------------------------------------------------------------------------
# The scenario the review measured, answered as ruled
# --------------------------------------------------------------------------


def _seed_the_review_scenario(config: Config) -> None:
    """The six movements the review used to measure the overcount.

    $2,400 mortgage to a lender nobody enrolled, $300 from an ATM, $1,200 rent,
    $5,000 of card purchases, an $1,800 card payment, and a $6,000 paycheque.
    Only the card is enrolled besides checking, which is the whole point: every
    other counterparty is outside the household, so that money is gone.
    """
    now = now_utc()
    today = str(now.date())
    _seed(config)

    def account(source_id: str, name: str, kind: str, subtype: str) -> dict[str, Any]:
        return {
            "account_id": source_id,
            "name": name,
            "mask": "0000",
            "type": kind,
            "subtype": subtype,
            "balances": {
                "current": "0.00",
                "available": None,
                "limit": None,
                "iso_currency_code": "USD",
            },
        }

    def txn(
        index: int, source_account: str, amount: str, name: str, primary: str, detailed: str
    ) -> dict[str, Any]:
        return {
            "account_id": source_account,
            "transaction_id": f"review-{index}",
            "amount": amount,
            "iso_currency_code": "USD",
            "date": today,
            "authorized_date": None,
            "pending": False,
            "pending_transaction_id": None,
            "name": name,
            "merchant_name": None,
            "personal_finance_category": {"primary": primary, "detailed": detailed},
        }

    with writer_connection(config) as conn:
        apply_response(
            conn,
            connection_id=1,
            endpoint=TRANSACTIONS_SYNC.path,
            body=json.dumps(
                {
                    "accounts": [account("acct-card", "Plaid Card", "credit", "credit card")],
                    "added": [
                        txn(
                            0,
                            "acct-1",
                            "2400.00",
                            "Mortgage Co",
                            "LOAN_PAYMENTS",
                            "LOAN_PAYMENTS_MORTGAGE_PAYMENT",
                        ),
                        txn(
                            1,
                            "acct-1",
                            "300.00",
                            "ATM Withdrawal",
                            "TRANSFER_OUT",
                            "TRANSFER_OUT_WITHDRAWAL",
                        ),
                        txn(
                            2,
                            "acct-1",
                            "1200.00",
                            "Landlord ACH",
                            "TRANSFER_OUT",
                            "TRANSFER_OUT_ACCOUNT_TRANSFER",
                        ),
                        txn(
                            3,
                            "acct-card",
                            "5000.00",
                            "Card Purchases",
                            "GENERAL_MERCHANDISE",
                            "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES",
                        ),
                        txn(
                            4,
                            "acct-1",
                            "1800.00",
                            "Card Payment",
                            "LOAN_PAYMENTS",
                            "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
                        ),
                        txn(
                            5,
                            "acct-card",
                            "-1800.00",
                            "Payment Received",
                            "LOAN_PAYMENTS",
                            "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
                        ),
                        txn(
                            6,
                            "acct-1",
                            "-6000.00",
                            "Payroll",
                            "INCOME",
                            "INCOME_WAGES",
                        ),
                    ],
                    "modified": [],
                    "removed": [],
                    "next_cursor": "cursor-review",
                    "has_more": False,
                    "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE",
                    "request_id": "req-review",
                }
            ).encode(),
            received_at=now,
            derivers=ALL_DERIVERS,
        )


def test_the_reviews_scenario_answers_as_ruled(initialized_config: Config) -> None:
    """🔴 The acceptance is ARITHMETIC, and it is the reason this item existed.

    On the old classifier this scenario answered **"spent $5,000, income $0"**.
    Both numbers were wrong and both were wrong in the direction that gets
    believed: the mortgage, the ATM cash and the rent were excluded from
    spending as though the money had merely moved between the household's own
    accounts, and the paycheque was excluded from income for the same reason.

    What is true: $2,400 + $300 + $1,200 left the household, and so did the
    $5,000 of card purchases. The $1,800 card payment did NOT -- the card is
    enrolled, its purchases are already counted, and counting the payoff too is
    the double count `debt_service` exists to prevent. And $6,000 of wages
    arrived.
    """
    _seed_the_review_scenario(initialized_config)
    by_class = {row["group_key"]: row for row in _rows(initialized_config, group_by="flow_class")}

    spent = by_class["external_spend"]["outflow_minor_units"]
    # The shared `_seed` contributes 89.40 + 12.00 of its own spending.
    assert spent == 240000 + 30000 + 120000 + 500000 + 10140, (
        "spending must include the mortgage, the ATM cash, the rent and the card purchases -- "
        "every one of them money that left the household"
    )
    assert "internal_transfer" not in by_class, (
        "nothing here has a counterparty leg on an enrolled account, so no movement is internal"
    )
    assert by_class["debt_service"]["outflow_minor_units"] == 180000, (
        "the card payment is the one movement that stayed inside the household"
    )

    income = by_class["external_spend"]["inflow_minor_units"]
    assert income >= 600000, (
        "the paycheque is external value entering the household, not a transfer"
    )


# --------------------------------------------------------------------------
# The fallback notice, on the answer rather than in the function behind it
# --------------------------------------------------------------------------


def _unmatched_notices(wire: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        warning
        for warning in wire["warnings"]
        if warning["kind"] == "partial" and "transfer-shaped" in warning["detail"]
    ]


def test_an_answer_says_how_many_transfer_shaped_rows_fell_back_to_spending(
    initialized_config: Config,
) -> None:
    """🔴 Asserted on the ANSWER, because that is where the contract promises it.

    The store function behind this was already pinned, and that is not the same
    guarantee: the call site in `query` could be deleted with every one of those
    tests still green, while `api-contract.md` promises the warning and three
    served surfaces tell an agent to read it as the signal that the classifier
    fell back rather than concluded.

    The review scenario seeds ACH rent, which is transfer-shaped and has no
    counterparty here, so the notice must fire and must count it.

    🔴 It seeds an ATM withdrawal too, and that row is NOT counted — which is
    the distinction worth pinning. `TRANSFER_OUT_WITHDRAWAL` is not
    transfer-shaped at all: cash out of a machine is definitionally not a
    movement to another account the household holds, so it reaches
    `external_spend` by classification rather than by fallback. Counting it here
    would tell an agent the classifier was unsure about a row it was certain of.
    """
    _seed_the_review_scenario(initialized_config)

    notices = _unmatched_notices(_wire(initialized_config))

    assert len(notices) == 1, "the answer does not say the classifier fell back"
    assert "1 transfer-shaped" in notices[0]["detail"], (
        "the notice does not name how many rows fell back, which is the number an agent is "
        "told to read -- and an ATM withdrawal must not be among them"
    )
    assert "nobody enrolled" in notices[0]["detail"], (
        "the notice does not say the counterparty may simply be unenrolled, so a reader takes "
        "the fallback for a finding"
    )


def test_the_notice_counts_the_window_it_rides_and_not_the_store(
    initialized_config: Config,
) -> None:
    """A count true of the store and quoted on a window is a precise wrong number.

    The unmatched rows sit on today's date, so a window that ends well before
    them contains none — and the notice must either not fire or not count them.
    """
    _seed_the_review_scenario(initialized_config)

    notices = _unmatched_notices(_wire(initialized_config, since="2020-01-01", until="2020-12-31"))

    assert not notices, (
        "a window holding no transfer-shaped rows still carried a fallback count, so the "
        "figure describes the store rather than the answer beside it"
    )


def test_an_answer_whose_every_leg_pairs_carries_no_fallback_notice(
    initialized_config: Config,
) -> None:
    """🔴 Silence, so the ABSENCE of the notice is information too.

    An agent told nothing fell back can quote the spending figure without a
    caveat. That only works if a store whose transfers all matched stays quiet —
    a notice present on every answer is the one a reader learns to skip.
    """
    _seed_every_flow_class(initialized_config)

    assert not _unmatched_notices(_wire(initialized_config))
