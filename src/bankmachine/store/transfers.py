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

from sqlalchemy import Connection as SAConnection
from sqlalchemy import func, select, update

from bankmachine.store.schema import accounts, transactions
from bankmachine.store.types import CalendarDate, calendar_date

#: How far apart two legs of one transfer may sit. Three days: an ACH between
#: two institutions routinely settles a day or two after it leaves, and a
#: weekend stretches that. Wider starts pairing genuinely unrelated round-number
#: movements; narrower misses ordinary interbank transfers, and a MISSED pair is
#: the direction that overstates spending rather than hiding it.
TRANSFER_WINDOW_DAYS = 3


@dataclass(frozen=True, slots=True)
class _Leg:
    transaction_id: int
    account_id: int
    ledger_date: CalendarDate
    amount_minor: int
    currency: str


def _candidates(conn: SAConnection) -> list[_Leg]:
    """Every unpaired, live row on an ENROLLED account that could be a leg.

    Ordered by `(ledger_date, transaction_id)` so the pairing below walks a
    stable sequence: the deterministic tie-break is only deterministic if the
    scan it runs over is.

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
            transactions.c.transfer_pair_id.is_(None),
            transactions.c.ledger_date.is_not(None),
            accounts.c.connection_id.is_not(None),
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

    Idempotent by construction: it considers only rows whose `transfer_pair_id`
    is null, so running it twice pairs nothing the second time, and a sync that
    brings the missing counterparty pairs it then. A rebuild replays into empty
    tables, so the pairing it produces is computed fresh from the archive rather
    than inherited.

    🔴 **Greedy over a stable order, with the nearest date winning.** That is the
    deterministic rule the module docstring names, and it is what makes the
    result a property of the data rather than of the scan.
    """
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


def unmatched_transfer_shaped(conn: SAConnection, categories: frozenset[str]) -> int:
    """How many transfer-shaped rows found no counterparty and count as spending.

    🔴 The tally rides the answer so an agent can see the classifier FELL BACK
    rather than concluded. A row the aggregator called a transfer and this store
    counts as money leaving is a judgement, and a judgement made silently on
    hundreds of rows is one nobody audits.
    """
    return int(
        conn.execute(
            select(func.count())
            .select_from(transactions)
            .where(
                transactions.c.removed_at.is_(None),
                transactions.c.transfer_pair_id.is_(None),
                transactions.c.source_category_detailed.in_(sorted(categories)),
            )
        ).scalar_one()
    )


#: Re-exported so a reader of the classifier finds the rule beside the names it
#: uses, rather than two modules apart.
__all__ = [
    "TRANSFER_WINDOW_DAYS",
    "pair_transfers",
    "unmatched_transfer_shaped",
]
