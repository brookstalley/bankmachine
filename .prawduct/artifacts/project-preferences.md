# Project Preferences

Developer preferences for how code is written in this project. Captured during discovery, updated as preferences evolve. Every session should read this before writing code.

## Language & Runtime

- **Language**: Python
- **Version**: 3.11+ (verified on 3.14.6, Apple Silicon). Native macOS, not containerized.
  **macOS is supported and tested; other platforms are unverified, not excluded** — the credential
  store and the scheduler sit behind seams so a port is a new implementation, not a refactor.
- **Package manager**: uv (dependency resolution, lockfile, venv). `uv run` for all dev commands.
  `.python-version` pins **3.14**, the version this artifact records as verified — the owner took
  that decision on 2026-09-06, and the whole suite, mypy strict and ruff were re-run on it before
  the pin moved. `requires-python` stays `>=3.11` — the floor is a claim about the code, the pin is
  a claim about what was run, and nothing here uses a 3.12+ language feature. The AC-ARCH.7
  concurrency probes recorded in `architecture.md` were measured on 3.12.3 and are left saying so;
  they are measurements, not settings, and the norm suite that encodes them passes on 3.14.

## Code Style

- **Naming**: snake_case functions and modules, PascalCase classes, SCREAMING_SNAKE constants
- **Formatting**: ruff format
- **Linting**: ruff
- **Type annotations**: required — mypy strict. Money is integer minor units and dates are of two
  distinct kinds (calendar date vs UTC instant); the type checker is what stops those being mixed.
- **Imports**: absolute, grouped stdlib / third-party / local (ruff isort rules)

## Testing

- **Framework**: pytest
- **Style**: descriptive test names stating the behaviour, AAA
- **Coverage expectations**: happy path plus error cases everywhere; **comprehensive edge cases** on
  the sync/cursor path, money arithmetic, dedup, and the rebuild — a defect there is silent wrong
  analysis, not a crash
- **Testing strategies**: property-based (hypothesis) for money arithmetic, idempotency, and
  rebuild losslessness — these are invariants, and invariants are what property tests are for.
  Integration tests against real SQLCipher, real Keychain (test-scoped service name), and the
  aggregator's sandbox. Import adapters tested against real exported sample files.
- **Test location**: `tests/` mirroring the source tree; `tests/preferences/` for norm tests
- **Parallelization**: (unset — revisit if the suite gets slow)

## Architecture Patterns

- **Data modeling**: **SQLAlchemy Core** — typed table metadata and the query builder, no ORM: no
  `declarative_base`, no `Session`, no identity map. Engines are constructed with
  `create_engine(..., creator=...)` where the creator is our own keyed connection from
  `store/connection.py`, so every SQLCipher-specific step (key first, WAL, `mode=ro`, `query_only`,
  the writer lock) stays inside the module that owns the architecture norms and SQLAlchemy never
  opens a connection itself. Decided 2026-09-05 by the owner, over a builder recommendation of
  hand-written SQL; both routes were verified to drive SQLCipher before the decision was taken.
- **Error handling**: exceptions, specific not broad. Per-connection errors are caught and recorded,
  never allowed to abort other connections. Silence is the one disallowed outcome.
- **Async**: sync unless needed. The workload is a daily batch and a stdio MCP server; neither is
  concurrency-bound.
- **File organization**: layer folders (store / connector / sync / rules / mcp / cli)

## Tooling

- **Key libraries**: `sqlcipher3-wheels` — this is the package that works on Apple Silicon.
  `sqlcipher3-binary` is unavailable for this platform; `sqlcipher3` and `pysqlcipher3` need a
  Homebrew build step. **Credential storage goes through `keyring`**, not direct `security` CLI
  calls: it wraps macOS Keychain, Windows Credential Manager and SecretService behind one
  interface, so the one part of the system that is genuinely painful to port later costs nothing
  to abstract now. The `security` CLI round-trip was verified during discovery and remains the
  fallback if `keyring` proves unsuitable — but it is no longer the specified mechanism.
- **Dev commands**: `uv sync` once, then `uv run pytest -q` (whole suite, including the shell
  leak guard), `uv run mypy` (strict, source and tests), `uv run ruff check` / `uv run ruff
  format`. `uv run bankmachine store init|status` drives the product itself.
  `uv run python tests/preferences/verify_norms_go_red.py` re-proves that the architecture
  norm tests still fail when their norms are broken — run it whenever the connection layer
  changes, because a norm test that has never been red is a claim, not a check.

## Workflow

