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
* **Reader** -- every read-role handle comes from `_open_read_role`, which
  `reader()` and `opens_with()` are the two entry points to. Opened `mode=ro` at
  the file, so the refusal lives in the file handle where SQL cannot reach it,
  with `PRAGMA query_only=ON` as a second layer. It holds no snapshot beyond the
  statement that needs it, and it never falls back to a writable handle. The two
  entry points differ in ONE thing -- where the key comes from, the keychain or
  an operator's candidate -- and that difference is an argument, not a second
  copy of the open.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from sqlcipher3 import dbapi2

from bankmachine.config import Config
from bankmachine.secrets import SecretsError, get_datastore_key, validate_candidate_key

Connection = dbapi2.Connection

#: The driver's base error class, re-exported. A caller outside the store layer
#: -- `sync shell` is the one that exists -- has to be able to catch a statement
#: that failed without importing a DBAPI module itself, because the moment a
#: second module imports one, "every handle is constructed here" stops being
#: checkable and goes back to being a convention.
DriverError = dbapi2.Error


def statement_is_complete(sql: str) -> bool:
    """Whether `sql` is a complete statement, or is still waiting for more.

    The prompt in `sync shell` needs this to decide between running what it has
    and asking for another line. It is the driver's own tokenizer, exposed here
    for the same reason `DriverError` is: it is the store layer's business to
    know what a SQL statement is, and nobody else's to import a driver to ask.
    """
    return bool(dbapi2.complete_statement(sql))


#: The schema versions this build of the code understands. A datastore outside
#: this range is refused, loudly, rather than served against.
SUPPORTED_SCHEMA_VERSION: Final = 9

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


class DatastorePathUnusableError(StoreError):
    """The filesystem refused a path this operation needs.

    An unwritable data directory, a lock file owned by another user, a path
    unlinked between the check and the open. These arrive as `OSError` from the
    syscall rather than from the driver, so without this they escape every
    handler that catches `StoreError` -- including `inspect`, whose whole
    contract is to report a state rather than raise on it.
    """


@contextmanager
def _filesystem(what: str) -> Iterator[None]:
    """Turn an `OSError` from a syscall into a named `StoreError`.

    The store layer's callers are promised `StoreError` for anything that can
    go wrong with the datastore, and `inspect` is promised not to raise at all.
    A bare `PermissionError` out of a lock-file probe breaks both, and does it
    on the most ordinary environmental failure there is.
    """
    try:
        yield
    except OSError as exc:
        raise DatastorePathUnusableError(f"{what}: {exc}") from exc


class SchemaVersionUnsupportedError(StoreError):
    """The datastore's schema version is not one this build understands."""


class DatastoreProblem(StrEnum):
    """Which unhealthy state `inspect` found, as a value rather than as prose.

    🔴 The `problem` sentence beside this is written for a human and changes
    freely; anything that must BRANCH on the state reads this instead. The two
    exist together because a caller composing an operator remedy needs to pick a
    remedy per state, and picking one by matching substrings of a sentence makes
    every later rewording of that sentence a silent behaviour change.

    `MISSING` is separated from the rest for a reason that outranks tidiness: a
    store that is not there has no data to misreport, so it is the one state the
    MCP surface may answer rather than refuse (AC-ARCH.3). Every other value here
    means data exists and could not be read.
    """

    MISSING = "missing"
    NO_SCHEMA_VERSION = "no_schema_version"
    #: 🔴 The two schema mismatches are SEPARATE values, and collapsing them is a
    #: defect with a specific victim. `migrate()` is forward-only, so "run the
    #: migrations" is the remedy for a store BEHIND this build and does literally
    #: nothing for one AHEAD of it -- the operator runs the command, is told
    #: there was nothing to apply, and never learns the real fix is to upgrade
    #: the reader. The ahead case is not the exotic one: it is what
    #: `api-contract.md` § Hard errors gives as the REASON this state refuses at
    #: all ("a reader running older code against a migrated schema"), and it is
    #: what a second checkout, a rolled-back deploy, or a sync process upgraded
    #: before the MCP server produces.
    SCHEMA_BEHIND_BUILD = "schema_behind_build"
    SCHEMA_AHEAD_OF_BUILD = "schema_ahead_of_build"
    KEY_MISSING = "key_missing"
    UNREADABLE = "unreadable"


