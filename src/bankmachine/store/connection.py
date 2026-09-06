"""The one module that constructs a datastore connection.

Nothing outside `store/` opens a connection, and nothing outside this file
constructs one. That single rule is what makes the architecture's four norms
enforceable by a structural test rather than by review, and it is why
`store/engine.py` hands SQLAlchemy a `creator=` and nothing else.

There are exactly two roles, and role membership is a property of how a handle
was constructed rather than of which command asked for it:

* **Writer** -- `writer()` and `initializing_writer()`. Both route through one
  private factory that takes the exclusive advisory lock *before* it returns a
  handle. There is no other way to obtain a connection that can write.
* **Reader** -- `reader()`. Opened `mode=ro` at the file, so the refusal lives
  in the file handle where SQL cannot reach it, with `PRAGMA query_only=ON` as a
  second layer. It holds no snapshot beyond the statement that needs it, and it
  never falls back to a writable handle.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlcipher3 import dbapi2

from bankmachine.config import Config
from bankmachine.logging_setup import get_logger
from bankmachine.secrets import SecretsError, get_datastore_key

Connection = dbapi2.Connection

logger = get_logger("store.connection")

#: The schema versions this build of the code understands. A datastore outside
#: this range is refused, loudly, rather than served against.
SUPPORTED_SCHEMA_VERSION: Final = 1

SCHEMA_VERSION_TABLE: Final = "schema_version"

#: The SQLite URI open modes, named because they are the norms rather than an
#: implementation detail. `ro` is where the read-role refusal lives -- in the
#: file handle, out of reach of `PRAGMA query_only=OFF`. `rw` is what makes "no
#: component creates the datastore implicitly" a property of construction rather
#: than of the existence check that precedes it: a plain `connect()` to a missing
#: path silently creates an empty database, and every answer derived from it
#: would be confidently wrong.
READ_ROLE_MODE: Final = "ro"
WRITER_MODE: Final = "rw"
CREATING_WRITER_MODE: Final = "rwc"


class StoreError(Exception):
    """Base for every failure to open or use the datastore."""


class DatastoreMissingError(StoreError):
    """The datastore file does not exist, and this caller may not create one."""


class DatastoreUnreadableError(StoreError):
    """The datastore exists but could not be opened read-only.

    The measured cause is a hot WAL left by a killed writer with no `-shm` file
    and a containing directory the reader cannot write to. This is raised, and
    never routed around: falling back to a read-write handle would silently
    reinstate the reversible guarantee at exactly the moment something has
    already gone wrong.
    """


class DatastoreKeyRejectedError(StoreError):
    """The key did not decrypt the datastore.

    SQLCipher reports a wrong key as `file is not a database`, which reads as
    corruption and sends the operator to the wrong recovery procedure. It is an
    authentication failure and is reported as one.
    """


class AnotherWriterRunningError(StoreError):
    """Another writer holds the advisory lock. This process does not wait."""


class SchemaVersionUnsupportedError(StoreError):
    """The datastore's schema version is not one this build understands."""


@dataclass(frozen=True, slots=True)
class DatastoreStatus:
    """What `store status` reports. Nothing here raises on an absent datastore."""

    path: str
    exists: bool
    environment: str
    journal_mode: str | None = None
    schema_version: int | None = None
    supported_schema_version: int = SUPPORTED_SCHEMA_VERSION
    writer_lock_held: bool = False
    readable: bool = True
    problem: str | None = None

    @property
    def healthy(self) -> bool:
        """A datastore this build can serve from.

        A store whose migration was interrupted has no recognized schema version
        and is therefore not healthy, which is the property the interrupted
        migration test asserts.
        """
        return (
            self.exists
            and self.readable
            and self.problem is None
            and self.schema_version == SUPPORTED_SCHEMA_VERSION
        )


def _uri(path: os.PathLike[str] | str, mode: str) -> str:
    """A SQLite URI in an explicit open mode.

    The mode is what enforces the norms, not the existence check that precedes
    it: `rw` refuses to create a missing file and `ro` refuses to write to a
    present one, so neither guarantee depends on a check that could go stale
    between the test and the open.
    """
    return f"file:{os.fsdecode(path)}?mode={mode}"


def _key_and_prepare(conn: Connection, config: Config, key: str) -> None:
    """Key the connection first, then everything else. Order is not negotiable."""
    conn.execute(f"PRAGMA key = \"x'{key}'\"")
    conn.execute(f"PRAGMA busy_timeout = {config.busy_timeout_ms}")
    try:
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except dbapi2.DatabaseError as exc:
        conn.close()
        raise DatastoreKeyRejectedError(
            f"the key in keychain {config.keychain_service}/{config.keychain_account} does not "
            f"decrypt {config.datastore_path} -- this is an authentication failure, not corruption"
        ) from exc
    conn.execute("PRAGMA foreign_keys = ON")


@contextmanager
def _writer(config: Config, *, create: bool) -> Iterator[Connection]:
    """The one writer factory. It holds the exclusive lock before it yields."""
    if not create and not config.datastore_path.exists():
        raise DatastoreMissingError(
            f"no datastore at {config.datastore_path} -- run `bankmachine store init` "
            f"(nothing creates it implicitly)"
        )

    key = get_datastore_key(config)
    with _exclusive_lock(config):
        if create:
            config.datastore_path.parent.mkdir(parents=True, exist_ok=True)
        conn = dbapi2.connect(
            _uri(config.datastore_path, CREATING_WRITER_MODE if create else WRITER_MODE),
            uri=True,
            isolation_level=None,
            check_same_thread=True,
        )
        try:
            _key_and_prepare(conn, config, key)
            conn.execute("PRAGMA journal_mode = WAL").fetchone()
            yield conn
        finally:
            conn.close()


