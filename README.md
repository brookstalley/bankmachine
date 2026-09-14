# bankmachine

Read-only local-first personal finance datastore and MCP server. It pulls your own
financial data from Plaid into an encrypted local datastore and serves it to an agent
over MCP, so you can ask period-over-period questions without a browser or a CSV in
the loop. It is a data pipeline: no budgeting, forecasting, categorization or advice.

## Read this first

**1. This connects to your real bank accounts.** Through Plaid, with your own credentials.
Real balances, real transactions, real merchant names — the actual ledger, not a copy of it.
Sandbox is the default and uses fake data; production is a deliberate, separate procedure.

**2. Using the MCP server sends your financial data to your AI provider.** Every tool call
hands rows to whatever model your client is wired to. If that is a hosted model, your
transactions go to that company under their terms, not this project's. Check your settings for
training, personalization, retention and human review before you connect anything. This is not
a side effect — it is what the product does.

**3. Read the code before you run it. Have your AI read it too.** By design bankmachine does
not phone home: no telemetry, no analytics, no error reporting, and the only network destination
is Plaid's API. That claim is worth exactly as much as your own verification of it, so verify it.
`tests/preferences/test_only_the_connector_reaches_the_network.py` is where it is enforced, and it
has limits it states itself — it cannot see a subprocess shelling out, or a dependency phoning
home on its own.

**It never moves money.** There is no write path to any bank or aggregator, and no
mutation tools on the MCP surface.

🔴 **Every answer says how far to trust it.** Responses carry the environment they came
from, a freshness stamp, coverage counts, which build answered, and warnings — `stale`,
`degraded`, `gapped`, `partial`. A `gapped` warning means the institution granted less history than was asked
for, so older data is *absent rather than zero*. Silent staleness is the failure mode
this project exists to prevent, and `docs/system-requirements.md` §7 is the deliverable
rather than a formality.

**Scope:** one developer, one machine, your own Plaid credentials. Not a service, not
multi-user, not distributed.

**Status:** enrollment, sync and the MCP server work end to end — build steps 1–4 of
`docs/system-requirements.md` §8 are complete. Seven of the eight specified MCP tools are serving
(§5); the other one is recorded as descoped in `.prawduct/artifacts/api-contract.md`.
Step 5 is built (balances, holdings and investment transactions land on every sync). Step 6's repair
has landed — `connections reauth <id>` renews an expired login through an update-mode session
without losing the connection's history or cursor — alongside the connection health and
per-connection error handling that were already there. **Not started:** scheduling (step 8 —
run `sync run` yourself, daily), the verification gate, and the file-import path.

## Install

