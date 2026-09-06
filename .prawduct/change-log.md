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

## 2026-09-05: AC-ARCH.7 resolved — the system architecture, measured rather than assumed

<!-- prawduct: scope=architecture -->

**Why:** `docs/system-requirements.md` AC-ARCH.7 deliberately deferred journal mode, locking
behaviour and reader isolation under encryption to "the system architecture" — a document that did
not exist. Build step 1 is the encrypted datastore, so step 1 would have been the component that
"encountered them first", which is precisely what the criterion forbids.

**What landed.** `.prawduct/artifacts/architecture.md`: topology, component responsibilities, the
three channels (one of which is the datastore file), data ownership, failure modes, deployment and
version skew, cross-cutting runtime concerns, and a decision log. It is this product's first
strategy-class artifact and its first `## Direction` section.

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

