# The first production connection

This page is the ordered procedure for moving from the sandbox to real accounts, and the checks
that follow. Sandbox needs nothing but a free aggregator dashboard signup; production needs work in
the dashboard **before** any command here will succeed, and it starts a bill.

Read it once end to end before running anything. Two steps are irreversible in ways no later
command can undo: the history window a connection is enrolled with, and the datastore key.

Every claim below names the file that makes it true. Where a claim comes from a command's own
`--help`, that is said instead.

---

## 1. Before touching production: four things in the aggregator dashboard

**1.1 Production access approved.** It is granted per account, and until it is granted your
production API keys do not exist. Nothing in this repository can hurry that.

**1.2 `transactions` enabled. `investments` enabled only if you want it.** Enrollment asks for
`transactions` as the required product and offers `investments` as an optional one
(`ENROLLMENT_PRODUCTS` and `ENROLLMENT_OPTIONAL_PRODUCTS` in `src/bankmachine/cli/enroll.py`).

The distinction is what decides which institutions you can pick. The **required** set narrows the
institution picker to institutions supporting *every* product in it — a required `investments`
silently removes most card issuers and credit unions from the list, with no error to explain the
absence. The **optional** set adds the product at institutions that support it and narrows nothing.

🔴 **Where `investments` is enabled, a capable connection carries TWO monthly subscriptions at the
aggregator, per Item.** *Investments Holdings* starts when the institution is linked, because
enrollment asks for the product. *Investments Transactions* starts at that connection's first
`sync run`, because the sync calls `/investments/transactions/get`, and that call adds it. The
aggregator does not publish the rate, so check your contract for both before you enroll a
brokerage or a retirement account.

If you do not want either bill, leave `investments` disabled in the dashboard. Enrollment asks for
it optionally, so a dashboard that does not offer it does not fail the link. **Unverified:** what
the nightly sync does then. It decides whether to call by what the Item reports it can serve, not
by the dashboard. If the aggregator still reports investments and refuses the call, every run
records an investments error and exits `1`. Watch the first `sync run` for an
`investments:` error line (`api-notes-plaid.md` § Still to verify).

**1.3 Hosted Link enabled.** `enroll` always requests a hosted session
(`link_token_create` in `src/bankmachine/connector/plaid/client.py` sends
`LinkTokenCreateHostedLink`), and there is no local web server to fall back on. With Hosted Link
off, enrollment fails at `/link/token/create` with *"returned no hosted_link_url, so there is no URL
to open"* — the message names an account without Hosted Link enabled as the likely cause.

🔴 **No redirect URI is needed, and none is sent.** Hosted Link is precisely what removes the need
for a listener and a registered redirect URI, so do not go looking for a redirect setting to fill
in; if a bank's OAuth flow is involved, the hosted session handles the return itself. This is worth
knowing because searching for a redirect URI to register is the obvious wrong turn here.

**1.4 Billing configured, and you know the per-Item price.** Every live connection bills monthly
until it is removed at the aggregator, and a connection that holds investments carries the two
investments subscriptions in §1.2 on top of its transactions fee. `bankmachine connections retire <id>` is what removes it —
per its `--help`, it "removes it at the aggregator, so it stops counting against the plan cap and
stops billing", and keeps every row the connection produced. Retiring also marks that connection's
accounts inactive, so their balances stop reading as current.

---

## 2. Rehearse against sandbox first

These cost nothing and one of them stops being rehearsable the moment production is enrolled.

🔴 **First, declare that you are in sandbox.** Every step below writes — `enroll`, `store key
import`, `store backup`, `store init` — and anything that opens the datastore for writing or
changes a per-environment keychain entry refuses on an environment nobody chose, rather than
guessing. One line, once:

```sh
mkdir -p ~/.config/bankmachine
$EDITOR ~/.config/bankmachine/config.toml
```

so that the file contains exactly one `environment` line:

```toml
environment = "sandbox"
```

🔴 **One line, not an appended one.** TOML forbids a duplicate key, so a file carrying two
`environment` entries fails to parse and every command — reads included — exits 2 with an error
about TOML syntax rather than about the step that caused it. § 3.1 edits this same line rather than
adding a second.

`export BANKMACHINE_ENVIRONMENT=sandbox` does the same for one shell. § 3.1 switches this to
production when you cut over; until then everything here stays safely in sandbox.

