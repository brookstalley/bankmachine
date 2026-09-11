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
from plaid.model.item_public_token_exchange_request import (  # noqa: F401
    ItemPublicTokenExchangeRequest,
)
from plaid.model.products import Products
from plaid.model.sandbox_item_reset_login_request import SandboxItemResetLoginRequest
from plaid.model.sandbox_public_token_create_request import SandboxPublicTokenCreateRequest

from bankmachine.config import MAX_HISTORY_DAYS, Config, load_config
from bankmachine.connector import (
    INSTITUTIONS_GET,
    ITEM_PUBLIC_TOKEN_EXCHANGE,
    AccessGrant,
    AggregatorNotConfiguredError,
    AggregatorRequestError,
    ConnectorError,
    CredentialBearingResponseError,
    Endpoint,
    FetchedResponse,
    ReauthRequiredError,
)
from bankmachine.connector.plaid.client import PlaidClient, capabilities_of
from bankmachine.connector.plaid.errors import RetryPolicy
from bankmachine.secrets import AggregatorCredentialMissingError, get_plaid_secret
from bankmachine.store.types import now_utc

pytestmark = pytest.mark.sandbox

FIXTURES = Path(__file__).parent / "fixtures"
RECORDING = os.environ.get("BANKMACHINE_RECORD_FIXTURES") == "1"

#: The aggregator's own fictional test bank. Belongs to the aggregator, not to
#: any institution roster, which is what makes it safe to name in a tracked file.
SANDBOX_INSTITUTION = "ins_109508"


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
# `/sandbox/item/reset_login` -- which needs the exchange call to mint an Item
# first.


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


# --------------------------------------------------------------------------
# Enrollment against the real sandbox (FR-1), and the item-level error half
# --------------------------------------------------------------------------


@pytest.fixture
def enrolled_item(sandbox_client: Any) -> str:
    """One disposable sandbox connection, and the access token that reads it.

    `/sandbox/public_token/create` mints an Item without a browser, which is the
    only way to reach the item-level half of the error taxonomy: a credential
    rejection is all that can be provoked without one, and those never touch the
    states FR-4 is actually written about.
    """
    public = sandbox_client._fetch_bytes(
        Endpoint("/sandbox/public_token/create"),
        sandbox_client._api.sandbox_public_token_create,
        SandboxPublicTokenCreateRequest(
            institution_id=SANDBOX_INSTITUTION, initial_products=[Products("transactions")]
        ),
    )
    public_token = json.loads(public)["public_token"]
    assert isinstance(public_token, str)
    grant: AccessGrant = sandbox_client.exchange_public_token(public_token)
    return grant.access_token


def test_a_link_token_is_created_live_at_the_configured_maximum(sandbox_client: Any) -> None:
    """AC-1.2 against the real aggregator, as far as the aggregator can be asked.

    🔴 **The response does not report the window** *(verified: a hosted session
    replies with `expiration`, `hosted_link_url`, `link_token`, `request_id`, and
    none of those is the window)*, so what is checkable here is that 730 is
    accepted and that the value travels back to the caller. What the aggregator
    actually *grants* is not observable anywhere in the enrollment path -- see
    AC-1.3a -- so no test here should be read as having verified it.

    The hosted URL is asserted alongside because AC-1.1 has nothing to print
    without it, and its absence is how an account without Hosted Link enabled
    would announce itself.
    """
    issued = sandbox_client.link_token_create(
        history_days=MAX_HISTORY_DAYS,
        client_user_id="bankmachine-suite",
        country_codes=["US"],
        products=["transactions"],
    )

    assert issued.token.startswith("link-sandbox-")
    assert issued.requested_history_days == MAX_HISTORY_DAYS == 730
    assert issued.expires_at, "a session with no expiry is not a session"
    assert issued.hosted_link_url.startswith("https://"), (
        "AC-1.1 prints this URL to the operator; anything but https is not printable"
    )


def test_an_unfinished_session_reports_itself_by_omitting_link_sessions(
    sandbox_client: Any,
) -> None:
    """🔴 The live shape the poll loop is written against.

    A session nobody has opened carries no `link_sessions` key at all -- not an
    empty list, and no status field. This asserts it against the real aggregator
    rather than against the fixture that encodes the same belief, because the
    fixture was written from this observation and could only ever agree with it.
    """
    issued = sandbox_client.link_token_create(
        history_days=MAX_HISTORY_DAYS,
        client_user_id="bankmachine-suite",
        country_codes=["US"],
        products=["transactions"],
    )

    session = sandbox_client.link_token_get(issued.token)

    assert not session.finished
    assert session.public_token is None


def test_an_exchange_yields_a_token_that_no_archive_could_have_taken(
    sandbox_client: Any, enrolled_item: str
) -> None:
    """AC-10.1 against a real credential, not a constructed one.

    The exchange happened for real and the access token is in hand, and there is
    still no `FetchedResponse` anywhere that could have carried it -- because the
    endpoint is declared credential-issuing and the archive's own input type
    refuses to exist for it.
    """
    assert enrolled_item.startswith("access-sandbox-")
    with pytest.raises(CredentialBearingResponseError):
        FetchedResponse(
            endpoint=ITEM_PUBLIC_TOKEN_EXCHANGE,
            body=b'{"access_token": "whatever"}',
            received_at=now_utc(),
            request_context=None,
        )


