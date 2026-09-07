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

import time
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
    AggregatorRequestError,
    ConnectorError,
    Endpoint,
    FetchedResponse,
    TransportError,
)
from bankmachine.connector.plaid.errors import (
    RetryPolicy,
    Sleeper,
    call_with_retry,
    classify,
    describe,
    parse_error_body,
    parse_retry_after,
)
from bankmachine.store.types import UtcInstant, now_utc

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
        retry_policy: RetryPolicy | None = None,
        sleep: Sleeper = time.sleep,
        now: Callable[[], UtcInstant] = now_utc,
    ) -> None:
        if not config.plaid_client_id:
            raise AggregatorNotConfiguredError(
                "no aggregator client id is configured. Set `plaid_client_id` in the config file "
                "or BANKMACHINE_PLAID_CLIENT_ID in the environment"
            )
        self._environment = config.environment
        self._timeout_seconds = timeout_seconds
        self._retry_policy = RetryPolicy() if retry_policy is None else retry_policy
        # Injected so the suite exercises the real backoff decisions at full
        # speed. A test that had to wait out the schedule would be rewritten to
        # a one-attempt policy, and the schedule -- the part with a bug in it --
        # would stop being tested at all.
        self._sleep = sleep
        self._now = now
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
        connection_id: int | None = None,
    ) -> FetchedResponse:
        """Make one call and return what came back, undecoded.

        `received_at` is stamped from the clock here because this is the moment
        the system learned the thing, and it is the value every deriver is
        required to use instead of consulting a clock of its own.

        `connection_id` travels into every failure raised from here. AC-4.1's
        "one broken connection never aborts another" is a property of the
        caller's loop, and a loop can only honour it if the error it catches
        says which connection it belongs to.
        """
        return call_with_retry(
            lambda: self._attempt(
                endpoint,
                invoke,
                request,
                request_context=request_context,
                connection_id=connection_id,
            ),
            policy=self._retry_policy,
            sleep=self._sleep,
            now=self._now,
        )

    def _attempt(
        self,
        endpoint: Endpoint,
        invoke: Callable[..., Any],
        request: Any,
        *,
        request_context: str | None,
        connection_id: int | None,
    ) -> FetchedResponse:
        """One call, with every way it can fail turned into a local type."""
        try:
            raw = invoke(
                request,
                _preload_content=False,
                _request_timeout=self._timeout_seconds,
            )
        except plaid.ApiException as exc:
            raise self._refusal(endpoint, exc, connection_id) from exc
        except urllib3.exceptions.HTTPError as exc:
            # The SDK wraps only SSL errors *(verified by probing an unreachable
            # host: a refused connection escapes as urllib3.MaxRetryError)*, so
            # without this a machine that is merely offline reports a traceback
            # from a library the operator never chose. Named as a transport
            # failure rather than an aggregator one, because the aggregator did
            # not answer -- and that distinction is what stops someone rotating a
            # secret that was never the problem.
            raise TransportError(
                f"{endpoint} could not reach the aggregator at the {self._environment} "
                f"host: {type(exc).__name__}",
                endpoint=endpoint,
                connection_id=connection_id,
                failed_at=self._now(),
            ) from exc
        except AttributeError as exc:
            # 🔴 A bug in the SDK, contained here because it surfaces as a
            # traceback from a library the operator never chose. `rest.py` raises
            # `ApiException(status=0, reason=...)` with no body when a bare
            # `urllib3.exceptions.SSLError` escapes retry wrapping, and
            # `api_client.py` then runs `e.body.decode('utf-8')` on that `None`
            # *(both verified by probe: the AttributeError is raised at
            # api_client.py:204 with the ApiException as its `__context__`)*.
            #
            # The narrowing is the point. Catching `AttributeError` around a call
            # would swallow every genuine typo in this module, so this re-raises
            # untouched unless the SDK's own exception is standing behind it.
            if not isinstance(exc.__context__, plaid.ApiException):
                raise
            raise TransportError(
                f"{endpoint} failed inside the aggregator SDK while it was reporting a "
                f"transport error of its own: {exc.__context__.reason}",
                endpoint=endpoint,
                connection_id=connection_id,
                failed_at=self._now(),
            ) from exc
        try:
            body = raw.data
        finally:
            raw.release_conn()
        if not isinstance(body, bytes):  # pragma: no cover -- urllib3 returns bytes
            raise AggregatorRequestError(
                f"{endpoint} returned {type(body).__name__}, expected undecoded bytes",
                endpoint=endpoint,
                connection_id=connection_id,
                failed_at=self._now(),
            )
        return FetchedResponse(
            endpoint=endpoint,
            body=body,
            received_at=self._now(),
            request_context=request_context,
        )

    def _refusal(
        self,
        endpoint: Endpoint,
        exc: plaid.ApiException,
        connection_id: int | None,
    ) -> ConnectorError:
        """The local exception one aggregator refusal becomes.

        Built rather than raised so the `raise ... from exc` at the call site
        keeps the SDK's exception as the cause -- the classification is this
        product's reading of the failure, and the original stays reachable for
        anyone who disagrees with it.
        """
        detail = parse_error_body(exc.body)
        return classify(exc.status, detail)(
            describe(endpoint, exc.status, exc.reason, detail),
            endpoint=endpoint,
            connection_id=connection_id,
            error_code=detail.error_code,
            request_id=detail.request_id,
            failed_at=self._now(),
            retry_after_seconds=parse_retry_after(exc.headers),
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
                f"count={count} offset={offset} country_codes={','.join(country_codes)}"
            ),
        )
