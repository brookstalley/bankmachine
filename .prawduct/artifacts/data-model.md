---
artifact: data-model
version: 1
depends_on:
  - artifact: system-requirements
    file_path: docs/system-requirements.md
  - artifact: architecture
    file_path: .prawduct/artifacts/architecture.md
last_validated: null
---

# Data Model — bankmachine

**Scope:** the thirteen tables of `docs/system-requirements.md` FR-6, their relationships, their
lifecycles, and the invariants that hold across them. This artifact is the home of §4's data-model
criteria (AC-5.1 through AC-8.5) at the level a reader needs before touching a query.

**Dependency note.** The template's usual upstream is a product brief, which does not exist as a
separate artifact here; §0 of `docs/system-requirements.md` carries that content and is this
artifact's upstream, matching the shape `architecture.md` already records.

**Status: built and frozen.** Unlike the other strategy artifacts, this one describes tables that
already exist. Migration 002 (`src/bankmachine/store/migrations/core_schema.py`) created them in
build step 1 and **its DDL never changes** — a forward-only migration is a record of what some
datastore in the world actually had done to it. Changing the schema is migration 003. Where this
document and that DDL disagree, the DDL is right and this document is a bug.

**The typed pair.** `store/schema.py` (SQLAlchemy Core metadata) and `store/migrations/core_schema.py`
(frozen DDL) are written **independently and never generated from one another**, and
`tests/store/test_schema.py::test_the_metadata_matches_the_migrated_database` compares them column by
column. Two independent descriptions can disagree, and the disagreement is the only drift signal
there is.

**Norms.** The datastore *access* norms live in `architecture.md` § Direction and are not restated
here (one home per fact). The norms below govern the data's *shape and meaning*.

---

## Direction

Norms. These bind future work; departure is a recorded decision, never silent
(`/prawduct:methodology norms`). Ratified 2026-09-07 by the owner. All but the first describe
constraints the shipped schema already enforces, so no retroactivity decision applies — there is
nothing to migrate or grandfather.

- **Every stored amount is signed from the operator's point of view.** A positive `amount_minor` is
  money moving *into* an account and a negative one is money moving out; a positive `current_minor`
  is value the operator holds and a negative one is value they owe, so a credit card's balance is
  stored negative. `available_minor` and `limit_minor` are the two documented exceptions and hold
  magnitudes as reported; neither participates in net worth. Normalizing to this convention is the
  connector's job.
  Why: aggregators disagree with each other, and several report a card balance as a **positive
  amount owed** — a consumer that takes that at face value is wrong *by twice the debt*, silently
  and plausibly, because the number is well-formed and the sum completes. One convention rather than
  one per account type is what keeps two later things from needing a special case: net worth is a
  plain sum, and AC-11.2's reconciliation is "change in balance equals sum of transactions" for
  every account. The rejected alternative — store as reported, let each consumer apply the sign —
  makes net worth type-dependent and puts the error in whichever consumer forgets.
  Status: **steady-state** as of 2026-09-07, closing `brookstalley/bankmachine#9`. The connector
  normalizes in `connector/plaid/derivers.py`; `test_a_liability_reported_positive_is_stored_negative`
  and its siblings assert it, `verify_norms_go_red.py` proves the assertion goes red with the
  normalization removed, and the two documented exceptions are asserted explicitly so a later change
  that signed every column alike cannot pass as a tidy-up.
  Ruling (2026-09-07): ratified in-transition rather than steady-state **because nothing tested it
  then** and the code that had to obey it was mid-build. Calling it steady-state with no mechanism
  would have been the aspirational failure this lifecycle exists to prevent.
  Amendment (2026-09-07, build step 2): flipped to steady-state on the mechanism landing. The
  clarification below is new normative content rather than a restatement, so it carries its own
  decision -- `[DECISION: an investment valuation is rounded half-even to the currency's minor unit,
  where a ledger amount is converted exactly or refused | taken by the owner 2026-09-07, on the
  evidence that the aggregator's own sandbox ships a four-decimal 401k balance as canned data and is
  therefore exercising the case deliberately | user can revisit]`, recorded in
  `build-plan-connector-v1.md` § Requirements Confidence alongside this plan's other owner
  decisions. Recorded there rather than asserted here because the code this amendment blesses is the
  code the same commit adds, which is the shape `docs/norms.md` warns hardest about.
  One clarification came with it, because a real payload forced it. **The convention governs ledger
  amounts and valuations alike, but only ledger amounts are exact.** An investment account's
  `current` is price times quantity — a *valuation* — and the aggregator returns it at whatever
  precision that arithmetic produced (measured: a sandbox 401k at `23631.9805` USD, canned data, so
  the case is deliberate). Those are rounded half-even to the currency's minor unit, logged each
  time, with the exact original kept in the archive. Ledger amounts are still converted exactly or
  refused. Recorded here rather than left implicit because "valuation" is a distinction this norm
  did not previously draw.

