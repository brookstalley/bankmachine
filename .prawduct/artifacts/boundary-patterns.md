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
`store/raw.py`, `store/derivation.py`, `store/rebuild.py`, `cli/store.py`. By
build step: the sync writer (steps 4–6), `sync shell` (Chunk 04), the MCP tool
surface (step 7), and the import adapters (step 10). The MCP tools in `system-requirements.md` §5 are the
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
   makes rebuild losslessness (AC-11.5) a well-defined claim. **These two foreign
   keys are also the schema's classification**: which tables a rebuild may empty,
   and which a deriver must upsert, are read off them rather than off a list
   anyone maintains — see the Derivation Seam, clause 6.
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
`BEGIN IMMEDIATE` / `COMMIT` on the driver — through `engine.transaction(conn)`,
which is that workaround kept in one place rather than repeated at call sites. It
does not nest.

**Taking a handle:** callers use `engine.writer_connection(config)` /
`engine.reader_connection(config)` and never call `engine.connect()` themselves.
A role is decided by the parameters a handle is opened with — URI mode, key, lock
taken first — and a pool checkout carries none, which is the discriminator
`tests/preferences/test_connection_is_the_sole_constructor.py` now uses in place
of matching on the name `connect`.

### Operator SQL Surface — `sync shell`

**Producer:** `src/bankmachine/cli/sync.py` — the only place operator-supplied
SQL reaches the datastore (AC-ARCH.6).

**Consumer:** a human at a terminal, and any transcript they keep.

**Contract:** the shell takes a read-role handle from `store/connection.py` and
adds nothing of its own to the refusal — `mode=ro` at the file is what makes
`PRAGMA query_only = OFF` typed at this prompt harmless. Everything it writes,
including the statement it echoes back in a piped session, goes through
`logging_setup`'s redaction, so AC-10.3 has one rule for values rather than one
per surface. Three boundaries make that rule precise:

- **Values, not numbers.** Money here is an INTEGER of minor units while an
  account number is TEXT (`accounts.mask`), so redacting integers would blank a
  six-figure balance and protect nothing.
- **Values, not structure.** Schema text is not a redaction surface at all.
  Everything in `sqlite_master` was authored by this repo's migrations, and
  AC-6.6 — enforced by `tests/preferences/test_no_provider_identity.py` — is
  that no institution, account or product identity is encoded in the schema, so
  there is nothing there for AC-10.3 to protect. Running the value rule over it
  destroys it instead: `_OPAQUE` blanks any 32-plus character run, and
  `source_investment_transaction_id` is exactly 32, so the column name came out
  `[REDACTED]` until this split.
- **Free text, separated by the store rather than by shape.** The echoed
  statement and driver error messages mix structure and value inside one
  string, and no *shape* tells the two apart — so the separator is the store
  itself. `_schema_identifiers` reads every table, view, index and column name
  out of `sqlite_master` once, when the shell opens, and
  `logging_setup.redact_free_text` spares a run that names one of them.
  `SELECT source_investment_transaction_id FROM investment_transactions;`
  echoes intact, and `no such column: <that name>` names the column.

🔴 **That narrowing is held to three properties, and each one is what keeps it
from sparing a secret.** Only the opaque rule takes the exemption, so a value
introduced as a credential is blanked whatever it looks like — `access_token=`
followed by a column name is still `[REDACTED]`. It matches a *whole* run, so a
token that merely contains a column name is still blanked. And it spares only
what **this** datastore holds, all of which was authored by this repo's
migrations — where `tests/preferences/test_no_credentials_tracked.py` and
`test_no_provider_identity.py` between them already refuse a credential-shaped
string and a roster name, so there is nothing in that set to spare. A name the
set has not heard of — an identifier-shaped thing the operator typed, or a
column a migration added under a live prompt — is over-redacted, which is the
direction this surface stays wrong in.

Row values keep bare-length matching, cost included: `raw_responses.body_sha256`
is 64 hex characters and is blanked. That is the direction to be wrong in while
"no credential is persisted verbatim" is still a recorded decision rather than a
mechanism, and it costs little — the join back to the archive is the integer
`raw_response_id`, which is not redacted.

**No writer shell.** The plan left one optional. It is declined: the product is
read-only, a writer shell would hold the exclusive `flock` for the whole
session — so an overnight prompt would block the nightly sync outright, not
merely starve its checkpointer — and hand-typed rows have no raw response
behind them, which is what `store rebuild`'s content digest exists to catch. If
one is ever added it comes from the writer factory like every other writer.

