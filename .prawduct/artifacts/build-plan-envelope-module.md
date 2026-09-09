---
artifact: build-plan
version: 1
scope: envelope-module-split
branch: refactor/envelope-module
partition: serial — all three chunks edit `query.py` or the file set that anchors into it; two agents would collide on the same import block
depends_on:
  - artifact: api-contract
    file_path: .prawduct/artifacts/api-contract.md
  - artifact: architecture
    file_path: .prawduct/artifacts/architecture.md
governed_by:
  - artifact: api-contract
    dispositions:
      - "the MCP surface is read-only, no mutation tools → conforms; this plan moves code between modules and adds no tool, no parameter and no write path"
      - "every response carries a freshness stamp, and incompleteness rides the success path as a warning field → conforms, and it is the thing being protected: the whole point of the extraction is that the module owning this guarantee stops being reviewed inside 900 lines of unrelated SQL"
      - "the CLI's three-way exit code → inapplicable; nothing here touches the CLI"
      - "🔴 a tool's boundary is drawn where the answer shape changes → inapplicable, and the distinction matters enough to write down. That norm governs where a TOOL boundary falls on the wire. This plan draws a MODULE boundary inside the server and changes no tool, so the norm neither licenses nor forbids it. Recorded rather than assumed because the two boundaries use the same word and a reviewer could reasonably ask"
      - "additive changes only; never remove or repurpose an existing field → conforms; the wire is byte-identical by acceptance criterion, not by intention"
  - artifact: architecture
    dispositions:
      - "every writable handle comes from the one writer factory → inapplicable; this plan opens no handle"
      - "🔴 every read-role handle is opened read-only at the file → conforms, unchanged; `reader_connection` keeps its single call site pattern and no code moves across that call"
      - "no component creates the datastore implicitly → inapplicable"
      - "a process that does not recognize the schema version refuses to serve → inapplicable; no schema version is read or written here"
---

# Build Plan — Envelope Module Split

**Backlog item:** `brookstalley/bankmachine#39`
**Type:** refactor (behavior preservation; tests do not change)
**Size:** medium

## Requirements Confidence

**High.** Problem, success and scope each state in one sentence, and the one open design
fork was closed by the owner this cycle before any code was written.

- *Problem:* `src/bankmachine/query.py` is 2101 lines owning both the wire-envelope model and
  every tool's SQL, so an envelope change is reviewed inside unrelated SQL and each new tool
  makes the seam more expensive.
- *Success:* the envelope model lives in its own module, `query.py` and `mcp_resources.py`
  import it, the published schemas and reference documents are byte-identical, and the suite is
  green at its baseline count.
- *Out of scope:* no tool's SQL is rewritten, no new tool lands, no wire-contract change.

### Open assumptions

`[ASSUMPTION: the new module is named src/bankmachine/envelope.py | LOW impact | user can
correct — it is a rename away at any point before chunk 01 commits]`

## Decisions

`[DECISION: per-tool published vocabulary (GROUPINGS, FLOW_CLASSES, BadGroupingError,
UnknownAccountError) stays with the SQL that produces it; mcp.py continues to import it from
query.py and imports the envelope from the new module | Building the per-tool contract object
instead would mean fitting that abstraction to five tools while three more are specified and
unbuilt — designing against the wrong sample, inside a change whose safety rests on being a
pure move. Keeping it here also preserves GROUPINGS' own stated reason for living beside the
SQL: it is a closed set because "the grouping is an expression this module builds, never a
column name a caller supplies" | owner chose this over the alternatives 2026-09-09; user can
veto/override]`

`[DECISION: the three-site per-tool key declaration (query._answer, query._unusable,
mcp._output_schema's windowed/capped/totals flags) collapses to one declaration when a FOURTH
conditional key arrives, not now | #39 itself records that invalid combinations are not
silently reachable today because both branches validate against the published schema, which is
what held the finding to a note. A fourth flag is the point at which the pattern stops being
cheaper than the model it is standing in for. Recorded as a standing ruling so the next builder
inherits the trigger rather than re-deriving it | owner chose this 2026-09-09; user can
veto/override]`

## The Boundary

Not "envelope vs SQL" — that cut does not survive contact with the file. Three categories:

1. **Pure wire model and vocabulary — MOVES.** `STALE_AFTER`, `MAX_ROWS`,
   `CONNECTION_SCOPED_KINDS`, `REQUEST_SCOPED_KINDS`, `WARNING_KINDS`, `Caveat`, `Window`,
   `Truncation`, `Cursor`, `Answer`, `resolve_window`, `parse_cursor`, `InvertedWindowError`,
   `MalformedCursorError`, and their private helpers. Touches no database.
2. **Assembly over the datastore — STAYS.** `_answer`, `_unusable`, `_pipeline_warnings`,
   `_coverage`, `_account_coverage`, `AccountCoverage`, `_uncovered_caveat`, `_covered_rows`,
   `_is_short`, `_readable`. `_answer` takes a live `SAConnection` and reconciles the requested
   window against `_coverage`; that is *how this store fills an envelope*, which is query-layer
   work, not model.
3. **Tool SQL and per-tool vocabulary — STAYS.** The five tool functions, `GROUPINGS`,
   `FLOW_CLASSES`, `KNOWN_SOURCE_CATEGORIES`, `_flow_class`, the category frozensets, and the
   two tool-domain error classes, per the decision above.

**The invariant that makes the seam checkable, and self-enforcing:** the new module touches no
database. No `SAConnection`, no `reader_connection`, no statement execution. Chunk 01 lands a guard
test asserting it, so the seam cannot erode the way a convention would — which is the same
reason `_refuse_optional_row_fields` runs inside `_tool_definitions()` rather than sitting in a
style guide.

## Status

- [x] **01 — Extract the envelope module**
- [x] **02 — Re-point the go-red anchors**
- [ ] **03 — Artifacts, ruling, and backlog close**

## Chunks

### Chunk 01: Extract the envelope module

**Type:** code

Create new `src/bankmachine/envelope.py` holding category 1 above. `query.py` imports from it;
`mcp_resources.py` re-points its three vocabulary reads; `mcp.py` re-points its envelope reads
(`WARNING_KINDS`, `MAX_ROWS`, `Cursor`, `parse_cursor`, `Answer`, `InvertedWindowError`,
`MalformedCursorError`) and keeps importing category 3 from `query.py`.

This is the thin vertical slice: it proves the import boundary holds across all three consumers
before anything else is disturbed.

**Done when:**
1. `envelope.py` exists and holds every category-1 name; none remain defined in `query.py`.
2. A new guard test asserts `envelope.py` reaches no database — no `SAConnection` import, no
   `reader_connection`, no statement execution — so the boundary is enforced rather than
   described.
3. `mcp_resources.py` imports the envelope from the new module and no longer reads it from
   `query.py`.
4. `mcp.py` imports envelope names from the new module. Whether it still imports from
   `query.py` is answered deliberately: **yes**, for category 3, per the recorded decision.
5. Published schemas and the reference documents are byte-identical — diffed, not asserted by
   eye. This is the acceptance criterion the whole plan rests on.
6. Suite green at the recorded baseline, plus the new guard's two cases.

**Correction, recorded rather than quietly satisfied.** This chunk was written
claiming "no test file changes." That was wrong, and the build proved it: tests import
the moved names from `bankmachine.query` directly, so the move re-points them the same
way it re-points `mcp.py`. That is an import change, not a change to what any test
asserts — the contracts are untouched — but the original line would have read as
satisfied while six test files changed, so it is replaced rather than reinterpreted.

🔴 **One of those re-points was load-bearing, and is the reason this is worth writing
down.** `test_a_kind_added_to_the_vocabulary_reaches_the_reference_by_itself`
monkeypatches `CONNECTION_SCOPED_KINDS` on the module `mcp_resources` reads it from.
Rewriting the attribute *reads* without moving the `setattr` *target* would have left
the patch aimed at `query` while the code under test read `envelope` — the patch would
have stopped reaching, and the test would have gone green while asserting nothing. It
failed loudly instead, which is the only reason it was caught. A sweep that renames
reads but not patch targets is the general form of this trap.

**Verification beyond tests:** start the MCP server and call each of the five tools, diffing the
full envelope against the same call on `develop`. A refactor that keeps the suite green and
changes a payload key is exactly the failure this chunk can produce.

**Result, recorded 2026-09-09 — a declared verification whose outcome is written nowhere is
indistinguishable from one nobody ran.** A `develop` worktree was checked out at `be10ac0` and
both surfaces were dumped from each tree and compared as bytes:

