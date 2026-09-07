"""The command-line surface. argparse, because the CLI is a handful of subcommands."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from bankmachine.cli import connections as connections_commands
from bankmachine.cli import connector as connector_commands
from bankmachine.cli import enroll as enroll_commands
from bankmachine.cli import store as store_commands
from bankmachine.cli import sync as sync_commands
from bankmachine.cli.enroll import EnrollmentError
from bankmachine.cli.exit_codes import EXIT_ERROR, EXIT_OK, EXIT_UNHEALTHY
from bankmachine.config import Config, ConfigError, load_config
from bankmachine.connector import ConnectorError
from bankmachine.logging_setup import configure_logging, log_startup
from bankmachine.secrets import SecretsError
from bankmachine.store.connection import StoreError

__all__ = ["EXIT_ERROR", "EXIT_OK", "EXIT_UNHEALTHY", "build_parser", "run"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bankmachine",
        description="Read-only local-first personal finance datastore. It never moves money.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="config file to read (default: $BANKMACHINE_CONFIG, else the documented default)",
    )
    parser.add_argument("--verbose", action="store_true", help="log at DEBUG instead of INFO")
    subparsers = parser.add_subparsers(dest="command", required=True)
    store_commands.add_arguments(subparsers)
    connector_commands.add_arguments(subparsers)
    enroll_commands.add_arguments(subparsers)
    connections_commands.add_arguments(subparsers)
    sync_commands.add_arguments(subparsers)
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config: Config = load_config(config_path=args.config)
    except ConfigError as exc:
        parser.exit(EXIT_ERROR, f"bankmachine: configuration error: {exc}\n")

    configure_logging(config, level=logging.DEBUG if args.verbose else logging.INFO)
    log_startup(config)

    try:
        return int(args.handler(config, args))
    except EnrollmentError as exc:
        # 🔴 The code comes off the exception, not from the type named here. The
        # api-contract norm calls the 1/2 split non-collapsible because a
        # scheduled job reads it, and the cap proved why that needs a mechanism:
        # it is refused from two places -- before the link token, and again inside
        # the write transaction that closes the race between two enrollments --
        # and reporting one condition with two different codes depending on which
        # check caught it is worse than either code alone. A tuple of types here
        # would have to be extended by whoever adds the next refusal, and would
        # silently answer `2` when they forget.
        print(f"bankmachine: {exc}", file=sys.stderr)
        return exc.exit_code
    except (StoreError, SecretsError, ConnectorError) as exc:
        # Expected failures get a sentence, not a traceback -- but they are never
        # silent, which is the one outcome this project disallows.
        print(f"bankmachine: {exc}", file=sys.stderr)
        return EXIT_ERROR
