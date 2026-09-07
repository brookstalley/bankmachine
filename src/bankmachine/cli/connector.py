"""`bankmachine connector` -- the commands that reach the aggregator.

`check` is the walking skeleton of build step 2: config resolves, the keychain
yields a secret, the aggregator answers, and the answer lands in the archive
verbatim. It asks for the smallest thing the aggregator will tell anyone --
one page of its supported-institution list -- because that needs client
credentials and nothing else, so the whole path is provable before enrollment
exists.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys

from bankmachine.config import Config
from bankmachine.connector import FetchedResponse
from bankmachine.connector.plaid.client import PlaidClient
from bankmachine.logging_setup import get_logger
from bankmachine.secrets import get_plaid_secret, set_plaid_secret
from bankmachine.store.connection import DatastoreMissingError, inspect
from bankmachine.store.engine import writer_connection
from bankmachine.store.raw import record_response

logger = get_logger("cli.connector")

#: What `check` asks for. One page is enough to prove the path, and asking for
#: the whole list would make a smoke test into a several-megabyte download.
CHECK_PAGE_SIZE = 1


def add_arguments(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    connector = subparsers.add_parser("connector", help="talk to the aggregator")
    commands = connector.add_subparsers(dest="connector_command", required=True)

    check = commands.add_parser(
        "check",
        help="verify credentials reach the aggregator, and archive what it answers",
        description=(
            "Makes the smallest authenticated call the aggregator offers and stores the "
            "response verbatim. Proves configuration, the keychain, the network path and "
            "the raw archive in one command, without needing an enrolled connection."
        ),
    )
    check.add_argument(
        "--country",
        action="append",
        dest="countries",
        metavar="CODE",
        help="ISO country code to ask about; repeatable (default: US)",
    )
    check.set_defaults(handler=cmd_check)

    set_secret = commands.add_parser(
        "set-secret",
        help="store the aggregator secret for this environment in the keychain",
        description=(
            "Prompts without echoing at a terminal, and reads standard input when piped, "
            "so the secret never appears in a shell history or a process listing. The "
            "secret is stored per environment: setting the sandbox one does not touch "
            "production's."
        ),
    )
    set_secret.set_defaults(handler=cmd_set_secret)


def cmd_set_secret(config: Config, _args: argparse.Namespace) -> int:
    # A pipe is how a password manager feeds this; a terminal gets a prompt that
    # does not echo. Reading stdin unconditionally would leave an operator at a
    # blank line with no idea the command was waiting for them.
    if sys.stdin.isatty():
        secret = getpass.getpass(f"aggregator secret for {config.environment}: ").strip()
    else:
        secret = sys.stdin.readline().strip()
    set_plaid_secret(config, secret)
    print(
        f"stored the {config.environment} aggregator secret in keychain "
        f"{config.keychain_service}/{config.plaid_keychain_account}"
    )
    return 0


def cmd_check(config: Config, args: argparse.Namespace) -> int:
    # Before the network call, deliberately, and through `inspect` because it is
    # the surface that reports an absent datastore instead of creating one. A
    # typo'd path then costs nothing at the aggregator -- which matters more than
    # it looks, since rate limits are per client and this command exists to be
    # run when something is already wrong.
    status = inspect(config)
    if not status.healthy:
        raise DatastoreMissingError(
            f"datastore at {status.path} is not ready ({status.problem or 'unknown problem'}); "
            f"run `bankmachine store init` before reaching the aggregator"
        )

    countries = args.countries or ["US"]
    secret = get_plaid_secret(config)

    with PlaidClient(config, secret) as client:
        fetched = client.institutions_get(count=CHECK_PAGE_SIZE, offset=0, country_codes=countries)

    with writer_connection(config) as conn:
        stored = record_response(
            conn,
            connection_id=None,
            endpoint=fetched.endpoint.path,
            body=fetched.body,
            received_at=fetched.received_at,
            request_context=fetched.request_context,
        )

    logger.info(
        "archived %s: %d bytes as raw_response %d",
        fetched.endpoint,
        len(fetched.body),
        stored.raw_response_id,
    )
    _print_check(config, fetched, stored_id=stored.raw_response_id)
    return 0


def _reported_total(body: bytes) -> int | None:
    """The aggregator's own count of matching institutions, if it said one.

    Parsed for the operator's benefit only -- the archive already holds the
    bytes this came from, so a shape the parser does not recognize costs a line
    of output rather than the response.
    """
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    total = payload.get("total")
    return total if isinstance(total, int) else None


def _print_check(config: Config, fetched: FetchedResponse, *, stored_id: int) -> None:
    print(f"aggregator:   {config.environment}")
    print(f"endpoint:     {fetched.endpoint}")
    print(f"received:     {fetched.received_at.isoformat()}")
    print(f"body:         {len(fetched.body)} bytes, archived as raw_response {stored_id}")
    total = _reported_total(fetched.body)
    if total is not None:
        print(f"institutions: {total} matching the requested countries")
