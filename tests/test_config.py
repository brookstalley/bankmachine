"""Configuration precedence and documented defaults (AC-ARCH.4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bankmachine.config import APP_NAME, ConfigError, default_config_path, load_config


def test_defaults_land_under_the_documented_base_directories(tmp_path: Path) -> None:
    env = {
        "HOME": str(tmp_path),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
    }
    config = load_config(env=env, config_path=tmp_path / "absent.toml")

    assert config.datastore_path == tmp_path / "data" / APP_NAME / "store-sandbox.db"
    assert config.log_dir == tmp_path / "state" / APP_NAME / "logs"
    assert config.keychain_service == APP_NAME
    assert config.config_path is None


def test_the_xdg_fallback_resolves_through_the_injected_home(tmp_path: Path) -> None:
    """The documented macOS default, with no XDG variable set.

    This is the branch `_home` exists for, and until it was asserted the whole
    seam could regress to `Path.home()` with every other config test still
    green -- they either set the XDG vars, or assert only that a path is
    absolute. Everything here must land under `tmp_path`, which the developer's
    real home never does.
    """
    config = load_config(env={"HOME": str(tmp_path)}, config_path=tmp_path / "absent.toml")

    assert config.datastore_path == tmp_path / ".local" / "share" / APP_NAME / "store-sandbox.db"
    assert config.log_dir == tmp_path / ".local" / "state" / APP_NAME / "logs"
    assert default_config_path({"HOME": str(tmp_path)}) == (
        tmp_path / ".config" / APP_NAME / "config.toml"
    )


def test_the_environment_defaults_to_sandbox(tmp_path: Path) -> None:
    """Production is opted into, never fallen into."""
    config = load_config(env={"HOME": str(tmp_path)}, config_path=tmp_path / "absent.toml")
    assert config.environment == "sandbox"


def test_sandbox_and_production_default_to_different_datastores(tmp_path: Path) -> None:
    """AC-10.6: putting fixture data in the real store should need an explicit override."""
    base = {"HOME": str(tmp_path), "XDG_DATA_HOME": str(tmp_path / "data")}
    sandbox = load_config(env=base, config_path=tmp_path / "absent.toml")
    production = load_config(
        env={**base, "BANKMACHINE_ENVIRONMENT": "production"},
        config_path=tmp_path / "absent.toml",
    )
    assert sandbox.datastore_path != production.datastore_path
    assert sandbox.keychain_account != production.keychain_account


def test_an_environment_variable_beats_the_config_file(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('datastore_path = "/from/file.db"\n', encoding="utf-8")
    config = load_config(
        env={"HOME": str(tmp_path), "BANKMACHINE_DATASTORE_PATH": str(tmp_path / "from-env.db")},
        config_path=config_file,
    )
    assert config.datastore_path == tmp_path / "from-env.db"


def test_the_config_file_beats_the_default(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'datastore_path = "{tmp_path / "from-file.db"}"\nkeychain_service = "custom"\n',
        encoding="utf-8",
    )
    config = load_config(env={"HOME": str(tmp_path)}, config_path=config_file)
    assert config.datastore_path == tmp_path / "from-file.db"
    assert config.keychain_service == "custom"
    assert config.config_path == config_file


def test_a_missing_config_file_is_not_an_error(tmp_path: Path) -> None:
    config = load_config(env={"HOME": str(tmp_path)}, config_path=tmp_path / "nope.toml")
    assert config.config_path is None


def test_malformed_toml_is_reported_rather_than_ignored(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("this is not toml = = =\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(env={"HOME": str(tmp_path)}, config_path=config_file)


def test_an_unknown_environment_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="environment must be one of"):
        load_config(
            env={"HOME": str(tmp_path), "BANKMACHINE_ENVIRONMENT": "staging"},
            config_path=tmp_path / "absent.toml",
        )


def test_a_negative_busy_timeout_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="must not be negative"):
        load_config(
            env={"HOME": str(tmp_path), "BANKMACHINE_BUSY_TIMEOUT_MS": "-1"},
            config_path=tmp_path / "absent.toml",
        )


def test_resolved_paths_are_absolute(tmp_path: Path) -> None:
    config = load_config(
        env={"HOME": str(tmp_path), "BANKMACHINE_DATASTORE_PATH": "relative/store.db"},
        config_path=tmp_path / "absent.toml",
    )
    assert config.datastore_path.is_absolute()
    assert config.log_dir.is_absolute()


def test_the_lock_sits_beside_the_datastore(tmp_path: Path) -> None:
    config = load_config(
        env={"HOME": str(tmp_path), "BANKMACHINE_DATASTORE_PATH": str(tmp_path / "s.db")},
        config_path=tmp_path / "absent.toml",
    )
    assert config.lock_path == tmp_path / "s.db.lock"


def test_the_default_config_path_follows_xdg(tmp_path: Path) -> None:
    assert default_config_path({"XDG_CONFIG_HOME": str(tmp_path)}) == (
        tmp_path / APP_NAME / "config.toml"
    )
