"""`bankmachine enroll` -- linking one institution, and the one irreversible step.

AC-1.1 asks for a hosted enrollment URL and no local web server. The operator
opens the URL, completes Link in a browser, and this command polls
`POST /link/token/get` until the session carries a public token. That is why
there is no listener here: the aggregator hosts the flow, and the result is
fetched rather than delivered.

🔴 **The window is the reason this command confirms before it exchanges.**
AC-1.2 makes the requested history window immutable for the life of the
connection -- raising it later means removing and re-linking -- so the last
moment it can be corrected is before the public token is spent. The required
argument on `link_token_create` stops a caller forgetting a window; nothing but
a human reading it stops a caller sending the wrong one.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Connection as SAConnection

from bankmachine.cli.exit_codes import EXIT_OK, EXIT_UNHEALTHY
from bankmachine.config import Config
from bankmachine.connector import LinkSession, LinkToken
from bankmachine.connector.plaid.client import (
    PlaidClient,
    capabilities_of,
    institution_ref_of,
)
from bankmachine.derivers import ALL_DERIVERS
from bankmachine.logging_setup import get_logger
from bankmachine.secrets import get_plaid_secret, set_access_token
from bankmachine.store.connection import DatastoreMissingError, inspect
from bankmachine.store.derivation import apply_response
from bankmachine.store.engine import reader_connection, transaction, writer_connection
from bankmachine.store.schema import connections, institutions
from bankmachine.store.types import UtcInstant, now_utc

logger = get_logger("cli.enroll")

#: What enrollment asks the aggregator for. Both products are requested up front
#: by operator decision (2026-09-07), which bills `investments` on every
#: connection including deposit-only ones. The recommendation was `transactions`
#: alone, because AC-3.2 drives investment pulls off `/item/get`'s
#: `available_products` rather than off what was requested -- and that discovery
#: still runs. What requesting both changes is the bill, and that a connection
#: enrolled without a product cannot gain it without re-linking.
ENROLLMENT_PRODUCTS: tuple[str, ...] = ("transactions", "investments")

#: Countries the institution picker offers. Configuration would be premature:
#: the roster is one operator's, and a second country is a config knob the day
#: someone needs one rather than a setting nobody has ever set.
ENROLLMENT_COUNTRIES: tuple[str, ...] = ("US",)

#: How often the hosted session is polled, and for how long. The ceiling is the
#: hosted URL's own lifetime: polling past the point the URL can still be used
#: would report a timeout the operator could no longer do anything about.
POLL_INTERVAL_SECONDS = 3.0


class EnrollmentError(Exception):
    """Enrollment could not be completed. Never raised after a token is spent."""


class ConnectionCapReachedError(EnrollmentError):
    """AC-1.5: the configured plan cap has no room for another connection.

    Carries the numbers rather than a formatted sentence so the caller can list
    the current connections beside them -- the requirement asks for the limit to
    be *explained* and for existing connections to be listed, which a bare
    message cannot do.
    """

    def __init__(self, *, live_count: int, cap: int) -> None:
        self.live_count = live_count
        self.cap = cap
        super().__init__(
            f"the configured connection cap is {cap} and {live_count} are already live. "
            f"Retire one with `bankmachine connections retire <id>` before enrolling another"
        )


class EnrollmentAbandonedError(EnrollmentError):
    """The operator did not finish the hosted session before it expired.

    Its own type because it is the one failure here that is not a fault: nothing
    is wrong, the operator simply walked away, and the remedy is to run the
    command again rather than to investigate anything.
    """


@dataclass(frozen=True, slots=True)
class EnrolledConnection:
    """What one completed enrollment produced."""

    connection_id: int
    institution_id: int
    institution_name: str
    source_connection_id: str
    capabilities: frozenset[str]
    requested_history_days: int
    updated_existing: bool


@dataclass(frozen=True, slots=True)
class LiveConnection:
    """One live connection, as the cap refusal and `connections list` report it."""

    connection_id: int
    institution_name: str
    enrolled_at: UtcInstant
    status: str


def live_connections(conn: SAConnection) -> list[LiveConnection]:
    """Every connection that has not been retired, oldest first.

    Retired connections are excluded because they cost nothing at the aggregator
    and hold history the operator chose to keep -- counting them toward the cap
    would make AC-1.6's retirement useless, since retiring would not free a slot.
    """
    rows = conn.execute(
        select(
            connections.c.connection_id,
            institutions.c.name,
            connections.c.enrolled_at,
            connections.c.status,
        )
        .select_from(connections.join(institutions))
        .where(connections.c.retired_at.is_(None))
        .order_by(connections.c.enrolled_at)
    ).all()
    return [
        LiveConnection(
            connection_id=int(row[0]),
            institution_name=str(row[1]),
            enrolled_at=row[2],
            status=str(row[3]),
        )
        for row in rows
    ]


def _explain_cap(live: list[LiveConnection], cap: int) -> str:
    """AC-1.5 asks for the limit explained AND the connections listed.

    A bare "cap reached" leaves the operator with no way to act on it: the whole
    point of the requirement is that they can see which connection to retire.
    """
    lines = [
        f"the configured connection cap is {cap} and {len(live)} are already live.",
        "Retire one to make room:",
        "",
    ]
    lines.extend(
        f"    {c.connection_id:>4}  {c.institution_name}  ({c.status}, "
        f"enrolled {c.enrolled_at.isoformat()[:10]})"
        for c in live
    )
    lines.extend(["", "    bankmachine connections retire <id>"])
    return "\n".join(lines)


def add_arguments(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    enroll = subparsers.add_parser(
        "enroll",
        help="link one institution and record the connection",
        description=(
            "Prints a hosted enrollment URL, waits for you to complete it in a browser, "
            "then exchanges the result for a stored connection. The history window is "
            "confirmed before anything irreversible happens, because it cannot be raised "
            "afterwards without re-linking."
        ),
    )
    enroll.add_argument(
        "--yes",
        action="store_true",
        help=(
            "skip the history-window confirmation. For unattended and test use; "
            "an interactive operator should read the window instead"
        ),
    )
    enroll.add_argument(
        "--timeout",
        type=int,
        default=900,
        metavar="SECONDS",
        help="how long to wait for the hosted session to be completed (default: 900)",
    )
    enroll.set_defaults(handler=cmd_enroll)


def cmd_enroll(config: Config, args: argparse.Namespace) -> int:
    # Before the network call and before the URL, because a typo'd datastore path
    # would otherwise be discovered after the operator had completed a Link
    # session -- at which point the Item exists at the aggregator and this side
    # has nowhere to record it.
    status = inspect(config)
    if not status.healthy:
        raise DatastoreMissingError(
            f"datastore at {status.path} is not ready ({status.problem or 'unknown problem'}); "
            f"run `bankmachine store init` before enrolling"
        )

    # 🔴 Checked BEFORE the link token exists, because the alternative is
    # discovering the cap after the operator has completed a Link session -- at
    # which point the aggregator has minted an Item that this side will refuse to
    # record, and the operator is paying for a connection nobody can use. The
    # check inside the write transaction below is not this check repeated: that
    # one closes the race between two enrollments, this one protects the
    # operator's time and the far end's state.
    with reader_connection(config) as reader:
        live = live_connections(reader)
    if len(live) >= config.connection_cap:
        print(f"bankmachine: {_explain_cap(live, config.connection_cap)}", file=sys.stderr)
        return EXIT_UNHEALTHY

    secret = get_plaid_secret(config)
    with PlaidClient(config, secret) as client:
        issued = client.link_token_create(
            history_days=config.history_days,
            client_user_id=f"bankmachine-{config.environment}",
            country_codes=list(ENROLLMENT_COUNTRIES),
            products=list(ENROLLMENT_PRODUCTS),
        )
        if not _confirm_window(config, issued, assume_yes=args.yes):
            print("enrollment cancelled; nothing was linked", file=sys.stderr)
            return EXIT_UNHEALTHY

        _print_invitation(config, issued)
        try:
            session = _await_completion(client, issued, timeout_seconds=args.timeout)
        except EnrollmentAbandonedError as exc:
            print(f"bankmachine: {exc}", file=sys.stderr)
            return EXIT_UNHEALTHY

        # 🔴 Past this line the far end has spent a single-use token and minted a
        # durable Item. Everything after it is this side catching up, and a
        # failure here leaves a connection that exists at the aggregator and not
        # in the datastore -- which is why nothing above it is allowed to fail.
        assert session.public_token is not None  # `finished` is derived from it
        grant = client.exchange_public_token(session.public_token)
        item = client.item_get(grant.access_token)

    credential_ref = config.connection_keychain_account(grant.source_connection_id)
    set_access_token(config, credential_ref, grant.access_token)

    with writer_connection(config) as conn:
        # `apply_response` rather than a bare record: it archives and derives as
        # the sync path does, in the two commits that keep the archive when a
        # deriver raises. The institution row this connection hangs from is the
        # derivation's output, so the connection cannot be written first.
        apply_response(
            conn,
            connection_id=None,
            endpoint=item.endpoint.path,
            body=item.body,
            received_at=item.received_at,
            derivers=ALL_DERIVERS,
            request_context=item.request_context,
        )
        source_institution_id, _ = institution_ref_of(item.body)
        with transaction(conn):
            enrolled = _record_connection(
                conn,
                source_institution_id=source_institution_id,
                source_connection_id=grant.source_connection_id,
                credential_ref=credential_ref,
                capabilities=capabilities_of(item.body),
                requested_history_days=issued.requested_history_days,
                connection_cap=config.connection_cap,
                now=now_utc(),
            )

    logger.info(
        "enrolled %s as connection %d (%s), requested %d days of history",
        enrolled.institution_name,
        enrolled.connection_id,
        "updated" if enrolled.updated_existing else "new",
        enrolled.requested_history_days,
    )
    _print_result(enrolled)
    return EXIT_OK


def _confirm_window(config: Config, issued: LinkToken, *, assume_yes: bool) -> bool:
    """The last moment AC-1.2 is reversible.

    Skipped without a prompt when stdin is not a terminal, because a piped run
    has nobody to answer and blocking would hang an unattended enrollment on a
    question -- but the window is still printed, so the transcript records what
    was asked for.
    """
    print(f"history window: {issued.requested_history_days} days will be requested")
    print(
        "🔴 this cannot be raised later without removing and re-linking the connection "
        "(AC-1.2). What the aggregator actually grants may be less, and is not known "
        "until the first sync."
    )
    if assume_yes or not sys.stdin.isatty():
        return True
    answer = input(f"enrol against {config.environment} with this window? [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def _print_invitation(config: Config, issued: LinkToken) -> None:
    print()
    print(f"open this URL to link an institution ({config.environment}):")
    print()
    print(f"    {issued.hosted_link_url}")
    print()
    print("waiting for you to finish...", flush=True)


def _await_completion(
    client: PlaidClient,
    issued: LinkToken,
    *,
    timeout_seconds: int,
) -> LinkSession:
    """Poll until the operator finishes, or until waiting stops being useful.

    The deadline is a wall-clock budget rather than an attempt count so that a
    slow aggregator does not shorten the operator's window to finish -- the thing
    being waited on is a human in a browser, not a request.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        session = client.link_token_get(issued.token)
        if session.finished:
            return session
        if time.monotonic() >= deadline:
            raise EnrollmentAbandonedError(
                f"the hosted session was not completed within {timeout_seconds}s"
                + (f" (session {session.session_id})" if session.session_id else "")
                + ". Nothing was linked; run `bankmachine enroll` again to start over"
            )
        time.sleep(POLL_INTERVAL_SECONDS)


