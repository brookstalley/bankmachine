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
  Scope (2026-09-09, AC-14.1): 🔴 **this norm is a PER-FEED claim, and a second institution is a new
  observation rather than a covered case.** The normalization it requires is an *unconditional*
  negation — `connector/plaid/derivers.py::_operator_signed_amount` negates every amount with no
  branch and no condition — and the premise that makes the negation right was measured on **one
  aggregator, one connection, one row**: `api-notes-plaid.md` §17, a merchant purchase arriving with
  a positive `amount`. The norm therefore holds where it was measured and is *assumed* everywhere
  else. An institution that signs its feed the other way is stored inverted, silently, and its
  spending reads as income in every aggregate over it.
  This is recorded as scope rather than as a caveat on the statement because the statement is not
  weakened: the convention is still what every stored amount means, and no consumer applies its own
  sign. What is bounded is the *evidence* for the conversion that produces it.
  Mechanism (AC-14.2): `src/bankmachine/signs.py` measures the stored sign distribution per
  connection over five never-plausibly-inflow categories and **reports** a connection whose
  distribution is inverted — it never corrects one, per AC-14.4, because inverting on suspicion
  fails in the direction that understates spending. The verdict rides `get_pipeline_health` per
  connection, and an inverted connection raises `sign_convention_unverified` on the success path.
  The check's negative control is the sandbox baseline measured on 2026-09-09 (219 rows in those
  categories on the single enrolled connection, **219 negative and none positive**); its positive
  control is a synthetic inverted feed in `tests/test_sign_convention.py`, carried in
  `verify_norms_go_red.py` so the go-red is re-proved rather than remembered.
  🔴 **The check is meaningless on one connection, which is why this scope is still open.** Until it
  has run across two institutions the norm remains verified for one feed only — AC-14.7 and AC-14.8,
  enqueued as VRF-006 in `.prawduct/operator-verification.md`, are what close it, and they are the
  operator's.

  > **Amendment, 2026-09-10 — a valuation is rounded to a minor unit this build KNOWS, and refused
  > otherwise.** *Statement:* the rounding clause above holds where the currency's minor-unit
  > exponent is a recorded fact. Where it is not, there is no scale to round to, and the row is
  > refused rather than approximated — per ROW, never per connection, with the raw response keeping
  > the value.
  > *Why:* the clause was implemented against a lookup that answered **2 for anything it did not
  > recognize**, which is ISO 4217's convention for its own codes and is not a fact about the codes
  > the aggregator sends in `unofficial_currency_code`. So `0.04217` in a cryptocurrency became
  > `0.04` — a 0.4% loss, disclosed only in a log line no caller of the MCP surface or the CLI ever
  > sees, and inherited by every total computed from it. That is neither exact nor refused, which
  > is the one state this norm's whole point is to exclude. The alternative considered and rejected
  > was to store the rounded value and flag it: a number wrong by 0.4% and marked is still wrong in
  > every identity this store maintains over it.
  > *Retroactivity:* none owed against stored rows — the exponent table gains every ISO 4217 code
  > rather than losing one, so no currency that derived exactly before derives differently now.
  > Rows previously rounded under a guessed exponent are re-derived by `store rebuild`, which the
  > derivation-version bump makes a recorded change rather than a silent one.
  > *Mechanism:* `store/types.py::minor_digits` raises `UnknownMinorDigitsError` rather than
  > defaulting; `has_minor_digits` is the same question asked without an exception, and is what the
  > read path uses to exclude such an account from a minor-units aggregate and name it under
  > `rule-applied`. Adding a currency's exponent to that table is the whole remedy: the amount then
  > derives exactly and nothing is refused or warned about.
  Status: steady-state.

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
| `roster_observed_date` | calendar date | nullable | 🔴 The date this connection's roster was last successfully READ (AC-12.4). Null means never observed, which marks nothing absent. Migration 004 |
| `consent_expires_at` | UTC instant | nullable | When the operator's authorisation for this connection lapses, from `/item/get`. Migration 008. 🔴 The pipeline is poll-only, so without this an expiry surfaces as a failed run rather than in advance — a connection reads healthy right up to the sync that fails. Null means the Item has not been polled since the column existed, never that consent does not expire |
| `source_error_code` | text | nullable | The aggregator's STANDING complaint about the Item, from `/item/get`. Migration 008. 🔴 Not `last_error_code`, which records the last sync *attempt* failing: an Item can be unwell while the most recent poll succeeded, and folding the two together would let one success bury a complaint nobody resolved |
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
| `source_persistent_account_id` | text | nullable | 🔴 The source's own cross-enrollment handle, where offered — **the first key account matching consults**, scoped to the institution |
| `name` / `official_name` | text | | As reported |
| `mask` | text | | Last 4 — the only account-number fragment stored anywhere |
| `account_type` / `account_subtype` | text | | The **source's** vocabulary, retained verbatim |
| `balance_class` | text | `asset` \| `liability` | Local classification, operator-correctable |
| `currency` | text | nullable | The unit the account is denominated in. 🔴 Null means **the aggregator has not stated one**, never `USD` and never another account's unit (migration 006) |
| `lifecycle_status` | text | `active` \| `inactive` | |
| `opened_date` | calendar date | nullable | |
| `first_seen_date` | calendar date | required | |
| `closed_date` | calendar date | nullable, `>= first_seen_date` | Operator-owned, like `lifecycle_status` |
| `source` | text | `aggregator` \| `manual` | Provenance, never lost (AC-7.4) |
| `created_at` / `updated_at` | UTC instant | required | |
| `last_seen_date` | calendar date | nullable | 🔴 The date this account was last listed on a successful roster observation, as a **monotone maximum** (AC-12.4). Migration 003; last in column order because `ALTER TABLE` appends |

