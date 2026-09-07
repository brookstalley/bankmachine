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

WHAT THE COPY IS

A single file. `VACUUM INTO` folds the WAL's contents into it, so the copy needs
no `-wal` or `-shm` companion -- measured against a source holding a 2 MB hot
WAL, whose 501 rows all appear in a 16 KB copy. That is what makes this better
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
from bankmachine.store import connection
from bankmachine.store.connection import StoreError


class BackupDestinationExistsError(StoreError):
    """The destination is already there. This command never overwrites a backup."""


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
        raise BackupDestinationExistsError(
            f"{destination.parent} is not a directory -- nothing creates a backup "
            f"directory implicitly, for the same reason nothing creates a datastore "
            f"implicitly: a typo'd path must be reported, not populated"
        )

    with connection.writer(config) as conn:
        try:
            # A bound parameter, not an f-string: a destination path is operator
            # input and may contain a quote, and string-building the SQL would
            # make that a syntax error at best.
            conn.execute("VACUUM INTO ?", (str(destination),))
        except dbapi2.DatabaseError as exc:
            # The destination is checked for existence above, so what reaches
            # here is a filesystem refusal: an unwritable directory, a full
            # disk, a path that is not a file. `VACUUM INTO` cleans up after
            # itself on this path -- measured: no partial file is left -- but
            # say so rather than leaving the operator to wonder.
            raise BackupNotWrittenError(
                f"could not write the copy to {destination} ({exc}). No partial file "
                f"was left behind; the datastore itself is untouched"
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
        with connection.reader(as_copy) as conn:
            version = connection.read_schema_version(conn)
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    except StoreError as exc:
        raise BackupUnverifiedError(
            f"the copy at {destination} could not be read back as a datastore ({exc}). "
            f"It is still on disk and is NOT a usable backup -- remove it rather than "
            f"leaving a file that looks like one"
        ) from exc

    if integrity != "ok":
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

    return BackupReport(
        destination=destination,
        bytes_written=destination.stat().st_size,
        schema_version=version,
        source_path=config.datastore_path,
    )
