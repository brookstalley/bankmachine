"""The four architecture norms, as tests that fail when the norm is violated.

These are the tests issue #1 asks for. Each one is written against the measured
behaviour recorded in `.prawduct/artifacts/architecture.md`, and each has been
verified to go red when its norm is deliberately broken -- a norm test that has
never been red is a claim, not a check.
"""

from __future__ import annotations

import ast
import inspect
import os
import pathlib
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from bankmachine.config import Config
from bankmachine.secrets import generate_datastore_key, get_datastore_key, set_datastore_key
from bankmachine.store import connection
from bankmachine.store.connection import (
    AnotherWriterRunningError,
    DatastoreMissingError,
    SchemaVersionUnsupportedError,
    initializing_writer,
    reader,
    writer,
)
from bankmachine.store.migrations import Migration, migrate

# --------------------------------------------------------------------------
# Norm 1: every writable handle comes from the one writer factory, and that
# factory takes the exclusive lock before it returns.
# --------------------------------------------------------------------------


def test_second_writer_process_refuses_while_the_first_holds_the_lock(
    initialized_config: Config,
) -> None:
    holder = _spawn_lock_holder(initialized_config)
    try:
        _wait_for(initialized_config.lock_path, holder)
        with pytest.raises(AnotherWriterRunningError), writer(initialized_config):
            pass
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_the_lock_is_released_when_the_holding_process_is_killed(
    initialized_config: Config,
) -> None:
    """The kernel releases an advisory lock on death. A lease row would not.

    AC-2.5 requires a process killed mid-run to be recoverable, so this is the
    property that makes `flock` the right mechanism rather than a table.
    """
    holder = _spawn_lock_holder(initialized_config)
    _wait_for(initialized_config.lock_path, holder)
    holder.kill()
    holder.wait(timeout=10)

    with writer(initialized_config) as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)


def test_the_writer_factory_holds_the_lock_for_the_whole_handle(
    initialized_config: Config,
) -> None:
    assert not connection.writer_lock_held(initialized_config)
    with writer(initialized_config):
        assert connection.writer_lock_held(initialized_config)
    assert not connection.writer_lock_held(initialized_config)


# --------------------------------------------------------------------------
# Norm 2: read-role handles open mode=ro, hold no snapshot beyond the statement
# that needs it, and never fall back to a writable handle.
# --------------------------------------------------------------------------


def test_a_read_role_handle_refuses_a_write(initialized_config: Config) -> None:
    with (
        reader(initialized_config) as conn,
        pytest.raises(Exception, match="readonly|read-only|query_only|attempt to write"),
    ):
        conn.execute("CREATE TABLE nope (a INTEGER)")


def test_a_read_role_handle_still_refuses_after_pragma_query_only_off(
    initialized_config: Config,
) -> None:
    """The case the on-only test cannot see.

    `query_only` is a reversible session flag, and `sync shell` ships the exact
    surface that can type `PRAGMA query_only=OFF`. Under `mode=ro` the refusal
    lives in the file handle, where SQL cannot reach it -- so this test is the
    one that distinguishes the real guarantee from the reversible one.
    """
    with reader(initialized_config) as conn:
        conn.execute("PRAGMA query_only = OFF")
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 0
        with pytest.raises(Exception, match="readonly|read-only|attempt to write"):
            conn.execute("CREATE TABLE nope (a INTEGER)")


def test_a_reader_sitting_between_statements_does_not_starve_the_checkpointer(
    initialized_config: Config,
) -> None:
    """A passive checkpoint moves every frame while a reader sits idle.

    The measured failure this guards against is 0 of 93 frames moved with one
    reader holding an open read transaction, and 93 of 93 the instant it
    released. An MCP session lasts as long as its client and a shell sits open
    overnight, so an idle reader must pin nothing.
    """
    with writer(initialized_config) as w:
        _create_filler(w)
        with reader(initialized_config) as r:
            r.execute("SELECT COUNT(*) FROM wal_filler").fetchone()
            # The reader is now idle -- between statements, holding no snapshot.
            # The writer appends AFTER that statement: frames the reader would
            # pin if its snapshot outlived the statement that opened it.
            _fill_wal(w)
            busy, log_frames, checkpointed = w.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            assert log_frames > 0, "the WAL held no frames, so this proved nothing"
            assert busy == 0
            assert checkpointed == log_frames
            assert r.execute("SELECT COUNT(*) FROM wal_filler").fetchone()[0] > 0


