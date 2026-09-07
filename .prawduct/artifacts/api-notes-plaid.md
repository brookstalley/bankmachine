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
`ITEM_LOGIN_REQUIRED` (re-link), `ITEM_LOCKED` (go to your bank) and
`NO_ACCOUNTS` (nothing to sync). Mapping it to any one of them would be
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

### 13. Capabilities are four separate product lists, and `products` is not the useful one

`/item/get` returns `item` with `available_products`, `billed_products`,
`products`, `consent_expiration_time`, `error`, `institution_id`,
`institution_name`, `item_id`, `update_type`, `webhook` — plus a top-level
`status`. On a sandbox item created with `transactions`:

- `products` → `['transactions']` (what was asked for)
- `available_products` → 14 entries including `investments`, `liabilities`,
  `identity` (what this connection *could* do)
- `consented_products` → `None` in the sandbox

AC-3.2 requires investments to be pulled for any connection whose recorded
capabilities include investments, **never for a named institution**. The list
that answers "could this connection do investments" is `available_products`, not
`products` — a capability discovery reading `products` would report exactly what
this product already asked for and never discover anything.

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

## Still to verify

- ~~**The success path has not been probed.**~~ Done 2026-09-06 — see §7. The fixture is
  recorded and `tests/connector/test_sandbox.py` now compares against it rather than skipping.
- ~~**`days_requested`'s actual location**~~ Done — §11. It is nested under
  `transactions`, capped at 730, and **not echoed by the response**.
- ~~**What `/item/public_token/exchange` carries**~~ Done — §12. `access_token`,
  `item_id`, `request_id`, which is what makes the archive exemption an
  obligation rather than a preference.
- ~~**A real `ITEM_LOGIN_REQUIRED`**~~ Done — driven live through
  `/sandbox/item/reset_login` in `test_sandbox.py`, once the exchange call
  existed to mint an Item.
- **A real rate limit or a real `PRODUCT_NOT_READY`.** Neither was provoked;
  both are exercised against constructed responses only, and `PRODUCT_NOT_READY`
  remains the least-evidenced entry in the taxonomy.
- 🔴 **The granted history window.** Not observable before build step 3's first
  real connection, so AC-11.8's shortfall cannot be computed until then.
- **What `/item/public_token/exchange` carries**, to make the credential-archive exemption
  a mechanism rather than the decision `store/raw.py` currently records.
