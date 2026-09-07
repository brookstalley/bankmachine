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
from collections.abc import Callable
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
    AggregatorRequestError,
    ConnectorError,
    Endpoint,
    MalformedResponseError,
    RateLimitedError,
    ReauthRequiredError,
    TransportError,
)
from bankmachine.connector.plaid.client import PlaidClient
from bankmachine.connector.plaid.errors import RetryPolicy


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


def _client(config: Config, **kwargs: Any) -> PlaidClient:
    """A client whose retry channel records instead of waiting.

    Every test here is about one attempt, so the default policy is a single try:
    a test that accidentally exercised the backoff would spend the schedule's
    wall time proving something `test_errors.py` already proves properly.
    """
    kwargs.setdefault("retry_policy", RetryPolicy(attempts=1))
    kwargs.setdefault("sleep", lambda seconds: None)
    return PlaidClient(config, "test-secret", **kwargs)


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

    with _client(client_config) as client, pytest.raises(TransportError) as caught:
        client._fetch(INSTITUTIONS_GET, invoke, object())

    assert response.released, "a read failure leaked a pooled connection"
    # urllib3 streams the body, so a reset lands at the read rather than at the
    # call. Read outside the mapping, it escaped as a bare `OSError` from a
    # connector whose contract is that its failures are local types -- and the
    # sync loop would have had no type to catch it by.
    assert "while the response was being read" in str(caught.value)
    assert isinstance(caught.value.__cause__, OSError)
    assert type(caught.value).retryable is True, "a reset connection is the transient case"


