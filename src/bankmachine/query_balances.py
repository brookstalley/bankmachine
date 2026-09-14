"""The `balance_history` tool: each account's balance over time, and net worth, as one series.

Its own module for the reason `query_holdings.py` is: it reaches the rest of the read surface
only through the shared assembly core in `query.py`, which it imports and adds nothing to.
`query.py` never imports this module back, so the dependency runs one way.

Read-role only, like every tool: its handles come from `reader_connection`, opened `mode=ro`.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import Connection as SAConnection
from sqlalchemy import and_, func, select

from bankmachine.config import Config
from bankmachine.envelope import MAX_ROWS, Answer, Caveat, SeriesCursor, Truncation, series_position
from bankmachine.query import (
    UnknownAccountError,
    _account_exists,
    _account_lifecycle,
    _answer,
    _not_active_caveat,
    _readable,
    _roster_observed_empty_caveat,
    _undenominable_accounts,
    _undenominable_caveat,
    _unusable,
)
from bankmachine.store import lineage
from bankmachine.store.engine import reader_connection
from bankmachine.store.schema import accounts, balances_daily
from bankmachine.store.types import CalendarDate, calendar_date

#: What a `balance_history` row is, where a sentence counts them.
_BALANCE_ROWS = "balance series rows"


@dataclass(frozen=True, slots=True)
class _Successor:
    """The account that took over a stopped account's balance, from its first capture."""

    account_id: int
    day: CalendarDate
    minor: int


def _successors(
    conn: SAConnection, stopped: dict[tuple[int, str], tuple[CalendarDate, int]]
) -> dict[tuple[int, str], _Successor]:
    """Which stopped accounts a later account row took over from, keyed like `stopped`.

    A successor shares the stopped account's identity partition and currency and
    was first captured AFTER the stopped account's last capture. Of those, the one
    first captured earliest is the successor, so a chain of re-links pairs each
    stop with the account that followed it.

    🔴 **The partition is `lineage`'s, not a second rule.** It already decides
    which account rows are one real account for the transaction exclusion. A
    second definition could disagree with it, and then one answer would call two
    rows the same account while another treated them as two.

    🔴 **Strictly after, and a tie names nobody.** A look-alike captured on or
    before the stop day was live beside the account rather than replacing it. Two
    candidates first captured on the same day cannot be told apart. Both cases
    claim no handover, because a handover wrongly claimed hides a move the reader
    needed to see, while a move wrongly flagged is only a figure they question.

    Read over the WHOLE store, whatever the request's scope, for
    `superseded_spans`'s reason: the two generations are different accounts, so
    narrowing to either one leaves it looking unreplaced.
    """
    if not stopped:
        return {}
    first = (
        select(
            balances_daily.c.account_id,
            balances_daily.c.currency,
            func.min(balances_daily.c.as_of_date).label("first_day"),
        )
        .group_by(balances_daily.c.account_id, balances_daily.c.currency)
        .subquery()
    )
    opening = balances_daily.alias("opening")
    rows = conn.execute(
        select(
            first.c.account_id,
            first.c.currency,
            first.c.first_day,
            opening.c.current_minor,
            accounts.c.institution_id,
            accounts.c.mask,
            accounts.c.name,
            accounts.c.account_type,
            accounts.c.account_subtype,
        ).select_from(
            first.join(accounts, accounts.c.account_id == first.c.account_id).join(
                opening,
                (opening.c.account_id == first.c.account_id)
                & (opening.c.currency == first.c.currency)
                & (opening.c.as_of_date == first.c.first_day),
            )
        )
    ).all()
    partition_of: dict[int, tuple[str | int, ...]] = {}
    openings: list[tuple[tuple[str | int, ...], str, _Successor]] = []
    for row in rows:
        held = int(row.account_id)
        partition = lineage.identity_partition(
            account_id=held,
            institution_id=int(row.institution_id),
            mask=row.mask,
            name=row.name,
            account_type=row.account_type,
            account_subtype=row.account_subtype,
        )
        partition_of[held] = partition
        openings.append(
            (
                partition,
                str(row.currency),
                _Successor(
                    account_id=held,
                    day=calendar_date(row.first_day),
                    minor=int(row.current_minor),
                ),
            )
        )
    found: dict[tuple[int, str], _Successor] = {}
    for (held, currency), (last_day, _) in stopped.items():
        later = [
            candidate
            for partition, counted_in, candidate in openings
            if partition == partition_of.get(held)
            and counted_in == currency
            and candidate.account_id != held
            and candidate.day > last_day
        ]
        if not later:
            continue
        earliest = min(candidate.day for candidate in later)
        on_that_day = [candidate for candidate in later if candidate.day == earliest]
        if len(on_that_day) == 1:
            found[(held, currency)] = on_that_day[0]
    # 🔴 One account row cannot take two balances over. Two look-alikes counted
    # together and followed by ONE account are a real drop by one balance, and
    # naming both as handed over would tell the reader net worth did not move.
    claims = Counter((successor.account_id, key[1]) for key, successor in found.items())
    return {
        key: successor
        for key, successor in found.items()
        if claims[(successor.account_id, key[1])] == 1
    }


