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
exact failure VRF-002 item 5 is written to catch. `_describe_failure` parses the body for
`error_code`, `error_message` and `request_id`, and degrades to the status line rather than
raising when the body is not what it expects.

Two facts worth keeping from the probe: the body arrives as `str`, because `api_client.py`
decodes it before re-raising; and that decode is unguarded, so an `ApiException` carrying no
body — the `status=0` SSL path — would raise `AttributeError` inside the SDK. Not reachable
through anything Chunk 01 does; **carried to Chunk 02**, which owns the taxonomy.

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

- **The success path has not been probed.** The failure path has (§3b), and so has the
  transport path (§3) — both need no valid credentials. What is still unverified is the
  shape of a *successful* `/institutions/get`, which is what the derivers in Chunk 04 will
  be written against. No sandbox credentials exist on this machine, so
  `tests/connector/fixtures/` is empty and `tests/connector/test_sandbox.py` skips.
  `BANKMACHINE_RECORD_FIXTURES=1 uv run pytest -m sandbox` closes Done-when 0b, tracked as
  VRF-002.
- **`days_requested`'s actual location** in the link-token request — Chunk 03's own
  `verify-api` step, and the highest-stakes parameter in the system (AC-1.2).
- **What `/item/public_token/exchange` carries**, to make the credential-archive exemption
  a mechanism rather than the decision `store/raw.py` currently records.
