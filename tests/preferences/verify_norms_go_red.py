#!/usr/bin/env python3
"""Break each architecture norm in turn and confirm its test goes red.

Not collected by pytest -- it edits the source tree, so it is run deliberately:

    uv run python tests/preferences/verify_norms_go_red.py

A norm test that has never been red is a claim, not a check. The suite proves
the norms hold; only this proves the suite would notice if they stopped. It is
kept rather than run once and discarded because the question comes back every
time the connection layer changes, and reconstructing the break list from
memory is how it quietly stops being asked.

Each case restores the file it edited, including on failure.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

CONNECTION = pathlib.Path("src/bankmachine/store/connection.py")
MIGRATIONS = pathlib.Path("src/bankmachine/store/migrations/__init__.py")
NORMS = "tests/store/test_connection_norms.py"

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
