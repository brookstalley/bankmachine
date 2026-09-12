---
artifact: build-plan
version: 1
scope: ci-runs-the-gate
branch: fix/mypy-green-and-gated
depends_on:
  - artifact: project-preferences
  - artifact: build-plan
    scope: mypy-green-and-gated
governed_by:
  - artifact: project-preferences
    dispositions:
      - "The gate runs every declared check, not just pytest — `scripts/check.sh` is what `test_command:` launches → conforms and is extended by one consumer. CI launches the SAME script rather than restating the four commands, so the gated set cannot fork between the developer's machine and the server. A norm test holds that shape"
      - "A red check is recorded, not just printed — anything added to the gate later must reach the JUnit report the same way or it is decorative → conforms, and the norm's *reason* is what decides how. That rule exists because `test-evidence record` builds `.test-evidence.json` from the JUnit report and ignores exit status, so the terminal and the record could disagree. In CI there is no such reader: the job's conclusion IS the record, and `scripts/check.sh` drives it by exiting non-zero. So CI adds no check, adds a caller, and uploads nothing — the script still needs a JUnit path, so it is handed one under `RUNNER_TEMP` and discarded. An `upload-artifact` step was considered and dropped: pytest's own failures and the script's `these commands were red:` summary are already in the run log, so it would have bought a third pinned third-party action for no information"
      - "macOS is supported and tested; other platforms are unverified, not excluded → conforms. The runner is `macos-latest`. An `ubuntu-latest` job was rejected: a green tick on an unverified platform is a claim this project has never earned, and the keychain integration tests would have to be given a Linux backend to produce it"
      - "Version 3.14, `.python-version` pins it, `requires-python = \">=3.14\"` → conforms and is deliberately NOT widened. CI installs the pinned interpreter and exercises exactly one, so it is not the matrix that `pyproject.toml` names as the precondition for lowering the floor"
      - "Testing — `sandbox` marker deselected by default → conforms. CI inherits `addopts = \"-m 'not sandbox'\"`, makes no live aggregator calls and needs no credentials, so no secret is configured on the repository"
      - "Test | `tests/preferences/test_*.py` | structural rules with named exceptions → conforms; the workflow gets a norm test for the reason the table gives"
  - artifact: operational-spec
    dispositions:
      - "Platform: macOS (Apple Silicon), native, not containerized; Deploy target: this machine, no staging, no remote → **a hosted runner is a new place this code executes, and the spec's rows do not cover it.** Not a departure, because those rows describe where the product is DEPLOYED and CI deploys nothing — it checks out, tests and discards. Matched as far as it can be (macOS, native, no container) and the runner is deliberately given no credential, no secret and a read-only token, so nothing about the deploy target becomes reachable from it. Recorded rather than waved through: if CI is ever given a secret, that is the moment this stops being true and operational-spec needs a row of its own"
last_validated: 2026-09-12
---

# Build Plan: CI Runs The Gate

## Problem

`scripts/check.sh` runs every declared check, and two things launch it: the governance
gate on the machine of whoever is editing, and `.githooks/pre-push`. Both are **per-clone
opt-in** — `core.hooksPath` is not committed, so a fresh clone pushes with no guard at
all — and both are bypassable by `git push --no-verify`, which the hook documents as its
own escape hatch.

So the checks are run by the honour system of one machine. Nothing on the server side
observes a branch. That is the same shape as brookstalley/bankmachine#92 — *a declared
check that nothing independent runs* — one hop out: #92 closed the gap between "declared"
and "run locally", and this closes the gap between "run locally" and "run where the code
actually arrives".

## The scope-out this reverses, and on whose authority

`build-plan-mypy-green-and-gated.md` § Scope-out says, in full:

> **No `.github/workflows/`.** The repo has no CI at all, which is worth knowing and is
> not this issue.

That reasoning was correct for that issue and is not being disowned: it argued the work
did not belong *to #92*, not that it should never happen. **The owner asked for it
directly on 2026-09-12.** Recorded here rather than by editing the older plan's
scope-out, because that plan is a record of what that work cycle decided and rewriting it
would erase a decision that was properly taken.

## Why this rides `fix/mypy-green-and-gated` rather than a branch off `develop`

`scripts/check.sh` **does not exist on `develop`** — it is four commits deep on this
branch and unmerged. A workflow branched off `develop` would invoke a path that is not
there and be red on its first run, for a reason having nothing to do with the code it
was meant to check.

## The runner decision, and what it costs

The repository is **private**, so Actions minutes bill and macOS runners carry a **10×
multiplier**. pytest alone is ~6.6 minutes; with checkout, `uv sync`, ruff and mypy a run
is ~10 minutes wall, so **~100 billable minutes per run** against the 2,000–3,000 minutes
a month a private plan includes. That is 20–30 runs before overage — few enough that the
trigger is a cost decision, not a detail.

**Taken (owner's choice, 2026-09-12): `macos-latest`, on `pull_request` into `develop`
and `main`, plus `workflow_dispatch`.** Not on every push. The local gate already covers
the edit loop; what CI adds that the loop cannot is an observer at the point the code
tries to become someone else's problem, and that point is the pull request.
`concurrency: cancel-in-progress` keeps a re-push from paying twice for a superseded run.

Two consequences worth stating rather than discovering:

- **A merge commit landing on `develop` re-runs nothing.** The PR run vouches for the
  merge result only while the base has not moved under it.
- **`workflow_dispatch` will not appear until this reaches `develop`.** GitHub reads the
  dispatchable-workflow list from the default branch only. The button is real but dark
  until the first merge.

## The keychain, which is the part most likely to be red

`tests/conftest.py` states it outright: *"Integration tests run against real SQLCipher and
the real OS keychain, under a test-scoped service name."* Those are not mocks and the plan
is not to make them mocks — the conftest explains why, and that reasoning holds on a
runner as well as a laptop.

A GitHub macOS runner is a non-interactive session, so the workflow creates, unlocks and
defaults a scratch keychain before pytest rather than assuming the login keychain is
reachable. `sqlcipher3-wheels` is a wheel, so SQLCipher needs no system install.

**This is the acceptance risk.** It cannot be settled from here — see § Verification.

## Chunks

### Chunk 01 — ruff moves to 0.16.7

`uv lock --upgrade-package ruff`. The floor in `pyproject.toml` stays `>=0.16`: the
comment there justifies the floor by what it must *reproduce*, and 0.16.7 reformats
nothing in this tree, so raising it would tighten a constraint no evidence asks for. The
lockfile is what pins the exact version, and CI resolves through `--locked`.

**Done when:** `uv run ruff check` and `uv run ruff format --check` are both clean on
0.16.7 with no file reformatted.

### Chunk 02 — a pull request runs the gate, and something holds it to that

`.github/workflows/check.yml`, whose only check-running command is `bash
scripts/check.sh`. Restating the four commands in YAML would recreate the exact drift
#92 was about, so the workflow is a *caller* and a norm test asserts it stays one.

`uv sync --locked` rather than `uv sync`: it fails when `uv.lock` and `pyproject.toml`
have drifted apart, which makes the lockfile the single answer to "which ruff ran" instead
of a suggestion.

Artifacts updated in the same chunk, because each currently describes a repo with no CI:

- `docs/README.md` § Working on the code and `README.md` — a clone's checks are no longer
  only as good as its `core.hooksPath`.
- `project-preferences.md` — the gate's entry names its second launcher.
- **A defect found while reading, fixed here rather than left.**
  `tests/preferences/test_python_floor_is_exercised.py` says the honest check becomes
  "the matrix covers the floor" *"when a CI matrix lands (build step 8)"*.
  `system-requirements.md` §8 is **Scheduling and logging**; no step in that sequence is
  a CI matrix. The pointer is wrong today, and it gets worse the moment a `.github/`
  directory exists to make it look satisfied. The condition it names is correct and
  stays; the false step reference goes, and the docstring says plainly that the CI which
  now exists runs one interpreter and therefore changes nothing about the floor.

**Done when:** the workflow's only check-running step is `scripts/check.sh`; the norm
test fails when that step is replaced by a direct `uv run pytest`; `uv run mypy` and the
full local gate stay green; and no artifact still says the repository has no CI.

## Scope-out

- **No Python or OS matrix.** One interpreter, one platform — the two the project
  actually claims. A matrix is the precondition `pyproject.toml` names for widening
  `requires-python`, and inventing one here would let the floor be widened on the
  strength of a job nobody asked for.
- **No `push:` trigger.** The owner chose PR-and-manual; the gap it leaves (a merge
  commit on `develop` re-runs nothing) is recorded above rather than quietly closed.
- **Not a required status check.** Branch protection is a paid feature on private repos,
  which is why `.githooks/pre-push` exists in the first place. CI reports; it cannot yet
  block. The hook's header comment stays true and is left alone.
- **No `sandbox`-marked run.** It would need real aggregator credentials as repository
  secrets. Deliberately not done: this workflow needs no secret at all, and that is worth
  more than the coverage.
- **No dependency-update automation** (Dependabot / Renovate). Not asked for.

## Status

- [x] Chunk 01 — ruff moves to 0.16.7
- [x] Chunk 02 — a pull request runs the gate, and something holds it to that

## Verification

**What can be proven here:** the script the workflow calls, on this machine, through the
real toolchain; the norm test, proven red with the call replaced.

**What cannot:** that the workflow is green on a GitHub runner. `pull_request` is the only
trigger that fires from a branch whose workflow is not yet on `develop`, so **the first
real run requires a PR** — and the keychain step is the part most likely to fail there.
Until that run exists this plan's acceptance is *unproven on the runner*, and saying
otherwise would be the kind of claim `test-evidence` was built to stop.

## Context

Branch `fix/mypy-green-and-gated`, stacked on the three complete chunks of
`build-plan-mypy-green-and-gated.md`. Both plans stay live until the branch merges.

## What the local run proved

Suite green through the real gate on ruff 0.16.7 (`prawduct-hook test-status` for the
count; it is not copied here). `ruff format --check` reformatted nothing on the bump, which
is why the `>=0.16` floor was left alone.

The three norm tests were each driven red before being accepted, since a norm test that has
never failed is a claim rather than a check:

| Mutation | What went red |
|---|---|
| workflow file removed entirely | `test_there_is_a_workflow_at_all`, `test_some_workflow_runs_the_gate` |
| run step points at a different script | `test_some_workflow_runs_the_gate` |
| `uv run pytest -q` added beside the gate call | `test_no_workflow_restates_the_checks_the_gate_owns` |

The workflow was restored byte-identical after each and re-verified green.

**Still unproven, and it is the acceptance risk named above:** no run has happened on a
GitHub macOS runner. The keychain step is written from what `keyring` requires (it writes
to the default keychain and reads through the search list, so a keychain that is only the
default takes writes and fails every read back) rather than from an observed run.
