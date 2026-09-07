"""The aggregator boundary: what the rest of the system is allowed to see.

Everything in this package's `plaid` subpackage speaks the aggregator's
vocabulary. Everything *outside* it speaks only what is defined here -- a
`FetchedResponse`, which is bytes and the facts about when they arrived.

**Why the boundary is a package rather than an interface.** `system-requirements.md`
§9.2 asked whether a second aggregator is ever expected, and the answer taken on
2026-09-06 was: one in v1, drawn so a second is a new module rather than a
rewrite. An abstract client `Protocol` with exactly one implementation would
encode that implementation's shape and call it a contract; the honest version
cannot be written until a second aggregator exists to disagree with the first.
So the boundary is containment, checked by
`tests/preferences/test_connector_is_contained.py`: nothing outside
`connector/plaid/` imports `plaid`, and nothing in `connector/` opens a
datastore.

🔴 **The connector cannot *obtain* a datastore handle**, which is the property
the norm test actually enforces and the one AC-5.1 rests on. The client returns
bytes and its caller archives them, so there is no path by which a response is
normalized before it is archived.

That is narrower than "the connector persists nothing", and the difference
became real when `connector/plaid/derivers.py` landed: a deriver *does* write
rows, through a connection handed to it by the caller that already opened one.
It cannot open one, cannot choose when the transaction commits, and cannot reach
the archive except through the response it was given. Saying "persists nothing"
would now be a guarantee this package does not make, which is worse than a
narrower one it does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from bankmachine.store.types import UtcInstant


class ConnectorError(Exception):
    """The aggregator could not be reached, or refused what it was asked.

    Defined here rather than beside the client so that catching an aggregator
    failure does not require importing the aggregator. The CLI's top-level
    handler turns these into a sentence, and it should not have to load the SDK
    to know what it is catching.

    **The subclasses below are the taxonomy FR-4 is written in, and they are
    organized by what the caller must do next rather than by what the aggregator
    called it.** An operator reading "the sync failed" learns nothing; the four
    outcomes that actually differ are *re-link this connection*, *wait and it
    will fix itself*, *fix this code*, and *fix the configuration*. A taxonomy
    keyed on the aggregator's own vocabulary would put `ITEM_LOCKED` and
    `ITEM_LOGIN_REQUIRED` side by side as if they were the same kind of thing,
    when one needs the operator at their bank's website and the other needs them
    in this product.

    Every field is optional because the earliest of these is raised before a call
    is made -- a missing client id fails at construction, where there is no
    endpoint and no connection yet.
    """

    #: Whether trying the same call again could plausibly succeed. Declared on
    #: every raisable subclass and deliberately **not** given a default here: a
    #: default is what lets a later error type inherit an answer nobody decided,
    #: and the two directions fail differently -- an un-retried transient stops
    #: the nightly sync, a retried permanent one hammers the aggregator with a
    #: call that cannot work.
    retryable: ClassVar[bool]

    #: A grouping class exists to be caught, never raised, and declares no
    #: `retryable` because it has no single answer to give.
    grouping: ClassVar[bool] = False

    def __init_subclass__(cls, *, grouping: bool = False, **kwargs: object) -> None:
        """Refuse, at class-creation time, a type that never decided whether it retries.

        🔴 **This is the mechanism; a test that walks `__subclasses__()` is not.**
        That walk sees only subclasses whose defining module has been imported,
        so it would guarantee something about "the types the test happens to
        import" rather than about every error type -- and the case it would miss
        is precisely the one this package's docstring anticipates, a second
        aggregator arriving as a new module. A type defined there would leave the
        walk green and raise `AttributeError` from inside the retry loop, one
        frame from the `raise`, on the error path of the error path.

        Enforcing at subclass creation moves the failure to import time, where
        it names the class that forgot.
        """
        super().__init_subclass__(**kwargs)
        cls.grouping = grouping
        if not grouping and "retryable" not in cls.__dict__:
            raise TypeError(
                f"{cls.__name__} must declare `retryable`: the retry loop asks the exception, "
                f"and inheriting the answer means inheriting one nobody decided for this type. "
                f"Pass `grouping=True` if it exists to be caught rather than raised"
            )

    def __new__(cls, *args: object, **kwargs: object) -> ConnectorError:
        """A grouping class cannot be instantiated, so it cannot be raised.

        Without this, `raise ConnectorError(...)` fails later and elsewhere --
        inside the retry loop, reading a class attribute that was never set --
        which is a confusing report of a simple mistake.
        """
        if cls.grouping or cls is ConnectorError:
            raise TypeError(
                f"{cls.__name__} groups error types and is not raisable. Raise the type that "
                f"names what the caller must do about this failure"
            )
        return super().__new__(cls, *args, **kwargs)

    def __init__(
        self,
        message: str,
        *,
        endpoint: Endpoint | None = None,
        connection_id: int | None = None,
        error_code: str | None = None,
        request_id: str | None = None,
        failed_at: UtcInstant | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.endpoint = endpoint
        self.connection_id = connection_id
        """Which connection this failure belongs to, when it belongs to one.

        AC-4.1 requires that one broken connection never abort another's sync,
        and a loop can only honour that if the error it catches says which
        connection it was. `None` means the call was not made on any connection's
        behalf -- an institution search, or a failure before enrollment exists.
        """
        self.error_code = error_code
        """The aggregator's own code, kept verbatim for `connections.last_error_code`.

        Recorded rather than translated because AC-4.2 asks for the error code,
        and a local name for it would make a support conversation with the
        aggregator harder in exactly the moment it is needed.
        """
        self.request_id = request_id
        self.retry_after_seconds = retry_after_seconds
        """How long the aggregator asked us to wait, when it said.

        A floor on the computed backoff rather than a replacement for it. `None`
        is the ordinary case -- most refusals carry no such instruction, and the
        policy's own schedule is always a valid answer.
        """
        self.failed_at = failed_at
        """When this failure happened.

        AC-4.5 refuses a degraded-state record whose data hole cannot be
        computed. The connector cannot compute it -- `last_success_at` lives in
        the datastore -- so what it owes the caller is the other end of the
        subtraction, stamped at the moment of failure rather than whenever the
        caller gets around to writing the row.
        """


class AggregatorNotConfiguredError(ConnectorError):
    """The credentials or settings this product holds are not usable.

    A missing client id, or an aggregator that rejected the pair outright. The
    operator has to change something here; retrying with the same values is the
    definition of hopeless.
    """

    retryable = False


class TransportError(ConnectorError):
    """The aggregator did not answer.

    Distinct from every refusal below, because the aggregator said nothing and
    so nothing about the request has been judged. Naming this separately is what
    stops an offline machine looking like a credential problem -- the wrong guess
    there costs a rotation that was never needed.
    """

    retryable = True


class CredentialBearingResponseError(ConnectorError):
    """A credential-bearing response was about to be turned into an archivable one.

    Raised at construction rather than at the archive, because the archive is
    append-only: by the time a token reaches `raw_responses` the damage is
    permanent and travels with every backup. This is a bug in the caller, not a
    condition -- there is nothing to retry and nothing to degrade.
    """

    retryable = False


class MalformedResponseError(ConnectorError):
    """The aggregator answered, and this build cannot use what it said.

    Distinct from a refusal: nothing was rejected, the response simply is not
    what the contract requires -- a decoded body where verbatim bytes were
    asked for, which means something upstream deserialized it and archiving it
    would put a re-encoding in the archive and call it verbatim.

    Not retried. A response shape does not change because it was asked for twice.
    """

    retryable = False


class AggregatorError(ConnectorError, grouping=True):
    """The aggregator answered, and the answer was a refusal.

    Everything below is a refusal this build recognized. An answer it did not
    recognize is `UnrecognizedAggregatorError`, which is a sibling rather than a
    fallback value of this one.

    🔴 **A grouping class, never raised** -- `grouping=True` makes that structural
    rather than a convention: it declares no `retryable`, and `__new__` refuses to
    build one, so the mistake fails at the `raise` instead of one frame away in
    the retry loop.
    """


class ReauthRequiredError(AggregatorError):
    """The connection's credentials no longer work; the operator must re-link it (AC-4.1).

    Terminal until a human acts, so retrying is not merely useless -- it delays
    the report that tells them to act, and AC-4.4 names silent staleness as this
    system's primary failure mode.
    """

    retryable = False


class ConnectionLockedError(AggregatorError):
    """The institution has locked the account (AC-4.1).

    Separate from `ReauthRequiredError` because the remedy is at the
    institution's own website, not in this product, and telling an operator to
    re-link an account their bank has locked sends them somewhere that cannot
    help.
    """

    retryable = False


class InstitutionUnavailableError(AggregatorError):
    """The institution is down or not responding (AC-4.1).

    Nobody has to do anything; it comes back. Not retried within a run, because
    an institution's outage outlasts any backoff worth waiting through, and a
    connection marked degraded for a day is the honest report. The *next* run is
    the retry.
    """

    retryable = False


class RateLimitedError(AggregatorError):
    """Too many calls, too fast (AC-4.1).

    The one refusal that is purely our own doing, and the one the architecture's
    retry channel exists for.
    """

    retryable = True


class DataNotReadyError(AggregatorError):
    """The aggregator has not finished assembling what was asked for (AC-2.6).

    A multi-year backfill takes time to materialize, and the requirement is
    explicit that this is backoff-and-retry rather than failure.
    """

    retryable = True


class AggregatorUnavailableError(AggregatorError):
    """The aggregator's own error, not ours.

    Retried because a 500 is frequently one bad server rather than a broken
    service, and because the alternative -- failing a nightly sync on a blip --
    produces the stale data AC-4.4 is written against.
    """

    retryable = True


class AggregatorRequestError(AggregatorError):
    """The aggregator rejected the request as malformed or unsupported.

    A bug in this code or an unsupported combination of arguments. Retrying an
    identical request that was just judged invalid cannot help.
    """

    retryable = False


class UnrecognizedAggregatorError(AggregatorError):
    """A refusal whose code this build has never seen.

    🔴 **Its own type, not a fallback into a neighbouring one.** Silence is the
    single disallowed outcome here, and quietly filing an unknown code under
    "probably transient" or "probably terminal" is silence wearing a
    classification: the first hides a connection that will never recover, the
    second retires one that only needed a retry. A type that says "this build
    does not know" is the only honest answer, and it carries the code so that
    finding out costs a grep rather than a reproduction.

    Not retried: an unknown refusal repeated is an unknown refusal.
    """

    retryable = False


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One of the aggregator's endpoints, named rather than spelled out at call sites.

    Three things need this to be a vocabulary rather than a scattering of string
    literals. The derivation registry is keyed by endpoint, so a deriver is
    registered against a name that has to match exactly. `store.raw`'s docstring
    defers the credential-archive rule to "the endpoint vocabulary that could
    enforce it", which is this. And AC-ARCH.4's guard reasonably reads a bare
    `"/institutions/get"` as an absolute filesystem path, because from a string
    literal's shape alone that is what it looks like -- wrapping it here says
    what it actually is, to the type checker and to that guard.
    """

    path: str
    """The aggregator's own path, which is also the archive key.

    Kept as the aggregator spells it rather than a local alias: a rebuild years
    from now has to be able to tell what a stored response was.
    """

    retry_safe: bool = True
    """Whether re-sending this call after a failure is harmless.

    🔴 **Not every call can be retried, and the retry channel's safety argument
    only covered the local side.** It is true that the connector persists
    nothing, so a retry cannot interleave a second attempt against a partial
    write *here* -- but `/item/public_token/exchange` consumes a single-use
    public token and mints a durable Item at the far end. A transport failure
    after the aggregator processed the request, retried, either fails on a spent
    token or enrolls twice; neither is recoverable by this side, and both look
    like a network blip in the logs.

    Declared on the endpoint rather than passed at the call site, for the same
    reason `issues_credential` is: the property belongs to the thing it is about,
    and a flag at one call site is a decision the next call site never sees.
    """

    issues_credential: bool = False
    """Whether this endpoint's response body carries a credential.

    🔴 **This is what makes the archive exemption a relationship rather than a
    skip-list.** `store/raw.py` is append-only and a datastore backup travels, so
    archiving a body that carries an access token would satisfy AC-5.1 by
    breaking AC-10.1 *permanently*. A list of exempt paths kept beside the
    archive would be an enumeration standing in for that property: correct on the
    day it was written, and silently wrong the first time an endpoint is added by
    someone who did not think to open that file.

    Declared here, on the endpoint itself, the property travels with the thing it
    is about -- and `FetchedResponse` refuses to exist for an endpoint that has
    it, so there is no object for the archive to be handed.
    """

    def __str__(self) -> str:
        return self.path


