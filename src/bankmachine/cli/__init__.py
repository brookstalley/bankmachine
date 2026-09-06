"""The command-line surface. argparse, because the CLI is a handful of subcommands."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from bankmachine.cli import connector as connector_commands
from bankmachine.cli import store as store_commands
from bankmachine.cli import sync as sync_commands
from bankmachine.config import Config, ConfigError, load_config
from bankmachine.connector import ConnectorError
from bankmachine.logging_setup import configure_logging, log_startup
from bankmachine.secrets import SecretsError
from bankmachine.store.connection import StoreError

EXIT_OK = 0
EXIT_UNHEALTHY = 1
EXIT_ERROR = 2


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
    except (StoreError, SecretsError, ConnectorError) as exc:
        # Expected failures get a sentence, not a traceback -- but they are never
        # silent, which is the one outcome this project disallows.
        print(f"bankmachine: {exc}", file=sys.stderr)
        return EXIT_ERROR
