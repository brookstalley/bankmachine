# Discovery — What the operator owns, and what a rebuild owes it

**Work cycle:** operator-owned columns on the store · medium · requirement
**Opened:** 2026-09-10
**Closes (proposed):** brookstalley/bankmachine #48
**Follows from:** #40 / FR-9, which shipped a `closed` lifecycle value reachable only by retiring a
whole connection

---

> **Scope.** This document settles the *contract* for an operator-writable column and writes the
> requirement #48 asks for. It does **not** implement `bankmachine accounts retire`; #48's own
> acceptance says "No code on this item", and the reason is that the trap below was found by
> measurement rather than by reasoning and has already refused a rebuild on a real store once.
>
> It is written to serve **two** known consumers, not one: the account-retirement path #48 needs,
> and a per-row annotation field the owner has named as upcoming. A contract derived from a single
> consumer is a carve-out wearing a contract's clothes, and this repo has the learning on file
> (§ *A carve-out reaches every state that shares its return type*).

---

## The questions this item has to answer

**What problem are we solving?** `lifecycle_status` and `balance_class` are declared operator-owned
and the product can properly write neither: `balance_class` has no writer at all, and
`lifecycle_status` has one that retires an entire connection. So FR-9's `closed` is reachable only
by an act that also stops syncing every sibling account — which is not a declaration path but a
reason to avoid declaring. Before a per-account write path can be built, the store has to say which
columns an operator may write, what happens to a derivation-owned timestamp when they do, and what
`content_digest` covers.

**What does success look like?** A new operator-writable column can be added by making a
declaration, and the declaration is what protects it — not the accident of no deriver happening to
emit that key. An operator's correction survives `store rebuild`, and a rebuild that would lose one
fails loudly instead of certifying itself.

**What is out of scope?** The `bankmachine accounts` command surface (its verbs, its flags, its
output) beyond what the contract constrains; `balance_class`'s missing write path, which is named
here as the same shape and not fixed; and the annotation field's own product semantics.

### 🔴 The question this item does not answer, and must not answer silently

**Whether an *agent* may write.** The owner has described the annotation field as a scratch pad for
"users and agents". The MCP surface is read-only by norm — *"Read-only. No mutation tools. No
exceptions"* (`api-contract.md` § Surface Inventory, `docs/system-requirements.md` § 5) — and an
agent reaching this store reaches it through that surface and no other. So an agent-authored
annotation is not a design question inside this contract; it is a **ruling on a red-line norm**, and
it belongs to the owner exactly as FR-9's net-worth treatment did.

This document assumes the write path is **operator-invoked CLI only** and marks that assumption
vetoable (§ *Assumptions, vetoable*). Every criterion below holds unchanged if the owner later rules
that agents may write; what changes is *who calls* the path, not what the store owes the value.

---

## Premise verification — what #48 claimed, and what the tree says

#48's claims were checked against the tree rather than carried forward.

**Claim — `closed` is unreachable. PARTLY WITHDRAWN, and the correction is F2.** The deriver half
holds: `_OPERATOR_OWNED` (`src/bankmachine/connector/plaid/derivers.py`) carries `balance_class` and
`lifecycle_status`, the accounts deriver hardcodes `"lifecycle_status": "active"` at insert and
filters the column out of the update arm, and there is no `bankmachine accounts` command module. But
`closed` is **not** unreachable — `connections retire` reaches it for every account on a connection
at once. The defect is granularity, not absence, and #48's body predates the retirement path that
made that true.

**Claim — `connections retire` does not touch `accounts`. WITHDRAWN, and this is the useful
correction.** It does now, and it establishes the precedent this contract generalizes.
`cli/connections.py::_mark_retired` sets `lifecycle_status='inactive'` and `closed_date` on the
connection's still-active accounts, in the retirement transaction, and it carries a comment saying
in so many words why `updated_at` is not written. So the *hard* half of #48's trap already has a
worked answer at one call site. What is missing is that the answer is a comment at one call site
rather than a property of the store.

**Claim — the `updated_at` trap. CONFIRMED, and it is the load-bearing finding.**
`_upsert_account` writes `updated_at` from `response.received_at`; `_OPERATOR_OWNED` does not shield
it; `rebuild.digest_columns` returns every non-rowid column of every table, so it is inside
`content_digest`. An operator write that bumps it to wall-clock time is reverted by the next sync,
and the next `store rebuild` replays the archived value, moves the digest, and raises
`RebuildNotReproducibleError` — which rolls back the whole rebuild rather than the one row.

