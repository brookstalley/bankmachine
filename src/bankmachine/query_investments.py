"""The `query_investment_transactions` tool: every stored trade, windowed, paged and totalled.

Its own module for the reason `query_holdings.py` is: it reaches the rest of the read surface
only through the shared assembly core in `query.py` -- `_readable`, `_unusable`, `_answer` and
the account helpers -- and `query.py` never imports this module back.

A trade is an investment account's activity: a buy, a sell, a dividend, a contribution, a fee.
The aggregator delivers it on its own feed, apart from the transactions `query_transactions`
reads, so an investment account can hold hundreds of these and no transaction at all.

Read-role only, like every tool: its handles come from `reader_connection`, opened `mode=ro`.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.engine import Connection as SAConnection

from bankmachine.config import Config
from bankmachine.envelope import (
    MAX_ROWS,
    Answer,
    Recovery,
    RefusedArgumentError,
    TradeCursor,
    Truncation,
)
from bankmachine.query import (
    ACCOUNT_RECOVERY,
    UnknownAccountError,
    _account_exists,
    _account_lifecycle,
    _answer,
    _not_active_caveat,
    _readable,
    _roster_observed_empty_caveat,
    _unusable,
)
from bankmachine.store.engine import reader_connection
from bankmachine.store.schema import accounts, investment_transactions, securities
from bankmachine.store.types import calendar_date

#: What the rows ARE, as the `rows_truncated` sentence names them.
_COUNTING = "investment transactions"

#: What a non-active account means for an answer over TRADES. Unlike a balance or a
#: position, a trade is a dated event rather than a value that froze, so what changes
#: is only that nothing newer will arrive for it.
_TRADES_STOPPED = (
    "Their trades are real history and stay in the rows and `totals`, but nothing newer will "
    "arrive for them, so an absence of recent trades there is not a quiet account"
)


class UnknownInvestmentTypeError(RefusedArgumentError):
    """An `investment_type` no stored trade carries, refused naming the ones that exist.

    Refused rather than answered empty, for `UnknownCategoryError`'s reason: an empty
    answer to a misspelled type reads as "no such trades", which is a plausible and
    false statement about the account.
    """


def _known_types(conn: SAConnection) -> list[str]:
    return [
        str(value)
        for (value,) in conn.execute(
            select(investment_transactions.c.investment_type)
            .where(investment_transactions.c.removed_at.is_(None))
            .distinct()
            .order_by(investment_transactions.c.investment_type)
        ).all()
    ]


def _filters(
    *,
    since: date | None,
    until: date | None,
    account_id: int | None,
    investment_type: str | None,
    after: TradeCursor | None,
) -> list[ColumnElement[bool]]:
    """The one predicate the rows, both counts and the totals are built from.

    🔴 One list, so the four statements cannot select different sets. The keyset
    clause is the only part `matching` and `totals` leave out, and they build the
    list again with `after=None` rather than editing a copy of it.
    """
    filters: list[ColumnElement[bool]] = [investment_transactions.c.removed_at.is_(None)]
    if since is not None:
        filters.append(investment_transactions.c.trade_date >= calendar_date(since))
    if until is not None:
        filters.append(investment_transactions.c.trade_date <= calendar_date(until))
    if account_id is not None:
        filters.append(investment_transactions.c.account_id == account_id)
    if investment_type is not None:
        filters.append(investment_transactions.c.investment_type == investment_type)
    if after is not None:
        # Newest first, so "after" is strictly older, or the same day with a lower id.
        position = calendar_date(after.trade_date)
        filters.append(
            or_(
                investment_transactions.c.trade_date < position,
                and_(
                    investment_transactions.c.trade_date == position,
                    investment_transactions.c.investment_transaction_id
                    < after.investment_transaction_id,
                ),
            )
        )
    return filters


def _totals(conn: SAConnection, filters: list[ColumnElement[bool]]) -> list[dict[str, Any]]:
    """Per currency, type and subtype: how many trades, and what their amounts come to.

    Over the WHOLE request, never the page, so "how much did I contribute this year" is one
    read. Grouped rather than netted into one figure per currency, because a buy and a
    contribution are both cash movements with opposite meanings, and one net number would
    add a purchase of shares to a deposit of cash.
    """
    result = conn.execute(
        select(
            investment_transactions.c.currency,
            investment_transactions.c.investment_type,
            investment_transactions.c.investment_subtype,
            func.count(investment_transactions.c.investment_transaction_id),
            func.sum(investment_transactions.c.amount_minor),
        )
        .where(*filters)
        .group_by(
            investment_transactions.c.currency,
            investment_transactions.c.investment_type,
            investment_transactions.c.investment_subtype,
        )
        .order_by(
            investment_transactions.c.currency,
            investment_transactions.c.investment_type,
            investment_transactions.c.investment_subtype,
        )
    ).all()
    return [
        {
            "currency": currency,
            "investment_type": investment_type,
            "investment_subtype": subtype,
            "transactions": int(count),
            "amount_minor_units": int(total),
        }
        for currency, investment_type, subtype, count, total in result
    ]


def query_investment_transactions(
    config: Config,
    *,
    since: date | None = None,
    until: date | None = None,
    account_id: int | None = None,
    investment_type: str | None = None,
    limit: int = 100,
    after: TradeCursor | None = None,
) -> Answer:
    """Trades in a window, newest first, with totals over the whole request.

    🔴 **A keyset on (trade_date, investment_transaction_id)**, for `Cursor`'s reason:
    a sync inserting a trade between two pages would shift an offset under a walking
    caller. `trade_date` does not move once stored, so the order a cursor names is the
    order the next page is read in.

    Removed trades are excluded from the rows, both counts and the totals, as
    `investment_transaction_count` on the coverage rows already excludes them.
    """
    problem = _readable(config)
    if problem is not None:
        return _unusable(
            config,
            problem,
            requested_window=(since, until),
            truncation=Truncation.over(
                returned=0, remaining=0, matching=0, resume_from=None, counting=_COUNTING
            ),
            totals=[],
            window_series="investment_transactions",
        )
    with reader_connection(config) as conn:
        if account_id is not None and not _account_exists(conn, account_id):
            raise UnknownAccountError(
                f"account_id {account_id} does not exist. list_accounts reports the ids that do.",
                recovery=ACCOUNT_RECOVERY,
            )
        if investment_type is not None:
            known = _known_types(conn)
            if investment_type not in known:
                raise UnknownInvestmentTypeError(
                    f"investment_type {investment_type!r} is carried by no stored trade. The "
                    f"types this store holds: {', '.join(known) or 'none'}",
                    recovery=Recovery(arguments=("investment_type",), valid_values=tuple(known)),
                )
        filters = _filters(
            since=since,
            until=until,
            account_id=account_id,
            investment_type=investment_type,
            after=after,
        )
        # The same predicate without the keyset clause, for `matching` and `totals`,
        # which describe the whole request rather than what is left of it.
        whole = (
            filters
            if after is None
            else _filters(
                since=since,
                until=until,
                account_id=account_id,
                investment_type=investment_type,
                after=None,
            )
        )
        source = investment_transactions.join(
            accounts, accounts.c.account_id == investment_transactions.c.account_id
        ).outerjoin(securities, securities.c.security_id == investment_transactions.c.security_id)
        selected = conn.execute(
            select(
                investment_transactions.c.investment_transaction_id,
                investment_transactions.c.account_id,
                accounts.c.name,
                investment_transactions.c.trade_date,
                investment_transactions.c.investment_type,
                investment_transactions.c.investment_subtype,
                investment_transactions.c.security_id,
                securities.c.name,
                securities.c.ticker,
                securities.c.security_type,
                investment_transactions.c.quantity,
                investment_transactions.c.price_minor,
                investment_transactions.c.fees_minor,
                investment_transactions.c.amount_minor,
                investment_transactions.c.currency,
                investment_transactions.c.description,
            )
            .select_from(source)
            .where(*filters)
            .order_by(
                investment_transactions.c.trade_date.desc(),
                investment_transactions.c.investment_transaction_id.desc(),
            )
            .limit(max(1, min(limit, MAX_ROWS)))
        ).all()
        lifecycle = _account_lifecycle(conn)
        rows = [
            {
                "investment_transaction_id": int(r[0]),
                "account_id": int(r[1]),
                "account": r[2],
                "trade_date": str(r[3]),
                "investment_type": r[4],
                "investment_subtype": r[5],
                "security_id": None if r[6] is None else int(r[6]),
                "security_name": r[7],
                "ticker": r[8],
                "security_type": r[9],
                # 🔴 The stored TEXT, untouched: a quantity is not money and never a float.
                "quantity": r[10],
                "price_minor_units": None if r[11] is None else int(r[11]),
                "fees_minor_units": None if r[12] is None else int(r[12]),
                "amount_minor_units": int(r[13]),
                "currency": r[14],
                "description": r[15],
                **lifecycle[int(r[1])].to_wire(),
            }
            for r in selected
        ]
        # Counted over the same from-clause as the rows: the inner join to `accounts`
        # is part of what selects a trade, so a count over the bare table could exceed
        # the rows and report a truncation that never happened.
        remaining = conn.execute(
            select(func.count()).select_from(source).where(*filters)
        ).scalar_one()
        matching = (
            remaining
            if after is None
            else conn.execute(select(func.count()).select_from(source).where(*whole)).scalar_one()
        )
        resume_from = (
            None
            if not selected
            else TradeCursor.issued_for(
                trade_date=calendar_date(selected[-1][3]),
                investment_transaction_id=int(selected[-1][0]),
                since=since,
                until=until,
                account_id=account_id,
                investment_type=investment_type,
            )
        )
        # Scoped to the account ASKED ABOUT, for `list_transactions`' reason: naming every
        # closed account on every page of an unscoped walk is noise.
        not_active = (
            [e for e in (lifecycle.get(account_id),) if e is not None and not e.active]
            if account_id is not None
            else []
        )
        return _answer(
            config,
            conn,
            rows,
            requested_window=(since, until),
            truncation=Truncation.over(
                returned=len(rows),
                remaining=int(remaining),
                matching=int(matching),
                resume_from=resume_from,
                counting=_COUNTING,
            ),
            totals=_totals(conn, whole),
            extra_caveats=(
                _not_active_caveat(not_active, consequence=_TRADES_STOPPED)
                + _roster_observed_empty_caveat(not_active)
            ),
            lifecycle=lifecycle,
            window_series="investment_transactions",
        )
