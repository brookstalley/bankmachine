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
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.engine import Connection as SAConnection

from bankmachine.cli.exit_codes import EXIT_OK, EXIT_UNHEALTHY
from bankmachine.config import Config
from bankmachine.connector import ConnectorError
from bankmachine.connector.plaid.client import PlaidClient
from bankmachine.logging_setup import get_logger
from bankmachine.secrets import (
    AccessTokenMissingError,
    SecretsError,
    delete_access_token,
    get_access_token,
    get_plaid_secret,
)
from bankmachine.store.connection import DatastoreMissingError, inspect
from bankmachine.store.engine import reader_connection, transaction, writer_connection
from bankmachine.store.schema import connections, institutions
from bankmachine.store.types import UtcInstant, now_utc

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


def add_arguments(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "connections",
        help="list and retire enrolled connections",
        description=(
            "Shows what is enrolled and lets one be retired. Retiring keeps every "
            "transaction, balance and account the connection produced -- it stops the "
            "connection syncing and releases its slot, it does not delete history."
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


def _require_datastore(config: Config) -> None:
    status = inspect(config)
    if not status.healthy:
        raise DatastoreMissingError(
            f"datastore at {status.path} is not ready ({status.problem or 'unknown problem'}); "
            f"run `bankmachine store init` first"
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
    _require_datastore(config)
    connection_id = int(args.connection_id)

    with reader_connection(config) as conn:
        row = _one_connection(conn, connection_id)
    if row is None:
        print(f"bankmachine: no connection {connection_id}. `bankmachine connections list` shows")
        return EXIT_UNHEALTHY
    if row.retired_at is not None:
        # Not an error: the operator asked for a state the system is already in,
        # and reporting failure would invite them to try something more drastic.
        print(f"connection {connection_id} ({row.institution_name}) was already retired")
        return EXIT_OK

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
        print(
            "  the aggregator was not reached, so the connection may still be billing there. "
            "Re-run this command to retry the removal",
        )
        return EXIT_UNHEALTHY
    return EXIT_OK


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

    try:
        secret = get_plaid_secret(config)
        with PlaidClient(config, secret) as client:
            client.item_remove(access_token, connection_id=connection_id)
    except (ConnectorError, SecretsError) as exc:
        # `SecretsError` as well as `ConnectorError`: the aggregator secret is read
        # here too, and a keychain that cannot be reached must not become an
        # exception either -- the docstring's promise is what the callers rely on.
        logger.warning(
            "could not remove the item behind %s at the aggregator: %s. "
            "It may still be counting against the plan cap",
            credential_ref,
            exc,
        )
        return False

    try:
        delete_access_token(config, credential_ref)
    except SecretsError as exc:
        # The item IS removed at this point, so this is not a failure of the
        # operation -- it is a stale credential for an item that no longer
        # exists. Reported rather than raised, and reported as what it is.
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
    """Retire without deleting. AC-1.6 and AC-6.5 -- the history outlives the connection."""
    conn.execute(
        update(connections)
        .where(connections.c.connection_id == connection_id)
        .values(status="retired", retired_at=now, updated_at=now)
    )