- **All monetary values are stored as integer minor units. No floats anywhere in the schema or in
  aggregation code.**
  Why: binary floating point cannot represent decimal currency exactly, and the error accumulates
  across aggregation — which is this product's main operation. The constraint lives in the database
  as `CHECK (typeof(x) = 'integer')` rather than only in code because **SQLite's typing is dynamic**:
  a column declared INTEGER will hold a float without complaint, so the CHECK is the difference
  between a rule that is enforced and one that is merely written down.
  Status: steady-state.

- **Calendar dates and UTC instants are distinct types and never mix.** Transaction dates are stored
  as dates; sync metadata as UTC timestamps.
  Why: a transaction date is a calendar fact from the institution, not an instant. Treating it as one
  introduces silent off-by-one-day errors **at period boundaries — exactly where period-over-period
  comparisons live**, which is the product's headline query. Enforced at three layers because each
  catches what the others cannot: the column types in the metadata, mypy in the code, and
  `GLOB`/`LIKE` constraints at the file, which is the layer that survives a `sync shell` session
  typing raw SQL.
  Status: steady-state.

- **Every silver row is either aggregator-sourced or manually imported, carries the evidence for
  whichever it is, and carries the derivation version that produced it.**
  Why: AC-7.4 requires provenance to survive into every query result, so an imported row is
  distinguishable from an aggregator row wherever it is read. Making it a database CHECK rather than
  a convention means a row cannot exist in a state that has lost its lineage. The
  `derivation_version_id NOT NULL` half is what makes AC-5.2's rebuild guarantee *checkable* rather
  than aspirational: losslessness is only well-defined relative to a recorded derivation version.
  Status: steady-state.

- **The daily balance and holdings series are append-only. A day already recorded is never
  overwritten.**
  Why: no aggregator backfills a balance series, so a day not captured is a day gone for good, and a
  day overwritten is a day rewritten by whatever time someone happened to look. The composite primary
  key carries the *insert* half — a second capture on a recorded day is rejected rather than allowed
  to win, which also makes a re-run a no-op (AC-2.4). 🔴 **It does not carry the whole rule:** a
  primary key permits `UPDATE`, `DELETE` and upsert, and AC-2.4's idempotency requirement is exactly
  what will tempt the sync writer toward `ON CONFLICT DO UPDATE`. Until a `BEFORE UPDATE/DELETE`
  trigger exists, the rest of this norm is judgment, and the Enforcement row says so rather than
  claiming a guarantee the schema does not give. This is the one class of data in the product that
  no re-sync can reconstruct.
  Status: steady-state.

- **A source value is never overwritten in place; local interpretation lives in its own column.**
  Source categories are retained as sent; `category_override` carries local intent.
  Why: overwriting destroys two different things at once — the ability to re-derive from the archive,
  and the ability to tell provider drift apart from a local decision. Once they are the same column,
  no later reader can separate them.
  Status: steady-state.

- **A transaction is never hard-deleted.** Removal is a soft delete carrying a removal timestamp.
  Why: AC-2.2. A removed transaction is evidence — of a reversal, a correction, or an upstream
  change — and a row that vanishes takes the reason with it. It also keeps the archive and the silver
  layer able to agree: a rebuild from raw responses can reproduce a removal, but cannot reproduce a
  row nobody kept.
  Status: steady-state.

- **A migration's DDL is frozen once written, and the Core metadata and that DDL are written
  independently rather than generated from one another.**
  Why: a forward-only migration is a historical record of what some datastore in the world actually
  had done to it; editing one rewrites history for every store that already ran it and leaves no two
  installations agreeing on what a version means. And metadata that emits its own migration agrees
  with whatever the code currently believes rather than with what any existing datastore contains —
  both sides move in the same edit and nothing is left over to disagree. **Two independent
  descriptions can disagree, and the disagreement is the only drift signal there is.**
  Status: steady-state.

