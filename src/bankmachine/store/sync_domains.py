"""What one connection's sync domains record about themselves.

`sync_state` is keyed on `(connection_id, domain)` because the domains advance on
their own schedules: a connection's transactions can be paging happily while its
investments have not returned for three weeks. **Every write that records a
domain's progress goes through this module.** Not every statement touching the
table does: re-linking a connection deletes its rows outright (`cli/enroll.py`),
which discards a whole connection's cursors rather than advancing one domain's,
and belongs to the command that decided the connection starts over.

🔴 **One home, because the row is written from two layers and read as one fact.**
A deriver writes it inside its own transaction -- the row claims a body became
rows, so it has to commit with those rows. The sync command writes it too, for
the two things no single body can establish: a whole PULL failing, and a windowed
pull that came back short. An insert-or-update spelled out at each of those call
sites is the shape that drifts, and what drifts is a freshness claim: a column
that says a domain is current when it is not is the silent staleness this product
exists to refuse (AC-4.4).

🔴 **`last_success_at` is a claim about the DOMAIN being current, not about a
response parsing.** So it is stamped only by a caller that knows the domain got
everything it asked for. A pull with two feeds under one domain key -- positions
and a transaction window -- is current only when both landed, and stamping it
when the first one did would report a portfolio as fresh while most of its
history was still missing.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection as SAConnection
from sqlalchemy import insert, select, update

from bankmachine.store.schema import sync_state
from bankmachine.store.types import CalendarDate, UtcInstant


def record_domain_attempt(
    conn: SAConnection, *, connection_id: int, domain: str, at: UtcInstant
) -> None:
    """This domain was tried, whatever came of it.

    🔴 **Written before the calls rather than after them, and that ordering is
    the point.** A domain with no `sync_state` row at all has never been tried,
    and the health surface reports exactly that -- so a pull that failed on its
    first ever run would otherwise leave nothing behind to report, and a
    connection that cannot serve the domain and one whose every attempt has
    failed would read identically (AC-4.5).
    """
    _upsert(conn, connection_id=connection_id, domain=domain, values={"last_attempt_at": at}, at=at)


def record_domain_success(
    conn: SAConnection,
    *,
    connection_id: int,
    domain: str,
    at: UtcInstant,
    cursor: str | None = None,
) -> None:
    """This domain got everything it asked for, as of `at`.

    Clears the error state in the same write: a domain that has just succeeded is
    not also failing, and leaving the code behind would keep the health surface
    reporting a failure that has been repaired.

    `cursor` is passed only by a domain that has one.
    """
    values: dict[str, Any] = {
        "last_success_at": at,
        "last_error_code": None,
        "last_error_at": None,
    }
    if cursor is not None:
        values["cursor"] = cursor
    _upsert(conn, connection_id=connection_id, domain=domain, values=values, at=at)


def record_domain_failure(
    conn: SAConnection, *, connection_id: int, domain: str, code: str, at: UtcInstant
) -> None:
    """This domain failed, and the rest of the connection is none of its business.

    🔴 **`last_success_at` is left exactly as it was.** It is what the size of
    the resulting hole is measured from (AC-4.5), and a failure is the moment
    that measurement matters most -- clearing it here would erase the one number
    an operator needs to know how far back the data stops.
    """
    _upsert(
        conn,
        connection_id=connection_id,
        domain=domain,
        values={"last_error_code": code, "last_error_at": at},
        at=at,
    )


def record_domain_incomplete(
    conn: SAConnection, *, connection_id: int, domain: str, at: UtcInstant
) -> None:
    """This domain's attempt ran to the end and neither failed nor finished.

    🔴 **The state that had no write, and whose absence made a published field
    lie.** `api-contract.md` says `last_error_code` is "what the last attempt at
    this domain failed with, null when it succeeded" -- and with nothing written
    here, a domain that failed on Monday and came back SHORT on Tuesday still
    reported Monday's code on Wednesday. Three surfaces then disagreed: the run
    printed no failure, the caveat said the domain "last failed with X", and the
    contract promised the code belonged to the last attempt. An operator chases a
    failure that is not the last thing that happened.

    So the error state is cleared and `last_success_at` is left exactly as it
    was. Both halves matter: the attempt did not fail, and it did not make the
    domain current either -- the hole is still there and is still measured from
    the stamp this does not move.
    """
    _upsert(
        conn,
        connection_id=connection_id,
        domain=domain,
        values={"last_error_code": None, "last_error_at": None},
        at=at,
    )


def record_domain_history_start(
    conn: SAConnection,
    *,
    connection_id: int,
    domain: str,
    start: CalendarDate,
    at: UtcInstant,
) -> None:
    """How far back this domain's history reaches, as its own feed reported it."""
    _upsert(
        conn,
        connection_id=connection_id,
        domain=domain,
        values={"history_start_date": start},
        at=at,
    )


def _upsert(
    conn: SAConnection,
    *,
    connection_id: int,
    domain: str,
    values: dict[str, Any],
    at: UtcInstant,
) -> None:
    """Insert the row or update it, never a bare `UPDATE`.

    🔴 A bare `UPDATE` against an absent row reports success and writes nothing,
    so the first thing that ever happens to a domain would be discarded -- and
    discarded on the path that believes it was recorded.
    """
    written = {**values, "updated_at": at}
    existing = conn.execute(
        select(sync_state.c.connection_id).where(
            sync_state.c.connection_id == connection_id,
            sync_state.c.domain == domain,
        )
    ).one_or_none()
    if existing is None:
        conn.execute(
            insert(sync_state).values(connection_id=connection_id, domain=domain, **written)
        )
        return
    conn.execute(
        update(sync_state)
        .where(sync_state.c.connection_id == connection_id, sync_state.c.domain == domain)
        .values(**written)
    )
