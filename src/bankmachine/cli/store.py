"""`bankmachine store` -- the commands that own the datastore's existence."""

from __future__ import annotations

import argparse
from pathlib import Path

from bankmachine.config import Config
from bankmachine.derivers import ALL_DERIVERS
from bankmachine.logging_setup import get_logger
from bankmachine.secrets import (
    DatastoreKeyMissingError,
    SecretsError,
    ensure_datastore_key,
    get_datastore_key,
)
from bankmachine.store import connection
from bankmachine.store.backup import BackupReport, back_up
from bankmachine.store.migrations import migrate
from bankmachine.store.rebuild import RebuildReport, rebuild

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

    rebuild_command = commands.add_parser(
        "rebuild",
        help="reconstruct the normalized tables from the raw responses (AC-5.2)",
        description=(
            "Deletes every row that was derived from a raw response, replays the whole "
            "archive through this build's derivation logic, and refuses to commit the "
            "result if it differs from what it replaced at an unchanged derivation "
            "version. Rows this archive cannot recreate -- anything imported from a "
            "file, and the accounts and connections that history hangs off -- are left "
            "alone."
        ),
    )
    rebuild_command.add_argument(
        "--accept-content-change",
        action="store_true",
        help=(
            "commit a rebuild whose content differs at an unchanged derivation version. "
            "The honest use is an archive that was deliberately pruned; anything else is a "
            "deriver that is not a pure function of its response"
        ),
    )
    rebuild_command.set_defaults(handler=cmd_rebuild)

    backup = commands.add_parser(
        "backup",
        help="write a verified, consistent, encrypted copy of the datastore",
        description=(
            "Takes the exclusive writer lock, writes a single-file copy with the WAL "
            "folded in, then opens that copy with the same key and checks it. A "
            "plain `cp` of the datastore silently loses whatever is still in the "
            "WAL, and copying all three files is only consistent if nothing is "
            "mid-commit. Never overwrites an existing file. The copy is encrypted "
            "and is USELESS WITHOUT THE KEY -- back the key up separately."
        ),
    )
    backup.add_argument(
        "destination",
        type=Path,
        metavar="PATH",
        help="where to write the copy; must not already exist",
    )
    backup.set_defaults(handler=cmd_backup)


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
    if created_key:
        _print_key_backup_instruction(config)
    return 0 if status.healthy else 1


def _print_key_backup_instruction(config: Config) -> None:
    """Printed, not logged, and only where a key was just minted.

    The log is the wrong channel for this: it is read after something has gone
    wrong, and by then the keychain entry is either there or gone. Minting is
    the one moment the operator is certainly looking at this terminal, and it is
    also the last moment at which losing the key costs nothing -- there is no
    data yet.

    It names the read-out command because otherwise the instruction asks for a
    value the operator has no route to: nothing in this product prints the key,
    which is deliberate, and the keychain is therefore the only place it exists.
    The key is not interpolated here or anywhere else.
    """
    print()
    print("BACK THE DATASTORE KEY UP NOW, WHILE THIS STORE IS STILL EMPTY.")
    print(
        f"  A new key was generated in keychain "
        f"{config.keychain_service}/{config.keychain_account}."
    )
    print("  It cannot be derived from the datastore. Without it this store, and every")
    print("  backup copy of it, is unrecoverable ciphertext -- there is no remedy.")
    print("  Nothing in this product prints the key. On macOS, read it out with")
    print(f"    {_keychain_recipe(config)}")
    print("  and put it in a password manager that survives this machine.")
    print()


def _keychain_recipe(config: Config) -> str:
    """The one macOS command that reads the datastore key out for a password manager."""
    return (
        f"security find-generic-password -s {config.keychain_service} "
        f"-a {config.keychain_account} -w"
    )


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
    # The standing reminder, on the command an operator runs when something
    # looks wrong. One line rather than `store init`'s block: a block repeated
    # on every run is a block nobody reads, and the block belongs to the moment
    # the key is minted.
    print(
        f"datastore key:   keychain {config.keychain_service}/{config.keychain_account} -- "
        f"this datastore is unrecoverable without it, and nothing here prints it. "
        f"Read it with `{_keychain_recipe(config)}`"
    )
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


def cmd_rebuild(config: Config, args: argparse.Namespace) -> int:
    report = rebuild(
        config,
        derivers=ALL_DERIVERS,
        accept_content_change=args.accept_content_change,
    )
    _print_rebuild(report)
    return 0


def _print_rebuild(report: RebuildReport) -> None:
    print(f"raw responses replayed:  {report.responses_replayed}")
    # "replaced", not "rebuilt": this counts the rows the replay cleared, and
    # under --accept-content-change the replay is allowed to produce fewer.
    print(f"rows replaced:           {sum(report.rows_deleted.values())}")
    for table, count in sorted(report.rows_deleted.items()):
        if count:
            print(f"  {table}: {count}")
    print(f"derivation version:      {report.derivation_version}")
    if report.previous_derivation_versions:
        previous = ", ".join(str(v) for v in report.previous_derivation_versions)
        print(f"previous version(s):     {previous}")
    if not report.content_changed:
        print("content:                 identical to what it replaced")
    elif report.change_was_expected:
        print(
            "content:                 changed, as expected at a new derivation version -- "
            "every rebuilt row now names it"
        )
    else:
        # Reached only under --accept-content-change; without it the rebuild
        # raises and this line is never printed against an unexplained change.
        print("content:                 CHANGED at an unchanged derivation version, accepted")


def cmd_backup(config: Config, args: argparse.Namespace) -> int:
    report = back_up(config, args.destination)
    _print_backup(config, report)
    return 0


def _print_backup(config: Config, report: BackupReport) -> None:
    print(f"source:          {report.source_path}")
    print(f"backup:          {report.destination}")
    print(f"bytes:           {report.bytes_written}")
    print(f"schema version:  {report.schema_version}")
    print("verified:        yes -- reopened with the datastore key, integrity_check ok")
    # Loud, every time, and not conditional on anything. The key is the half of
    # a backup that has no other source: an aggregator secret can be re-read
    # from the vendor's dashboard, a datastore key cannot be re-read from
    # anywhere. A copy without it is noise that looks like a backup.
    logger.warning(
        "this copy is encrypted and is USELESS WITHOUT THE DATASTORE KEY, which lives in "
        "keychain %s/%s and cannot be recovered if lost -- back the key up separately, "
        "somewhere that survives both this disk and this keychain",
        config.keychain_service,
        config.keychain_account,
    )
