# Discovery — What an agent may remember, and where a memory lives

**Work cycle:** agent annotations on stored items · medium · requirement
**Opened:** 2026-09-10
**Follows from:** FR-10 / #48, which settled the operator-owned-column contract and was deliberately
written for **two** consumers — the account-retirement path, and this one
**Amends:** the MCP read-only norm (`api-contract.md` § Direction; `docs/system-requirements.md` § 5),
on the owner's instruction of 2026-09-10

---

> **Scope.** This document writes the requirement and prices the implementation options. It builds
> nothing. The owner's own sequencing is that this lands **after** production onboarding, and the
> reason is stated in § *Sequencing* rather than left as a preference: the requirement's shape
> depends on evidence that only real sessions produce.
>
> One thing it does settle now, because the owner ruled on it and because leaving it open would
> block the write-up: **an agent may write.** The MCP read-only norm is amended, not reinterpreted.

---

## The questions this item has to answer

**What problem are we solving?** An agent that works this store learns things about it that the
store cannot hold — that a recurring charge is a subscription the owner meant to cancel, that an
account's balance is stale for a known reason, that a merchant string maps to something no category
captures. Today that knowledge lives in a session transcript and dies with it, so the next session
re-derives it from scratch or, worse, re-derives it differently.

**What does success look like?** An agent can attach a note to a transaction, an account or a
security; the note is still there next session, after a `store rebuild`, and after a backup and
restore; a later agent can tell the note apart from the data, and can tell who wrote it and when;
and no number this product reports has ever moved because of one.

**What is out of scope?** Search over annotations. Any structured or typed annotation (tags,
categories, key-value facts) — this is free text with provenance, and a schema for *kinds* of
learning is a second requirement that should wait for evidence of what the kinds are. Annotations on
anything that is not an enrolled item — no free-floating agent scratch pad, no session journal.
Sharing annotations between deployments.

---

## The requirement as the owner stated it (2026-09-10)

Recorded verbatim in substance, because the ACs below are derived from it and a paraphrase would
quietly become the requirement:

1. An agent can record notes for any item — a transaction, an investment, an account — in order to
   persist learnings for future sessions.
2. The annotations are mutable: they may be deleted, appended to, rewritten. A ledger of discrete
   annotations is permitted but not required.
3. The annotations travel with the database and survive backup / restore.
4. Metadata such as a datetime is welcome but not strictly required.
5. Storage shape — same database or not, column or otherwise — is explicitly the implementer's call.

**Item 4 is the one this document declines to take at face value**, and § *Why provenance is not
optional* says why: the metadata is not bookkeeping here, it is what keeps a later agent from
reading its own guess as the store's fact.

---

## The norm, and what the amendment does and does not license

The MCP surface is read-only by norm and by file handle. That norm is worth something precisely
because the second half is structural — every read-role handle opens `mode=ro` at the file, so the
refusal survives a future tool author who never read the norm (`security-model.md` § Authorization).
An unqualified "no mutation tools, no exceptions" forces the whole handle open to admit one note.

**The owner's amendment, in this document's words:** an agent may write only to sidecar data
bankmachine itself maintains; data that came from outside is never written.

Rendered into this store's own vocabulary, so it is checkable rather than interpretable:

> An agent-originated write may touch only a table that appears in an explicit **agent-writable
> declaration**, and no table carrying `raw_response_id` or `derivation_version_id` may appear in
> that declaration. Any table carrying either is read-only to the MCP surface, **whatever the
> column**.

🔴 **Both halves are load-bearing, and the first was nearly left out — see F8.** The provenance test
alone reads as a complete rule and is not one: `accounts`, `institutions` and `connections` carry
neither provenance column, so a bare "no raw provenance, no derivation version" bound would hand an
agent `lifecycle_status`, `balance_class` and every connection's sync state. The declaration is what
grants; the provenance test is a floor beneath it that nothing may be declared through.

