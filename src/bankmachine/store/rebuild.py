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

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from sqlalchemy import Column, Integer, Table, delete, select
from sqlalchemy import Connection as SAConnection

from bankmachine.config import Config
from bankmachine.logging_setup import get_logger
from bankmachine.store import derivation
from bankmachine.store.connection import StoreError
from bankmachine.store.derivation import DerivationContext, Deriver
from bankmachine.store.engine import transaction, writer_connection
from bankmachine.store.raw import iter_responses
from bankmachine.store.schema import derivation_versions, metadata, raw_responses

logger = get_logger("store.rebuild")

#: The column carrying a normalized row's link back to the response it came from.
RAW_PROVENANCE_COLUMN = "raw_response_id"

#: The column that makes a table's rows *derived* at all: they were produced by
#: some version of the normalization logic, and they say which (AC-5.3).
DERIVATION_VERSION_COLUMN = "derivation_version_id"


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

    Rolled back rather than committed. Either a deriver is not a pure function
    of its response -- a clock call is the usual one -- or the archive no longer
    holds every response the rows were built from. Both are conditions to
    investigate before the old rows are gone.
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


def rebuild(
    config: Config,
    *,
    derivers: Mapping[str, Deriver] | None = None,
    accept_content_change: bool = False,
) -> RebuildReport:
    """Reconstruct every raw-derived row from the archive, or change nothing at all.

    The whole rebuild is one transaction under the exclusive writer lock, so a
    process killed partway leaves the previous content intact rather than a
    datastore holding half of one derivation and half of another.
    """
    with writer_connection(config) as conn:
        tables = rebuildable_tables()
        with transaction(conn):
            previous_versions = _previous_derivation_versions(conn, tables)
            digest_before = content_digest(conn)

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
            replayed = 0
            for response in iter_responses(conn):
                if context is None:
                    derivation_version_id = derivation.ensure_derivation_version(conn)
                    context = DerivationContext(derivation_version_id=derivation_version_id)
                derivation.derive(conn, response, context, derivers=derivers)
                replayed += 1

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
                        f"Rolled back. Either a deriver is not a pure function of its response -- "
                        f"a call to the clock is the usual cause -- or the archive no longer holds "
                        f"every response those rows came from. If responses were deliberately "
                        f"pruned, re-run with --accept-content-change"
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