#: What an operator should DO about each state, as an imperative clause.
#:
#: 🔴 One map, because the remedy is a property of the STATE and not of the
#: surface that noticed it. Four CLI commands and the MCP tool layer each used to
#: compose their own, and all four CLI ones said "run `bankmachine store init`"
#: for every state -- including the two where that command is actively wrong: it
#: applies nothing to a store already AHEAD of this build, and it refuses, on
#: purpose, to mint a key for a store that already exists. A remedy written per
#: caller is a remedy that gets the easy state right and the rare ones wrong,
#: which is the shape of the defect this map exists to make unrepresentable.
#:
#: Each value is the ACTION only. The diagnosis in front of it belongs to the
#: surface: the MCP layer states the environment and that nothing was read or
#: changed while carrying no path, and the CLI states the path because a terminal
#: is where a path is useful rather than exported.
_REMEDIES: Final[dict[DatastoreProblem, str]] = {
    DatastoreProblem.MISSING: "Run `bankmachine store init` to create it",
    DatastoreProblem.NO_SCHEMA_VERSION: (
        "Run `bankmachine store init` to apply the migrations, then `bankmachine store status` "
        "to confirm"
    ),
    DatastoreProblem.SCHEMA_BEHIND_BUILD: (
        "Run `bankmachine store init` to apply the pending migrations, then `bankmachine store "
        "status` to confirm"
    ),
    DatastoreProblem.SCHEMA_AHEAD_OF_BUILD: (
        "Update this bankmachine to the build that wrote it; do not run `bankmachine store init`, "
        "which applies nothing here"
    ),
    DatastoreProblem.KEY_MISSING: (
        "Restore the keychain entry from your backup, or move the datastore aside to start a new "
        "one"
    ),
    DatastoreProblem.UNREADABLE: "Run `bankmachine store status` for the file-level diagnosis",
}


#: 🔴 Checked at import, so a sixth `DatastoreProblem` cannot ship without one.
#: `_REMEDIES[problem]` would raise `KeyError` from inside an error path that is
#: already telling the operator something went wrong -- a crash while reporting a
#: crash, and at exactly the moment the product is least able to afford it. The
#: enum-driven test in `tests/test_unservable_datastore.py` covers the same gap
#: from the other side; this one costs nothing and fires before any test runs.
_MISSING_REMEDIES = set(DatastoreProblem) - set(_REMEDIES)
if _MISSING_REMEDIES:  # pragma: no cover - import-time guard
    raise RuntimeError(f"DatastoreProblem members with no remedy: {sorted(_MISSING_REMEDIES)}")


def remedy_for(problem: DatastoreProblem | None) -> str:
    """The action clause for one unhealthy state.

    Takes the enum, never the `problem` sentence: picking a remedy by matching
    substrings of prose makes every later rewording a silent behaviour change,
    which is the whole reason `DatastoreProblem` exists beside that sentence.

    A `None` problem means healthy, and no caller should be composing a remedy at
    all -- but `DatastoreStatus.reason` is typed optional, so this answers rather
    than raising into an error path that is already reporting something else.
    """
    if problem is None:
        return "Run `bankmachine store status` for the current state"
    return _REMEDIES[problem]


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
    #: The machine-readable twin of `problem`; None exactly when healthy.
    reason: DatastoreProblem | None = None

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
        raise _diagnose_first_read(exc, config) from exc
    conn.execute("PRAGMA foreign_keys = ON")


def _diagnose_first_read(exc: dbapi2.DatabaseError, config: Config) -> StoreError:
    """Tell a wrong key apart from a datastore that cannot be read at all.

    Both surface here rather than at `connect()`, because SQLite opens lazily --
    the URI is parsed and nothing is touched until the first read. That is why
    this decision lives at the probe instead of around the connect call, where
    the `mode=ro` guard was originally written and where this edge never
    reaches it.

    SQLCipher reports a wrong key as `SQLITE_NOTADB` -- `file is not a database`
    -- which reads as corruption and sends the operator to the wrong recovery
    procedure unless it is renamed. Anything else is not an authentication
    failure and must not be reported as one: the measured case is
    `SQLITE_CANTOPEN` from a hot WAL left by a killed writer, with no `-shm`
    file, in a directory this process cannot write to. SQLite has to create the
    shared-memory index before it can read the WAL, cannot, and gives up.
    Reporting that as a bad key sends the operator to restore a keychain entry
    that was never the problem, which is the exact failure
    `DatastoreKeyRejectedError` was introduced to prevent.
    """
    if getattr(exc, "sqlite_errorname", "") == "SQLITE_NOTADB":
        return DatastoreKeyRejectedError(
            f"the key in keychain {config.keychain_service}/{config.keychain_account} does not "
            f"decrypt {config.datastore_path} -- this is an authentication failure, not corruption"
        )
    return DatastoreUnreadableError(
        f"{config.datastore_path} could not be read ({exc}). A hot WAL from a killed writer, "
        f"with no -shm file, in a directory this process cannot write to produces exactly this: "
        f"the shared-memory index the WAL needs cannot be created. The key is not implicated. "
        f"Not retried read-write -- that would restore the writes this handle exists to refuse"
    )


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
            with _filesystem(f"could not create the directory for {config.datastore_path}"):
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
    with _filesystem(f"could not open the writer lock at {config.lock_path}"):
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


