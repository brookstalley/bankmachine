---
artifact: build-plan
version: 1
scope: backup-at-any-schema-version
branch: fix/backup-at-any-schema-version
critic_mode: cumulative-final
depends_on:
  - artifact: architecture
    file_path: .prawduct/artifacts/architecture.md
  - artifact: operational-spec
    file_path: .prawduct/artifacts/operational-spec.md
governed_by:
  - artifact: architecture
    dispositions:
      - "🔴 § Direction: *a process that does not recognize the datastore's schema version refuses to serve, loudly* → this is the governing norm and this plan RULES AT ITS EDGE rather than amending it. `store backup` does not serve: it runs `VACUUM INTO`, a page-level copy of opaque ciphertext, reads no table and answers no query. The norm's own why -- 'plausible, structurally valid, wrong answers' -- has no purchase on a copy that produces no answers. Reader, writer and rebuild keep the check unchanged"
      - "§ Direction: *every writable handle comes from the one writer factory, and that factory takes the exclusive writer lock* → conforms, and this is why the fix is a named role rather than a flag. `copying_writer` goes through `_writer(create=False)` exactly as `writer` and `initializing_writer` do, so it inherits the lock. No new construction path, no `dbapi2.connect` outside the factory"
      - "§ Direction: *every read-role handle is opened read-only at the file (`mode=ro`), holds no read snapshot beyond the statement, and never falls back to a writable handle* → conforms; `_verify` keeps using `connection.reader`, and gains only the `require_supported_schema=False` argument the reader already exposes for `inspect`"
      - "§ Direction: *no component creates the datastore implicitly* → conforms; `copying_writer` is `create=False`, so a typo'd source path raises `DatastoreMissingError` exactly as `writer` does"
  - artifact: operational-spec
    dispositions:
      - "§ Direction: *a backup destination is never created implicitly and never overwritten* → conforms; both refusals live in `back_up` above the handle and are untouched by this change"
      - "§ Direction: *no filesystem path is hardcoded* → conforms; the copy's source is `config.datastore_path` and its destination is the operator's argument. `_verify` reaches the copy by `dataclasses.replace(config, datastore_path=destination)` rather than by constructing a path, which is the same rule held one layer in"
---

# Build Plan — a datastore can be backed up at any schema version

## Problem

`back_up()` opens through `connection.writer()`, which enforces Norm 4's schema check
(`_require_supported_schema`). So a datastore at a version this build does not serve cannot be
copied at all:

```
bankmachine: ...store-sandbox.db is at schema version 4; this build serves version 9 only.
Refusing rather than writing into a schema it does not understand
```

**This is a known, documented limitation, not an unnoticed defect** (`operational-spec.md`,
§ "What backup looks like" → *Known limitation*), and the documented upgrade procedure works
around it correctly by ordering the backup **before** `git pull` -- taken with the old build,
which still serves the store.

🔴 **What is wrong is not the procedure but what happens when the order is not followed.** The
escape the limitation offers is *"copy the three files by hand"* -- and the same document, three
paragraphs earlier, says 🔴 **"Do not use `cp`"**, backed by a measured case: a source with a 2 MB
hot WAL holding 300 rows produced a `cp` copy short of every one of them. So the product's advice
for its riskiest moment is the thing it proves unsafe everywhere else.

The trap is reached by `git pull` before `store backup` -- ordinary muscle memory -- and by every
development checkout that moves ahead of its store. It was reached on this machine on 2026-09-10.

**The consequence is not theoretical.** `balances_daily` is the one series no aggregator backfills
(`backup.py` module docstring); a `cp` that silently drops a hot WAL takes the newest of it.

## What success looks like

`bankmachine store backup <dest>` produces a verified, WAL-folded, encrypted copy of a datastore at
**any** schema version, under the exclusive writer lock, and reports the version the copy carries.
The *Known limitation* paragraph and its `cp` instruction are deleted because they stop being true,
and 🔴 "Do not use `cp`" becomes unconditional.

## Out of scope

