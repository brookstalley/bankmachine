"""Which rows are the two legs of one transfer between accounts this store holds.

A transfer between two accounts the household holds is not spending: the money
is still there. Counting the outgoing leg as spend overstates what left, and
excluding a row that merely LOOKS like a transfer understates it -- so the
question is not what the aggregator called the row, but whether the counterparty
is somewhere in this store.

🔴 **A pairing, not a lookup.** Each leg matches at most once. A lookup would let
one $500 deposit absorb three separate $500 withdrawals, excluding all three
from spending and understating the month by $1,000 -- silently, because every
row involved is real and every arithmetic step succeeds.

🔴 **Contention is resolved deterministically -- nearest date, then lowest
transaction id -- so a rebuild reproduces the same pairing.** Without that, two
candidate counterparties would pair by whatever order the scan happened to
return, and `store rebuild` could produce a different set of exclusions from the
same archive. A classification that changes under replay is one no operator can
audit.

🔴 **Measured on `ledger_date`, never `posted_date`.** The posting date moves
when a hold settles, so a leg measured on it could drift out of the matching
window between two syncs -- and a transfer would stop being a transfer with
nothing to say why.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy import Connection as SAConnection

from bankmachine.store.schema import accounts, transactions
from bankmachine.store.types import CalendarDate, calendar_date

#: How far apart two legs of one transfer may sit. Three days: an ACH between
#: two institutions routinely settles a day or two after it leaves, and a
#: weekend stretches that. Wider starts pairing genuinely unrelated round-number
#: movements; narrower misses ordinary interbank transfers, and a MISSED pair is
#: the direction that overstates spending rather than hiding it.
TRANSFER_WINDOW_DAYS = 3


#: The detailed categories that MIGHT be a movement between two accounts this
#: household holds -- transfer-shaped, which is not the same as being a transfer.
#:
#: 🔴 A row is only `internal_transfer` when a matching opposite leg is FOUND on
#: another enrolled account. These names are the candidates; `transfer_pair_id`
#: is the evidence. An ATM withdrawal, a payment to a person, ACH rent to a
#: landlord -- all transfer-shaped, none of them a transfer, because from the
#: household's point of view the money is gone.
#:
#: 🔴 **`TRANSFER_IN_PAYROLL` is deliberately absent, and so is every `INCOME_*`
#: name.** Wages arriving are external value ENTERING the household, whatever
#: the aggregator's transfer-shaped naming suggests. Classifying the payroll row
#: as an internal transfer is what made income read $0 on this surface, and no
#: leg match should be able to bring it back: a paycheque has no counterparty
#: leg here, but an accidental amount-and-date collision must not be allowed to
#: invent one.
TRANSFER_SHAPED_DETAILED: frozenset[str] = frozenset(
    {
        "TRANSFER_IN_ACCOUNT_TRANSFER",
        "TRANSFER_IN_DEPOSIT",
        "TRANSFER_IN_INVESTMENT_AND_RETIREMENT_FUNDS",
        "TRANSFER_IN_SAVINGS",
        "TRANSFER_IN_OTHER_TRANSFER_IN",
        "TRANSFER_OUT_ACCOUNT_TRANSFER",
        "TRANSFER_OUT_INVESTMENT_AND_RETIREMENT_FUNDS",
        "TRANSFER_OUT_SAVINGS",
        "TRANSFER_OUT_OTHER_TRANSFER_OUT",
    }
)

#: Detailed categories naming a payment toward a liability.
#:
#: 🔴 It is `debt_service` only when THIS STORE HOLDS the liability. A mortgage
#: to a lender the operator has not enrolled is money out of the household and
#: is `external_spend`; a card payoff where the card IS enrolled is
#: `debt_service`, because that card's own purchases are already counted and
#: counting the payoff too is the double count the class exists to prevent.
#:
#: 🔴 **Principal and interest are explicitly NOT split**, and the silence is a
#: decision rather than an oversight. The aggregator does not decompose a loan
#: payment per transaction, and deriving a split from balance movement would be
#: an inference presented as a record. The whole payment classifies together.
DEBT_SERVICE_DETAILED: frozenset[str] = frozenset(
    {
        "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
        "LOAN_PAYMENTS_MORTGAGE_PAYMENT",
        "LOAN_PAYMENTS_CAR_PAYMENT",
        "LOAN_PAYMENTS_STUDENT_LOAN_PAYMENT",
        "LOAN_PAYMENTS_PERSONAL_LOAN_PAYMENT",
        "LOAN_PAYMENTS_OTHER_PAYMENT",
    }
)


@dataclass(frozen=True, slots=True)
class _Leg:
    transaction_id: int
    account_id: int
    ledger_date: CalendarDate
    amount_minor: int
    currency: str


#: The only categories a pair can change the meaning of. A row outside these is
#: `external_spend` whether or not something opposite exists somewhere.
PAIRABLE_DETAILED: frozenset[str] = TRANSFER_SHAPED_DETAILED | DEBT_SERVICE_DETAILED


def _candidates(conn: SAConnection) -> list[_Leg]:
    """Every live row that a pair could actually change the meaning of.

    Ordered by `(ledger_date, transaction_id)` so the pairing below walks a
    stable sequence: the deterministic tie-break is only deterministic if the
    scan it runs over is.

    🔴 **Restricted to the categories a pair can change the meaning of, and that
    is correctness before it is cost.** Matching is a PAIRING -- each leg matches
    at most once -- so a row that can never use its pair can still consume one.
    A $5 purchase and a $5 refund on two accounts are opposite, equal and days
    apart; unrestricted, they would take each other, and a genuine $5 transfer
    on the same day would then find no counterparty and be reported as money
    that left the household. The classifier only ever consults
    `transfer_pair_id` for these categories, so pairing anything else buys
    nothing and can only steal.

    🔴 It is also what makes this affordable. The scan is quadratic in its own
    result, and it runs on every response; over every unpaired row in a store
    holding years of history that is a cost with no answer attached to it. Most
    rows are ordinary spending and never pair, so they would sit in the
    candidate set forever, re-scanned on every page.

    🔴 Restricted to accounts carrying a `connection_id`. An import-only account
    is operator-owned and this store holds no counterparty feed for it, so a row
    on one cannot be shown to have a matching leg -- and showing it is the whole
    test. Treating it as a transfer on shape alone is the overstatement this
    module exists to remove.
    """
    rows = conn.execute(
        select(
            transactions.c.transaction_id,
            transactions.c.account_id,
            transactions.c.ledger_date,
            transactions.c.amount_minor,
            transactions.c.currency,
        )
        .select_from(transactions.join(accounts))
        .where(
            transactions.c.removed_at.is_(None),
            transactions.c.ledger_date.is_not(None),
            accounts.c.connection_id.is_not(None),
            transactions.c.source_category_detailed.in_(sorted(PAIRABLE_DETAILED)),
        )
        .order_by(transactions.c.ledger_date, transactions.c.transaction_id)
    ).all()
    return [
        _Leg(
            transaction_id=int(row[0]),
            account_id=int(row[1]),
            ledger_date=calendar_date(row[2]),
            amount_minor=int(row[3]),
            currency=str(row[4]),
        )
        for row in rows
    ]


def _matches(left: _Leg, right: _Leg) -> bool:
    """Whether these two rows are the two sides of one movement.

    Equal magnitude and OPPOSITE sign, on DIFFERENT accounts, in the same
    currency, within the window. Same-account is excluded explicitly: a
    correction posted against one account is not money moving between two, and
    pairing it would exclude a real reversal from every total.
    """
    if left.account_id == right.account_id or left.currency != right.currency:
        return False
    if left.amount_minor != -right.amount_minor or left.amount_minor == 0:
        return False
    return abs((left.ledger_date - right.ledger_date).days) <= TRANSFER_WINDOW_DAYS


def pair_transfers(conn: SAConnection) -> int:
    """Pair every unpaired leg that has a counterparty, and return the pair count.

    🔴 **Every pairable row is RE-paired from scratch, not just the new ones**,
    and that is what makes the result a function of the store rather than of the
    order pages arrived in. Pairing only unpaired rows was the obvious design and
    it is wrong: a leg that paired on page 1 could never re-pair against a nearer
    counterparty landing on page 2, so an incremental sync and a `store rebuild`
    over the same archive would disagree -- and the rebuild's own digest guard
    would then report a routine rebuild as content-changing and steer the
    operator at `--accept-content-change`. Clearing first costs a scan bounded by
    the pairable categories and buys back the guarantee that the same archive
    always yields the same classification.

    🔴 **Greedy over a stable order, with the nearest date winning.** That is the
    deterministic rule the module docstring names, and it is what makes the
    result a property of the data rather than of the scan.
    """
    conn.execute(
        update(transactions)
        .where(
            transactions.c.transfer_pair_id.is_not(None),
            transactions.c.source_category_detailed.in_(sorted(PAIRABLE_DETAILED)),
        )
        .values(transfer_pair_id=None)
    )
    legs = _candidates(conn)
    taken: set[int] = set()
    pairs: list[tuple[int, int]] = []
    for index, left in enumerate(legs):
        if left.transaction_id in taken:
            continue
        best: _Leg | None = None
        for right in legs[index + 1 :]:
            if right.transaction_id in taken or not _matches(left, right):
                continue
            if best is None:
                best = right
                continue
            # Nearest date, then lowest id. Spelled as one comparison so the two
            # halves cannot be applied in different orders by a later edit.
            here = abs((left.ledger_date - right.ledger_date).days)
            there = abs((left.ledger_date - best.ledger_date).days)
            if (here, right.transaction_id) < (there, best.transaction_id):
                best = right
        if best is not None:
            taken.add(left.transaction_id)
            taken.add(best.transaction_id)
            pairs.append((left.transaction_id, best.transaction_id))

    if not pairs:
        return 0
    # 🔴 The pair id is the LOWER of the two transaction ids, not a sequence.
    # A sequence would need its own counter and its own migration, and it would
    # make the value depend on the order pairs were discovered -- which is
    # exactly the replay instability the deterministic tie-break exists to
    # prevent. The lower id is a function of the pair itself.
    for one, two in pairs:
        pair_id = min(one, two)
        conn.execute(
            update(transactions)
            .where(transactions.c.transaction_id.in_((one, two)))
            .values(transfer_pair_id=pair_id)
        )
    return len(pairs)


def unmatched_transfer_shaped(
    conn: SAConnection, categories: frozenset[str], *, since: date | None, until: date | None
) -> int:
    """How many transfer-shaped rows in THIS WINDOW found no counterparty.

    🔴 The tally rides the answer so an agent can see the classifier FELL BACK
    rather than concluded. A row the aggregator called a transfer and this store
    counts as money leaving is a judgement, and a judgement made silently over
    hundreds of rows is one nobody audits.

    🔴 **Scoped to the window the answer covers, not to the store.** A count over
    every such row anywhere would ride a windowed answer describing rows that
    answer is not about -- "12 rows here fell back" when the window contains
    none of them. A caller cannot tell those apart, and a figure that is true of
    something other than the answer beside it is the precise wrong number this
    surface exists to refuse. Measured on `ledger_date`, like the window itself.
    """
    filters: list[ColumnElement[bool]] = [
        transactions.c.removed_at.is_(None),
        transactions.c.transfer_pair_id.is_(None),
        transactions.c.source_category_detailed.in_(sorted(categories)),
    ]
    if since is not None:
        filters.append(transactions.c.ledger_date >= since)
    if until is not None:
        filters.append(transactions.c.ledger_date <= until)
    return int(
        conn.execute(select(func.count()).select_from(transactions).where(*filters)).scalar_one()
    )


#: Re-exported so a reader of the classifier finds the rule beside the names it
#: uses, rather than two modules apart.
__all__ = [
    "TRANSFER_WINDOW_DAYS",
    "pair_transfers",
    "unmatched_transfer_shaped",
]
