"""`bankmachine sync run` -- fetch what each connection has, one page at a time.

🔴 **The status is consulted before `has_more`, and that ordering is the whole
loop.** A `NOT_READY` reply carries `has_more: false` AND an empty `next_cursor`
*(measured, `api-notes-plaid.md` §17)*, so the obvious `while has_more:` exits
immediately on the first sync of every new connection and records a successful
run with zero transactions. Nothing raises. `last_success_at` gets stamped and
the account reports no activity -- a *successful* response computed over data
that has not materialized, which `api-contract.md` § Direction names as this
product's primary failure mode. The naive loop satisfies AC-2.1's "loop until
the source reports no more pages" literally while being wrong.

🔴 **One connection's failure never aborts another** (AC-4.1). The loop records
the error on the failing connection's own row and continues, and the run's exit
code reports what it found: `1` if it ran and something is degraded, `2` only if
it could not run at all.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.engine import Connection as SAConnection

from bankmachine.cli.exit_codes import EXIT_OK, EXIT_UNHEALTHY
from bankmachine.config import Config
from bankmachine.connector import ConnectorError, FetchedResponse
from bankmachine.connector.plaid.client import PlaidClient
from bankmachine.derivers import ALL_DERIVERS
from bankmachine.logging_setup import get_logger
from bankmachine.secrets import SecretsError, get_access_token, get_plaid_secret
from bankmachine.store.connection import DatastoreMissingError, inspect
from bankmachine.store.derivation import DerivationError, apply_response
from bankmachine.store.engine import reader_connection, transaction, writer_connection
from bankmachine.store.schema import (
    TRANSACTIONS_DOMAIN,
    accounts,
    connections,
    institutions,
    sync_state,
    transactions,
)
from bankmachine.store.types import UtcInstant, now_utc

logger = get_logger("cli.sync_run")

#: The aggregator's own word for "the backfill has not materialized yet". It
#: rides the SUCCESS path, so it is a state rather than an error: the connection
#: is healthy and simply has nothing to give yet (AC-2.6).
NOT_READY = "NOT_READY"

#: The status that means the whole requested window has arrived. AC-1.3a's
#: granted window cannot be computed before this: an earlier reading measures a
#: backfill still in flight and records a shortfall that does not exist.
HISTORICAL_COMPLETE = "HISTORICAL_UPDATE_COMPLETE"

#: How long to wait between polls of a connection that is still materializing,
#: and how many times. AC-2.6 asks for backoff rather than failure; a multi-year
#: backfill takes minutes, and a run that gave up in seconds would report an
#: empty connection as synced.
NOT_READY_DELAYS: tuple[float, ...] = (2.0, 5.0, 15.0, 30.0, 60.0)

#: A ceiling on pages per connection per run. Not a correctness bound -- the
#: cursor makes a resumed run continue exactly where this one stopped -- but a
#: runaway pager on a bad cursor would otherwise loop until the rate limit, and
#: a bounded run that says it stopped early is easier to reason about.
MAX_PAGES_PER_RUN = 500


@dataclass(slots=True)
class ConnectionOutcome:
    """What one connection's sync did, whether or not it worked."""

    connection_id: int
    institution_name: str
    pages: int = 0
    stopped_short: bool = False
    degraded: bool = False
    reason: str | None = None
    still_materializing: bool = False
    historical_complete: bool = False
    granted_history_days: int | None = None
    history_shortfall_days: int | None = None


@dataclass(slots=True)
class RunOutcome:
    """What the whole run did. Its exit code is derived, not accumulated."""

    outcomes: list[ConnectionOutcome] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        """`1` when the run completed and found a problem; `0` when it did not.

        Derived rather than tracked, so a caller cannot forget to set it. `2` is
        never produced here: it means "could not run", and by this point the run
        has run.
        """
        if any(o.degraded for o in self.outcomes):
            return EXIT_UNHEALTHY
        return EXIT_OK