---

## The layering rule, applied to the schema

🔴 **Institution identity is data (AC-6.6).** No table, column, enum, or code path encodes a
particular institution's name or behaviour. An institution is a row in `institutions`; a per-account
exception is a row in `account_rules`; an import format is a configured adapter. This is the
provider-agnostic norm expressed in the schema, and
`tests/preferences/test_no_provider_identity.py` enforces it over the source and schema roots.

The **aggregator** is expressly carved out and is not what this rule governs — it is a single named
dependency in v1 (§0.1 scope note). The rule binds *roster* identity.

---

## Entities

Thirteen tables in four groups: provenance, the roster, the bronze layer, and the silver layer with
its bookkeeping.

### Provenance

#### `derivation_versions` — what code produced a row (AC-5.3)

Losslessness is only well-defined relative to a recorded derivation version. Without one, an
upstream taxonomy change silently breaks an invariant that is supposed to be permanent.

| Field | Type | Constraints | Description |
|---|---|---|---|
| `derivation_version_id` | int | PK | Local identity |
| `version` | int | required, unique, integer-typed | The derivation logic's version number |
| `description` | text | required | What changed in this version |
| `first_used_at` | UTC instant | required | When this version first derived a row |

Rows are written by the derivation/rebuild path, never seeded by the migration: a version this build
did not derive anything with would be a claim about code that has not run.

Every normalized row in the silver layer carries `derivation_version_id NOT NULL`.

### The roster, as data

#### `institutions` — an institution as a value, not a code path

| Field | Type | Constraints | Description |
|---|---|---|---|
| `institution_id` | int | PK | **Stable local identity** |
| `source_institution_id` | text | unique, nullable | The aggregator's own id; null for an import-only institution |
| `name` | text | required | The institution's own spelling, as data |
| `first_seen_at` / `last_seen_at` | UTC instant | required | Observation window |

#### `connections` — one enrolled link to an institution (FR-1)

| Field | Type | Constraints | Description |
|---|---|---|---|
| `connection_id` | int | PK | Stable local identity |
| `institution_id` | int | required, FK → `institutions` | |
| `source_connection_id` | text | required, unique | The aggregator's connection handle |
| `credential_ref` | text | | **A keychain lookup handle, never a token.** See the security model |
| `capabilities` | text (JSON) | | Discovered at enrollment; drives investment pulls (AC-3.2) |
| `requested_history_days` | int | integer-or-null | What AC-1.2 asked for |
| `granted_history_days` | int | integer-or-null | 🔴 What was actually granted — **may be less**, and the delta is a recorded gap (AC-11.8). **Null means not yet known, never no shortfall** (AC-1.3a) |
| `status` | text | `active` \| `degraded` \| `retired` | |
| `last_success_at` | UTC instant | nullable | |
| `last_error_code` / `last_error_at` | text / UTC instant | nullable | |
| `enrolled_at` | UTC instant | required | |
| `retired_at` | UTC instant | nullable | Set iff `status = 'retired'` |
| `created_at` / `updated_at` | UTC instant | required | |

Table-level invariants, enforced in the database:

- `(status = 'retired') = (retired_at IS NOT NULL)` — the flag and the date cannot disagree.
- `(status = 'degraded') <= (last_error_code IS NOT NULL)` — a degraded connection always names its
  error. This is AC-4.2 made structural.
- Partial unique index `connections_one_live_per_institution ON (institution_id) WHERE retired_at IS NULL`
  — one live connection per institution, and re-enrollment updates rather than duplicating (AC-1.4),
  while retired connections accumulate freely (AC-1.6).

🔴 **`requested_history_days` and `granted_history_days` are separate columns on purpose.** AC-1.2
calls the requested window the single highest-stakes parameter in the system — it cannot be raised
after enrollment without re-linking — and AC-3.3/AC-11.8 require shortfalls to be *visible rather
than silent*. Storing only what was granted would make the shortfall unrecoverable.

