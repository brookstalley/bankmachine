# MCPlaid

> **Working name.** `MCPlaid` embeds a third-party trademark and locks the project to one
> aggregator; a rename is decided but not yet chosen, and must land before the first build step
> fixes the package name, the keychain service name and the scheduler label.

A locally-hosted service that pulls **your own** financial data from an aggregator API and from
file imports into an encrypted local datastore, and exposes it read-only over MCP — so an analyst
agent can answer period-over-period questions without a browser or a CSV in the loop, **and can
independently establish that the underlying data is complete and fresh before it answers.**

It is a data pipeline. It does no budgeting, forecasting, categorization or advice.

**Status: pre-implementation.** The requirements are specified and reviewed; no code exists yet.
See `docs/system-requirements.md` for what is being built and `docs/build-vs-adopt-investigation.md`
for why it is being built rather than adopted.

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
| `scripts/` | the repository leak guard and its self-test |
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
./scripts/check-no-personal-data.selftest.sh   # 15 cases, all must pass
./scripts/check-no-personal-data.sh            # scan the working tree
```

A clone with no `deployment/` directory has no tokens and nothing to leak, so the guard passes with
a note. That is expected, and it is why the guard is safe to publish. When you fill in your own
roster, `docs/deployment-requirements.template.md` §8 says what the token files look like.

## License

Not yet chosen — see the open items in `docs/build-vs-adopt-investigation.md` §7.
