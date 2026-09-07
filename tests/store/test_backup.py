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
from bankmachine.store import connection
from bankmachine.store.backup import (
    BackupDestinationExistsError,
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


def test_it_refuses_a_destination_directory_that_does_not_exist(
    initialized_config: Config, tmp_path: Path
) -> None:
    destination = tmp_path / "nope" / "backup.db"
    with pytest.raises(BackupDestinationExistsError):
        back_up(initialized_config, destination)
    assert not destination.exists()


def test_it_leaves_no_zero_byte_file_when_it_cannot_write(
    initialized_config: Config, tmp_path: Path
) -> None:
    """The hazard this command exists to avoid producing.

    A read-role handle fails VACUUM INTO with SQLITE_READONLY *and leaves a
    zero-byte destination behind* -- a file indistinguishable from a backup
    until the day it is needed. Whatever the failure, no file may be left.
    """
    unwritable = tmp_path / "locked"
    unwritable.mkdir()
    unwritable.chmod(0o500)
    destination = unwritable / "backup.db"
    try:
        with pytest.raises(StoreError):
            back_up(initialized_config, destination)
        assert not destination.exists(), "a failed backup left a file that looks like one"
    finally:
        unwritable.chmod(0o700)


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
    initialized_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    with pytest.raises(BackupUnverifiedError):
        back_up(initialized_config, tmp_path / "backup.db")
