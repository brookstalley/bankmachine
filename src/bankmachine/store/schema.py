"""SQLAlchemy Core table metadata -- the schema as the query builder sees it.

This is the typed half of a deliberate pair. The other half is the frozen DDL in
`store/migrations/core_schema.py`, which is what actually creates the tables.
Neither generates the other, because a metadata definition that emits its own
migrations drifts in silence: both sides move in the same edit and nothing is
left over to disagree. Written independently, they can be compared, and
`tests/store/test_schema.py` compares them column for column on every run.

Nothing here opens a connection or executes anything. It is a description, used
to build queries against handles that `store/connection.py` constructed.

Read `core_schema.py`'s module docstring before adding a column: it carries the
sign convention, the provenance rules, and why the money and date constraints
are in the database rather than only in the code.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import (
    Column,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
)

from bankmachine.store.types import CalendarDateColumn, MinorUnitsColumn, UtcInstantColumn

metadata = MetaData()

derivation_versions = Table(
    "derivation_versions",
    metadata,
    Column("derivation_version_id", Integer, primary_key=True),
    Column("version", Integer, nullable=False, unique=True),
    Column("description", Text, nullable=False),
    Column("first_used_at", UtcInstantColumn, nullable=False),
)

institutions = Table(
    "institutions",
    metadata,
    Column("institution_id", Integer, primary_key=True),
    Column("source_institution_id", Text, nullable=True, unique=True),
    Column("name", Text, nullable=False),
    Column("first_seen_at", UtcInstantColumn, nullable=False),
    Column("last_seen_at", UtcInstantColumn, nullable=False),
)

connections = Table(
    "connections",
    metadata,
    Column("connection_id", Integer, primary_key=True),
    Column(
        "institution_id",
        Integer,
        ForeignKey("institutions.institution_id"),
        nullable=False,
    ),
    Column("source_connection_id", Text, nullable=False, unique=True),
    Column("credential_ref", Text, nullable=False),
    Column("capabilities", Text, nullable=False, server_default="[]"),
    Column("requested_history_days", Integer, nullable=True),
    Column("granted_history_days", Integer, nullable=True),
    Column("status", Text, nullable=False),
    Column("last_success_at", UtcInstantColumn, nullable=True),
    Column("last_error_code", Text, nullable=True),
    Column("last_error_at", UtcInstantColumn, nullable=True),
    Column("enrolled_at", UtcInstantColumn, nullable=False),
    Column("retired_at", UtcInstantColumn, nullable=True),
    Column("created_at", UtcInstantColumn, nullable=False),
    Column("updated_at", UtcInstantColumn, nullable=False),
)

Index(
    "connections_one_live_per_institution",
    connections.c.institution_id,
    unique=True,
    sqlite_where=connections.c.retired_at.is_(None),
)

accounts = Table(
    "accounts",
    metadata,
    Column("account_id", Integer, primary_key=True),
    Column(
        "institution_id",
        Integer,
        ForeignKey("institutions.institution_id"),
        nullable=False,
    ),
    Column("connection_id", Integer, ForeignKey("connections.connection_id"), nullable=True),
    Column("source_account_id", Text, nullable=True),
    Column("source_persistent_account_id", Text, nullable=True),
    Column("name", Text, nullable=False),
    Column("official_name", Text, nullable=True),
    Column("mask", Text, nullable=True),
    Column("account_type", Text, nullable=False),
    Column("account_subtype", Text, nullable=True),
    Column("balance_class", Text, nullable=False),
    Column("currency", Text, nullable=False),
    Column("lifecycle_status", Text, nullable=False),
    Column("opened_date", CalendarDateColumn, nullable=True),
    Column("first_seen_date", CalendarDateColumn, nullable=False),
    Column("closed_date", CalendarDateColumn, nullable=True),
    Column("source", Text, nullable=False),
    Column("created_at", UtcInstantColumn, nullable=False),
    Column("updated_at", UtcInstantColumn, nullable=False),
    # 🔴 Last, not beside `first_seen_date` where it reads better. It arrived in
    # migration 003 through `ALTER TABLE ... ADD COLUMN`, which appends, and the
    # drift guard compares this list against `PRAGMA table_info` *in order*.
    # Moving it up would make two correct descriptions of one correct database
    # disagree.
    #
    # AC-12.4: the date this account was last listed on a successful roster
    # observation, taken as a monotone MAXIMUM the way `first_seen_date` is taken
    # as a minimum -- which is what makes an archive replay order-independent.
    # 🔴 Nullable, and the null MEANS "no roster observation is recorded for this
    # account" -- `query._account_lifecycle` reads it as exactly that, never as a
    # date. Migration 003 backfilled nothing because there is nothing honest to
    # backfill with: the correct value is the connection's last successful roster
    # observation, which is the record this column exists because nothing kept.
    Column("last_seen_date", CalendarDateColumn, nullable=True),
)

Index(
    "accounts_source_identity",
    accounts.c.connection_id,
    accounts.c.source_account_id,
    unique=True,
    sqlite_where=accounts.c.source_account_id.is_not(None),
)
Index("accounts_by_institution", accounts.c.institution_id)

raw_responses = Table(
    "raw_responses",
    metadata,
    Column("raw_response_id", Integer, primary_key=True),
    Column("connection_id", Integer, ForeignKey("connections.connection_id"), nullable=True),
    Column("endpoint", Text, nullable=False),
    Column("received_at", UtcInstantColumn, nullable=False),
    Column("body_gzip", LargeBinary, nullable=False),
    Column("body_sha256", Text, nullable=False),
    Column("body_bytes", Integer, nullable=False),
    Column("request_context", Text, nullable=True),
)

Index("raw_responses_by_connection", raw_responses.c.connection_id, raw_responses.c.received_at)

manual_imports = Table(
    "manual_imports",
    metadata,
    Column("manual_import_id", Integer, primary_key=True),
    Column("account_id", Integer, ForeignKey("accounts.account_id"), nullable=False),
    Column("adapter", Text, nullable=False),
    Column("source_name", Text, nullable=False),
    Column("file_sha256", Text, nullable=False),
    Column("file_bytes", Integer, nullable=False),
    Column("imported_at", UtcInstantColumn, nullable=False),
    Column("rows_seen", Integer, nullable=False),
    Column("rows_applied", Integer, nullable=False),
)

Index(
    "manual_imports_file_identity",
    manual_imports.c.account_id,
    manual_imports.c.file_sha256,
    unique=True,
)

transactions = Table(
    "transactions",
    metadata,
    Column("transaction_id", Integer, primary_key=True),
    Column("account_id", Integer, ForeignKey("accounts.account_id"), nullable=False),
    Column("source_transaction_id", Text, nullable=True),
    Column("source_pending_transaction_id", Text, nullable=True),
    Column("pending", Integer, nullable=False),
    Column("posted_date", CalendarDateColumn, nullable=False),
    Column("authorized_date", CalendarDateColumn, nullable=True),
    Column("amount_minor", MinorUnitsColumn, nullable=False),
    Column("currency", Text, nullable=False),
    Column("description", Text, nullable=False),
    Column("merchant_name", Text, nullable=True),
    Column("source_category_primary", Text, nullable=True),
    Column("source_category_detailed", Text, nullable=True),
    Column("category_override", Text, nullable=True),
    Column("source", Text, nullable=False),
    Column("raw_response_id", Integer, ForeignKey("raw_responses.raw_response_id"), nullable=True),
    Column(
        "manual_import_id",
        Integer,
        ForeignKey("manual_imports.manual_import_id"),
        nullable=True,
    ),
    Column(
        "derivation_version_id",
        Integer,
        ForeignKey("derivation_versions.derivation_version_id"),
        nullable=False,
    ),
    Column("import_fingerprint", Text, nullable=True),
    Column("removed_at", UtcInstantColumn, nullable=True),
    Column("first_seen_at", UtcInstantColumn, nullable=False),
    Column("updated_at", UtcInstantColumn, nullable=False),
)

Index(
    "transactions_source_identity",
    transactions.c.account_id,
    transactions.c.source_transaction_id,
    unique=True,
    sqlite_where=transactions.c.source_transaction_id.is_not(None),
)
Index(
    "transactions_import_identity",
    transactions.c.account_id,
    transactions.c.import_fingerprint,
    unique=True,
    sqlite_where=transactions.c.import_fingerprint.is_not(None),
)
Index("transactions_by_account_date", transactions.c.account_id, transactions.c.posted_date)
# 🔴 **This index serves the REVERSE lookup, and does not serve AC-2.3's match.**
# The two are easy to confuse and the distinction is the whole of AC-13.6. A
# posting transaction finds the hold it replaces by that hold's OWN identifier --
# `_existing_transaction` compares the incoming `pending_transaction_id` against
# the pending row's `source_transaction_id`, which is served by
# `transactions_source_identity` -- because a hold answers to its own id right up
# until it posts.
#
# What this index answers is the other direction: given a hold's id, which posted
# row settled out of it, and more usefully in the aggregate, which rows in a
# window arrived by replacing a hold. `query._hold_transitions` is that reader
# (AC-13.4), and asking for it as `source_pending_transaction_id IS NOT NULL`
# lets SQLite answer from this partial index, which holds only the small
# minority of rows that carry a link, rather than scanning every row in the
# window.
Index(
    "transactions_pending_link",
    transactions.c.account_id,
    transactions.c.source_pending_transaction_id,
    sqlite_where=transactions.c.source_pending_transaction_id.is_not(None),
)

balances_daily = Table(
    "balances_daily",
    metadata,
    Column(
        "account_id",
        Integer,
        ForeignKey("accounts.account_id"),
        primary_key=True,
    ),
    Column("as_of_date", CalendarDateColumn, primary_key=True),
    Column("current_minor", MinorUnitsColumn, nullable=False),
    Column("available_minor", MinorUnitsColumn, nullable=True),
    Column("limit_minor", MinorUnitsColumn, nullable=True),
    Column("currency", Text, nullable=False),
    Column("captured_at", UtcInstantColumn, nullable=False),
    Column("source", Text, nullable=False),
    Column("raw_response_id", Integer, ForeignKey("raw_responses.raw_response_id"), nullable=True),
    Column(
        "manual_import_id",
        Integer,
        ForeignKey("manual_imports.manual_import_id"),
        nullable=True,
    ),
    Column(
        "derivation_version_id",
        Integer,
        ForeignKey("derivation_versions.derivation_version_id"),
        nullable=False,
    ),
)

securities = Table(
    "securities",
    metadata,
    Column("security_id", Integer, primary_key=True),
    Column("source_security_id", Text, nullable=True, unique=True),
    Column("name", Text, nullable=True),
    Column("ticker", Text, nullable=True),
    Column("cusip", Text, nullable=True),
    Column("isin", Text, nullable=True),
    Column("security_type", Text, nullable=True),
    Column("currency", Text, nullable=True),
    Column("close_price_minor", MinorUnitsColumn, nullable=True),
    Column("close_price_as_of", CalendarDateColumn, nullable=True),
    Column(
        "derivation_version_id",
        Integer,
        ForeignKey("derivation_versions.derivation_version_id"),
        nullable=False,
    ),
    Column("created_at", UtcInstantColumn, nullable=False),
    Column("updated_at", UtcInstantColumn, nullable=False),
)

holdings = Table(
    "holdings",
    metadata,
    Column("account_id", Integer, ForeignKey("accounts.account_id"), primary_key=True),
    Column("security_id", Integer, ForeignKey("securities.security_id"), primary_key=True),
    Column("as_of_date", CalendarDateColumn, primary_key=True),
    Column("quantity", Text, nullable=False),
    Column("cost_basis_minor", MinorUnitsColumn, nullable=True),
    Column("market_value_minor", MinorUnitsColumn, nullable=False),
    Column("currency", Text, nullable=False),
    Column("captured_at", UtcInstantColumn, nullable=False),
    Column("source", Text, nullable=False),
    Column("raw_response_id", Integer, ForeignKey("raw_responses.raw_response_id"), nullable=True),
    Column(
        "manual_import_id",
        Integer,
        ForeignKey("manual_imports.manual_import_id"),
        nullable=True,
    ),
    Column(
        "derivation_version_id",
        Integer,
        ForeignKey("derivation_versions.derivation_version_id"),
        nullable=False,
    ),
)

investment_transactions = Table(
    "investment_transactions",
    metadata,
    Column("investment_transaction_id", Integer, primary_key=True),
    Column("account_id", Integer, ForeignKey("accounts.account_id"), nullable=False),
    Column("security_id", Integer, ForeignKey("securities.security_id"), nullable=True),
    Column("source_investment_transaction_id", Text, nullable=True),
    Column("trade_date", CalendarDateColumn, nullable=False),
    Column("settlement_date", CalendarDateColumn, nullable=True),
    Column("investment_type", Text, nullable=False),
    Column("investment_subtype", Text, nullable=True),
    Column("quantity", Text, nullable=True),
    Column("price_minor", MinorUnitsColumn, nullable=True),
    Column("fees_minor", MinorUnitsColumn, nullable=True),
    Column("amount_minor", MinorUnitsColumn, nullable=False),
    Column("currency", Text, nullable=False),
    Column("description", Text, nullable=True),
    Column("source", Text, nullable=False),
    Column("raw_response_id", Integer, ForeignKey("raw_responses.raw_response_id"), nullable=True),
    Column(
        "manual_import_id",
        Integer,
        ForeignKey("manual_imports.manual_import_id"),
        nullable=True,
    ),
    Column(
        "derivation_version_id",
        Integer,
        ForeignKey("derivation_versions.derivation_version_id"),
        nullable=False,
    ),
    Column("import_fingerprint", Text, nullable=True),
    Column("removed_at", UtcInstantColumn, nullable=True),
    Column("first_seen_at", UtcInstantColumn, nullable=False),
    Column("updated_at", UtcInstantColumn, nullable=False),
)

Index(
    "investment_transactions_source_identity",
    investment_transactions.c.account_id,
    investment_transactions.c.source_investment_transaction_id,
    unique=True,
    sqlite_where=investment_transactions.c.source_investment_transaction_id.is_not(None),
)
Index(
    "investment_transactions_import_identity",
    investment_transactions.c.account_id,
    investment_transactions.c.import_fingerprint,
    unique=True,
    sqlite_where=investment_transactions.c.import_fingerprint.is_not(None),
)
Index(
    "investment_transactions_by_account_date",
    investment_transactions.c.account_id,
    investment_transactions.c.trade_date,
)

#: The `sync_state.domain` a transaction cursor is keyed under. One domain today;
#: the column exists because balances and holdings advance on their own schedules
#: and a single cursor per connection would make one wait for another.
#:
#: Homed here rather than beside the aggregator's deriver because it is a fact
#: about this table's key, and the read surface needs it without reaching into
#: `connector/` to get it.
TRANSACTIONS_DOMAIN: Final = "transactions"

#: The two values `source` may take on every silver table, spelled once for the
#: readers that must report a zero for a source with no rows -- a breakdown that
#: omitted the empty source would make a consumer guess whether it meant zero or
#: unknown.
#:
#: 🔴 An enumeration, and therefore checked rather than trusted: the authority is
#: the DDL's `CHECK (source IN (...))`, and `tests/store/test_schema.py` asserts
#: this tuple still agrees with it. `data-model.md` names provenance exclusive
#: and total, so a third value arriving without this tuple noticing is exactly
#: the drift that assertion exists to catch.
PROVENANCE_SOURCES: Final[tuple[str, ...]] = ("aggregator", "manual")

sync_state = Table(
    "sync_state",
    metadata,
    Column("connection_id", Integer, ForeignKey("connections.connection_id"), primary_key=True),
    Column("domain", Text, primary_key=True),
    Column("cursor", Text, nullable=True),
    Column("history_start_date", CalendarDateColumn, nullable=True),
    Column("last_attempt_at", UtcInstantColumn, nullable=True),
    Column("last_success_at", UtcInstantColumn, nullable=True),
    Column("last_error_code", Text, nullable=True),
    Column("last_error_at", UtcInstantColumn, nullable=True),
    Column("updated_at", UtcInstantColumn, nullable=False),
)

account_rules = Table(
    "account_rules",
    metadata,
    Column("account_rule_id", Integer, primary_key=True),
    Column("account_id", Integer, ForeignKey("accounts.account_id"), nullable=False),
    Column("rule_type", Text, nullable=False),
    Column("parameters", Text, nullable=False),
    Column("active", Integer, nullable=False),
    Column("note", Text, nullable=True),
    Column("created_at", UtcInstantColumn, nullable=False),
    Column("updated_at", UtcInstantColumn, nullable=False),
)

Index(
    "account_rules_one_per_type_per_account",
    account_rules.c.account_id,
    account_rules.c.rule_type,
    unique=True,
)

#: The tables FR-6 names as the minimum core schema. Kept as a literal so a
#: table added to the metadata without a place in the requirement is a failing
#: test rather than a quiet expansion of the contract.
CORE_TABLES = (
    "derivation_versions",
    "institutions",
    "connections",
    "accounts",
    "raw_responses",
    "manual_imports",
    "transactions",
    "balances_daily",
    "securities",
    "holdings",
    "investment_transactions",
    "sync_state",
    "account_rules",
)