def test_the_checkpoint_probe_can_actually_detect_a_pinned_snapshot(
    initialized_config: Config,
) -> None:
    """The negative control for the test above.

    A probe that only ever confirms what was expected is the one to distrust.
    This holds a read transaction open deliberately and asserts the checkpoint
    starves -- if this passes and the test above also passes, the mechanism is
    being measured rather than assumed.
    """
    with writer(initialized_config) as w:
        _create_filler(w)
        with reader(initialized_config) as r:
            r.execute("BEGIN")
            r.execute("SELECT COUNT(*) FROM wal_filler").fetchone()
            try:
                _fill_wal(w)
                _busy, log_frames, checkpointed = w.execute(
                    "PRAGMA wal_checkpoint(PASSIVE)"
                ).fetchone()
                assert log_frames > 0
                assert checkpointed < log_frames, (
                    "a pinned read snapshot did not starve the checkpointer, so the "
                    "positive test above cannot distinguish a released snapshot from a held one"
                )
            finally:
                r.execute("ROLLBACK")


def test_the_read_role_opens_read_only_at_the_file() -> None:
    """The structural half of norm 2, separate from the behaviour it produces.

    Behaviour alone cannot tell a `mode=ro` handle from a read-write one that
    merely has `query_only` set, because both refuse the first write. The mode
    is the guarantee, so the mode is asserted.
    """
    assert connection.READ_ROLE_MODE == "ro"


def test_an_unopenable_datastore_raises_and_names_the_state(config: Config) -> None:
    """The no-fallback clause: a loud error, never a retry on a writable handle."""
    set_datastore_key(config, generate_datastore_key())
    config.datastore_path.write_bytes(b"not a database, and not openable read-only either")
    with pytest.raises(connection.StoreError) as excinfo, reader(config):
        pass
    assert str(excinfo.value), "the failure was raised without naming the state"


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes to a read-only directory anyway")
def test_the_measured_unreadable_edge_is_not_reported_as_a_bad_key(
    initialized_config: Config,
) -> None:
    """The one norm case that had no staged test, staged.

    The state is a hot WAL from a killed writer, no `-shm` file, in a directory
    the reader cannot write to. SQLite opens lazily, so `connect()` succeeds and
    the failure lands on the first read as `SQLITE_CANTOPEN` -- it cannot build
    the shared-memory index the WAL needs.

    What this asserts is the diagnosis, not merely that something was raised.
    Reported as a rejected key, this sends the operator to restore a keychain
    entry that was never the problem, and the datastore they would restore it
    for is fine -- checkpointing the WAL is all it needs. That misdiagnosis is
    the exact failure `DatastoreKeyRejectedError` was introduced to prevent, so
    it must not be the thing that produces it.
    """
    directory = initialized_config.datastore_path.parent
    _kill_a_writer_holding_a_hot_wal(initialized_config)
    assert initialized_config.datastore_path.with_suffix(".db-wal").stat().st_size > 0

    initialized_config.datastore_path.with_suffix(".db-shm").unlink(missing_ok=True)
    directory.chmod(0o555)
    try:
        with (
            pytest.raises(connection.DatastoreUnreadableError) as excinfo,
            reader(initialized_config),
        ):
            pass
    finally:
        directory.chmod(0o755)

    message = str(excinfo.value)
    assert "key is not implicated" in message
    assert "hot WAL" in message
    # And the store was never the problem: it reads once the directory does.
    with reader(initialized_config) as conn:
        assert conn.execute("SELECT COUNT(*) FROM hot_wal").fetchone()[0] == 400


@pytest.mark.skipif(os.geteuid() == 0, reason="root opens a mode-000 file anyway")
def test_a_lock_file_this_process_cannot_open_is_a_named_error(
    initialized_config: Config,
) -> None:
    """A filesystem refusal arrives as `OSError`, not from the driver.

    Nothing else in the store layer would map it, so without this it escapes
    every handler that catches `StoreError` -- and the caller most exposed is
    `inspect`, whose contract is to report a state rather than raise on one.
    """
    initialized_config.lock_path.touch()
    initialized_config.lock_path.chmod(0o000)
    try:
        with pytest.raises(connection.DatastorePathUnusableError) as excinfo:
            connection.writer_lock_held(initialized_config)
    finally:
        initialized_config.lock_path.chmod(0o600)

    assert str(initialized_config.lock_path) in str(excinfo.value)