def test_capabilities_come_back_from_a_real_connection(
    sandbox_client: Any, enrolled_item: str
) -> None:
    """AC-3.2 against a live item, where `products` and `available_products` differ.

    They differ *because* the item was enrolled with `transactions` alone, which
    is what makes this a real discrimination rather than two names for one list.
    A fake could be written either way and would agree with whichever was coded.
    """
    fetched = sandbox_client.item_get(enrolled_item, connection_id=1)
    capabilities = capabilities_of(fetched.body)

    _record_or_compare("item_get", fetched.body)
    assert "investments" in capabilities, (
        "the sandbox item reports investments among what it could do; if this is empty, "
        "capability discovery is reading `products` and would never discover anything"
    )
    # Both lists, whole. Asserted as containment rather than as inequality with
    # either one: an item with every product already initialized reports an EMPTY
    # `available_products`, and against that item a correct union equals
    # `products` exactly -- so `capabilities != products` would fail a connection
    # that had been read perfectly.
    item = json.loads(fetched.body)["item"]
    assert capabilities >= set(item["products"])
    assert capabilities >= set(item["available_products"])


def test_accounts_come_back_and_are_archivable(sandbox_client: Any, enrolled_item: str) -> None:
    """The shape the accounts deriver is written against, recorded from the live call."""
    fetched = sandbox_client.accounts_get(enrolled_item, connection_id=1)
    payload = _record_or_compare("accounts_get", fetched.body)

    assert payload["accounts"], "an enrolled item with no accounts is not a fixture"
    account = payload["accounts"][0]
    for field in ("account_id", "name", "type", "subtype", "balances"):
        assert field in account, f"the deriver reads {field} and the aggregator stopped sending it"


def test_a_reset_login_drives_a_real_item_login_required_through_the_taxonomy(
    sandbox_client: Any, enrolled_item: str
) -> None:
    """🔴 The item-level half of FR-4, which no credential rejection can reach.

    Needs an Item, so it needs the exchange call that mints one -- which is why
    it lives beside enrollment rather than beside the taxonomy it exercises.
    `/sandbox/item/reset_login` invalidates a connection's
    credentials exactly as an institution's password change does, so this is the
    real `ITEM_LOGIN_REQUIRED` -- the state that decides whether an operator is
    told to re-link, and the one whose remedy is in this product rather than at
    their bank.
    """
    sandbox_client._fetch_bytes(
        Endpoint("/sandbox/item/reset_login"),
        sandbox_client._api.sandbox_item_reset_login,
        SandboxItemResetLoginRequest(access_token=enrolled_item),
    )

    with pytest.raises(ReauthRequiredError) as caught:
        sandbox_client.accounts_get(enrolled_item, connection_id=7)

    failure = caught.value
    assert failure.error_code == "ITEM_LOGIN_REQUIRED"
    assert failure.connection_id == 7, "AC-4.1's isolation needs the connection named"
    assert failure.failed_at is not None, "AC-4.5 needs the far end of the data hole"
    assert type(failure).retryable is False, (
        "retrying a connection that needs a human delays the report that tells them"
    )


# --------------------------------------------------------------------------
# AC-4.3 -- update mode, live
# --------------------------------------------------------------------------


def test_an_update_mode_session_is_hosted_too(sandbox_client: Any, enrolled_item: str) -> None:
    """🔴 The assumption the whole repair command rests on, and the only place it is testable.

    `connections reauth` prints a hosted URL because AC-1.1 rules out a local web
    server -- for the repair exactly as for the enrollment. Nothing in a fixture
    can establish that Hosted Link is available in UPDATE mode on this account;
    the aggregator either returns a `hosted_link_url` for an update-mode token or
    it does not, and if it does not the command has nothing to print.
    """
    session = sandbox_client.link_token_create_update(
        access_token=enrolled_item,
        client_user_id="bankmachine-suite",
        country_codes=["US"],
    )

    assert session.hosted_link_url.startswith("https://"), (
        "the repair prints this URL to the operator; anything but https is not printable"
    )
    assert session.expires_at, "a session with no expiry is not a session"


def test_an_expired_login_is_reported_inside_the_item_body(
    sandbox_client: Any, enrolled_item: str
) -> None:
    """🔴 The live shape `connections reauth` polls against.

    The repair waits on `item.error.error_code` rather than on a public token,
    because update mode mints none. That decision is only sound if `/item/get`
    answers 200 with the complaint *in the body* -- if the call raised instead,
    the poll loop would never get a body to read. Asserted against the real
    aggregator rather than against the fixture that encodes the same belief.
    """
    sandbox_client._fetch_bytes(
        Endpoint("/sandbox/item/reset_login"),
        sandbox_client._api.sandbox_item_reset_login,
        SandboxItemResetLoginRequest(access_token=enrolled_item),
    )

    fetched = sandbox_client.item_get(enrolled_item, connection_id=1)
    item = json.loads(fetched.body)["item"]

    assert item["item_id"], "the repair compares this against the row before trusting anything"
    assert item["error"]["error_code"] == "ITEM_LOGIN_REQUIRED"