🔴 **The row is the unit, not the column, and that is the load-bearing half of the ruling.**
`transactions.category_override` is operator-authored, is already declared operator-owned, and stays
out of an agent's reach under this amendment. Two reasons, both of which outlive this item:

- **A permission scoped by table is auditable; one scoped by column is not.** SQLite has no
  per-column grant, so a column-scoped rule can only ever live in application logic — which is the
  guarantee this norm exists to avoid resting on.
- **`category_override` enters arithmetic.** It is what `money_summary` groups by. An agent that can
  move it can move a total, which is the one thing § *Why an annotation never enters arithmetic*
  forbids outright.

**What the amendment does not license:** it does not open a writable handle in the server process.
Which write path is chosen is § *Option axis 2*, and the recommended one keeps `mode=ro` intact
there permanently.

---

## Findings — measured against the tree, 2026-09-10

**F1 · Backup already satisfies requirement 3, for anything inside the store.** `store backup` is
`VACUUM INTO` of the whole file (`store/backup.py`), so every table travels through a backup by
construction and needs no per-table work. The corollary is the argument against a second file, and
it is in § *Option axis 1*.

**F2 · A sidecar table survives a rebuild with no mechanism at all.** `store rebuild` deletes rows
only from tables carrying a foreign key into `raw_responses` (`rebuild.py`, `RAW_PROVENANCE_COLUMN`).
A table carrying neither that nor `derivation_version_id` is in a **third class** the FR-10 contract
does not name: not derived-and-rebuildable, not a dimension the deriver upserts, but a table nothing
derives. It needs neither `OPERATOR_STATE` capture-and-restore nor an `_OPERATOR_OWNED` filter,
because nothing ever comes for its rows.

**F3 · And it is inside the content digest for free.** `rebuild.content_digest` iterates
`metadata.sorted_tables` and hashes every non-rowid column of every table. So a sidecar table is
covered by the digest without an entry anywhere, and — since the rebuild does not touch it — its
contribution is identical on both sides. AC-15.9's demand (operator values stay inside the digest,
never excluded to dodge the work) is satisfied by construction rather than by care.

**F4 · 🔴 One of the three subject types has a surrogate key a rebuild reassigns, and only one.**
The rebuild partitions derived tables on whether they point at a raw response (`rebuild.py`,
`rebuildable_tables` / `derived_dimension_tables`), and the three subjects land in three different
places:

| subject | class | is its `INTEGER PRIMARY KEY` stable across a rebuild? |
|---|---|---|
| `transactions` | rebuildable — deleted and replayed | **No.** `transaction_id` is reassigned on the replay |
| `securities` | derived dimension — never emptied | Yes, and `rebuild.py` says why: a dimension row is shared by holdings and investment transactions, so it has "no id that could be reassigned without orphaning them" |
| `accounts` | not derived at all — no `derivation_version_id` | Yes; it is upserted in place |

So the trap is real and narrower than it first looks. `transactions` is the one that bites, and it is
exactly why `rebuild.OPERATOR_STATE` keys `category_override` on
`("account_id", "source_transaction_id")` rather than on the row id: an annotation keyed on
`transaction_id` would survive every rebuild **and silently re-point at a different transaction** — a
note that still reads plausibly, against the wrong row.

🔴 **The recommendation is a uniform natural key anyway, and the reason is not today's
classification.** `securities.security_id` is stable *because* the deriver upserts on the natural key
— a norm the deriver must obey, not a property of the schema — and a table's class is a thing that
changes: `securities` would become rebuildable the day it gained a raw-response FK. A key that is
correct only while a classification holds is the same shape as AC-15.1's guard that holds only while
a dict stays empty. **Named trap for the build:** the natural key for an account is not obvious —
`source_account_id` can change across a re-link and `source_persistent_account_id` is nullable — and
picking between them is a decision this document does not make.

