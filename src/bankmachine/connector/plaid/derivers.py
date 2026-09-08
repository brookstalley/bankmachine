"""Institutions and accounts, derived from what the aggregator actually said.

The first derivers this product has, and the ones that make `store rebuild`
exercisable end-to-end: until they existed a rebuild refused any real archive for
want of one. They are composed into a registry by `bankmachine.derivers`, above
both layers, and passed to whoever runs a derivation -- not registered into
`store.derivation`, which would mean `store` importing `connector`.

🔴 **No clock, ever.** A deriver is a pure function of its response. Every
timestamp here comes from `response.received_at` -- when this system actually
learned the thing -- because a `now()` anywhere makes replay produce different
rows than the original, and `store.rebuild` would then report content it could
not reproduce. That check is the whole reason the seam exists, so a clock here
does not merely bend a rule, it disarms the mechanism.

**Idempotence is not a bonus here; it is load-bearing, and it is uneven.**
`balances_daily` points at a raw response, so a rebuild empties and replays it.
`institutions` and `accounts` do **not** -- their local ids are what every row
of history references (AC-6.3), so a rebuild that reassigned them would orphan
that history. They are therefore never emptied, and a deriver writing one must
**upsert on its natural key**: a plain insert would raise on the replay's second
pass, and the failure would read as a purity bug rather than what it is.

**Order-independence follows from how the columns are computed**, not from the
replay order happening to be stable. `first_seen_at` is a minimum and
`last_seen_at` a maximum over the responses that mention a thing, so replaying
the archive in any order lands on the same values -- which is what makes the
property test a property and not a description of today's `ORDER BY`.

🔴 **Money never touches a float.** `json.loads` turns `110.0` into a Python
float, and by the time you can see it the fraction is already gone. Everything
here parses with `parse_float=str`, so an amount arrives as the digits the
aggregator sent and `from_decimal_string` converts it exactly or refuses.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Final

from sqlalchemy import Connection as SAConnection
from sqlalchemy import delete, insert, select, update

from bankmachine.connector import (
    ACCOUNTS_GET,
    INSTITUTIONS_GET,
    ITEM_GET,
    ITEM_REMOVE,
    TRANSACTIONS_SYNC,
)
from bankmachine.logging_setup import get_logger
from bankmachine.store.derivation import DerivationContext, DerivationError, Deriver
from bankmachine.store.raw import RawResponse
from bankmachine.store.schema import (
    accounts,
    balances_daily,
    connections,
    institutions,
    sync_state,
    transactions,
)
from bankmachine.store.types import (
    CalendarDate,
    MinorUnits,
    MoneyError,
    UtcInstant,
    calendar_date,
    from_decimal_string,
    minor_digits,
    negate,
)

_log = get_logger(__name__)

#: Columns an account row keeps once it has one, because something other than
#: this deriver owns them afterwards.
#:
#: `balance_class` is declared operator-correctable in `data-model.md` -- it
#: partitions a report rather than deciding an arithmetic sign, so the operator
#: gets the last word. `lifecycle_status` belongs to whatever retires an account.
#:
#: 🔴 **Nothing retires one yet, and that is a real gap rather than an oversight
#: this list closes.** An account that stops appearing in `/accounts/get` stays
#: `active` with a frozen balance, and `data-model.md` § Account lifecycle notes
#: that the coverage report reads exactly `lifecycle_status` and `closed_date` to
#: tell a closure from a hole. Until the transition exists, a closed account will
#: report as a permanent gap. The removal case *is* derivable here --
#: `/accounts/get` returns the full list per connection -- but making it
#: order-independent under replay needs the care `first_seen_at` got, so it is
#: recorded as deferred in the build plan rather than half-built.
_OPERATOR_OWNED: Final[frozenset[str]] = frozenset({"balance_class", "lifecycle_status"})

#: Aggregator account types whose balance is money the operator *owes*.
#:
#: Read from the account's own type rather than from anything naming an
#: institution, per AC-3.2's rule that nothing branches on a roster identity. A
#: type this build has never seen is refused rather than assumed to be an asset:
#: guessing "asset" on an unrecognized liability reports a debt as savings, which
#: is wrong by twice the balance and looks entirely reasonable.
_LIABILITY_TYPES: Final[frozenset[str]] = frozenset({"credit", "loan"})
_ASSET_TYPES: Final[frozenset[str]] = frozenset({"depository", "investment", "brokerage", "other"})


def _payload(response: RawResponse) -> dict[str, Any]:
    """The response body, with every number left as the text the aggregator sent.

    `parse_float=str` is the load-bearing argument. Without it `json.loads`
    builds a float and the exactness question is already lost -- `from_decimal_string`
    would then be converting this machine's best rendering of a number rather
    than the number itself.
    """
    try:
        parsed = json.loads(response.body, parse_float=str)
    except json.JSONDecodeError as exc:
        raise DerivationError(
            f"raw response {response.raw_response_id} ({response.endpoint}) is not JSON, so "
            f"nothing can be derived from it"
        ) from exc
    if not isinstance(parsed, dict):
        raise DerivationError(
            f"raw response {response.raw_response_id} ({response.endpoint}) is a "
            f"{type(parsed).__name__}, expected an object"
        )
    return parsed


def _required(value: object, what: str, response: RawResponse) -> str:
    """A string the schema declares NOT NULL, or a refusal naming what was missing."""
    if not isinstance(value, str) or not value:
        raise DerivationError(
            f"raw response {response.raw_response_id} ({response.endpoint}) has no {what}; "
            f"the column is NOT NULL, and a placeholder here would be this system inventing "
            f"a fact and recording it as one the aggregator supplied"
        )
    return value


def _optional(value: object) -> str | None:
    """A nullable string. An absent field and an empty one are both nothing."""
    return value if isinstance(value, str) and value else None


def to_minor(amount: object, currency: str, what: str, response: RawResponse) -> MinorUnits:
    """One *valuation* as integer minor units, rounded to the currency if it has to be.

    🔴 **This rounds, and `from_decimal_string` deliberately does not.** The
    distinction is between a **ledger amount** -- a transaction, a payment, a
    posted balance, where a fraction of a cent lost on every row reconciles to
    nothing -- and a **valuation**: an investment account's `current` is price
    times quantity, computed rather than transacted, and it arrives with
    whatever precision that arithmetic produced.

    *(Measured, not assumed: Plaid's own sandbox institution returns a 401k
    balance of `23631.9805` USD. It is canned data, which is the aggregator
    deliberately exercising the case -- so sub-cent valuations are a production
    shape, not a sandbox artifact.)*

    No brokerage statement reports hundredths of a cent, so `23631.98` is the
    correct representation of that account's value rather than a lossy one. The
    exact original is in the archive regardless, so nothing is irrecoverable and
    a later build that widens the column can re-derive it.

    **Half-even**, because it is applied to every valuation on every sync: round-
    half-up would bias a portfolio's recorded value upward a fraction of a cent
    at a time, in one direction, forever.

    **Logged every time.** Rounding that nobody can see is the silent-loss
    failure this convention exists to avoid; rounding that says so is a recorded
    approximation.

    `amount` arrives as `str` because `_payload` parsed with `parse_float=str`.
    A float reaching here means someone parsed the body a second way, and it is
    refused rather than converted: `float` has already lost whatever it lost,
    and rounding it would launder that loss into a number that looks exact.
    """
    if isinstance(amount, int) and not isinstance(amount, bool):
        amount = str(amount)
    if not isinstance(amount, str):
        raise DerivationError(
            f"raw response {response.raw_response_id} gives {what} as a "
            f"{type(amount).__name__}; amounts must reach this point as the text the aggregator "
            f"sent, because a float has already lost fractions of a cent by the time it is seen"
        )
    exponent = minor_digits(currency)
    try:
        return from_decimal_string(amount, exponent=exponent)
    except MoneyError:
        # Only reached when the value carries more precision than the currency
        # has. Re-derived through Decimal rather than caught-and-guessed: the
        # quantize is what decides the stored value, and it must be visible.
        rounded = Decimal(amount).quantize(Decimal(1).scaleb(-exponent), rounding=ROUND_HALF_EVEN)
        _log.info(
            "raw response %s: %s of %s %s rounded to %s for storage (a valuation, "
            "not a ledger amount; the archive keeps the original)",
            response.raw_response_id,
            what,
            amount,
            currency,
            rounded,
        )
        return from_decimal_string(str(rounded), exponent=exponent)


def balance_class_of(account_type: str, response: RawResponse) -> str:
    """Whether this account's balance is value held or value owed."""
    if account_type in _LIABILITY_TYPES:
        return "liability"
    if account_type in _ASSET_TYPES:
        return "asset"
    raise DerivationError(
        f"raw response {response.raw_response_id} carries account type {account_type!r}, which "
        f"this build cannot classify as an asset or a liability. Refusing rather than assuming: "
        f"an unrecognized liability recorded as an asset is wrong by twice the balance, and the "
        f"total it feeds still looks reasonable"
    )


