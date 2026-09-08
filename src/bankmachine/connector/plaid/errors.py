"""The aggregator's error surface, turned into the states FR-4 is written in.

**Why this mapping is not read off the SDK.** `plaid-python`'s `PlaidError`
model types `error_code` as a bare `str` -- there is no generated enum for it,
so there is no authoritative list to import *(verified: `plaid/model/plaid_error.py`
declares `'error_code': (str,)` and its `allowed_values` is empty)*. `error_type`
*is* an enum (`plaid/model/plaid_error_type.py`, 25 values), which is why it
serves as the second layer here. The individual codes below each come from one
of two places, and the difference is recorded per entry: a value observed in a
live response, or a value the SDK spells out somewhere else in its own source.

**Two layers, because they fail differently.** The code layer is precise and
incomplete; the type layer is complete and coarse. A code the aggregator adds
tomorrow falls through the first and is still classified by the second, which is
the difference between a sync that degrades one connection and a sync that
raises something nobody wrote a handler for. Each layer is tested separately:
a behavioural test cannot tell them apart, and "the system still classified it"
is exactly how a lost layer hides.

🔴 **`ITEM_ERROR` is deliberately absent from the type layer.** It spans
`ITEM_LOGIN_REQUIRED` (re-link it), `ITEM_LOCKED` (go to your bank) and
`NO_ACCOUNTS` (nothing to sync) -- three different remedies. Mapping it to any
one of them would produce a confident classification that is wrong two-thirds of
the time, which is worse than the refusal `UnrecognizedAggregatorError` gives,
because a wrong remedy sends the operator somewhere that cannot help them.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from bankmachine.connector import (
    AggregatorNotConfiguredError,
    AggregatorRequestError,
    AggregatorUnavailableError,
    ConnectionLockedError,
    ConnectorError,
    DataNotReadyError,
    Endpoint,
    InstitutionUnavailableError,
    RateLimitedError,
    ReauthRequiredError,
    UnrecognizedAggregatorError,
)
from bankmachine.logging_setup import get_logger
from bankmachine.store.types import UtcInstant

#: Aggregator error code -> the local type naming what the caller must do.
#:
#: Provenance is recorded per entry because these are not equally well
#: established: two were seen in real responses from the aggregator, and the
#: rest are spelled by the SDK in enums belonging to other products, which is
#: evidence the string is part of the aggregator's vocabulary but not proof of
#: which endpoint emits it.
CODE_TO_ERROR: Final[Mapping[str, type[ConnectorError]]] = {
    # -- Observed live against the sandbox host -------------------------------
    # `INVALID_API_KEYS` came back from a production secret sent to the sandbox
    # host; `INVALID_FIELD` from a malformed client id. The remedies genuinely
    # differ -- rotate a credential versus fix a call -- which is why the
    # aggregator separating them is worth preserving rather than collapsing.
    "INVALID_API_KEYS": AggregatorNotConfiguredError,
    "INVALID_FIELD": AggregatorRequestError,
    # -- Spelled by the SDK's own source --------------------------------------
    # `plaid/model/credit_bank_income_error_type.py` enumerates the item and
    # institution codes; `plaid/model/plaid_error_type.py` the rate limit.
    "ITEM_LOGIN_REQUIRED": ReauthRequiredError,
    "ITEM_LOCKED": ConnectionLockedError,
    "INSTITUTION_DOWN": InstitutionUnavailableError,
    "INSTITUTION_NOT_RESPONDING": InstitutionUnavailableError,
    "RATE_LIMIT_EXCEEDED": RateLimitedError,
    "INTERNAL_SERVER_ERROR": AggregatorUnavailableError,
    # -- Named by the requirement rather than by the SDK ----------------------
    # AC-2.6 requires the not-yet-ready backfill to back off rather than fail.
    # The SDK does not spell this code anywhere *(searched: no `PRODUCT_NOT_READY`
    # in the installed package)*, so unlike every entry above it rests on the
    # aggregator's documented behaviour alone. `tests/connector/test_sandbox.py`
    # is where that gets confirmed against a real backfill, and until a real one
    # has been seen this entry is the least-evidenced line in this file.
    "PRODUCT_NOT_READY": DataNotReadyError,
}

#: Aggregator error *type* -> local type. The coarse layer, and complete: every
#: value here is from the SDK's `PlaidErrorType` enum.
#:
#: Only the types whose whole membership shares one remedy appear. `ITEM_ERROR`
#: is the notable absence -- see the module docstring.
TYPE_TO_ERROR: Final[Mapping[str, type[ConnectorError]]] = {
    "INSTITUTION_ERROR": InstitutionUnavailableError,
    "RATE_LIMIT_EXCEEDED": RateLimitedError,
    "API_ERROR": AggregatorUnavailableError,
    "INVALID_REQUEST": AggregatorRequestError,
    "INVALID_INPUT": AggregatorRequestError,
}

#: Codes this build recognizes but has no FR-4 class for.
#:
#: FR-4 names four connection-health states and these are none of them: each
#: means "stop asking about this connection", which is a retirement decision the
#: enrollment layer owns rather than a health state the sync layer records. They
#: are listed so that an operator who hits one reads "no remedy is implemented
#: for this" instead of "never seen", which is the difference between a gap
#: someone chose and a gap nobody noticed.
KNOWN_UNCLASSIFIED_CODES: Final[frozenset[str]] = frozenset(
    {
        "INSTITUTION_NO_LONGER_SUPPORTED",
        "ITEM_NOT_SUPPORTED",
        "NO_ACCOUNTS",
        "ACCESS_NOT_GRANTED",
    }
)


@dataclass(frozen=True, slots=True)
class AggregatorErrorDetail:
    """What an error response said, as far as it could be read.

    Every field is optional: a failure that cannot be described must still be
    reported, so an unparseable body degrades to empty fields rather than
    raising and replacing one bad message with a worse one.
    """

    error_type: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    request_id: str | None = None


def parse_error_body(body: object) -> AggregatorErrorDetail:
    """Read an error response body, defensively.

    🔴 **`ApiException.reason` is the HTTP reason phrase and says nothing.**
    Wrong credentials, a malformed field and an unsupported country all arrive
    as `400: Bad Request`; the cause lives here, in the body *(verified by
    probing the real sandbox host with deliberately invalid credentials)*.

    The body arrives as `str` because the SDK decodes it before re-raising, but
    `bytes` is accepted too rather than trusting that to stay true.
    """
    if isinstance(body, bytes | bytearray):
        body = body.decode("utf-8", errors="replace")
    if not isinstance(body, str):
        return AggregatorErrorDetail()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return AggregatorErrorDetail()
    if not isinstance(payload, dict):
        return AggregatorErrorDetail()

    def _text(key: str) -> str | None:
        value = payload.get(key)
        return value if isinstance(value, str) and value else None

    return AggregatorErrorDetail(
        error_type=_text("error_type"),
        error_code=_text("error_code"),
        error_message=_text("error_message") or _text("display_message"),
        request_id=_text("request_id"),
    )


def classify(status: int | None, detail: AggregatorErrorDetail) -> type[ConnectorError]:
    """Which local type a refusal is, in the order the evidence is trustworthy.

    The code first because it is the aggregator's own most specific statement,
    then the error type, then the HTTP status. The status is last and not
    ignored: a 429 or a 503 carrying a code this build has never seen is still
    unambiguously a rate limit or an outage, and refusing to say so would throw
    away a signal that needs no vocabulary at all.

    Never returns `None` and never returns a bare `Exception`: an unclassifiable
    refusal is `UnrecognizedAggregatorError`, which is a type in the taxonomy
    rather than a hole in it. Silence is the one disallowed outcome.
    """
    if detail.error_code is not None:
        mapped = CODE_TO_ERROR.get(detail.error_code)
        if mapped is not None:
            return mapped
    if detail.error_type is not None:
        mapped = TYPE_TO_ERROR.get(detail.error_type)
        if mapped is not None:
            return mapped
    if status == 429:
        return RateLimitedError
    if status is not None and 500 <= status <= 599:
        return AggregatorUnavailableError
    return UnrecognizedAggregatorError


def describe(
    endpoint: Endpoint,
    status: int | None,
    reason: str | None,
    detail: AggregatorErrorDetail,
) -> str:
    """What actually went wrong, in one line an operator can act on.

    An operator reading `400: Bad Request` has no way to tell a rotated secret
    from a bug in this code, and the wrong guess costs them a credential
    rotation that was never the problem. The `request_id` is included because it
    is what makes a failure traceable in the aggregator's own dashboard.
    """
    described = f"{endpoint} failed with status {status}"
    if detail.error_code:
        described += f" ({detail.error_code})"
    described += f": {detail.error_message or reason}"
    if detail.error_code in KNOWN_UNCLASSIFIED_CODES:
        described += (
            " -- this build recognizes that code but implements no remedy for it; "
            "it is not one of the four connection-health states FR-4 defines"
        )
    if detail.request_id:
        described += f" [request_id {detail.request_id}]"
    return described


def parse_retry_after(headers: object) -> float | None:
    """The aggregator's own instruction about how long to wait, when it gives one.

    Only the delay-seconds form is read. HTTP allows an HTTP-date here too, and
    parsing that would need the current time -- which would make the wait depend
    on this machine's clock agreeing with the aggregator's, and a skewed clock
    turning a two-second wait into an hour is a worse failure than ignoring the
    header. An absent or unreadable value falls back to the computed backoff,
    which is always a valid answer.
    """
    if not isinstance(headers, Mapping):
        return None
    for key, value in headers.items():
        if not isinstance(key, str) or key.lower() != "retry-after":
            continue
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(seconds) or seconds < 0:
            # `float()` accepts "inf", "Infinity" and "1e400". A header is
            # external input, and `time.sleep(inf)` does not wait forever -- it
            # raises `OverflowError`, untyped, past this boundary and into a CLI
            # handler that catches only `ConnectorError` and its kin. An
            # unreadable instruction falls back to the computed backoff, which
            # is always a valid answer.
            return None
        return seconds
    return None


#: How many times one call is attempted in total, first try included.
#:
#: Four, because the transient cases this retries are a rate limit and a backfill
#: that is not ready, and both resolve in seconds-to-minutes or not at all. With
#: the delays below that is a bounded ~7 seconds of waiting before a connection
#: is reported degraded -- which AC-4.4 would rather have than a sync that hangs
#: on optimism. `max_delay_seconds` is what makes "bounded" true of *every* wait,
#: a `Retry-After` the aggregator asked for included, so the worst case is
#: `(attempts - 1) * max_delay_seconds` rather than whatever a header said.
DEFAULT_RETRY_ATTEMPTS: Final = 4

#: The first wait, doubling each attempt up to the cap.
DEFAULT_BACKOFF_SECONDS: Final = 1.0
DEFAULT_BACKOFF_MULTIPLIER: Final = 2.0
DEFAULT_MAX_BACKOFF_SECONDS: Final = 60.0


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How the one retryable channel waits.

    `architecture.md` permits backoff on exactly this boundary -- the aggregator
    HTTPS one -- because it is the only channel that can fail transiently. The
    datastore channel does not retry, and this type is deliberately not general
    enough to be reached for there.
    """

    attempts: int = DEFAULT_RETRY_ATTEMPTS
    base_delay_seconds: float = DEFAULT_BACKOFF_SECONDS
    multiplier: float = DEFAULT_BACKOFF_MULTIPLIER
    max_delay_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError(f"attempts must be at least 1, got {self.attempts}")

    def delay_before(self, attempt: int) -> float:
        """Seconds to wait before attempt number `attempt`, counting from 1.

        There is no wait before the first attempt, so `delay_before(1)` is zero
        rather than the base delay -- a policy whose first call sleeps would put
        a fixed tax on every successful sync to insure against the rare one.
        """
        if attempt <= 1:
            return 0.0
        return min(
            self.base_delay_seconds * (self.multiplier ** (attempt - 2)),
            self.max_delay_seconds,
        )


