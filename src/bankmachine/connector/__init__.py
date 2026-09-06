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

from bankmachine.store.types import UtcInstant


class ConnectorError(Exception):
    """The aggregator could not be reached, or refused what it was asked.

    Defined here rather than beside the client so that catching an aggregator
    failure does not require importing the aggregator. The CLI's top-level
    handler turns these into a sentence, and it should not have to load the SDK
    to know what it is catching.
    """


class AggregatorNotConfiguredError(ConnectorError):
    """A credential or setting the client needs is absent."""


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
