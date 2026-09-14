---
artifact: build-plan
version: 2
scope: build-identity
branch: feature/build-identity
depends_on:
  - artifact: api-contract
    file_path: .prawduct/artifacts/api-contract.md
governed_by:
  - artifact: api-contract
    dispositions:
      - "additive changes only on the MCP surface: new tools, new optional arguments, new response fields → conforms; this adds one response field and one `serverInfo` key, removes and repurposes nothing"
      - "never remove or repurpose an existing field → conforms; `version` in `serverInfo` keeps its meaning and gains a sibling"
      - "🔴 versioning scheme: none, deferred (recorded 2026-09-05) → NOT reversed by this plan. That decision is about versioning the API CONTRACT so consumers can negotiate. This is build PROVENANCE — which code is running — and it sits with `as_of` and `environment`, not with a version handle. The revisit trigger (a consumer outside this machine) is untouched"
lifecycle: completed
archived: 2026-09-14
released_in: v0.1.0
maintained: false
---

> **Archived — no longer maintained.** This plan records what was built, not what will be. Do not edit it to reflect later changes; write those where they are true.

# Build plan — build identity in every answer

## Why

The MCP server is a subprocess the client launches, so it runs whatever code existed at
connect time. Nothing in its output said which code that was. On 2026-09-08 an acceptance
session spent a full round testing a build that predated the fix under test, and caught it
only by fingerprinting a refusal string against a previous round's record. That worked
because a value happened to have changed; it would not have worked for a fix that changed
no observable string.

This is the same commitment the product already makes about data — every answer says how
far to trust it — extended to the code that produced the answer.

## Requirements confidence

**High.** The observable is exact: a caller can read which build answered, without being
told what to expect. Out of scope, deliberately: comparing that against a remote, warning
when stale, and any form of API versioning.

## Chunk 1 — `build_identity()`, captured once, reported in the envelope and `serverInfo`

**Delivers**

- `src/bankmachine/build_id.py`: a frozen `BuildIdentity` with `version`, `commit`, `dirty`,
  captured **once at import** and cached for the life of the process.
- `Answer.to_wire()` gains a `build` key, beside `environment` and `as_of`.
- `_server_info()` gains `commit` and `dirty` alongside its existing `version`.
- `api-contract.md`: the envelope section documents the field and the capture-once rule.

**Acceptance criteria**

1. Every tool response carries `build` with `version`, `commit`, `dirty`.
2. 🔴 The identity is captured **once at import and never re-read**. This is the load-bearing
   requirement, not an optimization: a hash read per request reports the REPOSITORY's current
   HEAD, so a server running stale code would report the merged commit and call itself current
   — building the exact defect this stamp exists to expose into the instrument meant to expose
   it.
3. 🔴 An unknown commit is reported as `null`, and `dirty` is `null` with it — never `false`.
   `false` is a claim that the tree is clean, and a build we cannot identify supports no such
   claim. Absence of evidence is reported as absence, which is this product's whole thesis.
4. A modified working tree reports `dirty: true`, because the hash alone would otherwise be a
   lie by omission about code that is running and is not in that commit.
5. The server starts and answers normally when it is not a git checkout at all.
6. No git invocation happens on the request path.

**Done when** — the above hold, `uv run pytest` is green, ruff check + `ruff format --check` +
mypy strict are clean, the norm harness reports all breaks red, the real server is driven end
to end over stdio, and `/prawduct:critic` has run.

## Status

- [x] Chunk 1 — reviewed (`rev-20260908T182846Z-2e593bb3`), all six findings fixed;
  resolutions verified (`rev-20260908T184013Z-eacec31f`, 0 blocking, 0 findings).

### Accepted, not dropped

🔴 **Every CLI invocation now pays the import-time git capture.** `cli/__init__.py` imports `mcp`,
which imports `query`, which imports `build_id` — so `bankmachine store status` runs two git
subprocesses it has no use for. **Measured 2026-09-08: 37.8 ms of a 573 ms invocation (~6.6%)**,
with a 4 s worst case on a wedged git, bounded by the 2 s per-call timeout and logged.

Accepted rather than fixed: 38 ms against a local checkout does not justify a structural change
made after a review closed. **The remedy is known if CLI latency ever matters** — move the
module-level `_ = build_identity()` into the `mcp` command's entry point. The requirement is that
the SERVER capture before it serves, not that every process capture at import, so warming it at
server start satisfies the reason exactly while removing the cost everywhere else. The test that
holds this would move with it, from import-time to server-start.
