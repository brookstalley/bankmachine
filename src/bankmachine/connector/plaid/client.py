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
import time
from collections.abc import Callable
from typing import Any, Final

import plaid
import urllib3.exceptions
from plaid.api import plaid_api
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.country_code import CountryCode
from plaid.model.institutions_get_request import InstitutionsGetRequest
from plaid.model.item_get_request import ItemGetRequest
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.item_remove_request import ItemRemoveRequest
from plaid.model.link_token_create_hosted_link import LinkTokenCreateHostedLink
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.link_token_get_request import LinkTokenGetRequest
from plaid.model.link_token_transactions import LinkTokenTransactions
from plaid.model.products import Products
from plaid.model.transactions_sync_request import TransactionsSyncRequest

from bankmachine.config import MAX_HISTORY_DAYS, Config
from bankmachine.connector import (
    ACCOUNTS_GET,
    INSTITUTIONS_GET,
    ITEM_GET,
    ITEM_PUBLIC_TOKEN_EXCHANGE,
    ITEM_REMOVE,
    LINK_TOKEN_CREATE,
    LINK_TOKEN_GET,
    TRANSACTIONS_SYNC,
    AccessGrant,
    AggregatorNotConfiguredError,
    ConnectorError,
    Endpoint,
    FetchedResponse,
    LinkSession,
    LinkToken,
    MalformedResponseError,
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

#: How long the hosted enrollment URL stays usable. The operator has to leave the
#: terminal, open a browser, find their institution and pass its authentication --
#: sometimes including a one-time code from a phone. Fifteen minutes is enough for
#: that without leaving a URL that mints an Item lying around for an afternoon.
DEFAULT_HOSTED_URL_LIFETIME_SECONDS: Final = 900

#: How many transaction changes to ask for per page. The aggregator's own
#: maximum is larger; this is smaller on purpose, because AC-2.5's guarantee is
#: that a crash loses at most one uncommitted page, and a page is the unit of
#: that loss.
TRANSACTIONS_PAGE_SIZE: Final = 100


def _first_item_add_result(session: dict[str, Any]) -> dict[str, Any] | None:
    """The one item this session added, or None if it has added none yet.

    Selected once and then read for every field that comes from it. The
    alternative -- each field walking the entries for itself -- lets the public
    token come from one item and the institution from another, producing a
    connection labelled with an institution it does not belong to. Both values
    would be individually well-formed, so nothing downstream could notice.
    """
    results = session.get("results")
    if not isinstance(results, dict):
        return None
    added = results.get("item_add_results")
    if not isinstance(added, list):
        return None
    for entry in added:
        if isinstance(entry, dict):
            return entry
    return None


def _public_token_of(session: dict[str, Any], added: dict[str, Any] | None) -> str | None:
    """The public token a finished Link session yields, or None while it runs.

    🔴 **Two places, read in order, because the aggregator's own models offer
    both and this product has not yet observed which a real completion uses.**
    `results.item_add_results[].public_token` is the current shape;
    `on_success.public_token` is the older one the SDK still types. Reading both
    costs a few lines and removes a class of failure that would only appear at a
    real enrollment -- the moment this product is least able to retry, since a
    completed session cannot be completed again.

    A session that is present but has neither is not an error: a session exists
    from the moment the operator opens the URL, and carries no token until they
    finish.
    """
    if added is not None:
        token = added.get("public_token")
        if isinstance(token, str):
            return token
    on_success = session.get("on_success")
    if isinstance(on_success, dict):
        token = on_success.get("public_token")
        if isinstance(token, str):
            return token
    return None


def _session_public_token(session: dict[str, Any]) -> str | None:
    """Whether a session carries a token, asked while choosing among several.

    The chosen session is then read once, through `_public_token_of`, so the
    selection pass and the read cannot disagree about which item they mean.
    """
    return _public_token_of(session, _first_item_add_result(session))


def _institution_id_of(added: dict[str, Any] | None) -> str | None:
    """Which institution the operator picked, for the log line that says so.

    Not the source of truth for the connection's institution -- that comes from
    `/item/get`, which reports the one the Item actually belongs to. Takes the
    already-selected item rather than searching for one, so it cannot disagree
    with the token about which item it is describing.
    """
    if added is None:
        return None
    institution = added.get("institution")
    if not isinstance(institution, dict):
        return None
    institution_id = institution.get("institution_id")
    return institution_id if isinstance(institution_id, str) else None


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


#: What Link is told to call this application, and in what language. Fixed rather
#: than configured: they are this product's own identity in someone else's UI,
#: not an operator preference, and `client_name` is what the operator will see at
#: the top of the enrollment flow.
LINK_CLIENT_NAME: Final = "bankmachine"
LINK_LANGUAGE: Final = "en"


def _payload(endpoint: Endpoint, body: bytes) -> dict[str, Any]:
    """Read a response body this client has to look inside.

    Used only where the client itself needs a value -- a link token, an access
    token. Archivable responses are never parsed here: AC-5.1 puts the archive
    before any normalization, and a client that parsed on the way through would
    make itself the first normalization step.
    """
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise MalformedResponseError(
            f"{endpoint} answered with something that is not JSON", endpoint=endpoint
        ) from exc
    if not isinstance(payload, dict):
        raise MalformedResponseError(
            f"{endpoint} answered with {type(payload).__name__}, expected an object",
            endpoint=endpoint,
        )
    return payload


def institution_ref_of(item_body: bytes) -> tuple[str, str]:
    """The institution behind one connection: its source id and its name.

    Read here rather than in the caller for the same reason `capabilities_of` is:
    the shape of an item body is the aggregator's, and knowing it is what
    `connector/plaid/` exists to contain. A CLI reaching into `item.institution_id`
    would put aggregator knowledge in a module the containment test cannot guard.

    🔴 From `/item/get`, never from `/institutions/get`. This is the institution
    the Item actually belongs to; the catalogue endpoint serves the aggregator's
    production list of every institution it supports, which is not a fact about
    this operator.
    """
    payload = _payload(ITEM_GET, item_body)
    item = payload.get("item")
    if not isinstance(item, dict):
        raise MalformedResponseError(
            f"{ITEM_GET} answered without an item object", endpoint=ITEM_GET
        )
    source_id = item.get("institution_id")
    name = item.get("institution_name")
    if not isinstance(source_id, str) or not isinstance(name, str):
        raise MalformedResponseError(
            f"{ITEM_GET} answered without an institution_id and institution_name, so the "
            f"connection has no institution to hang from",
            endpoint=ITEM_GET,
        )
    return source_id, name


def capabilities_of(item_body: bytes) -> frozenset[str]:
    """What a connection can do, read from its own record.

    🔴 **`available_products`, not `products`.** AC-3.2 requires investments to be
    pulled for any connection whose capabilities include investments and **never
    for a named institution** -- so this must answer "what could this connection
    do", and `products` answers "what did we already ask for". A discovery reading
    `products` would report back exactly what this product requested and never
    discover anything *(measured: a sandbox item enrolled with `transactions`
    returns `products: ['transactions']` and 14 entries in `available_products`)*.

    Nothing here branches on `institution_id`, and nothing may: the whole point
    of discovery is that the roster stays out of the code.
    """
    payload = _payload(ITEM_GET, item_body)
    item = payload.get("item")
    if not isinstance(item, dict):
        raise MalformedResponseError(f"{ITEM_GET} answered without an item", endpoint=ITEM_GET)
    available = item.get("available_products")
    if not isinstance(available, list):
        raise MalformedResponseError(
            f"{ITEM_GET} answered without an available_products list", endpoint=ITEM_GET
        )
    return frozenset(product for product in available if isinstance(product, str))


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
        """Make one call and return what came back, undecoded and archivable.

        `received_at` is stamped from the clock here because this is the moment
        the system learned the thing, and it is the value every deriver is
        required to use instead of consulting a clock of its own.

        `connection_id` travels into every failure raised from here. AC-4.1's
        "one broken connection never aborts another" is a property of the
        caller's loop, and a loop can only honour it if the error it catches
        says which connection it belongs to.

        Refuses a credential-issuing endpoint, by way of `FetchedResponse`
        itself: those go through `_fetch_bytes`, which hands the body to a
        wrapper that reads what it needs and lets the rest go.
        """
        return FetchedResponse(
            endpoint=endpoint,
            body=self._fetch_bytes(endpoint, invoke, request, connection_id=connection_id),
            received_at=self._now(),
            request_context=request_context,
        )

    def _fetch_bytes(
        self,
        endpoint: Endpoint,
        invoke: Callable[..., Any],
        request: Any,
        *,
        connection_id: int | None = None,
    ) -> bytes:
        """The body, with retries, and nothing built around it.

        The only route a credential-issuing response takes. Its caller parses out
        the fields it needs and returns those; the body itself is never handed to
        anything that could persist it, because nothing here builds the type
        `store.raw` consumes.
        """
        # 🔴 A call the far end cannot absorb twice is made once. The retry
        # channel's safety argument -- that this side persists nothing, so there
        # is no partial write for a second attempt to interleave against -- is
        # about the local side only, and an exchange spends a single-use token
        # and mints a durable Item at the aggregator.
        policy = self._retry_policy if endpoint.retry_safe else RetryPolicy(attempts=1)
        return call_with_retry(
            lambda: self._attempt(endpoint, invoke, request, connection_id=connection_id),
            policy=policy,
            sleep=self._sleep,
            now=self._now,
        )

    def _attempt(
        self,
        endpoint: Endpoint,
        invoke: Callable[..., Any],
        request: Any,
        *,
        connection_id: int | None,
    ) -> bytes:
        """One call, with every way it can fail turned into a local type.

        Returns bytes rather than a `FetchedResponse` because a
        credential-issuing endpoint must never have one built for it, and the
        thing that must not exist should not be constructed here and discarded
        upstream.
        """
        raw = None
        try:
            raw = invoke(
                request,
                _preload_content=False,
                _request_timeout=self._timeout_seconds,
            )
            body = raw.data
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
        except OSError as exc:
            # The body is read inside this `try` on purpose. urllib3 streams it,
            # so a connection reset lands *here* rather than at the call -- and
            # read outside, it escaped as a bare `OSError` from a connector whose
            # whole contract is that its failures are local types. Retryable,
            # because a reset connection is the transient case by definition.
            raise TransportError(
                f"{endpoint} lost the connection to the aggregator while the response "
                f"was being read: {type(exc).__name__}",
                endpoint=endpoint,
                connection_id=connection_id,
                failed_at=self._now(),
            ) from exc
        finally:
            if raw is not None:
                raw.release_conn()
        if not isinstance(body, bytes):
            raise MalformedResponseError(
                f"{endpoint} returned {type(body).__name__}, expected undecoded bytes",
                endpoint=endpoint,
                connection_id=connection_id,
                failed_at=self._now(),
            )
        return body

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

    def link_token_create(
        self,
        *,
        history_days: int,
        client_user_id: str,
        country_codes: list[str],
        products: list[str],
        hosted_url_lifetime_seconds: int = DEFAULT_HOSTED_URL_LIFETIME_SECONDS,
    ) -> LinkToken:
        """Open a Link session that will request `history_days` of history.

        🔴 **`history_days` is required and has no default, and that is the whole
        point of this signature.** AC-1.2 makes the granted window immutable
        after enrollment, and the requirements call a vendor-default build a
        failed build -- so a caller that forgets the window must fail to
        typecheck rather than silently enroll at whatever the aggregator would
        have chosen. A default value here is precisely how that failure happens,
        and it would not be visible until someone asked for three-year-old
        transactions and found they had never been fetched.

        The response carries a `link_token` and nothing else this product keeps;
        it is a credential, so it never becomes an archivable response.
        """
        if not 1 <= history_days <= MAX_HISTORY_DAYS:
            raise AggregatorNotConfiguredError(
                f"history_days must be between 1 and {MAX_HISTORY_DAYS}, got {history_days}. "
                f"The aggregator would reject it, and the window cannot be changed after "
                f"enrollment"
            )
        request = LinkTokenCreateRequest(
            client_name=LINK_CLIENT_NAME,
            language=LINK_LANGUAGE,
            country_codes=[CountryCode(code) for code in country_codes],
            user=LinkTokenCreateRequestUser(client_user_id=client_user_id),
            products=[Products(product) for product in products],
            transactions=LinkTokenTransactions(days_requested=history_days),
            # 🔴 What makes AC-1.1 reachable without a local web server. Asking
            # for a hosted session is what puts `hosted_link_url` in the reply;
            # without it the operator has a token and nowhere to type it, and
            # the product would need a listener and a registered redirect URI.
            hosted_link=LinkTokenCreateHostedLink(
                url_lifetime_seconds=hosted_url_lifetime_seconds,
            ),
        )
        body = self._fetch_bytes(LINK_TOKEN_CREATE, self._api.link_token_create, request)
        payload = _payload(LINK_TOKEN_CREATE, body)
        token = payload.get("link_token")
        expiration = payload.get("expiration")
        hosted_url = payload.get("hosted_link_url")
        if not isinstance(token, str) or not isinstance(expiration, str):
            raise MalformedResponseError(
                f"{LINK_TOKEN_CREATE} answered without a link_token and expiration",
                endpoint=LINK_TOKEN_CREATE,
                failed_at=self._now(),
            )
        if not isinstance(hosted_url, str) or not hosted_url:
            # Checked separately from the pair above because its absence means
            # something different and more specific: the request asked for a
            # hosted session and did not get one, which is the aggregator saying
            # Hosted Link is not available here. Folding it into the same message
            # would send the operator looking at their link token.
            raise MalformedResponseError(
                f"{LINK_TOKEN_CREATE} returned no hosted_link_url, so there is no URL to "
                f"enrol at. The request asked for a hosted session; an account without "
                f"Hosted Link enabled is the likely cause",
                endpoint=LINK_TOKEN_CREATE,
                failed_at=self._now(),
            )
        return LinkToken(
            token=token,
            expires_at=expiration,
            requested_history_days=history_days,
            hosted_link_url=hosted_url,
        )

    def link_token_get(self, link_token: str) -> LinkSession:
        """Poll one Link session. Never archived: a finished one carries a credential.

        🔴 **An unfinished session omits `link_sessions` altogether** *(verified
        live)* -- it is not an empty list and there is no status field, so
        "still waiting" is the absence of a key. Reading a length here would
        raise on every poll before the operator finishes, which is most of them.

        The endpoint is `retry_safe`: polling is a pure read the far end can
        absorb any number of times. It is also `issues_credential`, so its body
        cannot become a `FetchedResponse` and therefore cannot be archived --
        the public token is read out here and the body let go.
        """
        body = self._fetch_bytes(
            LINK_TOKEN_GET, self._api.link_token_get, LinkTokenGetRequest(link_token=link_token)
        )
        payload = _payload(LINK_TOKEN_GET, body)
        sessions = payload.get("link_sessions")
        if sessions is None:
            # Absent is the live unfinished shape, and the only one that means
            # "not yet". Kept distinct from a wrong type below: collapsing them
            # would poll a malformed response until the enrollment timed out,
            # which is the failure every comment in this method exists to avoid.
            return LinkSession(public_token=None, session_id=None, institution_id=None)
        if not isinstance(sessions, list):
            raise MalformedResponseError(
                f"{LINK_TOKEN_GET} answered with link_sessions as "
                f"{type(sessions).__name__}, not a list",
                endpoint=LINK_TOKEN_GET,
                failed_at=self._now(),
            )
        if not sessions:
            return LinkSession(public_token=None, session_id=None, institution_id=None)
        # An entry that is not an object is skipped rather than raised on. One
        # unreadable entry must not abort the poll: the readable ones may hold a
        # completed session, and refusing the whole response would strand an Item
        # that already exists at the aggregator -- spent, billable, and invisible
        # from here. That is the same harm the ordering below guards against, so
        # it would be incoherent to reintroduce it as a validation.
        readable = [entry for entry in sessions if isinstance(entry, dict)]
        if not readable:
            # Every entry unreadable is different in kind: there is nothing to
            # poll, and reporting "still waiting" would wait forever on a response
            # that will never become readable.
            raise MalformedResponseError(
                f"{LINK_TOKEN_GET} returned {len(sessions)} link_sessions and not one "
                f"is an object, so no session can be read",
                endpoint=LINK_TOKEN_GET,
                failed_at=self._now(),
            )
        # 🔴 Every session is searched, and the NEWEST match wins -- both halves
        # matter and they are one decision. One hosted URL can be opened more than
        # once, and each opening is another entry. Searching all of them means an
        # operator who completes the flow and then reopens the link is not
        # reported as "still waiting" forever while their Item already exists.
        # Taking the newest means that when two sessions are BOTH finished -- the
        # same reopening, completed twice -- the token exchanged belongs to the
        # Item just created, not to an older one whose public token may already
        # have expired while the newer Item stays live and billable. The fallback
        # for the unfinished case is newest for the same reason, so one rule
        # covers both rather than two that can disagree.
        finished = next(
            (s for s in reversed(readable) if _session_public_token(s) is not None), None
        )
        session = finished if finished is not None else readable[-1]
        added = _first_item_add_result(session)
        session_id = session.get("link_session_id")
        return LinkSession(
            public_token=_public_token_of(session, added),
            session_id=session_id if isinstance(session_id, str) else None,
            institution_id=_institution_id_of(added),
        )

    def exchange_public_token(self, public_token: str) -> AccessGrant:
        """Trade a public token for the access token a connection is read with.

        🔴 The most sensitive response this product ever receives. Its body is
        `access_token`, `item_id`, `request_id` *(verified live)*, and it is
        never archived -- not by a rule anyone follows, but because
        `ITEM_PUBLIC_TOKEN_EXCHANGE` is declared credential-issuing and
        `FetchedResponse` refuses to exist for such an endpoint. The body is read
        here and let go.
        """
        request = ItemPublicTokenExchangeRequest(public_token=public_token)
        body = self._fetch_bytes(
            ITEM_PUBLIC_TOKEN_EXCHANGE, self._api.item_public_token_exchange, request
        )
        payload = _payload(ITEM_PUBLIC_TOKEN_EXCHANGE, body)
        access_token = payload.get("access_token")
        item_id = payload.get("item_id")
        if not isinstance(access_token, str) or not isinstance(item_id, str):
            raise MalformedResponseError(
                f"{ITEM_PUBLIC_TOKEN_EXCHANGE} answered without an access_token and item_id",
                endpoint=ITEM_PUBLIC_TOKEN_EXCHANGE,
                failed_at=self._now(),
            )
        return AccessGrant(access_token=access_token, source_connection_id=item_id)

    def item_get(self, access_token: str, *, connection_id: int | None = None) -> FetchedResponse:
        """One connection's own record of itself. Archivable: nothing comes back down."""
        return self._fetch(
            ITEM_GET,
            self._api.item_get,
            ItemGetRequest(access_token=access_token),
            connection_id=connection_id,
        )

    def item_remove(
        self, access_token: str, *, connection_id: int | None = None
    ) -> FetchedResponse:
        """End a connection at the aggregator. Archivable: nothing comes back down.

        The counterpart to enrollment, and the half that keeps a re-link from
        leaving a paid-for Item behind. `connections.retired_at` records the local
        decision; this is what makes the far end agree with it.
        """
        return self._fetch(
            ITEM_REMOVE,
            self._api.item_remove,
            ItemRemoveRequest(access_token=access_token),
            connection_id=connection_id,
        )

    def transactions_sync(
        self,
        access_token: str,
        *,
        cursor: str | None,
        count: int = TRANSACTIONS_PAGE_SIZE,
        connection_id: int | None = None,
    ) -> FetchedResponse:
        """One page of transaction changes. Archivable: nothing comes back down.

        `cursor` is `None` for a connection that has never synced, which asks for
        everything the aggregator will grant. It is a required keyword rather than
        a defaulted one for the same reason `history_days` is: a caller that
        forgot it would silently re-fetch all history on every run, and the cost
        would show up as a rate limit rather than as a wrong answer.
        """
        request = TransactionsSyncRequest(access_token=access_token, count=count)
        if cursor is not None:
            request.cursor = cursor
        return self._fetch(
            TRANSACTIONS_SYNC,
            self._api.transactions_sync,
            request,
            request_context=f"cursor={'initial' if cursor is None else 'resumed'} count={count}",
            connection_id=connection_id,
        )

    def accounts_get(
        self, access_token: str, *, connection_id: int | None = None
    ) -> FetchedResponse:
        """The accounts behind one connection."""
        return self._fetch(
            ACCOUNTS_GET,
            self._api.accounts_get,
            AccountsGetRequest(access_token=access_token),
            connection_id=connection_id,
        )
