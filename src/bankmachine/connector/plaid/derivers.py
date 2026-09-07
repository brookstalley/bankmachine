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
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Final

from sqlalchemy import Connection as SAConnection
from sqlalchemy import delete, insert, select, update

from bankmachine.connector import ACCOUNTS_GET, INSTITUTIONS_GET, ITEM_GET
from bankmachine.logging_setup import get_logger
from bankmachine.store.derivation import DerivationContext, DerivationError, Deriver
from bankmachine.store.raw import RawResponse
from bankmachine.store.schema import accounts, balances_daily, connections, institutions
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
    str(ITEM_GET): derive_item,
    str(ACCOUNTS_GET): derive_accounts,
}
