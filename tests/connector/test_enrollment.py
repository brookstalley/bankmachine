"""The enrollment endpoints (FR-1), and the two rules they exist under.

Two things here are held by construction rather than by anyone remembering
them, and each gets a test that would fail if the construction were undone:

* **The history window cannot be omitted.** AC-1.2 makes it immutable after
  enrollment and the requirements call a vendor-default build a failed build, so
  forgetting it is a type error rather than a silent enrollment at someone
  else's default.
* **A credential-bearing response cannot be archived**, because the type the
  archive consumes refuses to exist for an endpoint that issues one.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import urllib3.exceptions

from bankmachine.config import MAX_HISTORY_DAYS, Config, ConfigError, load_config
from bankmachine.connector import (
    ACCOUNTS_GET,
    INSTITUTIONS_GET,
    ITEM_GET,
    ITEM_PUBLIC_TOKEN_EXCHANGE,
    LINK_TOKEN_CREATE,
    AccessGrant,
    AggregatorNotConfiguredError,
    CredentialBearingResponseError,
    Endpoint,
    FetchedResponse,
    LinkToken,
    MalformedResponseError,
    TransportError,
)
from bankmachine.connector.plaid.client import PlaidClient, capabilities_of
from bankmachine.connector.plaid.errors import RetryPolicy
from bankmachine.store.types import now_utc

REPO_ROOT = Path(__file__).parents[2]


@pytest.fixture
def client_config(config: Config) -> Config:
    return replace(config, plaid_client_id="test-client-id")


def _client(config: Config) -> PlaidClient:
    return PlaidClient(
        config, "test-secret", retry_policy=RetryPolicy(attempts=1), sleep=lambda seconds: None
    )


class FakeHttpResponse:
    """Stands in for the urllib3 response the SDK returns undecoded."""

    def __init__(self, data: object) -> None:
        self.data = data
        self.released = False

    def release_conn(self) -> None:
        self.released = True


def _answering(payload: dict[str, Any]) -> Any:
    captured: dict[str, Any] = {}

    def invoke(request: Any, **kwargs: Any) -> FakeHttpResponse:
        captured["request"] = request
        return FakeHttpResponse(json.dumps(payload).encode())

    invoke.captured = captured  # type: ignore[attr-defined]
    return invoke


# --------------------------------------------------------------------------
# AC-1.2 — the window is required, and it is the configured one
# --------------------------------------------------------------------------

_WINDOW_PREAMBLE = """
from bankmachine.connector.plaid.client import PlaidClient


def enroll(client: PlaidClient) -> None:
"""

WITHOUT_WINDOW = (
    _WINDOW_PREAMBLE
    + """    client.link_token_create(
        client_user_id="operator",
        country_codes=["US"],
        products=["transactions"],
    )
"""
)

WITH_WINDOW = (
    _WINDOW_PREAMBLE
    + """    client.link_token_create(
        history_days=730,
        client_user_id="operator",
        country_codes=["US"],
        products=["transactions"],
    )
