"""The read surface, and the envelope every answer carries.

🔴 **This module exists because of one norm.** `api-contract.md` § Direction:
*every response carries a freshness stamp, and incompleteness rides the success
path as a warning field rather than as an exception.* The dangerous case is not
an error — it is a **successful** answer computed over incomplete data, because
nothing throws and the numbers simply stop being true. The consumer is an
analyst agent that **cannot see a caveat which is not in the payload**:
documentation, log files and a health tool it did not think to call are all
invisible at the moment of answering.

So every function here returns an `Answer`, and an `Answer` cannot be built
without its warnings — they are computed from the datastore in the same call
that computes the rows.

🔴 **The environment is part of the envelope, not just the launch flag.** A
server pointed at sandbox data and one pointed at real money look identical in
their answers unless the answer says which it is. A flag selects; the envelope
confesses.

Read-role only: every handle here opens `mode=ro` at the file, so no query can
write whatever SQL reaches it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Connection as SAConnection

from bankmachine.config import Config
from bankmachine.store.engine import reader_connection
from bankmachine.store.schema import (
    accounts,
    balances_daily,
    connections,
    institutions,
    sync_state,
    transactions,
)
from bankmachine.store.types import UtcInstant, now_utc

#: How long since a connection's last successful sync before its data is called
#: stale. A day and a half: the scheduled job runs nightly, so one missed run is
#: not yet a story and two consecutive ones are.
STALE_AFTER = timedelta(hours=36)

#: The warning vocabulary the API contract fixes. Named here as a tuple rather
#: than left to string literals at each site, because a warning nobody spells the
#: same way twice is a warning a consumer cannot branch on.
WARNING_KINDS: tuple[str, ...] = ("stale", "degraded", "gapped", "partial", "rule-applied")


@dataclass(frozen=True, slots=True)
class Caveat:
    """One reason an answer is less complete than it looks.

    Named `Caveat` rather than `Warning` because the builtin of that name is a
    different thing entirely, and a module that shadows it makes every later
    reader check which one they are looking at.

    Carries the connection it is about where there is one, because an operator
    with ten institutions needs to know which of them went quiet — a bare
    "some data is stale" is a warning they cannot act on.
    """

    kind: str
    detail: str
    connection_id: int | None = None
    institution: str | None = None

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"kind": self.kind, "detail": self.detail}
        if self.connection_id is not None:
            wire["connection_id"] = self.connection_id
        if self.institution is not None:
            wire["institution"] = self.institution
        return wire


@dataclass(frozen=True, slots=True)
class Answer:
    """Rows, and everything a consumer needs to know about how far to trust them.

    🔴 `warnings` is not optional and has no default. A caller cannot construct
    an answer without having considered incompleteness, which is the difference
    between the norm being enforced and being remembered.
    """

    rows: list[dict[str, Any]]
    warnings: list[Caveat]
    environment: str
    as_of: UtcInstant
    coverage: dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return {
            # 🔴 The environment leads. It is the difference between an answer
            # about someone's money and an answer about a fixture, and a reader
            # skimming a payload should not have to look for it.
            "environment": self.environment,
            "as_of": self.as_of.isoformat(),
            "warnings": [w.to_wire() for w in self.warnings],
            "coverage": self.coverage,
            "rows": self.rows,
        }


def _is_short(granted: Any, requested: Any) -> bool:
    """Whether the aggregator granted less history than was asked for.

    A named predicate rather than an inline conjunction, because the null case is
    the one that matters: null granted is NOT a shortfall of zero, it is an
    unmeasured window, and the caller reports the two differently (AC-1.3a).
    """
    return granted is not None and requested is not None and int(granted) < int(requested)


def _pipeline_warnings(conn: SAConnection, now: UtcInstant) -> list[Caveat]:
    """Everything wrong with the data underneath any answer.

    Computed per call rather than cached: an answer's warnings describe the
    datastore at the moment it was read, and a cache would make them describe
    some earlier moment while the rows described this one.
    """
    warnings: list[Caveat] = []
    rows = conn.execute(
        select(
            connections.c.connection_id,
            institutions.c.name,
            connections.c.status,
            connections.c.last_success_at,
            connections.c.last_error_code,
            connections.c.requested_history_days,
            connections.c.granted_history_days,
        )
        .select_from(connections.join(institutions))
        .where(connections.c.retired_at.is_(None))
    ).all()

    if not rows:
        warnings.append(
            Caveat(
                kind="partial",
                detail=(
                    "no connections are enrolled, so this answer is computed over an empty "
                    "datastore rather than over an absence of activity"
                ),
            )
        )

    for row in rows:
        connection_id, name = int(row[0]), str(row[1])
        status, last_success = str(row[2]), row[3]
        requested, granted = row[5], row[6]

        if status == "degraded":
            warnings.append(
                Caveat(
                    kind="degraded",
                    detail=(
                        f"{name} last failed to sync with {row[4] or 'an unrecorded error'}; "
                        f"its data stops at the last successful run"
                    ),
                    connection_id=connection_id,
                    institution=name,
                )
            )
        if last_success is None:
            warnings.append(
                Caveat(
                    kind="partial",
                    detail=f"{name} has never completed a sync, so it contributes no data yet",
                    connection_id=connection_id,
                    institution=name,
                )
            )
        elif now - last_success > STALE_AFTER:
            hours = int((now - last_success).total_seconds() // 3600)
            warnings.append(
                Caveat(
                    kind="stale",
                    detail=f"{name} has not synced successfully for {hours} hours",
                    connection_id=connection_id,
                    institution=name,
                )
            )
        # 🔴 The shortfall AC-11.8 exists to surface. Null granted is NOT no
        # shortfall -- it is not yet known -- and the two are reported
        # differently, because reading the first as the second is the exact
        # inference AC-1.3a forbids.
        if granted is None and last_success is not None:
            warnings.append(
                Caveat(
                    kind="partial",
                    detail=(
                        f"{name}'s granted history window is not yet known; it is measured "
                        f"when the initial backfill completes"
                    ),
                    connection_id=connection_id,
                    institution=name,
                )
            )
        elif _is_short(granted, requested):
            warnings.append(
                Caveat(
                    kind="gapped",
                    detail=(
                        f"{name} granted {int(granted)} days of history against "
                        f"{int(requested)} requested, so anything older than that is absent "
                        f"rather than zero"
                    ),
                    connection_id=connection_id,
                    institution=name,
                )
            )
    return warnings


def _coverage(conn: SAConnection) -> dict[str, Any]:
    """What the datastore actually holds, so an empty answer can be told from an empty world."""
    span = conn.execute(
        select(func.min(transactions.c.posted_date), func.max(transactions.c.posted_date)).where(
            transactions.c.removed_at.is_(None)
        )
    ).one()
    return {
        "connections": conn.execute(
            select(func.count()).select_from(connections).where(connections.c.retired_at.is_(None))
        ).scalar_one(),
        "accounts": conn.execute(select(func.count()).select_from(accounts)).scalar_one(),
        "transactions": conn.execute(
            select(func.count()).select_from(transactions).where(transactions.c.removed_at.is_(None))
        ).scalar_one(),
        "earliest_transaction": None if span[0] is None else str(span[0]),
        "latest_transaction": None if span[1] is None else str(span[1]),
    }


def _answer(config: Config, conn: SAConnection, rows: list[dict[str, Any]]) -> Answer:
    now = now_utc()
    return Answer(
        rows=rows,
        warnings=_pipeline_warnings(conn, now),
        environment=config.environment,
        as_of=now,
        coverage=_coverage(conn),
    )


def list_accounts(config: Config) -> Answer:
    """Every account, with its latest recorded balance."""
    with reader_connection(config) as conn:
        latest = (
            select(
                balances_daily.c.account_id,
                func.max(balances_daily.c.as_of_date).label("as_of_date"),
            )
            .group_by(balances_daily.c.account_id)
            .subquery()
        )
        result = conn.execute(
            select(
                accounts.c.account_id,
                institutions.c.name.label("institution"),
                accounts.c.name,
                accounts.c.mask,
                accounts.c.account_type,
                accounts.c.account_subtype,
                accounts.c.balance_class,
                balances_daily.c.current_minor,
                balances_daily.c.currency,
                latest.c.as_of_date,
            )
            .select_from(
                accounts.join(institutions)
                .outerjoin(latest, latest.c.account_id == accounts.c.account_id)
                .outerjoin(
                    balances_daily,
                    (balances_daily.c.account_id == accounts.c.account_id)
                    & (balances_daily.c.as_of_date == latest.c.as_of_date),
                )
            )
            .order_by(institutions.c.name, accounts.c.name)
        ).all()
        rows = [
            {
                "account_id": int(r[0]),
                "institution": r[1],
                "name": r[2],
                "mask": r[3],
                "type": r[4],
                "subtype": r[5],
                "balance_class": r[6],
                # 🔴 Minor units, and the field says so. A consumer that divided
                # by 100 without knowing would be wrong by two orders of
                # magnitude, and the number would still look plausible.
                "current_minor_units": None if r[7] is None else int(r[7]),
                "currency": r[8],
                "balance_as_of": None if r[9] is None else str(r[9]),
            }
            for r in result
        ]
        return _answer(config, conn, rows)


def list_transactions(
    config: Config,
    *,
    since: str | None = None,
    until: str | None = None,
    account_id: int | None = None,
    limit: int = 100,
) -> Answer:
    """Transactions in a window, newest first. Soft-deleted rows are excluded."""
    with reader_connection(config) as conn:
        statement = (
            select(
                transactions.c.transaction_id,
                accounts.c.name.label("account"),
                transactions.c.posted_date,
                transactions.c.description,
                transactions.c.merchant_name,
                transactions.c.amount_minor,
                transactions.c.currency,
                transactions.c.pending,
                transactions.c.source_category_primary,
                transactions.c.category_override,
            )
            .select_from(transactions.join(accounts))
            .where(transactions.c.removed_at.is_(None))
            .order_by(transactions.c.posted_date.desc(), transactions.c.transaction_id.desc())
            .limit(max(1, min(limit, 1000)))
        )
        if since is not None:
            statement = statement.where(transactions.c.posted_date >= since)
        if until is not None:
            statement = statement.where(transactions.c.posted_date <= until)
        if account_id is not None:
            statement = statement.where(transactions.c.account_id == account_id)
        rows = [
            {
                "transaction_id": int(r[0]),
                "account": r[1],
                "date": str(r[2]),
                "description": r[3],
                "merchant": r[4],
                "amount_minor_units": int(r[5]),
                "currency": r[6],
                "pending": bool(r[7]),
                "category": r[9] or r[8],
                "category_is_override": r[9] is not None,
            }
            for r in conn.execute(statement).all()
        ]
        return _answer(config, conn, rows)


def spending_by_category(
    config: Config, *, since: str | None = None, until: str | None = None
) -> Answer:
    """🔴 An aggregate, which is the shape AC-4.2 asks the tool surface to prefer.

    Money *out* only: this sums negative amounts and reports them as positive
    magnitudes, because "spending" is a question about outflow and mixing
    refunds in would answer a different one. The sign convention is what makes
    that a filter rather than a per-account special case.
    """
    with reader_connection(config) as conn:
        statement = (
            select(
                func.coalesce(
                    transactions.c.category_override,
                    transactions.c.source_category_primary,
                    "UNCATEGORIZED",
                ).label("category"),
                func.count().label("count"),
                func.sum(transactions.c.amount_minor).label("total"),
            )
            .where(
                transactions.c.removed_at.is_(None),
                transactions.c.amount_minor < 0,
            )
            .group_by("category")
            .order_by(func.sum(transactions.c.amount_minor))
        )
        if since is not None:
            statement = statement.where(transactions.c.posted_date >= since)
        if until is not None:
            statement = statement.where(transactions.c.posted_date <= until)
        rows = [
            {
                "category": r[0],
                "transactions": int(r[1]),
                "spent_minor_units": abs(int(r[2])),
            }
            for r in conn.execute(statement).all()
        ]
        return _answer(config, conn, rows)


def pipeline_health(config: Config) -> Answer:
    """Every connection and what is wrong with it — the question AC-ARCH.3 names.

    Returns rows even when everything is fine, because "healthy" is an answer
    and an empty result would be indistinguishable from a broken query.
    """
    with reader_connection(config) as conn:
        result = conn.execute(
            select(
                connections.c.connection_id,
                institutions.c.name,
                connections.c.status,
                connections.c.last_success_at,
                connections.c.last_error_code,
                connections.c.requested_history_days,
                connections.c.granted_history_days,
                connections.c.retired_at,
                sync_state.c.history_start_date,
            )
            .select_from(
                connections.join(institutions).outerjoin(
                    sync_state, sync_state.c.connection_id == connections.c.connection_id
                )
            )
            .order_by(connections.c.connection_id)
        ).all()
        rows = [
            {
                "connection_id": int(r[0]),
                "institution": r[1],
                "status": r[2],
                "last_success_at": None if r[3] is None else r[3].isoformat(),
                "last_error_code": r[4],
                "requested_history_days": None if r[5] is None else int(r[5]),
                # 🔴 Null is reported as null, never as zero or as "complete".
                "granted_history_days": None if r[6] is None else int(r[6]),
                "history_starts": None if r[8] is None else str(r[8]),
                "retired": r[7] is not None,
            }
            for r in result
        ]
        return _answer(config, conn, rows)
