---
artifact: build-plan
version: 1
scope: connections-reauth
branch: feature/connections-reauth-update-mode
critic_mode: cumulative-final
closes: brookstalley/bankmachine#67
depends_on:
  - artifact: discovery
    file_path: .prawduct/artifacts/discovery-relink-account-identity.md
  - artifact: api-contract
    file_path: .prawduct/artifacts/api-contract.md
  - artifact: api-notes-plaid
    file_path: .prawduct/artifacts/api-notes-plaid.md
governed_by:
  - artifact: api-contract
    dispositions:
      - "§ Direction: *the CLI exit codes `0`/`1`/`2` are a contract and the 1/2 split is not collapsible* → conforms. `connections reauth` returns codes directly, as `cmd_list` and `cmd_retire` already do in this module, rather than raising into the `EnrollmentError` hierarchy. An abandoned session and a refused re-link are both *ran and found a problem* → `1`; a datastore or keychain that could not be read is *could not run* → `2`, raised as the existing `StoreError` / `SecretsError` and mapped where they already are"
      - "§ Direction: *the MCP surface is read-only over everything the aggregator produced* → untouched; this plan adds no tool and no mutation. The only MCP-adjacent edit is the `degraded` guidance string in `mcp_resources.py`, which is prose naming a remedy"
  - artifact: security-model
    dispositions:
      - "§ Direction: *secrets live only in the OS keychain, and never reach a log, exception or `repr`* → conforms. Update mode needs the connection's access token as a *request argument*; it is read from the keychain at the call and never stored, printed, or logged. `LinkToken` already carries `token` and the new update-mode path reuses that type, whose redaction is unchanged. 🔴 A new log line naming the item id is deliberately NOT added — a production item id is redacted by shape, so the line would arrive half-blank (`enroll.py` records the same reasoning)"
      - "§ Direction: *the aggregator's API is the only network destination* → conforms; every call added here goes through `PlaidClient`"
  - artifact: architecture
    dispositions:
      - "§ Direction: *every writable handle comes from the one writer factory* → conforms; `connections reauth` writes through `writer_connection` + `transaction`, exactly as `cmd_retire` does"
---

# Build Plan — repairing an expired login without minting a second Item

## Problem

**AC-4.3** asks for a repair command that re-authenticates a broken connection *without losing its
history or cursor*. It is unbuilt, and its own note in `docs/system-requirements.md` records the
recovery that exists instead: **re-run `bankmachine enroll` and pick the same institution.**

🔴 **That recovery is data-destroying, and it is silent.** Reproduced end to end on the sandbox
store on 2026-09-10 (connection 1):

1. `enroll` converges the row in place — same `connection_id`, cursor cleared.
2. The new Item's roster issues new `source_account_id`s. `source_persistent_account_id` is NULL, so
   `_match_account` matches nothing and inserts **14 new account rows**; the 14 originals go
   `no_longer_reported`.
3. The re-fetched window lands on the new accounts. **390 transactions became 784.**
   `money_summary` for 2026-06 returned `outflow_minor_units: 2229892` against a truth of
   `1114946` — exactly 2×, **with no warning naming it.**

