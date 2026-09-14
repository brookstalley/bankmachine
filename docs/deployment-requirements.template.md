# Deployment Requirements — TEMPLATE

**Layer:** the instance. **Companion:** `system-requirements.md` (the provider-agnostic engine).
**Status:** template. Copy it, fill it in, and keep the copy out of version control.

---

## 0. What this document is, and why it is a template

`system-requirements.md` describes an engine that knows nothing about any particular financial
institution. **A deployment-requirements document is the roster** — which institutions *one*
operator connects, in what order, which need file import instead of the aggregator, and which
carry special rules.

Those are **real acceptance criteria**, not notes. They are simply criteria for one deployment
rather than for the engine, and they are numbered `DAC-n.n` to keep them distinct from the
engine's `AC-n.n` when the two documents cite each other.

### 0.1 Why this ships as a template and not as a filled-in document

A filled-in roster names the institutions an operator banks with, usually their account counts,
and often balances. That is exactly the material this repository must not carry, for two
independent reasons:

1. **It is nobody else's business.** This repository is a general-purpose tool. One operator's
   roster is not part of it.
2. **It is a norm this project already ratified, applied to itself.** The roster *is*
   configuration (engine AC-0.2), and configuration is gitignored.

🔴 **Fill in your copy at a path this repository ignores.** The convention is `deployment/`,
which `.gitignore` excludes; `tests/preferences/check-no-personal-data.sh` reads the token list described
in §8 from there and refuses a push that reintroduces any of it into a tracked file.

### 0.2 The contract with the system layer

🔴 **Every requirement in your filled-in copy must be satisfiable by configuration plus an
adapter, with zero change to the engine.**

This is a test, not a filing convention. If a deployment requirement cannot be expressed as
config or an adapter, that is a **gap in `system-requirements.md`** — fix it there and
generalize, never special-case it here. §7 is the checkable form of this contract, and it is
the single most important section to actually fill in: a roster with no traceability table has
lost the property that makes the engine trustworthy.

---

## 1. Roster — aggregator-connected

State the connection budget against the configured cap (engine AC-1.5 — the cap is
configuration, not a literal).

| Institution | Accounts known | Status as of YYYY-MM-DD | Capabilities needed |
|---|---|---|---|
| *(institution)* | *(n)* | *(OK / degraded / not linked)* | transactions, balance |
| *(institution)* | *(n)* | *(…)* | transactions, balance, **investments** |

Then write the criteria the table implies. The recurring ones, with the engine mechanism each
one leans on:

- **`DAC-1.1` — enrollment order**, and the reason for it. An institution holding *no* data in
  any current system is usually the right first connection.
- **`DAC-1.2` — which single connection carries the history-window verification** (engine
  AC-1.2, build step 3). 🔴 The requested window **cannot be raised after enrollment** without
  removing and re-linking. Verify the granted window on **one** connection before enrolling any
  other; getting it wrong means re-linking all of them.
  🔴 **The granted window is not visible at enrollment, and the wait is the point.** No response in
  the enrollment path reports it (engine AC-1.3a), so `granted_history_days` is **null on a
  freshly enrolled connection** and is filled once the first backfill reveals the oldest
  transaction actually returned. **Null means not yet known — never "we got what we asked for".**
  Reading it as satisfied is the one mistake this requirement exists to prevent, and it is
  available precisely at the moment an impatient operator would enroll the rest.
- **`DAC-1.3` — connections that are broken rather than closed.** A dead connection to a live
  account is enrolled fresh, not written off.
- **`DAC-1.4` — which connections need which capabilities.** Recorded as per-connection
  capabilities discovered at enrollment (engine AC-3.2), never as a named-institution branch.
- **`DAC-1.5` — anything descoped**, stated explicitly. If the roster once claimed a capability
  the engine defines nowhere, resolve it by descoping in writing rather than by dropping it
  silently, and say which engine path carries the remaining need.

---

## 2. Roster — file import required

Institutions with no aggregator path. Each needs an adapter under engine FR-7, selected by
configuration (AC-7.2) — never a hardcoded branch.

