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

import functools
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any, Final

from sqlalchemy import Connection as SAConnection
from sqlalchemy import Table, delete, insert, select, update

from bankmachine.connector import (
    ACCOUNTS_GET,
    INSTITUTIONS_GET,
    INVESTMENTS_HOLDINGS_GET,
    INVESTMENTS_TRANSACTIONS_GET,
    ITEM_GET,
    ITEM_REMOVE,
    TRANSACTIONS_SYNC,
    MalformedResponseError,
    parse_response_body,
)
from bankmachine.logging_setup import get_logger
from bankmachine.store.derivation import DerivationContext, DerivationError, Deriver
from bankmachine.store.raw import RawResponse
from bankmachine.store.schema import (
    TRANSACTIONS_DOMAIN,
    accounts,
    balances_daily,
    connections,
    holdings,
    institutions,
    investment_transactions,
    refused_holdings,
    securities,
    transactions,
)
from bankmachine.store.sync_domains import record_domain_success
from bankmachine.store.types import (
    CalendarDate,
    MinorUnits,
    MoneyError,
    TemporalError,
    UnknownMinorDigitsError,
    UtcInstant,
    calendar_date,
    from_decimal_string,
    minor_digits,
    negate,
    utc_instant,
)

_log = get_logger(__name__)

#: Columns an account row keeps once it has one, because something other than
#: this deriver owns them afterwards.
#:
#: `balance_class` is declared operator-correctable in `data-model.md` -- it
#: partitions a report rather than deciding an arithmetic sign, so the operator
#: gets the last word. `lifecycle_status` belongs to whatever retires an account.
#:
#: 🔴 **`lifecycle_status` stays here now that absence IS derivable, and that is
#: the point rather than an omission.** AC-12.2: the aggregator reports no closure
#: signal, so an account dropping out of `/accounts/get` is equally consistent
#: with closure, with de-selection from sharing, and with the institution changing
#: what it shares. A deriver writing `inactive` from that would be recording a
#: conclusion the response does not contain. What this deriver records instead is
#: the *observation* -- `last_seen_date`, below -- and `query._account_lifecycle`
#: derives `no_longer_reported` from it at read time, named for what was seen.
#: `inactive` remains the operator's own declaration, which is what lets AC-12.6
#: rank it above the derived signal.
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

    `parse_float=str` is the load-bearing argument, and it is a property of
    `parse_response_body` rather than of this call: without it `json.loads`
    builds a float and the exactness question is already lost --
    `from_decimal_string` would then be converting this machine's best rendering
    of a number rather than the number itself.

    🔴 **Ruling on the three parse failures: the shared helper, translated back
    to `DerivationError` so the seam's contract is unchanged.** A body that
    cannot be read is a refusal to derive one response, and `store.derivation`'s
    callers are written to catch that -- `sync run` degrades the connection it
    belongs to and carries on with the others. A syntax error already did that;
    a body nested past the stack and a body whose bytes are not UTF-8 raised
    `RecursionError` and `UnicodeDecodeError`, which share no base with
    `ValueError`, so they escaped the run and every connection queued behind
    this one went unsynced. `response.body` is `bytes`, so the parser does the
    decoding and the third mode is reachable here.
    """
    try:
        parsed = parse_response_body(
            response.body, what=f"raw response {response.raw_response_id} ({response.endpoint})"
        )
    except MalformedResponseError as exc:
        raise DerivationError(f"{exc}, so nothing can be derived from it") from exc
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


def _stated_currency(fields: dict[str, Any]) -> str | None:
    """Whichever of the two currency fields the aggregator populated, if either.

    🔴 **Both, everywhere an amount is read.** The aggregator nulls
    `iso_currency_code` and populates `unofficial_currency_code` for
    cryptocurrencies and other non-ISO instruments -- and reading only the ISO
    field in one place and both in another produced an account that could exist
    while none of its transactions could be derived, which stops the cursor
    dead. The code is kept as sent, so an unofficial one groups separately from
    every ISO total rather than being folded into one.
    """
    return _optional(fields.get("iso_currency_code")) or _optional(
        fields.get("unofficial_currency_code")
    )


class UndenominableAmountError(DerivationError):
    """One row is in a unit this build cannot express in minor units.

    🔴 **A ROW's refusal, never a connection's.** A `DerivationError` aborts the
    response that raised it, and for `/accounts/get` that is fetched first on
    every run -- so one unrepresentable holding would take the whole institution
    offline, on that run and every run after it. This subclass is what the two
    loops catch to skip the row and keep going: the account stays, its other
    rows derive, and the raw body keeps what was refused.

    🔴 **Refused, not approximated.** The alternative -- store the rounded value
    and flag it -- puts a number that is wrong by a fraction into the ledger and
    asks every later reader to notice a marker. The balance-sheet identities
    this store maintains would then be built on values that do not add up, and a
    figure wrong by 0.4% and marked is still wrong in every total computed from
    it.
    """


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
    # 🔴 **Rounded to the currency's OWN minor unit, which has to be known.**
    # This once defaulted an unrecognized code to two digits, which is right for
    # most fiat and wrong for every cryptocurrency: `0.04217` in a currency whose
    # minor unit is 1/100,000,000 became `0.04`, and the 0.4% it lost was
    # disclosed in a log line no caller of any surface ever sees. Rounding a
    # valuation to a scale the currency actually has is a recorded
    # approximation; rounding it to a scale that was guessed is a wrong number.
    try:
        exponent = minor_digits(currency)
    except UnknownMinorDigitsError as exc:
        raise UndenominableAmountError(
            f"raw response {response.raw_response_id} gives {what} in {currency}, and {exc}. "
            f"The row is refused rather than rounded; the archive keeps the value, so a build "
            f"that knows this currency's minor unit derives it exactly and nothing is lost"
        ) from exc
    try:
        return from_decimal_string(amount, exponent=exponent)
    except MoneyError:
        # Only reached when the value carries more precision than the currency
        # has. Re-derived through Decimal rather than caught-and-guessed: the
        # quantize is what decides the stored value, and it must be visible.
        rounded = Decimal(amount).quantize(Decimal(1).scaleb(-exponent), rounding=ROUND_HALF_EVEN)
        _disclose_rounding(
            _Rounding(response.raw_response_id, what, amount, currency, str(rounded))
        )
        return from_decimal_string(str(rounded), exponent=exponent)


@dataclass(frozen=True, slots=True)
class _Rounding:
    """One distinct rounding: the same value, rounded the same way, in one response."""

    raw_response_id: int
    what: str
    original: str
    currency: str
    rounded: str


#: The roundings of the response being derived, while a registered deriver runs.
_ROUNDINGS: ContextVar[dict[_Rounding, int] | None] = ContextVar("_ROUNDINGS", default=None)


def _disclose_rounding(rounding: _Rounding) -> None:
    """Count it toward the response's disclosure, or disclose it now if there is none."""
    tally = _ROUNDINGS.get()
    if tally is None:
        _log_rounding(rounding, 1)
        return
    tally[rounding] = tally.get(rounding, 0) + 1


def _log_rounding(rounding: _Rounding, times: int) -> None:
    _log.info(
        "raw response %s: %s of %s %s rounded to %s for storage, %s (a valuation, "
        "not a ledger amount; the archive keeps the original)",
        rounding.raw_response_id,
        rounding.what,
        rounding.original,
        rounding.currency,
        rounding.rounded,
        "once" if times == 1 else f"{times} times",
    )


def _discloses_rounding(deriver: Deriver) -> Deriver:
    """The deriver, disclosing its roundings once per distinct value when it returns.

    🔴 **Every rounding is still disclosed; what changed is the unit.** One line
    per row buried a sync's own report: the investments feed re-derives its whole
    window on every run, so a price repeated across a portfolio's history printed
    the same sentence dozens of times, and the lines naming the connection and its
    shortfall scrolled away above them. Grouping keeps the value, the currency,
    the result and the response, and adds the count a reader was tallying by hand.

    Nothing is disclosed when the deriver raises. Its transaction rolls back, so
    nothing was rounded for storage, and `apply_response` logs the failure itself.

    Applied to the registry rather than taken as a parameter by `to_minor`, so
    every path that derives -- sync, enrollment, reauth and rebuild -- gets it by
    going through the one registry, and no caller can forget to pass it.
    """

    @functools.wraps(deriver)
    def disclosing(conn: SAConnection, response: RawResponse, context: DerivationContext) -> None:
        tally: dict[_Rounding, int] = {}
        token = _ROUNDINGS.set(tally)
        try:
            deriver(conn, response, context)
        finally:
            _ROUNDINGS.reset(token)
        for rounding, times in tally.items():
            _log_rounding(rounding, times)

    return disclosing


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

    The item's `products` and `available_products` together are what
    `connections.capabilities` is built from at enrollment (AC-3.2) -- neither
    alone, because the aggregator makes them mutually exclusive and so each
    omits what the other holds. `capabilities_of` in the client reads them, and
    writing that column belongs to enrollment rather than to a deriver.
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
    _record_item_standing(conn, item=item, response=response)


