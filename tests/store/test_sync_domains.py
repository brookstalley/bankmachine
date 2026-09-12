"""The `(connection, domain)` row, written from two layers and read as one fact.

🔴 **Every case here is about an ABSENT row**, because that is the one a bare
`UPDATE` gets wrong: SQLite reports success and writes nothing, so the first
thing that ever happens to a domain is discarded on the path that believes it was
recorded. Every writer in this module inserts or updates for that reason, and the
callers that reach it with no row yet — a pull that fails on a connection's first
run, and the rebuild that replays a window into an emptied store — are exactly the
ones whose evidence matters most.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from bankmachine.config import Config
from bankmachine.store.engine import reader_connection, writer_connection
from bankmachine.store.schema import (
    INVESTMENTS_DOMAIN,
    TRANSACTIONS_DOMAIN,
    connections,
    institutions,
    sync_state,
)
from bankmachine.store.sync_domains import (
    record_domain_attempt,
    record_domain_failure,
    record_domain_history_start,
    record_domain_success,
)
from bankmachine.store.types import calendar_date, now_utc, utc_instant


@pytest.fixture
def connection_id(initialized_config: Config) -> int:
    now = now_utc()
    with writer_connection(initialized_config) as conn:
        institution = conn.execute(
            institutions.insert().values(
                source_institution_id="ins-sync-domains",
                name="Test Institution",
                first_seen_at=now,
                last_seen_at=now,
            )
        ).inserted_primary_key
        assert institution is not None  # an INTEGER PRIMARY KEY insert always yields one
        connection = conn.execute(
            connections.insert().values(
                institution_id=institution[0],
                source_connection_id="item-sync-domains",
                credential_ref="cred-sync-domains",
                status="active",
                capabilities="[]",
                enrolled_at=now,
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key
        assert connection is not None
        return int(connection[0])


def _row(config: Config, connection_id: int, domain: str) -> dict[str, Any] | None:
    with reader_connection(config) as conn:
        found = (
            conn.execute(
                select(sync_state).where(
                    sync_state.c.connection_id == connection_id, sync_state.c.domain == domain
                )
            )
            .mappings()
            .one_or_none()
        )
    return None if found is None else dict(found)


def test_a_failure_on_a_domain_that_has_no_row_yet_still_lands(
    initialized_config: Config, connection_id: int
) -> None:
    """The first thing that ever happens to a domain may be a failure.

    A product the Item never initialized fails on the connection's first run, so
    there is nothing to update — and an update that wrote nothing would report
    the domain as never tried, which is the one reading that says there is no
    hole.
    """
    at = now_utc()
    with writer_connection(initialized_config) as conn:
        record_domain_failure(
            conn,
            connection_id=connection_id,
            domain=INVESTMENTS_DOMAIN,
            code="PRODUCT_NOT_READY",
            at=at,
        )

    row = _row(initialized_config, connection_id, INVESTMENTS_DOMAIN)
    assert row is not None
    assert row["last_error_code"] == "PRODUCT_NOT_READY"
    assert row["last_success_at"] is None


def test_a_failure_leaves_the_last_success_exactly_where_it_was(
    initialized_config: Config, connection_id: int
) -> None:
    """🔴 AC-4.5: the hole is measured FROM that instant.

    Clearing it on failure would erase the one number an operator needs at the
    moment they need it most.
    """
    landed = utc_instant(now_utc() - timedelta(days=9))
    with writer_connection(initialized_config) as conn:
        record_domain_success(
            conn, connection_id=connection_id, domain=INVESTMENTS_DOMAIN, at=landed
        )
        record_domain_failure(
            conn,
            connection_id=connection_id,
            domain=INVESTMENTS_DOMAIN,
            code="PRODUCT_NOT_READY",
            at=now_utc(),
        )

    row = _row(initialized_config, connection_id, INVESTMENTS_DOMAIN)
    assert row is not None
    assert row["last_success_at"] == landed
    assert row["last_error_code"] == "PRODUCT_NOT_READY"


def test_a_success_clears_the_error_it_repaired(
    initialized_config: Config, connection_id: int
) -> None:
    """A code left behind is a health surface stuck red on a transient error."""
    with writer_connection(initialized_config) as conn:
        record_domain_failure(
            conn,
            connection_id=connection_id,
            domain=INVESTMENTS_DOMAIN,
            code="PRODUCT_NOT_READY",
            at=now_utc(),
        )
        record_domain_success(
            conn, connection_id=connection_id, domain=INVESTMENTS_DOMAIN, at=now_utc()
        )

    row = _row(initialized_config, connection_id, INVESTMENTS_DOMAIN)
    assert row is not None
    assert row["last_error_code"] is None
    assert row["last_error_at"] is None


def test_an_attempt_on_a_domain_that_has_no_row_yet_creates_one(
    initialized_config: Config, connection_id: int
) -> None:
    """What tells a domain nobody has tried from one that has never landed."""
    at = now_utc()
    with writer_connection(initialized_config) as conn:
        record_domain_attempt(conn, connection_id=connection_id, domain=INVESTMENTS_DOMAIN, at=at)

    row = _row(initialized_config, connection_id, INVESTMENTS_DOMAIN)
    assert row is not None
    assert row["last_attempt_at"] == at
    assert row["last_success_at"] is None
    assert row["last_error_code"] is None


def test_an_attempt_does_not_disturb_what_the_domain_already_recorded(
    initialized_config: Config, connection_id: int
) -> None:
    """It says a run started, and nothing else. A run that then fails must not
    have cleared the previous success on its way in."""
    landed = utc_instant(now_utc() - timedelta(days=2))
    with writer_connection(initialized_config) as conn:
        record_domain_success(
            conn,
            connection_id=connection_id,
            domain=TRANSACTIONS_DOMAIN,
            at=landed,
            cursor="cursor-kept",
        )
        record_domain_attempt(
            conn, connection_id=connection_id, domain=TRANSACTIONS_DOMAIN, at=now_utc()
        )

    row = _row(initialized_config, connection_id, TRANSACTIONS_DOMAIN)
    assert row is not None
    assert row["last_success_at"] == landed
    assert row["cursor"] == "cursor-kept"


def test_a_measured_range_on_a_domain_that_has_no_row_yet_is_not_discarded(
    initialized_config: Config, connection_id: int
) -> None:
    """The rebuild's case, reachable before the rebuild exists.

    A replay into an emptied store reconciles a window against a domain with no
    row, and a bare `UPDATE` there would drop the measured range silently —
    leaving `history_starts` null, which reads as "nobody has ever measured".
    """
    start = calendar_date(now_utc().date() - timedelta(days=400))
    with writer_connection(initialized_config) as conn:
        record_domain_history_start(
            conn,
            connection_id=connection_id,
            domain=INVESTMENTS_DOMAIN,
            start=start,
            at=now_utc(),
        )

    row = _row(initialized_config, connection_id, INVESTMENTS_DOMAIN)
    assert row is not None
    assert row["history_start_date"] == start
