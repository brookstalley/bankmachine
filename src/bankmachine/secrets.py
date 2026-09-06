"""The keychain seam: the datastore key and, later, aggregator credentials.

This is the only module that imports `keyring` (AC-10.1). Everything else asks
here, so the one part of the system that is genuinely painful to port lives
behind a single interface.

Nothing in this module returns a secret into a log line, an exception message or
a `repr`. A `KeyError` naming the account is fine; the value never is.
"""

from __future__ import annotations

from secrets import token_hex

import keyring
from keyring.errors import KeyringError

from bankmachine.config import Config

#: SQLCipher takes a 256-bit raw key as 64 hex characters. Passing the key raw
#: rather than as a passphrase skips SQLCipher's KDF, which means the key in the
#: keychain IS the key -- there is no derivation whose parameters could drift
#: between the process that created the datastore and the one that opens it.
KEY_BYTES = 32
KEY_HEX_LENGTH = KEY_BYTES * 2


class SecretsError(Exception):
    """The keychain could not be reached, or held something unusable."""


class DatastoreKeyMissingError(SecretsError):
    """No datastore key exists for this configuration."""


def generate_datastore_key() -> str:
    """A fresh 256-bit key as lowercase hex."""
    return token_hex(KEY_BYTES)


def _validate(key: str, *, service: str, account: str) -> str:
    """Reject anything SQLCipher would silently accept as a different key.

    The value is never included in the message -- a malformed key is still a
    key, and the exception text reaches logs.
    """
    if len(key) != KEY_HEX_LENGTH:
        raise SecretsError(
            f"datastore key in {service}/{account} is {len(key)} characters, "
            f"expected {KEY_HEX_LENGTH} hex characters"
        )
    try:
        int(key, 16)
    except ValueError as exc:
        raise SecretsError(f"datastore key in {service}/{account} is not hexadecimal") from exc
    return key.lower()


def get_datastore_key(config: Config) -> str:
    """The datastore key for this environment, from the OS keychain."""
    service, account = config.keychain_service, config.keychain_account
    try:
        stored = keyring.get_password(service, account)
    except KeyringError as exc:
        raise SecretsError(f"keychain {service}/{account} could not be read: {exc}") from exc
    if stored is None:
        # Deliberately not "run `bankmachine store init`". Both `writer()` and
        # `reader()` check that the datastore exists BEFORE they ask for a key,
        # so every path that reaches this line has a datastore in hand -- and
        # `store init` refuses to mint a key for one of those, correctly. The
        # message names the state and both real remedies instead.
        raise DatastoreKeyMissingError(
            f"no datastore key in keychain {service}/{account}. A key cannot be recovered from an "
            f"existing datastore: restore the keychain entry from your backup, or move the "
            f"datastore aside. `bankmachine store init` creates a key only when no datastore "
            f"exists yet"
        )
    return _validate(stored, service=service, account=account)


def set_datastore_key(config: Config, key: str) -> None:
    """Store the datastore key, replacing any existing one."""
    service, account = config.keychain_service, config.keychain_account
    _validate(key, service=service, account=account)
    try:
        keyring.set_password(service, account, key.lower())
    except KeyringError as exc:
        raise SecretsError(f"keychain {service}/{account} could not be written: {exc}") from exc


def delete_datastore_key(config: Config) -> None:
    """Remove the datastore key. Absence is not an error."""
    service, account = config.keychain_service, config.keychain_account
    try:
        keyring.delete_password(service, account)
    except keyring.errors.PasswordDeleteError:
        return
    except KeyringError as exc:
        raise SecretsError(f"keychain {service}/{account} could not be cleared: {exc}") from exc


def ensure_datastore_key(config: Config) -> tuple[str, bool]:
    """The datastore key, generating and storing one if none exists.

    Returns the key and whether it was newly created. Only `store init` may call
    this: every other caller uses `get_datastore_key`, so a missing key is an
    error rather than a silent new datastore nobody can decrypt tomorrow.
    """
    try:
        return get_datastore_key(config), False
    except DatastoreKeyMissingError:
        key = generate_datastore_key()
        set_datastore_key(config, key)
        return key, True
