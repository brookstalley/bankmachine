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

🔴 **A backfill still arriving is neither of those, and it gets its own code.**
`INITIAL_UPDATE_COMPLETE` hands over roughly the last thirty days of a two-year
grant, with the rest following minutes to hours later; `NOT_READY` hands over
nothing yet; a page run stopped at its ceiling has more to fetch. All three are
`75` -- ran, nothing wrong, come back -- because a scheduled runner reads the
exit code and nothing else, and under a bare `0` it cannot tell a whole history
from thirty days of one.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.engine import Connection as SAConnection

from bankmachine.cli.exit_codes import EXIT_OK, EXIT_RUN_AGAIN, EXIT_UNHEALTHY
from bankmachine.config import Config
from bankmachine.connector import (
    ConnectorError,
    FetchedResponse,
    ReauthRequiredError,
    TransactionsPaginationRestartError,
    parse_response_body,
)
from bankmachine.connector.plaid.client import PlaidClient
from bankmachine.derivers import ALL_DERIVERS
from bankmachine.logging_setup import get_logger
from bankmachine.secrets import SecretsError, get_access_token, get_plaid_secret
from bankmachine.store.connection import (
    DatastoreMissingError,
    StoreError,
    inspect,
    remedy_for,
)
from bankmachine.store.derivation import apply_response
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

#: How many times one connection's page run may start over within one run.
#:
#: A restart re-reads the cursor and re-fetches, so it does not advance the page
#: count -- which means an unbounded one is a sync that never returns, and a
#: nightly job that never returns is indistinguishable from a slow machine.
#: Five, because the aggregator raises this when its own data moved under the
#: page run, and data that has moved five times in the seconds one run takes is
#: not settling on its own; the next run is the better retry.
MAX_PAGINATION_RESTARTS = 5


@dataclass(slots=True)
class ConnectionOutcome:
    """What one connection's sync did, whether or not it worked."""

    connection_id: int
    institution_name: str
    pages: int = 0
    stopped_short: bool = False
    degraded: bool = False
    reason: str | None = None
    #: Whether the failure is an expired login, which `connections reauth` repairs
    #: in place. Carried as its own field rather than re-derived from the error
    #: code, because the code recorded on the row is the exception's class name
    #: and a report keyed on that string would drift the moment the class moved.
    login_expired: bool = False
    still_materializing: bool = False
    historical_complete: bool = False
    granted_history_days: int | None = None
    history_shortfall_days: int | None = None

    @property
    def unfinished(self) -> bool:
        """Whether this connection still owes history that this run did not get.

        🔴 Positive evidence, not the absence of a stop signal.
        `HISTORICAL_UPDATE_COMPLETE` is the only status that proves the backfill
        landed, so anything else -- `NOT_READY`, `INITIAL_UPDATE_COMPLETE`, a
        status this build does not recognise -- leaves the connection reported as
        still arriving. That is the only direction that cannot claim a window
        nobody watched close, and it is the same gate `_record_granted_window`
        applies for the same reason.

        `stopped_short` is a second, independent way to owe more: a run can page
        through history the aggregator calls complete and still hit its ceiling
        with `has_more` true.
        """
        return not self.historical_complete or self.stopped_short


