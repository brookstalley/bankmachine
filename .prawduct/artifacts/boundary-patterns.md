# Boundary Patterns — bankmachine

<!-- Contract surfaces where components interact. When changes cross these
     boundaries, the builder investigates consumer impact before completing
     the chunk. The Critic verifies investigation occurred. -->

Populated 2026-09-06 with Chunk 02 of `build-plan-datastore-v1.md`, whose first
contract surface — the datastore schema — is the reason this file exists.
`.prawduct/artifacts/architecture.md` names that chunk as this document's owner.

Two surfaces below are **not built yet** and are recorded anyway, with the build
step that will own them. A boundary named before it exists is a boundary someone
can design against; one recorded afterwards is an audit of what already leaked.

## Contract Surfaces

### Database Schema — the load-bearing one

**Producer:** `src/bankmachine/store/migrations/core_schema.py` — the frozen DDL
that creates the tables, and the only thing that ever has.
`src/bankmachine/store/schema.py` — SQLAlchemy Core metadata describing the same
tables for the query builder.

**Consumers:** everything downstream of the store. Today: `store/engine.py`,
`cli/store.py`. By build step: the sync writer (steps 4–6), the rebuild path
(FR-5), `sync shell` (Chunk 04), the MCP tool surface (step 7), and the import
adapters (step 10). The MCP tools in `system-requirements.md` §5 are the
consumers this schema was designed *from* — its columns were chosen by
enumerating their queries before any field was drawn.

**Contract:** table and column names, types, nullability, and the constraints
that carry requirements. Five parts of it are load-bearing and none are
inferable from a column name:

1. 🔴 **Sign convention.** A positive `amount_minor` is money moving *into* the
   account; negative is out. This holds for every account type, liabilities
   included. Aggregators disagree with one another and several use the opposite
   sign, so normalizing is the connector's job, and AC-9.4 requires every MCP
   tool description to state the convention it is reporting in.
2. **Money is integer minor units, always** (AC-6.2), with the currency in a
   column beside it. Enforced at the database by `typeof(x) = 'integer'`, not
   only by the column type — SQLite will store a float in an INTEGER column
   without complaint.
3. **Two kinds of time, never mixed** (AC-6.4). Calendar dates are `YYYY-MM-DD`;
   instants are ISO-8601 ending `+00:00`. mypy holds them apart in code
   (`store/types.py`), CHECK constraints hold them apart in the file.
4. **Provenance on every normalized row** (AC-7.4). `source` is `aggregator` or
   `manual`, and exactly one of `raw_response_id` / `manual_import_id` is set —
   a CHECK, so a row cannot claim an origin it has no link to. Every row also
   names the `derivation_version_id` that produced it (AC-5.3), which is what
   makes rebuild losslessness (AC-11.5) a well-defined claim.
5. **Local ids are the only stable ones.** History references `account_id`, never
   `source_account_id`, because the aggregator's id changes when a connection is
   re-linked (AC-6.3).

**How the contract is kept:** the DDL and the metadata are written independently
and compared column by column on every test run
(`tests/store/test_schema.py::test_the_metadata_matches_the_migrated_database`).
Generating one from the other would remove the disagreement that makes drift
visible. Migrations are forward-only and never edited: changing the schema is a
new migration, and `SUPPORTED_SCHEMA_VERSION` in
`src/bankmachine/store/connection.py` moves with it — a process that does not
recognize the version refuses to serve. "Never edited" is itself a mechanism
rather than a promise: `CORE_SCHEMA_DDL_SHA256` records a hash of migration
002's rendered statements, so any change to them — including one made through
the constraint idioms a later migration would want to share — fails a test.

**Crossing it:** adding or changing a column touches every consumer above,
including ones not yet written. Adding a table is also a change to
`schema.py::CORE_TABLES`, which is deliberately a literal list so a table added
without a place in FR-6 fails a test rather than quietly widening the contract.

### Connection Roles

**Producer:** `src/bankmachine/store/connection.py` — the only module that
constructs a connection, in exactly two roles.

**Consumers:** every module that touches the datastore, via `store/engine.py`.

**Contract:** a writer handle comes from the one factory, which takes the
exclusive `flock` before it returns; a read-role handle is opened `mode=ro`,
holds no snapshot beyond the statement that needs it, and never falls back to a
writable handle. The four norms in `architecture.md` § Direction are this
contract, and `tests/preferences/test_connection_is_the_sole_constructor.py`
enforces the structural half of it.

🔴 **The inherited surprise:** every handle is in autocommit
(`isolation_level=None`), so `engine.begin()` opens no transaction and block-exit
rollback undoes nothing. A multi-statement unit that must be atomic issues
`BEGIN IMMEDIATE` / `COMMIT` on the driver, the way the migration runner does.

### Configuration Interface

**Producer:** `src/bankmachine/config.py` — the resolved `Config`, and the only
place a filesystem path is decided (AC-ARCH.4).

**Consumers:** every entry point. Values arrive by precedence: explicit argument,
`BANKMACHINE_*` environment variable, config file, documented default.

**Contract:** every path is absolute by the time it leaves `load_config`, and
sandbox and production resolve to different datastore files and different
keychain accounts, so cross-contamination requires an explicit override (AC-10.6).

### Credential Seam

**Producer:** `src/bankmachine/secrets.py` — the only module that imports
`keyring`.

**Consumers:** `store/connection.py` today; the aggregator client from step 2.

**Contract:** secrets are fetched by name and never returned into a log line, an
exception message or a `repr` (AC-10.1, AC-10.3). The datastore holds
`connections.credential_ref` — the *name* of a keychain entry, never a token,
because a datastore backup travels and a credential inside it travels with it.

### MCP Tool Surface — *not built; build step 7*

**Producer:** `src/bankmachine/mcp/` (does not exist yet).
**Consumer:** an MCP client, and through it an analyst agent.
**Contract:** `system-requirements.md` §5 — the ten tools, aggregate-first, with
a freshness stamp on every response (AC-9.2), explicit warnings over gapped or
degraded data (AC-9.3), and stated units, sign conventions and applied rules
(AC-9.4). This *is* the product's public API contract; `api-contract.md` is the
artifact that will hold it, and it does not exist yet.

### Aggregator Client — *not built; build step 2*

**Producer:** `src/bankmachine/connector/` (does not exist yet).
**Consumer:** the sync path, and the raw-response layer that persists what it
returned before anything normalizes it.
**Contract:** open in `system-requirements.md` §9.2 — whether a second aggregator
is ever expected decides how hard this boundary is drawn.

## Test Levels

| Level | Exists | When to Run | Location |
|-------|--------|-------------|----------|
| Unit | yes | Every change | `tests/test_*.py`, `tests/store/test_types.py` |
| Integration | yes — against real SQLCipher and the real keychain, never mocks | Changes crossing boundaries | `tests/store/`, `tests/cli/` |
| Contract | yes — schema drift, norm structure, type-checker assertions | Schema or connection-layer changes | `tests/store/test_schema.py`, `tests/store/test_temporal_types_are_distinct.py`, `tests/preferences/` |
| End-to-end | partial — `store init` / `store status` drive the real CLI; the full pipeline arrives with build step 9's verification gate | Before release / major features | `tests/cli/`, and `docs/system-requirements.md` §7 |

**The negative-control run.** `uv run python tests/preferences/verify_norms_go_red.py`
breaks each structural guarantee in turn and confirms its test goes red. It is
not collected by pytest because it edits the source tree. Run it whenever the
connection layer or the schema changes: a contract test that has never been red
is a claim, not a check.
