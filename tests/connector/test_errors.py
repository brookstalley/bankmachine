"""The error taxonomy and the one retry channel (FR-4, AC-2.6).

The bodies below are shaped from real aggregator responses -- see
`.prawduct/artifacts/api-notes-plaid.md` -- rather than from the SDK's models,
because the two disagree in exactly the places that matter and a fixture written
from a model definition inherits whatever that model got wrong.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from bankmachine.connector import (
    INSTITUTIONS_GET,
    AggregatorError,
    AggregatorNotConfiguredError,
    AggregatorRequestError,
    AggregatorUnavailableError,
    ConnectionLockedError,
    ConnectorError,
    DataNotReadyError,
    InstitutionUnavailableError,
    RateLimitedError,
    ReauthRequiredError,
    TransportError,
    UnrecognizedAggregatorError,
)
from bankmachine.connector.plaid.errors import (
    CODE_TO_ERROR,
    KNOWN_UNCLASSIFIED_CODES,
    TYPE_TO_ERROR,
    AggregatorErrorDetail,
    RetryPolicy,
    call_with_retry,
    classify,
    describe,
    parse_error_body,
    parse_retry_after,
)
from bankmachine.store.types import now_utc

# --------------------------------------------------------------------------
# Layer 1: the error code
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("ITEM_LOGIN_REQUIRED", ReauthRequiredError),
        ("ITEM_LOCKED", ConnectionLockedError),
        ("INSTITUTION_DOWN", InstitutionUnavailableError),
        ("INSTITUTION_NOT_RESPONDING", InstitutionUnavailableError),
        ("RATE_LIMIT_EXCEEDED", RateLimitedError),
        ("PRODUCT_NOT_READY", DataNotReadyError),
        ("INVALID_API_KEYS", AggregatorNotConfiguredError),
        ("INVALID_FIELD", AggregatorRequestError),
        ("INTERNAL_SERVER_ERROR", AggregatorUnavailableError),
    ],
)
def test_each_code_maps_to_the_type_that_names_its_remedy(
    code: str, expected: type[ConnectorError], error_body: Callable[..., str]
) -> None:
    detail = parse_error_body(error_body(error_code=code, error_type="ITEM_ERROR"))
    assert classify(400, detail) is expected


def test_the_four_connection_health_states_are_four_distinct_types() -> None:
    """AC-4.1 names four, and a taxonomy that collapses any two of them lies.

    Auth-required and locked in particular: they are both `ITEM_ERROR` to the
    aggregator, and their remedies are in different buildings -- one is this
    product's re-link flow, the other is the operator's own bank.
    """
    states = {
        "auth-required": classify(400, AggregatorErrorDetail(error_code="ITEM_LOGIN_REQUIRED")),
        "locked": classify(400, AggregatorErrorDetail(error_code="ITEM_LOCKED")),
        "institution-down": classify(400, AggregatorErrorDetail(error_code="INSTITUTION_DOWN")),
        "rate-limit": classify(429, AggregatorErrorDetail(error_code="RATE_LIMIT_EXCEEDED")),
    }
    assert len(set(states.values())) == 4, f"two states share a type: {states}"


# --------------------------------------------------------------------------
# Layer 2: the error type
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error_type", "expected"),
    [
        ("INSTITUTION_ERROR", InstitutionUnavailableError),
        ("RATE_LIMIT_EXCEEDED", RateLimitedError),
        ("API_ERROR", AggregatorUnavailableError),
        ("INVALID_REQUEST", AggregatorRequestError),
        ("INVALID_INPUT", AggregatorRequestError),
    ],
)
def test_an_unknown_code_still_classifies_by_its_error_type(
    error_type: str, expected: type[ConnectorError], error_body: Callable[..., str]
) -> None:
    """The coarse layer, exercised where the precise one cannot reach.

    Tested separately from the code layer on purpose: both layers produce the
    same answer for a code that is in the table, so a behavioural test cannot
    tell which one did the work, and losing the second layer would show up only
    when the aggregator ships a code nobody has seen -- which is the exact
    moment there is no one watching.
    """
    detail = parse_error_body(
        error_body(error_code="A_CODE_INVENTED_FOR_THIS_TEST", error_type=error_type)
    )
    assert classify(400, detail) is expected


def test_item_error_is_not_classified_by_its_type() -> None:
    """The deliberate hole in layer 2, asserted so nobody helpfully fills it.

    `ITEM_ERROR` spans re-link, go-to-your-bank and nothing-to-sync. A mapping
    for it would be confidently wrong most of the time, and a wrong remedy sends
    the operator somewhere that cannot help.
    """
    assert "ITEM_ERROR" not in TYPE_TO_ERROR
    detail = AggregatorErrorDetail(error_code="SOME_NEW_ITEM_CODE", error_type="ITEM_ERROR")
    assert classify(400, detail) is UnrecognizedAggregatorError


# --------------------------------------------------------------------------
# Layer 3: the status, and the refusal to guess
# --------------------------------------------------------------------------


def test_a_rate_limit_with_no_vocabulary_at_all_is_still_a_rate_limit() -> None:
    """429 needs no code and no type to mean what it means."""
    assert classify(429, AggregatorErrorDetail()) is RateLimitedError


@pytest.mark.parametrize("status", [500, 502, 503, 599])
def test_a_server_error_with_no_vocabulary_is_the_aggregators_own(status: int) -> None:
    assert classify(status, AggregatorErrorDetail()) is AggregatorUnavailableError


def test_an_unrecognized_refusal_raises_something_specific_rather_than_being_swallowed(
    error_body: Callable[..., str],
) -> None:
    """Silence is the one disallowed outcome.

    Not folded into a neighbouring type: filing an unknown code under "probably
    transient" hides a connection that will never recover, and under "probably
    terminal" retires one that only needed a retry.
    """
    detail = parse_error_body(error_body(error_code="SOMETHING_NEW", error_type="A_NEW_TYPE"))
    assert classify(400, detail) is UnrecognizedAggregatorError
    assert issubclass(UnrecognizedAggregatorError, ConnectorError)
    assert UnrecognizedAggregatorError is not ConnectorError


@pytest.mark.parametrize("code", sorted(KNOWN_UNCLASSIFIED_CODES))
def test_a_known_code_with_no_remedy_says_so_rather_than_claiming_it_is_new(
    code: str, error_body: Callable[..., str]
) -> None:
    """The difference between a gap someone chose and a gap nobody noticed."""
    detail = parse_error_body(error_body(error_code=code, error_type="ITEM_ERROR"))
    assert classify(400, detail) is UnrecognizedAggregatorError
    message = describe(INSTITUTIONS_GET, 400, "Bad Request", detail)
    assert "implements no remedy" in message
    assert code in message


@given(
    status=st.one_of(st.none(), st.integers(min_value=0, max_value=999)),
    detail=st.builds(
        AggregatorErrorDetail,
        error_type=st.one_of(st.none(), st.text(max_size=40)),
        error_code=st.one_of(st.none(), st.text(max_size=40)),
        error_message=st.one_of(st.none(), st.text(max_size=40)),
        request_id=st.one_of(st.none(), st.text(max_size=40)),
    ),
)
def test_classification_never_returns_nothing_and_never_returns_a_bare_exception(
    status: int | None, detail: AggregatorErrorDetail
) -> None:
    """No input produces silence.

    The property, rather than a list of cases, because the inputs that would
    break it are the ones nobody thought to write down: an empty code, a code
    that is whitespace, a status outside every band.
    """
    classified = classify(status, detail)
    assert classified is not None
    assert classified is not Exception
    assert classified is not ConnectorError, (
        "a bare base class is a classification refusing to be one"
    )
    assert issubclass(classified, ConnectorError)


@given(st.binary(max_size=200) | st.text(max_size=200) | st.none() | st.integers())
def test_any_body_at_all_parses_into_a_detail_rather_than_raising(body: object) -> None:
    """A failure that cannot be described must still be reported."""
    detail = parse_error_body(body)
    assert isinstance(detail, AggregatorErrorDetail)


# --------------------------------------------------------------------------
# The retryable decision lives on the type
# --------------------------------------------------------------------------


def test_a_type_that_never_decided_whether_it_retries_cannot_be_defined() -> None:
    """The mechanism, checked where it actually fires: class creation.

    A test that walked `ConnectorError.__subclasses__()` would only ever see
    subclasses whose module had been imported -- so it would guarantee something
    about the types this file happens to import, not about every error type. The
    case it misses is the one the connector's own docstring anticipates: a second
    aggregator arriving as a new module, whose new error type would leave the
    walk green and raise `AttributeError` from inside the retry loop, one frame
    from the `raise`, on the error path of the error path.
    """
    with pytest.raises(TypeError, match="must declare `retryable`"):

        class ForgotToDecideError(ConnectorError):
            """A type that never said whether trying again could help."""

    # Positive control: declaring it is all that was missing, so the refusal is
    # about the decision rather than about subclassing being blocked outright.
    class DecidedError(ConnectorError):
        retryable = False

    assert DecidedError("x").retryable is False


def test_a_grouping_class_cannot_be_raised_because_it_cannot_be_built() -> None:
    """Grouping classes exist to be caught. Raising one is a mistake that reports itself.

    Structural rather than conventional: they declare no `retryable`, so without
    this the mistake would surface inside the retry loop as an `AttributeError`
    about a class attribute -- a confusing report of a simple error, on a path
    that may not run for months.
    """
    for grouping in (ConnectorError, AggregatorError):
        with pytest.raises(TypeError, match="not raisable"):
            grouping("this should never be constructible")

    # Positive control: a leaf of each builds fine, so the refusal is about the
    # grouping classes rather than about the hierarchy being broken.
    assert TransportError("offline").retryable is True
    assert ReauthRequiredError("re-link it").retryable is False


def test_the_retry_loop_asks_the_type_rather_than_keeping_its_own_list() -> None:
    """A list in the loop is an enumeration that goes stale silently.

    Proved by a type the loop has never heard of: it retries because it says it
    does, not because it appears anywhere in `call_with_retry`.
    """

    class InventedTransientError(ConnectorError):
        retryable = True

    attempts = 0

    def call() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise InventedTransientError("not yet")
        return "answered"

    assert _run(call) == "answered"
    assert attempts == 3


# --------------------------------------------------------------------------
# Backoff and retry
# --------------------------------------------------------------------------


class FakeClock:
    """A sleeper that records instead of waiting.

    The schedule is what has a bug in it, so the suite exercises the real
    schedule at full speed rather than shortening it to one attempt and testing
    nothing.
    """

    def __init__(self) -> None:
        self.slept: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


def _run(
    call: Any,
    *,
    policy: RetryPolicy | None = None,
    clock: FakeClock | None = None,
    now: Any = now_utc,
) -> Any:
    return call_with_retry(
        call,
        policy=RetryPolicy() if policy is None else policy,
        sleep=FakeClock() if clock is None else clock,
        now=now,
    )


@pytest.mark.parametrize(
    ("error", "code"),
    [(RateLimitedError, "RATE_LIMIT_EXCEEDED"), (DataNotReadyError, "PRODUCT_NOT_READY")],
)
def test_a_transient_refusal_succeeds_after_backoff(error: type[ConnectorError], code: str) -> None:
    """AC-2.6 and AC-4.1's rate limit: backoff and retry, not failure."""
    clock = FakeClock()
    attempts = 0

    def call() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise error("try again", error_code=code)
        return "answered"

    assert _run(call, clock=clock) == "answered"
    assert attempts == 2
    assert clock.slept == [1.0], "the first retry did not wait the base delay"


