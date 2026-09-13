"""The seam between a raw response and the normalized rows derived from it.

What ships here is the contract a deriver is written against, and it was
deliberately in place *before* the sync path existed -- a sync path written first
would normalize straight into the tables, and retro-fitting raw preservation
around it afterwards would mean rewriting the part that was already working. The
first derivers, for institutions and accounts, arrived with build step 2.

🔴 **There is no registry in this module, and `derivers` is a required argument.**
Holding one here would mean `store` importing `connector`, which pulls the
aggregator SDK into every process that opens the datastore -- the read-only query
surface included, which must never load the network layer at all. An import graph
is a better guarantee of that than a rule about who calls what, so the registry is
composed in `bankmachine.derivers`, above both layers, and passed in.

It briefly had a default. Once the composition moved up a layer that default had
exactly one reachable outcome, so a caller could omit the argument, pass mypy
strict and the whole suite, and fail at runtime on the first response of an
unattended nightly sync.

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
from typing import Protocol

from sqlalchemy import Connection as SAConnection
from sqlalchemy import insert, select

from bankmachine.logging_setup import get_logger
from bankmachine.store import transfers
from bankmachine.store.connection import StoreError
from bankmachine.store.engine import transaction
from bankmachine.store.raw import RawResponse, record_response
from bankmachine.store.schema import derivation_versions
from bankmachine.store.types import UtcInstant, now_utc

logger = get_logger("store.derivation")

#: The version of the normalization logic in this build. **Bump it in the same
#: commit as any change to a deriver that could produce different rows from the
#: same response** -- a renamed category, a corrected sign, a newly-populated
#: column. AC-5.3 exists because losslessness is only well-defined against a
#: recorded version: without one, an upstream taxonomy change and a rebuild bug
#: are indistinguishable, since both simply produce different rows than before.
DERIVATION_VERSION = 8

#: What that version means, recorded beside it so a datastore carrying rows from
#: an old version says something useful about them years later.
DERIVATION_DESCRIPTION = (
    "institutions and accounts derived from the aggregator; balances signed from the "
    "operator's point of view; the roster observation recorded per account "
    "(`accounts.last_seen_date`) and per connection (`connections.roster_observed_date`); "
    "the day a transaction's money was committed stamped once as "
    "`transactions.ledger_date` and never moved by settlement; an account created with no "
    "currency where the aggregator has stated none, and any row whose currency has no known "
    "minor-unit exponent refused rather than rounded; an account matched on the persistent "
    "identity the aggregator gives it, scoped to its institution, so a re-link converges on "
    "the row its history already hangs from; and every transaction stamped with the "
    "aggregator Item that produced it (`transactions.lineage_id`); the Item's consent "
    "expiry and its standing error recorded on the connection; and the two legs of a "
    "transfer between enrolled accounts paired, so a movement between them is not "
    "counted as money leaving the household"
)


class DerivationError(StoreError):
    """A response could not be turned into rows, and no partial row was written.

    Belongs to the seam rather than to any one deriver: a caller catching a
    derivation failure should not have to know which aggregator produced it, and
    a deriver must not reach `store.connection` for a base class -- the
    containment norm names that module as one that hands out a datastore handle,
    and a connector module that can import it can also open one.

    Loud rather than best-effort. A deriver that wrote what it understood and
    skipped the rest would produce a dataset that is incomplete and still adds
    up, which is this product's named primary failure mode reached through the
    layer built to prevent it.
    """


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


class ReplayPass(Protocol):
    """What a replay concludes from a SEQUENCE of responses, not from one body.

    A deriver is a pure function of one response, and nearly everything this
    system stores is derivable that way. One thing is not: an
    investment-transaction window's removals are the rows ABSENT from a window
    that came back whole, and absence is not visible on page four of twelve. A
    rebuild that replayed only the derivers would re-upsert every row that ever
    appeared and clear every soft delete with it -- so the rebuilt store would
    hold rows the synced store had retired, and report success.

    🔴 **Shown every response in archive order, including the ones it ignores.**
    What closes a window is a page and what opens one is an earlier page, so a
    pass that was handed a pre-filtered stream would be reading an order it did
    not establish. Filtering is the pass's own job, against
    `response.endpoint`.

    🔴 **Stateful, and therefore built fresh for each replay.** A pass
    accumulates across the responses it is shown, so one carried over from a
    previous replay would attribute that replay's pages to this one's window.
    `store.rebuild.rebuild` takes a factory rather than instances for exactly
    that reason.
    """

    def observe(self, conn: SAConnection, response: RawResponse) -> None:
        """Fold one replayed response in, writing whatever it now establishes."""


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


def deriver_for(endpoint: str, derivers: Mapping[str, Deriver]) -> Deriver:
    """The deriver registered for an endpoint, or a refusal naming it.

    🔴 **`derivers` is required, and has no default.** It briefly had one -- an
    empty module-level registry, left over from when this seam expected build
    step 2 to populate it in place. Once the registry moved a layer up, that
    default had exactly one reachable outcome: `UnknownEndpointError` on the
    first response. A caller could omit the argument, pass mypy strict and the
    whole suite, and fail at runtime on the first response of an unattended
    nightly sync. The same reasoning as `link_token_create`'s history window: a
    forgetful caller should fail to typecheck.
    """
    registry = derivers
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
    derivers: Mapping[str, Deriver],
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
    derivers: Mapping[str, Deriver],
    request_context: str | None = None,
) -> RawResponse:
    """Persist a response, commit it, then derive from it. The sync path's one entry point.

    The two commits are the point. See this module's docstring: the archive
    survives a deriver that raises, because the response may be unfetchable and
    the derivation is always re-runnable.

    🔴 **A failing derivation names the row an operator can go and read.** The
    body is archived and therefore inspectable, but only if the operator learns
    which row it is, and the refusal alone does not reliably tell them: the
    message is written by whichever deriver refused, `UnknownEndpointError` names
    an endpoint and no response at all, and the sentence that reaches the log is
    scrubbed by the redacting formatter -- an aggregator's account identifier is
    a 32-character run, so a message that identified the response by naming one
    arrives blanked. Logging it here makes the id a property of the
    derivation-failure path rather than of each message that travels it, and
    `sync shell` reads the body from there.

    Logged and re-raised, never absorbed: the caller is the one that decides
    whether this ends a connection or a run.
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
        try:
            derive(conn, response, context, derivers=derivers)
        except StoreError:
            logger.warning(
                "raw response %d (%s) on connection %s was archived but could not be derived; "
                "`bankmachine sync shell` can read the body back by that id",
                response.raw_response_id,
                response.endpoint,
                "none" if response.connection_id is None else response.connection_id,
            )
            raise
        # 🔴 Inside the same transaction as the derivation that produced the
        # rows, so a page never commits with its rows visible and their pairing
        # not yet computed -- a reader between the two would see a transfer's
        # outgoing leg counted as spending and then watch it stop being counted,
        # with nothing to say why.
        #
        # Per response rather than per run because this is the sync path's ONE
        # entry point and it has no notion of a run ending. The pass considers
        # only unpaired rows, so the cost is the rows this page added, and a leg
        # whose counterparty arrives on a later page pairs when that page lands.
        transfers.pair_transfers(conn)
    return response
