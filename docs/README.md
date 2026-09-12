# Docs

| Page | Read it when |
|---|---|
| [`first-production-connection.md`](first-production-connection.md) | you are about to connect real accounts — dashboard prerequisites, the ordered cutover, day-one checks, the daily routine |
| [`connecting-an-mcp-client.md`](connecting-an-mcp-client.md) | you are wiring an agent to the datastore, or reading what it answers |
| [`system-requirements.md`](system-requirements.md) | you want the engine's acceptance criteria — the contract of record, provider-agnostic |
| [`deployment-requirements.template.md`](deployment-requirements.template.md) | you are writing down your own roster of institutions, which stays out of git |
| [`build-vs-adopt-investigation.md`](build-vs-adopt-investigation.md) | you want to know why this was built rather than forked, with six candidates read at source level |

## Working on the code

```sh
git clone <your remote> && cd bankmachine
git config core.hooksPath .githooks   # per clone; hooks directories are not committed
uv sync
uv run pytest                          # the whole suite
uv run mypy                            # strict, project-wide
uv run ruff check && uv run ruff format --check
bash scripts/check.sh report.xml       # all four at once, the way the gate runs them
```

The individual commands are for while you are editing. `scripts/check.sh` is the one the
governance gate launches: it runs all four, reports every one that went red rather than
stopping at the first, and writes each failure into the JUnit report it is handed — the
evidence record is built from that report, so a check that only failed the exit code
would not be recorded anywhere.

`tests/` mirrors `src/bankmachine/`. `tests/preferences/` is different in kind: those are the norm
tests — the rules this project holds itself to, each written so that violating the norm turns the
test red. The leak guard (`tests/preferences/check-no-personal-data.sh`) also runs pre-push, and on
a clone with no `deployment/` directory it has no tokens to match on and says so.

## What `.prawduct/` is

`.prawduct/` is this project's governance trail: build plans, discovery notes, review rounds, a
change log and a backlog. It is committed so the owner can audit how a decision was reached, and it
is **not an entry point** — most of it is the record of one session's work rather than a statement
of what is true now. `CLAUDE.md` is addressed to the tooling the owner uses and will not run
elsewhere; nothing in this repository depends on it.

Four files under `.prawduct/artifacts/` are durable contracts rather than session records, and an
outsider reading the code will want them:

| Artifact | What it fixes |
|---|---|
| `artifacts/api-contract.md` | the MCP surface of record — tools, arguments, the closed warning vocabulary, what the server refuses to answer |
| `artifacts/data-model.md` | the tables, the sign convention, money as integer minor units, and the two kinds of date |
| `artifacts/security-model.md` | the threat model, what is encrypted, what is trusted, and what leaves the machine |
| `artifacts/operational-spec.md` | backup, restore, the upgrade order across a schema migration, and what is not re-derivable |

`docs/system-requirements.md` § 5 is where the tool surface is specified, and `api-contract.md`
holds the same contract in detail. Neither can drift alone:
`tests/preferences/test_the_documented_tool_surface_is_the_built_one.py` holds both of them, this
directory's client guide and the README against the tools the server actually serves.

## Licence

No licence has been chosen yet, so default copyright applies.