🔴 **They are also written at different times, which is why neither can stand in for the other.**
`requested_history_days` is known at enrollment; `granted_history_days` is not, because no response
in the enrollment path reports it (AC-1.3a). It is filled once the initial backfill reveals the
oldest transaction actually returned. So a connection with a requested window and a null granted one
is the **normal** state between enrollment and first sync — a reader that treats null as "granted
what we asked for" converts an unmeasured window into a silent claim of completeness, which is the
exact failure AC-11.8 exists to prevent.

#### `accounts` — the unit everything else hangs from (AC-6.3, AC-6.5)

| Field | Type | Constraints | Description |
|---|---|---|---|
| `account_id` | int | PK | 🔴 **Stable local id that survives re-enrollment** |
| `institution_id` | int | required, FK | |
| `connection_id` | int | nullable, FK | Null for an import-only account |
| `source_account_id` | text | nullable | The aggregator's id; **not** the identity |
| `source_persistent_account_id` | text | nullable | The source's own cross-enrollment handle, where offered |
| `name` / `official_name` | text | | As reported |
| `mask` | text | | Last 4 — the only account-number fragment stored anywhere |
| `account_type` / `account_subtype` | text | | The **source's** vocabulary, retained verbatim |
| `balance_class` | text | `asset` \| `liability` | Local classification, operator-correctable |
| `currency` | text | | ISO code |
| `lifecycle_status` | text | `active` \| `inactive` | |
| `opened_date` | calendar date | nullable | |
| `first_seen_date` | calendar date | required | |
| `closed_date` | calendar date | nullable, `>= first_seen_date` | |
| `source` | text | `aggregator` \| `manual` | Provenance, never lost (AC-7.4) |
| `created_at` / `updated_at` | UTC instant | required | |

- `(source = 'aggregator') <= (source_account_id IS NOT NULL)`.
- Partial unique index `accounts_source_identity ON (connection_id, source_account_id) WHERE source_account_id IS NOT NULL`.

🔴 **Why `balance_class` exists as data rather than being derived from `account_type`.** `net_worth`
is an enumerated consumer of this schema and `account_type` cannot answer it: the types are the
*source's* vocabulary, they differ between sources, and an import-only account has none. Per the sign
convention below, `balance_class` partitions a *report* rather than deciding an arithmetic sign — so
a misclassification mislabels a breakdown instead of producing a wrong total. (Found by the lock-in
checkpoint re-reading the consumer questions against the delivered tables, and fixed while the schema
was still free.)

### The bronze layer (FR-5)

#### `raw_responses` — every response, verbatim, before normalization (AC-5.1)

| Field | Type | Constraints | Description |
|---|---|---|---|
| `raw_response_id` | int | PK | |
| `connection_id` | int | nullable, FK | |
| `endpoint` | text | required | Which call produced this |
| `received_at` | UTC instant | required | |
| `body_gzip` | blob | required | The response **verbatim**, compressed |
| `body_sha256` | text | required | Content hash |
| `body_bytes` | int | required, integer-typed | Uncompressed size |
| `request_context` | text (JSON) | | Parameters of the call |

Index `raw_responses_by_connection ON (connection_id, received_at)`.

*Rationale (the bronze/silver split): a categorization or derivation bug becomes a re-run rather than
a re-fetch, and lineage back to source is preserved. Disk cost is trivial at this volume.* AC-5.2
requires the silver layer to be rebuildable from this table alone.

#### `manual_imports` — the file-import counterpart (FR-7)

| Field | Type | Constraints | Description |
|---|---|---|---|
| `manual_import_id` | int | PK | |
| `account_id` | int | required, FK | |
| `adapter` | text | required | Which configured adapter parsed it (AC-7.2) |
| `source_name` | text | | Operator-facing label for the file |
| `file_sha256` | text | required | 🔴 The file's **identity** |
| `file_bytes` | int | required, integer-typed | |
| `imported_at` | UTC instant | required | |
| `rows_seen` / `rows_applied` | int | required, integer-typed | Applied ≤ seen; the difference is the duplicate-suppression count |

Unique index `manual_imports_file_identity ON (account_id, file_sha256)` — AC-7.5: re-importing the
same file is a no-op, and **an operator who renames the export cannot import it twice.**

### The silver layer

#### `transactions`

