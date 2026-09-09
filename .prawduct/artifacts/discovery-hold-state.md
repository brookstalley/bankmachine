# Discovery — Five states, three columns, and which shape should own them

**Work cycle:** production-blocker findings · chunk 03 · medium · design discovery
**Opened:** 2026-09-09
**Addresses:** brookstalley/bankmachine #53 (`store: hold state is rebuilt by hand at six call sites`)

---

## 🔴 Status: PROPOSED. Nothing here is ratified, and nothing implements it this cycle.

This document is a **derivation and a recommendation**, not a decision. It is not ratified by having
been written. `build-plan-production-blocker-findings.md` § *What this plan does NOT close* states
that `#53` ends this cycle with a written recommendation and nothing built; that is still true after
this document exists.

Two things in particular are the owner's and not this document's:

- **Which shape is adopted.** The recommendation in § *Recommendation* is argued, not ruled.
- **Whether the state model is declared TOTAL.** § *The states* shows the real state space is eight
  combinations and the item names five. Naming the other three is a product ruling with a
  reachability question attached that the tree cannot answer (§ *What the tree could not tell me*).

Every claim about the tree below is cited to a symbol and a line and was read at `1d888ec`. Every
claim about intent is cited to the artifact or criterion that holds it. Where I inferred, the
inference is in § *Assumptions, vetoable*.

---

## The three questions

**What problem are we solving?** A transaction's hold state has no single production. The condition
that distinguishes an outstanding hold from an expired one from a row that settled out of one is
spelled at several sites in `query.py`, each from the raw columns, and the sites can disagree — they
already did, in production code, and the two surfaces published contradictory answers about the same
row.

**What does success look like?** A recommendation, priced against the sites that actually exist, that
a future implementer can build without re-deriving the trade-off — including an honest statement of
what the recommended shape does *not* cover, so the residue is inherited rather than rediscovered.

**What is out of scope?** Building it. Any wire change (`#53` scopes that out and this document
respects it). Any schema change — the three columns are right. The two riders parked on `#53` in
§ *Also rides here* (the duplicated latest-balance subquery; the narrating comment, which chunk 00
already deleted at `2700559`). Nothing here retires either rider.

---

## Premise verification — what `#53` claims, and what the tree says

The brief for this chunk instructed enumeration against the tree rather than against the issue,
because this repo's issue bodies have been wrong in a load-bearing way repeatedly. Five claims were
checked. **One is materially incomplete, one is a category error with real design consequences, one
is defensible only under a counting rule the issue never states, and two hold.**

### Claim 1 — "six call sites". Defensible, but the rule is missing, and the rule changes the design.

The issue says the state is reconstructed "at six call sites, each spelling the condition by hand"
and never says what counts as a site. Three rules are available, and they give three answers:

| Counting rule | Count | The sites |
|---|---|---|
| Spells a **multi-column** hold condition itself | **3** | `_stranded_holds`, `_hold_transitions.expired`, `_hold_transitions.settled` |
| …plus sites that read **one** hold column with hold meaning | **5** | + `list_transactions`' page tally, `money_summary`'s `CASE` pair |
| …plus the shared liveness constructor and the write path | **7** | + `_transaction_filters`, `derivers._write_transaction`/`_mark_removed` |

