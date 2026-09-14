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

import json
from collections.abc import Iterable
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

#: The `Column.info` key naming a column whose values are public identifiers,
#: and which identifier. Read by `sync shell`, whose value rule blanks any run of
#: eight or more digits as an account number. An all-digit CUSIP is exactly that
#: shape, and it is printed on every brokerage statement, so masking it protects
#: nothing and hides which security a row is.
#:
#: The value names the identifier rather than being `True`, because the shell
#: spares a cell only when it also passes that identifier's own check. A flag
#: with no check behind it would spare whatever an alias put under the name.
PUBLIC_IDENTIFIER: Final = "public_identifier"


def public_identifier_columns() -> dict[str, str]:
    """Every column flagged as a public identifier, mapped to which identifier it holds.

    Keyed by bare column name, because that is all a result set carries: the
    driver does not say which table a result column came from.
    """
    return {
        column.name: str(column.info[PUBLIC_IDENTIFIER])
        for table in metadata.tables.values()
        for column in table.columns
        if PUBLIC_IDENTIFIER in column.info
    }


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
    # 🔴 Last, not beside `last_success_at` where it reads better. It arrived in
    # migration 004 through `ALTER TABLE ... ADD COLUMN`, which appends, and the
    # drift guard compares this list against `PRAGMA table_info` *in order*.
    #
    # AC-12.4: the date this connection's roster was last successfully observed
    # -- *we looked*, as against `accounts.last_seen_date`'s *and this is what we
    # found*. A calendar date because its only use is a comparison against that
    # column, and `data-model.md` § Constraints 3 makes a comparison across a
    # date and an instant a defect rather than a conversion.
    #
    # 🔴 Not `last_success_at` under another name: that records a sync attempt
    # succeeding, and the two diverge whenever a sync succeeds without a roster
    # call. 🔴 Nullable, and the null MEANS "this connection's roster has never
    # been observed" -- `query._account_lifecycle` reads it as exactly that, so
    # such a connection marks nothing absent.
    Column("roster_observed_date", CalendarDateColumn, nullable=True),
    # 🔴 Declared LAST, not beside `last_error_code` where they read better:
    # migration 008 adds them with `ALTER TABLE`, which appends, and this
    # metadata is compared to the migrated database column-by-column in order.
    #
    # A null in either means the Item has not been fetched since the columns
    # existed -- never that consent does not expire, and never that the
    # aggregator reports no error.
    Column("consent_expires_at", UtcInstantColumn, nullable=True),
    # The aggregator's STANDING complaint about the Item, which is not
    # `last_error_code`: that records the last sync attempt failing, this
    # records the Item being unwell whether or not the last attempt succeeded.
    Column("source_error_code", Text, nullable=True),
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
    # 🔴 Nullable, and the null MEANS "the aggregator has not stated this
    # account's unit" -- never `USD`, never the unit of the operator's other
    # accounts. Both of the aggregator's currency fields are documented
    # nullable, and while this column was NOT NULL such an account could not be
    # created at all: it was skipped, and every transaction on it went on
    # refusing to derive. An account that is honest about its unknown unit is
    # strictly better than one that is invisible.
    #
    # 🔴 The four other NOT NULL currency columns stay NOT NULL. They each
    # describe ONE amount, and an amount whose unit nothing stated is refused
    # row by row with a named reason; this column describes an account, which
    # can exist perfectly well before its unit is known.
    Column("currency", Text, nullable=True),
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
    # 🔴 Declared LAST, and not beside `posted_date` where the split it makes
    # would read far better: migration 005 adds it with `ALTER TABLE`, which
    # appends, and this metadata is compared to the migrated database
    # column-by-column in order. Two correct descriptions of one correct
    # database must not disagree about position.
    #
    # Nullable because there is no constant default that is correct -- the value
    # is `COALESCE(authorized_date, posted_date)` per row -- so a null means
    # "predates the split, not yet rebuilt", never "committed on the posting
    # date". `store rebuild` fills it from the archive.
    Column("ledger_date", CalendarDateColumn, nullable=True),
    # 🔴 Declared LAST for the same reason `ledger_date` is: migration 007 adds
    # it with `ALTER TABLE`, which appends, and this metadata is compared to the
    # migrated database column-by-column in order.
    #
    # The aggregator Item this row was produced under, which is this store's
    # `connections` row -- `source_connection_id` holds the Item id and is
    # unique, so one connection is one Item. Removing a connection and linking
    # it again yields a NEW Item that re-issues every transaction id, so the
    # whole granted history arrives again as rows nothing can collide with. The
    # rows are all kept; the read path counts the newest lineage over the range
    # it covers and older lineages only outside it, and DISCLOSES the overlap.
    #
    # 🔴 Nullable, and the null means "predates the split, not yet rebuilt" or
    # "came from an operator file, which no Item produced" -- never "belongs to
    # the current Item". A row with no lineage is therefore never excluded: the
    # alternative is deleting money on the strength of a column nothing filled.
    # `store rebuild` fills it from the archive.
    Column("lineage_id", Integer, ForeignKey("connections.connection_id"), nullable=True),
    # 🔴 Declared LAST for the same reason every ALTER-added column is: this
    # metadata is compared to the migrated database column-by-column in order.
    #
    # A shared token, not a pointer: both legs of one transfer carry the same
    # value, and neither is the other's parent. A null means no counterparty leg
    # was found -- the ordinary case for most rows, and never "not checked".
    Column("transfer_pair_id", Integer, nullable=True),
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
    Column("cusip", Text, nullable=True, info={PUBLIC_IDENTIFIER: "cusip"}),
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
    # Migration 010. Last, because `ALTER TABLE ... ADD COLUMN` put it there and
    # the drift guard compares columns in order. Null is never "priced today".
    Column("price_as_of", CalendarDateColumn, nullable=True),
)

