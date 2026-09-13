"""The `list_holdings` tool: what each investment account holds, and what its answer owes.

Its own module because it reaches the rest of the read surface only through the shared
assembly core in `query.py` -- `_readable`, `_unusable`, `_answer` and the account-lifecycle
helpers -- and reviewing it inside every other tool's SQL made each change to either more
expensive. It imports that core and adds nothing to it; `query.py` never imports this module
back, so the dependency runs one way.

Read-role only, like every tool: its handles come from `reader_connection`, opened `mode=ro`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, union_all
from sqlalchemy.engine import Connection as SAConnection

from bankmachine.config import Config
from bankmachine.connector import INVESTMENTS_HOLDINGS_GET
from bankmachine.envelope import Answer, Caveat, iso_or_none
from bankmachine.query import (
    _POSITIONS_FROZE,
    _account_lifecycle,
    _answer,
    _not_active_caveat,
    _readable,
    _roster_observed_empty_caveat,
    _unusable,
)
from bankmachine.store.engine import reader_connection
from bankmachine.store.schema import (
    INVESTMENTS_DOMAIN,
    accounts,
    holdings,
    institutions,
    raw_responses,
    refused_holdings,
    securities,
    sync_state,
)
from bankmachine.store.types import CalendarDate, UtcInstant, calendar_date, utc_instant

#: How far a position's price may trail the day it was captured before the answer
#: says so. Four calendar days clears a long weekend of closed markets -- a Friday
#: close read on a Tuesday after a holiday Monday is ordinary -- and anything older
#: is a price nobody refreshed. 🔴 An assumption rather than a measurement, and the
#: owner's to override; the investment-sync build plan records it as one.
POSITION_PRICE_STALE_AFTER = timedelta(days=4)


@dataclass(frozen=True, slots=True)
class InvestmentFeed:
    """One connection's investments feed: its domain's own record and its newest reply.

    🔴 **Every comparison is between facts one pull wrote about itself, never
    between the calendar days of separate stamps.** One sync attempts investments,
    archives the holdings reply, stamps the domain, and only then attempts and
    stamps transactions (the order `sync run` takes, measured on the sandbox
    store). Any two of those instants can straddle midnight UTC, and days taken
    from two of them named a working feed stopped, or every account left out of a
    newer pull, until the next sync.
    """

    attempted: UtcInstant | None
    succeeded: UtcInstant | None
    replied: UtcInstant | None
    error: str | None

    @property
    def stopped(self) -> bool:
        """Its last attempt brought no holdings reply back.

        A reply archived after the attempt began IS that attempt's positions, so
        a failure after it -- the trades' window, not the positions -- and a
        window that merely came back short are not a stopped feed.
        """
        return self.attempted is not None and (
            self.replied is None or self.replied < self.attempted
        )

    @property
    def replied_day(self) -> CalendarDate | None:
        """The newest reply's day, on the clock `holdings.as_of_date` keys a capture by."""
        return None if self.replied is None else calendar_date(self.replied.date())


def _investment_feeds(conn: SAConnection) -> dict[int, InvestmentFeed]:
    """Every connection with an investments domain row or an archived holdings reply.

    🔴 **A reply is read from the archive, not inferred from positions.** A pull
    that lists no position writes no holdings row, so the newest capture can sit
    days behind a feed that works; the archived reply is there either way, and
    its `received_at` is the instant the capture day is taken from.
    """
    domains: dict[int, tuple[Any, Any, str | None]] = {
        int(connection_id): (attempted, succeeded, error)
        for connection_id, attempted, succeeded, error in conn.execute(
            select(
                sync_state.c.connection_id,
                sync_state.c.last_attempt_at,
                sync_state.c.last_success_at,
                sync_state.c.last_error_code,
            ).where(sync_state.c.domain == INVESTMENTS_DOMAIN)
        ).all()
    }
    replies: dict[int, Any] = {
        int(connection_id): received
        for connection_id, received in conn.execute(
            select(raw_responses.c.connection_id, func.max(raw_responses.c.received_at))
            .where(
                raw_responses.c.endpoint == INVESTMENTS_HOLDINGS_GET.path,
                raw_responses.c.connection_id.is_not(None),
            )
            .group_by(raw_responses.c.connection_id)
        ).all()
    }

    def instant(value: Any) -> UtcInstant | None:
        return None if value is None else utc_instant(value)

    feeds: dict[int, InvestmentFeed] = {}
    for connection_id in domains.keys() | replies.keys():
        attempted, succeeded, error = domains.get(connection_id, (None, None, None))
        feeds[connection_id] = InvestmentFeed(
            attempted=instant(attempted),
            succeeded=instant(succeeded),
            replied=instant(replies.get(connection_id)),
            error=error,
        )
    return feeds