- Norm 4 for the reader, the writer and `store rebuild` -- unchanged, and tested to stay unchanged.
- Backup scheduling (**#10**, still at `stage: requirements`).
- Restoring *across* schema versions, or any migration rollback. A copy taken at version N restores
  to version N and is then migrated by the ordinary runner.
- The journal-mode side effect: `_writer` issues `PRAGMA journal_mode = WAL`, so backing up a
  `delete`-mode store flips it. That is true of every writer open today and is not introduced here.

## The decision this plan rests on

> **[DECISION: Rule at Norm 4's edge rather than amend it.** "Serve" means answering queries or
> writing derived data; a page-level copy that interprets nothing is outside the norm's reach.
> `store backup` gets a named, lock-taking, schema-agnostic handle. **| Why:** the norm exists to
> stop confidently-wrong answers, and a backup produces no answers -- while refusing it leaves the
> operator with `cp`, which this repo has measured to be lossy. Amending Norm 4 would weaken a norm
> that is correct for every other caller, and editing a norm to bless one's own code is the
> laundering `docs/norms.md` names as the aggravated form of unrecorded amendment.
> **| Owner may veto** -- the alternative is to amend Norm 4, or to accept the limitation and
> instead make the CLI refuse with a message naming the correct procedure. **]**

🔴 **A named role, not a flag.** `learnings.md` § *Guarantees by construction*: "a guarantee
defined by an enumeration decays, and one resting on a reversible flag was never a guarantee." A
`require_supported_schema=False` parameter on `writer()` would put the norm's exemption within reach
of every future caller. `copying_writer` is one name with one caller, exactly as
`initializing_writer` is the migration runner's.

## Chunk 01 — the copying handle, and the remedy asserted

**Delivers**

1. `connection.copying_writer(config)` -- `_writer(create=False)` with no schema check, documented as
   the second and last exemption from Norm 4, with the ruling's reasoning in the docstring
   (present-tense why, no history narration).
2. `back_up()` opens through it.
3. `_verify()` passes `require_supported_schema=False` to `connection.reader`, so a copy at an
   unservable version can be read back. Without this the copy is written and then rejected by its
   own verification.
4. `_print_backup` names a copy whose version this build does not serve, so a verified backup at
   schema 4 is not mistaken for one this build can open.

**Tests** (`tests/store/test_backup.py`, `tests/store/test_connection_norms.py`)

- A store stamped to an unservable version is backed up, and the report carries that version.
- 🔴 The hot-WAL guarantee holds at an unservable version -- the existing 300-row/2 MB case,
  re-run against a store stamped forward. This is the property the `cp` workaround loses and the
  whole reason the fix is worth making.
- 🔴 **The documented remedy, asserted end to end** (`learnings.md` § *A documented remedy is a
  claim and is asserted like one*): populate a store at the current version, stamp it to an older
  one, back it up, restore the copy to a fresh path, run the migration runner over it, and confirm
  it opens and still holds the rows. That is `operational-spec.md` § Rollback's
  "recovery is restore-from-backup" walked by a test.
- Norm 4 unchanged: the reader and the writer still refuse an unrecognized version (existing tests
  stay green, unmodified), and a new structural test asserts `copying_writer` and
  `initializing_writer` are the *only* handles that skip `_require_supported_schema`.
- `back_up` still refuses an existing destination and an absent parent directory at an unservable
  version -- the operational-spec norm holding where the new path could have bypassed it.
- The CLI note is covered both ways: it appears for a copy this build cannot serve and names
  `store init` as the way forward, and it is ABSENT for an ordinary backup -- an unconditional
  note trains the operator to skip the one that matters.

**Artifacts, updated as part of the chunk**

- `architecture.md` -- Norm 4 gains a **Rulings** entry; the norm's Statement and Why are untouched.
- `learnings.md` -- the ruling cross-linked (norms are statute, learnings are case law).
- `operational-spec.md` -- delete *Known limitation*; make 🔴 "Do not use `cp`" unconditional;
  rewrite upgrade step 2 so the ordering is a convenience rather than a requirement.
- `project-preferences.md` -- norm-registry row for the ruling's enforcement test.
- `.prawduct/change-log.md` -- entry.
- `docs/first-production-connection.md` § **5.4** -- rewritten. The ordering step gave the
  limitation as its REASON ("backup opens through the ordinary writer factory and cannot back up a
  datastore at a version this build does not serve"), which sent a post-pull operator to `cp`. It
  now says the order is the tidy one rather than a required one, and 🔴 to take the backup anyway if
  the pull already happened.
  🔴 **This bullet first read "checked, no change owed", from a search that reached § 2.3 and
  stopped.** § 2.3 genuinely has no ordering warning; § 5.4 did. Kept as a correction rather than
  overwritten, because the wrong disposition is the reusable lesson
  (`learnings.md` § *A truncated search proves nothing about what it did not reach*).

**Done when:** the suite is green, the new tests are verified to go red without the fix, and
`/prawduct:critic` returns zero blocking.

## Status

- [x] Chunk 01 — the copying handle, and the remedy asserted

## Context

Branch `fix/backup-at-any-schema-version` off `develop` at `3985f77`. Found while rehearsing
`docs/first-production-connection.md` § 2.3 against the sandbox store, which sits at schema 4
against a build serving 9.

**Closed 2026-09-10** across three commits — `4267038` (the copying handle), `0fbf985` (the
per-state remedy, the optional version, the source-level exemption walk) and `acc80d4` (restoring a
settings file a broad `git add` had swept in). Four Critic rounds; the last returned 0 blocking,
0 warning, 0 note. Suite green at 1271.

**Not built here, deliberately:** nothing pins `enabledPlugins["prawduct@prawduct"]` to `true`, so
the sweep that flipped it would flip it again. The remedy that landed is procedural (a
`learnings.md` entry); a `tests/preferences/` guard is the stronger one and is judgeable work, so it
belongs in a chunk of its own rather than riding this one. Filed as **#88**
(`stage: ready`, effort S, `affected: .claude/settings.json, tests/preferences`).
