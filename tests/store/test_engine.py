"""SQLAlchemy Core over connections this package keyed itself.

The point of the `creator=` route is that SQLAlchemy never opens a connection:
every SQLCipher-specific step stays in `store.connection`. These tests assert
that the route works AND that it inherits the role of the handle underneath it,
because an engine that quietly reconnected would be a writable handle nobody
constructed through the factory.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from bankmachine.config import Config
from bankmachine.store.engine import reader_engine, writer_engine


def test_a_writer_engine_reads_and_writes(initialized_config: Config) -> None:
    with writer_engine(initialized_config) as engine, engine.connect() as conn:
        conn.execute(text("CREATE TABLE via_engine (a INTEGER)"))
        conn.execute(text("INSERT INTO via_engine VALUES (1)"))
        assert conn.execute(text("SELECT a FROM via_engine")).scalar_one() == 1


def test_a_reader_engine_reads(initialized_config: Config) -> None:
    with writer_engine(initialized_config) as engine, engine.connect() as conn:
        conn.execute(text("CREATE TABLE via_engine (a INTEGER)"))
        conn.execute(text("INSERT INTO via_engine VALUES (7)"))

    with reader_engine(initialized_config) as engine, engine.connect() as conn:
        assert conn.execute(text("SELECT a FROM via_engine")).scalar_one() == 7


def test_a_reader_engine_inherits_the_read_only_file_handle(initialized_config: Config) -> None:
    """The role belongs to the handle, so the engine cannot widen it."""
    with writer_engine(initialized_config) as engine, engine.connect() as conn:
        conn.execute(text("CREATE TABLE via_engine (a INTEGER)"))

    with reader_engine(initialized_config) as engine, engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA query_only = OFF")
        with pytest.raises(Exception, match="readonly|read-only|attempt to write"):
            conn.execute(text("INSERT INTO via_engine VALUES (1)"))


def test_the_engine_speaks_to_the_encrypted_store_not_a_new_one(
    initialized_config: Config,
) -> None:
    """A creator that opened its own connection would silently create a plaintext file."""
    with writer_engine(initialized_config) as engine, engine.connect() as conn:
        version = conn.execute(text("SELECT MAX(version) FROM schema_version")).scalar_one()
    assert version == 1

    siblings = list(initialized_config.datastore_path.parent.glob("*.db"))
    assert siblings == [initialized_config.datastore_path]


def test_engine_transaction_control_is_inert_over_these_handles(initialized_config: Config) -> None:
    """Pin the autocommit semantics the engines inherit from `store.connection`.

    This is not an endorsement of the behaviour -- it is a fence around it. The
    handles are opened `isolation_level=None` so the reader releases its
    snapshot per statement and the migration runner owns its own transaction, and
    the engine wraps that handle rather than replacing it. A future chunk that
    reaches for `engine.begin()` expecting atomicity will get none, so the
    expectation is asserted here rather than discovered during a rebuild.
    """
    with writer_engine(initialized_config) as engine:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE rolled_back (a INTEGER)"))
            conn.execute(text("INSERT INTO rolled_back VALUES (1)"))
            conn.rollback()

        with engine.connect() as conn:
            surviving = conn.execute(text("SELECT COUNT(*) FROM rolled_back")).scalar_one()

    assert surviving == 1, (
        "a rollback through the engine discarded the write -- the handles are no longer in "
        "autocommit, and store/engine.py's docstring plus this fence both need revisiting"
    )
