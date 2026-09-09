# Learnings

Accumulated wisdom from building this product. Entries use "When X, do Y because Z" format, and each rule carries its instances inline — they are what a reader pattern-matches their own case against.

<!-- prawduct:descent-obligation — the statement below is the HOME of the
     descent rule; `/prawduct:learnings` points here rather than restating
     it. Reword the prose freely; keep this marker, above the first rule. -->

**Reading a rule is not applying it.** The failure mode of a learnings file is not absence, it is assent: a rule arrives at the right moment, is read, is agreed with, and changes nothing, because nothing made you recognize the case in hand as an instance of it. So for any rule you read here, name the decision you are about to make and say what the rule changes about it — or say that it does not apply, which is also an answer.

---

## Review coverage

**When work should be reviewable, land it on a feature branch and review it BEFORE pushing to the
base branch — because committing straight to `develop` collapses the review interval to nothing,
and the coverage gate then reports "satisfied" on an empty span.**

The gate resolves its base with `resolve-base`, which returns `origin/develop`. Push a commit to
`develop` and base *is* HEAD: `merge-base…HEAD` is empty, `critic-begin` refuses both `final` and
`cumulative` with "empty diff", and `check-cumulative-critic` answers `satisfied … (empty span
(base tree == HEAD tree), 0 unresolved blocking)`.

**Why this one bites:** that output is indistinguishable from a review that ran and found nothing.
Nothing in it says "this work was never looked at". The failure is silent and it reads as success,
which is the same shape as every other defect this project has been burned by — a check whose only
bad-news channel is the absence of output.

**Instances:**

- *2026-09-05, the `bankmachine` rename.* Eight files including `docs/system-requirements.md` and
  five governance records, committed straight to `develop` and pushed, then `/prawduct:critic` ran
  and found no interval. Verified instead by four mechanical checks (grep for the stale name with
  each surviving occurrence classified, YAML parse, dangling-path sweep, 22-case self-test) —
  adequate for a mechanical rename, and not a substitute for review on anything with logic in it.
- *Contrast, same day:* `repo-sanitization` used `feature/repo-sanitization` and got two real
  reviews, the first of which returned 3 blocking findings including one that had the guard
  checking the wrong thing entirely. That work was not more dangerous than the rename in kind — it
  was just on a branch where the Critic could see it.

**How to apply:** if the answer to "would I want a second pair of eyes on this?" is anything but a
flat no, branch first. `.prawduct/` bookkeeping — change-log, learnings, reflections, backlog,
project-state — is explicitly exempt: the Critic names those as free to write at any time because
they do not move coverage.

---

## Guarantees by construction

**When you state a guarantee, ask what else can reach the mechanism — not just whether the
mechanism works in the case you had in mind. A guarantee defined by an enumeration decays, and one
resting on a reversible flag was never a guarantee; both look correct at the moment you write
them, and both fail silently later.**

The tell is grammatical. "The following commands take the lock" and "the reader sets `query_only`"
both describe a mechanism *doing the right thing in a case*. "Every writable handle comes from the
factory that takes the lock" and "the handle is opened `mode=ro`" describe a property that has no
case to fall outside of. Prefer the second form even when the first is true today.

**Instances:**

- *2026-09-05, the architecture artifact's first two norms.* The writer norm listed the commands
  that take the exclusive lock, and the list was already wrong on the day it was written — it
  omitted `store init`, which creates the datastore, and `store rebuild`, which rewrites every
  normalized table. The reader norm rested on `PRAGMA query_only=ON`, which I had measured refusing
  a write; re-probing on the Critic's finding showed `PRAGMA query_only=OFF` restores writes, and
  the product ships `sync shell` — an operator SQL prompt — as the surface that can type it. Both
  were fixed by changing the mechanism rather than lengthening or annotating it: one writer factory,
  and `mode=ro` at the file handle where SQL cannot reach.
