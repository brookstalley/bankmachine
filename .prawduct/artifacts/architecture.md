---
artifact: architecture
version: 1
depends_on:
  - artifact: system-requirements
    file_path: docs/system-requirements.md
last_validated: null
---

# Architecture — bankmachine

**Scope:** the process and component topology, and the rules that keep two runtimes coherent over
one encrypted datastore file. This artifact is the home of **AC-ARCH.7**, which
`docs/system-requirements.md` deliberately deferred to it: *"Journal mode, locking behavior, and
reader isolation under encryption are specified in the system architecture, not left to whichever
component encounters them first."*

**Dependency note.** The template's usual upstreams — product brief, data model, security model,
non-functional requirements — do not exist as separate artifacts in this project. Their content
lives in `docs/system-requirements.md` (§0 purpose, §4 data model, §6 security), which is this
artifact's single upstream. That is a recorded shape, not an oversight; the strategy-artifact
coverage advisory tracks the rest.

**Evidence.** Every concurrency claim below was measured on this machine (macOS, Apple Silicon,
CPython 3.12.3) against `sqlcipher3-wheels` 0.5.7 — SQLCipher 4.12.0 community, OpenSSL provider,
SQLite 3.51.1 — not taken from documentation. Measurements are marked *(measured)* and the
probe results are summarized in the Decision Log. One prior assumption was falsified by probing
and is recorded there.

---

## Direction

Norms. These bind future work; departure is a recorded decision, never silent
(`/prawduct:methodology norms`). All four are born **before any code exists**, so no retroactivity
decision applies — there is nothing to migrate, contain, or grandfather.

- **The datastore has exactly one writer process at a time, and it is a sync-role process.**
  The scheduled sync, a manual `sync run`, `enroll`, `repair` and `import` all take the same
  exclusive lock before opening a write transaction; the MCP server never takes it.
  Why: two SQLCipher connections cannot both hold a write transaction — the second fails with
  `database is locked` once `busy_timeout` expires *(measured)*, and a retry that succeeds partway
  through would interleave cursor advances against the data they are supposed to accompany,
  breaking AC-2.1's transactional-cursor guarantee and AC-2.5's crash-resume guarantee. Serializing
  at the process boundary turns a data-corruption risk into a wait or a clean refusal.
  Status: steady-state.

- **The MCP server opens the datastore `query_only` and holds no read transaction across tool
  calls.** One snapshot per tool call, released before the call returns.
  Why: two reasons that happen to agree. `query_only` is the mechanism that makes AC-10.5's
  read-only claim enforced rather than asserted — it refuses writes at the SQLite layer
  (`attempt to write a readonly database`, *measured*). And a pinned snapshot starves WAL
  checkpointing: with one reader holding an open read transaction, a passive checkpoint moved
  **0 of 93** frames, and the same checkpoint moved **93 of 93** the moment the reader released
  *(measured)*. An MCP server lives as long as its client, so a snapshot held across tool calls
  would let the WAL grow unbounded through the multi-year initial backfill.
  Status: steady-state.

- **No component creates the datastore implicitly.** Only an explicit `store init` and the
  migration runner may bring a datastore file into existence; every other connection opens a file
  that must already be there, and reports its absence.
  Why: a plain `connect()` to a missing path silently creates an empty database *(measured)*. Under
  AC-ARCH.4 the datastore path is configuration, so a typo'd or unset path would produce a valid,
  empty, correctly-encrypted store — and every downstream answer would be confidently derived from
  nothing. AC-ARCH.3 requires the MCP server to *report* an empty or missing datastore through
  `get_pipeline_health`; it cannot report a state it just erased by creating the file.
  Status: steady-state.

- **A process that does not recognize the datastore's schema version refuses to serve, loudly.**
  The reader compares the stored schema version against the range it was built for and, on a
  mismatch, answers `get_pipeline_health` with a hard error instead of serving queries.
  Why: AC-4.4 names silent staleness as this system's primary failure mode, and a reader running
  older code against a migrated schema is the same failure with a different cause — it would return
  plausible, structurally valid, wrong answers. Refusing is recoverable; a wrong number that looks
  right is not.
  Status: steady-state.

---

## Overview & Topology

Three runtimes and one CLI, all on one machine, all owned by one operator, sharing one encrypted
file. Nothing listens on a network socket.

