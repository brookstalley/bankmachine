"""The command-line surface. argparse, because the CLI is a handful of subcommands."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from bankmachine import mcp as mcp_commands
from bankmachine.cli import connections as connections_commands
from bankmachine.cli import connector as connector_commands
from bankmachine.cli import enroll as enroll_commands
from bankmachine.cli import store as store_commands
from bankmachine.cli import sync as sync_commands
from bankmachine.cli.enroll import EnrollmentError
from bankmachine.cli.exit_codes import EXIT_ERROR, EXIT_OK, EXIT_UNHEALTHY
from bankmachine.config import Config, ConfigError, load_config
from bankmachine.connector import ConnectorError
from bankmachine.logging_setup import configure_logging, get_logger, log_startup
from bankmachine.secrets import SecretsError
from bankmachine.store.connection import StoreError

__all__ = ["EXIT_ERROR", "EXIT_OK", "EXIT_UNHEALTHY", "build_parser", "run"]

logger = get_logger(__name__)


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
    mcp_commands.add_arguments(subparsers)
    sync_commands.add_arguments(subparsers)
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config: Config = load_config(config_path=args.config)
    except ConfigError as exc:
        # 🔴 This is the one failure that cannot reach the log file, and the reason
        # is structural rather than an omission: the log destination is `log_dir`,
        # which comes from the configuration that just failed to load. There is
        # nowhere to write. A scheduled run that dies here leaves only its exit
        # code, so the exit code has to carry it.
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
        logger.error("command %s failed: %s", args.command, exc)
        return exc.exit_code
    except (StoreError, SecretsError, ConnectorError) as exc:
        # Expected failures get a sentence, not a traceback -- but they are never
        # silent, which is the one outcome this project disallows.
        #
        # 🔴 The log record is not a duplicate of the stderr line. Nobody is
        # watching stderr on a scheduled run, so without this the log cannot tell
        # a run that failed from a run that found nothing to do -- and for a
        # product whose named primary failure mode is silent staleness, those two
        # must never look alike in the durable record.
        print(f"bankmachine: {exc}", file=sys.stderr)
        logger.error("command %s failed: %s", args.command, exc)
        return EXIT_ERROR
    except Exception:  # prawduct:allow prawduct/broad-except -- logs and re-raises
        # An unexpected failure is exactly the one worth a traceback in the file,
        # and it is the case most likely to leave no other trace. This swallows
        # nothing: the `raise` preserves the exit status and the stderr traceback
        # a developer sees interactively, and only the log gains a record.
        logger.exception("command %s failed unexpectedly", args.command)
        raise
