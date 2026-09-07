---
artifact: build-plan
version: 2
scope: enrollment-v1
branch: feature/enrollment-v1
depends_on:
  - artifact: system-requirements
    file_path: docs/system-requirements.md
  - artifact: build-plan-connector-v1
    file_path: .prawduct/artifacts/build-plan-connector-v1.md
  - artifact: api-notes-plaid
    file_path: .prawduct/artifacts/api-notes-plaid.md
  - artifact: data-model
    file_path: .prawduct/artifacts/data-model.md
  - artifact: boundary-patterns
    file_path: .prawduct/artifacts/boundary-patterns.md
governed_by:
  - artifact: architecture
    dispositions:
      - "every writable handle comes from the one writer factory, which takes the lock before it returns → conforms; enrollment writes `institutions` and `connections` through `engine.writer_connection`, and the connector still opens no handle of its own. The containment test from connector-v1 keeps that true"
      - "read-role handles open mode=ro, hold no snapshot beyond the statement that needs it, and never fall back to a writable handle → conforms, and Chunk 03 is where it bites: the cap check counts live connections, and counting on a read handle then writing on a writer handle is a TOCTOU window. The count is taken inside the writer transaction that performs the insert, so the partial unique index — not the count — is what actually enforces the cap"
      - "no component creates the datastore implicitly → conforms; `enroll` refuses an absent datastore through `inspect` before any network call, in the same shape as `connector check`, so a typo'd path cannot cost a live Link session"
      - "a process that does not recognize the schema version refuses to serve → conforms by inheritance; every write goes through the existing writer path, which already performs the check"
  - artifact: security-model
    dispositions:
      - "secrets live only in the OS keychain, and nothing returns one into a log line, exception message or repr → 🔴 governs Chunk 02's central act. The access token goes to the keychain and `connections.credential_ref` holds the account name that finds it. Three new credential-shaped values pass through this plan — link token, public token, access token — and none may reach the datastore, a log, or an exception message"
      - "log redaction happens at the formatter, over-redacts by design, keyed to credential shape not vendor prefix → conforms by inheritance; the formatter already covers records this plan's code emits. Chunk 02 asserts it over a real enrollment log rather than assuming inheritance"
      - "no tracked file carries a credential-shaped string → conforms; the enrollment fixtures carry sandbox-issued tokens, so Chunk 02 redacts them at record time rather than relying on the guard to accept them"
      - "the aggregator's API is the only network destination → conforms; `/link/token/get` is the aggregator. The hosted URL is opened by the operator's browser, not by this process — and Chunk 02 prints it rather than shelling to `open`, which would be this product starting a network fetch by proxy"
  - artifact: api-contract
    dispositions:
      - "the MCP surface is read-only → inapplicable because this plan adds no MCP tool. Enrollment is deliberately not a tool surface: it requires the operator to type bank credentials into a browser, which is not an act an agent initiates"
      - "every response carries a freshness stamp, incompleteness rides the success path as a warning → inapplicable because this plan adds no MCP response. Its sibling obligation is discharged instead by AC-1.3a: a null granted window is a recorded unknown, not a silent completeness claim"
      - "the CLI's three-way exit code is a contract: 0 success, 1 ran and found a problem, 2 could not run → 🔴 conforms, and this plan must decide two cases rather than inherit them. A cap refusal is `1` (it ran and found a problem); an abandoned or expired Link session is `1`; an unreachable aggregator or absent datastore is `2`. Recorded in Chunk 03's Done-when because a wrong exit code here is invisible until a scheduler reads it"
  - artifact: data-model
    dispositions:
      - "every stored amount is signed from the operator's point of view → inapplicable because this plan stores no amounts. Enrollment writes `institutions` and `connections`, neither of which holds money"
      - "all monetary values are stored as integer minor units → inapplicable, same reason"
      - "calendar dates and UTC instants are distinct types and never mix → conforms; `enrolled_at` and `retired_at` are UTC instants through `UtcInstantColumn`, and this plan introduces no calendar date"
  - artifact: operational-spec
    dispositions:
      - "no filesystem path is hardcoded → conforms; the connection cap joins `history_days` as configuration with a documented default, and this plan adds no path of any kind"
      - "a backup destination is never created implicitly and never overwritten → inapplicable because this plan writes no backup"
  - artifact: project-preferences
    dispositions:
      - "provider-agnostic engine, no roster identity in code or schema → 🔴 conforms, and this is the plan where it is most exposed: enrollment is the act that names a real institution. Sandbox fixtures use the aggregator's fictional institutions only, and no test may name an institution from `deployment/`"
      - "credential storage goes through `keyring`, not direct `security` CLI calls → conforms; Chunk 02 extends `secrets.py` with per-connection access-token accessors rather than opening a second credential path"
      - "errors are exceptions, specific not broad; silence is the one disallowed outcome → conforms; the poll loop's timeout and the cap refusal are both named exception types, not a `None` return the caller may ignore"
      - "requirement ids unique within a requirements document → 🔴 engaged rather than inapplicable: this plan ADDS `AC-1.3a` to `docs/system-requirements.md`. `tests/preferences/test_requirement_ids_unique.py` is the check, and it passes on the amendment as written"