---

## The model: two classes of table, two mechanisms, one contract

The store already distinguishes two kinds of table, and **operator ownership means a different
mechanism in each**. Neither mechanism is wrong; what is missing is the statement that they are the
same contract wearing two implementations.

| | Derived (rebuildable) | Dimension |
|---|---|---|
| Marked by | carries `derivation_version_id` | carries none |
| What `store rebuild` does | DELETEs the rows and replays them | never empties it; the replay upserts in place |
| What threatens an operator value | the delete | the deriver's update arm |
| The mechanism that saves it | `rebuild.OPERATOR_STATE` — identity + columns, captured before the delete, restored **before** the digest | `_OPERATOR_OWNED` — the update arm filters the column out |
| Shipped instance | `transactions.category_override` | `accounts.lifecycle_status` |

**The annotation field lands in the left column and #48's work lands in the right one.** That is the
whole reason to settle the contract once: the two consumers do not share a mechanism, so a rule
written from either one alone will not cover the other.

**Both mechanisms are hand-maintained lists of column names, and that is deliberate.**
`rebuild.py` records the reason beside `OPERATOR_STATE`: *"the operator owns this" is a decision
recorded in `data-model.md` and not something a column's type can say.* This document does not
propose deriving them from the schema. It proposes that something **compare** them, which is the
repo's own § *Two descriptions, compared* rule — the disagreement is the only thing that could ever
tell you they had drifted.

---

## Findings

**F1 · `closed_date` is operator-owned everywhere except in the guard.** `data-model.md`'s `accounts`
field table calls it *"Operator-owned, like `lifecycle_status`"*; its only writer in `src/` is
`_mark_retired`; and it is **absent from `_OPERATOR_OWNED`**. It is safe today only because the
accounts deriver never puts a `closed_date` key in the values dict it filters — so the filter has
nothing to exclude. The column is protected by an absence, not by the declaration whose entire job is
to protect it. The first time a deriver learns to emit `closed_date`, an operator's declaration is
silently overwritten and the guard named for preventing exactly that does not fire.

This is § *Guarantees by construction* verbatim: the rule matches on a **name** where it means a
**relationship**.

**F2 · The two declared operator-owned columns on `accounts` fail in two different ways, and
#48's own framing collapses them.** `balance_class` has **no writer at all** — declared
operator-correctable, reachable by nothing. `lifecycle_status` **does** have one, and #48's claim
that it does not is stale: `_mark_retired` writes it. But that writer acts at **connection**
granularity, so the only way to declare one account closed is to retire the institution it belongs
to, which also stops syncing every other account on that connection.