#: The supported-institution list. Needs client credentials and nothing else --
#: no enrolled connection, no access token, no operator data.
INSTITUTIONS_GET = Endpoint("/institutions/get")

#: Opens a Link session. Its response carries a `link_token`, which authorizes
#: enrollment against this product's account -- short-lived, and still a
#: credential.
LINK_TOKEN_CREATE = Endpoint("/link/token/create", issues_credential=True)

#: Trades a public token for the access token a connection is thereafter read
#: with. 🔴 The single most sensitive response this product ever receives:
#: *(verified live)* its body is exactly `access_token`, `item_id`, `request_id`.
ITEM_PUBLIC_TOKEN_EXCHANGE = Endpoint(
    "/item/public_token/exchange", retry_safe=False, issues_credential=True
)

#: The state of a Link session, polled while the operator completes it in a
#: browser. 🔴 `issues_credential` because a *finished* session's body carries the
#: `public_token` -- the same single-use value the exchange spends to mint a
#: durable Item, so archiving this response would put a credential in the
#: append-only store exactly as archiving the exchange would. `retry_safe`
#: because polling is a pure read that the far end can absorb any number of
#: times; the two properties are independent and both are declared.
LINK_TOKEN_GET = Endpoint("/link/token/get", issues_credential=True)

#: One connection's own record of itself -- which products it was enrolled with,
#: and which it could support. Carries no credential: the access token goes up in
#: the request, and nothing comes back down.
ITEM_GET = Endpoint("/item/get")

