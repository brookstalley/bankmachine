---
artifact: security-model
version: 1
depends_on:
  - artifact: system-requirements
    file_path: docs/system-requirements.md
  - artifact: data-model
    file_path: .prawduct/artifacts/data-model.md
  - artifact: architecture
    file_path: .prawduct/artifacts/architecture.md
last_validated: null
---

# Security Model — bankmachine

**Scope:** the trust boundaries of a single-operator local pipeline holding financial data, the
controls on each, and — stated plainly — the places where the control is "the operating system" and
that is the right answer.

**Dependency note.** The template's usual product-brief upstream does not exist as a separate
artifact; §0 and §6 of `docs/system-requirements.md` carry that content.

---

## Direction

Norms. These bind future work; departure is a recorded decision, never silent
(`/prawduct:methodology norms`). Ratified 2026-09-07 by the owner. Each describes a control the
code already implements, so no retroactivity decision applies.

- **Secrets live only in the OS keychain, and nothing returns one into a log line, an exception
  message, or a `repr`.** The datastore key, the aggregator secret, and every access token. One
  module imports the keychain library; everything else asks it.
  Why: AC-10.1. A credential in a dotfile, a log, or shell history is a credential in a backup, a
  screen share, and a support paste. Confining the keychain to one module is what makes the rule
  checkable rather than a habit — and *a `KeyError` naming the account is fine; the value never is*,
  because exception text reaches logs by paths nobody planned.
  Status: **steady-state** since 2026-09-10, on the condition the transition set: FR-12's paths
  exist, the shell recipe is gone from `cli/store.py` and the operator docs, and AC-17.6 has a
  mechanism.

  > **Amendment (2026-09-10, owner ruling).** *Statement:* the datastore key — and **only** the
  > datastore key — may leave the keychain by a deliberate, operator-initiated export to a
  > destination the operator names. AC-10.1 and FR-12 (`docs/system-requirements.md` § 6) are the
  > extent of it.
  >
  > 🔴 **What the amendment does not touch is the part a reader will over-generalize.** *Nothing
  > returns a secret into a log line, an exception message, or a `repr`* is untouched and applies
  > with full force to the new paths — AC-17.6 restates it there for exactly that reason. An
  > aggregator secret and an access token remain unexportable, and their reason is unchanged: both
  > have another source (a vendor dashboard, a re-enrolment), so neither needs escrow and neither
  > gets it. The datastore key is the one secret with **no other source at all**, which is what
  > earns it the exception and simultaneously bounds the exception to it.
  >
  > *Why:* the rule was being routed around rather than obeyed. Nothing in the product printed the
  > key, so the product's own instruction pointed at a shell recipe that puts it in history — the
  > rule intact on paper, broken in practice, on the one secret whose loss is unrecoverable.
  >
  > *Retroactivity:* none owed — no shipped code exported a key when this was written.
  >
  > *Transition closed 2026-09-10.* `store key export | verify | import` shipped, `_keychain_recipe`
  > was deleted rather than left unused, and every surface that mentions the key now names the
  > commands. The interim rule — keep the recipe, because withdrawing it would leave the operator
  > with no path at all — is discharged and does not apply to anything.
  >
  > 🔴 **What enforces the untouched half is new and is worth naming, because the old mechanism
  > could not see it.** `tests/test_logging_setup.py` proves no secret reaches a *log*; it is blind
  > to a caller that legitimately *prints* one, and FR-12 created the first such caller.
  > `tests/cli/test_store_key_commands.py` covers that gap per verb — the key in no log file, no
  > stderr and no refusal message — and the log-absence assertions carry a negative control, because
  > a check reading a file that was never written passes forever.

- **Log redaction happens at the formatter, over-redacts by design, and is keyed to credential
  shape rather than to any one vendor's token prefix.**
  Why: at the formatter means it covers records this project did not write, including third-party
  libraries and exception text. Over-redaction is a deliberate trade — a filesystem path containing
  a 32-character segment is blanked along with the tokens, costing some legibility, and the
  alternative of requiring high entropy first trades that back for the chance of a real token
  slipping through. Provider-agnostic matters most: **a rule keyed to one aggregator's prefix would
  silently stop redacting the day a second is added**, a failure with no symptom until the leak.
  Status: steady-state.

