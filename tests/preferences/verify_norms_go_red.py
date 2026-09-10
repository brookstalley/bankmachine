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
metadata and the frozen DDL describing the same tables. The raw-response layer
adds four more, and `sync shell` two -- the redaction on the way out and the
snapshot release before the prompt returns. They are the same shape as the
rest: each is a refusal, so each fails silently and in the direction of looking
finished if its check ever stops firing.

The three security guarantees are here for a sharper reason than completeness.
AC-10.4's red run was originally verified by hand-planting an `httpx` import and
removing it again -- the exact one-off this file exists to replace, and one
nobody can repeat without reconstructing it from memory. AC-10.2 and the
backup-destination refusal were in the same state.

Each case restores the file it edited, including on failure.
"""

from __future__ import annotations

import ast
import pathlib
import shutil
import subprocess
import sys

CONNECTION = pathlib.Path("src/bankmachine/store/connection.py")
ENGINE = pathlib.Path("src/bankmachine/store/engine.py")
RAW = pathlib.Path("src/bankmachine/store/raw.py")
REBUILD = pathlib.Path("src/bankmachine/store/rebuild.py")
MIGRATIONS = pathlib.Path("src/bankmachine/store/migrations/__init__.py")
DDL = pathlib.Path("src/bankmachine/store/migrations/core_schema.py")
METADATA = pathlib.Path("src/bankmachine/store/schema.py")
SHELL = pathlib.Path("src/bankmachine/cli/sync.py")
SECRETS = pathlib.Path("src/bankmachine/secrets.py")
CLI_CONNECTOR = pathlib.Path("src/bankmachine/cli/connector.py")
CLI_ENROLL = pathlib.Path("src/bankmachine/cli/enroll.py")
CLI_CONNECTIONS = pathlib.Path("src/bankmachine/cli/connections.py")
CLI_MAIN = pathlib.Path("src/bankmachine/cli/__init__.py")
CONNECTOR_CLIENT = pathlib.Path("src/bankmachine/connector/plaid/client.py")
CONNECTOR_PACKAGE = pathlib.Path("src/bankmachine/connector/__init__.py")
CONNECTOR_ERRORS = pathlib.Path("src/bankmachine/connector/plaid/errors.py")
NORMS = "tests/store/test_connection_norms.py"
SCHEMA = "tests/store/test_schema.py"
SOLE_CONSTRUCTOR = "tests/preferences/test_connection_is_the_sole_constructor.py"
RAW_TESTS = "tests/store/test_raw.py"
REBUILD_TESTS = "tests/store/test_rebuild.py"
SHELL_TESTS = "tests/cli/test_sync_shell.py"
SECRETS_TESTS = "tests/test_secrets.py"
CONTAINED = "tests/preferences/test_connector_is_contained.py"
CONNECTOR_CLI_TESTS = "tests/cli/test_connector_commands.py"
CONNECTOR_TESTS = "tests/connector/test_client.py"
ERROR_TESTS = "tests/connector/test_errors.py"
ENROLLMENT_TESTS = "tests/connector/test_enrollment.py"
ENROLL_CLI_TESTS = "tests/cli/test_enroll.py"
DERIVER_TESTS = "tests/connector/test_derivers.py"
CONNECTOR_DERIVERS = pathlib.Path("src/bankmachine/connector/plaid/derivers.py")
SYNC_CURSOR_TESTS = "tests/connector/test_sync_cursor.py"
TXN_TESTS = "tests/connector/test_transaction_derivers.py"
SYNC_RUN = pathlib.Path("src/bankmachine/cli/sync_run.py")
SYNC_RUN_TESTS = "tests/cli/test_sync_run.py"
QUERY = pathlib.Path("src/bankmachine/query.py")
SIGNS = pathlib.Path("src/bankmachine/signs.py")
SIGN_TESTS = "tests/test_sign_convention.py"
ENVELOPE = pathlib.Path("src/bankmachine/envelope.py")
MCP = pathlib.Path("src/bankmachine/mcp.py")
CLIENT_GUIDE = pathlib.Path("docs/connecting-an-mcp-client.md")
MCP_TESTS = "tests/test_mcp.py"
UNSERVABLE_TESTS = "tests/test_unservable_datastore.py"
NO_STDOUT = "tests/preferences/test_the_server_never_writes_to_stdout.py"
TOOL_SURFACE = "tests/preferences/test_the_documented_tool_surface_is_the_built_one.py"
VOCABULARY = "tests/preferences/test_the_warning_vocabulary_is_closed.py"
REGISTRATION = "tests/preferences/test_an_undescribable_tool_is_refused_at_registration.py"
TRUNCATION_TESTS = "tests/test_query_truncation.py"
WINDOW_TESTS = "tests/test_query_window.py"
AGGREGATE_TESTS = "tests/test_money_summary.py"
COVERAGE_TESTS = "tests/test_account_coverage.py"
LIFECYCLE_TESTS = "tests/test_account_lifecycle.py"
PENDING_TESTS = "tests/test_pending_semantics.py"
BACKUP = pathlib.Path("src/bankmachine/store/backup.py")
BACKUP_TESTS = "tests/store/test_backup.py"
CREDENTIALS_TESTS = "tests/preferences/test_no_credentials_tracked.py"
NETWORK_TESTS = "tests/preferences/test_only_the_connector_reaches_the_network.py"

#: A datastore key is 64 hex characters, so anything of that shape is a
#: credential to AC-10.2's scanner. Repeating one word keeps it unmistakably
#: synthetic to a human while still matching, which matters because this string
#: is written into a tracked file for the length of one subprocess.
FAKE_KEY_SHAPED = "deadbeef" * 8

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
        "norm 4: a writer refuses to write into a version it does not recognize",
        CONNECTION,
        "if version != SUPPORTED_SCHEMA_VERSION:",
        "if False:",
        f"{NORMS}::test_a_writer_refuses_an_unrecognized_schema_version",
    ),
    (
        "migration atomicity: DDL and version stamp commit together",
        MIGRATIONS,
        '            conn.execute("BEGIN IMMEDIATE")\n            try:',
        "            try:",
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
        "drift: an index whose partial predicate is inverted enforces the opposite rule",
        METADATA,
        "sqlite_where=connections.c.retired_at.is_(None),",
        "sqlite_where=connections.c.retired_at.is_not(None),",
        f"{SCHEMA}::test_the_metadata_declares_the_same_indexes_as_the_database",
    ),
    (
        "drift: the Core metadata and the frozen DDL describe the same tables",
        METADATA,
        'Column("amount_minor", MinorUnitsColumn, nullable=False),',
        'Column("amount_minor", MinorUnitsColumn, nullable=True),',
        f"{SCHEMA}::test_the_metadata_matches_the_migrated_database",
    ),
    (
        "checkout: the carve-out for engine.py does not admit a parameterized connect",
        ENGINE,
        "engine.connect() as conn",
        "engine.connect(None) as conn",
        f"{SOLE_CONSTRUCTOR}::test_only_connection_py_constructs_a_connection",
    ),
    (
        "bronze: a stored body that no longer matches its digest is refused",
        RAW,
        "if recomputed != body_sha256 or len(body) != body_bytes:",
        "if False:",
        f"{RAW_TESTS}::test_a_body_that_no_longer_matches_its_digest_is_refused",
    ),
    (
        "rebuild: a table is classified by what it references, never by a column name",
        REBUILD,
        "return any(key.column is target for key in column.foreign_keys)",
        "return True",
        f"{REBUILD_TESTS}::test_neither_registry_is_classified_as_derived_from_itself",
    ),
    (
        "rebuild: content it could not reproduce is rolled back rather than committed",
        REBUILD,
        "            if report.content_changed and not report.change_was_expected:",
        "            if False:",
        f"{REBUILD_TESTS}::test_a_rebuild_that_cannot_reproduce_its_input_is_rolled_back",
    ),
    (
        "AC-10.1: a key SQLCipher would treat as a passphrase is refused",
        SECRETS,
        "if not _HEX_KEY.fullmatch(key):",
        "if False:",
        f"{SECRETS_TESTS}::test_a_key_of_the_right_length_that_is_not_hex_is_rejected",
    ),
    (
        "AC-ARCH.3: a filesystem refusal becomes a StoreError instead of escaping",
        CONNECTION,
        "    except OSError as exc:\n        raise DatastorePathUnusableError",
        "    except ValueError as exc:\n        raise DatastorePathUnusableError",
        f"{NORMS}::test_inspect_reports_an_unopenable_lock_file_rather_than_raising",
    ),
    (
        "no-fallback: an unreadable datastore is not reported as a rejected key",
        CONNECTION,
        'if getattr(exc, "sqlite_errorname", "") == "SQLITE_NOTADB":',
        "if True:",
        f"{NORMS}::test_the_measured_unreadable_edge_is_not_reported_as_a_bad_key",
    ),
    (
        "AC-10.3: the shell redacts a token on its way to the operator's terminal",
        SHELL,
        "        return redact(value)",
        "        return value",
        f"{SHELL_TESTS}::test_output_redacts_tokens_and_account_numbers_but_keeps_masks",
    ),
    (
        "AC-ARCH.6: the prompt comes back holding no snapshot, whatever was typed",
        SHELL,
        "    if not conn.in_transaction:\n        return\n",
        "    if True:\n        return\n",
        f"{SHELL_TESTS}::test_a_transaction_typed_at_the_prompt_is_released_before_the_prompt_returns",
    ),
    (
        "§9.2: the aggregator SDK stays inside connector/plaid/",
        CLI_CONNECTOR,
        "from bankmachine.config import Config",
        "import plaid  # noqa: F401\nfrom bankmachine.config import Config",
        f"{CONTAINED}::test_only_the_plaid_package_imports_the_aggregator_sdk",
    ),
    (
        "AC-5.1: the connector cannot reach the datastore, so it cannot normalize first",
        CONNECTOR_CLIENT,
        "from bankmachine.store.types import",
        (
            "from bankmachine.store.engine import writer_connection  # noqa: F401\n"
            "from bankmachine.store.types import"
        ),
        f"{CONTAINED}::test_the_connector_cannot_reach_the_datastore",
    ),
    (
        "AC-5.1: the body is archived as received, not round-tripped through a parser",
        CONNECTOR_CLIENT,
        "                _preload_content=False,",
        "                _preload_content=True,",
        f"{CONNECTOR_TESTS}::test_the_body_reaches_the_caller_byte_for_byte",
    ),
    (
        "the datastore is checked before the aggregator is reached",
        CLI_CONNECTOR,
        "    if not status.healthy:",
        "    if False:",
        f"{CONNECTOR_CLI_TESTS}::"
        "test_check_refuses_a_missing_datastore_before_reaching_the_aggregator",
    ),
    (
        "FR-4: the retry loop asks the exception rather than keeping its own list",
        CONNECTOR_ERRORS,
        "            if not type(exc).retryable:",
        "            if type(exc) is not RateLimitedError:",
        f"{ERROR_TESTS}::test_the_retry_loop_asks_the_type_rather_than_keeping_its_own_list",
    ),
    (
        "FR-4: an error type too coarse to name a remedy is not classified by it",
        CONNECTOR_ERRORS,
        '    "INSTITUTION_ERROR": InstitutionUnavailableError,',
        '    "ITEM_ERROR": ReauthRequiredError,\n'
        '    "INSTITUTION_ERROR": InstitutionUnavailableError,',
        f"{ERROR_TESTS}::test_item_error_is_not_classified_by_its_type",
    ),
    (
        "AC-2.6: a not-yet-ready backfill backs off instead of failing",
        CONNECTOR_PACKAGE,
        """    explicit that this is backoff-and-retry rather than failure.
    \"\"\"

    retryable = True""",
        """    explicit that this is backoff-and-retry rather than failure.
    \"\"\"

    retryable = False""",
        f"{ERROR_TESTS}::test_a_transient_refusal_succeeds_after_backoff",
    ),
    (
        "FR-4: a grouping error class cannot be built, so it cannot be raised",
        CONNECTOR_PACKAGE,
        "        if cls.grouping or cls is ConnectorError:",
        "        if False:",
        f"{ERROR_TESTS}::test_a_grouping_class_cannot_be_raised_because_it_cannot_be_built",
    ),
    (
        "FR-4: a type that never decided whether it retries cannot be defined",
        CONNECTOR_PACKAGE,
        '        if not grouping and "retryable" not in cls.__dict__:',
        "        if False:",
        f"{ERROR_TESTS}::test_a_type_that_never_decided_whether_it_retries_cannot_be_defined",
    ),
    (
        "AC-1.2: a link token cannot be requested without naming its history window",
        CONNECTOR_CLIENT,
        "        history_days: int,",
        "        history_days: int = 90,",
        f"{ENROLLMENT_TESTS}::test_a_link_token_cannot_be_requested_without_a_history_window",
    ),
    (
        "AC-10.1: a credential-bearing response cannot be made archivable",
        CONNECTOR_PACKAGE,
        "        if self.endpoint.issues_credential:",
        "        if False:",
        f"{ENROLLMENT_TESTS}::test_a_credential_bearing_response_cannot_be_made_archivable",
    ),
    (
        "AC-3.2: capabilities are what the connection could do, not what we asked for",
        CONNECTOR_CLIENT,
        'for field in ("products", "available_products"):',
        'for field in ("products",):',
        f"{ENROLLMENT_TESTS}::"
        "test_capabilities_answer_what_the_connection_could_do_not_what_we_asked_for",
    ),
    (
        "FR-1: a call the far end cannot absorb twice is never retried",
        CONNECTOR_PACKAGE,
        '    "/item/public_token/exchange", retry_safe=False, issues_credential=True',
        '    "/item/public_token/exchange", retry_safe=True, issues_credential=True',
        f"{ENROLLMENT_TESTS}::test_an_exchange_is_never_retried",
    ),
    (
        "AC-10.1: no enrollment credential reaches a repr",
        CONNECTOR_PACKAGE,
        '            f"AccessGrant(access_token=<redacted>, "',
        '            f"AccessGrant(access_token={self.access_token}, "',
        f"{ENROLLMENT_TESTS}::test_no_enrollment_credential_reaches_a_repr",
    ),
    (
        "the sign convention: a liability balance is stored negative",
        CONNECTOR_DERIVERS,
        '    if balance_class == "liability":',
        "    if False:",
        f"{DERIVER_TESTS}::test_a_liability_reported_positive_is_stored_negative",
    ),
    (
        "AC-6.2: money is read as text, never through a float",
        CONNECTOR_DERIVERS,
        "        parsed = json.loads(response.body, parse_float=str)",
        "        parsed = json.loads(response.body)",
        f"{DERIVER_TESTS}::test_an_amount_a_float_would_have_mangled_survives_exactly",
    ),
    (
        "the derivation seam: no deriver reads the clock",
        CONNECTOR_DERIVERS,
        '        "lifecycle_status": "active",\n        "source": "aggregator",\n'
        '        "updated_at": response.received_at,',
        '        "lifecycle_status": "active",\n        "source": "aggregator",\n'
        '        "updated_at": __import__("bankmachine.store.types", fromlist=["x"]).now_utc(),',
        f"{DERIVER_TESTS}::test_no_deriver_reads_the_clock",
    ),
    (
        "AC-3.1: a day already recorded is not overwritten by a later capture",
        CONNECTOR_DERIVERS,
        "        if (captured_at, raw_response_id or 0) <= incoming:",
        "        if False:",
        f"{DERIVER_TESTS}::test_a_second_capture_on_a_recorded_day_is_rejected_not_merged",
    ),
    (
        "the roster holds the operator's institutions, not the aggregator's catalogue",
        CONNECTOR_DERIVERS,
        "    str(INSTITUTIONS_GET): derive_nothing,",
        "    str(INSTITUTIONS_GET): derive_item,",
        f"{DERIVER_TESTS}::test_a_catalogue_page_derives_no_rows_at_all",
    ),
    (
        "FR-7: a row this deriver did not write is never replaced by one it did",
        CONNECTOR_DERIVERS,
        "        if raw_response_id is None:",
        "        if False:",
        f"{DERIVER_TESTS}::test_a_manual_row_survives_an_older_response_replayed_over_it",
    ),
    (
        "AC-10.1: a polled Link session carries a public token and is never archived",
        CONNECTOR_PACKAGE,
        'LINK_TOKEN_GET = Endpoint("/link/token/get", retry_safe=True, issues_credential=True)',
        'LINK_TOKEN_GET = Endpoint("/link/token/get", retry_safe=True)',
        f"{ENROLLMENT_TESTS}::test_a_polled_session_can_never_become_an_archivable_response",
    ),
    (
        "the MCP boundary: a date argument is parsed, never forwarded raw",
        MCP,
        # `until`, not `since`: the test's negative case is an `until` bound in
        # 2020, so dropping `since` would leave it green.
        'until = _calendar_date(arguments, "until")',
        "until = None",
        f"{MCP_TESTS}::test_a_transaction_window_filters_rather_than_failing",
    ),
    (
        "the MCP boundary: the argument bag is typed object, so narrowing cannot be skipped",
        MCP,
        "arguments: dict[str, object]) -> envelope.Answer:",
        "arguments: dict[str, Any]) -> envelope.Answer:",
        f"{MCP_TESTS}::test_the_dispatch_bag_is_typed_object_so_narrowing_cannot_be_skipped",
    ),
    (
        "AC-3.2: an item with nothing left to add is read, not refused",
        CONNECTOR_CLIENT,
        "        if not isinstance(listed, list):",
        "        if not listed:",
        f"{CONNECTOR_TESTS}::test_an_item_with_nothing_left_to_add_is_read_not_refused",
    ),
    (
        "AC-3.2: a product already initialized still counts as a capability",
        CONNECTOR_CLIENT,
        'for field in ("products", "available_products"):',
        'for field in ("available_products",):',
        f"{CONNECTOR_TESTS}::test_a_product_already_initialized_is_still_a_capability",
    ),
    (
        "AC-1.1: an unfinished session is the absence of a key, not an empty list",
        CONNECTOR_CLIENT,
        "        if sessions is None:",
        "        if False:",
        f"{ENROLLMENT_TESTS}::test_an_unfinished_session_is_reported_by_the_absence_of_a_key",
    ),
    (
        "AC-1.1: a hosted session with no URL is refused rather than returned empty",
        CONNECTOR_CLIENT,
        "        if not isinstance(hosted_url, str) or not hosted_url:",
        "        if False:",
        f"{ENROLLMENT_TESTS}::"
        "test_a_hosted_session_that_returns_no_url_is_refused_not_returned_empty",
    ),
    (
        "AC-1.2: the window that gets sent is the configured one, not a literal",
        CONNECTOR_CLIENT,
        "            transactions=LinkTokenTransactions(days_requested=history_days),",
        "            transactions=LinkTokenTransactions(days_requested=MAX_HISTORY_DAYS),",
        f"{ENROLLMENT_TESTS}::test_the_configured_window_travels_from_config_not_from_a_literal",
    ),
    (
        "AC-1.1: a finished session is found behind a newer empty one",
        CONNECTOR_CLIENT,
        "        session = finished if finished is not None else readable[-1]",
        "        session = readable[-1]",
        f"{ENROLLMENT_TESTS}::test_a_completed_session_is_found_behind_a_newer_empty_one",
    ),
    (
        "AC-1.1: a mistyped link_sessions is refused, not polled to timeout",
        CONNECTOR_CLIENT,
        "        if not isinstance(sessions, list):",
        "        if False:",
        f"{ENROLLMENT_TESTS}::test_a_mistyped_link_sessions_is_refused_not_read_as_waiting",
    ),
    (
        "AC-1.3: the token and the institution describe the same added item",
        CONNECTOR_CLIENT,
        "        added = _first_item_add_result(session)",
        "        added = _first_item_add_result(readable[0])",
        f"{ENROLLMENT_TESTS}::test_the_token_and_the_institution_come_from_the_same_item",
    ),
    (
        "AC-1.1: one unreadable session entry does not abort the poll",
        CONNECTOR_CLIENT,
        "        readable = [entry for entry in sessions if isinstance(entry, dict)]",
        "        readable = list(sessions)",
        f"{ENROLLMENT_TESTS}::test_an_unreadable_entry_does_not_strand_a_completed_session",
    ),
    (
        "AC-1.1: a response with no readable session is refused, not waited on",
        CONNECTOR_CLIENT,
        "        if not readable:",
        "        if False:",
        f"{ENROLLMENT_TESTS}::test_a_response_with_no_readable_session_at_all_is_refused",
    ),
    (
        "AC-1.1: the newest of two finished sessions is the one exchanged",
        CONNECTOR_CLIENT,
        "            (s for s in reversed(readable) if _session_public_token(s) is not None), None",
        "            (s for s in readable if _session_public_token(s) is not None), None",
        f"{ENROLLMENT_TESTS}::test_the_newest_of_two_finished_sessions_is_the_one_exchanged",
    ),
    (
        "AC-1.5: the cap refuses before a token is minted, not after",
        CLI_ENROLL,
        "    if len(live) >= config.connection_cap:",
        "    if False:",
        f"{ENROLL_CLI_TESTS}::test_the_cap_refuses_before_anything_is_minted",
    ),
    (
        "AC-1.2: enrollment asks for the CONFIGURED window, not a literal",
        CLI_ENROLL,
        "            history_days=config.history_days,",
        "            history_days=730,",
        f"{ENROLL_CLI_TESTS}::test_the_configured_window_is_what_enrollment_asks_for",
    ),
    (
        "AC-1.3a: a new connection's granted window is null, never assumed",
        CLI_ENROLL,
        "            requested_history_days=requested_history_days,\n"
        "            granted_history_days=None,",
        "            requested_history_days=requested_history_days,\n"
        "            granted_history_days=730,",
        f"{ENROLL_CLI_TESTS}::"
        "test_enrollment_records_the_requested_window_and_leaves_granted_unknown",
    ),
    (
        "AC-1.4: re-enrolling an institution updates rather than duplicating",
        CLI_ENROLL,
        "    if existing is not None:",
        "    if False:",
        f"{ENROLL_CLI_TESTS}::test_re_enrolling_the_same_institution_updates_rather_than_duplicates",
    ),
    (
        "AC-1.2: declining the window confirmation links nothing",
        CLI_ENROLL,
        "        if not _confirm_window(config, issued, assume_yes=args.yes):",
        "        if False:",
        f"{ENROLL_CLI_TESTS}::test_nothing_is_linked_when_the_window_is_not_confirmed",
    ),
    (
        "exit codes: a cap refusal is 1 (ran and found a problem), never 2",
        CLI_ENROLL,
        "EXIT_UNHEALTHY  # ran and found a problem: the roster is full",
        "EXIT_ERROR  # ran and found a problem: the roster is full",
        f"{ENROLL_CLI_TESTS}::test_the_cap_race_releases_the_item_it_just_minted",
    ),
    (
        "exit codes: an abandoned session is 1, not a broken install",
        CLI_ENROLL,
        "EXIT_UNHEALTHY  # ran and found a problem: the operator walked away",
        "EXIT_ERROR  # ran and found a problem: the operator walked away",
        f"{ENROLL_CLI_TESTS}::test_an_abandoned_session_exits_one_and_names_the_session",
    ),
    (
        "exit codes: the code comes off the exception, not a tuple of types in run()",
        CLI_MAIN,
        "        return exc.exit_code",
        "        return EXIT_UNHEALTHY",
        f"{ENROLL_CLI_TESTS}::test_a_keychain_failure_after_the_exchange_still_names_the_item",
    ),
    (
        "FR-1: the cap race releases the item it just minted",
        CLI_ENROLL,
        "        if not release_at_aggregator(config, credential_ref):",
        "        if False:",
        f"{ENROLL_CLI_TESTS}::test_the_cap_race_releases_the_item_it_just_minted",
    ),
    (
        "FR-1: a keychain failure past the exchange still names the item",
        CLI_ENROLL,
        "    try:\n        # Inside the guard",
        "    set_access_token(config, credential_ref, grant.access_token)\n"
        "    try:\n        # Inside the guard",
        f"{ENROLL_CLI_TESTS}::test_a_keychain_failure_after_the_exchange_still_names_the_item",
    ),
    (
        "exit codes: an unreadable credential does not collapse a cap refusal to 2",
        CLI_CONNECTIONS,
        "        return True\n    except SecretsError as exc:",
        "        return True\n    except AccessTokenMissingError as exc:  # noqa",
        f"{ENROLL_CLI_TESTS}::test_an_unreadable_credential_does_not_collapse_a_cap_refusal_to_two",
    ),
    (
        "AC-1.2: --timeout is floored, because it is also the URL's lifetime",
        CLI_ENROLL,
        "    if seconds < MIN_HOSTED_WAIT_SECONDS:",
        "    if False:",
        f"{ENROLL_CLI_TESTS}::test_a_timeout_below_the_floor_is_refused_before_the_aggregator",
    ),
    (
        "FR-1: a re-enrollment releases the item it superseded at all",
        CLI_ENROLL,
        "    superseded_released = True\n    if enrolled.superseded_credential_ref is not None:",
        "    superseded_released = True\n    if False:",
        f"{ENROLL_CLI_TESTS}::test_removal_happens_only_after_the_replacement_is_committed",
    ),
    (
        "AC-1.2: the hosted URL dies when this side stops waiting for it",
        CLI_ENROLL,
        "            hosted_url_lifetime_seconds=args.timeout,",
        "            hosted_url_lifetime_seconds=900,",
        f"{ENROLL_CLI_TESTS}::test_the_url_lifetime_is_the_wait_not_a_second_number",
    ),
    (
        "AC-1.4: a re-enrollment removes the item it superseded",
        CLI_ENROLL,
        "    if enrolled.superseded_credential_ref is not None:",
        "    if False:",
        f"{ENROLL_CLI_TESTS}::test_re_enrolling_removes_the_item_it_superseded",
    ),
    (
        "AC-1.4: a converging re-run against the same item removes nothing",
        CLI_ENROLL,
        "superseded_credential_ref=(previous_credential_ref if replaced_the_item else None)",
        "superseded_credential_ref=previous_credential_ref",
        f"{ENROLL_CLI_TESTS}::test_a_converging_re_run_against_the_same_item_removes_nothing",
    ),
    (
        "FR-1: a failure after the exchange names the item and the credential",
        CLI_ENROLL,
        "            source_connection_id=grant.source_connection_id,\n            credential_ref",
        '            source_connection_id="",\n            credential_ref',
        f"{ENROLL_CLI_TESTS}::test_a_failure_after_the_exchange_names_the_item_and_the_credential",
    ),
    (
        "AC-1.6: retirement removes the item at the aggregator, not just locally",
        CLI_CONNECTIONS,
        "    removed = release_at_aggregator(config, credential_ref, connection_id=connection_id)",
        "    removed = True",
        f"{ENROLL_CLI_TESTS}::test_retiring_removes_the_item_at_the_aggregator",
    ),
    (
        "AC-1.6: retiring keeps the connection row and its history",
        CLI_CONNECTIONS,
        '        .values(status="retired", retired_at=now, updated_at=now)',
        '        .values(status="retired", updated_at=now)',
        f"{ENROLL_CLI_TESTS}::test_retiring_keeps_every_row_the_connection_produced",
    ),
    (
        "AC-10.1: the credential outlives a failed removal, being the only handle left",
        CLI_CONNECTIONS,
        "        delete_access_token(config, credential_ref)",
        "        pass",
        f"{ENROLL_CLI_TESTS}::test_retiring_removes_the_item_at_the_aggregator",
    ),
    (
        "AC-2.1: an empty next_cursor never overwrites a good one",
        CONNECTOR_DERIVERS,
        "    if not isinstance(next_cursor, str) or not next_cursor:",
        "    if False:",
        f"{SYNC_CURSOR_TESTS}::test_a_not_ready_response_does_not_move_the_cursor",
    ),
    (
        "AC-2.1: a sync page archived against no connection advances nothing",
        CONNECTOR_DERIVERS,
        "    if response.connection_id is None:\n"
        "        raise DerivationError(\n"
        '            f"raw response {response.raw_response_id} ({TRANSACTIONS_SYNC})',
        "    if False:\n"
        "        raise DerivationError(\n"
        '            f"raw response {response.raw_response_id} ({TRANSACTIONS_SYNC})',
        f"{SYNC_CURSOR_TESTS}::test_a_page_archived_against_no_connection_is_refused",
    ),
    (
        "the sign convention: a purchase reported positive is stored negative",
        CONNECTOR_DERIVERS,
        "    return negate(exact)",
        "    return exact",
        f"{TXN_TESTS}::test_a_purchase_reported_positive_is_stored_negative",
    ),
    (
        "AC-2.3: a posting transaction updates the pending row, never duplicates it",
        CONNECTOR_DERIVERS,
        "    if pending_source_id is None:\n        return None",
        "    if True:\n        return None",
        f"{TXN_TESTS}::test_a_posting_transaction_updates_the_pending_row",
    ),
    (
        "AC-2.2: a removed transaction is soft-deleted, never hard-deleted",
        CONNECTOR_DERIVERS,
        "        .values(removed_at=response.received_at, updated_at=response.received_at)",
        "        .values(updated_at=response.received_at)",
        f"{TXN_TESTS}::test_a_removed_transaction_is_soft_deleted",
    ),
    (
        "AC-2.2: a transaction sent again after removal is present again",
        CONNECTOR_DERIVERS,
        "            removed_at=None,\n            **values,",
        "            **values,",
        f"{TXN_TESTS}::test_a_transaction_removed_then_sent_again_is_present_again",
    ),
    (
        "AC-6.2: a ledger amount is converted exactly or refused, never rounded",
        CONNECTOR_DERIVERS,
        "        exact = from_decimal_string(amount, exponent=minor_digits(currency))",
        '        exact = to_minor(amount, currency, "a transaction", response)',
        f"{TXN_TESTS}::test_an_amount_with_sub_cent_precision_is_refused_not_rounded",
    ),
    (
        "AC-2.1: a transaction for an unknown account is refused, not skipped",
        CONNECTOR_DERIVERS,
        "    if not isinstance(source_account_id, str) or source_account_id not in known:",
        "    if False:",
        f"{TXN_TESTS}::test_a_transaction_for_an_unknown_account_is_refused",
    ),
    (
        "AC-2.6: NOT_READY is read before has_more, or an empty sync reports success",
        SYNC_RUN,
        "                if status == NOT_READY:",
        "                if False:",
        f"{SYNC_RUN_TESTS}::test_a_not_ready_first_page_is_not_reported_as_a_successful_empty_sync",
    ),
    (
        "AC-2.1: each page resumes from the cursor the datastore committed",
        SYNC_RUN,
        "                cursor = _cursor_for(config, connection_id)",
        "                cursor = None",
        f"{SYNC_RUN_TESTS}::test_each_page_resumes_from_the_cursor_the_last_one_stored",
    ),
    (
        "AC-4.1: one connection's failure never aborts another",
        SYNC_RUN,
        "        return _degrade(config, outcome, type(exc).__name__, str(exc))",
        "        raise",
        f"{SYNC_RUN_TESTS}::test_one_connection_failing_does_not_stop_the_others",
    ),
    (
        "AC-2.1: accounts are refreshed before transactions are paged",
        SYNC_RUN,
        "            accounts_page = client.accounts_get(",
        "            accounts_page = None  # type: ignore[assignment]\n            _unused(",
        f"{SYNC_RUN_TESTS}::test_every_run_refreshes_accounts_before_paging_transactions",
    ),
    (
        "AC-1.6: a retirement whose removal never confirmed is retried, not reported done",
        CLI_CONNECTIONS,
        "        if not _credential_survives(config, row.credential_ref):",
        "        if True:",
        f"{ENROLL_CLI_TESTS}::"
        "test_retrying_a_retirement_whose_removal_never_confirmed_actually_retries",
    ),
    (
        "FR-1: an item this product could not release gets a row to retry from",
        CLI_ENROLL,
        "            _record_orphan(config, item.body, grant.source_connection_id, credential_ref)",
        "            pass",
        f"{ENROLL_CLI_TESTS}::test_an_item_orphaned_by_the_cap_race_becomes_a_retirable_connection",
    ),
    (
        "FR-1: a post-exchange failure releases the item it could not record",
        CLI_ENROLL,
        "        released = release_at_aggregator(config, credential_ref)",
        "        released = False",
        f"{ENROLL_CLI_TESTS}::test_a_failure_after_the_exchange_releases_the_item_it_could_not_record",
    ),
    (
        "exit codes: syncing an unknown connection is 1, not a silent 0",
        SYNC_RUN,
        "        if args.connection is not None:",
        "        if False:",
        f"{SYNC_RUN_TESTS}::test_syncing_an_unknown_connection_is_reported_not_silently_fine",
    ),
    (
        "AC-2.1: a run that hits its page ceiling says it stopped short",
        SYNC_RUN,
        "                outcome.stopped_short = True",
        "                outcome.stopped_short = False",
        f"{SYNC_RUN_TESTS}::test_a_run_that_hits_the_page_ceiling_says_it_stopped_short",
    ),
    (
        "AC-1.3a: the granted window is measured only once history is complete",
        SYNC_RUN,
        "    if outcome.historical_complete:",
        "    if True:",
        f"{SYNC_RUN_TESTS}::test_the_granted_window_is_not_computed_before_the_backfill_completes",
    ),
    (
        "AC-11.8: a shortfall against the requested window is recorded, not swallowed",
        SYNC_RUN,
        "    if _is_short(granted, requested):\n        # AC-11.8",
        "    if False:\n        # AC-11.8",
        f"{SYNC_RUN_TESTS}::test_a_shortfall_against_the_requested_window_is_reported",
    ),
    (
        "the MCP envelope: every answer names the environment it came from",
        ENVELOPE,
        '            "environment": self.environment,',
        '            "environment": "unknown",',
        f"{MCP_TESTS}::test_every_answer_names_the_environment_it_came_from",
    ),
    (
        "AC-11.8: a history shortfall rides the success path as a warning",
        QUERY,
        "        elif _is_short(granted, requested):",
        "        elif False:",
        f"{MCP_TESTS}::test_a_shortfall_rides_the_success_path_as_a_warning",
    ),
    (
        "AC-1.3a: an unmeasured window is not reported as no shortfall",
        QUERY,
        "        if granted is None and last_success is not None:",
        "        if False:",
        f"{MCP_TESTS}::test_an_unmeasured_window_is_reported_differently_from_no_shortfall",
    ),
    (
        # 🔴 Retargeted when the merge landed, not deleted. Both halves of this
        # case had gone stale at once -- the anchor moved under a reformat and
        # the test it named was replaced along with `spending_summary` -- and a
        # stale case is a SURVIVOR, so the harness reports it rather than
        # passing quietly. The guarantee itself did not change: outflow is the
        # negative half reported as a positive magnitude, and a predicate that
        # swept in the positive half too would make every inflow subtract from
        # the money that went out.
        "AC-4.2: outflow sums the negative half only, as a positive magnitude",
        QUERY,
        "                            (transactions.c.amount_minor < 0, "
        "-transactions.c.amount_minor), else_=0",
        "                            (transactions.c.amount_minor != 0, "
        "-transactions.c.amount_minor), else_=0",
        f"{MCP_TESTS}::test_the_aggregate_reports_both_directions_as_magnitudes",
    ),
    (
        # 🔴 The anchor is a whole statement rather than a fragment spanning a
        # formatter-chosen line break, so a reformat cannot move it out from
        # under this case.
        "MCP: nothing on the server's import path writes to stdout",
        ENVELOPE,
        "MAX_ROWS = 500",
        'MAX_ROWS = 500\nprint("answering")',
        f"{NO_STDOUT}::test_nothing_the_server_reaches_addresses_the_process_stdout",
    ),
    (
        "MCP: a caveat names a kind the vocabulary declares",
        QUERY,
        'kind="stale",',
        'kind="rate_limited",',
        f"{VOCABULARY}::test_every_caveat_names_a_kind_the_vocabulary_declares",
    ),
    (
        # Mutating the DOC rather than the code, because the drift this guard
        # exists for runs that way round: the tool is renamed and the page that
        # advertises it is not.
        "MCP: the documented tool surface is the built one",
        CLIENT_GUIDE,
        "| `list_accounts` |",
        "| `list_acounts` |",
        f"{TOOL_SURFACE}::test_every_document_names_the_tools_that_are_actually_built",
    ),
    (
        "MCP: the client's protocol version is honoured when recognized",
        MCP,
        "            if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS",
        "            if False",
        f"{MCP_TESTS}::test_the_clients_protocol_version_is_honoured_when_recognized",
    ),
    (
        "AC-2.1: the derivation transaction rolls back a cursor it already wrote",
        ENGINE,
        '        conn.exec_driver_sql("ROLLBACK")',
        '        conn.exec_driver_sql("COMMIT")',
        f"{SYNC_CURSOR_TESTS}::"
        "test_a_cursor_written_then_abandoned_does_not_survive_the_transaction",
    ),
    (
        "AC-2.1: a bounded run does not stamp last_success_at",
        SYNC_RUN,
        "    _record_success(config, connection_id, complete=not outcome.stopped_short)",
        "    _record_success(config, connection_id, complete=True)",
        f"{SYNC_RUN_TESTS}::test_a_bounded_run_does_not_claim_the_connection_is_up_to_date",
    ),
    (
        # 🔴 This case used to read "an unreadable datastore, never refuses", and
        # that label was where an over-generalization lived: AC-ARCH.3's words
        # are "empty or missing", and this case's own anchor test is named for a
        # MISSING datastore. The requirement never covered a populated store the
        # build cannot serve -- `api-contract.md` § Hard errors and
        # `architecture.md` § Direction both require that one to refuse -- but
        # the code it guarded collapsed every unreadable state into one path, so
        # the carve-out reached them all. The label now names the state it
        # actually enforces.
        "AC-ARCH.3: the MCP server reports a MISSING datastore rather than refusing",
        QUERY,
        "    if status.reason is DatastoreProblem.MISSING:\n"
        '        return status.problem or "it is missing or unreadable"',
        '    if False:\n        return status.problem or "it is missing or unreadable"',
        f"{MCP_TESTS}::test_the_server_starts_and_answers_when_the_datastore_is_missing",
    ),
    (
        # The other half of the same line, and the reason the split exists. A
        # store holding 14 accounts and 388 transactions at a schema version this
        # build does not serve answered every tool with zeroed coverage on the
        # SUCCESS path; an agent skipping `warnings` reported the household owned
        # nothing. Removing the raise restores exactly that.
        "api-contract § Hard errors: a store this build cannot serve refuses, never answers zero",
        QUERY,
        "    raise DatastoreUnservableError(_unservable_remedy(status))",
        '    return status.problem or "it is missing or unreadable"',
        f"{UNSERVABLE_TESTS}::test_every_tool_refuses_an_unservable_store",
    ),
    (
        "AC-11.8: the granted window is measured once, never re-measured",
        SYNC_RUN,
        "        if existing is not None:\n            # Already measured",
        "        if False:\n            # Already measured",
        f"{SYNC_RUN_TESTS}::test_the_granted_window_is_measured_once_and_never_re_measured",
    ),
    (
        "AC-ARCH.3: cmd_mcp starts against a missing datastore rather than refusing",
        MCP,
        # Anchored on the branch's FIRST line rather than on the statement that
        # used to follow it: what the branch does inside grew a state check, and
        # an anchor that reached past the `if` broke the moment it did. The
        # mutation still says the same thing -- refuse to start instead of
        # serving -- and everything after it in the branch becomes unreachable,
        # which parses and is what makes the case runnable.
        "    status = inspect(config)\n    if not status.healthy:\n",
        "    status = inspect(config)\n    if not status.healthy:\n        raise SystemExit(2)\n",
        f"{MCP_TESTS}::test_cmd_mcp_itself_starts_against_a_missing_datastore",
    ),
    (
        "AC-11.8: a later run still reports a shortfall it did not measure",
        SYNC_RUN,
        "            if _is_short(existing, requested):\n"
        "                outcome.history_shortfall_days = int(requested) - int(existing)",
        "            if False:\n"
        "                outcome.history_shortfall_days = int(requested) - int(existing)",
        f"{SYNC_RUN_TESTS}::test_a_later_run_still_reports_the_shortfall_it_did_not_measure",
    ),
    (
        "AC-10.1: a public token never reaches a repr",
        CONNECTOR_PACKAGE,
        '        held = "<redacted>" if self.public_token is not None else None',
        "        held = self.public_token",
        f"{ENROLLMENT_TESTS}::test_no_enrollment_credential_reaches_a_repr",
    ),
    (
        "AC-10.2: a credential-shaped literal in a tracked file is caught",
        SECRETS,
        "KEY_BYTES = 32",
        f'KEY_BYTES = 32\n_PARKED_HERE_BRIEFLY = "{FAKE_KEY_SHAPED}"',
        f"{CREDENTIALS_TESTS}::test_no_tracked_file_carries_a_token_shaped_string",
    ),
    (
        "AC-10.4: a network import outside connector/ is caught",
        QUERY,
        "from dataclasses import dataclass",
        "import ssl\n\nfrom dataclasses import dataclass",
        f"{NETWORK_TESTS}::test_nothing_outside_the_connector_can_reach_the_network",
    ),
    # 🔴 The C1 block below was written from an AUDIT of where the cases were,
    # not from a failure. 120 cases and not one touched the clamp, `truncation`,
    # the cursor condition, the window-scoped sibling or the absence rules --
    # every one of them a judgment call, all of them shipped, and none of them
    # ever proven able to fail. A guarantee whose test has never been red is a
    # claim; these are the claims that were oldest.
    (
        "C1 clamp: a start before coverage is pulled forward, not answered as asked",
        ENVELOPE,
        "    effective_since = earliest if since is None else max(since, earliest)",
        "    effective_since = earliest if since is None else since",
        f"{WINDOW_TESTS}::test_a_start_before_coverage_is_named_and_clamped",
    ),
    (
        "C1 clamp: an end past coverage is pulled back, not answered as asked",
        ENVELOPE,
        "    effective_until = covered_end if until is None else min(until, covered_end)",
        "    effective_until = covered_end if until is None else until",
        f"{WINDOW_TESTS}::test_an_end_after_today_is_named_and_clamped",
    ),
    (
        "C1 clamp: the covered end follows a row dated ahead of today",
        ENVELOPE,
        "    covered_end = today if latest is None or latest < today else latest",
        "    covered_end = today",
        f"{WINDOW_TESTS}::test_the_covered_end_follows_the_data_when_a_row_is_dated_after_today",
    ),
    (
        "C1 truncation: `truncated` is true exactly when rows are missing",
        ENVELOPE,
        "        return self.returned < self.remaining",
        "        return self.returned <= self.remaining",
        f"{TRUNCATION_TESTS}::test_an_untruncated_answer_reports_false_and_equal_counts",
    ),
    (
        "C1 truncation: the counts are floored at the rows already in hand",
        ENVELOPE,
        "        left = max(remaining, returned)",
        "        left = remaining",
        f"{TRUNCATION_TESTS}::test_a_count_that_lags_the_rows_is_reconciled_rather_than_refused",
    ),
    (
        # The break leaves the aggregate computing the roster verdicts and
        # raising nothing about them, which is the state the tool shipped in:
        # the one answer an agent is told to quote was the one carrying no
        # lifecycle caveat.
        "AC-12.8: the aggregate names the accounts that stopped being reported",
        QUERY,
        "        not_active = [entry for entry in lifecycle.values() if not entry.active]\n"
        "        uncovered = [entry for entry in _account_coverage(conn).values()",
        "        not_active: list[AccountLifecycle] = []\n"
        "        uncovered = [entry for entry in _account_coverage(conn).values()",
        f"{LIFECYCLE_TESTS}::test_a_money_summary_spanning_an_account_that_went_quiet_says_so",
    ),
    (
        "#19: the aggregate names an account that has never had a transaction",
        QUERY,
        "        uncovered = [entry for entry in _account_coverage(conn).values() "
        "if entry.uncovered]",
        "        uncovered: list[AccountCoverage] = []",
        f"{COVERAGE_TESTS}::test_summarising_money_warns_when_an_account_in_scope_has_no_coverage",
    ),
    (
        # The break makes the whole-request count take the keyset predicate the
        # page count takes, which is the meaning `matching` used to carry: the
        # figure then falls page by page and an agent quoting the last page
        # answers with the size of that page.
        "truncation: `matching` counts the whole request, cursor or no cursor",
        QUERY,
        "                        since=since, until=until, account_id=account_id, after=None",
        "                        since=since, until=until, account_id=account_id, after=after",
        f"{TRUNCATION_TESTS}::"
        "test_a_cursor_narrows_what_is_left_and_leaves_the_whole_request_count_alone",
    ),
    (
        "C1 cursor: a cursor rides an answer only when there is a next page",
        ENVELOPE,
        "        if not self.truncated or self.resume_from is None:",
        "        if self.resume_from is None:",
        f"{TRUNCATION_TESTS}::test_a_complete_answer_offers_no_cursor_to_follow",
    ),
    (
        # The break is the exact defect the code's own comment names: narrowing
        # the store-wide figure in place instead of adding a sibling beside it.
        "C1 coverage: the window-scoped count is a SIBLING, never a narrowing",
        QUERY,
        '            "transactions_in_effective_window": (',
        '            "transactions": (',
        f"{TRUNCATION_TESTS}::"
        "test_the_window_scoped_count_is_a_sibling_and_leaves_the_store_wide_one_alone",
    ),
    (
        "C1 absence: an uncapped tool carries no truncation block at all",
        ENVELOPE,
        "            **({} if self.truncation is None else "
        '{"truncation": self.truncation.to_wire()}),',
        '            **({"truncation": {}} if self.truncation is None else '
        '{"truncation": self.truncation.to_wire()}),',
        f"{TRUNCATION_TESTS}::test_an_uncapped_tool_carries_no_truncation_block",
    ),
    (
        "registration: a parameter name meaning two types is refused (#30 A3)",
        MCP,
        "        if len(set(by_tool.values())) > 1:",
        "        if False:",
        f"{REGISTRATION}::test_a_parameter_meaning_two_types_is_refused",
    ),
    (
        "registration: an optional row field is refused (norm guardrail 1)",
        MCP,
        "    optional = sorted(set(properties) - required)",
        "    optional = []",
        f"{REGISTRATION}::test_a_row_field_that_is_present_on_some_answers_is_refused",
    ),
    (
        "registration: the row check descends into a block on the row",
        MCP,
        "            _refuse_loose_object(spec, tool=tool, path=child)",
        "            pass",
        f"{REGISTRATION}::test_an_optional_field_nested_inside_a_row_block_is_refused",
    ),
    (
        "registration: a row that leaves additionalProperties open is refused",
        MCP,
        '    if schema.get("additionalProperties") is not False:',
        "    if False:",
        f"{REGISTRATION}::test_a_row_that_leaves_additional_properties_open_is_refused",
    ),
    (
        "registration: the refusals sit in the one function that hands out definitions",
        MCP,
        "    _refuse_optional_row_fields(definitions)\n    return definitions",
        "    return definitions",
        f"{REGISTRATION}::test_no_path_can_obtain_an_unvalidated_definition",
    ),
    (
        # The break is the back door the mapping's comment names: an override
        # says what a transaction was FOR, so letting it decide the flow class
        # reclassifies a transfer as spending, silently and upward.
        "flow class: the class reads the source column, never category_override",
        QUERY,
        "            transactions.c.source_category_primary.in_("
        "sorted(_INTERNAL_TRANSFER_CATEGORIES)),",
        "            transactions.c.category_override.in_(sorted(_INTERNAL_TRANSFER_CATEGORIES)),",
        f"{AGGREGATE_TESTS}::test_a_re_categorisation_cannot_move_a_transfer_into_spending",
    ),
    (
        "flow class: the three totals partition the outflow rather than sampling it",
        QUERY,
        "        entry[f\"{row['flow_class']}_outflow_minor_units\"] += "
        'int(row["outflow_minor_units"])',
        "        entry[f\"{row['flow_class']}_outflow_minor_units\"] += 0",
        f"{AGGREGATE_TESTS}::test_the_three_totals_partition_the_windows_outflow",
    ),
    (
        "flow class: an unknown category falls to external spend, never off the edge",
        QUERY,
        '        else_="external_spend",',
        '        else_="internal_transfer",',
        f"{AGGREGATE_TESTS}::test_an_unrecognised_category_falls_to_external_spend",
    ),
    (
        # 🔴 #24's whole point: with ONE connection the field cannot be shown to
        # do anything, so a mutation that hard-codes the name has to be caught by
        # a fixture holding two. The anchor ends at the next branch, which is what
        # makes it the degraded warning's own rather than any of the four
        # identical attribution lines below it.
        "#24: a warning names WHICH connection it describes, not a fixed one",
        QUERY,
        "                    connection_id=connection_id,\n"
        "                    institution=name,\n"
        "                )\n"
        "            )\n"
        "        if last_success is None:",
        "                    connection_id=connection_id,\n"
        '                    institution="First Platypus Bank",\n'
        "                )\n"
        "            )\n"
        "        if last_success is None:",
        f"{MCP_TESTS}::test_two_connections_in_the_same_state_are_still_told_apart",
    ),
    (
        "cadence: a feed posting many times a day can still be reported silent",
        QUERY,
        "            cadence = None if median is None else max(median, 1.0)",
        "            cadence = median",
        f"{COVERAGE_TESTS}::test_an_account_that_posts_many_times_a_day_can_still_go_silent",
    ),
    (
        "registration: an undescribable surface refuses at STARTUP, with a channel",
        MCP,
        "    try:\n        _tool_definitions()\n    except ToolRegistrationError:",
        "    try:\n        pass\n    except ToolRegistrationError:",
        f"{REGISTRATION}::test_an_undescribable_surface_refuses_at_startup_rather_than_mid_session",
    ),
    (
        # 🔴 The break removes a category the FIXTURE actually contains. The
        # first version of this case removed `TRANSPORTATION`, which the seed
        # never writes -- so the guard could not have noticed, the case reported
        # GREEN, and the harness caught a hole in its own new case rather than
        # in the code. A break the fixture cannot reach proves nothing.
        "flow class: a category nobody classified cannot reach the fallback unnoticed",
        QUERY,
        '    "FOOD_AND_DRINK",\n    "GENERAL_MERCHANDISE",',
        '    "GENERAL_MERCHANDISE",',
        f"{AGGREGATE_TESTS}::test_no_category_reaches_external_spend_without_a_decision",
    ),
    (
        # Derivation, not enumeration: the break removes a tool from the loop's
        # source, which is what a literal list would have let happen silently.
        "test reach: the every-tool loops derive their tools from the registry",
        MCP,
        '            "name": "get_coverage_report",',
        '            "name": "get_coverage_report_RENAMED",',
        f"{MCP_TESTS}::test_every_tool_answers_against_a_missing_datastore",
    ),
    (
        "backup destination: an existing file is never overwritten",
        BACKUP,
        "    if destination.exists():",
        "    if False:",
        f"{BACKUP_TESTS}::test_it_refuses_an_existing_destination",
    ),
    (
        # 🔴 AC-14.3's positive control, carried here so it stops depending on
        # anyone re-running it by hand. The break pushes the threshold past 1.0,
        # which no share can exceed, so a wholly inverted feed comes back
        # `consistent` -- the check still runs, still publishes a verdict, and
        # is simply never able to say the one thing it exists to say. That is
        # the shape of the two checks this repo has already shipped covering
        # nothing (#46, #47), which is why the go-red is a requirement here
        # rather than a formality.
        "sign convention: an inverted connection is reported",
        SIGNS,
        "INVERTED_ABOVE_SHARE: float = 0.5",
        "INVERTED_ABOVE_SHARE: float = 1.5",
        f"{SIGN_TESTS}::test_the_positive_control_is_reported",
    ),
    (
        # 🔴 One measurement per answer. The reader is autocommit and pins no
        # snapshot, so a second scan is a second observation -- and this answer
        # publishes a verdict per row while warning from the same data. Ignoring
        # the handed-in measurement leaves every other test green, which is why
        # this case anchors on the parameter being USED rather than on the caller
        # passing it.
        "sign convention: caveats honour the measurement they were handed",
        SIGNS,
        "    for measurement in measure(conn) if measured is None else measured:",
        "    for measurement in measure(conn):",
        f"{SIGN_TESTS}::test_caveats_uses_the_measurement_it_was_handed_rather_than_re_reading",
    ),
    (
        # 🔴 The CALLER half, and it needs its own case: mutating the call site
        # cannot redden a test that calls `caveats` directly, so the producer
        # case above is blind to a surface that stops passing the measurement
        # through. This is the rule the build plan set for the other two
        # boundary crossings -- anchor on the call, because a producer that
        # works and a surface that never invokes it look identical from the
        # producer's own tests.
        "sign convention: pipeline_health passes its measurement rather than re-reading",
        QUERY,
        # Re-anchored 2026-09-09: adding the empty-roster finding beside this
        # call reflowed `extra_caveats=` onto its own line, so the old
        # whole-argument anchor stopped matching. The mutation is unchanged --
        # drop `measured=` and the surface re-reads what it was handed.
        "                signs.caveats(conn, measured=measured)",
        "                signs.caveats(conn)",
        f"{SIGN_TESTS}::test_pipeline_health_measures_once_even_when_a_second_scan_would_differ",
    ),
    (
        # 🔴 The same property one surface over: the envelope's non-active
        # figures and the rows they qualify have to be ONE observation. The
        # reader releases its snapshot per statement, so a second walk is a
        # second observation and the answer can contradict itself about one
        # account. Anchors on the CALL, for the reason the two above it do.
        "AC-12.8: an answer derives the lifecycle once, not once per consumer",
        QUERY,
        "    coverage = _coverage(conn, lifecycle=lifecycle)",
        "    coverage = _coverage(conn)",
        f"{LIFECYCLE_TESTS}::"
        "test_one_answer_derives_the_lifecycle_once_even_when_a_second_walk_would_differ",
    ),
    # -- FR-9 (#40): account lifecycle -------------------------------------
    #
    # 🔴 Four cases rather than one, because #40 is a population path, a read
    # path and a treatment ruling, and each fails silently in its own direction.
    # A break in the deriver leaves every account permanently current; a break in
    # the read path leaves the state unreachable; a break in the coverage report
    # turns a closure back into a permanent finding; a break in the envelope
    # figure leaves the count legible to nobody. All four look finished.
    (
        # 🔴 Anchored on the ORDER-INDEPENDENCE property, not on the drop test
        # beside it. The drop case never reaches the update arm for the account
        # that dropped -- nothing writes its row, which is the whole mechanism --
        # so it stays green with the maximum deleted. Measured: it did.
        "AC-12.4: when an account was last listed is a maximum, not the latest replay",
        CONNECTOR_DERIVERS,
        "                seen_date if last_seen_date is None else max(last_seen_date, seen_date)",
        "                seen_date",
        f"{DERIVER_TESTS}::"
        "test_when_an_account_was_last_listed_is_a_maximum_so_order_cannot_matter",
    ),
    (
        "AC-12.9: an account absent from the latest roster is no_longer_reported",
        QUERY,
        "        elif last_seen is None or last_seen < roster:",
        "        elif False:",
        f"{LIFECYCLE_TESTS}::"
        "test_an_account_the_roster_stopped_listing_is_reported_no_longer_reported",
    ),
    (
        # 🔴 A warning nothing can ever clear is the "true and useless" defect
        # the two warning tuples exist to prevent. A retired connection has no
        # next roster read, so the condition ends only by never starting.
        # Anchors on the one shared producer both emitters read.
        "AC-12.5a: a retired connection raises no empty-roster warning it could never clear",
        QUERY,
        "    observed = {cid: date for cid, date in observed.items() if cid in live}",
        "    observed = dict(observed)",
        f"{LIFECYCLE_TESTS}::"
        "test_a_retired_connection_raises_no_empty_roster_warning_it_could_never_clear",
    ),
    (
        # 🔴 AC-12.6: `closed` is the operator's own declaration and is no
        # evidence about a roster. Flattening it into the empty-roster caveat
        # asks an operator to re-confirm a closure they made themselves.
        "AC-12.6: the empty-roster caveat asserts no verdict over the accounts it names",
        QUERY,
        'f"accounts at all. Account(s) "',
        'f"accounts at all, so account(s) are all marked no longer reported: "',
        f"{LIFECYCLE_TESTS}::"
        "test_an_empty_roster_names_an_operator_closed_account_without_assigning_it_the_verdict",
    ),
    (
        # 🔴 AC-5.3: losslessness is only well-defined against a recorded
        # version. A deriver that fills a new column without a bump makes
        # `store rebuild` refuse and roll back -- which is the remedy the
        # upgrade procedure prescribes for the connection that never syncs
        # again, so the documented fix would fail on the store it is for.
        "AC-5.3: a newly-populated column bumps the derivation version",
        pathlib.Path("src/bankmachine/store/derivation.py"),
        "DERIVATION_VERSION = 3",
        "DERIVATION_VERSION = 2",
        f"{LIFECYCLE_TESTS}::"
        "test_a_store_derived_before_the_roster_column_rebuilds_instead_of_rolling_back",
    ),
    (
        "AC-12.7: a non-active account's silence is closure, not a coverage finding",
        QUERY,
        "                        False if ratio is None or not lifecycle[account_id].active "
        "else ratio > 1.0",
        "                        False if ratio is None else ratio > 1.0",
        f"{LIFECYCLE_TESTS}::"
        "test_a_non_active_accounts_silence_is_not_reported_as_a_coverage_finding",
    ),
    (
        "AC-12.8: the account count says how many of itself are not active",
        QUERY,
        '        "accounts_not_active": len(not_active),',
        '        "accounts_not_active": 0,',
        f"{LIFECYCLE_TESTS}::"
        "test_the_envelope_counts_every_account_and_says_how_many_are_not_active",
    ),
    (
        # 🔴 The MAGNITUDE, not the flag -- and it is a separate case because the
        # norm it certifies names it separately. `api-contract.md` § Direction's
        # fifth norm flips to steady-state only once "the guard must assert the
        # FIGURE and not only the flag, and be seen red with the magnitude
        # removed". The count case above mutates the flag and leaves the figure
        # untouched, so on its own it cannot discharge that condition. Emptying
        # the figure is the failure the norm actually fears: a consumer told
        # something is included and handed nothing to subtract.
        "AC-12.8: the flagged magnitude is reported, not just the flag",
        QUERY,
        "    if not account_ids:\n        return []",
        "    if True:\n        return []",
        f"{LIFECYCLE_TESTS}::test_the_magnitude_is_signed_and_grouped_by_currency",
    ),
    # -- #51: the roster observation, RECORDED rather than derived ----------
    #
    # 🔴 Eight cases rather than two, because the amendment of 2026-09-09 is a
    # write path, a read path and two emitters on two surfaces, and each fails
    # silently in its own direction. The write path failing leaves nothing to
    # measure against; the read path failing puts the N=1 blind spot back; an
    # emitter's producer failing publishes frozen balances with no way to tell a
    # broken feed from a household closing its accounts; and an emitter's CALL
    # SITE failing ships a finished, correct, tested producer that nothing
    # invokes. The last is why four of these anchor on a call rather than on the
    # code it reaches: a test that exercised the producer directly would stay
    # green with the call site reverted.
    (
        # 🔴 Guarded on the roster being non-empty, which is the regression, not
        # deleted outright: the non-empty case must stay GREEN, or the case
        # proves only that something records an observation somewhere.
        "AC-12.5a: an EMPTY roster is a successful observation and is recorded as one",
        CONNECTOR_DERIVERS,
        "    _record_roster_observation(conn, response)",
        "    _record_roster_observation(conn, response) if listed else None",
        f"{DERIVER_TESTS}::test_a_roster_that_lists_nothing_still_records_the_observation",
    ),
    (
        # The connection half of the property the account half already carries.
        # An observation that moves BACKWARDS on a replay puts accounts a later
        # roster listed behind it -- a fabricated closure produced by replay
        # order alone.
        "AC-12.4: a connection's roster observation is a maximum, not the latest replay",
        CONNECTOR_DERIVERS,
        "                observed if recorded is None else max(calendar_date(recorded), observed)",
        "                observed",
        f"{DERIVER_TESTS}::"
        "test_the_roster_observation_is_a_maximum_so_a_replay_cannot_move_it_back",
    ),
    (
        # 🔴 The break is the design the amendment REVERSED, written out in
        # full, rather than a constant that makes the read return nothing. The
        # regression this case exists to catch is somebody restoring the derived
        # maximum because it needs no column and cannot disagree with its rows --
        # and under it every other lifecycle test still passes, which is exactly
        # why the N=1 case had to be written before it could be caught.
        "AC-12.5: absence is measured against the RECORDED observation, at N=1 too",
        QUERY,
        "            select(connections.c.connection_id, connections.c.roster_observed_date)",
        "            select(accounts.c.connection_id, func.max(accounts.c.last_seen_date, "
        "type_=accounts.c.last_seen_date.type)).where(accounts.c.connection_id.is_not(None))"
        ".group_by(accounts.c.connection_id)",
        f"{LIFECYCLE_TESTS}::test_a_single_account_connections_only_account_is_reported_absent",
    ),
    (
        # 🔴 A separate case because the norm it certifies names the figure
        # separately. The amendment CHANGES the population the flagged magnitude
        # is computed over, and a verdict that moved without the figure moving
        # leaves a reader told a total includes something and handed nothing to
        # subtract. The case above mutates the same line and asserts the
        # verdict; this one asserts the money.
        "AC-12.8: the newly-absent only account reaches the flagged magnitude",
        QUERY,
        "            select(connections.c.connection_id, connections.c.roster_observed_date)",
        "            select(accounts.c.connection_id, func.max(accounts.c.last_seen_date, "
        "type_=accounts.c.last_seen_date.type)).where(accounts.c.connection_id.is_not(None))"
        ".group_by(accounts.c.connection_id)",
        f"{LIFECYCLE_TESTS}::"
        "test_the_only_account_of_a_shrunk_connection_reaches_the_flagged_magnitude",
    ),
    (
        "AC-12.5a: the answer that draws on an empty roster says so (producer)",
        QUERY,
        "        if entry.roster_observed_empty and entry.connection_id is not None:",
        "        if False:",
        f"{LIFECYCLE_TESTS}::test_an_empty_roster_says_which_connection_and_which_accounts",
    ),
    (
        # 🔴 Anchored on the CALL, and this is the half that fails invisibly.
        # A producer with no caller is a finished, tested, correct function that
        # never runs, and every test written against it stays green. The anchor
        # carries the two lines after it because the same term appears at three
        # call sites and the harness replaces only the first.
        "AC-12.5a: the answer that draws on an empty roster says so (call site)",
        QUERY,
        "                + _roster_observed_empty_caveat(not_active)\n"
        "            ),\n            lifecycle=lifecycle,",
        "                + []\n            ),\n            lifecycle=lifecycle,",
        f"{LIFECYCLE_TESTS}::test_an_empty_roster_says_which_connection_and_which_accounts",
    ),
    (
        # A separate producer from the one above rather than the same one
        # reused, because the trigger differs: a health check's own envelope is
        # not a request scope, and the connection whose roster has come back
        # empty from the first read holds no account for a request scope to
        # contain.
        "AC-12.5a: get_pipeline_health names the connection whose roster was empty (producer)",
        QUERY,
        '        if int(row["connection_id"]) in empty',
        "        if False",
        f"{LIFECYCLE_TESTS}::"
        "test_the_health_surface_names_a_connection_whose_roster_came_back_empty",
    ),
    (
        # The call-site half on the second surface, for the reason the first
        # surface has one.
        "AC-12.5a: get_pipeline_health names the connection whose roster was empty (call site)",
        QUERY,
        "                + _roster_observed_empty_findings(rows, "
        "_connections_with_an_empty_roster(conn))",
        "                + []",
        f"{LIFECYCLE_TESTS}::"
        "test_the_health_surface_names_a_connection_whose_roster_came_back_empty",
    ),
    # ----------------------------------------------------------------------
    # Pending-transaction semantics on the read path (#22, AC-13.1-13.7). No
    # pending row had ever reached `query.py`, so every guarantee below is one
    # nothing could previously have gone red on.
    # ----------------------------------------------------------------------
    (
        # The break reads the SETTLED half instead, so the disclosure is a real
        # number computed the wrong way round rather than an obvious zero -- a
        # constant would be caught by inspection, a mirrored predicate would not.
        "AC-13.1: an aggregate states how much of itself is an unsettled hold",
        QUERY,
        "                        case((transactions.c.pending == 1, "
        "transactions.c.amount_minor), else_=0)",
        "                        case((transactions.c.pending == 0, "
        "transactions.c.amount_minor), else_=0)",
        f"{PENDING_TESTS}::test_a_hold_is_counted_and_its_magnitude_stated",
    ),
    (
        "AC-13.1: an answer that drew on a hold says so on the success path",
        QUERY,
        "    if total == 0:\n        return []",
        "    if True:\n        return []",
        f"{PENDING_TESTS}::test_a_transaction_page_holding_a_hold_says_so",
    ),
    (
        # Without the `pending` half, an ordinary withdrawal of a row that had
        # already settled is reported as a hold that expired -- the total is
        # explained by the wrong cause, which is worse than unexplained.
        "AC-13.4: an expired hold is one that never posted, not any removed row",
        QUERY,
        "            [transactions.c.removed_at.is_not(None), transactions.c.pending == 1],",
        "            [transactions.c.removed_at.is_not(None)],",
        f"{PENDING_TESTS}"
        "::test_a_settled_row_that_was_later_withdrawn_is_not_reported_as_an_expired_hold",
    ),
    (
        # The reader still returns the right rows without the null test -- it is
        # the PLAN that changes, which is the whole of AC-13.6: an index nothing
        # plans against is the dead weight the criterion exists to remove.
        "AC-13.6: the pending-link reader asks in the shape the index serves",
        QUERY,
        "                transactions.c.source_pending_transaction_id.is_not(None),\n"
        "                transactions.c.pending == 0,",
        "                transactions.c.pending == 0,",
        f"{PENDING_TESTS}::test_the_pending_link_index_serves_the_reader_that_exists",
    ),
    (
        "AC-13.5: a hold past the declared threshold is reported as maybe stranded",
        QUERY,
        "    return calendar_date(today - timedelta(days=STRANDED_HOLD_AFTER_DAYS))",
        "    return calendar_date(today - timedelta(days=100 * STRANDED_HOLD_AFTER_DAYS))",
        f"{PENDING_TESTS}::test_a_hold_older_than_the_declared_threshold_is_reported",
    ),
    (
        # The identity survives and the amount does not, which is exactly the
        # settlement the two older dedup cases cannot see: they send the same
        # figure on the hold and on the posting, so both stay green here.
        "AC-13.2: a settlement carries the settled amount, not the hold's",
        CONNECTOR_DERIVERS,
        "            removed_at=None,\n            **values,",
        "            removed_at=None,\n"
        '            **{k: v for k, v in values.items() if k != "amount_minor"},',
        f"{TXN_TESTS}::test_a_settlement_that_changes_the_amount_updates_it_in_place",
    ),
    (
        # Matching a removal on the hold id would soft-delete the row that just
        # posted, so the purchase leaves every total: an UNDERCOUNT, which is the
        # direction that gets believed.
        "AC-13.3: a removal names a row's own id, never the hold it replaced",
        CONNECTOR_DERIVERS,
        "            transactions.c.source_transaction_id == source_transaction_id,\n"
        "            transactions.c.removed_at.is_(None),",
        "            transactions.c.source_pending_transaction_id == source_transaction_id,\n"
        "            transactions.c.removed_at.is_(None),",
        f"{TXN_TESTS}::test_a_hold_removed_in_the_same_page_as_its_posting_leaves_one_row",
    ),
    # ----------------------------------------------------------------------
    # 🔴 The two criteria that crossed a delegation boundary.
    #
    # Both producers were built by one agent and both call sites live in
    # functions another owned, so the call between them was written by neither
    # and had no test on either side of it. A producer returning the right
    # answer and a surface that never calls it are indistinguishable from the
    # producer's own tests -- which is the state the tree was actually in --
    # so these anchor on the CALL, not on the producer.
    # ----------------------------------------------------------------------
    (
        "AC-14.5: an aggregate over an inverted connection says so",
        QUERY,
        "                + _pending_caveat(pending)\n"
        "                + signs.caveats(conn, since=since, until=until)",
        "                + _pending_caveat(pending)",
        f"{SIGN_TESTS}::test_an_aggregate_over_a_flagged_connection_says_so",
    ),
    (
        "AC-13.5: a stranded hold reaches the verification surface",
        QUERY,
        '                    "stranded_holds": len(stranded_by_account.get(account_id, ())),',
        '                    "stranded_holds": 0,',
        f"{PENDING_TESTS}::"
        "test_the_coverage_report_names_a_stranded_hold_on_the_account_holding_it",
    ),
    (
        # The suppression is a separate failure from the wiring: this one leaves
        # the feature working and hands a closed account a hold nobody can clear.
        "AC-13.5/AC-12.7: a closed account is not asked to pursue a hold",
        QUERY,
        "                        active=lifecycle[account_id].active,",
        "                        active=True,",
        f"{PENDING_TESTS}::"
        "test_a_stranded_hold_on_a_non_active_account_is_counted_but_not_asked_about",
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

        mutated = original.replace(old, new, 1)
        # 🔴 The mutation must still be valid Python, and this is not a nicety.
        # `main` judges a case by pytest's exit code, and pytest exits non-zero
        # on a COLLECTION error exactly as it does on a failure -- so a mutation
        # that does not parse prints RED without ever exercising the guarantee,
        # and the case passes forever no matter what the code does. That is the
        # same shape as every defect this harness exists to catch, in the harness
        # itself. Checked before the run rather than inferred from its output,
        # because by then the two are indistinguishable.
        if path.suffix == ".py":
            try:
                ast.parse(mutated)
            except SyntaxError as exc:
                print(
                    f"INVALID {name}\n"
                    f"       the mutation does not parse ({exc.msg} at line {exc.lineno}), so "
                    f"pytest would fail to COLLECT and the case would report RED without "
                    f"testing anything"
                )
                survivors.append(name)
                continue

        path.write_text(mutated, encoding="utf-8")
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
