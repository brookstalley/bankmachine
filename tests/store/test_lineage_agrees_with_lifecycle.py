"""Two derivations of one fact, held to each other.

`query._account_lifecycle` decides whether an account is `no_longer_reported` --
the verdict `list_accounts` publishes and an agent reads to know a balance is
frozen. `store.lineage._still_reported` decides whether an older generation of an
account may be excluded from a total. Over the roster they read the same two
columns and must reach the same answer.

The two live in different modules for a reason -- `query` imports `lineage`, so
the dependency cannot run the other way -- and that separation is exactly what
lets them drift. `_account_lifecycle`'s own docstring names the cost in the
neighbouring case: "Built twice they can disagree, and a verification surface
that contradicts the analysis surface is worse than one that is absent."

🔴 A drift here is not cosmetic. `lineage` saying "no longer reported" where
`query` says "active" is an aggregate silently dropping an account the
verification surface is calling live -- an undercount, in the direction this
product has recorded as the dangerous one, with nothing in either answer to
explain it.

🔴 **They part on ONE branch, deliberately, and it is asserted rather than
tolerated.** A RETIRED connection observes no further rosters, so its last
observation is frozen and every account on it still matches that observation.
`_account_lifecycle` therefore calls such an account `active`, and does not read
`retired_at` at all. `lineage` must reach the opposite answer: the re-link that
retires its connection and enrols a replacement is precisely the shape whose
older generation has to be supersedable, and a frozen observation reading as
current would keep the duplicate in every total. Each side is pinned below, so
neither can quietly acquire the other's answer.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy import insert, select

from bankmachine.config import Config
from bankmachine.query import _account_lifecycle
from bankmachine.store.engine import reader_connection, writer_connection
from bankmachine.store.lineage import _still_reported
from bankmachine.store.schema import accounts, connections, institutions
from bankmachine.store.types import now_utc

_TODAY = date(2026, 9, 11)
_YESTERDAY = _TODAY - timedelta(days=1)


def _connection(conn: Any, *, observed: date | None, retired: bool) -> int:
    now = now_utc()
    institution_id = conn.execute(
        insert(institutions).values(
            source_institution_id=f"ins-{observed}-{retired}",
            name="First Platypus Bank",
            first_seen_at=now,
            last_seen_at=now,
        )
    ).inserted_primary_key[0]
    return int(
        conn.execute(
            insert(connections).values(
                institution_id=institution_id,
                source_connection_id=f"item-{observed}-{retired}",
                credential_ref=f"connection:sandbox:item-{observed}-{retired}",
                capabilities="[]",
                requested_history_days=730,
                status="retired" if retired else "active",
                enrolled_at=now,
                retired_at=now if retired else None,
                roster_observed_date=observed,
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key[0]
    )


def _account(conn: Any, *, connection_id: int, institution_id: int, last_seen: date | None) -> int:
    now = now_utc()
    return int(
        conn.execute(
            insert(accounts).values(
                institution_id=institution_id,
                connection_id=connection_id,
                source_account_id=f"acct-{connection_id}-{last_seen}",
                name="Plaid Checking",
                mask="0000",
                account_type="depository",
                account_subtype="checking",
                balance_class="asset",
                currency="USD",
                lifecycle_status="active",
                first_seen_date=_YESTERDAY,
                last_seen_date=last_seen,
                source="aggregator",
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key[0]
    )


#: Every branch either derivation can take, with BOTH verdicts stated: what
#: `lineage` must answer, and what `list_accounts` must publish. Writing the two
#: out per case is what makes the one deliberate divergence visible as a decision
#: rather than as a test that happens to pass.
_CASES = [
    # case, roster observed, account last seen, connection retired,
    # lineage still_reported, published lifecycle
    ("roster never observed", None, _TODAY, False, True, "active"),
    ("account on the latest roster", _TODAY, _TODAY, False, True, "active"),
    ("account behind the latest roster", _TODAY, _YESTERDAY, False, False, "no_longer_reported"),
    ("account carrying no observation", _TODAY, None, False, False, "no_longer_reported"),
    # The one branch where they part, and the reason is in the module docstring.
    ("connection retired", _TODAY, _TODAY, True, False, "active"),
]


@pytest.mark.parametrize(
    ("case", "observed", "last_seen", "retired", "expected_reported", "expected_lifecycle"),
    _CASES,
    ids=[case[0] for case in _CASES],
)
def test_each_derivation_answers_what_it_is_meant_to(
    initialized_config: Config,
    case: str,
    observed: date | None,
    last_seen: date | None,
    retired: bool,
    expected_reported: bool,
    expected_lifecycle: str,
) -> None:
    """🔴 Asserted per branch, not over a store that happens to hold one shape.

    A single fixture would agree by covering one case and say nothing about the
    other four, which is how a guard like this passes while the thing it guards
    has already drifted.
    """
    with writer_connection(initialized_config) as conn:
        connection_id = _connection(conn, observed=observed, retired=retired)
        institution_id = conn.execute(
            select(connections.c.institution_id).where(connections.c.connection_id == connection_id)
        ).scalar_one()
        account_id = _account(
            conn,
            connection_id=connection_id,
            institution_id=institution_id,
            last_seen=last_seen,
        )

    with reader_connection(initialized_config) as conn:
        published = _account_lifecycle(conn)[account_id].lifecycle
        row = conn.execute(
            select(
                accounts.c.last_seen_date,
                connections.c.roster_observed_date,
                connections.c.retired_at,
            )
            .select_from(
                accounts.outerjoin(
                    connections, connections.c.connection_id == accounts.c.connection_id
                )
            )
            .where(accounts.c.account_id == account_id)
        ).one()

    counted = _still_reported(last_seen=row[0], roster_observed=row[1], retired_at=row[2])

    assert published == expected_lifecycle, (
        f"{case}: list_accounts now reports {published!r} rather than "
        f"{expected_lifecycle!r}, so the verdict an agent reads has moved"
    )
    assert counted is expected_reported, (
        f"{case}: the lineage rule now says still_reported={counted}. Where that "
        f"contradicts the published {published!r} on a branch this table does not "
        f"mark as deliberate, an aggregate is excluding rows on a verdict the "
        f"verification surface calls live"
    )