- **No tracked file carries a credential-shaped string, and the ignore rules cover data, logs, and
  credential paths.** The one exemption is a per-line `credential-shape: test vector` declaration.
  Why: AC-10.2, and this repository is a general-purpose tool that may be published. The guard is
  deliberately separate from the roster leak guard because **neither subsumes the other**: that one
  hunts institution names supplied by `deployment/`, so a stray secret matching no roster token walks
  past it. The exemption is per-line and hand-written rather than a file skip list, because a skip
  list exempts the *next* secret to land in that file and nobody decides anything; a marker appears
  in the diff of whoever adds it.
  Status: steady-state.

- **The aggregator's API is the only network destination.** No telemetry, no analytics, no
  third-party error reporting. Nothing outside `connector/` may reach a network transport.
  Why: AC-10.4, and it is a confidentiality requirement rather than a cost one — which is why it is
  not revisitable if a free tier appears. It also does more architectural work than any other control
  here: it rules out every hosted service that would otherwise be the cheap answer to monitoring,
  alerting, and log aggregation, and that constraint is what shapes `observability-strategy.md`.
  **Known limit of the mechanism, stated so it is not mistaken for coverage:** the import scan cannot
  see a subprocess shelling out to `curl`, nor a dependency phoning home on its own. Those remain
  judgment calls under this same norm.
  Status: steady-state.

---

## Threat model, stated first

Getting this right determines whether every control below is proportionate or theatre.

**Who this system defends against:**

| Adversary | Reachable how | Control |
|---|---|---|
| **Someone holding a backup copy** of the datastore | Cloud sync, external drive, an old Time Machine volume | 🔴 The **primary** threat. Page-level encryption; the backup is ciphertext without further work |
| **Someone with the repository** (it may be published) | GitHub, a clone, a fork | No roster, no credentials, no operator identity in any tracked file — enforced on every push |
| **A process on this machine running as another user** | Filesystem | OS file permissions, made real by `os.umask(0o077)` at the single CLI entry point (`cli/__init__.py`, which every command including `mcp` passes through): the datastore, its WAL and shm, the plaintext log, every backup copy and the directories holding them are created owner-only. The key is in the keychain, not on disk |
| **The network** | — | 🔴 **Not reachable.** The MCP server opens no sockets; the only outbound destination is the aggregator. This says nothing about what the *client* sends onward — see the row below |
| **Whoever writes the text in a transaction** — a counterparty choosing its own descriptor, or anyone who can move $0.01 to the operator | The aggregator relays descriptions and merchant names verbatim; the MCP surface hands them to a model that holds tools far beyond this server | 🔴 Structural first: the surface is **read-only**, so the worst case is a wrong answer or a nudged agent, never a moved dollar. Stated second: the server's instructions and its envelope reference name `description`, `merchant`, `account` and `institution` as third-party text to be quoted and never followed, and say that no instruction, URL or credential request appearing in a row comes from the operator or from this server. 🔴 **This risk does not exist against sandbox fixtures. It begins with the first real account** |

**Who it does *not* defend against, deliberately:**

- **The operator.** They own the accounts, the machine, and the keychain. There is no privilege
  boundary to draw inside a single-user tool, and inventing one would be theatre.
- **An attacker with the unlocked machine and the operator's session.** They have the keychain. This
  is the OS's boundary — FileVault and the login password — and it is the right layer for it.
- **The model the MCP client is wired to.** 🔴 **Every tool call hands merchant names, amounts,
  balances, account names and masks to whatever model that client runs** — hosted or local, under
  that provider's terms rather than this project's. That is the product working as designed, and it
  is also the single place the "never leaves the machine" property stops holding. The control is the
  operator's choice of client, so it is stated rather than enforced.
- **A malicious aggregator.** Trusted by construction: it is the data source. What *is* defended is
  the aggregator being *wrong* — see "Data integrity" below, which is where this product's real
  paranoia lives.