- *Same day, the corollary about probes.* I had recorded `mode=ro` as rejected on a **falsified
  premise**, having probed it and seen it work. Re-probing the exact adjacent case — hot WAL, `-shm`
  deleted, directory unwritable — reproduced the original failure. The premise was neither true nor
  false but *unscoped*, and my first probe had missed it only because the crashed writer left its
  `-shm` behind. **A probe that confirms what you expected is the one to distrust**: the failing
  case is usually one variable away from the one you set up.
- *2026-09-06, the rebuild's list of tables to empty.* "A table is rebuildable if it has a
  `raw_response_id` column" reads like a property derived from the schema rather than an enumeration
  — which is exactly what makes it dangerous, because it is an enumeration wearing a predicate's
  clothes. `raw_responses` has a `raw_response_id`: its own primary key. The rebuild would have
  deleted the entire archive and then replayed it, finding nothing. The fix was not an exception for
  that one table but the predicate the sentence had always meant: a table is rebuildable when it
  holds a foreign key *pointing at* a raw response. **The tell was that the rule matched on a name
  where it meant a relationship** — the same tell as a rule that matches on a filename where it means
  a role, which the sole-constructor norm hit in the same chunk: `engine.connect()` is a checkout,
  not a construction, and the rule now turns on whether the call carries connection parameters
  rather than on which file it sits in.

- *2026-09-07, the go-red harness judging its own results.* `verify_norms_go_red.py` decided a
  mutation had been caught by testing `pytest exited non-zero` — an enumeration standing in for
  "the named test failed", because pytest exits non-zero on a **collection error** too. A mutation
  that did not parse therefore printed RED without running anything, and the case passed forever.
  Two such cases existed; only one was found by review, and the other had been green since it was
  written. The fix was not to correct the two mutations but to `ast.parse` every mutation before
  running it, so a non-parsing one is reported INVALID and counted as a survivor. 🔴 **The defect
  was in the mechanism whose entire job is catching this class**, which is the strongest version of
  this rule: the check you trust most is the one nothing is checking. Then, one layer down, both
  repaired cases were *still* green — one targeted lines the test's `os._exit` never reaches, and
  the other rested on an assertion that could not distinguish the two values it named, because one
  string embedded the other. **A check that cannot fail hides every problem in its blast radius,
  not one, and they surface a layer at a time.**

- *2026-09-08, seven in one work cycle.* Across build steps 3 and 4 I wrote seven checks that did
  not exercise what they named: a fixture whose bad entry sat where the search never reached it; a
  race test that patched the very check it was testing; a credential-absence assertion reading a
  `caplog` that collected nothing; a go-red mutation that did not parse, so pytest failed at
  COLLECTION and printed RED forever; a cursor-atomicity claim that held **positionally**, so
  turning the transaction's `ROLLBACK` into a `COMMIT` left the suite green; a test whose docstring
  said *"the server refused to start"* that never called the function which refused; and a mutation
  aimed one line away from the branch its test reads. Every one passed on first run. 🔴 **The
  through-line is not carelessness about behaviour — it is that when writing a check, attention goes
  to the behaviour wanted and not to the path the check traverses to reach it.** Two of them were
  guarding requirements I had implemented backwards, which is exactly when a check is least likely
  to be examined and most needed.

- *2026-09-08, the capabilities union.* Two assertions written the same hour as a commit message
  boasting about breaking checks to watch them fail. `assert missing in str(caught.value)` looked
  like it pinned which product list a refusal names — but **`products` is a SUBSTRING of
  `available_products`**, so the products case passed even when the message named the other one.
  `assert "gap" in out` passed identically before and after the `a 8-day gap` → `a gap of N days`
  reword it was written to pin. 🔴 **Substring containment is the specific trap**: when one valid
  value contains another as text, `in` cannot tell them apart — assert the whole phrase, or match a
  pattern. Both found by the Critic, neither by a green suite.
