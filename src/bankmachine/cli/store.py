"""`bankmachine store` -- the commands that own the datastore's existence."""

from __future__ import annotations

import argparse
import os
import sys
from getpass import getpass
from pathlib import Path
from typing import NoReturn

from bankmachine.cli.exit_codes import EXIT_OK, EXIT_UNHEALTHY
from bankmachine.cli.parser import AnyParser, RedactingParser
from bankmachine.config import Config
from bankmachine.derivers import ALL_DERIVERS, all_replay_passes
from bankmachine.logging_setup import get_logger
from bankmachine.secrets import (
    DatastoreKeyMissingError,
    KeyEscrowRefusedError,
    SecretsError,
    ensure_datastore_key,
    get_datastore_key,
    set_datastore_key,
    validate_candidate_key,
)
from bankmachine.store import connection
from bankmachine.store.backup import BackupReport, back_up
from bankmachine.store.migrations import migrate
from bankmachine.store.rebuild import RebuildReport, rebuild

logger = get_logger("cli.store")


def add_arguments(subparsers: argparse._SubParsersAction[AnyParser]) -> None:
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

    # Annotated to the base class on purpose: the `__class__` swap below needs a
    # parser this module can name, and `add_arguments` is generic in the class its
    # caller chose. Everything here relies on `ArgumentParser` and nothing narrower.
    key: argparse.ArgumentParser = commands.add_parser(
        "key",
        help="export, check, and restore the datastore key",
        description=(
            "The datastore key cannot be recovered from the datastore, and the daily "
            "balance series cannot be re-synced -- so a lost key is permanent data loss. "
            "These are the three things an operator has to be able to do about that: get "
            "the key out, confirm the copy they stored actually opens their datastore, "
            "and put it back on a new machine. Where the key is SUPPLIED it is read by "
            "prompt and never taken as an argument, so it reaches neither shell history "
            "nor the process table."
        ),
    )
    # The group parser is created by the `store` subparsers action, whose
    # `parser_class` is the ordinary one every other command wants -- so this is
    # the one place the class is swapped after construction rather than chosen at
    # it. `store key <64-hex>` reaches THIS parser's `error()` as an invalid
    # choice, which is exactly an invocation whose text must not be repeated.
    key.__class__ = _KeyParser
    key_commands = key.add_subparsers(
        dest="store_key_command", required=True, parser_class=_KeyParser
    )

    key_export = key_commands.add_parser(
        "export",
        help="render the datastore key so it can be put somewhere that survives this machine",
        description=(
            "Prints the key to an interactive terminal. Refuses a redirected or piped "
            "stdout: an export that can be captured by accident is the shell recipe this "
            "command replaces. Use --to to write a file deliberately."
        ),
    )
    key_export.add_argument(
        "--to",
        type=Path,
        metavar="PATH",
        help=(
            "write the key to a path you name, readable only by you. Must not already "
            "exist. There is no default -- the destination is the operator's choice, "
            "never the product's"
        ),
    )
    key_export.set_defaults(handler=cmd_key_export)

    key_verify = key_commands.add_parser(
        "verify",
        help="check whether a key you have stored actually opens this datastore",
        description=(
            "Prompts for a key and opens the datastore with it. This is a question about "
            "the DATASTORE, not about the keychain: it answers when the keychain entry is "
            "gone, which is the situation an operator is actually in when it matters."
        ),
    )
    key_verify.set_defaults(handler=cmd_key_verify)

    key_import = key_commands.add_parser(
        "import",
        help="restore a datastore key into the keychain",
        description=(
            "Prompts for a key, confirms it opens the datastore, and only then writes it "
            "to the keychain. A key that does not open the datastore is never stored, so "
            "a typo cannot replace a working entry."
        ),
    )
    key_import.set_defaults(handler=cmd_key_import)