_log = get_logger(__name__)

#: What the retry loop waits with, injected so tests exercise the real decisions
#: at full speed instead of a mock of them.
Sleeper = Callable[[float], None]


def call_with_retry[T](
    call: Callable[[], T],
    *,
    policy: RetryPolicy,
    sleep: Sleeper,
    now: Callable[[], UtcInstant],
) -> T:
    """Run `call`, retrying the failures whose own type says they are worth retrying.

    🔴 **The retryable set is a property of each error type, not a list kept
    here.** A list in this function is an enumeration that goes stale the moment
    someone adds an error type and does not think to visit this file -- and it
    goes stale silently, in whichever direction the omission happens to fall.
    Asking the exception makes the decision travel with the thing it is about.

    **This wraps the HTTP call and nothing else, which is what makes it safe.**
    A retry that spans a write can interleave a second attempt's data with the
    first's partial effects. Nothing inside this boundary writes: the client
    returns bytes and the caller archives once, after this returns. **That is an
    argument about the local side only** -- an endpoint the far end cannot absorb
    twice is excluded by `Endpoint.retry_safe`, not by this reasoning.

    `now` is passed rather than read so the failure that finally escapes carries
    a `failed_at` from the same clock the caller stamps everything else with.
    """
    last: ConnectorError | None = None
    pending_delay = 0.0
    for attempt in range(1, policy.attempts + 1):
        if pending_delay > 0:
            sleep(pending_delay)
        try:
            return call()
        except ConnectorError as exc:
            if not type(exc).retryable:
                # 🔴 The terminal failure is the one worth recording, and it was
                # the one going unrecorded: the retryable path logged and the
                # path that actually ends a sync re-raised in silence. A caller
                # may well log it too, but "someone upstream will" is how a
                # nightly job comes to have no trace of why it stopped.
                _log.error(
                    "%s on %s for connection %s: not retryable (%s)",
                    type(exc).__name__,
                    exc.endpoint or "the aggregator",
                    exc.connection_id if exc.connection_id is not None else "-",
                    exc.error_code or "no code",
                )
                raise
            last = exc
            # Logged at every retry, because a call that succeeds on attempt 3
            # otherwise leaves no trace at all, and an operator asking why last
            # night's unattended sync took minutes has nothing to read. In the
            # one subsystem whose named primary failure mode is silent
            # staleness, a wait nobody can see is the failure in miniature.
            _log.warning(
                "%s on %s for connection %s: retrying (attempt %d of %d)",
                type(exc).__name__,
                exc.endpoint or "the aggregator",
                exc.connection_id if exc.connection_id is not None else "-",
                attempt,
                policy.attempts,
            )
            # The aggregator's own instruction is a floor, not a replacement: a
            # service that says "wait 30s" and is asked again at 2s has been
            # given a reason to keep saying no. Computed after the failure, so
            # the last attempt sets a delay nothing waits out.
            # 🔴 Clamped, because `max_delay_seconds` is the field whose whole
            # job is bounding the wait. A `Retry-After: 3600` that bypassed it
            # would spend three silent hours inside one nightly sync -- turning a
            # connection this run could have reported as degraded into one that
            # simply never reports, which is the exact failure AC-4.4 calls this
            # system's primary mode. The aggregator gets to ask for longer; it
            # does not get to decide how long this product hangs.
            pending_delay = min(
                max(policy.delay_before(attempt + 1), exc.retry_after_seconds or 0.0),
                policy.max_delay_seconds,
            )
    assert last is not None  # the loop runs at least once and only exits here on failure
    # The last failure is re-raised rather than rebuilt. A rebuild has to copy
    # every field by hand, which is a list that goes stale the moment a field is
    # added -- and it would drop the cause, burying the aggregator's own
    # exception a level further down than the operator has to dig. `failed_at`
    # is restamped because the interesting moment is when this gave up, not when
    # it first tried.
    last.args = (f"{last} -- gave up after {policy.attempts} attempts",)
    last.failed_at = now()
    _log.error(
        "%s on %s for connection %s: giving up after %d attempts",
        type(last).__name__,
        last.endpoint or "the aggregator",
        last.connection_id if last.connection_id is not None else "-",
        policy.attempts,
    )
    raise last
