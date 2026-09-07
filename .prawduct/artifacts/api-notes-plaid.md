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
- **`days_requested`'s actual location** in the link-token request — Chunk 03's own
  `verify-api` step, and the highest-stakes parameter in the system (AC-1.2).
- **A real `ITEM_LOGIN_REQUIRED`**, via `/sandbox/item/reset_login`. It needs an
  enrolled Item, which needs the exchange call, so it moved from Chunk 02 to
  Chunk 03 — see that plan's amendment note.
- **A real rate limit or a real `PRODUCT_NOT_READY`.** Neither was provoked;
  both are exercised against constructed responses only.
- **What `/item/public_token/exchange` carries**, to make the credential-archive exemption
  a mechanism rather than the decision `store/raw.py` currently records.
