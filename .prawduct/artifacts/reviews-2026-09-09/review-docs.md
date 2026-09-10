# bankmachine — documentation & onboarding review (read-only)

Reviewed 2026-09-09 against commit `4b78550` (develop, clean tree). Every CLI claim below was
checked by running `--help` under `BANKMACHINE_ENVIRONMENT=sandbox`, or by reading the source at
the cited line. No file was modified; no enroll/sync was run; production was never touched.

**Bottom line on (a):** a stranger can get from clone to a working *sandbox* MCP server on what is
written. They cannot get to *production* — nothing in `README.md` or `docs/` mentions Plaid
production access, product enablement, Hosted Link, or billing, and the one page that explains the
MCP client is not linked from the README. There is no backup instruction in the README and no way
at all, from any command, to read the datastore key that the docs correctly say is unrecoverable.

---

# Section A — ranked findings

## BLOCKER

### A1. No production-onboarding procedure exists anywhere in `docs/` or `README.md`
**Where:** absent — `README.md:41-70` (Configure/Run), `docs/connecting-an-mcp-client.md:53-60`
(Before connecting). Confirmed by grep: the strings "dashboard", "Hosted Link", "production
access", "billing" appear in tracked docs only at `README.md:43` ("free Plaid dashboard signup")
and `README.md:68` (retire stops billing).

**Evidence (what the code actually requires, none of it documented):**
- `src/bankmachine/cli/enroll.py:165` — `ENROLLMENT_PRODUCTS = ("transactions", "investments")`.
  Both products are requested on **every** connection. The comment at `enroll.py:155-164` records
  this as an owner decision that "bills `investments` on every connection including deposit-only
  ones". A Plaid account without `investments` enabled in production will fail
  `/link/token/create`; a Plaid account with it enabled will be billed for it per Item.
- `src/bankmachine/connector/plaid/client.py:589-592` — `hosted_link=LinkTokenCreateHostedLink(...)`
  is always sent. `client.py:610-614` raises `MalformedResponseError` with the message
  "an account without Hosted Link enabled is the likely cause". **Hosted Link must be enabled on
  the Plaid account.** (Note: *no* redirect URI is involved — `link_token_create` sets none, and
  `client.py:586-589` states hosted mode is precisely what removes the need for "a listener and a
  registered redirect URI". The premise that redirect URIs must be allow-listed is false for this
  code path and should not be added to the docs.)
- `src/bankmachine/cli/enroll.py:170` — `ENROLLMENT_COUNTRIES = ("US",)`, not configurable.
- `src/bankmachine/config.py:170` — production's datastore is the *unsuffixed* `store.db`;
  `config.py:108,120,134` give production its own keychain accounts. Switching is
  `BANKMACHINE_ENVIRONMENT=production` plus a fresh `store init` and a fresh `connector set-secret`
  — stated only in `.env.example:31-34` and `docs/connecting-an-mcp-client.md:9-28`, never as an
  ordered procedure.

**Suggested fix:** add `docs/first-production-connection.md`, linked from the README, opening with:

> ## Going to production
>
> Sandbox needs nothing but a free Plaid signup. Production needs four things done in the Plaid
> dashboard **before** any command here will work:
>
> 1. **Production access approved** for your account. Plaid grants it per-account; until it is
>    granted, production API keys do not exist.
> 2. **Products enabled: `transactions` *and* `investments`.** `bankmachine enroll` requests both
>    on every connection (`src/bankmachine/cli/enroll.py:165`). This is deliberate — a connection
>    enrolled without a product cannot gain it without re-linking — and it means **you are billed
>    for `investments` on every Item, including deposit-only ones.**
> 3. **Hosted Link enabled.** `bankmachine enroll` asks for a hosted session and has no local web
>    server and no redirect URI. If Hosted Link is off, enrollment fails with *"returned no
>    hosted_link_url"*.
> 4. **Billing set up**, and you have read Plaid's per-Item pricing. Every live connection bills
>    monthly until `bankmachine connections retire <id>` removes it at Plaid.
>
> Then, on this machine:
>
> ```sh
> export BANKMACHINE_ENVIRONMENT=production
> export BANKMACHINE_PLAID_CLIENT_ID=<same client id as sandbox>
> uv run bankmachine connector set-secret   # your PRODUCTION secret; cannot overwrite sandbox's
> uv run bankmachine store init             # creates store.db and a NEW datastore key
> uv run bankmachine connector check        # smallest authenticated call against the real host
> ```

---

### A2. The datastore key is unrecoverable, and no command in the product can show it to you
**Where:** `.prawduct/artifacts/operational-spec.md:215-226`; `src/bankmachine/cli/store.py:88-93`;
`src/bankmachine/secrets.py:229-241`.

**Evidence:** `operational-spec.md:217-218` — *"The datastore key cannot be recovered once lost. The
data is then gone — permanently, with no remedy"* — and `:224-226` says to put the 64 hex
characters in a password manager. But `ensure_datastore_key` (`secrets.py:229`) returns the key to
`cmd_init`, which discards it: `store.py:126` is `_, created = ensure_datastore_key(config)`, and
`store.py:88-93` logs only *that* a key was generated, never its value. Grep across `src/` shows
`get_datastore_key` has exactly three callers (`cli/store.py:130`, `store/connection.py:331,426`),
none of which print. **There is no `bankmachine` command that reveals the key**, and no doc tells
the operator how to get it out of the keychain by hand. The one instruction that exists
(`operational-spec.md:224`) is in a governance artifact the README never links.

**Suggested fix:** in the README's Configure block, immediately after `store init`, add:

