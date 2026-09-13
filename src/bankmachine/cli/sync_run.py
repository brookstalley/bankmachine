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
import math
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Final

from sqlalchemy import func, select, update
from sqlalchemy.engine import Connection as SAConnection

from bankmachine.cli.exit_codes import EXIT_OK, EXIT_RUN_AGAIN, EXIT_UNHEALTHY
from bankmachine.cli.parser import AnyParser
from bankmachine.config import Config
from bankmachine.connector import (
    INVESTMENTS_PRODUCT,
    ConnectorError,
    FetchedResponse,
    ReauthRequiredError,
    TransactionsPaginationRestartError,
    parse_response_body,
)
from bankmachine.connector.plaid.client import PlaidClient
from bankmachine.connector.plaid.window import read_investment_transaction_page
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
from bankmachine.store.investments import (
    WindowOutcome,
    record_investment_transaction_window,
    window_is_exhausted,
)
from bankmachine.store.raw import RawResponse
from bankmachine.store.schema import (
    INVESTMENTS_DOMAIN,
    TRANSACTIONS_DOMAIN,
    accounts,
    connections,
    decode_capabilities,
    institutions,
    sync_state,
    transactions,
)
from bankmachine.store.sync_domains import (
    record_domain_attempt,
    record_domain_failure,
    record_domain_history_start,
    record_domain_incomplete,
    record_domain_success,
)
from bankmachine.store.types import UtcInstant, calendar_date, now_utc

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

#: What `--until-ready` does between whole runs, and how many it will make.
#:
#: 🔴 A different wait from `NOT_READY_DELAYS`, which polls one connection
#: *inside* a run over about two minutes. This one waits between runs, and the
#: thing it is waiting for is a multi-year backfill the aggregator delivers over
#: minutes to hours -- so the two cannot share a schedule. Twelve attempts five
#: minutes apart bounds the wait at roughly an hour, which is long enough for
#: the backfills actually measured and short enough that a connection which will
#: never finish is reported to someone the same morning. A backfill outlasting
#: it wants the scheduler, not a longer flag.
DEFAULT_RETRY_DELAY_SECONDS: Final[float] = 300.0
DEFAULT_MAX_ATTEMPTS: Final[int] = 12

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
    #: 🔴 The investments window's own shortfall, kept apart from `stopped_short`.
    #: That flag is the transactions pager's, and `unfinished` reads it to decide
    #: `history_complete`, which is the only thing that stamps
    #: `connections.last_success_at`. Routing an investments shortfall through it
    #: withheld the CONNECTION's freshness stamp for a condition belonging to one
    #: domain -- so the connection then read stale, `_domain_caveats` suppressed
    #: the per-domain caveat exactly because it did, and the operator was pointed
    #: at the connection for something no connection-level repair touches. Both
    #: flags mean "come back", and only one of them means the history is not in.
    investments_window_short: bool = False
    degraded: bool = False
    reason: str | None = None
    #: Whether the failure is an expired login, which `connections reauth` repairs
    #: in place. Carried as its own field rather than re-derived from the error
    #: code, because the code recorded on the row is the exception's class name
    #: and a report keyed on that string would drift the moment the class moved.
    login_expired: bool = False
    still_materializing: bool = False
    historical_complete: bool = False
    #: Whether this run pulled the connection's positions. Reported because the
    #: gate is silent by design -- a connection that cannot serve investments and
    #: one whose capabilities could not be read both simply do not call, and
    #: without a word in the report the two are indistinguishable from a run that
    #: pulled them and found nothing.
    investments_pulled: bool = False
    #: How many investment transactions a complete window retired this run.
    #: Reported because a soft delete is otherwise invisible -- the rows simply
    #: stop appearing in every total, which is the shape of silent wrongness this
    #: product exists to refuse.
    investment_transactions_removed: int = 0
    #: 🔴 What went wrong with the investments pull, recorded against the DOMAIN
    #: rather than the connection. `connections.status` means credential health,
    #: and a product this Item never initialized failing is not a statement about
    #: the login -- marking the connection degraded for it sends the operator to
    #: `connections reauth`, which cannot fix it. A credential error reached
    #: through an investments call is different in kind and still degrades the
    #: connection, through the path that prints the repair.
    #:
    #: The code rather than a boolean, because the run's exit code and its report
    #: both need to say WHICH failure, and a flag beside a separately-stored
    #: string is two things that can disagree.
    investments_error_code: str | None = None
    investments_error_reason: str | None = None
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

        🔴 **An investments window that came back short is NOT one of them.**
        This property answers "is this connection's HISTORY in", and that is the
        transactions backfill -- it is what `_record_success` stamps
        `last_success_at` from. A domain that owes more makes the RUN worth
        repeating (`RunOutcome.exit_code` reads it separately) without making the
        connection's freshness a lie.
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
        # 🔴 A domain failure counts, though it leaves the CONNECTION active.
        # `api-contract.md`'s exit-code contract splits on what happened, not on
        # which column recorded it: a connection that ran and found a problem is
        # `1` whether the problem was its login or one of its domains. Collapsing
        # an investments failure into `0` would make a scheduled runner's only
        # machine-readable signal say nothing is wrong.
        if any(o.degraded or o.investments_error_code is not None for o in self.outcomes):
            return EXIT_UNHEALTHY
        # 🔴 A domain that owes more counts here and nowhere else. It asks the
        # caller to come back, which is what `75` means -- and it deliberately
        # does not travel through `unfinished`, which answers a different
        # question (is the connection's history in) whose answer stamps
        # `connections.last_success_at`.
        if any(o.unfinished or o.investments_window_short for o in self.outcomes):
            return EXIT_RUN_AGAIN
        return EXIT_OK


