# Project Preferences

Developer preferences for how code is written in this project. Captured during discovery, updated as preferences evolve. Every session should read this before writing code.

## Language & Runtime

- **Language**: Python
- **Version**: 3.11+ (verified on 3.12.3, Apple Silicon). Native macOS, not containerized.
  **macOS is supported and tested; other platforms are unverified, not excluded** — the credential
  store and the scheduler sit behind seams so a port is a new implementation, not a refactor.
- **Package manager**: uv (dependency resolution, lockfile, venv). `uv run` for all dev commands.

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

- **Data modeling**: (unset — decide during planning; a typed model layer is expected given mypy strict)
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
- **Dev commands**: (unset — set when the project scaffold lands)

## Workflow

- **Branching**: feature-branches (default: feature-branches — create a branch for medium+ work, direct commits to protected branches only for trivial fixes; set to "direct" for solo projects where committing to main is OK)
- **Protected branches**: main, develop (branches that should not receive direct commits unless branching is "direct")
- **PR creation**: wait_for_user (default: wait_for_user — only create PRs when explicitly asked; set to "automatic" to create PRs after Critic review passes)
- **PR merge**: wait_for_user (default: wait_for_user — present the PR for user review before merging; set to "automatic" to merge after CI passes and review is clean)
- **PR merge strategy**: merge commit (default: merge commit — `gh pr merge --merge`; preserves each commit's identity so a reused branch's merge-base stays correct and the review/PR gates don't re-review already-merged work; set to "squash" for one linear commit per PR, or "rebase" — with either, branches are single-use: delete after merge and never reuse, because the rewritten history strands a reused branch's merge-base)
- **Commit attribution**: none (default: none — no `Co-Authored-By`, `Signed-off-by`, or "Generated with …" trailers on commits or PR bodies; set to "co-authored" to add a Claude `Co-Authored-By` trailer)
- **Delegation**: (unset — `/prawduct:methodology delegation` states the default and anything written here overrides it. Say in prose how much this project wants fanned out to subagents and what it is worth fanning out for. `off` is a complete answer: it means no delegation at all, it is honoured without ceremony, and nothing nags a repo that has said it.)
- **Delegate verification**: (unset — what a delegate here may run to prove its own change, and what it must leave to the coordinator's integration run. In this project's own words: prawduct does not know this project's test regime and will not invent a vocabulary for it. `/prawduct:doctor` will propose a starting point from what this repo already encodes about running part of its suite.)
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
| Provider-agnostic engine: no **financial-institution, account or financial-product name from the deployment roster** in code or schema; the roster, per-account rules, product capabilities and import-format adapters are configuration. **The aggregator is expressly carved out** — a single named dependency in v1 (`system-requirements.md` §0.1), so its client package, the keychain service name and the product name may name it | Test | `tests/preferences/test_no_provider_identity.py` | janitor | The roster changes over the product's life — accounts are added and removed. Hardcoding it makes every roster change a code change and a regression risk, and turns the product into one operator's script. The carve-out is stated because without it the norm forbids what the spec expressly permits, and a reviewer would file a false departure against the connector layer. The test matches the roster config's explicit per-entry tokens on word boundaries over the source and schema roots (`AC-0.3`); the residual judgment case — code that *branches* on provider identity without naming one — is Critic's, under the same norm. |
| No roster or operator identity in a tracked file: no institution, account, balance, operator name or machine name anywhere in the repository, not only under the source root | Test | `scripts/check-no-personal-data.sh`, wired into `.githooks/pre-push` on every branch | advisory | This repository is a general-purpose tool that may be published, and the leak it actually had was in **documentation** — three doc paths reached a remote — which a source-root check would never have seen. The guard reads the roster's own explicit match tokens from the gitignored `deployment/roster-tokens.txt` (engine AC-0.3) rather than guessing them from labels, and matches on word boundaries. A checkout with no `deployment/` directory has no roster to leak and passes with a note, which is what makes the guard itself publishable. 🔴 **Migration obligation:** it is a shell script only because no Python scaffold exists yet and creating one would fix the package name ahead of the rename decision — move it under `tests/preferences/` when the scaffold lands. |
| Requirement ids are unique within a requirements document | Test | `tests/preferences/test_requirement_ids_unique.py` | janitor | The two requirements docs cite each other by id, and §7 of a roster document calls itself "the checkable form of §0.1" — which it cannot be against an ambiguous key. A duplicate id silently resolves a citation to the wrong requirement, and the one a reader lands on by accident is as likely to be a scheduled job as the immutable enrollment parameter the doc calls its highest-stakes one. Uniqueness is assertable by grep over `**AC-` headers, so it should never again be caught by review. |

**A filled `Delegation` / `Delegate verification` row states a norm, and it takes `Critic`** — a policy stated in prose is judgment-required by construction, so no linter or test can grade it; audit home `janitor`, and the why is the sentence the owner gave for it. One row covers the policy the two state together. `Delegation approval` is a setting like `PR creation`, not a norm. The row is written when the policy is **ratified** (`/prawduct:doctor` proposes, the owner confirms), never shipped here, because this table ships empty.

**Rule for adding a new preference:** assign a mechanism. If the preference can be expressed as "every file/function/config matches pattern X with named exceptions" → write a test. If a linter rule already exists for it → configure the linter. If it requires understanding intent → assign to Critic. Never leave a preference unassigned.

**False-confidence guardrail:** if a generated test would pass on conforming code but couldn't reliably catch a real violation (e.g., greppy heuristics for semantic rules), prefer Critic over a weak test. A green test that doesn't actually check the rule is worse than no test.