> 🔴 **Back the datastore key up now, before there is any data.** It is generated once and cannot
> be recovered from the datastore; a backup without it is a backup of noise. Nothing in this
> product prints it — read it out of the keychain yourself and put it somewhere that survives this
> machine:
>
> ```sh
> security find-generic-password -s bankmachine -a datastore:production -w
> ```
>
> (`-a datastore:sandbox` for the sandbox store. The service name is
> `BANKMACHINE_KEYCHAIN_SERVICE`, default `bankmachine`; the account names are built at
> `src/bankmachine/config.py:108`.)

---

## HIGH

### A3. README says "Python ≥3.11"; the package will not install below 3.14 and will not *parse* below 3.12
**Where:** `README.md:27` vs `pyproject.toml:10` (`requires-python = ">=3.14"`), `.python-version:1`
(`3.14`), `docs/system-requirements.md:94` ("Python 3.11+"),
`.prawduct/artifacts/operational-spec.md:62` ("`requires-python >= 3.11`"),
`.prawduct/artifacts/project-preferences.md:8,14` ("3.11+", "`requires-python` stays `>=3.11`").

**Evidence:** five documents claim a 3.11 floor; the package declares 3.14. This is not only an
inconsistency — `tests/preferences/test_python_floor_is_exercised.py:31` records
`SYNTAX_FLOOR = (3, 12)` because `call_with_retry[T]` in `connector/plaid/errors.py` uses PEP 695
type parameters, so **the source cannot be imported on 3.11 at all**. The test at `:56-64` pins
`requires-python` to `.python-version`, so `>=3.14` is the enforced truth and every "3.11" in prose
is stale. A reader on 3.12 who trusts the README will be surprised (uv will silently fetch a
managed 3.14, or fail outright if managed downloads are disabled or offline).

**Suggested fix:** `README.md:27` →

> Needs Python 3.14 (`.python-version` pins it; `uv` will fetch it) and
> [uv](https://docs.astral.sh/uv/). Every dependency is a wheel.

and correct `docs/system-requirements.md:94` to "Python 3.14 (floor tracks the pinned interpreter;
see `pyproject.toml`)", `operational-spec.md:62` to `requires-python >= 3.14`, and
`project-preferences.md:8,14` to record that the floor moved to 3.14 with the PEP 695 adoption.
🔴 `project-preferences.md:14`'s sentence *"nothing here uses a 3.12+ language feature"* is now
false and must go, not just the number.

---

### A4. `docs/connecting-an-mcp-client.md` — the single most important page for a new user — is not linked from the README
**Where:** `README.md:72-88` (the "Wire it to an MCP client" section) and `README.md:92-99` (the
repository-layout table, which lists three `docs/` files and omits this one).

**Evidence:** the README gives one `claude mcp add` line and nothing else. The page that explains
environment selection, the tool surface, warnings, truncation, paging and the resources exists at
`docs/connecting-an-mcp-client.md` and is never named in the README. Verified by grep: `README.md`
contains no occurrence of `connecting-an-mcp-client`.

**Suggested fix:** add to `README.md:88`, after the two 🔴 notes:

> **`docs/connecting-an-mcp-client.md` is the full page** — Claude Desktop and generic JSON config,
> how `BANKMACHINE_ENVIRONMENT` picks sample data or real money, every tool, and how to read the
> warnings before you believe a number.

and add the row to the layout table:

> | `docs/connecting-an-mcp-client.md` | wiring an MCP client, and how to read what it answers |

---

### A5. Nothing schedules a sync, and no user-facing doc says so — while the one non-rebuildable series depends on it
**Where:** absent from `README.md:63-70`. `docs/system-requirements.md:97` presents scheduling as
architecture ("an OS-level user agent, daily"); it is build step 8
(`docs/system-requirements.md:783`) and `.prawduct/artifacts/operational-spec.md:148` labels it
*"(specified — build step 8)"*.

**Evidence:** grep for `launchd|plist|StartCalendarInterval` over `src/` returns nothing; the only
hits are in tests. `sync run` is the only fetch path (`src/bankmachine/cli/sync_run.py:145`), and
it is what writes `balances_daily` (via `_persist` → `apply_response`, `sync_run.py:244-245,400`).
`operational-spec.md:295-297` names the daily balance series as the one thing **no re-sync can
rebuild**. So on the current build, a day the operator does not run `sync run` by hand is a day of
balance history lost permanently — and the README never says the command must be run repeatedly.

**Suggested fix:** in `README.md`'s Run block, annotate and add a note:

> ```sh
> uv run bankmachine sync run           # fetch accounts and transactions; resumes if interrupted
> ```
>
> 🔴 **Nothing schedules this yet.** `sync run` is manual, and each run is also what appends today's
> row to the balance series — the one table no later re-sync can rebuild. A day you do not run it
> is a day of balance history gone. Run it daily (a `cron`/`launchd` entry of your own is fine
> until scheduling ships).

---

### A6. No backup instruction in the README, and `store backup` is undiscoverable from the front door
**Where:** `README.md:63-70` lists four commands and not `store backup`;
`src/bankmachine/cli/store.py:66-83` defines it; `.prawduct/artifacts/operational-spec.md:228-270`
documents it fully — in a governance artifact the README does not link.

**Evidence:** `store backup --help` (run) states the real constraint: *"A plain `cp` … silently
loses whatever is still in the WAL"*, *"The copy is encrypted and is USELESS WITHOUT THE KEY"*.
`operational-spec.md:266-268` also records a limitation nobody outside that file will find:
`store backup` **cannot back up a datastore at a schema version this build does not serve**, which
is exactly the state you are in immediately before an upgrade.

**Suggested fix:** add to the README Run block:

> ```sh
> uv run bankmachine store backup ~/backups/bankmachine-$(date +%F).db
> ```
>
> Takes the writer lock, folds the WAL in, and verifies the copy by reopening it with the key.
> Never use `cp` — it loses the WAL. The copy is ciphertext and is useless without the key
> (see Configure). Back it up before every `git pull` that lands a migration.

