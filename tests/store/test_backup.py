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
    monkeypatch.setattr(connection, "writer", connection.reader)
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
    monkeypatch.setattr(connection, "writer", connection.reader)
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
