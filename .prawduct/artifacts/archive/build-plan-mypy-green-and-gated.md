---
artifact: build-plan
version: 2
scope: mypy-green-and-gated
branch: fix/mypy-green-and-gated
depends_on:
  - artifact: project-preferences
governed_by:
  - artifact: project-preferences
    dispositions:
      - "Type annotations: required — mypy strict → this plan is that norm's enforcement, not a departure from it. The rule selection and strict setting are untouched; only the annotations the checker was already asking for change"
      - "Dev commands: `uv run pytest -q`, `uv run mypy`, `uv run ruff check` / `ruff format` → conforms and is strengthened: they stay the operator-facing commands and gain one launcher that runs every one of them, so the documented set and the gated set cannot diverge. `ruff format --check` joined the gate mid-build on the strength of a dated learnings instance; the departure is recorded in this plan and in the script's header"
      - "Imports: absolute, grouped stdlib / third-party / local → conforms; the new `AnyParser` import follows the existing grouping and ruff's isort rules decide it"
  - artifact: api-contract
    dispositions:
      - "the CLI's exit code is a contract (0/1/2, plus 75) → conforms, and deliberately so: both `error()` overrides keep their exact runtime behaviour. `NoReturn` documents what argparse already did (raise `SystemExit`) and what the escrow parser already did (raise `KeyEscrowRefusedError`); no code path's exit code moves"
  - artifact: security-model
    dispositions:
      - "argparse must not echo a credential back → conforms and is untouched. `RedactingParser.error` and the key-escrow `error` keep their bodies verbatim; this plan changes the two functions' RETURN ANNOTATION only, and `parser_class=RedactingParser` stays on the subparsers action so every subcommand parser still redacts"
partition: serial — chunk 03's gate can only be made green by chunks 01 and 02, and a gate declared before the tree is clean would land red on purpose
last_validated: 2026-09-12
lifecycle: completed
archived: 2026-09-14
released_in: v0.1.0
maintained: false
---

> **Archived — no longer maintained.** This plan records what was built, not what will be. Do not edit it to reflect later changes; write those where they are true.

# Build Plan: mypy Is Green, And Something Runs It

Closes brookstalley/bankmachine#92.

## Problem

`project-preferences.md` records **"Type annotations: required — mypy strict"** and names
`uv run mypy` and `uv run ruff check` as dev commands. Nothing executes either one. The
Stop gate and `test-evidence` cover `pytest` alone, so that norm's declared enforcement
mechanism is not run by anything, and the drift was invisible until someone ran it by hand.

Measured on this branch's base, 2026-09-12:

- `uv run mypy` — **12 errors in 5 files** (107 source files checked).
- `uv run ruff check` — **green.**

## A correction to the issue's evidence, and why it matters

**#92 records 3 ruff `I001` findings. They are gone** — fixed incidentally by later work,
not by anyone closing this issue. The ruff half of the issue's evidence is stale.

**The mypy half is exact**: 12 errors in 5 files, unchanged since the issue was filed.

Recorded because the natural shortcut — running `mypy src` — reports **8 errors in 3
files** and reads like progress. It is not progress; it is a narrower scope.
`[tool.mypy] files = ["src", "tests"]`, so the canonical command covers both trees and
the four errors in `tests/` are as real as the eight in `src/`. **The gate in chunk 03
runs the canonical command with no path argument**, which is what keeps this from
recurring.

## Four root causes, not twelve errors

1. **`RedactingParser.error` returns `None`, supertype says `Never`** — `cli/parser.py:46`.
   It ends in `super().error(...)`, which raises `SystemExit`. (1 error)
2. **The key-escrow `error` has the same shape** — `cli/store.py:178`. Its body is a bare
   `raise KeyEscrowRefusedError(...) from None`. (1 error)
3. **`_SubParsersAction` is invariant** — `cli/__init__.py:59-64`. `add_subparsers(...,
   parser_class=RedactingParser)` yields `_SubParsersAction[RedactingParser]`; all seven
   `add_arguments` functions declare `_SubParsersAction[ArgumentParser]`. Invariance makes
   the two unrelated, so every call site is an error. (6 errors)
4. **Two tests reach into argparse internals untyped** — `tests/cli/test_sync_shell.py:210`
   (3 errors) and `tests/cli/test_store_key_commands.py:272` (1 error).

## The decision in cause 3, recorded because two fixes look equivalent and are not

The six errors can be silenced at one call site with a `cast` to the wider type, touching
one file instead of seven. **Rejected.** The cast asserts that the action's `choices` maps
to plain `ArgumentParser`s, which is the one thing that is false — they are
`RedactingParser`s, and that class exists because a subcommand parser printing raw input
is a credential leak (`security-model.md`). A cast that lies about exactly the property
the security control rests on is a worse artifact than six honest signatures.

**Taken:** the seven `add_arguments` functions are generic in the parser class. They only
ever call `add_parser`, so they are genuinely agnostic to which class comes back, and a
`TypeVar` bound to `ArgumentParser` says so. This also keeps
`tests/cli/test_sync_shell.py` — which builds a plain `ArgumentParser` and passes its
subparsers action to `sync.add_arguments` — type-correct without a second exception.

`AnyParser` lives in `cli/parser.py` beside `RedactingParser`. That module already exists
to be importable without importing the package that imports the commands, and
`mcp.py` already imports `bankmachine.cli.exit_codes`, so the sibling import is proven
cycle-safe rather than assumed so.

## The decision in chunk 03, recorded because the obvious vehicle does not work

