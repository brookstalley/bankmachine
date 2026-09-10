"""`store backup`: a copy that is consistent, encrypted, and checked.

The claim under test is not "a file appeared". It is that the copy carries data
that was still in the WAL when the backup ran, that it is ciphertext, that it
opens with the datastore key and refuses a wrong one, and that the command
refuses every way of quietly producing something that is not a backup.

The WAL case is the one that justifies the command existing at all: `cp
store.db` passes a naive test and loses uncommitted-to-main-db rows, so the
test writes enough rows to leave a WAL and then asserts the copy has them.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlcipher3.dbapi2 as dbapi2
from sqlalchemy import insert

from bankmachine.config import Config
from bankmachine.secrets import get_datastore_key
from bankmachine.store import backup as backup_module
from bankmachine.store import connection
from bankmachine.store.backup import (
    BackupDestinationExistsError,
    BackupDestinationUnusableError,
    BackupUnverifiedError,
    back_up,
)
from bankmachine.store.connection import StoreError
from bankmachine.store.engine import transaction, writer_engine
from bankmachine.store.schema import derivation_versions


def _fill(config: Config, rows: int) -> None:
    """Write enough rows that the WAL is non-empty when the backup is taken."""
    now = datetime.now(UTC)
    with writer_engine(config) as engine, engine.connect() as conn, transaction(conn):
        for i in range(rows):
            conn.execute(
                insert(derivation_versions).values(
                    version=i + 1, description=f"probe {i}", first_used_at=now
                )
            )


def test_the_copy_carries_rows_that_are_still_in_an_uncheckpointed_wal(
    initialized_config: Config, tmp_path: Path
) -> None:
    """The whole justification for this command over `cp store.db`.

    A cleanly-closed writer checkpoints and removes the WAL, so reaching the
    state that matters takes a second connection held open across the write --
    which is not contrivance but the product's ordinary shape: the MCP reader
    lives as long as its client while the scheduled sync writes underneath it.
    The killed-writer case of AC-2.5 leaves the same uncheckpointed WAL.

    In that state `cp store.db` copies a file missing these rows.
    """
    wal = initialized_config.datastore_path.with_name(
        initialized_config.datastore_path.name + "-wal"
    )
    destination = tmp_path / "backup.db"

    with connection.reader(initialized_config):
        _fill(initialized_config, 300)
        assert wal.exists() and wal.stat().st_size > 0, (
            "no uncheckpointed WAL, so this test would pass against a plain file copy "
            "and would prove nothing"
        )
        # The negative control. Copy ONLY store.db, the way `cp` would, and
        # confirm it is short -- otherwise every assertion below would pass
        # against a plain file copy and this test would be measuring nothing.
        naive = tmp_path / "naive-cp.db"
        naive.write_bytes(initialized_config.datastore_path.read_bytes())

        report = back_up(initialized_config, destination)

    assert report.destination == destination
    assert report.bytes_written > 0
    # The copy is a single file: VACUUM INTO folds the WAL in, so a restore
    # needs no -wal or -shm companion.
    assert not destination.with_name(destination.name + "-wal").exists()

    as_copy = dataclasses.replace(initialized_config, datastore_path=destination)
    with connection.reader(as_copy) as conn:
        count = conn.execute("SELECT count(*) FROM derivation_versions").fetchone()[0]
    assert count == 300, "the copy lost rows that were still in the source's WAL"

    as_naive = dataclasses.replace(initialized_config, datastore_path=naive)
    with connection.reader(as_naive) as conn:
        naive_count = conn.execute("SELECT count(*) FROM derivation_versions").fetchone()[0]
    assert naive_count < 300, (
        f"a plain copy of store.db already had all {naive_count} rows, so the WAL held "
        f"nothing and this test cannot tell `store backup` apart from `cp`"
    )


def test_a_successful_backup_records_what_it_took_and_where(
    initialized_config: Config, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The log directory must be able to answer "was a backup taken, of what, to where".

    `operational-spec.md` § Monitoring points an operator at the log directory for
    exactly this question, so the records are part of the contract rather than
    debugging residue.
    """
    _fill(initialized_config, 5)
    destination = tmp_path / "backup.db"
    with caplog.at_level("INFO", logger="bankmachine"):
        report = back_up(initialized_config, destination)

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert str(initialized_config.datastore_path) in logged
    assert str(destination) in logged
    assert "backup verified" in logged
    assert str(report.bytes_written) in logged