def _as_of(received_at: UtcInstant) -> CalendarDate:
    """The date a balance was captured on, from the response rather than a clock."""
    return calendar_date(received_at.date())


def derive_nothing(conn: SAConnection, response: RawResponse, context: DerivationContext) -> None:
    """`/institutions/get` -> no rows, deliberately and by name.

    🔴 **A catalogue page is reference data about the aggregator's coverage, not
    a fact about this operator.** `/institutions/get` serves the *production*
    catalogue -- 10,083 US institutions *(measured; `api-notes-plaid.md` §7)* --
    and `institutions` is the roster: the table `connections` hangs off, holding
    the institutions the operator actually enrolled. Deriving a page into it
    would put banks the operator never linked beside the ones they did, with
    nothing to tell them apart and nothing that removes them, since a rebuild
    never empties this table.

    **Registered rather than omitted.** `deriver_for` refuses an endpoint it has
    no deriver for, which is right -- a rebuild that stepped over a response it
    could not interpret would report success on an incomplete dataset. But
    "archived and implies no rows" is a real answer, and it has to be *stated*,
    or it is indistinguishable from the endpoint nobody got round to.

    The response is still archived: it is what `connector check` proves the whole
    path with, and the archive is the record of what this system was told.
    """


def derive_item(conn: SAConnection, response: RawResponse, context: DerivationContext) -> None:
    """`/item/get` -> the one institution this connection belongs to.

    The roster grows a row per *connection*, not per catalogue page, and this
    body carries exactly that: `item.institution_id` and `item.institution_name`
    are the institution behind the connection whose access token fetched it.

    Upserts on `source_institution_id`, because a rebuild does not empty this
    table -- accounts reference `institution_id`, and reassigning it would orphan
    them.

    The item's `available_products` is what `connections.capabilities` will be
    built from at enrollment (AC-3.2); `capabilities_of` in the client reads it,
    and writing that column is build step 3's job rather than a deriver's.
    """
    payload = _payload(response)
    item = payload.get("item")
    if not isinstance(item, dict):
        raise DerivationError(
            f"raw response {response.raw_response_id} ({ITEM_GET}) has no item object"
        )
    _upsert_institution(
        conn,
        source_institution_id=_required(item.get("institution_id"), "institution_id", response),
        name=_required(item.get("institution_name"), "institution name", response),
        seen_at=response.received_at,
    )


