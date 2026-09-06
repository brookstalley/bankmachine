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
- 83 tests, mypy strict clean, ruff clean.

**The norms are now mechanisms, and issue #1 is closed by this work.** Each of the four has a test,
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
promising, and closes issue #1.

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