#: Migration 011. A position a capture listed that this build could not record,
#: because its currency has no known minor-unit exponent or states none. A
#: refusal is not a position with its value missing -- it lives apart from
#: `holdings` so a holdings total never meets a row with no figure. A raw
#: response is its only possible provenance, so the link is NOT NULL and there is
#: no `source`. Null `currency` means the aggregator stated none.
refused_holdings = Table(
    "refused_holdings",
    metadata,
    Column("account_id", Integer, ForeignKey("accounts.account_id"), primary_key=True),
    Column("security_id", Integer, ForeignKey("securities.security_id"), primary_key=True),
    Column("as_of_date", CalendarDateColumn, primary_key=True),
    Column("currency", Text, nullable=True),
    Column("captured_at", UtcInstantColumn, nullable=False),
    Column(
        "raw_response_id",
        Integer,
        ForeignKey("raw_responses.raw_response_id"),
        nullable=False,
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

#: The `sync_state.domain` a transaction cursor is keyed under. The column is
#: keyed by domain because the domains advance on their own schedules and a
#: single cursor per connection would make one wait for another.
#:
#: Homed here rather than beside the aggregator's deriver because it is a fact
#: about this table's key, and the read surface needs it without reaching into
#: `connector/` to get it.
TRANSACTIONS_DOMAIN: Final = "transactions"

#: The `sync_state.domain` the investments pull records itself under.
#:
#: 🔴 **Not the aggregator's `investments` product, though the two are spelled
#: alike.** This is a key in this product's own table, and every investments row
#: ever written is stored under it; the product name is the aggregator's
#: vocabulary, lives in `connector/`, and is what `connections.capabilities`
#: records. Kept as two constants so that the aggregator renaming its product
#: cannot silently rewrite the key this table's history is filed under.
INVESTMENTS_DOMAIN: Final = "investments"


def encode_capabilities(capabilities: Iterable[str]) -> str:
    """What `connections.capabilities` holds: a sorted JSON array of product names.

    🔴 **The encoding has one home, and this is it.** The column is written at
    enrollment and read by the sync run to decide whether a connection's
    investments are pulled at all (AC-3.2) -- a writer and a reader in different
    layers, which is exactly the shape where `json.dumps` on one side and a
    hand-rolled parse on the other drift apart. Sorted so that two enrollments
    reporting the same capabilities store the same bytes.
    """
    return json.dumps(sorted(capabilities))


def decode_capabilities(stored: str) -> frozenset[str]:
    """The product names in a stored `capabilities` value.

    Raises `ValueError` on anything that is not an array of strings rather than
    returning an empty set: *this connection reports no capabilities* and *this
    column cannot be read* are different facts, and collapsing them would make a
    connection stop pulling investments with nothing anywhere saying why.
    """
    parsed = json.loads(stored)
    if not isinstance(parsed, list) or not all(isinstance(name, str) for name in parsed):
        raise ValueError(
            f"capabilities is {type(parsed).__name__}, expected an array of product names"
        )
    return frozenset(parsed)


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

#: The tables a migration after 002 created. Not FR-6's -- that list is a
#: minimum, and these extend it rather than amend it -- and kept as a literal
#: beside it for the same reason: a table in the metadata that neither list
#: names is a contract widened without anyone having decided to widen it.
LATER_TABLES = ("refused_holdings",)
