"""Migration 002 -- the thirteen tables of FR-6, as frozen DDL.

**This DDL never changes.** A forward-only migration is a historical record of
what some datastore in the world actually had done to it; editing one rewrites
history for every store that already ran it and leaves no two installations
agreeing on what "version 2" means. Changing the schema is migration 003.

The literal SQL is deliberately not generated from `store/schema.py`. Metadata
that generates its own migrations drifts silently the moment someone edits a
column, because both sides move together and nothing is left to disagree.
Here the two are written independently and
`tests/store/test_schema.py::test_the_metadata_matches_the_migrated_database`
compares them column by column, so drift is a red test rather than a surprise
in a query six chunks from now.

Three constraint idioms carry requirements into the database itself, where no
future writer can forget them:

* `typeof(x) = 'integer'` on every monetary column. SQLite's typing is dynamic,
  so a column declared INTEGER will hold a float without complaint. AC-6.2 says
  no floats anywhere in the schema, and this is the difference between a rule
  that is enforced and one that is merely written down.
* `GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'` on every calendar date,
  and `LIKE '%+00:00'` on every UTC instant. AC-6.4 keeps the two kinds apart;
  mypy keeps them apart in code, and these keep them apart at the file, which is
  the layer that survives a `sync shell` session typing raw SQL.
* Partial unique indexes for identity. Idempotency (AC-1.4, AC-7.5) is a
  property of the store, not a discipline of the code that writes to it.

🔴 **Sign convention, stated once and depended on everywhere: every stored
amount is signed from the operator's point of view.** A positive `amount_minor`
is money moving *into* an account and a negative one is money moving out; a
positive `current_minor` is value the operator holds and a negative one is value
they owe, so a credit card's balance is stored negative. Aggregators disagree
with each other -- several report a card balance as a positive amount owed -- so
normalizing to this convention is the connector's job.

One convention rather than one per account type is what keeps two later things
from needing a special case: net worth is a plain sum, and AC-11.2's
reconciliation is "change in balance equals sum of transactions" for every
account. `accounts.balance_class` partitions assets from liabilities for
*reporting*, never for arithmetic -- a misclassification mislabels a breakdown,
where a wrong sign would produce a wrong total.

Two balance columns are deliberate exceptions and hold magnitudes as the source
reports them: `available_minor` (available funds, or available credit) and
`limit_minor` (the credit limit). Neither participates in net worth.

AC-9.4 requires every MCP tool description to state the convention it reports
in; this is the definition those descriptions refer to.
"""

from __future__ import annotations

from typing import Final

from bankmachine.store.connection import Connection

#: The two constraint idioms of migration 002, named for the migration that
#: owns them. A later migration defines its own rather than editing these:
#: `CORE_SCHEMA_DDL_SHA256` is what stops an edit here from being quiet.
_V2_DATE = "GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"
_V2_INSTANT = "LIKE '%+00:00'"