🔴 **The distinctive risk here is not breach. It is silent wrongness.** A system that leaks nothing
and reports confidently incorrect financial data has failed at its actual job. That threat is handled
across `data-model.md` (constraints in the database), `observability-strategy.md` (loud degradation),
and §7's verification gate — not here. This document covers confidentiality and integrity of the
data at rest and in transit.

---

## Authentication

🔴 **There is none, and that is the design.**

This is a single-operator tool. The authentication boundary is **the macOS user account** — login
password, FileVault, and the keychain's own unlock. Adding an application-level password would store
a second credential to protect data already protected by the first, and the operator would keep it in
the same keychain.

**What authenticates instead, per surface:**

| Surface | Who may use it | Enforced by |
|---|---|---|
| CLI (`bankmachine …`) | Anyone in the operator's OS session | Filesystem + keychain ACL |
| MCP server (stdio) | The process that spawned it — the MCP client | 🔴 **Process ancestry.** No socket, no listener, nothing to authenticate to |
| Datastore file | Anyone holding the 256-bit key | SQLCipher |
| Aggregator API | This installation's client credentials | Keychain-held secret |

**Why the MCP transport decision is a security decision.** Local stdio only, confirmed 2026-09-05 and
recorded because it had previously been an *unexamined default rather than a decision*. It is the one
architectural choice that would have been expensive to reverse: a remote transport would require
authentication, authorization, TLS, and rate limiting, and would expose a financial datastore to a
socket. AC-10.5 — *the MCP server opens no network sockets and reads only its own datastore file* —
**holds**, and holds structurally rather than by policy.

---

## Authorization

**Within the product: none, for the same reason.** One operator, one role, everything visible. There
are no per-entity access rules to write.

**The one authorization boundary that does exist is between the two processes**, and it is enforced
by the operating system rather than by application logic — which is what makes it hold. From
`architecture.md` § Direction (ratified norms; not restated here, only cited):

- Every **writable** handle comes from one writer factory that takes an exclusive `flock` before it
  returns. Writer-role membership is a property of how a handle was constructed, not a list someone
  has to remember to keep current.
- Every **read-role** handle opens `mode=ro` at the file, with `PRAGMA query_only=ON` as a second
  layer, and never falls back to a writable handle.

🔴 **Why `mode=ro` and not `query_only` alone — this is a security control, not a concurrency one.**
`PRAGMA query_only=OFF` re-enables writes on a read-write handle *(measured)*, and `sync shell` exists
precisely to run operator-supplied SQL — it is the surface that can type it. Under `mode=ro` the
identical sequence still fails *(measured)*, because the refusal lives in the file handle rather than
in a session flag. §5's mandate is therefore structural rather than a convention every future tool author
remembers.

🔴 **The 2026-09-10 amendment narrowed that mandate and left this control untouched, deliberately.**
§5 read *"Read-only. No mutation tools. No exceptions."* and now reads read-only over everything the
aggregator produced, with one class of write permitted — to sidecar tables bankmachine itself
maintains (FR-11). What did **not** change is that the MCP server process holds no writable handle:
the recommended write path is out-of-process, so the refusal stays in the file handle rather than
becoming a rule about which tool may call which function. AC-16.10 states that as a criterion
precisely so a build cannot satisfy the amendment by opening the handle this paragraph is about.

### API-design failure modes (OWASP API Top 10)

Assessed rather than skipped, because `exposes_programmatic_interface` is recorded. Most do not apply,
and *why* they do not apply is the useful part:

| Failure mode | Applies? | Reasoning |
|---|---|---|
| **BOLA / object-level authz** (API1) | **No** | One operator owns every object. There is no "another user's transaction" to leak |
| **Broken authentication** (API2) | **No** | Nothing to authenticate to; process ancestry is the boundary |
| **Broken object property level authz** / mass assignment | 🔴 **Yes, once FR-11 ships** | It was **No** while the surface was read-only — no request binding, nothing to over-permit. § 5's 2026-09-10 amendment lets an agent write annotations, and an `annotate` call **is** a request binding. The mitigation is not field validation: it is that the write path reaches exactly one table, and that the server process still holds no writable handle (AC-16.9, AC-16.10) — the same argument `mode=ro` makes one layer down. Until FR-11 builds there is still no mutation tool and no binding |
| **Unrestricted resource consumption** (API4) | 🔴 **Yes** | A caller can drive cost. Mitigated by the ~500-row hard cap and aggregate-first design (AC-9.1); a cursor narrows a request rather than lifting the cap, so each page stays bounded by it |
| **Improper inventory management** (API9) | 🔴 **Yes** | A forgotten tool is a real risk. Mitigated by the declared surface inventory in `api-contract.md` |
| **Unsafe consumption of third-party APIs** (API10) | 🔴 **Yes** | The aggregator's responses are unowned input in two distinct ways. The aggregator being *wrong* is "Data integrity" below. The aggregator being *right* about text an outsider wrote — a descriptor is chosen by the counterparty, not by the bank — is the transaction-text row in the threat model above |
| **Excessive data exposure** | Partly | The consumer is an LLM with finite context; returning less is a *performance* requirement too |

Each row marked **Yes** is handled in `api-contract.md` § Security, not duplicated here — except the mass-assignment row, whose handling is FR-11's own criteria and does not exist yet, because neither does the tool.

---

## Data Privacy

### Classification

| Class | Examples | Handling |
|---|---|---|
| 🔴 **Secret** | Datastore key, aggregator client secret, access tokens | Keychain only. Never on disk, never in a log, never in a `repr` |
| 🔴 **Sensitive** | Transactions, balances, holdings, account numbers, institution roster | Encrypted at rest. It leaves this machine two ways, both deliberate: an encrypted backup copy, and every MCP tool answer — the client is the egress, and what it is handed goes wherever its model runs |
| **Restricted** | The operator's identity, machine name, institution names | Never in a tracked file — the repository may be published |
| Public | The engine's source code | The point of the layering rule |

### Secrets: the keychain seam

🔴 **AC-10.1 — the aggregator credentials, all access tokens, and the datastore encryption key live in
the OS keychain**, accessed at runtime. Never in the repo, never in a plaintext dotfile, never in
shell history, never in a log line — **with one exception, amended in on the owner's ruling of
2026-09-10: a deliberate, operator-initiated export of the datastore key to a destination the
operator names.** Its extent is FR-12; § Direction above carries the amendment and the interim rule
that holds until FR-12 ships.

`src/bankmachine/secrets.py` is **the only module that imports `keyring`**. Everything else asks it, so
the one part of the system that is genuinely painful to port lives behind a single interface. Nothing
in it returns a secret into a log line, an exception message, or a `repr` — *a `KeyError` naming the
account is fine; the value never is.*

Two properties worth recording:

- **The key in the keychain *is* the key.** SQLCipher takes a 256-bit raw key as 64 hex characters;
  passing it raw rather than as a passphrase skips SQLCipher's KDF, so **there is no derivation whose
  parameters could drift** between the process that created the datastore and the one that opens it.
- **Malformed keys are rejected loudly, and the value is never in the message.** A malformed key is
  still a key, and exception text reaches logs.
- **The three missing-secret errors are deliberately distinct types** because their remedies share
  nothing: 🔴 **a datastore key cannot be recovered once lost**, while an aggregator secret is always
  re-readable from the vendor's own dashboard. See "Backup & Recovery" in `operational-spec.md` — this
  is the sharpest operational hazard in the product. The third is a **connection's access token**,
added with enrollment: it cannot be pasted from anywhere, and its only remedy is to re-enrol that
one institution — an operator action against a single connection rather than a credential to
supply.

`connections.credential_ref` holds **a keychain lookup handle, never a token** (`data-model.md`).