partition: serial — all three chunks extend the same two modules (`connector/plaid/client.py` and a new `cli/enroll.py`), and Chunk 02 cannot be specified until Chunk 01's live probe says what Hosted Link actually returns. A fan-out here would have two delegates editing one client module against an API shape neither had confirmed.
last_validated: null
---

## Requirements Confidence

**Level:** Medium

**Why:** FR-1's six acceptance criteria are written at field level and the schema they land in has
shipped, so *what* to build is not in question. What is unconfirmed is one vendor behaviour that the
whole shape of Chunk 02 rests on — whether Hosted Link is enabled for this account and whether the
completed session's public token is actually retrievable by polling. The SDK surface is verified
(see below); the runtime behaviour is not.

**What would raise it:** one live sandbox probe, which is Chunk 01's `verify-api` step and the first
thing built. If Hosted Link is unavailable, Chunk 02's design changes rather than its scope — the
fallback is the loopback callback server, and that fallback requires amending AC-1.1.

**Decisions taken by the owner, 2026-09-07:**

- `[DECISION: enrollment uses Hosted Link plus polling `/link/token/get`, not a local callback server
  | the operator offered a web server; the SDK surface shows it is not needed, and AC-1.1 already
  forbids one. A listener would add a registered redirect URI, a process holding a single-use
  credential, and a second thing that can be running when the CLI believes it is not | user can
  revisit if Chunk 01's probe shows Hosted Link is unavailable]`

- `[DECISION: AC-1.3 is split — enrollment records the requested window, and AC-1.3a homes the
  granted window at the first backfill | no response in the enrollment path carries the granted
  value, verified against the pinned SDK; and `core_schema.py` has assumed exactly this since build
  step 1, so the amendment settles a disagreement rather than creating one | user can revisit]`
  The amendment and its evidence are recorded in `docs/system-requirements.md` § FR-1, which is the
  artifact that owns the norm.

**Open assumptions:**

- `[ASSUMPTION: the operator completes the Link session within one invocation, so the CLI can poll
  in the foreground rather than persisting session state across runs | MED impact | user can
  correct]` A resumable enrollment would need a `link_sessions` table; nothing in FR-1 asks for one,
  and a re-run simply starts a new session. Chunk 02 prints the session id so an abandoned run is
  identifiable rather than mysterious.
- `[ASSUMPTION: the connection cap defaults to 10, matching the plan tier recorded in
  `nonfunctional-requirements.md` | LOW impact | user can override in config]`
- `[ASSUMPTION: `products` at enrollment stays `["transactions"]`, with investments discovered from
  `available_products` rather than requested up front | MED impact | user can correct]` AC-3.2 makes
  capability discovery drive investment pulls, and `/item/get` already reports `available_products`
  (`api-notes-plaid.md` §13). Requesting `investments` at enrollment would bill a product the
  operator may not have.