def _record_item_standing(
    conn: SAConnection, *, item: dict[str, Any], response: RawResponse
) -> None:
    """The two facts about the Item itself, which nothing used to read.

    🔴 **Both were archived and discarded, and that is why a dying connection
    reported healthy.** The pipeline is poll-only, so an expiring consent
    otherwise surfaces as a failure on the NEXT run rather than in advance --
    and the date to say so in advance was sitting in the archive the whole time.

    🔴 **`error` is written even when it is null, and that is the point.** The
    aggregator clears the field once the Item recovers, so a null is a real
    observation -- *the aggregator is not complaining now* -- and skipping the
    write would leave a resolved complaint standing forever. It is NOT folded
    into `last_error_code`, which records the last sync ATTEMPT failing: an Item
    can be unwell while the most recent poll happened to succeed, and merging
    them would let that success erase a complaint nobody resolved.

    🔴 **A missing key is not the same as a null value**, and only the second is
    written. A body that omits `consent_expiration_time` entirely says nothing
    about consent, so blanking a date already recorded would discard the only
    warning the operator was going to get.

    🔴 **`connections.updated_at` is NOT written here, and the omission is
    load-bearing.** That column is stamped by the commands that change the row --
    enrollment, retirement, a degrade, a recorded success, a repair -- and
    `rebuild.content_digest` hashes it like any other. A deriver that also set it
    would overwrite the live value with the archived `received_at` on every
    replay, so `store rebuild` would report content changed at an unchanged
    `DERIVATION_VERSION` and refuse -- blaming an impure deriver or a pruned
    archive, when the truth is that one column had two owners. The columns below
    are derived from the body and replay to the same values; `updated_at` is not
    one of them. (`accounts.updated_at` is owned the other way round, by its
    deriver, because every column on that table is derived.)
    """
    values: dict[str, Any] = {}
    if "consent_expiration_time" in item:
        raw = item.get("consent_expiration_time")
        values["consent_expires_at"] = (
            None if raw is None else _parse_instant(raw, "a consent expiry", response)
        )
    if "error" in item:
        error = item.get("error")
        values["source_error_code"] = (
            _optional(error.get("error_code")) if isinstance(error, dict) else None
        )
    if not values:
        return
    conn.execute(
        update(connections)
        .where(connections.c.connection_id == response.connection_id)
        .values(**values)
    )