Needs Python 3.14 — `.python-version` pins it and `uv` will fetch it — and
[uv](https://docs.astral.sh/uv/). Every dependency resolves to a wheel except `plaid-python`, which
is an sdist and so runs its own build at install time; `uv.lock` pins it by hash.

```sh
git clone <your remote> && cd bankmachine
git config core.hooksPath .githooks
uv sync
uv run pytest tests/preferences      # the guards' own self-tests
```

🔴 **The hooks line is not optional.** A hooks directory is per-clone config and cannot
be committed, so a fresh clone pushes unguarded until you run it. It wires two pre-push
guards: one that refuses to push a commit carrying roster or operator identity, and a
gitflow guard that keeps `main` release-only.

The suite above proves the guards run; on a fresh clone it does not prove anything is being
matched, because the leak guard reads its match tokens from a gitignored `deployment/` directory
that a new clone does not have, and says so rather than passing silently.

On a pull request into `develop` or `main`, `.github/workflows/check.yml` runs the full gate —
pytest, `ruff check`, `ruff format --check`, mypy — on a macOS runner. That is a report, not a
barrier: requiring a status check needs branch protection, which is paid on private repositories,
so the pre-push guards above remain the only thing that actually refuses a push.

The gitflow guard is this repository's own release policy, not a safety property: it rejects a
push to `main` that is not a merge of a `release/*` or `hotfix/*` branch. If you fork and work
differently, `git push --no-verify` bypasses both guards for one push — that is the documented
escape and the hook says so itself — or drop the hooks with `git config --unset core.hooksPath`
and run `tests/preferences/check-no-personal-data.sh` yourself, so you keep the guard that is
about data rather than the one that is about branches.

## Configure

Sandbox needs nothing but a free Plaid dashboard signup — but the environment has to be
**declared**, not defaulted: anything that opens the datastore for writing, or changes a
per-environment keychain entry, refuses when nothing chose one. `.env.example` declares it, so
the block below works as written; a cron entry sources nothing and needs it in the config file
instead. The reasoning is further down this section.

```sh
cp .env.example .env                        # fill in BANKMACHINE_PLAID_CLIENT_ID
source .env                                 # nothing reads this file for you
uv run bankmachine connector set-secret     # prompts without echoing; accepts a pipe
uv run bankmachine store init
uv run bankmachine connector check          # smallest authenticated call, archived verbatim
```

🔴 **Back the datastore key up now, before there is any data.** It is minted once, by `store init`,
and it cannot be recovered from the datastore — a backup without it is a backup of noise.

```sh
uv run bankmachine store key export     # prints it; refuses a redirected or piped stdout
uv run bankmachine store key verify     # confirms the copy you stored opens THIS datastore
```

Put it in a password manager, and ideally on paper. **Then run `verify`, and do not skip it:** a
transposed pair of hex digits is still 64 characters of valid hex and is simply a different key, so
a bad transcription looks exactly like a good one until the day the store will not open. `verify`
is the only thing that closes that gap, and it costs ten seconds.

If a machine's keychain ever loses the key, `store key import` puts it back — it opens the
datastore with what you paste *before* it writes anything, so a typo cannot overwrite a working
entry. These act on the environment you are pointed at (`BANKMACHINE_ENVIRONMENT`), so the sandbox
and production keys are separate entries and separate exports.

🔴 **`.env` is a file you `source`, not one the product loads.** There is no dotenv
dependency, on purpose: a file that is silently read is a file whose contents are
silently trusted. The same keys live in `~/.config/bankmachine/config.toml` without the
`BANKMACHINE_` prefix if you would rather not source anything. `.env.example` lists
every setting with its default.

🔴 **Anything that opens the datastore for writing, or that changes a per-environment
keychain entry, needs the environment DECLARED, and will exit 2 rather than guess.** Every per-environment container is keyed on it —
`datastore:<env>` and `plaid:<env>` in the keychain, `connection:<env>:<item>`, and the
datastore filename — so a write on an environment nobody chose would land in whichever
one the fallback names. Reads (`store status`, `connections list`, the MCP server) still
fall back to sandbox. **A process with no login shell has nothing to `source`, so put it
in the config file:**

```toml
# ~/.config/bankmachine/config.toml
environment = "sandbox"
```

🔴 **The aggregator secret has no environment variable and will not get one.** It goes
in the OS keychain, per environment, so it reaches neither a shell history nor a process
listing — and setting sandbox's cannot overwrite production's.

## Run

```sh
uv run bankmachine enroll             # prints a hosted URL, waits while you link in a browser
uv run bankmachine sync run           # fetch accounts and transactions; resumes if interrupted
uv run bankmachine connections list   # `retire <id>` also stops the aggregator billing for it
uv run bankmachine store status       # prints the datastore path it actually resolved
uv run bankmachine store backup ~/backups/bankmachine-$(date +%F).db
```

🔴 **Nothing schedules any of this yet.** `sync run` is manual, and each run is also what appends
today's row to the daily balance series — the one table no later re-sync can rebuild, because no
aggregator backfills it. A day you do not run it is a day of balance history gone. Run it daily —
`sync run --until-ready` keeps going until no connection still owes history, rather than asking
you to re-run it yourself. A `cron` or `launchd` entry of your own is fine until scheduling ships
(build step 8), but 🔴 **it inherits no login shell, so it must get the environment from the config
file above** — an entry relying on `source .env` writes nothing and exits 2 every night, and the
daily balance row is the one series no later re-sync can rebuild. There is no webhook: a connection
whose login has expired surfaces on the next run, not before it.

🔴 **`store backup` is not `cp`.** It takes the writer lock, folds the WAL in and verifies the copy
by reopening it with the key; a plain `cp` silently loses whatever is still in the WAL. The copy is
ciphertext and is useless without the key from Configure. Back up before every `git pull` that
lands a migration — afterwards the old build can no longer produce one.

**Connecting real accounts is a separate procedure.** Sandbox needs nothing but a signup;
production needs work in the aggregator's dashboard first, and two of its steps cannot be undone.
Read [`docs/first-production-connection.md`](docs/first-production-connection.md) before you start.

## Wire it to an MCP client

Register the server per project. For Claude Code:

```sh
claude mcp add bankmachine-sandbox --env BANKMACHINE_ENVIRONMENT=sandbox \
  -- "$(which uv)" run --directory /absolute/path/to/bankmachine bankmachine mcp
```

🔴 **The server is a subprocess your client launches, so it runs whatever code existed
at connect time.** After changing the code you must restart the client session before
anything can exercise the change — including an agent you have testing it, which will
otherwise report a confident pass against the old build.

🔴 **`--directory` pins which checkout gets served, regardless of where the client sits.**
Point it at a worktree deliberately, or you will test one branch believing you tested
another.

**[`docs/connecting-an-mcp-client.md`](docs/connecting-an-mcp-client.md) is the full page** —
Claude Desktop and generic JSON configuration, how `BANKMACHINE_ENVIRONMENT` decides whether you
are looking at sample data or real money, every tool, and how to read the warnings before you
believe a number.

## What leaves this machine

**Outbound traffic from this product goes to exactly one place: the aggregator's API.** No
telemetry, no analytics, no error reporting. The datastore is encrypted at rest and a backup copy
is ciphertext.

🔴 **The MCP client is the exception, and it is not a small one.** Every tool call hands rows —
merchant names, descriptions, amounts, balances, account names and masks — to whatever model your
client is wired to. If that client talks to a hosted model, **your financial data goes to that
provider**, under their terms rather than this project's. That is the point of the product; it is
also the one place the "never leaves the machine" property stops holding. Choose the client
accordingly.

🔴 **Treat transaction text as untrusted input to your agent.** Descriptions and merchant names are
written by third parties and passed through verbatim, so anyone who can move a cent to you chooses
a string that lands in your model's context. There are no mutation tools here, so the worst case is
a wrong answer or a nudged agent rather than a moved dollar — but do not let an agent act on
instructions it found in a transaction.

## Repository layout

| Path | Holds |
|---|---|
| `docs/README.md` | the index to everything below, and what `.prawduct/` is |
| `docs/first-production-connection.md` | going to production: prerequisites, the cutover, day-one checks |
| `docs/connecting-an-mcp-client.md` | wiring an MCP client, and how to read what it answers |
| `docs/system-requirements.md` | the engine — provider-agnostic, names no financial institution |
| `docs/deployment-requirements.template.md` | the shape of one operator's roster, carrying no data |
| `docs/build-vs-adopt-investigation.md` | why clean-slate, with six candidates read at source level |
| `src/bankmachine/` | the product. `store/` is the only module that opens the datastore |
| `tests/` | mirrors the source tree; `tests/preferences/` holds the norm tests and the leak guard |
| `deployment/` | **gitignored** — your roster, your match tokens. Never committed. |

## Licence

No licence has been chosen yet, so default copyright applies.
