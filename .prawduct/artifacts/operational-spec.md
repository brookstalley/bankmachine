---
artifact: operational-spec
version: 1
depends_on:
  - artifact: nonfunctional-requirements
    file_path: .prawduct/artifacts/nonfunctional-requirements.md
  - artifact: security-model
    file_path: .prawduct/artifacts/security-model.md
  - artifact: observability-strategy
    file_path: .prawduct/artifacts/observability-strategy.md
last_validated: null
---

# Operational Specification — bankmachine

**Scope:** how this product is installed, scheduled, backed up, and repaired on the one machine it
runs on.

**Dependency note.** The template's usual upstreams exist as artifacts; `docs/system-requirements.md`
§1 and AC-ARCH.1–ARCH.4 are the underlying source.

**Build status.** Installation and the CLI exist (steps 1–2). 🔴 **Scheduling does not exist yet**
(step 8) — this document specifies it. Sections are marked *(built)* or *(specified)* so an operator
can tell what they can actually do today.

---

## Direction

Norms. These bind future work; departure is a recorded decision, never silent
(`/prawduct:methodology norms`). Ratified 2026-09-07 by the owner.

- **No filesystem path is hardcoded.** The datastore path, log directory, and config location are
  configuration with documented defaults, and no code path assumes where the repository lives.
  Why: AC-ARCH.4. The requirements document originally named an absolute repository and datastore
  path that did not match reality; rather than make either literal true, both became configuration.
  It is the same config-over-code line the provider-agnostic norm draws, applied to the filesystem —
  and it is what lets sandbox and production be separate files rather than separate ceremonies.
  Status: steady-state.

- **Nothing brings a datastore, or a backup, into existence implicitly.** Only an explicit
  `store init` and the migration runner may create a datastore; `store backup` refuses an existing
  destination and refuses a destination directory that is not there.
  Why: a plain connect to a missing path silently creates an empty encrypted database *(measured)*,
  and under a configurable path a typo would then answer every question confidently from nothing.
  The backup half is the same rule pointed at a different hazard: a command that creates directories
  on request will eventually write a backup to a path nobody meant, and one that overwrites will
  eventually overwrite the only good copy. **A typo'd path must be reported, not populated.**
  Status: steady-state.

---

## Deployment

### Target *(built)*

| | |
|---|---|
| Platform | **macOS (Apple Silicon), native. Not containerized.** |
| Runtime | Python — `requires-python >= 3.11`; the pinned interpreter is **3.14** |
| Package manager | `uv`, with `uv.lock` pinning the full dependency graph |
| Install shape | A git clone plus `uv sync`. There is no published package |
| Deploy target | **This machine.** No staging, no remote, nothing to roll out to |

🔴 **macOS-only for v1, but with seams from day one.** The credential store goes behind the `keyring`
library rather than direct `security` CLI calls, and the scheduler goes behind an interface with
launchd as its only implementation. Documentation states *"macOS supported, other platforms
unverified."* A public tool that hardcodes one OS's credential store is painful to port later and
nearly free to abstract now.

### Configuration *(built)*

🔴 **AC-ARCH.4 — no filesystem path is hardcoded.** The datastore path, log directory, and config
location are configuration with documented defaults; **no code path assumes where the repo lives.**

**Precedence, highest first:** an explicit argument → an environment variable (`BANKMACHINE_*`) → the
config file → the documented default.

| Setting | Default |
|---|---|
| Config file | `~/.config/bankmachine/config.toml` (`$XDG_CONFIG_HOME` honoured) |
| Datastore | `$XDG_DATA_HOME/bankmachine/store.db`, falling back to `~/.local/share/bankmachine/` |
| Log directory | `$XDG_STATE_HOME/bankmachine/logs`, falling back to `~/.local/state/` |
| Keychain service | `bankmachine` |
| Busy timeout | 5000 ms |
| Environment | `sandbox` |

🔴 **Sandbox and production are separated at four levels**, and the redundancy is deliberate: a
different **datastore file** (`store-sandbox.db` vs `store.db`), a different **keychain account for the
datastore key**, a different **keychain account for the aggregator secret**, and a **loud banner at
every startup** (AC-10.6). Distinct default paths mean mixing them takes an explicit act rather than
an omission — and setting the sandbox secret cannot overwrite production's.

🔴 **`.env` is not loaded automatically, on purpose.** There is no dotenv dependency: *a file that is
silently read is a file whose contents are silently trusted.* `.env.example` documents the variables;
the operator `source`s it or moves the values into the config file.

🔴 **The aggregator secret has no environment variable at all**, and adding one would put a live
credential into every process listing and shell history that touched it. It is set through
`bankmachine connector set-secret`, which prompts without echoing.

### First install *(built, and AC-ARCH.1 is its acceptance test)*

```
git clone <repo> && cd bankmachine
uv sync
git config core.hooksPath .githooks     # the pre-push leak guard
cp .env.example .env && $EDITOR .env    # client id; NOT the secret
source .env
bankmachine connector set-secret        # prompts; writes to the keychain
bankmachine store init                  # generates the key, creates the datastore
bankmachine store status                # exit 0 = healthy
```