**The snapshot rule is a property, not a statement list.** `_release_snapshot`
asks the handle whether a transaction is open and rolls it back before the
prompt returns. `BEGIN` opens one, so does `SAVEPOINT`, and the next thing that
does would not have been on a list.

**Nothing here imports a DBAPI module.** The shell needs to know when a
statement is complete and how to catch a failed one; both come from
`store.connection` (`statement_is_complete`, `DriverError`). A second driver
import anywhere outside `store/` would turn "every handle is constructed in one
place" back into a convention, and
`tests/preferences/test_connection_is_the_sole_constructor.py` fails on one.

### Configuration Interface

**Producer:** `src/bankmachine/config.py` — the resolved `Config`, and the only
place a filesystem path is decided (AC-ARCH.4).

**Consumers:** every entry point. Values arrive by precedence: explicit argument,
`BANKMACHINE_*` environment variable, config file, documented default.

**Contract:** every path is absolute by the time it leaves `load_config`, and
sandbox and production resolve to different datastore files and different
keychain accounts, so cross-contamination requires an explicit override (AC-10.6).

`Config` also carries **who chose the environment** (`environment_source`), and
`require_chosen_environment` refuses when nobody did. The predicate is *opening
the per-environment datastore under the writer lock, or mutating a
per-environment keychain entry* — enforced at `store.connection._writer` and the
`secrets` mutators, not at a list of commands. So a consumer that writes needs
the environment supplied by an argument, a `BANKMACHINE_ENVIRONMENT` export or a
config-file entry; the fallback to `sandbox` now serves reads only. **A consumer
with no login shell (cron, launchd, a spawned test child) must therefore carry
the config file or the variable explicitly.**

### Credential Seam

**Producer:** `src/bankmachine/secrets.py` — the only module that imports
`keyring`.

**Consumers:** `store/connection.py` today; the aggregator client from step 2.

**Contract:** secrets are fetched by name and never returned into a log line, an
exception message or a `repr` (AC-10.1, AC-10.3). The datastore holds
`connections.credential_ref` — the *name* of a keychain entry, never a token,
because a datastore backup travels and a credential inside it travels with it.

🔴 **The archive exemption is a property of the endpoint, not a list kept beside
the archive.** `Endpoint.issues_credential` marks the endpoints whose *response
body* carries a credential — today `POST /link/token/create`,
`POST /link/token/get` and `POST /item/public_token/exchange`. The last is
verified live as returning `access_token`, `item_id`, `request_id`; the middle
one was added with hosted enrollment, because a *finished* Link session's body
carries the `public_token` — the same single-use value the exchange spends, and
therefore the same reason its body may never reach an append-only store. `FetchedResponse.__post_init__` refuses
to exist for such an endpoint, and `store.raw` derives everything it persists
from one of those — so a credential-bearing body cannot be archived by any
caller, including one written years from now by someone who never read
`store/raw.py`'s docstring.

The alternative was a list of exempt paths consulted at the archive site. That is
an enumeration standing in for a property, and this project has already been
burned once by a rule that matched on a name where it meant a relationship. The
cost of getting it wrong is not recoverable: `raw_responses` is append-only, so a
token written there is written permanently and travels with every backup.

**The credential-issuing calls therefore return their own types** — `LinkToken`
and `AccessGrant` — rather than a `FetchedResponse`. There is no path from either
into the archive, which is what keeps this structural rather than remembered.

### MCP Tool Surface — *first slice built 2026-09-08; build step 7*

**Producer:** `src/bankmachine/mcp.py` — stdio JSON-RPC, no SDK dependency
(`api-notes-plaid.md` §18), reading through `src/bankmachine/query.py`. Not every
specified tool ships; which do, and the descope, are recorded in `api-contract.md`.
**Consumer:** an MCP client, and through it an analyst agent.
**Contract:** `system-requirements.md` §5 — the tools, aggregate-first, with
a freshness stamp on every response (AC-9.2), explicit warnings over gapped or
degraded data (AC-9.3), and stated units, sign conventions and applied rules
(AC-9.4). This *is* the product's public API contract, and `api-contract.md` now
holds it — the two are held to the same built set by
`tests/preferences/test_the_documented_tool_surface_is_the_built_one.py`.

🔴 **No count is spelled here, on purpose.** §5's table is the authority and the
guard reads it; a number repeated in this entry is one nobody updates when the
table moves, which is exactly how this entry came to claim "four of the ten"
long after both halves had stopped being true.

### Derivation Seam — the contract the aggregator's derivers are written against

**Producer:** `src/bankmachine/store/derivation.py` — the `Deriver` signature,
the endpoint registry, and `apply_response`, which persists a response and
commits it before anything derives from it.

