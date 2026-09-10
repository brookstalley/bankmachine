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

🔴 **Where `investments` is actually enabled on the Item, the aggregator bills for it, per Item and
per month.** That is the price of having holdings discoverable later on the connections that can
carry it. If you do not want that bill, leave `investments` disabled in the dashboard: enrollment
asks for it optionally, so a dashboard that does not offer it does not fail the link.

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
until it is removed at the aggregator. `bankmachine connections retire <id>` is what removes it —
per its `--help`, it "removes it at the aggregator, so it stops counting against the plan cap and
stops billing", and keeps every row the connection produced. Retiring also marks that connection's
accounts inactive, so their balances stop reading as current.

---

## 2. Rehearse against sandbox first

These cost nothing and one of them stops being rehearsable the moment production is enrolled.

**2.1 Read the enrollment prompt as a human would, in sandbox.** Run `uv run bankmachine enroll`
against sandbox and read what it prints *before* the URL: the requested window, and the line saying
it cannot be raised later without removing and re-linking the connection (`_confirm_window` in
`src/bankmachine/cli/enroll.py`). If that reads as information rather than as a warning, you will
skim past it tomorrow, when it is the last correctable moment.
(`.prawduct/operator-verification.md`, VRF-003.)

**2.2 Read the sandbox datastore key out of the keychain.**

```sh
security find-generic-password -s bankmachine -a datastore:sandbox -w
```

**Nothing in this product prints the key.** `store init` logs only *that* a key was generated;
`cmd_init` in `src/bankmachine/cli/store.py` never receives its value, and `get_datastore_key` has
no printing caller. So the retrieval above is the whole recovery path, and it is worth having done
once on a key that does not matter. The service name is `BANKMACHINE_KEYCHAIN_SERVICE` (default
`bankmachine`); the account name is built by `Config.keychain_account`
(`src/bankmachine/config.py:97`) as `datastore:<environment>`.

*Failure looks like:* nothing returned, or an error naming the account — check the service and the
environment suffix rather than assuming the key is gone.

**2.3 Rehearse a backup and a restore against a scratch store.**

```sh
uv run bankmachine store backup /tmp/rehearse.db
BANKMACHINE_DATASTORE_PATH=/tmp/rehearse.db uv run bankmachine store status
```

`store backup` takes the exclusive writer lock, folds the WAL in, and reopens the copy with the same
key to check it (its `--help` says so). The restore direction — copy a backup into place and open it
— has never been walked by a human (`.prawduct/artifacts/operational-spec.md`, § Restore), and the
first production sync is what starts accumulating the one series no re-sync can rebuild.

*Failure looks like:* `store status` on the copy reporting `healthy: no`, or the backup refusing
because the destination already exists — it never overwrites.

**2.4 Decide `BANKMACHINE_HISTORY_DAYS` and then leave it alone.** The default is 730, which is the
aggregator's own inclusive maximum (`MAX_HISTORY_DAYS`, `src/bankmachine/config.py:42`), and a value
outside 1–730 is **refused rather than clamped** (`load_config`, `src/bankmachine/config.py:259`) —
a clamp would enroll a connection at a window nobody chose. Before enrolling, confirm no
`history_days` sits in `~/.config/bankmachine/config.toml` and no `BANKMACHINE_HISTORY_DAYS` is
exported in the shell you will run `enroll` from; the environment variable wins over the file.

🔴 The window is immutable per connection once the link is completed (AC-1.2). Getting it wrong
costs a re-link of every institution, so if `enroll` prints a number you did not expect, stop there.

---

## 3. The cutover

### 3.1 Point the shell at production and store the production secret

```sh
export BANKMACHINE_ENVIRONMENT=production
export BANKMACHINE_PLAID_CLIENT_ID=<your client id>   # the same id across environments
uv run bankmachine connector set-secret               # the PRODUCTION secret
```

*Verifies:* the secret lands under its own keychain account, `plaid:production`
(`Config.plaid_keychain_account`, `src/bankmachine/config.py:111`), which the sandbox secret cannot
overwrite. `set-secret` prompts without echoing at a terminal and reads stdin when piped.

*Failure looks like:* a refusal to store an empty secret; or the command appearing to succeed while
you were in a shell that never exported `BANKMACHINE_ENVIRONMENT=production`, in which case you have
just replaced the sandbox secret. Check the environment before, not after.

### 3.2 Create the production datastore

```sh
uv run bankmachine store init
```

*Verifies:* it prints `datastore created at …/store.db` — **unsuffixed**, which is production's
default while sandbox gets `store-sandbox.db` (`_default_datastore_name`,
`src/bankmachine/config.py:163`) — then the migrations applied and a status block ending
`healthy: yes`. A **new** datastore key is minted here, and only here: a key is never minted for a
store that already exists, because a fresh key would decrypt nothing.

*Failure looks like:* `datastore already present` when you expected a new store — you are pointed at
the wrong file, check `BANKMACHINE_DATASTORE_PATH`; or an error naming a missing keychain entry for
`datastore:production`, which means a store exists whose key is gone. The command refuses to mint a
replacement on purpose, because the exact diagnosis is worth more than a misleading one.

### 3.3 🔴 Back up the datastore key, now, before there is any data

```sh
security find-generic-password -s bankmachine -a datastore:production -w
```

Put those 64 hex characters in a password manager, and ideally on paper. Then verify you can read
them back from where you put them.

**Why here and not later.** The key cannot be recovered from the datastore, and a backup of the
datastore without it is a backup of noise (`.prawduct/artifacts/operational-spec.md`, § Backup &
Recovery). Every day you wait adds a day of `balances_daily` — the one series no aggregator
backfills — that a lost keychain destroys permanently. Right now the store is empty and the cost of
getting this wrong is zero, which is exactly why it is the moment to practise it.

