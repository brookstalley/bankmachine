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

**Chunk:** enrollment-v1 Chunk 02 · **Raised:** 2026-09-07 · **Status:** pending

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

**Chunk:** the MCP surface · **Raised:** 2026-09-08 · **Status:** pending

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

## VRF-005 — one real pending transaction watched across settlement

**Chunk:** production-data semantics (#22) · **Raised:** 2026-09-09 · **Status:** pending

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

## VRF-006 — the sign convention on a real inflow, across two institutions

**Chunk:** production-data semantics (#23) · **Raised:** 2026-09-09 · **Status:** pending

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