- *2026-09-08, the same change.* **A refactor that changes control flow can orphan an existing test
  without touching it.** Reading two fields in a loop meant a body missing the first never reached
  the second's `isinstance` guard, so the case covering a non-list `available_products` kept passing
  while covering nothing. After changing control flow, ask which existing tests now short-circuit —
  mutation testing is blind to it, because reverting removes the damage along with the fix.
- *2026-09-08, the capability fixture.* **A fixture drawn from one instance of a shape cannot
  discriminate a rule about the shape.** Every capability fixture used `ins_109508`, where
  investments happens to sit in `available_products` — so the right read and the wrong read agreed,
  and 552 tests plus a live sandbox test all passed against a criterion that was exactly inverted. A
  second institution found it in one enrollment.

- *2026-09-08, the window resolver, and the sharpest version of this yet.* I wrote a hand-built
  boundary matrix for a clamp — inside coverage, before it, after today, both, unbounded, no
  overlap, empty store, on the edge — then ran nine mutations against it and **every mutation was
  caught.** By the rule as written, the checks were validated. Three real bugs were still live, and
  each was a *plausible sentence the payload beside it contradicted*: a future window whose warning
  read "this answer covers through 2026-09-08" while `effective` was null; `{since: 2027-01-01}`
  with no `until`, an ordinary shape, producing a null window and **no warning at all**; and its
  mirror, `{until: "2020-01-01"}` with no `since`. I found the first by re-reading the diff, the
  second by probing reachable inputs by hand, and **the third was found in under a second by a
  property test asserting the invariant** — "a window that covers nothing always says why" — which
  I had only written *because* the first two had already escaped.
  🔴 **Mutation testing validates the checks you wrote; it is structurally blind to the case you
  did not think to write.** And a hand-built matrix is written by the same mind, at the same
  sitting, from the same mental model as the code — so it reproduces the code's blind spot rather
  than crossing it. Both bound-checks were keyed on one bound because I was picturing one bound.
  **The escape is to assert the INVARIANT rather than enumerate the instances**: `if
  window.covers_nothing: assert window.caveats` cannot be written from a mental model of which
  windows are empty, so it does not inherit one. Note the shape of the failure — the enumeration
  was in the *test matrix*, not in the code, which is the same decay this rule names one layer up.

- *2026-09-08, the truncation guard, and the sharpest form of this rule yet — because the check that
  failed was **mine**, and it failed silently.* The guard compared the WHERE clause of the row query
  against the WHERE clause of the count, to catch the two being built from different predicates. It
  extracted each by `statement.partition(" WHERE ")`. SQLAlchemy compiles with newlines
  (`count_1 \nFROM transactions \nWHERE ...`), so the separator never matched, the extraction
  returned `""` for **both** sides, and `"" == ""` agreed with everything. It passed its first run
  and it passed every mutation aimed at it; only the *survival* of a mutation another test should
  not have caught exposed it.
  🔴 **A test that derives what it compares — parsing, extracting, filtering, transforming — has a
  second point of failure, and that one fails OPEN.** A loose comparison is the known trap; this is
  its quieter sibling, where the comparison is strict equality and the *operands* are empty. Nothing
  distinguishes "these two agree" from "I found neither". The defense is one line: **every
  extraction asserts it found what it was looking for**, before anything is compared. The repo
  already had this right one function away — the helper in the MCP tests that derives the
  request-scoped warning kinds from the vocabulary asserts the derived set is non-empty before using
  it, for exactly this reason — and I did not carry it to the new extraction I wrote beside it.
