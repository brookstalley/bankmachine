"""The command-line surface. argparse, because the CLI is a handful of subcommands."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path

from bankmachine import mcp as mcp_commands
from bankmachine.cli import connections as connections_commands
from bankmachine.cli import connector as connector_commands
from bankmachine.cli import enroll as enroll_commands
from bankmachine.cli import store as store_commands
from bankmachine.cli import sync as sync_commands
from bankmachine.cli.enroll import EnrollmentError
from bankmachine.cli.exit_codes import EXIT_ERROR, EXIT_OK, EXIT_RUN_AGAIN, EXIT_UNHEALTHY
from bankmachine.config import Config, ConfigError, load_config
from bankmachine.connector import ConnectorError
from bankmachine.logging_setup import configure_logging, get_logger, log_startup
from bankmachine.secrets import SecretsError
from bankmachine.store.connection import StoreError

__all__ = [
    "EXIT_ERROR",
    "EXIT_OK",
    "EXIT_RUN_AGAIN",
    "EXIT_UNHEALTHY",
    "build_parser",
    "run",
]

logger = get_logger(__name__)

#: Marks a record whose stderr copy the caller has already printed itself.
#: Without it every failure appears twice on the terminal, once as the
#: `bankmachine: ...` sentence and again as a timestamped log line.
_FILE_ONLY = {"file_only": True}


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
    # 🔴 The one place file permissions are decided, and it is here rather than
    # at each `open` on purpose. The datastore, its WAL and shm, the plaintext
    # log, a backup copy and every directory holding them are created by five
    # different modules; a `chmod` after each one leaves a window in which the
    # file already exists group- and world-readable, and a rule spread over five
    # call sites is one the sixth forgets. Under the inherited default (022)
    # every one of them lands 0644, which makes the "another local user is
    # stopped by OS file permissions" control a claim rather than a fact -- and
    # the log is plaintext, carrying paths, institution ids and SQL text.
    #
    # Every command, `bankmachine mcp` included, is a subparser of the parser
    # built below, so this single statement covers the whole product.
    os.umask(0o077)

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
        # 🔴 This is the one arm where a non-zero code does not mean failure.
        # `EnrollmentError` carries its own code, and the subclasses that mean
        # "ran and found a problem" -- the roster is full, the operator walked
        # away -- carry EXIT_UNHEALTHY. Recording those as errors would conflate
        # exactly the two outcomes this logging exists to separate, so the level
        # follows the code the exception chose rather than the arm it was caught in.
        if exc.exit_code == EXIT_ERROR:
            logger.error("command %s failed: %s", args.command, exc, extra=_FILE_ONLY)
        else:
            logger.warning("command %s refused: %s", args.command, exc, extra=_FILE_ONLY)
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
        logger.error("command %s failed: %s", args.command, exc, extra=_FILE_ONLY)
        return EXIT_ERROR
    except Exception:  # prawduct:allow prawduct/broad-except -- logs, reports, and exits 2
        # An unexpected failure is exactly the one worth a traceback in the file,
        # and it is the case most likely to leave no other trace.
        #
        # 🔴 `2`, not `1`. `1` means the command ran to the end and found a
        # problem, so what it printed can be trusted as far as it goes; a crash
        # means it did not finish and nothing it printed can be relied on. The
        # scheduled job reads the code and nothing else, and those two outcomes
        # must not look alike to it -- which is the same argument that makes the
        # 1/2 split non-collapsible in the first place.
        #
        # Nothing is swallowed: the traceback goes to the log file, which is the
        # only durable record a scheduled run leaves, and to stderr, which is what
        # a developer running this by hand reads. Only the propagation is traded
        # away, and it is traded for an exit code that tells the truth.
        logger.exception("command %s failed unexpectedly", args.command, extra=_FILE_ONLY)
        print(f"bankmachine: {args.command} failed unexpectedly", file=sys.stderr)
        traceback.print_exc()
        return EXIT_ERROR