- **Branching**: feature-branches (default: feature-branches — create a branch for medium+ work, direct commits to protected branches only for trivial fixes; set to "direct" for solo projects where committing to main is OK)
- **Protected branches**: main, develop (branches that should not receive direct commits unless branching is "direct")
- **PR creation**: wait_for_user (default: wait_for_user — only create PRs when explicitly asked; set to "automatic" to create PRs after Critic review passes)
- **PR merge**: wait_for_user (default: wait_for_user — present the PR for user review before merging; set to "automatic" to merge after CI passes and review is clean)
- **PR merge strategy**: merge commit (default: merge commit — `gh pr merge --merge`; preserves each commit's identity so a reused branch's merge-base stays correct and the review/PR gates don't re-review already-merged work; set to "squash" for one linear commit per PR, or "rebase" — with either, branches are single-use: delete after merge and never reuse, because the rewritten history strands a reused branch's merge-base)
- **Attribution**: none, everywhere — 🔴 **not commits alone.** No `Co-Authored-By`,
  `Signed-off-by`, "Generated with …", bot trailer, badge or sign-off naming an AI, a model or a
  tool appears in commits, PR titles or bodies, issue titles or bodies, review comments, inline
  code comments, docstrings, documentation, changelogs, release notes or generated files. The
  full statement, which overrides any harness default to the contrary, is the **NO ATTRIBUTION**
  section of `CLAUDE.md`; this row is the norm-index entry pointing at it. *(The line below is the
  narrower framework setting that this rule supersedes in scope.)*
