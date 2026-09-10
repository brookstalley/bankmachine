# Read-only review — Plaid connector, sync pipeline, datastore robustness

Scope: what breaks tomorrow against **real production Plaid** that sandbox never
exhibited. Every code claim below was read in the tree at `develop` (4b78550) and,
where marked, executed as a probe. Claims about **Plaid's own behaviour** carry an
explicit confidence tag; anything I could not verify from the installed SDK or the
repo's own measured notes is marked so rather than asserted.

Probes run (no network): `uv run python -c` against `classify()`, `to_minor()`,
`_local_account()`, `balance_class_of()`, and the installed `plaid-python` 44.0.0
request models. One test file run: `uv run pytest tests/cli/test_sync_run.py -q` →
25 passed.

---

## BLOCKER 1 — `investments` is requested at Link time, which filters the production institution picker

**`src/bankmachine/cli/enroll.py:165`**

```python
ENROLLMENT_PRODUCTS: tuple[str, ...] = ("transactions", "investments")
```

passed straight through to `link_token_create(products=list(ENROLLMENT_PRODUCTS))`
(`enroll.py:391-403`) → `LinkTokenCreateRequest(products=[Products(p) for p in products])`
(`client.py:584`).

**Plaid behaviour, HIGH confidence:** `products` in `/link/token/create` is the
*required* product set, and Link only offers institutions that support **every**
product in it. `optional_products` / `additional_consented_products` exist precisely
so you can ask for a product without narrowing the picker. In Sandbox this is
invisible: `ins_109508` (Plaid's own test bank, which every fixture in
`tests/` uses) supports every product, so the filter removes nothing.

**Failure scenario:** tomorrow the operator opens the hosted URL, searches for their
credit union / regional bank / card issuer, and it is **not in the list**. There is no
error, no log line, nothing in the product says why. They conclude Plaid does not
support their bank. Many US card issuers and most credit unions do not support
`investments`.

Second-order: `investments` is a separately billed product per Item per month, and
**nothing in this product ever calls an investments endpoint** — `grep -rn
"investments" src/bankmachine/connector/ src/bankmachine/cli/` returns only the
`ENROLLMENT_PRODUCTS` line and comments. `capabilities_of()` records the capability
and `sync run` never acts on it. So the narrowing buys a bill and no data.

**Fix:** move `investments` out of `products` and into `optional_products` (SDK 44.0.0
has `LinkTokenCreateRequest.optional_products`; verify field name against
`plaid/model/link_token_create_request.py` before shipping). Keep `("transactions",)`
as the required set. `capabilities_of()`'s union read already handles the resulting
`available_products` correctly — the docstring at `client.py:258-282` argues exactly
this and is undermined by the constant above it.

**Test that pins it:** in `tests/cli/test_enroll.py`, assert the captured
`LinkTokenCreateRequest` has `products == [Products("transactions")]` and that
`investments` appears only in `optional_products` — i.e. a test that fails if anyone
puts a non-universal product back into the required list.

---

## BLOCKER 2 — a re-enrollment reuses the connection row but never resets its cursor, wedging the new Item

**`src/bankmachine/cli/enroll.py:654-699`** — `_record_connection` finds the existing
live row *by institution* and UPDATEs it in place:

```python
existing = conn.execute(
    select(...).where(
        connections.c.institution_id == institution_id,
        connections.c.retired_at.is_(None),
    )
).one_or_none()
...
conn.execute(
    update(connections)
    .where(connections.c.connection_id == connection_id)
    .values(source_connection_id=source_connection_id,   # NEW item
            credential_ref=credential_ref, ...)
)
```

`grep -rn sync_state src/` (verified) shows **nothing anywhere resets
`sync_state.cursor`** on re-enrollment: the only writers are
`derivers.py:329/339` (advance) and `sync_run.py:376` (`history_start_date`).
`store rebuild` explicitly does not touch it either — `store/rebuild.py:19`
lists "sync cursors" among what a rebuild never deletes.

So after a re-link the row keeps `connection_id`, keeps its old cursor, and points at
a **brand-new Item**. The next `sync run` reads that cursor at `sync_run.py:414-428`
and sends it with the new access token.

**Plaid behaviour, MEDIUM-HIGH confidence:** a `/transactions/sync` cursor is scoped
to the Item that issued it. Presenting Item A's cursor with Item B's access token is
rejected (I expect `INVALID_FIELD` under `error_type: INVALID_REQUEST`). It is *not*
silently treated as an initial sync.

