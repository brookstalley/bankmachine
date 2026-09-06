"""`bankmachine store init` / `status` end to end, over a real encrypted datastore."""

from __future__ import annotations

from pathlib import Path

import pytest

from bankmachine.cli import run
from bankmachine.config import Config
from bankmachine.store.connection import SUPPORTED_SCHEMA_VERSION


@pytest.fixture
def cli_env(config: Config, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("BANKMACHINE_DATASTORE_PATH", str(config.datastore_path))
    monkeypatch.setenv("BANKMACHINE_LOG_DIR", str(config.log_dir))
    monkeypatch.setenv("BANKMACHINE_KEYCHAIN_SERVICE", config.keychain_service)
    monkeypatch.setenv("BANKMACHINE_ENVIRONMENT", config.environment)
    monkeypatch.setenv("BANKMACHINE_CONFIG", str(config.datastore_path.parent / "absent.toml"))
    return config


def test_status_reports_a_missing_datastore_without_creating_one(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = run(["store", "status"])
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "present:         no" in out
    assert "datastore missing" in out
    assert not cli_env.datastore_path.exists()


def test_init_creates_an_encrypted_datastore_at_the_current_schema_version(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = run(["store", "init"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "datastore created at" in out
    assert "journal mode:    wal" in out
    assert f"schema version:  {SUPPORTED_SCHEMA_VERSION}" in out
    assert "healthy:         yes" in out
    assert cli_env.datastore_path.read_bytes()[:16] != b"SQLite format 3\x00"


def test_init_is_idempotent(cli_env: Config, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["store", "init"]) == 0
    capsys.readouterr()

    assert run(["store", "init"]) == 0
    out = capsys.readouterr().out
    assert "datastore already present" in out
    assert "no migrations to apply" in out


def test_status_after_init_is_healthy(cli_env: Config, capsys: pytest.CaptureFixture[str]) -> None:
    run(["store", "init"])
    capsys.readouterr()

    assert run(["store", "status"]) == 0
    assert "healthy:         yes" in capsys.readouterr().out


def test_the_startup_banner_names_the_environment(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    run(["store", "status"])
    assert "environment=SANDBOX" in capsys.readouterr().err


def test_the_datastore_key_is_never_printed(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    run(["store", "init"])
    captured = capsys.readouterr()

    from bankmachine.secrets import get_datastore_key

    key = get_datastore_key(cli_env)
    assert key not in captured.out
    assert key not in captured.err
    log_file: Path = cli_env.log_dir / "bankmachine.log"
    if log_file.exists():
        assert key not in log_file.read_text(encoding="utf-8")


def test_init_refuses_to_mint_a_key_for_a_datastore_it_cannot_decrypt(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """The restored-from-backup path: datastore present, keychain entry gone.

    Generating a key here would store one that decrypts nothing, and the next
    `store status` would report an authentication failure instead of a missing
    key -- routing the operator to the wrong recovery for a state that is still
    fully recoverable.
    """
    from bankmachine.secrets import (
        DatastoreKeyMissingError,
        delete_datastore_key,
        get_datastore_key,
    )

    assert run(["store", "init"]) == 0
    capsys.readouterr()
    delete_datastore_key(cli_env)

    assert run(["store", "init"]) == 2
    err = capsys.readouterr().err
    assert "holds no key for it" in err
    assert "No key was generated" in err

    with pytest.raises(DatastoreKeyMissingError):
        get_datastore_key(cli_env)


def test_init_still_creates_a_key_when_there_is_no_datastore(cli_env: Config) -> None:
    """The refusal above must not break the ordinary first run."""
    from bankmachine.secrets import get_datastore_key

    assert not cli_env.datastore_path.exists()
    assert run(["store", "init"]) == 0
    assert get_datastore_key(cli_env)
