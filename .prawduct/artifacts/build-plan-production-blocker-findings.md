---
artifact: build-plan
version: 1
scope: production-blocker-findings
branch: feat/production-blocker-findings
partition: |
  delegated, three ways, in isolated worktrees the coordinator creates. The partition is by ITEM
  this time rather than by requirement block, because the three items are genuinely independent in
  subject matter -- a lifecycle observation, a contract-completeness sweep, and a discovery that
  writes no code. What is NOT independent is `api-contract.md`, which C1's norm work and C2's field
  descriptions would both edit, and `query.py`, which C1 edits and C3 reads. Chunk 00 removes the
  first collision by landing every norm and vocabulary edit in advance, so C2 is left owning the
  field-description sections alone; the second is a read/write pair rather than a write/write pair
  and is handled by naming what C3 may assume.
  🔴 Two requirements cross the map and are NOT delegated, named here so neither can be read as
  dropped: (a) any field chunk 01 adds to the wire must be described in `api-contract.md` and must
  NOT be added to `UNDOCUMENTED_AT_FREEZE`, which chunk 02 is emptying -- the two chunks would each
  do half and neither would own the seam; (b) the `_stranded_holds` narrating comment, whose backlog
  carrier is #53, has no code chunk here because #53 is discovery-only. Chunk 00 deletes it.
