"""Raw preservation (FR-5): every response kept verbatim, before anything reads it.

This is the bronze half of the bronze/silver split. A response arrives, it is
written here exactly as it came off the wire, and only then does anything derive
a normalized row from it. That ordering is what makes a categorization bug a
re-run instead of a re-fetch -- and a re-fetch is often impossible, because an
aggregator's history window is not a thing you get back.

Three properties are load-bearing:

* **Verbatim.** The body is stored as the bytes that were received. It is
  compressed for size, never re-serialized, re-encoded or pretty-printed: a
  round trip through `json.loads`/`json.dumps` would reorder keys and normalize
  numbers, and the archive would then be a record of what we understood rather
  than of what we were told.
* **Self-checking.** `body_sha256` is the digest of the *plaintext* body, so it
  identifies the response independently of how it was compressed, and
  `load_response` recomputes it. A body that no longer hashes to what was
  recorded is refused rather than derived from, because a rebuild that quietly
  normalizes corrupted bytes produces analysis that is wrong and internally
  consistent -- this product's named primary failure mode.
* **Append-only.** Two identical bodies received at two times are two rows. The
  table records what this system was told and when, so deduplicating it would
  destroy the evidence that the source repeated itself.

  🔴 **A digest-keyed dedup would not do what it looks like it does, which is
  the more important half of this rule.** Every response the aggregator sends
  carries its own per-request identifier in the body, so two archives of the
  same page never hash to the same value: a guard on `body_sha256` would read as
  a fix for the connection that fails to derive and re-fetches the same page
  every run, and would fire on nothing. Nor would collapsing the rows be
  harmless if it did fire -- a deriver reads `received_at` off the row it is
  given, so `accounts.last_seen_date` would freeze on the day of the first copy
  and a live account would drift into looking no-longer-reported. What actually
  stops that loop is the derivation succeeding: `store.derivation.apply_response`
  names the archived row in the log when one fails, and `store rebuild` replays
  it from here once the build can read it.

🔴 **What the archive does NOT hold: credentials.** AC-5.1 says every response is
kept verbatim and AC-10.1 says every access token lives in the keychain, and a
response whose body carries a token would satisfy the first by breaking the
second -- permanently, because this table is append-only and a datastore backup
travels. So a credential-bearing exchange is not persisted here; the archive is
for the responses that carry *data*. The same applies to `request_context`, which
records what was asked rather than what it was asked with: no `Authorization`
header, no token in a query string. The endpoint vocabulary that can enforce this
lives in the aggregator client (`connector.Endpoint`), and **it is built**:
`issues_credential` marks the endpoints whose body carries one, and
`FetchedResponse` -- the only thing a caller derives an archive write from --
refuses to exist for them. So the rule is not something a caller has to follow;
there is no credential-bearing object for one to be handed.

Compression is pinned to a fixed level with a zeroed mtime, so the same body
always produces the same blob. Nothing depends on that today; it costs one
keyword argument, and without it two archives of the same response differ in
bytes for no reason anyone could later explain.
"""

from __future__ import annotations

import gzip
from collections.abc import Iterator
from dataclasses import dataclass
from hashlib import sha256

from sqlalchemy import Connection as SAConnection
from sqlalchemy import insert, select

from bankmachine.store.connection import StoreError
from bankmachine.store.schema import raw_responses
from bankmachine.store.types import UtcInstant

#: Highest ratio, and the archive is written once and read rarely. The level is
#: named because it is part of what makes the stored blob reproducible.
COMPRESSION_LEVEL = 9

#: gzip writes the current time into its header by default, which would make the
#: blob differ for a byte-identical body. Zeroed, so the stored bytes are a
#: function of the body alone.
COMPRESSION_MTIME = 0


class RawResponseCorruptError(StoreError):
    """A stored body no longer hashes to the digest recorded beside it.

    Raised instead of returning the bytes. Normalizing a corrupted response
    would produce rows that are wrong and carry a provenance link asserting they
    are trustworthy.
    """


class RawResponseMissingError(StoreError):
    """No raw response with that id. A dangling provenance link, not an empty result."""


@dataclass(frozen=True, slots=True)
class RawResponse:
    """One persisted response, with its body already decompressed and verified.

    Instances come from `record_response` or `load_response` and nowhere else,
    so a `RawResponse` in hand is evidence that the row exists in the datastore.
    That is how AC-5.1's "before any normalization" is held by construction
    rather than by convention: `store.derivation` cannot derive from anything
    else, so there is no path that normalizes a response that was not persisted
    first.
    """

    raw_response_id: int
    connection_id: int | None
    endpoint: str
    received_at: UtcInstant
    body: bytes
    body_sha256: str
    request_context: str | None