#: The `sync_state` row a transaction sync belongs to. One domain today; the
#: column exists because balances and holdings advance on their own schedules and
#: a single cursor per connection would make one of them wait for another.
TRANSACTIONS_DOMAIN: Final = "transactions"


def derive_transactions_sync(
    conn: SAConnection, response: RawResponse, context: DerivationContext
) -> None:
    """`/transactions/sync` -> the cursor that follows this page.

    🔴 **The cursor is written HERE, by the deriver, and that is the whole point.**
    AC-2.1 requires it to be persisted transactionally with the data it
    accompanies and AC-2.5 requires a crash mid-sync not to advance it. Those are
    one requirement stated twice, and a caller that wrote the cursor beside the
    derivation would satisfy them only for as long as everyone remembered to keep
    the two inside one transaction. Deriving the cursor from the same body as the
    rows makes them commit together because they are produced together -- there is
    no ordering left for anyone to get wrong.

    🔴 **A response with no `next_cursor` does not move the cursor.** A
    `NOT_READY` reply carries an empty one *(measured, `api-notes-plaid.md` §17)*,
    and storing it would mean either "start from the beginning" or, worse,
    overwriting a good cursor with nothing.

    Transaction rows are not written here yet -- they are the next chunk. The
    boundary is built and proved first, because every plausible implementation of
    this endpoint satisfies or violates both acceptance criteria at this one seam.
    """
    if response.connection_id is None:
        raise DerivationError(
            f"raw response {response.raw_response_id} ({TRANSACTIONS_SYNC}) was archived "
            f"without a connection, so there is no cursor for it to advance. A sync page "
            f"belongs to exactly one connection and nothing else can say which"
        )
    payload = _payload(response)
    next_cursor = payload.get("next_cursor")
    if not isinstance(next_cursor, str) or not next_cursor:
        return

    _apply_transaction_changes(conn, response, context)

    existing = conn.execute(
        select(sync_state.c.connection_id).where(
            sync_state.c.connection_id == response.connection_id,
            sync_state.c.domain == TRANSACTIONS_DOMAIN,
        )
    ).one_or_none()
    if existing is None:
        conn.execute(
            insert(sync_state).values(
                connection_id=response.connection_id,
                domain=TRANSACTIONS_DOMAIN,
                cursor=next_cursor,
                last_success_at=response.received_at,
                updated_at=response.received_at,
            )
        )
        return
    conn.execute(
        update(sync_state)
        .where(
            sync_state.c.connection_id == response.connection_id,
            sync_state.c.domain == TRANSACTIONS_DOMAIN,
        )
        .values(
            cursor=next_cursor,
            last_success_at=response.received_at,
            last_error_code=None,
            last_error_at=None,
            updated_at=response.received_at,
        )
    )


