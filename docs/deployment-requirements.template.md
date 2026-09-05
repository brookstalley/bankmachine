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
which `.gitignore` excludes; `scripts/check-no-personal-data.sh` reads the token list described
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
| DAC-1.2 window verification on first connection | build-step discipline + AC-1.3 recording granted window | no |
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

Engine AC-0.3 requires an automated check that no roster name reaches the source or schema roots,
and it requires the roster to **carry its own explicit match tokens** rather than have the check
guess them from labels. Guessing fails both ways: a label is the institution's own spelling, so a
shortened form used in code slips past a literal match, while tokenizing a label collides with
unrelated legitimate text.

So the filled-in copy is accompanied by a plain token list, one token per line, at
`deployment/roster-tokens.txt`:

```
# One match token per line. Blank lines and #-comments ignored.
# Include every spelling that could plausibly appear in code or a doc:
# the institution's own name, any shortened form, and any internal nickname.
examplebank
exbank
```

`scripts/check-no-personal-data.sh` matches these on word boundaries across tracked files. A
checkout with no `deployment/` directory has no roster to leak, and the guard passes with a note
— that is the ordinary state for anyone who is not this deployment's operator.
