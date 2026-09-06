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

**How to apply:** before recording a guarantee, name the surface that could violate it and check
that surface exists in the product. If the answer is "a future command someone forgets to add to
the list" or "any SQL that reaches this handle", the guarantee needs a different mechanism, not a
firmer sentence. Related: [[review-coverage]] — both are the same family, a check whose bad news
never arrives.

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