- `(source = 'aggregator') <= (source_account_id IS NOT NULL)`.
- Partial unique index `accounts_source_identity ON (connection_id, source_account_id) WHERE source_account_id IS NOT NULL`.
- 🔴 **Matching is `source_persistent_account_id` first, `(connection_id, source_account_id)` second.**
  A full re-link issues a new Item, and the new Item issues a NEW id for every account — so matched on
  the index alone the same real account appears a second time, the whole granted history is re-fetched
  against it, and annual spending doubles. Where the aggregator supplies a persistent identity it
  decides: the newly issued `source_account_id` and `connection_id` are written onto the existing row
  and the `account_id` every row of history references survives.
  🔴 **Scoped to the institution, never globally.** Nothing makes the field unique across banks, and a
  global match would turn a collision between two of them into a silent merge of two real accounts.
  🔴 **Absence is ORDINARY.** The aggregator supplies it for select institutions only; the fallback is
  the index above, and a re-link at such an institution still duplicates. That is **not fixed here** —
  the remedy is re-authorising in place, which keeps the same Item and so creates no second lineage.
  🔴 **A recorded persistent identity is kept when a body omits it**, for the reason a recorded
  currency is: blanking it on an ordinary sync would remove the only key the next re-link can converge
  on, and nothing would look wrong until it duplicated.
- 🔴 **`last_seen_date` is nullable and was not backfilled, and the null is load-bearing.** It means
  *no roster observation is recorded for this account*, and `query._account_lifecycle` reads it as
  exactly that — never as a date. The deeper reason there is no backfill is that there is nothing
  honest to backfill with: the correct value is the connection's last successful roster observation,
  and the absence of that record is the whole reason AC-12.4 exists.
  ⚠️ **Reading a null as `first_seen_date` was tried and is wrong**, recorded so it is not
  re-proposed as an obvious simplification. It looks true for one account and breaks across a
  connection: the verdict compares an account against the maximum over its siblings, first-seen dates
  legitimately differ between them (a second card, a later savings account), and every older account
  then fell behind that maximum and was reported closed for the whole window until the connection's
  next successful sync. A connection with no observation marks nothing absent; once any of its
  accounts carries one, an account still null was genuinely not in that roster.
- 🔴 **`currency` is nullable and its null is load-bearing too.** It means *the aggregator has not
  told us what unit this account is in* — never `USD`, never the unit of the operator's other
  accounts. Both of the aggregator's currency fields are documented nullable, and while the column
  was NOT NULL an account of that shape could not be written at all: it was skipped, invisible to
  `list_accounts`, and every transaction on it went on refusing to derive until a later sync
  happened to state one. The transactions never needed it — they carry their own
  `transactions.currency`, stated per row.
  ⚠️ **The four other NOT NULL currency columns stay NOT NULL** — `transactions`, `balances_daily`,
  `holdings`, `investment_transactions`. Each describes ONE amount, and an amount whose unit nothing
  stated is refused row by row with a named reason; an account with no unit *yet* is merely
  incompletely known. Widening those too would turn a narrow honesty fix into a store-wide
  loosening.
  🔴 **Such an account, and one whose currency has no known minor-unit exponent, are excluded from
  every minor-units aggregate, and the exclusion is disclosed** under `rule-applied` naming the
  account (`query._undenominable_accounts`). Adding an amount whose unit or scale is unknown to a
  total in a unit that is known produces a figure that means nothing, and an exclusion nobody
  announces is one that gets silently forgotten during analysis.
  Migration 006 dropped the NOT NULL by rebuilding the table — SQLite cannot drop one in place — and
  **no row changed value**: every account in every datastore had a currency, so the copy is the
  identity and the migration is schema-only.

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
| `ledger_date` | calendar date | nullable | 🔴 `COALESCE(authorized_date, posted_date)`, stamped **once** on insert and never moved by settlement — the day the money was committed. Migration 005; last in column order because `ALTER TABLE` appends. Null means *predates the split, not yet rebuilt*, never *committed on the posting date* |
| `lineage_id` | int | nullable, FK → `connections` | 🔴 The aggregator Item that produced the row. Migration 007; last in column order for the same reason. Null means *predates the split, not yet rebuilt* or *came from an operator file*, never *the current Item* — so a null-lineage row is always counted |
| `transfer_pair_id` | int | nullable | 🔴 The other leg of one transfer: both rows carry the same value, and neither is the other's parent — a shared token, not a pointer, which is why it has no FK. Migration 009; last in column order for the same reason. Set by the pairing pass over the categories a pair can change the meaning of, and **recomputed from scratch on every pass** so the classification is a function of the store rather than of the order pages arrived in. Null means *no counterparty leg was found here*, which is the ordinary case and is what makes a transfer-shaped row count as money that left |