## What I Would Do Differently

The advisory obligation, stated rather than left implied:

**I would cut AC-1.6 from this plan if you wanted it smaller.** Retirement is genuinely required by
AC-1.5 — the cap refusal has to list connections *so one can be removed*, and a refusal pointing at
a command that does not exist is a dead end. So I have kept it. But it is the one piece here that is
not on the critical path to a first real connection, and if you want the history window verified
sooner, Chunk 03 is the chunk to defer.

**The risk this plan does not price is the one the requirements also do not.** AC-1.2 is called the
highest-stakes parameter in the system, and the entire protection against getting it wrong is that
`history_days` is a required argument. That stops a *forgotten* window. It does nothing about a
*wrong* one — a config file with `history_days = 90` enrolls silently and irreversibly, and the
operator finds out two years later. Chunk 02 therefore prints the requested window and requires
confirmation before the exchange, which is the last moment it is still reversible. That is a
deliberate addition to AC-1.1's flow, and I would rather over-confirm here than anywhere else in
this product.

**I am not building account retirement**, which build step 2 left as an inherited obligation. It
belongs to the accounts deriver, not to enrollment, and folding it in would mean this plan touches
the derivers it otherwise leaves alone. It stays in `build-plan-connector-v1.md` § "What Build Step 3
Inherits" as step 4's work — flagged here so it is visibly deferred rather than quietly dropped.

## Chunks

### Chunk 01 — Hosted Link, and the window that must not be guessed

**Type:** code
**Foreign API:** Plaid Link (`/link/token/create`, `/link/token/get`)

Extends the client so an enrollment URL can exist at all, and closes the inherited obligation that
no call site passes `config.history_days`.

- `link_token_create` gains `hosted_link`, and `LinkToken` carries the `hosted_link_url` the response
  returns. It is currently discarded, so AC-1.1 cannot be met without this.
- New endpoint `LINK_TOKEN_GET`, declared `retry_safe=True` (a pure read) and 🔴
  `issues_credential=True` — its body carries a `public_token` that mints a durable Item, the same
  property that keeps the exchange endpoint (`POST /item/public_token/exchange`) out of the
  archive. Without this declaration the
  poll response lands verbatim in the append-only archive and travels in every backup.
- `link_token_get(link_token)` returns the completed session's public token, or reports that the
  session is unfinished. The path is
  `link_sessions[].results.item_add_results[].public_token`, with `on_success.public_token` as the
  alternate the probe must disambiguate.

**Done-when:**

0. **`verify-api`** — probe the live sandbox: create a link token with `hosted_link`, confirm
   `hosted_link_url` is returned, and read back an *unfinished* session through
   `POST /link/token/get` to
   capture the shape before completion. Record findings in `api-notes-plaid.md` §15. 🔴 If Hosted
   Link is unavailable on this account, stop and report — Chunk 02's design depends on the answer.
1. `hosted_link_url` is on `LinkToken` and asserted from a recorded fixture.
2. `LINK_TOKEN_GET` is declared credential-issuing, and a test proves `FetchedResponse` refuses to
   exist for it — the same assertion shape that guards the exchange endpoint.
3. A test drives `link_token_create` through a config whose `history_days` differs from the default
   and asserts the *request object* carries that value at `transactions.days_requested`. This is the
   inherited obligation; a test passing a literal does not discharge it.
4. `verify_norms_go_red.py` gains the credential-archive case for the new endpoint.

**Critic mode:** chunk

### Chunk 02 — `bankmachine enroll`, end to end

**Type:** code
**Visual change:** yes

The command AC-1.1 names, through to a persisted connection.

- `enroll` prints the hosted URL and the **requested window**, then waits. 🔴 It confirms the window
  before exchanging, because that is the last moment AC-1.2 is reversible.
- Polls `/link/token/get` with a bounded timeout, then exchanges the public token.
- Stores the access token in the keychain under a per-connection account name; `credential_ref`
  holds that name. New accessors in `secrets.py` — it has none for access tokens today.