def _attempt_cap(raw: str) -> int:
    """`--max-attempts`, bounded where argparse reads it.

    🔴 At the converter rather than inside the loop, because the loop is one
    branch of two: validating there left `sync run --retry-delay -1` refused
    under `--until-ready` and accepted without it, which is a bound that holds
    only on the path somebody remembered. A converter holds on both, and a bad
    value becomes a usage error -- which is what it is -- instead of borrowing
    `ConfigError`, whose own docstring says the configuration would not resolve.
    """
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be an integer, got {raw!r}") from None
    if value < 1:
        # Zero attempts would exit 75 having run nothing, which is exactly what
        # a backfill that never landed looks like.
        raise argparse.ArgumentTypeError(f"must be at least 1, got {value}")
    return value


def _retry_delay(raw: str) -> float:
    """`--retry-delay`, bounded where argparse reads it. See `_attempt_cap`."""
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be a number, got {raw!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"must not be negative, got {value}")
    if not math.isfinite(value):
        # `float("nan") < 0` is False and `float("inf") < 0` is False, so neither
        # is caught above -- and `time.sleep(nan)` raises from inside the loop
        # while `sleep(inf)` waits forever, which is the one outcome a bounded
        # cap exists to prevent.
        raise argparse.ArgumentTypeError(f"must be a finite number, got {raw!r}")
    return value


def add_arguments(commands: argparse._SubParsersAction[AnyParser]) -> None:
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
    run_parser.add_argument(
        "--until-ready",
        action="store_true",
        help=(
            "keep running until no connection still owes history (exit 0). Any other "
            "problem stops the loop and is reported as itself"
        ),
    )
    run_parser.add_argument(
        "--max-attempts",
        type=_attempt_cap,
        default=None,
        metavar="N",
        help=(
            f"with --until-ready, how many runs to make before giving up "
            f"(default: {DEFAULT_MAX_ATTEMPTS})"
        ),
    )
    run_parser.add_argument(
        "--retry-delay",
        type=_retry_delay,
        default=None,
        metavar="SECONDS",
        help=(
            f"with --until-ready, how long to wait between runs "
            f"(default: {DEFAULT_RETRY_DELAY_SECONDS:.0f})"
        ),
    )
    # The parser itself, so the handler can raise a real usage error through
    # argparse's own channel rather than inventing a second one.
    run_parser.set_defaults(handler=cmd_sync_run, tuning_parser=run_parser)