- *2026-09-08, the cursor's forged-payload fixtures, and the same rule one layer further out.* Ten
  mutations were aimed at the new refusal paths and two SURVIVED — removing the bool guard that stops
  JSON `true` becoming transaction id 1, and removing the scheme-tag check. Neither assertion was
  wrong. **The fixtures were**: each forged cursor carried a placeholder fingerprint, so an EARLIER
  guard refused every one of them before the branch under test was reached, and ten cases all passed
  by proving the same one thing. Fixed by giving the fixtures the real fingerprint, which is the only
  value that lets each case be wrong in exactly one way.
  🔴 **The trap here is not a loose assertion or a derived operand — it is a fixture that cannot
  REACH the subject**, because some other guard between the entry point and the branch fires first.
  A parametrized list makes it worse, not better: ten green cases read as ten checks. The tell is
  that the input is invalid in more than one way at once. **When a case exists to exercise one
  rejection path, make it valid in every respect but that one** — and confirm it by breaking that
  path alone and watching this case, not the list, go red.

  *(Two further defects in the same chunk were found by neither the matrix nor 22 mutations, but by
  reading reachable inputs by hand: a caveat advising a caller to raise `limit` past a cap it had
  already hit, and a windowed answer that dropped a coverage key only when the datastore was
  unreadable. Both reconfirm the rule above rather than extending it.)*

**How to apply:** before recording a guarantee, name the surface that could violate it and check
that surface exists in the product. **And for every check you write, break the thing it names and
watch it fail** — a green first run is the moment to distrust, not the moment to move on. Where a
mutation harness exists, point one at the branch the assertion actually reads; where it does not,
edit the source, run the test, and put the source back. If the answer is "a future command someone forgets to add to
the list" or "any SQL that reaches this handle", the guarantee needs a different mechanism, not a
firmer sentence. And when a rule matches on a *name* — a column name, a filename, a function name —
ask what relationship the name is standing in for, and match on that instead. **When the thing
under test has a stateable invariant, write the invariant as well as the cases** — the cases check
your model, and only the invariant checks the model itself; where a property-based library is
available, that is what it is for. **And when a check derives its own operands, make the derivation
assert it succeeded** — an extraction that can quietly return nothing turns a strict comparison into
a tautology, and the green it produces is indistinguishable from the green you wanted. Related:
[[review-coverage]] — both are the same family, a check whose bad news never arrives.

---

## Two descriptions, compared

**When one thing must mirror another — table metadata against the DDL that built it, a test's
expected value against the code that computes it, a constant against the document that quotes it —
write both independently and have something compare them. Generating one from the other, or reusing
the same expression on both sides, removes the disagreement; the disagreement was the only thing
that could ever have told you they had drifted.**

This is the constructive half of [[guarantees-by-construction]]. That rule says a guarantee needs a
mechanism no case can fall outside of. This one says how you find out when you were wrong anyway:
keep a second, independently-derived account of the same fact, and let a test read both.

**Instances:**

- *2026-09-06, the core schema.* `store/schema.py` (SQLAlchemy Core metadata) and
  `store/migrations/core_schema.py` (frozen DDL) describe the same thirteen tables and neither is
  generated from the other. Every run compares them column for column. The obvious alternative —
  emitting the migration from the metadata — would have been fewer lines and would have made every
  future edit to a column *silently correct on both sides*, with the file on disk agreeing with
  whatever the code currently believes. Migrations are forward-only precisely because that
  agreement is a lie for any datastore that already ran the old one.
- *Same day, a property test's oracle.* `test_any_two_place_decimal_converts_exactly` checked
  `from_decimal_string` against `Decimal.scaleb`, and hypothesis failed it on
  `100000000000000000000000000.01`. **The code was right and the oracle was wrong**:
  `Decimal.scaleb` rounds at the default 28-digit context, while the implementation scales the
  digit tuple and is exact. Had the test reused the implementation's own approach it would have
  agreed with itself forever and taught nothing.

**How to apply:** when you catch yourself about to derive the checker from the checked, ask what
observation the shortcut is making impossible. If the answer is "the two disagreeing", write it
twice. And when an independent oracle disagrees with your code, find out which one is wrong before
assuming — a second implementation is evidence, not a verdict.

## Blocked is a claim, and it is usually wider than the truth

**Before recording a step as blocked on a credential, a device or an environment, ask which part of
it can be checked without one. The failure paths of an external system almost never need valid
credentials — invalid ones reach the same server and come back with the real error shape.**