| Field | Type | Constraints | Description |
|---|---|---|---|
| `transaction_id` | int | PK | |
| `account_id` | int | required, FK | |
| `source_transaction_id` | text | nullable | Aggregator identity |
| `source_pending_transaction_id` | text | nullable | The pending row this one posted from (AC-2.3) |
| `pending` | int | required, 0 or 1 | |
| `posted_date` | calendar date | required | |
| `authorized_date` | calendar date | nullable | |
| `amount_minor` | int | required, integer-typed | 🔴 Minor units, operator-signed |
| `currency` | text | required | |
| `description` / `merchant_name` | text | | |
| `source_category_primary` / `source_category_detailed` | text | | 🔴 The source's values, **never overwritten** |
| `category_override` | text | nullable | Local interpretation, in its own column (AC-6.1) |
| `source` | text | `aggregator` \| `manual` | |
| `raw_response_id` | int | nullable, FK | |
| `manual_import_id` | int | nullable, FK | |
| `derivation_version_id` | int | required, FK | |
| `import_fingerprint` | text | nullable | Import-path identity |
| `removed_at` | UTC instant | nullable | 🔴 **Soft delete** (AC-2.2) |
| `first_seen_at` / `updated_at` | UTC instant | required | |

Identity and access indexes:

- `transactions_source_identity ON (account_id, source_transaction_id) WHERE source_transaction_id IS NOT NULL`
- `transactions_import_identity ON (account_id, import_fingerprint) WHERE import_fingerprint IS NOT NULL`
- `transactions_by_account_date ON (account_id, posted_date)` — the shape the coverage walk and every
  spending/cashflow aggregate reads in.
- `transactions_pending_link ON (account_id, source_pending_transaction_id) WHERE source_pending_transaction_id IS NOT NULL`
  — AC-2.3: a posting transaction finds its pending row **by the source's own pending identifier
  rather than by guessing from amount and date.**

#### `balances_daily` (AC-3.1)

| Field | Type | Constraints | Description |
|---|---|---|---|
| `account_id`, `as_of_date` | int, calendar date | **composite PK** | |
| `current_minor` | int | required, integer-typed | Operator-signed |
| `available_minor` | int | nullable | ⚠️ **Magnitude as reported** — a deliberate exception |
| `limit_minor` | int | nullable | ⚠️ **Magnitude as reported** — a deliberate exception |
| `currency` | text | required | |
| `captured_at` | UTC instant | required | |
| `source`, `raw_response_id`, `manual_import_id`, `derivation_version_id` | | | Provenance |

🔴 **Append, never overwrite.** No aggregator backfills a balance series, so a day not captured is a
day gone for good. The composite primary key **is the append rule made structural**: a second capture
on a day already recorded is rejected rather than allowed to overwrite, so the series does not depend
on what time of day anyone happened to look, and a re-run changes nothing (AC-2.4).

#### `securities`, `holdings`, `investment_transactions` (AC-3.2, AC-3.3)

Securities live in their own table referenced by id, so a position and a transaction in the same
instrument agree about what it is. `holdings` is a **daily snapshot on the same append rule as
balances** (composite PK `account_id, security_id, as_of_date`) — a position is what it was on a day,
and `net_worth` over time reads the series rather than the latest row.

🔴 **Quantities are exact decimal text, not minor units.** A price is money and is therefore minor
units; a *quantity* is not money — fractional shares are routine and a share is not divided into
hundredths, so a scaled integer's scale would be a guess. Text keeps the source's own precision and
converts to `Decimal` **without ever passing through a float.**

`investment_transactions` carries the same identity, provenance, and soft-delete shape as
`transactions`, keyed on `trade_date`.

### Bookkeeping

#### `sync_state` — per connection, **per domain** (FR-2, FR-4)

| Field | Type | Constraints |
|---|---|---|
| `connection_id`, `domain` | int, text | **composite PK**; `domain IN ('transactions','balances','investments')` |
| `cursor` | text | nullable |
| `history_start_date` | calendar date | nullable |
| `last_attempt_at`, `last_success_at`, `last_error_at` | UTC instant | nullable |
| `last_error_code` | text | nullable |
| `updated_at` | UTC instant | required |

One row per connection **per data domain**, because the domains advance independently: transactions
have a cursor, balances and investments are pulled whole, and a connection may be healthy for one and
failing for another.