Hosted enrollment added three more credential-shaped values, all enforced in code before they were
written down here — recorded now so the classification matches what the mechanism already treats as
secret. A **link token** authorizes an enrollment session against this product's account; a **public
token** is single-use but mints a durable Item, so it is a credential for the moment it exists; an
**access token** reads one connection forever. All three ride response bodies whose endpoints are
declared `issues_credential`, so `FetchedResponse` refuses to exist for them and none can reach the
append-only archive. The **hosted enrollment URL** is deliberately NOT in this class: the operator
has to read it off their terminal to use it, and a session someone is being asked to complete is not
a secret being kept from them.

### Encryption at rest

🔴 **AC-ARCH.5 — SQLCipher Community Edition, page-level AES-256 with per-page HMAC**, the whole file
encrypted including the header, so a file-copy backup is ciphertext without further work.

Empirically verified on this machine 2026-09-05: the prebuilt wheel installs with no compiler, the
`SQLite format 3` magic is **absent** from the file, a known plaintext is **not recoverable** from raw
bytes, and a wrong key **raises rather than returning garbage.** AC-ARCH.5 requires that last property
to stay verified by a test, not by memory.

**The WAL and shm files are encrypted too — checked**, because an unencrypted WAL would have traded
AC-ARCH.5 away for AC-ARCH.7's concurrency.

🔴 **Why not field-level encryption — the rejection is load-bearing.** Application-level AES on
sensitive columns destroys the SQL queryability the aggregate-first design depends on: no range scans
on date, no `GROUP BY` category, no `SUM` in SQL, no useful indexes. And the workaround —
deterministic or order-preserving encryption to restore indexability — **leaks equality and ordering
patterns that frequency analysis over a personal transaction set substantially defeats.** It also
leaves schema, indexes, row counts, and freed-page residue structurally visible, which page encryption
does not. *(Plain SQLite was the v1 requirement before this decision superseded it; it leaves the
backup in plaintext, and the backup is the copy that leaves the machine.)*

### Data in transit

🔴 **AC-10.4 — no telemetry, no analytics, no error reporting to third parties.** The **only** network
destinations are the aggregator's API hosts, over HTTPS, read-only endpoints only. Any other
destination is a finding, not a feature.

This constraint does more architectural work than any other in this document: it rules out every
hosted service that would otherwise be the cheap answer to monitoring, alerting, and log aggregation.
See `observability-strategy.md`.

**Polling, not webhooks** (v1) — webhooks require a publicly reachable endpoint, which this machine is
not and should not become. A clean seam is left; the endpoint is not.

### Logs

🔴 **AC-10.3 — logs redact access tokens and account numbers. Account masks (last 4) are acceptable.**

Redaction happens **at the formatter**, so it applies to every record regardless of which code emitted
it. Two rules: named sensitive keys (`access_token`, `client_secret`, `api_key`, `password`, `token`,
`key`, …) and opaque high-entropy strings.

🔴 **They over-redact, and that is the chosen direction.** A filesystem path containing a 32-character
segment is blanked along with the tokens, which costs some log legibility. The alternative — requiring
high entropy before redacting — trades that back for the chance of a real token slipping through.

The rules are deliberately **provider-agnostic**: a redaction rule keyed to one aggregator's token
prefix would silently stop redacting the day a second one is added. Account masks survive on purpose —
a fully redacted number would make logs useless for the operator.

### Repository scope — the control that is not about the datastore at all

🔴 **This repository is a general-purpose tool that may be published.** No operator's roster,
institution name, account detail, balance, operator name, or machine name may appear in a tracked
file. The roster lives in the gitignored `deployment/` directory;
`docs/deployment-requirements.template.md` carries its shape.

Two automated checks apply the rule at **two different scopes**, stated separately because they are
not the same guarantee (AC-0.4):

1. **Source and schema roots** — `tests/preferences/test_no_provider_identity.py`. No roster name
   reaches the code. This is what a build regression trips.
2. **Every commit being pushed** — `tests/preferences/check-no-personal-data.sh`, wired into
   `.githooks/pre-push` and run from the suite. Over the *commits*, not the working tree.