*Failure looks like:* `no environment was chosen, so 'sandbox' was assumed`, and exit 2, with
nothing written. Reads (`store status`, `connections list`) work either way.

**2.1 Read the enrollment prompt as a human would, in sandbox.** Run `uv run bankmachine enroll`
against sandbox and read what it prints *before* the URL: the requested window, and the line saying
it cannot be raised later without removing and re-linking the connection (`_confirm_window` in
`src/bankmachine/cli/enroll.py`). If that reads as information rather than as a warning, you will
skim past it tomorrow, when it is the last correctable moment.
(`.prawduct/operator-verification.md`, VRF-003.)

**2.2 Read the sandbox datastore key out, and check it back.**

```sh
uv run bankmachine store key export
uv run bankmachine store key verify
```

`export` renders the key to your terminal and **refuses a redirected or piped stdout** — a key you
can capture by accident is a key that left the keychain by accident. If you would rather not retype
64 characters, `--to <path>` writes a file only you can read, at a path you name; delete it once it
is in your password manager.

🔴 **Then run `verify`, and do it here, on the key that does not matter.** It prompts for the key
and opens the datastore with it. This is the step that catches the failure nothing else can: a
transposed pair of hex digits is still 64 characters of valid hex and is simply a different key, so
a bad transcription looks exactly like a good one right up until the day the store will not open.

The service name is `BANKMACHINE_KEYCHAIN_SERVICE` (default `bankmachine`); the account name is
built by `Config.keychain_account` (in `src/bankmachine/config.py`) as `datastore:<environment>`.

*Failure looks like:* an error naming the account — check the service and the environment suffix
rather than assuming the key is gone. Or `DOES NOT MATCH` from `verify`, which means what you pasted
is not what this datastore takes; `export` again and compare.

**2.3 Rehearse a backup and a restore against a scratch store.**

```sh
uv run bankmachine store backup /tmp/rehearse.db
BANKMACHINE_DATASTORE_PATH=/tmp/rehearse.db uv run bankmachine store status
```

`store backup` takes the exclusive writer lock, folds the WAL in, and reopens the copy with the same
key to check it (its `--help` says so). The restore direction — copy a backup into place and open it
— was walked against sandbox on 2026-09-10 and works; the first production sync is what starts
accumulating the one series no re-sync can rebuild, which is why it is worth having walked before
then (`.prawduct/artifacts/operational-spec.md`, § Restore).

🔴 **Rehearse the half that actually fails in an incident, too — losing the keychain entry.** It
costs nothing here and there is no other time you will do it calmly:

```sh
uv run bankmachine store key export --to /tmp/rehearse.key
security delete-generic-password -s bankmachine -a datastore:sandbox
uv run bankmachine store status                 # unhealthy, and says exactly why
uv run bankmachine store key import             # paste from /tmp/rehearse.key
uv run bankmachine store status                 # healthy again
rm /tmp/rehearse.key
```

`store init` will refuse to mint a new key over the existing datastore, which is correct and is the
behaviour you want to have seen once before it happens for real.

🔴 **A freshly restored copy reads `journal mode: delete`, not `wal`, and that is expected.** SQLite's
backup API writes the destination in the default journal mode; the product's writer factory sets
`PRAGMA journal_mode = WAL` on every writer open (`src/bankmachine/store/connection.py`), so the copy
flips to `wal` the moment anything writes to it — `store init` or the first `sync run`. `store status`
opens read-only and therefore reports what it finds. Measured 2026-09-10: `delete` on the restored
copy, `wal` after one `store init`, `healthy: yes` throughout. Step 3.7 below says `store status`
should name `journal mode: wal`; that is the steady state, not what a restore shows before its first
writer, and reading `delete` during a recovery is not evidence the restore failed.

*Failure looks like:* `store status` on the copy reporting `healthy: no`, or the backup refusing
because the destination already exists — it never overwrites.

**2.4 Decide `BANKMACHINE_HISTORY_DAYS` and then leave it alone.** The default is 730, which is the
aggregator's own inclusive maximum (`MAX_HISTORY_DAYS` in `src/bankmachine/config.py`), and a value
outside 1–730 is **refused rather than clamped** (`load_config`, the `history_days` bounds check in `src/bankmachine/config.py`) —
a clamp would enroll a connection at a window nobody chose. Before enrolling, confirm no
`history_days` sits in `~/.config/bankmachine/config.toml` and no `BANKMACHINE_HISTORY_DAYS` is
exported in the shell you will run `enroll` from; the environment variable wins over the file.

