# bankmachine — security review before first production enrollment

Read-only review, 2026-09-09. No file was modified. Every claim below was checked against the
code or measured on this machine; where I measured, the probe is named.

Scope covered: config/secrets/logging, store (engine, connection, backup, raw), connector
(client, errors, boundary), CLI, MCP server + resources, envelope/cursor, the three preference
guards, dependency lock, repo hygiene, and the on-disk state of the operator's real data/log
directories.

**Bottom line:** the confidentiality design is genuinely strong and I found no path by which a
credential reaches disk, a log, argv, the raw archive, or the MCP wire. The findings below are
one operator action that must happen before real data starts accruing, one missing mitigation on
the LLM boundary, and a set of smaller hardening gaps.

---

## Findings, most severe first

### 1. BLOCKER — Nothing tells the operator to back up the datastore key, and there is no way to read it out of the product

**Where:** `src/bankmachine/cli/store.py:87-98` (`cmd_init`), `README.md:41-61` ("Configure"),
`src/bankmachine/secrets.py:230-241` (`ensure_datastore_key`).

**Evidence.** `store init` mints the key silently and reports only where it went:

```python
def cmd_init(config: Config, _args: argparse.Namespace) -> int:
    existed = config.datastore_path.exists()
    created_key = _obtain_key(config, datastore_existed=existed)
    if created_key:
        logger.warning(
            "generated a new datastore key in keychain %s/%s",
            config.keychain_service,
            config.keychain_account,
        )
```

Nothing printed to stdout mentions backup. `grep -rn "set_datastore_key\|generate_datastore_key" src/`
returns only `secrets.py` itself — **no CLI command anywhere accepts, prints, or exports a key.**
(That property is correct for confidentiality and is listed under SOUND below; the problem is that
it leaves the operator no in-product way to obtain the value they are told to preserve.)