🔴 **So `closed` is not unreachable — it is reachable only at a granularity that makes it the wrong
instrument.** That is a materially different defect from the one #48 filed, and it changes what the
build owes: not "give `lifecycle_status` a writer" but "give it one whose blast radius is the
account the operator named." A declaration that has never been exercised is a claim rather than a
guarantee (`verify_norms_go_red.py`'s standard); a declaration exercisable only by over-reaching is
one an operator will decline to use.

**F3 · `content_digest` is opt-out by restoration, not opt-in by declaration.** `digest_columns`
covers every non-rowid column, so **a new operator column is inside the digest the moment it
exists**. It stays reproducible only because something puts the value back before
`content_digest` is taken. A column added without its mechanism entry therefore fails twice at
once: the rebuild wipes the operator's value, *and* the digest moves, so the rebuild refuses. The
second failure is what makes the first survivable — it is the reason this is a loud bug rather than
a silent one, and it must not be traded away for convenience.

**F4 · The precedent for the timestamp rule exists at one call site and nowhere else.**
`_mark_retired`'s comment is correct, specific, and invisible to the next person who writes an
operator path against a different table.

---

## Requirements — AC-15.1 through AC-15.9

Ids are drawn from the reserved block **AC-15.1 – AC-15.9**, the next free block (§ 4's FR-9 holds
AC-12.x; AC-13.x and AC-14.x are § 7's). Proposed home: **FR-10 · Operator-owned columns**, in § 4
Data model, immediately after FR-9.

**AC-15.1 · An operator-writable column is declared, and the declaration is what protects it.**
Every column an operator may write appears in the declaration its table class requires — the
dimension guard, or the rebuild's operator-state map — and no column is protected merely because no
deriver currently emits it. *Why:* F1. A guard that holds only while an unrelated dict stays empty
has not been tested by anything and will fail on the change that fills it.

**AC-15.2 · An operator write never moves a derivation-owned column.** A write on the operator's
behalf sets the operator's own columns and nothing else; in particular it does not stamp
`updated_at`, which is written from the archived response and is inside the content digest. *Why:*
F2/F4 and the measured trap. The consequence of breaking this is not a wrong field, it is
`store rebuild` refusing to run at all, and the operator discovers it at the moment they most need
the rebuild to work.

**AC-15.3 · An operator's value survives a rebuild, by the mechanism its table class requires.** For
a table the rebuild empties, the value is captured before the delete and restored before the digest
is taken; for a table it does not empty, the deriver's update arm excludes the column. *Why:* the
two shipped instances already do this and the property is currently a coincidence of two independent
implementations rather than a stated contract.

**AC-15.4 · The declarations and the schema are compared by something that fails.** A check
enumerates the declared operator-owned columns, confirms each exists on the table it names, and
confirms each is covered by the mechanism its table class requires. *Why:* § *Two descriptions,
compared*. Both declarations are hand-maintained on purpose; hand-maintained and unchecked is a
different thing, and F1 is what it looks like.

**AC-15.5 · No column is declared operator-owned without a path that can write it, at the
granularity the declaration is about.** A declaration with no writer is refused by the same check as
AC-15.4; a declaration whose only writer acts on a coarser entity than the column's own row is
recorded as unmet, not as satisfied. *Why:* the two instances on `accounts` fail differently and a
criterion that only asked "is there a writer" would catch one and bless the other. `balance_class`
has none. `lifecycle_status` has one that retires a whole connection, so an operator wanting to
close one card must stop syncing every account at that institution — which is not a declaration
path, it is a reason not to declare. The granularity clause is what makes this criterion say what
#48 actually needs.

**AC-15.6 · An operator declaration says when it was made, without borrowing a derivation-owned
timestamp.** Where the time of an operator's declaration is worth keeping, it is kept in a column
the derivation does not write. *Why:* AC-15.2 forbids the obvious shortcut, and the need is real —
`closed_date` already exists for exactly this reason on exactly this path. Stated as a rule so the
next operator column does not rediscover it by breaking a rebuild.

**AC-15.7 · An operator declaration is reversible by the same surface that made it.** Any path that
records an operator declaration offers the way back. *Why:* the declaration is a human judgement
about which of several indistinguishable causes applies (AC-12.2), and a judgement made from
ambiguous evidence is one a person will sometimes get wrong. A store that accepts a correction and
cannot accept its withdrawal converts a typo into a permanent fact.

**AC-15.8 · An operator value with nowhere to land is reported, never re-homed.** Where a rebuild
cannot match a captured value back to a row, the value is surfaced to the operator and not applied
to any other row. *Why:* already true in `_restore_operator_state` and stated here so it is a
contract rather than an implementation detail — applying a correction to the wrong row is worse than
losing it, and losing it silently is worse than either.

**AC-15.9 · Operator-owned columns stay inside the content digest.** They are not excluded from
`content_digest` to avoid the capture-and-restore work. *Why:* the alternative was weighed and
rejected. Excluding them would let a rebuild drop every operator correction in the store and still
certify itself as reproducing what it replaced, which is the one thing the digest exists to prevent.
The cost of keeping them in is that a new operator column needs its mechanism entry, and AC-15.4 is
what makes that a caught omission rather than a discovered one.

---

## Design — proposed, and deliberately thin

The contract above is the deliverable; the command is #48's build. Two shape notes are recorded
because they were derived here and would otherwise be rediscovered:

- **`bankmachine accounts retire <account_id>` / `... reinstate <account_id>`**, in the shape
  `connections retire` established. It writes `lifecycle_status` and `closed_date` only, per
  AC-15.2, and `reinstate` is AC-15.7.
- **The annotation field, when it is built**, is `transactions.category_override`'s shape: a column
  on a rebuildable table, an `OPERATOR_STATE` entry keyed on the identity that table already uses,
  and no `updated_at` write. Its product questions — which tables carry it, one value or many,
  whether it is searchable, and 🔴 **whether an annotation may ever influence an aggregate** — are
  not settled here and are not this contract's to settle.

🔴 **The strong prior on that last one, recorded for whoever writes it:** an annotation should ride
the row as data and never enter arithmetic. An operator note that silently moves a number is the
failure class this whole surface exists to refuse, and it would arrive with no warning kind able to
describe it.

---

## Assumptions, vetoable

Each of these was inferred from the tree and the artifacts rather than taken from the owner. An
assumption the owner rejects retires the criteria resting on it.

**A1 · The write path is operator-invoked CLI only.** Rests on the MCP read-only norm. If the owner
rules that agents may write, no criterion above changes — AC-15.1–15.9 are about what the *store*
owes an operator value, not about who calls in — but the norm needs an amendment and the annotation
item inherits a much larger surface. *Retires:* nothing. *Expands:* the annotation item's scope.

**A2 · `updated_at` stays derivation-owned.** The alternative — making it operator-writable and
excluding it from the digest — was not costed in depth. It is rejected here because `updated_at` is
the only record of when the archive last spoke about a row, and an operator write would overwrite a
derivation fact with a bookkeeping one. *Retires if vetoed:* AC-15.2 and AC-15.6.

**A3 · The annotation field is a real upcoming requirement rather than an idea being explored.**
Taken from the owner's own framing. It is why the contract is written for two consumers. *Retires if
vetoed:* nothing; it would make AC-15.3's two-mechanism framing over-general but not wrong.

**A4 · No existing store holds an operator value that would be lost by adopting this.** `closed_date`
and `lifecycle_status` are written only by `_mark_retired`, which shipped recently; `category_override`
already has its mechanism. *Retires if vetoed:* nothing, but a migration note would be owed.

---

## Requirements confidence

**High** on the findings. F1 through F4 were each read off the tree during this pass, and the two
that matter — `closed_date`'s absence from `_OPERATOR_OWNED`, and `digest_columns` covering every
non-rowid column — are single-line reads that a reviewer can re-derive in under a minute.

**High** on AC-15.1 through AC-15.4 and AC-15.8 through AC-15.9. These state properties the store
either already has at one call site or demonstrably lacks, and each names the observation behind it.

**Medium** on AC-15.5. The criterion is right in principle; what a "path that can write it" means for
a column reachable only through an interactive command is a definition the build will have to
sharpen, and a check that is too literal will fail on a legitimate case before it catches a real one.

**Medium** on AC-15.6 and AC-15.7. Both are inferred from one shipped precedent each rather than
from a stated need, and `reinstate` in particular has no filed request behind it — it is derived
from AC-12.2's ambiguity, which is an argument rather than a report.

**Not measured:** whether any check satisfying AC-15.4 can run without importing the deriver module
into the store layer. That is a layering question this pass did not open, and it is the most likely
place for the build to find the design harder than this document makes it sound.

---

## Shared-artifact deltas for the integrator

1. **`docs/system-requirements.md`** — a new **FR-10 · Operator-owned columns** in § 4, after FR-9,
   carrying AC-15.1–15.9.
2. **`.prawduct/artifacts/data-model.md`** — the `accounts` field table's `closed_date` row currently
   claims an ownership the guard does not encode (F1); it should either stop claiming it or the guard
   should encode it, and AC-15.1 chooses the latter. § Account lifecycle's ⚠️ *"`closed` is reachable
   in the read path and unreachable in the product"* is discharged when #48 builds, not by this pass.
3. **`.prawduct/artifacts/architecture.md`** — the two-class table model in § *The model* above is
   stated nowhere as a single idea; the rebuild's classification and the deriver's guard are each
   documented alone.
4. **`.prawduct/backlog.md` / the tracker** — #48 advances to `stage:ready` on this document landing.
   The annotation field wants its own item at `stage:requirements`, linked here, carrying the
   agent-write ruling as its named blocker.
5. **`api-contract.md` § Direction** — no norm is proposed. The read-only norm already governs A1 and
   this document does not amend it; if the owner rules that agents may write, that ruling amends it
   and this is where the amendment lands.
