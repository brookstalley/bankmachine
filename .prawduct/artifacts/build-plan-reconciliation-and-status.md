---
artifact: build-plan
version: 2
scope: reconciliation-and-status
branch: feature/reconciliation-and-status
depends_on:
  - artifact: discovery-reconciliation-and-status
  - artifact: data-model
  - artifact: api-contract
  - artifact: api-notes-plaid
  - artifact: operational-spec
governed_by:
  - artifact: api-contract
    dispositions:
      - "MCP surface is read-only, no mutation tools → conforms; this adds a read-only field and opens no writable handle"
      - "freshness stamp, incompleteness rides the success path as a warning → conforms; two kinds added to a vocabulary the norm calls a minimum, which is the norm obeyed rather than amended"
      - "CLI three-way exit code, 1/2 not collapsible → conforms; `status` answers 0/1/2 and deliberately does NOT reuse 75, which means 'did not finish' and would misdescribe a completed check that found a problem"
      - "tool boundary drawn where answer SHAPE changes → conforms; the residual joins `get_coverage_report` because it is the same shape (per account) in the same domain (verification), not a new tool. 🔴 Binds the field design: `mcp._refuse_optional_row_fields` refuses a row field the definition does not require, so the investment case carries NULLS, never absence"
      - "stored balance reported with lifecycle; no total over balances without stating non-active treatment → conforms, and it INHERITS. A per-account residual is not a total, but any aggregate over residuals is, so every such figure carries a count AND a signed magnitude grouped by currency — matching `query._coverage`'s already-paid pattern, per `learnings.md`'s 'the magnitude is the load-bearing half, not the flag'"
  - artifact: data-model
    dispositions:
      - "every stored amount operator-signed → 🔴 AMENDMENT PROPOSED (Chunk 01). The norm's own Why cites 'AC-11.2's reconciliation is change in balance equals sum of transactions for EVERY account' as a load-bearing consequence. `api-notes-plaid.md` §23 measured that investment accounts cannot reconcile. The statement is untouched; its Why is corrected"
      - "integer minor units, no floats → conforms; the residual is integer minor units throughout"
      - "calendar dates and UTC instants distinct → conforms; interval bounds are CalendarDate"
      - "every silver row carries provenance and derivation version → inapplicable because this plan writes no row; it computes over existing ones"
      - "daily balance and holdings series append-only → conforms; read-only access"
      - "a source value is never overwritten in place → inapplicable because this plan writes nothing"
      - "a transaction is never hard-deleted → conforms; the interval sum excludes soft-deleted rows, as every other reader does"
      - "a migration's DDL is frozen → conforms; AC-11.2a edits a MODULE DOCSTRING, not `CORE_SCHEMA_DDL`. Verified: `CORE_SCHEMA_DDL_SHA256` hashes the statements, not the file"
  - artifact: architecture
    dispositions:
      - "every writable handle from the one writer factory → inapplicable because this plan opens no writable handle"
      - "read-role handles are mode=ro, hold no cross-call snapshot → conforms; both surfaces read through `reader_connection`"
      - "no component creates the datastore implicitly → conforms; `status` reads and never creates"
      - "a process that does not recognize the schema version refuses to serve → conforms; inherited from the reader factory's `_require_supported_schema`"
  - artifact: operational-spec
    dispositions:
      - "no filesystem path is hardcoded → 🔴 BINDS CHUNK 03. The expected-inventory file path is configuration with a documented default; a literal path would depart from this norm"
      - "a backup destination is never created implicitly and never overwritten → inapplicable because this plan writes no file"
partition: serial — Chunk 03 reads the producer Chunk 02 adds, and both extend `query.py`'s coverage path; there is no independent surface to hand out, and a delegate would be waiting on 02 for the whole of its brief
last_validated: 2026-09-16
---

## Requirements Confidence

**Level:** Medium

**Why:** The problem, success criteria and scope are each statable in one sentence, the data model
is unusually well documented, and three decisions that would otherwise be assumptions were ruled by
the owner on 2026-09-16 (the residual's home, a zero tolerance, the inventory's location). Medium
rather than High for exactly one reason, and it is a foreign-API semantic rather than a product
question — see the assumption below.

