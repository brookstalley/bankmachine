---
artifact: build-plan
version: 2
scope: connector-v1
branch: feature/connector-v1
depends_on:
  - artifact: system-requirements
    file_path: docs/system-requirements.md
  - artifact: architecture
    file_path: .prawduct/artifacts/architecture.md
  - artifact: boundary-patterns
    file_path: .prawduct/artifacts/boundary-patterns.md
governed_by:
  - artifact: architecture
    dispositions:
      - "every writable handle comes from the one writer factory, which takes the lock before it returns → conforms; the connector opens no handle of its own. It returns bytes, and the caller archives them through `store/raw.py` on a handle from `engine.writer_connection`. Chunk 01's containment test is what keeps that true — a connector module that imported `store.connection` would fail it"
      - "read-role handles open mode=ro, hold no snapshot beyond the statement that needs it, and never fall back to a writable handle → inapplicable because nothing in this plan takes a read-role handle; the connector reads from the network, not from the datastore"
      - "no component creates the datastore implicitly → conforms; `connector check` opens nothing itself and reports an absent datastore rather than archiving into a fresh one. Chunk 01 tests that the command refuses before it makes a network call, so a typo'd datastore path cannot cost a live API round-trip"
      - "a process that does not recognize the schema version refuses to serve → conforms by inheritance; every archive write in this plan goes through the existing writer path, which already performs the check. No new entry point bypasses it"
  - artifact: project-preferences
    dispositions:
      - "provider-agnostic engine, no roster identity in code or schema — aggregator expressly carved out → conforms, and this is the plan the carve-out was written for. `plaid` may be named as the single aggregator dependency; no financial institution from the roster may be. The sandbox fixtures are the new exposure: Chunk 01 records them from Plaid's own sandbox institutions (`ins_109508` 'First Platypus Bank'), which are fictional and belong to the aggregator, not to the roster"
      - "no roster or operator identity in anything pushed → conforms; `client_id` and secret live in config and keyring, never in source, and the committed fixtures are scanned by the existing pre-push guard like every other file"
      - "credential storage goes through `keyring`, not direct `security` CLI calls → conforms; Chunk 01 extends the existing `secrets.py` rather than adding a second credential path"
      - "errors are exceptions, specific not broad; per-connection errors never abort other connections; silence is the one disallowed outcome → conforms; Chunk 02 is that norm made into the connector's error taxonomy"
      - "sync unless needed — a sync HTTP client is the default → conforms; `plaid-python`'s synchronous ApiClient, no async path"
      - "requirement ids unique within a requirements document → inapplicable because this plan adds no requirement ids"
partition: serial — 02 and 03 both extend the same client module, and 04 derives from the fixtures 01 records. The chunks are a dependency chain rather than a fan-out, and Chunk 01 fixes the containment boundary every later chunk is checked against, so it is the one that must not be built twice.
last_validated: null
---

## Requirements Confidence

**Level:** High

**Why:** Build step 2's scope is stated in `system-requirements.md` §8, its acceptance criteria are
written at field level in FR-1 through FR-5, and the two questions that would have made this Medium
were both answered by the owner before drafting. The contract this plan is written against — the
Derivation Seam — was built in step 1 specifically so step 2 would have something to register into.

**Decisions taken, both by the owner on 2026-09-06:**

- `[DECISION: the connector boundary is module containment plus a test, not an abstract interface |
  system-requirements §9.2, open since the investigation, is answered "one aggregator in v1, drawn so
  a second is a new module rather than a rewrite" | user can revisit]` All `plaid-python` imports live
  under `src/bankmachine/connector/plaid/`; every caller sees `RawResponse` and local types.
  `tests/preferences/test_connector_is_contained.py` is the mechanism, in the same shape as
  `test_connection_is_the_sole_constructor.py`. A `Protocol` with one implementation was considered
  and rejected: an interface designed against a single known implementation encodes that
  implementation's shape, and the honest version of it cannot be written until a second aggregator
  exists. **This closes §9.2** — fold the answer back into that section when the plan lands.