def _require_supported_schema(conn: Connection, config: Config, *, consequence: str) -> None:
    """Norm 4's check, with one home because both roles need it.

    A reader that misreads a schema it does not recognize returns answers that
    are wrong; a writer that misunderstands one writes them down. Only
    `initializing_writer` skips this, because bringing an old datastore forward
    is the one job that has to open a version this build does not serve.
    """
    version = read_schema_version(conn)
    if version != SUPPORTED_SCHEMA_VERSION:
        raise SchemaVersionUnsupportedError(
            f"{config.datastore_path} is at schema version {version}; this build serves version "
            f"{SUPPORTED_SCHEMA_VERSION} only. Refusing rather than {consequence}"
        )


@contextmanager
def writer(config: Config) -> Iterator[Connection]:
    """A writable handle to an existing datastore, under the exclusive lock."""
    with _writer(config, create=False) as conn:
        _require_supported_schema(
            conn, config, consequence="writing into a schema it does not understand"
        )
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
def copying_writer(config: Config) -> Iterator[Connection]:
    """The handle `store backup` takes: the writer lock, and no schema check.

    🔴 The second and last exemption from the schema check, and the reasoning is
    the norm's own. A process that does not recognize the schema version refuses
    to *serve* -- because a reader that misreads a schema returns plausible,
    structurally valid, wrong answers, and a writer writes them down. A backup
    does neither. `VACUUM INTO` copies pages of ciphertext; it reads no table,
    interprets no column and answers no question, so there is no answer for an
    unrecognized schema to make wrong.

    Refusing here costs something real: the only remaining way to copy such a
    datastore is `cp`, which drops whatever is still in the WAL -- measured, at
    300 rows -- and the newest of `balances_daily` is what a dropped WAL takes.

    A named role rather than a `require_supported_schema=False` argument on
    `writer`: an exemption within reach of every future caller is not an
    exemption, and the norm it guards is correct for all of them.

    The lock still applies. This goes through the one writer factory, so no sync
    run can be committing while the copy is made.
    """
    with _writer(config, create=False) as conn:
        yield conn


def _open_read_role(config: Config, key: str) -> Connection:
    """The one place a read-role handle is constructed.

    🔴 Two callers, and the second is why this exists as a function. `reader()`
    takes the key from the keychain; `opens_with()` is handed a candidate and
    must not. That single difference had been enough to justify a second copy of
    the open, and the copy immediately drifted -- it lost `PRAGMA query_only`
    and the `OperationalError` translation, so a read-role handle held half the
    norm and a driver error escaped the layer that turns it into a `StoreError`.
    The difference is the ARGUMENT. Everything else is the role, and the role is
    written once.

    `mode=ro` puts the refusal in the file handle where SQL cannot reach it;
    `query_only` is the second layer; autocommit is what keeps a reader from
    holding a snapshot across statements and starving WAL checkpointing.
    """
    try:
        conn = dbapi2.connect(
            _uri(config.datastore_path, READ_ROLE_MODE),
            uri=True,
            isolation_level=None,
            check_same_thread=True,
        )
    except dbapi2.OperationalError as exc:
        # No cause is named here. SQLite opens lazily, so the hot-WAL edge this
        # branch used to claim actually surfaces at the first read, where
        # `_diagnose_first_read` owns it -- and two operator-facing texts
        # describing one failure is how the wrong one gets believed.
        raise DatastoreUnreadableError(
            f"{config.datastore_path} could not be opened read-only ({exc}). Not retried "
            f"read-write: that would restore the writes this handle exists to refuse"
        ) from exc
    _key_and_prepare(conn, config, key)
    conn.execute("PRAGMA query_only = ON")
    return conn


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
    conn = _open_read_role(config, get_datastore_key(config))
    try:
        if require_supported_schema:
            _require_supported_schema(
                conn,
                config,
                consequence="returning answers derived from a schema it does not understand",
            )
        yield conn
    finally:
        conn.close()