**Open assumptions / unknowns:**

- 🔴 **RESOLVED 2026-09-16, and FALSIFIED — the assumption was wrong.** It read:
  `[ASSUMPTION: the aggregator's reported `current` balance INCLUDES authorization holds, so a
  pending row belongs in the interval sum | HIGH impact]`. Settled from SDK source, which is the
  `verify-api` order of preference, not from docs or recall. `plaid/model/account_balance.py`
  documents `available` as, for depository accounts, *"the `current` balance less any pending
  outflows plus any pending inflows"* — arithmetic that only holds if `current` does **not** already
  net out pending. **So `current` is the settled balance and EXCLUDES pending**, and the interval
  sum must count posted rows only (`pending = 0`).
  Had this been built as assumed, every account with an open hold would have carried a spurious
  residual equal to that hold — on a feature whose entire value is that a nonzero residual means
  something. **Consequence for the design: the `pending_holds` cause largely dissolves.** With both
  sides excluding pending, a hold is not an ordinary residual cause at all; it was a category
  invented to absorb a mismatch that does not exist. Chunk 02 must not ship a branch that cannot
  fire — either it is dropped, or it is re-scoped to the narrow case that survives and that case is
  named.
- 🔴 **A second question the first one raised, and its answer.** If a settling transaction were
  dated by its *authorization*, posting would backdate a row into an already-reconciled interval and
  a clean residual would go nonzero later with no error anywhere. `plaid/model/transaction.py`:
  *"For pending transactions, the date that the transaction occurred; for posted transactions, the
  date that the transaction posted."* The date moves **forward**, never back, and `posted_date`
  mirrors that field (`plaid/derivers.py:893`). So a transaction enters the sum on the day it moves
  the balance, and closed intervals stay closed. 🔴 This holds **only because** pending rows are
  excluded — the two findings are load-bearing together, and changing either alone reintroduces the
  instability.
  Neither finding closes **#22 / VRF-038**, which is about watching one real pending row across
  settlement. They answer an adjacent question that obligation kept colliding with.
- `[ASSUMPTION: the reconciliation pairs CONSECUTIVE balance snapshots rather than a caller-supplied
  period | MED impact | user can correct]` — consecutive pairing needs no parameter and localizes a
  discrepancy to the interval that produced it.
- `[ASSUMPTION: the inventory file is TOML, matching `~/.config/bankmachine/config.toml` | LOW
  impact | user can correct]`

**What would raise confidence:** The HIGH-impact assumption is now resolved against SDK source, so
the level would be High but for the two that remain, both LOW/MED and both cheap to reverse. The
remaining open question is empirical rather than definitional: whether the production residuals
actually come back zero. That is Chunk 02's step 2 and cannot be answered before the code exists.

## Status

- [x] Chunk 01: The requirements, and the norm whose Why this cycle falsifies
- [ ] Chunk 02: The residual — one producer, reported on the verification surface
- [ ] Chunk 03: `bankmachine status`, and the inventory it checks against

Context: Chunk 01 closed 2026-09-16 — requirements written, the operator-signed norm amended, all
four overclaim sites corrected. Gate green against the committed tree; Critic `verify-resolutions`
returned 0 blocking / 0 warning / 0 note.

Next: **Chunk 02**, and its `verify-api` is already ANSWERED — `current` excludes pending, so the
interval sum counts posted rows only and the `pending_holds` cause is gone. Step 0 is still open
because the **record** is owed: `api-notes-plaid.md` needs the numbered section, carrying the
vendor's own "typically" hedge.

🔴 **This plan's `branch:` will not resolve after its PR merges, and that is a step someone has to
take.** The frontmatter declares `branch: feature/reconciliation-and-status`; that branch is merged
and deleted by the PR carrying Chunk 01, so the declaration then resolves for nobody and the plan
reads live-but-inactive — which is what RETAIN wants on gitflow, but it also means **Chunk 02 must
open a new branch and repoint `branch:` to it in the same commit.** Not automatic, and nothing
reports it: a plan claiming a branch that does not exist simply stops governing.

