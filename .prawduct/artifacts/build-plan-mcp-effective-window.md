---
artifact: build-plan
version: 2
scope: mcp-answer-scope
branch: feature/mcp-effective-window
depends_on:
  - artifact: discovery
    file_path: .prawduct/artifacts/discovery-mcp-answer-scope.md
  - artifact: api-contract
    file_path: .prawduct/artifacts/api-contract.md
  - artifact: nonfunctional-requirements
    file_path: .prawduct/artifacts/nonfunctional-requirements.md
governed_by:
  - artifact: api-contract
    dispositions:
      - "MCP surface is read-only, no mutation tools → inapplicable because every deliverable here is a read path or a response field; no tool is added"
      - "incompleteness rides the success path as a warning field, never as an exception → conforms, and this plan is that norm's largest application: a window narrower than the one asked for is exactly 'a successful response computed over incomplete data', and it currently says nothing"
      - "warning vocabulary is a minimum; new warning codes are additive and need no version bump; consumers tolerate unknown enum values → conforms; the two new kinds are additive by the norm's own terms, so no amendment to AC-9.3's list is needed"
      - "additive changes only: new tools, new optional arguments, new response fields → conforms; `cursor` is a new optional argument, and every envelope block below is a new field"
      - "🔴 never remove or repurpose an existing field → BINDING CONSTRAINT ON CHUNK 02. `coverage.transactions` is store-wide today. Window-scoping it in place is a repurpose — the exact failure the norm names, a consumer still reading it gets a wrong answer rather than an error. Chunk 02 adds a sibling and leaves it alone"
      - "raw-row cap ~500, hard — a contract term, not a tuning knob → conforms; Chunk 03 escapes truncation with a cursor and does NOT raise the cap. Measurement already refuted raising it: 74.2% of rows are dropped at full coverage, so no default a human would pick fixes this"
      - "aggregates are unpaginated, bounded by grouping → conforms; the truncation block and the cursor land on `query_transactions` only, never on `spending_summary`"
      - "CLI three-way exit code → inapplicable because nothing here touches the CLI"
  - artifact: operational-spec
    dispositions:
      - "🔴 an out-of-range history window is refused rather than clamped, 'because a clamp would enroll at a window the operator never chose and never told them about' (line 113) → applies by analogy, and this plan honours the WHY rather than the letter. [DECISION: a query window is clamped and announced, not refused | the norm's reason is the operator never being TOLD; enrollment clamping silently costs history that cannot be bought back, while a query clamp costs nothing and refusing 'show me 2024' against coverage starting 2024-09-16 would refuse an ordinary question. `effective_window` is the telling. 🔴 The clamp is reportorial, not selective — it changes no predicate and no returned row | user can veto]"
      - "no filesystem path is hardcoded → inapplicable"
      - "a backup destination is never created implicitly → inapplicable"
  - artifact: nonfunctional-requirements
    dispositions:
      - "MCP aggregate tool response under ~1s over 24 months → ruling needed at Chunk 02, not before: `matching` costs a second COUNT per call. Free on this 388-row fixture and unmeasured at real volume. Chunk 02's Done-when measures it rather than assuming it"
  - artifact: data-model
    dispositions:
      - "calendar dates and UTC instants are distinct types and never mix → conforms, and it is load-bearing here: the window bounds are `date`, `as_of` is a UTC instant, and clamping `until` against today is the one place they meet. The comparison converts explicitly at that site"
      - "every stored amount is signed from the operator's point of view → inapplicable because no amount is read or written by this plan"
partition: serial — all three chunks edit the same two modules (`query.py` and `mcp.py`), and each extends the statement the previous one built: 02 counts the rows 01's window selects, 03 pages the rows 02 counted. A fan-out would have three delegates editing one `select()` against a window type none of them had settled.
last_validated: 2026-09-08
---

# Build plan — the window and row count an answer actually used

