"""The error taxonomy and the one retry channel (FR-4, AC-2.6).

The bodies below are shaped from real aggregator responses -- see
`.prawduct/artifacts/api-notes-plaid.md` -- rather than from the SDK's models,
because the two disagree in exactly the places that matter and a fixture written
from a model definition inherits whatever that model got wrong.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from bankmachine.connector import (
    INSTITUTIONS_GET,
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


def _body(**fields: object) -> str:
    """An error body in the shape the aggregator actually sends."""
    payload: dict[str, object] = {
        "display_message": None,
        "documentation_url": "https://plaid.com/docs/?ref=error",
        "error_code": None,
        "error_message": None,
        "error_type": None,
        "request_id": "476e317baa236b1",
        "suggested_action": None,
    }
    payload.update(fields)
    return json.dumps(payload)


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
    code: str, expected: type[ConnectorError]
) -> None:
    detail = parse_error_body(_body(error_code=code, error_type="ITEM_ERROR"))
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
    error_type: str, expected: type[ConnectorError]
) -> None:
    """The coarse layer, exercised where the precise one cannot reach.

    Tested separately from the code layer on purpose: both layers produce the
    same answer for a code that is in the table, so a behavioural test cannot
    tell which one did the work, and losing the second layer would show up only
    when the aggregator ships a code nobody has seen -- which is the exact
    moment there is no one watching.
    """
    detail = parse_error_body(
        _body(error_code="A_CODE_INVENTED_FOR_THIS_TEST", error_type=error_type)
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


def test_an_unrecognized_refusal_raises_something_specific_rather_than_being_swallowed() -> None:
    """Silence is the one disallowed outcome.

    Not folded into a neighbouring type: filing an unknown code under "probably
    transient" hides a connection that will never recover, and under "probably
    terminal" retires one that only needed a retry.
    """
    detail = parse_error_body(_body(error_code="SOMETHING_NEW", error_type="A_NEW_TYPE"))
    assert classify(400, detail) is UnrecognizedAggregatorError
    assert issubclass(UnrecognizedAggregatorError, ConnectorError)
    assert UnrecognizedAggregatorError is not ConnectorError


@pytest.mark.parametrize("code", sorted(KNOWN_UNCLASSIFIED_CODES))
def test_a_known_code_with_no_remedy_says_so_rather_than_claiming_it_is_new(code: str) -> None:
    """The difference between a gap someone chose and a gap nobody noticed."""
    detail = parse_error_body(_body(error_code=code, error_type="ITEM_ERROR"))
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


def _leaf_error_types() -> list[type[ConnectorError]]:
    """Every error type nothing else inherits from -- the ones actually raised."""

    def walk(cls: type[ConnectorError]) -> list[type[ConnectorError]]:
        subclasses = cls.__subclasses__()
        if not subclasses:
            return [cls]
        return [leaf for sub in subclasses for leaf in walk(sub)]

    return walk(ConnectorError)


def test_every_error_type_decides_whether_it_retries() -> None:
    """The mechanism behind `retryable` having no default on the base class.

    A default is what lets a type added next year inherit an answer nobody
    decided, and the two directions fail differently: an un-retried transient
    stops the nightly sync, a retried permanent one hammers the aggregator with
    a call that cannot work. Requiring the attribute in the class's *own*
    `__dict__` is what makes inheriting the answer insufficient.
    """
    leaves = _leaf_error_types()
    assert leaves, "the walk found no error types, so it is proving nothing"
    undecided = [cls.__name__ for cls in leaves if "retryable" not in cls.__dict__]
    assert not undecided, (
        f"{undecided} inherit `retryable` instead of declaring it. Decide it on the class: "
        f"the retry loop asks the exception, and a wrong inherited answer is silent"
    )


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
    assert isinstance(caught.value.__cause__, RateLimitedError)


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


def test_the_description_names_the_cause_and_not_the_reason_phrase() -> None:
    detail = parse_error_body(
        _body(
            error_code="INVALID_FIELD",
            error_type="INVALID_REQUEST",
            error_message="client_id must be a properly formatted, non-empty string",
        )
    )
    described = describe(INSTITUTIONS_GET, 400, "Bad Request", detail)
    assert "INVALID_FIELD" in described
    assert "client_id must be a properly formatted" in described
    assert "476e317baa236b1" in described
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