🔴 The suite is parallelised under brookstalley/bankmachine#44 (PR #132), which merges ahead of
Chunk 02 — so pull `develop` before starting, and a full gate run costs ~8.5 minutes rather than
~11. The larger cost is #131: about 61% of suite wall-clock is serialised macOS keychain I/O, which
`-n auto` cannot reach. Both are different scopes and neither belongs in this plan.

## Scaffolding

Not applicable — this is established work in a built product. `uv sync` is current, and
`bash scripts/check.sh {junit_xml}` is the gate (pytest, `ruff check`, `ruff format --check`,
`uv run mypy`).

🔴 **Chunk 01 is deliberately NOT a walking skeleton**, departing from the template's "first chunk
is a thin vertical slice" rule. That rule exists to prove the layers connect before widening the
path; here the layers are built, exercised by a green suite, and serving production. What is
*not* established is the requirement — a norm's Why currently asserts the opposite of what Chunk 02
implements. The risk this plan front-loads is the requirement risk, which is where it actually sits.

### Verification Strategy

Tests carry the arithmetic. Two things they cannot carry:

1. **What the production store actually returns.** The residual's expected value is zero; whether it
   *is* zero across two real institutions is the measurement this cycle exists to take, and it is
   the evidence the two docstrings have never had. Run `get_coverage_report` against production
   after Chunk 02 and record the residual distribution in the change-log entry.
2. **Whether the operator can read it.** `bankmachine status` is a new human-facing output surface,
   so Chunk 03 declares `Visual change: yes` and queues an operator-verification entry.

## Project Structure

No new top-level structure. The work lands in existing modules:

```
src/bankmachine/
├── query.py              # `_account_coverage`'s neighbour: the residual producer, ONE of it
├── envelope.py           # two warning kinds
├── mcp.py                # `get_coverage_report`'s outputSchema
└── cli/
    └── status.py         # new verb, reading the same producer
```

### Module Boundaries

🔴 **One producer, two readers — this is the plan's central structural constraint, and it is not a
tidiness preference.** `_account_coverage`'s own docstring states why: *"Computed twice they can
disagree, and a verification surface that contradicts the analysis surface is worse than one that
is missing."* The CLI calls the same function the MCP tool does. A second implementation over the
same tables is the failure this boundary exists to prevent, and it would pass every test written
against either one alone.

`cli/status.py` holds presentation only — no SQL, no second arithmetic.

## Build Chunks

### Chunk 01: The requirements, and the norm whose Why this cycle falsifies

- **Description:** Write the requirements before the code that implements them, and correct
  **every** site that asserts the reconciliation holds "for every account" when a measurement
  already in this repo says it cannot. The Deliverables below enumerate them and say how each one
  changes; one is a `## Direction` **norm's Why**, which makes that site a norm amendment with a
  recorded decision rather than a prose fix. No count here — re-derive the list by search, which
  is what the Deliverables instruct.
- **Depends on:** none
- **Artifacts consumed:** `.prawduct/artifacts/discovery-reconciliation-and-status.md` (the
  requirements as proposed), `.prawduct/artifacts/api-notes-plaid.md` §23 (the measurement)
- **Deliverables:**
  - `docs/system-requirements.md` — AC-11.2 amended with the investment exclusion and the zero
    tolerance; new AC-11.2a, AC-11.6a, AC-18.1–18.3. §7's AC-11.2 clause gains an amendment note in
    the voice of the AC-11.1 and AC-11.3 notes already beside it.
  - **Four** overclaim sites, and they split on the norms line — bind vs. track:
    - `.prawduct/artifacts/data-model.md:57` — the operator-signed norm's **Why**. **Normative**, so
      a recorded amendment carrying statement / why / retroactivity and a `[DECISION: …]`. 🔴 The
      norm's *statement* is untouched: the sign convention is not what measurement falsified. What
      is corrected is a consequence it claims, and the amendment must say so explicitly, because a
      norm amended in the same cycle as the code it governs is the laundering shape
      /prawduct:methodology norms warns hardest about — the defence is that the falsifying measurement (§23) predates this cycle
      and was recorded by other work.
    - `.prawduct/artifacts/data-model.md:776` — § Sign convention prose. Descriptive; tracks the norm.
    - `src/bankmachine/store/migrations/core_schema.py:40` — module docstring, **not** the frozen DDL.
    - `src/bankmachine/connector/plaid/derivers.py:1631` — the balance-row docstring, on
      `_write_balance`.
    🔴 Re-derive the site list with `grep -rn "change in balance equals sum"` before editing rather
    than trusting these four line numbers — lines shift, and the first pass of the discovery
    document said three sites and missed one.