For each, record: the **exact export path** through the institution's UI, whether a full-history
dump exists or only per-statement files, and whether ongoing sync is needed or a **one-time
history import** suffices. Per-statement exports overlap at their boundaries, so the adapter has
to be idempotent across them (engine AC-7.5).

An account with a zero balance may still need its history imported: money that left it went
somewhere, and an unexplained transfer is a hole in the cashflow reconstruction. Decide that
explicitly rather than by omission.

---

## 3. Account rules — instances

Rules are data in `account_rules` (engine AC-8.1), never query-logic special cases. This section
records **which accounts get which rule type**, and the configuration each instance needs.

The v1 rule type is `contribution_only` (engine AC-8.2), for an account the operator funds but
does not solely own. Each instance must state the set of accounts whose inbound transfers count
as contributions — an inbound credit from outside that set is an **anomaly to surface**, not
income to classify silently (engine AC-8.4).

Record whether income is deposited into such an account. If none is, the rule is unambiguous and
needs no carve-out; if some is, say how it is distinguished.

---

## 4. Coverage expectations for this deployment

Target history depth, and what each institution actually supplies against it. Shortfalls are
recorded as known gaps (engine AC-11.8), never treated as complete.

🔴 Coverage is read **per account, never per institution** (engine AC-9.5). One institution may
hold many accounts with different coverage windows, and an institution-level summary hides that.

If the deployment is migrating off an incumbent aggregator, state the incumbent's floor — the
date everything was linked — and whether it backfills investment accounts at all. That floor is
usually the reason this pipeline exists.

---

## 5. Account inventory reconciliation

🔴 **A build task, not a formality.** Operators reliably under-recall their own accounts, and an
unaccounted-for credit line or loan materially changes the debt picture.

After enrollment, produce a written inventory of every account discovered — institution, type,
mask, current balance, first and last transaction date — for line-by-line review. **Unrecognized
accounts are a finding, not noise.** That inventory is the operator-supplied expected inventory
that engine AC-11.6 reconciles against; recollection stays out of the source-of-truth path.

---

## 6. Open questions (deployment layer)

Track them here with **what each one blocks**, because that is what decides whether the build can
start. The usual shape:

- *Blocks nothing* — answer before the first schema chunk, but do not wait on it.
- *Blocks enrollment (build step 3) only* — the complete institution roster usually lives here.
  Steps 1–2 (schema, raw layer, aggregator client against sandbox) do not depend on it.
- *Operator's call, no technical block.*

Engine-layer open questions live in `system-requirements.md` §9. Keep them there.

---

## 7. Traceability — deployment requirement → engine mechanism

🔴 **The checkable form of §0.2, and the section most worth the effort.** Every row must resolve
to configuration or an adapter. A row that cannot is a gap in `system-requirements.md`.

| Deployment requirement | Satisfied by | Engine change needed? |
|---|---|---|
| DAC-1.1 enrollment order | operator runbook / config ordering | no |
| DAC-1.2 window verification on first connection | build-step discipline + AC-1.3a recording the granted window at first backfill (AC-1.3 records the requested one at enrollment) | no |
| DAC-1.4 per-connection capabilities | capabilities discovered at enrollment (AC-3.2) | no |
| DAC-2.x file-import institutions | import adapter + config (FR-7, AC-7.2, AC-7.5) | no |
| DAC-3.x account rules | `account_rules` row + rule type (FR-8) | no |
| DAC-4.x coverage targets | configured window + per-account coverage (AC-9.5, AC-11.8) | no |
| DAC-5.x inventory reconciliation | operator-supplied expected inventory (AC-11.6) | no |

**If any row in your filled-in copy reads anything other than "no" in the third column, stop.**
That is the engine spec telling you it is missing something, and the fix belongs in
`system-requirements.md`.

---

## 8. Match tokens for the leak guard

Engine AC-0.3 requires roster identity to be matched by **explicit tokens the roster carries**,
never guessed from labels. Guessing fails both ways: a label is the institution's own spelling, so a
shortened form used in code slips past a literal match, while tokenizing a label collides with
unrelated legitimate text.

