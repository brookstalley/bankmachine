"""`bankmachine store` -- the commands that own the datastore's existence."""

from __future__ import annotations

import argparse

from bankmachine.config import Config
from bankmachine.logging_setup import get_logger
from bankmachine.secrets import (
    DatastoreKeyMissingError,
    SecretsError,
    ensure_datastore_key,
    get_datastore_key,
)
from bankmachine.store import connection
from bankmachine.store.migrations import migrate

logger = get_logger("cli.store")


def add_arguments(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    store = subparsers.add_parser("store", help="create and inspect the encrypted datastore")
    commands = store.add_subparsers(dest="store_command", required=True)

    init = commands.add_parser(
        "init",
        help="create the datastore and bring it to the current schema version",
        description=(
            "The only command that creates a datastore. Every other command opens a file "
            "that must already exist, so a typo'd path is reported rather than silently "
            "turned into a valid, empty store."
        ),
    )
    init.set_defaults(handler=cmd_init)

    status = commands.add_parser("status", help="report the datastore's state without changing it")
    status.set_defaults(handler=cmd_status)


def cmd_init(config: Config, _args: argparse.Namespace) -> int:
    existed = config.datastore_path.exists()
    created_key = _obtain_key(config, datastore_existed=existed)
    if created_key:
        logger.warning(
            "generated a new datastore key in keychain %s/%s",
            config.keychain_service,
            config.keychain_account,
        )

    applied = migrate(config)

    status = connection.inspect(config)
    if existed:
        print(f"datastore already present at {status.path}")
    else:
        print(f"datastore created at {status.path}")
    if applied:
        print(f"applied migration(s): {', '.join(str(v) for v in applied)}")
    else:
        print("no migrations to apply")
    _print_status(status)
    return 0 if status.healthy else 1


def _obtain_key(config: Config, *, datastore_existed: bool) -> bool:
    """Get the datastore key, generating one ONLY for a datastore that does not exist yet.

    A key is never minted for a store that is already there. On the
    restored-from-backup path -- datastore present, keychain entry gone -- a
    fresh key would decrypt nothing, and storing it would replace an exact
    diagnosis ("no key in the keychain") with a misleading one ("authentication
    failure"), sending the operator to the wrong recovery. The key cannot be
    derived from the datastore, so the only honest move is to say so and stop.

    Returns whether a key was generated. The key itself is deliberately not
    returned: nothing above this line needs its value.
    """
    if not datastore_existed:
        _, created = ensure_datastore_key(config)
        return created

    try:
        get_datastore_key(config)
    except DatastoreKeyMissingError as exc:
        raise SecretsError(
            f"a datastore exists at {config.datastore_path}, but keychain "
            f"{config.keychain_service}/{config.keychain_account} holds no key for it. The key "
            f"cannot be recovered from the datastore. Restore the keychain entry from your "
            f"backup, or move the datastore aside to start a new one. No key was generated: a "
            f"fresh one would decrypt nothing and would hide this diagnosis behind an "
            f"authentication failure."
        ) from exc
    return False


def cmd_status(config: Config, _args: argparse.Namespace) -> int:
    status = connection.inspect(config)
    _print_status(status)
    return 0 if status.healthy else 1


def _print_status(status: connection.DatastoreStatus) -> None:
    print(f"environment:     {status.environment}")
    print(f"datastore:       {status.path}")
    print(f"present:         {'yes' if status.exists else 'no'}")
    if status.exists:
        print(f"journal mode:    {status.journal_mode or 'unknown'}")
        version = "none" if status.schema_version is None else str(status.schema_version)
        print(f"schema version:  {version} (this build serves {status.supported_schema_version})")
        print(
            f"writer lock:     {'held by another process' if status.writer_lock_held else 'free'}"
        )
    print(f"healthy:         {'yes' if status.healthy else 'no'}")
    if status.problem:
        print(f"problem:         {status.problem}")