#: Migration 002 before the two constraint idioms above are spliced in. Kept
#: separate only so the date and instant CHECKs are written once each; the
#: statements themselves are frozen, and `CORE_SCHEMA_DDL` is what runs.
_DDL_TEMPLATES: tuple[str, ...] = (
    # -- Provenance -----------------------------------------------------------
    #
    # AC-5.3. Losslessness is only well-defined relative to a recorded
    # derivation version, so every normalized row names the version of the code
    # that produced it. Rows are written by the rebuild path, never seeded here:
    # a version this build did not derive anything with would be a claim about
    # code that has not run.
    """
    CREATE TABLE derivation_versions (
        derivation_version_id INTEGER PRIMARY KEY,
        version               INTEGER NOT NULL UNIQUE
                              CHECK (typeof(version) = 'integer'),
        description           TEXT NOT NULL,
        first_used_at         TEXT NOT NULL CHECK (first_used_at %(instant)s)
    )
    """,
    # -- The roster, as data (AC-6.6) -----------------------------------------
    #
    # An institution's name is a value in a row. No table, column, enum or code
    # path anywhere in this schema encodes a particular institution.
    """
    CREATE TABLE institutions (
        institution_id        INTEGER PRIMARY KEY,
        source_institution_id TEXT UNIQUE,
        name                  TEXT NOT NULL,
        first_seen_at         TEXT NOT NULL CHECK (first_seen_at %(instant)s),
        last_seen_at          TEXT NOT NULL CHECK (last_seen_at %(instant)s)
    )
    """,
    # `credential_ref` names the keychain account holding the access token. The
    # token itself is never in this file (AC-10.1): a datastore backup travels,
    # and a credential that travels with it is a credential that has leaked.
    #
    # `granted_history_days` is nullable because AC-1.3a records what the source
    # actually gave, which is not known until the first sync returns; AC-11.8
    # turns the shortfall against `requested_history_days` into a known gap
    # rather than letting the returned window pass as complete.
    """
    CREATE TABLE connections (
        connection_id          INTEGER PRIMARY KEY,
        institution_id         INTEGER NOT NULL REFERENCES institutions(institution_id),
        source_connection_id   TEXT NOT NULL UNIQUE,
        credential_ref         TEXT NOT NULL,
        capabilities           TEXT NOT NULL DEFAULT '[]',
        requested_history_days INTEGER
                               CHECK (typeof(requested_history_days) IN ('integer', 'null')),
        granted_history_days   INTEGER
                               CHECK (typeof(granted_history_days) IN ('integer', 'null')),
        status                 TEXT NOT NULL
                               CHECK (status IN ('active', 'degraded', 'retired')),
        last_success_at        TEXT CHECK (last_success_at %(instant)s),
        last_error_code        TEXT,
        last_error_at          TEXT CHECK (last_error_at %(instant)s),
        enrolled_at            TEXT NOT NULL CHECK (enrolled_at %(instant)s),
        retired_at             TEXT CHECK (retired_at %(instant)s),
        created_at             TEXT NOT NULL CHECK (created_at %(instant)s),
        updated_at             TEXT NOT NULL CHECK (updated_at %(instant)s),
        CHECK ((status = 'retired') = (retired_at IS NOT NULL)),
        CHECK ((status = 'degraded') <= (last_error_code IS NOT NULL))
    )
    """,
    # AC-1.4: re-enrolling an institution updates rather than duplicates. Making
    # that a partial unique index rather than a rule in the enrollment command
    # means it also holds for the import path, the repair path, and whatever
    # writes the connection next.
    """
    CREATE UNIQUE INDEX connections_one_live_per_institution
        ON connections (institution_id) WHERE retired_at IS NULL
    """,
    # -- Accounts (AC-6.3, AC-6.5) --------------------------------------------
    #
    # `account_id` is local and permanent; it is what every row of history
    # points at. `source_account_id` is the aggregator's, and it changes when a
    # connection is removed and re-linked -- which is exactly why history does
    # not reference it (AC-6.3). `connection_id` is nullable because FR-7's
    # import path serves accounts no aggregator reaches, and repointable because
    # re-enrollment produces a new connection for the same accounts.
    #
    # AC-6.5: `closed_date` is what keeps a retired account's dormant period
    # from reading as a coverage gap (AC-11.1), and nothing about retiring an
    # account touches its transactions.
    #
    # `balance_class` exists because `net_worth` is an enumerated consumer of
    # this schema and `account_type` alone cannot answer it: the types are the
    # source's vocabulary, they differ between sources, and an import-only
    # account has no source type at all. Classifying once, as data the operator
    # can correct, beats a taxonomy hardcoded in whichever query needs it first
    # -- and per the sign convention above it partitions a report rather than
    # deciding an arithmetic sign, so getting it wrong is visible rather than
    # silent.
    """
    CREATE TABLE accounts (
        account_id                   INTEGER PRIMARY KEY,
        institution_id               INTEGER NOT NULL REFERENCES institutions(institution_id),
        connection_id                INTEGER REFERENCES connections(connection_id),
        source_account_id            TEXT,
        source_persistent_account_id TEXT,
        name                         TEXT NOT NULL,
        official_name                TEXT,
        mask                         TEXT,
        account_type                 TEXT NOT NULL,
        account_subtype              TEXT,
        balance_class                TEXT NOT NULL
                                     CHECK (balance_class IN ('asset', 'liability')),
        currency                     TEXT NOT NULL,
        lifecycle_status             TEXT NOT NULL
                                     CHECK (lifecycle_status IN ('active', 'inactive')),
        opened_date                  TEXT CHECK (opened_date %(date)s),
        first_seen_date              TEXT NOT NULL CHECK (first_seen_date %(date)s),
        closed_date                  TEXT CHECK (closed_date %(date)s),
        source                       TEXT NOT NULL
                                     CHECK (source IN ('aggregator', 'manual')),
        created_at                   TEXT NOT NULL CHECK (created_at %(instant)s),
        updated_at                   TEXT NOT NULL CHECK (updated_at %(instant)s),
        CHECK ((source = 'aggregator') <= (source_account_id IS NOT NULL)),
        CHECK (closed_date IS NULL OR closed_date >= first_seen_date)
    )
    """,
    """
    CREATE UNIQUE INDEX accounts_source_identity
        ON accounts (connection_id, source_account_id) WHERE source_account_id IS NOT NULL
    """,
    """
    CREATE INDEX accounts_by_institution ON accounts (institution_id)
    """,
    # -- The bronze layer (FR-5) ----------------------------------------------
    #
    # AC-5.1: verbatim, compressed, hashed, before any normalization. The hash
    # is over the uncompressed body so it is a property of what the source said
    # rather than of how this build happened to compress it.
    """
    CREATE TABLE raw_responses (
        raw_response_id INTEGER PRIMARY KEY,
        connection_id   INTEGER REFERENCES connections(connection_id),
        endpoint        TEXT NOT NULL,
        received_at     TEXT NOT NULL CHECK (received_at %(instant)s),
        body_gzip       BLOB NOT NULL,
        body_sha256     TEXT NOT NULL,
        body_bytes      INTEGER NOT NULL CHECK (typeof(body_bytes) = 'integer'),
        request_context TEXT
    )
    """,
    """
    CREATE INDEX raw_responses_by_connection ON raw_responses (connection_id, received_at)
    """,
    # AC-7.5: re-importing the same file is a no-op. The file's hash is the
    # identity, so an operator who renames the export cannot import it twice.
    """
    CREATE TABLE manual_imports (
        manual_import_id INTEGER PRIMARY KEY,
        account_id       INTEGER NOT NULL REFERENCES accounts(account_id),
        adapter          TEXT NOT NULL,
        source_name      TEXT NOT NULL,
        file_sha256      TEXT NOT NULL,
        file_bytes       INTEGER NOT NULL CHECK (typeof(file_bytes) = 'integer'),
        imported_at      TEXT NOT NULL CHECK (imported_at %(instant)s),
        rows_seen        INTEGER NOT NULL CHECK (typeof(rows_seen) = 'integer'),
        rows_applied     INTEGER NOT NULL CHECK (typeof(rows_applied) = 'integer')
    )
    """,
    """
    CREATE UNIQUE INDEX manual_imports_file_identity
        ON manual_imports (account_id, file_sha256)
    """,
    # -- Transactions ---------------------------------------------------------
    #
    # AC-6.1: the source's own category fields are retained and never written
    # over; `category_override` is a separate, nullable column, so the original
    # is always recoverable and an override is always visible as one.
    #
    # AC-2.2: `removed_at` is a soft delete. A row the source withdrew is part
    # of the history of what this system was told.
    #
    # AC-7.4: `source` and the two provenance keys make every row's origin
    # answerable in any query result. Exactly one of them is set, and it is
    # always set -- an aggregator row names the response it was derived from, an
    # imported row names the import. The CHECK enforces that rather than the
    # code that inserts, because a row with no link is a row whose answer to
    # "where did this come from" is silence, and the rebuild is written
    # against this constraint rather than against a convention.
    """
    CREATE TABLE transactions (
        transaction_id               INTEGER PRIMARY KEY,
        account_id                   INTEGER NOT NULL REFERENCES accounts(account_id),
        source_transaction_id        TEXT,
        source_pending_transaction_id TEXT,
        pending                      INTEGER NOT NULL CHECK (pending IN (0, 1)),
        posted_date                  TEXT NOT NULL CHECK (posted_date %(date)s),
        authorized_date              TEXT CHECK (authorized_date %(date)s),
        amount_minor                 INTEGER NOT NULL
                                     CHECK (typeof(amount_minor) = 'integer'),
        currency                     TEXT NOT NULL,
        description                  TEXT NOT NULL,
        merchant_name                TEXT,
        source_category_primary      TEXT,
        source_category_detailed     TEXT,
        category_override            TEXT,
        source                       TEXT NOT NULL
                                     CHECK (source IN ('aggregator', 'manual')),
        raw_response_id              INTEGER REFERENCES raw_responses(raw_response_id),
        manual_import_id             INTEGER REFERENCES manual_imports(manual_import_id),
        derivation_version_id        INTEGER NOT NULL
                                     REFERENCES derivation_versions(derivation_version_id),
        import_fingerprint           TEXT,
        removed_at                   TEXT CHECK (removed_at %(instant)s),
        first_seen_at                TEXT NOT NULL CHECK (first_seen_at %(instant)s),
        updated_at                   TEXT NOT NULL CHECK (updated_at %(instant)s),
        CHECK (CASE source
                 WHEN 'aggregator' THEN source_transaction_id IS NOT NULL
                                    AND raw_response_id IS NOT NULL
                                    AND manual_import_id IS NULL
                 ELSE manual_import_id IS NOT NULL
                      AND import_fingerprint IS NOT NULL
                      AND raw_response_id IS NULL
               END)
    )
    """,
    """
    CREATE UNIQUE INDEX transactions_source_identity
        ON transactions (account_id, source_transaction_id)
        WHERE source_transaction_id IS NOT NULL
    """,
    """
    CREATE UNIQUE INDEX transactions_import_identity
        ON transactions (account_id, import_fingerprint)
        WHERE import_fingerprint IS NOT NULL
    """,
    # The coverage report walks an account's transactions in date order looking
    # for gaps (AC-9.5, AC-11.1), and every spending and cashflow aggregate is
    # an account-and-period scan. This index is the shape all of them read in.
    """
    CREATE INDEX transactions_by_account_date ON transactions (account_id, posted_date)
    """,
    # AC-2.3: a posting transaction finds its pending row by the source's own
    # pending identifier rather than by guessing from amount and date.
    """
    CREATE INDEX transactions_pending_link
        ON transactions (account_id, source_pending_transaction_id)
        WHERE source_pending_transaction_id IS NOT NULL
    """,
    # -- Balances (AC-3.1) ----------------------------------------------------
    #
    # Append, never overwrite: no aggregator backfills a balance series, so a
    # day not captured is a day gone for good. The primary key is the append
    # rule made structural -- a second capture on a day already recorded is
    # rejected rather than allowed to overwrite, so the series does not depend
    # on what time of day anyone happened to look, and a re-run changes nothing
    # (AC-2.4).
    """
    CREATE TABLE balances_daily (
        account_id            INTEGER NOT NULL REFERENCES accounts(account_id),
        as_of_date            TEXT NOT NULL CHECK (as_of_date %(date)s),
        current_minor         INTEGER NOT NULL CHECK (typeof(current_minor) = 'integer'),
        available_minor       INTEGER CHECK (typeof(available_minor) IN ('integer', 'null')),
        limit_minor           INTEGER CHECK (typeof(limit_minor) IN ('integer', 'null')),
        currency              TEXT NOT NULL,
        captured_at           TEXT NOT NULL CHECK (captured_at %(instant)s),
        source                TEXT NOT NULL CHECK (source IN ('aggregator', 'manual')),
        raw_response_id       INTEGER REFERENCES raw_responses(raw_response_id),
        manual_import_id      INTEGER REFERENCES manual_imports(manual_import_id),
        derivation_version_id INTEGER NOT NULL
                              REFERENCES derivation_versions(derivation_version_id),
        PRIMARY KEY (account_id, as_of_date),
        CHECK (CASE source
                 WHEN 'aggregator' THEN raw_response_id IS NOT NULL
                                    AND manual_import_id IS NULL
                 ELSE manual_import_id IS NOT NULL AND raw_response_id IS NULL
               END)
    )
    """,
    # -- Investments (AC-3.2) -------------------------------------------------
    #
    # Securities are their own table referenced by id, so a position and a
    # transaction in the same instrument agree about what it is.
    #
    # A price is money and is therefore minor units. A *quantity* is not money:
    # fractional shares are routine and a share is not divided into hundredths,
    # so quantities are exact decimal text rather than a scaled integer whose
    # scale would be a guess. Text keeps the source's own precision and converts
    # to `Decimal` without ever passing through a float.
    """
    CREATE TABLE securities (
        security_id           INTEGER PRIMARY KEY,
        source_security_id    TEXT UNIQUE,
        name                  TEXT,
        ticker                TEXT,
        cusip                 TEXT,
        isin                  TEXT,
        security_type         TEXT,
        currency              TEXT,
        close_price_minor     INTEGER
                              CHECK (typeof(close_price_minor) IN ('integer', 'null')),
        close_price_as_of     TEXT CHECK (close_price_as_of %(date)s),
        derivation_version_id INTEGER NOT NULL
                              REFERENCES derivation_versions(derivation_version_id),
        created_at            TEXT NOT NULL CHECK (created_at %(instant)s),
        updated_at            TEXT NOT NULL CHECK (updated_at %(instant)s)
    )
    """,
    # Holdings are a daily snapshot on the same append rule as balances: a
    # position is what it was on a day, and net worth over time (AC-9.1's
    # `net_worth`) reads the series rather than the latest row.
    """
    CREATE TABLE holdings (
        account_id            INTEGER NOT NULL REFERENCES accounts(account_id),
        security_id           INTEGER NOT NULL REFERENCES securities(security_id),
        as_of_date            TEXT NOT NULL CHECK (as_of_date %(date)s),
        quantity              TEXT NOT NULL,
        cost_basis_minor      INTEGER CHECK (typeof(cost_basis_minor) IN ('integer', 'null')),
        market_value_minor    INTEGER NOT NULL
                              CHECK (typeof(market_value_minor) = 'integer'),
        currency              TEXT NOT NULL,
        captured_at           TEXT NOT NULL CHECK (captured_at %(instant)s),
        source                TEXT NOT NULL CHECK (source IN ('aggregator', 'manual')),
        raw_response_id       INTEGER REFERENCES raw_responses(raw_response_id),
        manual_import_id      INTEGER REFERENCES manual_imports(manual_import_id),
        derivation_version_id INTEGER NOT NULL
                              REFERENCES derivation_versions(derivation_version_id),
        PRIMARY KEY (account_id, security_id, as_of_date),
        CHECK (CASE source
                 WHEN 'aggregator' THEN raw_response_id IS NOT NULL
                                    AND manual_import_id IS NULL
                 ELSE manual_import_id IS NOT NULL AND raw_response_id IS NULL
               END)
    )
    """,
    """
    CREATE TABLE investment_transactions (
        investment_transaction_id        INTEGER PRIMARY KEY,
        account_id                       INTEGER NOT NULL REFERENCES accounts(account_id),
        security_id                      INTEGER REFERENCES securities(security_id),
        source_investment_transaction_id TEXT,
        trade_date                       TEXT NOT NULL CHECK (trade_date %(date)s),
        settlement_date                  TEXT CHECK (settlement_date %(date)s),
        investment_type                  TEXT NOT NULL,
        investment_subtype               TEXT,
        quantity                         TEXT,
        price_minor                      INTEGER
                                         CHECK (typeof(price_minor) IN ('integer', 'null')),
        fees_minor                       INTEGER
                                         CHECK (typeof(fees_minor) IN ('integer', 'null')),
        amount_minor                     INTEGER NOT NULL
                                         CHECK (typeof(amount_minor) = 'integer'),
        currency                         TEXT NOT NULL,
        description                      TEXT,
        source                           TEXT NOT NULL
                                         CHECK (source IN ('aggregator', 'manual')),
        raw_response_id                  INTEGER REFERENCES raw_responses(raw_response_id),
        manual_import_id                 INTEGER REFERENCES manual_imports(manual_import_id),
        derivation_version_id            INTEGER NOT NULL
                                         REFERENCES derivation_versions(derivation_version_id),
        import_fingerprint               TEXT,
        removed_at                       TEXT CHECK (removed_at %(instant)s),
        first_seen_at                    TEXT NOT NULL CHECK (first_seen_at %(instant)s),
        updated_at                       TEXT NOT NULL CHECK (updated_at %(instant)s),
        CHECK (CASE source
                 WHEN 'aggregator' THEN source_investment_transaction_id IS NOT NULL
                                    AND raw_response_id IS NOT NULL
                                    AND manual_import_id IS NULL
                 ELSE manual_import_id IS NOT NULL
                      AND import_fingerprint IS NOT NULL
                      AND raw_response_id IS NULL
               END)
    )
    """,
    """
    CREATE UNIQUE INDEX investment_transactions_source_identity
        ON investment_transactions (account_id, source_investment_transaction_id)
        WHERE source_investment_transaction_id IS NOT NULL
    """,
    """
    CREATE UNIQUE INDEX investment_transactions_import_identity
        ON investment_transactions (account_id, import_fingerprint)
        WHERE import_fingerprint IS NOT NULL
    """,
    """
    CREATE INDEX investment_transactions_by_account_date
        ON investment_transactions (account_id, trade_date)
    """,
    # -- Sync bookkeeping (FR-2, FR-4) ----------------------------------------
    #
    # One row per connection per data domain, because the domains advance
    # independently: transactions have a cursor, balances and investments are
    # pulled whole, and a connection may be healthy for one and failing for
    # another. AC-2.1 requires the cursor to be written in the same transaction
    # as the data it accompanies, which this shape allows and the sync path
    # will owe.
    #
    # AC-4.5: a degraded record without `last_success_at` is insufficient,
    # because the size of the resulting hole must be computable rather than
    # guessed. It is nullable only for a domain that has never once succeeded.
    #
    # `domain` is a CHECKed vocabulary and `account_rules.rule_type` is not,
    # which is deliberate rather than an oversight. A misspelled domain is a
    # class of data that silently never syncs -- the exact failure this product
    # is built to prevent -- so it is worth a migration to widen. A rule row
    # with an unrecognized type is inert: nothing applies it, and the rule
    # engine validates against its own registry when it lands (FR-8).
    """
    CREATE TABLE sync_state (
        connection_id      INTEGER NOT NULL REFERENCES connections(connection_id),
        domain             TEXT NOT NULL
                           CHECK (domain IN ('transactions', 'balances', 'investments')),
        cursor             TEXT,
        history_start_date TEXT CHECK (history_start_date %(date)s),
        last_attempt_at    TEXT CHECK (last_attempt_at %(instant)s),
        last_success_at    TEXT CHECK (last_success_at %(instant)s),
        last_error_code    TEXT,
        last_error_at      TEXT CHECK (last_error_at %(instant)s),
        updated_at         TEXT NOT NULL CHECK (updated_at %(instant)s),
        PRIMARY KEY (connection_id, domain)
    )
    """,
    # -- Account rules (FR-8) -------------------------------------------------
    #
    # AC-8.1/AC-8.5: the rule *type* is code, everything else is data. Applying
    # an existing type to an account is a row, never a branch, and never keyed
    # to a named institution.
    #
    # `parameters` is JSON because each rule type has its own shape;
    # `contribution_only` (AC-8.2) carries the set of local account ids whose
    # inbound transfers count as contributions (AC-8.4). Those ids are not
    # foreign keys -- SQLite cannot reference into a JSON document -- so the
    # rule engine validates them when it lands in its own build step, and the
    # cost of that is recorded here rather than discovered there.
    """
    CREATE TABLE account_rules (
        account_rule_id INTEGER PRIMARY KEY,
        account_id      INTEGER NOT NULL REFERENCES accounts(account_id),
        rule_type       TEXT NOT NULL,
        parameters      TEXT NOT NULL,
        active          INTEGER NOT NULL CHECK (active IN (0, 1)),
        note            TEXT,
        created_at      TEXT NOT NULL CHECK (created_at %(instant)s),
        updated_at      TEXT NOT NULL CHECK (updated_at %(instant)s)
    )
    """,
    """
    CREATE UNIQUE INDEX account_rules_one_per_type_per_account
        ON account_rules (account_id, rule_type)
    """,
)

