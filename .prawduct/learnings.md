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
