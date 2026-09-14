# Release plan — v0.1.0

The first release of bankmachine: `develop` promoted to `main` and tagged `v0.1.0`.

**Why now:** production has run against real accounts since 2026-09-11 — enrollment, sync, the
encrypted datastore and the read-only MCP server end to end, with a verified key backup and a
scheduled sync. Everything on `develop` has been reviewed and merged through its own PR, and the one
defect found on first production use of an investment-only institution (#123) is fixed ahead of it.

**Version:** `pyproject.toml` and `bankmachine.__version__` already read `0.1.0`, so no version file
moves. The owner asked for v0.1; the tag is written `v0.1.0` because a change-log `release=` tag must be
`vMAJOR.MINOR.PATCH` and the published tag has to agree with the version files it ships.

**Nothing is withheld.** Every release-pending scope ships. Two obligations stay open and ride
forward as pending operator verifications rather than as withholdings, because neither is a defect
in shipped code: a real pending transaction watched across settlement (#22) and the sign convention
on a real inflow at a second cash institution (#23).

**After promotion:** the production worktree is pinned to a commit and moves only when the operator
moves it. This release carries derivation version 12, so moving it follows the upgrade order,
`store rebuild` included.

## Release classification

| Scope | Disposition | Blocker |
|---|---|---|
| empty-complete-transactions-page | ships |  |
| window-concluded-with-its-page | ships |  |
| pre-production-fixes | ships |  |
| investments-followups | ships |  |
| transaction-filters | ships |  |
| investments-v1 | ships |  |
| ci-runs-the-gate | ships |  |
| mypy-green-and-gated | ships |  |
| duplicated-roster-double-count | ships |  |
| environment-guard | ships |  |
| connections-reauth | ships |  |
| shell-banner-role-line | ships |  |
| backup-at-any-schema-version | ships |  |
| datastore-key-escrow | ships |  |
| production-cutover-hardening | ships |  |
| unservable-datastore | ships |  |
| production-blocker-findings | ships |  |
| production-blockers | ships |  |
| envelope-module-split | ships |  |
| mcp-answer-scope-completion | ships |  |
| mcp-tool-surface-norm | ships |  |
| mcp-alignment | ships |  |
| mcp-answer-scope | ships |  |
| observability | ships |  |
| sync-v1 | ships |  |
| enrollment-v1 | ships |  |
| connector-v1 | ships |  |
| strategy-artifacts | ships |  |
| datastore-v1 | ships |  |
| architecture | ships |  |
| rename | ships |  |
| repo-sanitization | ships |  |
