"""The seam between a raw response and the normalized rows derived from it.

Nothing in this build derives anything: the aggregator client is build step 2,
and `DERIVERS` ships empty. What ships here is the contract that step 2 has to
be written against, and it is deliberately in place *before* the sync path
exists -- a sync path written first would normalize straight into the tables,
and retro-fitting raw preservation around it afterwards would mean rewriting the
part that was already working.

**One normalization, two callers.** The sync path and `store rebuild` run the
same derivers over the same responses. That is what makes AC-11.5's "rebuild
reproduces the normalized tables" checkable rather than aspirational: rebuild is
not a second implementation of normalization that has to be kept in step with
the first, it is the first one replayed.

**A deriver is a pure function of its response.** Given the same
`RawResponse` and the same `DerivationContext` it must write the same rows --
every time, in any order relative to other responses' derivations, on any
machine. In practice that means one rule with teeth: **never call the clock.**
A `first_seen_at` stamped `now()` differs on every replay, and the rebuild would
report content it could not reproduce. Use `response.received_at`, which is when
this system actually learned the thing. `DerivationContext` deliberately carries
no clock and no configuration, so the pure path is also the convenient one, and
`store.rebuild` catches the impure one by comparing content before and after.

**Persist first, then derive.** `apply_response` writes the raw row and commits
it, and only then derives -- two transactions, on purpose. A deriver that raises
must not take the archive down with it: the response may be unfetchable
afterwards (an aggregator's history window does not come back), while the
derivation can be re-run at any time by `store rebuild`. So a crash mid-derive
leaves the response kept and no half-derived rows, which is the state a rebuild
repairs.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from sqlalchemy import Connection as SAConnection
from sqlalchemy import insert, select

from bankmachine.store.connection import StoreError
from bankmachine.store.engine import transaction
from bankmachine.store.raw import RawResponse, record_response
from bankmachine.store.schema import derivation_versions
from bankmachine.store.types import UtcInstant, now_utc

#: The version of the normalization logic in this build. **Bump it in the same
#: commit as any change to a deriver that could produce different rows from the
#: same response** -- a renamed category, a corrected sign, a newly-populated
#: column. AC-5.3 exists because losslessness is only well-defined against a
#: recorded version: without one, an upstream taxonomy change and a rebuild bug
#: are indistinguishable, since both simply produce different rows than before.
DERIVATION_VERSION = 1

#: What that version means, recorded beside it so a datastore carrying rows from
#: an old version says something useful about them years later.
DERIVATION_DESCRIPTION = "raw-response layer and rebuild; no aggregator derivers registered yet"


class UnknownEndpointError(StoreError):
    """No deriver is registered for a response's endpoint.

    Raised rather than skipped. A rebuild that steps over a response it cannot
    interpret would report success while producing a dataset missing whatever
    that endpoint carried -- silent incompleteness, which is worse here than a
    refusal, because every number downstream would still add up.
    """


@dataclass(frozen=True, slots=True)
class DerivationContext:
    """What a deriver is given besides the response itself.

    Just the version id every row it writes must carry. There is no clock and no
    configuration here by design: a deriver that cannot reach the clock cannot
    accidentally make its output depend on when it ran.
    """

    derivation_version_id: int


#: Given a writable handle, one persisted response, and the version stamp its
#: rows must carry, write the normalized rows that response implies.
Deriver = Callable[[SAConnection, RawResponse, DerivationContext], None]

#: Endpoint -> deriver. Empty in this build: build step 2 registers the
#: aggregator's endpoints here as it learns to call them. It is a mapping rather
#: than a mutable dict so nothing registers a deriver as a side effect of being
#: imported -- what a rebuild replays is then a property of the build, not of
#: which modules happened to be loaded first.
DERIVERS: Mapping[str, Deriver] = MappingProxyType({})


def ensure_derivation_version(
    conn: SAConnection,
    *,
    version: int | None = None,
    description: str | None = None,
    first_used_at: UtcInstant | None = None,
) -> int:
    """The id of this build's derivation version, recorded on first use.

    `first_used_at` is a wall-clock stamp and it is written exactly once, when
    the version first derives something. Re-running against an existing row
    leaves it alone, so a rebuild does not rewrite the history of when a version
    entered service -- and so a rebuild's output stays a function of the archive.

    The version defaults to the module constant read at call time rather than at
    definition time, so a test can stand in the next build's version the same
    way the next build will: by changing the constant.
    """
    version = DERIVATION_VERSION if version is None else version
    description = DERIVATION_DESCRIPTION if description is None else description
    existing = conn.execute(
        select(derivation_versions.c.derivation_version_id).where(
            derivation_versions.c.version == version
        )
    ).one_or_none()
    if existing is not None:
        return int(existing[0])
    result = conn.execute(
        insert(derivation_versions).values(
            version=version,
            description=description,
            first_used_at=first_used_at if first_used_at is not None else now_utc(),
        )
    )
    primary_key = result.inserted_primary_key
    assert primary_key is not None  # an INTEGER PRIMARY KEY insert always yields one
    return int(primary_key[0])


def deriver_for(endpoint: str, derivers: Mapping[str, Deriver] | None = None) -> Deriver:
    """The deriver registered for an endpoint, or a refusal naming it."""
    registry = DERIVERS if derivers is None else derivers
    try:
        return registry[endpoint]
    except KeyError:
        raise UnknownEndpointError(
            f"no deriver is registered for endpoint {endpoint!r}; this build understands "
            f"{sorted(registry) or 'no endpoints at all'}. The response is kept -- it is the "
            f"derivation that cannot proceed, and skipping it would silently produce an "
            f"incomplete dataset that still adds up"
        ) from None


def derive(
    conn: SAConnection,
    response: RawResponse,
    context: DerivationContext,
    *,
    derivers: Mapping[str, Deriver] | None = None,
) -> None:
    """Write the normalized rows one already-persisted response implies.

    Takes no transaction boundary of its own: the caller owns it, because a
    rebuild's boundary spans every response and a live sync's spans one.
    """
    deriver_for(response.endpoint, derivers)(conn, response, context)


def apply_response(
    conn: SAConnection,
    *,
    connection_id: int | None,
    endpoint: str,
    body: bytes,
    received_at: UtcInstant,
    request_context: str | None = None,
    derivers: Mapping[str, Deriver] | None = None,
) -> RawResponse:
    """Persist a response, commit it, then derive from it. The sync path's one entry point.

    The two commits are the point. See this module's docstring: the archive
    survives a deriver that raises, because the response may be unfetchable and
    the derivation is always re-runnable.
    """
    with transaction(conn):
        response = record_response(
            conn,
            connection_id=connection_id,
            endpoint=endpoint,
            body=body,
            received_at=received_at,
            request_context=request_context,
        )
    with transaction(conn):
        context = DerivationContext(derivation_version_id=ensure_derivation_version(conn))
        derive(conn, response, context, derivers=derivers)
    return response