**Consumers:** `src/bankmachine/store/rebuild.py` and the sync path. The
aggregator's derivers live in `connector/plaid/derivers.py` and are composed into
a registry by `bankmachine/derivers.py`, which is passed explicitly to whoever
runs a derivation.

🔴 **`store.derivation` holds no registry at all, and that is the design rather
than a gap.** One there would mean `store` importing `connector`, which pulls the
aggregator SDK into every process that opens the datastore — the read-only query
surface included, which must never load the network layer at all. An import graph
is a better guarantee of that than a rule about who calls what.

**`derivers` is a required argument** on `deriver_for`, `derive`, `apply_response`
and `rebuild`. It briefly had a default; once the composition moved up a layer
that default had exactly one reachable outcome, so a caller could omit it, pass
mypy strict and the whole suite, and fail on the first response of an unattended
nightly sync.

**Contract**, and every clause is load-bearing:

1. **One normalization, two callers.** The live sync path and `store rebuild`
   run the same derivers over the same responses. Rebuild is the first
   implementation replayed, not a second one kept in step with it — which is
   what makes AC-11.5 checkable rather than aspirational.
2. 🔴 **A deriver is a pure function of `(RawResponse, DerivationContext)`.**
   Same inputs, same rows, every time, on any machine. The rule with teeth is
   *never call the clock*: stamp rows from `response.received_at`, which is when
   this system actually learned the thing. `DerivationContext` carries no clock
   and no configuration so the pure route is also the convenient one, and
   `store rebuild` catches the impure one by comparing content before and after.
3. **Persist first, derive second, in two transactions.** A deriver that raises
   leaves the response kept and no half-derived rows. The asymmetry is
   deliberate: a response may be unfetchable afterwards, a derivation is always
   re-runnable.
4. **A response with no registered deriver is a refusal, not a skip.** A rebuild
   that stepped over an endpoint it could not interpret would report success over
   a dataset missing whatever that endpoint carried, and every number in it would
   still add up.
5. **Every derived row carries `derivation_version_id`**, and the version is
   recorded on first use rather than seeded. Bump `DERIVATION_VERSION` in the
   same commit as any deriver change that could produce different rows from the
   same response.
6. 🔴 **A derived table is either rebuildable or a dimension, and a deriver must
   know which before it writes a row.** Both answers come from one property in
   `store/rebuild.py`, never from a column name: a table is *derived* when it
   references `derivation_versions`, and of those, the ones referencing
   `raw_responses` are **rebuildable** — emptied before the replay — while the
   rest are **derived dimensions**. `securities` is the only dimension today. A
   dimension row is shared: one security is named by holdings and investment
   transactions across many responses, so it has no single response to point at
   and no id that could be reassigned without orphaning them. **A deriver writing
   a dimension row upserts on its natural key**, because the replay meets rows it
   did not delete — a plain insert raises on the second pass, and neither failure
   is the deriver-purity bug it will look like.
7. 🔴 **A credential-bearing response is not persisted verbatim.** AC-5.1 keeps
   every response and AC-10.1 keeps every access token in the keychain; a token
   in `raw_responses.body_gzip` satisfies the first by breaking the second,
   permanently, because the table is append-only and a datastore backup travels.
   The archive is for responses carrying *data*. `request_context` records what
   was asked, never what it was asked with — no `Authorization` header, no token
   in a query string. This is a recorded decision rather than a mechanism: the
   endpoint vocabulary that could enforce it is the aggregator client's, so this
   is the clause step 2 meets. It is also what keeps `sync shell`'s AC-10.3
   redaction (Chunk 04) from needing to cover a table nobody planned to redact.

**Crossing it:** registering a deriver, or changing one, changes what a rebuild
of every existing datastore produces. `tests/store/test_rebuild.py` carries
stand-in derivers that exercise the three shapes the schema has — an identity
table a rebuild must not delete, an append-only series, and a fact table it
rebuilds outright.

### Aggregator Client — *built; build step 2*

**Producer:** `src/bankmachine/connector/` — `plaid/client.py` makes the calls;
`__init__.py` holds everything a caller is allowed to see.

**Consumer:** the sync path, and the raw-response layer that persists what it
returned before anything normalizes it. Today: `cli/connector.py`.

**Contract:** the connector returns `FetchedResponse` — an `Endpoint`, undecoded
`body` bytes, and when they arrived. Archiving is the caller's act, through
`store.raw.record_response`.

