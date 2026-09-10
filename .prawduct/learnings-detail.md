# Learnings — evidence

<!-- The bodies behind `learnings.md`'s rule index. One `## ` heading here per rule there,
     spelled the same way. The index is what gets read; this is what gets checked. -->

## Measure what the client delivers, not what the server sends

**Instances:**

- *2026-09-10, the MCP primer.* `mcp.py` served 6,673 characters of instructions with a table of
  warning kinds and the rule that prevents "no payments found" on a mortgage; two tests held the
  string against the vocabulary and both were green. Claude Code's system prompt showed
  `… [truncated]` after 2,045 characters — the reviewer measured it from the coordinator's own
  context. The fix was a 1,693-character primer whose second line names the resources that carry the
  rest, and a test that holds the union of primer and resources. Reviews:
  `artifacts/reviews-2026-09-09/review-mcp.md` finding 1.

## Write a guard from the failure's point of view, not the fix's

**Instances (2026-09-09, `production-blocker-findings`) — three more, each failing differently, and
none caught by reading the test:**

- *The count that was right and the number that was wrong.* A guard for "`coverage_report` derives
  its calendar day once" counted clock reads and asserted ONE. It failed at two, because the
  envelope's `as_of` is a legitimate second read of a **different fact**. Two is correct; the defect
  made three. Count what is actually there before asserting a number.
- *The relative constant that can never collide.* A guard for "the derivation version was bumped"
  stamped its fixture rows at `DERIVATION_VERSION - 1`. That moves with the constant, so the fixture
  and the build can never carry the same version — it passed with the bump reverted. Pin the
  historical value and say in a comment why it is not written relatively.
- *The frozen input that sees nothing.* Both of the above would also have passed against a stopped
  clock, because two reads of a stopped clock agree. If the defect is "this is computed twice", the
  input must MOVE between reads or the test proves nothing.

**The tell, in all three: the assertion was written after the fix, describing what the code now
does.** The question that catches them is *name the change that would flip this test*, asked BEFORE
writing the assertion — and then actually applying that change by hand. The go-red harness proves an
anchor still MATCHES; it does not prove the mutation still bites.

## Reversing a ratified decision is a sweep, not an edit

*2026-09-09.* Two consecutive Critic rounds on one chunk found the same class — prose still asserting
the design the amendment had just replaced. Round one found `data-model.md` arguing against the
ratified design. Round two found it surviving in two files round one had not cited, including the
build plan, where it was the new column's NAME. Delegates read the plan for their chunk spec, so the
rejected spelling would have reached an isolated worktree unopposed.

Cost when skipped: a review round, or three worktrees built on a document that contradicts its own
amendment.

## A documented remedy is a claim and is asserted like one

*2026-09-09.* The upgrade procedure prescribed `bankmachine store rebuild` to close migration 004's
window on a connection that never syncs again — the connection whose absent accounts matter most. On
that exact store it rolled back: replaying the archive moved the content digest, and
`DERIVATION_VERSION` had not been bumped, so `change_was_expected` was false.
`derivation.py`'s own docstring names "a newly-populated column" as the trigger for the bump.

The existing rebuild tests could not catch it — they replay through the same deriver that produced
their rows, so their digest cannot move. The case that catches it stamps rows at a fixed historical
version and clears the column, which is the only shape a real upgraded store has.

Cost when skipped: the operator follows the documentation into a rollback and an error blaming
deriver impurity or a pruned archive. A remedy that fails is worse than none — it spends their trust
on the way to failing.

## Recording a policy is not ratifying one

*2026-09-09.* An owner stated a delegate verification ceiling for one dispatch. It was written
straight into `project-preferences.md` as a binding norm — without the norm-registry row or the
doctor-plus-owner ratification that file's own text requires two lines away. The ceiling belonged in
the build plan's frontmatter, which is this repo's own precedent.

Cost when skipped: a norm nobody ratified binds every future cycle, and the tell — amending a norm to
match a decision you just took — is the one the Critic routes to BLOCKING.


## Removing a value from every literal does not remove it from what those literals interpolate

*2026-09-09, `_unservable_remedy`.* A refusal sentence carried the absolute datastore path — which
on a personal machine contains the operator's account name — onto the MCP wire, where nothing else
carries it. The path was removed from **every literal**, a docstring was written stating "no branch
carries the datastore PATH" as a rule, and a guard was added asserting it.

The path was still on the wire. The fallback branch interpolated `status.problem`, and for the
unreadable state that string is `str(exc)` from the store layer, whose exceptions carry absolute
paths *by design* (`could not probe the writer lock at <path>`). Four branches were cleaned, one
built its sentence from someone else's, and the boundary performs no redaction.

**The guard is why it survived a round.** It ran against one fixture — a schema-2 store — which
takes a branch that interpolates nothing. It asserted the rule on the one state that could not
break it. The sibling enum-driven test *did* build the leaking state, and asserted only that its
remedy was not a duplicate of another's.

Two things generalize. **A rule about output is not checked by testing the branch you were looking
at**; drive it from the set of states the code declares, which is the same fix as the entry below.
And **the fallback branch is where this hides**, because it looks too generic to be worth checking
and is the only one whose text comes from elsewhere.

## A carve-out reaches every state that shares its return type

*2026-09-09, `query._readable` (#58).* `connection.inspect` distinguishes five unhealthy states.
`_readable` returned `str | None` for all of them. AC-ARCH.3 carves out one — *"the MCP server
starts successfully when the datastore is empty or missing, and reports that state through
`get_pipeline_health` rather than crashing"* — and because every state arrived as the same string,
the carve-out written for a MISSING store governed a POPULATED one this build could not serve.

Measured cost: a store holding 14 accounts and 388 transactions answered every tool with
`isError: false`, `rows: []` and every coverage figure zero. An agent that does not parse `warnings`
reported the household owned nothing. Three acceptance rounds missed it because all three probed a
store the build could serve — the only state in which it is invisible.

Every individual piece was correct. `inspect` reported truthfully, the carve-out was real, the
warning was accurate. **The defect was one return type erasing a distinction the layer below had
already made.** The fix was not new logic: it was making `inspect` say WHICH state it found, so the
branch is on a declared value rather than on prose.

🔴 **The same collapse reappeared inside the fix.** `version != SUPPORTED_SCHEMA_VERSION` is true in
both directions, and one remedy answered both — telling an operator whose store is AHEAD of the
build to run forward-only migrations that apply nothing. Caught by the Critic, not by me, in the
commit that existed to fix exactly this shape. Fixing a class of bug does not confer immunity to it
while you work.
