"""The seam between a response and the rows derived from it.

Two properties are tested here rather than in the rebuild's own file, because
they are properties of the *live* path -- the one the aggregator client will use
at build step 2 -- and they are what makes the rebuild's job well-defined:
a response is persisted and committed before anything derives from it, and a
deriver that fails takes its own rows down without taking the archive with them.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Connection as SAConnection
from sqlalchemy import func, insert, select

from bankmachine.config import Config
from bankmachine.logging_setup import redact
from bankmachine.store.derivation import (
    DERIVATION_VERSION,
    DerivationContext,
    DerivationError,
    UnknownEndpointError,
    apply_response,
    ensure_derivation_version,
)
from bankmachine.store.engine import transaction, writer_engine
from bankmachine.store.raw import RawResponse
from bankmachine.store.schema import derivation_versions, institutions, raw_responses
from bankmachine.store.types import UtcInstant, utc_instant

ENDPOINT = "/test/institutions"
BODY = b'{"institutions": [{"id": "src-inst-1", "name": "An Institution"}]}'
RECEIVED = utc_instant(datetime(2026, 9, 6, 12, 0, tzinfo=UTC))


@pytest.fixture
def writer(initialized_config: Config) -> Iterator[SAConnection]:
    with writer_engine(initialized_config) as engine, engine.connect() as conn:
        yield conn


def _record_one_institution(
    conn: SAConnection, response: RawResponse, context: DerivationContext
) -> None:
    conn.execute(
        insert(institutions).values(
            source_institution_id="src-inst-1",
            name="An Institution",
            first_seen_at=response.received_at,
            last_seen_at=response.received_at,
        )
    )


class DeriverFailedError(Exception):
    """Raised by a deriver in a test, to stand for any failure partway through one."""


def _record_then_fail(
    conn: SAConnection, response: RawResponse, context: DerivationContext
) -> None:
    _record_one_institution(conn, response, context)
    raise DeriverFailedError("halfway through")


def _count(conn: SAConnection, table: object) -> int:
    return int(conn.execute(select(func.count()).select_from(table)).scalar_one())  # type: ignore[arg-type]


def test_a_response_is_committed_before_anything_derives_from_it(writer: SAConnection) -> None:
    seen: list[int] = []

    def _observe(conn: SAConnection, response: RawResponse, context: DerivationContext) -> None:
        # A separate statement, outside the derivation's own inserts: what it
        # sees is what a crash would leave behind.
        seen.append(_count(conn, raw_responses))

    apply_response(
        writer,
        connection_id=None,
        endpoint=ENDPOINT,
        body=BODY,
        received_at=RECEIVED,
        derivers={ENDPOINT: _observe},
    )

    assert seen == [1]


def test_a_deriver_that_fails_keeps_the_archive_and_leaves_no_half_derived_rows(
    writer: SAConnection,
) -> None:
    # The asymmetry is the point: a response may be unfetchable afterwards --
    # an aggregator's history window does not come back -- while a derivation
    # can be re-run against the archive at any time by `store rebuild`.
    with pytest.raises(DeriverFailedError):
        apply_response(
            writer,
            connection_id=None,
            endpoint=ENDPOINT,
            body=BODY,
            received_at=RECEIVED,
            derivers={ENDPOINT: _record_then_fail},
        )

    assert _count(writer, raw_responses) == 1
    assert _count(writer, institutions) == 0


def test_a_response_no_deriver_understands_is_kept_and_named(writer: SAConnection) -> None:
    with pytest.raises(UnknownEndpointError) as caught:
        apply_response(
            writer,
            connection_id=None,
            endpoint="/test/unheard-of",
            body=BODY,
            received_at=RECEIVED,
            derivers={ENDPOINT: _record_one_institution},
        )

    assert "/test/unheard-of" in str(caught.value)
    assert _count(writer, raw_responses) == 1


def test_the_derivation_version_is_recorded_once_and_reused(writer: SAConnection) -> None:
    with transaction(writer):
        first = ensure_derivation_version(writer)
        stamped = writer.execute(
            select(derivation_versions.c.first_used_at).where(
                derivation_versions.c.derivation_version_id == first
            )
        ).scalar_one()
    with transaction(writer):
        second = ensure_derivation_version(writer)

    assert first == second
    assert _count(writer, derivation_versions) == 1
    # `first_used_at` is the one wall-clock value in the derivation path, and it
    # is written once: a rebuild that rewrote it would make its own output
    # depend on when it ran.
    unchanged = writer.execute(
        select(derivation_versions.c.first_used_at).where(
            derivation_versions.c.derivation_version_id == first
        )
    ).scalar_one()
    assert unchanged == stamped


def test_a_new_derivation_version_is_recorded_beside_the_old_one(writer: SAConnection) -> None:
    with transaction(writer):
        old = ensure_derivation_version(writer)
    with transaction(writer):
        new = ensure_derivation_version(
            writer, version=DERIVATION_VERSION + 1, description="the next build"
        )

    assert old != new
    versions = writer.execute(
        select(derivation_versions.c.version).order_by(derivation_versions.c.version)
    ).scalars()
    assert list(versions) == [DERIVATION_VERSION, DERIVATION_VERSION + 1]


def test_the_registry_has_to_be_passed_and_cannot_be_forgotten() -> None:
    """No module-level registry to fall back to, and no default to omit.

    There was one, and it became a trap: once the composition moved above this
    layer the default had exactly one reachable outcome -- `UnknownEndpointError`
    on the first response. A caller could omit the argument, pass mypy strict and
    the whole suite, and fail at runtime on the first response of an unattended
    nightly sync. Same reasoning as `link_token_create`'s history window: a
    forgetful caller should fail to typecheck.
    """
    import inspect

    from bankmachine.store import derivation, rebuild

    assert not hasattr(derivation, "DERIVERS"), (
        "a module-level registry is back; it can only ever be empty here, because "
        "populating it would mean `store` importing `connector`"
    )
    for function in (
        derivation.deriver_for,
        derivation.derive,
        derivation.apply_response,
        rebuild.rebuild,
    ):
        parameter = inspect.signature(function).parameters["derivers"]
        assert parameter.default is inspect.Parameter.empty, (
            f"{function.__name__} lets a caller omit the registry, and the only thing that "
            f"happens then is a runtime refusal on the first response"
        )


def test_received_at_is_the_derivation_clock(writer: SAConnection) -> None:
    # The rule that keeps a rebuild reproducible: a deriver stamps rows from the
    # response, never from the current time. `DerivationContext` carries no
    # clock, which is what makes the pure route the convenient one.
    captured: list[UtcInstant] = []

    def _stamp(conn: SAConnection, response: RawResponse, context: DerivationContext) -> None:
        captured.append(response.received_at)

    apply_response(
        writer,
        connection_id=None,
        endpoint=ENDPOINT,
        body=BODY,
        received_at=RECEIVED,
        derivers={ENDPOINT: _stamp},
    )

    assert captured == [RECEIVED]
    assert not hasattr(DerivationContext(derivation_version_id=1), "now")


# --------------------------------------------------------------------------
# A failed derivation names the row an operator can go and read
# --------------------------------------------------------------------------


def _refuse_the_response(
    conn: SAConnection, response: RawResponse, context: DerivationContext
) -> None:
    """A deriver that refuses the way a real one does: naming an account, not a row.

    The identifier is the shape an aggregator actually sends -- a 32-character
    opaque run -- which is exactly what the redacting formatter blanks. That is
    the whole reason the response's own id cannot ride on the refusal's sentence.
    """
    raise DerivationError(f"account {'a1b2c3d4' * 5} has no currency, so nothing can be derived")


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_derivation_that_fails_names_the_archived_row_in_the_log(
    writer: SAConnection, caplog: pytest.LogCaptureFixture
) -> None:
    """🔴 The body is kept and inspectable, but only if the operator learns which row.

    The refusal's own sentence cannot carry that reliably. It is written by
    whichever deriver refused; `UnknownEndpointError` names an endpoint and no
    response at all; and what reaches the log is scrubbed, so a message that
    identified the response by naming an aggregator account identifier -- a
    32-character run -- arrives blanked. Naming the id on the failure path makes
    it a property of the path rather than of each message that travels it.

    Re-raised, never absorbed: the caller is the one that decides whether this
    ends a connection or a run.
    """
    caplog.set_level(logging.WARNING)

    with pytest.raises(DerivationError):
        apply_response(
            writer,
            connection_id=None,
            endpoint=ENDPOINT,
            body=BODY,
            received_at=RECEIVED,
            derivers={ENDPOINT: _refuse_the_response},
        )

    stored = writer.execute(select(raw_responses.c.raw_response_id)).scalar_one()
    named = [m for m in _warnings(caplog) if f"raw response {stored}" in m and ENDPOINT in m]
    assert named, (
        f"nothing in the log names raw response {stored}, so `sync shell` has no id to read "
        f"the archived body back by"
    )
    assert redact(named[0]) == named[0], (
        "the line an operator actually reads is scrubbed on its way to the handler; a line "
        "that only names the row before redaction names nothing after it"
    )
    assert "a1b2c3d4a1b2c3d4" not in named[0], "the deriver's own identifier rode along"


def test_the_same_page_failing_twice_names_both_archived_rows(
    writer: SAConnection, caplog: pytest.LogCaptureFixture
) -> None:
    """A connection whose derivation fails re-fetches, so the archive really does grow.

    🔴 The rows are not collapsed, and that is a ruling rather than an omission.
    `store.raw` keeps this table append-only because two bodies received at two
    times are two things the source said; a digest-keyed guard would also fire on
    nothing, since every response carries its own per-request identifier and no
    two archives of the same page hash alike. What is fixed is that each attempt
    names its own row, so an operator can read either body back and `store
    rebuild` can replay them once the build understands the page.
    """
    caplog.set_level(logging.WARNING)

    for _ in range(2):
        with pytest.raises(DerivationError):
            apply_response(
                writer,
                connection_id=None,
                endpoint=ENDPOINT,
                body=BODY,
                received_at=RECEIVED,
                derivers={ENDPOINT: _refuse_the_response},
            )

    archived = [int(row[0]) for row in writer.execute(select(raw_responses.c.raw_response_id))]
    assert len(archived) == 2
    for raw_response_id in archived:
        assert any(f"raw response {raw_response_id}" in m for m in _warnings(caplog)), (
            f"the attempt that archived raw response {raw_response_id} left no way to find it"
        )