🔴 **The property is that nothing under `connector/` can *obtain* a datastore
handle** — not that nothing under it writes. The distinction became real when the
derivers landed: a deriver writes rows, through a connection the caller already
opened and owns. It cannot open one, cannot decide when the transaction commits,
and cannot reach the archive except through the response it was handed. That is
what makes AC-5.1's "archive before normalize" structural, and it is narrower and
truer than "the connector persists nothing".

`system-requirements.md` §9.2 is answered (2026-09-06): one aggregator in v1,
contained so a second is a new module rather than a rewrite. The boundary is
therefore a package rather than an interface — a client `Protocol` with one
implementation would encode that implementation and call it a contract.
`tests/preferences/test_connector_is_contained.py` holds the two properties:
nothing outside `connector/plaid/` imports the SDK, and nothing in `connector/`
imports a module that hands out a datastore handle.

🔴 **The second property is what makes AC-5.1 structural.** "Archive before
normalize" is not a rule anyone has to follow here — the connector has no way to
write at all, so there is no path through it that could normalize first.

🔴 **The body is taken undecoded.** Every call passes `_preload_content=False`,
because the SDK's default path deserializes into generated models that silently
drop fields they do not know about — and those are exactly the fields a later
rebuild would need. Verified against the SDK's source, recorded in
`api-notes-plaid.md`, and held red by `verify_norms_go_red.py`.

**`Endpoint` is a vocabulary, not a label.** The derivation registry is keyed
by it, `store/raw.py` defers its credential-archive rule to it, and AC-ARCH.4's
guard needs it to tell `/institutions/get` from a filesystem path.

**Failure is part of the contract, and it is typed.** A caller catches a local
exception from `connector/__init__.py`, never `plaid.ApiException` — the
taxonomy is defined outside the `plaid` subpackage precisely so that catching an
aggregator failure does not require importing the aggregator. The types are
organized by *what the caller must do next* rather than by what the aggregator
called it, because the aggregator's own `ITEM_ERROR` spans three different
remedies and a consumer switching on it would send the operator somewhere that
cannot help them.

Every failure carries `connection_id`, `error_code`, `request_id` and
`failed_at`. That set is not decoration: AC-4.1's "one broken connection never
aborts another" is a property of the *caller's* loop, which can only honour it
if the error says which connection it was; AC-4.2 wants the code recorded; and
AC-4.5 refuses a degraded record whose data hole cannot be computed, so the
connector owes the far end of that subtraction even though `last_success_at`
lives in the datastore.

🔴 **Whether a failure is worth retrying is a property of its type, not a list
kept in the retry loop.** A list in the loop is an enumeration that goes stale
the moment an error type is added by someone who does not think to visit that
file — and it goes stale silently, in whichever direction the omission falls: an
un-retried transient stops the nightly sync, a retried permanent one hammers the
aggregator with a call that cannot work. `retryable` has no default on the base
class, and `ConnectorError.__init_subclass__` refuses **at class creation** any
subclass that did not declare one — so a second aggregator's module cannot define
an error type without deciding. A test walking `__subclasses__()` was the first
attempt and is not enough: that walk sees only subclasses whose module has been
imported, so it guarantees something about the types one test file happens to
import rather than about every error type.

The grouping classes — `ConnectorError` and `AggregatorError` — exist to be
caught and are declared `grouping=True`, which exempts them from that rule and
makes them **non-instantiable**. Raising one would otherwise fail a frame away
inside the retry loop, reading a class attribute nobody set, on the error path of
the error path.

**The retry channel wraps the HTTP call and nothing else.** `architecture.md`
permits backoff on exactly this boundary because it is the only one that can
fail transiently. Nothing inside the retried boundary writes, so there is no
partial effect for a second attempt to interleave against — which is what makes
retrying safe here and would not make it safe anywhere downstream.

🔴 **That argument is about the local side only.** An endpoint the *far* end
cannot absorb twice — an exchange spends a single-use token and mints a durable
Item — is excluded by `Endpoint.retry_safe`, not by this reasoning.

**Built as of build step 2:** the endpoints `/institutions/get`,
`/link/token/create`, `/item/public_token/exchange`, `/item/get` and
`/accounts/get`; the error taxonomy and its retry channel; the
credential-archive mechanism; and the institutions and accounts derivers, so
`store rebuild` now runs end-to-end over a real archive.

**Built 2026-09-07 (build step 3):** the enrollment flow — the CLI, idempotency,
the connection cap and retirement. **Built 2026-09-08 (build step 4):** the
transactions deriver and its cursor loop, `bankmachine sync run`.

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
connection layer, the schema or the raw/rebuild layer changes: a contract test that has never been red
is a claim, not a check.