def opens_with(config: Config, key: str) -> bool:
    """Whether `key` decrypts the datastore. The question key escrow is actually about.

    🔴 Deliberately NOT built on `reader()`: that fetches the key from the
    keychain, which is the one thing a candidate key must not do. The seam is
    `_key_and_prepare`, which already takes an explicit key, and
    `_diagnose_first_read`, which already tells a wrong key apart from a
    datastore that cannot be read at all. Nothing here adds a diagnosis.

    🔴 The schema version is deliberately NOT required. Restoring an older
    backup onto a newer build is a real path, the key is correct there, and
    refusing to say whether a key works because the schema is old fails the
    operator in the exact scenario the command exists for.

    Returns False ONLY for a key the datastore rejects. A missing or unreadable
    datastore raises instead: neither is an answer about the key, and reporting
    "no" for a hot WAL this process cannot open would send the operator to
    restore a keychain entry that was never the problem.

    🔴 True requires POSITIVE evidence that a page was decrypted, and the reason
    is a state an incident actually produces. SQLite reads a file with no pages
    -- a truncated copy, an interrupted restore, a `store init` killed between
    creating the file and writing to it -- as a valid empty schema: the first
    read succeeds without page 1 ever being touched, so SQLCipher's codec is
    never invoked and NO KEY IS TESTED. Answering True there would have
    `store key verify` print MATCHES for an arbitrary candidate, and
    `store key import` -- whose whole warrant is that verification precedes the
    write -- store that unverified key over a working keychain entry. So a
    pageless file raises rather than answering: it is a fact about the file, not
    about the key.

    The candidate is validated here rather than only in the callers. SQLCipher
    runs anything that is not exactly 64 hex digits through its KDF, so a
    malformed value would come back as a confident False -- "this key does not
    open the datastore" about something that is not a key at all.
    """
    key = validate_candidate_key(key)
    if not config.datastore_path.exists():
        raise DatastoreMissingError(
            f"no datastore at {config.datastore_path} -- there is nothing to check a key against. "
            f"A key can only be verified against the datastore it is supposed to open"
        )
    if config.datastore_path.stat().st_size == 0:
        raise DatastoreUnreadableError(
            f"{config.datastore_path} is empty -- it holds no pages, so nothing was ever "
            f"encrypted with any key and no key can be checked against it. This is a truncated "
            f"copy or an interrupted restore, not a key problem: replace the file from a backup "
            f"taken with `bankmachine store backup`"
        )
    try:
        conn = _open_read_role(config, key)
    except DatastoreKeyRejectedError:
        return False
    try:
        if conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0:
            raise DatastoreUnreadableError(
                f"{config.datastore_path} has no schema -- no page was decrypted, so this run "
                f"tested no key. A datastore this build can serve always carries a schema; a "
                f"file that does not is an incomplete copy, not a wrong key"
            )
        return True
    finally:
        with suppress(dbapi2.Error):
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
    # The file can vanish between that probe and this open, and it can belong to
    # another user; both are OSError, and this is called from `inspect`.
    with _filesystem(f"could not probe the writer lock at {config.lock_path}"):
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
            path=path,
            exists=False,
            environment=config.environment,
            problem="datastore missing",
            reason=DatastoreProblem.MISSING,
        )
    try:
        lock_held = writer_lock_held(config)
    except StoreError as exc:
        # Inside the report, not around it: a lock file this process cannot open
        # is a state to describe, and it is the state a datastore restored from
        # a backup under another user arrives in.
        return DatastoreStatus(
            path=path,
            exists=True,
            environment=config.environment,
            readable=False,
            problem=str(exc),
            reason=DatastoreProblem.UNREADABLE,
        )
    try:
        with reader(config, require_supported_schema=False) as conn:
            journal = conn.execute("PRAGMA journal_mode").fetchone()
            version = read_schema_version(conn)
            problem = None
            reason = None
            if version is None:
                problem = (
                    "no schema version recorded -- datastore is uninitialized, "
                    "or a migration did not complete"
                )
                reason = DatastoreProblem.NO_SCHEMA_VERSION
            elif version != SUPPORTED_SCHEMA_VERSION:
                problem = f"schema version {version} is not served by this build"
                reason = (
                    DatastoreProblem.SCHEMA_AHEAD_OF_BUILD
                    if version > SUPPORTED_SCHEMA_VERSION
                    else DatastoreProblem.SCHEMA_BEHIND_BUILD
                )
            return DatastoreStatus(
                path=path,
                exists=True,
                environment=config.environment,
                journal_mode=str(journal[0]) if journal else None,
                schema_version=version,
                writer_lock_held=lock_held,
                problem=problem,
                reason=reason,
            )
    except (StoreError, SecretsError) as exc:
        # SecretsError belongs here as much as StoreError does: a datastore whose
        # key is missing from the keychain is a state to report, not a crash. It
        # is the state a fresh clone is in, and AC-ARCH.3 asks for it by name.
        #
        # 🔴 The two are told apart in `reason` even though they share this
        # handler, because their remedies share nothing: a missing key is
        # restored from the operator's backup and cannot be regenerated, while
        # every other read failure is about the file. A caller composing a
        # remedy must not have to guess which happened from the sentence.
        return DatastoreStatus(
            path=path,
            exists=True,
            environment=config.environment,
            writer_lock_held=lock_held,
            readable=False,
            problem=str(exc),
            reason=(
                DatastoreProblem.KEY_MISSING
                if isinstance(exc, SecretsError)
                else DatastoreProblem.UNREADABLE
            ),
        )