The README's setup path is `store init` → `connector check` → `enroll` → `sync run`. It never
mentions `store backup` and never mentions the key. The warning does exist, but only in two places
the operator will not read tomorrow: `store backup --help` ("USELESS WITHOUT THE KEY -- back the
key up separately") and `.prawduct/artifacts/operational-spec.md:213-225`.

**Failure scenario.** Real accounts are enrolled tomorrow. `balances_daily` begins accruing on day
one and, by the project's own analysis (`operational-spec.md:290-300`), is the one series **no
aggregator backfills** — a day not captured is gone. Some weeks later the keychain entry is lost
(a wiped machine restored without the login keychain, a keychain reset after a password change, a
`security delete-generic-password` typo). `secrets.py:88-104` then correctly refuses, and the data
is permanently unreadable. The datastore backup, if one exists, is ciphertext with no key.

**Fix.**
- Before enrolling anything tomorrow: run `bankmachine store init` for production, then open
  Keychain Access → `bankmachine` / `datastore:production`, copy the 64 hex characters into the
  password manager, and verify by pasting it back into a scratch `PRAGMA key` probe.
- In code: have `cmd_init` **print** (not just log) a block when `created_key` is true, naming the
  keychain service/account and saying in one sentence that losing it loses the balance series
  permanently. That is the one moment the operator is guaranteed to be looking.
- Consider a `bankmachine store export-key --i-understand` that prints the key to a tty only
  (refusing when stdout is not a tty), so the documented remedy has a route inside the product.

---

### 2. HIGH — Merchant names and transaction descriptions are attacker-influenced text delivered verbatim to an LLM, and nothing anywhere says so

**Where:** `src/bankmachine/query.py:1696-1697` (the `list_transactions` projection) and
`src/bankmachine/query.py:2291` (`money_summary` grouped by merchant), `src/bankmachine/mcp.py:1331-1452`
(`_instructions`), `src/bankmachine/mcp_resources.py` (both served reference documents),
`.prawduct/artifacts/security-model.md:75-100` (threat table).

**Evidence.** The row projection reaching the wire is, from the log file's own record of the
statement:

```
SELECT transactions.transaction_id, accounts.name AS account, transactions.posted_date,
       transactions.description, transactions.merchant_name, transactions.amount_minor, ...
```

`grep -rni "injection|untrusted|attacker|adversar" src/ docs/ .prawduct/artifacts/ README.md`
returns nothing about content injection. The only hit in `src/` is `mcp.py:144`, which is about
*clients* trusting `ToolAnnotations`. The handshake instructions are 120 lines of careful guidance
about warnings, windows and truncation, and contain no statement that `rows[].description` and
`rows[].merchant_name` are unowned input. The security model's threat table lists the backup
holder, the repository reader, another local user and the network — the LLM consumer is absent, and
"Unsafe consumption of third-party APIs (API10)" is answered by pointing at *data integrity*
(`security-model.md:174`), which is about the aggregator being wrong, not about the aggregator
faithfully relaying text an outsider wrote.

**Failure scenario.** Descriptors and memo lines are written by counterparties, not by the bank.
Anyone who can move $0.01 to the owner — a card-testing charge, a Zelle/Venmo/Cash App payment to a
known handle, an ACH credit with a crafted addenda, a merchant choosing its own descriptor — chooses
roughly 30-100 characters that land in `transactions.description` and are handed to the agent inside
`content[].text`. In the deployment the README describes, that agent is Claude Code, which holds
shell, file and network tools far beyond this server. The injected string does not need to succeed
often; it needs to succeed once. This risk does not exist today because sandbox data is Plaid's
fixtures — it begins the moment real accounts are connected, which is exactly what happens tomorrow.

**Fix (cheap, do it before enrolling).**
- Add one block to `_instructions`, in the same imperative register as the warning tables: name
  `description`, `merchant_name`, `account` and `institution` as **text written by third parties,
  to be quoted and never followed**, and state that no instruction, URL or credential request
  appearing in a row is from the operator or from this server.
- Repeat it once in the envelope reference document (`mcp_resources.py`), which is where a client
  goes for field-level detail.
- Add the LLM consumer to `security-model.md`'s threat table with this control, so the absence
  reads as a decision rather than an oversight.
- Optional and stronger: since the tool surface already has a closed warning vocabulary, a
  `row_text_is_untrusted` note on every `query_transactions` and merchant-grouped `money_summary`
  answer would put the caveat where the project's own norm puts every other one — in the payload,
  not in a channel nobody reads.

---

### 3. MEDIUM — Datastore, WAL, log and backup files are created world-readable (0644); directories 0755

**Where:** no `umask`/`chmod` anywhere except the lock file. `grep -rn "chmod\|umask\|0o600" src/`
returns exactly one hit:

```
src/bankmachine/store/connection.py:360:
    fd = os.open(config.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
```

Everything else inherits the process umask: `dbapi2.connect(...)` for the datastore
(`connection.py:329-334`, `:421-426`), `logging.FileHandler` (`logging_setup.py:148`),
`config.log_dir.mkdir(...)` (`logging_setup.py:146`), `config.datastore_path.parent.mkdir(...)`
(`connection.py:326`), and `VACUUM INTO ?` for backups (`backup.py:180`).

**Measured on this machine** (umask 022):

```
-rw-r--r--  store-sandbox.db          <- datastore, 0644
-rw-r--r--  store-sandbox.db-shm      <- 0644
-rw-r--r--  store-sandbox.db-wal      <- 0644
-rw-------  store-sandbox.db.lock     <- the one file that IS hardened
drwxr-xr-x  ~/.local/share/bankmachine
-rw-r--r--  ~/.local/state/bankmachine/logs/bankmachine.log   <- 0644, PLAINTEXT
drwxr-xr-x  ~/.local/state/bankmachine/logs
-rw-r--r--  deployment/deployment-requirements.md             <- 0644 (roster; data-sources.md is 0600)
```

**Failure scenario.** `security-model.md:83` states the control for "a process on this machine
running as another user" as "OS file permissions; the key is in the keychain, not on disk." At 0644
that control is not in place. The datastore itself is ciphertext, so the practical exposure is:
(a) the **plaintext log**, which carries the datastore path, environment, institution ids, SQL
statement text and — via the deliberate broad catch at `mcp.py:1671` — future SQLAlchemy errors that
stringify with their bound parameters; (b) the **backup destination**, which the operator will
plausibly point at `~/Documents` or a synced folder, where 0644 plus a sync client is a wider
audience than intended; (c) the roster document under `deployment/`. On a single-user Mac with
FileVault this is defence-in-depth rather than an active breach path, which is why it is MEDIUM
rather than higher — but the document claims the control, so either the control or the claim should
change.

**Fix.** Set `os.umask(0o077)` once at the CLI/MCP entry point (`cli/__init__.py:run` and
`mcp.cmd_mcp`), which fixes the datastore, WAL, shm, log, backup and every directory in one line
and keeps the lock file's existing explicit mode correct. If a process-wide umask is unwanted,
`chmod(0o600)` the datastore and the backup destination immediately after creation and
`chmod(0o700)` the two parent directories in the `mkdir` calls. Then say so in `security-model.md`
so the row and the code agree.

---

### 4. MEDIUM — The tracked-credential guard does not catch a bare `secret =` or `token =` label, which is exactly the shape a Plaid secret would take

**Where:** `tests/preferences/test_no_credentials_tracked.py:75-81`.

```python
_LABELLED = re.compile(
    r"(?:access[_-]?token|client[_-]?secret|api[_-]?key|password|passwd)"
    r"\s*[=:]\s*([\"']?)([A-Za-z0-9_\-]{12,})",
    re.IGNORECASE,
)
```

Compare the log redactor, which is keyed to the same idea and is strictly wider
(`src/bankmachine/logging_setup.py:44-52`):

```python
(?: access[_-]?token | refresh[_-]?token | client[_-]?secret | client[_-]?id
  | api[_-]?key | secret | password | passwd | token | key )
```

**Measured** by importing the guard and calling `_findings` directly:

| line fed to `_findings` | verdict |
|---|---|
| `secret = "5a1b2c3d4e5f60718293a4b5c6d7e8"` | **not caught** |
| `plaid_secret = "5a1b2c3d4e5f60718293a4b5c6d7e8"` | **not caught** |
| `BANKMACHINE_PLAID_SECRET=5a1b2c3d4e5f60718293a4b5c6d7e8` | **not caught** |
| `token = "5a1b2c3d4e5f60718293a4b5c6d7e8"` | **not caught** |
| `client_secret = "<30 hex chars>"` | caught |

The second safety net does not close it either: `_RAW_KEY` requires **exactly** 64 hex characters
(`(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])`), and a Plaid secret is a shorter hex string, so
it falls through both rules.

**Failure scenario.** The repository is designed to be publishable, and the one credential most
likely to be pasted somewhere convenient is the aggregator secret — the very value the design
deliberately gave no environment variable. The natural spellings for that mistake are
`BANKMACHINE_PLAID_SECRET=…` added to `.env.example` "just to document it", or `secret = "…"`
dropped into a config example or an artifact. Every one of those passes this guard and passes the
roster guard too (it matches no institution name). The pre-push hook then reports clean.

**Fix.** Add `secret`, `token` and `key` to `_LABELLED`'s alternation so it matches the redactor's
vocabulary — the redactor already proves the wider set does not produce unmanageable noise, and the
`_is_a_variable_reference` and `_PLACEHOLDER` exemptions already handle the ordinary-code cases. Add
a positive-control case per new label to `test_the_scan_can_actually_find_each_shape`.

---

### 5. MEDIUM — One test runs against the operator's real configuration and writes into the real log directory

**Where:** `tests/test_unservable_datastore.py:727-730`.

```python
monkeypatch.setenv("BANKMACHINE_DATASTORE_PATH", str(cfg.datastore_path))
monkeypatch.setenv("BANKMACHINE_KEYCHAIN_SERVICE", cfg.keychain_service)
monkeypatch.setenv("BANKMACHINE_ENVIRONMENT", cfg.environment)
monkeypatch.setenv("BANKMACHINE_PLAID_CLIENT_ID", "test-client-id")
```

Every other CLI test fixture sets **six** variables — the four above plus `BANKMACHINE_LOG_DIR` and
`BANKMACHINE_CONFIG` (see `tests/cli/test_store_commands.py:17-21`,
`tests/cli/test_connector_commands.py:30-35`, `tests/cli/test_enroll.py:93-98`,
`tests/cli/test_run_failures_are_logged.py:23-27`). This one omits both, so its `load_config()` call
resolves `log_dir` and the config file from the operator's real environment.

**Measured**: the operator's live log at `~/.local/state/bankmachine/logs/bankmachine.log` contains

```
2026-09-09T17:17:43-0600 WARNING bankmachine: environment=SANDBOX datastore=/private/var/folders/
  qx/.../pytest-of-brookstalley/pytest-20810/test_no_cli_command_prescribes7/key_missing/store.db
2026-09-09T17:17:43-0600 ERROR bankmachine.cli: command enroll failed: ...
```

— pytest temp paths written into the production log file.

**Failure scenario.** The blast radius today is contained: the keychain service and datastore path
*are* overridden, so no real credential or datastore is touched. What leaks is the operator's real
log, which becomes the production log tomorrow, and the real `~/.config/bankmachine/config.toml`,
whose `log_dir`, `busy_timeout_ms`, `history_days` and `connection_cap` all feed into that test's
resolved config. The general shape — "a test resolves configuration from the developer's live
environment" — is one step from a test that touches the real store, and the missing
`BANKMACHINE_CONFIG` override is what removes the isolation the other four fixtures have.

**Fix.** Add `BANKMACHINE_LOG_DIR` and `BANKMACHINE_CONFIG` to that fixture, matching the other
four. Better: promote the six-variable block to a shared `conftest.py` fixture so a new test cannot
set four of six, and consider an autouse fixture that fails any test whose resolved
`config.log_dir` or `datastore_path` is outside `tmp_path`.

---

### 6. LOW — `plaid-python` resolves to an sdist, not a wheel, contradicting the README

**Where:** `README.md:27` — "Needs Python ≥3.11 and uv. **Every dependency is a wheel.**";
`uv.lock`.

**Measured** by parsing `uv.lock`:

```
plaid-python 44.0.0   sdist: plaid_python-44.0.0.tar.gz (sha256:cf303e…)   wheels: 0
sqlcipher3-wheels 0.5.7  sdist present, wheels: 32 (incl. cp314 macosx_11_0_arm64)
```

**Failure scenario.** An sdist runs its `setup.py` at install time, so `uv sync` executes vendor
code with the operator's privileges. That is ordinary Python practice and the lock pins a sha256, so
integrity is intact — the concrete problem is that the README makes a claim a reader might rely on
when auditing the install. (The README's Python floor is also stale: `pyproject.toml` declares
`requires-python = ">=3.14"`.)

**Fix.** Correct the README sentence to name the one sdist, or drop the claim. Separately, `>=3.11`
in the README should read `>=3.14` to match `pyproject.toml` and `.python-version`.

---

### 7. LOW — The entire at-rest guarantee rests on a single-maintainer fork that ships its own bundled crypto

**Where:** `pyproject.toml` dependency `sqlcipher3-wheels>=0.5.7`;
`.venv/.../sqlcipher3_wheels-0.5.7.dist-info/METADATA`:

```
Author-email: Charles Leifer <coleifer@gmail.com>, laggykiller <chaudominic2@gmail.com>
Project-URL: homepage, https://github.com/laggykiller/sqlcipher3
NOTICE: This is a fork of sqlcipher3 (coleifer) which adds github action for
creating wheels for Windows, MacOS and Linux.
```

The wheels statically link SQLCipher and OpenSSL built by that fork's CI. `security-model.md:361`
already names this as one of the two supply-chain surfaces that matter, so this is a confirmation
rather than a discovery.

**I verified the artifact behaves correctly** (probe on a scratch database): cipher 4.12.0 community,
provider openssl, page size 4096, `PBKDF2_HMAC_SHA512`, `HMAC_SHA512`, `cipher_plaintext_header_size
= 0`, the file's first 16 bytes are the random salt rather than `SQLite format 3`, a known plaintext
is absent from the raw bytes, and a wrong key raises `SQLITE_NOTADB`. So the crypto is doing what the
project claims.

**Fix.** Record the resolved sha256 (already in `uv.lock`) and re-check it deliberately on any bump
rather than letting `uv lock --upgrade` move it silently; if PyPI attestations are published for the
fork, check them once and note the result beside the dependency. Optional belt-and-braces: keep a
note of how to fall back to upstream `sqlcipher3` against a Homebrew SQLCipher if the fork goes
unmaintained.

---

### 8. LOW — No pinned CA bundle; `SSL_CERT_FILE` / `SSL_CERT_DIR` can redirect the trust store

**Where:** `src/bankmachine/connector/plaid/client.py:322-325` builds
`plaid.Configuration(host=..., api_key=...)` and leaves `ssl_ca_cert` at its default `None`;
`plaid/rest.py:85-98` passes that through as `ca_certs=None`. `uv.lock` contains no `certifi`.

**Measured** in the project venv:

```
verify_mode 2  check_hostname True
cert_store_stats {'x509': 128, 'crl': 0, 'x509_ca': 128}
default verify paths: cafile='/private/etc/ssl/cert.pem', openssl_cafile_env='SSL_CERT_FILE',
                      capath='/private/etc/ssl/certs', openssl_capath_env='SSL_CERT_DIR'
certifi NOT installed
```

Verification is genuinely on and the system store is loaded, so this is **not** a broken-TLS finding.
The gap is that the trust anchor set comes from OpenSSL's default paths, which honour two environment
variables — and this product's documented setup step is `source .env`, i.e. the operator routinely
imports environment into the shell that runs `bankmachine`.

**Failure scenario.** A stray `export SSL_CERT_FILE=…` (from another project's `.env`, a debugging
session, a proxy tool's setup script) silently narrows or replaces the trust anchors for every
aggregator call, with no symptom until an interception succeeds.

**Fix.** Add `certifi` and pass `ssl_ca_cert=certifi.where()` in the `plaid.Configuration`
constructor. That pins the anchor set to something the lockfile controls and makes the environment
variables inert for this client. One extra dependency, and it removes an environment-shaped
downgrade from the one channel that carries live credentials.

---

### 9. LOW — No length bound on the `cursor` argument or on a stdio frame

**Where:** `src/bankmachine/envelope.py:568-573` and `src/bankmachine/mcp.py:1758`.

```python
payload = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
```

```python
line = stdin.readline()
```

`_cursor` (`mcp.py:1206-1229`) checks the type but not the length, so an arbitrarily large string is
base64-decoded and JSON-parsed before any refusal. `_read_messages` reads an unbounded line. Deep
nesting is already handled (`RecursionError` is caught in both places, which is more care than most
servers take), and there is a frame-count cap `_MAX_UNDECODABLE_FRAMES` — size is the one dimension
left open.

**Failure scenario.** The caller is a local MCP client the operator launched, so this is
denial-of-service against oneself: a confused or looping agent sends a very large argument and the
server balloons or is killed, taking the session with it. Not a confidentiality issue.

**Fix.** Reject a `cursor` longer than a few hundred characters in `_cursor` before decoding (an
issued cursor is fixed-size, so the bound is exact rather than a guess), and read frames with a
bounded read rather than an unbounded `readline`.

---

### 10. INFORMATIONAL — `git` runs as a subprocess at import time, inside the network guard's stated blind spot

`src/bankmachine/build_id.py:78-107` shells out to `git rev-parse` and `git status` in the package
directory, at module import (`build_id.py:157`), on every CLI invocation and before every MCP
handshake. `tests/preferences/test_only_the_connector_reaches_the_network.py` is explicit in its own
docstring that "it cannot see a subprocess shelling out to `curl`", so this is the exact class the
guard does not cover. It is benign here — fixed argv, no shell, 2s timeout, `check=False`, narrow
`OSError`/`SubprocessError` catches, `stderr` truncated to 200 chars in a debug line — and `git`
running in a repository the operator owns is the operator's own trust boundary. Recorded so the
subprocess surface is on the ledger, not because it needs a change.

---

## What I checked and found SOUND

**Datastore key.**
- Generated with `secrets.token_hex(32)` — 256 bits from the OS CSPRNG (`secrets.py:60-62`).
- Reaches SQLCipher as `PRAGMA key = "x'<64 hex>'"` (`connection.py:262`), and **every** path to
  that line goes through `get_datastore_key` → `_validate`, which full-matches
  `[0-9a-fA-F]{64}` and explicitly rejects `int(key, 16)` as a parser because it would accept
  `0x`, `_`, signs and whitespace (`secrets.py:65-86`). No injection is reachable, and the
  "silently treated as a passphrase and run through the KDF" failure is the specific thing the
  validation exists to prevent. Both callers (`_writer`, `reader`) validate.
- **Never in argv or env.** No CLI subcommand accepts a key; `set_datastore_key` has no caller
  outside `secrets.py`. No environment variable reads one.
- **Never in a message.** `_validate`'s two errors name the service/account and the length, never
  the value; the docstring states the rule and the code keeps it.
- Measured cipher state: SQLCipher 4.12.0 community / openssl, page 4096, `PBKDF2_HMAC_SHA512`,
  `HMAC_SHA512`, `cipher_plaintext_header_size = 0`; file header is the random salt, not
  `SQLite format 3`; a known plaintext is not recoverable from raw bytes; a wrong key raises
  `SQLITE_NOTADB` rather than returning garbage. Raw-key mode means no KDF parameters can drift
  between the writer and the reader, exactly as documented.

**Sandbox/production isolation.** Separate keychain accounts for all three secret classes —
`datastore:{env}`, `plaid:{env}`, `connection:{env}:{item}` (`config.py:96-140`) — and separate
default filenames `store.db` / `store-sandbox.db` (`config.py:167-175`). A cross-environment mix-up
therefore **fails closed at the key** rather than silently reading or writing the wrong store. The
host is chosen by an explicit dict (`client.py:_HOSTS`) so an environment with no defined host raises
instead of defaulting. A sandbox secret cannot be sent to the production host because the secret is
fetched from the environment-scoped account. The environment is logged at WARNING on every startup in
**both** directions (`logging_setup.py:log_startup`), rides `serverInfo.title`, and rides every MCP
answer.

**Config precedence and trust.** `load_config` (`config.py:213-298`) is explicit and injectable:
env var → config file → documented default, with every numeric value range-checked and **refused
rather than clamped**. A stale or hostile `config.toml` can redirect `datastore_path`,
`keychain_service` or `log_dir`, but every such redirect either fails loudly (wrong key for the
store, or no key at all) or is announced in the startup banner and in `store status`. There is no
dotenv dependency, so no file is read that the operator did not name.

**Plaid secret and access tokens.**
- Keychain only. `secrets.py` is the sole importer of `keyring`. No environment variable exists for
  the aggregator secret, and `connector set-secret` reads it via `getpass` at a tty or from a pipe —
  never from argv (`cli/connector.py:69-81`).
- `connections.credential_ref` holds a keychain **handle** (`connection:{env}:{item_id}`), never a
  token, so a datastore backup carries no credential.
- `AccessGrant`, `LinkToken` and `LinkSession` mark the credential field `repr=False` **and**
  override `__repr__` (`connector/__init__.py`), so a token cannot reach a traceback or an
  interpolated log line via a `repr`.
- I grepped every `logger.*` and `print(` call in `src/`: no call site passes an access token, a
  secret, a public token or a link token. Enrollment logs the institution id and the credential
  handle only, and `enroll.py:487-499` explicitly documents keeping the item id out.

**The raw archive cannot hold a credential, by construction.** `Endpoint.issues_credential` marks
`/link/token/create`, `/link/token/get` and `/item/public_token/exchange`, and
`FetchedResponse.__post_init__` raises `CredentialBearingResponseError` for any such endpoint
(`connector/__init__.py`). `store.raw.record_response` derives everything it writes from a
`FetchedResponse`, so there is no object for a caller to hand it. `request_context` carries only
counts, offsets and `cursor=initial|resumed` — never a token and never an `Authorization` header.
Archived bodies (`/item/get`, `/accounts/get`, `/transactions/sync`, `/institutions/get`) do contain
item ids, account ids, masks and full transaction text, and the archive lives inside the SQLCipher
database, so it is encrypted at rest along with everything else.

**Logging.** Redaction is at the formatter (`RedactingFormatter.format`), which means it runs over
`super().format(record)` — i.e. over rendered exception text and tracebacks too, not just the
message. Three rules: labelled credentials (wider vocabulary than the tracked-file guard), any
opaque `[A-Za-z0-9_-]{32,}` run, and 8+ digit runs reduced to `****<last4>`. I read the operator's
real 950-line log: 49 redactions present, no token, no secret, no transaction description, no
amount, no account number. The home directory is elided from the startup banner
(`_display_path`). The SQLAlchemy tracebacks in it render `[SQL: …]` with `?` placeholders and no
bound parameters, and the bound parameters that a different error class *would* render are dates,
limits and account ids rather than row content. Log failures never take the process down.

**MCP input validation** — this is the strongest part of the surface.
- Unknown argument names are **refused, not dropped** (`mcp.py:1241-1250`), with the reasoning
  written down: a misspelled `since` returning the all-time aggregate is indistinguishable from the
  window that was asked for.
- `arguments` is typed `dict[str, object]`, not `dict[str, Any]`, so an unnarrowed value cannot
  reach the query layer at all — the type checker enforces the narrowing.
- Dates parsed with `date.fromisoformat`; integers reject `bool` (which is an `int` in Python and a
  JSON value a caller can send) and are range-checked and **refused rather than clamped**;
  `group_by` is checked against a closed set (`BadGroupingError`); an inverted window is refused.
- `limit` is hard-capped at `MAX_ROWS = 500`, and paging narrows rather than lifts the cap.
- Cursors are scheme-tagged and fingerprinted against `(since, until, account_id)` with blake2s, so
  a cursor from a different question is refused rather than answered; `"t": true` is explicitly
  rejected.
- `tools/call` resolves the tool name **before** dispatch, so a `KeyError` from beneath the query
  layer cannot be reported as "no such tool".

**No SQL injection surface.** `query.py` (2517 lines) contains no `text()`, no `exec_driver_sql`,
no `literal_column`, no f-string SQL, no `getattr`/`eval`/`exec` — everything is SQLAlchemy Core
expression objects. The only raw SQL in the tree is: the `PRAGMA key` with a validated hex key,
fixed `PRAGMA`/`BEGIN IMMEDIATE`/`COMMIT`/`ROLLBACK` literals, `f"SELECT MAX(version) FROM
{SCHEMA_VERSION_TABLE}"` over a module constant, and `conn.execute("VACUUM INTO ?", (str(dest),))`
— which uses a bound parameter with a comment explaining exactly why.

**No path traversal, and no file read, on the resource surface.** `_reference_documents()` builds
both documents in memory from the tool definitions and the warning vocabulary; `resources/read`
matches `params["uri"]` by **equality** against that fixed list and otherwise returns
`_INVALID_PARAMS` naming what is served (`mcp.py:1483-1536`). Nothing opens a file, so a missing or
unreadable datastore cannot break the reference surface either.

**No leak through error text on the wire.** `_unservable_remedy` (`query.py:1110-1192`) branches on
the `DatastoreProblem` enum, never on prose, and **deliberately carries no datastore path** — the
comment records that the absolute path contains the operator's account name and that it got back in
once through the generic branch. The broad catch at `mcp.py:1662-1678` never puts the exception on
the wire; it logs it and returns a fixed remedy sentence, with the reasoning stated (a SQLAlchemy
error stringifies to the failing SELECT and its parameters). Nothing on the wire carries an access
token, an item id, `credential_ref`, the datastore path, or the operator's name or email. Account
masks (last 4) and institution names do ride `list_accounts`, which is intended and is what
AC-10.3 permits.

**Read-only enforcement is structural.** `store/connection.py` is the only module that constructs a
handle. Readers open `file:…?mode=ro` — the refusal is in the file handle where `PRAGMA
query_only = OFF` cannot reach it — with `query_only=ON` as a second layer. The single writer factory
takes an exclusive `flock` before it returns. `mode=rw` (not `rwc`) means no component can create a
datastore implicitly; only `initializing_writer` may. The schema version is checked in both roles and
the two mismatch directions are separate values with opposite remedies.

**Network boundary.** Only two destinations exist, `plaid.Environment.Sandbox` and
`.Production`, selected by dict lookup. `verify_ssl` defaults to `True` → `ssl.CERT_REQUIRED`, and
`assert_hostname` is left at `None` so urllib3's hostname checking applies (measured:
`check_hostname True`, 128 CA certificates loaded). `plaid/rest.py` only builds a `ProxyManager` when
`configuration.proxy` is set, which this code never sets — so `HTTP_PROXY`/`HTTPS_PROXY` are ignored
and cannot silently interpose. Every call carries `_request_timeout=30.0`. Retries are bounded to 4
attempts with a 60s clamp that a hostile or mistaken `Retry-After` cannot exceed (the reasoning —
that a 3600s header would hang a nightly sync — is written down). Non-idempotent endpoints are forced
to a single attempt via `Endpoint.retry_safe=False`, and urllib3's own default `Retry` does not
retry POST on read errors, so that decision is not undermined a layer down. `_preload_content=False`
is handled correctly: the body is read inside the `try` so a mid-stream reset becomes a
`TransportError` rather than a bare `OSError`, and `raw.release_conn()` runs in a `finally`. A known
`plaid-python` bug (an `AttributeError` from `e.body.decode()` on a bodyless `ApiException`) is caught
narrowly, only when the SDK's own exception is the `__context__`. The import-scan guard keeps every
network transport out of everything but `connector/`, with a positive control so a green run means
"contained" rather than "absent".

**Repo hygiene.** `.env` is untracked, ignored, and mode 0600, and contains only
`BANKMACHINE_PLAID_CLIENT_ID` — no secret, as designed. `deployment/` is untracked and ignored.
`git ls-files` shows no datastore, log, roster or credential file. Test fixtures are Plaid sandbox
data (`"Plaid Checking"`, `mask "0000"`, `institution_id "ins_130958"`, error bodies with sandbox
request ids) — nothing real. The leak guard runs clean against the working tree ("11 tokens over
working tree"), loads its tokens from the gitignored `deployment/` directory so a published clone
inherits no stranger's identity, validates tokens at load, carries a canary positive control, and
fails closed on every error path (exit 2 distinct from exit 1). The pre-push hook wires it over the
**commit range** rather than the tip, and refuses to run at all if the guard is missing or
non-executable. `pyproject.toml` carries no `authors` field, deliberately.

**Dependencies.** `uv.lock` is present and pins all 35 packages with sha256 for every artifact. The
dependency set is genuinely small — `keyring`, `plaid-python`, `sqlalchemy`, `sqlcipher3-wheels`,
`urllib3` — with no telemetry, analytics or error-reporting package anywhere in the graph, which is
the norm holding. `plaid-python` is at 44.0.0 (uploaded 2026-09-01), `urllib3` 2.7.0, `sqlalchemy`
2.0.52, `keyring` 25.7.0 — all current, none with a known issue I could identify.

---

## Suggested order for tomorrow morning

1. Copy the production datastore key out of Keychain Access into the password manager, immediately
   after `bankmachine store init` and **before** `bankmachine enroll`. (Finding 1.)
2. Add the untrusted-text paragraph to `_instructions` and the envelope reference. Roughly ten
   minutes, and it is the only finding whose window opens precisely when real accounts connect.
   (Finding 2.)
3. `os.umask(0o077)` at both entry points. One line. (Finding 3.)
4. Run `bankmachine store backup` once after the first successful production sync, and confirm the
   copy is verified — it is the rehearsal the operational spec says has never happened.

Findings 4-9 are worth a follow-up cycle and none of them gate the enrollment.