#: The accounts behind one connection.
ACCOUNTS_GET = Endpoint("/accounts/get")


@dataclass(frozen=True, slots=True)
class LinkToken:
    """A Link session, and the window it was opened asking for.

    `requested_history_days` is carried because the response does not contain it
    *(verified live: a hosted session replies with `expiration`, `hosted_link_url`,
    `link_token`, `request_id`, and none of those is the window)*. Without it the
    caller would have no record of what was asked for, and AC-11.8's shortfall --
    requested minus granted -- would have no left-hand side.

    `hosted_link_url` is what AC-1.1 prints. It is the operator-facing half of the
    session and is deliberately NOT redacted: it is a URL the operator must be
    able to read off their terminal and open, and a session they are being asked
    to complete is not a secret being kept from them. The `token` beside it is a
    credential and stays out of the `repr`.

    Defined here rather than beside the client so that the enrollment command can
    name one without importing the aggregator SDK.
    """

    token: str = field(repr=False)
    expires_at: str
    requested_history_days: int
    hosted_link_url: str

    def __repr__(self) -> str:
        """🔴 The token stays out, per the never-in-a-`repr` norm.

        A dataclass writes every field into its generated `repr`, and a `repr` is
        what reaches a traceback, a debugger and a log line that interpolated the
        object rather than a field of it -- which is exactly the accident the
        norm exists to prevent.
        """
        return (
            f"LinkToken(token=<redacted>, expires_at={self.expires_at!r}, "
            f"requested_history_days={self.requested_history_days}, "
            f"hosted_link_url={self.hosted_link_url!r})"
        )