**F5 · "An investment" is two different things and only one of them can carry a note.** `holdings`
is a daily snapshot keyed `(account_id, security_id, as_of_date)` — the schema comment calls it "a
daily snapshot on the same append rule as balances". Annotating one annotates *a position on a day*,
which is almost never the intent, and the note stops applying tomorrow. `securities` is the durable
object (`source_security_id` is its natural key). The annotatable set is therefore **account,
security, transaction** — and `investment_transactions` on the same argument as `transactions`, when
that path is built.

**F6 · The security model's mass-assignment row becomes false the moment this ships.**
`security-model.md` § Authorization records API3 (broken object property level authz / mass
assignment) as **No — the surface is read-only, there is no request binding to over-permit**. An
`annotate` tool is a request binding. The mitigation is not application-level validation; it is that
the write path's reachable statement set is one table (§ *Option axis 2*, Option 1), which is the
same argument `mode=ro` makes one layer down. The row needs rewriting with the requirement, not
after it.

**F8 · 🔴 The obvious statement of the owner's rule has a hole in it, and FR-10 already named the
class.** "A table whose rows no aggregator produced and no deriver writes" sounds like it means
"carries neither `raw_response_id` nor `derivation_version_id`" — but three tables satisfy that test
today and none of them is sidecar data. `accounts`, `institutions` and `connections` are
**dimensions**: the rebuild never empties them (that is what makes them dimensions), the deriver
upserts them in place, and they carry no provenance column because their provenance is the upsert
itself. A bound written as the negative test would license an agent to write `accounts.lifecycle_status`
— which the balance-lifecycle norm makes arithmetic — and every connection's sync cursor.

This is AC-15.1's finding arriving one layer up: *no column is protected merely because no deriver
currently emits it*, and by the same argument no table is unwritable merely because no test currently
says so. So the permission is a **declaration**, the provenance test is a **floor** under what may be
declared, and neither alone is the rule. Rendered as such above.

**F7 · The prior recorded shape for this field is wrong, and it is on disk.**
`discovery-operator-owned-columns.md` § *Design* and its assumption A3 record the annotation field as
`transactions.category_override`'s shape — *a column on a rebuildable table with an `OPERATOR_STATE`
entry*. Against the owner's actual requirement (three subject types, multiple mutable notes, an
author) that shape is wrong on every axis. A3 is not vetoed — the field is real, which is what A3
asserted — but the design note beside it is superseded here.

---

## Option axis 1 — where an annotation lives

**Option A · A column on each annotatable table.** The previously recorded shape. One note per row,
no author, no timestamp that is not derivation-owned (AC-15.2 forbids reusing `updated_at`), a
migration per subject type, and two hand-maintained declaration lists to keep in step per FR-10.
**Rejected.** It reproduces FR-10's whole mechanism burden three times to store strictly less than
the requirement asks for.

**Option B · One sidecar table in the same database. ✅ Recommended.**

```
annotations(
  annotation_id     INTEGER PRIMARY KEY,      -- internal only; never an external handle
  subject_type      TEXT NOT NULL,            -- 'account' | 'security' | 'transaction'
  subject_key       TEXT NOT NULL,            -- the subject's DURABLE identity (F4), not its row id
  body              TEXT NOT NULL,
  author_class      TEXT NOT NULL,            -- 'operator' | 'agent'
  author_id         TEXT,                     -- which agent/session, where the surface knows
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL             -- this table's own; no derivation column is reused
)
```

Carries neither `raw_response_id` nor `derivation_version_id` — which is what makes it the third
class in F2, what makes it the *only* thing the amended norm lets an agent write, and what gets it
F1/F2/F3 for free. Many notes per subject, so mutability (requirement 2) is rows, and provenance
(§ below) has somewhere to live. Adding a fourth subject type later is a value, not a migration.

**Option C · A separate `annotations.db` file.** Its one real virtue is that the main store's handle
could stay `mode=ro` in every process forever, with no ruling needed at all. **Rejected, and the
reason is requirement 3.** Two files drift apart: a backup taken of one and not the other, or a
restore that pairs a fresh store with a stale sidecar, produces notes silently attached to rows that
have moved on — which is *precisely* the failure class this product exists to refuse, arriving
through the one door that has no warning kind able to describe it. It also doubles the key
derivation, the lock regime and the backup surface to avoid a ruling the owner has already made.

---

## Option axis 2 — how an agent's write reaches the store

**Option 1 · The server delegates the write to a separate writer. ✅ Recommended.** The MCP process
keeps `mode=ro` on its own handle permanently; an `annotate` tool hands the write to the writer
entry point (the CLI path, invoked as a subprocess, or an equivalent narrow writer module invoked
out-of-process), which opens the writer handle, writes one table and exits.

- The read surface stays *structurally* incapable of mutation — the norm's whole value (F6, and
  `security-model.md`'s `mode=ro` paragraph) is preserved rather than traded.
- The audit surface is one small program with one reachable table, which is what makes AC-16.9
  checkable by a test rather than by review.
- Costs: a process spawn per write — negligible for something written a few times a session — and a
  stated behaviour when the exclusive writer lock is held by a sync or a backup (AC-16.11).

**Option 2 · The server opens a writer handle for annotation writes.** Simplest to build, and it is
the option to take if Option 1's out-of-process hop proves unworkable. Its cost is precise: the
guarantee drops from *enforced by the file handle* to *enforced by whoever reviews the next tool*,
for **every** tool in the process and not just this one, and SQLite has no per-table permission that
would narrow it back. `sync shell` is the standing reminder of why that distinction was drawn.

**Option 3 · No agent write — the agent proposes, the operator commits.** Recorded because it is the
status quo and costs nothing. It fails requirement 1: a learning that needs a human in the loop to
persist is not persisted for the next session, it is queued for a person.

---

## Why provenance is not optional, though the owner made it optional

Requirement 4 makes metadata welcome and not required. The recommendation is to require **author and
both timestamps** anyway, and the reason is not audit:

**A later agent must be able to tell "something an agent concluded once" from "something the data
says".** Without an author, session five reads session two's guess as an established fact of the
store, and each session's speculation becomes the next one's premise. That is a self-confirming
memory, and it degrades exactly as the sessions accumulate — worst at the moment the store is most
worth trusting. An author field costs one column and closes it.

The same argument sets the read shape: an annotation is returned **labelled as an assertion, with
its author and its age**, never merged into the field it comments on.

## Why an annotation never enters arithmetic

`discovery-operator-owned-columns.md` recorded the strong prior; this document adopts it as a
requirement (AC-16.6) and states the argument that makes it absolute rather than a default.

Every other form of incompleteness in this product has a warning kind that can describe it — `stale`,
`degraded`, `gapped`, `partial`, `rule-applied`. There is **no kind that can say "this total moved
because an agent believed something,"** the vocabulary is deliberately closed (`api-contract.md`
§ *The warning vocabulary*), and adding one would be inventing a way to announce a failure rather
than preventing it. So the prohibition is the mitigation: a note rides the row as commentary and
touches no number.

---

## Requirements — AC-16.1 through AC-16.11

*Proposed for `docs/system-requirements.md` § 4 as **FR-11 · Agent annotations**. They hold under
either write-path option; Option 1 is what makes AC-16.9 and AC-16.10 cheap to enforce.*

**AC-16.1 · An annotation addresses its subject by durable identity, never by a surrogate row id.**
The stored key is the identity that survives a rebuild, and it does not rest on the subject table's
current rebuild classification. *Why:* F4. A rebuild reassigns `transactions.transaction_id`, so an
annotation keyed on it survives intact and points somewhere else — a note that still reads plausibly
against the wrong transaction, with nothing in the payload able to say it moved. The second clause is
what stops the criterion being satisfied by today's accident: `securities.security_id` is stable only
because that table is a dimension and its deriver upserts on the natural key, and a table's class
changes when its schema does.