"""
)


@pytest.fixture(scope="module")
def window_report(tmp_path_factory: pytest.TempPathFactory) -> str:
    """One mypy run over both snippets, because mypy is the slow part.

    AC-1.2's mechanism is the signature, so this test runs the type checker --
    asserting at runtime that a window was passed would test the call site in
    front of it rather than the property that no call site can omit it.
    """
    workspace = tmp_path_factory.mktemp("window")
    (workspace / "without_window.py").write_text(WITHOUT_WINDOW, encoding="utf-8")
    (workspace / "with_window.py").write_text(WITH_WINDOW, encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "--no-incremental",
            "--no-error-summary",
            "--cache-dir",
            str(workspace / ".mypy_cache"),
            str(workspace / "without_window.py"),
            str(workspace / "with_window.py"),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, (
        "mypy accepted a link-token call with no history window. AC-1.2's only enforcement "
        f"is this signature, so this is the requirement failing:\n{result.stdout}{result.stderr}"
    )
    return result.stdout


def test_a_link_token_cannot_be_requested_without_a_history_window(window_report: str) -> None:
    """🔴 The most expensive mistake in the system, made impossible to write.

    The window is immutable after enrollment: getting it wrong means re-linking
    every institution. A default parameter value is exactly how that happens, and
    it would not surface until someone asked for three-year-old transactions and
    found they had never been fetched.
    """
    errors = [line for line in window_report.splitlines() if "without_window.py" in line]
    assert any("history_days" in line for line in errors), (
        f"mypy rejected the call, but not for the missing window:\n{errors}"
    )


def test_mypy_accepts_the_call_that_names_its_window(window_report: str) -> None:
    """The positive control.

    Without it this test would pass just as happily if mypy had failed because
    the import was broken -- which is the one way a type-check assertion goes
    quietly green.
    """
    assert not [line for line in window_report.splitlines() if "with_window.py" in line], (
        f"mypy rejected the correct call:\n{window_report}"
    )


def test_the_configured_window_is_what_gets_sent_not_a_vendor_default(
    client_config: Config,
) -> None:
    """Asserted on the request the SDK builds, because that is what is sent."""
    invoke = _answering({"link_token": "link-sandbox-x", "expiration": "2026-09-08T00:00:00Z"})

    with _client(client_config) as client:
        client._api.link_token_create = invoke
        issued = client.link_token_create(
            history_days=365,
            client_user_id="operator",
            country_codes=["US"],
            products=["transactions"],
        )

    request = invoke.captured["request"]
    assert request.transactions.days_requested == 365
    # Carried on the way out because the response does not contain it *(verified
    # live: the reply is expiration, link_token, request_id)*. Without this the
    # caller would have no record of what was asked, and AC-11.8's shortfall --
    # requested minus granted -- would have no left-hand side.
    assert issued.requested_history_days == 365


def test_a_window_the_aggregator_would_reject_is_refused_here(client_config: Config) -> None:
    """Refused before the call, so the message names the real constraint.

    The aggregator's own rejection would arrive as `INVALID_FIELD` naming a field
    path, which tells an operator nothing about why 900 is not allowed.
    """
    with _client(client_config) as client:
        for window in (0, -1, MAX_HISTORY_DAYS + 1):
            with pytest.raises(AggregatorNotConfiguredError, match="history_days"):
                client.link_token_create(
                    history_days=window,
                    client_user_id="operator",
                    country_codes=["US"],
                    products=["transactions"],
                )


def test_the_configured_window_defaults_to_the_documented_maximum(tmp_path: Path) -> None:
    """The default errs in the direction that is reversible.

    Asking for more history than needed costs nothing and can be ignored; asking
    for less costs history that cannot be recovered at any price, because the
    window is immutable once the connection exists.
    """
    config = load_config(env={"HOME": str(tmp_path)}, config_path=tmp_path / "absent.toml")
    assert config.history_days == MAX_HISTORY_DAYS == 730


def test_a_window_outside_the_aggregators_range_is_refused_not_clamped(tmp_path: Path) -> None:
    """A clamp would enroll at a window the operator never chose and never told them."""
    for value in ("0", "731", "-5"):
        with pytest.raises(ConfigError, match="history_days"):
            load_config(
                env={"HOME": str(tmp_path), "BANKMACHINE_HISTORY_DAYS": value},
                config_path=tmp_path / "absent.toml",
            )


# --------------------------------------------------------------------------
# AC-10.1 — a credential-bearing response cannot become an archivable one
# --------------------------------------------------------------------------


def test_the_endpoints_that_issue_credentials_are_named_as_such() -> None:
    """The exemption is a property of the endpoint, not a list beside the archive.

    A list kept at the archive site is an enumeration standing in for a property:
    right on the day it was written, and silently wrong the first time someone
    adds an endpoint without opening that file.
    """
    assert LINK_TOKEN_CREATE.issues_credential
    assert ITEM_PUBLIC_TOKEN_EXCHANGE.issues_credential
    # The mirror half, which is what makes the flag a discrimination rather than
    # a decoration: the data endpoints must NOT carry it, or the archive would be
    # empty and every test above would still pass.
    for endpoint in (INSTITUTIONS_GET, ITEM_GET, ACCOUNTS_GET):
        assert not endpoint.issues_credential, f"{endpoint} would never be archived"


def test_a_credential_bearing_response_cannot_be_made_archivable() -> None:
    """🔴 `raw_responses` is append-only and a datastore backup travels.

    A token written there is written permanently, so this refuses at the type
    that feeds the archive rather than at the archive -- by which point there is
    nothing to undo.
    """
    for endpoint in (LINK_TOKEN_CREATE, ITEM_PUBLIC_TOKEN_EXCHANGE):
        with pytest.raises(CredentialBearingResponseError, match="never reach the archive"):
            FetchedResponse(
                endpoint=endpoint,
                body=b'{"access_token": "fake-access-token-for-tests"}',
                received_at=now_utc(),
                request_context=None,
            )

    # Positive control: the same construction succeeds for a data endpoint, so
    # the refusal is about credentials rather than about the type being broken.
    archivable = FetchedResponse(
        endpoint=ACCOUNTS_GET, body=b"{}", received_at=now_utc(), request_context=None
    )
    assert archivable.endpoint is ACCOUNTS_GET


def test_an_exchange_returns_no_archivable_response_at_all(client_config: Config) -> None:
    """There is no object for a caller to hand the archive, which is the point.

    A test asserting "the caller does not archive it" would be about the caller.
    This is about there being nothing to archive.
    """
    invoke = _answering({"access_token": "fake-access-token-for-tests", "item_id": "item-1"})

    with _client(client_config) as client:
        client._api.item_public_token_exchange = invoke
        grant = client.exchange_public_token("public-sandbox-token")

    assert grant.access_token == "fake-access-token-for-tests"
    assert grant.source_connection_id == "item-1"
    assert not isinstance(grant, FetchedResponse)
    assert not hasattr(grant, "body"), "the exchange body outlived the call that read it"


def test_an_accounts_call_is_archivable_because_it_carries_no_credential(
    client_config: Config,
) -> None:
    """The other half of the discrimination, and the reason it is not just a ban.

    If credential-bearing and data endpoints were treated alike, the archive
    would be empty and FR-5 would be unmet -- so the exemption has to let this
    through.
    """
    invoke = _answering({"accounts": [], "item": {}, "request_id": "r-1"})

    with _client(client_config) as client:
        client._api.accounts_get = invoke
        fetched = client.accounts_get("fake-access-token-for-tests", connection_id=4)

    assert isinstance(fetched, FetchedResponse)
    assert fetched.endpoint is ACCOUNTS_GET


def test_no_access_token_reaches_the_request_context(client_config: Config) -> None:
    """`store/raw.py` extends the credential rule to what gets recorded alongside.

    `request_context` records what was asked, never what it was asked with -- and
    these are the first two calls in the product whose *arguments* are secret.
    """
    token = "fake-access-token-for-tests"
    calls: list[tuple[str, Callable[[PlaidClient], FetchedResponse]]] = [
        ("item_get", lambda client: client.item_get(token, connection_id=4)),
        ("accounts_get", lambda client: client.accounts_get(token, connection_id=4)),
    ]
    for endpoint_attr, call in calls:
        with _client(client_config) as client:
            setattr(client._api, endpoint_attr, _answering({"item": {}, "accounts": []}))
            fetched = call(client)
        assert token not in (fetched.request_context or "")


# --------------------------------------------------------------------------
# AC-3.2 — capabilities come from the connection, never from the institution
# --------------------------------------------------------------------------

#: Shaped from a real `/item/get` reply for a sandbox item enrolled with
#: `transactions` alone: `products` lists what was asked for, and
#: `available_products` the fourteen things the connection could actually do.
ITEM_BODY = json.dumps(
    {
        "item": {
            "item_id": "item-1",
            "institution_id": "ins_109508",
            "institution_name": "First Platypus Bank",
            "products": ["transactions"],
            "available_products": ["investments", "liabilities", "auth", "balance"],
            "billed_products": ["transactions"],
            "consented_products": None,
            "error": None,
        },
        "request_id": "r-1",
        "status": None,
    }
).encode()


def test_capabilities_answer_what_the_connection_could_do_not_what_we_asked_for() -> None:
    """🔴 `available_products`, not `products`.

    AC-3.2 pulls investments for any connection whose capabilities include
    investments. A discovery reading `products` would report back exactly what
    this product already requested -- it would never discover anything, and every
    test asserting "capabilities were discovered" would still pass.
    """
    capabilities = capabilities_of(ITEM_BODY)
    assert "investments" in capabilities
    assert "liabilities" in capabilities
    # The tell that the wrong field was read: `transactions` is what was asked
    # for and is absent from `available_products` on this item.
    assert "transactions" not in capabilities, "capabilities were read from `products`"


def test_nothing_in_capability_discovery_reads_an_institution() -> None:
    """AC-3.2 forbids branching on a named institution, and this keeps the roster out.

    Asserted by changing the institution and nothing else: if any code path
    consulted it, two identical connections at different banks would report
    different capabilities.
    """
    other = json.loads(ITEM_BODY)
    other["item"]["institution_id"] = "ins_000000"
    other["item"]["institution_name"] = "Somewhere Else Entirely"
    assert capabilities_of(json.dumps(other).encode()) == capabilities_of(ITEM_BODY)


def test_an_item_without_a_capability_list_is_refused_rather_than_read_as_empty() -> None:
    """An empty capability set and an unreadable one are different facts.

    Read as empty, a connection that can do investments is silently never asked
    for them -- which is a data hole nothing reports, in a product whose named
    failure mode is exactly that.
    """
    for body in (b"{}", b'{"item": {}}', b'{"item": {"available_products": "not a list"}}'):
        with pytest.raises(MalformedResponseError):
            capabilities_of(body)


def test_the_endpoint_paths_are_the_aggregators_own() -> None:
    """They are archive keys and `DERIVERS` keys. Changing one orphans an archive."""
    assert LINK_TOKEN_CREATE.path == "/link/token/create"
    assert ITEM_PUBLIC_TOKEN_EXCHANGE.path == "/item/public_token/exchange"
    assert ITEM_GET.path == "/item/get"
    assert ACCOUNTS_GET.path == "/accounts/get"
    assert Endpoint("/accounts/get") == ACCOUNTS_GET


# --------------------------------------------------------------------------
# What the far end cannot absorb twice (review R-1)
# --------------------------------------------------------------------------


def test_an_exchange_is_never_retried(client_config: Config) -> None:
    """🔴 A single-use token, and a durable Item at the other end.

    The retry channel's safety argument is that this side persists nothing, so a
    second attempt has no partial write to interleave against. That is true and
    it is *local*: an exchange spends the public token and creates an Item at the
    aggregator, so a transport failure after the far end processed the request
    would, retried, either fail on a spent token or enroll twice -- and both look
    like a network blip from here.
    """
    attempts = 0

    def invoke(request: Any, **kwargs: Any) -> FakeHttpResponse:
        nonlocal attempts
        attempts += 1
        raise urllib3.exceptions.MaxRetryError(
            pool=urllib3.HTTPConnectionPool("127.0.0.1", 9), url="/item/public_token/exchange"
        )

    with PlaidClient(
        client_config, "test-secret", retry_policy=RetryPolicy(attempts=5), sleep=lambda s: None
    ) as client:
        client._api.item_public_token_exchange = invoke
        with pytest.raises(TransportError):
            client.exchange_public_token("public-sandbox-token")

    assert attempts == 1, "the exchange was retried; a spent token or a second Item"


def test_a_read_only_call_is_still_retried(client_config: Config) -> None:
    """The control that makes `retry_safe` a discrimination rather than an off switch.

    Without it, disabling retries everywhere would pass the test above and
    quietly undo AC-2.6 and the rate-limit channel.
    """
    attempts = 0

    def invoke(request: Any, **kwargs: Any) -> FakeHttpResponse:
        nonlocal attempts
        attempts += 1
        raise urllib3.exceptions.MaxRetryError(
            pool=urllib3.HTTPConnectionPool("127.0.0.1", 9), url="/accounts/get"
        )

    with PlaidClient(
        client_config, "test-secret", retry_policy=RetryPolicy(attempts=3), sleep=lambda s: None
    ) as client:
        client._api.accounts_get = invoke
        with pytest.raises(TransportError):
            client.accounts_get("fake-access-token-for-tests")

    assert attempts == 3


def test_the_endpoints_that_cannot_be_retried_are_named_as_such() -> None:
    assert not ITEM_PUBLIC_TOKEN_EXCHANGE.retry_safe
    # The mirror half: every read is retryable, or the backoff channel guards
    # nothing and the assertion above would pass with retries off everywhere.
    for endpoint in (INSTITUTIONS_GET, ITEM_GET, ACCOUNTS_GET, LINK_TOKEN_CREATE):
        assert endpoint.retry_safe, f"{endpoint} would never retry a rate limit"


# --------------------------------------------------------------------------
# No credential in a repr (security-model Direction; review R-13)
# --------------------------------------------------------------------------


def test_no_enrollment_credential_reaches_a_repr() -> None:
    """🔴 A ratified norm: no secret in a log line, an exception message or a `repr`.

    A dataclass writes every field into its generated `repr`, and a `repr` is
    what reaches a traceback, a debugger, and any log line that interpolated the
    object rather than a field of it. That is the accident, and it is why this
    is asserted rather than left to whoever adds the next field.
    """
    grant = AccessGrant(access_token="fake-token-value-for-tests", source_connection_id="item-1")
    issued = LinkToken(
        token="fake-link-token-for-tests",
        expires_at="2026-09-08T00:00:00Z",
        requested_history_days=730,
    )

    for rendered in (repr(grant), str(grant), f"{grant}", repr(issued), str(issued)):
        assert "fake-token-value-for-tests" not in rendered
        assert "fake-link-token-for-tests" not in rendered
        assert "<redacted>" in rendered
    # Positive control: the non-secret half still shows, so the redaction is
    # about the credential rather than about the repr being empty.
    assert "item-1" in repr(grant)
    assert "730" in repr(issued)


def test_the_enrollment_types_are_nameable_without_the_aggregator_sdk() -> None:
    """Build step 3's CLI will name an `AccessGrant`; it should not load `plaid` to do it.

    They were first defined inside the vendor package, which would have pulled
    the SDK into the import graph of every module that mentions one.
    """
    import bankmachine.connector as boundary

    assert boundary.AccessGrant is AccessGrant
    assert boundary.LinkToken is LinkToken


def test_the_documented_maximum_is_the_one_the_sdk_enforces() -> None:
    """🔴 `MAX_HISTORY_DAYS` mirrors a number that lives in the dependency.

    `plaid-python` is not pinned to an exact version, so the aggregator's own
    maximum can move under this constant -- and the failure would be a link token
    rejected at enrollment, which is the least convenient moment this product
    has. Comparing them here turns that into a test failure on `uv sync`.
    """
    from plaid.model.link_token_transactions import LinkTokenTransactions

    declared = LinkTokenTransactions.validations[("days_requested",)]
    assert declared["inclusive_maximum"] == MAX_HISTORY_DAYS, (
        f"the aggregator now allows {declared['inclusive_maximum']} days of history and this "
        f"build still asks for {MAX_HISTORY_DAYS}"
    )
    assert declared["inclusive_minimum"] == 1
