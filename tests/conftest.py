"""Shared fixtures.

Integration tests run against real SQLCipher and the real OS keychain, under a
test-scoped service name. Mocks would not answer the questions these tests ask:
every norm here was written because a *measured* behaviour contradicted a
plausible assumption, and a mock returns the assumption.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from bankmachine.config import Config, Environment
from bankmachine.secrets import delete_datastore_key, set_datastore_key


@pytest.fixture
def keychain_service() -> Iterator[str]:
    """A service name unique to one test, so a run never touches real credentials."""
    yield f"bankmachine-test-{uuid.uuid4()}"


@pytest.fixture
def config(tmp_path: Path, keychain_service: str) -> Iterator[Config]:
    """A configuration pointing at a temporary datastore and a scratch keychain entry."""
    cfg = make_config(tmp_path, keychain_service)
    try:
        yield cfg
    finally:
        delete_datastore_key(cfg)


def make_config(
    tmp_path: Path,
    keychain_service: str,
    *,
    environment: Environment = "sandbox",
    datastore_name: str = "store.db",
) -> Config:
    return Config(
        environment=environment,
        datastore_path=tmp_path / datastore_name,
        log_dir=tmp_path / "logs",
        keychain_service=keychain_service,
        busy_timeout_ms=200,
        config_path=None,
    )


@pytest.fixture
def initialized_config(config: Config) -> Config:
    """A configuration whose datastore exists and is at the current schema version."""
    from bankmachine.secrets import generate_datastore_key
    from bankmachine.store.migrations import migrate

    set_datastore_key(config, generate_datastore_key())
    migrate(config)
    return config


@pytest.fixture(autouse=True)
def _isolate_application_logging() -> Iterator[None]:
    """Leave the application logger exactly as each test found it.

    `configure_logging` attaches a file handler under the calling test's tmp
    directory. Without this, that handler outlives its test and later tests log
    into a directory that no longer belongs to them -- or, once something calls
    `logging.shutdown()`, into a closed stream. Tests are independent or they
    are not tests.
    """
    import logging

    logger = logging.getLogger("bankmachine")
    handlers, level, propagate = logger.handlers[:], logger.level, logger.propagate
    try:
        yield
    finally:
        for handler in logger.handlers[:]:
            if handler not in handlers:
                logger.removeHandler(handler)
                handler.close()
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate
