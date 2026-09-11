---
artifact: build-plan
version: 2
scope: environment-guard
branch: fix/environment-guard-and-until-ready
depends_on:
  - artifact: operational-spec
  - artifact: api-contract
  - artifact: security-model
governed_by:
  - artifact: operational-spec
    dispositions:
      - "no filesystem path is hardcoded → conforms; the refusal message interpolates `default_config_path()` and elides the operator's home through the same `_home` seam the paths resolve through, it never spells a literal path"
      - "a backup destination is never created implicitly and never overwritten → conforms, and the plan DOES touch backup after all: `copying_writer` routes through the guarded `_writer`, so `store backup` refuses on a defaulted environment. Nothing about the destination's creation or overwrite behaviour changes; the refusal happens before a destination is opened"
  - artifact: api-contract
    dispositions:
      - "the CLI's exit code is a contract (0/1/2, plus 75 'ran and still owes work'); 1 outranks 75; the code is carried on the exception, not decided by the caller → conforms; the refusal is a `ConfigError` subclass inheriting the existing 2 mapping rather than choosing one, bad flag VALUES are argparse usage errors (also 2), and `--until-ready` returns the inner run's code unchanged"
      - "the MCP surface is read-only → conforms; the guard exempts reads, so no MCP tool changes behaviour"
      - "every response carries a freshness stamp, and incompleteness rides the success path as a warning → inapplicable because this plan emits no MCP response"
      - "a tool's boundary is drawn where the answer shape changes → inapplicable because this plan adds no tool"
      - "a stored balance is reported with its lifecycle, and no total over balances is emitted without it → inapplicable because this plan reports no balance"
  - artifact: architecture
    dispositions:
      - "retry/backoff applies on exactly one channel, the aggregator HTTPS one → conforms; `--until-ready` re-invokes the whole command and adds no retry inside the datastore channel"
      - "every writable handle comes from the one writer factory, and that factory takes the exclusive lock → conforms, and this plan leans on it: the guard is placed IN that factory precisely because the norm makes it the single point every write passes through"
      - "every read-role handle is opened read-only at the file → conforms; `reader` is untouched and is the guard's one deliberate exemption"
      - "no component creates the datastore implicitly → conforms, and is strengthened: `initializing_writer` now also refuses when no environment was chosen, so an implicit creation cannot happen under a defaulted environment either"
      - "a process that does not recognize the datastore's schema version refuses to serve, loudly → inapplicable because this plan changes no schema check; the environment guard runs before it and is a separate refusal"
partition: serial — chunk 02 re-enters the function chunk 01 guards, and chunk 03 is documentation the first two make true
last_validated: 2026-09-11
---

# Build Plan: A Defaulted Environment Cannot Receive a Write

Closes brookstalley/bankmachine#98 and #97, and discharges the mislabel correction
carried in #95.

## Problem

Two operability defects found during the first production cutover, 2026-09-11.

**#98.** `environment` falls back to `sandbox` when nothing chose it
(`src/bankmachine/config.py:235`). Every per-environment container is keyed on that
value — `plaid:<env>` and `datastore:<env>` in the keychain, and the datastore
filename itself — so a command run in a shell that forgot to export lands its write
in a container nobody selected. The production runbook § 3.1 names the resulting
footgun outright: `connector set-secret` in an unexported shell overwrites the
*other* environment's secret and reports success. The overwrite is unrecoverable.

**#97.** `sync run` exits 75 when a backfill is still arriving and the documented
remedy is "run it again". The command knows it is unfinished and makes a person be
the loop.

## Two premises in #98 that the code does not support

Recorded here because both changed the shape of this plan, and the issue text still
carries them.

1. **"the CLI needs two shell exports."** It does not. `load_config` already resolves
   both `environment` and `plaid_client_id` from `~/.config/bankmachine/config.toml`
   (`config.py:198-250`), env var first and file second. No config file exists on the
   operator's machine and the runbook teaches the export path, so the daily annoyance
   is real — but it is a documentation gap, not a missing mechanism. Chunk 03 closes
   it; no config-reading code is written.

2. **"reads no `.env`", proposed as a defect to fix.** `operational-spec.md:123` is a
   standing norm: *"`.env` is not loaded automatically, on purpose. There is no dotenv
   dependency: a file that is silently read is a file whose contents are silently
   trusted."* **This plan does not depart from that norm** — it is the same argument
   the guard below makes, one layer out. `.env` stays un-read; the durable answer for
   an operator who does not want to export is the config file, which is what chunk 03
   documents.

## What the guard is, and why it is not a list of commands

The hazard is not that one named command is dangerous. It is that *state keyed to an
environment can be written on an environment nobody chose*. A guard written as a list
of guarded command names decays the moment a new writer is added and nobody remembers
the list — `learnings.md`, *"a guarantee defined by an enumeration decays"*.

So the property is carried where the writes actually happen:

- the six per-environment keychain mutators in `secrets.py` (`set_`/`delete_` for the
  datastore key, the aggregator secret, and an access token), and
- `store/connection._writer`, which its own docstring calls *the one writer factory* --
  every datastore write arrives through it, `store init` and the migration runner
  included. The guard sits there rather than one layer up at
  `engine.writer_connection`, because `initializing_writer` does not pass through that
  layer and creating the wrong environment's datastore is the same hazard.

**Three handles route through it, not two** -- `writer`, `initializing_writer` and
`copying_writer`. The third is `store backup`'s, so backup refuses on a defaulted
environment. That is the intended reading of the rule: a backup taken against the wrong
environment is indistinguishable from a good one and announces itself only at a restore.
`tests/test_environment_guard.py` walks the module for handles rather than naming them,
so a fourth cannot arrive unrecorded -- which is exactly how this one nearly did.

Reads are untouched. `store status`, `connections list`, `store key export|verify`,
`sync shell` and the MCP server keep defaulting to sandbox, which is what makes this
safe to ship without breaking an existing sandbox workflow.

`Config.environment_source` records where the value came from, and a Config built
directly — as the suite does — is `"argument"`, i.e. chosen. Only the fallback inside
`load_config` produces an unchosen one.

[ASSUMPTION: `sync run` is inside the guard, because it writes into the
per-environment datastore. This is the one inclusion that touches automation, and the
consequence is deliberate: an unattended run against an environment nobody declared is
exactly the case #98 was filed about, and it now fails loudly at exit 2 instead of
filling the wrong datastore. #98's own acceptance — "an unattended run can be
configured without exports" — is satisfied by the config file, which counts as chosen.
Veto-able: the alternative is to exempt read-mostly writers by name, which is the
enumeration this plan just argued against.]

## Success

- A command that writes per-environment state refuses when the environment was
  defaulted, and the refusal names both ways to choose one.
- `sync run --until-ready` returns 0 only when no connection still owes history.
- The runbook stops teaching exports as the only path, and stops telling the operator
  to re-run by hand.

## Out of scope

- The identity fallback itself (#95) — `stage: research`, and its own evidence says a
  heuristic is unsafe to design until more than one real institution has been seen.
  Only the mislabel it flags is discharged here.
- `bankmachine status` (#96) — still `stage: requirements`.
- Reading `.env`; a `--env-file` flag (`--config PATH` already points at another file);
  a general settings redesign; moving secrets out of the keychain.

## Chunks

### Chunk 01 — a defaulted environment refuses a per-environment write

**Delivers**

- `EnvironmentSource` and `Config.environment_source`, defaulting to `"argument"` so a
  directly-constructed Config is chosen by construction.
- `Config.environment_chosen`.
- `UnchosenEnvironmentError(ConfigError)` — inherits the existing exit-2 mapping in
  `cli/__init__.py` rather than choosing a code at the call site.
- `require_chosen_environment(config)`, called from the six `secrets.py` mutators and
  from `store/connection._writer`.
- A refusal message that names the defaulted value, the `BANKMACHINE_ENVIRONMENT`
  export, and the exact TOML line to add — with the config path interpolated from
  `default_config_path()` and the home directory elided.

**Done when**

- [x] `load_config` with neither env var nor config file reports source `"default"`;
      with either, it reports that one.
- [x] Each of the six mutators refuses on a defaulted environment; each keychain
      account is proven untouched afterwards.
- [x] Every handle in `store.connection` but `reader` refuses, discovered by walking the
      module rather than by naming them.
- [x] The refusal text contains no literal path and no operator home directory.
- [x] Suite green.

### Chunk 02 — `sync run --until-ready`

**Delivers**

- `_run_once`, the current body of `cmd_sync_run`, extracted unchanged.
- `--until-ready`, `--max-attempts` (default 12), `--retry-delay` (default 300s).
- The loop: 0 returns 0; 75 sleeps and retries; **any other code returns immediately
  and unchanged**, because 1 needs a person rather than another attempt. Reaching the
  cap reports it and still exits 75.

**Done when**

- [x] A run whose codes are 75, 75, 0 exits 0 after three attempts — the input MOVES
      between attempts, per `learnings.md`'s frozen-input rule.
- [x] 75 then 1 exits 1 after two attempts.
- [x] The cap is honoured, reported, and still exits 75.
- [x] Without the flag, behaviour and attempt count are unchanged.
- [x] Suite green.

### Chunk 03 — the documentation the first two chunks make true

**Delivers**

- Runbook § 3.1: the config file as the durable path, exports as the inline
  alternative, and the overwrite footgun replaced by what now actually happens.
- Runbook §§ 3.6 and 5.1: name `--until-ready` instead of instructing a manual loop.
- `operational-spec.md` § Configuration: the config file shown alongside the exports
  it already documents. The `.env` norm is quoted, not amended.
- **#95's mislabel**, corrected in all three places it appears —
  `build-plan-connections-reauth.md:82,126` and `api-notes-plaid.md:678` credit the
  identity fallback to #91, which is the *lineage* bug. Point them at #95.

**Done when**

- [x] No document instructs an operator to re-run `sync run` by hand.
- [x] § 3.1's stated failure mode matches chunk 01's behaviour.
- [x] `grep -n "#91" .prawduct/artifacts/` shows no line attributing the identity
      fallback to it.

## Status

- [x] Chunk 01 — a defaulted environment refuses a per-environment write
- [x] Chunk 02 — `sync run --until-ready`
- [x] Chunk 03 — the documentation the first two chunks make true

**Context:** built 2026-09-11 on `fix/environment-guard-and-until-ready`. All three chunks
landed in one cycle; suite green (`prawduct-hook test-status`).

Two things the build found that the plan did not predict:

- **`ConfigError` raised from a handler reached the unexpected-failure arm** of
  `cli/__init__.py`, which exits 2 *and* prints a traceback -- so the refusal looked like a
  crash. Found by running the real CLI, not by the suite: the exit code was already right,
  which is why a test asserting only the code passed. `ConfigError` now joins the expected
  failures, and a test asserts the absence of the traceback rather than the presence of a 2.
- **The guard reaches subprocesses.** Four tests spawn a child that builds its config from the
  environment and then writes; three of their harnesses never declared one. They now do. This
  is direct evidence for the `sync run` [ASSUMPTION] above: any unattended writer -- launchd,
  cron, a test child -- must declare the environment, and the config file is how.
`Critic mode:` chunk per chunk, then `cumulative`.