class _KeyParser(RedactingParser):
    """A parser that never repeats back what it was given.

    🔴 Attached to the `key` GROUP, not to individual verbs, and that is the
    whole point of the class. An earlier version guarded `verify` and `import`
    one at a time, which left `store key <64-hex>`, `store key export <key>` and
    `store key import --key=<value>` all echoing the secret through argparse's
    own `unrecognized arguments: <value>` -- onto a stderr a scheduled runner
    captures and a shared terminal displays, and before `configure_logging` runs,
    so the formatter's redaction is not even loaded yet.

    An operator who typed the key on the command line has already put it in
    shell history; the product's job is not to print it a second time and to say
    what they now have to go clean up. Inheriting the refusal also means the
    next verb added to this group gets it without anyone remembering to ask.
    """

    def error(self, message: str) -> NoReturn:
        # `from None` is load-bearing: argparse raises its own `ArgumentError`
        # first -- "invalid choice: '<the key>'" -- and chaining it would carry
        # the value into the `__context__` of anything that later renders a
        # traceback. The refusal must not smuggle back what it declined to
        # print.
        raise KeyEscrowRefusedError(
            f"{self.prog}: that is not a usable invocation, and the arguments are not "
            f"repeated back here in case one of them was the key. These commands take no "
            f"key as an argument -- they prompt for it. If you did type it, it is in your "
            f"shell history now: clear it there, then run the command with no arguments. "
            f"(`--help` on this command shows what it does accept.)"
        ) from None


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

    It names both commands, and the second is not decoration. Getting the key out
    is the half an operator remembers to do; confirming that what they wrote down
    is what the datastore actually takes is the half that fails silently, because
    a transposed pair of hex digits is still 64 characters of valid hex and is
    simply a different key. The key is not interpolated here or anywhere else.
    """
    print()
    print("BACK THE DATASTORE KEY UP NOW, WHILE THIS STORE IS STILL EMPTY.")
    print(
        f"  A new key was generated in keychain "
        f"{config.keychain_service}/{config.keychain_account}."
    )
    print("  It cannot be derived from the datastore. Without it this store, and every")
    print("  backup copy of it, is unrecoverable ciphertext -- there is no remedy.")
    print("  Read it out with")
    print("    bankmachine store key export")
    print("  put it in a password manager that survives this machine, and then confirm")
    print("  what you stored actually opens this datastore with")
    print("    bankmachine store key verify")
    print()


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
            f"cannot be recovered from the datastore. Restore it from your backup with "
            f"`bankmachine store key import`, or move the datastore aside to start a new one. "
            f"No key was generated: a fresh one would decrypt nothing and would hide this "
            f"diagnosis behind an authentication failure."
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
        f"this datastore is unrecoverable without it. `bankmachine store key export` "
        f"reads it out; `bankmachine store key verify` checks the copy you stored"
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
        replay_passes=all_replay_passes,
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
    # "none recorded" rather than "None": the absence is a state the operator has
    # to act on, and a bare Python repr in a terminal reads as a bug in the tool.
    version = report.schema_version
    print(f"schema version:  {version if version is not None else 'none recorded'}")
    print("verified:        yes -- reopened with the datastore key, integrity_check ok")
    problem = connection.schema_problem_for(version)
    if problem is not None:
        # The copy is sound and this line is not a warning about it. It exists
        # because a verified backup at a version this build cannot open is a
        # restorable file that `store status` will call unhealthy the moment it
        # is put back -- and an operator mid-recovery reading that, with no
        # warning that it was expected, has every reason to think the restore
        # failed.
        #
        # 🔴 The remedy comes from `remedy_for`, never from a sentence written
        # here. A copy BEHIND this build is migrated forward; one AHEAD of it is
        # not, and telling that operator to run `store init` sends them to a
        # command that applies nothing and reads as "the fix is broken". Two
        # states, two remedies, and one place that knows which is which.
        print(
            f"note:            this copy is at schema version "
            f"{version if version is not None else 'none recorded'}; this build serves "
            f"{connection.SUPPORTED_SCHEMA_VERSION}. {connection.remedy_for(problem)}"
        )
    # Loud, every time, and not conditional on anything. The key is the half of
    # a backup that has no other source: an aggregator secret can be re-read
    # from the vendor's dashboard, a datastore key cannot be re-read from
    # anywhere. A copy without it is noise that looks like a backup.
    logger.warning(
        "this copy is encrypted and is USELESS WITHOUT THE DATASTORE KEY, which lives in "
        "keychain %s/%s and cannot be recovered if lost -- back the key up separately with "
        "`bankmachine store key export`, somewhere that survives both this disk and this "
        "keychain, and check it with `bankmachine store key verify`",
        config.keychain_service,
        config.keychain_account,
    )


def cmd_key_export(config: Config, args: argparse.Namespace) -> int:
    """Hand the operator the key, or refuse in a way that cannot be mistaken for success."""
    if args.to is None and not sys.stdout.isatty():
        # 🔴 The refusal IS the command, and it comes before the key is read:
        # without it this is `security find-generic-password -w` with a nicer
        # name -- a key that can be redirected into a file or a pipe by accident
        # has left the keychain incidentally, which is the thing AC-10.1 forbids.
        # An export the operator asked for in a way nobody can misread is the
        # thing it permits.
        raise KeyEscrowRefusedError(
            "refusing to print the datastore key to something that is not a terminal. "
            "A redirected or piped key is a key that left the keychain by accident. "
            "Use `--to <path>` to write a file deliberately"
        )

    key = get_datastore_key(config)
    if args.to is not None:
        _write_key_file(args.to, key, config)
        _record("exported the datastore key to a file named by the operator")
        print(f"wrote {args.to}, readable only by you.")
        print("Put it in a password manager, check it with `bankmachine store key verify`,")
        print("then delete the file -- it is plaintext until you do.")
        return EXIT_OK

    _record("rendered the datastore key to the operator's terminal")
    print(f"datastore key for keychain {config.keychain_service}/{config.keychain_account}")
    print(f"  {key}")
    print("Put it in a password manager, and ideally on paper.")
    print("Then check it back with `bankmachine store key verify`.")
    return EXIT_OK


def _write_key_file(destination: Path, key: str, config: Config) -> None:
    """Write the key to a path the operator named, readable only by them.

    `O_EXCL` rather than a prior existence check, and it matches `store backup`'s
    refusal: never overwrite a destination. The mode is on the `open` call rather
    than a later `chmod`, so the file is never briefly world-readable.
    """
    _refuse_the_data_directory(destination, config)
    try:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise KeyEscrowRefusedError(
            f"{destination} already exists -- refusing to overwrite it. "
            f"Name a path that does not exist yet"
        ) from exc
    except OSError as exc:
        raise KeyEscrowRefusedError(f"could not write {destination}: {exc}") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"{key}\n")


def _refuse_the_data_directory(destination: Path, config: Config) -> None:
    """Refuse to put the plaintext key beside the ciphertext it decrypts.

    🔴 The one destination the operator does not get to choose, and it is not
    paternalism: the whole value of encryption at rest is that a copied data
    directory is noise. A key file sitting in it makes the directory
    self-decrypting, so a single careless `cp -r`, backup sweep or synced folder
    carries both halves and the encryption stops meaning anything.

    Everywhere else is theirs. AC-17.2 is that the PRODUCT never picks a
    destination -- this refuses the one pick that would undo the product's own
    guarantee, and it names the reason so the operator can pick again.
    """
    data_directory = config.datastore_path.parent.resolve()
    try:
        candidate = destination.resolve().parent
    except OSError:  # an unresolvable path fails later, with a better message
        return
    if candidate == data_directory or data_directory in candidate.parents:
        raise KeyEscrowRefusedError(
            f"refusing to write the key inside {data_directory}, which is where the "
            f"datastore it decrypts lives. A directory holding both is a directory that "
            f"decrypts itself, and one careless copy of it carries both halves. "
            f"Name a path somewhere else"
        )


def cmd_key_verify(config: Config, _args: argparse.Namespace) -> int:
    """Answer whether a candidate opens the datastore. Exit 1 means it does not."""
    candidate = validate_candidate_key(_prompt_for_key("key to check"))
    if connection.opens_with(config, candidate):
        _record("checked a candidate datastore key; it opens the datastore")
        print(f"MATCHES -- this key opens the datastore at {config.datastore_path}")
        return EXIT_OK
    # Ran, and found a problem. Not EXIT_ERROR: the command did exactly what it
    # was asked to do, and the answer is the bad news.
    print(
        f"DOES NOT MATCH -- this key does not open the datastore at {config.datastore_path}",
        file=sys.stderr,
    )
    return EXIT_UNHEALTHY


def cmd_key_import(config: Config, _args: argparse.Namespace) -> int:
    """Restore a key to the keychain, but only once it has proved it opens the store."""
    candidate = validate_candidate_key(_prompt_for_key("key to restore"))
    # 🔴 Verify BEFORE writing. `store init` refuses to mint a key for an
    # existing datastore so that a fresh key cannot hide "no key in the keychain"
    # behind an authentication failure; writing an unverified key here would
    # reintroduce exactly that from the other direction, and would additionally
    # overwrite a working entry with a typo.
    if not connection.opens_with(config, candidate):
        # 🔴 Exit 1, matching `verify` on the identical fact. The contract reads
        # `1` as "ran and found a problem" and `2` as "could not run" -- and this
        # command ran, tested the key, and found it wrong. Reporting it as `2`
        # would put a correct refusal in the same bucket as a missing datastore,
        # which is the collapse the exit-code norm exists to prevent.
        print(
            f"DOES NOT MATCH -- that key does not open the datastore at "
            f"{config.datastore_path}, so it was NOT written to the keychain. Any key "
            f"already in {config.keychain_service}/{config.keychain_account} is untouched",
            file=sys.stderr,
        )
        return EXIT_UNHEALTHY
    _record("restored the datastore key to the keychain, verified against the datastore")
    set_datastore_key(config, candidate)
    print(
        f"restored the datastore key to keychain {config.keychain_service}/"
        f"{config.keychain_account}, verified against {config.datastore_path}."
    )
    return EXIT_OK


def _record(what: str) -> None:
    """Log that an escrow action happened. Never what the key was.

    🔴 The success paths are the ones worth recording, and they were the ones
    recording nothing. A key leaving the keychain is the single most consequential
    thing this product does to a secret; an operator reconstructing "when did this
    key get exported, and did anyone restore one" had only the failures to read,
    because those went through the error path and the successes went nowhere.

    The value is not interpolated here -- the sentence names the ACTION. That is
    what makes this safe to write at INFO to a file the operator keeps.

    🔴 Holding no secret is not the same as giving the redactor nothing to scrub.
    The formatter blanks the word after any credential label and `:` or `=`,
    whatever that word is, so a sentence reading "key: it opens" logs as
    "key: [REDACTED] opens" -- a record that looks like a leak on the one line an
    operator reads to learn whether a key left. Every `what` is therefore a string
    literal the redactor leaves whole, and
    `tests/cli/test_store_commands.py::test_every_escrow_record_survives_redaction_word_for_word`
    collects each call's literal from this file's source and holds that.
    """
    logger.info("%s", what)


def _prompt_for_key(what: str) -> str:
    """Read a key from the operator without echoing it and without a command line.

    🔴 Never an argument. An argument lands in shell history and in the process
    table, which is the failure AC-10.1 names and the reason the shell recipe was
    never an acceptable permanent answer -- fixing the export direction while
    reintroducing the same leak on the input direction would move the defect
    rather than close it.
    """
    return getpass(f"{what} (not echoed): ")