Wave 1 of the work cycle opened by `.prawduct/artifacts/discovery-mcp-answer-scope.md`.
That discovery plans six issues as one defect on six axes and specifies four chunks;
**this plan builds its first, the time and row axes, and closes #16 and #17.** The
account/field, classification, and direction/units axes get their own plans when their
wave starts — four chunks spanning six issues is a program, not a plan.

## Advisory — what I would do differently

**On scope: nothing. This is the right first wave, and it is the right size.** The time
and row axes are the two best-evidenced of the six, they share one mechanism, and they
are the only two where a caller is currently handed a *precise wrong number* rather than
a vague one. Everything below is a caveat on execution, not a case for a different shape.

**The chunk worth cutting under pressure is 03, and I would say so before starting rather
than during.** Chunks 01–02 remove the *lie* — a caller who asks for two years of card
activity and sums the answer understates it by ~40%, with nothing in the payload saying
so. Chunk 03 removes the *limit*. The discovery says #17 "must ship a total or a cursor"
— **or**. If this wave has to end early, 01–02 is a coherent, shippable, honest surface
and 03 is a clean follow-on. I am planning all three because #17 deserves to close
properly, not because 03 is load-bearing for 01–02.

**The risk the requirements do not price is `matching`.** It costs a second `COUNT(*)`
on every `query_transactions` call, and `nonfunctional-requirements.md` promises under
~1s over 24 months. On this 388-row fixture that cost is unmeasurable. I am not going to
assume it stays free at real volume, so Chunk 02 measures it against a synthetic
two-year store as a Done-when step. If it does not hold, the fallback is an estimated
count with an explicit `approximate` flag — worse, but honest, which is the whole point
of this work cycle.

**The design position I hold most strongly: `effective_window` must be a type, not two
fields added to two functions.** C2, C3 and C4 all add windowed surfaces
(`get_coverage_report`, `cashflow_summary`). If Chunk 01 ships as a per-tool patch, each
later wave re-implements the clamp and they drift — which is precisely what #16's own
triage comment predicted about a second mechanism, and precisely the failure mode
`learnings.md` § *Guarantees by construction* names: *"a guarantee defined by an
enumeration decays."* So the windowed query functions stop taking `since`/`until` and
start taking a `Window`, whose only route into existence computes its own caveats. **A
future windowed tool cannot be written without the clamp, because it cannot be called
without one.** That is the repo's existing idiom — `Answer.warnings` has no default for
the same reason — not a new invention.

**One thing I am deliberately not building, so it does not get built twice.** `coverage`
stays store-wide in this plan even when `account_id` narrows the query. Per-account
coverage is #19, specified in the discovery's C2, and doing half of it here would leave
C2 amending a field this plan just shipped.

## Requirements Confidence

**Level:** High

**Why:** The problem is stated in one sentence (an answer does not say which window or
how many rows it actually used), the success criterion is stated in one sentence (a
caller can tell from the response alone, without knowing what to expect), and the scope
is two issues that a committed discovery already decomposed. Both axes were measured by
an independent acceptance session against a build identified by commit — not inferred —
and the numbers are on record in `.prawduct/artifacts/mcp-fact-find-ac91.md` and
`.prawduct/artifacts/mcp-acceptance-round-4-half-a.md`. The owner's scope ruling
("envelope + two tools") is recorded on the issues.

**Open assumptions / unknowns:**

- [ASSUMPTION: `effective_window` reports the clamp but changes no predicate, so no
  currently-returned row or figure moves | HIGH impact | user can veto] — This is what
  makes Chunk 01 safe to land in one pass. It holds because clamping `since` up to
  `coverage.earliest_transaction` and `until` down to `as_of` can only remove rows that
  do not exist. **Chunk 01's acceptance criterion is that the existing MCP test file
  passes untouched**; if any assertion there has to change, this assumption was wrong and
  the chunk stops.
- [ASSUMPTION: an unbounded request (no `since`/`until`) should report the coverage span
  as its effective window, rather than reporting `null` | MED impact | user can override]
  — It is the case where a caller most needs the answer, and it costs nothing extra.