@dataclass(frozen=True, slots=True)
class LinkSession:
    """One polled look at a Link session the operator is completing in a browser.

    🔴 **`finished` is derived from the token rather than stored beside it.** The
    aggregator reports an unfinished session by *omitting* `link_sessions`
    entirely -- not an empty list, not a status field *(verified live: an
    unfinished session replies with `created_at`, `expiration`, `link_token`,
    `metadata`, `request_id` and no `link_sessions` key at all)*. A separate
    `finished` flag could be set with no token or unset with one, and the caller
    that trusted the flag would exchange `None`; deriving it means the two cannot
    disagree.

    `institution_id` is what the operator picked. It is recorded for the log line
    that says which institution was enrolled, not as the source of truth --
    AC-1.3's institution comes from `/item/get`, which reports the institution the
    Item actually belongs to.
    """

    public_token: str | None = field(repr=False)
    session_id: str | None
    institution_id: str | None

    @property
    def finished(self) -> bool:
        """Whether the operator has completed the session."""
        return self.public_token is not None

    def __repr__(self) -> str:
        """The public token is single-use, and single-use is not the same as harmless."""
        held = "<redacted>" if self.public_token is not None else None
        return (
            f"LinkSession(public_token={held}, session_id={self.session_id!r}, "
            f"institution_id={self.institution_id!r})"
        )