**AC-16.2 · An annotation survives `store rebuild` without being captured, restored, or replayed.**
Annotations live in a table the rebuild does not empty — carrying neither `raw_response_id` nor
`derivation_version_id` — and no `OPERATOR_STATE` or `_OPERATOR_OWNED` entry is needed for them.
*Why:* F2. The mechanism-free property is worth stating as a contract, because the alternative shape
(a column on a rebuildable table) needs both mechanisms and gets no protection FR-10 does not already
have to hand-maintain.

**AC-16.3 · An annotation is inside the content digest, and inside the same file as the data.** It
is not excluded from `content_digest`, and it is not stored beside the datastore in a second file.
*Why:* F3 makes inclusion free, and AC-15.9 already rejected the excluded-to-save-work shape for
operator values. The one-file half is requirement 3 stated so it cannot be satisfied by a design
that technically backs the notes up: two files can be restored out of step, and a note re-attached
to a row that has moved on is the failure this product refuses everywhere else.

**AC-16.4 · An annotation whose subject cannot be found is reported, never silently kept and never
re-homed.** A write naming an unknown subject is refused at the time of writing; an annotation whose
subject later disappears from the store is surfaced to the operator and attached to no other row.
*Why:* the AC-15.8 argument, arriving by the other door. FR-10's restore path gets this moment for
free — the rebuild has to match a captured value back to a row and can say when it cannot. A sidecar
table has **no such moment**: nothing ever walks it, so an orphan simply persists, and the note keeps
being returned for a subject that is gone. This is the price of the recommended shape and it is paid
by an explicit reconciliation, not by a mechanism that already exists.

**AC-16.5 · Every annotation says who wrote it and when.** Author class (operator or agent), the
author's identity within that class where the surface knows it, and the time it was created and last
changed — in the annotation's own columns, never by reusing a derivation-owned timestamp.
*Why:* § *Why provenance is not optional*, and AC-15.2/AC-15.6 for the second clause — `updated_at`
on a derived row is written from the archived response and is inside the digest, so borrowing it
breaks the next rebuild.

**AC-16.6 · 🔴 An annotation never enters arithmetic.** No aggregate reads one, no total moves
because of one, no derived value is computed from one, and no annotation changes how a row is
classified or grouped. It is returned as commentary on a row, labelled as an assertion with its
author and age — never merged into the field it comments on. *Why:* § *Why an annotation never
enters arithmetic*. The warning vocabulary is closed and has no kind that could describe the
violation, so this is prevented rather than announced.

**AC-16.7 · An annotation is rewritable and deletable by the surface that wrote it, and a delete
deletes.** No tombstone, no soft-delete, no append-only ledger imposed on the operator's behalf.
*Why:* requirement 2, stated as a contract because the store's other norms run the other way — a
transaction is never hard-deleted, a source value is never overwritten in place (`data-model.md`
§ Direction). Those norms exist to protect a record of what an institution said. An annotation is
not that record: it is the author's own current belief, and a belief that cannot be withdrawn is a
belief that becomes wrong permanently. **This is a boundary ruling on those two norms, recorded in
this document and linked from them, not a silent exemption.**

**AC-16.8 · An annotation rides the item it annotates.** Where a tool returns an item singly, its
annotations come with it; an aggregate carries none, in either its figures (AC-16.6) or its payload.
*Why:* a memory that a later agent has to remember to go and look for is not a memory. The aggregate
half is AC-16.6's corollary at the envelope level: an annotation next to a total invites exactly the
arithmetic AC-16.6 forbids.