🔴 The window is immutable per connection once the link is completed (AC-1.2). Getting it wrong
costs a re-link of every institution, so if `enroll` prints a number you did not expect, stop there.

---

## 3. The cutover

### 3.1 Switch the environment to production, and store the production secret

§ 2 put `environment = "sandbox"` in the config file. 🔴 **Edit that line — do not append a second
one.** TOML forbids a duplicate key, so a file carrying both `environment` entries fails to parse
and *every* command, reads included, exits 2 with an error about TOML syntax rather than about the
step that caused it.

```sh
$EDITOR ~/.config/bankmachine/config.toml
```

so that the file reads:

```toml
environment = "production"
plaid_client_id = "<your client id>"   # the same id across environments
```

Then store the secret:

```sh
uv run bankmachine connector set-secret   # the PRODUCTION secret
```

Either value can still be exported (`BANKMACHINE_ENVIRONMENT`, `BANKMACHINE_PLAID_CLIENT_ID`) and an
export wins over the file — useful for a one-off command against the other environment:

```sh
BANKMACHINE_ENVIRONMENT=sandbox uv run bankmachine store status
```

🔴 **`.env` is not read, deliberately** — a file that is silently read is a file whose contents are
silently trusted (`.prawduct/artifacts/operational-spec.md` § Configuration). `source .env` works because
you ran it; the config file above is the durable equivalent.

*Verifies:* the secret lands under its own keychain account, `plaid:production`
(`Config.plaid_keychain_account` in `src/bankmachine/config.py`), which the sandbox secret cannot
overwrite. `set-secret` prompts without echoing at a terminal and reads stdin when piped.

*Failure looks like:* a refusal to store an empty secret — or, if you skipped the config file and
exported nothing, a refusal to store anything at all:

```
bankmachine: no environment was chosen, so 'sandbox' was assumed -- and this command
writes state that belongs to one environment. Nothing was written.
```

🔴 **The product refuses here rather than assuming, and the refusal is the feature.** Every per-environment
container — `plaid:<env>` and `datastore:<env>` in the keychain, and the datastore filename — is
keyed on the environment, and a `set-secret` that defaulted would overwrite the *other*
environment's secret while reporting success. There is no undo for that, so a command that writes
per-environment state will not run until somebody has said which environment they mean. Reads
(`store status`, `connections list`, the MCP server) still default to sandbox.

### 3.2 Create the production datastore

```sh
uv run bankmachine store init
```

*Verifies:* it prints `datastore created at …/store.db` — **unsuffixed**, which is production's
default while sandbox gets `store-sandbox.db` (`_default_datastore_name` in `src/bankmachine/config.py`) — then the migrations applied and a status block ending
`healthy: yes`. A **new** datastore key is minted here, and only here: a key is never minted for a
store that already exists, because a fresh key would decrypt nothing.

*Failure looks like:* `datastore already present` when you expected a new store — you are pointed at
the wrong file, check `BANKMACHINE_DATASTORE_PATH`; or an error naming a missing keychain entry for
`datastore:production`, which means a store exists whose key is gone. The command refuses to mint a
replacement on purpose, because the exact diagnosis is worth more than a misleading one.

🔴 **Then check the mode the file actually landed with.** "Another local user is stopped by OS file
permissions" is a control this product claims, and the only thing enforcing it is a single
`os.umask(0o077)` at the CLI entry point (`run` in `src/bankmachine/cli/__init__.py`). umask governs
**creation only** — it never repairs a file that already exists — so the claim is true for a store
created by a fixed build and silently false for anything older. Confirm it rather than assume it:

```sh
ls -ld ~/.local/share/bankmachine ~/.local/share/bankmachine/store.db \
       ~/.local/state/bankmachine/logs ~/.local/state/bankmachine/logs/bankmachine.log
```

Expect `drwx------` on both directories and `-rw-------` on both files. The log is **plaintext** and
carries paths, institution ids and SQL text, so it matters as much as the encrypted store does.
Anything group- or world-readable, repair by hand — nothing in the product will do it for you:

```sh
chmod 700 ~/.local/share/bankmachine ~/.local/state/bankmachine ~/.local/state/bankmachine/logs
chmod 600 ~/.local/share/bankmachine/store.db ~/.local/state/bankmachine/logs/bankmachine.log
```

*(Measured 2026-09-10: the sandbox store and the log both predated the umask fix and were `0644`;
they were repaired by hand. A production store created by this build lands `0600` on its own — this
step is here to prove that rather than to trust it.)*

### 3.3 🔴 Back up the datastore key, now, before there is any data

```sh
uv run bankmachine store key export
```

Put those 64 hex characters in a password manager, and ideally on paper. Then read them back from
where you put them and confirm they are right — not by eye, by asking the datastore:

```sh
uv run bankmachine store key verify
```

🔴 **Do not skip the second command.** Reading your copy back proves you can read it; it does not
prove you wrote it down correctly, and a single wrong character in 64 is silent until the day it is
fatal. `verify` is the only thing that closes that gap, and it costs ten seconds here.

If a future machine's keychain has lost the key, `bankmachine store key import` puts it back — it
opens the datastore with what you paste before it writes anything, so a typo cannot overwrite a
working entry.

**Why here and not later.** The key cannot be recovered from the datastore, and a backup of the
datastore without it is a backup of noise (`.prawduct/artifacts/operational-spec.md`, § Backup &
Recovery). Every day you wait adds a day of `balances_daily` — the one series no aggregator
backfills — that a lost keychain destroys permanently. Right now the store is empty and the cost of
getting this wrong is zero, which is exactly why it is the moment to practise it.

`store init` prints this instruction when it mints a key, and `store status` repeats a one-line
reminder. Both name these commands, so the instruction and the way to follow it stay together.

### 3.4 Prove the credentials reach the real host

```sh
uv run bankmachine connector check
```

*Verifies:* configuration, keychain, network path and the raw archive in one call, against the
production host, with nothing enrolled. Expect a report naming the environment, the endpoint, the
received timestamp, the byte count and the `raw_response` id it was archived as
(`_print_check` in `src/bankmachine/cli/connector.py`).

*Failure looks like:* `400 (INVALID_API_KEYS): invalid client_id or secret provided` — the shape a
sandbox/production secret mix-up takes, and distinguishable from a malformed-field error and from a
network fault. It also refuses **before** calling out if the datastore is missing, so a failure here
is not automatically a credential problem.

### 3.5 Enroll the first institution — one, not all of them

```sh
uv run bankmachine enroll --timeout 1800
```

*Verifies:* the whole irreversible path.

Read the window line before answering `y`; the URL prints after it. The command then polls the
aggregator every 3 seconds until the session finishes or the timeout expires, and **the timeout and
the hosted URL's own lifetime are one number** (`--help`: "how long to wait for the hosted session
to be completed, and how long the URL stays usable"). The default is 900 seconds. A first
production link can involve an OAuth redirect to the bank, a password reset, an SMS code, an
in-app approval and sometimes a device registration; 15 minutes is not generous for that, so raise
it deliberately with `--timeout`. The aggregator's own accepted range for the hosted URL's lifetime
is not declared by its SDK (`LinkTokenCreateHostedLink.url_lifetime_seconds` carries no bound), so
if `/link/token/create` refuses the value, lower it rather than assuming the link is broken.

*Expect on success:* `linked <id>: <institution>`, `requested history: 730 days`, and

```
  granted history:   not yet known -- filled at the first sync (AC-1.3a)
```

🔴 **That line does not mean you got 730 days.** Reading it as a grant is the exact mistake AC-1.3a
exists to prevent: what was granted is not measurable until the aggregator finishes the backfill.

*Failure looks like:* a refusal because the connection cap is reached — raised **before** the link
token, so no browser trip is wasted; a timeout, which exits 1; or a *successful* enrollment that
still exits 1 carrying *"the connection this replaced could not be removed at the aggregator, so it
may still be billing"* (`cmd_enroll`, `src/bankmachine/cli/enroll.py`). Follow that last one up — it is
money leaving monthly for an Item you meant to drop.

*Why one institution:* `docs/system-requirements.md` § 8, build step 3 — verify the granted window
on ONE real connection before enrolling any others.

### 3.6 Sync, and keep syncing