- `[DECISION: fixtures are recorded from live sandbox responses, not written from the SDK's models |
  confirmed the owner has sandbox credentials | user can revisit]` Chunk 01's `verify-api` step reads
  `plaid-python`'s source first and *then* probes, because the two disagree in exactly the places
  that matter — a fixture written from a model definition inherits whatever the model got wrong.

**Open assumptions:**

- `[ASSUMPTION: credential-issuing responses are exempt from the raw archive | HIGH impact | user can
  veto]` **This is the one to look at first.** AC-5.1 says every aggregator response is persisted
  verbatim, and `/item/public_token/exchange` returns a live `access_token` while
  `/link/token/create` returns a short-lived `link_token`. Archiving them verbatim would copy a
  credential out of the Credential Seam and into the datastore, where `store rebuild` reads it and a
  file-copy backup carries it. The archive exists to make normalized tables rebuildable (AC-5.2), and
  a credential response derives no rows — so the exemption costs nothing the archive was for. The
  alternative, archiving with the token elided, is worse: it breaks "verbatim" while still implying
  the response is replayable. Chunk 03 records the exemption in `boundary-patterns.md` and enforces it
  with a test naming the two exempt endpoints; **everything that carries data is archived verbatim,
  with no exceptions.** If you would rather archive them, say so — it is a two-line change in Chunk 03
  and a paragraph in AC-5.1.
- `[ASSUMPTION: "full test suite" in §8 means fixtures replayed offline by default, with live-sandbox
  tests opt-in behind a marker | MED impact | user can override]` A suite that needs network and
  credentials on every run stops being run, and step 1's suite is currently 212 tests that pass
  offline. `uv run pytest` stays offline and deterministic; `uv run pytest -m sandbox` makes the live
  calls, and CI-less as this project is, the marker is what keeps the offline default honest.
- `[ASSUMPTION: the transactions deriver belongs to build step 4, not here | MED impact | user can
  override]` See "What I would do differently" below.

**What would raise confidence:** N/A at this level; the two MED assumptions are both cheap to reverse
mid-plan.

## What I Would Do Differently

Three positions, since a plan handed over without one reads as endorsed:

1. **I would cut the transactions deriver from this plan, and I have.** The obvious reading of "step 2
   registers into `DERIVERS`" is that step 2 derives everything it can fetch. I think that is wrong:
   deriving a `/transactions/sync` response is inseparable from the cursor loop, the soft-delete rule
   (AC-2.2) and the pending→posted match (AC-2.3), all of which are FR-2 and build step 4. Splitting
   the deriver from the loop that feeds it means writing the hard half twice. So Chunk 04 registers
   the **institutions and accounts** derivers — which are the entities transactions reference, so step
   4 has somewhere to point — and `store rebuild` becomes exercisable end-to-end on a real archive,
   which is what the handoff actually needed from this step. The transactions deriver lands with its
   loop.

2. **AC-1.2 deserves a mechanism here, not just a correct call site.** The requested history window is
   immutable after enrollment and the requirements call a vendor-default build a failed build. A
   default parameter value is exactly how that failure happens. So in Chunk 03 the link-token wrapper
   takes the window as a **required argument with no default**, and the configured value carries the
   documented maximum — a caller that forgets it fails to typecheck rather than silently enrolling at
   Plaid's default. This is the cheapest possible insurance against the most expensive mistake in the
   system, and it costs one keyword.

3. **The risk this plan does not price is that sandbox shapes are not production shapes.** Plaid's
   sandbox returns tidy, complete, small responses; production returns nulls in optional fields,
   institutions with missing logos, and accounts whose `mask` is absent. Fixtures recorded from
   sandbox will make the derivers look finished. I have put a hostile-fixture step in Chunk 04 rather
   than pretending otherwise, but the honest statement is that step 3's first real connection is where
   this gets found out, and some rework there is expected rather than a sign something went wrong.

## Status

