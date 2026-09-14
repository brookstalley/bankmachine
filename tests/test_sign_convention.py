"""The per-connection sign-convention check — AC-14.1 through AC-14.6.

🔴 **This file carries both of AC-14.3's controls, and the positive one is the
point.** A check with no observed go-red is not coverage: this repository has
twice shipped a guard whose failing case existed only in an argument (#46, #47).
So the negative control — a feed signed the way the product's one measured
connection is signed — has to stay quiet, and the positive control — a feed
signed the other way — has to fire, in the same file, over stores built the same
way.

🔴 **Every fixture goes through the real derivers**, like every other fixture
here. The schema enforces provenance with a CHECK, and a hand-inserted row would
either encode this file's assumptions about that constraint or fail on it. It
matters more than usual for this check: the *whole subject* is the connector's
unconditional negation, so a fixture that wrote `amount_minor` directly would be
this file asserting the sign it wanted to see instead of exercising the code path
that produces one. Fixtures state what the AGGREGATOR sent; the product decides
what gets stored.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from bankmachine import envelope, query, signs
from bankmachine.config import Config
from bankmachine.connector import ACCOUNTS_GET, TRANSACTIONS_SYNC
from bankmachine.derivers import ALL_DERIVERS
from bankmachine.store.derivation import apply_response
from bankmachine.store.engine import reader_connection, writer_connection
from bankmachine.store.schema import connections, institutions, transactions
from bankmachine.store.types import now_utc

#: One of the five declared categories, used wherever a case needs a judged row
#: and does not care which category it is.
_JUDGED = "FOOD_AND_DRINK"

#: The kind an inverted connection rides out on. Spelled here rather than
#: imported from a constant in `signs`, because the module doesn't have one:
#: `tests/preferences/test_the_warning_vocabulary_is_closed.py` requires every
#: `Caveat(...)` in the tree to name its kind as a literal so an `ast` scan can
#: check it against the closed vocabulary before a validating client does. Two
#: independent spellings that must agree is the point: this file fails if the
#: emitter changes its kind, and that scan fails if it changes to one the
#: vocabulary does not declare.
_KIND = "sign_convention_unverified"

#: 🔴 The sign an aggregator that obeys the ONE measured convention sends
#: (`api-notes-plaid.md` §17: a purchase arrives POSITIVE), and the sign one
#: that does not would send. The product negates unconditionally, so the first
#: stores negative and the second stores positive. Named rather than spelled
#: `"12.00"` / `"-12.00"` at twenty call sites, because the whole file turns on
#: which of the two a fixture is speaking.
_CONFORMING_PURCHASE = "12.00"
_INVERTED_PURCHASE = "-12.00"


def _accounts_body(source_account_id: str, item_id: str) -> bytes:
    return json.dumps(
        {
            "accounts": [
                {
                    "account_id": source_account_id,
                    "name": "Checking",
                    "mask": "0000",
                    "type": "depository",
                    "subtype": "checking",
                    "balances": {
                        "current": "110.94",
                        "available": "100.00",
                        "limit": None,
                        "iso_currency_code": "USD",
                    },
                }
            ],
            "item": {"item_id": item_id},
            "request_id": f"req-accounts-{item_id}",
        }
    ).encode()


def _sync_body(
    source_account_id: str,
    entries: list[tuple[str, str, str, date]],
    *,
    cursor: str,
) -> bytes:
    """A sync page. `entries` are `(id, aggregator amount, category, date)`.

    The description is a constant here on purpose: nothing in this check may
    read it, and a fixture that varied it alongside the category would leave the
    two indistinguishable as explanations for any result.
    """
    return json.dumps(
        {
            "accounts": [],
            "added": [
                {
                    "account_id": source_account_id,
                    "transaction_id": entry_id,
                    "amount": amount,
                    "iso_currency_code": "USD",
                    "date": str(posted),
                    "authorized_date": None,
                    "pending": False,
                    "pending_transaction_id": None,
                    "name": "Corner Store",
                    "merchant_name": None,
                    "personal_finance_category": {"primary": category, "detailed": category},
                }
                for entry_id, amount, category, posted in entries
            ],
            "modified": [],
            "removed": [],
            "next_cursor": cursor,
            "has_more": False,
            "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE",
            "request_id": f"req-sync-{cursor}",
        }
    ).encode()


def _enroll(config: Config, *, institution: str, item_id: str) -> int:
    """One institution and one connection, returning the local connection id."""
    now = now_utc()
    with writer_connection(config) as conn:
        institution_pk = conn.execute(
            institutions.insert().values(
                source_institution_id=f"ins-{item_id}",
                name=institution,
                first_seen_at=now,
                last_seen_at=now,
            )
        ).inserted_primary_key
        assert institution_pk is not None
        connection_pk = conn.execute(
            connections.insert().values(
                institution_id=int(institution_pk[0]),
                source_connection_id=item_id,
                credential_ref=f"connection:sandbox:{item_id}",
                capabilities="[]",
                requested_history_days=730,
                granted_history_days=730,
                status="active",
                last_success_at=now,
                last_error_code=None,
                last_error_at=None,
                enrolled_at=now,
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key
        assert connection_pk is not None
        connection_id = int(connection_pk[0])
        apply_response(
            conn,
            connection_id=connection_id,
            endpoint=ACCOUNTS_GET.path,
            body=_accounts_body(f"acct-{item_id}", item_id),
            received_at=now,
            derivers=ALL_DERIVERS,
            replay_passes=(),
        )
    return connection_id


def _feed(
    config: Config,
    connection_id: int,
    *,
    item_id: str,
    entries: list[tuple[str, str, str, date]],
    cursor: str = "cursor-1",
) -> None:
    """One sync page of `entries` onto an already-enrolled connection."""
    with writer_connection(config) as conn:
        apply_response(
            conn,
            connection_id=connection_id,
            endpoint=TRANSACTIONS_SYNC.path,
            body=_sync_body(f"acct-{item_id}", entries, cursor=cursor),
            received_at=now_utc(),
            derivers=ALL_DERIVERS,
            replay_passes=(),
        )


def _spend(
    item_id: str, count: int, amount: str, *, category: str = _JUDGED, on: date | None = None
) -> list[tuple[str, str, str, date]]:
    """`count` identical purchases, one per day, as the aggregator would send them."""
    posted = on or now_utc().date()
    return [
        (f"{item_id}-{category}-{amount}-{index}", amount, category, posted - timedelta(days=index))
        for index in range(count)
    ]


def _conforming(config: Config, *, item_id: str = "item-a", count: int = 20) -> int:
    """🔴 The NEGATIVE control: a feed signed the way the measured one is.

    Twenty purchases, sent positive as `api-notes-plaid.md` §17 measured them,
    stored negative by the product's unconditional negation. This is the shape
    the live sandbox store carries at scale — 219 rows across the five declared
    categories on 2026-09-09, **219 of them negative and none positive** — and
    a check that fired on it would be unusable from its first day.
    """
    connection_id = _enroll(config, institution="First Platypus Bank", item_id=item_id)
    _feed(
        config,
        connection_id,
        item_id=item_id,
        entries=_spend(item_id, count, _CONFORMING_PURCHASE),
    )
    return connection_id


def _inverted(config: Config, *, item_id: str = "item-b", count: int = 20) -> int:
    """🔴 The POSITIVE control: a second institution signing its feed the other way.

    Identical in every respect except the sign the aggregator sent, which is
    exactly the tier-3 risk AC-14.2 exists for: the negation is a global
    constant applied to every feed, and institution B is under no obligation to
    obey institution A's convention. Stored positive, in categories where money
    does not arrive.

    🔴 This makes no claim about what any real aggregator does. It stipulates an
    input and checks what THIS system does with it, which is the line
    `discovery-production-data-semantics.md` draws between a legitimate fixture
    and one that asserts the outside world and then verifies against its own
    assertion. Whether a real institution B is signed this way is AC-14.8's, and
    AC-14.8 is the operator's.
    """
    connection_id = _enroll(config, institution="Second Platypus Bank", item_id=item_id)
    _feed(
        config,
        connection_id,
        item_id=item_id,
        entries=_spend(item_id, count, _INVERTED_PURCHASE),
    )
    return connection_id


def _measurements(config: Config) -> dict[int, signs.ConnectionSignConvention]:
    with reader_connection(config) as conn:
        return {m.connection_id: m for m in signs.measure(conn)}


def _caveats(config: Config, **window: Any) -> list[envelope.Caveat]:
    with reader_connection(config) as conn:
        return signs.caveats(conn, **window)


def test_the_negative_control_is_consistent_and_says_nothing(
    initialized_config: Config,
) -> None:
    """🔴 AC-14.3's negative control: the measured baseline's shape stays quiet.

    Both halves are asserted, because they fail separately. A verdict of
    `consistent` with a caveat beside it would put an unactionable warning on
    every answer over a healthy store; no caveat with a verdict of
    `undetermined` would mean the check never ran and nothing said so.
    """
    connection_id = _conforming(initialized_config)
    measurement = _measurements(initialized_config)[connection_id]
    assert measurement.verdict == "consistent"
    assert (measurement.rows_negative, measurement.rows_positive) == (20, 0)
    assert measurement.positive_share == 0.0
    assert _caveats(initialized_config) == []


def test_the_positive_control_is_reported(initialized_config: Config) -> None:
    """🔴 AC-14.2 and AC-14.3's positive control: an inverted feed goes red.

    This is the case whose absence makes a check cover nothing, so it asserts
    the whole finding rather than its existence: the kind a consumer branches
    on, and the connection an operator can act on.
    """
    _conforming(initialized_config)
    inverted_id = _inverted(initialized_config)

    measurement = _measurements(initialized_config)[inverted_id]
    assert measurement.verdict == "inverted"
    assert (measurement.rows_negative, measurement.rows_positive) == (0, 20)

    found = _caveats(initialized_config)
    assert [caveat.kind for caveat in found] == [_KIND]
    assert found[0].connection_id == inverted_id
    assert found[0].institution == "Second Platypus Bank"


def test_the_finding_names_the_connection_and_offers_no_correction(
    initialized_config: Config,
) -> None:
    """🔴 AC-14.4. The system refuses and names the connection.

    Auto-inverting a suspect feed is a heuristic that fails in the direction
    that UNDERSTATES spending, which is the argument that decided #18 and #20.
    Asserted where it can actually be checked — on the stored rows, which must
    be exactly what they were before the check ran and looked at them.
    """
    inverted_id = _inverted(initialized_config)

    def stored() -> list[int]:
        with reader_connection(initialized_config) as conn:
            return [
                int(row[0])
                for row in conn.execute(
                    select(transactions.c.amount_minor).order_by(transactions.c.transaction_id)
                ).all()
            ]

    before = stored()
    assert before and all(amount > 0 for amount in before)

    found = _caveats(initialized_config)
    assert len(found) == 1
    assert str(inverted_id) in found[0].detail
    assert "Second Platypus Bank" in found[0].detail

    assert stored() == before, "the check corrected a sign instead of reporting it"


def test_a_connection_below_the_sample_floor_is_undetermined_rather_than_consistent(
    initialized_config: Config,
) -> None:
    """🔴 The two answers this product exists to keep apart.

    One row short of the floor, every one of them stored positive — the most
    suspicious shape a small sample can take — and the answer is still "we could
    not tell", never "we checked and it is fine". A share over seven rows is not
    a distribution, and a check that pronounced on one would be inventing
    confidence from the sample size it happened to get.
    """
    connection_id = _enroll(initialized_config, institution="Small Bank", item_id="item-small")
    _feed(
        initialized_config,
        connection_id,
        item_id="item-small",
        entries=_spend("item-small", signs.MINIMUM_JUDGEABLE_ROWS - 1, _INVERTED_PURCHASE),
    )
    measurement = _measurements(initialized_config)[connection_id]
    assert measurement.rows_judged == signs.MINIMUM_JUDGEABLE_ROWS - 1
    assert measurement.positive_share == 1.0
    assert measurement.verdict == "undetermined"
    assert _caveats(initialized_config) == []


def test_one_more_row_crosses_the_floor_and_the_same_feed_is_reported(
    initialized_config: Config,
) -> None:
    """The floor is a floor, not a wall: the case above with one row added.

    Paired with it deliberately. A floor set too high silences the check
    forever, and nothing but the transition itself distinguishes "correctly
    withheld" from "never fires".
    """
    connection_id = _enroll(initialized_config, institution="Small Bank", item_id="item-small")
    _feed(
        initialized_config,
        connection_id,
        item_id="item-small",
        entries=_spend("item-small", signs.MINIMUM_JUDGEABLE_ROWS, _INVERTED_PURCHASE),
    )
    assert _measurements(initialized_config)[connection_id].verdict == "inverted"
    assert [caveat.kind for caveat in _caveats(initialized_config)] == [_KIND]


def test_an_exact_tie_is_undetermined(initialized_config: Config) -> None:
    """Half positive is not a feed this check has cleared.

    Calling a 50/50 split `consistent` would be a statement the evidence does
    not support, in the direction of silence — and silence about a plausible
    wrong number is the defect class this whole surface exists to refuse.
    """
    connection_id = _enroll(initialized_config, institution="Split Bank", item_id="item-tie")
    _feed(
        initialized_config,
        connection_id,
        item_id="item-tie",
        entries=(
            _spend("item-tie", 6, _CONFORMING_PURCHASE) + _spend("item-tie", 6, _INVERTED_PURCHASE)
        ),
    )
    measurement = _measurements(initialized_config)[connection_id]
    assert (measurement.rows_negative, measurement.rows_positive) == (6, 6)
    assert measurement.verdict == "undetermined"


def test_a_connection_with_nothing_to_judge_still_appears(initialized_config: Config) -> None:
    """🔴 Absence from the measurement would read as "cleared" on the surface.

    A connection whose rows are all in categories this check does not judge has
    no verdict to give, and it says so. Dropping it from the list instead would
    make it indistinguishable from a connection that passed.
    """
    connection_id = _enroll(initialized_config, institution="Transfers Only", item_id="item-tx")
    _feed(
        initialized_config,
        connection_id,
        item_id="item-tx",
        entries=_spend("item-tx", 12, "-400.00", category="TRANSFER_IN"),
    )
    measurement = _measurements(initialized_config)[connection_id]
    assert measurement.rows_judged == 0
    assert measurement.positive_share is None
    assert measurement.verdict == "undetermined"


def test_a_category_that_legitimately_carries_both_signs_is_not_judged(
    initialized_config: Config,
) -> None:
    """🔴 Why the declared set is a set and not "every spending category".

    `TRAVEL` was measured carrying both signs legitimately — 24 charges against
    24 refunds — and `INCOME` and `TRANSFER_IN` are inflows by name. A
    connection whose every positive row sits in those categories is conforming,
    and a check that judged them would fire on a healthy feed and be switched
    off within a week.
    """
    connection_id = _conforming(initialized_config, count=10)
    _feed(
        initialized_config,
        connection_id,
        item_id="item-a",
        cursor="cursor-2",
        entries=(
            _spend("item-a", 12, _INVERTED_PURCHASE, category="TRAVEL")
            + _spend("item-a", 12, _INVERTED_PURCHASE, category="INCOME")
            + _spend("item-a", 12, _INVERTED_PURCHASE, category="TRANSFER_IN")
        ),
    )
    measurement = _measurements(initialized_config)[connection_id]
    assert (measurement.rows_negative, measurement.rows_positive) == (10, 0)
    assert measurement.verdict == "consistent"
    assert _caveats(initialized_config) == []


def test_the_verdict_does_not_move_when_every_description_is_rewritten(
    initialized_config: Config,
) -> None:
    """🔴 AC-14.6. Free-text description is never a sign oracle.

    The settled case is the sandbox's own payroll row: it reads "ACH Electronic
    Credit" and the aggregator categorises it `TRANSFER_OUT` in **both**
    structured fields, so a check that read the word "Credit" would overrule the
    only structured evidence there is. That adjudication has been re-derived
    three times and this test exists so there is no fourth.

    Behavioural rather than a grep for the column name: descriptions are
    rewritten to the most misleading text available on a store whose verdict is
    already known, and the verdict does not move. A later refactor that started
    reading `description` would fail here whatever it named its variables.
    """
    connection_id = _conforming(initialized_config)
    before = _measurements(initialized_config)[connection_id]

    with writer_connection(initialized_config) as conn:
        conn.execute(
            transactions.update().values(
                description="ACH Electronic Credit", merchant_name="Deposit"
            )
        )

    after = _measurements(initialized_config)[connection_id]
    assert after == before
    assert after.verdict == "consistent"
    assert _caveats(initialized_config) == []


def test_the_measurement_ignores_a_local_recategorisation(initialized_config: Config) -> None:
    """`category_override` is local intent; it never moves a row into or out of the judged set.

    An override says what a transaction was FOR. Letting it decide which rows
    this check judges would let a re-categorisation switch the check off for a
    connection, quietly and from the side.
    """
    connection_id = _inverted(initialized_config)
    with writer_connection(initialized_config) as conn:
        conn.execute(transactions.update().values(category_override="TRAVEL"))
    assert _measurements(initialized_config)[connection_id].verdict == "inverted"


def test_a_removed_row_leaves_the_distribution(initialized_config: Config) -> None:
    """Soft-deleted rows are excluded, as they are from every other read.

    A removed transaction is not part of the feed's current distribution, and
    counting it would let a reversal history outvote what the connection
    actually holds.
    """
    connection_id = _inverted(initialized_config, count=10)
    with writer_connection(initialized_config) as conn:
        conn.execute(transactions.update().values(removed_at=now_utc()))
    measurement = _measurements(initialized_config)[connection_id]
    assert measurement.rows_judged == 0
    assert measurement.verdict == "undetermined"


def test_the_verdict_is_judged_over_all_history_even_when_the_window_is_narrow(
    initialized_config: Config,
) -> None:
    """🔴 The judgment reads every row; only the WARNING is scoped to the window.

    A window holding two of an inverted connection's four hundred rows must not
    answer "not enough rows to judge" about a connection the store has four
    hundred rows of evidence about. Narrowing the measurement to the window
    would make the check quietest exactly where a caller asked the narrowest
    question.
    """
    today = now_utc().date()
    connection_id = _enroll(initialized_config, institution="Second Platypus Bank", item_id="b")
    _feed(
        initialized_config,
        connection_id,
        item_id="b",
        entries=(
            _spend("b", 20, _INVERTED_PURCHASE, on=today - timedelta(days=200))
            + _spend("b", 2, _INVERTED_PURCHASE, on=today, category="PERSONAL_CARE")
        ),
    )
    found = _caveats(initialized_config, since=today - timedelta(days=1), until=today)
    assert [caveat.kind for caveat in found] == [_KIND]
    assert "22" in found[0].detail, "the finding quoted the window's rows, not the feed's"


def test_no_warning_about_a_connection_the_window_did_not_draw_on(
    initialized_config: Config,
) -> None:
    """🔴 AC-14.5 is about an aggregate computed OVER a flagged connection.

    A request-scoped kind that fires regardless of what the request touched is
    the failure `envelope.py` records at length: the same true sentence on four
    unrelated questions, which teaches a reader to skip the field. An aggregate
    that drew nothing from the inverted connection is not an aggregate computed
    over it.
    """
    today = now_utc().date()
    _conforming(initialized_config)
    inverted_id = _enroll(initialized_config, institution="Second Platypus Bank", item_id="b")
    _feed(
        initialized_config,
        inverted_id,
        item_id="b",
        entries=_spend("b", 20, _INVERTED_PURCHASE, on=today - timedelta(days=400)),
    )
    assert _measurements(initialized_config)[inverted_id].verdict == "inverted"
    assert _caveats(initialized_config, since=today - timedelta(days=30), until=today) == []
    assert _caveats(initialized_config) != []


def test_the_kind_is_one_the_contract_already_declares(initialized_config: Config) -> None:
    """The emitter and the closed vocabulary cannot drift to two spellings.

    `test_the_warning_vocabulary_is_closed.py` holds the wire to the tuple;
    this holds the producer to it, so a kind invented here fails at the source
    rather than at whichever answer first carried it.
    """
    assert _KIND in envelope.REQUEST_SCOPED_KINDS
    assert _KIND not in envelope.CONNECTION_SCOPED_KINDS


def test_every_verdict_the_check_can_return_is_a_declared_one(
    initialized_config: Config,
) -> None:
    """🔴 The published enum is what a consumer branches on.

    Driven over the three stores that produce the three verdicts rather than
    read off the constant: a value the code can emit and the schema does not
    name is rejected by a validating client, and the constant agreeing with
    itself would not notice.
    """
    _conforming(initialized_config, item_id="item-a")
    _inverted(initialized_config, item_id="item-b")
    _enroll(initialized_config, institution="Quiet Bank", item_id="item-quiet")
    verdicts = {m.verdict for m in _measurements(initialized_config).values()}
    assert verdicts == set(signs.VERDICTS)


def test_pipeline_health_reports_the_verdict_on_every_connection_row(
    initialized_config: Config,
) -> None:
    """The verification surface carries the measurement, including the quiet answers.

    🔴 `undetermined` reaches the wire. A tool that published only the two
    confident verdicts would leave "we could not tell" looking exactly like
    "nothing to report", on the one surface built to tell them apart.
    """
    conforming_id = _conforming(initialized_config, item_id="item-a")
    inverted_id = _inverted(initialized_config, item_id="item-b")
    quiet_id = _enroll(initialized_config, institution="Quiet Bank", item_id="item-quiet")

    answer = query.pipeline_health(initialized_config)
    rows = {int(row["connection_id"]): row for row in answer.rows}

    assert rows[conforming_id]["sign_convention"] == "consistent"
    assert rows[conforming_id]["sign_convention_rows_judged"] == 20
    assert rows[conforming_id]["sign_convention_rows_positive"] == 0

    assert rows[inverted_id]["sign_convention"] == "inverted"
    assert rows[inverted_id]["sign_convention_rows_positive"] == 20

    assert rows[quiet_id]["sign_convention"] == "undetermined"
    assert rows[quiet_id]["sign_convention_rows_judged"] == 0

    assert [caveat.connection_id for caveat in answer.warnings if caveat.kind == _KIND] == [
        inverted_id
    ]


def test_pipeline_health_is_silent_about_a_healthy_store(initialized_config: Config) -> None:
    """The other half of the negative control, at the surface a caller sees."""
    _conforming(initialized_config)
    answer = query.pipeline_health(initialized_config)
    assert [caveat for caveat in answer.warnings if caveat.kind == _KIND] == []


@pytest.mark.parametrize(
    "judged, positive, expected",
    [
        (0, 0, "undetermined"),
        (signs.MINIMUM_JUDGEABLE_ROWS - 1, 0, "undetermined"),
        (signs.MINIMUM_JUDGEABLE_ROWS, 0, "consistent"),
        (signs.MINIMUM_JUDGEABLE_ROWS, signs.MINIMUM_JUDGEABLE_ROWS // 2, "undetermined"),
        (signs.MINIMUM_JUDGEABLE_ROWS, signs.MINIMUM_JUDGEABLE_ROWS // 2 + 1, "inverted"),
        (signs.MINIMUM_JUDGEABLE_ROWS, signs.MINIMUM_JUDGEABLE_ROWS, "inverted"),
    ],
)
def test_the_verdict_boundaries(judged: int, positive: int, expected: str) -> None:
    """Every boundary of the rule, stated once against the two constants.

    A unit test beside the store-backed ones rather than instead of them: the
    stores prove the measurement, and this proves the arithmetic at the exact
    row counts where it changes its mind, which a fixture would need six more
    stores to reach.
    """
    measurement = signs.ConnectionSignConvention(
        connection_id=1,
        institution="Bank",
        rows_negative=judged - positive,
        rows_positive=positive,
        rows_zero=0,
    )
    assert measurement.verdict == expected


def test_a_zero_amount_row_is_counted_but_never_judged(initialized_config: Config) -> None:
    """A zero carries no direction, so it joins neither side of the share.

    Counted rather than dropped: a connection whose judged count is smaller than
    its row count has a reason, and a reader who cannot see the zeroes cannot
    tell that reason from a category filter having eaten the rows.
    """
    connection_id = _conforming(initialized_config, count=10)
    _feed(
        initialized_config,
        connection_id,
        item_id="item-a",
        cursor="cursor-zero",
        entries=_spend("item-a", 4, "0.00"),
    )
    measurement = _measurements(initialized_config)[connection_id]
    assert (measurement.rows_negative, measurement.rows_positive) == (10, 0)
    assert measurement.rows_zero == 4
    assert measurement.rows_judged == 10
    assert measurement.verdict == "consistent"


# ---------------------------------------------------------------------------
# AC-14.5 — the finding reaching an aggregate answer.
#
# 🔴 These tests exist because this criterion crossed a delegation boundary.
# The producer and the aggregate it rides on were built by two agents who could
# not see each other, and the call between them was made by neither. A
# requirement split that way is the one that gets silently dropped, so it is
# pinned from the aggregate's side -- through `money_summary`, over the wire,
# not by asserting that a function is called.
# ---------------------------------------------------------------------------


def test_an_aggregate_over_a_flagged_connection_says_so(initialized_config: Config) -> None:
    """AC-14.5: no aggregate is computed over a flagged connection in silence.

    Goes through `money_summary` rather than through `signs.caveats`, because
    what this criterion protects is the ANSWER. A producer that returns the
    right list and a summary that never calls it look identical from the
    producer's own tests, and that is exactly the state the tree was in before
    this wiring landed.
    """
    _conforming(initialized_config)
    _inverted(initialized_config)

    answer = query.money_summary(initialized_config, group_by="category")
    kinds = [caveat.kind for caveat in answer.warnings]

    assert "sign_convention_unverified" in kinds, (
        "a spending total drawing on a connection measured as inverted said nothing about "
        "it; the figure's DIRECTION is in question and the answer carried no sign of that"
    )


def test_the_flagged_connection_is_named_in_the_aggregate_answer(
    initialized_config: Config,
) -> None:
    """The warning has to be actionable, and a connection nobody can name is not.

    AC-14.4 forbids correcting the feed, so naming it is the entire remedy on
    offer: the operator cannot act on "one of your connections is inverted".
    """
    _conforming(initialized_config)
    inverted_id = _inverted(initialized_config)

    answer = query.money_summary(initialized_config, group_by="category")
    flagged = [c for c in answer.warnings if c.kind == "sign_convention_unverified"]

    assert len(flagged) == 1, f"expected exactly one flagged connection, got {flagged}"
    assert flagged[0].connection_id == inverted_id, (
        "the caveat did not name the connection it is about"
    )


def test_an_aggregate_over_only_conforming_connections_is_silent(
    initialized_config: Config,
) -> None:
    """🔴 The absence is the information, so the absence is asserted.

    A request-scoped kind that fired regardless of what the answer drew on
    would be the `gapped` failure this vocabulary is split in two to avoid --
    and the wiring is exactly where that regression would enter, because the
    call site sees the window and the producer does not.
    """
    _conforming(initialized_config)

    answer = query.money_summary(initialized_config, group_by="category")

    assert [c for c in answer.warnings if c.kind == "sign_convention_unverified"] == []


def test_a_window_that_missed_the_flagged_connection_is_silent(
    initialized_config: Config,
) -> None:
    """The window scoping survives the wiring, which is the half most easily lost.

    `money_summary` passes its own `since`/`until` through. Dropping them would
    still pass every test above -- the warning would simply start appearing on
    answers that drew nothing from the flagged feed, which is the failure that
    trains a reader to skip the field rather than one that looks broken.
    """
    _conforming(initialized_config)
    _inverted(initialized_config)

    long_before = date(2000, 1, 1)
    answer = query.money_summary(
        initialized_config,
        group_by="category",
        since=long_before,
        until=long_before + timedelta(days=1),
    )

    assert [c for c in answer.warnings if c.kind == "sign_convention_unverified"] == [], (
        "a window holding none of the flagged connection's rows still warned about it"
    )


# ---------------------------------------------------------------------------
# AC-14.3 — the declared category set is an enumeration, so it is checked.
#
# 🔴 A check whose input set is wrong covers less than it claims and says
# nothing about it: a misspelled category simply matches no row, the connection
# falls under the sample floor, and the verdict comes back `undetermined` --
# which reads as "not enough data" rather than as "this check is aimed at
# nothing". `query.KNOWN_SOURCE_CATEGORIES` carries two guards one module away
# for the same reason; this set had none.
# ---------------------------------------------------------------------------


def test_every_judged_category_is_one_the_product_already_knows(
    initialized_config: Config,
) -> None:
    """The declared set is a subset of the taxonomy the rest of the code knows.

    Catches the failure that has no symptom -- a typo, a renamed category, a
    value carried over from a different vocabulary. Each of those silently
    shrinks what the check measures while every test still passes.
    """
    unknown = sorted(set(signs.NEVER_INFLOW_CATEGORIES) - set(query.KNOWN_SOURCE_CATEGORIES))

    assert not unknown, (
        f"{unknown} are judged for sign but are not in `query.KNOWN_SOURCE_CATEGORIES`. "
        f"A category the taxonomy does not contain matches no row, so it narrows the check "
        f"silently: the verdict degrades to `undetermined`, which reads as thin data rather "
        f"than as a check aimed at nothing"
    )


def test_no_judged_category_is_one_that_legitimately_carries_both_signs(
    initialized_config: Config,
) -> None:
    """🔴 The other direction, and the one that would produce a FALSE report.

    A category that legitimately holds refunds as well as charges puts genuine
    positives into the baseline, which pushes a conforming feed toward the
    inverted verdict. `TRAVEL` is the measured case -- 24 charges against 24
    refunds in the sandbox -- and it is excluded for exactly that reason. So are
    the transfer and income categories, which are inflows by definition.

    Named here rather than left to the module comment, because this is the
    assertion that would fail if someone "completed" the set by adding the
    categories it deliberately omits.
    """
    forbidden = {"TRANSFER_IN", "TRANSFER_OUT", "INCOME", "LOAN_PAYMENTS", "TRAVEL"}

    overlap = sorted(forbidden & set(signs.NEVER_INFLOW_CATEGORIES))

    assert not overlap, (
        f"{overlap} legitimately carry inbound amounts, so judging them puts real positives "
        f"into the distribution and biases a CONFORMING feed toward being reported inverted"
    )


@pytest.mark.parametrize("category", signs.NEVER_INFLOW_CATEGORIES)
def test_each_judged_category_on_its_own_reaches_a_stored_row(
    initialized_config: Config, category: str
) -> None:
    """🔴 Every member of the set, one at a time, proven to match something.

    A category can be well-formed, absent from the excluded set, and still never
    match a stored row -- a spelling the deriver writes differently, a column
    that holds something else. Nothing about the check would look wrong: the
    category simply contributes nothing, the connection drifts under the sample
    floor, and the verdict reads `undetermined`, which a reader takes for thin
    data rather than for a check aimed at nothing. Three of the five were
    exercised by no fixture at all.

    Parametrised deliberately, so the failure NAMES the category that stopped
    matching. One aggregate assertion over all five would say only that the
    total came up short.
    """
    item = f"item-{category.lower()}"
    connection_id = _enroll(initialized_config, institution="First Platypus Bank", item_id=item)
    _feed(
        initialized_config,
        connection_id,
        item_id=item,
        entries=_spend(item, signs.MINIMUM_JUDGEABLE_ROWS, _CONFORMING_PURCHASE, category=category),
    )

    with reader_connection(initialized_config) as conn:
        judged = {m.connection_id: m for m in signs.measure(conn)}[connection_id]

    assert judged.rows_judged == signs.MINIMUM_JUDGEABLE_ROWS, (
        f"{category} was declared as judged but matched {judged.rows_judged} of "
        f"{signs.MINIMUM_JUDGEABLE_ROWS} stored rows carrying it; the check is narrower than "
        f"its declared set says, and nothing else would report that"
    )
    assert judged.verdict == "consistent", (
        f"{category} rows stored the conforming way did not read as consistent: {judged}"
    )


# ---------------------------------------------------------------------------
# AC-14.5 / one measurement per answer.
#
# 🔴 These exist because the fix for "pipeline_health measures twice" shipped
# with nothing holding it: `caveats` grew a `measured=` parameter and
# `pipeline_health` began passing it, and every existing test passed identically
# whether the parameter was passed or not. Reverting the call site would have
# left the suite green and silently restored the defect.
# ---------------------------------------------------------------------------


def test_caveats_uses_the_measurement_it_was_handed_rather_than_re_reading(
    initialized_config: Config,
) -> None:
    """🔴 The parameter is honoured, proven with a measurement the store contradicts.

    A fabricated `measured` that says a CONFORMING store is inverted is the only
    input that can tell "used what it was given" apart from "re-measured and
    happened to agree" -- which is what a fixture built from the real store can
    never distinguish. If `caveats` re-reads, it finds a consistent connection
    and stays silent; if it honours the argument, it warns.
    """
    connection_id = _conforming(initialized_config)

    fabricated = [
        signs.ConnectionSignConvention(
            connection_id=connection_id,
            institution="First Platypus Bank",
            rows_negative=0,
            rows_positive=signs.MINIMUM_JUDGEABLE_ROWS,
            rows_zero=0,
        )
    ]
    with reader_connection(initialized_config) as conn:
        found = signs.caveats(conn, measured=fabricated)

    assert [caveat.kind for caveat in found] == ["sign_convention_unverified"], (
        "caveats re-read the store instead of using the measurement it was handed; the "
        "one-measurement-per-answer property rests entirely on this parameter being honoured"
    )
    assert found[0].connection_id == connection_id


def test_pipeline_health_measures_once_even_when_a_second_scan_would_differ(
    initialized_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 The CALL is what this pins, and a static store cannot pin it.

    An earlier version of this test asserted that rows and warnings agree
    against an ordinary fixture. That proves nothing about the call site: the
    reader returns the same verdicts however many times it is scanned, so
    `caveats(conn)` and `caveats(conn, measured=measured)` are indistinguishable
    there, and reverting the call site left the whole suite green.

    The two measurements are made to DIFFER instead. `measure` is replaced with
    one that answers `consistent` first and `inverted` second, which is the
    autocommit reader's real hazard in miniature -- it pins no snapshot, so a
    second scan is a second observation. Passing the first measurement through
    makes the answer coherent; re-measuring inside `caveats` takes the second and
    the answer then calls one connection `consistent` in its row while warning
    that it is unverified.

    This is the rule the build plan already set for the other two boundary
    crossings, applied here: anchor on the call, because a producer that works
    and a surface that never invokes it are indistinguishable from the
    producer's own tests.
    """
    connection_id = _conforming(initialized_config)

    consistent = signs.ConnectionSignConvention(
        connection_id=connection_id,
        institution="First Platypus Bank",
        rows_negative=signs.MINIMUM_JUDGEABLE_ROWS,
        rows_positive=0,
        rows_zero=0,
    )
    inverted = signs.ConnectionSignConvention(
        connection_id=connection_id,
        institution="First Platypus Bank",
        rows_negative=0,
        rows_positive=signs.MINIMUM_JUDGEABLE_ROWS,
        rows_zero=0,
    )
    answers = iter([[consistent], [inverted]])

    def _drifting(_conn: object) -> list[signs.ConnectionSignConvention]:
        return next(answers, [inverted])

    monkeypatch.setattr(signs, "measure", _drifting)

    answer = query.pipeline_health(initialized_config)
    warned = {c.connection_id for c in answer.warnings if c.kind == "sign_convention_unverified"}
    verdicts = {int(row["connection_id"]): row["sign_convention"] for row in answer.rows}

    assert verdicts == {connection_id: "consistent"}, (
        f"the row should carry the FIRST measurement; got {verdicts}"
    )
    assert connection_id not in warned, (
        "this answer reports the connection `consistent` in its row and warns that it is "
        "unverified -- two measurements reached one answer, which is the defect the "
        "`measured=` parameter exists to prevent"
    )