def test_the_copy_is_ciphertext(initialized_config: Config, tmp_path: Path) -> None:
    _fill(initialized_config, 5)
    destination = tmp_path / "backup.db"
    back_up(initialized_config, destination)

    raw = destination.read_bytes()
    assert b"SQLite format 3" not in raw[:32], "the copy has a plaintext SQLite header"
    assert b"probe 1" not in raw, "a known plaintext is recoverable from the copy's raw bytes"


def test_a_wrong_key_will_not_open_the_copy(initialized_config: Config, tmp_path: Path) -> None:
    destination = tmp_path / "backup.db"
    back_up(initialized_config, destination)

    real = get_datastore_key(initialized_config)
    wrong = ("b" * 64) if real != "b" * 64 else "c" * 64
    conn = dbapi2.connect(f"file:{destination}?mode=ro", uri=True, isolation_level=None)
    try:
        conn.execute(f"PRAGMA key = \"x'{wrong}'\"")
        with pytest.raises(dbapi2.DatabaseError):
            conn.execute("SELECT count(*) FROM derivation_versions").fetchone()
    finally:
        conn.close()


def test_it_refuses_an_existing_destination(initialized_config: Config, tmp_path: Path) -> None:
    destination = tmp_path / "backup.db"
    destination.write_bytes(b"an earlier backup")

    with pytest.raises(BackupDestinationExistsError):
        back_up(initialized_config, destination)

    assert destination.read_bytes() == b"an earlier backup", "an existing backup was overwritten"


def test_a_missing_destination_directory_is_reported_as_unusable_not_as_existing(
    initialized_config: Config, tmp_path: Path
) -> None:
    """The two refusals are different errors because the remedies are opposite.

    "Already exists" means pick another path; "unusable" means the path you
    picked has no directory. Reporting the second as the first sends the
    operator looking for a file that is not there.
    """
    destination = tmp_path / "nope" / "backup.db"
    with pytest.raises(BackupDestinationUnusableError):
        back_up(initialized_config, destination)
    assert not destination.exists()