- [ ] Chunk 01: Walking skeleton — credentials, the contained client, one sandbox response archived
- [ ] Chunk 02: The error taxonomy and the one retry channel
- [ ] Chunk 03: Enrollment endpoints — link token, exchange, capability discovery
- [ ] Chunk 04: Institutions and accounts derivers; rebuild on a real archive
Context: Plan drawn 2026-09-06, directly after `build-plan-datastore-v1.md` closed and merged as
`4c7a491`. Nothing built yet; `feature/connector-v1` is not cut. Next: Chunk 01, whose step 0 is
`verify-api` and whose first act is reading `plaid-python`'s source rather than writing any client
code. The datastore layer this plan archives through is complete and green (212 tests); the
`DERIVERS` registry it registers into is deliberately empty, and `store rebuild` refuses a real
archive until Chunk 04 fills it.

## Scaffolding

### Project Initialization

The package exists. This plan adds one runtime dependency: `uv add plaid-python`.

### Dependencies

`plaid-python` — the aggregator's first-party SDK, chosen in
`docs/build-vs-adopt-investigation.md` §6 over a hand-rolled HTTP client. It is the fourth runtime
dependency, against a stated preference for a dependency set small enough to read; the justification
is that error-code taxonomy and request/response models are the parts a hand-rolled client gets
subtly wrong, and they are the parts this plan's correctness rests on. No new dev dependencies —
`pytest`, `hypothesis`, `mypy` and `ruff` are already present.

### Build & Test Configuration

`uv run pytest` continues to run everything offline. This plan adds a `sandbox` pytest marker
(registered in `pyproject.toml`, deselected by default) for tests making live API calls;
`uv run pytest -m sandbox` runs those. mypy strict and ruff clean remain gates, and `plaid-python`
ships type information — if it turns out not to, that is a Chunk 01 finding, not a reason to relax
strictness.

### Scaffold Verification

`bankmachine connector check` completes against sandbox and prints the institution count; the offline
suite passes with the live tests deselected.

### Verification Strategy

Beyond tests, each chunk is exercised as the operator would: `connector check` against the real
sandbox after Chunk 01, a deliberately broken item (`/sandbox/item/reset_login`) driven through the
taxonomy after Chunk 02, a link token opened in Plaid Link after Chunk 03, and after Chunk 04 a full
`store rebuild` over an archive of real sandbox responses, verified by the rebuild's own content
comparison rather than by reading the tables. `sync shell` is the inspection surface throughout —
which is why step 1 built it.

## Project Structure

```
src/bankmachine/
├── connector/
│   ├── __init__.py        # local types only; no plaid import reaches here
│   └── plaid/             # the ONLY place `plaid` is imported
│       ├── client.py      # transport, auth, the endpoint wrappers
│       ├── errors.py      # the error taxonomy (Chunk 02)
│       └── derivers.py    # registered into store.derivation.DERIVERS (Chunk 04)
└── cli/connector.py       # `bankmachine connector check`
tests/
├── connector/
│   └── fixtures/          # responses recorded from sandbox, committed
└── preferences/           # + test_connector_is_contained.py
```

### Module Boundaries

The connector returns response **bytes** and local types; it never opens a datastore handle, never
imports `store.connection`, and never persists anything itself. Archiving is the caller's act,
through `store/raw.py`. This is what makes the containment test checkable — the boundary is a
relationship (who may import what), not a naming convention.

## Build Chunks

### Chunk 01: Walking skeleton — credentials, the contained client, one sandbox response archived

- **Description:** Prove the whole path with the smallest possible call: config and keyring supply
  credentials, the contained client calls `/institutions/get`, and the verbatim response lands in the
  raw archive. `/institutions/get` is chosen deliberately — it needs client credentials only and no
  enrolled Item, so the skeleton stands before any enrollment exists. This chunk also fixes the
  containment boundary that every later chunk is checked against.
- **Depends on:** none
- **Artifacts consumed:** `.prawduct/artifacts/boundary-patterns.md` (Credential Seam, Configuration
  Interface, Aggregator Client), `docs/system-requirements.md` AC-5.1
- **Deliverables:** new `src/bankmachine/connector/__init__.py`, new
  `src/bankmachine/connector/plaid/client.py`, new `src/bankmachine/cli/connector.py`, aggregator
  environment + `client_id` added to `src/bankmachine/config.py`, the secret added to
  `src/bankmachine/secrets.py`, new `tests/preferences/test_connector_is_contained.py` with its
  negative control in `tests/preferences/verify_norms_go_red.py`, first fixtures under new
  `tests/connector/fixtures/`