def _parse_calendar(value: object, what: str, response: RawResponse) -> CalendarDate:
    """`YYYY-MM-DD` from the aggregator into a calendar date, or refuse.

    🔴 A transaction date is a **calendar fact from the institution**, not an
    instant. Treating it as one introduces silent off-by-one-day errors at period
    boundaries -- exactly where period-over-period comparison lives, which is this
    product's headline query.
    """
    if not isinstance(value, str) or not value:
        raise DerivationError(
            f"raw response {response.raw_response_id} ({response.endpoint}) has no {what}"
        )
    try:
        return calendar_date(date.fromisoformat(value))
    except ValueError as exc:
        raise DerivationError(
            f"raw response {response.raw_response_id} ({response.endpoint}) has {what} "
            f"{value!r}, which is not a calendar date"
        ) from exc


def _operator_signed_amount(
    amount: object, currency: str, response: RawResponse
) -> MinorUnits:
    """🔴 The aggregator's sign, inverted to the operator's point of view.

    *Measured* (`api-notes-plaid.md` §17): a merchant purchase on a depository
    account arrives as a **positive** `amount` -- money leaving. `data-model.md`
    § Direction stores a positive `amount_minor` as money moving *into* an
    account, so every amount is negated on the way in. Taking the aggregator's
    sign at face value would be wrong **by twice the amount on every spend row**,
    silently and plausibly, because each number is well-formed and every sum
    completes.

    `from_decimal_string`, not `to_minor`: a transaction is a **ledger amount**
    and is converted exactly or refused. `to_minor` rounds, and it exists for
    investment valuations, which are price times quantity rather than a sum of
    money that moved.
    """
    # 🔴 `parse_float=str` leaves JSON INTEGERS as `int`, so a whole-dollar amount
    # arrives as one. `to_minor` already carried this conversion for balances; the
    # transaction path did not, and every fixture here used a fractional amount --
    # so 505 tests passed while the first real sandbox sync refused every
    # whole-dollar transaction it fetched. An int is exact, so `str()` loses
    # nothing; `bool` is excluded because it is an `int` and `str(True)` is not a
    # number.
    if isinstance(amount, int) and not isinstance(amount, bool):
        amount = str(amount)
    if not isinstance(amount, str) or not amount:
        raise DerivationError(
            f"raw response {response.raw_response_id} ({response.endpoint}) has a "
            f"transaction whose amount is {type(amount).__name__}, not a number the "
            f"aggregator sent as text; the column is NOT NULL and a placeholder would be "
            f"this system inventing a number and recording it as the source's"
        )
    try:
        exact = from_decimal_string(amount, exponent=minor_digits(currency))
    except MoneyError as exc:
        # Wrapped so the refusal names the response, like every other refusal
        # here. A bare `MoneyError` says an amount was unrepresentable and not
        # which page it came from -- and a page is what an operator can re-fetch.
        raise DerivationError(
            f"raw response {response.raw_response_id} ({response.endpoint}) has transaction "
            f"amount {amount!r} in {currency}, which cannot be represented exactly in minor "
            f"units: {exc}. A ledger amount is converted exactly or refused -- rounding one "
            f"loses a fraction of a cent per row, which reconciles to nothing"
        ) from exc
    return negate(exact)


def _category(payload: object, key: str) -> str | None:
    """One half of `personal_finance_category`, as the source sent it.

    Kept as sent. `category_override` is where local intent goes, because
    overwriting a source value destroys the ability to re-derive from the archive.
    """
    if not isinstance(payload, dict):
        return None
    return _optional(payload.get(key))


def _account_ids(conn: SAConnection, connection_id: int) -> dict[str, int]:
    """This connection's accounts, by the aggregator's id for them.

    Read once per page rather than per transaction: a page is up to a hundred
    rows and they cluster on a handful of accounts.
    """
    rows = conn.execute(
        select(accounts.c.source_account_id, accounts.c.account_id).where(
            accounts.c.connection_id == connection_id,
            accounts.c.source_account_id.is_not(None),
        )
    ).all()
    return {str(row[0]): int(row[1]) for row in rows}