- [ASSUMPTION: the cursor is opaque keyset state over `(posted_date, transaction_id)`,
  not an offset | MED impact | user can override] — The existing `ORDER BY posted_date
  DESC, transaction_id DESC` is already a total order, so keyset is available for free
  and is stable under a concurrent sync in a way `OFFSET` is not. `api-contract.md`
  specifies "cursor-based, opaque" and this is the cheapest thing that is genuinely both.

**What would raise confidence:** N/A at High. The one open measurement is the `matching`
latency question, which Chunk 02 answers as a build step rather than a precondition.

## Status

- [ ] Chunk 01: `Window` — the clamp a windowed tool cannot skip, and the two warnings that announce it
- [ ] Chunk 02: `returned` / `matching` / `truncated`, so a capped answer stops reading as a complete one
- [ ] Chunk 03: cursor pagination, so truncation is escapable rather than only visible

Context: Plan written 2026-09-08 on `feature/mcp-effective-window`, branched from
`develop` at `a4e78e5`. Nothing built yet. Next: Chunk 01. The peer acceptance session is
NOT dispatched during this plan's chunks — it is relaunched and briefed once, after
Chunk 03, because its MCP server is a subprocess it cannot restart (#25) and each
mid-plan round would spend that relaunch on a build about to change.

## Scaffolding

Already built. This plan extends a shipped surface: `src/bankmachine/query.py` holds the
`Answer` envelope and the four query functions, `src/bankmachine/mcp.py` holds argument
narrowing and dispatch, and `tests/test_mcp.py` is the surface's test file. No new
dependency, no new module, no scaffold step.

### Verification Strategy

Tests are the floor, and for this work they are unusually weak evidence on their own:
every defect being fixed here produced a *well-formed, plausible* payload that passed
every test it had. So each chunk also verifies by driving the real server over stdio and
reading the payload as a consumer would — the same route the acceptance rounds used, and
the only one that can show a field is present, correctly named, and says something true.

`learnings.md` § *Guarantees by construction* sets the bar for the tests themselves:
**break the thing each check names and watch it fail.** Two specific traps apply here and
are called out in the chunks — assert whole phrases rather than `in`, because
`window_starts_before_coverage` and `window_extends_past_today` share a prefix with each
other and `partial` is a substring of nothing but sits beside `gapped` in a list that is
easy to assert loosely; and after Chunk 03 changes control flow through the statement
builder, ask which existing tests now short-circuit.

## Build Chunks

### Chunk 01: `Window` — the clamp a windowed tool cannot skip, and the two warnings that announce it

- **Description:** Every windowed answer states the window it was asked for and the
  window it could actually cover, and warns when they differ. The clamp is *reportorial*:
  it changes no predicate and no returned row. Delivered as a type rather than as two
  patches, so that C2's `get_coverage_report` and C4's `cashflow_summary` inherit it
  instead of re-deriving it.
- **Depends on:** none
- **Artifacts consumed:** `.prawduct/artifacts/discovery-mcp-answer-scope.md` (C1, the
  time axis), `.prawduct/artifacts/api-contract.md` § Warning vocabulary,
  `.prawduct/artifacts/mcp-acceptance-round-4-half-a.md` A2 and A3
- **Deliverables:**
  - A frozen `Window` in `src/bankmachine/query.py`, beside `Caveat` and `Answer`,
    carrying `requested_since`, `requested_until`, `effective_since`, `effective_until`
    and `caveats`. **`caveats` has no default**, exactly as `Answer.warnings` has none:
    a window that has not been reconciled against coverage cannot be constructed, so the
    guarantee holds by construction rather than by every future author remembering it.
    Built only by a resolver that takes the requested bounds, the coverage span and
    `as_of`.
  - `list_transactions` and `spending_by_category` take `window: Window` in place of
    `since`/`until`. This is the structural half — a new windowed tool cannot be called
    without a resolved window.
  - Two additive members in `WARNING_KINDS`: `window_starts_before_coverage` and
    `window_extends_past_today`, each raised **only when this request's window actually
    crosses the boundary** — the fix for A2, where one invariant connection-level
    `gapped` notice rides every response and therefore says nothing about any of them.
  - `effective_window` in `Answer.to_wire()`, present on windowed answers only, carrying
    the requested and effective bounds side by side.
  - Tool descriptions in `_tool_definitions()` updated to say the window is clamped and
    reported (AC-9.4's "tool descriptions state their conventions").
- **Tests:** unit — the resolver over the boundary matrix: window wholly inside coverage
  (no caveat), starting before it, ending after `as_of`, both, and unbounded (effective
  = coverage span). Integration — a full stdio round trip asserting `effective_window`
  and each warning kind **by whole phrase, never by `in`**: the two kind strings share a
  `window_` prefix and a substring assertion cannot tell them apart, which is the trap
  `learnings.md` records twice. Regression — `tests/test_mcp.py` passes **unmodified**.
- **Acceptance criteria:** `uv run pytest -q` passes with `tests/test_mcp.py` unchanged;
  `spending_summary{since:2024-01-01, until:2024-06-30}` — the measured case that reads
  as "you spent nothing" against coverage starting 2024-09-16 — returns an answer whose
  `effective_window` and warning together say the window is outside coverage; a
  fully-covered August 2026 query returns the identical figures it returns today with no
  window warning.
- **Critic mode:** final
  <!-- Override: inference picks `chunk` mid-plan. This lands the keystone every later
       wave of the work cycle inherits — three more windowed tools are specified against
       it — and its coherence matters before 02 and 03 build on it, let alone C2/C4. -->
- **Done when:**
  1. Acceptance criteria met and tests pass
  2. Each new assertion verified by breaking what it names and watching it fail — for the
     two warning kinds specifically, swap one kind string for the other and confirm the
     test goes red rather than passing on a shared prefix
  3. `/prawduct:critic` run and blocking findings resolved
  4. Committed and chunk marked `[x]` in Status

### Chunk 02: `returned` / `matching` / `truncated`, so a capped answer stops reading as a complete one

- **Description:** `query_transactions` says how many rows matched, how many it returned,
  and whether it stopped early. Measured harm: the documented default of 100 silently
  drops ~16 months of one account's history, and summing what comes back understates a
  two-year card total by roughly 40% with nothing in the payload saying so.
- **Depends on:** Chunk 01
- **Artifacts consumed:** `.prawduct/artifacts/discovery-mcp-answer-scope.md` (C1, the
  row axis), `.prawduct/artifacts/mcp-acceptance-round-4-half-a.md` A1,
  `.prawduct/artifacts/nonfunctional-requirements.md` § latency
- **Deliverables:**
  - A truncation block on `query_transactions` answers only — `returned`, `matching`,
    `truncated` — never on `spending_summary`, which `api-contract.md` fixes as
    unpaginated.
  - 🔴 **One predicate list, applied to both the row query and the count.** The two
    statements must agree, and a plan that says "remember to update both" is the
    enumeration failure this repo has already been bitten by. The filters are built once
    and handed to each. A filter added later to one is added to both by construction.
  - A window-scoped transaction count as a **new** `coverage` sibling. `coverage.transactions`
    keeps its store-wide meaning untouched — window-scoping it in place is the repurpose
    `api-contract.md` names in red, where a consumer still reading it gets a wrong answer
    rather than an error.
- **Tests:** unit — the count agrees with the row query across window, account and
  default-limit combinations, including the measured case (account 4 over 2024-01-01..2026-12-31
  returns 100 of a larger `matching` with `truncated` true). Guard — a test that fails if
  the row query and the count are ever built from different predicates, since that
  divergence is the failure mode and it is silent. Regression — an untruncated answer
  reports `truncated` false and `returned == matching`.
- **Acceptance criteria:** the A1 call returns `truncated: true` with `matching` well
  above `returned`; a narrow window returns `truncated: false`; `spending_summary` gains
  no truncation block.
- **Done when:**
  1. Acceptance criteria met and tests pass
  2. **Measure the count's cost** against a synthetic ~24-month store, not the 388-row
     fixture, and record the figure against the under-~1s target. If it does not hold,
     stop and put the approximate-count fallback to the owner rather than shipping a
     latency regression quietly
  3. Each new assertion verified by breaking what it names and watching it fail
  4. `/prawduct:critic` run and blocking findings resolved
  5. Committed and chunk marked `[x]` in Status

### Chunk 03: cursor pagination, so truncation is escapable rather than only visible

- **Description:** A caller who sees `truncated: true` can reach the rest. Closes #17's
  other half — visibility without a route past the cap would leave the honest answer
  still unobtainable.
- **Depends on:** Chunk 02
- **Artifacts consumed:** `.prawduct/artifacts/api-contract.md` § Pagination and caps
- **Deliverables:**
  - An optional `cursor` argument on `query_transactions` — added to
    `_tool_definitions()` and therefore to `_permitted_arguments()`, so a misspelling is
    refused by name like every other argument.
  - `next_cursor` in the envelope when and only when `truncated` is true.
  - Opaque keyset state over `(posted_date, transaction_id)`, the existing total order.
    **Not an offset:** a concurrent sync inserting a row shifts every offset page and
    silently duplicates or skips, which would reintroduce this work cycle's defect
    through a new door.
  - The 500-row cap is **unchanged**. It is a contract term, and measurement already
    showed no default fixes this: 74.2% of rows are dropped at full coverage.
- **Tests:** unit — cursor round trip returns every row exactly once with no duplicate
  and no gap across pages; a malformed or foreign cursor is refused naming the argument,
  in the established refusal form, leaking no exception class; the final page reports
  `truncated: false` and carries no `next_cursor`. Integration — a paged walk of the A1
  case reassembles the full `matching` count.
- **Type:** cumulative-final
  <!-- Last chunk: its review IS the one `/prawduct:critic cumulative` over
       merge-base...HEAD — commit first, run once, no separate `final`. -->
- **Done when:**
  1. Acceptance criteria met and tests pass
  2. **Ask which existing tests now short-circuit.** Chunk 03 changes control flow
     through the statement builder, and `learnings.md` records that a control-flow change
     can orphan a test without touching it — mutation testing is blind to this, because
     reverting removes the damage along with the fix
  3. Each new assertion verified by breaking what it names and watching it fail
  4. Committed, then `/prawduct:critic cumulative` run and blocking findings resolved
  5. Chunk marked `[x]` in Status

## Early Feedback Milestone

**Milestone chunk:** 01
**What the user can do:** ask the running server a question whose window is partly
outside coverage and read, in the payload, which window it actually answered over — the
thing four acceptance rounds each had to work out by hand from `coverage`.

## Governance Checkpoints

**Commit & PR cadence:** commit per chunk after its Critic review passes. Chunk 03's
`cumulative` review makes the branch PR-ready; `/prawduct:pr create` runs when the user
asks.

- **After Chunk 01 (architecture):** confirm `Window` is genuinely the only route into a
  windowed query before 02 and 03 build on it, and before C2/C4 are planned against it.
  The question to ask is the one `learnings.md` frames: not "does the clamp work" but
  "what else can reach a windowed answer without one".
- **After Chunk 02 (midpoint):** the latency measurement is in hand — decide whether
  `matching` ships exact or approximate, on evidence rather than on the fixture.
- **After Chunk 03 (cumulative):** full-bundle review. Then, and only then, relaunch the
  peer acceptance session (#25) and brief it on #16 and #17 against a build it can
  confirm by `build.commit` rather than by fingerprinting a refusal string — the
  fingerprint route is recorded as already decayed once.