🔴 **AC-ARCH.1 requires this to work on a clean machine with no undocumented manual steps.** It is an
acceptance criterion, not documentation courtesy — the check is that a fresh clone produces a working
sync, and it is **owed as an automated test** rather than a remembered procedure.

### Scheduling *(specified — build step 8)*

**A launchd user agent, daily.** Survives reboot, requires no open terminal, and runs as the operator
so the keychain is reachable.

🔴 **AC-ARCH.2 — a missed window is recovered on next wake, never skipped.** A laptop asleep at the
scheduled time is the normal case, not the exception, and skipping is silent data staleness — the
product's named primary failure mode arriving through the scheduler. This is why **cron was rejected**:
it does not survive sleep or handle missed windows on macOS. launchd's `StartCalendarInterval` fires
on wake for a missed window; that behaviour is the requirement, and it must be **verified by actually
installing and firing the agent, including a missed-window case — not simulated.**

The agent invokes the CLI, so 🔴 **the exit-code contract is a scheduling contract**: `1` means the run
found a problem, `2` means it could not run. Collapsing them would make a broken scheduler
indistinguishable from a degraded feed.

**Rollback** is `git checkout` plus `uv sync`. There is nothing deployed to roll back — but note that
🔴 **a schema migration is forward-only and is not rolled back by reverting code.** A process that does
not recognize the datastore's schema version refuses to serve rather than guessing, so an accidental
downgrade fails loudly instead of corrupting. Recovery is restore-from-backup, below.

---

## Backup & Recovery

### 🔴 The single most important operational fact in this product

**The datastore key cannot be recovered once lost. The data is then gone — permanently, with no
remedy.**

The two missing-secret error types are deliberately distinct because their remedies share nothing: an
**aggregator secret** is always re-readable from the vendor's dashboard; a **datastore key** has no
such source. A backup of the datastore without the key is a backup of noise.

**Therefore: back up the key and the datastore, and treat them as one unit.** The key is 64 hex
characters in the keychain; it belongs in the operator's password manager or a paper copy in a safe —
somewhere that survives the machine's disk *and* the machine's keychain.

### What backup looks like *(built)*

```
bankmachine store backup ~/backups/bankmachine-2026-09-06.db
```

🔴 **Do not use `cp`.** The datastore runs in WAL mode, so copying `store.db` alone silently loses
whatever is still in `store.db-wal` — and that state is not exotic: it is what a killed sync (AC-2.5)
leaves behind, and what exists any time the MCP reader is open while the sync writes. **Measured: a
source with a 2 MB hot WAL holding 300 rows produced a `cp` copy short of every one of them.** The
command's own test asserts that gap, so the guarantee is checked rather than claimed.

`store backup` instead:

1. **Takes the exclusive writer lock**, so no sync run can be committing while the copy is made.
   Consistency is a consequence of the lock rather than of timing.
2. **Writes a single file** with the WAL folded in — no `-wal` or `-shm` companion to keep together,
   so a restore cannot be half-done.
3. **Verifies before reporting success** — reopens the copy with the datastore key and runs
   `integrity_check`. 🔴 *An unverified backup is a belief about a file, and the whole point of the
   file is the day the belief gets tested.*
4. **Never overwrites.** An existing destination is refused by name.
5. **Leaves no debris on failure.** This matters more than it sounds: taking the copy from a
   *read-role* handle fails and leaves a **zero-byte file** behind *(measured)* — a file
   indistinguishable from a backup until the day it is needed. That hazard is why the command is a
   writer-role operation, and a test asserts no file survives a failed run.

🔴 **The copy is ciphertext** — AC-ARCH.5's page-level AES-256 with the header encrypted, so it
leaves the machine as ciphertext with no export step. FileVault does not cover a backup; this does.
A wrong key raises rather than returning garbage.

**Known limitation:** `store backup` requires a datastore at a schema version this build serves,
because it opens through the ordinary writer factory. Backing up *before* a migration that has not
been written yet is therefore not available — copy the three files by hand for that case, with no
process running.

**Still manual:** nothing schedules this. The command exists; running it is the operator's, and
automating it is not yet specified.

### Restore

Copy the backup to the configured datastore path, ensure the key is in the keychain under the right
account **for that environment**, then `bankmachine store status`. A copy made by `store backup` is a
single file, so there is no WAL or shm to keep with it and no way to restore a partial set.

A wrong key **raises rather than returning garbage** *(verified)*, so a mismatched restore fails
loudly instead of presenting an empty or corrupt store as a working one.

🔴 **This path is not yet tested end to end by a human.** The command's own tests reopen the copy
through the ordinary reader — the same route a restore takes — so the copy is known to be readable;
what is unrehearsed is the operator procedure around it. That is what the restore runbook is for.

### RPO / RTO

**Deliberately none.** This is a personal tool with no availability obligation — and 🔴 **the data is
substantially re-derivable**: transactions can be re-pulled from the aggregator within the granted
history window, and the silver layer rebuilds from the bronze layer with `store rebuild`.

**What is *not* re-derivable, and this is the asymmetry that matters:**

