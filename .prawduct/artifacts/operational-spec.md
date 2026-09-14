---
artifact: operational-spec
version: 1
depends_on:
  - artifact: architecture
    file_path: .prawduct/artifacts/architecture.md
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

- **A backup destination is never created implicitly and never overwritten.** `store backup`
  refuses an existing destination, and refuses a destination whose parent directory is not there.
  Why: this is `architecture.md` § Direction's *no component creates the datastore implicitly*
  pointed at a second hazard, and the datastore half stays homed there rather than being restated
  here. A command that creates directories on request will eventually write a backup to a path
  nobody meant, and one that overwrites will eventually overwrite the only good copy — which, for
  the one series no re-sync can rebuild, is the whole loss. **A typo'd path must be reported, not
  populated.**
  Status: steady-state.

---

## Deployment

### Target *(built)*

| | |
|---|---|
| Platform | **macOS (Apple Silicon), native. Not containerized.** |
| Runtime | Python — `requires-python >= 3.14`, the same number as the pinned interpreter, because nothing here runs on another |
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
| Aggregator client id | none — `connector check` refuses without one |
| History window | 730 days (the aggregator's own documented maximum) — **read at every enrollment** |
| Connection cap | 10 — the aggregator plan tier (AC-1.5). Retired connections do not count |

🔴 **The history window is the one setting that cannot be corrected later, and `bankmachine enroll`
reads it every time.** It is what enrollment asks the aggregator to grant, and it becomes immutable
per connection at that moment (AC-1.2): changing the value afterwards moves nothing already
enrolled, it only changes what the *next* enrollment asks for. Getting it wrong costs a re-link of
every institution, which is why `enroll` prints the window and asks you to confirm it **before** the
exchange — the last point at which it can still be corrected.

What the aggregator actually *grants* may be less, and is **not visible at enrollment**: no response
in that path reports it (AC-1.3a). `connections.granted_history_days` is null until the first
backfill reveals the oldest transaction returned, and 🔴 **null means not yet known, never "we got
what we asked for"**.

**The connection cap** is configuration rather than a literal because plan tiers change (AC-1.5).
`enroll` refuses past it *before* creating a link token, so a full roster costs no browser round
trip, and the refusal lists the live connections with the command that retires one.
`bankmachine connections retire <id>` frees a slot and removes the connection at the aggregator so
it stops billing; every row it produced is kept (AC-1.6).

The default is therefore the **maximum**, deliberately: the two ways to be wrong are not
symmetric. Asking for more history than you need costs nothing and can be ignored; asking for less
costs history that cannot be bought back at any price. A value outside 1–730 is **refused rather
than clamped**, because a clamp would enroll at a window the operator never chose and never told
them about.

🔴 **Sandbox and production are separated at four levels**, and the redundancy is deliberate: a
different **datastore file** (`store-sandbox.db` vs `store.db`), a different **keychain account for the
datastore key**, a different **keychain account for the aggregator secret**, and a **loud banner at
every startup** (AC-10.6). Distinct default paths mean mixing them takes an explicit act rather than
an omission — and setting the sandbox secret cannot overwrite production's.

🔴 **A fifth separation, and the only one that refuses rather than diverging: opening the
per-environment datastore under the writer lock, or mutating a per-environment keychain entry,
will not run on an environment nobody chose.**

The predicate is stated as a property because the handles are what enforce it, and a list of
command names would already be wrong: it also refuses `store backup` (whose `copying_writer`
takes the writer lock to fold the WAL in) and `store key import` (which replaces the datastore
key). **That is deliberate for backup** — § 5.1 makes it a daily habit, and a backup silently
taken against the wrong environment announces itself only at a restore, which is the one moment
there is nothing left to fall back on. The other four keep the two
environments apart once you have said which one you are in; this one covers the case where nobody
said. `environment` has a default, and reads still take it — but the write path
(`require_chosen_environment`, called from the one writer factory in `store.connection` and from
the keychain mutators in `secrets`) refuses unless an exported variable or a config-file entry
selected it. **There is no `--environment` option** — the general precedence above still leads with
an explicit argument, but no command offers one for this value, so the two real channels are the
variable and the file. The guard is at those two chokepoints rather than in a list of command names, because
a guarantee defined by an enumeration decays at the first command nobody adds to the list.

🔴 **Two commands additionally ask the guard at their first statement, and that is a bounded
exception to the sentence above rather than a retreat from it.** The chokepoints remain the
guarantee: every per-environment write is refused there whether or not a command remembers. What a
front guard buys is *where the refusal lands*, and it is owed by a command that can reach an
irreversible remote or keychain effect **before its own first guarded write** — because at the
chokepoint the refusal is correct and already too late.

- `cmd_enroll` (`cli/enroll.py`) — enrollment's two per-environment writes both follow the exchange
  that mints a durable, billable Item, so a refusal at the write burns a real Item and discards the
  only handle to it. Not even `connections retire` could then remove it.
- `cmd_retire` (`cli/connections.py`) — the already-retired retry path runs only reads before
  `item_remove`, and reads are exempt. Refused at the chokepoint, the Item is already gone while
  `delete_access_token` fails, leaving a surviving credential that reads as "removal never
  confirmed" forever.

**The predicate is the membership rule, not the pair.** A new command joins this list when that
predicate holds of it, and a reader deciding about a third command asks the predicate rather than
matching against these two. Neither member is special; both are instances.

🔴 **`.env` is not loaded automatically, on purpose.** There is no dotenv dependency: *a file that is
silently read is a file whose contents are silently trusted.* `.env.example` documents the variables;
the operator `source`s it or moves the values into the config file. **For an unattended run the
config file is the answer, not a dotenv loader** — a scheduled job has no login shell to source
anything into, and `~/.config/bankmachine/config.toml` carries the same keys without the prefix.

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

The agent invokes the CLI, so 🔴 **the exit-code contract is a scheduling contract**: `0` means the
run finished, `1` means it found a problem, `2` means it could not run, and `75` (`EX_TEMPFAIL`)
means it ran, nothing is wrong, and history is still owed. Collapsing `1` and `2` would make a broken
scheduler indistinguishable from a degraded feed.

🔴 **`75` is the code this agent is built around, and it is why the agent needs one at all.** A
first sync on a real institution reaches `INITIAL_UPDATE_COMPLETE` — roughly thirty days of a
730-day grant — minutes to hours before the rest lands, and the aggregator does not call back. The
exit code is the only channel the agent reads: under a bare `0` it cannot tell a whole history from
thirty days of one, and the connection would sit at 4% of its data with `StartCalendarInterval`
next firing tomorrow. `NOT_READY` and a page run stopped at its ceiling are the same fact and the
same code. **The agent's behaviour on `75` is to re-run before the next calendar window** — that is
a requirement on build step 8, not an optimisation, and the interval is the step's to choose.
`1` outranks `75` on a run that produced both: a stuck connection needs a person, and the arriving
one gets its retry from the next scheduled window regardless.

🔴 **Upgrading across a schema migration has an order, and it is not the usual one.** A build that
does not recognize the datastore's version refuses to serve, so the datastore must be brought forward
*and* every process that reads it must be the new build — in this order, on one machine:

1. `launchctl unload` the sync agent, and stop the MCP server (a client disconnect is enough; the
   server is a subprocess started at connect time).
2. `bankmachine store backup`. Taking it here rather than after step 3 is the tidier order, because
   a copy at the version you are still running is one you can open without migrating it first — but
   it is no longer load-bearing. A store already sitting at a version this build does not serve is
   still copyable, which is the case reached by `git pull` before the backup.
3. `git pull && uv sync`.
4. `bankmachine store init` — the migration runner is idempotent and applies only what is pending.
5. `bankmachine store status` — exit 0, and the reported schema version is the new one.
6. Reconnect the MCP client and `launchctl load` the agent.

**Migrations 003 (`accounts.last_seen_date`, FR-9) and 004 (`connections.roster_observed_date`,
AC-12.4) are what this procedure has been written for.** Both are additive nullable columns,
applied forward-only, and neither backfills — so the first sync after the upgrade is what
populates them.

🔴 **Their upgrade windows are NOT the same, and 004's costs something 003's did not.** For 003 the
window reads as the pre-migration answer: nothing had ever been reported absent, so nothing changes.
**For 004 it is a regression, briefly.** Before it, an account was measured against the maximum of
its own connection's last-listed dates, so an account whose siblings kept being listed *already*
read `no_longer_reported`. After 004, `roster_observed_date` is null for every connection, the
"never observed" branch marks nothing absent, and **those same accounts revert to `active` beside a
frozen balance with no warning** — FR-9's own failure, restored for the length of the window.

**For a healthy connection the window closes at the next successful sync.** For a connection that
never syncs successfully again — login expired, never repaired with `connections reauth` — **it
never closes on its own, and that is exactly the connection whose absent accounts matter most.**

🔴 **So run `bankmachine store rebuild` after step 5 if any connection is not syncing** — and
`bankmachine connections list` is how you tell, since at this point in the procedure the MCP client
is disconnected and the agent unloaded, so `get_pipeline_health` cannot answer it. It replays
every archived `/accounts/get` through the accounts deriver, which populates the column from the
same responses the original observation came from. Expect it to report a content change:
`content_digest` walks every table, `connections` included. This is the remedy; it existed before
this migration and simply was not written down.

> 🔴 **Amendment, 2026-09-10 — the rebuild is now UNCONDITIONAL after an upgrade, not conditional
> on a connection being stuck.** *Statement:* run `bankmachine store rebuild` after any migration,
> whatever `connections list` says. *Why:* the condition above was correct while the only
> derivation-owned column a migration left empty was the roster observation, which a healthy
> connection refilled at its next sync. Migrations 005, 007 and 009 each add a column that is
> stamped **once, at insert** — `ledger_date`, `lineage_id`, `transfer_pair_id` — so an ordinary
> sync refills them only for rows it ADDS, and every row already in the store stays null on a
> perfectly healthy connection. Until the rebuild runs, every windowed total silently excludes
> the unstamped rows: it is a floor, not a measurement. The answers disclose that with a
> `partial` warning naming the count and this command, but a disclosure is not the fix.
> *Retroactivity:* none owed — the condition was true of the migration it was written for.
> Migration 011 has the same shape in a table: a sync records a refused position only for the
> captures it derives, so every capture already archived stays unnamed on `list_holdings` until the
> rebuild runs.

🔴 **A derivation-version bump needs the same rebuild, with or without a migration, and it is now
visible rather than remembered (AC-5.4).** `store status` prints a `derivation:` line listing every
version a derived row carries beside the one this build derives. Every MCP answer carries
`derivation_version_mismatch`, and `get_pipeline_health` carries `coverage.derivation`, until no
older version remains. So step 5's check is also the rebuild's check: if the `derivation:` line
lists more than this build's version, run `bankmachine store rebuild`, then run `store status`
again and see the list collapse to one. A version *newer* than this build's means the reverse: this
checkout is older than the one that derived the rows, so upgrade it. `store rebuild` refuses in
that state, before deleting anything.

The MCP server and the CLI must be upgraded *with* the datastore — an older reader
refuses to serve, which is the norm working rather than a fault.

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

```
bankmachine store key export          # renders it; refuses a non-terminal stdout
bankmachine store key export --to P   # writes 0600 to a path you name, if you'd rather not retype
bankmachine store key verify          # confirms the copy you stored opens THIS datastore
bankmachine store key import          # puts it back, on a machine whose keychain lost it
```

🔴 **`verify` is the half that is easy to skip and expensive to skip.** Shape validation catches a
malformed key and never a *wrong* one: a transposed pair of hex digits is still 64 characters of
valid hex and is simply a different key. Without this command there is no moment at which an
operator can learn their stored copy is bad while it is still fixable — SQLCipher raises on first
open, which is correct and useless, because that is the moment nothing can be done.

`import` opens the datastore with the candidate before it writes the keychain entry, so a typo
cannot replace a working key.

The operator-facing form of this — the keychain recipe, and the moment in the cutover to run it,
which is immediately after `store init` and before any data exists — is
`docs/first-production-connection.md`, along with the ordered production procedure, the day-one
checks and the standing daily routine this section's guarantees depend on.

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
5. **Removes its own debris on failure.** This matters more than it sounds: taking the copy from a
   *read-role* handle fails and leaves a **zero-byte file** behind *(measured)* — a file
   indistinguishable from a backup until the day it is needed. That hazard is why the command is a
   writer-role operation, and why the failure path **unlinks the destination itself** rather than
   trusting `VACUUM INTO` to have tidied up: the guarantee is enforced here, not inherited. The
   error message says which happened — *no partial file was left* and *a partial file was removed*
   are different facts about the run. A test reproduces the measured hazard in a writable directory
   and asserts the file is gone, with a negative control that neutralizes the cleanup and confirms
   the driver really does leave one.

🔴 **The copy is ciphertext** — AC-ARCH.5's page-level AES-256 with the header encrypted, so it
leaves the machine as ciphertext with no export step. FileVault does not cover a backup; this does.
A wrong key raises rather than returning garbage.

🔴 **A copy runs at any schema version, including one this build does not serve.** `store backup`
opens through `copying_writer` rather than the ordinary writer factory: a page-level copy answers no
question, so the check that stops a process serving an unrecognized schema has nothing to protect
here (`architecture.md` § Direction, the fourth norm's 2026-09-10 ruling). This is what makes 🔴 "do
not use `cp`" unconditional — there is no longer a case the command cannot cover. The copy reports
the version it was taken at, and says so when this build cannot serve it — 🔴 with the remedy for
**that** state rather than one sentence for both: a copy BEHIND this build is migrated forward by
`store init`, and a copy AHEAD of it is not, because migrations are forward-only. The note takes its
action clause from `remedy_for`, the vocabulary every other unhealthy-store surface uses, so the two
cannot drift apart. A copy recording **no** version is reported rather than refused: that is a
migration that died between creating the file and stamping the version, which is the state most
worth holding a copy of.

**Still manual:** nothing schedules this. The command exists; running it is the operator's, and
automating it is not yet specified.

### Restore

Copy the backup to the configured datastore path, put the key back in the keychain under the right
account **for that environment**, then `bankmachine store status`:

```
cp <backup> <datastore path>          # the configured path for THIS environment
bankmachine store key import          # prompts; refuses a key that does not open the copy
bankmachine store status
```

A copy made by `store backup` is a single file, so there is no WAL or shm to keep with it and no way
to restore a partial set.

A wrong key **raises rather than returning garbage** *(verified)*, so a mismatched restore fails
loudly instead of presenting an empty or corrupt store as a working one. 🔴 **`store key import`
makes that failure arrive one step earlier and one step safer**: it opens the copy with the
candidate before it writes the keychain, so a mistyped key is refused rather than stored, and
whatever key was already there is untouched. Before FR-12 this step was an instruction — *restore
the keychain entry from your backup* — with no command behind it.

**Walked against sandbox on 2026-09-10** (`docs/first-production-connection.md` § 2.3): backup
verified, the copy opened at the configured path, `healthy: yes`. What that walk added to the
procedure is one expectation the reader needs: a restored copy reports `journal mode: delete`
until something writes to it, because SQLite's backup API writes the destination in the default
mode and the writer factory is what sets `PRAGMA journal_mode = WAL`. It flips on the first
writer open. An operator mid-recovery who reads `delete` where the healthy-store description
promises `wal` has not found a failed restore.

**The key half is now rehearsable, and was rehearsed 2026-09-10.** It was recorded here as
unrehearsable, and that was true while nothing could put a key back: the drill needs a store whose
keychain entry is *gone*, and before FR-12 that state had no exit. `store key export` → delete the
keychain entry → `store key verify` → `store key import` → `store status` walks it end to end
against sandbox at zero cost, and does. A two-character transposition was also injected and caught,
which is the failure the drill exists to prove is catchable.

🔴 **Still unwalked, and narrower than the claim it replaces: a restore into the *production*
path, and a round trip through the operator's OWN storage** — a password manager entry, a sheet of
paper — rather than through a file the drill wrote. The first needs production. The second is about
the operator's process rather than the product's mechanism, which is exactly why `store key verify`
exists and why `docs/first-production-connection.md` § 3.3 ends in it.

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
| Did last night's sync run? | `get_pipeline_health`, or `last_success_at` on the connection row — 🔴 which is stamped only once the whole granted history has landed, so a connection mid-backfill reads as not-yet-succeeded rather than as current |
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
| **One connection broke** (auth required, locked, institution down) | `status = 'degraded'` + error code + `last_success_at` | `connections reauth <id>` → update-mode enrollment URL. 🔴 **History and cursor are preserved** (AC-4.3), because it renews the login against the Item the connection already has. Re-running `enroll` is NOT the repair: a new Item re-issues every account and transaction id and doubles every total spanning them. The other connections were never affected (AC-4.1) |
| **Rate limited** | Error code on that connection | Backoff; the next scheduled run resumes. Never a crash |
| **Initial backfill not ready** | Not-yet-ready response; exit `75` | 🔴 Backoff-and-retry, not failure (AC-2.6). A multi-year backfill takes time to materialize |
| **Backfill only partly arrived** | Exit `75` with pages applied; `last_success_at` still null, so every answer carries `partial` | 🔴 Run again. `INITIAL_UPDATE_COMPLETE` is ~30 days of a 730-day grant and the rest follows minutes to hours later. Nothing is wrong and nothing is lost; the connection is simply not yet whole, and it says so on both surfaces rather than on neither |
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
| **Backup is not scheduled** (`#10`) | `store backup` exists and is verified, but nothing runs it. A backup command nobody invokes protects nothing. 🔴 The remedy this row used to name — wire it into the sync's launchd agent — was corrected on 2026-09-10: that agent does not exist (build step 8), launchd is a design choice rather than a requirement (AC-ARCH.2 asks for a daily job that recovers a missed window), and nothing requires a backup cadence to ride on the sync's at all. What should schedule it is an open requirement |
| **Restore is rehearsed against sandbox, not against production** (`#10`) | 🔴 **This row said "has never been rehearsed" and that stopped being true on 2026-09-10** — § Restore records the walk, including the keychain-loss drill FR-12 made possible. What is still owed is narrower: a restore into the **production** path, and a round trip through the operator's own storage rather than a file a drill wrote. Keeping the old wording here would have told an operator not to bother trying the thing that now works |
| **The key is exported by a command, and preserved by the operator** | 🔴 **This row said the key backup was "the half with no remedy" and that is now wrong**: `store key export`, `verify` and `import` shipped under FR-12 on the owner's ruling of 2026-09-10. The position it was defending survives and is narrower — the product still never chooses where the key goes, and refuses to write it inside the data directory — but the operator is no longer told to preserve a value they have no route to. What remains theirs is the storage medium, which is why `verify` exists |
| **AC-ARCH.1 is not an automated test** | "A fresh clone works on a clean machine" is asserted, not checked, and it decays silently with every undocumented step someone adds |
| **launchd agent not built** (step 8) | Including the missed-window recovery case, which must be verified by firing it for real rather than simulated |
| **No runbooks** (`#10` covers restore) | See above |
| **Log rotation unspecified** — 🔴 **untracked, and `#3` is why** | Low stakes (local files, trivial volume) but genuinely unbounded: `logging_setup.py` attaches a plain `FileHandler`, and no rotation handler exists anywhere in `src/` (checked 2026-09-07). `#3` names this gap and is **closed as COMPLETED**, so the one item that would track it reads as done. The row is the accurate half; the closed issue is not. Reopening it is the operator's call, not a builder's — it may have been closed as a deliberate "won't do" |
| **Raw-response retention undecided** (`#5`) | `docs/system-requirements.md` §9 open question 1; the recommendation is to keep indefinitely |
