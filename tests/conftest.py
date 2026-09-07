"""Shared fixtures.

Integration tests run against real SQLCipher and the real OS keychain, under a
test-scoped service name. Mocks would not answer the questions these tests ask:
every norm here was written because a *measured* behaviour contradicted a
plausible assumption, and a mock returns the assumption.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from bankmachine.config import DEFAULT_CONNECTION_CAP, MAX_HISTORY_DAYS, Config, Environment
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
    plaid_client_id: str | None = None,
    history_days: int = MAX_HISTORY_DAYS,
    connection_cap: int = DEFAULT_CONNECTION_CAP,
) -> Config:
    return Config(
        environment=environment,
        datastore_path=tmp_path / datastore_name,
        log_dir=tmp_path / "logs",
        keychain_service=keychain_service,
        busy_timeout_ms=200,
        plaid_client_id=plaid_client_id,
        history_days=history_days,
        connection_cap=connection_cap,
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


#: The recorded rejection whose shape every constructed error body is built from.
#:
#: The offline suite has to construct error bodies for codes that cannot be
#: provoked -- there is no way to make the sandbox return `ITEM_LOCKED` on
#: demand. What it must not do is *invent the shape* while doing so: a
#: hand-written body tests the taxonomy against what its author remembered, and
#: the fields they forget are exactly the ones nothing then checks. This file was
#: recorded verbatim from a real rejection by `tests/connector/test_sandbox.py`,
#: and is the one that carries a non-null `error_type` -- the field the
#: taxonomy's second classification layer reads.
#:
#: It lives in this conftest rather than beside the connector tests because two
#: `conftest.py` files in one un-packaged tree collide under mypy.
ERROR_SHAPE_FIXTURE = Path(__file__).parent / "connector" / "fixtures" / "error_invalid_field.json"


@pytest.fixture(scope="session")
def error_body() -> Callable[..., str]:
    """Build an aggregator error body in the shape the aggregator actually sends.

    Session-scoped because it reads one file and returns a pure function; nothing
    it hands out is mutable state shared between tests. When the aggregator
    changes its error body, the sandbox test fails on the recorded keys and every
    offline test that builds one moves with it.
    """
    recorded: dict[str, Any] = json.loads(ERROR_SHAPE_FIXTURE.read_text())

    def build(**fields: object) -> str:
        payload = dict(recorded)
        payload.update(fields)
        return json.dumps(payload)

    return build