```
                          ┌─────────────────────────────────────────┐
  aggregator API ──HTTPS──┤ sync process          (writer)          │
                          │  launchd-scheduled, daily; also on      │
  export files ───────────┤  demand via CLI                         │
                          └──────────────┬──────────────────────────┘
                                         │ exclusive lock, then writes
                                         ▼
                          ┌─────────────────────────────────────────┐
                          │  store.db  (SQLCipher, WAL)             │
                          │  + store.db-wal, store.db-shm           │
                          └──────────────┬──────────────────────────┘
                                         │ query_only, short snapshots
                          ┌──────────────┴──────────────────────────┐
  MCP client ────stdio────┤ MCP server            (reader)          │
  (analyst agent)         │  launched and owned by the MCP client   │
                          └─────────────────────────────────────────┘

  operator ──terminal─────► CLI: enroll · repair · import · sync run · store init · sync shell
                            (writer-role commands take the same lock as the sync process)

  secrets: macOS Keychain via `keyring` — datastore key, aggregator credentials, access tokens
  config:  a file at a documented default path; no path is hardcoded (AC-ARCH.4)
```

**Why the product is split at all.** Not scale, and not isolation — the split is forced by
**lifecycle mismatch**. The sync must run daily whether or not anyone is at the keyboard
(AC-ARCH.2), while the MCP server exists only for the duration of an MCP client session and is
started and stopped by that client, not by us. Neither can host the other. AC-ARCH.7's requirement
that "the MCP server must not depend on sync liveness, nor the reverse" is the direct consequence:
these two processes have no lifecycle relationship at all, and the datastore file is their only
contact.

---

## Components & Responsibilities

### Sync process — the writer

- **Purpose:** move data from the aggregator API and from import files into the datastore.
- **Owned state:** every table. It is the sole writer of all of them.
- **Never:** serves a query to anything but the operator's own terminal; opens a listening socket;
  writes without holding the exclusive lock.

### MCP server — the reader

- **Purpose:** answer the §5 tool surface over stdio from the datastore as it currently stands.
- **Owned state:** none. It is authoritative for nothing and persists nothing.
- **Never:** writes to the datastore; triggers a sync; opens a network socket (AC-10.5); creates
  the datastore file; holds a read transaction across tool calls.

### CLI — the operator's hands

- **Purpose:** enrollment, repair, import, manual sync, `store init`, and `sync shell` (AC-ARCH.6 —
  built in step 1 because it is the debugging affordance for every step after it).
- **Owned state:** none of its own; its writer-role subcommands act *as* the writer and take the
  same lock.
- **Never:** a second, parallel implementation of sync logic. `sync run` invokes the same code path
  the scheduler does, so what the operator debugs is what the schedule executes.

### Scheduler — an interface with one implementation

- **Purpose:** start the sync process daily, surviving reboot and sleep, with no terminal open.
- **Implementation:** launchd (`StartCalendarInterval`), which is the only implementation in v1.
- **Never:** carries product logic. It starts a process; everything else is the sync process's job.
  This is the seam from the recorded macOS-with-seams decision — a port supplies a new
  implementation, not a refactor.

**Two components sharing a purpose would be a smell.** None do here: writer, reader, operator
entry point, and process starter are four distinct jobs.

---

## Communication & Boundaries

There are exactly three channels, and one of them is the datastore file.

| Channel | Transport | Direction | Sync/async | Contract |
|---|---|---|---|---|
| MCP client ↔ MCP server | stdio (JSON-RPC), same machine | request/response | sync | The §5 tool surface. **This is the product's API contract** — it belongs in `api-contract.md`, not restated here |
| sync process → aggregator | HTTPS to the aggregator's API hosts, the only network destinations permitted (AC-10.4) | outbound only | sync | The aggregator's own API; wrapped behind the connector boundary (§3 scope note) |
| sync process ↔ MCP server | **the datastore file** | writer → reader | async | This section's Data Ownership rules |

**The shared database is a communication channel, and it is the only one these two processes
have.** Its coordination rule is the whole of the next section. There is no queue, no socket, no
IPC, and no signal between them — deliberately, because AC-ARCH.7 forbids either depending on the
other's liveness, and any direct channel would create exactly that dependency.

**Trust boundaries.** Only one channel crosses one: the aggregator API is third-party and outbound.
The stdio channel is same-machine, same-user, and carries no credential. There is no untrusted
inbound surface anywhere in the topology — which is what makes AC-10.5 (no sockets) affordable, and
is why that criterion is now a confirmed decision rather than an unexamined default.

`boundary-patterns.md` is still a template; populating it is build-step-1 work, and the datastore
schema is its first real entry.

---

## Data Ownership & Consistency

**This section is AC-ARCH.7.**