```sh
uv run bankmachine sync run
```

*Verifies:* the backfill. Each run fetches the account roster first, then pages the transaction
changes since the connection's cursor. A backfill that has not materialized yet answers `NOT_READY`,
and the command waits across five attempts totalling 112 seconds (`NOT_READY_DELAYS`,
`src/bankmachine/cli/sync_run.py`) before printing `history is still being prepared; run again
shortly` and exiting **75**.

🔴 **Exit 75 is the expected first result on a real institution, and it means run it again.**
`75` is `EX_TEMPFAIL`: the run worked, nothing is wrong, and history is still owed. Let the command
do the repeating:

```sh
uv run bankmachine sync run --until-ready
```

It re-runs until no connection still owes history, waiting five minutes between attempts and giving
up after twelve (`--retry-delay`, `--max-attempts`). Reaching the cap still exits **75** — giving up
waiting is not the same event as finishing. Any other non-zero code ends the loop immediately and is
returned unchanged, because `1` is a connection that needs a person rather than another attempt.
Three states produce 75 — `NOT_READY` with nothing applied, `INITIAL_UPDATE_COMPLETE`
with the first pages applied and the rest still arriving, and a page run stopped at its ceiling.
They are one code on purpose: whichever it is, the action is the same.

🔴 **`N pages applied` is NOT the finish line.** The first successful pull on a real institution
typically reports `INITIAL_UPDATE_COMPLETE` — roughly the last thirty days of a 730-day grant — and
says so:

```
  1  Your Bank: 4 pages applied
       the history is still arriving. The granted window is not yet
       known, so it cannot be reported here, and the oldest
       transaction applied so far is not the oldest that exists.
       Run again shortly

1 of 1 connections still owe history; run again
```

Until you see exit **0**, that connection's `last_success_at` is deliberately left unstamped, so
`get_pipeline_health` and every MCP answer report it as `partial` rather than as current. The
terminal and the MCP surface say the same thing on purpose.

*The number that matters:* `granted_history_days` is written **once**, and only when the aggregator
reports `HISTORICAL_UPDATE_COMPLETE` (`_record_granted_window`,
`src/bankmachine/cli/sync_run.py`) — deliberately, because measuring at
`INITIAL_UPDATE_COMPLETE` records a shortfall that does not exist while the backfill is still
arriving. In sandbox the two statuses arrive **seconds** apart — measured, three seconds, in
`.prawduct/artifacts/api-notes-plaid.md` § 17.1 — and on a real institution they are expected to be
**minutes to hours** apart, which is why the first run so often applies pages, exits 75 and still has
no window to report. If the grant comes up short you get a line naming the gap in days and saying it
cannot be widened without re-linking.

*Failure looks like:* a per-connection line carrying an error name — that connection is marked
degraded with its code and timestamp and the run exits **1**, while the other connections still
sync. 🔴 `1` outranks `75`: a run that found both a stuck connection and an arriving backfill exits
`1`, because the stuck one needs a person and the arriving one only needs another run. The other
non-zero line you will see is `stopped at the page ceiling; run again to continue`, which is benign
and part of the same `75` — the cursor resumes exactly there and the run deliberately does not stamp
a successful-sync time.

### 3.7 Look at what you have

```sh
uv run bankmachine connections list
uv run bankmachine store status
```

*Verifies:* one row per institution, `active`, and a line reading `N of 10 connection slots in use`
(the cap is `BANKMACHINE_CONNECTION_CAP`, default 10); and that `store status` names `production`,
the unsuffixed path, `journal mode: wal`, a schema version this build serves, and `healthy: yes`.

*Failure looks like:* a connection still reading degraded, or a schema version this build does not
serve.

### 3.8 Enroll the rest, one at a time, repeating 3.5–3.7

*Verifies:* each institution's own granted window. They differ sharply between institutions, and a
short-history institution is invisible in any global figure — which is why the windows are measured
per connection and why the MCP surface reports coverage per connection rather than once.

---

## 4. Day one: the checks no test can perform

Everything above can be verified by the machine. Nothing below can. These are the checks that need
an operator who knows what the answer should be, and the sandbox cannot provide any of them.

**4.1 Reconcile every account balance against the institution's own app, on the same day.** This is
the one check nothing inside the system can do. It catches sync gaps, stale connections and zombie
accounts at once.

