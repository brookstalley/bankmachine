"""Shared fixtures.

Integration tests run against real SQLCipher and the real OS keychain, under a
test-scoped service name. Mocks would not answer the questions these tests ask:
every norm here was written because a *measured* behaviour contradicted a
plausible assumption, and a mock returns the assumption.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Iterator, Mapping
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


def _bankmachine_env(config: Config, **extra: str) -> dict[str, str]:
    """The `BANKMACHINE_*` pairs that point a run at THIS config.

    One definition, two deliveries: `child_env` merges it into a subprocess
    environment and `use_cli_env` sets it on the process. Splitting it here is
    what keeps an in-process fixture and a spawned child from drifting into two
    different ideas of what "this config" means.
    """
    return {
        "BANKMACHINE_DATASTORE_PATH": str(config.datastore_path),
        "BANKMACHINE_KEYCHAIN_SERVICE": config.keychain_service,
        "BANKMACHINE_LOG_DIR": str(config.log_dir),
        "BANKMACHINE_ENVIRONMENT": config.environment,
        **extra,
    }


def use_cli_env(
    monkeypatch: pytest.MonkeyPatch, config: Config, **extra: str
) -> Config:
    """Point an in-process `run([...])` at this config, and return it.

    🔴 The in-process twin of `child_env`, and it exists for the same reason:
    every CLI test module had its own `cli_env` fixture repeating the same five
    `setenv` calls, so the environment declaration -- which a write now REFUSES
    without -- was carried by memory at nine sites. The next module's author
    inherits it here instead.

    `BANKMACHINE_CONFIG` points at a file that does not exist, so a config file
    on the developer's machine cannot reach a test.
    """
    pairs = _bankmachine_env(
        config,
        BANKMACHINE_CONFIG=str(config.datastore_path.parent / "absent.toml"),
        **extra,
    )
    for key, value in pairs.items():
        monkeypatch.setenv(key, value)
    return config


def child_env(config: Config, **extra: str) -> dict[str, str]:
    """The environment a spawned child needs to resolve THIS config.

    🔴 One construction rather than a dict pasted at each spawn site. Every
    entry here is load-bearing and the failure mode differs per omission: drop
    the datastore path and the child works on the developer's real store, drop
    the keychain service and it reaches their real credentials, drop the
    environment and it exits 2 because a write on an environment nobody chose is
    refused (`require_chosen_environment`).

    That last one is why this exists. The guard was added by pasting
    `BANKMACHINE_ENVIRONMENT` into the three spawn sites that happened to be
    red, which carries the property by memory at every site instead of by
    construction at one -- the same decay the guard itself was written to avoid.
    The next test that spawns a writer inherits it from here instead.
    """
    return {**os.environ, **_bankmachine_env(config, **extra)}


@pytest.fixture
def initialized_config(config: Config) -> Config:
    """A configuration whose datastore exists and is at the current schema version."""
    from bankmachine.secrets import generate_datastore_key
    from bankmachine.store.migrations import migrate

    set_datastore_key(config, generate_datastore_key())
    migrate(config)
    return config


#: The variables that decide where a `load_config()` call lands. Written out
#: rather than discovered by prefix so that a seventh is a decision somebody
#: takes in this file, beside the guard that reads them.
CONFIGURATION_ENVIRONMENT = (
    "BANKMACHINE_CONFIG",
    "BANKMACHINE_DATASTORE_PATH",
    "BANKMACHINE_ENVIRONMENT",
    "BANKMACHINE_KEYCHAIN_SERVICE",
    "BANKMACHINE_LOG_DIR",
    "BANKMACHINE_PLAID_CLIENT_ID",
)

#: The variables whose value is a path this suite must never let escape the
#: pytest temp tree.
_PATH_VALUED = ("BANKMACHINE_CONFIG", "BANKMACHINE_DATASTORE_PATH", "BANKMACHINE_LOG_DIR")


@pytest.fixture(autouse=True)
def _no_test_resolves_its_paths_from_the_operators_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[None]:
    """Fail a test that pointed the product at anything outside the pytest temp tree.

    🔴 **The failure this exists to catch has already happened, and it was
    silent.** One test overrode the datastore path and the keychain service and
    stopped there, so its `load_config()` resolved `log_dir` from the
    developer's real environment -- and pytest temp paths were written into the
    operator's live `~/.local/state/bankmachine/logs/bankmachine.log`, the file
    that becomes the production log. Nothing failed. Nothing could: the test
    asserted on what the command printed, and where the log went was not part of
    any assertion anywhere.

    **Two rules, and the smaller one is the one that would have caught it.**
    A test that sets `BANKMACHINE_DATASTORE_PATH` is driving the product from
    the environment, and the log follows the datastore or it follows the
    operator -- there is no third option -- so `BANKMACHINE_LOG_DIR` must be set
    too. The second rule is the general form: any of these variables that names
    a path must name one inside the pytest temp tree.

    Deliberately NOT a rule that `BANKMACHINE_CONFIG` must be set. It is the
    right thing for a CLI fixture to do and every one of them does it, but the
    leak was the log, and a guard that fails tests for a second reason is a
    guard that gets read as noise the first time it fires for the wrong one.

    **It takes `monkeypatch` so that it can still see what the test set.**
    Finalizers run in reverse order of setup: requesting `monkeypatch` here
    forces it to be created first, which puts its undo *after* this check rather
    than before it, where there would be nothing left to look at.
    """
    yield

    complaint = configuration_leak(os.environ, tmp_path_factory.getbasetemp())
    if complaint is not None:
        raise AssertionError(complaint)


def configuration_leak(environ: Mapping[str, str], base: Path) -> str | None:
    """What escaped the temp tree, or `None`. Public so it can be driven directly.

    A guard nobody has watched fire is a claim rather than a check, and an
    autouse fixture is the hardest kind to watch: reddening it means writing a
    test that deliberately leaks, which is the thing this file exists to stop.
    Separating the judgment from the fixture lets both controls be ordinary
    assertions (`tests/test_config.py`).
    """
    set_here = {name: environ[name] for name in CONFIGURATION_ENVIRONMENT if name in environ}

    if "BANKMACHINE_DATASTORE_PATH" in set_here and "BANKMACHINE_LOG_DIR" not in set_here:
        return (
            "this test pointed BANKMACHINE_DATASTORE_PATH at a temporary store but left "
            "BANKMACHINE_LOG_DIR unset, so its logging went to the operator's real log "
            "directory. Set both, under tmp_path, the way every CLI fixture here does."
        )

    for name in _PATH_VALUED:
        value = set_here.get(name)
        if value is not None and not Path(value).is_relative_to(base):
            return (
                f"{name} is {value}, which is outside the pytest temp tree at {base}. "
                f"A test that resolves configuration from the environment must resolve it "
                f"to somewhere disposable."
            )
    return None


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