### Journal mode: WAL

The datastore runs in WAL mode, set once at `store init` immediately after keying — `PRAGMA key`
first, always, then `PRAGMA journal_mode=WAL`, which returns `wal` and persists in the file header
*(measured)*.

WAL is what makes the topology legal. In rollback-journal mode a writer blocks readers for the
duration of its transaction, so a multi-hour historical backfill would make the MCP server
unavailable for as long as it ran. Under WAL, a reader issued a query while the writer held an open
write transaction returned immediately with the pre-transaction row count, and saw the new count
after the writer committed *(measured)* — snapshot isolation, no blocking, in both directions.

**The WAL file is encrypted.** A scan of `store.db`, `store.db-wal` and `store.db-shm` for a known
plaintext marker written through the schema found it in none of them *(measured)*, and the main
file's first 16 bytes are ciphertext rather than the `SQLite format 3` magic. This matters because
AC-ARCH.5 asks that a file-copy backup be ciphertext without further work, and a WAL-mode database
is **three** files, not one — had the WAL been plaintext, enabling WAL would have quietly defeated
the encryption criterion for any data not yet checkpointed.

**Operational consequence for backups:** copying only `store.db` can lose committed data still
living in the WAL. A backup either checkpoints first (`PRAGMA wal_checkpoint(TRUNCATE)`, which
drains the WAL to 0 bytes *(measured)*) or copies all three files together. This belongs in the
operational spec; it is recorded here because it is a direct consequence of the journal-mode
decision.

### Locking: one writer, enforced above SQLite

SQLite's own locking is necessary but not sufficient. A second connection attempting
`BEGIN IMMEDIATE` while the first held a write transaction failed with `database is locked` once
`busy_timeout` expired *(measured)* — correct, but the failure lands at an arbitrary point in a
sync run, and a longer `busy_timeout` only converts a fast failure into a slow one.

So writer-role processes serialize **before** they touch the database, on an OS advisory file lock
(`flock`) on a lockfile beside the datastore:

- The lock is taken for the duration of a sync run and released on exit.
- **The kernel releases it if the process dies**, which is the property a lease row in the database
  cannot offer: AC-2.5 requires that a process killed mid-pagination be recoverable, and it is
  tested by actually killing the process. A database lease would survive that kill and strand the
  next run behind a lock whose holder no longer exists.
- A writer that cannot take the lock **reports that another sync is running and exits non-zero.**
  It does not wait indefinitely and does not proceed anyway.
- `busy_timeout` is still set on every connection as the second layer. Two layers, because the
  lockfile is advisory and coordinates only processes that agree to use it.

The MCP server never takes this lock — it has nothing to serialize against, since WAL readers do
not block and are not blocked.

### Reader isolation

The MCP server connects, keys, and immediately issues `PRAGMA query_only=ON`. Writes then fail at
the SQLite layer *(measured)*, which makes AC-10.5's read-only guarantee structural rather than a
convention every future tool author has to remember.

**Why `query_only` on a read-write handle rather than `mode=ro`.** Both work. `mode=ro` was
probed against a hot WAL left by a `SIGKILL`ed writer and read the committed-but-uncheckpointed
rows correctly, including with an unwritable containing directory *(measured)* — so the read-only
WAL hazard this design was originally going to route around does not bite here. `query_only` is
chosen anyway for two reasons that survive that finding: it keeps one connection-opening path for
all components with a single flag distinguishing them, and it does not depend on a `-shm` file
already existing, which `mode=ro` does in the general case. **The falsified assumption is recorded
in the Decision Log** rather than quietly dropped.

Each tool call opens its snapshot and releases it before returning. A read transaction spanning
tool calls would pin the WAL against checkpointing — the measured 0-of-93 starvation above — and
an MCP session outlives any individual query by hours.

**WAL size is bounded by `wal_autocheckpoint` (1000 pages at a 4096-byte page size, both defaults
confirmed *measured*), not by zero.** The file holds its high-water mark and is reused in place
rather than shrinking after a checkpoint *(measured)*. A steady-state WAL of a few megabytes is
normal and is not a leak; `wal_checkpoint(TRUNCATE)` is the remedy if it is ever wanted, and
`sync shell` is where an operator would run it.

### Single-writer table ownership

Every table in FR-6 is written by the sync role and by nothing else. There are no exceptions, so
there is no exceptions list to maintain — which is the point of stating it as a norm rather than a
description.

### Consistency model