@pytest.mark.skipif(os.geteuid() == 0, reason="root opens a mode-000 file anyway")
def test_inspect_reports_an_unopenable_lock_file_rather_than_raising(
    initialized_config: Config,
) -> None:
    """AC-ARCH.3 / AC-4.4: `inspect` reports a state, it does not raise on one.

    The MCP server starts on this branch. A datastore restored from a backup
    under another user, or one on a volume this process cannot write to, reaches
    it -- and a traceback out of the function whose whole job is to describe
    that situation is the failure this asserts against.
    """
    initialized_config.lock_path.touch()
    initialized_config.lock_path.chmod(0o000)
    try:
        status = connection.inspect(initialized_config)
    finally:
        initialized_config.lock_path.chmod(0o600)

    assert status.exists
    assert not status.readable
    assert not status.healthy
    assert status.problem is not None
    assert str(initialized_config.lock_path) in status.problem


def _kill_a_writer_holding_a_hot_wal(config: Config) -> None:
    """Leave a WAL behind that no clean close ever checkpointed."""
    killed = subprocess.run(
        [sys.executable, "-c", _HOT_WAL_WRITER],
        env={
            **os.environ,
            "BANKMACHINE_DATASTORE_PATH": str(config.datastore_path),
            "BANKMACHINE_KEYCHAIN_SERVICE": config.keychain_service,
            "BANKMACHINE_LOG_DIR": str(config.log_dir),
            "BANKMACHINE_ENVIRONMENT": config.environment,
        },
        capture_output=True,
        text=True,
    )
    assert killed.returncode == 9, f"the writer was not killed mid-flight: {killed.stderr}"


# --------------------------------------------------------------------------
# Norm 3: no component creates the datastore implicitly.
# --------------------------------------------------------------------------


def test_a_writer_refuses_a_missing_datastore_and_creates_nothing(config: Config) -> None:
    set_datastore_key(config, generate_datastore_key())
    with pytest.raises(DatastoreMissingError), writer(config):
        pass
    assert not config.datastore_path.exists()


def test_a_reader_refuses_a_missing_datastore_and_creates_nothing(config: Config) -> None:
    set_datastore_key(config, generate_datastore_key())
    with pytest.raises(DatastoreMissingError), reader(config):
        pass
    assert not config.datastore_path.exists()


def test_status_reports_a_missing_datastore_as_missing_not_empty(config: Config) -> None:
    """AC-ARCH.3: the state is reported, and reporting it does not erase it."""
    status = connection.inspect(config)
    assert status.exists is False
    assert status.healthy is False
    assert "missing" in (status.problem or "")
    assert not config.datastore_path.exists()


def test_the_non_creating_writer_opens_a_mode_that_cannot_create() -> None:
    """The structural half of norm 3.

    The existence check above produces the good error message; this constant is
    what holds the norm if the check is ever removed. Both layers are asserted
    because losing either one is a real regression, even while the other still
    makes the system behave.
    """
    assert connection.WRITER_MODE == "rw"
    assert connection.CREATING_WRITER_MODE == "rwc"


def test_mode_rw_refuses_to_create_and_a_plain_connect_does_not(tmp_path: Path) -> None:
    """The measured fact norm 3 rests on, with the failure it prevents beside it."""
    from sqlcipher3 import dbapi2

    guarded = tmp_path / "guarded.db"
    with pytest.raises(dbapi2.OperationalError):
        dbapi2.connect(f"file:{guarded}?mode={connection.WRITER_MODE}", uri=True)
    assert not guarded.exists()

    # The negative control: this is exactly what the norm exists to prevent.
    unguarded = tmp_path / "unguarded.db"
    dbapi2.connect(str(unguarded)).close()
    assert unguarded.exists()


def test_only_the_initializing_writer_creates_the_file(config: Config) -> None:
    set_datastore_key(config, generate_datastore_key())
    with initializing_writer(config) as conn:
        conn.execute("SELECT 1").fetchone()
    assert config.datastore_path.exists()