- **Tests:** contract — no module outside `connector/plaid/` imports `plaid`, and `connector/`
  imports no datastore handle; unit — response bytes reach `record_response` unaltered, digest
  matches; integration — `connector check` against a missing datastore refuses **before** the network
  call; sandbox (marked) — a live `/institutions/get` round-trip
- **Acceptance criteria:** `bankmachine connector check` completes against sandbox, archives the
  response, and prints what it found; the offline suite passes with sandbox tests deselected; the
  containment test goes red when its norm is broken
- **Foreign API:** plaid-python
- **Visual change:** yes — `connector check`'s output is the operator's first sight of the aggregator
  layer, and the errors it prints are the ones they will meet when credentials are wrong
- **Done when:**
  0. verify-api — read `plaid-python`'s source and type stubs for the client construction path and the
     `/institutions/get` response model, **then** probe sandbox and capture the actual response;
     record both, and any disagreement between them, in new `.prawduct/artifacts/api-notes-plaid.md`
  1. Acceptance criteria met and tests pass
  2. `/prawduct:critic` run and blocking findings resolved
  3. Committed and chunk marked `[x]` in Status

### Chunk 02: The error taxonomy and the one retry channel

- **Description:** Turn Plaid's error surface into the connection-health states FR-4 is written in,
  and build the retry channel `architecture.md` permits on exactly this one boundary. Every error
  carries the connection it belongs to, so one broken connection can never abort another's sync —
  the isolation is established here, before there is a loop that could violate it.
- **Depends on:** Chunk 01
- **Artifacts consumed:** `docs/system-requirements.md` FR-4 (AC-4.1, AC-4.2, AC-4.5), AC-2.6;
  `.prawduct/artifacts/architecture.md` Failure Modes & Resilience
- **Deliverables:** new `src/bankmachine/connector/plaid/errors.py` mapping the aggregator's codes to
  local exception types, backoff-and-retry around the transport in
  `src/bankmachine/connector/plaid/client.py`, error fixtures under `tests/connector/fixtures/`
- **Tests:** unit — each mapped code produces its own exception type, and an unrecognized code raises
  something specific rather than being swallowed; unit — backoff retries a rate-limit and a
  not-ready response and gives up loudly, with the clock injected so nothing sleeps; property
  (hypothesis) — no input to the mapper returns `None` or a bare `Exception`, since silence is the one
  disallowed outcome; sandbox (marked) — `/sandbox/item/reset_login` drives a real
  `ITEM_LOGIN_REQUIRED` through the taxonomy
- **Acceptance criteria:** the four FR-4 error classes each map to a distinct local type carrying the
  connection identifier; a rate-limited call succeeds after backoff rather than failing; a mapped
  failure records what AC-4.5 needs to compute the hole, not just that something broke
- **Done when:**
  1. Acceptance criteria met and tests pass
  2. `/prawduct:critic` run and blocking findings resolved
  3. Committed and chunk marked `[x]` in Status

### Chunk 03: Enrollment endpoints — link token, exchange, capability discovery

- **Description:** The client half of FR-1: create a link token requesting the maximum history window,
  exchange a public token, and discover what a connection can actually do. The enrollment *flow* — the
  CLI, idempotency, the connection cap — is build step 3; this chunk builds only the calls it will
  make, and the credential-handling rules they need.
- **Depends on:** Chunk 02
- **Artifacts consumed:** `docs/system-requirements.md` FR-1 (AC-1.2, AC-1.3), AC-3.2;
  `.prawduct/artifacts/boundary-patterns.md` Credential Seam
- **Deliverables:** `/link/token/create`, `/item/public_token/exchange`, `/item/get` and
  `/accounts/get` wrappers in `src/bankmachine/connector/plaid/client.py`; the configured history
  window with its documented maximum in `src/bankmachine/config.py`; the archive exemption for
  credential-issuing endpoints recorded in `.prawduct/artifacts/boundary-patterns.md`