#: Every statement of migration 002, in creation order. Applied inside the one
#: transaction the runner owns, so the thirteen tables and the version stamp
#: that claims them commit together or not at all.
CORE_SCHEMA_DDL: Final[tuple[str, ...]] = tuple(
    statement % {"date": _V2_DATE, "instant": _V2_INSTANT} for statement in _DDL_TEMPLATES
)


#: SHA-256 of migration 002's rendered DDL, joined by newlines.
#:
#: The docstring above promises this DDL never changes, and until this constant
#: existed that promise rested on nobody editing `_V2_DATE`, `_V2_INSTANT` or a
#: template -- an enumeration of things not to touch, which is the shape of
#: guarantee this project has already been burned by. Migration 003 will want
#: the same two idioms, and the obvious move is to reuse the constants; doing so
#: and then tightening one would silently change what version 2 means for every
#: datastore created afterwards, with every test still green.
#:
#: `test_migration_002_ddl_is_frozen` is what makes that impossible to do
#: quietly. If it fails, the answer is almost never to update this hash: it is
#: that the change belongs in a new migration. Update it only when migration 002
#: has never run anywhere, which after this branch merges is never again.
CORE_SCHEMA_DDL_SHA256: Final = "18864345dc1b47abb93adedb6c8f11d6f7f88d80972a0307252dcb269e813894"


def apply_core_schema(conn: Connection) -> None:
    """Issue migration 002's DDL. The runner owns the transaction around it."""
    for statement in CORE_SCHEMA_DDL:
        conn.execute(statement)
