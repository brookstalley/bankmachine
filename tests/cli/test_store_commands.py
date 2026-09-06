"""`bankmachine store init` / `status` end to end, over a real encrypted datastore."""

from __future__ import annotations

from pathlib import Path

import pytest

from bankmachine.cli import run
from bankmachine.config import Config
from bankmachine.store.connection import SUPPORTED_SCHEMA_VERSION
from bankmachine.store.rebuild import RebuildReport


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


def test_rebuild_on_a_fresh_datastore_reports_that_it_changed_nothing(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["store", "init"]) == 0
    capsys.readouterr()

    exit_code = run(["store", "rebuild"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "raw responses replayed:  0" in out
    assert "content:                 identical to what it replaced" in out


def test_rebuild_refuses_a_datastore_that_does_not_exist(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    # `store init` is the only thing that creates a datastore; a typo'd path is
    # reported rather than turned into an empty store that rebuilds to nothing.
    exit_code = run(["store", "rebuild"])

    assert exit_code == 2
    assert "run `bankmachine store init`" in capsys.readouterr().err
    assert not cli_env.datastore_path.exists()


def _report(*, previous: tuple[int, ...], changed: bool) -> RebuildReport:
    """A report whose digests differ or match, so the printer's branches are reachable.

    Constructed rather than provoked: the two branches below are pure formatting
    over a `RebuildReport`, and driving a real datastore into each state would
    test the rebuild again rather than the sentence an operator reads.
    """
    return RebuildReport(
        responses_replayed=4,
        rows_deleted={"transactions": 3, "balances_daily": 1, "holdings": 0},
        derivation_version=2,
        derivation_version_id=7,
        previous_derivation_versions=previous,
        digest_before="aaaa",
        digest_after="bbbb" if changed else "aaaa",
    )


def test_rebuild_says_a_new_derivation_version_explains_the_difference(
    cli_env: Config, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "bankmachine.cli.store.rebuild",
        lambda config, **kwargs: _report(previous=(1,), changed=True),
    )

    assert run(["store", "rebuild"]) == 0
    out = capsys.readouterr().out

    assert "rows replaced:           4" in out
    assert "  holdings" not in out  # a table with nothing to replace is not listed
    assert "previous version(s):     1" in out
    assert "changed, as expected at a new derivation version" in out


def test_rebuild_names_an_accepted_change_as_unexplained(
    cli_env: Config, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only reachable under --accept-content-change; without it the rebuild raises
    # and this line is never printed against a difference nobody accounted for.
    monkeypatch.setattr(
        "bankmachine.cli.store.rebuild",
        lambda config, **kwargs: _report(previous=(2,), changed=True),
    )

    assert run(["store", "rebuild", "--accept-content-change"]) == 0

    assert "CHANGED at an unchanged derivation version, accepted" in capsys.readouterr().out