def test_a_non_bytes_body_is_refused_rather_than_archived(client_config: Config) -> None:
    """A decoded body means something upstream deserialized it.

    Archiving a `str` would put a re-encoding in the archive and call it
    verbatim, so this is loud rather than best-effort.
    """

    def invoke(request: object, **kwargs: Any) -> FakeHttpResponse:
        return FakeHttpResponse('{"total": 1}')

    with (
        _client(client_config) as client,
        pytest.raises(MalformedResponseError, match="expected undecoded"),
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


def test_a_rejected_call_names_the_cause_not_just_the_status(
    client_config: Config, error_body: Callable[..., str]
) -> None:
    """`400: Bad Request` is what every rejection looks like from the status alone.

    Wrong credentials, a malformed field and an unsupported country are the same
    HTTP status; only the body separates them. An operator who cannot tell a
    rotated secret from a bug in this code pays for the wrong guess with a
    credential rotation that was never the problem.
    """

    body = error_body(error_code="INVALID_FIELD")

    def invoke(request: object, **kwargs: Any) -> FakeHttpResponse:
        raise _api_exception(400, "Bad Request", body)

    with _client(client_config) as client, pytest.raises(AggregatorRequestError) as caught:
        client._fetch(INSTITUTIONS_GET, invoke, object())

    message = str(caught.value)
    assert "INVALID_FIELD" in message
    assert caught.value.error_code == "INVALID_FIELD"
    # Read out of the same body rather than copied from it. A literal here is a
    # recorded value living in a second place, and it goes stale the next time
    # the fixture is re-recorded -- failing for a reason that says nothing about
    # the code under test.
    assert caught.value.request_id == json.loads(body)["request_id"]
    assert "client_id must be a properly formatted" in message
    assert json.loads(body)["request_id"] in message, (
        "the request id is what makes a failure traceable in the aggregator's dashboard"
    )
    assert "Bad Request" not in message, "the reason phrase should not crowd out the real cause"


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

    with _client(client_config) as client, pytest.raises(TransportError) as caught:
        client._fetch(INSTITUTIONS_GET, invoke, object())

    message = str(caught.value)
    assert "could not reach the aggregator" in message
    assert "sandbox" in message, "the message should name which environment was unreachable"


def test_the_endpoint_is_the_aggregators_own_path() -> None:
    """The archive key, and the derivation-registry key. Changing it orphans an archive."""
    assert INSTITUTIONS_GET.path == "/institutions/get"
    assert str(INSTITUTIONS_GET) == "/institutions/get"
    assert Endpoint("/institutions/get") == INSTITUTIONS_GET


def test_the_client_talks_to_the_environment_it_was_configured_for(
    client_config: Config,
) -> None:
    with _client(client_config) as client:
        assert client.environment == "sandbox"


def test_a_failure_inside_the_sdks_own_error_reporting_is_still_a_sentence(
    client_config: Config,
) -> None:
    """🔴 A bug in `plaid-python` 44.0.0, contained rather than left to surface.

    `rest.py` raises `ApiException(status=0, reason=...)` with no body when a
    bare `urllib3.exceptions.SSLError` escapes retry wrapping, and
    `api_client.py` then runs `e.body.decode('utf-8')` on that `None`. Verified
    by probe: the `AttributeError` is raised at `api_client.py:204` with the
    `ApiException` as its `__context__`.

    Left unmapped, a certificate problem reports `'NoneType' object has no
    attribute 'decode'` from a library the operator never chose -- which says
    nothing about certificates and sends them reading this product's source.
    """

    def invoke(request: object, **kwargs: Any) -> FakeHttpResponse:
        try:
            raise plaid.ApiException(status=0, reason="SSLError\ncertificate verify failed")
        except plaid.ApiException:
            # The shape the SDK produces: the AttributeError is raised while the
            # ApiException is being handled, so it stands as `__context__`.
            raise AttributeError("'NoneType' object has no attribute 'decode'") from None

    with _client(client_config) as client, pytest.raises(TransportError) as caught:
        client._fetch(INSTITUTIONS_GET, invoke, object())

    assert "certificate verify failed" in str(caught.value)


def test_an_ordinary_bug_in_this_module_is_not_disguised_as_a_transport_failure(
    client_config: Config,
) -> None:
    """The negative control for the containment above.

    Catching `AttributeError` around a call would swallow every genuine typo in
    this module and report it as a network problem -- turning a five-minute fix
    into a hunt for a firewall. The narrowing is what makes the catch legitimate,
    so it needs a test that fails if the narrowing is dropped.
    """

    def invoke(request: object, **kwargs: Any) -> FakeHttpResponse:
        raise AttributeError("'PlaidClient' object has no attribute 'instituions_get'")

    with _client(client_config) as client, pytest.raises(AttributeError) as caught:
        client._fetch(INSTITUTIONS_GET, invoke, object())

    assert "instituions_get" in str(caught.value)
    assert not isinstance(caught.value, ConnectorError)


def test_every_failure_names_the_connection_it_belongs_to(
    client_config: Config, error_body: Callable[..., str]
) -> None:
    """AC-4.1 is a property of the caller's loop, and the loop needs this to honour it.

    Asserted across the three ways a call can fail rather than the one that was
    easiest to write: a refusal, an unreachable host, and the SDK's own bug all
    reach different `raise` sites, and a connection id threaded through only the
    first of them would look correct until the night an institution went down.
    """

    def refused(request: object, **kwargs: Any) -> FakeHttpResponse:
        raise _api_exception(400, "Bad Request", error_body(error_code="ITEM_LOGIN_REQUIRED"))

    def unreachable(request: object, **kwargs: Any) -> FakeHttpResponse:
        raise urllib3.exceptions.MaxRetryError(
            pool=urllib3.HTTPConnectionPool("127.0.0.1", 9), url="/institutions/get"
        )

    def sdk_bug(request: object, **kwargs: Any) -> FakeHttpResponse:
        try:
            raise plaid.ApiException(status=0, reason="SSLError")
        except plaid.ApiException:
            raise AttributeError("'NoneType' object has no attribute 'decode'") from None

    with _client(client_config) as client:
        for invoke, expected in (
            (refused, ReauthRequiredError),
            (unreachable, TransportError),
            (sdk_bug, TransportError),
        ):
            with pytest.raises(expected) as caught:
                client._fetch(INSTITUTIONS_GET, invoke, object(), connection_id=42)
            assert caught.value.connection_id == 42, f"{expected.__name__} lost the connection"
            assert caught.value.endpoint is INSTITUTIONS_GET
            assert caught.value.failed_at is not None, "AC-4.5 needs the far end of the hole"


def test_the_client_actually_retries_a_transient_refusal(
    client_config: Config, error_body: Callable[..., str]
) -> None:
    """The retry channel wired into the real client, not just the helper.

    `test_errors.py` proves `call_with_retry` retries what it should; this proves
    `_fetch` is routed through it. Both are needed -- a correct helper nothing
    calls is the failure that looks most like success.
    """
    waits: list[float] = []
    attempts = 0

    def invoke(request: object, **kwargs: Any) -> FakeHttpResponse:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise _api_exception(
                429, "Too Many Requests", error_body(error_code="RATE_LIMIT_EXCEEDED")
            )
        return FakeHttpResponse(b'{"institutions": []}')

    with _client(client_config, retry_policy=RetryPolicy(attempts=4), sleep=waits.append) as client:
        fetched = client._fetch(INSTITUTIONS_GET, invoke, object())

    assert fetched.body == b'{"institutions": []}'
    assert attempts == 3
    assert waits == [1.0, 2.0]


def test_the_client_gives_up_loudly_rather_than_retrying_forever(
    client_config: Config, error_body: Callable[..., str]
) -> None:
    def invoke(request: object, **kwargs: Any) -> FakeHttpResponse:
        raise _api_exception(429, "Too Many Requests", error_body(error_code="RATE_LIMIT_EXCEEDED"))

    with (
        _client(
            client_config, retry_policy=RetryPolicy(attempts=2), sleep=lambda s: None
        ) as client,
        pytest.raises(RateLimitedError) as caught,
    ):
        client._fetch(INSTITUTIONS_GET, invoke, object())

    assert "gave up after 2 attempts" in str(caught.value)


def test_a_terminal_refusal_is_not_retried_by_the_client(
    client_config: Config, error_body: Callable[..., str]
) -> None:
    """A connection needing a human is reported now, not after four rounds of hope."""
    attempts = 0

    def invoke(request: object, **kwargs: Any) -> FakeHttpResponse:
        nonlocal attempts
        attempts += 1
        raise _api_exception(400, "Bad Request", error_body(error_code="ITEM_LOGIN_REQUIRED"))

    with (
        _client(client_config, retry_policy=RetryPolicy(attempts=4)) as client,
        pytest.raises(ReauthRequiredError),
    ):
        client._fetch(INSTITUTIONS_GET, invoke, object())

    assert attempts == 1


def test_no_credential_reaches_a_failure_message(
    client_config: Config, error_body: Callable[..., str]
) -> None:
    """The security-model norm: no secret in a log line, an exception, or a repr.

    An error message is the likeliest place for one to escape, because it is
    built from whatever was to hand at the worst moment.
    """

    def invoke(request: object, **kwargs: Any) -> FakeHttpResponse:
        raise _api_exception(
            400,
            "Bad Request",
            error_body(
                error_code="INVALID_API_KEYS",
                error_message="invalid client_id or secret",
            ),
        )

    with _client(client_config) as client, pytest.raises(ConnectorError) as caught:
        client._fetch(INSTITUTIONS_GET, invoke, object())

    rendered = f"{caught.value}{caught.value!r}"
    assert "test-secret" not in rendered
    assert "test-client-id" not in rendered
    # Positive control: the message is not empty, so its silence about the
    # credentials is a property of the message rather than of there being none.
    assert "INVALID_API_KEYS" in rendered