def cmd_sync_run(
    config: Config,
    args: argparse.Namespace,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """One run, or -- under `--until-ready` -- as many as it takes.

    🔴 The loop re-enters the whole command rather than retrying anything
    inside it. A run is already idempotent and resumes from its own cursor
    (AC-2.5), so "run it again" is the operation the runbook has always
    prescribed; this flag stops making a person be the one who repeats it.
    """
    if not args.until_ready:
        # 🔴 A usage error, not a silent no-op. Without this,
        # `sync run --max-attempts 20` makes exactly one attempt and exits 75
        # with nothing said -- and an operator who believes they enabled the
        # loop reads that 75 as the loop having given up.
        tuning = (("--max-attempts", args.max_attempts), ("--retry-delay", args.retry_delay))
        supplied = [name for name, value in tuning if value is not None]
        if supplied:
            args.tuning_parser.error(f"{' and '.join(supplied)} only applies with --until-ready")
        return _run_once(config, args)

    max_attempts = DEFAULT_MAX_ATTEMPTS if args.max_attempts is None else args.max_attempts
    retry_delay = DEFAULT_RETRY_DELAY_SECONDS if args.retry_delay is None else args.retry_delay

    for attempt in range(1, max_attempts + 1):
        code = _run_once(config, args)
        # 🔴 Only 75 is retried. `1` outranks it (`exit_codes.py`): a stuck
        # connection needs a person, and another attempt would bury the one
        # signal that says so under an hour of polling. Every other code,
        # including an unrecognised one, is returned unchanged rather than
        # interpreted here.
        if code != EXIT_RUN_AGAIN:
            if attempt > 1:
                # 🔴 The durable record of the wait. Without it the log of an
                # hour of polling is indistinguishable from one ordinary exit,
                # and nobody is watching the terminal of a flag built to be run
                # unattended.
                logger.info(
                    "--until-ready finished at attempt %d of %d with exit %d",
                    attempt,
                    max_attempts,
                    code,
                )
            return code
        if attempt == max_attempts:
            break
        logger.info(
            "--until-ready: history still owed after attempt %d of %d; waiting %.0fs",
            attempt,
            max_attempts,
            retry_delay,
        )
        print(f"\nstill owed after attempt {attempt} of {max_attempts}; waiting {retry_delay:.0f}s")
        sleep(retry_delay)

    # Still 75, deliberately. Reaching the cap is not a new state: history is
    # still owed and the answer is still to come back. Saying so on stderr
    # separates "I stopped waiting" from "it finished", which the exit code
    # alone cannot.
    logger.warning(
        "--until-ready gave up waiting after %d attempts; history is still owed",
        max_attempts,
    )
    print(
        f"bankmachine: history is still owed after {max_attempts} attempts; "
        f"giving up waiting. Nothing is wrong with the connections -- run again "
        f"later, or leave it to the scheduler",
        file=sys.stderr,
    )
    return EXIT_RUN_AGAIN


def _run_once(config: Config, args: argparse.Namespace) -> int:
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
    for connection_id, institution_name, credential_ref, capabilities in targets:
        run.outcomes.append(
            _sync_one(
                config,
                secret,
                connection_id=connection_id,
                institution_name=institution_name,
                credential_ref=credential_ref,
                capabilities=capabilities,
                wait=not args.no_wait,
            )
        )

    _report(run)
    return run.exit_code


def _live_connections(
    conn: SAConnection, *, only: int | None
) -> list[tuple[int, str, str, frozenset[str]]]:
    """Every connection to sync, and what each of them reports it can serve.

    The capabilities ride along because AC-3.2 decides per connection whether the
    investments endpoints are called at all, and the alternative -- re-reading the
    column inside the per-connection loop -- would open a second handle per
    connection to answer a question this one already had in hand.
    """
    statement = (
        select(
            connections.c.connection_id,
            institutions.c.name,
            connections.c.credential_ref,
            connections.c.capabilities,
        )
        .select_from(connections.join(institutions))
        .where(connections.c.retired_at.is_(None))
        .order_by(connections.c.connection_id)
    )
    if only is not None:
        statement = statement.where(connections.c.connection_id == only)
    targets: list[tuple[int, str, str, frozenset[str]]] = []
    for row in conn.execute(statement).all():
        connection_id = int(row[0])
        try:
            capabilities = decode_capabilities(str(row[3]))
        except ValueError as exc:
            # 🔴 Said out loud rather than defaulted to "can do nothing". An
            # unreadable capabilities column means this connection's optional
            # domains go unpulled, and a connection that quietly stops pulling
            # investments looks exactly like one whose institution never offered
            # them. The domains it has no choice about still run.
            logger.warning(
                "connection %d has an unreadable capabilities record, so only the domains "
                "every connection has are synced for it: %s",
                connection_id,
                exc,
            )
            capabilities = frozenset()
        targets.append((connection_id, str(row[1]), str(row[2]), capabilities))
    return targets


def _sync_one(
    config: Config,
    secret: str,
    *,
    connection_id: int,
    institution_name: str,
    credential_ref: str,
    capabilities: frozenset[str],
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

            # 🔴 AC-3.2: pulled for a connection that reports it can serve them,
            # and for no other reason. Whole-value membership of the recorded
            # list -- nothing here looks at which institution this is, and the
            # roster stays out of the code.
            #
            # Before the page loop, not after it, because that loop returns early
            # while the aggregator is still materializing a transactions backfill
            # (`NOT_READY`, which can persist for minutes on a first sync). The
            # positions are ready regardless -- they share no cursor and no
            # window with the transactions -- so ordering them after that return
            # would leave an investments-capable connection with no holdings for
            # as long as its transactions took to build.
            if INVESTMENTS_PRODUCT in capabilities:
                _pull_investments(
                    config,
                    client,
                    access_token,
                    connection_id=connection_id,
                    outcome=outcome,
                )

            _record_attempt(config, connection_id, TRANSACTIONS_DOMAIN)
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
        record_domain_history_start(
            conn,
            connection_id=connection_id,
            domain=TRANSACTIONS_DOMAIN,
            start=calendar_date(oldest),
            at=now,
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


def _persist(config: Config, fetched: FetchedResponse, connection_id: int) -> RawResponse:
    """Archive and derive one page. The cursor rides the derivation's transaction.

    Returns the archived response, because the investments window needs to name
    the pages it was assembled from: a row carrying none of THIS window's
    response ids is a row the window did not return, and that is the only
    removal signal the endpoint offers (`store.investments`).
    """
    with writer_connection(config) as conn:
        return apply_response(
            conn,
            connection_id=connection_id,
            endpoint=fetched.endpoint.path,
            body=fetched.body,
            received_at=fetched.received_at,
            derivers=ALL_DERIVERS,
            request_context=fetched.request_context,
        )


def _record_attempt(config: Config, connection_id: int, domain: str) -> None:
    """This domain was tried on this run, whatever comes of it.

    🔴 **Before the calls, not after them.** The health surface reads a domain
    with no `sync_state` row at all as one that has never been tried, and that
    reading is what makes AC-4.5's distinction possible -- so a pull that fails
    on its first ever run has to have left the row behind before it failed.
    Without this, a connection that cannot serve investments and one whose every
    attempt has failed are the same absence.
    """
    with writer_connection(config) as conn:
        record_domain_attempt(conn, connection_id=connection_id, domain=domain, at=now_utc())


def _pull_investments(
    config: Config,
    client: PlaidClient,
    access_token: str,
    *,
    connection_id: int,
    outcome: ConnectionOutcome,
) -> None:
    """One capable connection's positions and its investment-transaction window.

    🔴 **A failure here costs the investments DOMAIN and nothing else.** The
    capability gate opens for any connection whose recorded capabilities name the
    product, and those include products the Item has never initialized -- so this
    can fail for a connection whose transactions are perfectly healthy. Two
    things follow, and they are separate:

    * The exception does not escape, so the page loop still runs. Raising would
      abandon the transactions backfill on this run and every run after it, for a
      reason that has nothing to do with transactions.
    * The failure is recorded against this connection's investments `sync_state`
      row rather than against `connections.status`. That column means credential
      health; a `PRODUCT_NOT_READY` recorded there sends the operator to
      `connections reauth`, which cannot fix it.

    🔴 **Recorded HERE, where it is caught, rather than carried to the end of the
    connection's run.** The page loop returns early while a first sync is still
    materializing its history, so a failure carried past it reached no column at
    all on exactly the run an operator most needs to see it.

    🔴 **`last_success_at` is stamped only once BOTH feeds are in.** Positions
    and the transaction window share one domain key, so a run whose holdings
    landed while its window came back short has not made the domain current --
    stamping it there would report a portfolio as fresh while most of its history
    was missing, which is the silent staleness this product exists to refuse.
    """
    _record_attempt(config, connection_id, INVESTMENTS_DOMAIN)
    try:
        holdings_page = client.investments_holdings_get(access_token, connection_id=connection_id)
        _persist(config, holdings_page, connection_id)
        outcome.investments_pulled = True
        window = _pull_investment_transactions(
            config,
            client,
            access_token,
            connection_id=connection_id,
            outcome=outcome,
        )
    except ReauthRequiredError:
        # 🔴 Re-raised, because this one IS about the login. It degrades the
        # connection through the path that prints the repair, which is the whole
        # difference between a failure an operator can fix and one they can only
        # stare at.
        raise
    except (ConnectorError, StoreError) as exc:
        outcome.investments_error_code = type(exc).__name__
        outcome.investments_error_reason = str(exc)
        now = now_utc()
        with writer_connection(config) as conn:
            record_domain_failure(
                conn,
                connection_id=connection_id,
                domain=INVESTMENTS_DOMAIN,
                code=outcome.investments_error_code,
                at=now,
            )
        logger.warning(
            "connection %d could not pull investments; its transactions are synced "
            "regardless and the failure is recorded against the investments domain: %s",
            connection_id,
            exc,
        )
        return

    if not window.exhausted:
        # Nothing is wrong: the window was simply seen in part, which the run
        # reports as work still owed. The domain is not current either, so the
        # stamp is withheld rather than the failure recorded.
        #
        # 🔴 But the attempt is still recorded as having not failed. Writing
        # nothing here left a previous run's `last_error_code` standing on a row
        # whose last attempt reached the aggregator and came back clean, and
        # every surface that reads that column then reported a failure that had
        # already stopped happening.
        with writer_connection(config) as conn:
            record_domain_incomplete(
                conn, connection_id=connection_id, domain=INVESTMENTS_DOMAIN, at=now_utc()
            )
        return
    with writer_connection(config) as conn:
        record_domain_success(
            conn, connection_id=connection_id, domain=INVESTMENTS_DOMAIN, at=now_utc()
        )


def _pull_investment_transactions(
    config: Config,
    client: PlaidClient,
    access_token: str,
    *,
    connection_id: int,
    outcome: ConnectionOutcome,
) -> WindowOutcome:
    """The configured window of investment transactions, paged to exhaustion.

    🔴 **Paged by offset against a stated total, with no cursor** *(§26)*. So
    unlike the transactions loop, there is nothing to store between pages and
    nothing to resume from: a run that stops early leaves no partial progress
    behind, and the next run re-reads the window from its start. That converges
    because every row is upserted on the aggregator's own id, and it is the
    reason the page ceiling here costs a re-read rather than a gap.

    AC-3.3: the window asked for is `config.history_days`, the one configured
    window. What came back is recorded by
    `record_investment_transaction_window`, which also owns the removal
    reconciliation -- and refuses both if these pages did not exhaust the window.
    """
    end = now_utc().date()
    start = end - timedelta(days=config.history_days)
    page_response_ids: list[int] = []
    rows_seen = 0
    stated_total: int | None = None
    # 🔴 The instant the removals are stamped with, taken from the ARCHIVE rather
    # than the clock. A `removed_at` of `now_utc()` could never be reproduced by
    # a replay of the same pages, so AC-5.2's "rebuildable from raw responses
    # alone" would be false for every soft delete. The last page's `received_at`
    # is a property of the window, so a rebuild that replays it concludes the
    # same removal at the same instant.
    concluded_at = now_utc()

    while len(page_response_ids) < MAX_PAGES_PER_RUN:
        fetched = client.investments_transactions_get(
            access_token,
            start_date=start,
            end_date=end,
            offset=rows_seen,
            connection_id=connection_id,
        )
        response = _persist(config, fetched, connection_id)
        page_response_ids.append(response.raw_response_id)
        concluded_at = response.received_at
        # 🔴 Read back off the ARCHIVED page rather than off the reply in hand,
        # through the reader a rebuild uses on the same row. The two paths then
        # cannot disagree about how many rows a page carried or how many the
        # window claims to hold -- and a disagreement would surface only as a
        # rebuild that concluded a removal this run never did.
        page = read_investment_transaction_page(response)
        rows_seen = page.rows_through_this_page
        stated_total = page.stated_total
        # 🔴 Three exits, and the first two are measured *(§26)*: an offset at
        # or past the total answers with an empty list and no error, and the
        # total does not move between pages. The empty-page exit is what stops a
        # total that is wrong in the high direction from looping to the ceiling.
        #
        # 🔴 The unstated total is the third and is NOT the exhaustion predicate
        # saying no. A window with no stated size has nothing to page against:
        # there is no offset at which this loop could learn it had finished, so
        # asking again 499 times spends the page ceiling in aggregator calls to
        # reach the conclusion the first page already supports. `window_is_exhausted`
        # answers False here for its own good reason -- nothing may be concluded
        # about what is missing -- and folding this case into it cost exactly
        # that: a single no-total page fetched to the ceiling, silently.
        if page.rows == 0 or stated_total is None or window_is_exhausted(rows_seen, stated_total):
            break
    else:
        logger.info(
            "connection %d stopped at the %d-page ceiling of its investment-transaction "
            "window with more to fetch. There is no cursor on this endpoint (§26), so the "
            "next run re-reads the window from its start rather than resuming here",
            connection_id,
            MAX_PAGES_PER_RUN,
        )

    with writer_connection(config) as conn, transaction(conn):
        window = record_investment_transaction_window(
            conn,
            connection_id=connection_id,
            window_start=calendar_date(start),
            window_end=calendar_date(end),
            page_response_ids=page_response_ids,
            rows_seen=rows_seen,
            stated_total=stated_total,
            at=concluded_at,
        )
        if window.history_start_date is not None:
            # 🔴 Recorded by the SYNC, in the same transaction as the removals
            # the range was measured after -- a store reporting one window's
            # range beside another's rows would claim a measurement its own rows
            # contradict. It is the sync's to record because `store rebuild`
            # re-runs the reconciliation above from the archived pages, and a
            # replay that stamped a domain's progress would restate a
            # measurement the run that took it has since moved past. The
            # transactions domain records its own range the same way, a few
            # functions up.
            record_domain_history_start(
                conn,
                connection_id=connection_id,
                domain=INVESTMENTS_DOMAIN,
                start=window.history_start_date,
                at=concluded_at,
            )
    outcome.investment_transactions_removed = window.removed
    if not window.exhausted:
        # 🔴 Taken from the window's OWN verdict rather than from the page
        # ceiling, because the ceiling is only one way to see a prefix. A page
        # that comes back empty while the stated total says there is more exits
        # the loop without ever reaching the `else` above -- and reporting that
        # run as finished would tell the operator there is nothing left to fetch
        # while the range stayed unmeasured and nothing was reconciled. One
        # source for "did we see it whole", and it is the one that decided.
        outcome.investments_window_short = True
    return window


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
            positions = ", positions recorded" if outcome.investments_pulled else ""
            # A soft delete leaves no trace in a page count -- the rows simply
            # stop appearing in every total. Named here so a run that retired
            # history says so at the moment the operator could still ask why.
            retired = (
                f", {outcome.investment_transactions_removed} investment "
                f"transaction(s) no longer reported"
                if outcome.investment_transactions_removed
                else ""
            )
            print(
                f"  {outcome.connection_id}  {outcome.institution_name}: "
                f"{outcome.pages} {pages} applied{positions}{retired}{more}"
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
        if outcome.investments_window_short:
            # 🔴 Its own line rather than the pager's, which says "stopped at the
            # page ceiling" -- a ceiling this window may never have reached. The
            # window is short whenever it was not seen WHOLE, and the ordinary
            # cause is a reply that stated no total at all.
            print(
                "       investments: the transaction window did not come back whole, so\n"
                "       nothing was retired from it and its range is unmeasured. Run again"
            )
        if outcome.investments_error_reason is not None and not outcome.degraded:
            # 🔴 Printed under a connection the run is otherwise reporting as
            # healthy, which is the whole shape of this failure: the login works,
            # the transactions landed, and one domain stopped. It names the
            # domain rather than the connection so the operator does not reach
            # for `connections reauth`, which repairs a credential and cannot
            # touch a product the Item never initialized.
            #
            # 🔴 `not degraded` is what keeps that sentence TRUE. Both can happen
            # in one run -- the investments call fails with a product error and
            # the page loop afterwards hits an expired login -- and without this
            # guard "no re-authentication is needed" printed two lines under the
            # instruction to re-authenticate. The summary counter below already
            # carries the same guard for the same reason.
            print(
                f"       🔴 investments: {outcome.investments_error_reason}. The "
                f"connection's other data is unaffected and no re-authentication "
                f"is needed"
            )
    degraded = sum(1 for o in run.outcomes if o.degraded)
    if degraded:
        print(f"\n{degraded} of {len(run.outcomes)} connections could not be synced")
    failed_domains = sum(
        1 for o in run.outcomes if not o.degraded and o.investments_error_code is not None
    )
    if failed_domains:
        # 🔴 Counted apart from the degraded connections rather than added to
        # them, and `not o.degraded` is what makes the sentence true: these
        # connections DID sync. A connection that lost its login AND its
        # investments would otherwise be counted in both lines, which read
        # together say it could not be synced and that it synced.
        print(
            f"\n{failed_domains} of {len(run.outcomes)} connections synced with their "
            f"investments domain failing"
        )
    owing = sum(1 for o in run.outcomes if not o.degraded and o.unfinished)
    if owing:
        # The summary an operator scanning ten institutions reads. Degraded
        # connections are counted separately above and never here: those need
        # someone to look at them, not another run. The exit code is deliberately
        # not quoted -- it is `1` rather than `75` whenever both counts are
        # non-zero, and a line that named the wrong number would be worse than
        # one that names none.
        print(f"\n{owing} of {len(run.outcomes)} connections still owe history; run again")
