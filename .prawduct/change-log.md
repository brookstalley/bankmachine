# Change Log — bankmachine

<!-- Append new entries at the top. Each entry is a ## section.
     This file is separate from project-state.yaml to reduce merge conflicts
     when multiple branches add entries simultaneously.

     # Tagged entries

     This file is PROSE. Its body is what a reader — and a release note —
     actually gets. Two machine-read keys ride in a tag-line directly under
     the ## header, and `check-releasability` is the only thing that reads
     them:

         ## YYYY-MM-DD: title (vN.M.P)

         <!-- prawduct: scope=v1.4 | release=v1.3.18 -->

         **Why:** ...

     Recognized keys:
       scope    - rollup identifier (e.g., v1.4), matching the `scope:`
                  frontmatter of the build plan that governs the work.
       release  - the version that carried this entry. Its ABSENCE is what
                  marks the entry release-pending, so write NO release= on
                  the feature branch and add it at release. Any value at all
                  — including a placeholder naming the absence, e.g.
                  `release=unreleased` — drops the whole scope out of the
                  release-pending set and silently unships the work.

     Nothing else is read. `chunks=` and `status=` were retired along with the
     derived views they fed; entries in older logs still carry them and are
     parsed as inert — leave them. Which chunks an entry shipped belongs in
     the entry BODY, where release notes and readers actually find it: a
     deliverable omitted from the body ships invisibly, and no tag ever
     caught that either. -->

## 2026-09-06: the connector's walking skeleton — the product reaches the outside world

<!-- prawduct: scope=connector-v1 -->

**Why:** build step 2 is the aggregator client, and every later step reads through it.
Chunk 01 proves the whole path before widening it: configuration resolves, the keychain
yields a secret, the aggregator answers, and the answer lands in the archive verbatim.
It asks for the smallest thing the aggregator will tell anyone — one page of the
supported-institution list — because that needs client credentials and nothing else, so
the path is provable before enrollment exists.

**What shipped:** `bankmachine connector check`, which reports what it fetched and logs the
archived response id so the unattended job later has a record; `connector set-secret`, which
prompts without echoing at a terminal and reads a pipe when given one, so a secret reaches
neither the shell history nor a process listing; the `connector/` package with the aggregator
SDK confined to `connector/plaid/`; aggregator credentials added to the existing Credential
Seam rather than a second one; and the `Endpoint` vocabulary that `DERIVERS`, the
credential-archive rule and AC-ARCH.4's guard all turn out to need.

**The boundary decision, and why it is a package rather than an interface.**
`system-requirements.md` §9.2 — is a second aggregator ever expected — is answered: one in
v1, contained so a second is a new module rather than a rewrite. A client `Protocol` with a
single implementation would encode that implementation and call it a contract; the honest
version cannot be written until a second aggregator exists to disagree with the first. The
mechanism is `tests/preferences/test_connector_is_contained.py`, holding two properties:
nothing outside `connector/plaid/` imports the SDK, and nothing in `connector/` imports a
module that hands out a datastore handle. The second is the load-bearing one — AC-5.1's
"archive before normalize" is not a rule anyone follows here, because the connector has no
way to write at all.

🔴 **The response is taken undecoded, and this was the finding that shaped the client.**
The SDK deserializes into generated models by default, and those models silently drop
fields they do not know about — which are exactly the fields a later `store rebuild` would
need to reproduce rows the aggregator has since started sending. Handing the archive a
model round-trip would have satisfied AC-5.1's letter and destroyed its point. Every call
passes `_preload_content=False`, verified against the SDK's own source rather than its
documentation, and held red by `verify_norms_go_red.py`.

**Two things reading the code first caught that drafting from documentation would not.**
The credential-archive exemption was already decided in build step 1 — `store/raw.py` says
so, and names build step 2 as where it stops being a decision and becomes a mechanism — so
it was withdrawn from this plan's open assumptions as an inherited obligation rather than a
departure to be argued for. And `plaid-python` ships no `py.typed`, so everything it
returns is `Any`; strictness was not relaxed, the override is scoped to the SDK alone, and
the untyped surface stops at the module that converts to local types.

**A norm's detector was corrected, not weakened.** AC-ARCH.4's guard reads any string
opening with a separator as an absolute filesystem path, and `/institutions/get` is not
one. The fix is the relationship rather than an exemption: a literal declared as an
`Endpoint` is the aggregator's vocabulary, anything else is still a path. A per-file
allowlist was rejected — it would decay on the first module someone forgot to add — and the
new test asserts both directions, including that a `Path("/Users/...")` in an
endpoint-declaring module is still caught.

🔴 **A rejected call names its cause, which took the Critic to notice.**
`ApiException.reason` is the HTTP reason phrase, so wrong credentials, a malformed field and
an unsupported country all read `400: Bad Request` — leaving the operator no way to tell a
rotated secret from a bug in this code, and the wrong guess costs a credential rotation that
was never the problem. The cause is in the response body. Verified by probing the real
sandbox host with deliberately invalid credentials, which needs no valid ones:
`error_code=INVALID_FIELD`, `error_message='client_id must be a properly formatted,
non-empty string'`, plus the `request_id` that makes a failure traceable in the aggregator's
dashboard. Also mapped: an unreachable host, which the SDK wraps only for SSL errors and
otherwise lets escape as a raw `urllib3.MaxRetryError` — a traceback from a library the
operator never chose.