critic_mode: cumulative-final
delegate_verification: |
  a delegate runs ONLY the named test files listed under its own chunk, and nothing wider. Three
  things are the coordinator's alone and no delegate may run them: the full suite (`uv run pytest`
  with no path), `tests/preferences/verify_norms_go_red.py` (160 cases, ~10 minutes, and it returns
  1 on a survivor -- piping it anywhere reports the pipe's exit code), and anything under
  `-m sandbox`, which makes live calls with real credentials and can fail for reasons that are not
  the delegate's change. Stated by the owner for this dispatch on 2026-09-09.
  🔴 It is a COST BOUND, not a rigor discount, and what it prevents fails silently: a contended box
  does not re-queue a dead test worker and does not fail the run, so several agents running the
  whole suite at once produce greens nobody can attribute. The delegate proves its own change; the
  coordinator owns the combined run, and owning it is what makes the ceiling safe.
depends_on:
  - artifact: build-plan-production-blockers
    file_path: .prawduct/artifacts/build-plan-production-blockers.md
  - artifact: discovery-account-lifecycle
    file_path: .prawduct/artifacts/discovery-account-lifecycle.md
  - artifact: api-contract
    file_path: .prawduct/artifacts/api-contract.md
  - artifact: data-model
    file_path: .prawduct/artifacts/data-model.md
governed_by:
  - artifact: api-contract
    dispositions:
      - "the MCP surface is read-only, no mutation tools → conforms; no chunk adds a tool, a parameter or a write path. C1 adds a stored column written by the SYNC path, which is the CLI's, not the MCP surface's"
      - "🔴 every response carries a freshness stamp, and incompleteness rides the success path as a warning → AMENDMENT PROPOSED, and it is chunk 00's. C1's empty-roster case is a NEW kind of incompleteness -- a roster observed successfully and found empty -- and the vocabulary is closed, so the kind is born in chunk 00 before any delegate can invent one. Landing it in advance is the one move that made the previous cycle's three-way parallelism conflict-free"
      - "🔴 a tool's boundary is drawn where the answer shape changes → conforms, and it constrains C1 rather than merely permitting it: every row field C1 adds is REQUIRED and nullable, never optional. `mcp._refuse_optional_row_fields` enforces this inside `_tool_definitions()`, so a delegate cannot route around it -- but the guard fires at registration, not at review, so the acceptance line names it"
      - "🔴 a stored balance is reported with its lifecycle, and no total over balances states no treatment of non-active accounts → conforms, and C1 makes it MORE true rather than departing: an account at a one-account connection that today reports `active` forever is exactly a stored balance reported with the wrong lifecycle. The flagged magnitude's population changes as a result, which is the norm working. C1 owes a go-red case proving the newly-absent account reaches the magnitude"
      - "the CLI's three-way exit code → inapplicable; no chunk changes a CLI exit path"
  - artifact: data-model
    dispositions:
      - "a migration's DDL is frozen once written → conforms; C1 adds migration 004 and does not edit 003, which shipped eight commits ago"
      - "🔴 calendar dates and UTC instants are distinct types and never mix → THIS IS THE DECISION CHUNK 00 OWES, not a check to tick. `accounts.last_seen_date` is a calendar date, and `roster_observed_at` -- the obvious spelling -- names an instant. A column whose name and type disagree is how this norm gets broken quietly. RULED in chunk 00: the column is `connections.roster_observed_date`, a CALENDAR DATE, because its only use is a comparison against `last_seen_date` and a comparison across the two types is a defect rather than a conversion. A delegate must not revisit it"
      - "a source value is never overwritten in place → conforms. `connections.roster_observed_date` is rewritten on every sync, which reads like a departure and is not: the norm protects a value the SOURCE reported from being clobbered by local interpretation. This is our own record of when we looked, which is not a source value at all -- and no aggregator field is overwritten to store it"
      - "every silver row carries exclusive provenance and its derivation version → recorded rather than assumed: `connections` is not one of the silver row types this norm enumerates, and `accounts` deliberately carries no `derivation_version_id` -- AC-12.6 rests on exactly that. C1 must not add one"
      - "all monetary values are integer minor units → inapplicable; no chunk here touches an amount"
      - "a transaction is never hard-deleted; removal is a soft delete → inapplicable to C1 and C2. C3 is a discovery ABOUT this norm's columns and must not propose weakening it"
      - "the daily balance and holdings series are append-only → inapplicable; nothing writes `balances_daily` or `holdings`"
      - "every stored amount is signed from the operator's point of view → inapplicable; no chunk changes a stored sign"
---

# Build Plan — The Three Findings the Blocker Cycle Produced

**Backlog items:** `brookstalley/bankmachine#51`, `#52`, `#53`
**Type:** mixed — one feature (#51), one documentation sweep (#52), one discovery (#53)
**Size:** large — a ratified-criterion amendment, a migration, a contract sweep, and a design discovery

These three are not new product ideas. Each is a finding the `production-blockers` cycle produced
and deliberately did not fix in place, and each was filed with the reason it was deferred. The plan
exists because two of them are at `stage: requirements` and one is not, so they are not three
instances of the same kind of work and must not be planned as if they were.

## Requirements Confidence

**Mixed, and the mix is the whole reason for chunk 00.**

- **#52 is `stage: ready` and needs nothing from this plan** — the guard already ships and holds the
  line; the work is the shrinking it exposed. High confidence.
- **#51 is `stage: requirements`, and the owner has ruled the OPTION but not written the CRITERION.**
  The ruling (2026-09-09): record the roster observation per connection, so that "the roster was
  observed, and this account was absent" is expressible at any N. AC-12.4 and AC-12.5 still say
  something else. **Chunk 00 writes the amendment; no delegate touches a criterion.**
- **#53 is `stage: requirements` and its design is deliberately unchosen.** The owner ruled that it
  goes to discovery with both shapes genuinely open, rather than being handed a decision to
  implement. Chunk 03 produces a discovery marked **proposed**; ratifying it is a separate cycle.

- *Problem:* one shipped answer is knowably wrong at a real shape (a one-account connection reports
  `active` forever with a frozen balance); one canonical document describes less than it publishes;
  and one five-state model is rebuilt by hand at six call sites and has already failed once.
- *Success:* the wrong answer is right at every N and the criterion says so in its own text; the
  contract's undocumented set is empty and the ratchet that held it is deleted; and the hold-state
  model has a written, priced recommendation nobody has yet built.
- *Out of scope:* the operator-declaration path (`#48`), the `sign_convention_unverified` rename
  (`#50`), VRF-005/VRF-006, and building #53's chosen shape. **None is descoped by silence — each is
  named here because it is adjacent enough to be picked up by accident.**

### The ruling this plan was unblocked by

🔴 **#51's option was ruled by the owner on 2026-09-09: record the observation per connection.** The
two options not taken are in the issue — putting shrink-to-zero on `get_pipeline_health`, and
accepting the N=1 blind spot in AC-12.5's text. The second was rejected on the ground that it leaves
the failure FR-9 removes alive inside FR-9; the first because it moves the signal to the
verification surface, so an agent reading `list_accounts` still sees `active` beside a frozen
balance — which is precisely what AC-12.1 exists to prevent.

🔴 **This ruling REVERSES a deliberate earlier choice, and that is what makes it a criterion
amendment rather than a bug fix.** AC-12.4 chose to derive the roster observation rather than store
it. The amendment must say why the earlier choice is being reversed, and the argument for it must
survive the reversal rather than being deleted with it.

## Status

- [x] **00 · The amendment, the column's type, the new warning kind, and one deletion** *(coordinator)*
- [x] **01 · The roster observation, recorded — #51** *(delegate, worktree)*
- [x] **02 · The contract describes everything it publishes — #52** *(delegate, worktree)*
- [x] **03 · Hold state: two shapes, priced — #53** *(delegate, worktree, discovery only)*
- [x] **04 · Integration** *(coordinator)*

**Context.** Chunk 00 closed 2026-09-09 at `66189da`, after two review rounds. Both found the same
class of defect — **prose still asserting the design the amendment had just replaced** — first in
`data-model.md`, then surviving in two files the first round had not cited. 🔴 **The lesson for
chunk 04: when a ratified decision is reversed, the reversal is a SWEEP, not an edit.** Grep the
repo for the rejected spelling and for the superseded claim; do not fix the file the finding names
and stop there. Suite green at 966.

---

## Chunk 00: The amendment, the column's type, the new warning kind, and one deletion

*(coordinator · lands in ONE commit BEFORE any delegate is dispatched)*

**This chunk exists because worktree-isolated delegates read HEAD.** Anything uncommitted is
invisible to them, and any surface two of them would edit at once is a merge conflict bought in
advance. The previous cycle spent one commit on exactly this and had zero conflicts across three
agents editing the same three files; that is the precedent being followed, not a general tidiness
argument.

Delivers:

1. **AC-12.4 and AC-12.5 amended in `docs/system-requirements.md`** to the ruled option, stating what
   each means at **any** N rather than at a scale where the argument happens to hold. AC-12.5's
   whole-roster clause is replaced, not annotated. The scale argument that justified it is preserved
   as the reasoning that was superseded, with the date and the ruling that superseded it.
2. **The empty-roster response gets a stated meaning** — `derive_accounts` receiving `"accounts": []`
   is today a successful observation of nothing, which is truthful when an operator de-selected every
   account and a lie when the feed broke. The criterion says which it is treated as; chunk 01
   implements what the criterion says.
3. 🔴 **The column's type and name, settled together.** `accounts.last_seen_date` is a calendar date;
   an instant named `_at` beside it either mixes the two types or misnames one. Chunk 00 decides
   which this is and names it accordingly, because `data-model.md`'s date/instant norm is broken
   quietly by exactly this and a delegate would settle it in passing.
4. **The new warning kind born in `envelope.REQUEST_SCOPED_KINDS`**, with its `_GUIDANCE` entry and
   its row in the server instructions — the vocabulary is closed, so an observed-and-empty roster
   has no way to be reported until this lands. Landing it here is what stops chunk 01 from inventing
   one and chunk 02 from documenting one that does not exist.
5. **The `_stranded_holds` narrating comment deleted** (`query.py`, the words "One caller did.").
   Its backlog carrier is `#53`, and `#53` is discovery-only in this plan, so it would otherwise have
   no code chunk anywhere and would be carried a third cycle. This is the cross-partition
   requirement (b) named in `partition:`.

**Done when:** AC-12.4 and AC-12.5 state the ruled behaviour at any N and carry the superseded
argument; the empty-roster meaning is stated; the column's type and name agree and the decision is
recorded against the date/instant norm; the new warning kind is in the closed vocabulary with
guidance and an instruction row; the narrating comment is gone; `tests/preferences` and
`tests/test_mcp*.py` are green; and **nothing a delegate needs is uncommitted.**

---

## Chunk 01: The roster observation, recorded (#51)

*(delegate · isolated worktree)*

Delivers the ruled option: **migration 004 adding `connections.roster_observed_date`** -- a CALENDAR
DATE, nullable, ruled in chunk 00 and not open for revisiting -- the sync path recording it
(including on an empty roster, per AC-12.5a), and `_account_lifecycle` reading the recorded
observation instead of deriving a maximum over the connection's own accounts.

🔴 **`roster_observed_at` is the spelling that was REJECTED.** It appears in this plan's
`governed_by` and in `discovery-account-lifecycle.md` only as the rejected option being named. The
column is `roster_observed_date`.

**The two narrating comments in `query.py` are rewritten by this chunk, not merely deleted** — the
one inside the `AccountLifecycle` docstring and the one inside `_account_lifecycle`'s. They are cited
by symbol rather than by line, because chunk 00 edits the same file and a line number does not
survive that. They describe behaviour this chunk changes. Only the *narration of a retraction* goes; the statement
of what the code now does stays, in the present tense, without the history.

🔴 **Also delivers the `roster_observed_empty` emitter, on BOTH surfaces AC-12.5a names.** Chunk 00
born the kind and wrote its guidance; nothing emits it yet. The Critic caught that the guidance sends
an agent to `get_pipeline_health`, which today reads `connections`, `sync_state` and `signs.measure`
only and would report such a connection healthy —

`[DECISION: AC-12.5a gains a get_pipeline_health clause, and chunk 01 gains the emitter there |
The alternative was deleting the referral from the guidance. That makes the text consistent by
making the product worse: an empty roster IS a connection-level anomaly, the old AC-12.5 explicitly
homed connection-level failures on that surface, and a broken feed that returns success is exactly
what a health check is for. The owner's ruling rejected get_pipeline_health as the SOLE home,
because a consumer reading list_accounts would never learn otherwise -- it did not reject the
surface. Making the guidance true costs one emitter; deleting it leaves a real gap and a health
check that calls a broken feed healthy | user can veto/override]`

**Owns:** `src/bankmachine/store/migrations/`, `src/bankmachine/store/schema.py`,
`src/bankmachine/connector/plaid/derivers.py` (`derive_accounts` only),
`src/bankmachine/query.py` (`AccountLifecycle`, `_account_lifecycle`, and `pipeline_health` only),
`tests/test_account_lifecycle.py`, `tests/connector/test_derivers.py`, `tests/store/test_schema.py`.

**Must not touch:** any `## Direction` section, `docs/system-requirements.md`, `envelope.py`,
`mcp.py`, `api-contract.md`, `_stranded_holds`, or any other function in `query.py` — **except the
narrow widening below.**

🔴 **Boundary widened for C1 on 2026-09-09, at the delegate's request, because the brief as written
was self-contradictory.** It said "emit it on the answer" and "do not touch any other function in
`query.py`" in the same breath, and every function that produces an answer is in the second clause.
A producer with no call site is exactly what this plan's `partition:` notes exist to prevent, and C1
stopped and asked rather than resolving it silently — which is the behaviour the naming is for.

**Granted:** the emitter term beside the existing `_not_active_caveat(...)` call in `extra_caveats=`
within `list_accounts`, `query_transactions` and `coverage_report`. **That term and nothing else in
those three functions.** Also `tests/preferences/verify_norms_go_red.py`, append-only to `CASES` —
the anchor is the delegate's deliverable, running the harness is the coordinator's, and a conflict
on that tail is expected and resolved by keeping every append.

`[DECISION: on get_pipeline_health the signal is a per-connection Caveat, not a new row field |
`mcp.py`'s `_output_schema` closes every level with `additionalProperties: False`, so a new row key
fails the registration guard and would require an `mcp.py` edit C1 is forbidden. `envelope.Caveat`
already declares `connection_id` and `institution`, and `signs.py` already builds that exact shape
on that surface -- so this is the established precedent AND it publishes no new wire field, which
independently discharges seam (a) from C1's side. Verified by the coordinator, not taken on the
delegate's report | user can veto/override]`

**Done when:** a single-account connection whose only account stops being listed reports it absent,
and **that assertion has been seen red** against the pre-change derivation; the empty-roster case
behaves as AC-12.5a states; `roster_observed_empty` is emitted on the answer AND visible on
`get_pipeline_health`, each with its own go-red case anchored on the CALL rather than on the
producer -- **on the health surface it is a row/finding about the connection, not a caveat on the
health envelope**, because the kind is declared request-scoped and fires when THIS request's scope
holds an account on such a connection, which is not what a health check's own envelope describes; every added row field is required-and-nullable; and a go-red case proves the newly-absent
account reaches AC-12.8's flagged magnitude.

---

## Chunk 02: The contract describes everything it publishes (#52)

*(delegate · isolated worktree)*

Delivers the shrinking the guard exposed: describe the **34** currently-undocumented published fields
in `api-contract.md`, deleting each name from `UNDOCUMENTED_AT_FREEZE` as its description lands,
until the set is empty and the constant — with the test guarding its staleness — is deleted and the
remaining assertion runs unconditionally.

🔴 **The item body says 41; the measured set is 34.** It fell during the previous cycle without
anyone working on it. Re-derive the count before starting rather than trusting either number.

**Owns:** `.prawduct/artifacts/api-contract.md` — **field-description sections only, never the
`## Direction` section** — and `tests/preferences/test_the_documented_wire_is_the_published_one.py`.

**Must not touch:** any norm, any source file, or `docs/connecting-an-mcp-client.md` (explicitly a
copy that may lag; the contract is the canonical document and the only one held to completeness).

🔴 **One deliverable is added to the item's own acceptance, and it is added out loud rather than
absorbed.** `#52` does not ask for it; `#47` needs it and cannot start without it. Describing 34
fields across `api-contract.md`'s row tables means DECIDING which tables are authoritative for row
shape, which column of them holds the field name, and how a nested block's fields are spelled --
the delegate makes that call whether or not anyone asks, and today it would be made in passing and
left unwritten. **Record it as an explicit extraction contract in `api-contract.md`.** The reason it
is worth a paragraph: `#47`'s remaining half must extract documented field names from these tables,
and unlike the delivered half it gets no free input from `outputSchema` -- the document backticks
tool names, warning kinds, column names and prose terms as well as fields, so a substring sweep is
unavailable. This is not scope creep onto `#52`; it is refusing to throw away a decision `#52` makes
anyway.

**Done when:** `UNDOCUMENTED_AT_FREEZE` is empty, the constant and its staleness test are deleted, the
remaining assertion is unconditional and passing, no name was removed without a description landing
for it, and the extraction contract above is written down where `#47` can build against it.

#### Chunk 02 as-built — met in full, and one measured residual it did not create

All four acceptance clauses hold, re-derived by the coordinator rather than accepted: the ratchet and
its staleness guard are gone from the tree, and a parser written independently from the extraction
contract's prose alone recovers **93 names from 16 tables**, set-equal to the published set. The
delegate also found by hand that `status`, `pending` and `rows` had passed the old guard on
*incidental backticks elsewhere in the document* and were undescribed regardless; all three now carry
real rows.

🔴 **The guard is weaker than "unconditional" makes it sound, and this is measured, not suspected.**
Its test is `f"\`{name}\`" not in contract` — a whole-document substring sweep. A name in backticks
anywhere at all counts as documented: in prose, in a `## Direction` norm, in the CLI section.
**76 of the 93 published names appear in backticks somewhere other than their own table row, so
destroying their entire description leaves this guard green. 17 are load-bearing.** The delegate's
seen-red evidence is honest and happens to have used one of the 17.

**This is not chunk 02's defect** — the sweep predates it and the chunk's brief was the shrinking. It
is recorded here because chunk 02 is what makes the fix cheap: the extraction contract now exists and
is validated, so the sweep can be replaced by a parser that reads only the authoritative tables. That
would take the guard from 17/93 to 93/93 load-bearing **and** is the same parser `#47`'s remaining
direction needs. **Routed to `#47`, not to chunk 04** — it is a scoped piece of work with its own
go-red, not an integration step, and rushing it at integration is how a guard gets written that looks
stronger than it is for a second time.

---

## Chunk 03: Hold state — two shapes, priced (#53)

*(delegate · isolated worktree · **discovery only, no code**)*

Delivers `new .prawduct/artifacts/discovery-hold-state.md`: the five states enumerated against the
**six actual call sites** rather than against the issue's description of them, both candidate shapes
priced against that evidence, and a recommendation with its reasoning.

**Both shapes stay genuinely open.** The owner ruled that this goes to discovery *because* #53 has
already failed once and the last cycle's lesson was that designing against a just-landed sample is
what produced the failure. A delegate that arrives with a preference and prices the other shape to
lose has done the opposite of the task.

The prior session's recorded opinion — that these conditions are aggregated rather than iterated, so
a per-row enum would move aggregation out of SQL — is **an input to be tested, not a conclusion to
be confirmed.** Check whether it is actually true at all six sites.

**Owns:** `.prawduct/artifacts/discovery-hold-state.md` (new). **Writes nothing else at all.**

**Done when:** all five states are enumerated with their real call sites, both shapes are priced
against that evidence, a recommendation is made with its reasoning, and the artifact is marked
**proposed** — it is not ratified by having been written, and no code implements it in this cycle.

---

## Chunk 04: Integration

*(coordinator)*

Merges the three worktrees, then pays the cross-partition requirements that no delegate owns:

- 🔴 **(c) `coverage_report` reads the clock TWICE in one call, and its own comment forbids it.**
  `query.py` calls `_stranded_holds(conn, today=calendar_date(now_utc().date()))`, then 32 lines
  later computes `today = calendar_date(now_utc().date())` again under a 🔴 comment reading *"One
  `today` for every row. Reading the clock per account would let a report straddle midnight and hand
  back rows measured against two different days, which is a difference nobody could explain from the
  payload."* A call straddling midnight measures the stranded holds against one day and the rest of
  the report against another — precisely what the comment says must not happen. **Found by C3 while
  pricing the shapes, confirmed by the coordinator against the file.** It lands HERE and not in
  chunk 01 for one reason: C1 is editing that same function under a narrow widening right now, so
  fixing it before C1 merges buys a conflict for nothing. Riding chunk 04's commit costs no extra
  review round; leaving it in a delegate's report would be a drop, not a deferral. **Hoist one
  `today` above the `_stranded_holds` call and pass it; do not add a second parameter.**

- 🔴 **(a) The seam between 01 and 02.** Any field chunk 01 added to the wire must be described in
  `api-contract.md` and must **not** be in `UNDOCUMENTED_AT_FREEZE` — chunk 02 is emptying that set
  and chunk 01 does not know it exists. Each chunk would do half of this correctly and the result
  would be a newly-published, undocumented field with a guard that no longer looks for it. **This is
  the shape that dropped two requirements last cycle, and naming it is the only reason they were
  caught.** It gets its own acceptance line below.
- The full suite, `verify_norms_go_red.py` in full (~10 min; **never piped to `tail`** — that returns
  tail's exit code, not its), and `/prawduct:critic cumulative`.

#### Chunk 04 as-built — what integration actually caught

**Seam (a): CLEAN, and re-derived rather than accepted.** C1 reported adding no wire field; the
published-name walk returns **93** both before and after its merge, and the now-unconditional
contract guard passes. The seam was still worth naming — the check is seconds and the failure it
catches is a published field guarded by nothing.

**Seam (c): fixed, and it needed a guard the fix's own point of view would not have produced.**
`coverage_report` now derives its calendar day once, above its first reader. 🔴 The first guard I
wrote counted clock reads and asserted ONE — and failed at two, because `_answer`'s `as_of` is a
legitimate second read of a different fact. **Two is correct; the defect made three**, and the count
is the discriminator. A frozen clock cannot see any of this: two reads of a stopped clock agree, so
the clock in the guard advances a day per read. Seen red by reintroducing the exact defect.

🔴 **One pre-existing go-red anchor drifted, caught in two seconds by the harness guard rather than
in ten minutes by the harness.** C1's health emitter reflowed `extra_caveats=signs.caveats(conn,
measured=measured),` onto its own line, so a case aimed at that whole argument stopped matching. Re-
aimed **and re-proved red by hand** — a matching anchor only shows the text is findable, never that
the mutation still bites.

**Done when:** the suite is green and recorded; every go-red case is RED; requirements (a) and (c)
are each verified by re-derivation rather than by a delegate's report; chunk 03's discovery is marked proposed
and its ratification is filed rather than assumed; and the cumulative review has no unresolved
blocking findings.

## What this plan does NOT close

- **`#53` does not close.** It ends this cycle with a written recommendation and nothing built.
- **`#47` does not close.** Its published→documented half shipped last cycle; the documented→wire
  half is unguarded and its Expected is being narrowed to that. Chunk 02 works the same surface and
  will surface how hard the extraction is, but it is not scoped to do it.
- **`#48` and `#50` are untouched.**