@dataclass(slots=True)
class RunOutcome:
    """What the whole run did. Its exit code is derived, not accumulated."""

    outcomes: list[ConnectionOutcome] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        """`1` found a problem, `75` did not finish, `0` finished with nothing wrong.

        Derived rather than tracked, so a caller cannot forget to set it. `2` is
        never produced here: it means "could not run", and by this point the run
        has run.

        🔴 A problem outranks an unfinished backfill. `75` asks the caller to
        come back, which a scheduled runner does on its own timetable anyway;
        `1` asks an operator to look at a connection that is stuck. A run that
        found both needs the operator, so the code that reaches one wins.
        """
        if any(o.degraded for o in self.outcomes):
            return EXIT_UNHEALTHY
        if any(o.unfinished for o in self.outcomes):
            return EXIT_RUN_AGAIN
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
            f"cannot sync: the datastore at {status.path} is not ready "
            f"({status.problem or 'unknown problem'}). {remedy_for(status.reason)}"
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
    restarts = 0
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
                try:
                    fetched = client.transactions_sync(
                        access_token, cursor=cursor, connection_id=connection_id
                    )
                except TransactionsPaginationRestartError:
                    # 🔴 The aggregator's data moved while this page run was in
                    # flight, and its documented remedy is to begin again from
                    # the last cursor that was stored. That is what the next
                    # iteration does: the cursor is re-read from the datastore
                    # every page, and a page that failed committed nothing. It is
                    # ordinary on a long initial backfill and cannot happen in
                    # sandbox, where backfills are tiny and static -- so a
                    # connection degraded here would stop at whichever page the
                    # first real backfill happened to be mutated on.
                    restarts += 1
                    if restarts > MAX_PAGINATION_RESTARTS:
                        raise
                    logger.info(
                        "connection %d restarted its page run from the last stored cursor "
                        "(restart %d of %d)",
                        connection_id,
                        restarts,
                        MAX_PAGINATION_RESTARTS,
                    )
                    continue
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
    except ReauthRequiredError as exc:
        # Named before the broad connector catch below so the report can offer the
        # repair. Everything else about the handling is identical -- this one
        # connection degrades and the run carries on (AC-4.1).
        outcome.login_expired = True
        return _degrade(config, outcome, type(exc).__name__, str(exc))
    except (ConnectorError, StoreError) as exc:
        # 🔴 `StoreError`, not `DerivationError`. `_persist` takes the exclusive
        # writer lock per page and does not wait, so an ordinary `store backup`
        # running beside a sync raises `AnotherWriterRunningError` -- a sibling
        # of `DerivationError` rather than a subclass, which escaped this catch,
        # escaped the run, and was reported as a command that could not run at
        # all. Every connection after the locked one was then skipped, which is
        # the one thing AC-4.1 says must never happen.
        return _degrade(config, outcome, type(exc).__name__, str(exc))

    if outcome.historical_complete:
        _record_granted_window(config, connection_id, outcome)
    # 🔴 A run that still owes history has NOT finished, and stamping it `active`
    # with a fresh `last_success_at` would tell every later reader -- the
    # freshness warning in `query.py` and `get_pipeline_health` most of all --
    # that this connection is up to date. The status is what a consumer trusts,
    # so it must not claim more than the run did.
    _record_success(config, connection_id, history_complete=not outcome.unfinished)
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
    look at amounts today" is how one gets in tomorrow. It is not passed at this
    call because `parse_response_body` makes it a property of reading a body at
    all, which is one fewer caller who has to remember.

    🔴 **Ruling on the three parse failures: the shared helper, raising into the
    per-connection refusal this loop already has.** This is the multi-connection
    path, and it had no guard at all: an unreadable page raised out of
    `_update_status` or `_has_more`, past `_sync_one`'s catch -- which names
    `ConnectorError` and `StoreError`, and none of `JSONDecodeError`,
    `RecursionError` or `UnicodeDecodeError` is either -- and ended the whole
    run, so every connection queued behind this one went unsynced on account of
    one institution's malformed reply. That is the single thing AC-4.1 says must
    never happen. `MalformedResponseError` is a `ConnectorError`, so this
    connection is now recorded degraded and the run carries on.

    Degrading rather than returning an empty mapping is the other half of the
    ruling: `{}` would make `has_more` false and the status unknown, which reads
    as a page run that finished cleanly. Silence is the one disallowed outcome.
    """
    parsed = parse_response_body(
        fetched.body, what="a transactions page", endpoint=fetched.endpoint
    )
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


def _record_success(config: Config, connection_id: int, *, history_complete: bool) -> None:
    """Clear the error state, and stamp `last_success_at` only once the history is in.

    🔴 The two halves are separated deliberately. A run that fetched pages really
    did clear whatever was wrong, so leaving the connection `degraded` would be
    false. But `last_success_at` is what the freshness warning and
    `get_pipeline_health` read, and to both of them a successful sync means *the
    backfill is in*.

    🔴 The parameter names the aggregator's status, not the pager's, because
    those are two different questions and only one of them is the one worth
    stamping. A connection at `INITIAL_UPDATE_COMPLETE` has paged cleanly to the
    end of what it was offered and holds roughly thirty days of a two-year grant;
    the remaining seven hundred are still arriving. Stamping there reports a
    connection as current while it is behind by almost all of its history --
    which is the silent staleness this product exists to refuse, produced by its
    own bookkeeping.
    """
    now: UtcInstant = now_utc()
    values: dict[str, Any] = {
        "status": "active",
        "last_error_code": None,
        "last_error_at": None,
        "updated_at": now,
    }
    if history_complete:
        values["last_success_at"] = now
    with writer_connection(config) as conn:
        conn.execute(
            update(connections).where(connections.c.connection_id == connection_id).values(**values)
        )


def _report(run: RunOutcome) -> None:
    for outcome in run.outcomes:
        if outcome.degraded:
            print(f"  {outcome.connection_id}  {outcome.institution_name}: {outcome.reason}")
            if outcome.login_expired:
                # 🔴 The line an operator acts on, and the reason it names the
                # command rather than the institution: enrolling again is the
                # move they will otherwise make, and it mints a SECOND item whose
                # roster re-issues every account and transaction id -- doubling
                # every total with no warning naming it (AC-4.3). `reauth`
                # renews the login against the item this connection already has,
                # so the cursor and the history survive.
                print(
                    "       the login expired. Repair it in place with\n"
                    f"       `bankmachine connections reauth {outcome.connection_id}` --\n"
                    "       enrolling again would duplicate this connection's history"
                )
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
            if not outcome.historical_complete:
                # 🔴 A bare "N pages applied" reads as finished, and at
                # `INITIAL_UPDATE_COMPLETE` it is roughly thirty days of a
                # two-year grant. The line names what is NOT known rather than
                # only that something is missing: the granted window is null, so
                # there is no window to report, and the oldest row here is the
                # oldest so far rather than the oldest that exists. This is the
                # same fact the read path already puts on every answer as a
                # `partial` warning; the two surfaces must not disagree.
                #
                # Wrapped by hand rather than left to the terminal, because the
                # shortfall line below it is the only other continuation the
                # operator ever sees and a reflowed paragraph beside a fixed one
                # reads as two different kinds of thing.
                print(
                    "       the history is still arriving. The granted window is not yet\n"
                    "       known, so it cannot be reported here, and the oldest\n"
                    "       transaction applied so far is not the oldest that exists.\n"
                    "       Run again shortly"
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
    owing = sum(1 for o in run.outcomes if not o.degraded and o.unfinished)
    if owing:
        # The summary an operator scanning ten institutions reads. Degraded
        # connections are counted separately above and never here: those need
        # someone to look at them, not another run. The exit code is deliberately
        # not quoted -- it is `1` rather than `75` whenever both counts are
        # non-zero, and a line that named the wrong number would be worse than
        # one that names none.
        print(f"\n{owing} of {len(run.outcomes)} connections still owe history; run again")