- **Commit attribution**: none (default: none — no `Co-Authored-By`, `Signed-off-by`, or "Generated with …" trailers on commits or PR bodies; set to "co-authored" to add a Claude `Co-Authored-By` trailer)
- **Delegation**: (unset — `/prawduct:methodology delegation` states the default and anything written here overrides it. Say in prose how much this project wants fanned out to subagents and what it is worth fanning out for. `off` is a complete answer: it means no delegation at all, it is honoured without ceremony, and nothing nags a repo that has said it.)
- **Delegate verification**: (unset — what a delegate here may run to prove its own change, and what it must leave to the coordinator's integration run. In this project's own words: prawduct does not know this project's test regime and will not invent a vocabulary for it. `/prawduct:doctor` will propose a starting point from what this repo already encodes about running part of its suite.)
- **Change-log merge strategy**: `.prawduct/change-log.md` is `merge=union` in `.gitattributes`.
  Every branch prepends a new entry to the same first lines, so a plain three-way merge conflicts
  there every single time. Union merge takes both sides instead. 🔴 **It never conflicts, which is
  the cost as well as the benefit:** a branch that *edits an existing entry* silently gets both
  versions concatenated rather than a conflict to resolve. The strategy assumes the file is
  append-only. If you find a duplicated entry, this is why.
- **Delegation approval**: ask-on-reason (default: ask-on-reason — a plan that will delegate discloses it and proceeds, asking for approval only on one of the enumerated reasons in `methodology/planning.md` "Partition: Serial or Delegated"; set to "pre-approved" once you have seen it work here, and the ask stops returning with every plan)

---

**What belongs here**: How you want code written. Conventions, tools, style preferences, workflow preferences.

**What doesn't belong here**: What to build (product-brief), system design (data-model, architecture), performance targets (nonfunctional-requirements), or deployment (operational-spec).

## Enforcement

Each preference above should be enforced by one of three mechanisms — assign the mechanism when you add the preference so it doesn't quietly become aspirational.

| Mechanism | Where it lives | What it catches | Trade-off |
|---|---|---|---|
| **Linter** | Project's configured linter (ruff, eslint, swiftlint, etc.) | Mechanical style/naming rules | Best tool when configured. If no linter, preferences in this category fall through to Critic. |
| **Test** | `tests/preferences/test_*.py` (or equivalent) | Structural rules with named exceptions (AST checks, config-presence checks) | Bakes the rule into CI; refuses to be silent. Cost: re-validate when the rule's shape changes. |
| **Critic** | `/critic` review (Goal 4: Norms) | Judgment-required rules (semantic naming, "appropriate" anything, what counts as a "boundary") | No false-confidence test. Cost: requires reviewer per chunk; misses violations between reviews. |

This per-preference table is the product's **norm index** (`/prawduct:methodology norms`): each row assigns a norm its **mechanism** (linter / test / Critic) and its **audit home** — `janitor` (only the deep sweep sees it) or `advisory` (a mechanical probe fires on it). A row may be a **pointer** to a `## Direction` section instead of restating the norm, and every norm carries its **why** (a whyless norm is unenforceable at its edges).

**Every populated row here *is* a homed norm** — the `norm-health-sweep-overdue` advisory reads
these rows to decide whether this product has norms worth auditing, so never leave an example or
placeholder row in the table: it would claim a norm registry that has not been ratified. Two row
shapes go in — an ordinary row naming the convention, its mechanism, its enforcement artifact, its
audit home (`janitor` or `advisory`) and its why; and a **pointer** row whose first cell reads
`norm lives in <artifact> § Direction`, whose enforcement artifact is `—`, and whose why lives in
the Direction entry it points at.

| Preference / norm | Mechanism | Enforcement artifact | Audit home | Why |
|---|---|---|---|---|
| Provider-agnostic engine: no **financial-institution, account or financial-product name from the deployment roster** in code or schema; the roster, per-account rules, product capabilities and import-format adapters are configuration. **The aggregator is expressly carved out** — a single named dependency in v1 (`system-requirements.md` §0.1), so its client package, the keychain service name and the product name may name it | Test | `tests/preferences/test_no_provider_identity.py` | janitor | The roster changes over the product's life — accounts are added and removed. Hardcoding it makes every roster change a code change and a regression risk, and turns the product into one operator's script. The carve-out is stated because without it the norm forbids what the spec expressly permits, and a reviewer would file a false departure against the connector layer. The test matches the roster config's explicit per-entry tokens on word boundaries over the source and schema roots (`AC-0.3`); the residual judgment case — code that *branches* on provider identity without naming one — is Critic's, under the same norm. The test carries a positive control and a not-scanning-nothing assertion, because a scan over zero files or with an unmatched pattern passes forever. |
| No roster or operator identity in anything pushed: no institution, account, balance, operator name or machine name in any commit reaching a remote — not merely in the working tree, and not merely under the source root | Test | `tests/preferences/check-no-personal-data.sh`, wired into `.githooks/pre-push` on every branch and run from the suite by `tests/preferences/test_no_personal_data.py` | advisory | This repository is a general-purpose tool that may be published, and the leak it actually had was in **documentation** — three doc paths reached a remote — which a source-root check would never have seen. The guard reads the roster's own explicit match tokens from the gitignored `deployment/roster-tokens.txt` (engine AC-0.3) rather than guessing them from labels, and matches on word boundaries. A checkout with no `deployment/` directory has no roster to leak and passes with a note, which is what makes the guard itself publishable. **Migration discharged (Chunk 01):** it moved from `scripts/` to `tests/preferences/` when the scaffold landed, and the pre-push wiring followed it — losing push-time enforcement to gain test-time enforcement would have been a straight downgrade, so the repo now has both. It stays shell: its 22 self-test cases drive real refspecs through a real hook in a throwaway repository, and a Python rewrite would test a reimplementation rather than the thing that runs. |
| Requirement ids are unique within a requirements document | Test | `tests/preferences/test_requirement_ids_unique.py` | janitor | The two requirements docs cite each other by id, and §7 of a roster document calls itself "the checkable form of §0.2" — which it cannot be against an ambiguous key. A duplicate id silently resolves a citation to the wrong requirement, and the one a reader lands on by accident is as likely to be a scheduled job as the immutable enrollment parameter the doc calls its highest-stakes one. Uniqueness is assertable by grep over `**AC-` headers, so it should never again be caught by review. |
| norm lives in `.prawduct/artifacts/architecture.md` § Direction — every writable handle comes from the one writer factory, which takes the lock before it returns | Test | `tests/store/test_connection_norms.py` (lock behaviour, incl. release on SIGKILL) + `tests/preferences/test_connection_is_the_sole_constructor.py` (only `connection.py` calls `connect`) | janitor | Why lives in the Direction entry. Mechanism landed in Chunk 01, closing issue #1; each test was verified to go red with its norm deliberately broken (`tests/preferences/verify_norms_go_red.py`). |
| norm lives in `.prawduct/artifacts/architecture.md` § Direction — read-role handles open `mode=ro`, hold no cross-call read transaction, and never fall back to a writable handle | Test | `tests/store/test_connection_norms.py` — refusal after `PRAGMA query_only=OFF`, and a checkpoint pair whose negative control starves when a snapshot IS pinned | janitor | Why lives in the Direction entry. Mechanism landed in Chunk 01, closing issue #1; each test was verified to go red with its norm deliberately broken (`tests/preferences/verify_norms_go_red.py`). |
| norm lives in `.prawduct/artifacts/architecture.md` § Direction — no component creates the datastore implicitly | Test | `tests/store/test_connection_norms.py` — both layers: the missing-datastore error and the non-creating `mode=rw` constant | janitor | Why lives in the Direction entry. Mechanism landed in Chunk 01, closing issue #1; each test was verified to go red with its norm deliberately broken (`tests/preferences/verify_norms_go_red.py`). |
| norm lives in `.prawduct/artifacts/architecture.md` § Direction — a process that does not recognize the schema version refuses to serve | Test | `tests/store/test_connection_norms.py` — reader refusal, `store status` reporting, and migration atomicity under a real kill | janitor | Why lives in the Direction entry. Mechanism landed in Chunk 01, closing issue #1; each test was verified to go red with its norm deliberately broken (`tests/preferences/verify_norms_go_red.py`). |

**A filled `Delegation` / `Delegate verification` row states a norm, and it takes `Critic`** — a policy stated in prose is judgment-required by construction, so no linter or test can grade it; audit home `janitor`, and the why is the sentence the owner gave for it. One row covers the policy the two state together. `Delegation approval` is a setting like `PR creation`, not a norm. The row is written when the policy is **ratified** (`/prawduct:doctor` proposes, the owner confirms), never shipped here, because this table ships empty.

**Rule for adding a new preference:** assign a mechanism. If the preference can be expressed as "every file/function/config matches pattern X with named exceptions" → write a test. If a linter rule already exists for it → configure the linter. If it requires understanding intent → assign to Critic. Never leave a preference unassigned.

**False-confidence guardrail:** if a generated test would pass on conforming code but couldn't reliably catch a real violation (e.g., greppy heuristics for semantic rules), prefer Critic over a weak test. A green test that doesn't actually check the rule is worse than no test.
