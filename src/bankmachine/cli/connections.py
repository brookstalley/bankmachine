"""`bankmachine connections` -- what is enrolled, and how to stop paying for it.

AC-1.5 requires the cap refusal to list current connections *so one can be
removed*, and AC-1.6 requires a connection to be retired without deleting its
history. Those are one feature: a refusal that names a command which does not
exist is not actionable, it is a dead end with better manners.

🔴 **Retirement is two-sided, and only one side is local.** Setting `retired_at`
frees a slot in this product's own cap. It does nothing at the aggregator, where
the Item keeps existing, keeps counting against the plan, and keeps billing. So
retiring calls `/item/remove` as well -- and the local row is written first,
because the two failure directions are not symmetric: a removed Item with a live
local row is a connection that stops syncing and says why, while a retired local
row with a live Item is a charge nobody can see.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.engine import Connection as SAConnection

from bankmachine.cli.exit_codes import EXIT_OK, EXIT_UNHEALTHY
from bankmachine.cli.hosted_link import (
    LINK_COUNTRIES,
    await_hosted_session,
    positive_seconds,
    print_invitation,
)
from bankmachine.cli.parser import AnyParser
from bankmachine.config import Config, ConfigError, require_chosen_environment
from bankmachine.connector import (
    ConnectorError,
    FetchedResponse,
    MalformedResponseError,
    ReauthRequiredError,
    parse_response_body,
)
from bankmachine.connector.plaid.client import (
    DEFAULT_HOSTED_URL_LIFETIME_SECONDS,
    PlaidClient,
)
from bankmachine.connector.plaid.errors import AggregatorErrorDetail, classify
from bankmachine.derivers import ALL_DERIVERS
from bankmachine.logging_setup import get_logger
from bankmachine.secrets import (
    AccessTokenMissingError,
    SecretsError,
    delete_access_token,
    get_access_token,
    get_plaid_secret,
)
from bankmachine.store.connection import DatastoreMissingError, inspect, remedy_for
from bankmachine.store.derivation import apply_response
from bankmachine.store.engine import reader_connection, transaction, writer_connection
from bankmachine.store.schema import accounts, connections, institutions
from bankmachine.store.types import UtcInstant, calendar_date, now_utc


def _is_expired_login(code: str | None) -> bool:
    """Whether a complaint on an item body is the one update mode exists to clear.

    🔴 **Asked of `errors.py`, never of a string literal here.** That module owns
    the aggregator's vocabulary -- it is where a second expired-login code would
    be added, and its own docstring says the code layer is "precise and
    incomplete" precisely because the aggregator adds codes. A copy of one code
    in this file would be invisible to whoever extends that map, and the repair
    would then read a still-expired item as "repaired, but complaining about
    something else". `sync_run.py` asks the same question by catching
    `ReauthRequiredError`; this is the same question against a body rather than
    against a raised refusal, because `/item/get` reports an unwell item in its
    body with a 200 (`api-notes-plaid.md` §13) and nothing is raised to catch.

    A body with no complaint is not an expired login: `None` is "the aggregator
    is not complaining", which is the state the repair is trying to REACH.
    """
    if code is None:
        return False
    return classify(None, AggregatorErrorDetail(error_code=code)) is ReauthRequiredError


logger = get_logger("cli.connections")


@dataclass(frozen=True, slots=True)
class ConnectionRow:
    """One connection as every surface reports it."""

    connection_id: int
    institution_name: str
    enrolled_at: UtcInstant
    status: str
    retired_at: UtcInstant | None
    credential_ref: str
    source_connection_id: str
    """The aggregator's own id for this connection.

    Carried because it is the only handle an operator has to an Item that is
    still billing after a failed removal: the log redacts it (a production id is
    an opaque 37-character run) and a retired connection does not appear in
    `connections list`.
    """
    last_error_code: str | None
    """What the last sync ATTEMPT failed with, if it failed.

    Read by `reauth`, which prints it so an operator can see whether the problem
    it is about to repair is the one update mode repairs. Deliberately not
    `source_error_code`, the Item's own standing: a sync can fail against an Item
    that is not complaining, and the two are kept apart everywhere else.
    """


def add_arguments(subparsers: argparse._SubParsersAction[AnyParser]) -> None:
    parser = subparsers.add_parser(
        "connections",
        help="list, repair and retire enrolled connections",
        description=(
            "Shows what is enrolled, repairs a connection whose login expired, and lets "
            "one be retired. Retiring keeps every transaction, balance and account the "
            "connection produced -- it stops the connection syncing and releases its "
            "slot, it does not delete history."
        ),
    )
    commands = parser.add_subparsers(dest="connections_command", required=True)

    listing = commands.add_parser("list", help="show enrolled connections")
    listing.add_argument(
        "--all",
        action="store_true",
        help="include retired connections, which are hidden by default",
    )
    listing.set_defaults(handler=cmd_list)

    retire = commands.add_parser(
        "retire",
        help="stop syncing a connection and release its slot, keeping its history",
        description=(
            "Sets the connection retired locally and removes it at the aggregator, so it "
            "stops counting against the plan cap and stops billing. Every row it produced "
            "is kept."
        ),
    )
    retire.add_argument("connection_id", type=int, metavar="ID")
    retire.set_defaults(handler=cmd_retire)

    reauth = commands.add_parser(
        "reauth",
        help="repair a connection whose login expired, keeping its history and cursor",
        description=(
            "Prints an update-mode enrollment URL for a connection the aggregator has "
            "stopped accepting. Completing it repairs the existing connection in place: "
            "the same item, the same accounts, the same transactions, and a sync that "
            "carries on from where it stopped. This is the remedy for an expired login -- "
            "re-running `bankmachine enroll` instead creates a second connection behind "
            "the same institution and counts all of its history twice."
        ),
    )
    reauth.add_argument("connection_id", type=int, metavar="ID")
    reauth.add_argument(
        "--timeout",
        type=positive_seconds,
        default=DEFAULT_HOSTED_URL_LIFETIME_SECONDS,
        metavar="SECONDS",
        help=(
            "how long to wait for the hosted session to be completed, and how long the "
            f"URL stays usable -- they are one number (default: "
            f"{DEFAULT_HOSTED_URL_LIFETIME_SECONDS})"
        ),
    )
    reauth.set_defaults(handler=cmd_reauth)


def _require_datastore(config: Config) -> None:
    status = inspect(config)
    if not status.healthy:
        raise DatastoreMissingError(
            f"cannot list connections: the datastore at {status.path} is not ready "
            f"({status.problem or 'unknown problem'}). {remedy_for(status.reason)}"
        )


def cmd_list(config: Config, args: argparse.Namespace) -> int:
    _require_datastore(config)
    with reader_connection(config) as conn:
        rows = connection_rows(conn, include_retired=args.all)

    if not rows:
        print("no connections enrolled. `bankmachine enroll` links one")
        return EXIT_OK

    live = [row for row in rows if row.retired_at is None]
    print(f"{len(live)} of {config.connection_cap} connection slots in use")
    print()
    for row in rows:
        state = "retired" if row.retired_at is not None else row.status
        print(
            f"  {row.connection_id:>4}  {row.institution_name}  ({state}, "
            f"enrolled {row.enrolled_at.isoformat()[:10]})"
        )
    return EXIT_OK


def cmd_retire(config: Config, args: argparse.Namespace) -> int:
    # 🔴 Guarded at the first statement, for `cmd_enroll`'s reason rather than by
    # analogy to it. The ordinary path marks the row retired under the writer lock
    # BEFORE it calls the aggregator, so the refusal lands there with nothing spent.
    # The already-retired retry path runs only READS before `item_remove`, and reads
    # are exempt -- so on a defaulted environment the Item is really removed, and
    # then `delete_access_token` is refused and swallowed. The credential survives,
    # `_credential_survives` keeps reading "removal never confirmed", and every
    # later retry hits ITEM_NOT_FOUND and reports "may still be billing" about an
    # Item that is gone. Nothing clears that state, which is why the guard belongs
    # ahead of the remote effect and not beside the write it protects.
    require_chosen_environment(config)
    _require_datastore(config)
    connection_id = int(args.connection_id)

    with reader_connection(config) as conn:
        row = _one_connection(conn, connection_id)
    if row is None:
        print(
            f"bankmachine: no connection {connection_id}. `bankmachine connections list` shows "
            f"what is enrolled",
            file=sys.stderr,
        )
        return EXIT_UNHEALTHY
    if row.retired_at is not None:
        # 🔴 Already retired locally does NOT mean finished. `release_at_aggregator`
        # deletes the credential only after the far end confirms, so a credential
        # that is still there is the persisted record of a divergence: this side
        # decided the Item should not exist and never got told it does not.
        #
        # Without this the retry `cmd_retire` promises is unreachable -- a re-run
        # would print "already retired" and exit 0 while the Item kept billing,
        # which is worse than not offering the retry at all.
        if not _credential_survives(config, row.credential_ref):
            print(f"connection {connection_id} ({row.institution_name}) was already retired")
            return EXIT_OK
        print(
            f"connection {connection_id} ({row.institution_name}) is retired here, but its "
            f"removal at the aggregator was never confirmed. Retrying"
        )
        if release_at_aggregator(config, row.credential_ref, connection_id=connection_id):
            print("  removed at the aggregator; it is no longer billing")
            return EXIT_OK
        print(
            f"  still could not reach the aggregator. Item {row.source_connection_id} may "
            f"still be billing; run this again when the network is back",
            file=sys.stderr,
        )
        return EXIT_UNHEALTHY

    credential_ref = row.credential_ref
    now = now_utc()
    with writer_connection(config) as conn, transaction(conn):
        _mark_retired(conn, connection_id=connection_id, now=now)

    # 🔴 After the local write, deliberately. A removed Item with a live local row
    # is a connection that stops syncing and says why; a retired local row with a
    # live Item is a charge nobody can see. The recoverable direction is the one
    # that leaves evidence here.
    removed = release_at_aggregator(config, credential_ref, connection_id=connection_id)

    print(f"retired connection {connection_id} ({row.institution_name}); its history is kept")
    if not removed:
        # 🔴 stderr, and it names the item. The convention is that failures go to
        # stderr, and the log cannot carry the id: a production item id is a ~37
        # character opaque run, which the formatter blanks. `connections list`
        # cannot show it either -- the connection is retired. So the one place the
        # operator can ever see which item is still billing is this line.
        print(
            f"  the aggregator was not reached, so item {row.source_connection_id} may still "
            f"be billing there. Run `bankmachine connections retire {connection_id}` again to "
            f"retry the removal",
            file=sys.stderr,
        )
        return EXIT_UNHEALTHY
    return EXIT_OK


def cmd_reauth(config: Config, args: argparse.Namespace) -> int:
    """AC-4.3: repair a broken connection without losing its history or cursor.

    🔴 **What this command does not write is the requirement.** No cursor is
    deleted, no `granted_history_days` is cleared, no `enrolled_at` is moved and
    no account row is touched, because update mode repairs the item the
    connection already names rather than minting a second one. `enroll`'s
    convergence clears all of that on purpose -- an item-scoped cursor cannot be
    replayed against its successor -- and the whole point of this path is that
    there is no successor.
    """
    _require_datastore(config)
    connection_id = int(args.connection_id)

    with reader_connection(config) as conn:
        row = _one_connection(conn, connection_id)
    if row is None:
        print(
            f"bankmachine: no connection {connection_id}. `bankmachine connections list` shows "
            f"what is enrolled",
            file=sys.stderr,
        )
        return EXIT_UNHEALTHY
    if row.retired_at is not None:
        # A retired connection has no item to repair -- `retire` removed it at the
        # aggregator -- so update mode has nothing to open. Naming `enroll` here
        # is correct precisely because there is no live connection to duplicate.
        print(
            f"bankmachine: connection {connection_id} ({row.institution_name}) is retired, so "
            f"there is no login to repair. `bankmachine enroll` links it again",
            file=sys.stderr,
        )
        return EXIT_UNHEALTHY

    try:
        access_token = get_access_token(config, row.credential_ref)
    except AccessTokenMissingError:
        # 🔴 The one unrecoverable shape: without the token there is no handle to
        # the item, so neither repair nor removal can reach it. Saying so beats
        # offering a retry that cannot work.
        print(
            f"bankmachine: connection {connection_id} ({row.institution_name}) has no stored "
            f"credential, so its item {row.source_connection_id} cannot be reached from here. "
            f"Remove it from the aggregator's dashboard and run `bankmachine enroll` to link "
            f"the institution again",
            file=sys.stderr,
        )
        return EXIT_UNHEALTHY

    # Before the network call, so a client that cannot reach the aggregator still
    # leaves the operator knowing which connection they were repairing and what
    # was wrong with it.
    print(
        f"connection {connection_id} ({row.institution_name}) is {row.status}"
        + (f" ({row.last_error_code})" if row.last_error_code else "")
    )

    secret = get_plaid_secret(config)
    with PlaidClient(config, secret) as client:
        # 🔴 **Establish the condition being repaired before printing a URL.**
        # Completion is "the item is no longer reporting an expired login", and
        # a connection that was never reporting one satisfies that on the first
        # poll -- before the operator has opened the URL at all. Without this
        # read the command prints `repaired connection N` and writes
        # `status="active"` for a session nobody completed, and tells an operator
        # whose item is LOCKED that the aggregator "accepted the new login".
        # `operational-spec.md`'s recovery row lists this command for a broken
        # connection generally, so both are reachable by following the product's
        # own instructions.
        pre_flight = client.item_get(access_token, connection_id=connection_id)
        before, present_item_id = _item_standing(pre_flight)
        _archive_item(config, pre_flight, row, item_id=present_item_id)
        if present_item_id != row.source_connection_id:
            # The stored credential no longer opens the item this row names. Not
            # the post-session guard below -- that one catches update mode minting
            # a successor -- but the same duplication arriving before any session
            # exists, and there is nothing here to repair in place.
            print(
                f"bankmachine: connection {connection_id} ({row.institution_name}) has a stored "
                f"credential that opens item {present_item_id}, but the connection was enrolled "
                f"with {row.source_connection_id}. Nothing was changed",
                file=sys.stderr,
            )
            return EXIT_UNHEALTHY
        if not _is_expired_login(before):
            # Refused rather than repaired: this command renews a login, and an
            # item that is not asking for one cannot be made better by renewing
            # it. Naming what the aggregator DOES say is the whole value here --
            # `ITEM_LOCKED` sends the operator to their bank, and nothing at all
            # means the row's degraded flag is stale and a sync will clear it.
            said = f"reports {before}" if before is not None else "reports no problem"
            print(
                f"bankmachine: connection {connection_id} ({row.institution_name}) is not "
                f"reporting an expired login, so there is nothing for update mode to renew. "
                f"The aggregator {said}.",
                file=sys.stderr,
            )
            if before is None:
                print(
                    "       If `connections list` still shows it degraded, that records the "
                    "last sync ATTEMPT; run `bankmachine sync run` to clear it",
                    file=sys.stderr,
                )
            return EXIT_UNHEALTHY
        print(f"  the aggregator reports {before}; update mode renews exactly that")

        session = client.link_token_create_update(
            access_token=access_token,
            client_user_id=f"bankmachine-{config.environment}",
            country_codes=list(LINK_COUNTRIES),
            # One number, not two that happen to agree -- the URL must die when
            # this side stops watching, or the operator completes a session
            # nothing is waiting for. `enroll` records the same reasoning.
            hosted_url_lifetime_seconds=args.timeout,
        )
        print_invitation(
            session.hosted_link_url,
            what_it_does=(
                f"repair connection {connection_id} -- {row.institution_name} "
                f"({config.environment})"
            ),
            timeout_seconds=args.timeout,
        )
        repaired = await_hosted_session(
            lambda: _poll_for_repair(config, client, row, access_token=access_token),
            timeout_seconds=args.timeout,
        )

    if repaired is None:
        # Nothing is written. The connection is exactly as degraded as it was, which
        # is the honest state: the operator did not finish, so nothing was repaired.
        logger.warning("repair of connection %d abandoned after %ss", connection_id, args.timeout)
        print(
            f"bankmachine: the hosted session was not completed within {args.timeout}s. "
            f"Connection {connection_id} is still degraded; run "
            f"`bankmachine connections reauth {connection_id}` again to start over",
            file=sys.stderr,
        )
        return EXIT_UNHEALTHY

    standing, item_id = repaired
    if item_id != row.source_connection_id:
        # 🔴 The guard the whole command rests on. Update mode is supposed to
        # repair the item in place; an item id that moved means a SECOND item now
        # stands behind this connection, and letting the next sync run against it
        # is exactly the duplication this command exists to prevent -- every
        # account re-issued, every transaction counted twice, every total doubled
        # with no warning naming it. Refusing leaves the row degraded, which is
        # recoverable, rather than silently correct-looking, which is not.
        #
        # The body that discovered this was archived with no connection id, so
        # nothing of the FOREIGN item's reached this row -- see `_archive_item`.
        # Earlier polls did derive this row's own item standing onto it, which is
        # what makes "Nothing was changed" below a claim about the repair rather
        # than about every column.
        logger.error(
            "connection %d came back from update mode behind a different item; "
            "the connection was left degraded",
            connection_id,
        )
        print(
            f"bankmachine: connection {connection_id} ({row.institution_name}) came back from "
            f"the repair behind a DIFFERENT item at the aggregator, so its accounts and "
            f"transactions would be re-issued and counted twice. Nothing was changed and the "
            f"connection is still degraded. The item it now answers as is {item_id}; the one "
            f"it was enrolled with is {row.source_connection_id}",
            file=sys.stderr,
        )
        return EXIT_UNHEALTHY

    if standing is not None:
        # Repaired, but complaining about something else. Not a failure of this
        # command -- the login really was renewed -- and not a success either, so
        # the row keeps a complaint that is now accurate rather than stale.
        print(
            f"bankmachine: connection {connection_id} ({row.institution_name}) accepted the new "
            f"login, but the aggregator now reports {standing}, which update mode does not "
            f"repair. The connection is left degraded",
            file=sys.stderr,
        )
        return EXIT_UNHEALTHY

    now = now_utc()
    with writer_connection(config) as conn, transaction(conn):
        conn.execute(
            update(connections)
            .where(connections.c.connection_id == connection_id)
            .values(status="active", last_error_code=None, last_error_at=None, updated_at=now)
        )
    logger.info("connection %d was repaired through update mode", connection_id)
    print()
    print(f"repaired connection {connection_id}: {row.institution_name}")
    print("  its item, accounts, transactions and sync cursor are unchanged")
    print("  `bankmachine sync run` continues from where it stopped")
    return EXIT_OK


def _poll_for_repair(
    config: Config,
    client: PlaidClient,
    row: ConnectionRow,
    *,
    access_token: str,
) -> tuple[str | None, str] | None:
    """One look at the item: `None` while the login is still expired, else its standing.

    Answers `(complaint, item_id)` once the item has stopped reporting an expired
    login -- `complaint` is `None` when it reports nothing at all, and a code when
    it has swapped one problem for another. The item id rides along because the
    caller has to compare it against the row before trusting any of this.

    🔴 **Done is "no longer `ITEM_LOGIN_REQUIRED`", not "no error at all".** An
    absent `error` key and a null one are not the same observation -- the
    derivation layer is careful about exactly that -- so waiting for a null would
    hang against a body that simply omits the field, and waiting for *any* error
    to clear would hang on a complaint update mode was never going to fix.

    Every poll is archived, per AC-5.1. A repair is a state change an operator
    may later have to reconstruct, and the archive is what `store rebuild` reads;
    the bodies are small and the write is brief, so the honest reading of "every
    response" costs little enough to take literally.
    """
    fetched = client.item_get(access_token, connection_id=row.connection_id)
    complaint, item_id = _item_standing(fetched)
    _archive_item(config, fetched, row, item_id=item_id)
    return None if _is_expired_login(complaint) else (complaint, item_id)


def _archive_item(
    config: Config, fetched: FetchedResponse, row: ConnectionRow, *, item_id: str
) -> None:
    """Archive one `/item/get` body; derive it onto this row only if it IS this row's item.

    🔴 **The identity question is settled before the derivation runs, not after.**
    `derive_item_get` writes `consent_expires_at` and `source_error_code` onto
    whichever connection the response was fetched for, and `query.py` turns
    `consent_expires_at` into the consent caveats the MCP surface attaches to
    every answer about this connection. A foreign item's consent date landing
    there would make the datastore warn -- or stop warning -- about a connection
    on the strength of a body that describes a different one, on the very path
    that then prints "Nothing was changed."

    Archived either way, with no connection id when the body is foreign. FR-5
    wants every response kept, and this is the one that evidences why the repair
    refused; `enroll` archives its pre-connection responses the same way.
    """
    belongs_here = item_id == row.source_connection_id
    with writer_connection(config) as conn:
        apply_response(
            conn,
            connection_id=row.connection_id if belongs_here else None,
            endpoint=fetched.endpoint.path,
            body=fetched.body,
            received_at=fetched.received_at,
            derivers=ALL_DERIVERS,
            request_context=fetched.request_context,
        )


def _item_standing(fetched: FetchedResponse) -> tuple[str | None, str]:
    """`(error code or None, item id)` off one `/item/get` body.

    A separate read from the deriver's, over the same two fields, because the
    questions differ: the deriver decides what to *record*, and this decides
    whether the operator is finished. The item id is required rather than
    optional -- a body that cannot say which item it describes cannot support the
    comparison the repair refuses to skip.
    """
    payload = parse_response_body(fetched.body, what="an item", endpoint=fetched.endpoint)
    item = payload.get("item") if isinstance(payload, dict) else None
    if not isinstance(item, dict):
        raise MalformedResponseError(
            f"{fetched.endpoint} answered without an item object, so the repair cannot "
            f"tell which connection it is looking at",
            endpoint=fetched.endpoint,
            failed_at=fetched.received_at,
        )
    item_id = item.get("item_id")
    if not isinstance(item_id, str):
        raise MalformedResponseError(
            f"{fetched.endpoint} answered without an item_id, so the repair cannot confirm "
            f"it is still the item this connection was enrolled with",
            endpoint=fetched.endpoint,
            failed_at=fetched.received_at,
        )
    error = item.get("error")
    code = error.get("error_code") if isinstance(error, dict) else None
    return (code if isinstance(code, str) else None), item_id


def _credential_survives(config: Config, credential_ref: str) -> bool:
    """Whether this connection's access token is still in the keychain.

    The marker for "we decided the far-end Item should not exist and never
    confirmed it": the credential is deleted only once removal succeeds, so its
    presence outlives a failed removal and nothing else has to be written down.
    A keychain that cannot be read is treated as "still there", because retrying
    a removal that already happened costs an ITEM_NOT_FOUND and skipping one that
    did not costs a bill.
    """
    try:
        get_access_token(config, credential_ref)
    except AccessTokenMissingError:
        return False
    except SecretsError:
        return True
    return True


def release_at_aggregator(
    config: Config, credential_ref: str, *, connection_id: int | None = None
) -> bool:
    """Remove the Item behind a credential, and forget the credential. Never raises.

    Returns whether the far end confirmed. 🔴 Failure here is reported, never
    fatal: the caller has already committed a local decision, and turning an
    unreachable aggregator into an exception would roll back a retirement the
    operator asked for -- or, on the enrollment path, discard a connection that
    already works. What must not happen is silence, so the outcome is both logged
    and returned.

    The credential is deleted only after the far end confirms. Deleting first
    would leave an Item nothing in this system can ever remove: the access token
    is the only handle to it.
    """
    try:
        access_token = get_access_token(config, credential_ref)
    except AccessTokenMissingError:
        # Nothing to remove and nothing to delete. Not a failure of this step --
        # the credential is already gone, which is the state this aims at.
        logger.info("no stored credential for %s; nothing to remove", credential_ref)
        return True
    except (SecretsError, ConfigError) as exc:
        # 🔴 `AccessTokenMissingError` alone was not enough: `get_access_token`
        # also raises plain `SecretsError` on an unreachable keychain or an empty
        # value, and this function's whole contract is that it never raises. The
        # escape had a consequence two modules away -- it pre-empted a pending
        # `ConnectionCapReachedError`, so a cap refusal exited 2 instead of 1,
        # which is the exact collapse the exit-code contract forbids.
        #
        # `ConfigError` joins it for the same reason and by the same argument:
        # the keychain mutators refuse outright on an environment nobody chose,
        # and that refusal is a `ConfigError`, not a `SecretsError`.
        logger.warning(
            "the credential for %s could not be read, so its item was not removed: %s",
            credential_ref,
            exc,
        )
        return False

    try:
        secret = get_plaid_secret(config)
        with PlaidClient(config, secret) as client:
            client.item_remove(access_token, connection_id=connection_id)
    except (ConnectorError, SecretsError, ConfigError) as exc:
        # `SecretsError` as well as `ConnectorError`: the aggregator secret is read
        # here too, and a keychain that cannot be reached must not become an
        # exception either -- the docstring's promise is what the callers rely on.
        # `ConfigError` covers the environment refusal, which is neither.
        logger.warning(
            "could not remove the item behind %s at the aggregator: %s. "
            "It may still be counting against the plan cap",
            credential_ref,
            exc,
        )
        return False

    try:
        delete_access_token(config, credential_ref)
    except (SecretsError, ConfigError) as exc:
        # The item IS removed at this point, so this is not a failure of the
        # operation -- it is a stale credential for an item that no longer
        # exists. Reported rather than raised, and reported as what it is.
        # 🔴 This is the arm the environment refusal actually reached: the remote
        # removal has already succeeded, so raising here would report a failure
        # for work that is done.
        logger.warning(
            "removed the item behind %s, but its keychain entry could not be "
            "cleared: %s. The entry is now stale rather than sensitive",
            credential_ref,
            exc,
        )
    logger.info("removed the item behind %s at the aggregator", credential_ref)
    return True


def connection_rows(conn: SAConnection, *, include_retired: bool) -> list[ConnectionRow]:
    """Every connection, oldest first. The one listing this product has.

    Read by `connections list` and by the cap refusal in `enroll`, which is why
    it lives here rather than beside either: AC-1.5 requires the refusal to list
    what is enrolled, so the refusal and the listing are the same question asked
    from two places, and two implementations of it would eventually disagree.
    """
    statement = (
        select(
            connections.c.connection_id,
            institutions.c.name,
            connections.c.enrolled_at,
            connections.c.status,
            connections.c.retired_at,
            connections.c.credential_ref,
            connections.c.source_connection_id,
            connections.c.last_error_code,
        )
        .select_from(connections.join(institutions))
        .order_by(connections.c.enrolled_at)
    )
    if not include_retired:
        statement = statement.where(connections.c.retired_at.is_(None))
    return [
        ConnectionRow(
            connection_id=int(row[0]),
            institution_name=str(row[1]),
            enrolled_at=row[2],
            status=str(row[3]),
            retired_at=row[4],
            credential_ref=str(row[5]),
            source_connection_id=str(row[6]),
            last_error_code=row[7],
        )
        for row in conn.execute(statement).all()
    ]


def _one_connection(conn: SAConnection, connection_id: int) -> ConnectionRow | None:
    return next(
        (
            row
            for row in connection_rows(conn, include_retired=True)
            if row.connection_id == connection_id
        ),
        None,
    )


def explain_cap(live: list[ConnectionRow], cap: int) -> str:
    """AC-1.5 asks for the limit explained AND the connections listed.

    A bare "cap reached" leaves the operator with no way to act on it: the point
    of the requirement is that they can see which connection to retire, and the
    command named here exists.
    """
    lines = [
        f"the configured connection cap is {cap} and {len(live)} are already live.",
        "Retire one to make room:",
        "",
    ]
    lines.extend(
        f"    {row.connection_id:>4}  {row.institution_name}  ({row.status}, "
        f"enrolled {row.enrolled_at.isoformat()[:10]})"
        for row in live
    )
    lines.extend(["", "    bankmachine connections retire <id>"])
    return "\n".join(lines)


def _mark_retired(conn: SAConnection, *, connection_id: int, now: UtcInstant) -> None:
    """Retire without deleting. AC-1.6 and AC-6.5 -- the history outlives the connection.

    🔴 **The accounts are closed here too, because nothing will ever observe them
    again.** Every absence signal this product has is measured against a later
    roster observation, and a retired connection is never rostered: the accounts'
    last observation still matches the connection's, so they keep reading `active`
    and their last captured balance keeps being summed as a present-day one. The
    operator retiring the connection is the declaration that it is over, and
    AC-12.6 makes the stored declaration outrank the derived signal -- so it is
    written down rather than inferred by every reader separately.

    Only the accounts still `active` are touched. One already closed was closed on
    its own date, and that date is a fact about the account rather than about the
    day somebody tidied up the connection.
    """
    conn.execute(
        update(connections)
        .where(connections.c.connection_id == connection_id)
        .values(status="retired", retired_at=now, updated_at=now)
    )
    conn.execute(
        update(accounts)
        .where(
            accounts.c.connection_id == connection_id,
            accounts.c.lifecycle_status == "active",
        )
        # 🔴 `updated_at` is NOT written. It is derivation-owned -- `_upsert_account`
        # stamps it from the archived response and `content_digest` covers it --
        # so an operator write here would be reverted by the next sync and, worse,
        # would leave `store rebuild` unable to reproduce the table from the
        # archive, and it refuses the whole rebuild rather than guess.
        .values(
            lifecycle_status="inactive",
            closed_date=calendar_date(now.date()),
        )
    )