def test_nothing_sleeps_before_the_first_attempt() -> None:
    """A policy whose first call waits taxes every successful sync forever."""
    clock = FakeClock()
    assert _run(lambda: "answered", clock=clock) == "answered"
    assert clock.slept == []


def test_the_backoff_doubles_and_is_capped() -> None:
    policy = RetryPolicy(attempts=6, base_delay_seconds=1.0, multiplier=2.0, max_delay_seconds=5.0)
    assert [policy.delay_before(n) for n in range(1, 7)] == [0.0, 1.0, 2.0, 4.0, 5.0, 5.0]


def test_giving_up_is_loud_and_says_how_many_times_it_tried() -> None:
    """A retry that exhausts itself quietly is a silent failure with extra steps."""
    clock = FakeClock()
    policy = RetryPolicy(attempts=3)

    def call() -> str:
        raise RateLimitedError(
            "slow down",
            endpoint=INSTITUTIONS_GET,
            connection_id=7,
            error_code="RATE_LIMIT_EXCEEDED",
            request_id="abc123",
        )

    with pytest.raises(RateLimitedError) as caught:
        _run(call, policy=policy, clock=clock)

    assert "gave up after 3 attempts" in str(caught.value)
    assert clock.slept == [1.0, 2.0]
    # The diagnosis survives the give-up: AC-4.2 wants the code recorded against
    # the connection, and a give-up that dropped it would degrade a connection
    # with no note of why.
    assert caught.value.error_code == "RATE_LIMIT_EXCEEDED"
    assert caught.value.connection_id == 7
    assert caught.value.endpoint is INSTITUTIONS_GET
    assert caught.value.request_id == "abc123"