`store init` prints this instruction when it mints a key, and `store status` repeats a one-line
reminder. Neither prints the key itself, and neither ever will.

### 3.4 Prove the credentials reach the real host

```sh
uv run bankmachine connector check
```

*Verifies:* configuration, keychain, network path and the raw archive in one call, against the
production host, with nothing enrolled. Expect a report naming the environment, the endpoint, the
received timestamp, the byte count and the `raw_response` id it was archived as
(`_print_check`, `src/bankmachine/cli/connector.py:141`).

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
shortly` and exiting **0**.

🔴 **Exit 0 with nothing applied is the expected first result on a real institution.** Re-run until
you see `N pages applied`.

*The number that matters:* `granted_history_days` is written **once**, and only when the aggregator
reports `HISTORICAL_UPDATE_COMPLETE` (`_record_granted_window`,
`src/bankmachine/cli/sync_run.py`) — deliberately, because measuring at
`INITIAL_UPDATE_COMPLETE` records a shortfall that does not exist while the backfill is still
arriving. In sandbox the two statuses arrive **seconds** apart — measured, three seconds, in
`.prawduct/artifacts/api-notes-plaid.md` § 17.1 — and on a real institution they are expected to be
**minutes to hours** apart, which is why the first run so often applies pages and still has no
window to report. If the grant comes up short you get a line naming the gap in days and saying it
cannot be widened without re-linking.

*Failure looks like:* a per-connection line carrying an error name — that connection is marked
degraded with its code and timestamp and the run exits 1, while the other connections still sync; or
`stopped at the page ceiling; run again to continue`, which is benign — the cursor resumes exactly
there and the run deliberately does not stamp a successful-sync time.

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
`external_spend_outflow_minor_units` is *external spend* — it excludes what the aggregator
categorized as transfers and what it categorized as debt service. Neither exclusion is a claim about
where money went: `internal_transfer` means the aggregator called it a transfer, not that a
counterparty leg was matched against another enrolled account, and `debt_service` includes mortgage,
auto and student-loan payments, which are real money out. `docs/connecting-an-mcp-client.md` carries
the field-by-field definitions.

**4.8 Point a second MCP server at production and confirm you can tell the two apart in your
client's list.** The environment rides every response and the server titles itself
`bankmachine (production)`, but a client may list the **registered key** rather than the title — so
name the entries distinctly yourself. *Failure looks like:* two entries you cannot distinguish.

---

## 5. The standing routine

**5.1 Run `sync run` and `store backup` daily, by hand.**

```sh
uv run bankmachine sync run
uv run bankmachine store backup ~/backups/bankmachine-$(date +%F).db
```

🔴 **Nothing schedules either of these.** The scheduler is build step 8 and is not built
(`docs/system-requirements.md` § 1). `sync run` is also what appends today's row to the daily
balance series — the one table no later re-sync can rebuild, because no aggregator backfills it. A
day you do not run it is a day of balance history gone for good. Until scheduling ships, a reminder
or a `cron`/`launchd` entry of your own is the mitigation.

🔴 **Never `cp` the datastore.** It runs in WAL mode, so copying `store.db` alone silently loses
whatever is still in `store.db-wal` — a measured `cp` of a source with a hot WAL lost every one of
300 rows. The copy `store backup` writes is ciphertext and is useless without the key from step 3.3.

**5.2 When a connection breaks.** `sync run` records the failure against that one connection and
keeps going; the run exits 1 and `connections list` shows the connection degraded.

- **`ITEM_LOGIN_REQUIRED`** — your login at that institution expired, or its MFA needs re-answering.
  **Re-run `uv run bankmachine enroll` and pick the same institution.** It converges on the existing
  connection instead of adding a second one: the connection id, its accounts and its transactions
  are kept, the degraded flag and error code are cleared, and the Item it replaced is removed at the
  aggregator so it stops billing. 🔴 If the re-link yields a **new** Item, the connection's cursor
  and its measured granted window are reset with it — both are Item-scoped and neither survives the
  credential they belonged to — so the next sync re-fetches the history from the start of the
  window. That is idempotent, but it is not instant. The dedicated repair command (update-mode
  re-auth, AC-4.3) is specified and **not built**.
- **Institution down, or rate limited** — nothing to do; run again later.
- **`ITEM_LOCKED`** — the institution locked the account. Fix it at the institution first.

**5.3 There are no webhooks; the pipeline is poll-only.** Nothing is pushed to this machine and
nothing listens. Consent expiry and an expired login therefore surface as a failure on the **next**
`sync run`, not in advance — which is another reason a day without a run is a day without a signal.

**5.4 Before any `git pull` that lands a migration, follow the upgrade order.** It is not the usual
one, because a build that does not recognize the datastore's schema version refuses to serve
(`.prawduct/artifacts/operational-spec.md`, § the upgrade order):

1. Disconnect the MCP client — that is enough; the server is a subprocess started at connect time.
2. **`store backup` first.** Backup opens through the ordinary writer factory and cannot back up a
   datastore at a version this build does not serve, so after the migration the old build can no
   longer produce one.
3. `git pull && uv sync`.
4. `uv run bankmachine store init` — the migration runner is idempotent and applies only what is
   pending.
5. `uv run bankmachine store status` — exit 0, and the schema version has moved.
6. Reconnect the client.

🔴 If any connection was not syncing at that moment, run `store rebuild` afterwards.

*Failure looks like:* every MCP tool answering `datastore_unservable` instead of an answer. That is
the guard working: a reader older than the store refuses rather than reporting zero.
