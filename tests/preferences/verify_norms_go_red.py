#!/usr/bin/env python3
"""Break each structural guarantee in turn and confirm its test goes red.

Not collected by pytest -- it edits the source tree, so it is run deliberately:

    uv run python tests/preferences/verify_norms_go_red.py

A norm test that has never been red is a claim, not a check. The suite proves
the norms hold; only this proves the suite would notice if they stopped. It is
kept rather than run once and discarded because the question comes back every
time the connection layer or the schema changes, and reconstructing the break
list from memory is how it quietly stops being asked.

It covers the four architecture norms and, since the core schema landed, the
guarantees migration 002 builds into the database itself -- the money and
temporal CHECKs, the identity indexes, and the drift guard that keeps the Core
metadata and the frozen DDL describing the same tables.

Each case restores the file it edited, including on failure.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

CONNECTION = pathlib.Path("src/bankmachine/store/connection.py")
MIGRATIONS = pathlib.Path("src/bankmachine/store/migrations/__init__.py")
DDL = pathlib.Path("src/bankmachine/store/migrations/core_schema.py")
METADATA = pathlib.Path("src/bankmachine/store/schema.py")
NORMS = "tests/store/test_connection_norms.py"
SCHEMA = "tests/store/test_schema.py"

#: (description, file, text to replace, replacement, the test that must go red)
CASES: list[tuple[str, pathlib.Path, str, str, str]] = [
    (
        "norm 1: the writer factory takes the exclusive lock",
        CONNECTION,
        "fcntl.LOCK_EX | fcntl.LOCK_NB",
        "fcntl.LOCK_SH | fcntl.LOCK_NB",
        f"{NORMS}::test_second_writer_process_refuses_while_the_first_holds_the_lock",
    ),
    (
        "norm 2 (behaviour): the read-role refusal survives PRAGMA query_only=OFF",
        CONNECTION,
        'READ_ROLE_MODE: Final = "ro"',
        'READ_ROLE_MODE: Final = "rw"',
        f"{NORMS}::test_a_read_role_handle_still_refuses_after_pragma_query_only_off",
    ),
    (
        "norm 2 (construction): the read-role mode is read-only at the file",
        CONNECTION,
        'READ_ROLE_MODE: Final = "ro"',
        'READ_ROLE_MODE: Final = "rw"',
        f"{NORMS}::test_the_read_role_opens_read_only_at_the_file",
    ),
    (
        "norm 3 (construction): the non-creating writer mode cannot create",
        CONNECTION,
        'WRITER_MODE: Final = "rw"',
        'WRITER_MODE: Final = "rwc"',
        f"{NORMS}::test_the_non_creating_writer_opens_a_mode_that_cannot_create",
    ),
    (
        "norm 3 (error quality): a missing datastore is named as missing",
        CONNECTION,
        "if not create and not config.datastore_path.exists():",
        "if False:",
        f"{NORMS}::test_a_writer_refuses_a_missing_datastore_and_creates_nothing",
    ),
    (
        "norm 4: an unrecognized schema version refuses to serve",
        CONNECTION,
        "if version != SUPPORTED_SCHEMA_VERSION:",
        "if False:",
        f"{NORMS}::test_a_reader_refuses_an_unrecognized_schema_version",
    ),
    (
        "migration atomicity: DDL and version stamp commit together",
        MIGRATIONS,
        "                step.apply(conn)\n"
        "                stamp_schema_version(conn, step.version)\n",
        "                step.apply(conn)\n"
        '            conn.execute("COMMIT")\n'
        '            conn.execute("BEGIN IMMEDIATE")\n'
        "            try:\n"
        "                stamp_schema_version(conn, step.version)\n",
        f"{NORMS}::test_a_migration_killed_between_its_ddl_and_its_stamp_leaves_nothing_healthy",
    ),
    (
        "AC-6.2: a monetary column refuses a float at the database",
        DDL,
        "CHECK (typeof(amount_minor) = 'integer')",
        "CHECK (1)",
        f"{SCHEMA}::test_a_float_amount_is_refused_by_the_database_itself",
    ),
    (
        "AC-6.4: an instant column refuses a value carrying no zone",
        DDL,
        "_V2_INSTANT = \"LIKE '%+00:00'\"",
        "_V2_INSTANT = \"LIKE '%'\"",
        f"{SCHEMA}::test_a_naive_timestamp_is_refused_by_the_database_itself",
    ),
    (
        "AC-6.4: a calendar-date column refuses an instant",
        DDL,
        "_V2_DATE = \"GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'\"",
        "_V2_DATE = \"GLOB '*'\"",
        f"{SCHEMA}::test_an_instant_in_a_calendar_date_column_is_refused",
    ),
    (
        "AC-7.4: a row cannot claim a source it has no provenance link to",
        DDL,
        "        CHECK (CASE source\n"
        "                 WHEN 'aggregator' THEN source_transaction_id IS NOT NULL",
        "        CHECK (1 OR CASE source\n"
        "                 WHEN 'aggregator' THEN source_transaction_id IS NOT NULL",
        f"{SCHEMA}::test_a_manual_row_without_an_import_to_point_at_is_refused",
    ),
    (
        "AC-1.4: one live connection per institution",
        DDL,
        "CREATE UNIQUE INDEX connections_one_live_per_institution",
        "CREATE INDEX connections_one_live_per_institution",
        f"{SCHEMA}::test_an_institution_holds_one_live_connection_at_a_time",
    ),
    (
        "AC-7.5: an imported row cannot be imported twice into one account",
        DDL,
        "CREATE UNIQUE INDEX transactions_import_identity",
        "CREATE INDEX transactions_import_identity",
        f"{SCHEMA}::test_overlapping_imports_cannot_duplicate_a_row",
    ),
    (
        "AC-3.1: a day's balance is written once, never overwritten",
        DDL,
        "        PRIMARY KEY (account_id, as_of_date)",
        "        UNIQUE (account_id, as_of_date, current_minor)",
        f"{SCHEMA}::test_a_days_balance_is_written_once_and_never_overwritten",
    ),
    (
        "AC-7.4: a balance or holding cannot claim a source it has no link to",
        DDL,
        "        PRIMARY KEY (account_id, as_of_date),\n        CHECK (CASE source",
        "        PRIMARY KEY (account_id, as_of_date),\n        CHECK (1 OR CASE source",
        f"{SCHEMA}::test_a_balance_and_a_holding_carry_a_transactions_provenance_rule",
    ),
    (
        "frozen: migration 002's DDL cannot change under a shared constraint idiom",
        DDL,
        "_V2_INSTANT = \"LIKE '%+00:00'\"",
        "_V2_INSTANT = \"LIKE '%'\"",
        f"{SCHEMA}::test_migration_002_ddl_is_frozen",
    ),
    (
        "drift: an index declared without its partial predicate is a different index",
        METADATA,
        "    sqlite_where=connections.c.retired_at.is_(None),\n",
        "",
        f"{SCHEMA}::test_the_metadata_declares_the_same_indexes_as_the_database",
    ),
    (
        "drift: the Core metadata and the frozen DDL describe the same tables",
        METADATA,
        'Column("amount_minor", MinorUnitsColumn, nullable=False),',
        'Column("amount_minor", MinorUnitsColumn, nullable=True),',
        f"{SCHEMA}::test_the_metadata_matches_the_migrated_database",
    ),
]


def _drop_bytecode() -> None:
    """Discard cached bytecode before every run.

    Several breaks here are the same length as the text they replace, and
    CPython validates a `.pyc` on (mtime, size). Two same-size writes inside one
    mtime second leave the stale bytecode valid, so the test imports the
    UNBROKEN module and reports green for a norm that was never violated. This
    harness reported exactly that before the call was added.
    """
    for cache in pathlib.Path("src").rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def main() -> int:
    survivors: list[str] = []
    for name, path, old, new, test in CASES:
        original = path.read_text(encoding="utf-8")
        if old not in original:
            print(f"SKIP   {name}\n       anchor no longer present: {old!r}")
            survivors.append(name)
            continue

        path.write_text(original.replace(old, new, 1), encoding="utf-8")
        _drop_bytecode()
        try:
            result = subprocess.run(
                ["uv", "run", "pytest", test, "-q", "--no-header", "-p", "no:cacheprovider"],
                capture_output=True,
                text=True,
                timeout=300,
            )
        finally:
            path.write_text(original, encoding="utf-8")
            _drop_bytecode()

        if result.returncode == 0:
            survivors.append(name)
            print(f"GREEN  {name}\n       the norm was broken and nothing noticed")
        else:
            print(f"RED    {name}")

    print()
    if survivors:
        print("norms whose test did NOT go red:")
        for name in survivors:
            print(f"  - {name}")
        return 1
    print(f"all {len(CASES)} norm breaks were caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())