**Not done, and the chunk is not ticked because of it.** The *success* path has never been
probed: no sandbox credentials exist on this machine, so `tests/connector/fixtures/` is
empty and the two `sandbox`-marked tests skip. The offline suite proves the bytes pass
through a fake unaltered; only a live call proves the SDK hands them over undecoded against
a real server, and `project-state.yaml` is explicit that the aggregator is verified against
rather than mocked at the layer under test. The build plan's acceptance criteria were split
to say so rather than leaving a done-when nobody could meet. Queued as VRF-002.

## 2026-09-06: `sync shell` — the operator gets to look inside their own datastore

<!-- prawduct: scope=datastore-v1 -->

**Why:** page encryption breaks every ad-hoc SQL tool — stock `sqlite3` reads this file as corrupt,
because the pages are ciphertext. Until this command existed there was no way for the operator to
look at their own data at all, which is why AC-ARCH.6 puts it in build step 1 rather than step 9: it
is the debugging affordance every later step is built over. It is also the product's only surface
that runs operator-supplied SQL, so it is where both read-role norms stop being theoretical.

**What landed:**

- **`bankmachine sync shell`** — an authenticated SQL prompt over a read-role handle. Statements may
  span lines, `.tables` / `.schema` / `.help` / `.quit` are there, a failed statement costs the
  statement and never the session, and results render as aligned columns. A blob is summarised
  (`<blob, 402 bytes>`) rather than dumped: `raw_responses.body_gzip` is the one that comes up, and a
  terminal full of gzip is not a debugging affordance.
- **The refusal to write stays in the file handle.** The shell adds nothing of its own — it asks
  `store/connection.py` for a read-role handle, which is `mode=ro`. An operator can type `PRAGMA
  query_only = OFF`, watch the flag flip to `0`, and still be refused. The test asserts both halves,
  because asserting only the refusal would pass just as well against a shell where the PRAGMA
  silently did nothing.
- **The prompt holds no snapshot between statements.** A shell left open overnight is open during
  the nightly sync, and a read-role handle takes no writer lock, so nothing else serialises the two;
  a pinned snapshot starves the checkpointer for hours. The release is a *property* of the handle —
  it asks whether a transaction is open and rolls it back — rather than a list of statements to watch
  for, because `BEGIN` opens one, so does `SAVEPOINT`, and the next thing that does would not have
  been on the list. Three tests hold it, including a negative control that disables the release and
  confirms the checkpoint genuinely starves; a probe that only ever confirms what was expected is
  the one to distrust.
- **AC-10.3 has one rule, not one per surface.** Everything the shell writes goes through
  `logging_setup.redact`, the same function the log formatter uses — including the statement echoed
  back in a piped session, because a transcript is the likeliest thing here to be committed or
  pasted into a bug report. Redaction runs over text and not over numbers: money here is an INTEGER
  of minor units and an account number is TEXT, so redacting integers would blank a six-figure
  balance — the number the operator opened the shell to read — while protecting nothing.
- **No writer shell.** The plan left one optional and it is declined: the product is read-only, a
  writer shell would hold the exclusive `flock` for its whole session so the overnight prompt above
  would block the nightly sync outright rather than merely starve it, and hand-typed rows have no
  raw response behind them, which is what `store rebuild`'s content digest exists to catch.

**The carried edge was staged, and staging it found a real misdiagnosis.** Since Chunk 01 the
no-fallback clause has had one case with no staged test: a hot WAL from a killed writer, no `-shm`,
in a directory the reader cannot write to. Staged here, it turned out the guard could never have
fired — SQLite opens lazily, so `connect()` succeeds and the failure lands on the *first read*,
where `_key_and_prepare` reported `SQLITE_CANTOPEN` as a rejected key. That told the operator to
restore a keychain entry that was never the problem, for a datastore that only needed its WAL
checkpointed — the exact wrong-recovery failure `DatastoreKeyRejectedError` was introduced to
prevent. `_diagnose_first_read` now separates the two on `SQLITE_NOTADB`, and the case has a real
test instead of a stand-in for one.

**The store layer grew two exports rather than the CLI growing a driver import.** The shell needs to
know when a statement is complete and how to catch a failed one; both now come from
`store.connection` (`statement_is_complete`, `DriverError`). The structural test caught the import
on the first full run — worth recording, because the norm it protects is exactly the kind that
degrades into a convention the moment a second module imports a DBAPI.

**The cumulative review returned 0 blocking, and six of its findings were worth fixing anyway.**
Two were real defects rather than polish. The **datastore key validator** tested hex with
`int(key, 16)`, which is a parser and not a predicate: it accepts an `0x` prefix, `_` separators, a
sign and surrounding whitespace, so `"0x" + "a" * 62` is 64 characters and passed both checks —
and SQLCipher treats anything that is not exact hex as a *passphrase*, runs its KDF over it, and
gives a store that works until those defaults change. That is the precise silent substitution the
validator exists to prevent. It is now a full match on the hex alphabet, with the four accepted-by-
`int` shapes as cases. And **filesystem `OSError` had no mapping into `StoreError`**, so a lock file
the process cannot open escaped as a traceback — including out of `inspect()`, whose entire contract
is to report a state rather than raise on one.

