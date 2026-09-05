---
artifact: build-plan
version: 2
scope: repo-sanitization
branch: feature/repo-sanitization
depends_on:
  - artifact: system-requirements
governed_by:
  - artifact: project-preferences
    dispositions:
      - "provider-agnostic engine: no roster identity in code or schema → conforms, and this plan widens the norm's reach from the source root to the whole tracked tree"
      - "requirement ids unique within a requirements document → inapplicable because this plan adds no numbered requirements"
partition: serial — Chunk 02 rewrites the history Chunk 01 writes into, so they cannot overlap
last_validated: 2026-09-05
---

## Requirements Confidence

**Level:** High

**Why:** The problem is one sentence — three doc paths carrying the operator's institution
names and balances are on the remote, and the repo is now intended for possible public
release. The exposure was measured directly (`git ls-tree origin/develop`, grep over every
tracked file at that ref), not estimated. Scope is the four paths named in Chunk 02 and
nothing else. All four forking decisions were confirmed with the operator this session.

**Open assumptions / unknowns:**

- [ASSUMPTION: this clone is the only one — no fork, no CI checkout, no collaborator has
  pulled `origin/develop` | HIGH impact | user can correct] — a history rewrite only
  removes the data from copies that later re-fetch. If another clone exists, its objects
  still carry the roster and it must be deleted rather than pulled.
- [ASSUMPTION: GitHub's server-side object retention is acceptable | MED impact | user can
  override] — force-pushing makes the old commits unreachable, but GitHub can still serve
  a dangling commit by SHA until it garbage-collects, and a private repo's SHAs are not
  discoverable. The belt-and-braces answer is to delete and recreate the remote repo;
  recorded here so choosing not to is a decision rather than an oversight.

**What would raise confidence:** N/A at High. The two assumptions above are the operator's
to confirm, and neither blocks Chunk 01.

## Status

- [ ] Chunk 01: Rebuild the public surface — roster out, layering contract kept
- [ ] Chunk 02: Purge the roster from history and force-push

Context: Chunk 01 built and reviewed (rev-20260905T203743Z-4323aa3a: 3 blocking, 10 warning, 6 note — all dispositioned in one pass). Supersedes deployment-requirements.md
open question 6.4, which asked where the roster should live: the operator's answer this
session — MCPlaid is a general-purpose tool that may be published, with nothing specific to
them in it — resolves it to option (a), plus the history rewrite that option (a) left open.
Next: Chunk 02, the history purge. 🔴 Until it lands, `origin` still carries the roster and the
leak guard blocks every push — which is the guard working, not a problem to route around.

## Scaffolding

### Project Initialization

None. No source code exists yet and none is added here — this plan touches documentation,
`.gitignore`, and one shell guard. The Python scaffold belongs to build step 1 of
`docs/system-requirements.md` §8 and is deliberately not started, because it fixes the
package name and the project has not yet chosen one.

### Dependencies

`git-filter-repo` (verified present at `~/.pyenv/shims/git-filter-repo`). Used once, in
Chunk 02, and not a runtime dependency of the product.

### Build & Test Configuration

No Python test runner exists yet, and creating one would fix the package name ahead of the
rename decision. So the guard and its self-test are shell, run directly:

```sh
./scripts/check-no-personal-data.selftest.sh   # 15 cases; must report 0 failed
```

The migration obligation is recorded in two durable places, not just here: the
`project-preferences.md` norm row, and `docs/system-requirements.md` §8 build step 1 — which is
the step that lands the test runner and therefore the moment the move is triggered.

### Scaffold Verification

`bash scripts/check-no-personal-data.selftest.sh` runs 15 cases in a throwaway repository under
`$TMPDIR` and must report `0 failed`. It covers both directions — clean passes, tokens caught —
plus the two that matter most and are not obvious: **a leak in history behind a sanitized tip**
(which a worktree-only guard reports clean, and which is the exposure this project actually had),
and **failing closed** on a malformed token file. A guard only ever tested green is a guard nobody
knows is wired up; this one failed 7 of 15 on its first run and caught a real defect in the guard.

### Verification Strategy

Chunk 01 is verified by the self-test above, and — separately, because grep cannot see it — by
reading the sanitized `build-vs-adopt-investigation.md` end to end for meaning that survived the
redaction. A decision record with its evidence stripped out is worse than one never written.

Chunk 02 is verified against the remote, not the working tree: after the force-push,
`git ls-tree -r origin/develop`, `git ls-tree -r origin/main`, and a fresh clone into the
scratchpad must all show none of the four purged paths in any commit.

## Project Structure

```
README.md                                   # NEW: what this is, and the hooks enable step
docs/
├── system-requirements.md                  # the engine — public, provider-agnostic
├── deployment-requirements.template.md     # NEW: the roster's shape, carrying no data
└── build-vs-adopt-investigation.md         # sanitized in place
scripts/
├── check-no-personal-data.sh               # NEW: the norm's mechanism
└── check-no-personal-data.selftest.sh      # NEW: 15 cases proving it
deployment/                                 # NEW, gitignored — never committed
├── deployment-requirements.md              # the roster, moved out of git
├── data-sources.md                         # inventory evidence, moved out of git
├── roster-tokens.txt                       # institution match tokens
└── identity-tokens.txt                     # operator + machine match tokens
```

### Module Boundaries

The line this plan draws is the one the product already ratified: **the engine is public,
the roster is local.** `docs/` may name the aggregator (a single named dependency, per
`system-requirements.md` §0.1) and may never name a financial institution, an account, or
the operator. `deployment/` is where institution identity is allowed to exist, and it is
gitignored. The template in `docs/` is the seam — it carries the contract and the
traceability table, so a public reader can see how the layering works without seeing whose
accounts it was derived from.

## Build Chunks

### Chunk 01: Rebuild the public surface — roster out, layering contract kept

- **Description:** Move the operator's roster off git without losing the layering idea that
  makes the engine spec trustworthy. `deployment-requirements.md` is the most valuable
  document in the project *as a structure* and the most disqualifying *as content* — so its
  structure is kept as a committed template and its content moves to a gitignored path.
- **Type:** code
- **Critic mode:** final
- **Depends on:** none
- **Artifacts consumed:** `docs/system-requirements.md` §0.1 (the layering rule),
  `.prawduct/artifacts/project-preferences.md` (the provider-agnostic norm row)
- **Deliverables:**
  - new `deployment/deployment-requirements.md` and new `deployment/data-sources.md` —
    the verbatim current roster and inventory evidence, moved into the gitignored
    `deployment/` directory
  - new `deployment/roster-tokens.txt` and new `deployment/identity-tokens.txt` — the
    guard's only token sources, both gitignored
  - new `docs/deployment-requirements.template.md` — §0.1 contract, the §7 traceability
    table shape, and the roster/rule/coverage headings, with every institution, balance,
    date and account count replaced by a worked placeholder
  - `docs/build-vs-adopt-investigation.md` sanitized in place: no operator name, no machine
    name, no institution name, no account counts
  - new `scripts/check-no-personal-data.sh`
  - new `scripts/check-no-personal-data.selftest.sh` — 15 cases in a throwaway repo, so
    "exercised in both directions" is reproducible rather than asserted
  - new `README.md` — carries the `core.hooksPath` enable step, which cannot live only
    inside the hook file it enables
  - `.githooks/pre-push` calls the guard on every push, not just pushes to `main`
  - `.gitignore` += `deployment/`
  - `.prawduct/project-state.yaml` `artifact_manifest` updated: the two moved docs are no
    longer repo artifacts; the template is
  - `docs/system-requirements.md` companion references repointed at the template
- **Tests:** `scripts/check-no-personal-data.selftest.sh` — 15 cases, all passing, run and
  recorded as test evidence
- **Acceptance criteria:**
  1. No tracked file contains an institution name, the operator's name, the machine name, or a
     balance figure — **except** an identity-owned GitHub repository slug (`<owner>/<repo>`),
     which is exempt by design: the owner segment is in the clone URL and so is inherently
     public the moment the repository is. Two tracked files carry one
     (`.claude/settings.json`, `.prawduct/project-state.yaml`). Stating the exemption here
     matters because the operator re-verifying before publication otherwise finds hits and
     cannot tell a designed exemption from a sanitization miss.
  2. The guard fails when a token is reintroduced
  3. `docs/deployment-requirements.template.md` still states the §0.1 contract and still
     carries a traceability table a reader could fill in
  4. Nothing in `docs/` cites a path that no longer exists
- **Done when:**
  1. Acceptance criteria met and the guard exercised in both directions
  2. `/prawduct:critic` run and blocking findings resolved
  3. Committed and chunk marked `[x]` in Status

### Chunk 02: Purge the roster from history and force-push

- **Description:** The one irreversible act in this plan, isolated so it can be confirmed on
  its own. Four paths leave every commit; the rewritten `develop` and `main` replace the
  remote's.
- **Type:** infrastructure
- **Critic mode:** final
- **Depends on:** Chunk 01 (its commit is itself rewritten by the purge, so the order is
  forced — reversing it would reintroduce the paths)
- **Artifacts consumed:** this plan's Chunk 01 deliverables
- **Deliverables:**
  - a full mirror backup of the pre-rewrite repo in the session scratchpad, taken before
    anything is rewritten and kept until the operator confirms the result
  - history rewritten with `git filter-repo --invert-paths` over exactly four paths:
    `docs/data-sources.md`, `docs/deployment-requirements.md`,
    `docs/plaid-pipeline-acceptance-criteria.md`, `docs/build-vs-adopt-investigation.md`
  - `origin` re-added (filter-repo removes it by design)
  - `develop` and `main` force-pushed
- **Tests:** none automated — the verification is the fresh-clone inspection below
- **Acceptance criteria:**
  1. `git log --all --pretty=format: --name-only | sort -u` names none of the four paths
  2. A fresh clone of `origin` into the scratchpad shows the same
  3. `docs/build-vs-adopt-investigation.md` is present in the working tree at its sanitized
     content, re-added after the purge, and its history starts clean
  4. The pre-push guard's block on a non-fast-forward `main` is bypassed **once**, with
     `--no-verify`, deliberately — the hook documents this as its escape hatch, and a
     history rewrite is exactly the case it was not written to stop
- **Done when:**
  1. Acceptance criteria met and the operator has confirmed the fresh-clone inspection
  2. `/prawduct:critic` run and blocking findings resolved
  3. Committed and chunk marked `[x]` in Status
  4. Scratchpad backup retained until the operator says otherwise