**4.2 Enumerate the accounts you know you have against `list_accounts`.** 🔴 **An account that was
never received is invisible.** There is no field that can report it and no query that can detect it,
and net worth is wrong by exactly that account's balance. The usual cause is de-selecting accounts
during the link flow. (A whole connection whose roster comes back empty is a different and much
louder failure: it raises a `roster_observed_empty` warning.)

**4.3 Confirm every enrolled account is USD.** Multi-currency behaviour is undefined: with two
currencies in one store, a bare minor-units total cannot be read at all. If any account is not USD,
stop asking spending questions until that is resolved.

**4.4 Check the sign of one known deposit and one known bill, at two different institutions.** The
convention is that value held is positive and value owed is negative, and the connector normalizes
to it — but a real inflow arriving the right way round has never been observed, because the
sandbox's only payroll row arrives pre-inverted (`.prawduct/artifacts/mcp-production-readiness.md`
§ Day one in production). One feed obeying the convention is not evidence about another, and the
sandbox is single-connection, so this cannot be rehearsed.
(`.prawduct/operator-verification.md`, VRF-006.)

*Failure looks like:* a deposit reading negative. A connection measured against a real inflow that
disagreed surfaces as `sign_convention_unverified`. Do not correct a sign by hand.

🔴 Do not judge direction from description text; the text is the counterparty's, not the ledger's.

**4.5 Run `sync run` a second time and look for duplicates.** The first pull is not where rows
double — the cursor boundary is. Idempotency is machine-tested offline; this is the live
confirmation. *Failure looks like:* a transaction you can see twice in `sync shell`, or a total that
moved while nothing happened.

**4.6 Watch one real pending transaction across settlement.** The pending→posted path has never seen
a real pending row; every sandbox row is already posted. After it settles, confirm exactly one row,
the same local transaction id, and the **settled** amount — a tip or a fuel hold makes the
authorized and settled amounts differ, and the offline tests use the same amount on both sides.
(`.prawduct/operator-verification.md`, VRF-005.)

*Failure looks like:* two rows, or a total that moved by more than the settlement difference.

**4.7 Ask three questions you already know the answer to, before asking any you do not.** Last
month's rent, the current card balance, what you actually paid the mortgage. Establish the oracle
deliberately: the sandbox was providing one for free and production will not.

🔴 When you ask what went out, read `totals` rather than summing rows, and read the whole-window
`outflow_minor_units` beside the three flow classes rather than any one of them alone.
`external_spend_outflow_minor_units` is *external spend* — money that left the household. Both
exclusions ARE claims about where money went: `internal_transfer` requires a matched counterparty
leg on another enrolled account, and `debt_service` requires the liability to be one this store
holds. So a mortgage to a lender you have not enrolled, cash from an ATM and rent by ACH all sit in
external spend, which is where they belong.

🔴 **The exclusions are only as good as what you have enrolled.** A transfer between two of your
accounts where only one is connected has no counterparty leg here, so it counts as spending — the
answer's `partial` warning tells you how many rows fell back that way. If that count is high on
your own data, the fix is to enrol the other side, not to discount the figure.
`docs/connecting-an-mcp-client.md` carries the field-by-field definitions.

**4.8 Point a second MCP server at production and confirm you can tell the two apart in your
client's list.** The environment rides every response and the server titles itself
`bankmachine (production)`, but a client may list the **registered key** rather than the title — so
name the entries distinctly yourself. *Failure looks like:* two entries you cannot distinguish.

---

## 5. The standing routine

**5.1 Run `sync run` and `store backup` daily, by hand.**

```sh
uv run bankmachine sync run --until-ready
uv run bankmachine store backup ~/backups/bankmachine-$(date +%F).db
```

🔴 **Nothing schedules either of these.** The scheduler is build step 8 and is not built
(`docs/system-requirements.md` § 1). `sync run` is also what appends today's row to the daily
balance series — the one table no later re-sync can rebuild, because no aggregator backfills it. A
day you do not run it is a day of balance history gone for good. Until scheduling ships, a reminder
or a `cron`/`launchd` entry of your own is the mitigation.