🔴 **The second is wider on purpose, and the reason is a real incident: the exposure this project
actually had was documentation sitting in already-pushed history behind a clean tip** — which a
worktree or tip-only check reports clean. Three doc paths reached a remote. The history was rewritten
rather than accepted, because the repository was three pushed commits old and the cost never gets
lower.

**Matching is by explicit per-entry tokens from the roster config, on word boundaries — never by
matching institution labels directly** (AC-0.3). Matching labels fails both ways: a label is the
institution's own spelling, so a shortened token in code slips past a literal match, while tokenizing
a label collides with unrelated legitimate text. 🔴 **A token that cannot match is worse than a missing
one, because it still counts toward a reassuring total** — so tokens are validated at load and
**rejected loudly**, never silently normalized into something unmatchable.

🔴 **Both fail closed.** An unreadable token file, a rejected regex, a missing script — all abort.
*A check whose only bad-news channel is the absence of output cannot report that it stopped checking.*
The guard carries a positive control and a not-scanning-nothing assertion, because a scan over zero
files passes forever.

A checkout with no `deployment/` directory has no roster to leak and passes with a note — which is what
makes the guard itself publishable.

### Retention

- **Raw responses: kept indefinitely** by default — the system of record for the rebuild guarantee.
  *(Pruning is `docs/system-requirements.md` §9 open question 1.)*
- **Transactions: never hard-deleted.** Removal is a soft delete with a timestamp (AC-2.2).
- **Retiring an account or connection never deletes history** (AC-1.6, AC-6.5).
- **Secrets: deleted on request** — `delete_datastore_key`, `delete_plaid_secret`.

There is no automatic expiry anywhere. For a personal financial record, deletion is the loss mode, not
the safety mode.

### Regulatory

🔴 **`regulatory: []` — and that is a recorded finding, not an unfilled field.** No regime binds a
personal, single-user, read-only tool: this is not a financial institution (GLBA), it stores no card
PANs (PCI-DSS), and it holds no health data. **The controls in this document are self-imposed, which
makes them requirements rather than compliance.** Recorded so a later reader does not mistake absence
for oversight.

---

## Data integrity — where this product's paranoia actually lives

The aggregator is trusted as a *source* and distrusted as an *authority*. Controls:

- **Verbatim bronze layer.** Every response persisted before normalization (AC-5.1), so a derivation
  bug is a re-run rather than a re-fetch and lineage back to source is preserved.
- **Constraints in the database, not only in code** — `typeof()` on money, `GLOB`/`LIKE` on dates and
  instants, provenance CHECKs, partial unique indexes for idempotency. These survive a `sync shell`
  session typing raw SQL, which a Python-side check does not.
- **Sign normalization is the connector's job** — aggregators disagree with each other, and several
  report a card balance as a positive amount owed (`data-model.md` § Constraints).
- **Foreign-API verification before wrapping.** Vendor docs lag code and training data lags further;
  the shape is read or probed, not assumed.

🔴 **AC-10.6 — sandbox vs. production is an explicit config flag, logged loudly at every startup.**
Inferring it from which credentials happen to be present was rejected as silent and easy to get wrong.
**Syncing fixture data into the real datastore must be hard to do by accident** — and the reverse,
real data into a test store, is the disclosure version of the same mistake.

---

## Abuse Prevention

There are no untrusted users, so the realistic failures are **operator error, supply chain, and
backup handling** — and pretending otherwise would be the over-engineering the template warns about.

| Risk | Control |
|---|---|
| 🔴 **Backup leaves the machine in plaintext** | Page-level encryption makes a file copy ciphertext. This is the primary control in the whole document |
| 🔴 **Datastore key lost** | Unrecoverable by design. Its own error type; the operational spec owns the remedy |
| **Wrong environment** | Explicit flag, loud banner at every startup (AC-10.6) |
| **Roster or identity pushed to a remote** | Fail-closed pre-push guard over commits, plus a source-root test |
| **Secrets in shell history** | Secrets are set through the keychain seam, never passed as CLI arguments |
| **Supply chain** | `uv.lock` pins the dependency graph. The aggregator SDK and `sqlcipher3-wheels` are the surfaces that matter |
| **A tool that mutates** | Structurally impossible: `mode=ro` at the file handle, and no mutation tool exists |
| **Cost/resource exhaustion by a caller** | ~500-row hard cap; aggregates computed in SQL |