def body_digest(body: bytes) -> str:
    """The digest recorded beside a body. Of the plaintext, never of the blob."""
    return sha256(body).hexdigest()


def compress(body: bytes) -> bytes:
    """Compress a body reproducibly: same bytes in, same bytes out, always."""
    return gzip.compress(body, compresslevel=COMPRESSION_LEVEL, mtime=COMPRESSION_MTIME)


def decompress(blob: bytes) -> bytes:
    """The inverse of `compress`."""
    return gzip.decompress(blob)


def record_response(
    conn: SAConnection,
    *,
    connection_id: int | None,
    endpoint: str,
    body: bytes,
    received_at: UtcInstant,
    request_context: str | None = None,
) -> RawResponse:
    """Persist a response verbatim and return the handle everything else derives from.

    `connection_id` is nullable because not every response belongs to a
    connection -- an institution search is worth keeping and has no account
    behind it yet. A credential exchange is the case that is *not* worth keeping;
    see the module docstring.
    """
    digest = body_digest(body)
    result = conn.execute(
        insert(raw_responses).values(
            connection_id=connection_id,
            endpoint=endpoint,
            received_at=received_at,
            body_gzip=compress(body),
            body_sha256=digest,
            body_bytes=len(body),
            request_context=request_context,
        )
    )
    primary_key = result.inserted_primary_key
    assert primary_key is not None  # an INTEGER PRIMARY KEY insert always yields one
    return RawResponse(
        raw_response_id=int(primary_key[0]),
        connection_id=connection_id,
        endpoint=endpoint,
        received_at=received_at,
        body=body,
        body_sha256=digest,
        request_context=request_context,
    )


_SELECT_RESPONSE = select(
    raw_responses.c.raw_response_id,
    raw_responses.c.connection_id,
    raw_responses.c.endpoint,
    raw_responses.c.received_at,
    raw_responses.c.body_gzip,
    raw_responses.c.body_sha256,
    raw_responses.c.body_bytes,
    raw_responses.c.request_context,
)


def _from_row(
    raw_response_id: int,
    connection_id: int | None,
    endpoint: str,
    received_at: UtcInstant,
    body_gzip: bytes,
    body_sha256: str,
    body_bytes: int,
    request_context: str | None,
) -> RawResponse:
    body = decompress(body_gzip)
    recomputed = body_digest(body)
    if recomputed != body_sha256 or len(body) != body_bytes:
        raise RawResponseCorruptError(
            f"raw response {raw_response_id} ({endpoint}) does not match what was recorded: "
            f"{len(body)} bytes hashing to {recomputed}, against {body_bytes} bytes hashing to "
            f"{body_sha256}. Refusing to derive from it -- rows built on a corrupted body would "
            f"carry a provenance link asserting they came from something they did not"
        )
    return RawResponse(
        raw_response_id=raw_response_id,
        connection_id=connection_id,
        endpoint=endpoint,
        received_at=received_at,
        body=body,
        body_sha256=body_sha256,
        request_context=request_context,
    )


def load_response(conn: SAConnection, raw_response_id: int) -> RawResponse:
    """One response by id, with its body verified against its recorded digest."""
    row = conn.execute(
        _SELECT_RESPONSE.where(raw_responses.c.raw_response_id == raw_response_id)
    ).one_or_none()
    if row is None:
        raise RawResponseMissingError(
            f"no raw response {raw_response_id} -- a normalized row names it as its origin, so "
            f"this is a broken lineage rather than an empty result"
        )
    return _from_row(*row)


def iter_responses(conn: SAConnection) -> Iterator[RawResponse]:
    """Every response, in the order it was received. The order a rebuild replays.

    Ties on `received_at` are broken by id, so the replay order is total: two
    responses that arrived in the same instant still replay in the order they
    were written, and a rebuild is therefore a function of the archive rather
    than of how the rows happen to be laid out.

    The compressed rows are fetched up front and decompressed one at a time, so
    a caller replaying the archive holds one body at a time rather than all of
    them. Buffering the rows is deliberate: the caller is writing to other tables
    on this same connection as it consumes them, and reading a cursor that is
    still open across those writes is a hazard not worth the memory it saves.
    """
    rows = conn.execute(
        _SELECT_RESPONSE.order_by(raw_responses.c.received_at, raw_responses.c.raw_response_id)
    ).fetchall()
    for row in rows:
        yield _from_row(*row)