def test_giving_up_keeps_every_field_the_failure_arrived_with() -> None:
    """The give-up path re-raises rather than rebuilds.

    A rebuild has to copy each field by hand, and the first version of this one
    copied five and forgot `retry_after_seconds` -- the field that says how long
    the aggregator asked us to wait, dropped at exactly the moment a caller is
    deciding what to do about a connection it has just given up on.
    """
    original = RateLimitedError(
        "slow down",
        endpoint=INSTITUTIONS_GET,
        connection_id=9,
        error_code="RATE_LIMIT_EXCEEDED",
        request_id="req-1",
        retry_after_seconds=15.0,
    )

    def call() -> str:
        raise original

    with pytest.raises(RateLimitedError) as caught:
        _run(call, policy=RetryPolicy(attempts=2))

    for field in ("endpoint", "connection_id", "error_code", "request_id", "retry_after_seconds"):
        assert getattr(caught.value, field) == getattr(original, field), (
            f"the give-up path dropped {field}"
        )
    assert caught.value.failed_at is not None, "giving up is itself a moment worth stamping"


@pytest.mark.parametrize(
    "error",
    [
        ReauthRequiredError,
        ConnectionLockedError,
        InstitutionUnavailableError,
        UnrecognizedAggregatorError,
    ],
)
def test_a_terminal_refusal_is_raised_at_once_rather_than_retried(
    error: type[ConnectorError],
) -> None:
    """Retrying these delays the report that tells the operator to act.

    AC-4.4 names silent staleness as this system's primary failure mode, and a
    connection sitting in a backoff loop it can never escape is exactly that.
    """
    clock = FakeClock()
    attempts = 0

    def call() -> str:
        nonlocal attempts
        attempts += 1
        raise error("done for")

    with pytest.raises(error):
        _run(call, clock=clock)

    assert attempts == 1
    assert clock.slept == []


