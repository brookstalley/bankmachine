"""The keychain seam: the datastore key and, later, aggregator credentials.

This is the only module that imports `keyring` (AC-10.1). Everything else asks
here, so the one part of the system that is genuinely painful to port lives
behind a single interface.

Nothing in this module returns a secret into a log line, an exception message or
a `repr`. A `KeyError` naming the account is fine; the value never is.
"""

from __future__ import annotations

import re
from secrets import token_hex

import keyring
from keyring.errors import KeyringError

from bankmachine.config import Config, require_chosen_environment

#: SQLCipher takes a 256-bit raw key as 64 hex characters. Passing the key raw
#: rather than as a passphrase skips SQLCipher's KDF, which means the key in the
#: keychain IS the key -- there is no derivation whose parameters could drift
#: between the process that created the datastore and the one that opens it.
KEY_BYTES = 32
KEY_HEX_LENGTH = KEY_BYTES * 2

#: The key alphabet, matched in full. See `_validate` for why this is not `int`.
_HEX_KEY = re.compile(f"[0-9a-fA-F]{{{KEY_HEX_LENGTH}}}")


class SecretsError(Exception):
    """The keychain could not be reached, or held something unusable."""


class DatastoreKeyMissingError(SecretsError):
    """No datastore key exists for this configuration."""


class AccessTokenMissingError(SecretsError):
    """A connection's row exists but its access token does not.

    Its own type because its remedy is neither of the other two: the datastore
    key is unrecoverable and the aggregator secret is re-readable from a
    dashboard, but an access token can only be restored by re-enrolling that one
    institution -- which is an operator action against one connection, not a
    credential to paste.
    """


class AggregatorCredentialMissingError(SecretsError):
    """No aggregator secret exists for this configuration.

    Distinct from `DatastoreKeyMissingError` because the remedies share nothing:
    a datastore key cannot be recovered once lost, while an aggregator secret is
    always re-readable from the aggregator's own dashboard.
    """


class KeyEscrowRefusedError(SecretsError):
    """An escrow operation was refused because performing it would leak the key.

    Its own type because its remedy is unlike the others here: nothing is
    missing and nothing is unreachable -- the operator is being told that the
    way they asked would have leaked the key, and to ask a different way.

    It is DEFINED here and raised in `cli/store.py`, which is deliberate and
    worth stating plainly rather than dressing up: the conditions are all
    properties of an invocation (a redirected stdout, an unnamed destination, a
    path inside the data directory), and those live at the command surface. What
    belongs in this module is the vocabulary, so the refusal is recognisably a
    secrets failure to every `except` that already catches one.
    """


def generate_datastore_key() -> str:
    """A fresh 256-bit key as lowercase hex."""
    return token_hex(KEY_BYTES)


def validate_candidate_key(key: str) -> str:
    """Check a key the operator supplied, before anything tries to open a store with it.

    Separate from `_validate` because the message is the whole point. `_validate`
    names the keychain entry holding a bad value, which is right for a key
    already stored and wrong for one being offered by hand.

    🔴 And an unchecked candidate fails in a way that misleads. SQLCipher uses
    the quoted value as a raw key ONLY when it is exactly the right number of hex
    digits, and otherwise treats it as a passphrase to run through its KDF -- so
    a candidate with a typo'd length does not report "that is not a key", it
    reports that the datastore did not open. True, useless, and indistinguishable
    from a correct key offered against the wrong datastore.
    """
    candidate = key.strip()
    return _check_shape(
        candidate,
        wrong_length=(
            f"that is {len(candidate)} characters; a datastore key is exactly "
            f"{KEY_HEX_LENGTH} hex characters. Nothing was checked against the datastore"
        ),
        not_hex=(
            "that is not hexadecimal; a datastore key is 64 characters of 0-9 and a-f. "
            "Nothing was checked against the datastore"
        ),
    )


def _check_shape(key: str, *, wrong_length: str, not_hex: str) -> str:
    """The one shape test. Two callers, because only the WORDING differs.

    A key already in the keychain and a key someone just typed fail for the same
    reason and need different sentences -- one names the entry holding a bad
    value, the other tells a person what they just mistyped. The test itself must
    not fork: two copies of a security check drift, and the copy that drifts is
    the one nobody is looking at.

    A full match on the hex alphabet, not `int(key, 16)`. `int` is a parser
    rather than a predicate: it accepts an `0x` prefix, `_` separators, a sign
    and surrounding whitespace, so `"0x" + "a" * 62` is 64 characters and passes
    both checks. SQLCipher uses the quoted value as a raw key only when it is
    exactly the right number of hex digits and otherwise treats it as a
    passphrase to run through its KDF -- which is the one class of value this
    function exists to reject, because the store then works until the KDF's
    default parameters change under it.

    The value is never included in either message -- a malformed key is still a
    key, and exception text reaches logs.
    """
    if len(key) != KEY_HEX_LENGTH:
        raise SecretsError(wrong_length)
    if not _HEX_KEY.fullmatch(key):
        raise SecretsError(not_hex)
    return key.lower()