def _holdings_totals(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What `list_holdings`' rows add up to, per currency, with the non-active part stated.

    🔴 **Summed from the rows this answer returns, never from a second read**, on
    `_flow_class_totals`' reason: a total taken at another instant than the rows
    beside it can contradict them, and a reader cannot tell which is wrong.
    `list_holdings` is uncapped, so the rows are every position.

    🔴 **Include and flag, with the magnitude** (`api-contract.md` § Direction's
    lifecycle norm). A position on an account no longer active stays in
    `market_value_minor_units`, and the count and signed value of those positions
    ride beside it, zero rather than absent, so a reader can subtract what froze.

    Not in it: cost basis, which is nullable per position, so a sum over the ones
    that have it is a wrong figure with no signal; and a refused position, which
    has no minor units to add and is named under `rule-applied`. Per currency,
    never one integer across two.
    """
    totals: dict[str, dict[str, int]] = {}
    for row in rows:
        entry = totals.setdefault(
            str(row["currency"]),
            {
                "positions": 0,
                "market_value_minor_units": 0,
                "not_active_positions": 0,
                "not_active_market_value_minor_units": 0,
            },
        )
        value = int(row["market_value_minor_units"])
        entry["positions"] += 1
        entry["market_value_minor_units"] += value
        if row["lifecycle"] != "active":
            entry["not_active_positions"] += 1
            entry["not_active_market_value_minor_units"] += value
    # Sorted so two identical stores answer identically.
    return [{"currency": currency, **entry} for currency, entry in sorted(totals.items())]


def _positions_not_current_caveat(
    *,
    old_prices: dict[int, tuple[int, CalendarDate]],
    unknown_prices: dict[int, int],
    left_behind: dict[int, tuple[CalendarDate, CalendarDate]],
    stopped_feed: dict[int, tuple[CalendarDate, InvestmentFeed]],
) -> list[Caveat]:
    """The warning that a well-formed position is not a current value.

    🔴 **Four conditions, kept apart in `detail`, because each asks something
    different of the reader.** A price older than its capture day is the
    commonest: the aggregator's sandbox values every position at a 2021 price on
    a capture taken today, and nothing about the row looks wrong. A price date the
    store does not have is named UNKNOWN rather than passed over as fresh -- the
    row predates the column, or the institution sent none, and either way the
    value cannot be vouched for.

    🔴 **The two capture conditions blame different things, and telling them
    apart is the point.** The aggregator's holdings reply covers the whole
    connection, so an account behind its OWN connection's newest pull was listed
    with no position in it: it may hold nothing now, and the feed is working.
    Only when the connection's last investments attempt brought no holdings reply
    back has the investments feed stopped. Naming
    the first as the second sends a reader to repair a sync that works. A
    non-active account is in neither -- the caller leaves it to
    `account_no_longer_active`, which already says why its captures stopped.

    Request-scoped: it names only accounts this answer's rows or refusals are
    about, so its absence says every position here was priced within the
    threshold of a capture no older than its connection's other data.

    🔴 A capture day is compared only with the day of an archived holdings reply,
    the clock `as_of_date` is keyed on, and a stopped feed only with its own
    domain's last attempt, so a sync straddling midnight UTC names nothing.
    """
    parts: list[str] = []
    if old_prices:
        named = ", ".join(
            f"{account_id} ({count} position(s), priced as early as {oldest.isoformat()})"
            for account_id, (count, oldest) in sorted(old_prices.items())
        )
        parts.append(
            f"account(s) {named} hold positions valued at a price more than "
            f"{POSITION_PRICE_STALE_AFTER.days} days older than the day they were captured, so "
            f"`market_value_minor_units` is that old price times the quantity held, not today's "
            f"value"
        )
    if unknown_prices:
        named = ", ".join(
            f"{account_id} ({count} position(s))"
            for account_id, count in sorted(unknown_prices.items())
        )
        parts.append(
            f"account(s) {named} hold positions whose price date is UNKNOWN -- the rows predate "
            f"the column and the store has not been rebuilt, or the institution sent none -- so "
            f"their values cannot be called current either"
        )
    if left_behind:
        named = ", ".join(
            f"{account_id} (positions from {captured.isoformat()}; its connection's newest "
            f"investments pull, {newest.isoformat()}, listed none for it)"
            for account_id, (captured, newest) in sorted(left_behind.items())
        )
        # 🔴 "The feed is working" only for the accounts it is true of. One whose
        # connection's investments feed has stopped is named again below, and for
        # it the positions may be gone OR merely unseen.
        stalled = sorted(left_behind.keys() & stopped_feed.keys())
        working = sorted(left_behind.keys() - stopped_feed.keys())
        if not stalled:
            feed = "The feed is working; the positions may be gone"
        elif not working:
            feed = "The connection's investments feed has since stopped as well, as named below"
        else:
            feed = (
                f"For account(s) {', '.join(map(str, working))} the feed is working and the "
                f"positions may be gone; for account(s) {', '.join(map(str, stalled))} the "
                f"connection's investments feed has since stopped as well, as named below"
            )
        parts.append(
            f"account(s) {named} were left out of a newer capture of their own connection, so "
            f"these rows are what each held on that earlier day and it may hold none of them "
            f"now. {feed}"
        )
    if stopped_feed:

        def why(feed: InvestmentFeed) -> str:
            attempted = "never" if feed.attempted is None else feed.attempted.date().isoformat()
            replied = "never" if feed.replied_day is None else feed.replied_day.isoformat()
            failed = "" if feed.error is None else f" (it failed with {feed.error})"
            return (
                f"its connection's last investments attempt, {attempted}, brought no positions "
                f"back{failed}; the newest came {replied}"
            )

        named = ", ".join(
            f"{account_id} (captured {captured.isoformat()}; {why(feed)})"
            for account_id, (captured, feed) in sorted(stopped_feed.items())
        )
        parts.append(
            f"account(s) {named} are on a connection whose investments feed has stopped "
            f"arriving: these are the last positions seen, and trades since may be missing "
            f"from them"
        )
    if not parts:
        return []
    return [
        Caveat(
            kind="positions_not_current",
            detail=(
                "; ".join(parts) + ". Quote `as_of_date` and `price_as_of` beside any value "
                "you report from those rows, and do not present it as current"
            ),
        )
    ]


def _refused_positions_caveat(refused: list[tuple[int, int, str | None]]) -> list[Caveat]:
    """🔴 The disclosure that an account holds more than its rows show.

    A position whose currency has no minor-unit exponent this build knows, or
    that states none, is refused when its capture is derived, and recorded in
    `refused_holdings`. It is not among the rows, so without this the account
    reads as a smaller portfolio than it is and nothing in the answer says so.

    `rule-applied`, because the position was left out ON PURPOSE: a value at an
    unknown scale cannot be expressed in minor units, and rounding it to a
    guessed one is the wrong number `data-model.md`'s valuation norm refuses.
    The exclusion is from the rows, and so from `totals`, which sums them.

    Names account, security and unit -- the reader's next move is to look at that
    account -- and ids rather than the security's name, because a name is text
    the institution chose and this detail is the server's own.
    """
    if not refused:
        return []
    by_account: dict[int, list[str]] = {}
    for account_id, security_id, currency in sorted(refused, key=lambda r: (r[0], r[1])):
        unit = "no currency stated" if currency is None else currency
        by_account.setdefault(account_id, []).append(f"security {security_id} ({unit})")
    named = "; ".join(
        f"account {account_id}: {', '.join(held)}" for account_id, held in by_account.items()
    )
    return [
        Caveat(
            kind="rule-applied",
            detail=(
                f"{named}. Each is a position its capture listed and this build could not "
                f"record, because its currency has no minor-unit exponent this build knows or "
                f"states none. They are ABSENT from these rows ON PURPOSE -- a value at an "
                f"unknown scale cannot be expressed in minor units, and rounding it to a guessed "
                f"one would be a wrong number -- so each named account holds more than its rows "
                f"show. Say so beside anything you report about it; the capture is archived, and "
                f"a build that knows the unit derives the position exactly"
            ),
        )
    ]


def list_holdings(config: Config) -> Answer:
    """Every position, as its account's latest holdings capture recorded it.

    🔴 **The latest captured day PER ACCOUNT, never today's and never one day
    store-wide.** A position is what it was on the day it was captured, so asking
    for today's rows answers nothing on any day the sync has not run -- and one
    store-wide latest day would drop every account whose most recent capture is
    older than another's, reading as "holds nothing" rather than as "not seen
    since". `as_of_date` on each row says which day it is.

    🔴 **A refusal counts as a capture when finding that day.** A position this
    build could not record lands in `refused_holdings` rather than `holdings`, so
    an account whose newest capture refused every position it held would
    otherwise answer from an OLDER day's rows -- positions it may no longer hold,
    served as its latest. Reading the day across both tables makes that account
    answer with no rows and a `rule-applied` naming what was refused. The day is
    found in SQL and both reads join it, so the answer reads one day per account
    rather than every day the append-only series has kept.

    🔴 **A position's day is recorded in ONE of the two tables.** The deriver
    claims the day across both, so where two captures disagree about a unit the
    day's first capture decides at write time, and this read never chooses
    between a row and a refusal of it.

    🔴 **`price_as_of` is served as stored and never coalesced.** A null means the
    row predates the column and has not been rebuilt, or the institution sent no
    price date -- filling it with `as_of_date` would state that the price is as
    recent as the capture, which is exactly what it cannot be assumed to be.

    🔴 **What the answer cannot vouch for rides the success path**, request-scoped
    to the accounts it is about: `positions_not_current` for a price or a capture
    that is old, `rule-applied` for a refused position, and
    `account_no_longer_active` beside `roster_observed_empty` for a position on an
    account that has stopped being reported.

    🔴 **`totals` sums the rows per currency, and is not a second balance.** A
    position DECOMPOSES its account's balance, which `balance_history` and
    `list_accounts` already count, and the two need not reconcile -- the
    institution reports them separately. See `_holdings_totals`.
    """
    problem = _readable(config)
    if problem is not None:
        return _unusable(config, problem, requested_window=None, truncation=None, totals=[])
    with reader_connection(config) as conn:
        captured_days = union_all(
            select(holdings.c.account_id, holdings.c.as_of_date),
            select(refused_holdings.c.account_id, refused_holdings.c.as_of_date),
        ).subquery()
        newest = (
            select(
                captured_days.c.account_id,
                func.max(captured_days.c.as_of_date).label("as_of_date"),
            )
            .group_by(captured_days.c.account_id)
            .subquery()
        )
        result = conn.execute(
            select(
                holdings.c.account_id,
                accounts.c.name,
                holdings.c.security_id,
                securities.c.name,
                securities.c.ticker,
                securities.c.security_type,
                holdings.c.quantity,
                holdings.c.market_value_minor,
                holdings.c.cost_basis_minor,
                holdings.c.currency,
                holdings.c.as_of_date,
                holdings.c.price_as_of,
            )
            .select_from(
                holdings.join(
                    newest,
                    (newest.c.account_id == holdings.c.account_id)
                    & (newest.c.as_of_date == holdings.c.as_of_date),
                )
                .join(accounts, accounts.c.account_id == holdings.c.account_id)
                .join(securities, securities.c.security_id == holdings.c.security_id)
                .join(institutions, institutions.c.institution_id == accounts.c.institution_id)
            )
            .order_by(
                institutions.c.name,
                accounts.c.name,
                holdings.c.account_id,
                securities.c.name,
                holdings.c.security_id,
            )
        ).all()
        refusals = conn.execute(
            select(
                refused_holdings.c.account_id,
                refused_holdings.c.security_id,
                refused_holdings.c.currency,
                refused_holdings.c.as_of_date,
            ).select_from(
                refused_holdings.join(
                    newest,
                    (newest.c.account_id == refused_holdings.c.account_id)
                    & (newest.c.as_of_date == refused_holdings.c.as_of_date),
                )
            )
        ).all()
        refused: list[tuple[int, int, str | None]] = [
            (int(account_id), int(security_id), currency)
            for account_id, security_id, currency, _ in refusals
        ]
        # 🔴 Each account's newest day, from the rows and refusals this answer
        # serves rather than a read of its own: a sync landing between two reads
        # would leave the warnings judging a different day than the rows.
        latest: dict[int, CalendarDate] = {int(r[0]): calendar_date(r[10]) for r in result}
        latest |= {int(account_id): calendar_date(day) for account_id, _, _, day in refusals}
        lifecycle = _account_lifecycle(conn)
        rows: list[dict[str, Any]] = []
        old_prices: dict[int, tuple[int, CalendarDate]] = {}
        unknown_prices: dict[int, int] = {}
        for r in result:
            account_id = int(r[0])
            captured, priced = calendar_date(r[10]), r[11]
            if priced is None:
                unknown_prices[account_id] = unknown_prices.get(account_id, 0) + 1
            elif captured - calendar_date(priced) > POSITION_PRICE_STALE_AFTER:
                count, oldest = old_prices.get(account_id, (0, calendar_date(priced)))
                old_prices[account_id] = (count + 1, min(oldest, calendar_date(priced)))
            rows.append(
                {
                    "account_id": account_id,
                    "account": r[1],
                    "security_id": int(r[2]),
                    "security_name": r[3],
                    "ticker": r[4],
                    "security_type": r[5],
                    # 🔴 The stored TEXT, untouched. A quantity is not money and is
                    # never a float: `0.00293644` of a coin survives only as text.
                    "quantity": str(r[6]),
                    "market_value_minor_units": int(r[7]),
                    # Present and null when the institution supplied none -- never
                    # dropped, and never a zero.
                    "cost_basis_minor_units": None if r[8] is None else int(r[8]),
                    "currency": r[9],
                    "as_of_date": str(captured),
                    "price_as_of": iso_or_none(priced),
                    # 🔴 On EVERY row, for the reason `list_accounts` carries it: a
                    # position in an account that is no longer active froze on the
                    # day it was captured, and a caller who has to opt in to learning
                    # that is a caller who sums it anyway.
                    **lifecycle[account_id].to_wire(),
                }
            )
        feeds = _investment_feeds(conn)
        # Each connection's newest holdings capture across every account it holds,
        # or the day of its newest archived holdings reply if that is later: a
        # reply that listed no position for an account writes no row, and is still
        # a newer capture that left the account out. Both days come from a reply's
        # own `received_at`, so they cannot straddle midnight against each other.
        connection_newest: dict[int, CalendarDate] = {}
        for account_id, captured in latest.items():
            connection_id = lifecycle[account_id].connection_id
            if connection_id is not None:
                feed = feeds.get(connection_id)
                replied = None if feed is None else feed.replied_day
                connection_newest[connection_id] = max(
                    captured,
                    connection_newest.get(connection_id, captured),
                    captured if replied is None else replied,
                )
        left_behind: dict[int, tuple[CalendarDate, CalendarDate]] = {}
        stopped_feed: dict[int, tuple[CalendarDate, InvestmentFeed]] = {}
        for account_id, captured in latest.items():
            entry = lifecycle[account_id]
            if entry.connection_id is None or not entry.active:
                continue
            newest_capture = connection_newest[entry.connection_id]
            if captured < newest_capture:
                left_behind[account_id] = (captured, newest_capture)
            # 🔴 Not an `elif`. Whether the feed stopped is a fact about the
            # CONNECTION's investments domain, so it holds for an account a newer
            # capture left out as much as for one it listed -- and for that
            # account "the feed is working" alone would be false.
            feed = feeds.get(entry.connection_id)
            if feed is not None and feed.stopped:
                stopped_feed[account_id] = (captured, feed)
        not_active = [lifecycle[a] for a in sorted(latest) if not lifecycle[a].active]
        return _answer(
            config,
            conn,
            rows,
            requested_window=None,
            truncation=None,
            totals=_holdings_totals(rows),
            extra_caveats=(
                _refused_positions_caveat(refused)
                + _positions_not_current_caveat(
                    old_prices=old_prices,
                    unknown_prices=unknown_prices,
                    left_behind=left_behind,
                    stopped_feed=stopped_feed,
                )
                + _not_active_caveat(not_active, consequence=_POSITIONS_FROZE)
                + _roster_observed_empty_caveat(not_active)
            ),
            lifecycle=lifecycle,
        )