`.prawduct/artifacts/discovery-relink-account-identity.md` establishes that this is the *default*
outcome, not an edge case: `persistent_account_id` is populated at three institutions only (depository
accounts; the aggregator's Accounts API reference names them). **NULL is the rule.** Lineage exclusion (#68) cannot cover it —
`_coverage` groups by `(account_id, lineage_id)` and the two generations differ in `account_id`.
"Retire first, then re-enroll" takes the same path.

So the product's only documented answer to its most common production event
(`ITEM_LOGIN_REQUIRED`) is the operation that doubles every money figure it will then be asked
about.

## What success looks like

- `bankmachine connections reauth <id>` prints an update-mode hosted URL, waits for the operator to
  complete it, and returns the connection to `active` — **with its Item, cursor, granted window,
  accounts and transactions untouched.** The next `sync run` continues from the stored cursor.
- `bankmachine enroll` can no longer repoint a live connection at a new Item by accident. The
  deliberate re-link AC-1.2 requires for a wider history window stays reachable, behind a flag that
  names what it costs.
- No surface still tells an operator that re-running `enroll` is the answer to an expired login.

## Out of scope

- **An identity fallback for NULL `persistent_account_id`** — the discovery's second open question
  (*what identity should stand in where the aggregator gives none?*). `_match_account` deliberately
  refuses to guess, and its reason is sound: a false merge of two real accounts is silent and
  unrecoverable, while a duplicate is at least visible. Update mode prevents the second generation;
  it does not give the store a way to reconcile one that already exists. **#91.**
- **Disclosing an already-doubled store.** Nothing today says "these two rows look like one account
  seen twice", and `account_no_longer_active` misattributes it to closure. Also **#91**.
- **Repairing the sandbox store that is currently doubled on purpose.** It is the only reproduction
  and is kept until the fix lands (handoff notes).
- `ITEM_LOCKED` and the other FR-4 states. Update mode is the remedy for `ITEM_LOGIN_REQUIRED`; a
  locked Item needs the operator's bank, and `errors.py` already keeps the two apart.

## The decisions this plan rests on

> **[DECISION 1: completion in update mode is the Item's error clearing, not a public token.**
> `LinkSession.finished` is *derived* from `public_token` (`connector/__init__.py:560`),
> deliberately, so a stored flag and a token cannot disagree. **Update mode mints no public token** —
> the Item already exists and nothing is exchanged — so `_await_completion` would poll a session
> that never reports finished. `connections reauth` therefore polls `/item/get` and treats *the Item
> no longer reporting an error* as completion. **| Why:** it is the state the command exists to
> reach, read from the endpoint that owns it, rather than a second completion marker inferred from
> the Link session. **| Rejected:** adding a `finished` flag to `LinkSession` for update sessions —
> that reintroduces exactly the two-values-that-can-disagree its docstring exists to prevent.
> **| Owner may veto** — the alternative is to read a completion marker out of `link_token_get`'s
> session entry, which is unverified for update mode and would be a second way to answer one
> question. **]**

> **[DECISION 2: `enroll`'s guard is a refusal at the point of repointing, not a warning.**
> The harmful state is *a live connection row about to be repointed at a different Item*, which
> `_record_connection` already computes as `replaced_the_item`. The guard sits there and refuses,
> releasing the fresh Item at the aggregator; `--relink` is the deliberate override. **| Why:**
> `learnings.md` § *Guarantees by construction* — a warning is a case an operator can miss, and the
> cost of missing it is silent and unrecoverable. A refusal on the branch that would commit the
> duplication has no case to fall outside of. **| The price, stated:** the institution is not known
> until the operator has completed Link in a browser, so the refusal necessarily lands *after* a
> completed session. That session is spent for nothing. Chunk 02 therefore also puts a signpost
> *before* the URL, where it costs nothing, so the refusal is the backstop rather than the first
> thing an operator meets. **| Owner may veto** — the alternative is warn-and-proceed, which was
> offered and declined on 2026-09-10. **]**

> **[ASSUMPTION: update mode preserves `account_id`.** The aggregator documents update mode as the
> repair for expired credentials and says to re-read account ids afterwards rather than assume they
> are stable — which stops short of a guarantee. **This plan does not rest on the assumption; it
> asserts the half it can.** `connections reauth` compares the `item_id` reported by `/item/get`
> after the session against the `source_connection_id` on the row, and **refuses loudly** if they
> differ, because that is the duplication path arriving by another door. Whether the *accounts*
> beneath an unchanged Item keep their ids is not assertable from one call and is enqueued as
> operator verification. **If it turns out they do not, #67 reduces the frequency of the
> duplication without eliminating it and #91's identity fallback is needed regardless** — which is
> the discovery's own conclusion, unchanged by this build. **]**

## Chunk 01 — the update-mode link token, and `connections reauth`

**Delivers**

1. `PlaidClient.link_token_create` accepts `access_token: str | None = None`. When given, the
   request carries it and `update=LinkTokenCreateRequestUpdate(...)`, and `products` is omitted —
   an update-mode session re-authenticates an Item rather than choosing products for a new one.
   Keyword-only already, so this is non-breaking *(verified: plaid 44.0.0's
   `LinkTokenCreateRequest.openapi_types` carries `access_token` and `update`)*.
   🔴 `history_days` stays **required and defaulted-never** for the new-Item path. Update mode does
   not re-request a window — the grant belongs to the Item that persists — so the update path must
   not send one, and must not be reachable by a caller who simply forgot the window.
2. `cli/hosted_link.py` — the hosted-session machinery both commands need, extracted rather than
   duplicated: the URL invitation, the poll interval and wait floor, and
   `await_hosted_session(poll, *, timeout_seconds)` returning `T | None`. Generic over *what counts
   as finished*, because that is the one thing the two commands do not share. It raises nothing;
   each caller owns its own timeout message and exit code.
   🔴 This extraction is what makes the feature possible at all: `enroll.py` already imports from
   `connections.py`, so `connections.py` cannot import from `enroll.py`.
3. `connections reauth <id>` in `cli/connections.py`, with `--timeout` on the same floor `enroll`
   uses. It:
   - refuses an unknown or retired connection, naming what to run instead;
   - reads the connection's access token from the keychain (a missing one is the state where the
     Item is unreachable — it says so and names `connections retire`);
   - mints the update-mode token, prints the URL, waits;
   - 🔴 on completion, calls `/item/get` and **compares `item_id` against the row's
     `source_connection_id`, refusing if they differ** — see the ASSUMPTION above;
   - archives the `/item/get` response through `apply_response` with this connection's id, as the
     enrollment path does, so FR-5's raw preservation holds on the repair path too;
   - clears `status`, `last_error_code` and `last_error_at` in one transaction, and writes
     **nothing else** — no cursor delete, no `granted_history_days` reset, no `enrolled_at` change.
     That absence is the requirement.

**Tests** (`tests/cli/test_connections_reauth.py`, `tests/connector/test_client.py`, `tests/connector/test_sandbox.py`)

- 🔴 **The preservation guarantee, asserted by what does not move.** A degraded connection with a
  stored cursor, a measured `granted_history_days`, an `enrolled_at` and accounts is reauth'd; every
  one of those is unchanged afterwards and `status` is `active`. Written from the failure's point of
  view (`learnings.md` § *Write a guard from the failure's point of view*): the fixture is built so
  that a `delete(sync_state)` or a `granted_history_days=None` **reddens it**, which a fixture with
  no cursor and no measured window could not.
- The Item-id guard: an `/item/get` reporting a *different* `item_id` is refused, the row is left
  degraded and untouched, and the exit code is `1`. Its negative control: the matching id proceeds.
- The completion predicate: a fake whose `/item/get` reports `ITEM_LOGIN_REQUIRED` on the first
  poll and no error on the second returns success — **the answer must change between calls**, or
  the test cannot tell polling from a single read (the `signs.caveats` instance in the same
  learning).
- Abandonment: the error never clears within the timeout → exit `1`, a message naming
  `connections reauth <id>` again, and **no write to the row at all**.
- Unknown id, retired connection, and absent keychain credential — one case each, each asserting
  the remedy it names.
- `link_token_create` builds an update-mode request that carries the access token and **no
  `products`**, and a new-Item request that carries `products` and **no `access_token`** — two
  cases, because one would pass against either implementation.
- `-m sandbox`: mint an Item via `/sandbox/public_token/create`, exchange it, and assert an
  update-mode `link_token_create` comes back with a `hosted_link_url`. 🔴 This is the one thing no
  fixture can establish — that Hosted Link is available in update mode on this account — and it is
  the assumption the whole feature rests on.

**Operator verification** — enqueued in `.prawduct/operator-verification.md`, `Visual change: yes`:
drive `/sandbox/item/reset_login` against an enrolled sandbox connection, complete the update-mode
URL in a browser, and record (a) that the connection returns to `active`, (b) that `sync run`
continues from the cursor rather than re-fetching the window, and (c) **whether the account ids are
re-issued** — the discovery's open question, which a browser-completed sandbox session can give a
*signal* on even though only production can settle it.

**Done when:** the suite is green, the new guards are verified to go red with the fix reverted, and
the sandbox probe has been run at least once.

## Chunk 02 — `enroll` stops repointing a live connection by accident

**Delivers**

1. `enroll --relink`, off by default, threaded to `_record_connection` as `allow_new_item`.
2. In `_record_connection`, on the `replaced_the_item` branch with `allow_new_item` false: refuse.
   The refusal names the connection, says what it would have cost in the store's own terms, and
   names `bankmachine connections reauth <id>`.
3. The refused Item is released at the aggregator on the way out, reusing the path the cap race
   already takes — including its fallback to `_record_orphan` when the release fails. An Item minted
   and then refused must not be left billing.
4. A signpost before the URL: when live connections exist, `enroll` lists them and points an
   operator whose login expired at `connections reauth` *before* they spend a browser session.
   Printed, not prompted — an unattended `--yes` run must not block on it.
5. 🔴 The converging re-run against the **same** Item is unaffected and needs no flag. The guard is
   on the branch that mints a second generation, not on re-running `enroll`.

**Tests** (`tests/cli/test_enroll.py`)

- The refusal: a live connection, an enrollment yielding a **different** item id, no `--relink` →
  exit `1`, the row unchanged (still pointing at the old item, cursor intact), and the new Item
  released.
- `--relink` given: the same fixture converges exactly as today, cursor cleared and old Item
  released — the existing behaviour, now behind the flag, with the existing assertions.
- Same-item re-run, no flag: converges, and 🔴 **does not** clear the cursor — the case the guard
  must not catch.
- The release failure path on a refused re-link records an orphan, as the cap race does.
- The signpost appears when live connections exist and is **absent** when none do; an unconditional
  one trains the operator to skip it.

**Artifacts, updated as part of the chunk**

- `docs/system-requirements.md` — AC-1.4's idempotency sentence gains the bound the flag draws.

**Done when:** the suite is green, the refusal is verified to go red with the guard removed, and the
same-item case is verified NOT to trip it.

## Chunk 03 — the sweep: no surface still names `enroll` as the expired-login remedy

`learnings.md` § *Reversing a ratified decision is a sweep, not an edit* — the superseded claim is
*"the recovery for `ITEM_LOGIN_REQUIRED` is to re-run `bankmachine enroll`"*, and it is written in
more than one place. Grep for it rather than fixing the file that prompted this.

**Delivers** — each rewritten to name `connections reauth <id>`:

- `docs/system-requirements.md` **AC-4.3** — the *(Specified — build step 6. **Not built** …)* note
  becomes a record of what was built, and the paragraph explaining why the cursor and granted window
  are lost on a re-link stays, because it is still true of `enroll --relink`.
- `connector/__init__.py` — `ReauthRequiredError`'s docstring ("the operator must re-link it").
- `connector/plaid/errors.py` — the module docstring's `ITEM_LOGIN_REQUIRED` *(re-link it)* gloss.
- `mcp_resources.py` — the `degraded` guidance string.
- `cli/sync_run.py` — the degraded reporting an operator actually reads.
- `docs/first-production-connection.md` and `.prawduct/artifacts/operational-spec.md` — wherever the
  expired-login recovery is written down as a procedure.
- `.prawduct/change-log.md` — entry.
- `.prawduct/artifacts/discovery-relink-account-identity.md` — its *What is still open* section
  loses the question this build answers and keeps the two it does not.

🔴 **The sweep is asserted, not just performed.** A test in `tests/preferences/` is the wrong shape
here (the prose is not patterned), so the check is the grep itself, recorded in the chunk's
verification with the exact command and its output — and `learnings.md` § *A truncated search proves
nothing about what it did not reach* applies: no `| head`.

**Done when:** the grep for the superseded claim returns only the paragraphs that are deliberately
about `enroll --relink`, the suite is green, and `/prawduct:critic cumulative` returns zero blocking.

## Status

- [ ] Chunk 01 — the update-mode link token, and `connections reauth`
- [ ] Chunk 02 — `enroll` stops repointing a live connection by accident
- [ ] Chunk 03 — the sweep

## Context

Branch `feature/connections-reauth-update-mode` off `develop` at `9d7ed55`. Closes
**brookstalley/bankmachine#67**, whose `stage: requirements` was stale — AC-4.3 has carried the
written requirement since the system-requirements v2 draft, and
`discovery-relink-account-identity.md` (2026-09-10) closed the research behind it.

The scope question *does this also close the `enroll` door?* was put to the owner on 2026-09-10 and
answered **reauth + guard enroll**, over reauth-only and reauth-plus-warning.