def add_arguments(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Registers `run` under the existing `sync` parser.

    Takes the subparser action rather than the top-level one, because `sync shell`
    already owns `sync` -- and two commands claiming the same name would leave
    whichever registered last silently in charge.
    """
    run_parser = commands.add_parser(
        "run",
        help="fetch new transactions for every enrolled connection",
        description=(
            "Pages through each connection's changes since its last cursor, applying "
            "additions, modifications and removals. Safe to re-run: a second run "
            "produces no net change, and an interrupted one resumes where it stopped."
        ),
    )
    run_parser.add_argument(
        "--connection",
        type=int,
        default=None,
        metavar="ID",
        help="sync only this connection (default: every live one)",
    )
    run_parser.add_argument(
        "--no-wait",
        action="store_true",
        help=(
            "do not wait for a connection whose history is still materializing; "
            "report it and move on"
        ),
    )
    run_parser.set_defaults(handler=cmd_sync_run)


def cmd_sync_run(config: Config, args: argparse.Namespace) -> int:
    status = inspect(config)
    if not status.healthy:
        raise DatastoreMissingError(
            f"datastore at {status.path} is not ready ({status.problem or 'unknown problem'}); "
            f"run `bankmachine store init` before syncing"
        )

    with reader_connection(config) as conn:
        targets = _live_connections(conn, only=args.connection)
    if not targets:
        if args.connection is not None:
            # 🔴 Distinguished from "nothing enrolled". Asking for a connection
            # that is retired or mistyped and being told everything is fine is
            # how an operator concludes a connection is syncing when it is not.
            print(
                f"bankmachine: no live connection {args.connection}. "
                f"`bankmachine connections list --all` shows what exists",
                file=sys.stderr,
            )
            return EXIT_UNHEALTHY
        print("no live connections to sync. `bankmachine enroll` links one")
        return EXIT_OK

    # The aggregator secret is read once, before the loop: a missing one is
    # "could not run" for every connection rather than a per-connection failure,
    # and discovering it once per connection would report N problems for one cause.
    secret = get_plaid_secret(config)
    run = RunOutcome()
    for connection_id, institution_name, credential_ref in targets:
        run.outcomes.append(
            _sync_one(
                config,
                secret,
                connection_id=connection_id,
                institution_name=institution_name,
                credential_ref=credential_ref,
                wait=not args.no_wait,
            )
        )

    _report(run)
    return run.exit_code


def _live_connections(conn: SAConnection, *, only: int | None) -> list[tuple[int, str, str]]:
    statement = (
        select(
            connections.c.connection_id,
            institutions.c.name,
            connections.c.credential_ref,
        )
        .select_from(connections.join(institutions))
        .where(connections.c.retired_at.is_(None))
        .order_by(connections.c.connection_id)
    )
    if only is not None:
        statement = statement.where(connections.c.connection_id == only)
    return [(int(r[0]), str(r[1]), str(r[2])) for r in conn.execute(statement).all()]


def _sync_one(
    config: Config,
    secret: str,
    *,
    connection_id: int,
    institution_name: str,
    credential_ref: str,
    wait: bool,
    sleep: Callable[[float], None] = time.sleep,
) -> ConnectionOutcome:
    """One connection, paged to exhaustion. Never raises for that connection's sake.

    🔴 AC-4.1: a failure here is recorded against this connection and returned,
    not raised. One institution's expired login must not stop the other nine from
    syncing -- and the operator finds out about all of them in one run rather than
    one per run.
    """
    outcome = ConnectionOutcome(connection_id=connection_id, institution_name=institution_name)
    try:
        access_token = get_access_token(config, credential_ref)
    except SecretsError as exc:
        return _degrade(config, outcome, "CREDENTIAL_UNREADABLE", str(exc))

    attempt = 0
    try:
        with PlaidClient(config, secret) as client:
            # 🔴 Accounts first, every run. The transactions deriver refuses a row
            # whose account it has no record of -- deliberately, because skipping
            # it would let the cursor advance past a transaction that is then
            # never offered again. Nothing else in the product called
            # `accounts_get`, so before this the first real sync after a real
            # enrollment would have refused every transaction it fetched.
            #
            # Re-fetched rather than fetched once at enrollment because accounts
            # open, close and are renamed, and a sync that never looked again
            # would derive transactions against a roster frozen on the day the
            # operator linked the institution.
            accounts_page = client.accounts_get(access_token, connection_id=connection_id)
            _persist(config, accounts_page, connection_id)

            while outcome.pages < MAX_PAGES_PER_RUN:
                cursor = _cursor_for(config, connection_id)
                fetched = client.transactions_sync(
                    access_token, cursor=cursor, connection_id=connection_id
                )
                status = _update_status(fetched)

                # 🔴 Before `has_more`, always. See this module's docstring.
                if status == NOT_READY:
                    outcome.still_materializing = True
                    if not wait or attempt >= len(NOT_READY_DELAYS):
                        logger.info(
                            "connection %d is still materializing its history; nothing to "
                            "apply yet",
                            connection_id,
                        )
                        return outcome
                    sleep(NOT_READY_DELAYS[attempt])
                    attempt += 1
                    continue

                attempt = 0
                outcome.still_materializing = False
                if status == HISTORICAL_COMPLETE:
                    outcome.historical_complete = True

                _persist(config, fetched, connection_id)
                outcome.pages += 1
                if not _has_more(fetched):
                    break
            else:
                # 🔴 The loop hit its page ceiling with more to fetch. Reported,
                # because "500 pages applied" with no further word reads as
                # finished -- and the operator would have no reason to run again.
                # Not degraded: nothing is wrong, the run is simply bounded, and
                # the cursor means the next one continues exactly here.
                outcome.stopped_short = True
                logger.info(
                    "connection %d stopped at the %d-page ceiling with more to fetch; "
                    "run again to continue",
                    connection_id,
                    MAX_PAGES_PER_RUN,
                )
    except (ConnectorError, DerivationError) as exc:
        return _degrade(config, outcome, type(exc).__name__, str(exc))

    if outcome.historical_complete:
        _record_granted_window(config, connection_id, outcome)
    # 🔴 A run stopped by the page ceiling has NOT finished, and stamping it
    # `active` with a fresh `last_success_at` would tell every later reader --
    # the freshness warning in `query.py` most of all -- that this connection is
    # up to date. It is not: `has_more` was still true. The status is what a
    # consumer trusts, so it must not claim more than the run did.
    _record_success(config, connection_id, complete=not outcome.stopped_short)
    return outcome


def _is_short(granted: Any, requested: Any) -> bool:
    """Whether the aggregator granted less history than was asked for.

    A named predicate rather than an inline conjunction, and the null case is why:
    a null granted window is NOT a shortfall of zero, it is an unmeasured window
    (AC-1.3a). `query.py` carries the same predicate for the same reason.
    """
    return granted is not None and requested is not None and int(granted) < int(requested)


def _record_granted_window(config: Config, connection_id: int, outcome: ConnectionOutcome) -> None:
    """🔴 AC-1.3a: what the aggregator ACTUALLY granted, measurable for the first time.

    **Only at `HISTORICAL_UPDATE_COMPLETE`, and that gate is the whole point.**
    At `INITIAL_UPDATE_COMPLETE` the backfill is still arriving, so the oldest
    transaction present is the oldest one *so far* -- computing the window there
    records a shortfall that does not exist. It would be a well-formed, plausible,
    wrong number, which is the failure class this product was built to prevent,
    and AC-11.8 would then report a gap against history the operator actually has.

    The window is measured from the oldest transaction the connection returned to
    the day it was measured. A connection with no transactions at all leaves it
    null: nothing was granted that can be counted, and zero would claim a
    measurement nobody made.
    """
    with reader_connection(config) as conn:
        existing, requested = conn.execute(
            select(
                connections.c.granted_history_days,
                connections.c.requested_history_days,
            ).where(connections.c.connection_id == connection_id)
        ).one()
        # 🔴 Measured ONCE, and this guard is the whole difference between a
        # recorded fact and a number that drifts. `HISTORICAL_UPDATE_COMPLETE` is
        # a persistent STATE, not an event -- every later sync reports it too. So
        # re-measuring oldest-held-to-today grows the window by a day per day,
        # and the AC-11.8 shortfall would shrink to nothing on its own: the
        # `gapped` warning would quietly stop being emitted while the missing
        # history stayed missing. That is the silent-staleness failure this
        # product exists to prevent, produced by its own bookkeeping.
        if existing is not None:
            # Already measured, so nothing is written -- but the shortfall is
            # still true, and the summary should keep saying so. Reading it back
            # here is what stops a persisted gap from disappearing out of the
            # CLI's own report just because this run was not the one that found
            # it.
            outcome.granted_history_days = int(existing)
            if _is_short(existing, requested):
                outcome.history_shortfall_days = int(requested) - int(existing)
            return
        oldest = conn.execute(
            select(func.min(transactions.c.posted_date))
            .select_from(transactions.join(accounts))
            .where(accounts.c.connection_id == connection_id)
        ).scalar_one_or_none()
    if oldest is None:
        logger.info(
            "connection %d completed its backfill with no transactions, so there is no "
            "granted window to measure",
            connection_id,
        )
        return

    now = now_utc()
    granted = (now.date() - oldest).days
    with writer_connection(config) as conn, transaction(conn):
        conn.execute(
            update(connections)
            .where(connections.c.connection_id == connection_id)
            .values(granted_history_days=granted, updated_at=now)
        )
        conn.execute(
            update(sync_state)
            .where(
                sync_state.c.connection_id == connection_id,
                sync_state.c.domain == TRANSACTIONS_DOMAIN,
            )
            .values(history_start_date=oldest, updated_at=now)
        )

    outcome.granted_history_days = granted
    if _is_short(granted, requested):
        # AC-11.8: the shortfall is recorded as a known gap rather than the
        # returned window being treated as complete. Logged as well as stored,
        # because the operator's one chance to act on it -- re-linking with a
        # different expectation -- is now.
        outcome.history_shortfall_days = int(requested) - granted
        logger.warning(
            "connection %d granted %d days of history against %d requested: a gap of %d days",
            connection_id,
            granted,
            int(requested),
            outcome.history_shortfall_days,
        )


def _persist(config: Config, fetched: FetchedResponse, connection_id: int) -> None:
    """Archive and derive one page. The cursor rides the derivation's transaction."""
    with writer_connection(config) as conn:
        apply_response(
            conn,
            connection_id=connection_id,
            endpoint=fetched.endpoint.path,
            body=fetched.body,
            received_at=fetched.received_at,
            derivers=ALL_DERIVERS,
            request_context=fetched.request_context,
        )


def _cursor_for(config: Config, connection_id: int) -> str | None:
    """The cursor to resume from, re-read each page rather than carried in memory.

    Carrying it would make the loop's idea of progress and the datastore's able to
    disagree -- and the datastore is the one that survives a crash, so it is the
    one that decides where the next page starts.
    """
    with reader_connection(config) as conn:
        row = conn.execute(
            select(sync_state.c.cursor).where(
                sync_state.c.connection_id == connection_id,
                sync_state.c.domain == TRANSACTIONS_DOMAIN,
            )
        ).one_or_none()
    return None if row is None else row[0]


def _page(fetched: FetchedResponse) -> dict[str, object]:
    """The two control fields this loop reads, and nothing else.

    `parse_float=str` even though no money is read here: it is the habit that
    keeps a float out of the system, and an exception for "this caller does not
    look at amounts today" is how one gets in tomorrow.
    """
    parsed = json.loads(fetched.body, parse_float=str)
    return parsed if isinstance(parsed, dict) else {}


def _update_status(fetched: FetchedResponse) -> str | None:
    value = _page(fetched).get("transactions_update_status")
    return value if isinstance(value, str) else None


def _has_more(fetched: FetchedResponse) -> bool:
    return _page(fetched).get("has_more") is True


def _degrade(
    config: Config, outcome: ConnectionOutcome, code: str, reason: str
) -> ConnectionOutcome:
    """Record the failure on the connection's own row, then let the run continue."""
    outcome.degraded = True
    outcome.reason = reason
    now = now_utc()
    logger.warning("connection %d (%s) failed to sync: %s", outcome.connection_id, code, reason)
    with writer_connection(config) as conn:
        conn.execute(
            update(connections)
            .where(connections.c.connection_id == outcome.connection_id)
            .values(status="degraded", last_error_code=code, last_error_at=now, updated_at=now)
        )
    return outcome


def _record_success(config: Config, connection_id: int, *, complete: bool) -> None:
    """Clear the error state, and stamp `last_success_at` only on a complete run.

    🔴 The two halves are separated deliberately. A bounded run really did clear
    whatever was wrong -- it fetched pages successfully -- so leaving the
    connection `degraded` would be false. But `last_success_at` is what the
    freshness warning reads, and advancing it on a run that stopped mid-history
    would report a connection as current when it is behind.
    """
    now: UtcInstant = now_utc()
    values: dict[str, Any] = {
        "status": "active",
        "last_error_code": None,
        "last_error_at": None,
        "updated_at": now,
    }
    if complete:
        values["last_success_at"] = now
    with writer_connection(config) as conn:
        conn.execute(
            update(connections).where(connections.c.connection_id == connection_id).values(**values)
        )


def _report(run: RunOutcome) -> None:
    for outcome in run.outcomes:
        if outcome.degraded:
            print(f"  {outcome.connection_id}  {outcome.institution_name}: {outcome.reason}")
        elif outcome.still_materializing:
            print(
                f"  {outcome.connection_id}  {outcome.institution_name}: history is still "
                f"being prepared; run again shortly"
            )
        else:
            pages = "page" if outcome.pages == 1 else "pages"
            more = (
                " (stopped at the page ceiling; run again to continue)"
                if (outcome.stopped_short)
                else ""
            )
            print(
                f"  {outcome.connection_id}  {outcome.institution_name}: "
                f"{outcome.pages} {pages} applied{more}"
            )
            if outcome.history_shortfall_days:
                print(
                    f"       🔴 {outcome.granted_history_days} days of history granted "
                    f"against what was requested — a gap of "
                    f"{outcome.history_shortfall_days} days. It cannot be widened "
                    f"without re-linking (AC-1.2)"
                )
    degraded = sum(1 for o in run.outcomes if o.degraded)
    if degraded:
        print(f"\n{degraded} of {len(run.outcomes)} connections could not be synced")