"Blocked on X" feels like a fact because X is genuinely absent. What makes it a claim is the scope:
it is asserted over the whole step, when what X actually gates is one half of it. The cost is
asymmetric and quiet — an unnecessary block defers work that would have *changed the design*, and
nothing ever reports that it could have run.

**Instances:**

- *2026-09-06, the aggregator client's first chunk.* The plan's `verify-api` step said "read the
  SDK's source, then probe sandbox", and with no sandbox credentials on the machine I filed the
  whole probe as blocked and built against source reading alone. Three things were reachable the
  entire time, and all three were reachable with *deliberately invalid* credentials or none:
  whether an unreachable host escapes the SDK unwrapped (it does — a raw `urllib3.MaxRetryError`,
  which would have printed a traceback at every offline operator); the real error-body shape; and
  that the SDK decodes an error body to `str` before re-raising. I only probed the second after a
  Critic finding forced it open, and it turned an unusable message — every rejection reading
  `400: Bad Request` — into one that names its cause. **The probe that would have changed the code
  was free, and I did not look for it because I had already written down that it was blocked.**
- *The shape to copy.* What remained genuinely blocked was narrow and worth stating narrowly: the
  *success* response shape, which is what derivers get written against. Splitting the plan's
  acceptance criteria into the half that could be met and the half that could not is what made the
  remainder a gate instead of a mood.

---

## The norm harness sabotages the working tree, so nothing else may read it

**When `verify_norms_go_red.py` is running, do not run the suite, dispatch a review, or commit —
it edits source files in place to prove each assertion goes red, so for the length of the run the
working tree contains code nobody wrote. Anything that reads the tree during that window reads
sabotage and reports it as fact.**

The harness is the mechanism behind [[two-descriptions-compared]] — it removes a mechanism and
checks that a named test notices. Removing the mechanism means *writing the broken version to
disk*, running one test, and putting it back — once per entry in its `CASES` table. The tree
is correct before and after and wrong in between, which is the shape that makes it invisible:
every check of the file afterwards agrees with what you meant.

**Instances:**

- *2026-09-07, Chunk 01 of enrollment.* A Critic review was dispatched while the harness ran. Its
  first two dispatches were unreviewable and it said so: one manifest caught `store/rebuild.py`
  holding the harness's sabotage — `_points_at` returning `True` unconditionally — and
  `connector/plaid/client.py` transiently holding `finished = None`, which was the *mutation* of a
  line I had just written rather than the line. **A review of either snapshot would have produced
  confident, well-argued findings about code that does not exist in any commit.** The Critic
  retried until the tree was stable, which is the only reason this was caught rather than acted on.

**How to apply:** treat the harness as an exclusive lock on the working tree. Run it alone, wait
for "all N norm breaks were caught", and only then run the suite, dispatch a review, or stage a
commit. The cost of getting this wrong is not a failed run — a failed run would be fine, because
it announces itself. It is a *successful* run of something else against source that was briefly a
lie, and that result looks exactly like a real one. Related: [[review-coverage]] and
[[guarantees-by-construction]] — the same family again, a check whose bad news never arrives.

---

## `ruff check` clean says nothing about `ruff format`