# --------------------------------------------------------------------------
# Norm 4: a process that does not recognize the schema version refuses to serve.
#
# 🔴 The three tests below cover the STORE layer -- reader, writer, and what
# `inspect` reports. The norm's other clause is about the surface above them:
# `architecture.md` § Direction and `api-contract.md` § Hard errors both say such
# a store "answers `get_pipeline_health` with a hard error instead of serving
# queries". Nothing covered that clause, and the code did the opposite for two
# years of migrations -- see `tests/test_unservable_datastore.py`, which owns it
# rather than duplicating these three. Kept as a pointer because the gap was
# invisible precisely BECAUSE this section looked complete.
# --------------------------------------------------------------------------


def test_a_reader_refuses_an_unrecognized_schema_version(initialized_config: Config) -> None:
    future = connection.SUPPORTED_SCHEMA_VERSION + 1
    with writer(initialized_config) as conn:
        conn.execute("BEGIN IMMEDIATE")
        connection.stamp_schema_version(conn, future)
        conn.execute("COMMIT")

    with (
        pytest.raises(SchemaVersionUnsupportedError, match=str(future)),
        reader(initialized_config),
    ):
        pass


def test_a_writer_refuses_an_unrecognized_schema_version(initialized_config: Config) -> None:
    # The reader's case is above. This one matters more: a reader that misreads
    # a schema it does not recognize returns wrong answers, and a writer that
    # misunderstands one writes them down. `store rebuild` is the first writer
    # that is not the migration runner, and the runner takes the initializing
    # writer precisely because opening an old version is its job.
    future = connection.SUPPORTED_SCHEMA_VERSION + 1
    with writer(initialized_config) as conn:
        conn.execute("BEGIN IMMEDIATE")
        connection.stamp_schema_version(conn, future)
        conn.execute("COMMIT")

    with (
        pytest.raises(SchemaVersionUnsupportedError, match=str(future)),
        writer(initialized_config),
    ):
        pass


def test_only_the_two_named_roles_are_exempt_from_the_schema_check(
    initialized_config: Config,
) -> None:
    """🔴 The exemptions are discovered, not listed, so a third one cannot arrive quietly.

    Two handles are permitted to open a version this build does not serve, and
    each has a reason the norm's own why does not reach. `initializing_writer`
    is the migration runner's: bringing an old datastore forward is the one job
    that must open an old version. `copying_writer` is `store backup`'s:
    `VACUUM INTO` copies pages of ciphertext and answers no question, so there
    is no answer for an unrecognized schema to make wrong -- and refusing left
    `cp`, which drops the WAL.

    This walks the module rather than naming the handles it expects to refuse.
    An enumeration would pass unchanged on the day someone adds a third handle,
    which is the only day it matters.
    """
    exempt = {"initializing_writer", "copying_writer"}

    handles = {
        name
        for name, value in vars(connection).items()
        if not name.startswith("_")
        and callable(value)
        and hasattr(value, "__wrapped__")
        and "config" in inspect.signature(value).parameters
    }
    assert exempt <= handles, f"a named exemption no longer exists: {exempt - handles}"

    future = connection.SUPPORTED_SCHEMA_VERSION + 1
    with writer(initialized_config) as conn:
        conn.execute("BEGIN IMMEDIATE")
        connection.stamp_schema_version(conn, future)
        conn.execute("COMMIT")

    for name in sorted(handles - exempt):
        with pytest.raises(SchemaVersionUnsupportedError, match=str(future)):
            with getattr(connection, name)(initialized_config):
                pass

    for name in sorted(exempt):
        with getattr(connection, name)(initialized_config) as conn:
            assert connection.read_schema_version(conn) == future

    # 🔴 The behavioural walk above can only reach handles it knows how to CALL,
    # which is every one taking a `Config` and no other. A handle with a
    # different signature would open the datastore and never be tried. So the
    # claim is closed at the source: any function that opens through `_writer`
    # either checks the schema or is one of the two named roles, whatever its
    # parameters look like.
    tree = ast.parse(pathlib.Path(connection.__file__).read_text(encoding="utf-8"))
    opens_the_datastore = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(call.func, ast.Name) and call.func.id == "_writer"
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
        )
    }
    assert opens_the_datastore, "the source walk found no writer at all, so it proves nothing"

    for name, node in sorted(opens_the_datastore.items()):
        checks = any(
            isinstance(call.func, ast.Name) and call.func.id == "_require_supported_schema"
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
        )
        assert checks or name in exempt, (
            f"{name}() opens the datastore through the writer factory without checking the "
            f"schema version, and is not one of the two roles ruled exempt. Either it checks, "
            f"or the ruling in architecture.md grows a third entry and this set grows with it"
        )


