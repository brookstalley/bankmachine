"""The keychain seam, against the real OS keychain under a test-scoped service."""

from __future__ import annotations

import pytest

from bankmachine.config import Config
from bankmachine.secrets import (
    KEY_HEX_LENGTH,
    DatastoreKeyMissingError,
    SecretsError,
    delete_datastore_key,
    ensure_datastore_key,
    generate_datastore_key,
    get_datastore_key,
    set_datastore_key,
)


def test_a_generated_key_is_256_bits_of_hex() -> None:
    key = generate_datastore_key()
    assert len(key) == KEY_HEX_LENGTH
    int(key, 16)


def test_generated_keys_differ() -> None:
    assert generate_datastore_key() != generate_datastore_key()


def test_a_key_round_trips_through_the_real_keychain(config: Config) -> None:
    key = generate_datastore_key()
    set_datastore_key(config, key)
    assert get_datastore_key(config) == key


def test_a_missing_key_names_the_state_and_both_remedies(config: Config) -> None:
    """Not "run store init" alone: every path that reaches this error has a datastore.

    `writer()` and `reader()` check the datastore exists before asking for a key,
    and `store init` refuses to mint one for an existing store -- so advice that
    named only that command would send the operator in a circle.
    """
    with pytest.raises(DatastoreKeyMissingError) as excinfo:
        get_datastore_key(config)
    message = str(excinfo.value)
    assert "cannot be recovered from an existing datastore" in message
    assert "restore the keychain entry" in message
    assert "only when no datastore exists yet" in message


def test_ensure_creates_a_key_once_and_then_returns_it(config: Config) -> None:
    first, created = ensure_datastore_key(config)
    assert created is True
    second, created_again = ensure_datastore_key(config)
    assert created_again is False
    assert second == first


def test_a_malformed_key_is_rejected_without_echoing_it(config: Config) -> None:
    with pytest.raises(SecretsError) as excinfo:
        set_datastore_key(config, "not-a-key")
    assert "not-a-key" not in str(excinfo.value)


def test_deleting_an_absent_key_is_not_an_error(config: Config) -> None:
    delete_datastore_key(config)
    delete_datastore_key(config)


def test_sandbox_and_production_keys_do_not_collide(config: Config) -> None:
    from dataclasses import replace

    production = replace(config, environment="production")
    set_datastore_key(config, generate_datastore_key())
    try:
        with pytest.raises(DatastoreKeyMissingError):
            get_datastore_key(production)
    finally:
        delete_datastore_key(production)