🔴 **Lineage, and why a re-link needs one.** Removing a connection and linking it again yields a new
Item, and the new Item re-issues every transaction id — so the whole granted history arrives again as
rows this store has never seen, against an account that converged on its persistent identity, with
nothing for a unique index to collide with. Summed naively, a year of spending doubles. `connections`
is already this store's row per Item (`source_connection_id` holds the Item id and is unique) and the
sync cursor and the measured granted window are already Item-scoped, so this column is that same
boundary recorded on the rows rather than a parallel mechanism.

**Every row is kept; aggregates count one lineage.** For a range covered by more than one Item the
NEWEST answers, older ones answer only outside it, and the overlap is disclosed with `rule-applied`
naming the account and the superseded range. The excluded rows stay in the store and stay reachable —
consistent with § Direction's rule that a transaction is never hard-deleted. `store/lineage.py` is the
one place the rule lives: `superseded_spans` computes it, `counts_once` filters a query by it, and
`only_superseded` asks for exactly what was left out.

🔴 **Rejected: natural-key dedupe** on `(account_id, ledger_date, amount_minor, normalized name)`.
It is what most systems do and it is wrong here: two genuinely distinct real transactions with the
same merchant, amount and day — two $5 coffees, two identical fares — collapse into one and the total
goes **down** with nothing to signal it. This product's asymmetry is that an overcount gets questioned
and an undercount gets believed, so a rule that can silently delete real money is on the wrong side of
it. Lineage exclusion fails the other way: a boundary computed wrongly leaves a **visible** duplicate
or a **disclosed** exclusion.

Identity and access indexes:

- `transactions_source_identity ON (account_id, source_transaction_id) WHERE source_transaction_id IS NOT NULL`
- `transactions_import_identity ON (account_id, import_fingerprint) WHERE import_fingerprint IS NOT NULL`
- `transactions_by_account_date ON (account_id, posted_date)` — the shape the coverage walk and every
  spending/cashflow aggregate reads in.
- `transactions_pending_link ON (account_id, source_pending_transaction_id) WHERE source_pending_transaction_id IS NOT NULL`
  — the **reverse** lookup: given a hold, which row settled out of it, and in the aggregate, which
  rows in a window arrived by replacing a hold. `query._hold_transitions` is the reader, and AC-13.4
  is why it exists — a total that moved because a hold settled has to be attributable, and a settled
  row is otherwise indistinguishable from any other posted row. Asking for it as
  `source_pending_transaction_id IS NOT NULL` is what lets SQLite plan against this partial index,
  which holds only the small minority of rows carrying a link; asserted by `EXPLAIN QUERY PLAN` in
  `tests/test_pending_semantics.py`, over the statement the engine actually ran.

  🔴 **This index does NOT serve AC-2.3's pending→posted match, and the line that said it did was
  wrong** (found 2026-09-09, resolved under AC-13.6 by giving the index a reader rather than dropping
  it). AC-2.3's match is served by `transactions_source_identity`: `_existing_transaction`
  (`connector/plaid/derivers.py`) compares an incoming `pending_transaction_id` against the pending
  row's **own** `source_transaction_id`, because a hold answers to its own id right up until it
  posts. That is the correct lookup and it is unchanged. One account of the two directions, held
  here, on the `Index` in `store/schema.py`, and in `_existing_transaction`'s docstring.

  **Corrected in the DDL too, 2026-09-09.** The comment beside this index in
  `store/migrations/core_schema.py` repeated the withdrawn claim, and it was left standing for one
  commit on the belief that the frozen-DDL norm covered it. It does not: `CORE_SCHEMA_DDL` is a tuple
  of statement *strings* and the hash is taken over those, so a Python comment between them is
  outside the freeze. That distinction is worth keeping — "the DDL is frozen" is a rule about what
  the database was told, not about the prose around it — and the comment beside an index is what the
  next reader believes, so leaving it wrong while correcting only this document would have kept the
  misleading half exactly where it does its damage.

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