**When you verify formatting, run `ruff format --check` — because `ruff check` and the formatter
are two different halves, and this repo's norm (`project-preferences.md`: `Formatting: ruff
format`) is the half `ruff check` never reaches.**

The two get conflated because "ruff is clean" is how the result gets reported and remembered. It
is not a claim about layout at all: `ruff check` runs the lint rules in `[tool.ruff.lint]`, and
none of `E, F, I, N, UP, B, SIM` is the formatter. A file can be hand-wrapped into a shape the
formatter would rewrite and stay clean forever.

**A reformat can break the go-red harness, and that is not a reason to skip either one.**
`verify_norms_go_red.py` mutates by literal `str.replace`, so its anchors are exact source text,
including the line breaks the formatter owns. Reformatting a mutated file moves anchors out from
under it. The harness reports this as `SKIP … anchor no longer present` and counts the case as a
survivor rather than printing RED — which is the right shape, and is the only reason the coupling
is cheap. **Prefer an anchor that names a whole expression or keyword argument over one that spans
a formatter-chosen line break**: the former survives rewrapping, the latter is a hostage to it.

**Instances:**

- *2026-09-08, merging `feature/sync-v1`.* Seven files had drifted, five written on the branch,
  across two build steps that both reported "ruff clean" at every close — because both had run
  `ruff check` and neither had run `--check` on the formatter. Fixing the drift collapsed a
  wrapped conditional in `cli/enroll.py` onto one line and broke the anchor for AC-1.4's
  converging-re-run case; the harness caught it on the next run and the anchor was rewritten to
  name the keyword argument instead.

---

## Ask the running process what it is running

**When a claim is about which code a live process is executing, get the answer FROM THAT PROCESS —
because your inference from the repository is strictly weaker evidence, and it fails in exactly the
case that matters: when something has moved underneath you.**

A repository tells you what is on disk. A running process tells you what it loaded, which is a
different question whenever the two could have diverged — a server started before a merge, a client
holding a subprocess it launched at connect time, a worktree pinned by a path argument, an import
cached in a session that has not restarted.

**Author dates are not landed dates.** A rebase, a cherry-pick, or a `filter-branch` rewrite leaves
the author date untouched while the commit joins the branch much later. Reading the former as the
latter produces a confident, specific, wrong answer about ordering — and reflog or
`git log --format=%cd` on the branch is the thing that actually answers it.

**Best: make the surface report its own build.** As of `6532659` every MCP response carries
`build.commit`, captured once at process start, so the question is answered by one field on any
call before any testing begins. 🔴 **Prefer this over every technique below** — a fingerprint infers
the build from a value that happens to differ, and it silently stops working the moment that value
changes for an unrelated reason.

**Fallback, for a surface that does not self-report**: fingerprint it — call for a string or value
known to differ between the two candidate builds, and read which one comes back. It costs one call
and settles what an argument cannot. 🔴 **A recorded fingerprint decays.** The one written down for
this repo (`limit:9999` → "at most 1000") was falsified within a day by `8c92131` moving the
ceiling to 500, and a later reader following it would have matched against a string the build no
longer emits — reading the mismatch as evidence of a *newer* build than they had. If you must
record one, record what it discriminated and when, never as a standing check.

**Why this one bites:** the wrong answer is a *pass*. A stale build that still behaves correctly on
everything except the fix under test reports green, and green is what you were hoping for. The
verification harness acquires the same defect as the system under test, and nothing in the output
says so.

🔴 **The same question applies to the RULE, not just the code: "recorded" and "reachable" are
different claims.** A worktree sits at its own commit, so guidance committed to `develop` is
invisible to a session working from an older branch — and that session is often the one the rule was
written for. Before relying on a peer having read something, ask which commit their tree is at.
Where the answer is "not yet", the forward handoff is the carrier that crosses the gap, which is an
argument for a live rule living in both places rather than only in its home.

**Instances:**

- *2026-09-08, MCP acceptance round 4.* I told a peer testing session its MCP server was serving the
  merged build, deriving "the tree has not moved since you connected" from the merge commit's author
  date of 11:32. The previous day's `filter-branch` had split author from landed dates; the merge
  reached `develop` after the peer connected at ~10:51. The peer called `query_transactions` with
  `limit: 9999`, got the pre-merge ceiling of 1000 rather than the merged 500, and refused to run the
  verification half at all — correctly, since a clean pass would have been read as confirming a fix
  it could not have exercised. After the operator relaunched it, the same call returned 500 and the
  round proceeded.
- 🔴 *2026-09-08, the sharpest form: a stale tree generated work for a human out of a closed
  decision.* The testing session's briefing fired the governance advisory about `change-log.md`
  exceeding a 100KB ceiling, and it relayed that to the operator as a decision they needed to make.
  The operator had settled it hours earlier — `b48ff1d` raised the ceiling to 150KB and recorded why
  trimming was unavailable. The briefing read `oversized_file_threshold_kb: 100` from the worktree's
  own `project-state.yaml` while `develop` said `150`. Verified: `git show
  testing/current:.prawduct/project-state.yaml` gives 100, `develop` gives 150, and
  `testing/current` is 11 behind with 0 ahead.
  **Why this instance beats the inert one below: it fails OUTWARD, at a person, and it looks like
  diligence.** A session that cannot reach a rule is silent and harms only itself; a session
  reasoning from stale governance actively asks a human to re-decide something they closed, and the
  ask is indistinguishable from good practice. The tell is not in the question's wording — it is
  that the tree asking it was old.
- *2026-09-08, the rule about the rule.* The testing session pointed out that the amended guidance
  above was not reachable from its worktree at all: `testing/current` sits at `f6e81f2`, and both the
  original rule and its amendment landed on `develop` afterwards. Verified — `git show
  testing/current:.prawduct/learnings.md` matches it zero times. So "amended in `learnings.md`" and
  "the session it was written for can read it" were not the same statement, and the only carrier on
  that side was the forward handoff note.
- *Same session, standing hazard.* The MCP server is a subprocess the client launches, so it runs
  whatever existed at connect time, and `--directory` pins which checkout it serves regardless of
  where the client sits. Both are now in `README.md` under "Wire it to an MCP client", because the
  gap between "I changed the code" and "the thing under test changed" is invisible from the client.


---

## A green mutation is not a proof until you know the mutation landed

**When you prove a guard goes red by editing source and re-running it, ASSERT that the edit
applied before reading the result — because a `str.replace` whose anchor did not match produces
a green run that is indistinguishable from a guard that works.**

The failure is silent and it reads as success, which is the shape this project keeps getting
caught by. You set out to prove a test can fail, the test passes, and the passing run means
nothing happened at all rather than nothing was wrong.

The defence is one line: `assert new != original` before writing, and prefer an anchor naming a
whole statement or keyword argument over one spanning a formatter-chosen line break — the latter
is a hostage to the next reformat, which is why [[the-norm-harness-sabotages-the-working-tree]]
reports `SKIP … anchor no longer present` rather than counting the case as passed.

**Instances:**

- *2026-09-08, the warning-vocabulary guard.* Two mutations of `query.py` both reported
  "4 passed". Neither had landed — the anchors carried indentation the real source did not have.
  Caught only because a *4-passed* looked wrong for a run meant to go red; a less suspicious
  reading would have recorded the guard as proven. Re-run with asserted anchors, both arms failed
  naming `query.py:906` and the offending kind.
- *2026-09-08, chunk 07's go-red cases.* Same error, caught by the harness instead of by me: one
  of three new cases reported `SKIP … anchor no longer present` because the anchor guessed at a
  `logger = get_logger("query")` line that does not exist in that module. The harness's decision
  to report a missing anchor as a survivor rather than a pass is what made it visible.

---

## A cited number measures what its study measured, not what you are about to change

**Before applying an external budget or threshold, check what the underlying research actually
varied — because a derived figure quoted out of its mechanism measures the wrong thing
confidently.**

**Instances:**

- *2026-09-08, the MCP token budget.* A sibling project's "≤200 tokens per tool description" was
  taken as a byte budget and a miss was treated as a defect worth three mitigation proposals. The
  figure is derived from tool-COUNT research — a model discriminating among many near-twin tools
  (10 fine, 20 near-fine, 107 total failure). This server has four differentiated tools going to
  ten specified ones, so the crowding it measures is not present. Worse, the mitigations all
  targeted prose, which was 21% of the cost; 78% was schema structure, and that structure is the
  drift detector, not overhead. **Measuring first would have prevented the recommendation, not
  just corrected it.** The revisit trigger is now the mechanism (tool count, near-twins) rather
  than a byte count.
