"""SQLAlchemy Core engines built over connections this package already keyed.

SQLAlchemy never opens a connection here. `create_engine(..., creator=...)` is
handed a handle that `store.connection` has already keyed, put in WAL, opened
`mode=ro` or locked as appropriate -- so every SQLCipher-specific step stays in
the module that owns the norms, and the query builder does only the part it is
good at. The engine's lifetime is the handle's lifetime, which is why both of
these are context managers rather than module-level singletons.

🔴 **SQLAlchemy's transaction control is inert over these handles, by
inheritance.** `store.connection` opens every handle in autocommit
(`isolation_level=None`) so the reader releases its snapshot per statement and
the migration runner can own its own `BEGIN IMMEDIATE`. The engine wraps that
handle rather than replacing it, so `with engine.begin():` opens nothing and
block-exit rollback undoes nothing -- each statement has already committed.
Anything here that needs a transaction issues `BEGIN IMMEDIATE` / `COMMIT` on
the driver, the way `store/migrations` does. This is written down because the
schema chunk and the rebuild chunk are the two that will assume otherwise.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.pool import StaticPool
from sqlcipher3 import dbapi2

from bankmachine.config import Config
from bankmachine.store import connection

#: The DBAPI is SQLCipher's, not the stdlib's. It is sqlite3-compatible, so the
#: stock SQLite dialect drives it correctly once it is told which module to use.
_URL = "sqlite://"


def _engine_over(conn: connection.Connection) -> Engine:
    return create_engine(
        _URL,
        module=dbapi2,
        creator=lambda: conn,
        poolclass=StaticPool,
    )


@contextmanager
def writer_engine(config: Config) -> Iterator[Engine]:
    """An engine over a writable handle, held under the exclusive writer lock."""
    with connection.writer(config) as conn:
        engine = _engine_over(conn)
        try:
            yield engine
        finally:
            engine.dispose()


@contextmanager
def reader_engine(config: Config) -> Iterator[Engine]:
    """An engine over a `mode=ro` handle. Writes through it fail at the file."""
    with connection.reader(config) as conn:
        engine = _engine_over(conn)
        try:
            yield engine
        finally:
            engine.dispose()