- **Tests:** `tests/preferences/test_requirement_ids_unique.py` covers the new ids.
  `tests/store/test_schema.py` must stay green across the migration docstring edit — **the evidence
  that the edit did not touch frozen DDL**, and the reason this chunk carries a code path at all.
- **Acceptance criteria:** `bash scripts/check.sh` green; no AC id collides; every overclaim
  sites agree with §23; `CORE_SCHEMA_DDL_SHA256` unchanged.
- **Type:** doc-only
  <!-- Two source files are touched, but only their docstrings; no behaviour changes. The
       test-evidence check is waived on that basis, and test_schema.py still runs. -->
- **Done when:**
  1. Acceptance criteria met and tests pass
  2. `/prawduct:critic` run and blocking findings resolved
  3. Committed and chunk marked `[x]` in Status

### Chunk 02: The residual — one producer, reported on the verification surface

- **Description:** Compute AC-11.2's residual per account per consecutive-snapshot interval, and
  report it on `get_coverage_report`. The vertical slice: `balances_daily` + `transactions` →
  producer → envelope warning → tool schema → wire.
- **Depends on:** Chunk 01
- **Artifacts consumed:** `.prawduct/artifacts/discovery-reconciliation-and-status.md` § *Design*,
  `.prawduct/artifacts/api-contract.md` § *The warning vocabulary*
- **Deliverables:**
  - A residual producer in `src/bankmachine/query.py`, beside `_account_coverage` and sharing its
    one-producer discipline. SQLAlchemy Core, no ORM, per project preferences. Pairs each account's
    `balances_daily` rows with their predecessor; sums `transactions.amount_minor` over `(from, to]`
    over **posted rows only (`pending = 0`)** and excluding soft-deleted rows; attributes each
    nonzero residual to `coverage_gap` |
    `window_truncated` | `unexplained`. 🔴 `pending_holds` is NOT in this list: the verify-api
    finding above removed it. Both sides of the comparison exclude pending, so a hold produces no
    residual to explain. Re-add it only if measurement produces a case it is needed for.
  - 🔴 **An explicit `reconciliation_state`, because three distinguishable states would otherwise
    collapse into one null.** `learnings.md` § *A carve-out reaches every state that shares its
    return type*: "when one function collapses distinguishable states into one value, the collapse
    is the defect." A null `residual_minor_units` would mean *investment account*, *only one
    snapshot so far*, and *no balance ever recorded* alike, and a consumer could not tell an
    unreconcilable account from an unreconciled one. The state is its own required field —
    `reconciled` | `not_applicable_investment` | `insufficient_snapshots` | `no_balance_recorded` —
    and is never inferred from the residual being null.
  - Investment accounts excluded, **named**, never silently dropped.
  - `src/bankmachine/envelope.py` — `balance_unreconciled` and `reconciliation_not_applicable`.
    🔴 **Both kinds, their `_GUIDANCE` entries and their server-instructions rows land in ONE
    commit.** `learnings.md` § *A shared closed set is the collision parallel agents cannot see*: a
    kind emitted but not declared is refused by the schema validator, which takes the whole answer
    down rather than degrading it. Also decide and record each kind's scope — `REQUEST_SCOPED_KINDS`
    for both, since each fires only when this request's scope holds such an account, and the
    `rule-applied` amendment records what goes wrong when a kind sits in the wrong set.
  - `src/bankmachine/mcp.py` — the `get_coverage_report` row fields. 🔴 Every new field is
    **required and nullable**; `_refuse_optional_row_fields` refuses anything else, and an
    investment account's residual is `null` beside a state that says why, never absent.
  - `.prawduct/artifacts/api-contract.md` — both kinds in the vocabulary table, the row-shape table,
    and any count the contract test reads.