Six is reachable — five read sites plus the deriver, or five plus `_transaction_filters` — but it is
not uniquely reachable, and **which rule you pick determines which shape wins**, because the two
single-column sites are exactly the ones a predicate constructor cannot serve. The issue's own
"Proposed change" implies the deriver is in scope (*"touching the deriver, both aggregates and the
verification surface together"*), which makes six the deriver-inclusive reading.

🔴 **The count is not the load-bearing correction.** It is defensible and I am not treating it as an
error. Claims 2 and 3 are the corrections.

### Claim 2 — "five states are in play". Materially incomplete. The space is eight, plus a refinement.

Three independent columns give **eight** combinations, not five. The item names five of them, and the
three it omits are not exotic — one of them is the majority of every row in the store.

| # | `pending` | `removed_at` | `source_pending_…` | The item's name | Who reads it |
|---|---|---|---|---|---|
| 1 | 1 | NULL | NULL | outstanding hold | `money_summary` `CASE` (2013/2021); `list_transactions` (1441); `_stranded_holds` (with the cutoff) |
| 2 | 1 | NULL | NOT NULL | *(unnamed)* | falls into "outstanding" at all three sites above, silently |
| 3 | 1 | NOT NULL | NULL | expired hold | `_hold_transitions.expired` (1251) |
| 4 | 1 | NOT NULL | NOT NULL | *(unnamed)* | falls into "expired" at 1251, silently |
| 5 | 0 | NULL | NOT NULL | settled from a hold | `_hold_transitions.settled` (1256–1257) |
| 6 | 0 | NULL | NULL | *(unnamed — the ordinary posted row)* | every live read in the file, as the residue |
| 7 | 0 | NOT NULL | NOT NULL | withdrawn settled row | **nothing** |
| 8 | 0 | NOT NULL | NULL | withdrawn settled row | **nothing** |

Three findings follow, and all three bear on the shape choice:

**(a) State 7/8 has no reader at all.** Nothing in `src/` selects `removed_at IS NOT NULL AND
pending = 0`. It exists only as the thing `_hold_transitions.expired` must *not* catch, and the
reason is recorded in place at `query.py:1242–1246`: *"a later removal of the settled row carries
`pending = 0` and is an ordinary withdrawal rather than a hold that never became anything."* That
exclusion is deliberate and correct under AC-13.4, which attributes hold transitions, not ordinary
withdrawals. It is pinned by
`test_a_settled_row_that_was_later_withdrawn_is_not_reported_as_an_expired_hold`
(`tests/test_pending_semantics.py:325`). **A state with a test and no reader is a constraint, not a
state** — and a model that must give it a name is being made to carry weight it does not have.

**(b) State 6, the ordinary posted row, is unnamed by the item and is the largest population.**
Every model that is total must name it. Every model that is not total leaves it as the residue,
which is what today's code does.

**(c) States 2 and 4 are of unknown reachability and are currently absorbed silently.** A row that is
`pending = 1` *and* carries a link would be counted as outstanding by every site that asks
`pending = 1`, and as expired by `_hold_transitions.expired` once removed. Nothing distinguishes
them. Whether the aggregator can produce one is not determinable here — see § *What the tree could
not tell me*.

### Claim 3 — "stranded hold: the same, plus `posted_date < cutoff`". A category error, and it is decisive.

Stranded is **not a state of a row.** It is a time-parameterised refinement of state 1, and the time
is not on the row:

- `_stranded_cutoff(today)` (`query.py:1085–1096`) takes `today` and returns a boundary.
- `coverage_report` states the discipline in place at `query.py:1593–1596` — *"One 'today' for every
  row. Reading the clock per account would let a report straddle midnight and hand back rows measured
  against two different days"* — and reads the clock at 1597 for the row loop.

🔴 **And the discipline is already half-broken by the split, which is evidence rather than a
tangent.** `coverage_report` reads the clock **twice** in one call: at 1565 for `_stranded_holds`, and
again at 1597 for the row loop that the "one today" comment governs. `days_pending` is measured
against the first and `silence_ratio` against the second, 32 lines apart. Nothing observable turns on
it today, but it is the same *two producers of one fact* family as the defect `#53` exists to fix,
arriving on the parameter rather than on the columns — and it is a hazard **only** because stranded is
clock-parameterised. A model that hides the clock inside a per-row value makes this harder to see, not
easier.

So an unchanged row is outstanding on day 29 and stranded on day 31. Any per-row enum must therefore
either take `today` as an argument — making "the state of this row" a function of when you ask,
which is a different kind of thing from the other four values and is a trap the next reader will
fall into — or omit `stranded`, in which case `_stranded_holds` still spells one third of its
condition by hand and **the issue's own second acceptance line is not met**:

> `_stranded_holds`' invariants (pending, not removed, past cutoff) come from that definition rather
> than being re-listed.

An enum can supply two of those three invariants. A predicate constructor can supply all three,
because a predicate is already allowed to close over a parameter.

### Claim 4 — the production failure's mechanism. Holds, and the tree records the corrective too.

The issue's account matches the record. `_stranded_holds` took its predicates from the caller;
`coverage_report` omitted the soft-delete clause; every long-expired hold was published as
outstanding while `money_summary` published the same row as expired.

🔴 **What the issue does not say, and what changes the pricing: the fix that landed was
DE-parameterisation, not abstraction.** `_stranded_holds`' docstring now says (`query.py:1119–1131`):

> **Takes no caller-supplied predicates, deliberately.** A stranded hold is `pending`, is not
> removed, and is older than the cutoff. None of the three is a caller's to choose, so none of them
> is a caller's to forget.

And in the `where()` itself (`query.py:1140–1151`): *"The guarantee belongs where it cannot be
forgotten."*

**The shape that failed was a predicate-constructor idiom.** A producer took a `filters` list from
its caller; the caller assembled the wrong list. That is the failure mode of parameterised
predicates, and it is a real mark against Shape A that Shape A's proponents (including the issue's
own "Proposed change" sentence) do not price. § *Shape A* names the constraint that answers it.