- 🔴 **The daily balance series.** No aggregator backfills it. A day not captured is a day gone for
  good — which is why `balances_daily` is append-only with the composite primary key enforcing it.
- **History older than the granted window**, once outside it.
- **Manually imported data**, if the source files are gone.

So the backup priority is not "the database" generically. It is **the balance series and the manual
imports** — the parts no re-sync can rebuild.

---

## Monitoring

Fully specified in `observability-strategy.md`; the operational summary is:

| Question | Command |
|---|---|
| Is the datastore healthy? | `bankmachine store status` — exit `0` healthy, `1` unhealthy, `2` could not run |
| Did last night's sync run? | `get_pipeline_health`, or `last_success_at` in `sync_state` |
| What happened during a run? | The log directory, per-run correlation id |
| Why is this number wrong? | `bankmachine sync shell` — read-only SQL over the real datastore |

🔴 **No alerting, no dashboards, no third-party monitoring — structurally, not for cost.** AC-10.4
forbids telemetry and third-party error reporting outright. The alerting story is that warnings ride
**in-band on every response**, so the consumer that most needs to know the data is stale finds out at
the moment it asks. `observability-strategy.md` § Alerting records the condition under which that
decision should be revisited.

---

## Failure Recovery

The blast radius is one machine and one operator's data, so most recovery is "run the repair command."
The exceptions are marked.

| Failure | Signal | Recovery |
|---|---|---|
| **One connection broke** (auth required, locked, institution down) | `status = 'degraded'` + error code + `last_success_at` | `sync repair` → update-mode enrollment URL. 🔴 **History and cursor are preserved** (AC-4.3). The other connections were never affected (AC-4.1) *(specified — step 6)* |
| **Rate limited** | Error code on that connection | Backoff; the next scheduled run resumes. Never a crash |
| **Initial backfill not ready** | Not-yet-ready response | 🔴 Backoff-and-retry, not failure (AC-2.6). A multi-year backfill takes time to materialize |
| **Crash mid-sync** | Run absent from logs; cursor unchanged | 🔴 **Nothing to do.** The cursor never advances without the data it accompanies committing in the same transaction, so the next run resumes without loss (AC-2.5). *Tested by killing the process mid-pagination* |
| **Missed schedule** (machine asleep) | `stale` | launchd fires on wake (AC-ARCH.2). If it did not, run the sync by hand and investigate the agent |
| **Datastore locked** | `database is locked` after busy timeout | A writer is running. Wait. 🔴 The `flock` is released by the kernel even on SIGKILL — which is why it is a `flock` and not a lease row in `sync_state`, since a lease survives SIGKILL and strands every later run |
| **Schema version unrecognized** | Hard error from `store status` / health | Code and datastore disagree. Run the migration forward, or check out matching code. 🔴 **Never "just open it anyway"** — that is how plausible wrong answers get served |
| **Wrong environment used** | The startup banner | Stop. The datastores are separate files, so sandbox data did not land in production. Confirm with `store status` against each |
| **Log directory unusable** | Logs on stderr only | Not urgent — the run continues by design. Fix the directory |
| **Aggregator secret lost** | `AggregatorCredentialMissingError` | Re-read it from the vendor dashboard, `connector set-secret` |
| 🔴 **Datastore key lost** | `DatastoreKeyMissingError`; data unopenable | **Unrecoverable.** Restore key + datastore from backup. If there is no backup, the data is gone — re-enroll and re-sync, accepting the permanent loss of the balance series |
| **Corrupt datastore** | SQLCipher errors | Restore from backup. Failing that, `store init` fresh and re-sync — same permanent loss |

### Runbooks

🔴 **None are written.** Every row above is a runbook trigger and several — key loss, corruption,
schema mismatch — are exactly the high-stakes, low-frequency procedures a runbook exists for, where
the operator will be improvising under pressure against data that cannot be recovered by trying again.

Authoring them is `/prawduct:runbook`'s job. The two worth writing first are **restore-from-backup**
and **repair a degraded connection**, in that order: the first because it is unforgiving and untested,
the second because it is the one that will actually happen most often.

---

## Owed

Operational gaps, recorded so they are filed rather than rediscovered. None is a defect in shipped
code; each is work the build sequence has not reached, except the first.

| Gap | Why it matters |
|---|---|
| **Backup is not scheduled** | `store backup` exists and is verified, but nothing runs it. The remaining half of the gap: a backup command nobody invokes protects nothing. Wiring it into the same launchd agent as the sync is the obvious answer and is not yet specified |
| **The key is still backed up by hand** | The command cannot do this — writing the datastore key anywhere the product controls would defeat the keychain. It stays an operator instruction, and it is the half with no remedy |
| **AC-ARCH.1 is not an automated test** | "A fresh clone works on a clean machine" is asserted, not checked, and it decays silently with every undocumented step someone adds |
| **launchd agent not built** (step 8) | Including the missed-window recovery case, which must be verified by firing it for real rather than simulated |
| **No runbooks** | See above |
| **Log rotation unspecified** | Low stakes — local files, trivial volume — but unbounded |
| **Raw-response retention undecided** | `docs/system-requirements.md` §9 open question 1; the recommendation is to keep indefinitely |