Read-your-writes does not apply across processes here and is not needed: the MCP server's consumer
is an analyst agent asking about months of history, not a user awaiting confirmation of a write.
What the reader gets is a consistent snapshot as of the start of the current tool call. A sync
committing mid-conversation means two tool calls in one conversation may see different data — which
is correct, and is precisely why AC-9.2 requires a freshness stamp on every response: the stamp is
how a snapshot boundary becomes visible instead of confusing.

There are no queues and no events, so at-least-once/ordering semantics do not arise. Idempotency is
still required of the sync path, but by AC-2.4 and AC-11.4 — a property of re-running against the
aggregator, not of message delivery.

---

## Failure Modes & Resilience

| What fails | Effect on the rest | Intended behavior |
|---|---|---|
| Sync process down or never scheduled | MCP server keeps serving | **Degraded, and loud.** Data ages; `get_pipeline_health` reports staleness per AC-4.4. The reader must never depend on sync liveness (AC-ARCH.7) |
| MCP server down | Sync unaffected | Hard failure of the query surface only. The MCP client restarts it |
| Sync killed mid-run | None — the datastore is consistent at the last commit | `flock` released by the kernel; cursor not advanced (AC-2.5); next run resumes |
| One connection broken (auth, locked, institution down) | Other connections unaffected | Caught per connection (AC-4.1), recorded with error code and last-successful-sync timestamp (AC-4.2, AC-4.5) |
| Datastore missing or empty | Sync: creates it only via explicit `store init`. MCP: reports it | AC-ARCH.3; the third Direction norm forbids implicit creation |
| Keychain unavailable or key wrong | Both processes fail to open the datastore | **Fail closed and say so.** A wrong key surfaces as `file is not a database` *(measured)* — the message names the wrong cause, so it is caught and re-reported as an authentication failure rather than passed through |
| Missed daily window (machine asleep or off) | Data ages by one window | Recovered on next run, not skipped (AC-ARCH.2). The recovery does not rest on launchd's wake semantics: sync is cursor-based and idempotent, so the next run — whenever it happens — fetches everything since the last cursor. launchd firing a missed calendar interval on wake is a convenience on top of that, not the mechanism |
| Aggregator rate-limits or backfill not ready | Sync slows | Backoff and retry, never failure (AC-2.6) |

**Retry and backoff** apply on exactly one channel — the aggregator HTTPS one — because it is the
only channel that can fail transiently. The datastore channel does not retry: `busy_timeout` is the
only wait, and lock acquisition failure is a clean refusal rather than a retry loop.

**Restart order does not exist,** deliberately. Neither process needs the other running, so there is
no ordering constraint to get wrong. Crash state is entirely in the datastore, recovered by
SQLite's own WAL recovery on the next write connection *(measured against a `SIGKILL`ed writer)*.

**What the operator sees.** Every failure above must reach `get_pipeline_health`, because the
operator's agent asks that tool rather than reading logs. A failure that only appears in a log file
is, for this product's actual user, a silent failure — the same defect shape this project has
already been burned by twice.

---

## Deployment & Version Skew

**One deploy unit.** All three runtimes ship from one checkout at one version; there is no
independent release and no supported mixed-version operation between them.

**Skew is still possible in one direction and is handled by refusal, not compatibility.** The
datastore outlives any checkout, so a long-running MCP server can find itself against a schema that
a newer sync process migrated underneath it. The fourth Direction norm covers this: the reader
checks the schema version and refuses rather than serving. It does not attempt to interpret a
schema it was not built for.

**Migrations run in the writer role,** under the same exclusive lock, so no migration can interleave
with a sync. Rollback of a migration that has already written data is not supported in v1 — the
recovery path is restore-from-backup plus rebuild-from-raw (AC-5.2), which exists precisely so that
a derivation mistake is a re-run rather than a re-fetch.

---

## Scaling Model

Largely **not relevant** — single operator, single machine, one deploy, a daily batch measured in
minutes. There is nothing to scale horizontally and no replica of anything.

Two limits are still worth naming, because they are the first things that would actually strain:

- **The initial historical backfill** is the largest workload the system ever runs, and it is a
  first-run-only event that also produces the most WAL churn. It is the reason the reader's
  snapshot discipline is a norm rather than a nicety.
- **`raw_responses` growth** is the only monotonically growing table, since raw preservation is
  append-only by design (FR-5) and retention is an open question in §9.1. At this volume the answer
  is very likely "keep indefinitely," but it is a decision that has not been made.

Deliberately not scaled for: multiple operators, remote access, and concurrent syncs. All three are
§2 non-goals, and the topology would have to change for any of them.

