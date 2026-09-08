# bankmachine

**Read-only.** It never moves money, and has no write path to any bank or aggregator — see
[What it is not](#what-it-is-not).

A locally-hosted service that pulls **your own** financial data from an aggregator API and from
file imports into an encrypted local datastore, and exposes it read-only over MCP — so an analyst
agent can answer period-over-period questions without a browser or a CSV in the loop, **and can
independently establish that the underlying data is complete and fresh before it answers.**

It is a data pipeline. It does no budgeting, forecasting, categorization or advice.

**Status: build step 1 of ten is complete, and step 2 has begun** (`docs/system-requirements.md`
§8). The encrypted datastore, its two connection roles, the core schema, the raw-response archive
with a rebuild that verifies its own output, `store backup` (a verified single-file encrypted copy
taken under the writer lock), and `sync shell` — an authenticated SQL prompt, since page encryption
breaks ordinary SQL tooling — all exist and are tested. The aggregator client's
walking skeleton has since landed and been run against the real sandbox: `bankmachine connector
check` makes one authenticated call and archives the answer verbatim. Enrollment has since landed:
`bankmachine enroll` prints a hosted URL, waits while you complete it in a browser, and records the
connection — with `bankmachine connections list` and `connections retire` alongside it, and
`bankmachine sync run` fetches each connection's accounts and transactions. `sync shell` is how you
look at any of it: page encryption breaks ordinary SQL tooling, so the datastore has its own
authenticated prompt. See `docs/system-requirements.md` for what is being built
and `docs/build-vs-adopt-investigation.md` for why it is being built rather than adopted.

## What it is not

- **Not a hosted service.** Every user runs their own copy against their own aggregator
  credentials. No financial data reaches this project, its author, or any third party.
- **Not multi-user.** One operator per deployment, by design.
- **Not write-capable.** Read-only, permanently — no mutation tools on the MCP surface, and no
  write path to any aggregator or institution.
- **Not financial advice.** It moves data. Every conclusion drawn from that data is the reader's.

## The design commitment worth knowing about

🔴 **Silent staleness is the failure mode this project is built to prevent.** A feed that quietly
stopped three weeks ago produces confidently wrong analysis, not an error. So every response
carries a freshness stamp, any result computed over a known gap or a degraded connection carries an
explicit warning, and no analysis is supposed to begin until the verification gate in
`docs/system-requirements.md` §7 passes.

If you use this, read that section. It is the deliverable, not a formality.

## Repository layout

| Path | Holds |
|---|---|
| `docs/system-requirements.md` | the engine — provider-agnostic, names no financial institution |
| `docs/deployment-requirements.template.md` | the shape of one operator's roster, carrying no data |
| `docs/build-vs-adopt-investigation.md` | why clean-slate, with six candidates read at source level |
| `src/bankmachine/` | the product. `store/` is the only module that opens the datastore |
| `tests/` | mirrors the source tree; `tests/preferences/` holds the norm tests and the leak guard |
| `deployment/` | **gitignored** — your roster, your match tokens. Never committed. |

## Setup for contributors and forks

🔴 **Enable the git hooks. Nothing does this for you** — a hooks directory is per-clone config and
cannot be committed, so a fresh clone pushes unguarded until you run:

```sh
git config core.hooksPath .githooks
```

That wires two pre-push guards: a leak guard that refuses to push a commit carrying roster or
operator identity, and a gitflow guard that keeps `main` release-only.

Verify the leak guard works in your clone:

```sh
./tests/preferences/check-no-personal-data.selftest.sh   # 22 cases, all must pass
./tests/preferences/check-no-personal-data.sh            # scan the working tree
```

`uv run pytest tests/preferences` runs both from the test suite, which is the entry point that
does not need each clone to have configured `core.hooksPath`.

A clone with no `deployment/` directory has no tokens and nothing to leak, so the guard passes with
a note. That is expected, and it is why the guard is safe to publish. When you fill in your own
roster, `docs/deployment-requirements.template.md` §8 says what the token files look like.

## Aggregator credentials

The client id is configuration. The secret is not, and has no environment variable at all — it goes
in the OS keychain, per environment, so it reaches neither a shell history nor a process listing.

```sh
cp .env.example .env               # fill in BANKMACHINE_PLAID_CLIENT_ID
source .env                        # nothing reads this file for you — see below
bankmachine connector set-secret   # prompts without echoing; accepts a pipe
bankmachine store init
bankmachine connector check        # the smallest authenticated call, archived verbatim
```

🔴 **`.env` is a file you `source`, not one the product loads.** There is no dotenv dependency:
a file that is silently read is a file whose contents are silently trusted. The same keys live in
`~/.config/bankmachine/config.toml` without the `BANKMACHINE_` prefix if you would rather not
source anything, and `.env.example` lists every one of them with its default.

Sandbox needs nothing but a free dashboard signup, and is the default environment. The secret is
per environment: setting sandbox's cannot overwrite production's.

## License

Not yet chosen — see the open items in `docs/build-vs-adopt-investigation.md` §7.