**Why this is the blocker and not an edge case:** re-enrollment is the **only** remedy
this product offers for `ITEM_LOGIN_REQUIRED`, which is the single most common
production event. There is no Link **update mode** anywhere — `link_token_create`
never accepts an `access_token` (`client.py:550-623`), so "re-link" always mints a new
Item rather than repairing the existing one. Probed classification:

```
INVALID_ACCESS_TOKEN -> AggregatorRequestError  retryable=False
```

so the wedge is terminal: the connection is marked `degraded` every run, forever, and
no CLI command can clear the cursor. Recovery requires hand-editing SQLCipher.

**Fix (two parts):**
1. In `_record_connection`, when `previous_source_id != source_connection_id`,
   `DELETE FROM sync_state WHERE connection_id = ? AND domain = 'transactions'`
   inside the same transaction that rewrites the row, and null
   `granted_history_days` (see MEDIUM 12).
2. Add an update-mode path: `bankmachine connections reauth <id>` that calls
   `link_token_create(access_token=<existing>)`, so the ordinary remedy for
   `ITEM_LOGIN_REQUIRED` keeps the Item, the cursor, the account ids and the billing.

**Test:** `tests/cli/test_enroll.py` — enrol institution X, write a cursor into
`sync_state`, re-enrol X against a *different* `item_id`, assert the cursor row is
gone and `granted_history_days IS NULL`. Today that test fails.

---

## BLOCKER 3 — `TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION` is unclassified, so it degrades the connection instead of restarting the page run

**`src/bankmachine/connector/plaid/errors.py:59-97`** — neither `CODE_TO_ERROR` nor
`TYPE_TO_ERROR` knows the code or its `TRANSACTIONS_ERROR` type. Probed:

```
TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION -> UnrecognizedAggregatorError retryable=False
```

`UnrecognizedAggregatorError.retryable = False` (`connector/__init__.py:313`), so
`call_with_retry` re-raises immediately and `sync_run.py:290` degrades the connection
for the rest of the run.

**Plaid behaviour, HIGH confidence:** Plaid raises this when the underlying data
changes mid-pagination and documents the remedy as *restart pagination from the last
cursor you successfully stored*. It is expected, not exceptional, on long initial
backfills — which is exactly what every connection does tomorrow and never did in
sandbox (sandbox backfills are tiny and static).

**The irony:** this loop is already built to do the right thing. `_cursor_for` re-reads
the cursor from the datastore on **every** page (`sync_run.py:414-428`), so simply
retrying the loop *is* "restart from the last saved cursor". The only missing piece is
the classification.

**Fix:** add a `TransactionsPaginationRestartError(AggregatorError)` with
`retryable = True`, map `"TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"` to it in
`CODE_TO_ERROR`, and — because a retry inside `call_with_retry` re-sends the *same*
call rather than re-reading the cursor — catch it in `_sync_one`'s page loop and
`continue` (the next iteration re-reads the cursor). Also add `"TRANSACTIONS_ERROR"`
is **not** safe for `TYPE_TO_ERROR` (it spans several remedies), so keep this at the
code layer only, consistent with the `ITEM_ERROR` reasoning at `errors.py:20-25`.

**Test:** `tests/cli/test_sync_run.py` — a fake client that returns page 1, then raises
this code, then returns page 2 from the cursor page 1 stored. Assert the run completes,
the connection is **not** degraded, and page 2's rows are present. Nothing in
`tests/` mentions this code today (`grep -rn MUTATION tests/ src/` → no hits).

---

## BLOCKER 4 — a transaction naming an account that `/accounts/get` no longer lists permanently wedges the connection, and duplicates raw rows on every retry

**`src/bankmachine/connector/plaid/derivers.py:496-512`**

```python
source_account_id = entry.get("account_id")
if not isinstance(source_account_id, str) or source_account_id not in known:
    raise DerivationError(... "which this connection has no row for" ...)
```

`known` is built solely from the local `accounts` table (`_account_ids`, line 433),
which is populated solely from `/accounts/get` (`sync_run.py:244-245`). **The
`accounts` array that rides every `/transactions/sync` response is ignored** — the
repo's own notes flag this: `api-notes-plaid.md:416-418` says "`accounts` also rides
the sync response… Whether to use it or keep the endpoints separate is a build step 4
decision, not settled here." It was never settled; the code took the narrower path.