def _record_connection(
    conn: SAConnection,
    *,
    source_institution_id: str,
    source_connection_id: str,
    credential_ref: str,
    capabilities: frozenset[str],
    requested_history_days: int,
    connection_cap: int,
    now: UtcInstant,
) -> EnrolledConnection:
    """The `connections` row, converged on the institution rather than inserted.

    🔴 AC-1.4's idempotency is structural before it is procedural: a partial
    unique index permits one live connection per institution, so a second
    enrollment of the same institution cannot insert even if this function tried.
    The cap count is taken inside this transaction for the same reason a read
    outside it would be wrong -- two enrollments could both see room.

    `enrolled_at` is preserved on an update. It records when the operator first
    linked this institution, and re-linking after an expired login is that
    enrollment continuing rather than a new one; `granted_history_days` is left
    alone for the same reason, since a re-link does not re-grant a window this
    product has not yet observed (AC-1.3a).

    `requested_history_days` IS rewritten, and the asymmetry is deliberate: a
    re-enrollment goes through Link again, which is exactly the "remove and
    re-link" AC-1.2 names as the only way to change the window. So the new value
    is what was actually asked for this time, and preserving the old one would
    make the column describe a request nobody made.
    """
    institution = conn.execute(
        select(institutions.c.institution_id, institutions.c.name).where(
            institutions.c.source_institution_id == source_institution_id
        )
    ).one_or_none()
    if institution is None:
        raise EnrollmentError(
            f"institution {source_institution_id} was not derived from the archived item "
            f"response, so there is nothing for the connection to hang from"
        )
    institution_id, institution_name = int(institution[0]), str(institution[1])

    existing = conn.execute(
        select(connections.c.connection_id, connections.c.enrolled_at).where(
            connections.c.institution_id == institution_id,
            connections.c.retired_at.is_(None),
        )
    ).one_or_none()

    capability_json = json.dumps(sorted(capabilities))
    if existing is not None:
        connection_id = int(existing[0])
        conn.execute(
            update(connections)
            .where(connections.c.connection_id == connection_id)
            .values(
                source_connection_id=source_connection_id,
                credential_ref=credential_ref,
                capabilities=capability_json,
                requested_history_days=requested_history_days,
                status="active",
                last_error_code=None,
                last_error_at=None,
                updated_at=now,
            )
        )
        return EnrolledConnection(
            connection_id=connection_id,
            institution_id=institution_id,
            institution_name=institution_name,
            source_connection_id=source_connection_id,
            capabilities=capabilities,
            requested_history_days=requested_history_days,
            updated_existing=True,
        )

    live = conn.execute(
        select(func.count())
        .select_from(connections)
        .where(connections.c.retired_at.is_(None))
    ).scalar_one()
    if live >= connection_cap:
        raise ConnectionCapReachedError(live_count=int(live), cap=connection_cap)

    result = conn.execute(
        insert(connections).values(
            institution_id=institution_id,
            source_connection_id=source_connection_id,
            credential_ref=credential_ref,
            capabilities=capability_json,
            requested_history_days=requested_history_days,
            granted_history_days=None,
            status="active",
            enrolled_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    primary_key = result.inserted_primary_key
    assert primary_key is not None  # an INTEGER PRIMARY KEY insert always yields one
    return EnrolledConnection(
        connection_id=int(primary_key[0]),
        institution_id=institution_id,
        institution_name=institution_name,
        source_connection_id=source_connection_id,
        capabilities=capabilities,
        requested_history_days=requested_history_days,
        updated_existing=False,
    )


def _print_result(enrolled: EnrolledConnection) -> None:
    print()
    verb = "updated connection" if enrolled.updated_existing else "linked"
    print(f"{verb} {enrolled.connection_id}: {enrolled.institution_name}")
    print(f"  requested history: {enrolled.requested_history_days} days")
    print("  granted history:   not yet known -- filled at the first sync (AC-1.3a)")
    print(f"  capabilities:      {', '.join(sorted(enrolled.capabilities)) or 'none reported'}")