The third was in this chunk's own output: **the log-tuned redaction rule was applied to schema
text**, where it is wrong. `_OPAQUE` blanks any 32-plus character run, and
`source_investment_transaction_id` is exactly 32, so `.schema` printed `[REDACTED]` where column and
index names belong — eight unreadable lines of the real schema. Schema text is now not a redaction
surface at all, and that is a property rather than an exemption: everything in `sqlite_master` here
is authored by this repo's migrations, and AC-6.6 with
`tests/preferences/test_no_provider_identity.py` is what makes it carry no operator data. Row values
keep the full rule, bare-length matching included, and the cost is recorded — a 64-hex digest is
blanked, and the join back to the archive is the integer `raw_response_id`, which is not.
`.schema` also matches `tbl_name` now, so a table's indexes come with it.

The rest: `architecture.md`'s canonical command table still advertised the writer shell this chunk
declined, which would have had a step-7 builder implement the refused flag; the README's status
stopped at Chunk 02; and `reader()`'s connect-time branch still stated the hot-WAL cause that Chunk
04 measured false, so two operator-facing texts described one failure and the less-reached one made
the disproved claim. Log rotation, unlogged run failures and archive retention are filed as #3, #4
and #5 rather than fixed here.

Suite green, mypy strict and ruff clean. The norm-break harness runs 29 cases, five of them new and
all verified red. The by-hand check AC-ARCH.6 asks for is recorded as VRF-001 in
`.prawduct/operator-verification.md` with its session transcript, and is the one item still awaiting
the owner's own eyes.

## 2026-09-06: Raw preservation and rebuild — a bronze layer that checks its own work

<!-- prawduct: scope=datastore-v1 -->

**Why:** FR-5's bronze/silver split turns a categorization bug into a re-run instead of a re-fetch,
and a re-fetch is often impossible — an aggregator's history window does not come back. It is built
now, with no aggregator to feed it, because a sync path written first would normalize straight into
the tables and retro-fitting raw preservation around it afterwards means rewriting the part that
already worked.

**What landed:**

- **`store/raw.py`** — every response persisted verbatim, compressed and hashed, before anything
  reads it (AC-5.1). The digest is over the *plaintext*, so it identifies the response independently
  of how it was compressed, and `load_response` recomputes it: a body that no longer matches what
  was recorded is refused rather than derived from. The archive is append-only — two identical
  responses at two times are two facts, and collapsing them would destroy the evidence that the
  source repeated itself.
- **`store/derivation.py`** — the seam build step 2 plugs into, shipped empty. One normalization,
  two callers: the sync path and `store rebuild` run the same derivers over the same responses, so
  rebuild is not a second implementation that has to be kept in step with the first. A deriver is a
  pure function of its response — `DerivationContext` carries no clock, because a `first_seen_at`
  stamped `now()` is the one mistake that makes a rebuild unreproducible.
- **Persist first, then derive, in two transactions.** A deriver that raises must not take the
  archive down with it: the response may be unfetchable afterwards, while the derivation can be
  re-run at any time. A crash mid-derive leaves the response kept and no half-derived rows.
- **`bankmachine store rebuild`** (AC-5.2) — one transaction under the exclusive writer lock:
  delete every row the archive can recreate, replay the whole archive in received order, and then
  **check its own work**. It hashes the datastore's content before and after and refuses to commit a
  rebuild that did not reproduce what it replaced, unless the derivation version changed (AC-5.3,
  AC-11.5). Without that refusal a rebuild is an irreversible bulk operation whose only failure
  signal is analysis quietly turning wrong weeks later.
- **What it deletes is derived, not listed.** A table is rebuildable when it holds a foreign key
  *pointing at* a raw response. The first draft matched on the column name, which put `raw_responses`
  itself — whose primary key is `raw_response_id` — first in the list of tables to empty before
  replaying them. A test caught it; the fix was a better predicate, not a longer exception list.
- **Rows nothing can recreate are never deleted.** Imported rows name a file rather than a response,
  and accounts carry the local ids every row of history points at (AC-6.3). Both survive a rebuild
  untouched, and the content digest covers them, so a rebuild that orphaned or renumbered anything
  fails its own check.
- **"Byte-identically" (AC-11.5), read deliberately:** the digest covers every column of every table
  except a table's own single-column integer primary key where nothing references it. Those are
  rowid allocations, not facts about the world — requiring `transaction_id` to come back identical
  would make the criterion a statement about SQLite's allocator. Every id that *is* a fact is
  covered.
- **The sole-constructor norm got sharper, not looser.** `engine.connect()` is a pool checkout over
  a handle `store/connection.py` already keyed and locked, but the AST scan matched any call named
  `connect`. Rather than exempt a file, the rule now says what it always meant: a role is decided by
  the parameters a handle is opened with, and a checkout carries none. `engine.py` gained
  `writer_connection` / `reader_connection` so nothing outside the store layer checks one out, and a
  positive control fails if that carve-out ever stops exempting anything real.
- **Norm 4 now covers writers too.** Only the reader refused a schema version this build does not
  recognize; `store rebuild` is the first writer that is not the migration runner, and a writer that
  misunderstands a schema writes wrong answers down rather than merely returning them. The check
  moved into one helper both roles call, and `initializing_writer` still skips it — bringing an old
  datastore forward is the one job that has to open a version this build does not serve.
- **The Chunk 02 ride-along is discharged: the index drift guard now compares what a partial
  predicate *says*, not whether one exists.** It read `sqlite_where is not None` against
  `PRAGMA index_list.partial`, so a condition inverted to `retired_at IS NOT NULL` kept every other
  property of the index intact while making it enforce the opposite rule. Both sides' text is
  normalized only for the qualifier, whitespace and case — never for meaning — and a positive
  control fails if the normalizer ever starts returning nothing.
