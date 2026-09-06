"""Forward-only migrations, each applied in exactly one transaction.

The runner is deliberately ~50 lines and owns its own transaction boundary
rather than delegating to a migration framework. That boundary is the whole
point: a migration's DDL and the row recording its version commit together or
not at all, so a process killed between them leaves a datastore that reports no
recognized schema version rather than one that reports healthy while missing the
table the version claims it has.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from bankmachine.config import Config
from bankmachine.logging_setup import get_logger
from bankmachine.store.connection import (
    Connection,
    initializing_writer,
    read_schema_version,
    stamp_schema_version,
)

logger = get_logger("store.migrations")


@dataclass(frozen=True, slots=True)
class Migration:
    """One forward step. `apply` issues DDL only; the runner stamps the version."""

    version: int
    name: str
    apply: Callable[[Connection], None]


def _create_schema_version(conn: Connection) -> None:
    """The table every later migration records itself in.

    One row per applied migration rather than one mutable row: the history is
    worth more than the byte it saves, and `MAX(version)` is the current state.
    """
    conn.execute(
        "CREATE TABLE schema_version (  version INTEGER PRIMARY KEY,  applied_at TEXT NOT NULL)"
    )


MIGRATIONS: Sequence[Migration] = (
    Migration(version=1, name="create schema_version", apply=_create_schema_version),
)


def migrate(config: Config, *, migrations: Sequence[Migration] | None = None) -> list[int]:
    """Apply every pending migration, and return the versions applied.

    The migration runner is one of the two paths permitted to bring a datastore
    into existence; every other caller opens a file that must already be there.
    """
    steps = MIGRATIONS if migrations is None else migrations
    applied: list[int] = []
    with initializing_writer(config) as conn:
        current = read_schema_version(conn) or 0
        for step in steps:
            if step.version <= current:
                continue
            conn.execute("BEGIN IMMEDIATE")
            try:
                step.apply(conn)
                stamp_schema_version(conn, step.version)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            logger.info("applied migration %d (%s)", step.version, step.name)
            applied.append(step.version)
    return applied