---

### A7. No threat model or privacy statement for the *user*, and the one privacy claim on file is wrong once an MCP client is attached
**Where:** `.prawduct/artifacts/security-model.md:179` — *"Sensitive … Never leaves the machine
except as an encrypted backup"*; `security-model.md:73-101` (threat model);
`security-model.md:166` (the only acknowledgement that the consumer is an LLM).

**Evidence:** grep across `README.md`, `docs/` and the artifacts for `cloud`, `inject`,
`untrusted`, `prompt injection`, `third-party model` returns **no statement** that:
1. every row the MCP server returns is handed to whatever model the client is wired to, which for
   Claude Desktop / Claude Code is a **hosted** model — so transaction descriptions, merchant
   names, balances and account masks *do* leave the machine the moment a tool is called;
2. transaction text is **attacker-influenceable input**. A merchant-controlled description string is
   passed through verbatim (`connector/plaid/derivers.py` derivations are faithful passthrough —
   `docs/system-requirements.md:744-747` explicitly ratifies passthrough of description text) and
   lands in an LLM's context. `src/bankmachine/mcp.py:144` shows the project already knows the
   general shape of this risk ("make tool use decisions based on ToolAnnotations received from
   untrusted…") but nothing states it for the *data*.

`security-model.md:83` lists "The network — 🔴 **Not reachable.** The MCP server opens no sockets"
as a control. That is true of the server and irrelevant to the exposure: the client is the egress.

**Suggested fix:** add a `## What leaves this machine` section to `README.md` (and mirror it into
`security-model.md` § Data Privacy), worded:

> ## What leaves this machine
>
> **Outbound network traffic from this product goes to exactly one place: Plaid's API.** No
> telemetry, no analytics, no error reporting; the datastore is encrypted at rest and a backup copy
> is ciphertext.
>
> 🔴 **The MCP client is the exception, and it is not a small one.** Every tool call hands rows —
> merchant names, amounts, balances, account names and masks — to whatever model your MCP client is
> wired to. If that client talks to a hosted model, **your financial data goes to that provider**
> under their terms, not this project's. That is the point of the product; it is also the one place
> the "never leaves the machine" property stops holding. Choose your client accordingly.
>
> 🔴 **Treat transaction text as untrusted input to your agent.** Descriptions and merchant names
> are written by third parties and passed through verbatim; a description crafted to read as an
> instruction reaches your model's context the same way any other row does. There are no mutation
> tools here, so the worst case is a wrong answer or a nudged agent — not a moved dollar — but do
> not let an agent act on instructions it found in a transaction.

---

### A8. `docs/connecting-an-mcp-client.md` cannot be followed by a non-Claude-Code user as written
**Where:** `docs/connecting-an-mcp-client.md:30-51` (Configuration) and `:53-60` (Before
connecting).

**Evidence, three gaps:**
1. The JSON block at `:36-46` uses `"command": "uv"` and never says which file it goes in.
   Claude Desktop reads `~/Library/Application Support/Claude/claude_desktop_config.json` and
   launches servers with a minimal `PATH` — a bare `uv` typically will not resolve. The README's
   Claude Code recipe (`README.md:77-78`) is fine because `claude mcp add` inherits the shell.
2. `"--directory", "/path/to/bankmachine"` — the README is stricter and says
   `/absolute/path/to/bankmachine` (`README.md:78`). This page should say absolute too, since it is
   the page a GUI-client user will use, where relative paths cannot work at all.
3. The "Before connecting" block at `:55-60` omits `BANKMACHINE_PLAID_CLIENT_ID`. Following this
   page alone, `enroll` fails with *"no aggregator client id is configured"*
   (`.prawduct/operator-verification.md:188-190` records that exact message). It also writes bare
   `bankmachine …` while the README writes `uv run bankmachine …`; there is no published package
   (`operational-spec.md:66`), so the bare form only works with the venv activated.

**Suggested fix:** replace `:36-46` with an absolute-path form and a note:

> ```json
> {
>   "mcpServers": {
>     "bankmachine-sandbox": {
>       "command": "/absolute/path/to/uv",
>       "args": ["run", "--directory", "/absolute/path/to/bankmachine", "bankmachine", "mcp"],
>       "env": { "BANKMACHINE_ENVIRONMENT": "sandbox" }
>     }
>   }
> }
> ```
>
> 🔴 **Both paths must be absolute, `uv` included.** A GUI client launches this server with a
> minimal `PATH` and no shell profile, so a bare `uv` will not be found; run `which uv` and paste
> the result. Claude Desktop reads
> `~/Library/Application Support/Claude/claude_desktop_config.json`; Claude Code takes the
> equivalent through `claude mcp add` (see the README). Either way the client must be **fully
> restarted** — quitting the window is not enough — because the server is a subprocess started at
> connect time.

and replace `:55-60` with:

> ```sh
> export BANKMACHINE_PLAID_CLIENT_ID=<your client id>   # or put it in config.toml
> uv run bankmachine connector set-secret               # per environment; prompts, does not echo
> uv run bankmachine store init                         # once per environment
> uv run bankmachine enroll                             # opens a hosted URL; complete it in a browser
> uv run bankmachine sync run                           # fetches accounts and transactions
> ```

---

### A9. `ITEM_LOGIN_REQUIRED` has no documented recovery, the specified repair command does not exist, and the improvised recovery may leave a stale cursor
**Where:** `docs/system-requirements.md:248` (AC-4.3, *"A repair command produces an update-mode
enrollment URL … without losing its history or cursor"*);
`.prawduct/artifacts/operational-spec.md:329` (*"`sync repair` → update-mode enrollment URL …
*(specified — step 6)*"*); `.prawduct/artifacts/api-contract.md:266` (*"Specified, not yet built
(steps 5–10): `sync repair` (update-mode re-auth)"*).

**Evidence:** `uv run bankmachine sync --help` lists exactly `{shell,run}`. There is no `repair`
anywhere in `src/`. What *does* happen: `ITEM_LOGIN_REQUIRED` maps to `ReauthRequiredError`
(`src/bankmachine/connector/plaid/errors.py:70`), `sync_run.py:290-291` catches it and
`_degrade`s the connection with the code, and the run exits `1` (`sync_run.py:100-107`). The only
available recovery is re-running `bankmachine enroll` for that institution, which converges onto
the existing row, clears `status`/`last_error_code`, and preserves history
(`src/bankmachine/cli/enroll.py:665-686`). **No user-facing doc says this.**

🔴 **Risk worth verifying, not asserted:** `enroll.py` never touches `sync_state` (grep for
`sync_state|cursor` in that file returns nothing), yet a re-link that produces a different Item
sets `replaced_the_item` and rewrites `source_connection_id`/`credential_ref`
(`enroll.py:669-673`). The next `sync run` then re-reads the old cursor
(`sync_run.py:248` → `_cursor_for`, `:414-427`) and sends it against a **new** access token. A
Plaid cursor is Item-scoped; if the aggregator rejects it, the connection degrades loudly (good),
but there is no command to clear the cursor. I could not test this without a live re-link.

**Suggested fix:** add to `docs/connecting-an-mcp-client.md` or the new production page:

> ### When a connection breaks
>
> `sync run` records the failure against that one connection and keeps going (AC-4.1); the run
> exits `1` and `bankmachine connections list` shows the connection as degraded.
>
> - **`ReauthRequiredError` / `ITEM_LOGIN_REQUIRED`** — your login at that institution expired or
>   its MFA needs re-answering. **Re-run `uv run bankmachine enroll` and pick the same
>   institution.** It converges on the existing connection rather than duplicating it: history,
>   accounts and the local connection id are kept, the degraded flag is cleared, and the Item it
>   replaced is removed at Plaid so it stops billing. The dedicated `sync repair` command
>   (update-mode re-auth, AC-4.3) is specified but **not built**.
> - **Institution down / rate limited** — nothing to do; run again later.
> - **`ITEM_LOCKED`** — the institution locked the account. Fix it at the institution first.

and file the cursor question as an issue before relying on that procedure in production.

---

## MEDIUM

### A10. No LICENSE file
**Where:** repository root. `git ls-files | grep -i licen[cs]e` → nothing;
`pyproject.toml` declares no `license` field.

**Evidence:** README describes a project meant to be cloned by strangers (`README.md:18`,
`README.md:30`) and `.prawduct/artifacts/security-model.md:79` plans for the repo being published.
With no licence, default copyright applies and a stranger has no right to use, modify or
redistribute it. (Per instructions: **not** proposing a licence choice — flagging the gap.)

**Suggested fix:** add a `LICENSE` file of the owner's choosing and a `license` /
`license-files` entry in `pyproject.toml`; add a one-line "License" section to the README.

---

### A11. Six commits in history carry an AI `Co-Authored-By` trailer, and one tracked doc carries an AI byline
**Where:** git history (6 commits), and `docs/build-vs-adopt-investigation.md:3`.

**Evidence:** `git log --all --grep='Co-Authored-By' -i` returns exactly 6 commits — `57b61d3`,
`ab96f04`, `2c9e1e2`, `5de1544`, `7712923`, `8d166b4` — each carrying
`Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`. All six are early (the oldest is the
prawduct onboarding commit). Rewriting them means rewriting 261 commits of history; noted, not
recommended either way.

🔴 The **live, fixable** violation is `docs/build-vs-adopt-investigation.md:3`:
`**Author:** Claude Code (session 1, pre-scaffold)`. `CLAUDE.md`'s NO ATTRIBUTION section forbids
this explicitly in documentation, and this file is linked from the README layout table
(`README.md:96`), so it is on the path a stranger reads.

**Suggested fix:** `docs/build-vs-adopt-investigation.md:3` →

> **Date:** 2026-09-05 · **Stage:** session 1, pre-scaffold

---

### A12. README's status line is stale in shape and will mislead
**Where:** `README.md:21-23`.

**Evidence:** *"build steps 1–4 are complete and five of the eight specified MCP tools are
serving"*. Both halves check out (`docs/system-requirements.md:766-786` for the sequence;
`docs/system-requirements.md:184-191` and `src/bankmachine/mcp.py:670,711,790,897,969` for the five
tools) — but the sentence is self-contradictory to a reader, because the MCP server is **step 7**.
What is actually true today: 1–4 done; step 5 partly (balances land via `/accounts/get` on every
sync, `sync_run.py:244`; investments not pulled at all); step 6 partly (health yes, `sync repair`
no — see A9); step 7 done for five of eight tools; step 8 (scheduling) **not started** (see A5);
9 and 10 not started.

**Suggested fix:** `README.md:21-23` →

> **Status:** enrollment, sync and the MCP server work end to end. Five of the eight specified MCP
> tools are serving (`docs/system-requirements.md` §5); the other three are recorded as descoped in
> `.prawduct/artifacts/api-contract.md`. **Not yet built:** scheduled syncs (run `sync run`
> yourself), investment/holdings pulls, the file-import path, and the `sync repair` re-auth
> command.

---

### A13. `.prawduct/` is the README's only pointer for two claims, and it is opaque to an outsider — no CONTRIBUTING, no docs index
**Where:** `README.md:23` points at `.prawduct/artifacts/api-contract.md`;
`docs/connecting-an-mcp-client.md:76` does the same; `CLAUDE.md:5-25` tells a contributor to run
`/prawduct:methodology`, a Claude Code plugin command a stranger does not have.

**Evidence:** `.prawduct/artifacts/` holds 30 files, most of them build plans, discovery documents
and acceptance rounds. A newcomer landing there from the README's Status line has no way to tell
which are contracts and which are session notes, and `CLAUDE.md` — the file most tools read first —
opens with governance that is unrunnable outside the owner's setup. There is no `CONTRIBUTING.md`
and no index in `docs/`.

**Suggested fix:** two small additions.
1. `docs/README.md` as an index:

> # Docs
>
> | Page | Read it when |
> |---|---|
> | `connecting-an-mcp-client.md` | you are wiring an agent to the datastore, or reading its answers |
> | `system-requirements.md` | you want the engine's acceptance criteria — the contract of record |
> | `deployment-requirements.template.md` | you are writing down your own roster (kept out of git) |
> | `build-vs-adopt-investigation.md` | you want to know why this was built rather than forked |
>
> `.prawduct/` is this project's internal governance trail — build plans, discovery notes and
> review rounds. It is committed for the owner's own audit and is **not** an entry point; the two
> files outsiders are ever pointed at are `artifacts/api-contract.md` (the MCP surface of record)
> and `artifacts/security-model.md` (the threat model).

2. A `CONTRIBUTING.md` stub saying the same, plus `uv sync`, `uv run pytest`, `uv run mypy`,
   `uv run ruff check`, and that `git config core.hooksPath .githooks` is per-clone.

---

### A14. `.env.example` says "both keychain entries"; there are three kinds, and one of them is per connection
**Where:** `.env.example:65-66`.

**Evidence:** *"The keychain service both keychain entries live under."* `config.py` defines three
account shapes under that one service: `datastore:<env>` (`config.py:108`), `plaid:<env>`
(`config.py:120`) and `connection:<env>:<item>` (`config.py:134`), the last one **per enrolled
connection** and written by `secrets.set_access_token` (`secrets.py:203`). This matters
operationally: an operator who changes `BANKMACHINE_KEYCHAIN_SERVICE` after enrolling orphans every
access token, and `secrets.py` exposes no enumeration to find them again (`enroll.py:70-72` says so
in as many words).

**Suggested fix:** `.env.example:65-66` →

> ```
> # The keychain service every entry lives under: the datastore key, the aggregator
> # secret, and one access token per enrolled connection. Default: bankmachine.
> # 🔴 Changing it after enrolling orphans the access tokens; nothing enumerates
> # them, so the only recovery is re-enrolling each institution.
> # export BANKMACHINE_KEYCHAIN_SERVICE=
> ```

---

### A15. `.env.example` scopes `BANKMACHINE_PLAID_CLIENT_ID` to one command
**Where:** `.env.example:23` — `# --- required for `bankmachine connector check` ---`.

**Evidence:** the client id is read by `load_config` (`config.py:252`) and required by every
aggregator-touching command — `connector check`, `enroll`, `sync run`, `connections retire` — all of
which construct a `PlaidClient` and raise `AggregatorNotConfiguredError` without it.

**Suggested fix:** `.env.example:23` →

> `# --- required for anything that reaches the aggregator -------------------`

---

### A16. `docs/system-requirements.md` §1 line 94 also carries the wrong runtime, and §1's diagram promises a "sync daemon"
**Where:** `docs/system-requirements.md:88-98`.

**Evidence:** the architecture block shows `sync daemon (scheduled, daily)` and `:97` states
"**Scheduling:** an OS-level user agent, daily. Survives reboot; requires no open terminal." The
build sequence at `:783` puts scheduling at step 8 and it is unbuilt (A5). A reader of §1 will
believe a daemon exists.

**Suggested fix:** annotate `:97`:

> - **Scheduling:** an OS-level user agent, daily. Survives reboot; requires no open terminal.
>   *(Specified — build step 8. Not built: `sync run` is manual today.)*

---

## LOW

### A17. README's `pytest tests/preferences` line overclaims for a fresh clone
**Where:** `README.md:33` — *"confirms the pre-push guards work in this clone"*.
**Evidence:** I ran it: `84 passed in 9.96s`. But the leak guard reads its match tokens from
`deployment/roster-tokens.txt` (`.gitignore:35`, `project-preferences.md:128`), and a fresh clone
has no `deployment/` — the guard "passes with a note", per `project-preferences.md:128`. So in the
clone that most needs reassurance, the suite proves the *harness* works, not that anything is being
matched.
**Fix:** `README.md:33` → `uv run pytest tests/preferences      # the guards' own self-tests (a clone with no deployment/ roster has nothing to match on)`

### A18. The gitflow half of the pre-push hook will surprise a fork
**Where:** `README.md:36-39`, `.githooks/pre-push:26-31`.
**Evidence:** the hook rejects any push to `refs/heads/main` that is not a merge of a `release/*`
or `hotfix/*` branch. A stranger who forks and works on `main` is blocked by a rule that is the
owner's release policy, not a safety guard. The hook itself says `--no-verify` bypasses it
(`.githooks/pre-push:15-16`); the README does not.
**Fix:** add to `README.md:39`: *"The gitflow half keeps `main` release-only, which is this
repository's own policy — if you fork and work differently, skip it (`git config --unset
core.hooksPath`) but keep the leak guard by running `tests/preferences/check-no-personal-data.sh`
yourself."*

### A19. The owner's GitHub handle appears in tracked artifacts
**Where:** `.claude/settings.json:6`, `.prawduct/project-state.yaml:733,1219`, and ~10
`.prawduct/artifacts/*.md` issue references (`brookstalley/bankmachine#NN`).
**Evidence:** grep over tracked files for the operator's name, handle, home path and mail domain returns
only these plus the test fixtures' deliberate `/Users/someone/` literals
(`tests/preferences/test_no_hardcoded_paths.py:23,132,156,167,182,192`). **No institution name, no
balance, no home directory path, and no email leaks into any tracked file** — the roster boundary
holds. The handle is a repo path, which is unavoidable for public issue references; noting it only
because `project-preferences.md:128` states the norm as "no … operator name … in any commit
reaching a remote".
**Fix:** none needed; if the norm is meant literally, amend the norm's wording to carve out the
repository slug rather than hunt down the references.

### A20. `.prawduct/artifacts/mcp-production-readiness.md` reads as current but is a 2026-09-08 snapshot with superseded facts
**Where:** `mcp-production-readiness.md:1-8`, `:227-238`, `:475-486`.
**Evidence:** the document names a tool `spending_summary` (merged into `money_summary` on
2026-09-09, `docs/system-requirements.md:195-208`), states "four tools" (`:33`) where five now
serve, and cites a "1000 ceiling" on `query_transactions` (`:223`) where `envelope.py:55` sets
`MAX_ROWS = 500`. Its own tracking block (`:128-170`) records which items closed, so the document
is *internally* honest — but its **verdict header still reads "No. Not today."** (`:16`) with no
date-stamped superseding line at the top. The owner cutting over tomorrow will re-read this file;
it should not be the last word without a header.
**Fix:** add under the title:

> **Superseded in part.** Verdict and measurements are as of 2026-09-08. Items 1–3 and 8 have since
> shipped; `spending_summary` is now `money_summary`; the row cap is 500, not 1000. The live gate is
> `gh issue list --label blocks:production`, and § *The ordered answer* and § *Day one in
> production* are the parts still in force.

---

# Section B — the owner's first-production-connection checklist

Ordered. Every claim cites the code that makes it true. Steps 1–4 are free and one of them stops
being meaningful the moment production is enrolled.

### Before touching production

**B1. Drain VRF-001 and run VRF-003 and VRF-004 against sandbox.**
*Verifies:* that the enrollment prompt reads as a warning **before** the URL, at the last moment
`history_days` is correctable (`src/bankmachine/cli/enroll.py:555-572` prints the window and the
"cannot be raised later" line, then asks; `enroll.py:566-570`), and that an agent actually reads
the warnings.
*Why now:* `enroll.py:11-14` — the window is immutable per connection once the public token is
spent. Running VRF-003 after the production enroll verifies nothing that still matters.
*Failure looks like:* the window line reads as information rather than a warning, or the agent in
VRF-004 step 2 answers "$0" for a pre-coverage month instead of "absent".
*Source:* `.prawduct/operator-verification.md:227-262` (VRF-003), `:264-358` (VRF-004).

**B2. Read the sandbox datastore key out of the keychain and store it, as a rehearsal.**
```sh
security find-generic-password -s bankmachine -a datastore:sandbox -w
```
*Verifies:* that you can actually retrieve a key, before the key that matters exists. **Nothing in
the product prints it** — `cli/store.py:126` discards the value `ensure_datastore_key` returns
(`secrets.py:229-241`), and `get_datastore_key` has no printing caller.
*Failure looks like:* the command returns nothing, or the wrong account name — check
`config.py:108`, which builds the account as `f"datastore:{environment}"`.

**B3. Rehearse a restore once, against a scratch store.**
```sh
uv run bankmachine store backup /tmp/rehearse.db
BANKMACHINE_DATASTORE_PATH=/tmp/rehearse.db uv run bankmachine store status
```
*Verifies:* the copy opens with the same key and reports healthy. `store backup` takes the
exclusive writer lock, folds the WAL in, and re-opens the copy with the key running
`integrity_check` before reporting success (`cli/store.py:66-77`;
`.prawduct/artifacts/operational-spec.md:234-262`).
*Failure looks like:* `store status` on the copy reports unhealthy, or the backup refuses because
the destination exists (it never overwrites, `cli/store.py:79-82`).
*Why now:* `mcp-production-readiness.md:396-403` — the first production sync starts accumulating
`balances_daily`, the one series no re-sync can rebuild (`operational-spec.md:295-297`), and the
restore path has never been rehearsed by a human (`operational-spec.md:279-283`).

**B4. Confirm the Plaid dashboard is ready.** Four things, none of them documented in this repo
(finding A1):
- production access **approved** on the account;
- products **`transactions` and `investments` both enabled** — `enroll.py:165` requests both on
  every connection, and `enroll.py:155-164` records that this bills `investments` per Item even for
  deposit-only institutions;
- **Hosted Link enabled** — `client.py:589-592` always requests a hosted session, and
  `client.py:610-614` fails with *"an account without Hosted Link enabled is the likely cause"*.
  🔴 No redirect URI is needed and none is sent (`client.py:586-589`); do not go looking for one;
- **billing configured**, and you know the per-Item price you are about to start paying.
*Failure looks like:* `bankmachine enroll` dying at `/link/token/create` with either
`INVALID_PRODUCT`-shaped text or "returned no hosted_link_url".

**B5. Decide `BANKMACHINE_HISTORY_DAYS` and leave it alone.**
*Verifies:* nothing — it is a decision. The default is 730, the aggregator's own inclusive maximum
(`config.py:42`, read off the SDK's request model), and a value outside 1–730 is **refused, not
clamped** (`config.py:259-267`). Confirm no `history_days` sits in
`~/.config/bankmachine/config.toml` and no `BANKMACHINE_HISTORY_DAYS` is exported in the shell you
will run `enroll` from — `config.py:204-214` gives the env var precedence over the file.
*Failure looks like:* `enroll` printing a number other than 730 at `enroll.py:563`. Stop there; it
cannot be raised afterwards without re-linking every institution (`enroll.py:11-14`, AC-1.2).

### The cutover

**B6. Set the production environment and secret.**
```sh
export BANKMACHINE_ENVIRONMENT=production
export BANKMACHINE_PLAID_CLIENT_ID=<client id>     # same id across environments
uv run bankmachine connector set-secret            # PRODUCTION secret
```
*Verifies:* the secret lands under a **separate** keychain account, `plaid:production`
(`config.py:120`), which sandbox's cannot overwrite (`config.py:111-120`). `set-secret` prompts
without echoing at a tty and reads stdin when piped (`connector set-secret --help`), and refuses an
empty value (`secrets.py:159-160`).
*Failure looks like:* `bankmachine: refusing to store an empty aggregator secret`, or nothing at
all — in which case check you are in the shell where `BANKMACHINE_ENVIRONMENT=production` is
exported, or you have just set sandbox's secret again.

**B7. `uv run bankmachine store init`.**
*Verifies:* it prints `datastore created at …/store.db` — **unsuffixed**, which is production's
default (`config.py:163-170`, AC-10.6) — plus the migrations applied and a healthy status
(`cli/store.py:86-107`). A **new** datastore key is generated and logged as a warning line
(`cli/store.py:88-93`); the key itself is never printed.
*Failure looks like:* `datastore already present` when you expected a new one (you are pointed at
the wrong file — check `BANKMACHINE_DATASTORE_PATH`), or a `DatastoreKeyMissingError` naming
`bankmachine/datastore:production` (a store exists whose key is gone — `_obtain_key`,
`cli/store.py:109-120`, deliberately refuses to mint a replacement).

**B8. 🔴 Immediately back up the production datastore key.**
```sh
security find-generic-password -s bankmachine -a datastore:production -w
```
into a password manager and, ideally, onto paper. *Verifies:* that the one unrecoverable secret in
this system exists somewhere other than this machine's keychain.
*Why here and not later:* `operational-spec.md:217-218` — the key cannot be recovered from the
datastore, and every day you wait is a day of `balances_daily` that a lost keychain destroys
permanently. Do it now, while the store is empty and the cost of getting it wrong is zero.

**B9. `uv run bankmachine connector check`.**
*Verifies:* configuration, keychain, network path and the raw archive in one call, against the
**production** host, without an enrolled connection (`connector check --help`). Expect a report
naming the environment, endpoint, received timestamp, byte count and archived `raw_response` id.
*Failure looks like:* `400 (INVALID_API_KEYS): invalid client_id or secret provided` — that exact
string is what a sandbox/production secret mix-up produced before
(`.prawduct/operator-verification.md:197-203`), and it is distinguishable from `INVALID_FIELD`
(malformed) and from a network fault. Also refuses **before** calling out if the datastore is
missing (`operator-verification.md:192-194`).

**B10. `uv run bankmachine enroll` — the first institution only.**
*Verifies:* the whole irreversible path. Read the window line before answering `y`
(`enroll.py:563-570`); the URL prints after (`enroll.py:575-584`); the command polls
`/link/token/get` every 3 s until the session finishes or the 900 s default timeout expires
(`enroll.py:172-176`, `enroll.py:597-610`, `enroll --help`).
*Expect on success:* `linked <id>: <institution>`, `requested history: 730 days`, and 🔴
`granted history: not yet known -- filled at the first sync (AC-1.3a)` (`enroll.py:733-739`).
**That line does not mean you got 730 days**, and reading it that way is the failure AC-1.3a exists
to prevent (`docs/system-requirements.md:164-168`).
*Failure looks like:* the cap refusal (`ConnectionCapReachedError`, `enroll.py:699-701`) — raised
**before** the link token so you spend no browser trip (`operational-spec.md:110-113`); a timeout
(exit 1); or an exit-1 success carrying *"the connection this replaced could not be removed at the
aggregator, so it may still be billing"* (`enroll.py:541-552`) — follow that one up, it is money.
*Why one institution:* `docs/system-requirements.md:772-774`, build step 3 — verify the granted
window on ONE real connection before enrolling any others.

**B11. `uv run bankmachine sync run`, then run it again, repeatedly.**
*Verifies:* the backfill. The first call fetches `/accounts/get` first, every run
(`sync_run.py:237-245`), then pages `/transactions/sync`. A backfill that has not materialized
returns `transactions_update_status: NOT_READY` (`sync_run.py:57`), and the command waits only
`2+5+15+30+60 = 112 s` across five attempts (`sync_run.py:68`) before printing
`history is still being prepared; run again shortly` (`sync_run.py:496-499`) and exiting **0**.
🔴 **Exit 0 with nothing applied is the expected first result on a real institution.** Re-run until
you see `N pages applied`.
*The number that matters:* `granted_history_days` is written **once**, and only when the
aggregator reports `HISTORICAL_UPDATE_COMPLETE` (`sync_run.py:62`, `sync_run.py:314-330`) — a
deliberate gate, because measuring earlier records a shortfall that does not exist. If the grant is
short you get a 🔴 line naming the gap in days and saying it cannot be widened without re-linking
(`sync_run.py:512-518`).
*Failure looks like:* a per-connection line carrying an error name — the connection is marked
`degraded` with its code and timestamp (`sync_run.py:452-465`) and the run exits `1`
(`sync_run.py:99-107`); or `stopped at the page ceiling; run again to continue`
(`MAX_PAGES_PER_RUN = 500`, `sync_run.py:74`) — benign, the cursor resumes exactly there, and
`last_success_at` is deliberately **not** stamped (`sync_run.py:468-480`).

**B12. `uv run bankmachine connections list` and `store status`.**
*Verifies:* one row per institution, `active`, and the slot count against the cap
(`connections list`, run against sandbox above prints `1 of 10 connection slots in use`); and that
`store status` names `production`, the unsuffixed path, `journal mode: wal`, the schema version and
`healthy: yes`.
*Failure looks like:* a connection still reading `degraded`, or `store status` reporting a schema
version this build does not serve.

**B13. Enroll the remaining institutions, one at a time, repeating B10–B12.**
*Verifies:* each institution's own granted window — they differ sharply
(`mcp-production-readiness.md:451-454`), and a short-history institution is invisible in the global
figure. The cap is 10 by default (`config.py:49`).

### After the data is in

**B14. Run `uv run bankmachine sync run` a second time and re-check for duplicates.**
*Verifies:* the incremental path, not the initial pull — the cursor boundary is where rows double
or drop (`mcp-production-readiness.md:458-460`). AC-2.4's idempotency is machine-tested offline;
this is the live confirmation.
*Failure looks like:* a transaction you can see twice in `sync shell`, or a total that moved when
nothing happened.

**B15. Back up immediately: `uv run bankmachine store backup ~/backups/bankmachine-<date>.db`.**
*Verifies:* you have a recoverable copy of the first day of `balances_daily`.
🔴 Never `cp` — the WAL is not in `store.db` alone, and a measured `cp` of a source with a 2 MB hot
WAL lost every one of 300 rows (`operational-spec.md:240-244`). The copy is ciphertext and is
useless without the key from B8.

**B16. Reconcile every account balance against each institution's own app, same day.**
*Verifies:* the one thing nothing inside the system can check
(`mcp-production-readiness.md:439-442`). Catches sync gaps, stale connections and zombie accounts
at once.

**B17. Enumerate the accounts you know you have against `list_accounts`.**
*Verifies:* completeness. 🔴 **An account that was never received is invisible** — there is no
field that can report it and no query that can detect it
(`mcp-production-readiness.md:443-447`). Net worth is wrong by exactly that account's balance.
*Common cause:* de-selecting accounts during the Link flow. If a whole connection's roster comes
back empty you get a `roster_observed_empty` warning
(`docs/connecting-an-mcp-client.md:222-227`) — that is a different, louder failure.

**B18. Confirm every enrolled account is USD.**
*Verifies:* that `money_summary`'s totals are interpretable. Multi-currency behaviour is
**undefined** and unfiled (`mcp-production-readiness.md:227-238`); with two currencies a bare
`spent_minor_units` cannot be read at all. If any account is not USD, stop asking spending
questions until that is resolved.

**B19. Check the sign of one known paycheck and one known bill (VRF-006).**
*Verifies:* that normalization runs the right way on a real feed. This has **never** been observed
in the correct direction — the sandbox's only payroll row arrives pre-inverted
(`mcp-production-readiness.md:250-255`). Do it at **two different institutions**: one feed obeying
the convention is not evidence about another, and the sandbox is single-connection so this cannot
be rehearsed (`.prawduct/operator-verification.md:393-416`).
*Failure looks like:* a deposit reading negative. A flagged connection surfaces as
`sign_convention_unverified` (`docs/connecting-an-mcp-client.md:229-230`) — do not correct it by
hand.
🔴 Do **not** judge direction from description text (`docs/system-requirements.md:744-747`).

**B20. Watch one real pending transaction across settlement (VRF-005).**
*Verifies:* the pending→posted path, which has never seen a real pending row — `pending` is 0 on
all 388 sandbox rows (`.prawduct/operator-verification.md:366-391`). Confirm exactly one row after
it posts, the same local `transaction_id`, and the **settled** amount (a tip or a fuel hold makes
these differ; existing tests use the same amount on both sides).
*Failure looks like:* two rows, or a total that moved by more than the settlement difference.

**B21. Ask three questions you already know the answer to, before asking any you don't.**
*Verifies:* the judgement layer. Last month's rent, the current card balance, what you actually
paid the mortgage (`mcp-production-readiness.md:463-468`). Establish the oracle deliberately — the
sandbox was providing it for free and production will not.
🔴 Read `totals.external_spend_outflow_minor_units`, not the raw outflow: over the sandbox store
the raw total is five times the money that actually left
(`docs/connecting-an-mcp-client.md:110-116`).

**B22. Point a second MCP server at production and confirm the two are distinguishable in the
client's own list.**
*Verifies:* that you cannot confuse them. The environment rides every response and the server title
is `bankmachine (production)` (`src/bankmachine/mcp.py:1304`) — but Claude Code lists the
**registered key**, not the title (measured 2026-09-09,
`.prawduct/operator-verification.md:276-284`), so name the entries distinctly yourself.
*Failure looks like:* two entries you cannot tell apart in the client's server list. Verify the
property, not the mechanism.

**B23. Set a recurring reminder to run `sync run` (and `store backup`) daily until scheduling
ships.**
*Verifies:* nothing — it is the mitigation for finding A5. Nothing schedules a sync; the daily
balance series only advances on a run, and a day missed is a day gone (`operational-spec.md:295`).

**B24. Before the next `git pull` that lands a migration, follow the upgrade order.**
`operational-spec.md:157-186`, in this order: stop the MCP client (disconnect is enough — the
server is a subprocess started at connect time) → **`store backup` first**, because backup opens
through the ordinary writer factory and cannot back up a store at a version this build does not
serve (`operational-spec.md:266-268`) → `git pull && uv sync` → `store init` (the migration runner
is idempotent, `cli/store.py:95`) → `store status` and check the schema version moved → reconnect.
🔴 If any connection is not syncing at that moment, run `store rebuild` after
(`operational-spec.md:180-186`).
*Failure looks like:* every MCP tool returning `isError: true` with code `datastore_unservable`
(`docs/connecting-an-mcp-client.md:90-94`) — that is the guard working, and it means a reader is
older than the store.
