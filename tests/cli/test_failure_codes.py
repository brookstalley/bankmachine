"""What a failed sync attempt records as `last_error_code` (AC-4.2).

The aggregator's code wherever it sent one, verbatim; this product's own code
otherwise. The closed-set guard over the whole hierarchy is
`tests/preferences/test_the_failure_code_vocabulary_is_closed.py`; these pin the
two rules on the exceptions a sync actually meets.
"""

from __future__ import annotations

import pytest

from bankmachine.cli.failure_codes import failure_code
from bankmachine.connector import (
    AggregatorNotConfiguredError,
    ConnectorError,
    InstitutionUnavailableError,
    MalformedResponseError,
    ReauthRequiredError,
    TransactionsPaginationRestartError,
    TransportError,
    UnrecognizedAggregatorError,
)
from bankmachine.connector.plaid.derivers import UndenominableAmountError
from bankmachine.connector.plaid.window import UnreadableWindowError
from bankmachine.secrets import AccessTokenMissingError, SecretsError
from bankmachine.store.connection import (
    AnotherWriterRunningError,
    DatastoreMissingError,
    StoreError,
)
from bankmachine.store.derivation import DerivationError, UnknownEndpointError


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (ReauthRequiredError("expired", error_code="ITEM_LOGIN_REQUIRED"), "ITEM_LOGIN_REQUIRED"),
        (
            TransactionsPaginationRestartError(
                "moved", error_code="TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"
            ),
            "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION",
        ),
        # Not an `AggregatorError`, and still the aggregator's answer: a rejected key
        # pair comes back with a code and is classified as a configuration problem.
        (
            AggregatorNotConfiguredError("rejected", error_code="INVALID_API_KEYS"),
            "INVALID_API_KEYS",
        ),
        # 🔴 A code this build has never seen is recorded as sent, not translated into
        # a neighbour -- finding out what it means has to cost a search, not a repro.
        (UnrecognizedAggregatorError("new", error_code="SOMETHING_NEW"), "SOMETHING_NEW"),
    ],
)
def test_the_aggregators_code_is_recorded_verbatim(exc: ConnectorError, code: str) -> None:
    assert failure_code(exc) == code


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (AccessTokenMissingError("no token"), "CREDENTIAL_UNREADABLE"),
        (SecretsError("keychain refused"), "CREDENTIAL_UNREADABLE"),
        (TransportError("no answer"), "AGGREGATOR_UNREACHABLE"),
        (AggregatorNotConfiguredError("no client id"), "AGGREGATOR_NOT_CONFIGURED"),
        (
            InstitutionUnavailableError("down, and said nothing more"),
            "AGGREGATOR_REFUSED_WITHOUT_CODE",
        ),
        (MalformedResponseError("not JSON"), "RESPONSE_UNUSABLE"),
        (UnreadableWindowError("no window"), "RESPONSE_UNUSABLE"),
        (AnotherWriterRunningError("backup holds the lock"), "DATASTORE_LOCKED"),
        (DerivationError("refused"), "DERIVATION_FAILED"),
        (UndenominableAmountError("no exponent"), "DERIVATION_FAILED"),
        (UnknownEndpointError("no deriver"), "DERIVATION_FAILED"),
        (DatastoreMissingError("gone"), "DATASTORE_FAILED"),
        (StoreError("refused a write"), "DATASTORE_FAILED"),
    ],
)
def test_a_failure_with_no_aggregator_code_records_this_products_own(
    exc: ConnectorError | StoreError | SecretsError, code: str
) -> None:
    assert failure_code(exc) == code


def test_an_empty_aggregator_code_is_no_code() -> None:
    """An empty string is the aggregator saying nothing, and recording it would
    publish a failure with no name at all."""
    assert failure_code(ReauthRequiredError("expired", error_code="")) == (
        "AGGREGATOR_REFUSED_WITHOUT_CODE"
    )


def test_a_class_name_is_never_what_gets_recorded() -> None:
    """The defect this module exists to close, asserted on the shape it took."""
    exc = TransportError("no answer")
    assert failure_code(exc) != type(exc).__name__
