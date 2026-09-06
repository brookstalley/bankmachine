"""The connector's boundary: what comes off the wire is what reaches the caller.

The fake response below mirrors the shape `verify-api` established by reading
`plaid-python` 44.0.0's own source: with `_preload_content=False`,
`api_client.call_api` returns before its deserialize branch, handing back the
underlying HTTP response, whose `.data` is undecoded bytes and whose connection
is returned with `.release_conn()`. It is a fake of a verified shape rather than
a fake of an assumed one, which is the only kind worth having.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import plaid
import pytest
import urllib3
import urllib3.exceptions

from bankmachine.config import Config
from bankmachine.connector import (
    INSTITUTIONS_GET,
    AggregatorNotConfiguredError,
    ConnectorError,
    Endpoint,
)
from bankmachine.connector.plaid.client import PlaidClient, _describe_failure


class FakeHttpResponse:
    """Stands in for the urllib3 response the SDK returns undecoded."""

    def __init__(self, data: object) -> None:
        self.data = data
        self.released = False

    def release_conn(self) -> None:
        self.released = True


@pytest.fixture
def client_config(config: Config) -> Config:
    return replace(config, plaid_client_id="test-client-id")


def _client(config: Config) -> PlaidClient:
    return PlaidClient(config, "test-secret")


def test_a_missing_client_id_is_refused_before_anything_is_built(config: Config) -> None:
    assert config.plaid_client_id is None
    with pytest.raises(AggregatorNotConfiguredError, match="client id"):
        PlaidClient(config, "test-secret")


def test_the_body_reaches_the_caller_byte_for_byte(client_config: Config) -> None:
    # Deliberately not valid JSON, and deliberately carrying bytes that a decode
    # and re-encode would alter. The archive stores what was received; if this
    # module ever parses on the way through, this test is what says so.
    body = b'{"total": 1, "raw": "\xc3\xa9\xed\xa0\x80", "trailing": }'
    response = FakeHttpResponse(body)
    captured: dict[str, Any] = {}

    def invoke(request: object, **kwargs: Any) -> FakeHttpResponse:
        captured.update(kwargs)
        captured["request"] = request
        return response

    with _client(client_config) as client:
        fetched = client._fetch(INSTITUTIONS_GET, invoke, object(), request_context="count=1")

    assert fetched.body == body
    assert fetched.endpoint == INSTITUTIONS_GET
    assert fetched.request_context == "count=1"
    assert captured["_preload_content"] is False, (
        "the SDK deserializes unless this is False, and a deserialized response "
        "re-serialized is not the verbatim body AC-5.1 requires"
    )
    assert captured["_request_timeout"] > 0
    assert response.released, "the pooled connection was not returned"


def test_the_connection_is_released_even_when_reading_the_body_fails(
    client_config: Config,
) -> None:
    class ExplodingResponse:
        """A body that fails partway through, as a reset connection does."""

        def __init__(self) -> None:
            self.released = False

        @property
        def data(self) -> bytes:
            raise OSError("connection reset while reading")

        def release_conn(self) -> None:
            self.released = True

    response = ExplodingResponse()

    def invoke(request: object, **kwargs: Any) -> ExplodingResponse:
        return response

    with _client(client_config) as client, pytest.raises(OSError, match="connection reset"):
        client._fetch(INSTITUTIONS_GET, invoke, object())

    assert response.released, "a read failure leaked a pooled connection"


def test_a_non_bytes_body_is_refused_rather_than_archived(client_config: Config) -> None:
    """A decoded body means something upstream deserialized it.

    Archiving a `str` would put a re-encoding in the archive and call it
    verbatim, so this is loud rather than best-effort.
    """

    def invoke(request: object, **kwargs: Any) -> FakeHttpResponse:
        return FakeHttpResponse('{"total": 1}')

    with (
        _client(client_config) as client,
        pytest.raises(ConnectorError, match="expected undecoded"),
    ):
        client._fetch(INSTITUTIONS_GET, invoke, object())


def test_institutions_get_builds_a_request_the_sdk_accepts(client_config: Config) -> None:
    """The public surface, not just `_fetch`.

    The SDK validates its own request models at construction, so this is what
    catches a parameter renamed or dropped upstream -- a failure that a test
    calling `_fetch` with a bare `object()` would sail straight past.
    """
    response = FakeHttpResponse(b'{"institutions": [], "total": 0}')
    seen: dict[str, Any] = {}

    def invoke(request: Any, **kwargs: Any) -> FakeHttpResponse:
        seen["request"] = request
        return response

    with _client(client_config) as client:
        client._api.institutions_get = invoke
        fetched = client.institutions_get(count=7, offset=3, country_codes=["US", "CA"])

    request = seen["request"]
    assert request.count == 7
    assert request.offset == 3
    assert [str(code.value) for code in request.country_codes] == ["US", "CA"]
    assert fetched.request_context == "count=7 offset=3 country_codes=US,CA"


def test_the_request_context_records_what_was_asked_not_what_it_was_asked_with(
    client_config: Config,
) -> None:
    """`store/raw.py` extends the credential rule to `request_context`.

    Asserted against the real client, because the value under test is the one
    the real client builds -- a stub asserting its own fabricated string would
    stay green through exactly the change that matters, which is Chunk 03 adding
    the link-token and exchange calls where a token could reach this field.
    """
    response = FakeHttpResponse(b"{}")

    def invoke(request: Any, **kwargs: Any) -> FakeHttpResponse:
        return response

    with _client(client_config) as client:
        client._api.institutions_get = invoke
        fetched = client.institutions_get(count=1, offset=0, country_codes=["US"])

    context = fetched.request_context
    assert context is not None
    assert "test-secret" not in context, "the secret reached the archive's request context"
    assert client_config.plaid_client_id is not None
    assert client_config.plaid_client_id not in context
    # Positive control: it records the parameters, so its silence about the
    # credentials is a fact about the field rather than about it being empty.
    assert context == "count=1 offset=0 country_codes=US"


def _api_exception(status: int, reason: str, body: object) -> plaid.ApiException:
    exc = plaid.ApiException(status=status, reason=reason)
    exc.body = body
    return exc


# The body shape below was taken from a live sandbox call made with deliberately
# invalid credentials -- a probe that needs no valid ones -- rather than from the
# aggregator's documentation.
REAL_ERROR_BODY = json.dumps(
    {
        "display_message": None,
        "documentation_url": "https://plaid.com/docs/?ref=error#invalid-request-errors",
        "error_code": "INVALID_FIELD",
        "error_message": "client_id must be a properly formatted, non-empty string",
        "error_type": "INVALID_REQUEST",
        "request_id": "476e317baa236b1",
        "suggested_action": None,
    }
)


def test_a_rejected_call_names_the_cause_not_just_the_status(client_config: Config) -> None:
    """`400: Bad Request` is what every rejection looks like from the status alone.

    Wrong credentials, a malformed field and an unsupported country are the same
    HTTP status; only the body separates them. An operator who cannot tell a
    rotated secret from a bug in this code pays for the wrong guess with a
    credential rotation that was never the problem.
    """

    def invoke(request: object, **kwargs: Any) -> FakeHttpResponse:
        raise _api_exception(400, "Bad Request", REAL_ERROR_BODY)

    with _client(client_config) as client, pytest.raises(ConnectorError) as caught:
        client._fetch(INSTITUTIONS_GET, invoke, object())

    message = str(caught.value)
    assert "INVALID_FIELD" in message
    assert "client_id must be a properly formatted" in message
    assert "476e317baa236b1" in message, (
        "the request id is what makes a failure traceable in the aggregator's dashboard"
    )
    assert "Bad Request" not in message, "the reason phrase should not crowd out the real cause"


def test_a_failure_whose_body_makes_no_sense_is_still_reported() -> None:
    """A description that cannot be built must not replace one bad message with a crash."""
    for body in (None, b"\x00 not json", "not json either", json.dumps(["a", "list"])):
        described = _describe_failure(INSTITUTIONS_GET, _api_exception(500, "Server Error", body))
        assert "/institutions/get" in described
        assert "500" in described


def test_a_body_that_arrives_as_bytes_is_still_described() -> None:
    """The SDK decodes before re-raising, but only on the path that reaches it."""
    described = _describe_failure(
        INSTITUTIONS_GET, _api_exception(400, "Bad Request", REAL_ERROR_BODY.encode())
    )
    assert "INVALID_FIELD" in described


def test_an_unreachable_aggregator_is_a_sentence_not_a_traceback(
    client_config: Config,
) -> None:
    """The SDK wraps only SSL errors, so this one is ours to map.

    Verified by probing an unreachable host: a refused connection escapes
    `plaid-python` as `urllib3.MaxRetryError`. Left unmapped, a machine that is
    merely offline reports a traceback from a library the operator never chose --
    and, worse, says nothing to distinguish "the network is down" from "your
    secret is wrong", which is the difference between waiting and rotating a
    credential that was fine.
    """

    def invoke(request: object, **kwargs: Any) -> FakeHttpResponse:
        raise urllib3.exceptions.MaxRetryError(
            pool=urllib3.HTTPConnectionPool("127.0.0.1", 9), url="/institutions/get"
        )

    with _client(client_config) as client, pytest.raises(ConnectorError) as caught:
        client._fetch(INSTITUTIONS_GET, invoke, object())

    message = str(caught.value)
    assert "could not reach the aggregator" in message
    assert "sandbox" in message, "the message should name which environment was unreachable"


def test_the_endpoint_is_the_aggregators_own_path() -> None:
    """The archive key, and later the `DERIVERS` key. Changing it orphans an archive."""
    assert INSTITUTIONS_GET.path == "/institutions/get"
    assert str(INSTITUTIONS_GET) == "/institutions/get"
    assert Endpoint("/institutions/get") == INSTITUTIONS_GET


def test_the_client_talks_to_the_environment_it_was_configured_for(
    client_config: Config,
) -> None:
    with _client(client_config) as client:
        assert client.environment == "sandbox"