---

## Cross-Cutting Runtime Concerns

**Correlation.** A sync run carries a run id, recorded on the rows and the raw responses it
produces, so a questionable number can be traced to the response it derived from and the run that
fetched it. Combined with AC-5.3's derivation version, that is the full lineage: run id says *when
it was fetched*, derivation version says *how it was interpreted*.

**Time.** One rule, and it is already a requirement: transaction dates are calendar dates, sync
metadata are UTC instants, and the two are never mixed (AC-6.4). `mypy strict` is the mechanism —
distinct types, so mixing them is a type error rather than a code review. No clock drift concern
arises; all runtimes share one machine clock.

**Configuration and secrets.** Config lives in a file at a documented default path, overridable,
with no hardcoded paths anywhere (AC-ARCH.4). Secrets — the datastore key, aggregator credentials,
and all access tokens — live in the macOS Keychain through `keyring`, whose macOS backend was
confirmed on this machine with a set/get/delete round-trip *(measured)*. Config propagates by
process start: neither process polls or reloads, so a config change takes effect on the next sync
and the next MCP client session. That is acceptable for a daily batch and is stated so it is not
mistaken for a bug.

**The sandbox/production flag is a config value, logged loudly at every startup** (AC-10.6), and it
is cross-cutting because getting it wrong writes fixture data into the real datastore — a failure
that both processes would then faithfully report as real.

**Environment parity.** The topology *is* the dev environment: same machine, same processes, same
launchd job. The one divergence is the aggregator, which is the sandbox environment in development
and production in production — which is exactly what the flag above guards. Tests run against real
SQLCipher and a test-scoped Keychain service name rather than fakes, per `project-preferences.md`.

---

## Decision Log

**D1 — WAL journal mode.** *Chosen:* WAL, set at `store init` after keying. *Alternatives:*
rollback journal (the SQLite default; would block readers for the length of a backfill),
`journal_mode=WAL2` (not available in stock SQLCipher). *Why:* the reader must stay available while
the writer runs, which AC-ARCH.7 requires and only WAL provides. *Trade-off accepted:* three files
instead of one, a WAL that holds its high-water mark, and a checkpointing discipline the reader has
to honor. Verified before adopting that the WAL file is itself encrypted — had it not been, this
decision would have traded AC-ARCH.5 for AC-ARCH.7.

**D2 — Process-level exclusive lock for writers.** *Chosen:* `flock` on a lockfile beside the
datastore, held for a whole writer run. *Alternatives:* rely on SQLite locking plus a long
`busy_timeout` (fails at an arbitrary point mid-run); a lease row in `sync_state` (survives a
`SIGKILL`, which AC-2.5 explicitly tests for, and would strand every subsequent run). *Why:* the
unit that must be serialized is the *run*, not the transaction. *Trade-off accepted:* advisory only
— it coordinates cooperating processes and nothing else, which is sufficient because every writer
is our own code.

**D3 — `query_only` rather than `mode=ro` for the reader.** *Chosen:* open read-write, then
`PRAGMA query_only=ON`. *Why:* one connection path for every component, no dependency on a `-shm`
file already existing, and a SQLite-layer refusal that makes AC-10.5 structural. *Trade-off
accepted:* the handle is technically writable for the instant before the pragma, so opening the
datastore is one function with one flag rather than scattered per-component logic — the norm and its
enforcement test are what hold this, not the file mode.

**D4 — A prior assumption, falsified by probing.** Before measuring, this design was going to route
around a believed limitation: that a `mode=ro` connection cannot read a WAL-mode database when no
`-shm` file exists, and would therefore fail against a hot WAL left by a crashed writer. **It read
correctly in every case probed**, including after a `SIGKILL` mid-write and with an unwritable
containing directory. D3 stands on its other two reasons; the reason that did not survive contact
is struck rather than retained. Recorded because a specification that quietly keeps a falsified
premise teaches the next reader to trust the wrong thing — and because the probe cost ten minutes
against a criterion the requirements call the highest-stakes kind of mistake.

**D5 — No IPC between sync and MCP server.** *Chosen:* the datastore file is their only contact.
*Alternatives:* a Unix socket for the reader to request a sync, or a signal to notify of new data.
*Why:* AC-ARCH.7 forbids either depending on the other's liveness, and any channel would create the
dependency it forbids. *Trade-off accepted:* the reader cannot trigger a sync, and freshness is
reported (AC-9.2) rather than repaired on demand. This is the correct trade for a read-only
product whose §2 non-goals rule out write paths anyway.