- Derives the institution from `/item/get` (**not** `/institutions/get`, which is the aggregator's
  production catalogue) and writes `institutions` + `connections` in one writer transaction.
- `requested_history_days` is set; `granted_history_days` stays null per AC-1.3a.
- Re-enrolling an institution that already has a live connection updates it rather than inserting
  (AC-1.4), which the partial unique index already enforces structurally.

**Done-when:**

1. An end-to-end sandbox enrollment produces one `connections` row with a non-null
   `requested_history_days`, a null `granted_history_days`, and a `credential_ref` that resolves in
   the keychain.
2. A test asserts the access token appears in **no** column of the datastore and in no log record
   emitted during enrollment — asserted over a real enrollment's captured log, not over the
   formatter in isolation.
3. Re-running enrollment for the same institution leaves exactly one live connection, with
   `enrolled_at` preserved and `updated_at` moved (AC-1.4).
4. An abandoned session exits `1` with a message naming the session id, not a traceback.
5. Fixtures recorded from this chunk carry no live token; the redaction happens at record time.
6. An entry is appended to `.prawduct/operator-verification.md` for the hosted-URL flow — a human
   must confirm the printed URL opens a working Link session and that the window confirmation reads
   unambiguously.

**Critic mode:** chunk

### Chunk 03 — The cap, the roster, and retirement

**Type:** cumulative-final

- `connection_cap` joins `Config`, defaulting to 10, range-checked like `history_days`. AC-1.5 calls
  it configuration, not a literal.
- Enrollment refuses past the cap, explains the limit, and lists current connections (AC-1.5).
- `connections list` and `connections retire <id>` — retirement sets `retired_at` and `status`
  without deleting history (AC-1.6, AC-6.5).
- Exit codes fixed per the api-contract norm: cap refusal `1`, absent datastore or unreachable
  aggregator `2`.

**Done-when:**

1. A cap of 1 refuses the second enrollment, exits `1`, and the message lists the live connection
   with the command that would retire it.
2. The count that enforces the cap is taken inside the writer transaction that inserts, and a test
   demonstrates the partial unique index refuses a second live connection for one institution even
   if the count were stale.
3. Retiring a connection preserves its accounts and their history; a test asserts row counts before
   and after are unchanged except for `connections.retired_at` and `status`.
4. A retired institution can be re-enrolled, producing a second connection row (retired connections
   accumulate freely per `data-model.md`).
5. `verify_norms_go_red.py` gains the exit-code cases.

## Verification Strategy

Tests cover the shapes; two things they cannot speak to are called out explicitly.

**The live path** is exercised against the sandbox in every chunk, and Chunk 01's probe gates the
rest. Sandbox Link can be completed programmatically, so Chunk 02's end-to-end test is automated
rather than manual — but the *printed output* an operator reads is not, which is why Chunk 02
carries an operator-verification entry.

🔴 **The granted history window is verified in build step 4, not here**, and this plan makes no
claim about it. AC-1.3a is the record of that boundary. Nothing in this plan should be read as
having confirmed what the aggregator actually grants.

## Governance Checkpoints

1. **After Chunk 01's probe** — if Hosted Link is unavailable, the plan's central decision is
   falsified and Chunk 02 is redesigned before it is built.
2. **Before Chunk 02's first real (non-sandbox) enrollment** — the irreversible moment. The
   requested window, the configured cap, and the institution roster (deployment open question 6.2)
   must all be settled by the owner first.
3. **Chunk 03 close** — cumulative review across the plan.

## Status

- [ ] Chunk 01 — Hosted Link, and the window that must not be guessed
- [ ] Chunk 02 — `bankmachine enroll`, end to end
- [ ] Chunk 03 — The cap, the roster, and retirement

## Context

Base is `develop` at `0528033`, suite green there (400 offline, 9 live deselected). Build step 2
merged as PR #15; `build-plan-connector-v1.md` stays live and unarchived because its change-log
entries are release-pending.