`test_commands:` is the multi-command form and looks like the answer. **It cannot carry
this**: its contract makes the `{junit_xml}` literal *mandatory in every entry*, and
neither mypy nor ruff emits a JUnit report. `test_command:` refuses shell operators for
the same reason. Its documentation names the escape hatch outright — *"point the command
at a script for compound runs"* — so that is what this uses.

Ordering inside the script is load-bearing: **pytest runs first**, so the JUnit report
exists and is parseable whatever the linters say. A lint failure then fails the record
with a named command rather than an unparseable-report error that says nothing about
which tool was unhappy.

## Chunks

### Chunk 01 — `src` typechecks

Causes 1, 2, 3. `NoReturn` on both `error()` overrides; `AnyParser` added to
`cli/parser.py`; seven `add_arguments` signatures made generic.

**Done when:** `uv run mypy src` is clean, and the full suite is green — annotation-only
changes must move no behaviour, and the CLI tests are what prove it.

### Chunk 02 — `tests` typechecks

Cause 4. Both sites keep what they are testing and stop returning `Any`.

**Done when:** `uv run mypy` (no path argument, so both trees) is clean.

### Chunk 03 — the gate runs every declared check

`scripts/check.sh` running pytest, then ruff, then mypy; `test_command:` repointed at
it; `project-preferences.md` updated to say they are gated. (`ruff format --check`
joined the set mid-build — see § What the Critic found — so the gate runs four
invocations, not three.)

**Grew a test the plan did not anticipate.** The gate is a contract -- "a red check is
named" is the whole reason it beats running the commands by hand -- and a one-off
demonstration proves that once, for the person watching. `tests/preferences/
test_the_gate_runs_the_declared_checks.py` drives the real script with a stub `uv` on
`PATH`, in the same shape as the existing leak-guard tests, and asserts the order, the
naming of each red tool, that a failure does not stop the remaining checks, and that the
JUnit report survives a red linter. It runs in ~2s rather than the ~6.5min a real
invocation costs, which is what makes it affordable to keep.

**Done when:** the gate passes on a green tree through `test-evidence record`; each
declared tool, failed in turn, fails the gate, is named in stderr, and is counted in the
JUnit report the evidence record is built from.

## Scope-out

- **No `.github/workflows/`.** The repo has no CI at all, which is worth knowing and is
  not this issue: #92's acceptance names "the gate/test-evidence path that runs `pytest`",
  and that path is `test_command:`. Adding a second, unasked-for enforcement surface here
  would make the gate's own contract harder to change later.
- **The mypy-strict and ruff rule selection**, per the issue's own scope-out. The config
  stands as `project-preferences.md` states it.
- **The 3 ruff `I001` findings** in the issue's evidence — already green; nothing to do.

## Status

- [x] Chunk 01 — `src` typechecks
- [x] Chunk 02 — `tests` typechecks
- [x] Chunk 03 — the gate runs every declared check

## Context

Branch `fix/mypy-green-and-gated` off `develop`. All three chunks done.

`uv run mypy` went 12 errors / 5 files to clean over 108 files; ruff and `ruff format`
were and remain clean; the suite is green through the new gate end to end (`prawduct-hook
test-status` for the count -- it is not copied here, because a total in prose drifts the
moment a test is added and nothing reads this one).

**One thing the plan did not foresee.** Making `add_arguments` generic surfaced that
`store.add_arguments` is not in fact agnostic to the parser class -- it swaps
`__class__` on the `key` parser to `_KeyParser`, which the TypeVar correctly refused.
The `key` local is annotated to `argparse.ArgumentParser` with the reason inline: the
swap needs a class this module can name, and nothing there relies on anything narrower.

**Not proven by a real red run, and why.** The red path is proven by the test above
driving the real script, plus a real green run through the true toolchain. A third run
with a hand-planted type error would only re-establish that `uv run mypy` exits nonzero
when mypy is unhappy and that `uv run` propagates it -- 6.5 minutes to confirm a
property of the tools rather than of this change.

## What the Critic found, and what it changed

Review `rev-20260912T143955Z-afdc2426`, 0 blocking. Three reviewers independently
reported the same defect from three different goals, and they were right:

**The gate reported a red linter without recording one.** `test-evidence record` writes
`.test-evidence.json` from the JUnit report (`prawduct-hook:3954`) and only afterwards
consults the command's exit status, which it does not store (`:4034`). pytest runs first
and green, so a red `mypy` or `ruff` left a session-fresh record reading `failed: 0` --
terminal red, evidence green, Stop gate satisfied. This plan's verification covered only
the green path, which is exactly the half the defect lives in. **This was #92's own shape
-- a declared check nothing notices -- moved one consumer along rather than closed.**

Fixed by writing each red check into the JUnit report as a failing case, so the durable
evidence says what the terminal says. Two tests now hold that, both proven red with the
injection disabled.

**And a scope decision reversed.** `ruff format --check` was scoped out on the grounds
that #92 names two commands. `learnings.md` disproves that reasoning with an instance: on
2026-09-08 seven files had drifted across two build steps that both reported "ruff clean",
because both ran `ruff check` and neither ran `--check` on the formatter. A gate built
because declared checks went unrun, omitting the one declared check with a recorded
instance of going unrun, is the same defect with a smaller blast radius. It now runs.

Accepted without change: the two `governed-by-gap` record-lint findings (this plan
disposes the norms its diff touches; enumerating the rest would be ceremony), and the
note that #92's `affected:` list names two `tests/store/` paths this diff does not touch
-- confirmed stale, they belong to the ruff `I001` findings that were already green.