- **Six more cases in `verify_norms_go_red.py`**, covering the body-integrity refusal, the
  table-classification rule, the reproducibility refusal, the checkout carve-out, the writer's
  schema refusal and an inverted index predicate. All 24 breaks go red.

**The Critic round returned no blocking findings and tightened two seam decisions**, both of which
would have landed on build step 2 rather than here:

- **A derived table is now either rebuildable or a dimension, from one property.** `securities`
  carries a `derivation_version_id` but no raw provenance, so "derived" was being reconstructed from
  two signals that disagreed on exactly one table. The first deriver to write a security would have
  hit the `source_security_id` unique index on replay, or upserted and left stale rows that the
  content digest then reports as an unreproducible rebuild — sending the next reader hunting a
  purity bug that is really a classification gap. A table is *derived* when it references
  `derivation_versions`; of those, the ones referencing `raw_responses` are rebuilt and the rest are
  dimensions a deriver must upsert. Fixing it turned up the same trap a second time:
  `derivation_versions` names its own primary key `derivation_version_id`, exactly as
  `raw_responses` names `raw_response_id`, so both classifications now go through one
  reference test.
- **The archive does not hold credentials.** AC-5.1 keeps every response verbatim and AC-10.1 keeps
  every access token in the keychain; a token in `body_gzip` satisfies the first by breaking the
  second, permanently, because the table is append-only and a datastore backup travels. Recorded as
  clause 7 of the Derivation Seam, where step 2 meets it, and it is what keeps Chunk 04's AC-10.3
  redaction from needing to cover a table nobody planned to redact. Each of these guarantees is a refusal, so each fails silently and in the direction of
  looking finished.

## 2026-09-06: The core schema — thirteen tables, with the requirements built into them

<!-- prawduct: scope=datastore-v1 -->

**Why:** the schema is the format every later consumer depends on, and it was being designed before
any of those consumers exist. Chunk 02 is the plan's lock-in chunk: the last point at which changing
it is free. The columns were not drawn from taste — the enumerated queries of the ten MCP tools in
`system-requirements.md` §5 were written down first, and the tables answer them.

**What landed:**

- **`store/types.py`** — the typed vocabulary the schema is written in. `MinorUnits`, `CalendarDate`
  and `UtcInstant` are distinct to mypy, with validating constructors, SQLAlchemy column types, and
  a `from_decimal_string` that scales the digit tuple so no amount is too large to convert exactly
  and no fraction is ever silently rounded away.
- **Migration 002** — the thirteen tables of FR-6 as frozen DDL, applied inside the one transaction
  the runner owns. Three requirement classes are enforced *by the database* rather than by the code
  that writes to it: `typeof(x) = 'integer'` on every monetary column (AC-6.2 — SQLite stores a
  float in an INTEGER column without complaint), format constraints separating calendar dates from
  UTC instants (AC-6.4), and a provenance CHECK so no row can claim an origin it has no link to
  (AC-7.4). Identity is partial unique indexes, so idempotency (AC-1.4, AC-7.5) and the
  append-only balance series (AC-3.1) are properties of the store rather than disciplines of its
  callers.
- **`store/schema.py`** — SQLAlchemy Core metadata for the same tables, written independently of the
  DDL and compared to it column by column on every run. Generating one from the other would have
  been fewer lines and would have made drift undetectable.
- **`boundary-patterns.md` populated** — the datastore schema as its first contract surface,
  carrying the five parts of the contract that no column name implies, and naming the MCP tool
  surface and aggregator client as boundaries that do not exist yet.
- **The sign convention, decided and written down once:** every stored amount is signed from the
  operator's point of view, liabilities included. Net worth is then a plain sum and AC-11.2's
  reconciliation needs no per-type special case. `accounts.balance_class` partitions a *report*,
  never an arithmetic sign.
- **`tests/preferences/verify_norms_go_red.py` extended to cover the schema** as well as the
  connection layer — every new structural guarantee was verified red with its mechanism broken. A
  constraint that has never refused anything is a claim, not a check.
- **`frozen` became a mechanism.** The DDL is rendered from two shared constraint idioms, so
  "this never changes" rested on nobody editing them — and migration 003 will want the same two.
  A recorded SHA-256 of the rendered statements, compared by a test, is what now stops a later
  migration from silently redefining what version 2 means for every datastore that already ran it.
- **An aggregator row must name the response it came from.** The provenance CHECK originally
  permitted a row with `source = 'aggregator'` and no `raw_response_id`, while the comment above it
  claimed exactly one link is always set. Tightened to match the claim, on all four normalized
  tables: a row whose answer to *where did this come from* is silence looks identical to one that
  can be traced, and Chunk 03's rebuild is written against this constraint.
- **Provenance made symmetric across the normalized tables.** `holdings.source` accepted `'manual'`
  while having no column to name the import it came from, and neither `holdings` nor `balances_daily`
  carried the CHECK that `transactions` had. Found by scrub, not by the requirement: AC-7.4 is a
  property of every normalized row, and it had been implemented on one table.

**What the lock-in checkpoint caught:** re-reading the enumerated consumer questions against the
delivered tables found one they could not answer. `net_worth` needs assets separated from
liabilities, and `account_type` is the source's vocabulary rather than a classification — different
between sources, and absent entirely for an import-only account. Added as `accounts.balance_class`
while it was still free. One limitation is recorded rather than fixed: there is no FX table, so a
multi-currency net worth is out of scope until it is asked for.