### Account lifecycle (AC-6.5, FR-9)

**Two axes, and keeping them apart is the design.** One is what the *operator declared*; the other
is what the *institution last did*. They are stored separately, and only the read path combines them.

```
  stored (operator's):   active ──── retire ────> inactive   (closed_date set)

  observed (roster's):   last_seen_date, a monotone MAXIMUM per account
                         roster_observed_date, RECORDED per connection when the roster is read
```

🔴 **The connection's observation is stored, not derived, and that is an amendment (2026-09-09).**
It was `MAX(last_seen_date)` over the connection's own accounts. A maximum taken over the accounts
that were listed moves with them, so a roster that comes back empty leaves nothing behind it and the
absence becomes inexpressible exactly when it is real — which at a one-account connection is the
ordinary case, not an exotic one. Storing it separates *we looked* from *here is what we found*.
The argument for deriving is preserved in AC-12.4: a derived value cannot disagree with the rows it
is computed from. It lost to a fact it could not express at any scale.

🔴 **It is a CALENDAR DATE, and the name says so.** `roster_observed_at` was the obvious spelling and
is wrong here: the value's only use is a comparison against `accounts.last_seen_date`, which is a
calendar date, and § *Calendar dates and UTC instants never mix* makes a comparison across the two
types a defect rather than a conversion. `connections.last_success_at` already carries the instant
for anything that needs sync timing — and it is **not** a substitute for this column, because it
records a sync attempt succeeding rather than a roster being read, and the two diverge whenever a
sync succeeds without a roster call.

The value on the wire is derived from both, at read time, and is **never stored**:

| `lifecycle` | Means | Reached by |
|---|---|---|
| `active` | Declared active, and listed on this connection's most recent successful roster | the ordinary case |
| `closed` | `lifecycle_status = 'inactive'` | 🔴 the **operator's declaration only** — the one value that asserts a closure |
| `no_longer_reported` | Declared active, but `last_seen_date < roster_observed_date`, or `last_seen_date` is null while the connection has an observation | derived from the observations |

🔴 **`no_longer_reported` names the observation, never the conclusion (AC-12.2).** The aggregator
publishes no closure signal, so a value called `closed` computed from absence would be a confident
wrong number. Absence is equally consistent with closure, with the account being de-selected from
sharing, and with the institution changing what it shares — and the schema field's own description
says so, in the payload.

🔴 **AC-12.5's clauses rest on the RECORDED observation, and each is a separate fact rather than a
consequence of one arithmetic.** A connection whose roster could not be fetched records no
observation, so there is nothing for an account to be behind and nothing is marked absent. A
connection whose roster came back empty *did* record one, so every account on it is behind it and
every account is marked absent — and `roster_observed_empty` rides the answer beside them, because
the account-level truth and the connection-level anomaly are two facts and publishing only one of
them is what the previous design got wrong. An import-only account has no connection, so it has no
roster to be absent from and the comparison is never reached.

🔴 **Two dates, and the difference between them is the whole mechanism.**
`connections.roster_observed_date` says *we looked*; `accounts.last_seen_date` says *and this is what
we found*. Deriving the first from the second — the design this replaced — collapses them, and a
collapsed pair cannot express "we looked and this account was not there" at any roster size, because
a maximum over the accounts that were listed moves with them. That is why the amendment is a stored
column rather than a better formula.

🔴 **The operator's declaration outranks the derived signal, and a rebuild never undoes it
(AC-12.6).** `_OPERATOR_OWNED` keeps the accounts deriver out of `lifecycle_status`, and `accounts`
carries no `derivation_version_id` — so `store.rebuild` classifies it as a dimension and never
empties it. The observation still rides the row beside the declaration, as evidence.

⚠️ **`closed` is reachable in the read path and unreachable in the product.** No CLI command and no
MCP path can set `lifecycle_status` — the MCP surface is read-only by norm, and no `accounts retire`
command exists. Until one does, the system can report what it suspects and the operator cannot
confirm it.

🔴 **A retired account's dormant period must not read as a permanent coverage gap (AC-12.7).**
`get_coverage_report` reads the same producer `list_accounts` does (`query._account_lifecycle`) and
leaves `silence_exceeds_cadence` false for a non-active account. The `silence_ratio` beside it is
still reported as measured: it is the evidence, and nulling it would destroy the number the ruling
on that field exists to preserve.

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