`_mark_removed` (line 619) calls `_local_account` too, so a `removed` entry for a
vanished account raises as well — contradicting its own docstring ("A removal naming a
row this system does not have is not an error").

Probed:
```
txn for unknown account          -> DerivationError
removed txn for unknown account  -> DerivationError
```

**Failure scenario (production only):** an account is closed, or the user de-selects it
in Account Select, or the institution stops sharing it. It drops out of
`/accounts/get`. `/transactions/sync` still emits `modified`/`removed` deltas for its
historical rows. Then, every run:

1. `apply_response` commits the **raw row** (`store/derivation.py:214-222`).
2. The derive transaction rolls back (`:223-225`).
3. The cursor never advances (`derivers.py:319` writes rows before the cursor, and the
   whole thing is one transaction).
4. `_sync_one` degrades the connection and returns.
5. Next run re-fetches the same cursor, gets the same page, **archives it again**.

So: the connection is permanently degraded, its data stops at that page forever, and
`raw_responses` grows one duplicate row per run. Worse, the archive itself becomes
**unrebuildable** — `store rebuild` replays every archived response and will hit the
same `DerivationError`, so the one recovery tool in the product also fails.

**Fix:**
1. Derive the account roster from `/transactions/sync`'s own `accounts` array *before*
   applying its change lists, in the same transaction. That is the aggregator telling
   you which accounts these transactions belong to, in the same body.
2. Failing that (or in addition), make `_mark_removed` tolerant of an unknown account,
   matching its docstring — a soft delete of a row you do not have is genuinely a no-op.
3. Consider a per-page derive failure that still lets *other* connections proceed
   (it does today) **and** does not re-archive an identical body — see MEDIUM 8.

**Test:** archive an `/accounts/get` listing account A only, then a `/transactions/sync`
page whose `added` names account B and whose `accounts` array lists both. Assert B's
row is created and the cursor advances. Then a `removed` entry for a third account C
present in neither: assert it is a no-op and the cursor still advances.

---

## HIGH 5 — a null `balances.current` (or a null currency) aborts the entire connection before a single transaction is fetched

**`src/bankmachine/connector/plaid/derivers.py:949`**

```python
current = to_minor(balances.get("current"), currency, "a current balance", response)
```

`to_minor` (line 148) accepts `int` and `str` only; `None` raises. Probed:

```
balances.current = null -> DerivationError: ... gives a current balance as a NoneType
```

Same shape at `derivers.py:794-802`: if both `iso_currency_code` and
`unofficial_currency_code` are null, the account is refused.

**Plaid behaviour, MEDIUM-HIGH confidence:** `AccountBalance.current` is documented
nullable, and `iso_currency_code` is nullable (with `unofficial_currency_code` as the
alternate, itself nullable for some non-standard accounts). Sandbox's canned accounts
always populate both, so this never fires there.

**Failure scenario:** `sync_run.py:244-245` fetches and persists `/accounts/get`
**first, every run**, before any pagination. One account anywhere on the Item with a
null `current` raises `DerivationError` → `_sync_one` degrades the connection and
returns → **no transactions are fetched at all** for that institution, ever, and the
raw `/accounts/get` body is re-archived every run. Same permanent wedge as BLOCKER 4,
reached one step earlier.

**Fix:** make `current_minor` nullable in `balances_daily` (it is `NOT NULL` today —
check `core_schema.py`; if so this needs migration 005) and record a null balance as a
null, not a refusal. A balance the institution did not report is *absent*, which is
exactly the distinction this product's whole warning vocabulary exists to preserve —
refusing the page instead loses the transactions too. If nullability is not acceptable,
skip the `balances_daily` row for that account and log it, but **still upsert the
account row** so its transactions can be derived.

**Test:** an `/accounts/get` body with two accounts, one with `"current": null`.
Assert the other account's balance is written, the null-balance account still gets an
`accounts` row, roster observation still advances, and a following
`/transactions/sync` page for *either* account derives.

---

## HIGH 6 — `options.include_original_description` is never requested, and the archive cannot be re-fetched

**`src/bankmachine/connector/plaid/client.py:764`**

```python
request = TransactionsSyncRequest(access_token=access_token, count=count)
```

No `options` at all. Probed the installed SDK:

```
TransactionsSyncRequestOptions attrs: ['account_id', 'days_requested',
  'include_logo_and_counterparty_beta', 'include_original_description',
  'include_personal_finance_category', 'personal_finance_category_version']
```

`include_original_description` defaults to false, so `original_description` — the raw
bank memo, which is frequently the only thing that identifies a transfer or an ACH —
is **absent from the response bytes**. This module's entire premise
(`store/raw.py:1-8`: "a re-fetch is often impossible, because an aggregator's history
window is not a thing you get back") means the field is lost *permanently* for every
row pulled tomorrow. Turning it on later only affects rows fetched later.

**Plaid behaviour, HIGH confidence:** the option is opt-in and costs nothing.

**Fix:** pass `options=TransactionsSyncRequestOptions(include_original_description=True)`.
Do it **before** the first production sync. Consider `include_personal_finance_category=True`
in the same change: `_category()` (`derivers.py:422`) silently yields `None` when the
field is absent, so a production Item that does not return PFC by default would give
you silently-uncategorized rows with no warning.

Do **not** set `options.days_requested` here — the link-time value is the right source
and `MAX_HISTORY_DAYS=730` is already read off the SDK's own declared maximum
(`config.py:28-42`). That part is sound.

**Test:** assert the captured `TransactionsSyncRequest` carries
`options.include_original_description is True` — a test on the request, not the
response, because the response is the thing you cannot get back.

---

## HIGH 7 — a datastore failure during one connection aborts the whole run (AC-4.1 hole)

**`src/bankmachine/cli/sync_run.py:290`**

```python
except (ConnectorError, DerivationError) as exc:
    return _degrade(config, outcome, type(exc).__name__, str(exc))
```

`_persist` (line 400) opens a fresh `writer_connection` per page, which takes the
`flock` via `_exclusive_lock` with `LOCK_NB` (`store/connection.py:363`) and raises
`AnotherWriterRunningError` if it cannot. That is a `StoreError`, **not** a
`DerivationError`, so it escapes `_sync_one`, escapes `cmd_sync_run`, and is caught by
`cli/__init__.py:99` → the whole run stops and every remaining connection is skipped.
The same is true for `RawResponseCorruptError` and for a bare
`sqlite3.OperationalError` (disk full, `database is locked` after the 5 s busy timeout).

Because `_persist`/`_cursor_for` take and release the lock **per page**, the window for
this is not narrow: `store backup` (which takes the same exclusive lock,
`backup.py:142`) run concurrently with a sync will do it.

**Fix:** widen the catch to `(ConnectorError, StoreError)` — `DerivationError` is
already a `StoreError` subclass — or explicitly add `AnotherWriterRunningError` and
`DriverError`. AC-4.1 says one broken connection never aborts another; a locked
datastore is currently the counterexample.

**Test:** in `tests/cli/test_sync_run.py`, patch `_persist` to raise
`AnotherWriterRunningError` for connection 1 only; assert connection 2 still syncs and
the run exits 1 rather than 2.

---

## MEDIUM 8 — every failed derivation re-archives an identical raw body, growing the archive without bound

`store/derivation.py:214-225` commits the raw row in its own transaction, then derives
in a second. That split is deliberate and correct (the response may be unfetchable).
But `sync_run.py` has no idempotency at the fetch level: a page whose derivation fails
is re-fetched and re-archived on **every** subsequent run. `store/raw.py:22-25`
explicitly makes the table append-only and dedup-free ("deduplicating it would destroy
the evidence that the source repeated itself"), which is right for genuine repeats and
wrong for this loop.

Combined with BLOCKER 4 or HIGH 5 and a nightly schedule, the archive grows one
duplicate `/accounts/get` or `/transactions/sync` blob per night, forever, and
`store rebuild` replays all of them.

**Fix:** cheapest correct move is to fix the two wedges above so the loop cannot spin.
Additionally: when `_degrade` fires on a `DerivationError`, log the
`raw_response_id` and the endpoint so an operator can find the offending body with
`sync shell`. Today the message names an account id that the log formatter redacts
(`logging_setup.py:49` blanks 32+ char runs; Plaid account ids are ~37 chars) — the
un-redacted copy only reaches stdout via `_report`.

**Test:** run the same failing page twice; assert exactly one `raw_responses` row, or
(if the append-only rule wins) assert the run does not re-fetch a cursor whose last
derivation failed.

---

## MEDIUM 9 — `INITIAL_UPDATE_COMPLETE` is reported as a finished sync, with no prompt to run again

`sync_run.py:270-276` treats every status that is not `NOT_READY` as a page to apply,
then `sync_run.py:300` stamps `last_success_at` because the run did not hit the page
ceiling.

**Plaid behaviour, HIGH confidence:** a production Item reaches
`INITIAL_UPDATE_COMPLETE` with roughly the last 30 days of transactions within a
minute or two, and `HISTORICAL_UPDATE_COMPLETE` — the full requested window —
**minutes to hours later**. In Sandbox both statuses arrive seconds apart
(`api-notes-plaid.md:437-440` measured exactly that: attempt 1 `NOT_READY`, attempt 2
`INITIAL_UPDATE_COMPLETE` three seconds later).

What the operator sees tomorrow: `2 pages applied`, exit 0, done. Two years of history
are not there and nothing on the terminal says so.

**Partly mitigated, and this is genuinely good:** `query.py:149-161` emits a `partial`
warning whenever `granted_history_days IS NULL` and `last_success_at IS NOT NULL`, so
the MCP surface *does* say "granted history window is not yet known". The gap is in the
CLI and in the run's exit code.

**Fix:** carry `outcome.historical_complete` into `_report` and print, for a connection
that finished a run without it, something like *"initial window only; the full history
is still being prepared — run `bankmachine sync run` again in an hour"*. Consider
making that state contribute `EXIT_UNHEALTHY` (see MEDIUM 10).

**Test:** a run whose last page reports `INITIAL_UPDATE_COMPLETE` with
`has_more: false` — assert the printed report says history is incomplete and names the
re-run.

---

## MEDIUM 10 — `still_materializing` and `stopped_short` both exit 0

`sync_run.py:99-109`:

```python
if any(o.degraded for o in self.outcomes):
    return EXIT_UNHEALTHY
return EXIT_OK
```

A run where every connection came back `NOT_READY` after 112 s of backoff
(`NOT_READY_DELAYS` sums to 112) applies **zero** transactions and exits `0`. So does a
run that stopped at the 500-page ceiling with more to fetch. `exit_codes.py:1-12` calls
the 1/2 split a scheduling contract; `1` is "ran and found a problem", which is what
both of these are.

**Fix:** `return EXIT_UNHEALTHY if any(o.degraded or o.stopped_short or
o.still_materializing or not o.historical_complete for o in self.outcomes)` — or at
minimum the first three.

**Test:** assert exit code 1 for a run whose only connection returned `NOT_READY`
throughout, and for a run that hit the page ceiling.

---

## MEDIUM 11 — an unexpected exception exits `1`, which the contract reserves for "ran and found a problem"

`cli/__init__.py:111-117` logs and **re-raises** anything that is not
`Store/Secrets/Connector/EnrollmentError`. `__main__.py` runs `sys.exit(main())`, so an
escaping exception gives the interpreter's default exit status of **1**. A scheduler
reading the contract in `exit_codes.py` will read a crash as a degraded feed.

**Fix:** keep the traceback (it is valuable) but exit `EXIT_ERROR`: log, print the
traceback to stderr explicitly, and `return EXIT_ERROR` rather than `raise`. Or wrap in
`__main__.main()`.

**Test:** invoke `run()` with a handler that raises `RuntimeError`; assert the process
exit status is 2 and the traceback is in the log file.

---

## MEDIUM 12 — a re-enrollment orphans the old accounts and never re-measures the granted window

Two consequences of the same in-place row update at `enroll.py:667-699`:

1. A new Item issues **new `account_id`s**. `_upsert_account` matches on
   `(connection_id, source_account_id)` (`derivers.py:841-850`), so every account is
   inserted afresh. The old rows keep their last `balances_daily` capture and stop
   being listed. Whether they then contribute to a net-worth total depends on
   `query._account_lifecycle`'s `no_longer_reported` handling — I did not trace that
   fully, but a doubled net worth for the length of the transition is the risk worth
   checking by hand. Plaid's `persistent_account_id` (already stored as
   `source_persistent_account_id`, `derivers.py:856`) is the intended re-link identity
   and is currently written but never matched on.
2. `_record_granted_window` returns early whenever `granted_history_days` is already
   set (`sync_run.py:344-353`, and the reasoning there is correct for a *stable* Item).
   After a re-link the recorded window belongs to the **retired** Item. The `gapped`
   warning then describes a connection that no longer exists.

**Fix:** null `granted_history_days` and `history_start_date` in the same transaction
that repoints the row (same place as BLOCKER 2's cursor reset); and match accounts on
`persistent_account_id` when the Item changed.

**Test:** re-enrol against a new item id, assert `granted_history_days IS NULL`, and
assert an account carrying the same `persistent_account_id` reuses its local row.

---

## MEDIUM 13 — two SQLCipher key derivations and one flock per page

`sync_run.py:248` (`_cursor_for` → `reader_connection`) and `:273` (`_persist` →
`writer_connection`) each open a **fresh** SQLCipher handle per page.
`store/connection.py:279` issues `PRAGMA key` on every one, and `_writer` also
re-issues `PRAGMA journal_mode = WAL` and takes/releases the advisory flock.

SQLCipher's default KDF is 256 000 PBKDF2 iterations; at ~0.1–0.3 s per open that is
0.2–0.6 s of pure key derivation per page. A 730-day initial backfill at
`TRANSACTIONS_PAGE_SIZE = 100` is easily 100+ pages per institution. It will work; it
will just be slow and will look like a hang, and it widens the window for HIGH 7.

**Fix:** hold one writer connection for the duration of `_sync_one` and pass it into
`_persist`/`_cursor_for`. The per-page *transaction* boundary is what AC-2.5 needs;
the per-page *connection* is not. This is a behaviour-preserving refactor.

**Test:** count `connection.writer` invocations across a 5-page run and assert it is 1,
while the existing crash-resume tests in `tests/connector/test_sync_cursor.py` stay green.

---

## MEDIUM 14 — a 15-minute hosted-link lifetime is tight for a production bank login

`client.py:94` `DEFAULT_HOSTED_URL_LIFETIME_SECONDS = 900`, and `enroll.py:402` makes
the URL lifetime and the poll deadline **one number** (which is the right design —
`enroll.py:396-401` explains why). But in production the operator may face an OAuth
redirect to the bank's own site, a password reset, an SMS code, an app-based approval,
and for some institutions a device-registration step. Fifteen minutes is not generous
for a first-time link at, say, a large retail bank.

**Fix:** no code change needed — just run `bankmachine enroll --timeout 1800` tomorrow,
and consider raising the default. Verify 1800 is within Plaid's accepted range for
`hosted_link.url_lifetime_seconds` (I am not confident of the documented bounds).

**Related, LOW confidence and worth checking before tomorrow rather than after:** the
link token sets no `redirect_uri` and no `webhook` (`client.py:579-593`). My
understanding is that **Hosted Link handles the OAuth return itself** and needs no
redirect URI registered by the client, but I would not stake the enrollment on my
recollection — confirm against Plaid's Hosted Link + OAuth documentation, since the
first production institution you try may well be an OAuth one.

---

## MEDIUM 15 — a page carrying data but no `next_cursor` silently drops its rows

**`src/bankmachine/connector/plaid/derivers.py:315-319`**

```python
next_cursor = payload.get("next_cursor")
if not isinstance(next_cursor, str) or not next_cursor:
    return                      # <- returns BEFORE applying added/modified/removed

_apply_transaction_changes(conn, response, context)
```

The guard is right for `NOT_READY` (measured empty cursor, `api-notes-plaid.md:17.1`)
but the *ordering* means any response with change lists and a missing/empty
`next_cursor` has its rows discarded with no error and no log line. If such a page also
carries `has_more: true`, `sync_run.py`'s loop re-fetches the identical page 500 times
and then reports `stopped_short` — a bounded spin that reads as "run again to
continue" but never will.

I have **no evidence Plaid ever sends that shape** with data, so this is defence in
depth rather than an observed failure. It is cheap.

**Fix:** apply the change lists first; skip only the cursor write when the cursor is
absent. Log at WARNING if a page carried entries but no cursor.

**Test:** a body with one `added` entry and `"next_cursor": ""` — assert the row is
written, the cursor is unchanged, and a warning is logged.

---

## LOW 16 — `PENDING_EXPIRATION` and consent expiry are invisible

Probed: `PENDING_EXPIRATION -> UnrecognizedAggregatorError`. There are no webhooks
(`link_token_create` sets none), so the only channel for consent state is
`/item/get`'s `item.consent_expiration_time` and `item.error` — both archived
verbatim, neither read. `derive_item` (`derivers.py:251`) reads only
`institution_id`/`institution_name`.

For a US-only roster (`ENROLLMENT_COUNTRIES = ("US",)`) this is mostly theoretical
today. Polling is a legitimate design choice and should simply be **said out loud** in
the README: there is no webhook receiver, the pipeline is poll-only, and a connection
that has expired will surface as `ITEM_LOGIN_REQUIRED` on the next run rather than in
advance.

**Fix:** read `item.error.error_code` and `item.consent_expiration_time` in
`derive_item` and surface them on `pipeline_health`. Map `PENDING_EXPIRATION` to a
non-retryable "re-auth soon" type.

---

## LOW 17 — the log file is never rotated and no command prints where it is

`logging_setup.py:113` uses a plain `logging.FileHandler` on
`<log_dir>/bankmachine.log` — no rotation, no size cap. `store status`
(`cli/store.py:149-162`) prints environment, path, journal mode, schema version and
health, but **not** `config.log_dir`. The path is documented in
`operational-spec.md:85` and nowhere the operator will be looking at 9pm.

**Fix:** `RotatingFileHandler(maxBytes=..., backupCount=...)`, and add
`log directory: <path>` to `_print_status`.

---

## LOW 18 — `plaid-python` has no upper bound

`pyproject.toml:13` — `"plaid-python>=44.0.0"`. `uv.lock` pins 44.0.0 so a plain
`uv sync` is safe, but a `uv lock --upgrade` before tomorrow could pull a major that
moves the models this client reaches into. Not a defect; just do not upgrade tonight.

---

## LOW 19 — nothing takes a backup before a migration

`store/migrations/__init__.py:67-90` applies pending migrations with no backup step;
`cli/store.py:87-107` calls it directly. This is correctly documented as a *manual*
step (`operational-spec.md:170`, and the reasoning there — that `store backup` cannot
open a store at an unserved version, so the backup must precede the migration — is
right). For a fresh production store tomorrow there is nothing to lose. Worth
automating before the first migration lands on a store with real balance history,
since `balances_daily` is the one series nothing can rebuild.

---

# What I verified as SOUND

These were checked against the code and hold up. Several are unusually well done.

**Cursor semantics and crash resume**
- The cursor is written **by the deriver, from the same body as the rows, inside one
  transaction** (`derivers.py:282-351`). Rows go in before the cursor, and the
  transaction makes the ordering a real guarantee. This is exactly right.
- `_cursor_for` re-reads from the datastore every page rather than carrying it in
  memory (`sync_run.py:414-428`), so the datastore — the thing that survives a kill —
  decides where the next page starts.
- A `NOT_READY` reply's empty `next_cursor` never overwrites a good one
  (`derivers.py:316`), measured and documented.
- The status is consulted **before** `has_more` (`sync_run.py:255`), which defuses the
  measured `NOT_READY`/`has_more: false` trap. `tests/connector/test_sync_cursor.py`
  pins crash-mid-derivation, failed-row-write, and rebuild-replays-to-the-same-cursor.
- `TRANSACTIONS_SYNC` is correctly declared `retry_safe` and `/item/public_token/exchange`
  and `/item/remove` correctly are not (`connector/__init__.py:337-352, 386-416`), with
  the far-end argument stated separately from the local-side one.
- `count=100` is well inside the SDK's declared `inclusive_maximum: 500` (probed).
- `days_requested` is nested correctly under `transactions` at link time, required with
  no default, bounds-checked against the SDK's own 1–730 (`client.py:573-585`,
  `config.py:28-42`).

**Archive integrity**
- Verbatim bytes via `_preload_content=False`, digest over plaintext, recomputed on
  read, reproducible gzip (`store/raw.py`). A corrupt body is refused rather than
  derived from.
- Credential-bearing endpoints **cannot be archived by construction** —
  `FetchedResponse.__post_init__` refuses to exist for them (`connector/__init__.py:548-569`).
  This is a relationship, not a skip-list, and it is the best thing in the codebase.
- Access tokens live in the OS keychain, environment-scoped and per-connection
  (`config.py:121-131`, `secrets.py:178-213`); the datastore holds only a
  `credential_ref`.
- Errors are described with `error_code` + `error_message` + `request_id` and never the
  token (`errors.py:194-218`); the log formatter redacts by shape, not by provider name.

**Error handling**
- Two-layer classification (code, then type, then HTTP status) with `ITEM_ERROR`
  deliberately excluded from the type layer because its members have three different
  remedies — correct and well argued (`errors.py:20-25`).
- `retryable` is a `ClassVar` refused at subclass creation (`connector/__init__.py:75-97`),
  so a new error type cannot inherit a decision nobody made.
- 429 and 5xx classify by status even with no vocabulary; `Retry-After` is a floor and
  is **clamped** by `max_delay_seconds`, with non-finite values rejected
  (`errors.py:221-248, 382-385`). Bounded worst-case wait.
- `TRANSACTIONS_LIMIT` classifies correctly via its `RATE_LIMIT_EXCEEDED` type (probed).
- Transport failures, SSL, connection-reset-during-body-read and the SDK's own
  `AttributeError` bug are each turned into local types with narrow catches
  (`client.py:441-489`). The `__context__` check on the `AttributeError` catch is
  precisely the right narrowing.
- Per-connection isolation for `ConnectorError`/`DerivationError` works, and
  `tests/cli/test_sync_run.py::test_one_connection_failing_does_not_stop_the_others`
  pins it. (See HIGH 7 for the class it misses.)

**Datastore**
- One connection factory, two roles, `mode=ro` at the file with `PRAGMA query_only` as
  a second layer; the read role cannot fall back to writable.
- Wrong key vs. hot-WAL told apart at the first read via `sqlite_errorname`
  (`connection.py:289-319`) — a genuinely hard-won distinction.
- `foreign_keys = ON`, `busy_timeout` from config, WAL, autocommit handles with
  explicit `BEGIN IMMEDIATE`/`COMMIT`/`ROLLBACK` (`engine.py:90-111`) and a documented
  refusal to nest.
- Writer serialization via `flock`, which the kernel releases if the process dies —
  correctly chosen over a DB lease for exactly the crash-resume reason.
- Migrations are forward-only, one transaction per step including the version stamp,
  so an interrupted migration reports "no recognized schema version" rather than a
  false healthy. Migration 002's DDL is frozen by a SHA-256 constant.
- **Backup is done right**: `VACUUM INTO` under the exclusive writer lock (so it is
  consistent by construction, not by timing), folds the WAL in, refuses to overwrite,
  removes partial/zero-byte output before raising, and **verifies** by reopening with
  the key and running `integrity_check`. The zero-byte-file hazard is called out and
  defended. Restore is documented in `operational-spec.md`.
- `store rebuild` refuses to commit a rebuild that changed content at an unchanged
  derivation version, with a content digest that deliberately excludes rowid
  allocations. That check is the reason the command is safe to offer.

**Determinism / correctness of derivation**
- No clock anywhere in a deriver; `first_seen` is a min and `last_seen` a max, so
  replay order does not matter.
- `parse_float=str` everywhere money is parsed; ledger amounts convert exactly or
  refuse; valuations round half-even and **log every rounding**.
- The sign inversion (Plaid positive = money out) is measured, cited, and independently
  re-measured per connection by `signs.py`.
- pending→posted is a **match** on `pending_transaction_id`, not an insert
  (`derivers.py:515-616`), so overrides survive the transition; `removed` is a soft
  delete that keeps the row; a re-added transaction clears `removed_at`.
- `_write_balance`'s one-capture-per-day rule compares captures rather than trusting
  arrival order, and refuses to overwrite a manual-import row.

**Production/sandbox separation**
- **No sandbox institution id, fixture shape or test product anywhere in `src/`** —
  `grep -rn "ins_109508" src/` is empty; every hit is in `tests/`.
- Host selection is a mapping, not a conditional, so an environment with no host fails
  loudly (`client.py:177-195`).
- Sandbox and production are separated at four levels — datastore file, datastore key
  account, aggregator secret account, per-connection credential account — plus a
  WARNING banner at every startup naming the environment and path. `client_id` is
  correctly shared across environments (that is Plaid's model); only the secret differs.

**CLI**
- Exit-code contract is a real module with real reasoning, and `EnrollmentError`
  carries its own code so one condition cannot answer 1 from one check and 2 from
  another.
- A failed run is obvious: `_report` prints the failure reason per connection plus
  "N of M connections could not be synced", exits 1, and the failure is *also* written
  to the log file with `file_only` so the terminal is not doubled.
- Enrollment checks the datastore and the cap **before** minting a link token, confirms
  the immutable history window before the exchange, and handles the post-exchange
  window with real care: the access token reaches the keychain before the first write;
  a cap race past the exchange releases the Item; an unreleasable Item is either given
  a retired row so `connections retire` can retry it, or named in the error when it
  cannot be. `/item/remove` is called on retire and on supersession, and a failure
  there is reported (exit 1) rather than swallowed.