**Rate limiting: not applicable inbound** (no listener, one local caller). *Outbound* rate-limit
errors from the aggregator are handled per connection as a degraded state, never as a crash (AC-4.1).

**Input validation** applies at exactly two places, and both are unowned surfaces: the aggregator's
responses, and manual-import files. 🔴 **Import adapters are verified against real exported sample
files, not hand-written fixtures that encode our assumptions about the format** (AC-7.3).

---

## Verification

Security properties that must stay checked by a test rather than by memory:

| Property | Evidence |
|---|---|
| The datastore file is ciphertext | Known plaintext not recoverable from raw bytes; `SQLite format 3` magic absent (AC-ARCH.5) |
| A wrong key raises rather than returning garbage | Verified 2026-09-05 |
| WAL and shm are encrypted too | Verified 2026-09-05 |
| A read-role handle cannot write, even after `query_only=OFF` | `tests/store/test_connection_norms.py` |
| No roster identity in source or schema | `tests/preferences/test_no_provider_identity.py` |
| No roster or operator identity in any pushed commit | `tests/preferences/check-no-personal-data.sh` + `.githooks/pre-push` |
| Secrets never reach a log line | `tests/test_logging_setup.py` — token-shaped values, labelled credentials whatever their shape, exception messages, and an account number keeping only its last four; plus a negative control that ordinary prose survives |
| The environment banner is loud in both sandbox and production | `tests/test_logging_setup.py` (AC-10.6) |
| An unusable log directory does not take the process down | `tests/test_logging_setup.py` |
| `.gitignore` covers data, logs, and credential paths | `tests/preferences/test_no_credentials_tracked.py` — `git check-ignore` over a representative of each AC-10.2 clause, plus a negative control that `.env.example` and `pyproject.toml` stay tracked (rules that ignored everything would pass the positive half) |
| No tracked file carries a token-shaped string | Same file — a fresh `git ls-files` scanned for aggregator access-token prefixes, 64-hex datastore-key runs, and labelled credentials with a real value |

**AC-10.2 is discharged.** The credential guard is deliberately a **separate check from
`check-no-personal-data.sh`**, and neither subsumes the other: that script hunts *roster tokens*
supplied by `deployment/`, so a stray secret matching no institution name walks past it; this one
knows nothing about the roster and looks only at credential **shape**. A roster name is not
token-shaped, and a leaked access token names no institution.

🔴 **Its one exemption is a per-line declaration, not a skip list**, and the distinction is
load-bearing. A skip list exempts a *file*, so the next real secret to land there is exempt too and
nobody decides anything. The marker `credential-shape: test vector` exempts exactly one line, is
written by hand, and appears in the diff of whoever adds it — so exempting a real credential is an
act someone performs and a reviewer can see. It is used once today, on the redaction test's own
fixtures, which must carry real credential shapes or they prove nothing. A test asserts the marker
does not spill onto neighbouring lines; it caught that exact bug while being written.

The MCP-surface controls are partly verifiable now that the first slice exists. **Verified:** the
surface is read-only by construction — every tool reads through a `mode=ro` handle, and a test
asserts the tool inventory contains no mutating verb. `query_transactions` is hard-capped at the contracted ~500
rows, refused above the ceiling rather than trimmed to it -- so an accepted
request is known not to have been capped. **Cursor pagination does not widen that.** A cursor
narrows the request rather than lifting the cap, so every single call stays bounded by the same
~500; walking a large result set costs one bounded call per page, which is what a caller could
already do by narrowing the window by hand. A cursor is also unsigned by design, and reaches only
rows the request's own filters already admit -- it names a position in a result set, never a
predicate of its own. **Not yet verifiable:** the caps and inventory of the six tools that are specification only,
and whether an analyst client actually *reads* the warnings every answer carries (queued as
VRF-004 — no test can settle it).