🔴 **`last_success_at` is nullable only for a domain that has never once succeeded.** AC-4.5: a
degraded record without it is *insufficient*, because the size of the resulting hole must be
computable rather than guessed.

**Why `domain` is a CHECKed vocabulary and `account_rules.rule_type` is not** — deliberate, not an
oversight. A misspelled domain is a class of data that silently never syncs, which is the exact
failure this product exists to prevent, so it is worth a migration to widen. A rule row with an
unrecognized type is inert: nothing applies it, and the rule engine validates against its own
registry.

#### `account_rules` (FR-8)

| Field | Type | Constraints |
|---|---|---|
| `account_rule_id` | int | PK |
| `account_id` | int | required, FK |
| `rule_type` | text | required (validated by the engine's registry, not by CHECK) |
| `parameters` | text (JSON) | required |
| `active` | int | required, 0 or 1 |
| `note` | text | nullable |
| `created_at` / `updated_at` | UTC instant | required |

Unique index `account_rules_one_per_type_per_account ON (account_id, rule_type)`.

🔴 AC-8.1/AC-8.5: **the rule *type* is code; everything else is data.** Applying an existing type to
an account is a row, never a branch, and never keyed to a named institution.

`parameters` is JSON because each rule type has its own shape. **Recorded cost:** for
`contribution_only` (AC-8.2) it carries the set of local account ids whose inbound transfers count as
contributions (AC-8.4), and those ids **are not foreign keys** — SQLite cannot reference into a JSON
document — so the rule engine must validate them when it lands. That cost is recorded here rather
than discovered there.

---

## Relationships

```
derivation_versions ──(stamps every silver row)──┐
                                                 │
institutions ──1:N──> connections ──1:N──> accounts ──1:N──> transactions
      │                     │                   │      └───> balances_daily      (1:N, one per day)
      └────────1:N──────────┼───────────────────┤      └───> holdings            (1:N, one per security per day)
                            │                   │      └───> investment_transactions
                            │                   └───> account_rules              (1:N, ≤1 per rule_type)
                            │                   └───> manual_imports             (1:N)
                            └──1:N──> raw_responses
                            └──1:N──> sync_state   (exactly one per domain)

securities ──1:N──> holdings
           ──1:N──> investment_transactions
```

- An **account** belongs to an institution directly *and* optionally to a connection. The direct link
  is what lets an import-only account exist with no connection at all, and what keeps history
  attached when a connection is retired.
- A **transaction** hangs from an account, never from a connection — so re-enrolling an institution
  cannot orphan it (AC-6.3).
- **Cardinality note:** `connections_one_live_per_institution` makes institution→connection
  effectively 1:1 *among live connections*, and 1:N across history.

---

## State Machines

### Connection status

```
        enroll
  (none) ──────> active ──────────────> degraded
                   │  ^                    │
                   │  └────── repair ──────┘   (update-mode re-auth, AC-4.3:
                   │                            history and cursor preserved)
                   └──── retire ────> retired   (terminal for sync; history retained, AC-1.6)
```

`retired` is terminal for *syncing*, not for data: retiring never deletes history, and a retired
connection's accounts keep every row they had.

### Account lifecycle (AC-6.5)

```
  active ──── closed/retired ────> inactive   (closed_date set)
```

🔴 **A retired account's dormant period must not read as a permanent coverage gap.** The coverage
report reads `lifecycle_status` and `closed_date` so a post-closure silence is *identified as
closure*, not reported as a hole.

### Transaction lifecycle (AC-2.2, AC-2.3)

```
  (absent) ──> pending=1 ──> pending=0        (posts; UPDATES the pending row,
       │                        │              matched on source_pending_transaction_id —
       │                        │              never inserts a second row)
       └────────────────────────┴──> removed_at set   (soft delete, never DELETE)
```

A removed transaction is **retained with a removal timestamp**. Nothing in this schema hard-deletes a
transaction.

---

## Constraints

The invariants that span entities. Each is enforced **in the database**, where no future writer can
forget it — including a `sync shell` session typing raw SQL, which is the layer a Python-side check
would not survive.

### 1. 🔴 Sign convention — stated once, depended on everywhere

**Every stored amount is signed from the operator's point of view.** A positive `amount_minor` is
money moving *into* an account; a negative one is money moving out. A positive `current_minor` is
value the operator holds; a negative one is value they owe — **so a credit card's balance is stored
negative.** Aggregators disagree with each other, and several report a card balance as a positive
amount owed, so **normalizing to this convention is the connector's job.**

One convention rather than one per account type is what keeps two later things from needing a special
case: net worth is a plain sum, and AC-11.2's reconciliation is "change in balance equals sum of
transactions" for *every* account.

**Exceptions, deliberate and named:** `available_minor` and `limit_minor` hold magnitudes as the
source reports them. Neither participates in net worth.

*Alternative rejected:* store balances as the source reports them and let each consumer apply the
sign per account type. That makes net worth type-dependent, and any consumer that forgets the special
case is wrong **by twice the debt** — silently, and plausibly.

### 2. 🔴 Money is integer minor units, everywhere (AC-6.2)

No floats in the schema or in aggregation code. Enforced by `CHECK (typeof(x) = 'integer')` on every
monetary column — **necessary because SQLite's typing is dynamic**: a column declared INTEGER will
hold a float without complaint. This CHECK is the difference between a rule that is enforced and one
that is merely written down.

### 3. 🔴 Calendar dates and UTC instants never mix (AC-6.4)

Transaction dates are **dates**; sync metadata is **UTC timestamps**. Enforced at three layers:
`CalendarDateColumn` / `UtcInstantColumn` in the metadata, mypy in the code, and in the file itself by
`GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'` and `LIKE '%+00:00'`.

*Why it matters:* a transaction date is a calendar fact from the institution, not an instant. Treating
it as one introduces silent off-by-one-day errors **at period boundaries — exactly where
period-over-period comparisons live**, which is the product's headline query.

### 4. Provenance is exclusive and total

Every silver row is *either* aggregator-sourced *or* manually imported, and carries the evidence for
whichever it is. On `transactions` and `investment_transactions`:

```sql
CHECK (CASE source
         WHEN 'aggregator' THEN source_transaction_id IS NOT NULL
                            AND raw_response_id     IS NOT NULL
                            AND manual_import_id    IS NULL
         ELSE                    manual_import_id   IS NOT NULL
                            AND import_fingerprint  IS NOT NULL
                            AND raw_response_id     IS NULL
       END)
```

`balances_daily` and `holdings` carry the same rule on their raw/import pair. This is **AC-7.4 made
structural**: provenance is never lost, so every MCP result can distinguish imported rows from
aggregator rows.

### 5. Idempotency is a property of the store, not a discipline of the code

Partial unique indexes carry AC-1.4, AC-2.4, and AC-7.5 into the database: source identity where a
source id exists, import fingerprint where it does not, file hash for imports, one live connection per
institution, one rule per type per account. A second consecutive sync produces zero net changes
because the store will not accept anything else.

### 6. Never overwrite a source value in place (AC-6.1)

Source categories are retained; local interpretation lives in `category_override`. Overwriting
destroys the ability to re-derive **and** the ability to distinguish provider drift from local intent.

### 7. Rebuildability (AC-5.2, AC-11.5)

The silver layer is reproducible from `raw_responses` alone, **byte-identically given a recorded
derivation version.** Every silver row's `derivation_version_id NOT NULL` is what makes that statement
checkable rather than aspirational.

---

## Recorded limitations

Stated as decisions rather than discovered later as gaps.

- **Multi-currency net worth is out of scope.** Amounts carry a currency code, but there is no FX rate
  table and no conversion. Adding rates later is a new table and a derivation version, not a reshaping
  of the existing ones. *A rate series nobody populates is worse than an absent one.*
- **Liability-product detail is descoped for v1** (AC-3.4). Loans and lines of credit are covered by
  account type, balance, and transactions — the same path as any other account. APR, minimum payment,
  payoff date, and statement schedule have **no capability and no table**. This is a recorded descope:
  a debt's contribution to net worth and cashflow needs only its balance and its transactions.
  Adding liability detail later is a new capability under AC-3.2's model, not a redesign.
- **JSON parameters are not referentially checked.** See `account_rules` above — the cost is the rule
  engine's to pay, and it is recorded rather than discovered.
- **`capabilities` is JSON text, not a table.** Capabilities are discovered per connection at
  enrollment and drive AC-3.2's investment pulls. A capability table would be a join for a value read
  once per connection per run.
