"""The aggregator client: authenticated calls in, verbatim bytes out.

**What this module uses the SDK for, and what it deliberately does not.**
`plaid-python` is a generated client whose largest half is response models. This
module uses almost none of that half, and the reason is AC-5.1: every response
is archived *verbatim, before any normalization*, so what the archive needs is
the bytes off the wire. Handing those bytes to the SDK's deserializer and then
re-serializing them would archive a round-trip through the SDK's model layer --
which silently drops fields the generated models do not know about, and those
are exactly the fields a later `store rebuild` would need to reproduce rows the
aggregator has since started sending. So every call passes
`_preload_content=False`, which the SDK documents as returning the underlying
HTTP response undecoded *(verified against `api_client.py`, which returns before
its deserialize branch)*.

What the SDK is still worth having: host and auth wiring, the endpoint paths and
their request validation, its error classes, and connection pooling. Those are
the parts a hand-rolled client gets subtly wrong.

🔴 **The SDK ships no `py.typed`** *(verified: no such file in the installed
package)*, so under mypy strict everything it returns is `Any`. That is contained
here rather than relaxed away: this module converts to local types at its own
boundary, and `warn_return_any` is what makes the conversion mandatory rather
than optional. A caller outside this package never touches an `Any` that came
from the aggregator.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Final

import plaid
import urllib3.exceptions
from plaid.api import plaid_api
from plaid.model.country_code import CountryCode
from plaid.model.institutions_get_request import InstitutionsGetRequest

from bankmachine.config import Config
from bankmachine.connector import (
    INSTITUTIONS_GET,
    AggregatorNotConfiguredError,
    ConnectorError,
    Endpoint,
    FetchedResponse,
)
from bankmachine.store.types import now_utc

#: How long a single aggregator call may take before it is abandoned. The
#: aggregator is the one channel that can fail transiently, and a call with no
#: timeout does not fail transiently -- it hangs the nightly job until someone
#: notices data has stopped arriving, which is this product's named primary
#: failure mode wearing a different hat.
DEFAULT_REQUEST_TIMEOUT_SECONDS: Final = 30.0

_HOSTS: Final[dict[str, str]] = {
    "sandbox": plaid.Environment.Sandbox,
    "production": plaid.Environment.Production,
}


def _host_for(environment: str) -> str:
    """The API host for a configured environment.

    A mapping rather than a conditional so that adding an environment to
    `config.Environment` without deciding its host fails here, loudly, instead
    of defaulting to one of them.
    """
    try:
        return _HOSTS[environment]
    except KeyError:  # pragma: no cover -- unreachable while Environment is a Literal
        raise AggregatorNotConfiguredError(
            f"no aggregator host is defined for environment {environment!r}"
        ) from None


def _describe_failure(endpoint: Endpoint, exc: plaid.ApiException) -> str:
    """What actually went wrong, in one line.

    🔴 **`reason` is the HTTP reason phrase and says nothing.** Wrong credentials,
    a malformed field and an unsupported country all arrive as
    `400: Bad Request`; the cause lives in the response body, which the SDK
    attaches to the exception and decodes to `str` before re-raising *(both
    verified by probing the real sandbox host with deliberately invalid
    credentials: `error_type=INVALID_REQUEST`, `error_code=INVALID_FIELD`,
    `error_message='client_id must be a properly formatted, non-empty string'`)*.

    An operator reading `400: Bad Request` has no way to tell a rotated secret
    from a bug in this code, and the wrong guess costs them a credential
    rotation that was never the problem. The `request_id` is included because it
    is what makes a failure traceable in the aggregator's own dashboard.

    The body is parsed defensively: a failure that cannot be described must
    still be reported, so an unrecognized body degrades to the status line
    rather than replacing one bad message with an exception.
    """
    status = f"{endpoint} failed with status {exc.status}"
    body = exc.body
    if isinstance(body, bytes | bytearray):
        body = body.decode("utf-8", errors="replace")
    if not isinstance(body, str):
        return f"{status}: {exc.reason}"
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return f"{status}: {exc.reason}"
    if not isinstance(payload, dict):
        return f"{status}: {exc.reason}"

    code = payload.get("error_code") or payload.get("error_type")
    message = payload.get("error_message") or payload.get("display_message")
    request_id = payload.get("request_id")

    described = status
    if code:
        described += f" ({code})"
    described += f": {message or exc.reason}"
    if request_id:
        described += f" [request_id {request_id}]"
    return described


class PlaidClient:
    """An authenticated client for one environment.

    Holds no datastore handle and writes nothing. Callers archive what they get
    back; see `bankmachine.connector` for why the split is structural.
    """

    def __init__(
        self,
        config: Config,
        secret: str,
        *,
        timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        if not config.plaid_client_id:
            raise AggregatorNotConfiguredError(
                "no aggregator client id is configured. Set `plaid_client_id` in the config file "
                "or BANKMACHINE_PLAID_CLIENT_ID in the environment"
            )
        self._environment = config.environment
        self._timeout_seconds = timeout_seconds
        configuration = plaid.Configuration(
            host=_host_for(config.environment),
            api_key={"clientId": config.plaid_client_id, "secret": secret},
        )
        # Held so `close()` can return the pool's sockets rather than leaving
        # them to the garbage collector; a CLI process that exits immediately
        # would not care, but the scheduled sync holds one client across many
        # connections.
        self._api_client = plaid.ApiClient(configuration)
        self._api = plaid_api.PlaidApi(self._api_client)

    @property
    def environment(self) -> str:
        """Which aggregator environment this client talks to."""
        return self._environment

    def close(self) -> None:
        """Release the connection pool."""
        self._api_client.close()

    def __enter__(self) -> PlaidClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _fetch(
        self,
        endpoint: Endpoint,
        invoke: Callable[..., Any],
        request: Any,
        *,
        request_context: str | None = None,
    ) -> FetchedResponse:
        """Make one call and return what came back, undecoded.

        `received_at` is stamped from the clock here because this is the moment
        the system learned the thing, and it is the value every deriver is
        required to use instead of consulting a clock of its own.
        """
        try:
            raw = invoke(
                request,
                _preload_content=False,
                _request_timeout=self._timeout_seconds,
            )
        except plaid.ApiException as exc:
            # Classified into the connection-health taxonomy in Chunk 02. What
            # this build owes already is a message that names the cause.
            raise ConnectorError(_describe_failure(endpoint, exc)) from exc
        except urllib3.exceptions.HTTPError as exc:
            # The SDK wraps only SSL errors *(verified by probing an unreachable
            # host: a refused connection escapes as urllib3.MaxRetryError)*, so
            # without this a machine that is merely offline reports a traceback
            # from a library the operator never chose. Named as a transport
            # failure rather than an aggregator one, because the aggregator did
            # not answer -- and that distinction is what stops someone rotating a
            # secret that was never the problem.
            raise ConnectorError(
                f"{endpoint} could not reach the aggregator at the {self._environment} "
                f"host: {type(exc).__name__}"
            ) from exc
        try:
            body = raw.data
        finally:
            raw.release_conn()
        if not isinstance(body, bytes):  # pragma: no cover -- urllib3 returns bytes
            raise ConnectorError(
                f"{endpoint} returned {type(body).__name__}, expected undecoded bytes"
            )
        return FetchedResponse(
            endpoint=endpoint,
            body=body,
            received_at=now_utc(),
            request_context=request_context,
        )

    def institutions_get(
        self, *, count: int, offset: int, country_codes: list[str]
    ) -> FetchedResponse:
        """One page of the aggregator's supported-institution list.

        Chosen as the first call this product ever makes because it needs client
        credentials and nothing else -- no enrolled connection, no access token,
        no operator data -- so the whole path from config through keychain to the
        raw archive can be proven before enrollment exists.
        """
        request = InstitutionsGetRequest(
            count=count,
            offset=offset,
            country_codes=[CountryCode(code) for code in country_codes],
        )
        return self._fetch(
            INSTITUTIONS_GET,
            self._api.institutions_get,
            request,
            request_context=(
                f"count={count} offset={offset} "
                f"country_codes={','.join(country_codes)}"
            ),
        )