def test_it_removes_the_zero_byte_file_a_failed_vacuum_leaves_behind(
    initialized_config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The measured hazard, reproduced in a directory that CAN be written to.

    This is the case that matters and the earlier version of this test could not
    reach it: asserting `not destination.exists()` inside a chmod-0o500
    directory is true before `back_up` runs, so it held no matter what the code
    did. Here the directory is writable, so a leftover file is genuinely
    possible -- and swapping the writer factory for the read-role one reproduces
    the exact measured failure the module docstring records: VACUUM INTO fails
    SQLITE_READONLY and leaves a zero-byte destination.

    A zero-byte file is indistinguishable from a backup until the day it is
    needed, so the module must remove it rather than trusting the driver to.
    """
    monkeypatch.setattr(connection, "copying_writer", connection.reader)
    destination = tmp_path / "backup.db"

    # `match=` rather than a bare raises: operational-spec.md item 5 states as a
    # GUARANTEE that the message says which happened -- "no partial file was left"
    # and "a partial file was removed" are different facts about the run, and an
    # unasserted sentence is a guarantee a refactor can drop while staying green.
    with (
        caplog.at_level("ERROR", logger="bankmachine"),
        pytest.raises(
            StoreError, match="partial file of .* bytes was written and has been removed"
        ),
    ):
        back_up(initialized_config, destination)

    assert not destination.exists(), (
        "a failed backup left a file behind -- it is zero bytes and looks like a backup"
    )
    errors = "\n".join(r.getMessage() for r in caplog.records if r.levelname == "ERROR")
    assert "backup failed" in errors and str(destination) in errors, (
        "an unattended failure left a 'backup starting' line and then silence"
    )


def test_the_failure_reproduces_the_debris_when_the_module_does_not_clean_up(
    initialized_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative control for the test above.

    Without this, `test_it_removes_the_zero_byte_file...` would pass just as
    happily if VACUUM INTO had quietly stopped leaving debris -- and the unlink
    it exists to pin would be protecting nothing. Neutralize the cleanup and the
    zero-byte file must appear, which is what proves there was something to
    clean up.
    """
    monkeypatch.setattr(connection, "copying_writer", connection.reader)
    monkeypatch.setattr(backup_module, "_discard", lambda destination: "left in place.")
    destination = tmp_path / "backup.db"

    with pytest.raises(StoreError):
        back_up(initialized_config, destination)

    assert destination.exists() and destination.stat().st_size == 0, (
        "the driver no longer leaves a zero-byte file, so the cleanup this module "
        "performs is no longer pinned by the test above -- re-derive the hazard"
    )


def test_a_backup_is_itself_backup_able(initialized_config: Config, tmp_path: Path) -> None:
    """The restore path, in the only form a test can assert it.

    A backup nobody can open is the failure this command's verification step
    exists to catch, so the copy is opened through the ordinary reader -- the
    same route a restore would take.
    """
    _fill(initialized_config, 10)
    first = tmp_path / "backup.db"
    report = back_up(initialized_config, first)
    assert report.schema_version == connection.SUPPORTED_SCHEMA_VERSION

    as_copy = dataclasses.replace(initialized_config, datastore_path=first)
    with connection.reader(as_copy) as conn:
        rows = conn.execute("SELECT version FROM derivation_versions ORDER BY version").fetchall()
    assert [r[0] for r in rows] == list(range(1, 11))


def test_verification_rejects_a_copy_that_is_not_a_datastore(
    initialized_config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The verification step must actually be able to fail.

    Without this the `_verify` branch is unfalsified: every real backup passes,
    so a verification that silently always returned success would look
    identical. The copy is corrupted between write and check.
    """
    from bankmachine.store import backup as backup_module

    real_verify = backup_module._verify

    def corrupt_then_verify(config: Config, destination: Path) -> object:
        destination.write_bytes(b"not a database at all" * 100)
        return real_verify(config, destination)

    monkeypatch.setattr(backup_module, "_verify", corrupt_then_verify)
    destination = tmp_path / "backup.db"
    with (
        caplog.at_level("ERROR", logger="bankmachine"),
        pytest.raises(BackupUnverifiedError),
    ):
        back_up(initialized_config, destination)

    # The record, not just the raise. An unattended failure is diagnosed from the
    # log directory (operational-spec.md § Monitoring), so the record is part of
    # the contract -- and this test already executes the line that writes it.
    errors = "\n".join(r.getMessage() for r in caplog.records if r.levelname == "ERROR")
    assert "backup NOT verified" in errors and str(destination) in errors


# --------------------------------------------------------------------------
# A datastore is copyable at ANY schema version.
#
# The norm that a process refusing an unrecognized schema version is about
# ANSWERS: a reader misreads and returns wrong ones, a writer writes them down.
# `VACUUM INTO` produces no answers, so the check has nothing to protect here --
# and refusing left `cp` as the only way to copy such a store, which the WAL
# test at the top of this file measures to be lossy.
#
# 🔴 These stamp FORWARD. `schema_version` is an append-only record of applied
# migrations, so a store cannot be stamped back onto a version it already holds;
# a version above this build is the same "this build does not serve it" state
# and is the one reachable by writing a row. The genuinely-older store, which
# the recovery test needs because it runs the migration runner, is built by
# applying a truncated migration list instead.
# --------------------------------------------------------------------------


def _unservable(config: Config) -> int:
    """Put the store at a version this build does not serve, and return it."""
    version = connection.SUPPORTED_SCHEMA_VERSION + 1
    with connection.writer(config) as conn:
        conn.execute("BEGIN IMMEDIATE")
        connection.stamp_schema_version(conn, version)
        conn.execute("COMMIT")
    return version


def test_a_store_this_build_cannot_serve_is_still_backed_up(
    initialized_config: Config, tmp_path: Path
) -> None:
    """The case an operator reaches by upgrading the code before taking a backup."""
    _fill(initialized_config, 5)
    version = _unservable(initialized_config)

    # The precondition, asserted rather than assumed: the ordinary writer really
    # does refuse this store, so the backup below is exercising the exemption
    # and not passing because nothing was in the way.
    with pytest.raises(connection.SchemaVersionUnsupportedError):
        with connection.writer(initialized_config):
            pass

    report = back_up(initialized_config, tmp_path / "backup.db")

    assert report.schema_version == version, (
        "the copy must carry the version it was taken at -- a backup reporting the "
        "version this build serves would be describing a store that does not exist"
    )
    assert report.destination.exists()


def test_the_wal_guarantee_holds_at_an_unservable_version(
    initialized_config: Config, tmp_path: Path
) -> None:
    """🔴 The whole reason this exemption is worth having.

    `cp` is what refusing leaves the operator, and `cp` drops the WAL. If the
    copy taken at an unservable version lost the WAL too, the exemption would
    buy nothing over the workaround it replaces -- so the negative control from
    the top of this file is re-run here rather than assumed to carry over.
    """
    wal = initialized_config.datastore_path.with_name(
        initialized_config.datastore_path.name + "-wal"
    )
    destination = tmp_path / "backup.db"

    with connection.reader(initialized_config):
        _fill(initialized_config, 300)
        version = _unservable(initialized_config)
        assert wal.exists() and wal.stat().st_size > 0, (
            "no uncheckpointed WAL, so this test would pass against a plain file copy"
        )
        naive = tmp_path / "naive-cp.db"
        naive.write_bytes(initialized_config.datastore_path.read_bytes())

        report = back_up(initialized_config, destination)

    assert report.schema_version == version

    as_copy = dataclasses.replace(initialized_config, datastore_path=destination)
    with connection.reader(as_copy, require_supported_schema=False) as conn:
        copied = conn.execute("SELECT count(*) FROM derivation_versions").fetchone()[0]

    as_naive = dataclasses.replace(initialized_config, datastore_path=naive)
    with connection.reader(as_naive, require_supported_schema=False) as conn:
        by_cp = conn.execute("SELECT count(*) FROM derivation_versions").fetchone()[0]

    assert copied == 300
    assert by_cp < copied, (
        "the `cp` control kept every row, so this store had no hot WAL and the test "
        "proves nothing about the guarantee it exists to check"
    )


def test_the_documented_recovery_path_is_walked_end_to_end(
    config: Config, tmp_path: Path
) -> None:
    """🔴 `operational-spec.md` § Rollback: "Recovery is restore-from-backup".

    A documented remedy is a claim, and this walks it against a genuinely older
    store -- built by applying a truncated migration list, so the physical
    schema really is short of the column the pending migration adds and the
    runner really does have work to do.

    Rows go in through `initializing_writer`, the one handle permitted to open a
    version this build does not serve; the ordinary writer refuses such a store,
    which is the norm working rather than an obstacle to route around.

    Nothing here is a special restore path. That is the point.
    """
    from bankmachine.secrets import generate_datastore_key, set_datastore_key
    from bankmachine.store.migrations import MIGRATIONS, migrate

    set_datastore_key(config, generate_datastore_key())
    short = list(MIGRATIONS[:-1])
    assert migrate(config, migrations=short) == [step.version for step in short]

    now = datetime.now(UTC)
    with connection.initializing_writer(config) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for i in range(12):
            conn.execute(
                "INSERT INTO derivation_versions (version, description, first_used_at) "
                "VALUES (?, ?, ?)",
                (i + 1, f"probe {i}", now.isoformat()),
            )
        conn.execute("COMMIT")

    backup_path = tmp_path / "before-upgrade.db"
    report = back_up(config, backup_path)
    assert report.schema_version == short[-1].version

    restored_path = tmp_path / "restored" / "store.db"
    restored_path.parent.mkdir()
    restored_path.write_bytes(backup_path.read_bytes())
    restored = dataclasses.replace(config, datastore_path=restored_path)

    assert migrate(restored) == [MIGRATIONS[-1].version], (
        "the restored copy must need exactly the migration it was short of"
    )
    with connection.reader(restored) as conn:
        assert conn.execute("SELECT count(*) FROM derivation_versions").fetchone()[0] == 12
        assert connection.read_schema_version(conn) == connection.SUPPORTED_SCHEMA_VERSION


def test_the_destination_norms_hold_at_an_unservable_version(
    initialized_config: Config, tmp_path: Path
) -> None:
    """`operational-spec.md` § Direction: never created implicitly, never overwritten.

    🔴 This one cannot be made red by changing which handle `back_up` opens,
    and saying so is the honest form of it: both refusals sit ABOVE the handle,
    so they hold no matter what the handle does. What it guards is the change
    that would move them below it -- reordering the checks after the open, or
    folding them into the copy step -- which is how a destination norm quietly
    stops applying to the one path that reaches an unservable store.
    """
    _unservable(initialized_config)

    existing = tmp_path / "taken.db"
    existing.write_bytes(b"")
    with pytest.raises(BackupDestinationExistsError):
        back_up(initialized_config, existing)

    with pytest.raises(BackupDestinationUnusableError):
        back_up(initialized_config, tmp_path / "no-such-dir" / "backup.db")
