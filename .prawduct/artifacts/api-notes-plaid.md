# Aggregator API notes — `plaid-python`

What `verify-api` established before any client code was written, per
`build-plan-connector-v1.md` Chunk 01 step 0. Read the SDK's source first, probe second;
the two disagree in exactly the places that matter, and a fake built from a model
definition inherits whatever that model got wrong.

**Verified against:** `plaid-python` 44.0.0 (OpenAPI document `2020-09-14_1.740.1`),
installed into this repo's `.venv`. Method is source reading unless a line says otherwise.

---

## The findings that shaped the client

### 1. The SDK ships no `py.typed`

There is no `py.typed` marker in the installed package, so mypy cannot see into it and
every value it returns is `Any`. The plan anticipated this as a possible Chunk 01 finding
and said it would not be a reason to relax strictness. It was not:

- `[[tool.mypy.overrides]] module = ["plaid.*"] ignore_missing_imports = true` is scoped to
  the SDK alone, and `strict = true` is untouched everywhere else.
- The `Any` stops at `connector/plaid/client.py`, which converts to `FetchedResponse` at
  its own boundary. `warn_return_any` is what makes that conversion mandatory rather than a
  thing to remember.

This is the strongest argument for the containment decision that anything turned up: the
boundary is not only about a hypothetical second aggregator, it is the line the type
checker stops at today.

### 2. 🔴 The response must be taken undecoded, or the archive is not verbatim

`api_client.call_api` deserializes into generated model objects by default. AC-5.1 requires
the archive to hold the response *before any normalization*, and a model round-trip is
normalization: the generated models silently drop fields they do not know about, which are
exactly the fields a later `store rebuild` would need to reproduce rows the aggregator has
since started sending.

`_preload_content=False` is the hook. In `api_client.py` the call returns the underlying
HTTP response **before** reaching the deserialize branch:

```python
if not _preload_content:
    return (return_data)
# deserialize response data
if response_type:
    ...
```

So every call in this client passes `_preload_content=False` and reads `.data` for bytes.
`tests/preferences/verify_norms_go_red.py` flips it back to `True` and confirms
`test_the_body_reaches_the_caller_byte_for_byte` goes red.

### 3. Errors still raise on the undecoded path — checked, because it would have been easy to assume

The worry was that the status check lived inside the `if _preload_content:` block, which
would mean a 400 came back as an ordinary response and got archived as if it were data.
It does not. In `rest.py` the wrapping is conditional but the status check is not:

```python
if _preload_content:
    r = RESTResponse(r)
    logger.debug("response body: %s", r.data)

if not 200 <= r.status <= 299:
    ...
    raise ApiException(http_resp=r)
```

`ApiException.__init__` reads `http_resp.data` and `http_resp.getheaders()`. On the
undecoded path `http_resp` is the raw urllib3 response rather than the SDK's `RESTResponse`
wrapper, so `getheaders()` has to exist on urllib3's own class — it does in urllib3 2.7.0
*(checked at runtime: `HTTPResponse.getheaders` is present and returns the headers)*. Worth
recording because urllib3 2.x deprecated several `httplib` compatibility methods, and if a
future urllib3 removes this one it breaks **only** the error path, which is the path least
likely to be exercised before it matters.

### 3b. 🔴 `ApiException.reason` names nothing; the cause is in the body

Found by the Critic on Chunk 01's own review, then verified by probing the real sandbox host
with deliberately invalid credentials — **a probe that needs no valid ones**, which is why
it could be done before VRF-002:

```
status: 400
reason: 'Bad Request'
body type: str
body keys: [display_message, documentation_url, error_code, error_message,
            error_type, request_id, suggested_action]
  error_type:    'INVALID_REQUEST'
  error_code:    'INVALID_FIELD'
  error_message: 'client_id must be a properly formatted, non-empty string'
  request_id:    '476e317baa236b1'
```

Wrong credentials, a malformed field and an unsupported country are all `400: Bad Request`.
Reporting `reason` alone gives the operator no way to tell a rotated secret from a bug in
this code, and the wrong guess costs a credential rotation that was never the problem — the
exact failure VRF-002 item 5 is written to catch. `connector/plaid/errors.py` parses the
body for `error_code`, `error_message` and `request_id`, and degrades to the status line
rather than raising when the body is not what it expects.

Two facts worth keeping from the probe: the body arrives as `str`, because `api_client.py`
decodes it before re-raising; and that decode is unguarded, so an `ApiException` carrying no
body — the `status=0` SSL path — would raise `AttributeError` inside the SDK. **Discharged in
Chunk 02**, where §9 below establishes which SSL failures actually reach that path.

### 4. Auth is three header api-keys, and the secret is one of them

`Configuration.auth_settings()` builds headers from `self.api_key`: `clientId` →
`PLAID-CLIENT-ID`, `secret` → `PLAID-SECRET`, `plaidVersion` → `Plaid-Version`. The client
constructs `Configuration(host=..., api_key={"clientId": ..., "secret": ...})` and lets
`ApiClient.__init__`'s default `{'Plaid-Version': '2020-09-14'}` supply the version.

`plaid.Environment` carries only `Sandbox` and `Production` hosts, which is why
`_HOSTS` in the client is a mapping rather than a conditional: adding an environment to
`config.Environment` without deciding its host now fails loudly instead of defaulting.

### 5. `/institutions/get` is the right first call

`InstitutionsGetRequest` requires `count`, `offset` and `country_codes` (a list of
`CountryCode` model instances, not bare strings). It authenticates with client credentials
alone — no Item, no access token, no operator data — which is what makes it a walking
skeleton the whole path can be proven against before enrollment exists.

### 6. The dependency brought four transitive packages

`plaid-python` pulled in `nulltype`, `python-dateutil`, `six` and `urllib3`. The handoff
notes record "three runtime dependencies, deliberately small enough to read" as a standing
property; it is now four direct and five transitive. Not a defect, but the sentence in the
handoff is no longer true as written, and `six` in particular is a Python-2 compatibility
shim arriving in a Python-3.11+ project.

### 7. The success path, probed at last — and the sandbox serves the *real* catalogue

VRF-002, discharged 2026-09-06 once sandbox credentials existed. `/institutions/get` with
`count=1, offset=0, country_codes=["US"]` answers 677 bytes, shaped:

```json
{"institutions": [{"country_codes": ["US"], "institution_id": "ins_130958",
                   "name": "...", "oauth": false,
                   "products": ["assets", "auth", "balance", "cra_lend_score", ...],
                   "routing_numbers": ["..."]}],
 "request_id": "...", "total": 10085}
```

- **The top-level keys are `institutions`, `request_id`, `total`.** That set is what
  `test_sandbox.py` pins, by keys and deliberately not by values — the sandbox's list grows.
- **`total` is the aggregator's count of matches, not the page size.** `connector check`
  reports it rather than `len(institutions)`, which is what makes a one-record page read as
  a page rather than as an alarming answer.
- 🔴 **Sandbox `/institutions/get` returns the production institution catalogue**, not the
  fictional test institutions — real names, real routing numbers, 10,085 for `US`. Two
  consequences. Institution shapes recorded from sandbox *are* production shapes, so the
  build plan's §4 risk ("sandbox shapes are not production shapes") is narrower than written
  for this endpoint, while standing exactly as written for accounts and transactions. And
  what makes a recorded fixture safe to commit is the leak guard, not the word "sandbox":
  VRF-002 item 7 was written believing the opposite, and its wording is corrected in place.
- An institution record carries `products` as the aggregator's own vocabulary — 15 values
  on this one, including four `cra_*` — which is what Chunk 03's capability discovery reads
  rather than a list of ours.

**A wrong credential and a malformed one are different error codes, which §3b could not
show.** A *production* secret against the sandbox host returns
`400 INVALID_API_KEYS: invalid client_id or secret provided`, against §3b's `INVALID_FIELD`
for a malformed one. Chunk 02's taxonomy gets both from observation: the remedies differ --
rotate versus fix the call — and the aggregator already separates them.

---

## What Chunk 02 established

### 8. 🔴 The SDK does not enumerate error codes, so the taxonomy cannot be read off it

`plaid/model/plaid_error.py` types `error_code` as a bare `str` and its
`allowed_values` is empty. `error_type` **is** an enum
(`plaid/model/plaid_error_type.py`, 25 values) and is used as the taxonomy's
coarse second layer. The individual codes had to come from elsewhere, and the
provenance differs per code, so `connector/plaid/errors.py` records it per entry:

- **Observed live** — `INVALID_API_KEYS` (a well-formed client id with a wrong
  secret) and `INVALID_FIELD` (a malformed client id). Both need no valid
  credentials, and both are now pinned by `tests/connector/test_sandbox.py`.
- **Spelled by the SDK elsewhere in its own source** — `ITEM_LOGIN_REQUIRED`,
  `ITEM_LOCKED`, `INSTITUTION_DOWN`, `INSTITUTION_NOT_RESPONDING`,
  `ITEM_NOT_SUPPORTED`, `NO_ACCOUNTS`, `ACCESS_NOT_GRANTED` and
  `INSTITUTION_NO_LONGER_SUPPORTED` appear in
  `plaid/model/credit_bank_income_error_type.py`, which belongs to a *different*
  Plaid product. That is evidence the strings are part of the aggregator's
  vocabulary, and it is **not** proof that these endpoints emit them.
- **Named by the requirement alone** — `PRODUCT_NOT_READY`, which AC-2.6 needs
  and which appears nowhere in the installed package *(searched)*. It is the
  least-evidenced line in the taxonomy and is labelled as such in the source.

`ITEM_ERROR` is deliberately **not** in the type layer: it spans
`ITEM_LOGIN_REQUIRED` (renew the login in place, update mode), `ITEM_LOCKED`
(go to your bank) and `NO_ACCOUNTS` (nothing to sync). Mapping it to any one of them would be
confidently wrong most of the time, and a wrong remedy is worse than a refusal.

### 9. 🔴 An SSL failure escapes the SDK as `AttributeError`, from inside the SDK

Two probes, and the first one's answer is not the second one's:

**Probe A — a plain-HTTP server spoken to over `https`.** The failure arrives as
`urllib3.exceptions.MaxRetryError` (`Caused by SSLError(... WRONG_VERSION_NUMBER)`),
raised from `urllib3/util/retry.py`. It never reaches `rest.py`'s
`except urllib3.exceptions.SSLError` clause at all, because urllib3's retry
machinery has already wrapped it. Chunk 01's existing `urllib3.exceptions.HTTPError`
arm catches it, so the common certificate failure was already handled.

**Probe B — a bare `urllib3.exceptions.SSLError`, which does reach that clause.**
`rest.py:211` raises `ApiException(status=0, reason=msg)` with **no**
`http_resp`, so `ApiException.__init__` sets `body = None`. `api_client.py:204`
then runs, unguarded:

```python
except ApiException as e:
    e.body = e.body.decode('utf-8')
```

and the call raises `AttributeError: 'NoneType' object has no attribute 'decode'`
at `api_client.py:204`, with the `ApiException` standing as its `__context__`.

This is a bug in `plaid-python` 44.0.0, and it is contained rather than left to
surface: `_attempt` catches `AttributeError`, **re-raises it untouched unless the
SDK's own exception is standing behind it**, and otherwise reports a transport
failure carrying the original reason. The narrowing is the load-bearing half —
catching `AttributeError` around a call would swallow every genuine typo in the
module and report it as a network problem, turning a five-minute fix into a hunt
for a firewall. `test_an_ordinary_bug_in_this_module_is_not_disguised_as_a_transport_failure`
is the negative control for exactly that.

**The general lesson, recorded because it cost two probes:** the first probe
disconfirmed the source reading, and the disconfirmation was itself incomplete.
Reading `rest.py` said "SSL errors become `ApiException(status=0)`"; probe A said
"no they do not"; probe B established that *both* are true of different SSL
failures, and only one of them is the one that matters.

### 10. `Retry-After` is read only in its delay-seconds form