def _validate(key: str, *, service: str, account: str) -> str:
    """Reject anything SQLCipher would silently accept as a different key."""
    return _check_shape(
        key,
        wrong_length=(
            f"datastore key in {service}/{account} is {len(key)} characters, "
            f"expected {KEY_HEX_LENGTH} hex characters"
        ),
        not_hex=f"datastore key in {service}/{account} is not hexadecimal",
    )


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
            f"existing datastore: restore it from your backup with `bankmachine store key import`, "
            f"or move the datastore aside. `bankmachine store init` creates a key only when no "
            f"datastore exists yet"
        )
    return _validate(stored, service=service, account=account)


def set_datastore_key(config: Config, key: str) -> None:
    """Store the datastore key, replacing any existing one."""
    require_chosen_environment(config)
    service, account = config.keychain_service, config.keychain_account
    _validate(key, service=service, account=account)
    try:
        keyring.set_password(service, account, key.lower())
    except KeyringError as exc:
        raise SecretsError(f"keychain {service}/{account} could not be written: {exc}") from exc


def delete_datastore_key(config: Config) -> None:
    """Remove the datastore key. Absence is not an error."""
    require_chosen_environment(config)
    service, account = config.keychain_service, config.keychain_account
    try:
        keyring.delete_password(service, account)
    except keyring.errors.PasswordDeleteError:
        return
    except KeyringError as exc:
        raise SecretsError(f"keychain {service}/{account} could not be cleared: {exc}") from exc


def get_plaid_secret(config: Config) -> str:
    """The aggregator secret for this environment, from the OS keychain.

    Unvalidated on purpose: the aggregator's secret format is theirs to change,
    and a length or alphabet check here would reject a rotated credential that
    works. The datastore key is validated because a malformed one is silently
    accepted by SQLCipher as a *different* key; a malformed aggregator secret
    produces an authentication error from the aggregator, which is loud already.
    """
    service, account = config.keychain_service, config.plaid_keychain_account
    try:
        stored = keyring.get_password(service, account)
    except KeyringError as exc:
        raise SecretsError(f"keychain {service}/{account} could not be read: {exc}") from exc
    if stored is None:
        raise AggregatorCredentialMissingError(
            f"no aggregator secret in keychain {service}/{account}. Store the secret for the "
            f"{config.environment} environment with `bankmachine connector set-secret`"
        )
    if not stored.strip():
        raise SecretsError(f"aggregator secret in {service}/{account} is empty")
    return stored


def set_plaid_secret(config: Config, secret: str) -> None:
    """Store the aggregator secret, replacing any existing one."""
    require_chosen_environment(config)
    service, account = config.keychain_service, config.plaid_keychain_account
    if not secret.strip():
        raise SecretsError("refusing to store an empty aggregator secret")
    try:
        keyring.set_password(service, account, secret)
    except KeyringError as exc:
        raise SecretsError(f"keychain {service}/{account} could not be written: {exc}") from exc


def delete_plaid_secret(config: Config) -> None:
    """Remove the aggregator secret. Absence is not an error."""
    require_chosen_environment(config)
    service, account = config.keychain_service, config.plaid_keychain_account
    try:
        keyring.delete_password(service, account)
    except keyring.errors.PasswordDeleteError:
        return
    except KeyringError as exc:
        raise SecretsError(f"keychain {service}/{account} could not be cleared: {exc}") from exc


def get_access_token(config: Config, credential_ref: str) -> str:
    """One connection's access token, by the handle its `connections` row holds.

    🔴 `credential_ref` is a keychain account name, not a credential. That is the
    whole reason the column can live in the datastore: a backup travels, and a
    backup carrying access tokens would be a permanent leak (AC-10.1). The
    lookup fails loudly rather than returning None, because a sync that silently
    skipped a connection whose credential vanished would report success over
    data it never fetched.
    """
    service = config.keychain_service
    try:
        stored = keyring.get_password(service, credential_ref)
    except KeyringError as exc:
        raise SecretsError(f"keychain {service}/{credential_ref} could not be read: {exc}") from exc
    if stored is None:
        raise AccessTokenMissingError(
            f"no access token in keychain {service}/{credential_ref}. The connection exists in "
            f"the datastore but its credential does not; re-enrol the institution to restore it"
        )
    if not stored.strip():
        raise SecretsError(f"access token in {service}/{credential_ref} is empty")
    return stored


def set_access_token(config: Config, credential_ref: str, access_token: str) -> None:
    """Store one connection's access token under its handle."""
    require_chosen_environment(config)
    service = config.keychain_service
    if not access_token.strip():
        raise SecretsError("refusing to store an empty access token")
    try:
        keyring.set_password(service, credential_ref, access_token)
    except KeyringError as exc:
        raise SecretsError(
            f"keychain {service}/{credential_ref} could not be written: {exc}"
        ) from exc


def delete_access_token(config: Config, credential_ref: str) -> None:
    """Remove one connection's access token. Absence is not an error."""
    require_chosen_environment(config)
    service = config.keychain_service
    try:
        keyring.delete_password(service, credential_ref)
    except keyring.errors.PasswordDeleteError:
        return
    except KeyringError as exc:
        raise SecretsError(
            f"keychain {service}/{credential_ref} could not be cleared: {exc}"
        ) from exc


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