def _apply_transaction_changes(
    conn: SAConnection, response: RawResponse, context: DerivationContext
) -> None:
    """`added`, `modified` and `removed`, in the one transaction the cursor rides.

    🔴 **Nothing here issues a DELETE.** AC-2.2 soft-deletes: a removed
    transaction keeps its row and gains a `removed_at`, because a transaction
    that vanished is a fact about the source, and a row that vanished with it
    leaves nothing to reconcile against.
    """
    assert response.connection_id is not None  # the caller refused None already
    payload = _payload(response)
    known = _account_ids(conn, response.connection_id)

    for entry in _entries(payload, "added", response):
        _write_transaction(conn, entry, response, context, known, existing_ok=False)
    for entry in _entries(payload, "modified", response):
        _write_transaction(conn, entry, response, context, known, existing_ok=True)
    for entry in _entries(payload, "removed", response):
        _mark_removed(conn, entry, response, known)


def _entries(payload: dict[str, Any], key: str, response: RawResponse) -> list[dict[str, Any]]:
    """One change list. Absent and empty are the same thing; a wrong type is not.

    A page that carried a malformed list would otherwise be treated as a page
    with no changes -- and a sync that silently applied nothing would advance its
    cursor past the transactions it skipped.
    """
    value = payload.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise DerivationError(
            f"raw response {response.raw_response_id} ({response.endpoint}) has {key} as "
            f"{type(value).__name__}, not a list"
        )
    entries: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise DerivationError(
                f"raw response {response.raw_response_id} ({response.endpoint}) has a "
                f"{key} entry that is not an object"
            )
        entries.append(item)
    return entries


def _local_account(
    entry: dict[str, Any], known: dict[str, int], response: RawResponse
) -> int:
    """The local account a transaction hangs from, or a refusal naming the gap.

    🔴 Refused rather than skipped. A transaction whose account this system has
    not derived yet means the accounts for this connection are older than its
    transactions -- and silently dropping the row would let the cursor advance
    past it, so it would never be offered again.
    """
    source_account_id = entry.get("account_id")
    if not isinstance(source_account_id, str) or source_account_id not in known:
        raise DerivationError(
            f"raw response {response.raw_response_id} ({response.endpoint}) has a "
            f"transaction for account {source_account_id!r}, which this connection has no "
            f"row for. Accounts are derived before transactions; syncing them in the other "
            f"order would advance the cursor past rows with nowhere to go"
        )
    return known[source_account_id]


def _write_transaction(
    conn: SAConnection,
    entry: dict[str, Any],
    response: RawResponse,
    context: DerivationContext,
    known: dict[str, int],
    *,
    existing_ok: bool,
) -> None:
    """One added or modified transaction, converged on its source identity.

    🔴 **The pending→posted transition is a match, not an insert.** A posting
    transaction arrives with a NEW `transaction_id` and the pending row's id in
    `pending_transaction_id`; AC-2.3 requires it to update that row rather than
    duplicate it. The row keeps its local `transaction_id`, so anything already
    pointing at it -- a category override, a rule -- survives the transition.
    """
    account_id = _local_account(entry, known, response)
    source_transaction_id = _required(entry.get("transaction_id"), "a transaction id", response)
    currency = _required(entry.get("iso_currency_code"), "a transaction currency", response)
    pending_source_id = _optional(entry.get("pending_transaction_id"))
    category = entry.get("personal_finance_category")

    values: dict[str, Any] = {
        "account_id": account_id,
        "source_transaction_id": source_transaction_id,
        "source_pending_transaction_id": pending_source_id,
        "pending": 1 if entry.get("pending") else 0,
        "posted_date": _parse_calendar(entry.get("date"), "a transaction date", response),
        "authorized_date": (
            None
            if entry.get("authorized_date") is None
            else _parse_calendar(entry.get("authorized_date"), "an authorized date", response)
        ),
        "amount_minor": _operator_signed_amount(entry.get("amount"), currency, response),
        "currency": currency,
        "description": _required(entry.get("name"), "a transaction description", response),
        "merchant_name": _optional(entry.get("merchant_name")),
        "source_category_primary": _category(category, "primary"),
        "source_category_detailed": _category(category, "detailed"),
        "source": "aggregator",
        "raw_response_id": response.raw_response_id,
        "derivation_version_id": context.derivation_version_id,
        "updated_at": response.received_at,
    }

    row_id = _existing_transaction(conn, account_id, source_transaction_id, pending_source_id)
    if row_id is None:
        if existing_ok:
            # A `modified` entry for a row this system has never seen. Inserted
            # rather than refused: a rebuild replays pages in archive order, and
            # a modification whose original page predates the archive is exactly
            # what that produces.
            _log.info(
                "raw response %s modified a transaction with no local row; inserting it",
                response.raw_response_id,
            )
        conn.execute(insert(transactions).values(first_seen_at=response.received_at, **values))
        return
    conn.execute(
        update(transactions).where(transactions.c.transaction_id == row_id).values(
            # 🔴 `removed_at` is cleared: a transaction the source removed and
            # then sent again is present again, and a stale removal stamp would
            # keep it out of every sum while its row said otherwise.
            removed_at=None,
            **values,
        )
    )