#: The one `transactions_update_status` that says a backfill has fully landed.
#: `INITIAL_UPDATE_COMPLETE` hands over the first stretch with the rest still
#: arriving, and `NOT_READY` hands over nothing yet.
HISTORICAL_UPDATE_COMPLETE = "HISTORICAL_UPDATE_COMPLETE"


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

    🔴 **A response with no `next_cursor` skips the CURSOR, not the page.** A
    `NOT_READY` reply carries an empty one *(measured, `api-notes-plaid.md` §17)*,
    and storing it would mean either "start from the beginning" or, worse,
    overwriting a good cursor with nothing. But that is a rule about the cursor
    alone: a page carrying changes and no cursor keeps its rows and leaves the
    cursor where it was, because discarding them would lose transactions the
    aggregator has already handed over and has no reason to send again. And it
    does not stop the domain landing: an institution with no cash accounts
    finishes its backfill at `HISTORICAL_UPDATE_COMPLETE` with nothing in it and
    no cursor to give, and a domain left unstamped there reads as never landed on
    every answer, for as long as the connection exists.

    Transaction rows are written here too, and the ordering is not incidental:
    the rows go in before the cursor, so a row that cannot be written stops the
    cursor from moving past the page it came from. That is a positional
    guarantee, and the transaction is what makes it a real one -- a cursor
    already written is rolled back with everything else, which is asserted
    separately from the ordering because the two fail differently.
    """
    if response.connection_id is None:
        raise DerivationError(
            f"raw response {response.raw_response_id} ({TRANSACTIONS_SYNC}) was archived "
            f"without a connection, so there is no cursor for it to advance. A sync page "
            f"belongs to exactly one connection and nothing else can say which"
        )
    payload = _payload(response)
    # 🔴 Before the change lists, because they depend on it. An account that has
    # closed, been de-selected in Account Select, or stopped being shared drops
    # out of `/accounts/get` while this endpoint keeps emitting deltas for its
    # rows -- and the aggregator names it right here, in the same body. Without
    # this the page refused forever: the raw body committed, the derivation
    # rolled back, the cursor never moved, and every later run re-fetched and
    # re-archived the identical page until the archive itself could not be
    # replayed.
    _derive_carried_accounts(conn, response=response, context=context, payload=payload)

    applied = _apply_transaction_changes(conn, response, context)

    next_cursor = payload.get("next_cursor")
    if not isinstance(next_cursor, str) or not next_cursor:
        if applied:
            # 🔴 A shape nothing has observed, and the reason the rows go in
            # first. The guard is right for `NOT_READY`, which carries an empty
            # cursor and no changes -- but standing before the change lists it
            # discarded the rows of any page that carried both, with no error and
            # no trace. A page like that saying `has_more` would then be
            # re-fetched to the page ceiling and reported as stopped short, so
            # the operator reads "run again to continue" about a run that never
            # can.
            _log.warning(
                "raw response %s (%s) carried %d change(s) and no cursor; the changes are "
                "applied and the cursor stays where it was",
                response.raw_response_id,
                response.endpoint,
                applied,
            )
        # 🔴 The cursor stays unstored, but the domain is landed when the
        # aggregator says the backfill is. An institution with no cash accounts
        # ends its backfill here with nothing to hand over and no cursor to give.
        if payload.get("transactions_update_status") == HISTORICAL_UPDATE_COMPLETE:
            record_domain_success(
                conn,
                connection_id=response.connection_id,
                domain=TRANSACTIONS_DOMAIN,
                at=response.received_at,
            )
        return

    record_domain_success(
        conn,
        connection_id=response.connection_id,
        domain=TRANSACTIONS_DOMAIN,
        at=response.received_at,
        cursor=next_cursor,
    )


def _derive_carried_accounts(
    conn: SAConnection,
    *,
    response: RawResponse,
    context: DerivationContext,
    payload: dict[str, Any],
) -> None:
    """The `accounts` array a body carries alongside its own rows, if it has one.

    🔴 **Not the roster.** `/accounts/get` answers *these are the accounts this
    connection has*; every other endpoint's array answers *these are the accounts
    the rows in this body belong to*, so `roster=False` keeps
    `accounts.last_seen_date` a record of having LOOKED rather than of having
    seen a name in passing.

    🔴 **Why every such endpoint derives it.** An account that has closed, been
    de-selected in Account Select, or stopped being shared drops out of
    `/accounts/get` while the endpoints that report its rows keep naming it. A
    body whose rows hang from an account this datastore has no row for refuses —
    correctly, because the alternative is a row pointing at nothing — and that
    refusal would then be permanent: the body is archived, the derivation rolls
    back, and every later run and every rebuild reproduces it. The aggregator
    names the account right here, in the same body, which is what makes the
    refusal avoidable rather than merely regrettable.

    An absent array is not an empty one: a body that says nothing about accounts
    leaves the roster alone.
    """
    listed = payload.get("accounts")
    if listed is None:
        return
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
    _derive_account_entries(
        conn,
        response=response,
        context=context,
        institution_id=int(institution_id),
        listed=listed,
        roster=False,
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


def _parse_instant(value: object, what: str, response: RawResponse) -> UtcInstant:
    """An ISO 8601 instant from the aggregator into a UTC instant, or refuse.

    🔴 The mirror of `_parse_calendar`, and separate from it for the reason
    `data-model.md` § Direction gives: a consent expiry is a moment in time the
    aggregator states with a zone, not a calendar fact an institution reports.
    Reading one as the other is the mixing that section forbids, and `utc_instant`
    refuses a naive value rather than assuming the reader's offset.
    """
    if not isinstance(value, str) or not value:
        raise DerivationError(
            f"raw response {response.raw_response_id} ({response.endpoint}) has no {what}"
        )
    try:
        return utc_instant(datetime.fromisoformat(value))
    except (ValueError, TemporalError) as exc:
        raise DerivationError(
            f"raw response {response.raw_response_id} ({response.endpoint}) has {what} "
            f"{value!r}, which is not an instant this store can record"
        ) from exc


def _operator_signed_amount(amount: object, currency: str, response: RawResponse) -> MinorUnits:
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
    except UnknownMinorDigitsError as exc:
        # 🔴 Distinguished from the refusal below, because the two are refused
        # at different scopes. A sub-cent amount in a currency this build knows
        # is a body it cannot interpret and the page is refused; an amount in a
        # currency whose minor unit is unknown is a fact about that one row, and
        # taking the connection offline over it would lose every other row on
        # the page for the lifetime of the unknown currency.
        raise UndenominableAmountError(
            f"raw response {response.raw_response_id} ({response.endpoint}) has transaction "
            f"amount {amount!r} in {currency}, and {exc}. The row is refused rather than "
            f"rounded; the archive keeps it, so a build that knows this currency's minor unit "
            f"derives it exactly"
        ) from exc
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
) -> int:
    """`added`, `modified` and `removed`, in the one transaction the cursor rides.

    Returns how many changes the page carried, which is what lets the caller tell
    a page with nothing to say from a page whose rows would otherwise vanish.

    🔴 **Nothing here issues a DELETE.** AC-2.2 soft-deletes: a removed
    transaction keeps its row and gains a `removed_at`, because a transaction
    that vanished is a fact about the source, and a row that vanished with it
    leaves nothing to reconcile against.
    """
    assert response.connection_id is not None  # the caller refused None already
    payload = _payload(response)
    known = _account_ids(conn, response.connection_id)

    added = _entries(payload, "added", response)
    modified = _entries(payload, "modified", response)
    removed = _entries(payload, "removed", response)
    for entry in added:
        _write_one_change(conn, entry, response, context, known, existing_ok=False)
    for entry in modified:
        _write_one_change(conn, entry, response, context, known, existing_ok=True)
    for entry in removed:
        _mark_removed(conn, entry, response, known)
    return len(added) + len(modified) + len(removed)


def _write_one_change(
    conn: SAConnection,
    entry: dict[str, Any],
    response: RawResponse,
    context: DerivationContext,
    known: dict[str, int],
    *,
    existing_ok: bool,
) -> None:
    """One change from the page, refusing THIS row where it must and no more.

    🔴 **The only refusal that stops here is a unit this build cannot express.**
    A row in a currency whose minor unit is unknown cannot be stored exactly and
    is not stored approximately, but it is one row: letting it escape would
    abort the page, leave the cursor where it was, and re-fetch and re-refuse the
    same body on every run afterwards -- so a single unrepresentable holding
    would take the whole institution's history offline indefinitely. Every other
    `DerivationError` still escapes, because every other one says the body could
    not be interpreted, which is a fact about the page rather than about a row.

    The raw response keeps what was refused, so nothing is lost and a build that
    knows the currency's minor unit derives it on the next `store rebuild`.
    """
    try:
        _write_transaction(conn, entry, response, context, known, existing_ok=existing_ok)
    except UndenominableAmountError as exc:
        _log.warning(
            "raw response %s: one transaction is in a unit this build cannot express in minor "
            "units, so it is not derived and the rest of the page is -- %s",
            response.raw_response_id,
            exc,
        )


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


def _local_account(entry: dict[str, Any], known: dict[str, int], response: RawResponse) -> int:
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
    currency = _stated_currency(entry)
    if currency is None:
        raise DerivationError(
            f"raw response {response.raw_response_id} ({response.endpoint}) has a transaction "
            f"in no stated currency; storing an amount whose unit is unknown is how a total "
            f"silently mixes two of them"
        )
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
        absorbed_by = _row_that_absorbed(conn, account_id, source_transaction_id)
        if absorbed_by is not None:
            # 🔴 A change to a hold whose posting this system has already merged
            # in. The merged row answers to the POSTED id, so neither lookup
            # above finds it and an insert here would put the same purchase in
            # the ledger twice -- disclosed as an ordinary pending row, which is
            # indistinguishable from a hold that has simply not settled yet.
            #
            # 🔴 And the row is not rewritten from the hold's entry. Doing so
            # would move it back under the hold's identity and mark it pending
            # again, so the retirement the aggregator sends next (`removed`,
            # naming the hold) would soft-delete the purchase itself. The
            # posting is the later fact about the same money; a change to what
            # it superseded does not undo it.
            _log.info(
                "raw response %s modified a hold whose posting is already merged into "
                "transaction %d; the posting stands",
                response.raw_response_id,
                absorbed_by,
            )
            return
        if existing_ok:
            # A `modified` entry for a row this system has never seen. Inserted
            # rather than refused: a rebuild replays pages in archive order, and
            # a modification whose original page predates the archive is exactly
            # what that produces.
            _log.info(
                "raw response %s modified a transaction with no local row; inserting it",
                response.raw_response_id,
            )
        conn.execute(
            insert(transactions).values(
                first_seen_at=response.received_at,
                # 🔴 The day this money was committed from the account holder's
                # point of view, stamped ONCE here and named on no other path.
                #
                # The aggregator's `date` -- which `posted_date` mirrors -- is
                # the transaction date while a charge is pending and the POSTING
                # date once it settles. So a hold authorised on 06-28 and posted
                # on 07-02 moves between months on its own, and every window
                # measured on `posted_date` reports a different June depending
                # on when it is asked. This column does not move.
                #
                # 🔴 That it is absent from `values`, rather than filtered out of
                # the update below, is the whole mechanism: an UPDATE cannot
                # carry a column no dictionary contains. A later exclusion list
                # would be one edit away from being forgotten, and the symptom
                # -- a settled hold silently changing period again -- is
                # invisible in every total it corrupts.
                #
                # `authorized_date` is nullable and null for institutions that do
                # not report it, which is why the window is measured on this
                # coalesced column rather than on `authorized_date` itself: a
                # total measured on a nullable field would count some rows by
                # when the money was committed and others by when it cleared,
                # with nothing telling a caller which.
                ledger_date=values["authorized_date"] or values["posted_date"],
                # 🔴 The Item this row was produced under, stamped ONCE here for
                # the same reason `ledger_date` is: it is a fact about where the
                # row came from, and a later change to the same transaction is
                # not a change of origin. Absent from `values` rather than
                # filtered out of the update below, so no UPDATE can carry it.
                #
                # A re-link yields a new Item that re-issues every transaction
                # id, so the whole granted history arrives again as rows this
                # store has never seen, under an account it now recognises.
                # Nothing collides and nothing is deduped -- every row is kept,
                # and the read path counts the newest lineage over the range it
                # covers, older lineages only outside it, and says so.
                lineage_id=response.connection_id,
                **values,
            )
        )
        return
    conn.execute(
        update(transactions)
        .where(transactions.c.transaction_id == row_id)
        .values(
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


def _row_that_absorbed(
    conn: SAConnection, account_id: int, source_transaction_id: str
) -> int | None:
    """The row that already merged this hold in, if one did.

    `source_pending_transaction_id` is the only column that still holds the
    hold's id once the transition has happened, and nothing else in the lookup
    path reads it -- which is why a change arriving for the hold afterwards
    looked like a transaction this system had never seen.

    Unlike the two lookups above, this column carries no unique index: two
    postings could name one hold. The question asked here is only whether SOME
    row already absorbed it, so the oldest match answers it, and a page shaped
    that way must not take the connection down over which one.
    """
    row = conn.execute(
        select(transactions.c.transaction_id)
        .where(
            transactions.c.account_id == account_id,
            transactions.c.source_pending_transaction_id == source_transaction_id,
        )
        .order_by(transactions.c.transaction_id)
        .limit(1)
    ).first()
    return None if row is None else int(row[0])


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
    source_account_id = entry.get("account_id")
    if not isinstance(source_account_id, str) or source_account_id not in known:
        # 🔴 Skipped, where an addition is refused, because the two are not the
        # same risk. Refusing an addition protects a transaction that would
        # otherwise be lost behind an advancing cursor; there is no such row
        # here, and refusing stopped the cursor on a page whose only unusable
        # entry was a soft delete of something this system never held.
        # Counted rather than named: both ids are opaque runs the log formatter
        # redacts, and the raw response id is what leads back to them.
        _log.info(
            "raw response %s removes a transaction from an account this connection has no "
            "row for; there is nothing to soft-delete",
            response.raw_response_id,
        )
        return
    account_id = known[source_account_id]
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

    _derive_account_entries(
        conn,
        response=response,
        context=context,
        institution_id=int(institution_id),
        listed=_payload(response).get("accounts"),
        roster=True,
    )
    # 🔴 AC-12.5a: recorded AFTER the loop and OUTSIDE it, so a roster that
    # listed nothing still advances the observation. An empty `/accounts/get`
    # is a successful observation of zero accounts, not a failed fetch, and the
    # state that must not exist is the third one -- a response that type-checks,
    # runs this loop zero times, and leaves no record that anyone looked.
    _record_roster_observation(conn, response)


def _derive_account_entries(
    conn: SAConnection,
    *,
    response: RawResponse,
    context: DerivationContext,
    institution_id: int,
    listed: object,
    roster: bool,
) -> None:
    """The `accounts` array, from whichever endpoint carried it.

    🔴 **One deriver, and `roster` is the one thing the two callers disagree
    about.** `/accounts/get` answers *these are the accounts this connection
    has*; `/transactions/sync` answers *these are the accounts the transactions
    in this body belong to*. The rows are the same shape and mean the same
    thing, so a second deriver would be two descriptions of one fact drifting
    apart -- but only the first is an observation of the roster, and
    `accounts.last_seen_date` is a record of that observation rather than of
    having seen the account named anywhere.
    """
    if not isinstance(listed, list):
        raise DerivationError(
            f"raw response {response.raw_response_id} ({response.endpoint}) has no accounts list"
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
            institution_id=institution_id,
            entry=entry,
            roster=roster,
        )


def _record_roster_observation(conn: SAConnection, response: RawResponse) -> None:
    """AC-12.4: this connection's roster was READ, whatever it turned out to list.

    🔴 **The half that says *we looked*.** `accounts.last_seen_date` says *and
    this is what we found*, and the two are separate records rather than one,
    because deriving this from that collapses them: a maximum over the accounts
    that were listed moves with them, so a roster listing nothing leaves nothing
    behind it and "the roster was observed, and this account was not in it"
    becomes inexpressible exactly when it is true. At a one-account connection
    that is the ordinary case.

    🔴 **A monotone MAXIMUM, for the reason `last_seen_date` is one**: a repaired
    connection, a backfill and a manual re-derive all hand this deriver
    responses in whatever order they were archived, and an observation that
    moved backwards on a replay would mark accounts absent that a later roster
    had listed. Order-independence is a property of how the column is computed,
    not of today's `ORDER BY`.

    A calendar date taken from `response.received_at` rather than from a clock,
    like every other date this module writes -- `connections.last_success_at`
    is a different fact (a sync attempt succeeding) and is not touched here.
    """
    observed = _as_of(response.received_at)
    recorded = conn.execute(
        select(connections.c.roster_observed_date).where(
            connections.c.connection_id == response.connection_id
        )
    ).scalar_one()
    conn.execute(
        update(connections)
        .where(connections.c.connection_id == response.connection_id)
        .values(
            roster_observed_date=(
                observed if recorded is None else max(calendar_date(recorded), observed)
            )
        )
    )


def _derive_one_account(
    conn: SAConnection,
    *,
    response: RawResponse,
    context: DerivationContext,
    institution_id: int,
    entry: dict[str, Any],
    roster: bool,
) -> None:
    source_account_id = _required(entry.get("account_id"), "account_id", response)
    account_type = _required(entry.get("type"), "account type", response)
    balances = entry.get("balances")
    if not isinstance(balances, dict):
        raise DerivationError(
            f"raw response {response.raw_response_id} gives account {source_account_id} no "
            f"balances object"
        )
    # 🔴 One unusable account must not cost the connection its transactions.
    # `/accounts/get` is fetched first on every run, so a refusal here aborts the
    # connection before a single page is pulled -- and the same body is archived
    # again on the next run, and the one on the run after that. What the
    # aggregator did not report is ABSENT, which is the distinction this
    # product's whole warning vocabulary exists to preserve; refusing the roster
    # loses the transactions as well and calls it safety.
    stated_currency = _stated_currency(balances)
    # 🔴 **`None` is a value this column holds, not a reason to skip the
    # account.** It means *the aggregator has not told us what unit this account
    # is in* -- never `USD`, never the unit of the operator's other accounts.
    # Skipping was the old behaviour and it cascaded: the account was invisible,
    # which `first-production-connection.md` § 4.2 names as the undetectable
    # failure, and every transaction on it went on refusing to derive until some
    # later sync happened to state one. The transactions never needed it -- they
    # carry their own currency, stated per row.
    matched = _match_account(
        conn,
        response=response,
        institution_id=institution_id,
        source_account_id=source_account_id,
        persistent_account_id=_optional(entry.get("persistent_account_id")),
    )
    currency = stated_currency or (None if matched is None else matched.currency)
    account_id = _upsert_account(
        conn,
        response=response,
        institution_id=institution_id,
        source_account_id=source_account_id,
        entry=entry,
        account_type=account_type,
        currency=currency,
        roster=roster,
        matched=matched,
    )
    if stated_currency is None:
        # The account row keeps the unit the aggregator itself recorded for it
        # earlier, or none where there has never been one. This BALANCE has no
        # stated unit, and storing an amount under a unit inferred from another
        # response is how a total silently mixes two of them -- so the account is
        # kept and the balance is not. `balances_daily.currency` stays NOT NULL
        # for exactly that reason: one amount with no unit is unusable, while an
        # account with no unit yet is merely incompletely known.
        _log.warning(
            "raw response %s gives account %d a balance in no stated currency; the account "
            "is kept and no balance is recorded for it",
            response.raw_response_id,
            account_id,
        )
        return
    try:
        _write_balance(
            conn,
            response=response,
            context=context,
            account_id=account_id,
            source_account_id=source_account_id,
            balances=balances,
            currency=stated_currency,
            balance_class=balance_class_of(account_type, response),
        )
    except UndenominableAmountError as exc:
        # 🔴 Caught HERE so the refusal costs one balance rather than the
        # roster. `/accounts/get` is fetched first on every run, so letting this
        # escape would abort the connection before a page was pulled -- on this
        # run and on every run after it, since the same body is archived again
        # each time. The account is kept and says which unit it is in; the read
        # path excludes it from minor-units figures and names it there.
        _log.warning(
            "raw response %s: account %d holds a balance this build cannot express in minor "
            "units, so no balance is recorded for it and its rows are excluded from every "
            "minor-units total until the currency's exponent is known -- %s",
            response.raw_response_id,
            account_id,
            exc,
        )


@dataclass(frozen=True, slots=True)
class _MatchedAccount:
    """The row an incoming account entry is about, and what a merge of it needs.

    `currency` rides along because the unit this datastore already holds for the
    account is the aggregator's own earlier word about it, and reading it is what
    lets an account whose balance states no unit keep the one it had. Fetching it
    separately would ask the same question twice and could answer it about a
    different row than the one the entry is merged into.
    """

    account_id: int
    first_seen_date: date
    last_seen_date: date | None
    currency: str | None


def _account_row(conn: SAConnection, *criteria: Any) -> Sequence[Any]:
    """The columns a merge needs, for every account matching `criteria`."""
    return conn.execute(
        select(
            accounts.c.account_id,
            accounts.c.first_seen_date,
            accounts.c.last_seen_date,
            accounts.c.currency,
        )
        .where(*criteria)
        .order_by(accounts.c.account_id)
    ).all()


def _match_account(
    conn: SAConnection,
    *,
    response: RawResponse,
    institution_id: int,
    source_account_id: str,
    persistent_account_id: str | None,
) -> _MatchedAccount | None:
    """The account this entry is about: its persistent identity first, this connection's key second.

    🔴 **Why the persistent identity comes first.** Removing a connection and
    linking it again yields a new Item, and the new Item issues a NEW id for
    every account. Matched on `(connection_id, source_account_id)` alone, the
    same real account appears a second time under the new connection, the whole
    granted history is re-fetched against it, and annual spending doubles --
    signalled by nothing louder than a count of accounts that are no longer
    active. `persistent_account_id` is the aggregator's own statement that two
    differently-numbered accounts are the same underlying account, so where it
    is present it decides, and the local `account_id` -- which every row of
    history references -- survives the break.

    🔴 **Scoped to the institution, never globally.** The field is documented
    stable for the same underlying account, but nothing makes it unique across
    institutions, and a global match would turn a collision between two banks
    into a silent merge of two real accounts into one. That is the worst outcome
    available here: every total over both is then wrong, and the store holds no
    record that two things were joined.

    🔴 **Absence is ORDINARY, not an error.** The aggregator populates this field
    for select institutions only. Where it is absent the match falls back to
    today's key, and a re-link at such an institution still duplicates -- which
    this deriver cannot fix by guessing. Re-authorising in place (the same Item,
    so no second lineage at all) is the remedy there.

    🔴 **A persistent match that would collide is refused rather than forced.**
    Two rows already sharing a persistent identity under one institution is what
    a re-link BEFORE this match existed left behind. Re-pointing the older row
    onto the newer one's `(connection_id, source_account_id)` would violate the
    identity index and take the connection down on every run; merging their
    history is a repair with its own requirement, not something a deriver does
    on the way past. So today's row answers, and the pair is named in the log.
    """
    own = _account_row(
        conn,
        accounts.c.connection_id == response.connection_id,
        accounts.c.source_account_id == source_account_id,
    )
    if persistent_account_id is not None:
        persistent = _account_row(
            conn,
            accounts.c.institution_id == institution_id,
            accounts.c.source_persistent_account_id == persistent_account_id,
        )
        if len(persistent) > 1:
            # Ordered by `account_id`, so the earliest row answers and a replay
            # of the archive in any order lands on the same one.
            _log.warning(
                "raw response %s names a persistent account identity that %d rows at this "
                "institution already carry; the earliest answers for it and the rest are "
                "left where they are",
                response.raw_response_id,
                len(persistent),
            )
        if persistent and (not own or own[0].account_id == persistent[0].account_id):
            if not own:
                _log.info(
                    "raw response %s carries a persistent identity account %d already holds, "
                    "so its history stays where it is and the newly issued account id is "
                    "recorded against it",
                    response.raw_response_id,
                    persistent[0].account_id,
                )
            return _MatchedAccount(*persistent[0])
        if persistent:
            _log.warning(
                "raw response %s names a persistent identity held by account %d, while "
                "account %d already answers to the id this connection used; converging them "
                "would collide on the identity index, so both are kept and neither moves",
                response.raw_response_id,
                persistent[0].account_id,
                own[0].account_id,
            )
    return None if not own else _MatchedAccount(*own[0])


def _upsert_account(
    conn: SAConnection,
    *,
    response: RawResponse,
    institution_id: int,
    source_account_id: str,
    entry: dict[str, Any],
    account_type: str,
    currency: str | None,
    roster: bool,
    matched: _MatchedAccount | None,
) -> int:
    """One account row, converged on the identity `_match_account` resolved.

    Matched explicitly rather than through the unique index: that index spans
    `(connection_id, source_account_id)`, and SQLite treats NULLs as distinct, so
    relying on a conflict would let the manual-import path's null connections
    duplicate silently -- and since a re-link converges on a row held under a
    DIFFERENT connection and a different source id, there is no conflict for the
    index to raise in the case that matters most.

    🔴 **`last_seen_date` moves only on a roster read** (`roster=True`). It
    records *the roster was observed and this account was in it*, and AC-12.5
    measures absence by comparing it against `connections.roster_observed_date`
    -- so a sync body advancing it past an observation nobody made would leave no
    account matching that observation, and the connection would report a roster
    that listed nothing. An account first learned from a sync body therefore has
    a NULL `last_seen_date`, which already means exactly what is true of it: no
    roster observation is recorded for this account.
    """
    seen_date = _as_of(response.received_at)
    persistent_account_id = _optional(entry.get("persistent_account_id"))
    values: dict[str, Any] = {
        "institution_id": institution_id,
        # 🔴 Both move on a match, and that is what convergence IS. The account
        # a re-link converged on is now reached through the NEW Item under the
        # NEW id, and the transaction path finds an account by exactly that pair
        # -- so a row left pointing at the retired connection would take every
        # page of the re-fetched history down with it.
        "connection_id": response.connection_id,
        "source_account_id": source_account_id,
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
    if matched is None:
        result = conn.execute(
            insert(accounts).values(
                **values,
                source_persistent_account_id=persistent_account_id,
                first_seen_date=seen_date,
                # AC-12.4: the same date at both ends on the first observation.
                # The pair only diverges once a later roster names the account
                # again, or stops naming it.
                last_seen_date=seen_date if roster else None,
                created_at=response.received_at,
            )
        )
        primary_key = result.inserted_primary_key
        assert primary_key is not None  # an INTEGER PRIMARY KEY insert always yields one
        return int(primary_key[0])

    account_id, first_seen_date, last_seen_date = (
        matched.account_id,
        matched.first_seen_date,
        matched.last_seen_date,
    )
    # 🔴 **A persistent identity already recorded is KEPT when an entry states
    # none.** This field is the only key convergence has, and the aggregator
    # populates the accounts array of one endpoint and not always the other --
    # so writing an unconditional `None` over it would erase, on an ordinary
    # sync, the one thing that will make the next re-link converge. Keeping the
    # aggregator's own earlier word about the same account is the rule the
    # currency column already follows.
    identity: dict[str, Any] = (
        {}
        if persistent_account_id is None
        else {"source_persistent_account_id": persistent_account_id}
    )
    # 🔴 **An update owns fewer columns than an insert, and the difference is the
    # point.** Applying one `values` dict to both would make derivation the
    # permanent owner of every column it names -- so an operator's correction to
    # `balance_class`, which `data-model.md` declares operator-correctable, would
    # be silently reverted on the next sync, and `lifecycle_status` would be
    # pinned to "active" by the code that is supposed to be able to retire it.
    #
    # `first_seen_date` takes the earliest mention rather than the latest, so a
    # replay in any order converges on the same row.
    #
    # 🔴 `last_seen_date` is the same property from the other end (AC-12.4), and
    # the pair is what makes account retirement derivable at all: a maximum that
    # only ever moves forward means replaying the archive in any order lands on
    # the same date, so "this account was last listed on X" is a fact about the
    # responses rather than about the order they happened to be replayed in.
    # That order-independence is the specific care this deriver recorded as the
    # reason retirement was deferred rather than half-built.
    #
    # `None` is the pre-migration-003 row that no sync has touched since. Taking
    # `seen_date` for it is right and is not a special case wearing a coalesce:
    # this response IS the latest observation of the account, whatever was or
    # was not recorded before it.
    observed: dict[str, Any] = (
        {
            "last_seen_date": (
                seen_date if last_seen_date is None else max(last_seen_date, seen_date)
            )
        }
        if roster
        else {}
    )
    conn.execute(
        update(accounts)
        .where(accounts.c.account_id == account_id)
        .values(
            **{k: v for k, v in values.items() if k not in _OPERATOR_OWNED},
            **identity,
            first_seen_date=min(first_seen_date, seen_date),
            **observed,
        )
    )
    return int(account_id)


def _write_balance(
    conn: SAConnection,
    *,
    response: RawResponse,
    context: DerivationContext,
    account_id: int,
    source_account_id: str,
    balances: dict[str, Any],
    currency: str,
    balance_class: str,
) -> None:
    """One day's balance for one account, signed from the operator's point of view.

    🔴 **A liability's stored sign is the NEGATION of the aggregator's**, applied
    unconditionally rather than only to a positive balance. This aggregator
    documents a credit or loan `current` as positive when the money is owed and
    negative when the lender owes the account holder -- the ordinary state of a
    card after a refund on a paid-off balance. A rule that only flipped positives
    would map $250 owed and $250 in credit onto the same stored value, so two
    opposite states would be one row and net worth would be understated by twice
    the credit, silently and plausibly.

    **The sign convention belongs to the connector, not to the account type.**
    A second aggregator that signed liabilities the other way would get its own
    connector declaring its own normalization; sniffing the sign here would put a
    hypothetical feed's convention inside the one built for a documented feed.
    One stored convention is what lets net worth be a plain sum and AC-11.2's
    reconciliation be "change in balance equals sum of transactions" for every
    account.

    `available_minor` and `limit_minor` are the documented exceptions and keep
    the magnitudes the source reported: neither participates in net worth, and
    "available credit" is not a negative quantity from anyone's point of view.

    The row references the **local** `account_id` (AC-6.3), never the
    aggregator's, because the aggregator's changes when a connection is removed
    and re-linked, and history that pointed at it would detach.
    """
    reported = balances.get("current")
    if reported is None:
        # 🔴 `current` is documented nullable, and a null one is an ABSENT
        # balance rather than a zero or a refusal. `current_minor` is NOT NULL,
        # so absence is recorded by the day having no row -- and the account
        # keeps its row, which is what lets its transactions derive. Refusing
        # instead aborted the connection before any page was fetched, on every
        # run, forever.
        _log.warning(
            "raw response %s reports no current balance for account %d, so no balance is "
            "recorded for that day; the account is kept",
            response.raw_response_id,
            account_id,
        )
        return
    current = to_minor(reported, currency, "a current balance", response)
    if balance_class == "liability":
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

    if not _claim_capture_day(
        conn,
        keys=(
            (
                balances_daily,
                (
                    balances_daily.c.account_id == account_id,
                    balances_daily.c.as_of_date == as_of,
                ),
            ),
        ),
        response=response,
    ):
        return
    conn.execute(insert(balances_daily).values(account_id=account_id, as_of_date=as_of, **values))


def _position_keys(
    *tables: Table, account_id: int, security_id: int, as_of: CalendarDate
) -> tuple[tuple[Table, tuple[Any, ...]], ...]:
    """One position's day, as a key in each table that can record it.

    🔴 A position's day is ONE key across `holdings` and `refused_holdings`: a
    capture either records the position or refuses it, and the day's first
    capture decides which. Claimed per table, each would keep its own first
    capture, and two captures disagreeing about a unit would leave a row in
    both -- a position served beside a disclosure calling it absent, and a
    total over positions left to guess which one counts. The caller names its
    own table first and the rival after it.
    """
    return tuple(
        (
            table,
            (
                table.c.account_id == account_id,
                table.c.security_id == security_id,
                table.c.as_of_date == as_of,
            ),
        )
        for table in tables
    )


def _claim_capture_day(
    conn: SAConnection,
    *,
    keys: Sequence[tuple[Table, Sequence[Any]]],
    response: RawResponse,
) -> bool:
    """Whether this response is the capture an append-only day-keyed row keeps.

    True once the day is this response's to write -- any row belonging to a
    later capture having been removed first. False when what is already there
    stands. `keys` is every (table, key) the one day can be recorded under: one
    for a balance, and both holdings tables for a position.

    🔴 **One capture per key per day, and the first one wins.** `data-model.md`
    says it of the daily balance series and of the holdings series in one breath,
    and the DDL comments above both tables repeat it: a second capture on a day
    already recorded is *rejected* rather than allowed to overwrite, so a series
    does not depend on what time of day anyone happened to look, and a re-run
    changes nothing (AC-2.4). No aggregator backfills either series, so a day not
    captured is a day gone for good -- which is why the rule leans toward keeping
    what is already there.

    🔴 **Shared rather than written once per table.** Two derivers implementing
    this from the same paragraph is how the balance series and the holdings
    series come to disagree about what "first" means, and the disagreement would
    show up as a rebuild that cannot reproduce its own content rather than as
    anything a reader could trace back to here.

    Skipped rather than raised, because rejecting loudly would make a rebuild
    fail on an archive that is perfectly legitimate: two responses carrying the
    same day is an ordinary thing for a sync to have recorded.

    **"First" is decided by comparing captures, not by arriving first.** Replay
    order would give the same answer today and would stop doing so the moment two
    responses shared a `received_at`, which the archive explicitly allows.

    This also protects a row the manual-import path wrote: overwriting one would
    turn it into an aggregator row that the next rebuild deletes, since a rebuild
    empties exactly the rows carrying a `raw_response_id`.
    """
    held = [
        (table, where)
        for table, where in keys
        if conn.execute(select(table.c.captured_at).where(*where)).one_or_none() is not None
    ]
    for table, where in held:
        captured_at, raw_response_id = conn.execute(
            select(table.c.captured_at, table.c.raw_response_id).where(*where)
        ).one()
        if (captured_at, raw_response_id or 0) <= (response.received_at, response.raw_response_id):
            return False
        if raw_response_id is None:
            # 🔴 A row this deriver did not write, and must not remove.
            # `manual_import_id` rows come from FR-7's import path -- the operator's
            # own statement -- and a rebuild deletes exactly the rows carrying a
            # `raw_response_id`. Replacing one with an aggregator row would make it
            # disappear a rebuild later, with nothing connecting the loss to the sync
            # that caused it. The comparison above already covers ties and later
            # captures; this covers the *earlier* archived response, which is the
            # case the comparison would otherwise let through.
            return False
    # The archive holds an earlier capture for this day than every row that is
    # here. Replaying it must still land on the earliest, or a rebuild would
    # depend on the order rows happened to be written in the first place.
    for table, where in held:
        conn.execute(delete(table).where(*where))
    return True


def derive_investments_holdings(
    conn: SAConnection, response: RawResponse, context: DerivationContext
) -> None:
    """`/investments/holdings/get` -> `securities`, `holdings`, and the investments domain.

    The first deriver that records what is *inside* an account rather than what
    the account is worth. AC-3.2: it runs for a connection whose recorded
    capabilities include the investments product, and the gate is the caller's
    because a capability is a fact in the datastore rather than one a response
    carries.

    🔴 **A position has no date of its own, so `as_of_date` is this system's
    capture date** -- the same `received_at` the balance series is keyed on
    *(measured: the only date on a holding is `institution_price_as_of`, the
    price's, and in the sandbox it is four years old;
    `api-notes-plaid.md` §22)*. Deriving the day from the price's date instead
    would file today's observation under 2021 and leave the series with a hole
    on every day the product actually ran.

    🔴 **No balance is ever computed from positions, and the arithmetic says
    why.** Summing this account's positions does not reproduce its balance --
    the aggregator's own sandbox is off by 6% on one of its two investment
    accounts *(§23)*. The two series are separate observations of the same
    account and neither is derivable from the other. The body's own `accounts`
    array is derived, because every endpoint that names an account derives it
    (see `_derive_carried_accounts`) -- but what lands from it is the balance
    the AGGREGATOR stated, through the one account deriver, and on any day
    `/accounts/get` also ran its earlier capture is the one the day keeps.

    **Securities before holdings**, because a holding references one and the
    reference is by local id. A holding naming a security the body did not list
    refuses rather than inventing a placeholder row for it.

    🔴 **This deriver does not stamp the investments domain as current, and the
    transactions deriver beside it does.** The asymmetry is the pull's, not a
    lapse: two feeds share one `sync_state` domain key -- positions and a
    transaction window -- so one body arriving is half the answer. A connection
    whose holdings landed while its window came back short would otherwise read
    fresh on every freshness surface while most of its investment history was
    missing. The sync command stamps it once both feeds are in
    (`store/sync_domains.py`).
    """
    if response.connection_id is None:
        raise DerivationError(
            f"raw response {response.raw_response_id} ({INVESTMENTS_HOLDINGS_GET}) was "
            f"archived without a connection, so there are no accounts for its positions to "
            f"hang from. A holdings reply belongs to exactly one connection and nothing "
            f"else can say which"
        )
    payload = _payload(response)
    _derive_carried_accounts(conn, response=response, context=context, payload=payload)
    local_security: dict[str, int] = {}
    for entry in _entries(payload, "securities", response):
        source_security_id, security_id = _upsert_security(
            conn, entry=entry, response=response, context=context
        )
        local_security[source_security_id] = security_id

    known_accounts = _account_ids(conn, response.connection_id)
    for entry in _entries(payload, "holdings", response):
        _write_holding(
            conn,
            response=response,
            context=context,
            entry=entry,
            known_accounts=known_accounts,
            local_security=local_security,
        )


def _upsert_security(
    conn: SAConnection, *, entry: dict[str, Any], response: RawResponse, context: DerivationContext
) -> tuple[str, int]:
    """One security row, converged on the aggregator's id for it.

    Upserted rather than inserted for the same reason institutions and accounts
    are: `holdings.security_id` references this row, so a rebuild that reassigned
    the local id would detach every position from the instrument it is in.

    🔴 **The columns take the LATEST observation, decided by comparing captures
    rather than by replay order.** A security's name, ticker and close price
    change under it, so an older archived response replaying last would otherwise
    reinstate a stale name -- and the rebuild that did it would report content it
    could not reproduce. `created_at` is a minimum and the values ride
    `updated_at`'s maximum, which is what makes this a property of the arithmetic.

    A close price this build cannot denominate costs the PRICE, not the security:
    the row is what every holding of it references, and refusing it would take
    the positions down with it.
    """
    source_security_id = _required(entry.get("security_id"), "a security_id", response)
    currency = _stated_currency(entry)
    close_price = entry.get("close_price")
    close_price_minor: MinorUnits | None = None
    if close_price is not None and currency is not None:
        try:
            close_price_minor = to_minor(close_price, currency, "a security close price", response)
        except UndenominableAmountError as exc:
            _log.warning(
                "raw response %s: security %s has a close price this build cannot express in "
                "minor units, so the security is recorded without one -- %s",
                response.raw_response_id,
                source_security_id,
                exc,
            )
    close_price_as_of = entry.get("close_price_as_of")
    values: dict[str, Any] = {
        "name": _optional(entry.get("name")),
        "ticker": _optional(entry.get("ticker_symbol")),
        "cusip": _optional(entry.get("cusip")),
        "isin": _optional(entry.get("isin")),
        "security_type": _optional(entry.get("type")),
        "currency": currency,
        "close_price_minor": close_price_minor,
        "close_price_as_of": (
            None
            if close_price_as_of is None
            else _parse_calendar(close_price_as_of, "a close price date", response)
        ),
        "derivation_version_id": context.derivation_version_id,
    }
    existing = conn.execute(
        select(securities.c.security_id, securities.c.created_at, securities.c.updated_at).where(
            securities.c.source_security_id == source_security_id
        )
    ).one_or_none()
    if existing is None:
        result = conn.execute(
            insert(securities).values(
                source_security_id=source_security_id,
                created_at=response.received_at,
                updated_at=response.received_at,
                **values,
            )
        )
        primary_key = result.inserted_primary_key
        assert primary_key is not None  # an INTEGER PRIMARY KEY insert always yields one
        return source_security_id, int(primary_key[0])

    security_id, created_at, updated_at = existing
    if response.received_at < updated_at:
        # An older capture of a security already described by a newer one. The
        # only thing it can still say is that this security was known earlier.
        conn.execute(
            update(securities)
            .where(securities.c.security_id == security_id)
            .values(created_at=min(created_at, response.received_at))
        )
        return source_security_id, int(security_id)
    conn.execute(
        update(securities)
        .where(securities.c.security_id == security_id)
        .values(
            created_at=min(created_at, response.received_at),
            updated_at=response.received_at,
            **values,
        )
    )
    return source_security_id, int(security_id)


def _exact_quantity(value: object, response: RawResponse) -> str:
    """A position size, kept as the exact text the aggregator sent.

    🔴 **A quantity is not money, and that is why it is neither minor units nor a
    float.** Fractional shares are routine -- a sandbox Bitcoin position is
    `0.00293644` *(§22)* -- so a scaled integer would need a scale this product
    would have to guess, and a float loses digits before anyone can look. The
    column is TEXT and holds what arrived.

    Validated by parsing it as a decimal and then storing the ORIGINAL text: a
    `Decimal` round trip would rewrite `1E+2` and `100` into one spelling, and
    the archive's own rendering is the one with a provenance.
    """
    if isinstance(value, bool) or value is None:
        raise DerivationError(
            f"raw response {response.raw_response_id} gives a position no quantity; "
            f"`holdings.quantity` is NOT NULL and a placeholder would be this system "
            f"inventing a position size and recording it as the aggregator's"
        )
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, str):
        raise DerivationError(
            f"raw response {response.raw_response_id} gives a position quantity as a "
            f"{type(value).__name__}; quantities must reach this point as the text the "
            f"aggregator sent, because a float has already dropped digits by the time it "
            f"is seen"
        )
    try:
        Decimal(value)
    except InvalidOperation as exc:
        raise DerivationError(
            f"raw response {response.raw_response_id} gives a position quantity of "
            f"{value!r}, which is not a number"
        ) from exc
    return value


def _write_holding(
    conn: SAConnection,
    *,
    response: RawResponse,
    context: DerivationContext,
    entry: dict[str, Any],
    known_accounts: dict[str, int],
    local_security: dict[str, int],
) -> None:
    """One position, for one account, on the day it was captured.

    🔴 **No sign is flipped here, and the omission is deliberate.** A liability's
    balance is negated on the way in because the aggregator reports what is owed
    as a positive number; a position has no such convention to undo -- a long
    holding is value held and a short one arrives negative in both quantity and
    value. Negating anything here would report a portfolio as a debt.

    The market value is a VALUATION -- price times quantity, arriving with
    whatever precision that arithmetic produced -- so it is rounded half-even to
    the currency's minor unit and logged, never converted exactly or refused the
    way a ledger amount is. Four of thirteen positions in the aggregator's own
    sandbox carry sub-cent values *(§22)*, so this is the ordinary path rather
    than an edge.
    """
    source_account_id = entry.get("account_id")
    if not isinstance(source_account_id, str) or source_account_id not in known_accounts:
        # Reachable only when the body names a position for an account its OWN
        # `accounts` array left out -- everything that array does name has been
        # derived by this point. A row pointing at nothing is worse than a
        # refusal that says which account went missing.
        raise DerivationError(
            f"raw response {response.raw_response_id} ({response.endpoint}) has a position "
            f"for account {source_account_id!r}, which this connection has no row for and "
            f"which the body's own accounts list does not carry either"
        )
    source_security_id = _required(entry.get("security_id"), "a security_id", response)
    if source_security_id not in local_security:
        raise DerivationError(
            f"raw response {response.raw_response_id} ({response.endpoint}) has a position "
            f"in security {source_security_id!r}, which its own securities list does not "
            f"carry. The instrument a position is in is not something this system can "
            f"supply for it"
        )
    account_id = known_accounts[source_account_id]
    security_id = local_security[source_security_id]
    as_of = _as_of(response.received_at)
    currency = _stated_currency(entry)
    if currency is None:
        # 🔴 `holdings.currency` is NOT NULL, and for the same reason
        # `balances_daily.currency` is: one amount with no unit is unusable, and
        # a unit borrowed from another response is how a total silently mixes
        # two. The position is not recorded -- its refusal is, so a read can say
        # which position is missing -- and the archive keeps the body, so a later
        # capture that states a currency derives it.
        _log.warning(
            "raw response %s: a position in security %s states no currency, so it is not recorded",
            response.raw_response_id,
            source_security_id,
        )
        _record_refused_holding(
            conn,
            response=response,
            context=context,
            account_id=account_id,
            security_id=security_id,
            as_of=as_of,
            currency=None,
        )
        return
    market_value = entry.get("institution_value")
    if market_value is None:
        raise DerivationError(
            f"raw response {response.raw_response_id} gives the position in security "
            f"{source_security_id!r} no institution_value; `holdings.market_value_minor` "
            f"is NOT NULL and a position of unstated value is not a zero"
        )
    cost_basis = entry.get("cost_basis")
    quantity = _exact_quantity(entry.get("quantity"), response)
    try:
        market_value_minor = to_minor(market_value, currency, "a position value", response)
        cost_basis_minor = (
            None
            if cost_basis is None
            else to_minor(cost_basis, currency, "a position cost basis", response)
        )
    except UndenominableAmountError as exc:
        # 🔴 One position's currency costs that position, never the account's
        # other holdings. A portfolio holding one instrument in a unit this
        # build has no exponent for would otherwise report nothing at all,
        # and the rest of it is perfectly denominable.
        _log.warning(
            "raw response %s: a position is held in a unit this build cannot express in "
            "minor units, so it is not recorded and the rest of the account's positions "
            "are kept -- %s",
            response.raw_response_id,
            exc,
        )
        _record_refused_holding(
            conn,
            response=response,
            context=context,
            account_id=account_id,
            security_id=security_id,
            as_of=as_of,
            currency=currency,
        )
        return
    # 🔴 The price's date, not the position's: a capture today can value a
    # position at a price years old *(§22)*. Absent stays absent -- never the
    # capture day, which is the one reading this column exists to refuse.
    price_as_of = _optional(entry.get("institution_price_as_of"))
    values: dict[str, Any] = {
        "quantity": quantity,
        "market_value_minor": market_value_minor,
        "cost_basis_minor": cost_basis_minor,
        "currency": currency,
        "captured_at": response.received_at,
        "source": "aggregator",
        "raw_response_id": response.raw_response_id,
        "manual_import_id": None,
        "derivation_version_id": context.derivation_version_id,
        "price_as_of": (
            None
            if price_as_of is None
            else _parse_calendar(price_as_of, "a position price date", response)
        ),
    }
    if not _claim_capture_day(
        conn,
        keys=_position_keys(
            holdings,
            refused_holdings,
            account_id=account_id,
            security_id=security_id,
            as_of=as_of,
        ),
        response=response,
    ):
        return
    conn.execute(
        insert(holdings).values(
            account_id=account_id, security_id=security_id, as_of_date=as_of, **values
        )
    )


def _record_refused_holding(
    conn: SAConnection,
    *,
    response: RawResponse,
    context: DerivationContext,
    account_id: int,
    security_id: int,
    as_of: CalendarDate,
    currency: str | None,
) -> None:
    """A position this build refused, kept where a read can name it.

    🔴 **The refusal, not the position.** Skipping the row keeps an unexpressible
    value out of `holdings`, and until this record existed the only trace was a
    log line no caller of any surface ever sees -- so `list_holdings` served the
    account's other positions as if they were all of them. `data-model.md`'s
    valuation norm requires the refusal NAMED under `rule-applied`, and a read
    can only name what the store records.

    The same first-capture-of-the-day rule the position would have obeyed, and
    over the SAME key: the day is claimed across both tables, so a capture that
    refuses a position the day already recorded changes nothing, and an earlier
    one replaces the row it disagrees with. A rebuild lands on the same record
    whatever order it replays the day's captures in. `currency` is None where
    the aggregator stated none.
    """
    if not _claim_capture_day(
        conn,
        keys=_position_keys(
            refused_holdings,
            holdings,
            account_id=account_id,
            security_id=security_id,
            as_of=as_of,
        ),
        response=response,
    ):
        return
    conn.execute(
        insert(refused_holdings).values(
            account_id=account_id,
            security_id=security_id,
            as_of_date=as_of,
            currency=currency,
            captured_at=response.received_at,
            raw_response_id=response.raw_response_id,
            derivation_version_id=context.derivation_version_id,
        )
    )


def derive_investment_transactions(
    conn: SAConnection, response: RawResponse, context: DerivationContext
) -> None:
    """`/investments/transactions/get` -> `securities`, `investment_transactions`.

    One page of one window. 🔴 **Everything that spans the window lives outside
    this function**, and that boundary is forced rather than chosen: a deriver
    sees one archived body, and the removal signal here is a row's ABSENCE from
    the whole window *(`api-notes-plaid.md` §26 -- the feed sends no `removed`
    list, no tombstone, nothing)*. Absence is not observable from one page of
    twelve, so `store.investments.record_investment_transaction_window` owns it
    and is handed the window only once the pages have been exhausted. The domain's
    freshness stamp is outside for the same reason and one scope wider still:
    positions and this window share one domain key, so neither body alone says
    the domain is current (`store/sync_domains.py`).

    **Securities before transactions**, as in the holdings deriver and for the
    same reason: a transaction references one by local id. Unlike a holding, a
    transaction may legitimately reference none -- a contribution of cash is not
    a trade in an instrument -- so a null `security_id` is stored rather than
    refused, while a stated one the body did not list still refuses the row.
    """
    if response.connection_id is None:
        raise DerivationError(
            f"raw response {response.raw_response_id} ({INVESTMENTS_TRANSACTIONS_GET}) was "
            f"archived without a connection, so there are no accounts for its transactions "
            f"to hang from. An investments reply belongs to exactly one connection and "
            f"nothing else can say which"
        )
    payload = _payload(response)
    _derive_carried_accounts(conn, response=response, context=context, payload=payload)
    local_security: dict[str, int] = {}
    for entry in _entries(payload, "securities", response):
        source_security_id, security_id = _upsert_security(
            conn, entry=entry, response=response, context=context
        )
        local_security[source_security_id] = security_id

    known_accounts = _account_ids(conn, response.connection_id)
    for entry in _entries(payload, "investment_transactions", response):
        try:
            _write_investment_transaction(
                conn,
                response=response,
                context=context,
                entry=entry,
                known_accounts=known_accounts,
                local_security=local_security,
            )
        except UndenominableAmountError as exc:
            # One row's currency costs that row, never the rest of the page --
            # the same scope `_write_one_change` refuses a transaction at, for
            # the same reason. Here there is no cursor to strand, but there is a
            # window: letting this escape would abandon the remaining pages and
            # leave the reconciliation with a window it never saw whole, which
            # by design then soft-deletes nothing at all.
            _log.warning(
                "raw response %s: one investment transaction is in a unit this build cannot "
                "express in minor units, so it is not derived and the rest of the page is -- %s",
                response.raw_response_id,
                exc,
            )


def _write_investment_transaction(
    conn: SAConnection,
    *,
    response: RawResponse,
    context: DerivationContext,
    entry: dict[str, Any],
    known_accounts: dict[str, int],
    local_security: dict[str, int],
) -> None:
    """One investment transaction, converged on the aggregator's id for it.

    🔴 **`settlement_date` is not written, and its absence here is the record of
    a measurement.** The feed has no such field *(§26)*; the frozen column keeps
    its meaning for the manual importer, which is now its only possible writer.
    Writing `trade_date` into it "for completeness" would manufacture a
    settlement the institution never stated, and every later reader would take
    it for one.

    Simpler than `_write_transaction` by the whole pending->posted machinery:
    an investment transaction has no pending state and no id that supersedes
    another, so identity is the aggregator's id alone.
    """
    account_id = _local_account(entry, known_accounts, response)
    source_id = _required(
        entry.get("investment_transaction_id"), "an investment transaction id", response
    )
    currency = _stated_currency(entry)
    if currency is None:
        raise DerivationError(
            f"raw response {response.raw_response_id} ({response.endpoint}) has an investment "
            f"transaction in no stated currency; storing an amount whose unit is unknown is "
            f"how a total silently mixes two of them"
        )

    source_security_id = _optional(entry.get("security_id"))
    if source_security_id is None:
        # A cash movement rather than a trade in an instrument -- a contribution
        # or an interest credit. The column is nullable for exactly this.
        security_id: int | None = None
    elif source_security_id in local_security:
        security_id = local_security[source_security_id]
    else:
        raise DerivationError(
            f"raw response {response.raw_response_id} ({response.endpoint}) has an investment "
            f"transaction in security {source_security_id!r}, which its own securities list "
            f"does not carry. The instrument a trade is in is not something this system can "
            f"supply for it"
        )

    quantity = entry.get("quantity")
    price = entry.get("price")
    fees = entry.get("fees")
    values: dict[str, Any] = {
        "account_id": account_id,
        "security_id": security_id,
        "source_investment_transaction_id": source_id,
        "trade_date": _parse_calendar(
            entry.get("date"), "an investment transaction date", response
        ),
        "investment_type": _required(entry.get("type"), "an investment transaction type", response),
        "investment_subtype": _optional(entry.get("subtype")),
        # 🔴 No sign is flipped, for the reason `_write_holding` states: a
        # quantity is not an amount. A sale arrives with a negative quantity
        # because shares left the account, which is already the operator's point
        # of view *(measured §26: -0.008902867462305952 on a sell)*.
        "quantity": None if quantity is None else _exact_quantity(quantity, response),
        # A unit price is a RATE, not a sum of money that moved, so it rounds
        # like a valuation rather than refusing like a ledger amount -- and it
        # does round: 12 of 100 live prices carry more precision than the cent
        # *(§26, e.g. 40876.02675)*.
        "price_minor": (
            None
            if price is None
            else to_minor(price, currency, "an investment transaction price", response)
        ),
        # 🔴 A fee IS a sum of money that moved, so it converts exactly or the
        # row is refused -- and it is operator-signed like every other stored
        # amount, because the aggregator sends a fee as a positive magnitude and
        # a fee is money leaving.
        #
        # 🔴 **How `fees` relates to `amount` is NOT measured, and nothing may
        # assume it.** The captured page rules out the obvious reading: a buy
        # arrives with `amount` 1.10 beside `fees` 7.99, so the fee is not a
        # component of that amount. Whether it is additive, settled separately,
        # or reported per-lot is unknown. Both columns are stored as sent and
        # NEITHER summed nor netted here; a total that wants to combine them
        # owes a measurement first.
        "fees_minor": (None if fees is None else _operator_signed_amount(fees, currency, response)),
        "amount_minor": _operator_signed_amount(entry.get("amount"), currency, response),
        "currency": currency,
        "description": _optional(entry.get("name")),
        "source": "aggregator",
        "raw_response_id": response.raw_response_id,
        "manual_import_id": None,
        "derivation_version_id": context.derivation_version_id,
        "updated_at": response.received_at,
    }

    existing = conn.execute(
        select(investment_transactions.c.investment_transaction_id).where(
            investment_transactions.c.account_id == account_id,
            investment_transactions.c.source_investment_transaction_id == source_id,
        )
    ).one_or_none()
    if existing is None:
        conn.execute(
            insert(investment_transactions).values(first_seen_at=response.received_at, **values)
        )
        return
    conn.execute(
        update(investment_transactions)
        .where(investment_transactions.c.investment_transaction_id == existing[0])
        .values(
            # 🔴 Cleared, as the transaction path clears it: a row the window
            # returned is a row that is present at the source. A stale removal
            # stamp would keep it out of every sum while its row said otherwise
            # -- and here the stamp's only author is a reconciliation that
            # concluded this row was gone, so the window disagreeing with that
            # conclusion is exactly the evidence that retires it.
            removed_at=None,
            **values,
        )
    )


#: Every endpoint this build derives, before the rounding disclosure is applied.
#:
#: Keyed by the endpoint's own path, which is also what `store.raw` records, so a
#: rebuild years from now can still tell what a stored response was.
_DERIVERS: Final[Mapping[str, Deriver]] = {
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
    str(INVESTMENTS_HOLDINGS_GET): derive_investments_holdings,
    str(INVESTMENTS_TRANSACTIONS_GET): derive_investment_transactions,
    str(TRANSACTIONS_SYNC): derive_transactions_sync,
}

#: What `bankmachine.derivers` composes into this build's registry. Every entry
#: discloses its roundings once per distinct value, whichever path derives.
PLAID_DERIVERS: Final[Mapping[str, Deriver]] = {
    endpoint: _discloses_rounding(deriver) for endpoint, deriver in _DERIVERS.items()
}
