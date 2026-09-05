# Build vs. Adopt — Investigation and Recommendation

**Date:** 2026-09-05 · **Author:** Claude Code (session 1, pre-scaffold)
**Question asked:** clone-and-run an existing project, fork one, or build clean-slate?
**Answer:** **Build clean-slate in Python.** Harvest `mbafford/plaid-sync`'s Plaid-client layer as
an attributed reference. Do not fork anything.

**Status:** recommendation only. No code written. Project renamed to `MCPlaid` (working name; rename still open). Prawduct scaffold not yet run.

---

## 1. Method

Six candidate repos were **cloned and read at source level**, not assessed from READMEs. This
followed the precedent set earlier in the project, where skim-vetting `copilot-money-mcp` from
its README would have missed the browser-storage scraping in `--live-reads`. That caution was
warranted again — see §3, where two repos' READMEs materially misdescribe their own code.

Two load-bearing technical assumptions were **empirically verified on the target machine** rather
than asserted. See §5.

---

## 2. What the spec actually asks for

Partitioning the ~40 acceptance criteria by whether anyone else has already solved them:

| Layer | Approx. share of spec | Solved elsewhere? |
|---|---|---|
| Plaid plumbing — cursor sync, link tokens, item errors, investments | ~30% | Yes, several repos |
| Bronze/silver datastore, integer cents, lossless rebuild, provenance | ~25% | **No one** |
| Aggregate-first MCP tools w/ freshness stamps + gap warnings | ~25% | **No one** |
| Verification gate, per-account coverage, shared-account rule, Keychain, launchd | ~20% | **No one** |

**This ratio is the whole argument.** Roughly 70% of the specified system is the part nobody has
built, and it is the part that constitutes the actual project. The Plaid API calls are the
commodity 30%. Adopting a codebase to get the 30% means inheriting its storage and interface
decisions in order to fight them across the other 70%.

**Requirements no evaluated candidate satisfies at all:**

1. Encryption at rest for the datastore (added 2026-09-05)
2. macOS Keychain credential storage (AC-10.1)
3. Raw-response bronze layer with provable lossless rebuild (FR-5, AC-11.5)
4. Integer minor units everywhere — every candidate stores Plaid floats (AC-6.2)
5. Freshness stamps and gap warnings on every MCP response (AC-9.2, AC-9.3)
6. `account_rules` as data, incl. the shared-account rule (FR-8)
7. The §7 verification gate in any form
8. launchd scheduling with missed-run recovery (AC-ARCH.2)

---

## 3. Candidates evaluated

### mbafford/plaid-sync — 54★, Python, MIT, active (last commit 2026-08-23)
**Best pipeline half. The reference implementation to learn from.**

Verified present in source:

- `days_requested = 730` as the **default**, not an option (`config.py:15`, `config.py:60`) — AC-1.2
- Real `/transactions/sync` cursor loop with `has_more` pagination (`plaidapi.py:462`,
  `plaid-sync.py:199` `sync_with_cursor`); cursor persisted on the `items` row
- Investments modelled properly: separate `securities`, `holdings`, `investment_transactions`
  tables, securities shared by ID (`transactionsdb.py:53-86`) — FR-3
- `holdings` keyed `(account_id, security_id, date)`, so a position history accumulates — close
  to the intent of AC-3.1
- `balances` keyed `(item_id, account_id, date)` — append-per-day time series, AC-3.1
- Item health: `last_failed_update`, `last_successful_update`, `consent_expiration` on the `items`
  table (`transactionsdb.py:49`) — this is AC-4.2 and AC-4.5 almost verbatim, and it is the single
  most valuable thing in the repo
- `ITEM_LOGIN_REQUIRED` handled explicitly (`plaidapi.py:158`); update-mode relink supported
- Small and readable: 12 files, ~1,895 LOC

Disqualifying as a base, individually fixable but collectively a rewrite:

- **No MCP server whatsoever** — the entire consumer-facing half is absent
- Credentials in a **plaintext INI file**, including `access_token` per account (`config.py`
  docstring) — direct violation of AC-10.1
