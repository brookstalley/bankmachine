# bankmachine

Read-only local-first personal finance datastore and MCP server. It pulls your own
financial data from Plaid into an encrypted local datastore and serves it to an agent
over MCP, so you can ask period-over-period questions without a browser or a CSV in
the loop. It is a data pipeline: no budgeting, forecasting, categorization or advice.

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

**Status:** build steps 1–4 are complete and four of the ten specified MCP tools are
serving (`docs/system-requirements.md` §8); the other six are recorded as descoped in
`.prawduct/artifacts/api-contract.md`.

## Install

Needs Python ≥3.11 and [uv](https://docs.astral.sh/uv/). Every dependency is a wheel.

```sh
git clone <your remote> && cd bankmachine
git config core.hooksPath .githooks
uv sync
uv run pytest tests/preferences      # confirms the pre-push guards work in this clone
```

🔴 **The hooks line is not optional.** A hooks directory is per-clone config and cannot
be committed, so a fresh clone pushes unguarded until you run it. It wires two pre-push
guards: one that refuses to push a commit carrying roster or operator identity, and a
gitflow guard that keeps `main` release-only.

## Configure

Sandbox is the default environment and needs nothing but a free Plaid dashboard signup.

```sh
cp .env.example .env                        # fill in BANKMACHINE_PLAID_CLIENT_ID
source .env                                 # nothing reads this file for you
uv run bankmachine connector set-secret     # prompts without echoing; accepts a pipe
uv run bankmachine store init
uv run bankmachine connector check          # smallest authenticated call, archived verbatim
```

🔴 **`.env` is a file you `source`, not one the product loads.** There is no dotenv
dependency, on purpose: a file that is silently read is a file whose contents are
silently trusted. The same keys live in `~/.config/bankmachine/config.toml` without the
`BANKMACHINE_` prefix if you would rather not source anything. `.env.example` lists
every setting with its default.

🔴 **The aggregator secret has no environment variable and will not get one.** It goes
in the OS keychain, per environment, so it reaches neither a shell history nor a process
listing — and setting sandbox's cannot overwrite production's.

## Run

```sh
uv run bankmachine enroll             # prints a hosted URL, waits while you link in a browser
uv run bankmachine sync run           # fetch accounts and transactions; resumes if interrupted
uv run bankmachine connections list   # `retire <id>` also stops the aggregator billing for it
uv run bankmachine store status       # prints the datastore path it actually resolved
```

## Wire it to an MCP client

Register the server per project. For Claude Code:

```sh
claude mcp add bankmachine-sandbox --env BANKMACHINE_ENVIRONMENT=sandbox \
  -- uv run --directory /absolute/path/to/bankmachine bankmachine mcp
```

🔴 **The server is a subprocess your client launches, so it runs whatever code existed
at connect time.** After changing the code you must restart the client session before
anything can exercise the change — including an agent you have testing it, which will
otherwise report a confident pass against the old build.

🔴 **`--directory` pins which checkout gets served, regardless of where the client sits.**
Point it at a worktree deliberately, or you will test one branch believing you tested
another.

## Repository layout

| Path | Holds |
|---|---|
| `docs/system-requirements.md` | the engine — provider-agnostic, names no financial institution |
| `docs/deployment-requirements.template.md` | the shape of one operator's roster, carrying no data |
| `docs/build-vs-adopt-investigation.md` | why clean-slate, with six candidates read at source level |
| `src/bankmachine/` | the product. `store/` is the only module that opens the datastore |
| `tests/` | mirrors the source tree; `tests/preferences/` holds the norm tests and the leak guard |
| `deployment/` | **gitignored** — your roster, your match tokens. Never committed. |