def _net_worth_days(rows: Sequence[tuple[object, dict[str, Any]]]) -> dict[str, list[str]]:
    """Each currency's net-worth days across the whole answer, oldest first, as `YYYY-MM-DD`."""
    days: dict[str, list[str]] = {}
    for _, row in rows:
        if row["account_id"] is None:
            days.setdefault(str(row["currency"]), []).append(str(row["date"]))
    return {currency: sorted(found) for currency, found in days.items()}


def _series_ends(
    stopped: dict[tuple[int, str], tuple[CalendarDate, int]],
    successors: dict[tuple[int, str], _Successor],
    net_worth_days: dict[str, list[str]],
) -> str:
    """What the lifecycle freeze means on an answer over the balance SERIES, with the figure.

    🔴 **A non-active account counts in net worth through its last capture and
    not after** (owner's ruling at the edge of `api-contract.md` § Direction's
    lifecycle norm). Carrying its last balance forward puts a balance on days
    nobody captured, and a relinked account's old row carried beside its
    replacement counts the same money twice -- measured on the sandbox store, where
    that is exactly 2x on every later day.

    🔴 **The magnitude is still the load-bearing half.** The norm chose include
    over exclude because an exclusion is invisible; in a series it is visible only
    if the answer says which account stopped counting, on which day, and at what
    balance. So each is named with its last day and signed last balance, and the
    per-currency count and signed sum follow. That sum is the figure
    `coverage.not_active_balance_minor_units` carries present-and-zero on every
    answer.

    🔴 **Stopping counting is not the same as net worth moving.** When a later
    account row took the balance over, as a re-link leaves it, net worth moves
    across the handover only by the difference between the two balances. On the
    sandbox store that difference is zero for every relinked account. So the
    figure is split: a replaced account is named with its successor, and the move
    is claimed only of the part nothing replaced. Claiming it of the whole would
    have a reader report a drop that never happened.

    🔴 **A net-worth day strictly between the two ends counts NEITHER account.**
    Another connection captured on it, so the row is complete by the series' own
    rule, and it reads without the relinked balance. A connection that broke, was
    left for days while others kept syncing, and was then re-linked leaves exactly
    that. So each such day is named beside the handover with the balance it leaves
    out, and "moves only by the difference" is said of the handover and not of
    those days.
    """

    def named(account_id: int, currency: str, day: CalendarDate, minor: int) -> str:
        text = f"account {account_id} through {day.isoformat()}, last balance {minor} {currency}"
        successor = successors.get((account_id, currency))
        if successor is None:
            return text
        text = (
            f"{text}, replaced by account {successor.account_id} from "
            f"{successor.day.isoformat()} at {successor.minor} {currency}"
        )
        gap = [
            between
            for between in net_worth_days.get(currency, [])
            if day.isoformat() < between < successor.day.isoformat()
        ]
        if gap:
            text += (
                f" (net-worth rows on {', '.join(gap)} count neither account, so they leave out "
                f"{minor} {currency})"
            )
        return text

    each = "; ".join(
        named(account_id, currency, day, minor)
        for (account_id, currency), (day, minor) in sorted(stopped.items())
    )
    #: Per currency: every stopped account's count and sum, then the replaced part's.
    by_currency: dict[str, tuple[int, int, int, int]] = {}
    for key, (_, minor) in stopped.items():
        count, total, replaced, replaced_total = by_currency.get(key[1], (0, 0, 0, 0))
        if key in successors:
            replaced, replaced_total = replaced + 1, replaced_total + minor
        by_currency[key[1]] = (count + 1, total + minor, replaced, replaced_total)
    ordered = sorted(by_currency.items())
    figure = "; ".join(
        f"{count} account(s) whose last balances sum to {total} {currency}"
        for currency, (count, total, _, _) in ordered
    )
    handed_over = "; ".join(
        f"{replaced} account(s) replaced, last balances summing to {replaced_total} {currency}"
        for currency, (_, _, replaced, replaced_total) in ordered
        if replaced
    )
    left = "; ".join(
        f"{count - replaced} account(s) with nothing replacing them, last balances summing to "
        f"{total - replaced_total} {currency}"
        for currency, (count, total, replaced, replaced_total) in ordered
        if count > replaced
    )
    text = (
        f"Each one counts in a net-worth row (`account_id` null) through the last day it was "
        f"captured and NOT after: {each} (MINOR UNITS, signed). What stopped counting after "
        f"those days: {figure}"
    )
    if handed_over:
        text += (
            f". Of that, {handed_over}: a later account row the institution describes the same "
            f"way counts each balance from the day named, so across that handover net worth "
            f"moves only by the difference between the two balances. The exception is a day "
            f"named above as counting neither account, whose net-worth row leaves that last "
            f"balance out"
        )
    if left:
        text += (
            f". Of that, {left}: a net worth read across one of those days moves by that "
            f"account's last balance with no activity behind it, so name those accounts and that "
            f"figure beside any net worth you quote from a later day"
        )
    return text


