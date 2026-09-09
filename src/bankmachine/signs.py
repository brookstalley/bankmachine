"""The per-connection sign-convention check — AC-14.1 through AC-14.6.

🔴 **This module exists because the sign convention is a per-feed claim resting
on ONE measurement of ONE connection.** `api-notes-plaid.md` §17 measured a
single sandbox row — a merchant purchase arriving with a POSITIVE `amount` — and
`connector/plaid/derivers.py` negates every amount on the strength of it, with
no branch and no condition. `data-model.md` § Direction records the resulting
scope: a second institution is a **new observation**, not a covered case. If
institution B signs its feed the other way, the same unconditional negation
stores B's spending as income and B's income as spending, and every total over
it is well-formed, plausible, and wrong in both directions at once.

Nothing here can prevent that. What it can do is **notice**, which is what this
module is: a population-level measurement of the stored sign distribution, per
connection, over categories that are never plausibly inflows.

🔴 **It reports; it never corrects (AC-14.4).** Auto-inverting a suspect feed is
a heuristic, and the direction it fails in is the one that *understates*
spending — the argument that decided #18 and #20. There is no write path in this
module, no `UPDATE`, and no caller-supplied "fix it" switch: the check names the
connection and stops.

🔴 **The category is read from `source_category_primary`, never from
`category_override` and never from the description.** Two separate reasons, and
both have been re-derived more than once:

* An override is local interpretation of what a transaction was *for*. Letting a
  re-categorisation move a row into or out of the judged set would let local
  intent decide whether a feed gets checked at all.
* Free-text description is not a sign oracle (AC-14.6). The sandbox's own
  payroll row reads "ACH Electronic Credit" and is categorised `TRANSFER_OUT` in
  **both** structured fields; a check reading the word "Credit" would call the
  aggregator's own categorisation wrong. This module never selects, groups or
  branches on `description`, and
  `tests/test_sign_convention.py::test_the_verdict_does_not_move_when_every_description_is_rewritten`
  holds it to that behaviourally rather than by inspection.

**Why a population check rather than a per-row one.** The obvious detector — a
row categorised `INCOME` whose stored amount is negative — was measured against
the whole store before being proposed and ships with no positive case anywhere:
the category/sign disagreement count over all 388 rows is zero, and `TRAVEL`
legitimately carries both signs (24 charges, 24 refunds), so its false-positive
rate is unknown as well as its true-positive rate. It is recorded as rejected in
`discovery-production-data-semantics.md` so it is not re-proposed. The real risk
is not one mis-signed row; it is a whole feed signed the other way, and that is
visible only at population scale and only per connection.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import and_, case, func, select
from sqlalchemy.engine import Connection as SAConnection

from bankmachine.envelope import Caveat
from bankmachine.store.schema import accounts, connections, institutions, transactions

#: 🔴 **The declared category set (AC-14.3).** Categories in which a stored
#: POSITIVE amount — money arriving, under `data-model.md` § Direction — is not
#: a thing that ordinarily happens. Measured over the sandbox store on
#: 2026-09-09: 219 rows across these five, **219 of them negative, none
#: positive**. That measurement is this check's negative control.
#:
#: 🔴 **What is deliberately NOT here matters as much as what is**, because each
#: exclusion is a category that would have produced findings and no signal:
#:
#: * `TRANSFER_IN` / `TRANSFER_OUT` — a transfer is an inflow on one side by
#:   definition; measured 25 of 25 positive and 48 of 48 negative respectively.
#: * `INCOME` — an inflow by name.
#: * `LOAN_PAYMENTS` — a disbursement and a payment share the category.
#: * `TRAVEL` — measured carrying **both** signs legitimately, 24 charges
#:   against 24 refunds. It is the standing proof that "never plausibly an
#:   inflow" is a property of the category rather than of the word "spending",
#:   and including it would have put a 50% positive rate into the baseline.
#:
#: A category is added here only with the measurement that says it belongs.
NEVER_INFLOW_CATEGORIES: tuple[str, ...] = (
    "FOOD_AND_DRINK",
    "GENERAL_MERCHANDISE",
    "PERSONAL_CARE",
    "RENT_AND_UTILITIES",
    "TRANSPORTATION",
)

#: 🔴 **The threshold (AC-14.3), and it is a PROPORTION rather than a count.**
#: A connection is inverted when more than this share of its judged rows are
#: stored positive.
#:
#: The value is 0.5 because 0.5 is the only value not chosen by taste: it is the
#: point of indifference between the two hypotheses — "this feed obeys the
#: convention" and "this feed is inverted" — and on separated data every
#: threshold strictly between 0 and 1 returns the same verdict. The measured
#: separation is near-total: a conforming feed sits at 0.00 (0 of 219) and an
#: inverted one at 1.00, so a tuned constant such as 0.9 would buy nothing and
#: would then need its own derivation.
#:
#: 🔴 **Being a proportion is what keeps this out of AC-9.1's trap.** The
#: precedent named in AC-13.5 is that *a fixed constant against a variable
#: cadence produces findings and no signal* — a 7-day gap rule reporting a
#: monthly account's ordinary silence. A share is dimensionless: it does not
#: move with how many rows a connection has, how often the institution posts, or
#: how long it has been enrolled. The one absolute number in this module is the
#: sample floor below, and it is derived rather than picked.
INVERTED_ABOVE_SHARE: float = 0.5

#: 🔴 **The sample floor, derived from the negative control's own precision.**
#: Below this many signed rows the check returns `undetermined` rather than a
#: verdict, because a share computed over three rows is not a distribution.
#:
#: Derivation. Positives in the declared categories were observed 0 times in
#: 219 rows, so the rule of three puts a 95% upper bound of 3/219 = 1.37% on the
#: per-row probability that a *conforming* feed stores one positive (a
#: merchandise return, say). Under that bound, the probability that a conforming
#: feed shows a positive MAJORITY is 5.2e-7 at 6 rows but 1.2e-6 at 7 — the
#: quantity oscillates with parity, since an odd sample needs the same count of
#: positives as the even one above it. **8 is the smallest n at which that
#: probability is below one in a million for n and for every larger n**
#: (2.6e-8 at 8, and falling monotonically from there).
#:
#: So the floor is set by how wrong the check could be about a *good* feed, not
#: by a round number. A real connection crosses it within days of enrollment;
#: what it excludes is the freshly-enrolled connection whose first three rows
#: happen to include a refund.
MINIMUM_JUDGEABLE_ROWS: int = 8

#: The three things this check can conclude. `undetermined` is a real answer and
#: is never collapsed into `consistent`: "we could not tell" and "we checked and
#: it is fine" are the two statements this product exists to keep apart.
VERDICTS: tuple[str, ...] = ("consistent", "inverted", "undetermined")


@dataclass(frozen=True, slots=True)
class ConnectionSignConvention:
    """One connection's stored sign distribution over the declared categories.

    Carries the counts rather than the verdict alone, because a verdict with no
    evidence under it is a claim a reader has to take on faith — and this check's
    whole subject is a claim that was taken on faith once already.
    """

    connection_id: int
    institution: str
    rows_negative: int
    rows_positive: int
    #: Zero-amount rows in the declared categories. They carry no direction, so
    #: they are excluded from the share and counted here instead of being
    #: silently dropped into one side or the other.
    rows_zero: int

    @property
    def rows_judged(self) -> int:
        """The rows the share is computed over: the signed ones, not all rows."""
        return self.rows_negative + self.rows_positive

    @property
    def positive_share(self) -> float | None:
        """Share of judged rows stored positive, or None when nothing is judged.

        None rather than 0.0: a connection with no rows in these categories has
        no share, and reporting 0.0 would read as "perfectly conforming" — the
        exact conflation `undetermined` exists to prevent.
        """
        if self.rows_judged == 0:
            return None
        return self.rows_positive / self.rows_judged

    @property
    def verdict(self) -> str:
        """`consistent`, `inverted` or `undetermined`.

        🔴 An exact tie is `undetermined`, not `consistent`. A feed half of whose
        never-inflow rows are stored positive is not a feed this check has
        cleared; calling it consistent would be a statement the evidence does not
        support, in the direction of silence.
        """
        share = self.positive_share
        if share is None or self.rows_judged < MINIMUM_JUDGEABLE_ROWS:
            return "undetermined"
        if share > INVERTED_ABOVE_SHARE:
            return "inverted"
        if share < INVERTED_ABOVE_SHARE:
            return "consistent"
        return "undetermined"

    def caveat(self) -> Caveat | None:
        """The finding this connection produces, or None when it produces none.

        🔴 **Names the connection, states the evidence, and refuses to correct
        it** (AC-14.4). The detail is written for an agent that will quote a
        total computed over this connection in the next sentence, so it says
        what is wrong, that the amounts are reported as stored, and what the
        operator can do that the software cannot.

        🔴 The kind is spelled as a LITERAL and not lifted to a constant, which
        is a requirement rather than a style choice:
        `tests/preferences/test_the_warning_vocabulary_is_closed.py` reads every
        `Caveat(...)` in the tree with `ast` and checks its kind against the
        closed vocabulary before a client can. A kind arriving through a name is
        a site that scan reports as fine while proving nothing about it — and an
        invented kind does not produce an unrecognized warning, it produces a
        REJECTED ANSWER.
        """
        if self.verdict != "inverted":
            return None
        return Caveat(
            kind="sign_convention_unverified",
            detail=(
                f"connection {self.connection_id} ({self.institution}): "
                f"{self.rows_positive} of {self.rows_judged} stored amounts in "
                f"{', '.join(NEVER_INFLOW_CATEGORIES)} are POSITIVE, which under this "
                f"product's convention means money arriving. A feed signed the other way "
                f"round would look exactly like this, and its spending would read as income. "
                f"The amounts are reported AS STORED and are NOT corrected — inverting them "
                f"on suspicion can be wrong in the direction that understates spending. "
                f"Check one known debit on this connection against the institution's own "
                f"statement before quoting any total that draws on it."
            ),
            connection_id=self.connection_id,
            institution=self.institution,
        )


def measure(conn: SAConnection) -> list[ConnectionSignConvention]:
    """Every connection's sign distribution, judged over its whole stored history.

    🔴 **Every connection appears, including one with no transactions at all.**
    A connection missing from this list would be indistinguishable from a
    connection this check cleared, on the surface that publishes it.

    🔴 **Judged over all stored rows, never over a window.** The convention is a
    property of the feed, so the measurement wants every row there is; narrowing
    it to a caller's window would let a short window drop a known-inverted
    connection below the sample floor and answer `undetermined` about a
    connection the store has 400 rows of evidence about. Which connections a
    given answer must WARN about is a separate question, and `caveats` below is
    where it is asked.

    Soft-deleted rows are excluded, matching every other read: a removed row is
    not part of the feed's current distribution.

    🔴 **Recorded limit: an account with no connection is not judged.** The unit
    of this check is the *feed*, and `accounts.connection_id` is nullable
    precisely because FR-7's manual-import path attaches to no feed. No such row
    can exist today — no import adapter is built — so this costs nothing now,
    and it is stated because it will stop being free the day one lands: an
    adapter reading a CSV whose amounts are signed the other way would produce
    exactly the defect this module exists to catch, on the one path it does not
    watch. Judging imported rows needs a unit that is not a connection, which is
    a decision rather than an oversight.
    """
    judged = and_(
        transactions.c.account_id == accounts.c.account_id,
        transactions.c.removed_at.is_(None),
        # 🔴 In the JOIN condition, not in a WHERE. A category filter in the
        # WHERE clause would drop the connections with no matching rows from the
        # result entirely -- and those are exactly the ones whose answer is
        # `undetermined`, which is the answer this check most needs to be able
        # to give.
        transactions.c.source_category_primary.in_(NEVER_INFLOW_CATEGORIES),
    )
    statement = (
        select(
            connections.c.connection_id,
            institutions.c.name,
            func.coalesce(func.sum(case((transactions.c.amount_minor < 0, 1), else_=0)), 0).label(
                "negative"
            ),
            func.coalesce(func.sum(case((transactions.c.amount_minor > 0, 1), else_=0)), 0).label(
                "positive"
            ),
            func.coalesce(func.sum(case((transactions.c.amount_minor == 0, 1), else_=0)), 0).label(
                "zero"
            ),
        )
        .select_from(
            connections.join(institutions)
            .outerjoin(accounts, accounts.c.connection_id == connections.c.connection_id)
            .outerjoin(transactions, judged)
        )
        .group_by(connections.c.connection_id, institutions.c.name)
        .order_by(connections.c.connection_id)
    )
    return [
        ConnectionSignConvention(
            connection_id=int(row[0]),
            institution=str(row[1]),
            rows_negative=int(row[2]),
            rows_positive=int(row[3]),
            rows_zero=int(row[4]),
        )
        for row in conn.execute(statement).all()
    ]


def _connections_in_window(
    conn: SAConnection, *, since: date | None, until: date | None
) -> set[int]:
    """The connections holding at least one live transaction in the window.

    Any category, because the question is which feeds an answer over this window
    actually drew on -- not which of them this check judges.
    """
    filters: list[Any] = [
        transactions.c.removed_at.is_(None),
        accounts.c.connection_id.is_not(None),
    ]
    if since is not None:
        filters.append(transactions.c.posted_date >= since)
    if until is not None:
        filters.append(transactions.c.posted_date <= until)
    rows = conn.execute(
        select(accounts.c.connection_id)
        .select_from(transactions.join(accounts))
        .where(*filters)
        .distinct()
    ).all()
    return {int(row[0]) for row in rows}


def caveats(
    conn: SAConnection,
    *,
    since: date | None = None,
    until: date | None = None,
    measured: Sequence[ConnectionSignConvention] | None = None,
) -> list[Caveat]:
    """The findings an answer over this window must carry (AC-14.2, AC-14.5).

    🔴 **The public producer.** One caveat per connection whose stored
    distribution is inverted AND which contributed at least one live row to the
    window asked about. Both halves are load-bearing:

    * Judged over the connection's whole history, so a narrow window cannot
      make a known-inverted feed look unjudgeable; and
    * scoped to the window's contributors, because a request-scoped warning that
      fires on every answer regardless of what the answer touched is the "true
      and useless" failure `envelope.py` records at length. An aggregate that
      drew nothing from an inverted connection is not an aggregate computed over
      it, and saying otherwise would train a reader to skip the field.

    `since=None, until=None` — the default — means the whole store, which is what
    an unwindowed surface passes.

    🔴 **An `undetermined` connection produces no caveat**, and that is a
    deliberate reading of AC-14.5's word *flagged*: AC-14.2 flags a connection
    whose distribution is **inverted**, and a connection below the sample floor
    has not been flagged, it has not been judged. It is not silent either — the
    verdict and its counts are published per connection on `get_pipeline_health`,
    which is the verification surface and the right home for "we could not
    tell". Emitting a warning on every aggregate over every young connection
    would put an unactionable string on almost every answer this product gives
    in its first week, which is the failure mode that makes a warning field stop
    being read.

    Returns them ordered by connection id -- two identical stores answer
    identically, and a wire array's order is information a consumer may rely on
    even when it should not.
    """
    contributing = _connections_in_window(conn, since=since, until=until)
    found = []
    # 🔴 `measured` lets a caller that ALREADY measured pass its own result in,
    # rather than paying for a second scan whose answer can differ from the
    # first. The reader is autocommit and holds no cross-call snapshot, so two
    # measurements taken moments apart are two different observations -- and
    # `pipeline_health` publishes one of them per row while warning from the
    # other, which is how a single answer came to state a connection is
    # `consistent` in its row and unverified in its warnings. One measurement
    # per answer is the property; the parameter is what makes it expressible.
    for measurement in measure(conn) if measured is None else measured:
        if measurement.connection_id not in contributing:
            continue
        caveat = measurement.caveat()
        if caveat is not None:
            found.append(caveat)
    return found
