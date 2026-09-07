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

**The connector persists nothing.** It returns bytes; the caller archives them
through `store.raw`. That is what keeps AC-5.1's "before any normalization"
true by construction rather than by everyone remembering to archive first --
there is no path through this package that could write a normalized row, because
this package cannot reach the datastore at all.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    #: every concrete subclass and deliberately **not** given a default here: a
    #: default is what lets a later error type inherit an answer nobody decided,
    #: and the two directions fail differently -- an un-retried transient stops
    #: the nightly sync, a retried permanent one hammers the aggregator with a
    #: call that cannot work. `test_every_error_type_decides_whether_it_retries`
    #: is the mechanism; without it this is a comment.
    retryable: ClassVar[bool]

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


class AggregatorError(ConnectorError):
    """The aggregator answered, and the answer was a refusal.

    Everything below is a refusal this build recognized. An answer it did not
    recognize is `UnrecognizedAggregatorError`, which is a sibling rather than a
    fallback value of this one.
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
    literals. `store.derivation.DERIVERS` is keyed by endpoint, so a deriver is
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

    def __str__(self) -> str:
        return self.path


#: The supported-institution list. Needs client credentials and nothing else --
#: no enrolled connection, no access token, no operator data.
INSTITUTIONS_GET = Endpoint("/institutions/get")


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