### Claim 5 — "these conditions are aggregated rather than iterated". True at three sites, false at two.

This is the recorded prior opinion the brief instructed me to test rather than confirm. Site by site:

| Site | Aggregated or iterated | Evidence |
|---|---|---|
| `_hold_transitions.expired` (1246–1252) | **Aggregated** | `count()` + `sum()` grouped by currency; rows never materialise. Counts rows every other statement excludes, so no arithmetic over an answer's rows could reach it (`query.py:1208–1212`). |
| `_hold_transitions.settled` (1253–1259) | **Aggregated**, and index-bound | Same shape; and the predicate must reach the `WHERE` literally for SQLite to plan against `transactions_pending_link` (AC-13.6). |
| `money_summary` (2005–2024) | **Aggregated**, hard | `SUM(CASE WHEN pending = 1 …)` in the same grouped pass as the money figures, with the reason recorded in place: *"has to be a property OF this row rather than a second count taken a moment later, which could disagree with the very number it is supposed to qualify."* |
| `_stranded_holds` (1109–1170) | **Mixed** — SQL-filtered, Python-iterated | *"Row-level rather than a count"* (1112). The predicate is SQL; the result is a list the caller groups by account (`query.py:1568–1570`) and `_oldest_stranded` reads positionally. |
| `list_transactions` (1439–1450) | **Iterated. The claim is false here.** | A literal `for row in rows: if not row["pending"]: continue` over the page already returned — and it must stay that way: *"Tallied from the rows THIS PAGE returned, not from `matching`"* (1434–1438). It cannot move into the count query without becoming a false statement about the payload. |
| `derivers._write_transaction` (515–586) | **Neither** | It assigns the columns from the source payload. No condition is evaluated. |

**Verdict: the prior opinion holds at three of six sites, fails at two, and does not apply at one.**

🔴 **And its premise is falsifiable in a second way that matters more than the count.** The opinion
assumes "enum" means "a Python value computed after the fetch." This file already contains a derived
per-row enum computed **in SQL** and returned as a column: `_flow_class()` (`query.py:1767–1800`) is
a `case()` yielding `internal_transfer` / `debt_service` / `external_spend`, selected as a labelled
column and grouped by at `query.py:2033`. So "derived enum" and "aggregation stays in SQL" are not
mutually exclusive in this codebase, and the prior opinion as stated does not by itself decide the
question. That variant is priced separately as Shape B2 below, because it is genuinely different from
both of the item's two shapes and it is the strongest available case for the enum family.

---

## The sites, in full

For the avoidance of a later "you missed one", the complete inventory at `1d888ec`.

**Reconstruction sites (read path), `src/bankmachine/query.py`:**

1. **`_stranded_holds`** (1109–1170) — `pending = 1` ∧ `removed_at IS NULL` ∧ `posted_date < cutoff`.
   Declares all three itself, takes nothing from the caller. Unwindowed; store-wide.
2. **`_hold_transitions.expired`** (1246–1252) — `_transaction_filters(include_removed=True)` ∧
   `removed_at IS NOT NULL` ∧ `pending = 1`. Windowed, aggregated per currency.