@dataclass(frozen=True, slots=True)
class BalanceCapture:
    """One recorded balance, as the series reads it."""

    account_id: int
    day: CalendarDate
    current_minor: int
    currency: str
    balance_class: str


@dataclass(frozen=True, slots=True)
class WithheldNetWorth:
    """A day in one currency whose net-worth row was withheld, and the accounts it lacked."""

    day: CalendarDate
    currency: str
    missing: tuple[int, ...]


def _split(balance_class: str, current_minor: int) -> tuple[int, int]:
    """`(assets, liabilities)` for one balance, partitioned by its account's CLASS.

    🔴 By `balance_class`, never by the sign of the balance. `data-model.md`
    records that column as existing because net worth is its consumer, and an
    operator's reclassification of an account has to reach the report. So an
    overdrawn asset account is negative ASSETS, and a card in credit is negative
    LIABILITIES -- the sign stays where the money is, and `net` is unchanged by
    which of the two it lands in.
    """
    if balance_class == "liability":
        return 0, -current_minor
    return current_minor, 0


def compose_balance_series(
    captures: Sequence[BalanceCapture],
    *,
    counted: dict[tuple[int, str], tuple[CalendarDate, CalendarDate | None]],
    aggregate: bool,
) -> tuple[list[tuple[tuple[int, int, str], dict[str, Any]]], list[WithheldNetWorth]]:
    """Both readings of one series -- per account and net worth -- from one set of captures.

    Pure, so the arithmetic is checkable over generated series without a store.
    Returns every row keyed by `series_position` and in that order, beside the
    days whose net-worth row was withheld.

    `counted` is each (account, currency)'s span in net worth: its first capture
    anywhere in the store, and the last day it counts -- `None` for an account
    still active, whose span is open-ended.

    🔴 **A net-worth row exists only for a COMPLETE day.** Every account counted
    in that currency on that day must have been captured on it, or the row is
    withheld and named. A net worth summed without an account it counts is the
    most believable wrong number this tool could emit, and a partial day is
    ordinary -- one connection's sync fails and the others' succeed.

    🔴 **An active account's span has no end.** Ending every span at its last
    capture would let a connection whose sync stopped three days ago drop out of
    the last three days' totals, silently -- the failure the rule above exists
    to refuse, re-entering through the span. Only an account no longer active
    stops counting after its last capture, and the caller names it.

    🔴 **`net` is summed from the signed balances, not derived from the split.**
    `net = assets - liabilities` then holds as a fact two sums agree on, which a
    test can check, rather than as a subtraction that agrees with itself.
    """
    rows: list[tuple[tuple[int, int, str], dict[str, Any]]] = []
    by_day: dict[tuple[CalendarDate, str], dict[int, BalanceCapture]] = {}
    for capture in captures:
        assets, liabilities = _split(capture.balance_class, capture.current_minor)
        rows.append(
            (
                series_position(capture.day, capture.account_id, capture.currency),
                _series_row(
                    capture.day,
                    capture.account_id,
                    capture.currency,
                    assets=assets,
                    liabilities=liabilities,
                    net=capture.current_minor,
                ),
            )
        )
        by_day.setdefault((capture.day, capture.currency), {})[capture.account_id] = capture
    withheld: list[WithheldNetWorth] = []
    if aggregate:
        # 🔴 Every day a capture landed, crossed with every currency net worth
        # counts -- never only the (day, currency) pairs that hold a capture. A
        # currency whose accounts ALL missed a day the sync ran has no such pair,
        # so judging only those would leave its net-worth row silently absent
        # rather than withheld and named.
        days = {day for day, _ in by_day}
        currencies = {currency for _, currency in counted}
        for day, currency in sorted((d, c) for d in days for c in currencies):
            held = by_day.get((day, currency), {})
            required = {
                account_id
                for (account_id, counted_in), (first, last) in counted.items()
                if counted_in == currency and first <= day and (last is None or day <= last)
            }
            if not required:
                continue
            missing = tuple(sorted(required - held.keys()))
            if missing:
                withheld.append(WithheldNetWorth(day=day, currency=currency, missing=missing))
                continue
            splits = [_split(c.balance_class, c.current_minor) for c in held.values()]
            rows.append(
                (
                    series_position(day, None, currency),
                    _series_row(
                        day,
                        None,
                        currency,
                        assets=sum(assets for assets, _ in splits),
                        liabilities=sum(liabilities for _, liabilities in splits),
                        net=sum(c.current_minor for c in held.values()),
                    ),
                )
            )
    rows.sort(key=lambda keyed: keyed[0])
    withheld.sort(key=lambda w: (w.day, w.currency))
    return rows, withheld


