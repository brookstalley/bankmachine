"""Rebuild (AC-5.2, AC-11.5): the normalized tables, reconstructed from the archive.

A rebuild deletes every row it can recreate, replays the whole raw archive
through this build's derivers, and then checks its own work: it hashes the
datastore's content before and after, and **refuses to commit a rebuild that did
not reproduce what it replaced** unless the derivation version changed. That
refusal is the reason this command is worth having. Without it a rebuild is an
irreversible bulk operation whose only failure signal is analysis quietly
turning wrong weeks later, which is this product's named primary failure mode.

**What it deletes is derived, not listed** -- and the classification has one
source of truth, read off the Core metadata, because two signals that had to
agree would eventually not. A table is **derived** when it carries a
`derivation_version_id`; of those, the ones holding a foreign key that *points
at* a raw response are **rebuildable** and are emptied before the replay, and the
rest are **derived dimensions** -- `securities` today -- which a deriver upserts
on its natural key, because the replay meets rows it did not delete. Everything
else is identity or operator state that a rebuild never touches: enrollment rows,
account rules, sync cursors, and the rows imported from operator files (FR-7),
which name a `manual_import_id` and no raw response. Nothing in the archive could
recreate those, and deleting a row you cannot recreate is not a rebuild.

**Replaying the derivers is not the whole replay.** Almost every row here is a
pure function of one archived body, and for those the replay is exactly the
derivers run again. One class of fact is not: a removal on the
investment-transaction feed is a row's ABSENCE from a window that came back
whole, and no single page carries it. A rebuild that ran only the derivers would
re-upsert every row that ever appeared and clear every soft delete with it, then
report success over a store holding transactions the source had dropped. So a
rebuild is also given `ReplayPass`es -- one per such fact, each fed the archive
in order and writing at the response that completes what it was accumulating.

**What "byte-identically" means here.** AC-11.5 asks that a rebuild reproduce
the normalized tables byte-identically. The digest covers every column of every
table in the schema except one class: a table's own single-column integer
primary key, where nothing else references it. Those are rowid allocations, not
facts about the world -- `transaction_id` is assigned by SQLite in insertion
order and is referenced by nothing, so requiring it to come back identical would
make the criterion a statement about the allocator. Every id that *is* a fact --
`account_id`, `security_id`, every foreign key -- is covered, so a rebuild that
renumbered or orphaned anything fails the check.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Final

from sqlalchemy import Column, Integer, Table, delete, select, update
from sqlalchemy import Connection as SAConnection

from bankmachine.config import Config
from bankmachine.logging_setup import get_logger
from bankmachine.store import derivation, transfers
from bankmachine.store.connection import StoreError
from bankmachine.store.derivation import DerivationContext, Deriver, ReplayPass
from bankmachine.store.engine import transaction, writer_connection
from bankmachine.store.raw import iter_responses
from bankmachine.store.schema import derivation_versions, metadata, raw_responses

logger = get_logger("store.rebuild")

#: The column carrying a normalized row's link back to the response it came from.
RAW_PROVENANCE_COLUMN = "raw_response_id"

#: The column that makes a table's rows *derived* at all: they were produced by
#: some version of the normalization logic, and they say which (AC-5.3).
DERIVATION_VERSION_COLUMN = "derivation_version_id"


@dataclass(frozen=True, slots=True)
class OperatorState:
    """Columns on a rebuildable table that the archive could never bring back."""

    identity: tuple[str, ...]
    """What names the same row on the other side of the replay.

    The SOURCE's identity, never the local primary key: a rebuild reinserts the
    rows and SQLite allocates fresh row numbers, so a key captured before the
    delete would point at whatever happened to land there afterwards.
    """

    columns: tuple[str, ...]
    """What is carried across, because no deriver writes it."""


#: What a rebuild carries across the replay, by table.
#:
#: 🔴 **A rebuild empties a table because the archive can recreate it, and these
#: columns are the exception inside the exception.** `category_override` is the
#: operator's own correction: no response contains it and no deriver writes it,
#: so replaying the archive lands on a row with the column blank. The digest
#: guard would normally refuse a rebuild that changed content -- but a changed
#: derivation version is the usual REASON to rebuild, and in that state the
#: guard treats a change as expected. So the loss was reported as a success.
#:
#: The sibling rule lives in the accounts deriver's `_OPERATOR_OWNED`, which
#: stops a sync overwriting an operator's column; this is the same principle at
#: the other event. Both are lists of columns rather than a property of the
#: schema, because "the operator owns this" is a decision recorded in
#: `data-model.md` and not something a column's type can say.
OPERATOR_STATE: Final[Mapping[str, OperatorState]] = {
    "transactions": OperatorState(
        identity=("account_id", "source_transaction_id"),
        columns=("category_override",),
    ),
}


class UnhashableValueError(StoreError):
    """The datastore holds a value the content digest cannot encode.

    A float is the one that matters: SQLite stores one in an INTEGER column
    without complaint, and money that has passed through a float has already
    lost fractions of a cent (AC-6.2). The schema's CHECK constraints keep them
    out of monetary columns; this refuses to certify a rebuild of a datastore
    holding one anywhere at all.
    """


class RebuildNotReproducibleError(StoreError):
    """The rebuild produced different content at an unchanged derivation version.

    Rolled back rather than committed. Three causes reach this, and the third is
    the one that looks like neither: a deriver is not a pure function of its
    response (a clock call is the usual one); the archive no longer holds every
    response the rows were built from; or an investments run archived a complete
    window and stopped before concluding the removals from it, so the replay
    retires rows at the closing page's instant while the live store retired them
    at a later run's -- or not at all. All three are conditions to investigate
    before the old rows are gone, and the third is not fixed by accepting the
    change: accepting it writes the replay's instant over the store's, which is
    the reproducible answer, but it also accepts whatever else moved in the same
    digest.
    """


@dataclass(frozen=True, slots=True)
class RebuildReport:
    """What a rebuild did, in the terms the operator needs to judge it."""

    responses_replayed: int
    rows_deleted: Mapping[str, int]
    derivation_version: int
    #: None when the archive was empty, because nothing was derived and a
    #: version stamp with no rows under it would assert otherwise.
    derivation_version_id: int | None
    previous_derivation_versions: tuple[int, ...]
    digest_before: str
    digest_after: str

    @property
    def content_changed(self) -> bool:
        return self.digest_before != self.digest_after

    @property
    def change_was_expected(self) -> bool:
        """Whether the rows this rebuild replaced were produced by this version.

        Only one state makes a difference suspicious: the replaced rows were all
        derived by the version that just re-derived them, so replaying the same
        archive through the same logic should have landed on the same content.
        Any other state -- a new derivation version, or no derived rows to
        reproduce in the first place -- has an explanation already, and the
        version stamp every rebuilt row carries is what records which.
        """
        return set(self.previous_derivation_versions) != {self.derivation_version}


def derived_tables() -> tuple[Table, ...]:
    """Every table whose rows were produced by the normalization logic (AC-5.3).

    The classification below is a partition of *these*, so the two questions a
    rebuild asks -- what may I delete, and what must a deriver upsert -- are
    answered from one property rather than from two that happen to agree.
    """
    return tuple(
        table
        for table in metadata.sorted_tables
        if _points_at(table, DERIVATION_VERSION_COLUMN, derivation_versions.c.derivation_version_id)
    )


def rebuildable_tables() -> tuple[Table, ...]:
    """The derived tables a rebuild empties before replaying the archive.

    A table qualifies by *pointing at* a raw response -- a foreign key, not a
    column of the right name. `raw_responses` has a `raw_response_id` of its own,
    and a name test would have put the archive itself first in the list of
    things a rebuild empties before replaying it.
    """
    return tuple(table for table in derived_tables() if _points_at_a_raw_response(table))


def derived_dimension_tables() -> tuple[Table, ...]:
    """The derived tables a rebuild does NOT empty, and what that costs a deriver.

    A dimension row is shared: one security is named by holdings and investment
    transactions across many responses, so it has no single response to point at
    and no id that could be reassigned without orphaning them. A rebuild
    therefore leaves these rows in place, and 🔴 **a deriver writing one must
    upsert on its natural key** -- a plain insert raises on the replay's second
    pass, and neither failure is the deriver-purity bug it will look like.
    """
    return tuple(table for table in derived_tables() if not _points_at_a_raw_response(table))


def _points_at_a_raw_response(table: Table) -> bool:
    return _points_at(table, RAW_PROVENANCE_COLUMN, raw_responses.c.raw_response_id)


def _points_at(table: Table, column_name: str, target: Column[Any]) -> bool:
    """Whether a table *references* something, rather than merely naming it.

    Both classifications above turn on this and neither may turn on a column
    name, because the two registries name their own primary keys exactly as
    their referents do: `raw_responses.raw_response_id` and
    `derivation_versions.derivation_version_id`. A name test puts each registry
    in the set of things a rebuild rewrites -- for the archive that means
    deleting it and then replaying nothing.
    """
    column = table.c.get(column_name)
    if column is None:
        return False
    return any(key.column is target for key in column.foreign_keys)


def _is_rowid_surrogate(table: Table, column: Column[object]) -> bool:
    """Whether a column is an allocated row number rather than a recorded fact."""
    primary_key = list(table.primary_key.columns)
    return (
        len(primary_key) == 1
        and primary_key[0] is column
        and isinstance(column.type, Integer)
        and not column.foreign_keys
    )


def digest_columns(table: Table) -> tuple[str, ...]:
    """The columns of a table that a rebuild must reproduce exactly."""
    return tuple(column.name for column in table.columns if not _is_rowid_surrogate(table, column))


def _encode(value: object) -> bytes:
    """One value, encoded so that no two different values encode alike.

    Length-prefixed and type-tagged: without the tag the integer 1 and the text
    "1" would hash the same, and without the length a digest could not tell
    ("ab", "c") from ("a", "bc"). Anything else -- a float above all -- raises;
    see `UnhashableValueError`.
    """
    if value is None:
        return b"N0:"
    if isinstance(value, bool):  # bool is an int subclass; SQLite has no boolean type
        raise UnhashableValueError("SQLite returned a bool, which it cannot store")
    if isinstance(value, int):
        tag, payload = b"I", str(value).encode("ascii")
    elif isinstance(value, str):
        tag, payload = b"T", value.encode("utf-8")
    elif isinstance(value, bytes):
        tag, payload = b"B", value
    else:
        raise UnhashableValueError(
            f"a {type(value).__name__} is stored in this datastore ({value!r}); the schema stores "
            f"integers, text and blobs only, and a float here would be money losing fractions of "
            f"a cent silently (AC-6.2)"
        )
    return tag + str(len(payload)).encode("ascii") + b":" + payload


def content_digest(conn: SAConnection) -> str:
    """A digest of everything in the datastore that a rebuild must not change.

    Rows are hashed individually and their digests sorted, so the result depends
    on the content of each table and not on the order SQLite happens to return
    rows in -- which a rebuild changes as a matter of course, since it reinserts
    them.
    """
    overall = sha256()
    for table in metadata.sorted_tables:
        columns = digest_columns(table)
        overall.update(_encode(table.name))
        overall.update(_encode(len(columns)))
        if not columns:
            continue
        # Read through the driver rather than the typed columns: the claim is
        # about what the file holds, and a TypeDecorator would hand back the
        # `datetime` it parsed instead of the text that was stored -- hiding
        # exactly the kind of representation drift this digest exists to catch.
        selected = ", ".join(f'"{name}"' for name in columns)
        rows = conn.exec_driver_sql(f'SELECT {selected} FROM "{table.name}"').fetchall()
        row_digests = sorted(
            sha256(b"".join(_encode(value) for value in row)).digest() for row in rows
        )
        overall.update(_encode(len(row_digests)))
        for row_digest in row_digests:
            overall.update(row_digest)
    return overall.hexdigest()


def no_replay_passes() -> tuple[ReplayPass, ...]:
    """A replay that concludes nothing beyond what each response says on its own.

    Passed explicitly by a caller whose archive holds no such fact, so that
    "nothing to reassemble here" is a decision in the call rather than an
    argument somebody left off.
    """
    return ()


def rebuild(
    config: Config,
    *,
    derivers: Mapping[str, Deriver],
    replay_passes: Callable[[], Sequence[ReplayPass]],
    accept_content_change: bool = False,
) -> RebuildReport:
    """Reconstruct every raw-derived row from the archive, or change nothing at all.

    The whole rebuild is one transaction under the exclusive writer lock, so a
    process killed partway leaves the previous content intact rather than a
    datastore holding half of one derivation and half of another.

    `replay_passes` is a FACTORY rather than a sequence: a pass accumulates
    across the responses it is shown (`store.derivation.ReplayPass`), so
    instances shared between two rebuilds would carry the first one's pages into
    the second one's window. Building them here makes that impossible rather
    than merely discouraged.

    🔴 **Required, for the reason `derivers` is** -- and more sharply. That
    argument briefly had a default, and once the composition moved up a layer the
    default had exactly one reachable outcome, so a caller could omit it, pass
    mypy strict and the whole suite, and fail at runtime. Omitting the passes
    fails *silently* instead: the rebuild runs, clears every investment-transaction
    soft delete, and reports success over a store holding rows the source had
    dropped. `no_replay_passes` is the value that says so out loud.
    """
    with writer_connection(config) as conn:
        tables = rebuildable_tables()
        with transaction(conn):
            previous_versions = _previous_derivation_versions(conn, tables)
            digest_before = content_digest(conn)

            preserved = _capture_operator_state(conn, tables)

            deleted: dict[str, int] = {}
            for table in tables:
                result = conn.execute(
                    delete(table).where(table.c[RAW_PROVENANCE_COLUMN].is_not(None))
                )
                deleted[table.name] = result.rowcount

            # The version is recorded on first use and not before. Migration 002
            # deliberately seeds none, for the same reason: a derivation version
            # that has derived nothing is a claim about code that never ran. That
            # is also why the responses are consumed as they arrive rather than
            # collected first -- an archive is the largest thing in this
            # datastore, and only one decompressed body needs to exist at a time.
            derivation_version_id: int | None = None
            context: DerivationContext | None = None
            passes = replay_passes()
            replayed = 0
            for response in iter_responses(conn):
                if context is None:
                    derivation_version_id = derivation.ensure_derivation_version(conn)
                    context = DerivationContext(derivation_version_id=derivation_version_id)
                derivation.derive(conn, response, context, derivers=derivers)
                # 🔴 Inside the loop, at the response that completes whatever the
                # pass was accumulating -- never once over the finished tables.
                # A conclusion drawn from a SET of responses is evidence about
                # the rows that existed when that set closed, and running it at
                # the end would judge an early window against rows that only
                # arrived in a later one. The replay reproduces the sequence of
                # conclusions, which is the only thing that reproduces the store.
                for replay_pass in passes:
                    replay_pass.observe(conn, response)
                replayed += 1

            # 🔴 After every response is replayed and BEFORE the digest is
            # taken. A transfer's two legs routinely arrive on different pages
            # and sometimes from different institutions, so pairing per response
            # would match only whatever happened to be in the store already --
            # and a rebuild would then produce a different pairing from the same
            # archive depending on page order. Once, over the finished tables,
            # is the only pass that is a function of the data.
            transfers.pair_transfers(conn)

            _restore_operator_state(conn, preserved)

            # 🔴 After the restore, because the digest is what decides whether
            # this rebuild reproduced what it replaced -- and a digest taken
            # while the operator's own columns were still blank would answer a
            # question nobody asked.
            digest_after = content_digest(conn)
            report = RebuildReport(
                responses_replayed=replayed,
                rows_deleted=deleted,
                derivation_version=derivation.DERIVATION_VERSION,
                derivation_version_id=derivation_version_id,
                previous_derivation_versions=previous_versions,
                digest_before=digest_before,
                digest_after=digest_after,
            )
            version = report.derivation_version
            if report.content_changed and not report.change_was_expected:
                if not accept_content_change:
                    raise RebuildNotReproducibleError(
                        f"the rebuild produced different content at an unchanged derivation "
                        f"version ({version}): {digest_before} became {digest_after}. "
                        f"Rolled back. A deriver may not be a pure function of its response -- "
                        f"a call to the clock is the usual cause -- or the archive may no longer "
                        f"hold every response those rows came from. Third cause, easy to mistake "
                        f"for either: an investments run that archived a complete window and "
                        f"stopped before concluding its removals leaves the replay retiring rows "
                        f"the live store retired later or not at all. Compare `removed_at` on "
                        f"`investment_transactions` against the page instants in `raw_responses` "
                        f"before deciding. If responses were deliberately pruned, re-run with "
                        f"--accept-content-change"
                    )
                logger.warning(
                    "committing a rebuild whose content changed at derivation version %d, "
                    "because the operator accepted it",
                    derivation.DERIVATION_VERSION,
                )
        logger.info(
            "rebuilt %d row(s) from %d raw response(s) at derivation version %d",
            sum(report.rows_deleted.values()),
            report.responses_replayed,
            derivation.DERIVATION_VERSION,
        )
        return report


#: One captured row: the identity that finds it again, and what to put back on it.
CapturedState = dict[str, list[tuple[tuple[object, ...], dict[str, object]]]]


def _capture_operator_state(conn: SAConnection, tables: tuple[Table, ...]) -> CapturedState:
    """Read the operator-owned columns off the rows about to be deleted.

    Read before the delete, because afterwards there is nothing left to ask --
    the same reason `_previous_derivation_versions` is.

    A row whose identity is not fully populated is skipped: it cannot be matched
    on the other side, and an incomplete key would match rows it does not mean.
    Rows with nothing set in any of the columns are skipped too, so a rebuild of
    a store nobody has corrected does no work at all.
    """
    captured: CapturedState = {}
    for table in tables:
        state = OPERATOR_STATE.get(table.name)
        if state is None:
            continue
        rows = conn.execute(
            select(*(table.c[name] for name in (*state.identity, *state.columns))).where(
                table.c[RAW_PROVENANCE_COLUMN].is_not(None)
            )
        ).all()
        held = []
        for row in rows:
            key = tuple(row[: len(state.identity)])
            values = dict(zip(state.columns, row[len(state.identity) :], strict=True))
            if None in key or all(value is None for value in values.values()):
                continue
            held.append((key, values))
        if held:
            captured[table.name] = held
    return captured


def _restore_operator_state(conn: SAConnection, captured: CapturedState) -> None:
    """Put each captured value back on the row the replay recreated.

    🔴 **A value with nowhere to land is reported, never re-homed.** The row it
    belonged to is not in the rebuilt table -- its response was pruned, or a
    changed deriver no longer emits it under that identity -- and applying the
    correction to some other row would be worse than losing it. Saying which one
    was lost is what lets the operator put it back; saying nothing is how a
    rebuild reports success over an answer that quietly changed.
    """
    for table_name, held in captured.items():
        table = metadata.tables[table_name]
        state = OPERATOR_STATE[table_name]
        for key, values in held:
            result = conn.execute(
                update(table)
                .where(
                    *(
                        table.c[name] == value
                        for name, value in zip(state.identity, key, strict=True)
                    )
                )
                .values(**values)
            )
            if result.rowcount == 0:
                logger.warning(
                    "the replay did not recreate the %s row carrying %s, so %s is lost: %s",
                    table_name,
                    dict(zip(state.identity, key, strict=True)),
                    ", ".join(state.columns),
                    values,
                )


def _previous_derivation_versions(conn: SAConnection, tables: tuple[Table, ...]) -> tuple[int, ...]:
    """The derivation versions the rows about to be deleted were produced by.

    Read before the delete, because afterwards there is nothing left to ask.
    """
    versions: set[int] = set()
    for table in tables:
        rows = conn.execute(
            select(derivation_versions.c.version)
            .distinct()
            .select_from(
                table.join(
                    derivation_versions,
                    table.c.derivation_version_id == derivation_versions.c.derivation_version_id,
                )
            )
            .where(table.c[RAW_PROVENANCE_COLUMN].is_not(None))
        ).fetchall()
        versions.update(int(row[0]) for row in rows)
    return tuple(sorted(versions))