🔴 **If you write that entry yourself, read the exit code.** `0` is done, `75` is *run it again
soon* (a still-arriving backfill or a page ceiling), `1` is a connection that needs you, `2` is a
run that could not happen at all. A wrapper that treats every non-zero code alike turns `75` into a
false alarm and, worse, treats `1` and `2` as one thing — which is precisely the collapse
`.prawduct/artifacts/api-contract.md` § Direction refuses. Retry on `75` — or hand that to
`--until-ready` and alert on whatever it finally returns; alert on `1` and `2`.

🔴 **The daily entry needs the environment declared, and `store backup` is part of why.** A cron
or launchd entry inherits no login shell, so it must read `~/.config/bankmachine/config.toml` — an
entry relying on an export writes nothing and exits 2. `store backup` is guarded along with
`sync run`, deliberately: a backup silently taken against the wrong environment looks identical to
a good one and announces itself only at a restore, which is the one moment there is nothing left to
fall back on.

🔴 **Never `cp` the datastore.** It runs in WAL mode, so copying `store.db` alone silently loses
whatever is still in `store.db-wal` — a measured `cp` of a source with a hot WAL lost every one of
300 rows. The copy `store backup` writes is ciphertext and is useless without the key from step 3.3
— which is why that step ends in `store key verify` rather than in a transcription you hope is right.

**5.2 When a connection breaks.** `sync run` records the failure against that one connection and
keeps going; the run exits 1 and `connections list` shows the connection degraded.

- **`ITEM_LOGIN_REQUIRED`** — your login at that institution expired, or its MFA needs re-answering.
  **Run `uv run bankmachine connections reauth <id>`** — `connections list` gives you the id. It
  prints an update-mode hosted URL; complete it in the browser and the connection returns to
  `active`. It renews the login against the Item the connection already has, so the connection id,
  its accounts, its transactions, its cursor and its measured granted window all survive, and the
  next `sync run` continues from where the last one stopped rather than re-fetching the window.
  🔴 **Do not re-run `enroll` here.** That mints a **new** Item, and a new Item re-issues every
  account id and every transaction id: your store gains a second copy of the same accounts and the
  same transactions, and every total spanning them doubles with **no warning naming it**. `enroll`
  now refuses that case for you and points you back here; `--relink` is the deliberate override,
  and it exists only because AC-1.2 makes re-linking the one way to widen the history window.
- **Institution down, or rate limited** — nothing to do; run again later.
- **`ITEM_LOCKED`** — the institution locked the account. Fix it at the institution first.

**5.3 There are no webhooks; the pipeline is poll-only.** Nothing is pushed to this machine and
nothing listens. Consent expiry and an expired login therefore surface as a failure on the **next**
`sync run`, not in advance — which is another reason a day without a run is a day without a signal.

**5.4 Before any `git pull` that lands a migration, follow the upgrade order.** It is not the usual
one, because a build that does not recognize the datastore's schema version refuses to serve
(`.prawduct/artifacts/operational-spec.md`, § the upgrade order):

1. Disconnect the MCP client — that is enough; the server is a subprocess started at connect time.
2. **`store backup` first.** This is the tidy order rather than a required one: a copy taken at the
   version you are still running opens without being migrated first. 🔴 **If you have already
   pulled, take the backup anyway** — `store backup` copies a datastore at any schema version,
   including one this build cannot serve, and names the version the copy carries. Never reach for
   `cp` here; it drops whatever is still in the WAL.
3. `git pull && uv sync`.
4. `uv run bankmachine store init` — the migration runner is idempotent and applies only what is
   pending.
5. `uv run bankmachine store status` — exit 0, and the schema version has moved. Read the
   `derivation:` line too: it lists every derivation version your stored rows were produced by,
   beside the one this build derives.
6. `uv run bankmachine store rebuild`, then `store status` again. The `derivation:` line should now
   name one version. 🔴 Run the rebuild after every upgrade, not only when a connection is stuck:
   several migrations add a column a sync fills only for rows it adds, and a derivation change with
   no migration at all leaves every stored row as the older logic produced it. Until the rebuild
   runs, every MCP answer carries a `derivation_version_mismatch` warning saying so. If the line
   names a version NEWER than this build's, you have pulled an older checkout; upgrade instead of
   rebuilding.
7. Reconnect the client.

*Failure looks like:* every MCP tool answering `datastore_unservable` instead of an answer. That is
the guard working: a reader older than the store refuses rather than reporting zero.