`ApiException.headers` exists on the undecoded path (`http_resp.getheaders()`,
present on urllib3 2.7.0's `HTTPResponse`) and is `None` when the exception was
built without a response. The HTTP-date form of `Retry-After` is deliberately
ignored: parsing it needs the current time, which would make the wait depend on
this machine's clock agreeing with the aggregator's, and a skewed clock turning a
two-second wait into an hour is a worse failure than falling back to the computed
backoff. **Not observed in a live response** — no rate limit was provoked — so
the parsing is exercised against synthetic headers only.

---

## What Chunk 03's `verify-api` established

### 11. 🔴 `days_requested` is nested, capped at 730 — and the response does not echo it

It lives at `LinkTokenCreateRequest.transactions.days_requested`, inside a
`LinkTokenTransactions` object rather than on the request itself
(`link_token_create_request.py` types the field as `LinkTokenTransactions`;
`link_token_transactions.py` declares `days_requested: (int,)`). The SDK carries
the documented bounds as validations: **inclusive maximum 730, inclusive minimum
1**, which is where this product's "documented maximum" comes from rather than
from a number someone remembered.

🔴 **The create response does not contain it.** Probed live with
`days_requested=730`: the response keys are exactly `expiration`, `link_token`,
`request_id`. There is nothing in the reply that says what window was requested,
let alone what was granted.

That falsifies the plan's Chunk 03 sandbox test as written — "a link token is
created and its `days_requested` echoes back at the configured maximum" is not a
thing this endpoint can be asked. What is checkable here is the *request*: the
value this product sends, asserted on the request object the SDK builds, plus a
live call proving the aggregator accepts 730. **The granted window is only
observable after enrollment**, which is build step 3's first real connection —
already the step flagged as where AC-1.2 becomes irreversible. AC-11.8's
shortfall (`requested_history_days` minus `granted_history_days`) therefore
cannot be computed until then, and no earlier chunk should imply otherwise.

### 12. The exchange response carries the access token in its body

`/item/public_token/exchange` answers with exactly `access_token`, `item_id`,
`request_id` — confirmed both in the SDK's model and against the live sandbox.

This is what turned the archive exemption from a decision into an obligation:
`store/raw.py` is append-only and a datastore backup travels, so archiving this
body verbatim would satisfy AC-5.1 by breaking AC-10.1 permanently. **Built in
Chunk 03**, and keyed on the endpoint rather than on a rule anyone has to
remember: `Endpoint.issues_credential` marks it, and `FetchedResponse` refuses to
exist for such an endpoint, so there is no object for the archive to be given.

### 13. Capabilities are four separate product lists, and no single one of them is the answer

`/item/get` returns `item` with `available_products`, `billed_products`,
`products`, `consent_expiration_time`, `error`, `institution_id`,
`institution_name`, `item_id`, `update_type`, `webhook` — plus a top-level
`status`. On a sandbox item created with `transactions`:

- `products` → `['transactions']` (what was asked for)
- `available_products` → 14 entries including `investments`, `liabilities`,
  `identity` (what this connection *could* do)
- `consented_products` → `None` in the sandbox

AC-3.2 requires investments to be pulled for any connection whose recorded
capabilities include investments, **never for a named institution**. No single
list answers "could this connection do investments" — it is the **union of
`products` and `available_products`**, and each alone fails in a different
direction:

- Reading `products` alone reports exactly what this product already asked for
  and never discovers anything.
- 🔴 Reading `available_products` alone drops whatever the Item is *already*
  doing. Plaid documents that field as "products available for the Item that
  have not yet been accessed", **mutually exclusive with `billed_products`** — so
  an initialized product is guaranteed absent from it.

The second failure is the one that survived a build step, because the measurement
above hides it: on `ins_109508` enrolled with `transactions` alone, investments
sits in `available_products` and either reading looks right. *(Measured
2026-09-08, `ins_109511`, a second sandbox institution: `products:
['investments', 'transactions']`, `available_products: ['balance']`,
`billed_products: ['investments', 'transactions']`. Reading `available_products`
alone recorded that connection's capabilities as `["balance"]` while it was
syncing transactions and holding a 401k — AC-3.2's criterion inverted for exactly
the connections that have investments.)*

`billed_products` is not read: Plaid documents it as equal to `products` in
almost all cases and mutually exclusive with `available_products`, so it can only
contribute what the union already holds.

### 14. `/accounts/get` shape, and what Chunk 04's derivers get

14 accounts from `ins_109508`. Each account carries `account_id`, `mask`,
`name`, `official_name`, `type`, `subtype`, `apy`, `holder_category`, and a
`balances` object of `available`, `current`, `iso_currency_code`, `limit`,
`unofficial_currency_code`.

Two things follow for the derivers. `current` and `available` are **floats** in
JSON, and the schema stores integer minor units — the conversion is the deriver's
job and is where a float rounding error becomes a wrong balance. And `mask` is
present here but is documented as nullable, which is one of Chunk 04's hostile
fixtures rather than a shape the sandbox will hand over on its own.

---

## What Chunk 01 of enrollment established

### 15. 🔴 Hosted Link works, and an unfinished session has no `link_sessions` key

Probed live against sandbox 2026-09-07, which is what settled the enrollment design.

**`/link/token/create` with `hosted_link` set** returns `expiration`, `hosted_link_url`,
`link_token`, `request_id` — one key more than the same call without it (§11 recorded
`expiration`, `link_token`, `request_id`). So the hosted URL is available on this account, and
AC-1.1's "prints a hosted enrollment URL" needs no local web server: the operator opens the URL,
and the CLI polls for the result.

🔴 **`hosted_link_url` WAS discarded by the client before this chunk.** `LinkToken` carried
`token`, `expires_at` and `requested_history_days` only, and `link_token_create` did not send
`hosted_link` at all — so AC-1.1 was unreachable, which is not what "the client half exists"
suggested. Both were fixed in the same chunk. `LinkToken.hosted_link_url` carries it now, and the
lifetime sent with the request is the caller's own wait, so the URL cannot outlive the poll watching
for it.

**`/link/token/get` on an unfinished session omits `link_sessions` entirely.** Measured keys:
`created_at`, `expiration`, `link_token`, `metadata`, `request_id`. `link_sessions` is **absent**,
not an empty list and not a status field — so *"has the operator finished?"* is answered by the
absence of a key. A poll loop that reads `len(link_sessions)` raises `TypeError` on every call
before completion, which is the normal case for most of the loop's life.

**The metadata carries no window field**, confirming live what §11 established from the models:
`client_name`, `country_codes`, `initial_products`, `language`, `redirect_uri`, `webhook`. Together
with `Item` carrying none either, this is the third and last place the granted window could have
been and is not — the evidence behind the AC-1.3/AC-1.3a split.

**Still unprobed: the finished-session payload.** Sandbox offers no way to complete a Hosted Link
session programmatically — `/sandbox/public_token/create` bypasses Link, so it mints a public token
without ever creating a session `/link/token/get` would report. The finished shape needs a human to
complete one session in a browser. Narrowly stated: what is blocked is the *finished* payload only,
and everything above was reachable without it.

---

## What the transaction-sync verify-api established

### 16. 🔴 `transactions_update_status` answers AC-2.6 on the SUCCESS path, and gates AC-1.3a

Read from the pinned SDK 2026-09-07, before any of build step 4 was designed.

`POST /transactions/sync` takes `access_token`, `cursor`, `count`, `options` and answers with
`transactions_update_status`, `accounts`, `added`, `modified`, `removed`, `next_cursor`,
`has_more`, `request_id`. Everything FR-2 asks for is there: `has_more`/`next_cursor` for AC-2.1's
loop and its transactional cursor, the three change lists for AC-2.2, and
`Transaction.pending_transaction_id` for AC-2.3's pending→posted match. A `RemovedTransaction`
carries only `transaction_id` and `account_id`, which is all AC-2.2's soft delete needs.

🔴 **`transactions_update_status` is an enum on the success path**, with values
`TRANSACTIONS_UPDATE_STATUS_UNKNOWN`, `NOT_READY`, `INITIAL_UPDATE_COMPLETE`,
`HISTORICAL_UPDATE_COMPLETE`. Two consequences, and the second is the one that would have been
expensive to discover late:

1. **AC-2.6's "not yet ready" is a field, not an error.** The requirement says a not-yet-ready
   response must trigger backoff-and-retry rather than failure, and this is how the aggregator
   actually says it. `PRODUCT_NOT_READY` — recorded in the handoff as the least-evidenced entry in
   the whole error taxonomy — is not the primary channel for this case. Reading the status field is
   both better evidenced and on the path the code already takes.

2. 🔴 **AC-1.3a's granted window cannot be computed until `HISTORICAL_UPDATE_COMPLETE`.**
   `sync_state.history_start_date` is meant to hold the oldest transaction the aggregator actually
   returned, and that is what `granted_history_days` is derived from. Computing it at
   `INITIAL_UPDATE_COMPLETE` would measure a backfill still in flight and record a shortfall that
   does not exist — a confidently wrong number, well-formed and plausible, which is the exact
   failure class this product was built to prevent. **The status field is the gate**, and a build
   that fills `granted_history_days` on the first completed page has failed AC-11.8 while appearing
   to satisfy it.

`accounts` also rides the sync response, so a sync refreshes account rows without a separate
`POST /accounts/get`. Whether to use it or keep the endpoints separate is a build step 4 decision,
not settled here.

---

### 17. `/transactions/sync` — the loop, the body, and the sign the whole product rests on

🔴 **This section is cited for two unrelated things and the number is load-bearing for both.**
It was headed for its first finding alone, which left the single most-cited measurement in
this document — the sign of an outbound amount, §17.2 below — findable only by full-text
search. That is how a sibling artifact came to cite §16 for it, caught as a Critic finding
on 2026-09-09. The number is unchanged so existing citations still resolve; the subsections
are what a new citation should name.

#### 17.1 🔴 `has_more` is FALSE on a `NOT_READY` response, so it cannot terminate the loop

Probed live 2026-09-07 against a freshly minted sandbox item
(`POST /sandbox/public_token/create` → exchange → `POST /transactions/sync`). Two attempts, three
seconds apart:

| attempt | `transactions_update_status` | added | `has_more` | `next_cursor` |
|---|---|---|---|---|
| 1 | `NOT_READY` | 0 | **`False`** | **empty** |
| 2 | `INITIAL_UPDATE_COMPLETE` | 10 | `True` | set |

🔴 **A loop written as `while has_more:` terminates immediately on the first sync of every new
connection, and records a successful run with zero transactions.** Nothing raises. The connection
looks synced, `last_success_at` is stamped, and the account reports no activity — a *successful*
response computed over data that has not materialized yet, which `api-contract.md` § Direction names
as this product's primary failure mode in so many words. AC-2.6 exists to prevent exactly this, and
the shape of the trap is that the naive loop satisfies AC-2.1's "loop until the source reports no
more pages" **literally** while being wrong.

**So the status is consulted before `has_more`, not after.** `NOT_READY` means back off and retry;
it is not an error, not a degraded connection, and not the end of a page run.

🔴 **`next_cursor` is empty on that response**, so a writer that persists it unconditionally either
stores an empty cursor — which means *start from the beginning* — or, worse, overwrites a good
cursor with one. The cursor is written only from a response that carried one.

🔴 **An empty cursor is not only `NOT_READY`.** Measured in production 2026-09-14 against an
investment-only institution (every account an investment account, none holding cash): `/transactions/sync`
answered `HISTORICAL_UPDATE_COMPLETE`, `has_more` false, **empty** `next_cursor`, and no added,
modified or removed rows. That is a finished backfill with nothing in it, so on a page with no
cursor the status is what says whether the domain landed. The empty cursor is still never stored.
A page that does carry a cursor still lands the domain whatever its status; that is tracked
separately rather than settled here.

**What the transaction body actually holds** *(measured on a real sandbox row)*: `account_id`,
`amount`, `iso_currency_code`, `date`, `authorized_date`, `pending`, `pending_transaction_id`,
`transaction_id`, `name`, `merchant_name`, `personal_finance_category` (`primary`/`detailed`/
`confidence_level`/`version`), plus `counterparties`, `location`, `payment_meta`, `running_balance`
and a dozen more. `personal_finance_category.primary`/`.detailed` are what
`source_category_primary`/`_detailed` take.

#### 17.2 🔴 A purchase arrives POSITIVE — the measurement the sign convention rests on

**Cite this subsection, not §16 and not §17 bare.** `data-model.md` § Direction's sign norm,
`connector/plaid/derivers.py::_operator_signed_amount`, `src/bankmachine/signs.py` and AC-14.1
all rest on the two sentences below. 🔴 **It is one row, on one connection, at one
aggregator** — which is exactly why AC-14.1 scopes the convention as a per-feed claim rather
than a property of the world, and why `signs.py` exists to notice a feed that disagrees.

The sample row is `amount: 89.4` for a merchant purchase on a
depository account — money leaving. Under `data-model.md` § Direction's operator-POV convention that
is stored **negative**, and a build that took it at face value would be wrong by twice the amount on
every spend row.

**The SDK's `to_dict()` floats the amount and parses the date into `datetime.date`.** Neither reaches
this product: bodies are archived as raw bytes and parsed with `parse_float=str`. Recorded because it
is a live demonstration of why AC-5.1 puts the archive before any normalization — the convenient path
loses exactness at the first hop.

---

## The MCP surface

### 18. 🔴 The official SDK is a web stack, and this server speaks over stdio

Measured 2026-09-08, in a throwaway venv rather than by recall: `uv pip install mcp` resolves to
**29 packages** — `mcp` and `mcp-types` plus 27 transitive, including `uvicorn`, `starlette`,
`sse-starlette`, `httpx2`, `httpcore2`, `cryptography`, `opentelemetry-api`, `pyjwt` and
`python-multipart`. This product currently has **five direct dependencies and four transitive**.

🔴 **Adopting it would put an HTTP server and an HTTP client into the dependency graph of a
read-only local tool** whose ratified norm is *"the aggregator's API is the only network
destination; nothing outside `connector/` may reach a network transport"*
(`security-model.md` § Direction). That norm's own recorded limit is that the import scan cannot
see a dependency phoning home on its own — so the mechanism that enforces it is weakest exactly
where a web stack would be added. Taking the SDK is therefore not a neutral convenience; it is a
decision that stresses the norm's weakest seam, for transports this product does not use.

**So the stdio transport is implemented directly, and the norm is conformed to rather than
departed from.** `[DECISION: the MCP server speaks JSON-RPC over stdio without the official SDK |
the SDK's value is its HTTP/SSE transports, session management and spec tracking, and this server
uses none of the first two; 29 packages against 9, including uvicorn and starlette, is a poor trade
for a local read-only tool, and the surface actually needed — `initialize`, `tools/list`,
`tools/call`, `notifications/initialized` — is small enough to read | user can revisit if the
handshake proves brittle in practice]`

**The honest cost of that decision**, stated rather than discovered later: the SDK tracks
protocol-version changes and negotiation edge cases, and a hand-rolled handshake can be subtly
wrong in a way that fails at connection time rather than in a test. The mitigation is that the wire
format below was read from the SDK's own type definitions rather than remembered, and the handshake
is exercised end to end.

**The wire format, read from `mcp_types` 2.2.0** — corrected 2026-09-08 after two of the
lines below were found to be wrong in a way that shipped:

- 🔴 **The version registry is PARTITIONED, and reading past the partition is how this went
  wrong.** `KNOWN_PROTOCOL_VERSIONS` splits into `HANDSHAKE_PROTOCOL_VERSIONS`
  (`2024-11-05`, `2025-03-26`, `2025-06-18`, `2025-11-25`) and `MODERN_PROTOCOL_VERSIONS`
  (`2026-07-28`), whose sessions use a stateless per-request envelope reached by a
  `server/discover` probe. `LATEST_PROTOCOL_VERSION` is documented as the newest revision the
  SDK speaks **in any era** — it is `2026-07-28`, and it is NOT reachable through
  `initialize`. The constant to build a handshake against is `LATEST_HANDSHAKE_VERSION`
  (`2025-11-25`). `DEFAULT_NEGOTIATED_VERSION` is `2025-03-26`. A server echoes back a version
  the client can speak, so **the client's requested version is honoured when recognized**
  rather than the server's newest being asserted.
- `InitializeResult` serializes as `protocolVersion`, `capabilities`, `serverInfo`,
  `instructions`, `_meta` — camelCase on the wire, snake_case in the SDK's Python. Getting this
  wrong is the most likely hand-rolling error, which is why it is recorded here rather than
  inferred. 🔴 `_meta` is the extension point: `serverInfo` is an `Implementation`, which
  declares only `name`, `title`, `version`, `description`, `websiteUrl` and `icons`, and the
  SDK's wire base leaves pydantic's `extra="ignore"` in force — **so a key hung off `serverInfo`
  that `Implementation` does not declare is discarded before any client reads it.**
- `Tool` serializes as `name`, `title`, `description`, `inputSchema`, `outputSchema`,
  `annotations`, `icons`, `_meta`. 🔴 `outputSchema` was missing from this line and its absence
  read as "the protocol has no such field".
- `CallToolResult` serializes as `content`, `structuredContent`, `isError`. A client validates
  `structuredContent` against the tool's `outputSchema` only when `isError` is false, and
  *raises* when a tool that declares an output schema returns no structured content.
- `ListToolsResult` is a `PaginatedResult` **and** a `CacheableResult`; on `2026-07-28`
  `ttlMs` and `cacheScope` are required on the wire.
- `ServerCapabilities` carries `tools`, `resources`, `prompts`, `logging`, `completions`,
  `experimental`, `extensions`, `tasks`.

### 🔴 The decision's predicted cost came due, and this is the evidence it asked for

The `[DECISION: ...]` above accepted one honest risk in writing: *"the SDK tracks
protocol-version changes and negotiation edge cases, and a hand-rolled handshake can be subtly
wrong in a way that fails at connection time rather than in a test."* Its mitigation was that
the wire format "was read from the SDK's own type definitions rather than remembered" — the
list above.

On 2026-09-08 a single review pass found **four** defects in the hand-rolled layer, and three of
them were facts this list either got wrong or never carried:

1. `2026-07-28` offered on the `initialize` path, where it does not exist.
2. `2025-11-25` absent from the supported set, downgrading current clients two revisions.
3. Build identity hung off `serverInfo`, discarded in transit by every SDK-based client.
4. `params` as a by-position array — permitted by JSON-RPC 2.0 — read as an object, raising
   `AttributeError` out of `serve()` and ending the session.

So the mitigation was not sufficient: reading the types once, by hand, produced a list that was
right about what it covered and silently short of what it did not. The decision's revisit clause
is *"user can revisit if the handshake proves brittle in practice."* **It has.** The re-examination
is filed as issue #32, and it turns on a unit this note did not price separately: `mcp-types`
is separately installable and resolves to six packages with no transport of any kind, against the
29-package figure that (correctly) ruled out `mcp` itself.

---

## What is deliberately unused

The generated response models — the largest part of the package — are not used at all, and
cannot be while AC-5.1 requires verbatim bytes and this product derives its own rows. What
the SDK is actually earning its place with is host and auth wiring, endpoint paths and
request validation, its error classes, and connection pooling.

That is a smaller share of the package than the build-vs-adopt investigation assumed when
it chose the SDK over a hand-rolled client (`docs/build-vs-adopt-investigation.md` §6). The
choice still looks right — auth and the error taxonomy are the parts a hand-rolled client
gets subtly wrong — but it is worth re-reading once Chunk 02 has mapped the error codes,
because that is the point at which the remaining value becomes measurable rather than
assumed.

---

## What the production-cutover hardening established

### 19. `/transactions/sync` takes an `options` object, and the bank's own memo is opt-in

Probed against the installed `plaid-python` 44.0.0:

```
TransactionsSyncRequest.openapi_types:
  access_token, client_id, count, cursor, options, secret
TransactionsSyncRequestOptions.openapi_types:
  account_id, days_requested, include_logo_and_counterparty_beta,
  include_original_description, include_personal_finance_category,
  personal_finance_category_version
```

🔴 **`include_original_description` decides what the ARCHIVE holds, not what a deriver
reads.** The field is absent from the response bytes unless it is asked for, and `store/raw.py`
exists because an aggregator's history window is not a thing you get back — so a page fetched
without it has lost the raw bank memo permanently, and turning the option on later affects only
rows fetched later. It is therefore requested on every page from the first one.

`days_requested` is deliberately NOT set here: the link-time value is the source of truth
(§11) and `MAX_HISTORY_DAYS` is already read off the SDK's declared maximum.

### 20. The `accounts` array on a sync response is the same shape as `/accounts/get`'s

§16 left this open ("whether to use it or keep the endpoints separate is a build step 4
decision, not settled here"). It is settled: the array is derived through the same account
deriver, before the change lists that reference it, because the aggregator is naming the
accounts those transactions belong to in the same body — and an account that has closed, been
de-selected in Account Select, or stopped being shared drops out of `/accounts/get` while its
deltas keep arriving here.

🔴 **It is NOT a roster observation.** `/accounts/get` answers *these are the accounts this
connection has*; this array answers *these are the accounts the transactions in this body
belong to*, which is a weaker statement. AC-12.5 measures an account's absence by comparing
`accounts.last_seen_date` against `connections.roster_observed_date`, so recording this array
as an observation would leave a connection whose sync page landed after midnight with no
account matching its own last roster read.

### 21. `plaid.Configuration` takes `ssl_ca_cert`, and naming it changes nothing else

Probed with `ssl_ca_cert` set:

```
pool_manager.connection_pool_kw:
  {'maxsize': 50, 'cert_reqs': VerifyMode.CERT_REQUIRED, 'ca_certs': '<the path>',
   'cert_file': None, 'key_file': None}
verify_ssl True   assert_hostname None
```

So the value reaches urllib3 as `ca_certs` and neither verification nor hostname checking is
affected. Left at its default `None`, urllib3 falls back to OpenSSL's default paths, which
honour `SSL_CERT_FILE` and `SSL_CERT_DIR` — and this product's documented setup step is
`source .env`, so the shell that runs a sync routinely imports environment. Pinning `certifi`'s
bundle makes both variables inert for the one channel that carries live credentials.

---

## What the investments verify-api established

*(Measured 2026-09-12 against the live sandbox, `ins_109511`, an Item enrolled with
`investments`. Recorded verbatim as `tests/connector/fixtures/investments_holdings_get.json`
by `test_holdings_come_back_and_carry_what_the_schema_declares_not_null`.)*

### 22. `/investments/holdings/get` — the shape, and the two fields the schema wanted that are not in it

The body is five keys: `accounts`, `holdings`, `securities`, `item`, `request_id`.
🔴 `is_investments_fallback_item`, which the pinned SDK's response model declares, is
**absent from the body** — so a deriver that read it off the model's field list would be
reading a key that is not there.

`accounts` is **every account on the Item** (14 here, the same shape `/accounts/get`
returns), not the investment ones alone. Only two of them hold positions. The
`balances` object has grown a field since §14 was measured: `margin_loan_amount`, beside
`available`, `current`, `iso_currency_code`, `limit` and `unofficial_currency_code`.

**A holding carries no capture date of its own.** The fields are `account_id`,
`security_id`, `quantity`, `institution_price`, `institution_value`, `cost_basis`,
`institution_price_as_of`, `institution_price_datetime`, `iso_currency_code`,
`unofficial_currency_code`, `tax_lots`, `vested_quantity`, `vested_value` — and the only
date among them is the price's, not the position's. So `holdings.as_of_date` is **this
system's capture date**, derived from `received_at` exactly as `balances_daily.as_of_date`
is, and the two series line up by construction rather than by the aggregator's agreement.

🔴 **And the price is four years stale in the sandbox**: `institution_price_as_of` is
`2021-05-25` on all 13 positions, `institution_price_datetime` is null on all 13. That is
canned data, but it is the aggregator deliberately serving a price older than the answer —
the case `list_holdings` has to disclose rather than smooth away.

What is nullable **in practice**, over 13 positions and 13 securities:

| Field | Nulls | Note |
|---|---|---|
| `holdings.quantity`, `institution_price`, `institution_value`, `account_id`, `security_id` | 0/13 | every NOT NULL column has something to hold |
| `holdings.cost_basis` | 0/13 | populated throughout here, which is **not** a guarantee — the aggregator documents it nullable |
| `holdings.iso_currency_code` | 0/13 | `unofficial_currency_code` null 13/13 |
| `holdings.institution_price_datetime`, `vested_quantity`, `vested_value` | 13, 12, 12 | |
| `securities.security_id`, `name`, `type`, `iso_currency_code`, `is_cash_equivalent` | 0/13 | |
| `securities.ticker_symbol` | 3/13 | |
| `securities.close_price`, `close_price_as_of`, `cusip`, `isin`, `sedol`, `figi`, `cfi_code`, `sector`, `industry`, `subtype`, `market_identifier_code`, `option_contract`, `fixed_income`, `update_datetime` | 13/13 | 🔴 **`close_price` is null on every security**, so `securities.close_price_minor` and `close_price_as_of` get nothing from this feed and their rounding path is unexercised live |
| `securities.institution_id`, `institution_security_id`, `proxy_security_id` | 12/13 | |

`security_type` takes seven values here: `cash`, `cryptocurrency`, `derivative`, `equity`,
`etf`, `fixed income`, `mutual fund`.

**Sub-cent valuations are ordinary, and quantities are finer still.** Four of thirteen
`institution_value`s and one `cost_basis` carry more precision than the cent:
`115.57268`, `636.309`, `1373.6865`, `1855.875`, `542.041`. So `to_minor`'s half-even
rounding fires on a third of a real payload rather than at an edge. Quantities include
`0.00293644` (a Bitcoin position) and `12345.67` — exact decimal text, never a float.

🔴 **A cryptocurrency holding is still denominated in USD.** The BTC position carries
`iso_currency_code: "USD"` and a null `unofficial_currency_code`: the currency on a holding
is the currency of the *value*, not of the instrument. Nothing in this payload can provoke
the undenominable-row refusal, so that path stays a constructed fixture — and this is the
measurement that says so, rather than an assumption that it would be easy.

### 23. 🔴 Holdings decompose an account's balance, and the two do not have to add up

The reason net worth must read one series or the other and never sum them is already in
this payload, and the arithmetic is not what the name suggests:

| Account | `balances.current` | Σ `institution_value` | Difference |
|---|---|---|---|
| Plaid IRA | 320.76 | 320.76 | 0 |
| Plaid 401k | 23631.9805 | 25125.63318 | **−1493.65268** |

The IRA reconciles exactly; the 401k does not, and no `margin_loan_amount` explains it —
the IRA is the account carrying one (100), and the 401k's is null. So:

- **Summing the two is a double count.** The 401k's value is already in `balances_daily`
  by way of `/accounts/get`; its positions are what that value is *made of*.
- **Neither can be derived from the other.** A reconciliation asserting
  Σ holdings = balance would go red on the aggregator's own canned data, so it is not a
  test this product can write, and a total built by substituting one for the other would be
  wrong by 6% on this account.

### 24. Capabilities, measured a second time on the institution that discriminates

`ins_109511` enrolled with `investments` alone reports `products: ['investments']` and an
`available_products` of twelve entries that **does not include investments**. §13's rule
survives its second measurement: reading `available_products` alone would record this
connection — the one actually holding a 401k — as incapable of the only thing it does.

### 25. An Item answers for investments it has only *available*, not initialized

The capability gate reads the union of `products` and `available_products` (§13), so in
production it will call for Items that have never had the product added. Probed directly
rather than reasoned about: an `ins_109508` Item enrolled with `transactions` alone reports
`products: ['transactions']`, carries `investments` (and `investments_auth`, which is why the
gate compares whole values) among fourteen `available_products` — and
`/investments/holdings/get` **answers it normally**, 13 holdings and 13 securities. No
`PRODUCT_NOT_READY`, no refusal.

🔴 **What this does NOT settle is the bill.** The aggregator adds a product to an Item on
first use and bills for it; the sandbox bills nothing, so this probe can say the call
succeeds and cannot say what it costs.

**The bill, as the aggregator documents it** *(<https://plaid.com/docs/account/billing/>,
read 2026-09-14)*. Investments is **two subscriptions**, each a monthly fee per Item for as
long as the access token is valid:

- *Investments Holdings* — added by requesting `investments` at `/link/token/create`, or by
  the first `/investments/holdings/get`.
- *Investments Transactions* — added, together with Holdings, by the first
  `/investments/transactions/get`.

Both are measured against this product's own calls. Enrollment asks for `investments`
optionally (`ENROLLMENT_OPTIONAL_PRODUCTS`), so a capable institution's Item carries Holdings
from the moment it is linked, and the union gate rarely reaches an Item it would not already
have reached. The Item it can reach is one whose institution could not serve investments at
link time and lists them later. Every sync calls both endpoints for every connection the gate
admits, so the **second subscription starts at that connection's first sync**.

**Ruled 2026-09-14 by the owner: both subscriptions are accepted and the union gate stands.**
The gate did not change, so §13's finding that `products` alone discovers nothing is untouched.
The per-Item price is **unpriced here**: the aggregator publishes no investments rate, and the
figure is the owner's contract, not something this repository can know. AC-3.2 records the
ruling.

---

## What the investment-transactions verify-api established

*(Measured 2026-09-12 against the live sandbox, `ins_109511`, an Item enrolled with
`investments`. First page recorded verbatim as
`tests/connector/fixtures/investments_transactions_get.json` by
`test_investment_transactions_come_back_windowed_and_paginated`; the paging and window
facts by `test_investment_transactions_page_by_offset_against_a_stated_total` and a
throwaway exhaustion probe.)*

### 26. `/investments/transactions/get` — paged by offset, and the three signals it does not send

The body is six keys: `accounts`, `investment_transactions`, `securities`, `item`,
`total_investment_transactions`, `request_id`. As in §22, the SDK response model's
`is_investments_fallback_item` is **absent from the body**. `accounts` is again **every
account on the Item** (14), not the investment ones alone, and `securities` (13) is the same
roster the holdings reply carries.

A row is fifteen fields: `investment_transaction_id`, `account_id`, `security_id`, `date`,
`name`, `quantity`, `amount`, `price`, `fees`, `type`, `subtype`, `iso_currency_code`,
`unofficial_currency_code`, `cancel_transaction_id`, `transaction_datetime`.

🔴 **Three things the schema and the plan expected are not in it.** Measured against the
raw body, which is what this system derives from — not against the SDK's model, which
omits and invents fields independently (§22):

| Expected | Reality |
|---|---|
| `settlement_date` | **No such field on any row.** The only date is `date` (the trade date) and a mostly-null `transaction_datetime`. `investment_transactions.settlement_date` gets **nothing** from this feed and stays null; the column keeps its meaning for a manual import, which is the only writer that can ever fill it |
| a removal signal | **None.** No `removed`, no `is_removed`, no `pending` — this is a windowed read, not a delta like `/transactions/sync`, so nothing tells you a row went away. `cancel_transaction_id` exists (null 100/100 here) and is a *cancellation reference*, not a tombstone: a cancelling row points at the row it cancels |
| a cursor | **None.** Paging is `options.offset` / `options.count` against the stated `total_investment_transactions`. There is no cursor, so the far-end idempotence `TRANSACTIONS_SYNC` documents — the same cursor returns the same page, and a killed process re-reads what it never committed — **is not available here** |

**Paging, measured.** `count` defaults to 100 and 500 is honoured; 1169 rows came back in 3
pages of 500. `offset` advances (page two's first id differs from page one's), the stated
total does **not** move between pages, and an offset at or past the total answers with an
empty `investment_transactions` and no error — so the loop's exit is the empty page or
`offset >= total`, both measured rather than assumed.

🔴 **Rows arrive newest-first**, descending by `date`. So the earliest date in the window is
on the LAST page: nothing about the range's start is knowable until the paging is exhausted,
which is a constraint on when `sync_state.history_start_date` can be written, not just on
what it is written from.

🔴 **The granted window is not stated anywhere, and the rows cannot stand in for it.** The
response does not echo `start_date`/`end_date` and has no `days_requested` counterpart to
§11's. Asking for the configured maximum (730 days, `2024-09-12 .. 2026-09-12`) returned rows
spanning exactly `2024-09-12 .. 2026-09-12` — the full window, so **no shortfall is
observable in the sandbox** and that path stays unexercised live, as `close_price`'s rounding
does in §22.

What follows is the part that bites: the span of the returned rows answers *"when was this
account last active"*, which is **not** the question `history_start_date` asks. An Item that
was granted two years and simply had no trades in the first eighteen months returns the same
narrow span as an Item granted six months. **This endpoint cannot tell a short window from a
quiet account**, so a "shortfall" derived from row dates would report an inactive brokerage
as a truncated history on every run.

**Precision, and which columns round.** Over the first page of 100:

| Field | Finer than a cent | Note |
|---|---|---|
| `amount` | 0/100 | clean at the cent here, which is **not** a guarantee — §22 found a third of holdings valuations sub-cent |
| `fees` | 0/100 | |
| `price` | 12/100 | `40876.02675`, `94.808` — so `price_minor`'s half-even rounding does fire on real data |
| `quantity` | — | 🔴 **17 significant digits**: `-0.008902867462305952`, `4211.152345617756`. Exact decimal TEXT, and the reason `parse_response_body`'s `parse_float=str` is not optional. `_exact_quantity` already carries this and is reused rather than reimplemented |

Nulls over the same 100 rows: `unofficial_currency_code` and `cancel_transaction_id` 100/100;
`transaction_datetime` 88/100; **every other field 0/100**, including `security_id` (which the
aggregator documents nullable) and the `type`/`subtype` pair the NOT NULL columns need.
`iso_currency_code` is `USD` throughout.

`type` takes four values here — `buy`, `sell`, `cash`, `fee` — and `subtype` six: `buy`,
`sell`, `contribution`, `interest`, `dividend`, `account fee`. Amounts are signed both ways
(56 negative, 44 positive over the page), so the operator's-point-of-view normalization has
both directions to exercise on live data.

---

## Still to verify

- ~~**The success path has not been probed.**~~ Done 2026-09-06 — see §7. The fixture is
  recorded and `tests/connector/test_sandbox.py` now compares against it rather than skipping.
- ~~**`days_requested`'s actual location**~~ Done — §11. It is nested under
  `transactions`, capped at 730, and **not echoed by the response**.
- ~~**What `/item/public_token/exchange` carries**~~ Done — §12. `access_token`,
  `item_id`, `request_id`. The archive exemption it forced is **built**:
  `Endpoint.issues_credential` marks the endpoint and `FetchedResponse` refuses
  to exist for one, so there is no object for the archive to be given.
- ~~**A real `ITEM_LOGIN_REQUIRED`**~~ Done — driven live through
  `/sandbox/item/reset_login` in `test_sandbox.py`, once the exchange call
  existed to mint an Item.
- **Hosted Link in UPDATE mode.** `link_token_create_update` sends `access_token` and an
  `update` object and no `products`; the SDK accepts all three *(verified against the pinned
  package: `LinkTokenCreateRequest.openapi_types` carries `access_token` and `update`)*. The
  aggregator does return an `https` `hosted_link_url`, with an expiry, for such a token on this
  account — `test_sandbox.py::test_an_update_mode_session_is_hosted_too` ran green against the live
  sandbox on 2026-09-11. This was the open one: without a hosted URL `connections reauth` would
  have had nothing to print, because AC-1.1 rules out a local web server for the repair exactly as
  for the enrollment. The probe skips without `BANKMACHINE_PLAID_CLIENT_ID`, so **a green
  `-m sandbox` run on a machine without credentials is still not evidence** — check the count.
- **Whether update mode re-issues `account_id`.** The aggregator documents update mode as the
  repair for expired credentials and says to re-read account ids afterwards rather than assume they
  are stable, which stops short of a guarantee. `connections reauth` asserts the half it can — the
  item id must come back unchanged or the repair is refused — and the account half was **VRF-007**,
  run 2026-09-11: in the sandbox, update mode kept the item AND every `source_account_id` on it. No
  account row was created, and the follow-on sync added no transactions.

  🔴 **A signal, not the answer.** One sandbox institution is not evidence about a real one, and
  this account's `persistent_account_id` is NULL here as it is nearly everywhere — so the fallback
  #95 exists for is untouched by this result. What it does establish is that the duplication has a
  path that does not produce it, which is what #67 was built to offer.
- ~~**The `/investments/holdings/get` response shape.**~~ Done 2026-09-12 — see §22-24.
  The fixture is recorded and the probe compares against it on every `-m sandbox` run.
- ~~**`/investments/transactions/get`.**~~ Done 2026-09-12 — see §26. Paged by offset, with no
  settlement date, no removal signal and no stated window of its own.
- **What an investments call returns when the product is disabled in the dashboard.** The
  production guide offers disabling `investments` as the way to avoid its bill, and enrollment
  still links because it asks optionally. But `sync run` gates on the Item's capabilities, not on
  the dashboard. If the aggregator still lists `investments` in `available_products` for such a
  client and refuses the call, every nightly sync records an investments error and exits `1`.
  The sandbox enables every product, so this cannot be probed before production.
- **A real rate limit or a real `PRODUCT_NOT_READY`.** Neither was provoked;
  both are exercised against constructed responses only, and `PRODUCT_NOT_READY`
  remains the least-evidenced entry in the taxonomy.
- 🔴 **The granted history window.** Not observable before build step 3's first
  real connection, so AC-11.8's shortfall cannot be computed until then.