def _series_row(
    day: CalendarDate,
    account_id: int | None,
    currency: str,
    *,
    assets: int,
    liabilities: int,
    net: int,
) -> dict[str, Any]:
    """One row of the series, at either level, under the ONE strict shape both share."""
    return {
        "date": day.isoformat(),
        # 🔴 Null marks the NET-WORTH row. Present at both levels, never dropped.
        "account_id": account_id,
        "assets_minor_units": assets,
        "liabilities_minor_units": liabilities,
        "net_minor_units": net,
        "currency": currency,
    }


def _withheld_net_worth_caveat(withheld: list[WithheldNetWorth]) -> list[Caveat]:
    """🔴 The disclosure that a net-worth row is missing ON PURPOSE, and for which accounts.

    `rule-applied`, because the row was left out by a rule rather than lost: the
    day is incomplete, and a net worth summed over what was captured would be a
    plausible figure with nothing in it saying so. Names each missing account
    with how many days and which span, so a long outage is one line rather than
    a list nobody reads, and says the account rows for those days are still
    here -- the reader's temptation is to add them up, which is the figure the
    rule refused.
    """
    if not withheld:
        return []
    days_by_account: dict[int, list[CalendarDate]] = {}
    for entry in withheld:
        for account_id in entry.missing:
            days_by_account.setdefault(account_id, []).append(entry.day)
    named = "; ".join(
        f"account {account_id} on {len(days)} day(s) between {min(days).isoformat()} and "
        f"{max(days).isoformat()}"
        for account_id, days in sorted(days_by_account.items())
    )
    days = sorted({entry.day for entry in withheld})
    return [
        Caveat(
            kind="rule-applied",
            detail=(
                f"no net-worth row (`account_id` null) is given for {len(days)} day(s) between "
                f"{days[0].isoformat()} and {days[-1].isoformat()}, because an account that net "
                f"worth counts was not captured on them: {named}. Each is WITHHELD ON PURPOSE "
                f"-- a net worth summed without an account it counts would be a wrong figure "
                f"with nothing in it saying so. The account rows for those days are still "
                f"here; do not add them up into a net worth for those days"
            ),
        )
    ]