def test_the_aggregators_own_retry_after_is_a_floor_on_the_wait() -> None:
    """Asked again sooner than a service said to, it has a reason to keep saying no."""
    clock = FakeClock()
    attempts = 0

    def call() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RateLimitedError("slow down", retry_after_seconds=30.0)
        return "answered"

    assert _run(call, clock=clock) == "answered"
    assert clock.slept == [30.0], "the computed backoff overrode the aggregator's instruction"


def test_the_aggregator_cannot_ask_for_a_longer_wait_than_the_policy_allows() -> None:
    """🔴 `max_delay_seconds` is the field whose whole job is bounding the wait.

    A `Retry-After` honoured without a ceiling bypasses it: `Retry-After: 3600`
    on a four-attempt policy spends three silent hours inside one nightly sync,
    turning a connection this run could have reported as degraded into one that
    simply never reports -- which is the exact failure AC-4.4 calls this
    system's primary mode. The aggregator gets to ask for longer; it does not
    get to decide how long this product hangs.
    """
    clock = FakeClock()
    policy = RetryPolicy(attempts=3, max_delay_seconds=5.0)

    def call() -> str:
        raise RateLimitedError("slow down", retry_after_seconds=3600.0)

    with pytest.raises(RateLimitedError):
        _run(call, policy=policy, clock=clock)

    assert clock.slept == [5.0, 5.0]
    assert sum(clock.slept) <= policy.max_delay_seconds * (policy.attempts - 1)