**Also in this session:** the owner settled the open interpreter question — the product moves to
**Python 3.14**. The full suite, mypy strict and ruff were re-run green on 3.14.6 before the pin
moved, and `requires-python` stays `>=3.11` because nothing in the code needs more.

**Verified:** suite green (`prawduct-hook test-status`), mypy strict and ruff clean, every
structural break caught by the harness — which now covers the schema's guarantees, the frozen-DDL
hash, and the index guard's partial predicates as well as the connection layer's four norms. `store init` and `store status` were driven against a real
encrypted datastore, reporting schema version 2, with a known plaintext written through the schema
unrecoverable from the file's raw bytes. **The lock-in check was executed, not read:** the plan's
seven enumerated consumer questions were run as real SQL against a seeded datastore — fourteen
queries, because several questions take more than one and the remaining §5 tools were covered too —
and every one returned, including the per-account gap walk, the plain-sum net worth, and the
freshness stamp for an import-only account with no connection.

## 2026-09-06: The walking skeleton — an encrypted WAL datastore with its four norms enforced

<!-- prawduct: scope=datastore-v1 -->

**Why:** the repository held zero lines of Python. The architecture's four norms were prose claims
with an open issue (#1) standing in for their mechanism, and an operator had no way to create or
look at a datastore. Chunk 01 of the datastore-v1 plan is deliberately the widest chunk in that
plan because it is the one that proves the topology: config → keychain key → encrypted WAL
datastore → migration → read back through the reader role → print from the CLI.

**What landed:**

- The `uv` package (`bankmachine`, Python 3.11+), three runtime dependencies and four dev. The
  smallness is deliberate: a public tool that pulls real bank data on a stranger's machine wants a
  runtime dependency surface small enough to read.
- `config.py` — every path is configuration with a documented default (AC-ARCH.4), resolved by
  precedence from argument, environment, config file, default. Sandbox and production default to
  **different datastore files and different keychain accounts**, so putting fixture data in the real
  store needs an explicit override rather than a forgotten flag (AC-10.6).
- `secrets.py` — the only module importing `keyring` (AC-10.1). The datastore key is a 256-bit raw
  key, so SQLCipher's KDF is skipped and the value in the keychain *is* the key: no derivation whose
  parameters could drift between the process that created the store and the one that opens it.
- `store/connection.py` — the one module that constructs a connection, with exactly two roles.
  Writers route through a single factory that takes an advisory `flock` before it returns; readers
  open `mode=ro` and hold no snapshot beyond the statement that needs it. The SQLite open modes are
  **named constants**, because they are the norms rather than an implementation detail.
- `store/migrations/` — a ~50-line forward-only runner that owns its own transaction boundary, so a
  migration's DDL and its version stamp commit together or not at all.
- `store/engine.py` — SQLAlchemy Core over `create_engine(..., creator=...)`, so SQLAlchemy never
  opens a connection and every SQLCipher-specific step stays in the module that owns the norms.
- `logging_setup.py` — redaction at the formatter (AC-10.3) and a loud environment banner at every
  startup (AC-10.6). Both environments are announced at WARNING: the accident runs in both
  directions, so neither state is the quiet one.
- `cli/` and `__main__.py` — `bankmachine store init` (the only creator) and `store status` (which
  reports a missing or unrecognized datastore rather than crashing or creating one, AC-ARCH.3).
- Suite green, mypy strict clean, ruff clean (`prawduct-hook test-status`).

**The norms are now mechanisms, and issue #1's ask is delivered by this work** — its close is
owed at merge, because on the Issues backend a status change is an immediate API call with no
branch to be abandoned alongside. Each of the four has a test,
and each test was verified to go **red** with its norm deliberately broken —
`tests/preferences/verify_norms_go_red.py` keeps that reproducible rather than a sentence in a
commit message. Two things that came out of running it are worth recording:

- **A norm with two layers needs a test per layer.** Breaking only the `mode=rw` open mode left the
  no-implicit-creation test green, because the existence check still refused. Behaviour alone could
  not tell the layers apart, so the modes became named constants with their own assertions; either
  layer regressing is now caught.
- **The verification harness lied once, and the reason generalizes.** `"ro"` → `"rw"` is a
  same-length edit, and CPython validates a `.pyc` on (mtime, size) — two same-size writes inside
  one mtime second leave stale bytecode valid, so the test imported the *unbroken* module and
  reported green. Any tooling that mutates source in a loop has this failure mode.

**Also in this bundle:**

- `check-no-personal-data.sh` and its 22-case self-test **moved from `scripts/` to
  `tests/preferences/`**, discharging an obligation recorded in three places. The pre-push wiring
  followed the script, so push-time enforcement was kept rather than traded for test-time
  enforcement. The move initially broke five self-test cases by silently skipping them; the sandbox
  and the hook path now resolve through `git rev-parse --show-toplevel` rather than counting `..`
  hops, so a future move fails loudly instead of quietly testing less.
- `test_no_provider_identity.py` and `test_requirement_ids_unique.py` — both named in the norm index
  and both marked aspirational until the scaffold existed — are now written.
- `test_command:` is declared in `project-state.yaml`, deliberately left unset until a runner
  existed that could emit `{junit_xml}`.

**What the Critic caught, and it was worth the round.** One blocking (the plan's Deliverables line
still named the guard's pre-move path) and three warnings, all fixed:

- **`store init` minted a key for a datastore it could not decrypt.** On the restored-from-backup
  path — datastore present, keychain entry gone — it generated *and stored* a fresh key, migration
  then failed, and every later `store status` reported an authentication failure instead of a
  missing key. That is a recoverable state being reported as a corrupt one, which routes the
  operator to the wrong recovery. `store init` now refuses to mint a key for a store that already
  exists, and says why.
- **SQLAlchemy's transaction control is inert over these handles**, inherited from the deliberate
  `isolation_level=None`. Nothing recorded it, and Chunks 02 and 03 are exactly the two that would
  have assumed otherwise. Now documented at the module, pinned by a test, and flagged in the plan
  where those chunks will meet it.
- **`load_config`'s injected `env` seam stopped one step short of `HOME`**, so five config tests
  read as isolated while resolving against the developer's real home — and that branch is the
  documented macOS default.

The second review round found one more, and it is the more interesting of the two: **the `HOME`
seam fix shipped without a test that would catch its own regression.** Every other config test
either sets the XDG variables or asserts only that a path is absolute, so the fallback branch — the
documented macOS default — could have reverted to `Path.home()` with the suite still green. The
test now exists and was verified red against that exact revert. A fix without the check that
protects it is a fix with a shelf life.

Two smaller things rode that same round. `get_datastore_key`'s "run `bankmachine store init`"
advice was wrong in **every** path that reaches it: `writer()` and `reader()` both check the
datastore exists before asking for a key, and `store init` now correctly refuses to mint one for an
existing store — so the advice sent the operator in a circle. It names the state and both real
remedies instead. And the read-only reframing had reached `pyproject.toml` and the package
docstring but not `argparse`'s `description`, which is the one summary an operator actually reads
(`bankmachine --help`).

**One decision deliberately not taken:** `uv init` pinned `.python-version` to 3.14, so the
first run of everything above happened on an interpreter no artifact records. The pin was reverted to
3.12 — the version `project-preferences.md` records as verified — and the whole suite re-run
there. Moving this product's tested interpreter is the owner's call, not a side effect of
scaffolding.

**Trade-off accepted:** the redaction patterns over-redact. A filesystem path holding a
32-character segment is blanked along with the tokens. The alternative — requiring high entropy
before redacting — trades a little log legibility back for the chance of a real token slipping
through, and under the documented default paths no ordinary path is long enough in one segment to
trip it.

## 2026-09-05: AC-ARCH.7 resolved — the system architecture, measured rather than assumed

<!-- prawduct: scope=architecture -->

**Why:** `docs/system-requirements.md` AC-ARCH.7 deliberately deferred journal mode, locking
behaviour and reader isolation under encryption to "the system architecture" — a document that did
not exist. Build step 1 is the encrypted datastore, so step 1 would have been the component that
"encountered them first", which is precisely what the criterion forbids.

**What landed — two artifacts, not one.**

`.prawduct/artifacts/architecture.md`: topology, component responsibilities, the four channels (one
of which is the datastore file, and one of which is the import-file surface), data ownership,
failure modes, deployment and version skew, cross-cutting runtime concerns, and a decision log. It
is this product's first strategy-class artifact and its first `## Direction` section.

`.prawduct/artifacts/build-plan-datastore-v1.md`: build step 1 of system-requirements §8, in four
chunks — the walking skeleton (config, keyring, encrypted WAL datastore, the two connection roles),
the FR-6 core schema, the FR-5 raw-response layer and rebuild, and `sync shell`. Chunk 01 is
deliberately the widest because it proves the topology; Chunk 02 is the lock-in chunk, so the
questions its schema must answer are enumerated from the §5 tool table before any field is designed.
Chunk 01 also carries the `tests/preferences/` guard migration that three separate records have been
promising, and delivers what issue #1 asked for.

**A dependency decision rides with it.** The store layer uses **SQLAlchemy Core** — typed table
metadata and the query builder, no ORM, no session or identity map — decided by the owner over a
builder recommendation of hand-written SQL. It adds `sqlalchemy` as a runtime dependency at Chunk
01, taking the runtime surface to three packages. The objection behind the recommendation is
answered by construction rather than dropped: engines are built with `create_engine(..., creator=...)`
over our own keyed connection, so every SQLCipher-specific step — key first, WAL, `mode=ro`,
`query_only`, the writer lock — stays inside the module that owns the architecture norms, and
SQLAlchemy never opens a connection itself. Both that route and the built-in `sqlite+pysqlcipher`
dialect were verified against SQLCipher before the decision was taken. Risk surfaces were confirmed
in the same pass and are now recorded in `project-state.yaml`.

**The concurrency answer.** WAL journal mode, set after keying. Writer-role processes serialise on
a `flock` held for a whole run, above SQLite's own locking. The MCP reader opens `query_only` and
releases its snapshot at the end of every tool call. Nothing creates the datastore implicitly. A
process that does not recognise the schema version refuses to serve.

**Measured, not remembered.** Every concurrency claim was probed against `sqlcipher3-wheels` 0.5.7
(SQLCipher 4.12.0, SQLite 3.51.1) on this machine. The probes earned their keep three times: the
WAL and shm files are themselves encrypted, which had to be true or WAL would have traded AC-ARCH.5
away for AC-ARCH.7; a reader holding a snapshot starved a passive checkpoint at 0 of 93 frames and
93 of 93 the instant it released, which turned the reader's snapshot discipline from advice into a
norm; and a plain `connect()` to a missing path silently creates an empty encrypted store, which
under AC-ARCH.4's configurable path would answer every question confidently from nothing.

**One premise was falsified.** The design was going to route around a believed limitation — that a
`mode=ro` connection cannot read a WAL database without an existing `-shm`, and would fail against
a hot WAL from a crashed writer. It read correctly in every probed case, including after a
`SIGKILL` mid-write. `query_only` is still the choice, on its two surviving reasons; the reason
that did not survive is struck and recorded as struck, in the artifact's Decision Log.

**Norm bookkeeping.** Four norms born, all before any code exists, so no retroactivity decision
applies — there is nothing to migrate, contain or grandfather. Four pointer rows added to the
preferences norm index, and issue **#1** filed for the enforcement tests, because a mechanism named
and never built is the aspirational failure with extra steps.

**What the review changed, and it was two of the four norms.** The cumulative Critic returned 1
blocking, 12 warnings, 8 notes, and two findings were defects in the norms themselves rather than in
their presentation.

The writer norm **defined the writer role by enumerating commands**, and the list had already
omitted `store init` — which creates the file — and `store rebuild`, which rewrites every normalized
table. A list is a thing to forget. It is now defined by construction: every writable handle comes
from one writer factory, which takes the lock before it returns, so there is no way to be a writer
without passing through it.

The reader norm rested on `PRAGMA query_only`, which **is reversible** — re-probed on the finding,
`query_only=OFF` restores writes on a read-write handle, and `sync shell` ships the operator exactly
the SQL prompt that can type it. Read-role handles now open `mode=ro`, where the same sequence still
fails because the refusal lives in the file handle. Re-probing also scoped the earlier "falsified"
premise properly: `mode=ro` *does* fail against a hot WAL with no `-shm` under an unwritable
directory — the first probe had missed it because the crashed writer left its `-shm` behind. So the
norm carries a no-fallback clause: that state is a loud error, never a quiet downgrade to a writable
handle.

Also from the review: import files named as the foreign inbound surface they are (the artifact had
claimed none existed); one canonical CLI command table instead of four disagreeing lists; a logging
and AC-10.3 redaction rule placed in step 1 rather than step 8, because steps 1-7 all write log
lines; migration DDL and its version stamp required to commit in one transaction, since the version
is the *sole* signal a store is safe to serve; `source_root` and `risk_surfaces` set; and the
enforcement item re-filed from frozen markdown into the live Issues backend.


## 2026-09-05: Named — the product is `bankmachine`

<!-- prawduct: scope=rename -->

**Why:** The working name embedded a third-party trademark and locked the product to one
aggregator, and build step 1 is what fixes the Python package name, the keychain service name, the
scheduler label and the MCP server name. The keychain service name is the expensive one — changing
it after enrollment orphans stored access tokens. Settling the name while the repository still held
**zero lines of code** made this a documentation sweep rather than a migration; that timing was the
whole point of deciding the rename before step 1 rather than at publish.

Verified rather than assumed: PyPI returned 404 for `bankmachine`, so the package name was free at
the time of choosing.

**What changed:** titles and labels across the requirements doc, README, change-log, backlog,
boundary patterns and project-state; the name open questions in `system-requirements.md` §9 and
`project-state.yaml` closed; the aggregator-pluggability question's stale clause corrected, since
the product name no longer embeds the aggregator's name — one fewer reason that question is forced.

Six occurrences of the old name were deliberately **left in place**: two historical change-log and
archived-plan entries that record what was said on the day, the decision entry that names what was
renamed away from, and the GitHub repository slug, which is still accurate because the remote has
not been renamed.

**Carried through to the remote and the checkout.** The GitHub repository was renamed
`brookstalley/MCPlaid` → `brookstalley/bankmachine` (verified still private), `backlog_service_repo`
repointed at it rather than left to lean on GitHub's redirect, and the local checkout moved to
`~/source/bankmachine`. Verified after the move that `core.hooksPath` survived, the guard is clean,
the self-test still passes 22/22, and both branches are in sync with the renamed remote.

**Recorded caveat, raised once and accepted.** "Bank machine" is the ordinary term for an ATM in
Canada and parts of the UK — a name suggesting a device that *dispenses money*, for a product whose
§2 non-goals make "read-only, permanently" a headline commitment. This is a connotation risk, not a
technical one, and the mitigation is placement rather than a different name: the README now leads
with **"Read-only. It never moves money"** above the description, where a reader arriving from the
name meets the correction first.

## 2026-09-05: Repository made publishable — roster out of git, history purged, boundary guarded

<!-- prawduct: scope=repo-sanitization -->

**Why:** The operator restated the product. MCPlaid is a **general-purpose tool, not linked to
their personal finances** — consumed by Claude Cowork, and possibly released publicly, so nothing
specific to them may be in it. That resolved the open question the previous entry left standing,
and resolved it harder than either option on the table: the roster does not belong in this
repository at all.

The exposure was measured rather than estimated. `origin/develop` carried the operator's
institution names and balances in exactly three paths, and every other tracked file at that ref
was grepped clean.

**What changed:**

- The roster and its account-inventory evidence moved to the gitignored `deployment/` directory.
  `docs/deployment-requirements.template.md` keeps what was worth keeping — the zero-engine-change
  contract and the §7 traceability table — with no institution, balance or account count in it.
  That contract is what makes the engine spec trustworthy to a reader who is not this operator.
- `docs/build-vs-adopt-investigation.md` sanitized in place: operator name, machine name,
  institution names and account counts out; every technical finding, including the source-level
  vetting of the candidate MCP servers, kept intact.
- `scripts/check-no-personal-data.sh` added and wired into `pre-push` on **every** branch. It
  matches the roster's own explicit tokens (engine AC-0.3) plus operator identity, on word
  boundaries, over every tracked file. A checkout with no `deployment/` directory has no roster to
  leak and passes with a note — which is why the guard itself is safe to publish.
- `README.md` added, carrying the `git config core.hooksPath .githooks` step. A hooks directory is
  per-clone config, so a fresh clone pushes unguarded and nothing says so — and the only previous
  statement of the step lived inside the hook file the unset config prevents from running.
- **No attribution, anywhere** — stated absolutely in `CLAUDE.md` and homed as a norm row in
  `project-preferences.md`. This widens the already-ratified `Commit attribution: none` past
  commits to PRs, issues, comments, code, docstrings, documentation and release notes, and it
  overrides any harness default to the contrary. Recorded here because the widening previously
  existed only in `CLAUDE.md` while the preferences row still read narrower.
- **History purged.** `git filter-repo` removed five paths from every commit; `develop` and `main`
  were force-pushed. Verified by fresh clone: zero institution or operator tokens anywhere in the
  remote's history. Two paths were purged and re-added at their current content rather than
  scrubbed in place — the decision record and the guard itself, both of which carried in early
  revisions exactly what the purge exists to remove.
- **The fifth path was found by the guard, not by us.** The purge was planned as four paths. The
  finished guard, scanning all history, reported a fifth: Chunk 01's own first commit hardcoded
  identity tokens in the guard's source — the arrangement the Critic's R-9 had just made us
  remove. The check caught its author.
- Four decisions recorded with alternatives: repository scope; MCP transport is local stdio only
  and AC-10.5 holds; macOS for v1 with the credential store and scheduler behind seams; rename
  before build step 1.

**What the review changed, and it was the important half.** The first version of this guard
scanned the *working tree*. Critic pointed out that this passes the exact exposure the guard exists
to stop — a leak sitting in already-pushed history behind a sanitized tip — and that the operator
would read "clean" as "nothing I am pushing carries the roster", which was not what was checked. The
guard now takes the ref range the pre-push hook already receives and scans **every commit being
pushed**. Run against this repository's own history it correctly refuses: the roster is still back
there, which is what Chunk 02 is for.

The same review found the guard failed open at every error path, and that its hardcoded identity
tokens forced a carve-out where the one tracked file containing the operator's name was the one file
never scanned. Both are fixed by construction rather than by patching: **all** tokens now come from
gitignored `deployment/`, so the script carries none and needs no self-exclusion, and every error
condition aborts rather than reporting clean.

**The self-test earned itself immediately: 7 of its 15 cases failed on first run.** The cause was a
genuine defect — the positive control used system `grep` while the scan used `git grep`, which does
not honour `\b` in ERE. So the control passed while the scan matched nothing: precisely the
fail-open shape the guard was being rewritten to refuse, reproduced inside the fix. Word boundaries
are now spelled out explicitly, and the control runs through the same engine that scans, using a
real token rather than a synthetic sentinel.

**Note on the mechanism.** Guard and self-test are shell rather than `tests/preferences/` because no
Python scaffold exists yet and creating one would fix the package name ahead of the rename decision.
The migration obligation is recorded in `project-preferences.md` and in `system-requirements.md` §8
build step 1 — the step that lands the test runner, and therefore the moment it is triggered.

## 2026-09-05: Discovery captured; requirements split into engine and roster layers

**Why:** The repo held three substantial docs but a template-default `project-state.yaml`, so
governance could not calibrate rigor and the build gates could not engage. Discovery ran in
reconciliation mode — the material was read and backfilled rather than re-interviewed.

Mid-discovery the operator imposed a constraint that reshaped the frame: **no hardcoded account
providers; accounts are added and removed over the product's life.** The requirements doc was a
snapshot of one roster on one day, and that roster was already known wrong in detail. The operator's
own refinement settled where the line falls — the roster requirements are *genuine* requirements,
they simply belong to a different layer than the engine.

**What changed:**

- The v1 acceptance-criteria document split into `docs/system-requirements.md` (the
  provider-agnostic engine, no institution name in it) and a deployment-requirements document
  holding this operator's roster, as real acceptance criteria. All 47 v1 criteria land in one or
  the other, generalized or instantiated; one is explicitly superseded.
- A load-bearing rule connects them: every deployment requirement must be satisfiable by
  configuration plus an adapter with zero engine change. The traceability table is its checkable
  form — a deployment requirement that cannot be expressed that way is a gap in the engine spec.
- Two norms ratified: the provider-agnostic engine (with the aggregator expressly carved out), and
  uniqueness of requirement ids within a document.
- Decisions recorded with alternatives: SQLCipher over plain SQLite and over field-level AES;
  no hardcoded filesystem paths; lossless rebuild qualified by a recorded derivation version;
  account lifecycle so a retired account stops reading as a permanent coverage gap.
- Toolchain set: uv, pytest, ruff, mypy strict, hypothesis on the money and idempotency invariants.

**Open, and the operator's to decide:** where the roster lives. `origin/develop` already carries
institution names and balances, against the constraint this project records; the remote is private.
*(Closed 2026-09-05 by the entry above this one: the roster moved out of git and the history was
rewritten.)*

**Reviewed:** `rev-20260905T195208Z-c1f3c450` (2 blocking, 9 warning, 5 note — all resolved),
verified clean by `rev-20260905T200406Z-290b9dcb`.

