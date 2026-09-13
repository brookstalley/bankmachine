# Operator Verification

Pre-merge checks a human has to make with their own eyes, for changes whose
correctness a test cannot speak to. Entries are appended by the chunk that
creates them and drained with `prawduct-hook verify-operator-verification <ID>`.
## VRF-001 — Chunk 04 — `bankmachine sync shell` output and redaction

**Status:** verified

**Why a human:** the chunk's acceptance criterion is that an operator can open
the shell against a real datastore, run a query, and *read the result*. The
tests assert that rows come back, that writes are refused and that tokens are
redacted; none of them can say whether the output is legible to a person who is
already having a bad day, which is the only reason this command ships in build
step 1.

**Where to verify:** any real datastore.

```
bankmachine store init          # if you do not already have one
bankmachine sync shell
```

**Verify:**

1. The banner names the environment, the datastore path, the read-only role and
   the redaction — before the first prompt, not after.
   🔴 **Read the role line as a sentence, not just for its keywords.** This step
   FAILED on 2026-09-10 (#89): it read `writes are refused and no PRAGMA changes
   that`, complete as authored and still stranding the reader, because the
   demonstrative parses just as readily as a conjunction. Every keyword this step
   names was present, so a check scanning for them would have passed it. On the
   one line stating the guarantee an operator is trusting, a sentence that reads
   as broken IS the defect.
2. A join across `accounts` and `balances_daily` renders as aligned columns that
   line up, with `(N rows)` under them.
3. A balance in minor units comes back **whole**. Six figures must not be
   masked: `48750000` is $487,500.00, not `****0000`.
4. An account `mask` (four digits) survives; a long digit run does not.
5. A `credential_ref` or any 32-plus character opaque string reads `[REDACTED]`.
   `.schema`, by contrast, is **not** redacted — it is structure, not values, so
   `connections_one_live_per_institution` must appear in full. If you see
   `CREATE UNIQUE INDEX [REDACTED]`, the split described in `boundary-patterns.md`
   has regressed.
6. `PRAGMA query_only = OFF;` reports `0` — the flag really does flip — and the
   next `UPDATE` still fails with `attempt to write a readonly database`.
7. `BEGIN;` prints the rolled-back note, and the prompt comes back.
8. A bad column name prints one line and leaves the session open.
9. Ctrl-D leaves. Ctrl-C abandons a half-typed statement without leaving.

**Recorded session** (re-run 2026-09-06 after the review fixes, against a scratch datastore seeded
with two accounts; the datastore path is elided):

🔴 **This transcript is what that session printed, including the `role:` line step 1 later failed
on.** It was briefly rewritten to the post-fix wording on 2026-09-10 and has been put back: a dated
transcript is evidence of a run, and editing it to show output nobody saw makes it evidence of
nothing — on the very step that exists because only a human catches this class. The line now reads
`writes are refused, and no PRAGMA re-enables them` (#89). Re-date this block only by re-running it.

```
bankmachine sync shell -- environment SANDBOX
datastore: /tmp/.../opcheck/store.db
role:      read-only at the file; writes are refused and no PRAGMA changes that
output:    access tokens and account numbers redacted (AC-10.3)
type .help for the command list, .quit to leave
bankmachine> SELECT a.name, a.mask, a.balance_class, b.current_minor
         ...>   FROM accounts a JOIN balances_daily b USING (account_id)
         ...>  ORDER BY a.account_id;
name               mask  balance_class  current_minor
-----------------  ----  -------------  -------------
Everyday Checking  4021  asset          428317
Mortgage           8830  liability      48750000
(2 rows)
bankmachine> SELECT source_connection_id, credential_ref, granted_history_days FROM connections;
source_connection_id  credential_ref              granted_history_days
--------------------  --------------------------  --------------------
conn-abc              datastore:token:[REDACTED]  365
(1 row)
bankmachine> .schema connections
CREATE TABLE connections (
        connection_id          INTEGER PRIMARY KEY,
        institution_id         INTEGER NOT NULL REFERENCES institutions(institution_id),
        ...
        CHECK ((status = 'retired') = (retired_at IS NOT NULL)),
        CHECK ((status = 'degraded') <= (last_error_code IS NOT NULL))
    );
CREATE UNIQUE INDEX connections_one_live_per_institution
        ON connections (institution_id) WHERE retired_at IS NULL;
bankmachine> PRAGMA query_only = OFF;
(no result set)
bankmachine> PRAGMA query_only;
query_only
----------
0
(1 row)
bankmachine> UPDATE accounts SET name = 'tampered' WHERE account_id = 1;
error: attempt to write a readonly database
bankmachine> BEGIN;
(no result set)
note: that statement left a transaction open; it was rolled back so the prompt holds no snapshot. Nothing was lost -- this handle cannot write.
bankmachine> SELECT COUNT(*) AS accounts FROM accounts;
accounts
--------
2
(1 row)
bankmachine> SELECT nope FROM accounts;
error: no such column: nope
bankmachine> .quit
```

The same file read by stock `sqlite3` reports `file is not a database`, which is
the reason this command exists.

Items 1-8 were exercised in that session. Item 9 is the terminal-only half:
`input()` handles it, and a piped session cannot show it.

---


**Verified:** 2026-09-10
## VRF-002 — Chunk 01 (connector-v1) — `bankmachine connector check` against the real sandbox

**Status:** verified

**Why a human:** this is the first time the product reaches the outside world,
and the two things that matter about it are things a test cannot speak to. The
first is whether the failure text is any use — a wrong secret, a missing client
id and an unreachable network have to be distinguishable *from the message
alone*, because that is all the operator will have. The second is that the whole
path is real: the offline suite proves the bytes pass through unaltered against
a fake, and only a live call proves the SDK hands them over undecoded when a
real server is on the other end.

**Prerequisite:** sandbox credentials. From the aggregator's dashboard:

```
export BANKMACHINE_PLAID_CLIENT_ID=<client id>
bankmachine connector set-secret        # prompts, does not echo
```

**Where to verify:** a sandbox datastore (`BANKMACHINE_ENVIRONMENT=sandbox`,
which is the default and uses its own `store-sandbox.db`).

```
bankmachine store init
bankmachine connector check
```

**Verify:**

1. The report names the environment, the endpoint, the received timestamp, the
   byte count and the `raw_response` id it archived — aligned, one fact per line.
2. The institution count is the aggregator's own `total`, not the number of
   records returned. Asking for one institution out of thousands should say so.
3. **Nothing in the output is a credential.** Not the secret, not the client id.
4. `bankmachine sync shell`, then `SELECT endpoint, body_bytes, request_context
   FROM raw_responses;` — the row is there, `endpoint` is `/institutions/get`,
   and `request_context` records the parameters with nothing that authenticated
   the request.
5. Break it on purpose, three ways, and read each message before fixing it:
   an empty `BANKMACHINE_PLAID_CLIENT_ID`, a wrong secret, and (if you can)
   no network. Each should name its own cause. **A wrong secret reported as a
   network problem is a defect** — it sends the operator after the wrong cause.
6. With a datastore that does not exist, `connector check` must refuse *without*
   calling the aggregator. The message should say to run `store init`.
7. Record the fixtures, which is what closes the chunk's `verify-api` step:
   `BANKMACHINE_RECORD_FIXTURES=1 uv run pytest -m sandbox`, then confirm
   `tests/connector/fixtures/institutions_get.json` exists and contains **no
   real institution the operator banks with** — sandbox institutions only.

---

**Verified 2026-09-06, against the real sandbox.** Items 1-4, 6 and 7 pass as
written. Item 5 passes on two of its three cases — a wrong secret and an empty
client id. The no-network case was not exercised; the SDK's transport wrapping
was mapped from source instead (`api-notes-plaid.md` §3), which is what the
offline suite's unreachable-host test is written against.

```
aggregator:   sandbox
endpoint:     /institutions/get
received:     2026-09-06T23:16:41.731628+00:00
body:         677 bytes, archived as raw_response 1
institutions: 10085 matching the requested countries
```

Item 2 holds visibly: 10,085 is the aggregator's own `total`, against a page of
one. Item 3 holds — no credential appears anywhere in that output. Item 4, the
archived row, carries the parameters and nothing that authenticated the call:

```
bankmachine> SELECT endpoint, body_bytes, request_context FROM raw_responses;
endpoint           body_bytes  request_context
-----------------  ----------  ---------------------------------
/institutions/get  677         count=1 offset=0 country_codes=US
(1 row)
```

Item 5's two exercised cases each name their own cause, and item 6 refuses
before reaching the aggregator:

```
$ BANKMACHINE_PLAID_CLIENT_ID= bankmachine connector check
bankmachine: no aggregator client id is configured. Set `plaid_client_id` in the
config file or BANKMACHINE_PLAID_CLIENT_ID in the environment

$ BANKMACHINE_DATASTORE_PATH=/tmp/absent.db bankmachine connector check
bankmachine: datastore at /tmp/absent.db is not ready (datastore missing); run
`bankmachine store init` before reaching the aggregator
```

A third failure arrived unasked and is the most useful of them. The production
secret was set first, against the sandbox host, and the report was
`400 (INVALID_API_KEYS): invalid client_id or secret provided` with a
`request_id` — a *different* error code from the `INVALID_FIELD` a malformed
credential returns. That is item 5's real question answered from observation:
the operator can tell a wrong credential from a malformed one and from a
network fault, by the message alone.

🔴 **Item 7's premise was wrong, and the correction is worth more than the
item.** It asks the fixture to hold "sandbox institutions only". The sandbox's
`/institutions/get` does not serve fictional test institutions — it serves the
production institution catalogue: real names, real routing numbers, and a
`total` of 10,085 for `US` alone. Any page recorded from it is public catalogue
data.

The criterion underneath that wording still holds, and was established
mechanically rather than by reading: `tests/preferences/check-no-personal-data.sh`
reports clean over the working tree with the fixture in it, so nothing on this
operator's roster is in the file. What is recorded is one institution,
`ins_130958`, 677 bytes — the same size as the response `connector check`
archived, from its own separate call and carrying its own `request_id`.

The consequence past this entry belongs to Chunk 04, and is recorded in
`api-notes-plaid.md` §7: the plan's §4 risk that "sandbox shapes are not
production shapes" is narrower than written for *this* endpoint, because this
response is production's own catalogue. It stands as written for accounts and
transactions, which do come from the fictional institutions.

**Verified:** 2026-09-06

## VRF-003 — the hosted enrollment flow reads correctly to a human

**Status:** verified

**Chunk:** enrollment-v1 Chunk 02 · **Raised:** 2026-09-07 · **Verified:** 2026-09-10

**Why a human:** the offline suite fakes the aggregator, and a Hosted Link session
cannot be completed programmatically — `/sandbox/public_token/create` bypasses Link
entirely, so it mints a public token without creating a session `/link/token/get`
would ever report. What no test here can speak to is whether the printed page reads
unambiguously to someone about to make an irreversible choice.

**To verify**, with sandbox credentials in place:

    uv run bankmachine store init
    uv run bankmachine enroll

Then, at a terminal (not piped — the confirmation prompt is skipped when stdin is
not a tty):

1. 🔴 **The history-window line appears BEFORE the URL, and reads as a warning.**
   An operator who has already opened the browser has stopped reading the terminal,
   and this is the last moment AC-1.2 is reversible. Confirm the number shown is the
   window you intend.
2. Answering anything but `y` links nothing and exits 1.
3. The printed URL opens a working Link session. Use Plaid's sandbox credentials
   (`user_good` / `pass_good`) and pick any institution.
4. While the browser is open, the terminal says it is waiting — it does not look hung.
5. On completion the command reports the institution, the requested window, and
   **"granted history: not yet known"**. 🔴 Confirm that line cannot be misread as
   "we got what we asked for"; that misreading is what AC-1.3a exists to prevent.
6. Re-run `bankmachine enroll` against the same institution: it reports an *updated*
   connection rather than a new one, and `bankmachine connections list` shows one
   row for it rather than two. (This step also re-derives `capabilities` from the
   fresh `/item/get`, which is how a connection enrolled before 2026-09-08 sheds
   the single-list value recorded then.)

**Drain with:** `prawduct-hook verify-operator-verification VRF-003`


## VRF-004 — the MCP surface answers usefully in a real client

**Status:** accepted

**Chunk:** the MCP surface · **Raised:** 2026-09-08

**Why a human:** every tool is tested and driven end to end over stdio, but no test can say whether
an *agent* can use these answers to reason correctly. What is unverified is the judgement layer:
whether the tool descriptions steer a model away from the mistakes the data invites, and whether the
warnings are read rather than skipped.

**To verify**, with the sandbox datastore populated (`enroll`, then `sync run`):

1. Add the server to the client using the config in `docs/connecting-an-mcp-client.md`. Confirm the
   two environments are **distinguishable in the client's own server list**.

   🔴 *Do not write this step around the server's `title`.* It once read "confirm it appears as
   `bankmachine (sandbox)`"; measured 2026-09-09 in Claude Code, the client lists the **registered
   key** (`bankmachine-sandbox`) and never shows `serverInfo.title` at all. The title is still sent
   and still correct — but the thing an operator actually reads is the name THEY chose when adding
   it, which is why `connecting-an-mcp-client.md` says to name the two entries distinctly. Verify
   the property (they cannot be confused) rather than the mechanism (the title says so), because
   which mechanism reaches the operator is the client's choice and not this product's.
2. 🔴 **Ask about a period that falls outside the granted window**, which is the sharp form of this
   check. Read `history_starts` from `get_pipeline_health` first, then ask for spending in a month
   *before* it — *"what did I spend on food in August 2024?"* against a window starting
   2024-09-16. Confirm the answer says that period is **absent rather than zero**. A model that
   returns `$0`, or a total drawn only from the months it does have, is the failure this step
   exists to catch, and it is a finding about the tool descriptions rather than about the data.

   *Do not write this step around a fixed shortfall.* It once read "confirm the answer mentions the
   90-day history limit"; a live sandbox connection granted **722 of 730 days**, so there was no
   90-day limit to mention and the step could not be run as written. An 8-day shortfall is too
   small to probe whether warnings are read — asking outside the window works at any grant size.
3. Ask *"how far back does my data go?"*. Confirm it distinguishes *absent* from *zero*, and that
   the date it gives matches `history_starts` rather than the oldest transaction it happened to see.
4. Ask something the data cannot answer — *"what will I spend next month?"* — and confirm the
   projection is **labelled, based, and not mistakable for recorded data**: it says the figure is
   not something the datastore produces, states what it was derived from, and does not present it
   as a fact the store holds.

   🔴 **This step used to require a REFUSAL, and the owner ruled otherwise on 2026-09-09.** A flat
   no to "what will I spend next month" is unhelpful for a personal-finance tool, and the observed
   answer did the honest thing instead: it named the extrapolation as its own, gave the flow-by-flow
   basis, and — unprompted — noticed that 20 of 22 months matching to the cent meant the data was
   probably synthetic and said its confidence should not transfer to real accounts. The criterion is
   the labelling, not the refusal. **This is a recorded amendment, not drift**: the product was not
   changed, and nothing in the server instructions or `api-contract.md` mentions forecasting at all.
   If that silence ever becomes a problem, it is a requirements question and not a wording fix.

   *(Run 2026-09-09, PASS on the amended criterion. Also passed the harder thing nobody asked for:
   it split `flow_class` correctly, refusing to add debt service and internal transfers to spending
   — "the AUTOMATIC PAYMENT exactly equals that month's credit-card purchases; the transfers never
   left." That is the product's headline correctness feature landing unprompted.)*
5. Age the connection past `STALE_AFTER` (36 hours) and confirm a `stale` warning changes how the
   answer is phrased. `sync shell` is read-only, so this needs the writer — run it through the
   product's own factory rather than opening the file by hand:

   ```python
   # sandbox only; undo by running `bankmachine sync run`, which re-stamps the field
   from datetime import timedelta
   from bankmachine.config import load_config
   from bankmachine.query import STALE_AFTER
   from bankmachine.store import connection as C
   from bankmachine.store.types import now_utc, utc_instant

   config = load_config()
   if config.environment != "sandbox":
       raise SystemExit(f"refusing: environment is {config.environment!r}, not sandbox")
   aged = utc_instant(now_utc() - STALE_AFTER - timedelta(hours=12)).isoformat()
   with C.writer(config) as conn:
       conn.execute("UPDATE connections SET last_success_at = ?", (aged,))
       conn.commit()
   ```

   🔴 The environment guard is not decoration — this rewrites a real column, and the production
   datastore is the *unsuffixed* default. `get_pipeline_health` should then carry **both** a `stale`
   and a `gapped` warning; what is being verified is whether the client's prose changes, not whether
   the payload does. *(Validated 2026-09-08 against a copy of the sandbox datastore: "Tartan Bank has
   not synced successfully for 48 hours".)*

   *(Run 2026-09-09, PASS. The answer grew a Caveats section it had not had: "The connection is
   flagged stale … Treat the figure as a floor for the most recent days," with the data's edge named.
   That is the instruction for `stale` — quote the figure, say it may be out of date, name `as_of` —
   actually reaching the reader.)*

   🔴 **Observation from that run: the `stale` detail is not self-contained.** It says "has not
   synced successfully for 48 hours" and never names `last_success_at`, so a reader cannot check the
   claim against anything. The client noticed the elapsed figure disagreed with a `last_success_at`
   it had read EARLIER IN THE SAME SESSION and said so — which it could only do because it happened
   to hold the earlier value. A fresh session has nothing to reconcile against. Naming the timestamp
   in the detail would cost nothing; the value is already on the `get_pipeline_health` row.
6. 🔴 Point a second server at `production` with no datastore. Confirm it **starts**, appears in the
   client, and that `get_pipeline_health` explains the absence (AC-ARCH.3) rather than the tool
   silently not appearing.

**Drain with:** `prawduct-hook verify-operator-verification VRF-004`

---

<!-- VRF-005 and VRF-006 enqueued 2026-09-09 from `.prawduct/artifacts/discovery-production-data-semantics.md`. Both are BLOCKED until production
     data is connected -- they gate trusting a current-period figure, not connecting. Enqueuing
     them is not a commitment to perform them today. -->

**Accepted:** 2026-09-11 — rationale: Accepted to unblock the connections-reauth PR, which none of the three bears on. VRF-004 (the MCP surface in a real client), VRF-005 (a real pending transaction across settlement) and VRF-006 (the sign convention at two institutions) each need production data or a real MCP client, and none is made more or less true by this branch. This branch's own entry, VRF-007, was verified end to end on 2026-09-11 and needed no override. Each of the three is re-raised immediately as a fresh pending entry (VRF-008/009/010), so this acceptance discharges the block and not the obligation.

## VRF-005 — one real pending transaction watched across settlement

**Status:** accepted

**Chunk:** production-data semantics (#22) · **Raised:** 2026-09-09

**Why a human:** the sandbox has zero pending rows and has never had one — `pending` is 0 on all
388, `source_pending_transaction_id` is NULL on all 388. The deriver's pending→posted branch is
unit-tested against hand-built payloads, which proves what *this system* does with a stipulated
input and says nothing about what the aggregator actually sends. Only a real settlement can show
the delivery sequence.

**To verify**, once at least one production connection is syncing:

1. Make a card purchase you will recognise. Within a day, find it: it should appear with
   `pending: true`. Note the amount, the date, and the local `transaction_id`.
2. Ask a spending question covering that day and note the total.
3. After it posts (typically 1-3 days), re-query. Confirm **exactly one** row for that purchase —
   not two — that `pending` is now false, and that the local `transaction_id` is **unchanged**.
4. 🔴 **Confirm the amount is the settled one.** If the hold and the settlement differ (a tip, a
   fuel hold), the row must carry the settled figure. This is the case the existing tests do not
   cover — both use the same amount on both sides.
5. Re-ask the question from step 2 and confirm the total moved by exactly the difference, and that
   nothing else changed.
6. Paste the two observations below, and record the `raw_response` ids of the sync pages spanning
   the transition — those are what AC-13.9 promotes to a regression fixture.

**Drain with:** `prawduct-hook verify-operator-verification VRF-005`

**Accepted:** 2026-09-11 — rationale: Accepted to unblock the connections-reauth PR, which none of the three bears on. VRF-004 (the MCP surface in a real client), VRF-005 (a real pending transaction across settlement) and VRF-006 (the sign convention at two institutions) each need production data or a real MCP client, and none is made more or less true by this branch. This branch's own entry, VRF-007, was verified end to end on 2026-09-11 and needed no override. Each of the three is re-raised immediately as a fresh pending entry (VRF-008/009/010), so this acceptance discharges the block and not the obligation.

## VRF-006 — the sign convention on a real inflow, across two institutions

**Status:** accepted

**Chunk:** production-data semantics (#23) · **Raised:** 2026-09-09

**Why a human:** normalization is an unconditional negation, and it is already exercised in both
directions by the suite and by the sandbox (49 of 388 rows are stored positive). What no test can
speak to is whether a **real institution's** feed obeys the convention the negation assumes — and
the sandbox is single-connection, so a per-connection comparison is impossible there. This needs
ground truth about money you already know about.

**To verify**, with at least two production connections at **different** institutions:

1. Pick a paycheck you know the amount of. Confirm it reads **positive** and matches to the cent.
2. Pick a bill you know the amount of. Confirm it reads **negative** and matches to the cent.
3. Repeat both at the second institution. 🔴 **This is the step the sandbox cannot rehearse**, and
   it is the whole point of the entry: one feed obeying the convention is not evidence about
   another.
4. Run the per-connection check (AC-14.2) and paste its output. A connection whose
   never-plausibly-inflow categories come back predominantly positive has an inverted feed;
   the sandbox's measured baseline on one connection is 219 of 219 negative.
5. 🔴 Do **not** use a transaction's description text to judge its direction. The sandbox's payroll
   row reads "ACH Electronic Credit" and is categorised `TRANSFER_OUT` in both structured fields;
   that was adjudicated as faithful passthrough and is settled.
6. Record the `raw_response` id of the page carrying the observed deposit (AC-14.9).

**Drain with:** `prawduct-hook verify-operator-verification VRF-006`

**Accepted:** 2026-09-11 — rationale: Accepted to unblock the connections-reauth PR, which none of the three bears on. VRF-004 (the MCP surface in a real client), VRF-005 (a real pending transaction across settlement) and VRF-006 (the sign convention at two institutions) each need production data or a real MCP client, and none is made more or less true by this branch. This branch's own entry, VRF-007, was verified end to end on 2026-09-11 and needed no override. Each of the three is re-raised immediately as a fresh pending entry (VRF-008/009/010), so this acceptance discharges the block and not the obligation.

## VRF-007 — an update-mode repair, completed in a browser

**Status:** verified

**Chunk:** connections reauth (#67) · **Raised:** 2026-09-10 · **Verified:** 2026-09-11
· **Visual change:** yes

**Why a human:** a Hosted Link session cannot be completed programmatically — the whole reason
`test_enroll.py` fakes the aggregator. Every test of `connections reauth` fakes the item's recovery,
so what none of them can say is whether update mode *actually repairs the item in place* when a real
browser completes it. Two of the three things this command promises are only observable on the far
side of that session.

🔴 **This is also the only cheap read on the discovery's open question.** `persistent_account_id` is
NULL at every institution but three, so if update mode re-issues `account_id` the way a re-link does,
#67 reduces the frequency of the duplication without eliminating it and #91's identity fallback is
needed regardless. The sandbox gives a *signal*, not the answer — a sandbox institution is not
evidence about a real one — but a signal costs one session here and nothing at all to look at.

**Where to verify:** the sandbox store, against a connection that has synced at least once.

```
# 1. Record what must survive, BEFORE anything is broken.
bankmachine sync shell
  select connection_id, source_connection_id, granted_history_days, enrolled_at from connections;
  select connection_id, domain, cursor from sync_state;
  select account_id, source_account_id, name from accounts order by account_id;
  select count(*) from transactions;

# 2. Break the login the way an institution's password change does.
#    (`/sandbox/item/reset_login`, driven with the connection's access token.)

# 3. Confirm the product notices.
bankmachine sync run          # expect the connection degraded, ITEM_LOGIN_REQUIRED
bankmachine connections list

# 4. Repair it.
bankmachine connections reauth <id>
```

**Verify:**

1. The command prints the connection's current state and an `https://` URL **before** it starts
   waiting, and says how long the URL lives.
2. Completing the URL in a browser (sandbox credentials `user_good` / `pass_good`) returns the
   command within a poll or two, exit `0`.
3. 🔴 **`source_connection_id` is unchanged.** If the command refused instead, saying the item came
   back different, that refusal is the correct outcome and this entry has found the thing it was
   written to find — record it and stop: update mode does not preserve the item, and the plan's
   assumption is false.
4. The cursor from step 1 is **still there**, byte for byte. `granted_history_days` and
   `enrolled_at` are unchanged.
5. 🔴 **The `account_id`s from step 1 are the same rows** — not new ones beside them. Count the
   accounts: 14 becoming 28 is the duplication this command exists to prevent, arriving anyway.
6. `bankmachine sync run` continues from the cursor rather than re-fetching the window: it should
   finish quickly and the transaction count should not double. **Compare the count against step 1.**
7. Read the success message as a sentence. It claims the item, accounts, transactions and cursor are
   unchanged; steps 3-6 are that claim checked, and if any of them failed the message is a lie the
   operator would have believed.

**Also worth doing while credentials are exported** (it is not part of this entry, and it skips
without them):

```
uv run pytest -m sandbox -k "update_mode_session or expired_login_is_reported"
```

Those two live probes assert that Hosted Link is available in update mode on this account, and that
`/item/get` reports an expired login **in the body** rather than by raising — the shape the poll
loop is written against. Both ran green against the live aggregator on 2026-09-11 — `2 passed`,
neither skipped — so those two assumptions now hold on this account. They say nothing about steps
1-7 above: no test can complete a Hosted Link session, which is the whole reason this entry exists.

**Drain with:** `prawduct-hook verify-operator-verification VRF-007`

🔴 **VRF-008, VRF-009 and VRF-010 re-raise obligations that VRF-004, VRF-005 and VRF-006 were
accepted out of on 2026-09-11.** The acceptance discharged the block on one PR; it did not discharge
the checks, none of which has been done. **`accept-operator-verification` takes no id and flips
every pending entry**, so running the override again would erase these three exactly as it erased
the originals. If a future PR has to be unblocked that way, restore them by hand afterwards — or
verify them, which is the point.

## VRF-008 — the MCP surface answers usefully in a real client

**Status:** accepted

**Chunk:** the MCP surface · **Raised:** 2026-09-11

**Why a human:** unchanged from **VRF-004**, which carries the full procedure and is the entry to
follow. VRF-004 was accepted on 2026-09-11 to unblock the connections-reauth PR, which does not
touch the MCP surface — the block was discharged, the obligation was not, and this entry is where
the obligation now lives. Verify against VRF-004's steps and drain this id.

**Drain with:** `prawduct-hook verify-operator-verification VRF-008`

**Accepted:** 2026-09-11 — rationale: the owner's one ruling, which unblocked both PR #101 and
PR #104. VRF-009 (#22) and VRF-010 (#23) require production data the sandbox cannot produce: it
has never held a pending row and is single-connection, and neither branch enrolls anything or
syncs a second institution. VRF-008 runs against sandbox but could not be drained from either
session: the reachable sandbox MCP server is on build `296b7a0`, an ancestor of neither `develop`
nor either branch, and still reports the pre-fix doubled state (28 accounts, 784 transactions);
draining it needs the server relaunched on the merged build, which is #25. Production access is
approved and the cutover is next, which discharges all three against real data within days.
**The block is discharged here and the obligation is not** — it is re-raised as VRF-011, VRF-012
and VRF-013 below.

## VRF-009 — one real pending transaction watched across settlement

**Status:** accepted

**Chunk:** production-data semantics (#22) · **Raised:** 2026-09-11

**Why a human:** unchanged from **VRF-005**, which carries the full procedure and is the entry to
follow. VRF-005 was accepted on 2026-09-11 to unblock the connections-reauth PR, which produces no
pending rows — the sandbox has never had one, which is why the check needs production data at all.
Verify against VRF-005's steps and drain this id.

**Drain with:** `prawduct-hook verify-operator-verification VRF-009`

**Accepted:** 2026-09-11 — rationale: the owner's one ruling, which unblocked both PR #101 and
PR #104. VRF-009 (#22) and VRF-010 (#23) require production data the sandbox cannot produce: it
has never held a pending row and is single-connection, and neither branch enrolls anything or
syncs a second institution. VRF-008 runs against sandbox but could not be drained from either
session: the reachable sandbox MCP server is on build `296b7a0`, an ancestor of neither `develop`
nor either branch, and still reports the pre-fix doubled state (28 accounts, 784 transactions);
draining it needs the server relaunched on the merged build, which is #25. Production access is
approved and the cutover is next, which discharges all three against real data within days.
**The block is discharged here and the obligation is not** — it is re-raised as VRF-011, VRF-012
and VRF-013 below.

## VRF-010 — the sign convention on a real inflow, across two institutions

**Status:** accepted

**Chunk:** production-data semantics (#23) · **Raised:** 2026-09-11

**Why a human:** unchanged from **VRF-006**, which carries the full procedure and is the entry to
follow. VRF-006 was accepted on 2026-09-11 to unblock the connections-reauth PR, which enrolls
nothing and syncs no second institution. One feed obeying the convention is still not evidence
about another. Verify against VRF-006's steps and drain this id.

**Drain with:** `prawduct-hook verify-operator-verification VRF-010`

**Accepted:** 2026-09-11 — rationale: the owner's one ruling, which unblocked both PR #101 and
PR #104. VRF-009 (#22) and VRF-010 (#23) require production data the sandbox cannot produce: it
has never held a pending row and is single-connection, and neither branch enrolls anything or
syncs a second institution. VRF-008 runs against sandbox but could not be drained from either
session: the reachable sandbox MCP server is on build `296b7a0`, an ancestor of neither `develop`
nor either branch, and still reports the pre-fix doubled state (28 accounts, 784 transactions);
draining it needs the server relaunched on the merged build, which is #25. Production access is
approved and the cutover is next, which discharges all three against real data within days.
**The block is discharged here and the obligation is not** — it is re-raised as VRF-011, VRF-012
and VRF-013 below.


## VRF-011 — the MCP surface answers usefully in a real client

**Status:** accepted

**Chunk:** the MCP surface · **Raised:** 2026-09-11

**Why a human:** unchanged from **VRF-004**, which carries the full procedure and is the entry to
follow. This is the third raising of that obligation, and the record of why it keeps moving rather
than draining: VRF-004 was accepted for the connections-reauth PR, VRF-008 for PRs #101 and #104.

🔴 **Its blocker is mechanical, not a missing decision, and has its own item: #25.** The check runs
against sandbox, but only against a server on the build under test. Measured 2026-09-11, the
reachable sandbox MCP server was on `296b7a0` — an ancestor of neither `develop` nor either branch
— and still reported the pre-fix doubled state (28 accounts, 784 transactions). Relaunch the
server on the merged build first; until then this cannot be drained from inside a session.

**Drain with:** `prawduct-hook verify-operator-verification VRF-011`

**Accepted:** 2026-09-12 — rationale: Accepted to unblock the mypy-green-and-gated / ci-runs-the-gate PR, which none of the three bears on: this branch adds a CI workflow, a gate script and type annotations, and changes no derivation, no query and no MCP tool. VRF-011 (the MCP surface in a real client) is blocked mechanically on relaunching the sandbox server on the merged build (#25) and cannot be drained before this merge exists. VRF-012 (a real pending transaction across settlement, #22) and VRF-013 (the sign convention at two institutions, #23) both need production data the sandbox cannot produce. Each is re-raised immediately as a fresh pending entry, so this acceptance discharges the block and not the obligation.

## VRF-012 — one real pending transaction watched across settlement

**Status:** accepted

**Chunk:** production-data semantics (#22) · **Raised:** 2026-09-11

**Why a human:** unchanged from **VRF-005**, which carries the full procedure and is the entry to
follow. Accepted as VRF-005, VRF-009 and now again — every time for the same reason, which is that
the sandbox has never held a pending row and no fixture can express one. It needs the production
cutover and a card hold settling, which is days of calendar rather than work. The obligation is
**#22**, and it blocks production.

**Drain with:** `prawduct-hook verify-operator-verification VRF-012`

**Accepted:** 2026-09-12 — rationale: Accepted to unblock the mypy-green-and-gated / ci-runs-the-gate PR, which none of the three bears on: this branch adds a CI workflow, a gate script and type annotations, and changes no derivation, no query and no MCP tool. VRF-011 (the MCP surface in a real client) is blocked mechanically on relaunching the sandbox server on the merged build (#25) and cannot be drained before this merge exists. VRF-012 (a real pending transaction across settlement, #22) and VRF-013 (the sign convention at two institutions, #23) both need production data the sandbox cannot produce. Each is re-raised immediately as a fresh pending entry, so this acceptance discharges the block and not the obligation.

## VRF-013 — the sign convention on a real inflow, across two institutions

**Status:** accepted

**Chunk:** production-data semantics (#23) · **Raised:** 2026-09-11

**Why a human:** unchanged from **VRF-006**, which carries the full procedure and is the entry to
follow. Accepted as VRF-006, VRF-010 and now again. The sandbox is single-connection, so the
per-connection sign check that would catch an institution with a differing convention cannot run
at all — one feed obeying the convention is not evidence about another. Needs a second institution
enrolled and a real inflow, which is up to a pay cycle. The obligation is **#23**, and it blocks
production.

**Drain with:** `prawduct-hook verify-operator-verification VRF-013`

**Accepted:** 2026-09-12 — rationale: Accepted to unblock the mypy-green-and-gated / ci-runs-the-gate PR, which none of the three bears on: this branch adds a CI workflow, a gate script and type annotations, and changes no derivation, no query and no MCP tool. VRF-011 (the MCP surface in a real client) is blocked mechanically on relaunching the sandbox server on the merged build (#25) and cannot be drained before this merge exists. VRF-012 (a real pending transaction across settlement, #22) and VRF-013 (the sign convention at two institutions, #23) both need production data the sandbox cannot produce. Each is re-raised immediately as a fresh pending entry, so this acceptance discharges the block and not the obligation.

🔴 **VRF-014, VRF-015 and VRF-016 re-raise obligations that VRF-011, VRF-012 and VRF-013 were
accepted out of on 2026-09-12 — the third raising of the first, and the fourth of the other two.**
The acceptance discharged the block on the mypy/CI PR; it did not discharge the checks, none of
which has been done. **`accept-operator-verification` takes no id and flips every pending entry**,
so running the override again would erase these three exactly as it erased their predecessors. If a
future PR has to be unblocked that way, restore them by hand afterwards — or verify them, which is
the point.

🔴 **Two of the three are now the oldest debt in this file, and the count is the signal.** VRF-012
and VRF-013 have been raised four times each without ever being attempted, because neither can be:
both need the production cutover. That is not a process failure — it is the queue correctly
refusing to let a sandbox-only build claim a production check — but a fifth raising would mean the
cutover has slipped again, and at that point the right response is to say so out loud rather than
to re-raise a sixth time.

## VRF-014 — the MCP surface answers usefully in a real client

**Status:** pending

**Chunk:** the MCP surface · **Raised:** 2026-09-12

**Why a human:** unchanged from **VRF-004**, which carries the full procedure and is the entry to
follow. Accepted as VRF-004, VRF-008 and VRF-011.

🔴 **Its blocker is mechanical, not a missing decision, and has its own item: #25.** The check runs
against sandbox, but only against a server on the build under test — measured 2026-09-11, the
reachable sandbox MCP server was on `296b7a0`, an ancestor of no live branch, and still reported the
pre-fix doubled state.

🔴 **Measured 2026-09-12: the merge was never the blocker, and the real one is one action long.**
`get_pipeline_health` on the reachable server answers `build.commit: efd64ad` — the merge of #101,
an ancestor of both `develop` and this branch and neither one's tip — while the same call made
from this checkout answers `7985a3b`. The server's rows also carry no `domains` key, which the
build under test emits. The cause is process lifetime, not code: **an MCP server outlives `/clear`**,
so a session that begins by clearing is talking to whatever build the client launched, however many
days ago. Nothing a session can do reaches it — `build_id` is captured at import in the server's
own process.

**What unblocks it:** the operator quits and relaunches the client (not `/clear`), which starts the
server on the checkout's current HEAD. Confirm with `get_pipeline_health` — `build.commit` must
match `git rev-parse --short HEAD` — and then run VRF-004's steps in one pass. Verify the build
stamp FIRST every time: an answer from the wrong build is indistinguishable from a wrong answer.

**Drain with:** `prawduct-hook verify-operator-verification VRF-014`

## VRF-015 — one real pending transaction watched across settlement

**Status:** pending

**Chunk:** production-data semantics (#22) · **Raised:** 2026-09-12

**Why a human:** unchanged from **VRF-005**, which carries the full procedure and is the entry to
follow. Accepted as VRF-005, VRF-009, VRF-012 and now again — every time for the same reason, which
is that the sandbox has never held a pending row and no fixture can express one. It needs the
production cutover and a card hold settling, which is days of calendar rather than work. The
obligation is **#22**, and it blocks production.

**Drain with:** `prawduct-hook verify-operator-verification VRF-015`

## VRF-016 — the sign convention on a real inflow, across two institutions

**Status:** pending

**Chunk:** production-data semantics (#23) · **Raised:** 2026-09-12

**Why a human:** unchanged from **VRF-006**, which carries the full procedure and is the entry to
follow. Accepted as VRF-006, VRF-010, VRF-013 and now again. The sandbox is single-connection, so
the per-connection sign check that would catch an institution with a differing convention cannot run
at all — one feed obeying the convention is not evidence about another.

🔴 **Enrolling a second SANDBOX institution does not drain this, and the investment-sync plan is
about to do exactly that.** `build-plan-investment-sync.md` Chunk 01 enrolls `ins_109511` as a
second sandbox connection so the capability gate has a discriminating fixture; that is a different
check, against canned data, and it says nothing about whether a real institution signs its feed the
way this build assumes. This entry needs a second **real** institution and a real inflow, which is
up to a pay cycle. The obligation is **#23**, and it blocks production.

**Drain with:** `prawduct-hook verify-operator-verification VRF-016`

## VRF-017 — a real sandbox sync records positions, and the operator can read them

**Status:** verified

**Chunk:** investments — holdings end to end · **Raised:** 2026-09-12

**Why a human:** this is the chunk's Early Feedback Milestone and the one claim its tests
cannot make. The deriver is proven against the payload the aggregator actually sent, and the
capability gate is proven across two connections in-process — but nothing here has run
`bankmachine sync run` against an enrolled sandbox connection end to end, because enrolling
one goes through Hosted Link in a browser. What the operator is checking is that the whole
path works when the parts are joined by the real command, and that what lands is legible.

**Prerequisite:** a sandbox connection at an institution that serves investments. The
aggregator's `ins_109511` is one *(measured: `api-notes-plaid.md` §24)*; `ins_109508` also
answers, because its capabilities carry investments among `available_products` and the gate
reads the union.

```
export BANKMACHINE_PLAID_CLIENT_ID=<client id>   # or `source .env`
bankmachine enroll                               # complete the hosted link in a browser
bankmachine sync run --no-wait
```

**Verify:**

1. The run's report says `positions recorded` for the investments-capable connection, and
   does **not** say it for a connection that is not.
2. `bankmachine sync shell`, then:
   ```sql
   SELECT s.ticker, s.name, h.quantity, h.market_value_minor, h.currency, h.as_of_date
     FROM holdings h JOIN securities s USING (security_id);
   ```
   Rows come back. `quantity` is exact decimal text — a fractional position must not read as
   a rounded one. `as_of_date` is **today**, not the price's date.
3. `SELECT domain, last_success_at, last_error_code FROM sync_state;` shows a row at
   `domain = 'investments'` beside the transactions one, and the two advance independently.
4. Run `bankmachine sync run --no-wait` a second time the same day. The holdings rows are
   **unchanged** — same `captured_at`, same values (AC-2.4).
5. 🔴 Read the value of an investment account against its balance:
   ```sql
   SELECT a.name, b.current_minor, (SELECT SUM(market_value_minor) FROM holdings h
          WHERE h.account_id = a.account_id AND h.as_of_date = b.as_of_date)
     FROM accounts a JOIN balances_daily b USING (account_id)
    WHERE a.account_type = 'investment';
   ```
   They may well **disagree** — the aggregator's own sandbox is off by 6% on one account
   *(§23)*. That is expected and is exactly why nothing sums the two. What must be true is
   that both are present: the positions decompose the account, they do not replace it.

**Recorded session** (2026-09-12, against the enrolled sandbox connection `ins_109511`
"Tartan Bank", capabilities `["balance","investments","transactions"]`; no browser was needed
because the connection was already enrolled):

```
$ BANKMACHINE_ENVIRONMENT=sandbox bankmachine sync run --no-wait
  1  Tartan Bank: 1 page applied
       🔴 725 days of history granted against what was requested — a gap of 5 days. It cannot be widened without re-linking (AC-1.2)
       investments: positions recorded

bankmachine> SELECT s.ticker, s.name, h.quantity, h.market_value_minor, h.currency, h.as_of_date
         ...>   FROM holdings h JOIN securities s USING (security_id)
         ...>  ORDER BY h.market_value_minor;
ticker               name                                     quantity    market_value_minor  currency  as_of_date
-------------------  ---------------------------------------  ----------  ------------------  --------  ----------
NULL                 U S Dollar                               0.01        1                   USD       2026-09-13
ACHN                 Achillion Pharmaceuticals Inc.           1           211                 USD       2026-09-13
DBLTX                DoubleLine Total Return Bond Fund        2           2084                USD       2026-09-13
NFLX180201C00355000  Nflx Feb 01'18 $355 Call                 10000       11000               USD       2026-09-13
BTC                  Bitcoin                                  0.00293644  11557               USD       2026-09-13
EWZ                  iShares Inc MSCI Brazil                  5           21075               USD       2026-09-13
NULL                 Trp Equity Income                        21.5        43000               USD       2026-09-13
MIPTX                Matthews Pacific Tiger Fund Insti Class  23.567      63631               USD       2026-09-13
NULL                 United States Treas Bills 0.000% ...     10          94808               USD       2026-09-13
NHX105509            NH PORTFOLIO 1055 (... INDEX)            100.05      137369              USD       2026-09-13
CAMYX                Cambiar International Equity Insti       75.75       185588              USD       2026-09-13
SBSI                 Southside Bancshares Inc.                213         739749              USD       2026-09-13
NULL                 U S Dollar                               12345.67    1234567             USD       2026-09-13
(13 rows)
bankmachine> SELECT domain, last_success_at, history_start_date, last_error_code FROM sync_state;
domain        last_success_at                   history_start_date  last_error_code
------------  --------------------------------  ------------------  ---------------
transactions  2026-09-13T01:34:43.846431+00:00  2024-09-16          NULL
investments   2026-09-13T01:34:43.511556+00:00  2024-09-13          NULL
(2 rows)
```

*(Two security names are elided above. One is a sandbox string carrying an institution this
operator's roster names, which `check-no-personal-data.sh` refuses to let this repository hold,
and one was too wide for the column. Every number is as printed.)*

**Step 1 — one half observed live, the other closed in-process and NOT by omission.** The report
says `positions recorded` for the capable connection, on its own continuation line.

*(It was appended to the pages sentence when this entry first ran, and a review of the same wave
moved it out: `_pull_investments` runs before the page loop, so a connection the pager then reports
as degraded or as still materializing was told nothing about its positions at all. The transcript
above is a RE-RUN made after that move rather than the first run's output edited to match — the
command was executed again at 02:37:35 UTC, which is the `investments` stamp in this store's
`sync_state`, and the first run's 01:34 stamps are the ones the steps below still quote. The shape
of those two lines is now pinned by
`test_a_retired_investment_transaction_is_reported_whatever_branch_the_pager_takes`, so it cannot
move again without a test saying so.)* The negative half cannot be observed here:
the sandbox holds one enrolled connection and it is investments-capable, and the only non-browser
way to mint a second Item (`/sandbox/public_token/create`) bypasses `bankmachine enroll` — a
connection that never went through the product's own path is not evidence about the product's
report. Rather than accept the half, it was made a contract:
`test_a_connection_that_cannot_serve_investments_is_not_said_to_have_recorded_positions` runs both
connections in one report and asserts the phrase appears on the capable line and on no other. The
gate's own discrimination is `test_investments_are_pulled_for_the_connection_that_reports_them_and_no_other`.

**Step 2 — 🔴 this step FAILED on first reading, and the failure is the reason it exists.**
`quantity` came back `0.****3644` for the Bitcoin position and `-430.****3123` in the transaction
table: `holdings.quantity` is exact decimal TEXT, and the shell's account-number rule blanks any
run of eight digits, so the fractional part of every high-precision position was masked. Nothing
was wrong with the stored value — the operator simply could not read the number the column exists
to state. Fixed by sparing a cell that is wholly digits-point-digits, held by
`test_an_exact_decimal_quantity_reads_whole` and `test_a_bare_digit_run_is_still_masked_however_it_is_labelled`,
and recorded as the fourth boundary in `boundary-patterns.md`. The transcript above is the re-read
after the fix. A fractional position now reads exactly; `0.00293644` is not a rounded `0.003`.

**Step 3 — observed.** Two rows, advancing independently: the investments row's `last_success_at`
is 0.3s ahead of the transactions row's, which is the two domains being stamped by their own
passes rather than by one shared write.

**Step 4 — observed.** A second `sync run` the same day left `holdings.captured_at` at
`2026-09-13T01:34:40.446399+00:00` — the first run's instant — with all 13 rows and values
unchanged (AC-2.4).

**Step 5 — observed, and the entry's own SQL was wrong.** It named `a.type`; the column is
`accounts.account_type`, and the query is corrected above. Both numbers are present and they
disagree in exactly the way §23 predicts:

```
name        as_of_date  current_minor  SUM(market_value_minor)
Plaid IRA   2026-09-13  32076          32076
Plaid 401k  2026-09-13  2363198        2512564
```

The IRA agrees to the cent, the 401k is 6.3% apart. Both are recorded and neither is summed into
the other.

**`as_of_date` is the sync's own UTC day**, not the price's — stamped `2026-09-13` by a run made
at 19:34 local (UTC-6). `balances_daily` for the same run carries the same date, so the join in
step 5 lines up; the convention is the store's, not something investments introduced.

**Verified:** 2026-09-12

## VRF-018 — a real sandbox sync records investment transactions, and the window it got

**Status:** verified

**Chunk:** investments — investment transactions and the window · **Raised:** 2026-09-12

**Why a human:** the chunk's acceptance criterion is that *a sandbox sync writes investment
transactions for the capable connection*, and no test here makes that claim. The endpoint,
its pagination and its window were probed live *(`api-notes-plaid.md` §26)* and the deriver
is proven against the payload the aggregator actually sent — but the CLI path between them
is exercised against a fake client, and the one thing a fake cannot tell you is whether the
real `sync run` joins the parts. Drain alongside VRF-017: the same run answers both.

**Prerequisite:** the same sandbox connection VRF-017 needs, at an institution that serves
investments (`ins_109511`; `ins_109508` also answers, per §25).

```
export BANKMACHINE_PLAID_CLIENT_ID=<client id>   # or `source .env`
bankmachine sync run --no-wait
```

**Verify:**

1. `bankmachine sync shell`, then:
   ```sql
   SELECT investment_type, investment_subtype, trade_date, quantity,
          amount_minor, fees_minor, currency, settlement_date, removed_at
     FROM investment_transactions ORDER BY trade_date DESC LIMIT 20;
   ```
   Rows come back. 🔴 **Check the SIGNS against the type**: a `buy` stores a NEGATIVE
   `amount_minor` (cash left the account) and a `cash`/`contribution` stores a positive one.
   The aggregator sends both the other way round, so a column that agrees with the
   aggregator's sign is the bug this reading exists to catch. `quantity` is exact decimal
   text. `settlement_date` is **null on every row** — the feed has no such field (§26), and
   a populated one would mean something invented it.
2. The recorded window:
   ```sql
   SELECT domain, history_start_date, last_success_at FROM sync_state;
   ```
   The investments row carries a `history_start_date` at or after the oldest `trade_date`
   above, and it is **not null** — a null there after a completed run would mean the window
   was never seen whole.
3. Run `bankmachine sync run --no-wait` a second time. 🔴 **`removed_at` stays null on every
   row** (AC-2.4). A second identical window retiring rows is the reconciliation concluding
   removal from a window it did not actually exhaust, which is the failure mode that costs
   real history.
4. The run's report. A run that retired nothing says nothing about removals; if it does name
   a count, that count must be explainable by rows the aggregator genuinely stopped sending.

**Recorded session** (2026-09-12, the same run as VRF-017, re-read after that entry's redaction
fix):

```
bankmachine> SELECT investment_type, investment_subtype, trade_date, quantity,
         ...>        amount_minor, fees_minor, currency, settlement_date, removed_at
         ...>   FROM investment_transactions ORDER BY trade_date DESC LIMIT 12;
investment_type  investment_subtype  trade_date  quantity             amount_minor  fees_minor  currency  settlement_date  removed_at
---------------  ------------------  ----------  -------------------  ------------  ----------  --------  ---------------  ----------
buy              buy                 2026-09-11  0.520877874205698    -110          -799        USD       NULL             NULL
buy              buy                 2026-09-10  4211.152345617756    -4632         -500        USD       NULL             NULL
cash             contribution        2026-09-10  -1200                120000        0           USD       NULL             NULL
cash             contribution        2026-09-10  -1500                150000        0           USD       NULL             NULL
sell             sell                2026-09-09  -49.02909689729298   206658        0           USD       NULL             NULL
sell             sell                2026-09-09  -430.80867509953123  1496199       0           USD       NULL             NULL
buy              buy                 2026-09-08  33.99208384602773    -46671        -195        USD       NULL             NULL
cash             interest            2026-09-08  0                    10            0           USD       NULL             NULL
buy              buy                 2026-09-08  0.00293644           -12003        0           USD       NULL             NULL
fee              account fee         2026-09-06  3                    -300          0           USD       NULL             NULL
cash             dividend            2026-09-05  0                    872           0           USD       NULL             NULL
buy              buy                 2026-09-05  10                   -94808        0           USD       NULL             NULL
(12 rows)
```

**Step 1 — observed.** 1167 rows for the connection, across accounts 20 (219) and 21 (948).

- **The signs are the operator's, not the aggregator's.** Every `buy` carries a NEGATIVE
  `amount_minor` and every `cash`/`contribution` a positive one, which is the reverse of what the
  feed sends. `fees_minor` is negative alongside both a buy and a sell — a fee is money leaving
  whichever way the trade went.
- `settlement_date` is null on every one of the 1167 rows. The feed carries no such field, and a
  populated one would mean something invented it.
- `quantity` reads as exact decimal text — after the fix VRF-017 step 2 forced. Note that a `cash`
  contribution arrives with a NEGATIVE quantity beside a positive amount; that is the feed's own
  spelling, stored as sent, and no sign is flipped on a quantity.

**Step 2 — observed.** `history_start_date` for the investments domain is `2024-09-13`, not null,
and equal to the oldest `trade_date` in the table (`MIN(trade_date) = 2024-09-13`,
`MAX = 2026-09-11`) — at or after the oldest row, as the step requires.

**Step 3 — observed.** A second `sync run` left `COUNT(*) = 1167` and `COUNT(removed_at) = 0`. An
identical window retired nothing.

**Step 4 — observed.** The report named no removal count, which is what a run that retired nothing
should say.

**Verified:** 2026-09-12

## VRF-019 — a real sandbox store rebuilds byte-for-byte with investments in it

**Status:** verified

**Chunk:** investments — rebuild, idempotency, and the properties that hold across both ·
**Raised:** 2026-09-12

**Why a human:** AC-5.2's claim is about the *operator's own store*, and every test of it
here builds its archive from a fake client. What a fake cannot produce is the shape the real
endpoint returns — a window of over a thousand transactions across a dozen pages, securities
shared between holdings and transactions, and page instants a real run assigned. The rebuild
refuses rather than guesses when it cannot reproduce what it replaced, so the thing to
observe is that it does **not** refuse.

**Prerequisite:** the sandbox store VRF-017 and VRF-018 leave behind. Drain those first — this
verifies the store they produced.

```
export BANKMACHINE_PLAID_CLIENT_ID=<client id>   # or `source .env`
bankmachine store rebuild
```

**Verify:**

1. The command exits 0 and prints `content: identical to what it replaced`. 🔴 **Anything
   else is the finding**, including a success under `--accept-content-change` — which must
   not be passed here. A content change at an unchanged derivation version means either a
   deriver read the clock or the archive no longer holds every response those rows came from,
   and both are conditions to investigate before the old rows are gone.
2. `bankmachine sync shell`, then:
   ```sql
   SELECT COUNT(*), COUNT(removed_at) FROM investment_transactions;
   SELECT COUNT(*) FROM holdings;
   ```
   Both counts match what VRF-017 and VRF-018 recorded. 🔴 **The second column is the one
   that matters**: a rebuild that resurrects soft-deleted rows reports success and leaves
   every other number looking right.
3. Run `bankmachine sync run --no-wait` once more, then `bankmachine store rebuild` again.
   Both exit 0 and the second rebuild again reports identical content (AC-2.4).

**Recorded session** (2026-09-12, against the store VRF-017 and VRF-018 left behind):

```
$ BANKMACHINE_ENVIRONMENT=sandbox bankmachine store rebuild
raw responses replayed:  82
rows replaced:           2020
  balances_daily: 56
  holdings: 13
  investment_transactions: 1167
  transactions: 784
derivation version:      8
previous version(s):     8
content:                 identical to what it replaced
```

**Step 1 — observed.** Exit 0, `content: identical to what it replaced`, and
`--accept-content-change` was not passed. A window of 1167 transactions over three real pages,
securities shared between the holdings and the transactions, and page instants a real run assigned
all replayed to the same rows.

**Step 2 — observed, and one column of it is vacuous in this store.** After the rebuild:
`COUNT(*) = 1167`, `COUNT(removed_at) = 0`, `COUNT(*) FROM holdings = 13` — each matching what
VRF-017 and VRF-018 recorded. 🔴 **The soft-delete column is 0 both before and after, so this run
cannot distinguish a rebuild that preserves removals from one that resurrects them.** The sandbox
returns the same complete window on every call, so nothing was ever retired to preserve; provoking
a removal would mean making the aggregator stop sending a row, which no sandbox control does. That
direction is held in-process by
`test_a_rebuild_judges_each_window_against_the_rows_that_existed_when_it_closed`, and the reason a
store-level test cannot stand in for it is that a replay must not restate a domain's progress.

**Step 3 — observed.** A third `sync run --no-wait` (exit 0) followed by a second
`store rebuild` (exit 0) replayed 88 responses to the same 2020 rows and again reported
`content: identical to what it replaced`. `sync_state` came through both rebuilds unchanged —
the rebuild reproduces rows and does not restate when a domain last succeeded.

**Verified:** 2026-09-12
