"""`store backup` -- one consistent encrypted copy, taken under the writer lock.

The datastore key cannot be recovered once lost, so a backup is the only thing
standing between a disk failure and permanent loss of the one series nothing can
rebuild: `balances_daily`. No aggregator backfills a balance history, so a day
not captured is a day gone for good, and re-enrolling recovers transactions but
never that.

WHY THIS IS A WRITER-ROLE OPERATION, WHICH LOOKS WRONG AT FIRST

`VACUUM INTO` writes nothing to the source, so a read-role handle ought to serve.
It does not: under `PRAGMA query_only = ON` it fails `SQLITE_READONLY`
("attempt to write a readonly database") *(measured)* -- and worse, the failed
attempt leaves a **zero-byte file at the destination** *(measured)*, which is the
single most dangerous artifact this command could produce. A zero-byte file is
indistinguishable from a backup until the day someone needs it.

So the copy is taken from the one writer factory, which is also the correct
answer for a second reason that has nothing to do with the pragma: the writer
factory takes the exclusive lock before it returns, so no sync run can be
committing while the copy is made. Consistency is a consequence of the lock
rather than of timing.

The handle is `copying_writer`, not `writer`: a copy runs at ANY schema version.
`VACUUM INTO` interprets nothing, so the check that stops a process serving a
schema it does not recognize has nothing to protect here -- while refusing would
leave `cp` as the only way to copy such a store, and `cp` drops the WAL. The
reasoning lives on `copying_writer` in `store/connection.py`, beside the norm it
rules at the edge of.

Verification reads the copy back with `require_supported_schema=False` for the
same reason: a copy taken at an unservable version is a good backup, and the
step that proves it is one must be able to open it.

WHAT THE COPY IS

A single file. `VACUUM INTO` folds the WAL's contents into it, so the copy needs
no `-wal` or `-shm` companion -- measured in `tests/store/test_backup.py`
against a source holding a 2 MB hot WAL, whose 300 rows all appear in the copy
while a plain `cp` of `store.db` is short of them. That is what makes this better
than `cp`: copying `store.db` alone silently loses everything still in the WAL,
and copying all three files is only consistent if nothing is mid-commit.

The copy is encrypted with the same key (the `SQLite format 3` magic is absent
and a known plaintext is not recoverable from its raw bytes), and a wrong key
raises `SQLITE_NOTADB` rather than returning garbage *(all measured)*.

`VACUUM INTO` refuses an existing destination itself *(measured: SQLITE_ERROR,
"output file already exists")*. This module checks first anyway, so the operator
gets a sentence naming the file instead of a driver error.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path

import sqlcipher3.dbapi2 as dbapi2

from bankmachine.config import Config
from bankmachine.logging_setup import get_logger
from bankmachine.store import connection
from bankmachine.store.connection import StoreError

logger = get_logger("store.backup")


class BackupDestinationExistsError(StoreError):
    """The destination is already there. This command never overwrites a backup."""


class BackupDestinationUnusableError(StoreError):
    """The destination path cannot be written to -- its parent is not a directory.

    Distinct from `BackupDestinationExistsError` because the operator's next move
    is the opposite one: that error means "pick another path", this one means
    "the path you picked has no directory". Reporting a missing directory as an
    existing file sends them looking for a file that is not there.
    """


class BackupNotWrittenError(StoreError):
    """The copy could not be written.

    Named rather than letting the driver's `OperationalError` out, because every
    other failure in this module is a `StoreError` with a remedy in it, and a
    caller that catches `StoreError` would otherwise see this one as a crash.
    The CLI's own handler catches `StoreError`; a raw driver error would print a
    traceback instead of a sentence.
    """


class BackupUnverifiedError(StoreError):
    """The copy was written but could not be read back as a datastore.

    Carries the path, because the unusable file is still on disk: deleting it is
    not this command's call, and an operator told only "backup failed" would not
    know there is a plausible-looking file to remove.
    """


@dataclass(frozen=True, slots=True)
class BackupReport:
    """What `store backup` reports. A report exists only for a VERIFIED copy."""

    destination: Path
    bytes_written: int
    schema_version: int
    source_path: Path


def _discard(destination: Path) -> str:
    """Remove a failed copy, and report what was actually there.

    Returns a sentence for the error message rather than a bool: "no partial
    file was left behind" and "a partial file was removed" are different facts
    about the run, and asserting the first when the second happened is the kind
    of confident wrongness this product is built against.
    """
    try:
        size = destination.stat().st_size
    except OSError:
        return "No partial file was left behind."
    try:
        destination.unlink()
    except OSError:  # pragma: no cover - the directory would have to change mid-run
        return f"A partial file of {size} bytes REMAINS at {destination} and is not a backup."
    return f"A partial file of {size} bytes was written and has been removed."


def back_up(config: Config, destination: Path) -> BackupReport:
    """Write a verified, consistent, encrypted copy of the datastore.

    Verification is not optional and not a flag. An unverified backup is a
    belief about a file, and the whole point of the file is the day the belief
    gets tested.
    """
    destination = destination.expanduser()
    if destination.exists():
        raise BackupDestinationExistsError(
            f"{destination} already exists; this command never overwrites a backup. "
            f"Choose another path, or move the existing file aside"
        )
    if not destination.parent.is_dir():
        raise BackupDestinationUnusableError(
            f"{destination.parent} is not a directory -- nothing creates a backup "
            f"directory implicitly, for the same reason nothing creates a datastore "
            f"implicitly: a typo'd path must be reported, not populated"
        )

    logger.info("backup starting: %s -> %s", config.datastore_path, destination)
    with connection.copying_writer(config) as conn:
        try:
            # A bound parameter, not an f-string: a destination path is operator
            # input and may contain a quote, and string-building the SQL would
            # make that a syntax error at best.
            conn.execute("VACUUM INTO ?", (str(destination),))
        except dbapi2.DatabaseError as exc:
            # Remove any partial or zero-byte destination BEFORE raising, rather
            # than trusting the driver to have cleaned up. `VACUUM INTO` does
            # tidy after itself on the paths measured here -- but it demonstrably
            # does NOT on all of them: from a read-role handle it fails
            # `SQLITE_READONLY` and leaves a zero-byte file (see the module
            # docstring). A zero-byte file is indistinguishable from a backup
            # until the day it is needed, so the guarantee has to be enforced by
            # this module rather than inherited from the driver's good behaviour.
            leftover = _discard(destination)
            # An unattended run would otherwise leave a "backup starting" line and
            # then silence, which reads exactly like a run still in progress.
            logger.error("backup failed: %s -- %s", destination, leftover)
            raise BackupNotWrittenError(
                f"could not write the copy to {destination} ({exc}). "
                f"{leftover} The datastore itself is untouched"
            ) from exc

    return _verify(config, destination)


def _verify(config: Config, destination: Path) -> BackupReport:
    """Open the copy as a datastore and confirm it is one.

    Reuses the ordinary read-role factory against a Config whose datastore path
    is the copy. That keeps connection construction in `store/connection.py`
    where the architecture norms put it -- the copy gets `mode=ro`, the same
    key, and the same schema check as any other read, with no second way to
    open a database anywhere in the tree.
    """
    as_copy = dataclasses.replace(config, datastore_path=destination)
    try:
        with connection.reader(as_copy, require_supported_schema=False) as conn:
            version = connection.read_schema_version(conn)
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    except StoreError as exc:
        logger.error(
            "backup NOT verified: %s could not be read back as a datastore (%s)", destination, exc
        )
        raise BackupUnverifiedError(
            f"the copy at {destination} could not be read back as a datastore ({exc}). "
            f"It is still on disk and is NOT a usable backup -- remove it rather than "
            f"leaving a file that looks like one"
        ) from exc

    if integrity != "ok":  # pragma: no cover - needs a copy corrupt in a way SQLCipher
        # still decrypts and opens, which no test here induces cheaply. Marked rather
        # than left looking covered: an unexercised branch that reads as tested is the
        # same defect as an unasserted guarantee, one layer down.
        logger.error("backup NOT verified: %s failed integrity_check (%s)", destination, integrity)
        raise BackupUnverifiedError(
            f"the copy at {destination} failed integrity_check ({integrity}). "
            f"It is still on disk and is NOT a usable backup"
        )

    if version is None:
        # `reader` raises on an unsupported version before reaching here, so this
        # is unreachable today. It is written out rather than cast away because
        # the alternative is coercing None to an int, and the value being coerced
        # would be the one that says whether the copy has a schema at all.
        raise BackupUnverifiedError(
            f"the copy at {destination} reports no schema version. "
            f"It is still on disk and is NOT a usable backup"
        )

    report = BackupReport(
        destination=destination,
        bytes_written=destination.stat().st_size,
        schema_version=version,
        source_path=config.datastore_path,
    )
    # An unattended run leaves only an exit code otherwise, and the log
    # directory is the one place an operator can ask what was taken and when.
    logger.info(
        "backup verified: %s (%d bytes, schema version %d)",
        report.destination,
        report.bytes_written,
        report.schema_version,
    )
    return report