def _existing_transaction(
    conn: SAConnection,
    account_id: int,
    source_transaction_id: str,
    pending_source_id: str | None,
) -> int | None:
    """The row this change belongs to: its own, or the pending one it posts.

    Its own identity is checked first. A posting transaction carries both -- a new
    id of its own and the pending id it replaces -- and once it has been applied
    the row answers to the new id, so a replay must find it there rather than
    matching the pending id a second time and inserting.
    """
    row = conn.execute(
        select(transactions.c.transaction_id).where(
            transactions.c.account_id == account_id,
            transactions.c.source_transaction_id == source_transaction_id,
        )
    ).one_or_none()
    if row is not None:
        return int(row[0])
    if pending_source_id is None:
        return None
    pending_row = conn.execute(
        select(transactions.c.transaction_id).where(
            transactions.c.account_id == account_id,
            transactions.c.source_transaction_id == pending_source_id,
        )
    ).one_or_none()
    return None if pending_row is None else int(pending_row[0])


def _mark_removed(
    conn: SAConnection, entry: dict[str, Any], response: RawResponse, known: dict[str, int]
) -> None:
    """A soft delete. The row stays; it gains a removal stamp.

    A `removed` entry carries only `transaction_id` and `account_id` *(measured,
    §16)*, which is all a soft delete needs. A removal naming a row this system
    does not have is not an error -- a rebuild replays removals whose original
    page predates the archive, and there is nothing to do about it.
    """
    source_transaction_id = _required(entry.get("transaction_id"), "a removed id", response)
    account_id = _local_account(entry, known, response)
    conn.execute(
        update(transactions)
        .where(
            transactions.c.account_id == account_id,
            transactions.c.source_transaction_id == source_transaction_id,
            transactions.c.removed_at.is_(None),
        )
        .values(removed_at=response.received_at, updated_at=response.received_at)
    )


def _upsert_institution(
    conn: SAConnection, *, source_institution_id: str, name: str, seen_at: UtcInstant
) -> int:
    """One institution row, converged rather than inserted.

    `first_seen_at` takes the earliest mention and `last_seen_at` the latest, so
    the values do not depend on the order the archive is replayed in -- which is
    what makes the order-independence property a property of the arithmetic
    rather than of today's `ORDER BY`.
    """
    existing = conn.execute(
        select(
            institutions.c.institution_id,
            institutions.c.first_seen_at,
            institutions.c.last_seen_at,
        ).where(institutions.c.source_institution_id == source_institution_id)
    ).one_or_none()
    if existing is None:
        result = conn.execute(
            insert(institutions).values(
                source_institution_id=source_institution_id,
                name=name,
                first_seen_at=seen_at,
                last_seen_at=seen_at,
            )
        )
        primary_key = result.inserted_primary_key
        assert primary_key is not None  # an INTEGER PRIMARY KEY insert always yields one
        return int(primary_key[0])

    institution_id, first_seen_at, last_seen_at = existing
    conn.execute(
        update(institutions)
        .where(institutions.c.institution_id == institution_id)
        .values(
            name=name,
            first_seen_at=min(first_seen_at, seen_at),
            last_seen_at=max(last_seen_at, seen_at),
        )
    )
    return int(institution_id)


def derive_accounts(conn: SAConnection, response: RawResponse, context: DerivationContext) -> None:
    """`/accounts/get` -> the `accounts` and `balances_daily` tables.

    The connection comes from the *archived row*, not from the body: the sync
    path records which connection it fetched for, and that local id is the one
    `accounts.connection_id` means. Reading `item.institution_id` out of the body
    instead would re-derive a link the archive already holds, and the two could
    disagree after a re-enrollment.
    """
    if response.connection_id is None:
        raise DerivationError(
            f"raw response {response.raw_response_id} ({ACCOUNTS_GET}) was archived without a "
            f"connection, so there is nothing to attach its accounts to. Deriving them against "
            f"no connection would make them indistinguishable from the manual-import path, and "
            f"the source identity index would stop preventing duplicates"
        )
    institution_id = conn.execute(
        select(connections.c.institution_id).where(
            connections.c.connection_id == response.connection_id
        )
    ).scalar_one_or_none()
    if institution_id is None:
        raise DerivationError(
            f"raw response {response.raw_response_id} names connection "
            f"{response.connection_id}, which is not in this datastore"
        )

    payload = _payload(response)
    listed = payload.get("accounts")
    if not isinstance(listed, list):
        raise DerivationError(
            f"raw response {response.raw_response_id} ({ACCOUNTS_GET}) has no accounts list"
        )
    for entry in listed:
        if not isinstance(entry, dict):
            raise DerivationError(
                f"raw response {response.raw_response_id} lists a "
                f"{type(entry).__name__} among its accounts"
            )
        _derive_one_account(
            conn,
            response=response,
            context=context,
            institution_id=int(institution_id),
            entry=entry,
        )


