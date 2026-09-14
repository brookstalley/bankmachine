"""What a failed sync attempt records as its `last_error_code`.

🔴 **The aggregator's own code wherever it sent one, verbatim.** AC-4.2 asks
for the error code, and the aggregator's is the one a support conversation with
it needs. A local translation would cost exactly that conversation.

🔴 **Otherwise one of `LOCAL_FAILURE_CODES`, never a Python class name.** Some
failures reach no aggregator at all -- a keychain that will not give up a
token, a writer lock a backup is holding -- and some reach it without a code
coming back. A class name in their place is implementation detail published
on `get_pipeline_health`, and it changes the day somebody renames the class.
The set is closed and published in `api-contract.md`, and
`tests/preferences/test_the_failure_code_vocabulary_is_closed.py` holds the
two together.

Resolved by walking the exception's MRO, so a subclass nobody mapped still
lands on its nearest mapped ancestor. Each code names what the operator has to
do, following the taxonomy in `connector/__init__.py`, rather than the class
that happened to be raised.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from bankmachine.connector import (
    AggregatorError,
    AggregatorNotConfiguredError,
    ConnectorError,
    TransportError,
)
from bankmachine.secrets import SecretsError
from bankmachine.store.connection import AnotherWriterRunningError, StoreError
from bankmachine.store.derivation import DerivationError, UnknownEndpointError

_BY_TYPE: Final[Mapping[type, str]] = {
    # The connection's access token could not be read from the keychain.
    SecretsError: "CREDENTIAL_UNREADABLE",
    # The aggregator did not answer, so nothing about the request was judged.
    TransportError: "AGGREGATOR_UNREACHABLE",
    AggregatorNotConfiguredError: "AGGREGATOR_NOT_CONFIGURED",
    # A refusal whose body carried no code. Reached only when the code is
    # missing: one that is present is recorded verbatim before this is read.
    AggregatorError: "AGGREGATOR_REFUSED_WITHOUT_CODE",
    # An answer this build cannot use -- malformed, credential-bearing, or an
    # archived page that does not say what window it answered.
    ConnectorError: "RESPONSE_UNUSABLE",
    # Typically `store backup` holding the writer lock beside the sync.
    AnotherWriterRunningError: "DATASTORE_LOCKED",
    DerivationError: "DERIVATION_FAILED",
    UnknownEndpointError: "DERIVATION_FAILED",
    StoreError: "DATASTORE_FAILED",
}

#: Every code this product records on its own behalf. Disjoint from the
#: aggregator's vocabulary, so a reader can tell whose code they are looking at.
LOCAL_FAILURE_CODES: Final[frozenset[str]] = frozenset(_BY_TYPE.values())


def failure_code(exc: ConnectorError | StoreError | SecretsError) -> str:
    """The code one failed attempt records: the aggregator's if it sent one, else ours."""
    if isinstance(exc, ConnectorError) and exc.error_code:
        return exc.error_code
    return local_failure_code(type(exc))


def local_failure_code(kind: type[Exception]) -> str:
    """The code this product records for a failure of this type that carried no aggregator code.

    Takes the type rather than an instance, because a grouping class such as
    `AggregatorError` refuses to be constructed and still has to resolve.
    """
    for klass in kind.__mro__:
        code = _BY_TYPE.get(klass)
        if code is not None:
            return code
    raise TypeError(f"{kind.__name__} is not a failure a sync attempt records")