**AC-16.9 · An agent writes only what is declared agent-writable, and the declaration cannot name a
table the store derives.** An explicit declaration lists the agent-writable tables; a check fails
when a write path reaches a table outside it, and fails when the declaration itself names a table
carrying `raw_response_id` or `derivation_version_id`. *Why:* two halves because the negative test
alone is not the rule — F8. `accounts`, `institutions` and `connections` pass it and must not be
writable, so a bound stated only as "no provenance columns" would license an agent to move
`lifecycle_status`, which the balance-lifecycle norm makes arithmetic. This is AC-15.1's argument at
the table level: a permission that holds only while an unrelated fact stays true has been tested by
nothing. It is also F6's replacement — the security model records mass assignment as inapplicable
*because the surface is read-only*, and this criterion is what takes over that argument.

**AC-16.10 · The read surface stays structurally incapable of writing.** Whatever path carries an
annotation write, no handle held by the MCP server process is writable. *Why:* the amended norm's
structural half, stated as a criterion so a build cannot satisfy the norm's letter by opening the
handle it was protecting. `PRAGMA query_only=OFF` re-enables writes on a read-write handle
*(measured, `security-model.md`)*; the refusal has to live in the file handle.

**AC-16.11 · A write that cannot take the writer lock fails as retryable, and never blocks a read.**
An annotation write attempted while a sync, a backup or a rebuild holds the exclusive lock returns a
distinguishable retryable failure; it does not wait unboundedly, and it does not degrade any read
the surface is serving. *Why:* the writer factory takes an exclusive `flock` before it returns
(`architecture.md` § Direction), and syncs and rebuilds are long. Without this, the first annotation
attempted during a nightly sync either hangs the session or fails as an unexplained error — and the
CLI's `75`/`EX_TEMPFAIL` precedent shows this store already distinguishes "could not run" from "ran
and failed" where a machine consumes the result.

---

## Sequencing — why this lands after production onboarding

The owner proposed it; this document agrees, and records the reason so it is a decision rather than
a preference.

**The requirement's shape depends on evidence that does not exist yet.** What agents actually want
to remember about a real store is currently a guess — the subject list in requirement 1 is three
plausible types, not an observed distribution, and § *out of scope* defers a typed-annotation schema
precisely because nothing yet says what the kinds are. A month of real sessions turns "agents want to
persist learnings" into a list of notes someone can read, and turns the subject list into evidence.

**And the write surface should not widen during onboarding.** Onboarding is the window in which the
store first holds real financial data and the operator is establishing whether its answers can be
trusted. Widening the surface that can mutate it in that same window means any anomaly has two
candidate explanations instead of one.

**Two things are worth doing before then, and both are cheap:**

1. **Let #48's AC-15.4 check ship first.** It is the check that will protect annotations later — the
   one that fails when a declaration and the schema disagree. Landing it before annotations exist
   means annotations arrive into a store that already enforces the contract, rather than the contract
   chasing them.
2. **This document, and the norm amendment.** Both are on the critical path for nothing and unblock
   the item whenever the owner picks it up. The amendment in particular is worth landing while the
   argument is fresh: it is a ruling on a red-line norm, and a ruling reconstructed months later from
   a backlog comment is a ruling nobody can check.

---

## Assumptions, vetoable

**A1 · Annotations are free text, not a typed or structured record.** Taken from the owner's framing
("notes"). *Retires if vetoed:* nothing directly, but a typed annotation reopens AC-16.6 — a *typed*
value is one an aggregate can plausibly read, and the prohibition would need restating in terms of
which types may never be arithmetic.

**A2 · The annotatable set is account, security and transaction — and not `holdings` or
`balances_daily`.** Derived in F5 from the schema's own snapshot semantics rather than stated by the
owner, whose word was "an investment". *Retires if vetoed:* if the owner means a *position* rather
than a security, AC-16.1's key clause needs a third form and AC-16.4's orphan case becomes the common
case rather than the exception, because tomorrow's snapshot is a different row by design.

**A3 · One operator, one trust level — an agent's annotation and the operator's are the same class of
object, distinguished by an author field rather than by permission.** Rests on `security-model.md`
§ Authorization ("one operator, one role"). *Retires if vetoed:* nothing in AC-16.1–16.8; it would
add a criterion, since "an agent may not overwrite the operator's note" is a rule this document does
not currently impose.

