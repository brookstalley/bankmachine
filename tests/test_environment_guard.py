"""A defaulted environment cannot receive a per-environment write.

Every container this product writes into is keyed on `environment` --
`datastore:<env>` and `plaid:<env>` in the keychain, `connection:<env>:<id>`, and
the datastore filename. When nothing chose the environment, the fallback still
names a container, and at the keychain a write into it is indistinguishable from
the operator having meant it. For the aggregator secret it is also unrecoverable:
the value it replaced is simply gone.

🔴 These tests assert the guard where it actually lives -- the one writer factory
and the keychain mutators -- rather than command by command. A test per command
would pass forever for the seventh command nobody added it for.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import keyring
import pytest

from bankmachine.cli import run as cli_run
from bankmachine.config import (
    Config,
    UnchosenEnvironmentError,
    display_path,
    load_config,
    require_chosen_environment,
)
from bankmachine.secrets import (
    delete_access_token,
    delete_datastore_key,
    delete_plaid_secret,
    generate_datastore_key,
    set_access_token,
    set_datastore_key,
    set_plaid_secret,
)
from bankmachine.store.connection import initializing_writer, reader, writer
from conftest import make_config


def unchosen(config: Config) -> Config:
    """The same configuration, as `load_config` would have produced it from nothing."""
    return dataclasses.replace(config, environment_source="default")


# --- where the environment came from ---------------------------------------


def test_nothing_choosing_the_environment_is_recorded_as_defaulted(tmp_path: Path) -> None:
    config = load_config(env={"HOME": str(tmp_path)}, config_path=tmp_path / "absent.toml")
    assert config.environment == "sandbox"
    assert config.environment_source == "default"
    assert not config.environment_chosen


def test_an_exported_variable_is_a_choice(tmp_path: Path) -> None:
    config = load_config(
        env={"HOME": str(tmp_path), "BANKMACHINE_ENVIRONMENT": "sandbox"},
        config_path=tmp_path / "absent.toml",
    )
    # Deliberately the SAME value the fallback would have produced: the guard
    # turns on who chose it, never on which environment was chosen. Asserting
    # this with "production" would pass even if the source were inferred from
    # the value.
    assert config.environment == "sandbox"
    assert config.environment_source == "environment variable"
    assert config.environment_chosen


def test_a_config_file_entry_is_a_choice(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('environment = "sandbox"\n')
    config = load_config(env={"HOME": str(tmp_path)}, config_path=config_file)

    assert config.environment == "sandbox"
    assert config.environment_source == "config file"
    assert config.environment_chosen


def test_a_config_file_that_omits_the_environment_is_not_a_choice(tmp_path: Path) -> None:
    """A file can exist and still say nothing about which environment this is."""
    config_file = tmp_path / "config.toml"
    config_file.write_text("history_days = 30\n")
    config = load_config(env={"HOME": str(tmp_path)}, config_path=config_file)

    assert config.config_path == config_file
    assert config.environment_source == "default"
    assert not config.environment_chosen


def test_an_empty_config_file_entry_is_not_a_choice(tmp_path: Path) -> None:
    """`environment = ""` selects the same fallback an absent key does.

    🔴 The guard turns on whether anybody chose, so a typo that resolves to
    nothing must not read as a choice -- that would satisfy the guard with a
    value that came from nowhere, which is the exact state it exists to refuse.
    """
    config_file = tmp_path / "config.toml"
    config_file.write_text('environment = ""\n')
    config = load_config(env={"HOME": str(tmp_path)}, config_path=config_file)

    assert config.environment == "sandbox"
    assert config.environment_source == "default"
    assert not config.environment_chosen


def test_constructing_a_configuration_directly_is_a_choice(
    tmp_path: Path, keychain_service: str
) -> None:
    """The caller named the environment in the constructor, which is choosing it."""
    assert make_config(tmp_path, keychain_service).environment_chosen


# --- the refusal itself -----------------------------------------------------


def test_the_refusal_names_both_ways_to_choose_an_environment(
    tmp_path: Path, keychain_service: str
) -> None:
    config = unchosen(make_config(tmp_path, keychain_service))
    with pytest.raises(UnchosenEnvironmentError) as raised:
        require_chosen_environment(config)

    message = str(raised.value)
    assert "BANKMACHINE_ENVIRONMENT=sandbox" in message
    assert 'environment = "sandbox"' in message
    assert "Nothing was written" in message


def test_a_chosen_environment_passes_the_guard(tmp_path: Path, keychain_service: str) -> None:
    require_chosen_environment(make_config(tmp_path, keychain_service))


def test_the_refusal_elides_the_operators_home_directory() -> None:
    """Errors reach terminals, bug reports and log files; an absolute path in one
    carries the operator's account name with it."""
    assert display_path(Path.home() / ".config" / "bankmachine" / "config.toml") == (
        "~/.config/bankmachine/config.toml"
    )
    assert display_path(Path("/etc/bankmachine.toml")) == "/etc/bankmachine.toml"


# --- the keychain mutators --------------------------------------------------