- **Tests:** unit — the link-token wrapper cannot be called without a window (a compile-time property,
  asserted by mypy over a negative fixture) and sends the configured value, not a default; unit — the
  exchange response is **not** archived, by a test naming the exempt endpoints, while `/accounts/get`
  is; unit — capabilities are read from the item's own product list, and no code path branches on an
  institution's identity; sandbox (marked) — a link token is created and its `days_requested` echoes
  back at the configured maximum
- **Acceptance criteria:** a link token opens in Plaid Link and reports the requested window; the
  access token returned by an exchange reaches the caller and appears in neither the archive nor any
  log line
- **Foreign API:** plaid-python
- **Visual change:** yes — the link URL is what the operator pastes into a browser at enrollment
- **Done when:**
  0. verify-api — read the SDK's link-token and item models for where `days_requested` actually lives
     and what the exchange response carries; probe sandbox to confirm both; append to
     `.prawduct/artifacts/api-notes-plaid.md`
  1. Acceptance criteria met and tests pass
  2. `/prawduct:critic` run and blocking findings resolved
  3. Committed and chunk marked `[x]` in Status

### Chunk 04: Institutions and accounts derivers; rebuild on a real archive

- **Description:** Register the first derivers into `DERIVERS`, so `store rebuild` works end-to-end
  over an archive of real responses instead of refusing for want of one. Institutions and accounts are
  the entities transactions will reference, which is what build step 4 needs from this plan. The
  derivers obey the seam's one rule with teeth: no clock, ever.
- **Depends on:** Chunk 03
- **Artifacts consumed:** `src/bankmachine/store/derivation.py` (the seam's contract),
  `docs/system-requirements.md` FR-5 (AC-5.2, AC-5.3), FR-6 (AC-6.2, AC-6.3)
- **Deliverables:** new `src/bankmachine/connector/plaid/derivers.py` registered into
  `DERIVERS`, `DERIVATION_VERSION` and its description updated in
  `src/bankmachine/store/derivation.py`, hostile fixtures under `tests/connector/fixtures/`
- **Tests:** unit — money lands as integer minor units and never as a float, and history references
  the local `account_id` rather than the aggregator's (AC-6.3); property (hypothesis) — deriving twice
  from one response writes identical rows, and derivation is order-independent; integration — a
  `store rebuild` over an archive of real sandbox responses reproduces the tables, verified by the
  rebuild's own content comparison; unit — hostile fixtures (absent `mask`, null optional fields, an
  institution with no logo) derive or fail loudly, never silently produce a partial row. Oracles are
  written by hand from the recorded responses, never generated by the deriver being tested
- **Acceptance criteria:** `store rebuild` completes over a real archive and reports content
  unchanged; the derivers pass a review for clock use, since a `now()` anywhere in them is the defect
  the whole seam exists to prevent
- **Type:** cumulative-final
- **Done when:**
  1. Acceptance criteria met and tests pass
  2. `uv run python tests/preferences/verify_norms_go_red.py` passes — the raw/rebuild layer changed
  3. Committed, then `/prawduct:critic cumulative` run and blocking findings resolved
  4. Chunk marked `[x]` in Status, and `system-requirements.md` §9.2 closed with this plan's answer

## Early Feedback Milestone

**Milestone chunk:** 01
**What the user can do:** run `bankmachine connector check` and see the system reach a real
aggregator, archive what came back, and say what it found — the first time this product touches the
outside world.

## Governance Checkpoints

**Commit & PR cadence:** commit per chunk after its Critic review passes. Chunk 04's `cumulative`
review makes the branch PR-ready and is the `/prawduct:pr create` gate.

- **After Chunk 01:** confirm the containment boundary before three more chunks are built on it —
  specifically whether the test matches the *relationship* (who imports what) rather than a module
  name, which is the failure this repo has already been burned by once.
- **After Chunk 02:** confirm the error taxonomy is complete enough to carry FR-4, since build steps 3
  and 6 both depend on it and neither will revisit it.
- **After Chunk 04 (cumulative):** full-bundle review, with the archive exemption of Chunk 03 read as
  a whole — an exemption is the kind of decision that looks reasonable in its own chunk and wrong in
  the diff.