@dataclass(frozen=True, slots=True)
class AccessGrant:
    """What an exchange yields: a credential, and the aggregator's id for the connection.

    Deliberately not a `FetchedResponse`. There is no path from this type into
    `store.raw`, which is what keeps the archive exemption structural rather than
    remembered.
    """

    access_token: str = field(repr=False)
    source_connection_id: str

    def __repr__(self) -> str:
        """The access token is the most sensitive value this product holds."""
        return (
            f"AccessGrant(access_token=<redacted>, "
            f"source_connection_id={self.source_connection_id!r})"
        )


@dataclass(frozen=True, slots=True)
class FetchedResponse:
    """One response, exactly as the aggregator sent it.

    `body` is undecoded bytes on purpose. Parsing it here would make this type a
    normalization step, and AC-5.1 puts the archive *before* any of those; the
    bytes that reach `store.raw.record_response` are the bytes that came off the
    wire, so the digest recorded beside them is a digest of what the aggregator
    actually said.
    """

    endpoint: Endpoint
    body: bytes
    received_at: UtcInstant
    request_context: str | None
    """What was asked for, when that is not recoverable from the response.

    A paginated fetch returns page three with nothing in it saying so.
    """

    def __post_init__(self) -> None:
        """Refuse to exist for an endpoint whose body carries a credential.

        🔴 **The archive exemption, held by construction.** `store.raw` derives
        everything it persists from one of these, so an endpoint that cannot
        produce one cannot be archived -- by any caller, including one written
        years from now by someone who never read `store/raw.py`'s docstring.

        The alternative was a list of exempt paths consulted at the archive
        site. That is an enumeration standing in for a property, and this
        project has already been burned once by a rule that matched on a name
        where it meant a relationship. Here the relationship is "this response
        carries a credential", it is declared on the endpoint, and the type that
        feeds the archive checks it.
        """
        if self.endpoint.issues_credential:
            raise CredentialBearingResponseError(
                f"{self.endpoint} issues a credential, so its body must never reach the "
                f"archive: `raw_responses` is append-only and a datastore backup travels, "
                f"so a token written there is written permanently. Read what is needed out "
                f"of the body and let the body go"
            )