def test_a_defaulted_environment_cannot_overwrite_the_aggregator_secret(
    tmp_path: Path, keychain_service: str
) -> None:
    """#98's repro: `connector set-secret` in a shell that forgot to export.

    The assertion that matters is the read-back. A refusal that still wrote
    would raise and pass a test that only checked for the exception.
    """
    config = make_config(tmp_path, keychain_service)
    set_plaid_secret(config, "the-secret-that-was-already-there")
    try:
        with pytest.raises(UnchosenEnvironmentError):
            set_plaid_secret(unchosen(config), "the-production-secret")

        assert (
            keyring.get_password(keychain_service, config.plaid_keychain_account)
            == "the-secret-that-was-already-there"
        )
    finally:
        delete_plaid_secret(config)


def test_a_defaulted_environment_cannot_overwrite_the_datastore_key(
    tmp_path: Path, keychain_service: str
) -> None:
    config = make_config(tmp_path, keychain_service)
    original = generate_datastore_key()
    set_datastore_key(config, original)
    try:
        with pytest.raises(UnchosenEnvironmentError):
            set_datastore_key(unchosen(config), generate_datastore_key())

        assert keyring.get_password(keychain_service, config.keychain_account) == original
    finally:
        delete_datastore_key(config)


def test_a_defaulted_environment_cannot_overwrite_an_access_token(
    tmp_path: Path, keychain_service: str
) -> None:
    config = make_config(tmp_path, keychain_service)
    ref = config.connection_keychain_account("item-1")
    set_access_token(config, ref, "the-live-token")
    try:
        with pytest.raises(UnchosenEnvironmentError):
            set_access_token(unchosen(config), ref, "a-token-for-another-environment")

        assert keyring.get_password(keychain_service, ref) == "the-live-token"
    finally:
        delete_access_token(config, ref)


@pytest.mark.parametrize(
    ("delete", "store", "account_of"),
    [
        (
            delete_plaid_secret,
            lambda c: set_plaid_secret(c, "kept"),
            lambda c: c.plaid_keychain_account,
        ),
        (
            delete_datastore_key,
            lambda c: set_datastore_key(c, generate_datastore_key()),
            lambda c: c.keychain_account,
        ),
    ],
)
def test_a_defaulted_environment_cannot_delete_per_environment_credentials(
    tmp_path: Path,
    keychain_service: str,
    delete,  # noqa: ANN001 -- parametrized over two distinct signatures
    store,  # noqa: ANN001
    account_of,  # noqa: ANN001
) -> None:
    """Deletion is guarded for the same reason storing is: it is unrecoverable,
    and `delete_*` swallows absence, so an unguarded one would report nothing."""
    config = make_config(tmp_path, keychain_service)
    store(config)
    try:
        with pytest.raises(UnchosenEnvironmentError):
            delete(unchosen(config))

        assert keyring.get_password(keychain_service, account_of(config)) is not None
    finally:
        delete(config)


def test_a_defaulted_environment_cannot_delete_an_access_token(
    tmp_path: Path, keychain_service: str
) -> None:
    config = make_config(tmp_path, keychain_service)
    ref = config.connection_keychain_account("item-1")
    set_access_token(config, ref, "the-live-token")
    try:
        with pytest.raises(UnchosenEnvironmentError):
            delete_access_token(unchosen(config), ref)

        assert keyring.get_password(keychain_service, ref) == "the-live-token"
    finally:
        delete_access_token(config, ref)


# --- the datastore ----------------------------------------------------------


def test_a_defaulted_environment_cannot_open_the_datastore_for_writing(
    initialized_config: Config,
) -> None:
    with (
        pytest.raises(UnchosenEnvironmentError),
        writer(unchosen(initialized_config)),
    ):
        pass  # pragma: no cover -- the guard refuses before the handle exists


def test_a_defaulted_environment_cannot_create_a_datastore(
    tmp_path: Path, keychain_service: str
) -> None:
    """`store init` arrives here. Creating the sandbox store while meaning
    production is how an operator ends up with two stores and one of them empty."""
    config = make_config(tmp_path, keychain_service, datastore_name="fresh.db")
    set_datastore_key(config, generate_datastore_key())
    try:
        with (
            pytest.raises(UnchosenEnvironmentError),
            initializing_writer(unchosen(config)),
        ):
            pass  # pragma: no cover -- refused before the file is created

        assert not config.datastore_path.exists()
    finally:
        delete_datastore_key(config)


def test_the_refusal_reaches_the_operator_as_a_sentence_not_a_crash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 Exit 2 alone is not enough. Before `ConfigError` was caught as an
    expected failure this refusal landed in the unexpected-failure arm, which
    exits 2 *and* prints `failed unexpectedly` with a traceback -- so a
    deliberate refusal was indistinguishable from a crash, and a test asserting
    only the exit code passed the whole time.
    """
    for key in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
        monkeypatch.setenv(key, str(tmp_path / key.lower()))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("BANKMACHINE_ENVIRONMENT", raising=False)
    monkeypatch.delenv("BANKMACHINE_CONFIG", raising=False)

    assert cli_run(["store", "init"]) == 2

    captured = capsys.readouterr()
    assert "no environment was chosen" in captured.err
    assert "failed unexpectedly" not in captured.err
    assert "Traceback" not in captured.err


def test_a_defaulted_environment_may_still_read(initialized_config: Config) -> None:
    """The deliberate asymmetry: reads keep working under the fallback.

    This is what makes the guard shippable without breaking an existing sandbox
    workflow -- `store status`, `connections list` and the MCP server all read.
    """
    with reader(unchosen(initialized_config)) as conn:
        assert conn is not None