- **Tests:**
  - unit — an account whose transactions bridge two snapshots exactly (residual `0`); one whose do
    not, asserting the magnitude **and** the attributed cause; each cause in the list above reaching
    its own branch; a liability account, proving the single sign convention makes the arithmetic
    uniform (this is the norm's Why under test for the first time); an investment account yielding
    `null` beside `not_applicable_investment`, with the warning raised.
  - 🔴 **each `reconciliation_state` distinctly** — the three non-`reconciled` states must be
    separable from one another, not merely from `reconciled`. This is the test that would catch the
    collapse the state field exists to prevent.
  - 🔴 **property-based (hypothesis), per project preferences for money arithmetic and
    invariants** — over a generated ledger: the residual equals zero whenever the transactions in
    an interval are exactly the balance movement, for any account type, any sign, any number of
    snapshots. `learnings.md` § *Guarantees by construction*: **assert the invariant, not
    enumerated cases** — the window-resolver instance found a bug nine enumerated mutations missed,
    and this is the same shape. The invariant is the whole feature.
  - 🔴 **sparse snapshots** — an interval spanning many days because the aggregator reported a null
    `current` in between (`plaid/derivers.py`'s `reported is None` path leaves no row). A missing day
    is not a defect and must not read as one.
  - integration — `get_coverage_report` end to end; the row schema accepted by
    `_refuse_optional_row_fields`; `tests/preferences/test_the_documented_tool_surface_is_the_built_one.py`
    green.
  - 🔴 Every fixture asserts it reached the subject before asserting about it —
    `learnings.md`: "a fixture that can't reach the subject passes forever," and a reconciliation
    over zero paired intervals is green by vacuity.
- **Acceptance criteria:** `bash scripts/check.sh` green; `get_coverage_report` carries a residual
  per non-investment account with an attributed cause; investment accounts are present, null, and
  named in a warning.
- **Foreign API:** plaid-accounts-balances — whether `balances.current` includes authorization holds
- **Done when:**
  0. verify-api — 🔴 **ANSWERED 2026-09-16, RECORD STILL OWED.** The finding is in § Requirements
     Confidence: `current` excludes pending, established from SDK source
     (`plaid/model/account_balance.py`, `plaid/model/transaction.py`). What is NOT done is writing
     it into `.prawduct/artifacts/api-notes-plaid.md` as a numbered section beside §23 and §26,
     which is where this project keeps measured aggregator behaviour and where the next reader will
     look for it. A finding that lives only in a build plan is one that disappears when the plan is
     archived. **This step is not discharged until that section exists.**
     🔴 Record the hedge with it: the SDK says `available` *"typically"* equals current less pending
     outflows plus pending inflows. "Typically" is the vendor allowing for institutions that differ,
     so the exclusion is well-evidenced rather than guaranteed. The direction chosen fails safe —
     an institution that behaves otherwise produces a visible residual rather than a silent wrong
     number — and step 2's production measurement is the empirical check on it. Say that in the
     section rather than stating the exclusion flatly, which is the overclaim this whole cycle
     exists to correct one instance of.
  1. Acceptance criteria met and tests pass
  2. Residual distribution read against the production store and recorded — the measurement, not a
     formality
  3. `/prawduct:critic` run and blocking findings resolved
  4. Committed and chunk marked `[x]` in Status

### Chunk 03: `bankmachine status`, and the inventory it checks against

- **Description:** One command answering *is my data sane?* per connection, reading Chunk 02's
  producer and an operator-supplied expected inventory. Collapses four runbook checks into an
  invocation — three fully, one partly.
- **Depends on:** Chunk 02
- **Artifacts consumed:** `.prawduct/artifacts/discovery-reconciliation-and-status.md`
  § *Requirements* (AC-18.1–18.3, AC-11.6a), `deployment/deployment-requirements.md` DAC-5.2/5.3
- **Deliverables:**
  - new `src/bankmachine/cli/status.py` — per connection: status, granted vs. requested window,
    account and transaction counts, the span they cover, currency mix, zero-activity accounts, what
    the last sync changed, Chunk 02's residuals, and the inventory states.
  - Registration in `src/bankmachine/cli/parser.py`. Exit `0` clean · `1` a finding · `2` could not
    run. 🔴 Not `75`: that means "did not finish", and a completed check that found a problem is
    exactly what `1` is for.
  - An inventory reader. 🔴 Path is **configuration with a documented default** — the no-hardcoded-
    paths norm binds here. Three states kept distinct: matched, unrecognized, expected-but-never-
    received.
  - 🔴 **The absent and the malformed inventory are different outcomes and must not collapse.**
    *Absent* — no file — is an opt-out: the command runs, reports the inventory section as
    `not supplied`, and exits on what the other checks found. "Never a pass" governs the **output**,
    not the exit code: the section must never render as a checkmark or be omitted, because an
    unperformed check that looks performed is the absent-bad-news shape this project's
    `learnings.md` already records against itself. *Malformed or unreadable* — a file the operator
    did supply and the command cannot use — is exit `2`: the command could not do what it was
    asked, and silently proceeding past a supplied-but-unusable oracle is the worse failure.
  - The AC-18.2 duplication signal, with its false-positive bar **written down before it ships** —
    per #96's own acceptance. The case that must not cry wolf is a joint account legitimately
    appearing on two connections.
  - `docs/first-production-connection.md` — §§ 3.7 and 4.3 collapse into the command; 4.5 partly
    (the check, not the second sync); 4.2 becomes "maintain the inventory, then run `status`".
    🔴 § 4's preamble — *"Everything above can be verified by the machine. Nothing below can"* — is
    falsified for three of its eight items and is rewritten, not left standing.
  - `.prawduct/artifacts/api-contract.md` (CLI verb table), `.prawduct/artifacts/operational-spec.md`
  - `deployment/deployment-requirements.md` — DAC-5.2/5.3 gain the file's location and format.
    🔴 Gitignored; this edit never reaches a remote.
  - `.prawduct/change-log.md` — one entry, `scope=reconciliation-and-status`, **no `release=`**
- **Tests:** unit — each of the three inventory states; the absent-file case asserting the section
  renders `not supplied`; the **malformed**-file case asserting exit `2`, separately, because these
  are the two outcomes most likely to be collapsed; the duplication signal firing on a true
  duplicate and **staying silent on the joint account**; a non-USD account reported.
  integration — all three exit codes; a store put into the state the runbook names as a remedy and
  `status` run against it, per `learnings.md` § *A documented remedy is a claim and is asserted like
  one* — this command becomes the runbook's remedy for three checks, which is what makes that rule
  bite here.
  🔴 **Two cases for the shared producer, not one** — `learnings.md`: "when a producer and its call
  site are the two halves of a fix, they need two cases, and the caller's must anchor on the call."
  The producer's case is Chunk 02's; this chunk's anchors on `status` calling it, so a second
  implementation added later goes red here rather than passing both suites.
  🔴 Any fixture inventory file uses invented institution names — `check-no-personal-data.sh` runs
  on every push and the real roster is gitignored for this reason.
- **Acceptance criteria:** `bash scripts/check.sh` green; one invocation answers AC-18.1 with no SQL
  and no `sync shell`; an absent inventory renders `not supplied`; a malformed one exits `2`.
- **Type:** cumulative-final
- **Visual change:** yes — a new human-facing output surface; legibility and whether a finding reads
  as a finding are not things a test can speak to
- **Done when:**
  1. Acceptance criteria met and tests pass
  2. An operator-verification entry queued for the new output
  3. Committed, then `/prawduct:critic cumulative` run and blocking findings resolved
  4. Chunk marked `[x]` in Status