def _derive_one_account(
    conn: SAConnection,
    *,
    response: RawResponse,
    context: DerivationContext,
    institution_id: int,
    entry: dict[str, Any],
) -> None:
    source_account_id = _required(entry.get("account_id"), "account_id", response)
    account_type = _required(entry.get("type"), "account type", response)
    balances = entry.get("balances")
    if not isinstance(balances, dict):
        raise DerivationError(
            f"raw response {response.raw_response_id} gives account {source_account_id} no "
            f"balances object"
        )
    currency = _optional(balances.get("iso_currency_code")) or _optional(
        balances.get("unofficial_currency_code")
    )
    if currency is None:
        raise DerivationError(
            f"raw response {response.raw_response_id} gives account {source_account_id} a "
            f"balance in no stated currency; storing an amount whose unit is unknown is how a "
            f"total silently mixes two of them"
        )

    account_id = _upsert_account(
        conn,
        response=response,
        institution_id=institution_id,
        source_account_id=source_account_id,
        entry=entry,
        account_type=account_type,
        currency=currency,
    )
    _write_balance(
        conn,
        response=response,
        context=context,
        account_id=account_id,
        balances=balances,
        currency=currency,
        balance_class=balance_class_of(account_type, response),
    )


def _upsert_account(
    conn: SAConnection,
    *,
    response: RawResponse,
    institution_id: int,
    source_account_id: str,
    entry: dict[str, Any],
    account_type: str,
    currency: str,
) -> int:
    """One account row, converged on its source identity.

    Matched explicitly rather than through the unique index: that index spans
    `(connection_id, source_account_id)`, and SQLite treats NULLs as distinct, so
    relying on a conflict would let the manual-import path's null connections
    duplicate silently.
    """
    existing = conn.execute(
        select(accounts.c.account_id, accounts.c.first_seen_date).where(
            accounts.c.connection_id == response.connection_id,
            accounts.c.source_account_id == source_account_id,
        )
    ).one_or_none()
    seen_date = _as_of(response.received_at)
    values: dict[str, Any] = {
        "institution_id": institution_id,
        "connection_id": response.connection_id,
        "source_account_id": source_account_id,
        "source_persistent_account_id": _optional(entry.get("persistent_account_id")),
        "name": _required(entry.get("name"), "account name", response),
        "official_name": _optional(entry.get("official_name")),
        # Absent on plenty of real accounts, and nullable for exactly that reason.
        "mask": _optional(entry.get("mask")),
        "account_type": account_type,
        "account_subtype": _optional(entry.get("subtype")),
        "balance_class": balance_class_of(account_type, response),
        "currency": currency,
        "lifecycle_status": "active",
        "source": "aggregator",
        "updated_at": response.received_at,
    }
    if existing is None:
        result = conn.execute(
            insert(accounts).values(
                **values,
                first_seen_date=seen_date,
                created_at=response.received_at,
            )
        )
        primary_key = result.inserted_primary_key
        assert primary_key is not None  # an INTEGER PRIMARY KEY insert always yields one
        return int(primary_key[0])

    account_id, first_seen_date = existing
    # 🔴 **An update owns fewer columns than an insert, and the difference is the
    # point.** Applying one `values` dict to both would make derivation the
    # permanent owner of every column it names -- so an operator's correction to
    # `balance_class`, which `data-model.md` declares operator-correctable, would
    # be silently reverted on the next sync, and `lifecycle_status` would be
    # pinned to "active" by the code that is supposed to be able to retire it.
    #
    # `first_seen_date` takes the earliest mention rather than the latest, so a
    # replay in any order converges on the same row.
    conn.execute(
        update(accounts)
        .where(accounts.c.account_id == account_id)
        .values(
            **{k: v for k, v in values.items() if k not in _OPERATOR_OWNED},
            first_seen_date=min(first_seen_date, seen_date),
        )
    )
    return int(account_id)