- **No encryption at rest**
- Storage is effectively schemaless: `create table transactions (account_id, transaction_id,
  created, updated, archived, plaid_json)` — untyped columns, **amounts live inside the JSON
  blob**. No integer cents (AC-6.2), no bronze/silver split (FR-5 wants per-*response* rows with
  endpoint/hash/timestamp; this is per-*entity* JSON), no migrations
- Runs a **local web server** for Link (`webserver.py`, `html/link.html`); spec wants Hosted Link
  with no local server (FR-1)
- No `account_rules`, `manual_imports`, `category_override`, rebuild command, or verification gate

### tomfunk/fungible — 108★, TypeScript, MIT, very active (last commit 2026-09-04)
🔴 **Disqualified on a hard requirement.**

- Exposes **~13 mutation tools over MCP**: `edit_transaction`, `set_transaction_date`,
  `clear_transaction_date`, `clear_edit`, `ignore_transaction`, `add_rule`, `delete_rule`,
  `add_name_rule`, `delete_name_rule`, `tag_transaction`, `toggle_hidden_category`,
  `delete_canvas`, `sync` (`core/tools.ts`, registered in `mcp/create-server.ts`)
- There **is** a `WRITE_TOOLS` set, but it is not a gate. Its only use is
  `if (opts.afterWrite && WRITE_TOOLS.has(name)) opts.afterWrite();` — a UI-refresh hook
  (`mcp/create-server.ts:10`). Nothing blocks a write.
- This is structurally the same pattern rejected earlier for `copilot-money-mcp`
  (19 mutation tools incl. `delete_transaction`). AC-9 admits no exceptions.
- **Zero investments/holdings support** — no `investmentsHoldingsGet` anywhere in the tree.
  Investment accounts are typically the largest unknown in an operator's inventory, and FR-3
  requires them; this is fatal independent of the mutation-tool problem.
- Scope mismatch: 257 files / ~35K LOC spanning TUI, Electron GUI, HTTP API, and an AI "canvas"
  feature, against a spec whose §2 non-goals explicitly exclude a web UI.
- Encryption is AES-256-GCM for *tokens only* (`core/crypto.ts`), key in a plain file at
  `~/.fungible/key` — not Keychain, and the database itself is unencrypted.

*Worth revisiting only as prior art for `days_requested` handling (`core/plaid.ts:65-76`), where
it correctly notes the parameter is ignored on update-mode link tokens.*

### t-rhex/plaid-mcp — 0★, Python
🔴 **README/source mismatch. Do not adopt.**

- README states: *"The server never makes outbound calls except to Plaid's API."*
- Source contains a payments/monetization gate: MPP (Machine Payments Protocol), Tempo
  stablecoin, and Stripe card rails (`src/plaid_mcp/config.py:58-86`,
  `src/plaid_mcp/payments/x402.py`), with egress URLs for `dashboard.stripe.com` and `x402.org`.
- The gate is opt-in and HTTP-transport-only, so this is **not** an active exfiltration finding —
  but an undisclosed payment rail inside a "local read-only finance server" is the same class of
  README-vs-reality gap already caught once on this project.
- Otherwise reasonable on paper (SQLite cache, cursor sync, investments), but architecture is
  live-call-with-cache rather than a durable pipeline.

### elcukro/bank-mcp — 35★, TypeScript
**Wrong architecture.** Cache is **in-memory only**, per-process, with 5–60 minute TTLs; every
request hits the provider API live. No cursor sync, no persistence, no investments. Structurally
cannot produce a 24-month history or a coverage report. Five read-only tools, cleanly done — but
it solves a different problem.

### JosueM1109/personal-finance-mcp — 7★, Python
~1,821 LOC, live Plaid calls per request, no cursor sync, no local pipeline. Does reference
investments endpoints (`server.py:11-13`). Too thin to build on.

### KenTaniguchi-R/ledgr — 12★, TypeScript, very active
Self-hosted personal finance **web application** — an explicit §2 non-goal. ~54K LOC, no MCP
server. Has investments handling and AES-256-GCM (`src/lib/encryption.ts`). Not a fit.

---

## 4. Recommendation and rationale

**Build clean-slate. Python 3.12. `plaid-python` SDK directly.**

**Why not clone-and-run:** no candidate exposes the required tool surface, and the two closest are
each missing a different half of the system. `plaid-sync` has no MCP layer; `fungible` has no
investments and ships write tools.

