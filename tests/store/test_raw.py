"""The bronze layer (FR-5): responses kept verbatim, and known to still be verbatim.

The tests that matter here are the ones about what the archive is *for*. It is
not a cache: it is the thing a rebuild derives from years after the source
stopped being able to return the same window, so "the body came back byte for
byte" and "a body that changed under us is refused rather than used" are the two
claims worth holding.
"""

from __future__ import annotations

import gzip
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import Connection as SAConnection
from sqlalchemy import update

from bankmachine.config import Config
from bankmachine.store.engine import transaction, writer_engine
from bankmachine.store.raw import (
    RawResponseCorruptError,
    RawResponseMissingError,
    body_digest,
    compress,
    decompress,
    iter_responses,
    load_response,
    record_response,
)
from bankmachine.store.schema import raw_responses
from bankmachine.store.types import UtcInstant, utc_instant

ENDPOINT = "/test/transactions"
BODY = b'{"transactions": [{"id": "t-1", "amount": "12.34"}]}'


def instant(second: int) -> UtcInstant:
    return utc_instant(datetime(2026, 9, 6, 12, 0, second, tzinfo=UTC))


@pytest.fixture
def writer(initialized_config: Config) -> Iterator[SAConnection]:
    """One writable handle for a test, taken through the factory that locks."""
    with writer_engine(initialized_config) as engine, engine.connect() as conn:
        yield conn


def test_a_recorded_response_comes_back_byte_for_byte(writer: SAConnection) -> None:
    with transaction(writer):
        recorded = record_response(
            writer,
            connection_id=None,
            endpoint=ENDPOINT,
            body=BODY,
            received_at=instant(0),
            request_context='{"cursor": null}',
        )

    loaded = load_response(writer, recorded.raw_response_id)

    assert loaded.body == BODY
    assert loaded.body_sha256 == body_digest(BODY)
    assert loaded.endpoint == ENDPOINT
    assert loaded.request_context == '{"cursor": null}'


def test_the_body_is_stored_compressed_rather_than_in_the_clear(writer: SAConnection) -> None:
    body = b'{"page": ' + b'"aaaaaaaaaa", ' * 200 + b'"end": true}'

    with transaction(writer):
        recorded = record_response(
            writer,
            connection_id=None,
            endpoint=ENDPOINT,
            body=body,
            received_at=instant(0),
        )

    stored = writer.execute(
        raw_responses.select().where(raw_responses.c.raw_response_id == recorded.raw_response_id)
    ).one()
    blob = stored.body_gzip
    assert isinstance(blob, bytes)
    assert len(blob) < len(body)
    assert body not in blob
    assert gzip.decompress(blob) == body
    assert stored.body_bytes == len(body)


def test_the_same_body_always_compresses_to_the_same_bytes() -> None:
    # gzip stamps the current time into its header unless told not to, which
    # would make two archives of one response differ for no explicable reason.
    assert compress(BODY) == compress(BODY)


def test_the_digest_is_of_the_body_not_of_the_compressed_blob() -> None:
    # So it stays the identity of the response even if the compression settings
    # ever change.
    assert body_digest(BODY) != body_digest(compress(BODY))
    assert decompress(compress(BODY)) == BODY


def test_a_body_that_no_longer_matches_its_digest_is_refused(writer: SAConnection) -> None:
    with transaction(writer):
        recorded = record_response(
            writer,
            connection_id=None,
            endpoint=ENDPOINT,
            body=BODY,
            received_at=instant(0),
        )
    with transaction(writer):
        writer.execute(
            update(raw_responses)
            .where(raw_responses.c.raw_response_id == recorded.raw_response_id)
            .values(body_gzip=compress(b'{"transactions": []}'))
        )

    with pytest.raises(RawResponseCorruptError) as caught:
        load_response(writer, recorded.raw_response_id)

    assert str(recorded.raw_response_id) in str(caught.value)


def test_a_body_whose_length_no_longer_matches_is_refused(writer: SAConnection) -> None:
    # The length is a second, cheaper witness to the same fact. It is checked
    # because a truncation that happened to leave the digest column stale would
    # otherwise be caught only by the hash, and having one check is having none
    # when the hash column is the thing that was rewritten.
    with transaction(writer):
        recorded = record_response(
            writer,
            connection_id=None,
            endpoint=ENDPOINT,
            body=BODY,
            received_at=instant(0),
        )
    with transaction(writer):
        writer.execute(
            update(raw_responses)
            .where(raw_responses.c.raw_response_id == recorded.raw_response_id)
            .values(body_bytes=len(BODY) + 1)
        )

    with pytest.raises(RawResponseCorruptError):
        load_response(writer, recorded.raw_response_id)


def test_a_dangling_provenance_link_is_named_rather_than_returned_empty(
    writer: SAConnection,
) -> None:
    with pytest.raises(RawResponseMissingError):
        load_response(writer, 4242)


def test_the_same_body_received_twice_is_two_rows(writer: SAConnection) -> None:
    # The archive records what this system was told and when. Two identical
    # responses at two times are two facts, and collapsing them would destroy
    # the evidence that the source repeated itself.
    with transaction(writer):
        first = record_response(
            writer,
            connection_id=None,
            endpoint=ENDPOINT,
            body=BODY,
            received_at=instant(0),
        )
        second = record_response(
            writer,
            connection_id=None,
            endpoint=ENDPOINT,
            body=BODY,
            received_at=instant(1),
        )

    assert first.raw_response_id != second.raw_response_id
    assert first.body_sha256 == second.body_sha256


def test_responses_replay_in_the_order_they_were_received(writer: SAConnection) -> None:
    # Written out of order on purpose: a rebuild's output must depend on when
    # the source said things, not on the order the rows happen to sit in.
    with transaction(writer):
        for second, marker in ((2, b"c"), (0, b"a"), (1, b"b")):
            record_response(
                writer,
                connection_id=None,
                endpoint=ENDPOINT,
                body=marker,
                received_at=instant(second),
            )

    assert [response.body for response in iter_responses(writer)] == [b"a", b"b", b"c"]


def test_responses_sharing_an_instant_replay_in_the_order_they_were_written(
    writer: SAConnection,
) -> None:
    with transaction(writer):
        for marker in (b"a", b"b", b"c"):
            record_response(
                writer,
                connection_id=None,
                endpoint=ENDPOINT,
                body=marker,
                received_at=instant(0),
            )

    assert [response.body for response in iter_responses(writer)] == [b"a", b"b", b"c"]


@given(body=st.binary(max_size=4096))
def test_any_body_survives_compression_and_hashing(body: bytes) -> None:
    # A response body is bytes off a socket, not text: a mis-declared charset or
    # a truncated stream must round-trip as whatever it actually was, because
    # the archive's job is to preserve what arrived rather than what parsed.
    assert decompress(compress(body)) == body
    assert body_digest(body) == body_digest(bytes(body))