@contextmanager
def _exclusive_lock(config: Config) -> Iterator[None]:
    """The advisory writer lock, released by the kernel if the process dies.

    A lease row in the database cannot offer that: AC-2.5 requires a process
    killed mid-pagination to be recoverable, and a database lease would survive
    the kill and strand the next run behind a holder that no longer exists.
    """
    config.lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(config.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise AnotherWriterRunningError(
                f"another writer holds {config.lock_path}; this process does not wait"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextmanager
def writer(config: Config) -> Iterator[Connection]:
    """A writable handle to an existing datastore, under the exclusive lock."""
    with _writer(config, create=False) as conn:
        yield conn


@contextmanager
def initializing_writer(config: Config) -> Iterator[Connection]:
    """The only handle permitted to bring a datastore file into existence.

    Callers are `store init` and the migration runner, and nothing else. Every
    other path opens `mode=rw` and reports absence rather than papering over a
    typo'd path with a valid, empty, correctly-encrypted store.
    """
    with _writer(config, create=True) as conn:
        yield conn


@contextmanager
def reader(config: Config, *, require_supported_schema: bool = True) -> Iterator[Connection]:
    """A read-only handle. Every statement is its own snapshot.

    The connection is opened in autocommit, so no read transaction spans two
    statements and none survives an idle moment -- the property that keeps a
    long-lived reader from starving WAL checkpointing.
    """
    if not config.datastore_path.exists():
        raise DatastoreMissingError(
            f"no datastore at {config.datastore_path} -- run `bankmachine store init`"
        )
    key = get_datastore_key(config)
    try:
        conn = dbapi2.connect(
            _uri(config.datastore_path, READ_ROLE_MODE),
            uri=True,
            isolation_level=None,
            check_same_thread=True,
        )
    except dbapi2.OperationalError as exc:
        raise DatastoreUnreadableError(
            f"{config.datastore_path} could not be opened read-only ({exc}). A hot WAL from a "
            f"killed writer in a directory this process cannot write to produces this. Not "
            f"retried read-write: that would restore the writes this handle exists to refuse"
        ) from exc
    try:
        _key_and_prepare(conn, config, key)
        conn.execute("PRAGMA query_only = ON")
        if require_supported_schema:
            version = read_schema_version(conn)
            if version != SUPPORTED_SCHEMA_VERSION:
                raise SchemaVersionUnsupportedError(
                    f"{config.datastore_path} is at schema version {version}; this build serves "
                    f"version {SUPPORTED_SCHEMA_VERSION} only. Refusing to serve rather than "
                    f"return answers derived from a schema it does not understand"
                )
        yield conn
    finally:
        conn.close()


def read_schema_version(conn: Connection) -> int | None:
    """The highest applied migration, or None when the table is absent or empty."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (SCHEMA_VERSION_TABLE,),
    ).fetchone()
    if row is None:
        return None
    version_row = conn.execute(f"SELECT MAX(version) FROM {SCHEMA_VERSION_TABLE}").fetchone()
    value = version_row[0] if version_row else None
    return int(value) if value is not None else None


def stamp_schema_version(conn: Connection, version: int) -> None:
    """Record a migration as applied. Called only inside the migration's transaction."""
    conn.execute(
        f"INSERT INTO {SCHEMA_VERSION_TABLE} (version, applied_at) VALUES (?, ?)",
        (version, datetime.now(UTC).isoformat()),
    )


def writer_lock_held(config: Config) -> bool:
    """Whether some other process currently holds the writer lock.

    Answering costs a lock acquisition, so this is for reporting only -- a
    caller that wants to write takes the lock through the factory instead of
    asking first and racing.
    """
    if not config.lock_path.exists():
        return False
    fd = os.open(config.lock_path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def inspect(config: Config) -> DatastoreStatus:
    """Report the datastore's state without raising (AC-ARCH.3, AC-4.4).

    The MCP server must start against an empty or missing datastore and *report*
    that state; it cannot report a state it crashed on, and it must not report a
    state it created by asking.
    """
    path = str(config.datastore_path)
    if not config.datastore_path.exists():
        return DatastoreStatus(
            path=path, exists=False, environment=config.environment, problem="datastore missing"
        )
    lock_held = writer_lock_held(config)
    try:
        with reader(config, require_supported_schema=False) as conn:
            journal = conn.execute("PRAGMA journal_mode").fetchone()
            version = read_schema_version(conn)
            problem = None
            if version is None:
                problem = (
                    "no schema version recorded -- datastore is uninitialized, "
                    "or a migration did not complete"
                )
            elif version != SUPPORTED_SCHEMA_VERSION:
                problem = f"schema version {version} is not served by this build"
            return DatastoreStatus(
                path=path,
                exists=True,
                environment=config.environment,
                journal_mode=str(journal[0]) if journal else None,
                schema_version=version,
                writer_lock_held=lock_held,
                problem=problem,
            )
    except (StoreError, SecretsError) as exc:
        # SecretsError belongs here as much as StoreError does: a datastore whose
        # key is missing from the keychain is a state to report, not a crash. It
        # is the state a fresh clone is in, and AC-ARCH.3 asks for it by name.
        return DatastoreStatus(
            path=path,
            exists=True,
            environment=config.environment,
            writer_lock_held=lock_held,
            readable=False,
            problem=str(exc),
        )