def test_status_reports_an_unrecognized_schema_version_as_unhealthy(
    initialized_config: Config,
) -> None:
    with writer(initialized_config) as conn:
        conn.execute("BEGIN IMMEDIATE")
        connection.stamp_schema_version(conn, connection.SUPPORTED_SCHEMA_VERSION + 1)
        conn.execute("COMMIT")

    status = connection.inspect(initialized_config)
    assert status.healthy is False
    assert "not served by this build" in (status.problem or "")


def test_a_datastore_with_no_schema_version_is_not_healthy(config: Config) -> None:
    set_datastore_key(config, generate_datastore_key())
    with initializing_writer(config) as conn:
        conn.execute("SELECT 1").fetchone()

    status = connection.inspect(config)
    assert status.exists is True
    assert status.schema_version is None
    assert status.healthy is False


# --------------------------------------------------------------------------
# Migration atomicity: DDL and version stamp commit together, or not at all.
# --------------------------------------------------------------------------


def test_a_migration_killed_between_its_ddl_and_its_stamp_leaves_nothing_healthy(
    config: Config,
) -> None:
    """The interrupted-migration case, run as a real process that really dies.

    The child applies the DDL inside the migration's transaction and then exits
    without ever reaching the version stamp. What must not survive is a
    datastore that reports healthy while missing what its version claims.
    """
    key = generate_datastore_key()
    set_datastore_key(config, key)

    result = subprocess.run(
        [sys.executable, "-c", _KILL_MID_MIGRATION],
        env={
            **os.environ,
            "BANKMACHINE_DATASTORE_PATH": str(config.datastore_path),
            "BANKMACHINE_KEYCHAIN_SERVICE": config.keychain_service,
            "BANKMACHINE_LOG_DIR": str(config.log_dir),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0, result.stdout

    status = connection.inspect(config)
    assert status.healthy is False
    assert status.schema_version is None
    with reader(config, require_supported_schema=False) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master").fetchall()}
    assert "half_migrated" not in tables, "uncommitted DDL survived the kill"


def test_migrations_are_idempotent(initialized_config: Config) -> None:
    assert migrate(initialized_config) == []
    status = connection.inspect(initialized_config)
    assert status.schema_version == connection.SUPPORTED_SCHEMA_VERSION


def test_a_failing_migration_rolls_its_ddl_back(config: Config) -> None:
    set_datastore_key(config, generate_datastore_key())

    def _explode(conn: connection.Connection) -> None:
        conn.execute("CREATE TABLE doomed (a INTEGER)")
        raise RuntimeError("migration failed halfway")

    steps = [
        *migrate.__globals__["MIGRATIONS"],
        Migration(version=99, name="doomed", apply=_explode),
    ]
    with pytest.raises(RuntimeError, match="halfway"):
        migrate(config, migrations=steps)

    with reader(config, require_supported_schema=False) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master").fetchall()}
    assert "doomed" not in tables
    assert connection.inspect(config).schema_version == connection.SUPPORTED_SCHEMA_VERSION


# --------------------------------------------------------------------------
# Encryption at rest (AC-ARCH.5) and the wrong-key message.
# --------------------------------------------------------------------------


def test_a_known_plaintext_is_not_recoverable_from_any_of_the_three_files(
    initialized_config: Config,
) -> None:
    """A WAL-mode datastore is three files, and the criterion covers all of them.

    The scan happens while the writer is still open, because closing the last
    connection checkpoints the WAL and deletes it -- a scan afterwards would
    read one file and report a three-file guarantee. That is precisely the
    shortcut AC-ARCH.5 names the `-wal` and `-shm` files to prevent.
    """
    marker = b"BANKMACHINE-PLAINTEXT-CANARY-8842"
    path = initialized_config.datastore_path
    wal = path.with_name(path.name + "-wal")
    shm = path.with_name(path.name + "-shm")

    with writer(initialized_config) as conn:
        conn.execute("CREATE TABLE canary (value TEXT)")
        conn.execute("INSERT INTO canary VALUES (?)", (marker.decode(),))

        present = [f for f in (path, wal, shm) if f.exists()]
        assert len(present) == 3, f"expected three files, found {[f.name for f in present]}"
        assert wal.stat().st_size > 0, "the WAL was empty, so scanning it proved nothing"
        for candidate in present:
            assert marker not in candidate.read_bytes(), (
                f"plaintext recoverable from {candidate.name}"
            )

    # And again after the close checkpointed everything into the main file.
    assert marker not in path.read_bytes()
    assert path.read_bytes()[:16] != b"SQLite format 3\x00"


def test_a_wrong_key_is_reported_as_authentication_not_corruption(
    initialized_config: Config,
) -> None:
    """SQLCipher says `file is not a database`, which sends operators to the wrong runbook."""
    set_datastore_key(initialized_config, generate_datastore_key())
    with (
        pytest.raises(connection.DatastoreKeyRejectedError, match="authentication failure"),
        reader(initialized_config),
    ):
        pass


def test_the_key_never_appears_in_the_datastore_files(initialized_config: Config) -> None:
    key = get_datastore_key(initialized_config).encode()
    path = initialized_config.datastore_path
    for candidate in (path, path.with_name(path.name + "-wal")):
        if candidate.exists():
            assert key not in candidate.read_bytes()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _create_filler(conn: connection.Connection) -> None:
    """The table both checkpoint tests read, committed before either reader opens."""
    conn.execute("CREATE TABLE IF NOT EXISTS wal_filler (a INTEGER, pad TEXT)")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()


def _fill_wal(conn: connection.Connection) -> None:
    """Commit enough pages that a checkpoint has real frames to move."""
    conn.execute("BEGIN IMMEDIATE")
    conn.executemany(
        "INSERT INTO wal_filler (a, pad) VALUES (?, ?)",
        [(i, "x" * 512) for i in range(400)],
    )
    conn.execute("COMMIT")


_LOCK_HOLDER = textwrap.dedent(
    """
    import time
    from bankmachine.config import load_config
    from bankmachine.store.connection import writer

    config = load_config()
    with writer(config):
        print("locked", flush=True)
        time.sleep(120)
    """
)

_HOT_WAL_WRITER = textwrap.dedent(
    """
    import os
    from bankmachine.config import load_config
    from bankmachine.store.connection import writer

    # os._exit skips the close that would checkpoint the WAL, which is what
    # leaves the file in the state a killed writer leaves it in.
    with writer(load_config()) as w:
        w.execute("CREATE TABLE hot_wal (a INTEGER, pad TEXT)")
        w.execute("BEGIN IMMEDIATE")
        w.executemany(
            "INSERT INTO hot_wal VALUES (?, ?)", [(i, "x" * 512) for i in range(400)]
        )
        w.execute("COMMIT")
        os._exit(9)
    """
)

_KILL_MID_MIGRATION = textwrap.dedent(
    """
    import os
    from bankmachine.config import load_config
    from bankmachine.store.migrations import Migration, migrate

    def apply(conn):
        conn.execute("CREATE TABLE half_migrated (a INTEGER)")
        os._exit(9)

    migrate(load_config(), migrations=[Migration(version=1, name="half", apply=apply)])
    """
)


def _spawn_lock_holder(config: Config) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", _LOCK_HOLDER],
        env={
            **os.environ,
            "BANKMACHINE_DATASTORE_PATH": str(config.datastore_path),
            "BANKMACHINE_KEYCHAIN_SERVICE": config.keychain_service,
            "BANKMACHINE_LOG_DIR": str(config.log_dir),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for(lock_path: Path, holder: subprocess.Popen[str]) -> None:
    """Block until the child reports that it holds the lock."""
    assert holder.stdout is not None
    line = holder.stdout.readline()
    if "locked" not in line:
        holder.terminate()
        stderr = holder.stderr.read() if holder.stderr else ""
        raise AssertionError(f"lock holder never started: {line!r} {stderr}")


def test_status_reports_a_missing_keychain_key_rather_than_crashing(config: Config) -> None:
    """AC-ARCH.3 covers a store that exists but cannot be opened, not only an absent one.

    A fresh clone whose datastore was restored from backup but whose keychain
    entry was not is exactly this state, and `store status` is where an operator
    would look to find that out.
    """
    from bankmachine.secrets import delete_datastore_key, generate_datastore_key
    from bankmachine.store.migrations import migrate

    set_datastore_key(config, generate_datastore_key())
    migrate(config)
    delete_datastore_key(config)

    status = connection.inspect(config)
    assert status.exists is True
    assert status.healthy is False
    assert status.readable is False
    assert "keychain" in (status.problem or "")