So the filled-in copy is accompanied by three plain token lists in the same gitignored directory.
Two matching modes, and the **file** carries the mode — never the token line, because a prefix or a
second column would have to pass the single-word validator, and that validator's strictness is what
stops a name matching nothing from counting toward a reassuring total:

| File | Holds | Matched |
|---|---|---|
| `deployment/roster-tokens.txt` | financial-institution spellings | case-insensitively, on word boundaries |
| `deployment/identity-tokens.txt` | the operator's name, username, machine name | case-insensitively, on word boundaries |
| `deployment/roster-tokens-cased.txt` | institution spellings whose lowercase form is an ordinary English word | **case-sensitively**, on word boundaries |

🔴 **The cased file is a genuine reduction in coverage, and the rule for using it is narrow.** A
token listed there no longer catches its lowercase or embedded form. That is the point — the leaked
form of a proper noun is capitalized, so ordinary prose using the word goes free — but it is less
protection than the strict files give. **A token belongs in the cased file only when its lowercase
form is an ordinary English word.** The default is the strict file, and a token's placement is a
review decision, not a convenience.

```
# One token per line. Blank lines and #-comments ignored.
examplebank
exbank
```

🔴 **A token is a SINGLE WORD** of letters, digits, `_` or `-`. The guard rejects anything else
loudly rather than accepting it, because both failure modes are silent: a multi-word line collapses
into a spelling that appears nowhere, and a regex metacharacter invalidates the whole pattern — and
in each case the bad token still counts toward a reassuring total. **List a multi-word institution
as the spellings that actually appear in prose, one per line.**

Identity tokens live here rather than in the guard's source for two reasons. A script carrying the
names it hunts for cannot scan itself, so the one tracked file guaranteed to contain them would be
the one file never checked. And anyone cloning a published copy of this repository would otherwise
inherit a guard protecting a stranger's identity while protecting none of their own.

`tests/preferences/check-no-personal-data.sh` matches these across **every commit being pushed** —
not the working tree, because the leak this project actually had was documentation in already-pushed
history behind a clean tip. A checkout with no `deployment/` directory has no tokens, nothing to
leak, and passes with a note; that is the ordinary state for anyone who is not this deployment's
operator, and it is what makes the guard safe to publish.

### 8.1 The audit that a push-scoped scan depends on

🔴 **A push is checked against the commits it actually publishes.** Where the remote has never seen
the branch, that is everything reachable from the tip that no remote-tracking ref already reaches —
not everything reachable, which on a branch cut from an existing one is almost entirely history the
remote already holds, and a refusal aimed at that history is one no push can act on.

**That narrowing has a precondition and it is not waived.** Trusting remote-tracking refs means
already-pushed history is never re-read, which is exactly the blind spot the guard exists to close.
🔴 **A one-off full-history audit closes it instead**, and it is a standing precondition rather than
a one-time chore: the audit is **per-deployment**, because the tokens are, and a token added later
has no audited history behind it.

- Run it before relying on the narrowed range, and again whenever a token is added.
- Record its adjudicated matches beside the token lists, in `deployment/`. A match judged not to be
  a leak is written down as adjudicated, so it does not re-trip the guard forever and so the
  judgement is reviewable.
- A match found in **unpublished** history is not covered by that adjudication. The push publishes
  it, and the guard blocking it is correct.

> **Amendment (2026-09-10).** This section previously described two token files matched
> case-insensitively, and a zero-remote push scanning every commit reachable from the tip. Both are
> restated above. The first push of a branch was refusing on matches in already-published history —
> a refusal unfixable at the moment it fires, whose only escape is `--no-verify`, which also
> switches off the gitflow guard that keeps non-release work off `main`. Narrowing the range is what
> removes it; the audit is what pays for the narrowing. The cased token file is the same problem in
> the other half: a roster token that is also an ordinary English word cannot be dropped, because a
> dropped token stops guarding every file, so it gets a narrower matching rule stated by the file it
> lives in.