@pytest.mark.parametrize("value", ["inf", "Infinity", "1e400", "-inf", "nan"])
def test_a_non_finite_retry_after_never_reaches_the_clock(value: str) -> None:
    """`float()` accepts these, and `time.sleep(inf)` raises `OverflowError`.

    Untyped, past this boundary, and into a CLI handler that catches only
    `ConnectorError` and its kin -- so an aggregator header would become a
    traceback. A header is external input and is treated as such.
    """
    assert parse_retry_after({"Retry-After": value}) is None


def test_a_retry_and_a_give_up_both_leave_a_trace(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Silence is the one disallowed outcome, and a wait nobody can see is silence.

    A call that succeeds on attempt 3 would otherwise leave nothing behind, and
    an operator asking why last night's unattended sync took minutes has only
    the logs to read.
    """
    attempts = 0

    def call() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RateLimitedError("slow down", endpoint=INSTITUTIONS_GET)
        return "answered"

    with caplog.at_level(logging.WARNING, logger="bankmachine"):
        assert _run(call) == "answered"
    retried = [r for r in caplog.records if "retrying" in r.getMessage()]
    assert len(retried) == 2, "a silent retry is a wait no operator can account for"
    assert "/institutions/get" in retried[0].getMessage()

    assert str(INSTITUTIONS_GET) in retried[0].getMessage()

    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="bankmachine"), pytest.raises(RateLimitedError):
        _run(lambda: (_ for _ in ()).throw(RateLimitedError("no")), policy=RetryPolicy(attempts=2))
    assert any("giving up" in r.getMessage() for r in caplog.records)


def test_the_failure_that_ends_a_sync_is_the_one_that_gets_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """🔴 The terminal path was the silent one, which is the failure in miniature.

    The retryable path logged and the path that actually ends an unattended sync
    re-raised without a word. `observability-strategy.md` asks for every error
    with its code in the per-run log, and `connection_id` on every record that
    touches a connection -- so the record IS the deliverable here rather than
    decoration around one, and it needs something that notices it going away.
    """

    def call() -> str:
        raise ReauthRequiredError(
            "re-link it",
            endpoint=INSTITUTIONS_GET,
            connection_id=12,
            error_code="ITEM_LOGIN_REQUIRED",
        )

    with caplog.at_level(logging.ERROR, logger="bankmachine"), pytest.raises(ReauthRequiredError):
        _run(call, policy=RetryPolicy(attempts=4))

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1, "a terminal failure left no record, or left more than one"
    message = errors[0].getMessage()
    assert "ITEM_LOGIN_REQUIRED" in message, "the code AC-4.2 records is not in the log"
    assert "12" in message, "the record does not say which connection is broken"
    assert "not retryable" in message


def test_every_retry_record_names_its_connection(caplog: pytest.LogCaptureFixture) -> None:
    """One broken connection must be attributable in the log, not just in the exception.

    An operator reading a night's log needs to tell four retries on one
    connection from one retry on each of four.
    """
    attempts = 0

    def call() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            raise RateLimitedError("slow down", endpoint=INSTITUTIONS_GET, connection_id=5)
        return "answered"

    with caplog.at_level(logging.WARNING, logger="bankmachine"):
        assert _run(call) == "answered"

    assert [r for r in caplog.records if "5" in r.getMessage()], (
        "no record names the connection it belongs to"
    )


def test_a_policy_that_could_never_call_anything_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        RetryPolicy(attempts=0)


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Retry-After": "12"}, 12.0),
        ({"retry-after": "0.5"}, 0.5),
        ({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, None),
        ({"Retry-After": "-3"}, None),
        ({"Content-Type": "application/json"}, None),
        (None, None),
    ],
)
def test_retry_after_is_read_only_where_it_needs_no_clock(
    headers: object, expected: float | None
) -> None:
    """The HTTP-date form is deliberately ignored.

    Parsing it needs the current time, which makes the wait depend on this
    machine's clock agreeing with the aggregator's -- and a skewed clock turning
    a two-second wait into an hour is worse than falling back on a schedule that
    is always valid.
    """
    assert parse_retry_after(headers) == expected


# --------------------------------------------------------------------------
# What a failure has to carry
# --------------------------------------------------------------------------


def test_a_failure_carries_what_ac_4_5_needs_to_compute_the_hole() -> None:
    """A degraded record whose data hole cannot be computed is insufficient.

    The connector cannot compute it -- `last_success_at` is in the datastore --
    so what it owes is the other end of the subtraction, plus the code AC-4.2
    asks for.
    """
    stamped = now_utc()
    error = ReauthRequiredError(
        "re-link it",
        endpoint=INSTITUTIONS_GET,
        connection_id=3,
        error_code="ITEM_LOGIN_REQUIRED",
        failed_at=stamped,
    )
    assert error.failed_at == stamped
    # `UtcInstant` is a NewType over `datetime`, so mypy owns the distinction and
    # a runtime `isinstance` against it is not a thing that exists. What is worth
    # asserting here is the property the subtraction needs: an aware instant, in
    # UTC, comparable to the `last_success_at` the datastore holds.
    assert error.failed_at is not None
    assert error.failed_at.tzinfo is not None
    assert error.failed_at.utcoffset() == timedelta(0)
    assert error.error_code == "ITEM_LOGIN_REQUIRED"
    assert error.connection_id == 3


def test_a_failure_before_enrollment_carries_no_connection_rather_than_a_wrong_one() -> None:
    """`None` is the honest answer for a call made on nobody's behalf."""
    error = TransportError("offline", endpoint=INSTITUTIONS_GET)
    assert error.connection_id is None


def test_the_description_names_the_cause_and_not_the_reason_phrase(
    error_body: Callable[..., str],
) -> None:
    detail = parse_error_body(
        error_body(
            error_code="INVALID_FIELD",
            error_type="INVALID_REQUEST",
            error_message="client_id must be a properly formatted, non-empty string",
        )
    )
    described = describe(INSTITUTIONS_GET, 400, "Bad Request", detail)
    assert "INVALID_FIELD" in described
    assert "client_id must be a properly formatted" in described
    # Read from the same body rather than copied from it: a literal here is a
    # value that goes stale the next time the fixture is re-recorded, and it
    # would fail for a reason that says nothing about the code under test.
    assert detail.request_id is not None
    assert detail.request_id in described
    assert "Bad Request" not in described


def test_a_description_that_cannot_be_built_still_reports_the_call() -> None:
    for body in (None, b"\x00 not json", "not json either", json.dumps(["a", "list"])):
        described = describe(INSTITUTIONS_GET, 500, "Server Error", parse_error_body(body))
        assert "/institutions/get" in described
        assert "500" in described


def test_the_code_table_and_the_taxonomy_do_not_drift_apart() -> None:
    """Every mapped type is one a caller can actually catch by name."""
    for mapped in {*CODE_TO_ERROR.values(), *TYPE_TO_ERROR.values()}:
        assert issubclass(mapped, ConnectorError)
        assert "retryable" in mapped.__dict__, (
            f"{mapped.__name__} is reachable from the code table but never decided whether "
            f"it retries"
        )