def balance_history(
    config: Config,
    *,
    since: date | None = None,
    until: date | None = None,
    account_id: int | None = None,
    limit: int = 100,
    after: SeriesCursor | None = None,
) -> Answer:
    """Each account's balance series, and net worth over time, from ONE read.

    🔴 **One statement, both readings.** It reads each (account, currency)'s
    first and last capture across the WHOLE store, left-joined to its captures
    inside the window. So the rule deciding whether a day's net worth is complete
    and the rows it judges come from one snapshot, and an account captured
    before the window but never inside it still counts on the days it should.

    🔴 **A day with no capture is absent, never smoothed.** Nothing carries a
    balance across a day nobody looked; the series has no row for it at either
    level, and a partial day has account rows and a withheld net-worth row.

    With `account_id` the answer is that account's series and no net-worth row:
    a net worth of one account is its balance, and a second row per day saying
    so is a row a caller would add in twice.

    Investment accounts are in these balances already -- the institution reports
    a brokerage account's value like any other -- so nothing here reads holdings.
    """
    problem = _readable(config)
    if problem is not None:
        return _unusable(
            config,
            problem,
            requested_window=(since, until),
            truncation=Truncation.over(
                returned=0, remaining=0, matching=0, resume_from=None, counting=_BALANCE_ROWS
            ),
            window_series="balances",
        )
    with reader_connection(config) as conn:
        if account_id is not None and not _account_exists(conn, account_id):
            raise UnknownAccountError(
                f"account_id {account_id} does not exist. list_accounts reports the ids that do."
            )
        spans = select(
            balances_daily.c.account_id,
            balances_daily.c.currency,
            func.min(balances_daily.c.as_of_date).label("first_day"),
            func.max(balances_daily.c.as_of_date).label("last_day"),
        ).group_by(balances_daily.c.account_id, balances_daily.c.currency)
        if account_id is not None:
            spans = spans.where(balances_daily.c.account_id == account_id)
        span = spans.subquery()
        captured = balances_daily.alias("captured")
        # The balance on each span's last day, read in the same statement, so the
        # figure named for an account that stopped counting is the one its last
        # row carries in this snapshot.
        closing = balances_daily.alias("closing")
        inside = [
            captured.c.account_id == span.c.account_id,
            captured.c.currency == span.c.currency,
        ]
        if since is not None:
            inside.append(captured.c.as_of_date >= calendar_date(since))
        if until is not None:
            inside.append(captured.c.as_of_date <= calendar_date(until))
        result = conn.execute(
            select(
                span.c.account_id,
                span.c.currency,
                span.c.first_day,
                span.c.last_day,
                closing.c.current_minor,
                accounts.c.balance_class,
                captured.c.as_of_date,
                captured.c.current_minor,
            ).select_from(
                span.join(accounts, accounts.c.account_id == span.c.account_id)
                .join(
                    closing,
                    (closing.c.account_id == span.c.account_id)
                    & (closing.c.currency == span.c.currency)
                    & (closing.c.as_of_date == span.c.last_day),
                )
                .outerjoin(captured, and_(*inside))
            )
        ).all()
        lifecycle = _account_lifecycle(conn)
        counted: dict[tuple[int, str], tuple[CalendarDate, CalendarDate | None]] = {}
        stopped: dict[tuple[int, str], tuple[CalendarDate, int]] = {}
        captures: list[BalanceCapture] = []
        for held, currency, first_day, last_day, last_minor, balance_class, day, current in result:
            key = (int(held), str(currency))
            active = lifecycle[key[0]].active
            counted[key] = (calendar_date(first_day), None if active else calendar_date(last_day))
            if not active:
                stopped[key] = (calendar_date(last_day), int(last_minor))
            if day is not None:
                captures.append(
                    BalanceCapture(
                        account_id=key[0],
                        day=calendar_date(day),
                        current_minor=int(current),
                        currency=key[1],
                        balance_class=str(balance_class),
                    )
                )
        ordered, withheld = compose_balance_series(
            captures, counted=counted, aggregate=account_id is None
        )
        unread = ordered if after is None else [k for k in ordered if k[0] > after.position()]
        page = unread[: max(1, min(limit, MAX_ROWS))]
        last = None if not page else page[-1]
        resume_from = (
            None
            if last is None
            else SeriesCursor.issued_for(
                day=date.fromisoformat(last[1]["date"]),
                row_account_id=last[1]["account_id"],
                currency=last[1]["currency"],
                since=since,
                until=until,
                account_id=account_id,
            )
        )
        in_scope = sorted({held for held, _ in counted})
        not_active = [lifecycle[held] for held in in_scope if not lifecycle[held].active]
        undenominable = [
            entry
            for entry in _undenominable_accounts(conn)
            if account_id is None or entry.account_id == account_id
        ]
        return _answer(
            config,
            conn,
            [row for _, row in page],
            requested_window=(since, until),
            truncation=Truncation.over(
                returned=len(page),
                remaining=len(unread),
                matching=len(ordered),
                resume_from=resume_from,
                counting=_BALANCE_ROWS,
            ),
            extra_caveats=(
                _withheld_net_worth_caveat(withheld)
                + _undenominable_caveat(undenominable)
                + _not_active_caveat(
                    not_active,
                    consequence=_series_ends(
                        stopped, _successors(conn, stopped), _net_worth_days(ordered)
                    ),
                )
                + _roster_observed_empty_caveat(not_active)
            ),
            lifecycle=lifecycle,
            window_series="balances",
        )