- **Tool schemas:** all five `_tool_definitions()` entries, serialised with sorted keys —
  44,113 bytes on both sides, `diff` clean.
- **Reference documents:** `mcp_resources.documents()` over those definitions, covering
  `bankmachine://reference/warnings` and `bankmachine://reference/envelope` — 14,592 bytes on
  both sides, `diff` clean.

Re-run after the test re-points and again after the review fixes; identical each time. The
comparison is of the published surface rather than of a live stdio session — the server
assembles its payload from exactly these definitions, so this pins the contract a client reads,
and a per-tool live call would add row data that the fixture, not this change, determines.

### Chunk 02: Re-point the go-red anchors

**Type:** code

22 cases in `tests/preferences/verify_norms_go_red.py` anchor `(file, exact source text)` pairs
into `src/bankmachine/query.py`. Every anchor whose text moved in chunk 01 now finds nothing and
reports as a **survivor** — correct behavior, and the reason the harness reports a missing
anchor as a survivor rather than a pass.

Re-point each moved case at the new module. Scope by the pattern, not by line: every case whose
anchor text now resolves in `envelope.py` rather than `query.py`.

**Done when:**
1. Every case anchoring into the moved region names the new module.
2. The harness runs clean — every case red, no survivors, no `SKIP … anchor no longer present`.
3. The count of go-red cases is unchanged: this chunk re-points cases, it never drops one.
   A case that cannot be re-pointed is reported, not deleted.

**Sequencing, and it is a hard constraint:** `verify_norms_go_red.py` sabotages the working
tree, and a Critic review reads it. Commit → run the harness → review. Never the harness and a
review against the same tree.

### Chunk 03: Artifacts, ruling, and backlog close

**Type:** doc-only

1. Record the fourth-conditional-key ruling in `learnings.md` so the trigger outlives this plan,
   and update `architecture.md`'s module description to name the new boundary and its no-database
   invariant.
2. Answer #39's four acceptance criteria explicitly, including the one added this cycle.
3. Add the `.prawduct/change-log.md` entry for this scope — `scope=envelope-module-split`,
   and **no** `release=`, since any value at all drops the scope out of the
   release-pending set and silently unships the work.
4. Close #39 via `/prawduct:backlog update status=shipped`.

**Done when:** the ruling is findable by someone adding a fourth conditional key who has never
read this plan; `architecture.md` describes the module boundary that now exists; #39 is closed
with each acceptance line answered rather than assumed.

The three chunks ship as one PR, so this chunk's own review is the single cumulative
pass over `merge-base...HEAD` — no separate `final`. Mode is left to inference rather
than declared: the earlier `Critic mode: cumulative-final` named a token no reader
recognises, and an unrecognised override is discarded in silence rather than refused.

## Governance checkpoints

- **After chunk 01** — the architecture validation point. If the published schemas are not
  byte-identical, the extraction is wrong and no later chunk can repair it.
- **Before chunk 03** — confirm the decisions above still hold against what the code revealed, per the
  standing rule that unrevisited assumptions are decisions taken on the user's behalf.

## What I would do differently

The advisory obligation, stated rather than left implied:

**I would not do this refactor at all if #20 were not queued behind it.** On its own merits a
2101-line module with a clean internal boundary and 45% explanatory prose is not urgent — the
file is long, but it is not confusing, and "split the big file" is the kind of work that feels
productive while changing nothing a user can observe. What makes it worth doing *now* is
sequencing: #20 adds three filters and their SQL to this file, and every line it adds has to be
re-read by whoever eventually splits it. The value here is entirely in the ordering, and if #20
slips indefinitely this plan should be shelved rather than executed on principle.

**The scope I would cut if asked to make this smaller:** chunk 02. Re-pointing 22 anchors is the
largest mechanical cost in the plan and it buys no behavior. But it cannot actually be cut —
leaving the anchors stale converts a working guard into 22 silent survivors, which is worse than
not having split the file. Naming it as the expensive part is the honest version.

**The risk the requirements do not price:** a pure move is the easiest change to review and the
easiest to get subtly wrong, because reviewers pattern-match "it's just a move" and stop reading.
The byte-identical schema diff in chunk 01 exists specifically because eyes are not sufficient here.
