"""Live calls to the aggregator's sandbox. Deselected by default.

    uv run pytest -m sandbox

A suite that needs network and credentials on every run stops being run, so the
offline suite is the one that has to stay honest and these are opt-in. What they
buy is the thing no fixture can: confirmation that the shape the fixtures were
recorded from is still the shape the aggregator sends.

**Recording a fixture.** With credentials in place:

    BANKMACHINE_RECORD_FIXTURES=1 uv run pytest -m sandbox

writes the response bodies under `tests/connector/fixtures/`. Without it, the
same tests *compare* the live response against what was recorded -- which is the
point of keeping both: the fixture and the live call are two independent
descriptions of the aggregator's shape, and only their disagreement can tell you
the aggregator changed.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import plaid
import pytest
from plaid.api import plaid_api
from plaid.model.country_code import CountryCode
from plaid.model.institutions_get_request import InstitutionsGetRequest

from bankmachine.config import Config, load_config
from bankmachine.connector import (
    INSTITUTIONS_GET,
    AggregatorNotConfiguredError,
    AggregatorRequestError,
    ConnectorError,
)
from bankmachine.connector.plaid.client import PlaidClient
from bankmachine.connector.plaid.errors import RetryPolicy
from bankmachine.secrets import AggregatorCredentialMissingError, get_plaid_secret

pytestmark = pytest.mark.sandbox

FIXTURES = Path(__file__).parent / "fixtures"
RECORDING = os.environ.get("BANKMACHINE_RECORD_FIXTURES") == "1"


@pytest.fixture
def sandbox_client() -> Any:
    """A client against the real sandbox, from the operator's own configuration.

    Skips rather than fails when credentials are absent: a missing sandbox
    credential means this suite was selected on a machine that cannot run it,
    which is a different thing from the aggregator being wrong.
    """
    config: Config = load_config()
    if config.environment != "sandbox":
        config = replace(config, environment="sandbox")
    if not config.plaid_client_id:
        pytest.skip("no aggregator client id configured; set BANKMACHINE_PLAID_CLIENT_ID")
    try:
        secret = get_plaid_secret(config)
    except AggregatorCredentialMissingError:
        pytest.skip("no aggregator secret in the keychain; run `bankmachine connector set-secret`")
    with PlaidClient(config, secret) as client:
        yield client


def _fixture_path(name: str) -> Path:
    return FIXTURES / f"{name}.json"


def _record_or_compare(name: str, body: bytes) -> dict[str, Any]:
    """Write the fixture, or check the live shape still matches it."""
    payload: dict[str, Any] = json.loads(body)
    path = _fixture_path(name)
    if RECORDING:
        FIXTURES.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return payload
    if not path.exists():
        pytest.skip(f"no recorded fixture at {path}; run with BANKMACHINE_RECORD_FIXTURES=1")
    recorded: dict[str, Any] = json.loads(path.read_bytes())
    # Keys, not values: the sandbox's institution list changes as the aggregator
    # adds test institutions, and a test that pinned values would fail for a
    # reason that says nothing about this product.
    assert set(payload) == set(recorded), (
        f"the aggregator's {name} response no longer has the keys the fixtures were "
        f"recorded from; the derivers written against them may be reading fields that "
        f"are gone"
    )
    return payload


def test_institutions_get_answers_and_its_shape_is_unchanged(sandbox_client: Any) -> None:
    fetched = sandbox_client.institutions_get(count=1, offset=0, country_codes=["US"])

    assert fetched.endpoint == INSTITUTIONS_GET
    assert isinstance(fetched.body, bytes)
    assert fetched.body, "the aggregator answered with an empty body"

    payload = _record_or_compare("institutions_get", fetched.body)

    assert "institutions" in payload
    assert "request_id" in payload, (
        "the aggregator's request id is what makes a failure traceable in its dashboard"
    )


def test_the_body_is_undecoded_bytes_from_a_real_response(sandbox_client: Any) -> None:
    """The verbatim guarantee, against the live path rather than a fake.

    The offline suite proves `_fetch` passes the bytes through; this proves the
    SDK actually hands them over undecoded when talking to a real server, which
    is the half a fake cannot establish.
    """
    fetched = sandbox_client.institutions_get(count=1, offset=0, country_codes=["US"])

    assert isinstance(fetched.body, bytes)
    assert json.loads(fetched.body), "the archived bytes are not the JSON that was sent"
    assert fetched.body.strip().startswith(b"{")


# --------------------------------------------------------------------------
# The error taxonomy, against the real server (FR-4)
# --------------------------------------------------------------------------
#
# These need no valid credentials beyond the client id, because *rejections* are
# the half of an external system that invalid inputs reach: the request goes to
# the same host and comes back with the real error shape. What genuinely needs
# an enrolled connection is the item-level half -- `ITEM_LOGIN_REQUIRED` via
# `/sandbox/item/reset_login` -- which cannot be enrolled until the exchange
# call exists, so it lands with Chunk 03 rather than here.


def _client_for(config: Config, secret: str) -> PlaidClient:
    return PlaidClient(config, secret, retry_policy=RetryPolicy(attempts=1))


@pytest.fixture
def sandbox_config() -> Config:
    config: Config = load_config()
    if config.environment != "sandbox":
        config = replace(config, environment="sandbox")
    if not config.plaid_client_id:
        pytest.skip("no aggregator client id configured; set BANKMACHINE_PLAID_CLIENT_ID")
    return config


def _record_or_compare_error(name: str, body: str) -> None:
    """Keep the recorded error shape honest against the live one.

    The body is recorded **verbatim**, for the same reason the archive holds
    responses verbatim: a body rebuilt from the fields this product happened to
    read back can only ever contain the fields this product happened to read.
    The first version of this helper did exactly that and silently dropped
    `error_type` -- the field the taxonomy's second layer classifies on.

    Compared by keys and by `error_code`, never by `request_id`, which changes
    every call and would fail for a reason that says nothing about this product.
    """
    path = _fixture_path(name)
    payload: dict[str, Any] = json.loads(body)
    if RECORDING:
        FIXTURES.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return
    if not path.exists():
        pytest.skip(f"no recorded fixture at {path}; run with BANKMACHINE_RECORD_FIXTURES=1")
    recorded = json.loads(path.read_text())
    assert set(recorded) == set(payload), (
        f"the aggregator's error body no longer has the keys {name} was recorded from; "
        f"the taxonomy reads error_code, error_type, error_message and request_id out of it"
    )
    assert recorded["error_code"] == payload["error_code"], (
        f"the aggregator now answers {name} with {payload['error_code']!r}; the taxonomy maps "
        f"{recorded['error_code']!r}, so a remedy is being chosen from a code that is gone"
    )


def _provoke(config: Config, secret: str) -> str:
    """One deliberately-rejected call, returning the aggregator's verbatim error body.

    Goes through the SDK rather than through `PlaidClient`, because the client's
    whole job is to turn this body into a local type -- and what needs recording
    is the body, before that happens.
    """
    api_client = plaid.ApiClient(
        plaid.Configuration(
            host=plaid.Environment.Sandbox,
            api_key={"clientId": config.plaid_client_id, "secret": secret},
        )
    )
    try:
        with pytest.raises(plaid.ApiException) as caught:
            plaid_api.PlaidApi(api_client).institutions_get(
                InstitutionsGetRequest(count=1, offset=0, country_codes=[CountryCode("US")]),
                _preload_content=False,
                _request_timeout=30.0,
            )
    finally:
        api_client.close()
    body = caught.value.body
    assert isinstance(body, str), "the SDK is documented to decode the error body before re-raising"
    return body


def test_a_wrong_secret_and_a_malformed_call_are_told_apart_by_the_real_server(
    sandbox_config: Config,
) -> None:
    """🔴 The failure VRF-002 item 5 was written to catch, now held by a test.

    An operator who cannot tell "your secret is wrong" from "this code is wrong"
    pays for the guess with a credential rotation that was never the problem.
    The aggregator already separates the two; this asserts that the taxonomy
    preserves the distinction rather than flattening it back out -- against the
    live server, because the whole question is what the live server actually
    sends, and a fake would answer with whatever was assumed while writing it.
    """
    with (
        _client_for(sandbox_config, "a" * 30) as client,
        pytest.raises(AggregatorNotConfiguredError) as rejected_credential,
    ):
        client.institutions_get(count=1, offset=0, country_codes=["US"])

    with (
        _client_for(replace(sandbox_config, plaid_client_id="not a client id"), "a" * 30) as client,
        pytest.raises(AggregatorRequestError) as rejected_request,
    ):
        client.institutions_get(count=1, offset=0, country_codes=["US"])

    assert rejected_credential.value.error_code == "INVALID_API_KEYS"
    assert rejected_request.value.error_code == "INVALID_FIELD"
    # Recorded so the offline suite tests the shape the aggregator actually
    # sends rather than one written from memory beside it. Two descriptions,
    # compared: only their disagreement can tell you the error shape changed.
    _record_or_compare_error("error_invalid_api_keys", _provoke(sandbox_config, "a" * 30))
    _record_or_compare_error(
        "error_invalid_field", _provoke(replace(sandbox_config, plaid_client_id="nope"), "a" * 30)
    )
    # The two `pytest.raises` above are what assert the distinction: each names a
    # different type, and neither is a subclass of the other, so a taxonomy that
    # flattened them back together would fail one of the two rather than reach here.
    # Neither is retried: hammering the aggregator with credentials it has just
    # rejected is how an account gets rate-limited on top of being misconfigured.
    assert type(rejected_credential.value).retryable is False
    assert type(rejected_request.value).retryable is False


def test_a_live_refusal_carries_what_a_degraded_record_needs(sandbox_config: Config) -> None:
    """AC-4.2 and AC-4.5, from a real response rather than a hand-written body."""
    with (
        _client_for(sandbox_config, "a" * 30) as client,
        pytest.raises(ConnectorError) as caught,
    ):
        client._fetch(
            INSTITUTIONS_GET,
            client._api.institutions_get,
            InstitutionsGetRequest(count=1, offset=0, country_codes=[CountryCode("US")]),
            connection_id=11,
        )

    failure = caught.value
    assert failure.error_code, "no code to write to connections.last_error_code"
    assert failure.request_id, "no request id, so the aggregator's dashboard cannot be searched"
    assert failure.failed_at is not None, "no far end for AC-4.5's subtraction"
    assert failure.connection_id == 11
    assert "a" * 30 not in str(failure), "the secret reached the failure message"