**Why not fork `plaid-sync` (the only real fork candidate):** a fork keeps roughly 800 of its
1,895 LOC — `plaidapi.py`'s error mapping and sync loop. Everything else is rewritten: the storage
layer is precisely what FR-5/FR-6 forbid, the credential model must be ripped out for Keychain,
and the local Link web server is removed for Hosted Link. A fork whose storage *and* interface
layers are both replaced is a clean-slate build carrying an upstream it can never merge from.

**What to do instead:** read `plaid-sync` as a reference implementation and lift specific functions
under MIT with attribution. Its Plaid error handling and cursor loop encode real production edge
cases and will save meaningful time. Vendor the knowledge, not the dependency. Its `items` health
columns should be copied more or less directly.

**Why Python over Node:** `plaid-python` is first-party, and the encryption-at-rest path is a
prebuilt wheel rather than a native module build (§5).

---

## 5. Empirically verified on this machine

Both checks were run and passed on 2026-09-05 on the target machine, Python 3.12.3 / Apple Silicon.

**SQLCipher — works, no compiler needed.**
`pip install sqlcipher3-wheels` installs a prebuilt wheel. Confirmed:
- SQLCipher **4.12.0 community**
- File header is encrypted — the `SQLite format 3` magic is **absent** from the file
- A known plaintext string written into a table was **not** recoverable from raw file bytes
- A wrong `PRAGMA key` raises `DatabaseError` rather than silently returning garbage

Note: `sqlcipher3-binary` is **not** available for this platform; `sqlcipher3-wheels` is the
package that works. `sqlcipher3` (source) and `pysqlcipher3` exist but require `brew install
sqlcipher` and a build step.

**macOS Keychain — works.** `security add-generic-password` / `find-generic-password` /
`delete-generic-password` round-trip cleanly from the CLI. AC-10.1 is cheap to satisfy.

**Proposed design:** SQLCipher for `data/finances.db`, key stored in Keychain alongside the Plaid
`client_id`/`secret`/access tokens. Backup then becomes a dumb file copy that is already ciphertext
at rest — which is the point, since the backup is the copy that leaves the machine. FileVault
alone does not cover that.

**Known trade-off:** SQLCipher breaks ad-hoc `sqlite3` CLI and Datasette querying, which will be
wanted during the §7 verification gate. Mitigation: a `sync shell` command opening an authenticated
SQLCipher REPL. **Build this in step 1, not step 9** — it is the primary debugging affordance for
every step in between.

---

## 6. Notes on the spec itself

Neither of these blocks the build; both are cheaper to settle before code exists.

**6.1 — `days_requested` is immutable after the fact. Confirmed against Plaid's docs.**
Default 90, maximum 730, and it *"cannot be updated if Transactions has already been added to the
Item."* Raising it later requires `/item/remove` plus a full re-link. §9's warning is correct and
if anything understated: the step-3 single-Item verification is the highest-value hour in the
build, and getting it wrong means re-linking every Item.

**6.2 — AC-11.5's "byte-identical" rebuild needs one qualifier.** A byte-identical re-derivation
from `raw_responses` is achievable only if the derivation logic is versioned and that version is
recorded in the database. Otherwise an upstream Plaid taxonomy change (e.g. to
`personal_finance_category`) silently breaks a gate that is supposed to be a permanent invariant.
Suggested rewording: *byte-identical given a recorded derivation version*.

---

## 7. Open items carried forward

- **Project name.** `MCPlaid` is a working name; rename still open, and now load-bearing — the
  name is fixed by build step 1 (package, keychain service, scheduler label), and the keychain
  service name is the expensive one to change, because altering it after enrollment orphans
  stored access tokens. Candidates considered and declined: assay, assayer, principal, quipu,
  exchequer, corpus, custodian, basis, tally, ledgerdemain, sclerotium.
- **Open questions remain unanswered and gate enrollment.** Engine-layer ones are
  `system-requirements.md` §9. Deployment-layer ones — above all the complete institution
  roster, which blocks build step 3 — belong to the operator's own roster document, which is
  deliberately not in this repository (`docs/deployment-requirements.template.md` §6 states
  where they go and why).