def _write_balance(
    conn: SAConnection,
    *,
    response: RawResponse,
    context: DerivationContext,
    account_id: int,
    balances: dict[str, Any],
    currency: str,
    balance_class: str,
) -> None:
    """One day's balance for one account, signed from the operator's point of view.

    🔴 **A liability's `current_minor` is stored negative**, whatever sign the
    aggregator used -- several report a card balance as a positive amount owed,
    and a consumer taking that at face value is wrong by twice the debt,
    silently and plausibly. One convention rather than one per account type is
    what lets net worth be a plain sum and AC-11.2's reconciliation be "change
    in balance equals sum of transactions" for every account.

    `available_minor` and `limit_minor` are the documented exceptions and keep
    the magnitudes the source reported: neither participates in net worth, and
    "available credit" is not a negative quantity from anyone's point of view.

    The row references the **local** `account_id` (AC-6.3), never the
    aggregator's, because the aggregator's changes when a connection is removed
    and re-linked, and history that pointed at it would detach.
    """
    current = to_minor(balances.get("current"), currency, "a current balance", response)
    if balance_class == "liability" and current > 0:
        current = negate(current)

    available_raw = balances.get("available")
    limit_raw = balances.get("limit")
    as_of = _as_of(response.received_at)
    values: dict[str, Any] = {
        "current_minor": current,
        "available_minor": (
            None
            if available_raw is None
            else to_minor(available_raw, currency, "an available balance", response)
        ),
        "limit_minor": (
            None if limit_raw is None else to_minor(limit_raw, currency, "a limit", response)
        ),
        "currency": currency,
        "captured_at": response.received_at,
        "source": "aggregator",
        "raw_response_id": response.raw_response_id,
        "manual_import_id": None,
        "derivation_version_id": context.derivation_version_id,
    }

    # 🔴 **One capture per account per day, and the first one wins.**
    #
    # `docs/system-requirements.md` AC-3.1, `data-model.md` and the DDL comment
    # above this table all say the same thing: a second capture on a day already
    # recorded is *rejected* rather than allowed to overwrite, so the series does
    # not depend on what time of day anyone happened to look, and a re-run
    # changes nothing (AC-2.4). No aggregator backfills a balance series, so a
    # day not captured is a day gone for good -- which is why the rule leans
    # toward keeping what is already there.
    #
    # Skipped rather than raised, because rejecting loudly would make a rebuild
    # fail on an archive that is perfectly legitimate: two `/accounts/get`
    # responses on one day is an ordinary thing for a sync to have recorded.
    #
    # **"First" is decided by comparing captures, not by arriving first.** Replay
    # order would give the same answer today and would stop doing so the moment
    # two responses shared a `received_at`, which the archive explicitly allows.
    #
    # This also protects a row the manual-import path wrote: overwriting one
    # would turn it into an aggregator row that the next rebuild deletes, since
    # a rebuild empties exactly the rows carrying a `raw_response_id`.
    existing = conn.execute(
        select(balances_daily.c.captured_at, balances_daily.c.raw_response_id).where(
            balances_daily.c.account_id == account_id,
            balances_daily.c.as_of_date == as_of,
        )
    ).one_or_none()
    if existing is not None:
        captured_at, raw_response_id = existing
        incoming = (response.received_at, response.raw_response_id)
        if (captured_at, raw_response_id or 0) <= incoming:
            return
        if raw_response_id is None:
            # 🔴 A row this deriver did not write, and must not remove.
            # `manual_import_id` rows come from FR-7's import path -- the
            # operator's own statement -- and a rebuild deletes exactly the rows
            # carrying a `raw_response_id`. Replacing one with an aggregator row
            # would make it disappear a rebuild later, with nothing connecting
            # the loss to the sync that caused it. The comparison above already
            # covers ties and later captures; this covers the *earlier* archived
            # response, which is the case the comparison would otherwise let
            # through.
            return
        # The archive holds an earlier capture for this day than the row that is
        # here. Replaying it must still land on the earliest, or a rebuild would
        # depend on the order rows happened to be written in the first place.
        conn.execute(
            delete(balances_daily).where(
                balances_daily.c.account_id == account_id,
                balances_daily.c.as_of_date == as_of,
            )
        )
    conn.execute(insert(balances_daily).values(account_id=account_id, as_of_date=as_of, **values))


#: What `bankmachine.derivers` composes into this build's registry.
#:
#: Keyed by the endpoint's own path, which is also what `store.raw` records, so a
#: rebuild years from now can still tell what a stored response was.
PLAID_DERIVERS: Final[Mapping[str, Deriver]] = {
    str(INSTITUTIONS_GET): derive_nothing,
    # Registered by name as deriving nothing, like the catalogue above and for the
    # same reason: its reply is `request_id` alone, so there is nothing in it to
    # normalize. Leaving it unregistered would be indistinguishable from the
    # endpoint nobody got round to. What a removal *means* -- the connection is
    # retired -- is a local decision recorded by the command that made it, not a
    # fact this response carries.
    str(ITEM_REMOVE): derive_nothing,
    str(ITEM_GET): derive_item,
    str(ACCOUNTS_GET): derive_accounts,
    str(TRANSACTIONS_SYNC): derive_transactions_sync,
}