**A4 · No annotation exists in any store today, so no migration is owed.** The table does not exist.
*Retires if vetoed:* nothing.

---

## Requirements confidence

**High** on F1–F5, F7 and F8. Each was read off the tree during this pass and is a single-file check a
reviewer can re-derive: `backup.py`'s `VACUUM INTO`, `rebuild.py`'s `RAW_PROVENANCE_COLUMN` delete
predicate and `content_digest`'s loop over `metadata.sorted_tables`, `core_schema.py`'s two surrogate
primary keys and the `holdings` composite key.

**High** on AC-16.1, AC-16.2, AC-16.3, AC-16.5 and AC-16.6. These state properties the store either
already has structurally or demonstrably lacks, and each names the observation behind it.

**Medium** on AC-16.4. The criterion is right; *when* the reconciliation runs is not settled here —
a check inside `store verify`, a step at the end of a rebuild, and a warning on the read path are
three different products, and the cheapest one that satisfies the criterion is a build decision.

**High** on AC-16.9's declaration half, which is F8 and is a single-file read. **Medium** on its
enforcement half: what "a write path reaches a table" means is precise under Option 1, where the
writer is a separate program with one reachable statement set, and vaguer under Option 2, where it
becomes a property of application code rather than of the process. A build that takes Option 2 should
expect to sharpen this criterion or record that it cannot.

**Medium** on AC-16.11, which is inferred from the lock's behaviour rather than from an observed
failure. Nothing has yet attempted a write while a sync holds the lock, because nothing but the sync
writes.

**Not measured:** whether the out-of-process write of Option 1 can be invoked from inside the MCP
server without violating `architecture.md`'s constructor norm (`connection.py` is the sole opener of
a handle) — the writer is a different process, so the norm is satisfied trivially, but *how* the
server invokes it (subprocess, entry point, socket) is unexplored and is the most likely place for
the build to be harder than this document makes it sound.

---

## Shared-artifact deltas for the integrator

1. **`docs/system-requirements.md`** — a new **FR-11 · Agent annotations** in § 4 after FR-10,
   carrying AC-16.1–16.11; and § 5's `Read-only. No mutation tools. No exceptions.` amended, since
   § 5 is the tool surface's contract of record.
2. **`.prawduct/artifacts/api-contract.md` § Direction** — the read-only norm's entry carries the
   amendment, `Status: in-transition` with this item as its tracking ref and an interim rule.
3. **`.prawduct/artifacts/project-preferences.md`** — the norm-index row for the read-only norm
   restated to match the amendment.
4. **`.prawduct/artifacts/security-model.md`** — § Authorization's mass-assignment row (F6) and the
   `mode=ro` paragraph, which quotes the pre-amendment § 5 sentence verbatim.
5. **`.prawduct/artifacts/data-model.md` § Direction** — a `Rulings:` line on *a transaction is
   never hard-deleted* and on *a source value is never overwritten in place*, pointing at the
   boundary ruling AC-16.7 rests on: neither norm governs a row the store itself authored.
6. **`.prawduct/learnings.md`** — that ruling, recorded at the category level so the next case at the
   same edge is pre-decided (*A store-authored row is not a record of what anyone said*).
7. **`.prawduct/artifacts/architecture.md`** — its `mode=ro` entry quotes the pre-amendment § 5
   sentence and calls the guarantee structural; it stays structural, and the entry now says why.
8. **`.prawduct/artifacts/discovery-operator-owned-columns.md`** — its § *Design* shape note for the
   annotation field is superseded by F7; **A1 is vetoed** by the same ruling that made this document
   necessary, and A3 stands.
9. **The tracker** — filed as **brookstalley/bankmachine#87** at `stage:ready`, `refs:` this
   document, sequenced after production onboarding per § *Sequencing*. It is also the tracking item
   the amended norm names while it is `in-transition`.