3. **`_hold_transitions.settled`** (1253–1259) — `_transaction_filters(…)` (supplies
   `removed_at IS NULL`) ∧ `source_pending_transaction_id IS NOT NULL` ∧ `pending = 0`.
4. **`money_summary`'s pending pair** (2012–2024) — `SUM(CASE WHEN pending = 1 …)` twice, under
   `_transaction_filters` at 2032.
5. **`list_transactions`' page tally** (1358 select, 1377 render, 1439–1450 loop) — reads `pending`
   per row in Python, under `_transaction_filters` at 1342.

**The shared half:** `_transaction_filters` (1003–1051) supplies `removed_at IS NULL` — or, with
`include_removed=True`, withholds it — to sites 2–5 and to two non-hold readers (`_covered_rows` at
702, and `list_transactions`' own `matching` count). Sites 1 and 2 assert the removal clause
themselves; sites 3, 4 and 5 inherit it. **That asymmetry is the live hazard**: the invariant arrives
two different ways and only one of them is un-forgettable.

**Write path:** `derivers._write_transaction` (515–586) sets `pending` and
`source_pending_transaction_id` from the payload and **clears `removed_at`** on a re-send (578–582);
`derivers._mark_removed` (619–639) sets `removed_at` under a `removed_at IS NULL` guard.

**Downstream consumers that do NOT reconstruct** — listed so they are not mistaken for sites:
`_pending_caveat` (1263), `_oldest_stranded` (1496), `coverage_report`'s grouping (1564–1566),
`_flow_class_totals`' pending sums (1855–1856), `money_summary`'s per-currency regrouping
(2051–2062). Each reads a figure another site produced.

**Seven further hand-spelled `removed_at IS NULL` clauses** carry no hold meaning — liveness only:
`query.py` 198, 227, 364, 1576, 1588 and `signs.py` 257, 308. They are out of scope for a hold-state
model and should stay out; folding them in would make the model a liveness model wearing a hold
model's name.

**Tests are not sites.** Every hold fixture goes through the real derivers from the wire shape
(`tests/test_pending_semantics.py:11–15`), and only 13 lines in the whole `tests/` tree reference
these columns directly. A guard scoped to `src/` therefore has a clean blast radius.

---

## The two shapes, priced

### Shape A — SQL predicate constructors, in the style of `_transaction_filters`

One function per named question, each returning a complete condition.

**What it costs.** One new block in `query.py` (or a small module) of perhaps four constructors and
their docstrings; five call sites edited; no behaviour change; no wire change; no migration.

**Where the evidence supports it:**

- **It fits the three aggregating sites exactly**, and those are the three that cannot move to
  Python without a real regression. `_hold_transitions.expired` counts rows every live statement
  excludes; pulling them into Python means fetching every removed row in the window.
- 🔴 **It is the only shape that preserves AC-13.6's measured index behaviour.**
  `test_the_pending_link_index_serves_the_reader_that_exists`
  (`tests/test_pending_semantics.py:518–560`) asserts two things: that the literal substring
  `source_pending_transaction_id IS NOT NULL` appears in **exactly one** executed statement, and
  that `EXPLAIN QUERY PLAN` names `transactions_pending_link`. A constructor emits that expression
  into the `WHERE` unchanged and both assertions survive. Any shape that moves the predicate out of
  the `WHERE` loses the partial index and the criterion with it.
- **`stranded` is expressible.** A constructor may take `today` (as `_stranded_cutoff` already
  does), so all three of `_stranded_holds`' invariants come from the definition — the issue's second
  acceptance line, met.
- **It does not force totality.** Four constructors for four questions; states 2, 4, 6, 7 and 8 stay
  unnamed rather than named wrongly. Given that AC-13.8 is still open and
  `query.py:1076–1080` records that **no pending row has ever reached this datastore**, declaring a
  total model now is fitting an abstraction to zero production samples — precisely the failure
  `.prawduct/learnings.md` § *A repeated declaration is cheaper than the model standing in for it*
  warns about.
- **The repo's guard idiom fits it** (§ *The guard*).

**What it costs that its proponents do not say:**

- 🔴 **This is the shape that already failed.** The production defect was a producer taking a
  `filters` list from a caller who assembled it wrong. More constructors are more things a caller
  can combine incorrectly or omit. **The constraint that answers this, and it must be written into
  the design rather than left to discipline:** each constructor returns the **complete** condition
  for one named question — including its own removal clause — and is never a fragment a caller
  assembles from parts. `_hold_transitions.expired` already has this shape; `_stranded_holds` was
  moved to it as the fix. A constructor library that ships composable fragments reproduces the
  original defect with better naming.
- **It does nothing for site 5.** `list_transactions` classifies rows already in hand; there is no
  `WHERE` to put a predicate in. A predicate constructor leaves that site reading `row["pending"]`
  exactly as it does today. Any claim that Shape A "gives the states one production" is false at one
  of five read sites, and saying so is not a concession — it is the residue a future implementer
  inherits.

### Shape B — a derived per-row enum, computed in Python

**Where the evidence supports it:**

- **It fits the two row-level sites natively.** Site 5 already carries the boolean to the wire and
  iterates it; site 1 iterates its results.
- **Exhaustiveness becomes statically checkable** — a closed set plus `assert_never` is verified by
  mypy at zero test cost, which is a stronger and cheaper guarantee than any source scan.

**What the evidence costs it:**

- **It moves three aggregations out of SQL.** Verified true at sites 2, 3 and 4 — and site 4's
  docstring gives the reason it must not move: computing the pending pair in a second pass "could
  disagree with the very number it is supposed to qualify."
- **It loses `transactions_pending_link`** and takes AC-13.6's measured test with it.
- 🔴 **It must be total, and totality is the problem.** The five named states cover five of eight
  combinations. A Python enum must either name all eight — including 7/8, which nothing reads, and
  2/4, whose reachability is unknown — or carry a catch-all. **A catch-all is "a state the model
  does not define", reintroduced inside the model**, which defeats the issue's own third acceptance
  line.
- **It cannot hold `stranded`** without becoming a function of the clock (Claim 3), so
  `_stranded_holds` keeps spelling one invariant by hand and acceptance line 2 fails.

### Shape B2 — a derived enum computed in SQL, in the style of `_flow_class()`

Priced separately because it is the strongest form of the enum family and neither the issue nor the
prior opinion considers it. `_flow_class()` proves the pattern works here: a `case()` producing a
labelled string column, selected and grouped by.

**Where it wins over B:** aggregation stays in SQL entirely, so the prior opinion's objection
evaporates. The `else_` branch makes the residue explicit rather than silent — an honest answer to
the totality problem. It is a real precedent in the same function, not an import from elsewhere.

**Where it still loses:**

- **The index.** `_flow_class` is safe because it is a *projection* grouped on, never a predicate.
  A hold-state `CASE` used the same way turns `_hold_transitions.settled` from
  `WHERE source_pending_transaction_id IS NOT NULL` into
  `SUM(CASE WHEN state = 'settled_from_hold' …)` over the whole window. SQLite does not use a partial
  index for a predicate buried in a select-list `CASE`, so the plan assertion goes red — and the
  substring assertion's `== 1` may go red too, since the expression would now appear wherever the
  `CASE` is selected. That is not a stylistic objection: it is a measured criterion with a test
  behind it.
- **`stranded` still needs the clock**, so acceptance line 2 still fails.
- **Site 5 is still not served** — the page tally is over rows already returned, so a SQL column
  helps only if it rides the row, which is a wire change the issue scopes out.

---

## The guard: "a call site cannot express a state the model does not define"

The item's third acceptance line. It is **reachable under every shape**, by the same mechanism, and
this repo has two working precedents:

- `tests/preferences/test_the_warning_vocabulary_is_closed.py` — an `ast` walk over `src/` finding
  every construction site, checking each against a closed vocabulary, reporting non-literal sites
  separately *"because the scan can prove nothing about them"*, and driving the scanner over a
  known-bad module as a positive control.
- `tests/preferences/test_the_envelope_reaches_no_datastore.py` — an **allowlist** rather than a ban
  list, with the reason stated: *"A guarantee defined by an enumeration decays; one defined by what
  is PERMITTED has no case to fall outside of."*

**The guard for `#53`, in that idiom:** `transactions.c.pending` and
`transactions.c.source_pending_transaction_id` may be referenced only from the module that defines
the model, plus an allowlisted `connector/plaid/derivers.py` (which must assign them). Any other
reference in `src/` fails. Thirteen direct references exist in `tests/`, none in `src/` outside the
sites above, so the guard is achievable today rather than after a cleanup.

**What each shape adds beyond that shared scan:**

| | Shape A | Shape B | Shape B2 |
|---|---|---|---|
| Provenance scan (columns appear in one place) | yes | yes | yes |
| Static exhaustiveness (`assert_never`) | no | **yes** | no (the `else_` is the residue) |
| Catches a caller composing two definitions into an undefined one | no | no | no |

🔴 **No shape catches the third row, and that is worth stating plainly rather than letting the
acceptance line imply otherwise.** A caller can write `and_(outstanding(), removed())`, or
`if state in (A, B)`, under any of them. The guard that is genuinely reachable proves the columns
have one production; it does not prove the *combinations* are closed. An acceptance line read as
promising the latter will be read as met when it is not.

The one asymmetry that is real: **Shape B's exhaustiveness check is free and static**, where A's is a
test. That is B's best argument and it is not a small one.

---

## Recommendation

🔴 **Shape A — whole-condition SQL predicate constructors — with three named constraints.**

Not because the enum family is weak; B2 in particular is a legitimate design with a working
precedent in the same file. The recommendation rests on evidence that is specific to this tree:

1. **AC-13.6 is a measured criterion with a plan assertion behind it**, and only Shape A leaves the
   predicate where SQLite can use the index. Both enum variants trade a criterion that is currently
   green for a design property. That is the single hardest fact in this document.
2. **Only Shape A can express `stranded`**, so only Shape A meets the item's second acceptance line.
   Both enum variants leave `_stranded_holds` re-listing an invariant.
3. **Three of five read sites aggregate in SQL, and one of them must** (site 4's "same pass" reason).
   Shape A is a no-op for them; Shape B is a regression; B2 is neutral-to-negative on the index.
4. **It does not require declaring the state space total** while AC-13.8 is unmet and no pending row
   has ever reached this datastore. The learning this repo recorded on 2026-09-09 says collapsing
   early produces "an abstraction shaped by an unrepresentative sample, which is more expensive to
   undo than the duplication it replaced." Shape A names four questions; the enum shapes must name a
   universe.

**The three constraints, which are part of the recommendation and not commentary:**

- **(i) Every constructor returns a complete condition**, including its own removal clause. No
  fragments, no `filters` list assembled by a caller. This is the de-parameterisation that actually
  fixed the production defect (`query.py:1119–1131`), stated as a rule rather than re-earned.
- **(ii) Site 5 is out of scope and is said so out loud.** `list_transactions`' page tally keeps
  reading `row["pending"]`. It is a single column with a single meaning, it must stay a Python
  iteration over the page (1434–1438), and pretending the model covers it would be the "silently
  dropped requirement" failure. If it should be covered, that is a separate decision with a wire
  question attached.
- **(iii) The provenance guard ships with the constructors, not after them**, in the allowlist form,
  with a positive control — and its docstring states what it does *not* prove (§ *The guard*).

### The strongest argument against this recommendation

**The shape being recommended is the shape that failed in production.** A predicate-constructor
library is a library of things a caller assembles, and the recorded defect was a caller assembling
one wrongly. Constraint (i) is the answer, but constraint (i) is a *convention* — nothing in the type
system stops a future author from adding a fragment-shaped helper beside the whole-condition ones,
and the provenance guard would not notice, because the fragment would live in the allowlisted module.
Shape B's static exhaustiveness has no equivalent weakness: it is checked by the compiler, not by a
reviewer's memory of a rule.

I do not think this outweighs the index and `stranded` findings, but it is not a small objection and
it is the one I would expect the owner to press on.

### What would flip this recommendation

- **AC-13.6 is retired or `transactions_pending_link` is dropped.** The index argument is the
  heaviest weight here; without it, B2 becomes close to a tie with A and its `else_`-branch honesty
  about the residue is attractive.
- **`stranded` moves off the row** — e.g. onto a stored `expires_after` — at which point the clock
  dependency disappears and an enum can hold all five values.
- **Production data shows states 2 and 4 are reachable.** Silent absorption of an unnamed state into
  a named one is exactly what an enum's totality prevents and a predicate library does not; if those
  rows exist, the exhaustiveness argument gets much stronger.
- **A fourth aggregating site does not arrive, but two more row-level ones do.** `find_recurring` is
  specified and unbuilt (`api-contract.md` § *MCP tool surface*, three of eight not built) and must
  decide whether an outstanding hold is an occurrence; `money_summary`'s specified-and-unbuilt
  period-over-period comparison would double the aggregating sites. The balance of the next two
  sites is the thing to watch, and it is the trigger the learning asks to be named.

---

## What the tree could not tell me

- **Whether states 2 and 4 are reachable** — a `pending = 1` row that also carries
  `source_pending_transaction_id`. `api-notes-plaid.md` §16 records that `pending_transaction_id`
  exists on the transaction body and §17 lists it among the measured fields, but neither says whether
  it can be non-null while `pending` is true. `derivers._write_transaction` (539–542) reads the two
  independently and would happily store the combination. This is an aggregator-behaviour question and
  wants either documentation or AC-13.8's real settlement.
- **Whether any of this survives contact with production data at all.**
  `query.py:1076–1080` records that no pending row has ever reached this datastore, and AC-13.8/13.9
  are still open. Every state above is derived from the write path and the criteria, not observed.
- **Whether SQLite would in fact abandon `transactions_pending_link` under a select-list `CASE`.**
  I did not run the suite (this chunk changes no behaviour and the brief sets the verification
  ceiling at none), so the B2 index claim is reasoning from how partial indexes are planned plus the
  existing test's shape — not a measurement. 🔴 **It is the single most load-bearing claim in the
  recommendation, and it is the one to verify first if the owner leans toward B2.** One
  `EXPLAIN QUERY PLAN` against a `CASE`-shaped rewrite settles it.
- **What `find_recurring` and `balance_history` will actually need**, since neither is built. I read
  their one-line contract rows only.

---

## Assumptions, vetoable

- `[ASSUMPTION: the state model is a READ-path concern and the deriver is a writer, so the deriver is not asked to produce an enum or consume a predicate | MED impact | user can correct]` — the issue's "touching the deriver" is read as *the deriver is in the blast radius of the guard*, not *the deriver classifies*. If the intent was a derived-and-stored state column, that is a schema change and the issue scopes schema changes out.
- `[ASSUMPTION: the seven liveness-only `removed_at IS NULL` clauses stay out of the model | MED impact | user can correct]` — folding them in would make this a liveness model, and `signs.py`'s two are in a module with a different unit of analysis (the feed, not the row).
- `[ASSUMPTION: no wire change means the enum cannot ride the row, so B2's state column would exist only inside SQL | HIGH impact on B2's pricing | user can override]` — if a `hold_state` field on the row is permitted, B2 gets considerably stronger and site 5 becomes servable.
- `[ASSUMPTION: "six call sites" is a description rather than a criterion, so a model that addresses five read sites and one write site satisfies the item | LOW impact | user can correct]`
- `[ASSUMPTION: constraint (ii) — leaving `list_transactions`' page tally uncovered — is acceptable | MED impact | user can correct]` — it is named rather than absorbed precisely so it can be vetoed. If it must be covered, the wire assumption above is the thing that has to move first.

---

## What this document does not decide

- It does not ratify a shape. § *Status*.
- It does not write or amend a criterion. AC-13.1–13.9 are untouched, and nothing here is a
  `## Direction` norm — `data-model.md` § Direction's *"a transaction is never hard-deleted"* is
  read as binding throughout and is not weakened anywhere above.
- It does not close `#53`, and it does not touch the two riders parked on it.
- It writes no code. No source, test, migration or existing artifact was modified in producing it.
