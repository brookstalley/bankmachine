---
artifact: build-plan
version: 1
scope: production-blockers
branch: feat/production-blockers
partition: |
  delegated, three ways, partitioned by REQUIREMENT BLOCK with a per-function ownership map over the
  three files all three must touch (`query.py`, `mcp.py`, `store/schema.py`) and a per-section map
  over `data-model.md`. The map is the whole reason three agents are safe here: the blocks are
  independent in their subject matter and overlapping in their file set, so a partition by file
  would have forced serial and a partition by item alone would have collided. Chunk 00 exists to
  make the map true — it lands, in advance and in one commit, every shared surface all three would
  otherwise have edited at once (the closed warning vocabulary above all).
  One requirement crosses the map and is not delegated: AC-14.5's wiring of C3's producer into C2's
  aggregate. It is chunk 04's, named here so it cannot be read as dropped.
critic_mode: cumulative-final
depends_on:
  - artifact: discovery-account-lifecycle
    file_path: .prawduct/artifacts/discovery-account-lifecycle.md
  - artifact: discovery-production-data-semantics
    file_path: .prawduct/artifacts/discovery-production-data-semantics.md
  - artifact: api-contract
    file_path: .prawduct/artifacts/api-contract.md
  - artifact: data-model
    file_path: .prawduct/artifacts/data-model.md
governed_by:
  - artifact: api-contract
    dispositions:
      - "the MCP surface is read-only, no mutation tools → conforms; all three items add fields, warnings and one read-side check, and no tool, parameter or write path"
      - "every response carries a freshness stamp, and incompleteness rides the success path as a warning field → conforms, and it is what all three chunks are FOR. Three new request-scoped kinds land in chunk 00 and are emitted by C1, C2 and C3 respectively"
      - "the CLI's three-way exit code → inapplicable; no chunk changes a CLI exit path"
      - "a tool's boundary is drawn where the answer shape changes → conforms; no tool is added, split or merged. C1 widens two existing row shapes and the guardrail holds — every added field is required and nullable, never optional"
      - "🔴 a stored balance is reported with its lifecycle, and no total over balances states no treatment of non-active accounts → this is C1's governing norm, born in chunk 00 on the owner's ruling. C1 discharges its one named retroactivity debt (`query._coverage`'s unfiltered `accounts` count)"
  - artifact: data-model
    dispositions:
      - "every stored amount is signed from the operator's point of view → C3 is a check ON this norm and must not weaken it; AC-14.4 forbids auto-correction, so no chunk changes a stored sign"
      - "all monetary values are integer minor units → conforms; C1's flagged magnitude and C2's pending magnitude are both integer minor units"
      - "calendar dates and UTC instants are distinct → C1 adds `last_seen_date` as a calendar date, matching `first_seen_date`; it is not an instant"
      - "a transaction is never hard-deleted; removal is a soft delete → C2 builds on `removed_at` and adds no delete path"
      - "a migration's DDL is frozen → C1 adds migration 003 rather than editing migration 002"
      - "every silver row carries exclusive provenance and its derivation version → `accounts` carries no `derivation_version_id` and is not rebuildable; C1's AC-12.6 rests on exactly that and must not change it"
      - "the daily balance and holdings series are append-only → inapplicable; nothing here writes `balances_daily` or `holdings`. FR-9 adds a column to `accounts`, which is a dimension table and not one of the two series this norm governs"
      - "🔴 a source value is never overwritten in place → CONFORMS, and it is the one disposition here that needed an argument rather than a check. AC-13.2 requires a settlement to update `amount_minor` IN PLACE, which reads at first like a departure. It is not: the norm protects a source value from being clobbered by LOCAL interpretation, which is why the remedy it names is a separate override column. A settlement replaces one figure the source reported with a later figure the source reported for the same transaction — the source correcting itself, not us reinterpreting it — and both raw responses stay in `raw_responses`, so the hold amount is never lost and the row remains rebuildable. C2 also verified the hold figure is not retained as a second field, which is AC-13.2's own requirement and would otherwise be the tempting way to dodge this question"
---

# Build Plan — The Three Production Blockers

**Backlog items:** `brookstalley/bankmachine#40`, `#22`, `#23` — the three open `blocks:production`
items, all at `stage: ready`
**Type:** feature (three requirement blocks already written and in force)
**Size:** large — three requirement blocks, a migration, a new module, and a contract surface each

## Requirements Confidence

**High, and unusually so: the requirements were written in a previous cycle, reviewed by the Critic,
and are in force in `docs/system-requirements.md` rather than proposed.** No chunk below designs a
requirement. FR-9 (AC-12.1–12.9) is § 4; AC-13.1–13.9 and AC-14.1–14.9 are § 7.

- *Problem:* three answers this product can give today are plausible, well-formed and capable of
  being wrong with no signal — a balance frozen since an account stopped being reported, a spending
  total that silently mixes in authorisation holds, and a signed amount from a connection whose
  direction has never been observed against a known inflow.
- *Success:* each of the three is disclosed in the payload, on the success path, on every answer it
  applies to; the state that makes disclosure possible is reachable rather than merely declared; and
  each guard has been seen red.
- *Out of scope:* AC-13.8, AC-13.9, AC-14.7, AC-14.8 and AC-14.9 gate on production data and are
  enqueued as VRF-005 / VRF-006 in `.prawduct/operator-verification.md`. **They are not descoped and
  not attempted** — they are the operator's, they block `blocks:production` closure, and no chunk
  here may claim them.

### The ruling this plan was unblocked by

🔴 **AC-12.8's treatment ruling was taken by the owner on 2026-09-09: a total over account balances
INCLUDES non-active accounts and states what they contributed.** The recommendation on file was the
opposite (exclude, and state the excluded magnitude) and was not taken; both arguments are preserved
in `discovery-account-lifecycle.md` § *The ruling the owner owes*. The ruling births the fifth
`api-contract.md` § Direction norm, which chunk 00 landed.

### Open assumptions

`[ASSUMPTION: the three warning kinds keep the names api-contract.md § The warning vocabulary
already published for them — account_no_longer_active, includes_pending_rows,
sign_convention_unverified | LOW impact | they were named by the previous cycle's two discovery
passes and are additive under the contract's evolution rule; a rename before any consumer exists is
free]`

`[ASSUMPTION: AC-13.6's "either serves a query that exists or is removed" resolves toward MAKING the
index serve the resolving query, rather than dropping it | MEDIUM impact | C2 owns this decision and
must record it either way. The criterion is deliberately written to permit both, and the discovery
document declines to pick]`

## Decisions

`[DECISION: the shared warning vocabulary lands in ONE coordinator commit before any delegate starts,
rather than each delegate adding its own kind | Three parallel agents cannot see each other, and
`envelope.REQUEST_SCOPED_KINDS` is a closed, test-enforced tuple whose three additions would land on
adjacent lines in the same tuple, the same `_GUIDANCE` map and the same instructions table. This is
the one collision the previous cycle's partition could not catch and only integration saw; landing it
up front is the cheapest possible answer | coordinator, 2026-09-09]`

`[DECISION: AC-14.5's wiring — C3's per-connection sign finding reaching C2's aggregate answers — is
the COORDINATOR's, in chunk 04, not either delegate's | It is the one requirement that crosses the
ownership map: the producer is C3's and the only call site is inside a function C2 owns outright. The
alternatives were both worse — a stub returning `[]` in chunk 00 is a check that covers nothing,
which this repo has now shipped twice (#46, #47) and should not ship a third time; and having C2 call
a module C3 has not written yet leaves C2 unable to run its own tests. Named here, with its own
acceptance line in chunk 04, because a requirement that crosses a partition boundary is exactly the
one that gets silently dropped | coordinator, 2026-09-09]`

`[DECISION: no delegate runs the full suite, and no delegate runs verify_norms_go_red.py | Three
whole-suite runs on one box is the unattributable-green anti-pattern with the machine load to
produce it, and the go-red harness runs every case in the list rather than a named one. Each delegate
proves its own go-red by hand — mutate, run the one named test, confirm red, revert — which is the
same evidence at one case's cost. The combined suite and the full harness are chunk 04's | user
directed no heavy tests in delegates, 2026-09-09]`

## Partition — the ownership map

🔴 **A delegate edits only what this table gives it.** Where a file is shared, ownership is by
function or by section, and the boundary is the function or section itself — not "the area around
it". Anything not listed is the coordinator's.

| Surface | C1 (#40) | C2 (#22) | C3 (#23) |
|---|---|---|---|
| `store/schema.py` | the `accounts` table | the `transactions_pending_link` index | — |
| `store/migrations/` | migration 003 + its registration | — | — |
| `connector/plaid/derivers.py` | the accounts deriver | — | — |
| `query.py` | `_coverage`, `AccountCoverage`, `_account_coverage`, `_uncovered_caveat`, `list_accounts`, `coverage_report`, and a new `_account_lifecycle` beside `_account_coverage` | `_transaction_filters`, `list_transactions`, `_flow_class`, `_flow_class_totals`, `_median_interval`, `money_summary` | `pipeline_health` |
| `mcp.py` | a `_lifecycle_row_fields()` beside `_coverage_row_fields()`, and the `list_accounts` + `get_coverage_report` definitions | the `query_transactions` + `money_summary` definitions | the `get_pipeline_health` definition |
| new module | — | — | `src/bankmachine/signs.py` |
| `data-model.md` | the `accounts` field table, § Account lifecycle | § Constraints, the index claim | § Direction, scoping the sign norm |
| `docs/system-requirements.md` | — | the AC-11.3 amendment note only | — |
| `operational-spec.md` | the migration-ordering line | — | — |

**Coordinator's alone, and no delegate may touch them:** `envelope.py`, `mcp_resources.py`,
`api-contract.md`, every `.prawduct/` governance file, `docs/system-requirements.md`'s FR-9 and § 7
criterion text (read it, never edit it), and the `git` history.

**The one expected conflict, accepted rather than engineered away:** all three delegates append to
`CASES` in `tests/preferences/verify_norms_go_red.py`, at the end of one list. Three appends to one
tail is an append/append conflict whose resolution is "keep all three", and inventing per-item
anchors to avoid it would be structure built for the org chart. The coordinator resolves it.

## Status

- [x] **00 · The ruling, the norm it births, and the shared vocabulary** *(coordinator)*
- [x] **01 · Account lifecycle, made visible — #40, AC-12.1–12.9** *(delegate)*
- [x] **02 · Pending-transaction semantics — #22, AC-13.1–13.7** *(delegate)*
- [x] **03 · The per-connection sign-convention check — #23, AC-14.1–14.6** *(delegate)*
- [x] **04 · Integration** *(coordinator)*

**Context.** All chunks complete, suite green and recorded at 966. Seven review rounds: one
cumulative over the delegate work, four verify-resolutions rounds, and a second cumulative; every
finding is fixed or accepted with a recorded reason.

**Merged 2026-09-09 as PR #54**, and the branch this plan's frontmatter names was deleted at merge —
which is the ordinary end state here, not a loose end. The three findings that cycle produced went
to `build-plan-production-blocker-findings.md`, merged as PR #55.

🔴 **This heading was `### Status` under a `## Chunks` parent until 2026-09-09.** Every other plan in
this repo puts it at the top level, and that is where governance looks, so a fully-ticked plan read
as one with chunks outstanding. The ticks are what disarm the Stop gates — nest this heading again
and the ticks stop being read.

🔴 **One warning is recorded UNRESOLVED by design, and it is not a drop.** The cumulative's R-8
part (2): `list_accounts` and `_not_active_balances` each build the latest-recorded-balance
subquery independently, and the latter's docstring asserts it produces "the same figure
`list_accounts` puts on the row". The identity IS pinned — `test_the_magnitude_is_signed_and_
grouped_by_currency` asserts both figures against one number in one test, which is this repo's
*two descriptions, compared* pattern — but the subquery itself is written twice. Extraction is
cosmetic and was left rather than carried into a closing commit. It rides #53, which spans the
same read path.

🔴 **Ticked ≠ closed.** Every one of the three items keeps a production-data tail (AC-13.8, AC-13.9,
AC-14.7, AC-14.8, AC-14.9) that no chunk here may claim, and #40 additionally cannot reach its
`closed` lifecycle value until an operator declaration path exists. See § *What this plan does NOT
close* and the as-built notes under chunk 04.

---

## Chunks

### Chunk 00: The ruling, the norm it births, and the shared vocabulary

Delivers: AC-12.8 restated to the ruled treatment; the ruling recorded where the argument for the
road not taken survives it; the fifth `api-contract.md` § Direction norm born, with its retroactivity
debt named rather than grandfathered and its norm-index row written `in-transition`; and all three
warning kinds landed in `envelope.REQUEST_SCOPED_KINDS` with their `_GUIDANCE` entries and their rows
in the server instructions.

**Done when:** `tests/preferences` and `tests/test_mcp*.py` are green, and no delegate needs to touch
the vocabulary.

### Chunk 01: Account lifecycle, made visible (#40)

Delivers AC-12.1 through AC-12.9. The population path, the read path and one migration — **not a read
path alone**, which is the false premise #40's body led with. Design proposed in
`discovery-account-lifecycle.md` § *Design*: four always-present row fields, a three-value vocabulary
naming the observation rather than the conclusion, `no_longer_reported` derived at read time and
never stored, and one producer feeding both `list_accounts` and `get_coverage_report`.

**Done when:** all nine criteria hold; AC-12.9's shrinking-roster replay exists and its assertion has
been seen red; `query._coverage`'s `accounts` count carries the ruled treatment's figure.

#### Chunk 01 notes — the delegate's recorded decisions *(C1, 2026-09-09)*

The proposed design was built as proposed. Four things it did not settle, decided here:

`[DECISION: the flagged magnitude is a PER-CURRENCY array, not a single signed integer | AC-12.8 says
"a count and a signed sum, both always present and zero rather than absent". A single integer over
two currencies is refused by `api-contract.md`'s standing ruling that currency groups and never sums
— "not a wrong number, it is not a number". So `coverage.not_active_balance_minor_units` is
`[{currency, current_minor_units}]`, present and EMPTY when nothing qualifies, on the same precedent
as `money_summary`'s `totals` block, which is present and empty against an unreadable store.
`coverage.accounts_not_active` carries the count as a plain integer | C1]`

`[DECISION: `account_no_longer_active` is emitted by `list_accounts` and `get_coverage_report`, and
by nothing else | AC-12.8's sentence "the answer also carries `account_no_longer_active`" sits beside
a clause that also covers `coverage.accounts`, which rides EVERY response — and a kind on every
response equally is the `gapped` defect the contract names by name. Resolved on the existing
precedent rather than by picking: `accounts_without_coverage` is store-wide standing state too, and
fires on exactly these two tools. The FIGURE rides every answer; the WARNING rides the two answers
that are about accounts. `query_transactions(account_id=N)` was proposed in the discovery document
and is NOT built here — `list_transactions` belongs to C2's half of the ownership map, and no
criterion names it. Left for integration | C1]`

`[DECISION: AC-12.7 is discharged by leaving `silence_exceeds_cadence` FALSE for a non-active
account, with `silence_ratio` reported as measured | The discovery document leaves "flag goes false
or ratio goes null" open. Nulling the ratio would destroy the number `silence_ratio`'s own ruling
exists to preserve, and the lifecycle fields on the same row are what say why the flag is false | C1]`

`[DECISION: the operator's retirement command (`bankmachine accounts retire`) is NOT built | It is a
vetoable MED-impact assumption in the discovery document, not a criterion — no clause of AC-12.1
through AC-12.9 requires it — and `src/bankmachine/cli/` is outside chunk 01's ownership map. The
consequence is stated rather than left to be found: `closed` is reachable in the read path and
unreachable in the product, so the operator can never confirm what the system suspects and AC-12.2's
honest ambiguity never resolves for any account. `test_account_lifecycle.py` asserts the absence so
it cannot be mistaken for closed | C1]`

**Three edits outside the ownership map, each forced by a criterion and each minimal:**

- `src/bankmachine/store/connection.py` — `SUPPORTED_SCHEMA_VERSION` 2 → 3. A migration that nothing
  serves is a datastore no build will open.
- `src/bankmachine/mcp.py::_output_schema` — the `coverage` block gains the two AC-12.8 keys. That
  block is shared by every tool and closes `additionalProperties`, so `query._coverage` could not
  emit a key the schema did not declare without every tool's answer failing its own published schema.
- `src/bankmachine/query.py::_unusable` — the same two keys, present and zero. The unreadable-store
  path builds its envelope in a different function, and AC-12.8 fixes both as always-present.

`src/bankmachine/mcp.py`'s server-instructions table also gained two rows, because
`test_the_instructions_name_every_field_the_envelope_actually_carries` fails on an envelope key the
instructions never name. No warning row was touched — chunk 00 landed those.

**The cost, measured rather than inherited** (2026-09-09, 16 accounts across 4 connections, 4 of
them no longer reported — the shipped sandbox's shape). The discovery document recorded the
negligible-cost expectation as an expectation, and `learnings.md` § *A cited number measures what its
study measured* is against reusing #19's 3.0ms figure for a different walk:

| | per call |
|---|---|
| `list_accounts` end to end | 10.70 ms |
| `_account_lifecycle` walk | **0.08 ms** |
| `_account_coverage` walk (#19, for scale) | 0.15 ms |
| `_coverage` envelope block, lifecycle figures included | 0.67 ms |

`list_accounts` runs the lifecycle walk twice — once for its rows and once inside `_coverage`, which
every tool calls. At 0.08 ms the duplication was left rather than threaded through `_answer`, whose
signature is shared with C2's and C3's tools and was not C1's to move mid-fan-out.

### Chunk 02: Pending-transaction semantics (#22)

Delivers AC-13.1 through AC-13.7. Tier 1 (the read path — no aggregate distinguishes pending from
posted today) and tier 2 (settlement branch cases the fixtures do not reach) per
`discovery-production-data-semantics.md` § *The three tiers*. **AC-13.8 and AC-13.9 are the
operator's and are out of this chunk.**

**Done when:** the seven criteria hold; AC-13.6's index question is resolved in code AND in
`data-model.md` § Constraints with the same account in both; the pending disclosure is present-and-
zero rather than absent; and a fixture carrying `pending: true` rows exercises the read path.

### Chunk 03: The per-connection sign-convention check (#23)

Delivers AC-14.1 through AC-14.6 — a per-connection check over a declared category set, with the
measured sandbox baseline as its negative control and a synthetic inverted feed as its positive one,
reporting rather than correcting. **AC-14.7–14.9 are the operator's and are out of this chunk.**

**Done when:** the six criteria hold; the check declares its category set, threshold and both
controls; an inverted feed is reported and never corrected; and `get_pipeline_health` surfaces it.

#### As built — the four decisions the criteria left to the builder

🔴 **The call chunk 04 wires for AC-14.5**, stated once so the crossing requirement cannot be
dropped: inside `money_summary`, where `conn` is already open, pass
`extra_caveats=signs.caveats(conn, since=since, until=until)` to its `_answer(...)` call. It returns
`list[Caveat]` — empty when nothing is flagged, which is the ordinary case — ordered by connection
id, and it opens no handle of its own.

`[DECISION: the threshold is a PROPORTION of judged rows (>50% positive) rather than a row count,
and the one absolute constant is a sample floor of 8 derived from the negative control's own
precision | AC-13.5's precedent is that a fixed constant against a variable cadence produces
findings and no signal — the 7-day gap rule reporting a monthly account's ordinary silence. A share
is dimensionless, so it does not move with a connection's size, its institution's posting rhythm or
how long it has been enrolled, which is how this criterion escapes that trap entirely. 0.5 is the
point of indifference between the two hypotheses and the only value not chosen by taste: measured
separation is 0.00 on a conforming feed against 1.00 on an inverted one, so every threshold strictly
between 0 and 1 returns the same verdict and a tuned 0.9 would buy nothing while needing its own
derivation. The floor of 8 is where it is because positives were observed 0 times in 219 rows, the
rule of three bounds a conforming feed's per-row positive rate at 3/219 = 1.37%, and 8 is the
smallest n at which a conforming feed's chance of showing a positive MAJORITY stays below one in a
million for n and every larger n — the quantity oscillates with parity, so 6 qualifies and 7 does
not | user can override, and moving either number moves only which connections are judged, never
what is done about one]`

`[DECISION: the `sign_convention_unverified` caveat fires for an INVERTED connection only; an
`undetermined` one is published on `get_pipeline_health` and raises nothing on the aggregates |
AC-14.2 flags a connection whose distribution is *inverted* and AC-14.5 is about an aggregate over a
*flagged* connection, so this is the narrow reading of both. The broad reading — every connection
whose direction has never been checked against a known inflow, which today is all of them — would
put an identical, unactionable string on almost every answer this product gives, which is exactly
the "true and useless" failure `envelope.py` records against the `gapped` notice arriving
character-for-character identical on four unrelated questions. `undetermined` is not silent: it
reaches the wire per connection on the verification surface, which is the surface built to tell "we
could not tell" apart from "we checked and it is fine" | 🔴 user can override, and this is the
decision most worth a second opinion — `mcp_resources.py`'s landed guidance for the kind reads
"never been observed against a known inflow", which is broader than what the emitter fires on]`

`[DECISION: a connection is judged over its whole stored history, and only the WARNING is scoped to
the caller's window | Judging within the window would let a two-day question answer "not enough rows
to judge" about a connection the store holds four hundred rows of evidence about — quietest exactly
where the narrowest question was asked. Scoping the warning is the other half: a request-scoped kind
that fires regardless of what the request touched is the failure above, and an aggregate that drew
nothing from an inverted connection is not an aggregate computed over it | user can override]`

`[DECISION: the check's unit is the CONNECTION, so an account with no connection is not judged |
`accounts.connection_id` is nullable for FR-7's manual-import path, and no such row can exist today
because no adapter is built. Recorded rather than left implicit because it stops being free the day
one lands: an adapter reading a CSV signed the other way is exactly this defect on the one path the
check does not watch, and judging imported rows needs a unit that is not a connection | user can
override; it is a scope statement, not a mechanism]`

**Not done here, and named rather than left to inference:** AC-14.5's wiring itself (chunk 04's, per
the plan's own decision above) and AC-14.7–14.9 (the operator's, VRF-006).

### Chunk 04: Integration

Delivers: the three branches merged; **AC-14.5's wiring** — `signs`' finding reaching `money_summary`
as `sign_convention_unverified`, with the test that proves an aggregate over a flagged connection
carries it; the `*(specified, not built)*` annotations cleared from `api-contract.md` § The warning
vocabulary now that each kind has an emitter; the `verify_norms_go_red.py` tail conflict resolved and
the full harness run; the combined suite; `/prawduct:critic cumulative`.

**Done when:** the suite is green on the merged tree, every go-red case in `CASES` is red, AC-14.5
holds end to end, and the Critic has no unresolved blocking findings.

### As built: integration notes for chunk 04

**The two boundary-crossing wirings, which were the partition's real risk and were both real.**
AC-14.5 (`signs.caveats` → `money_summary`) and AC-13.5 (`_stranded_holds` → `coverage_report`) each
had a producer that was finished, tested and correct, and a call site inside a function a different
delegate owned. Neither wrote the call, exactly as this plan predicted. Both are now wired and tested
**from the answer's side, over the wire** — never by asserting that a function is called — and both
go-red cases anchor on the CALL rather than the producer, because a producer that works and a surface
that never invokes it are indistinguishable from the producer's own tests.

AC-13.5's suppression carries AC-12.7's reasoning onto its sibling: a non-active account's stranded
hold can never settle and can never be cleared, so the *call to action* is withheld while the *count*
stays as measured. Hiding the count too would be the opposite error and was just as available.

**A third crossing, wired after the review and recorded here because chunk 01 left it open.**
`account_no_longer_active` now also fires on `query_transactions(account_id=N)`, scoped to the
account asked about. Chunk 01 deliberately did not build it — `list_transactions` was C2's function
and no criterion names it — and recorded it as *left for integration*. Integration answered it by
building it: AC-12.1's rationale is the agent that never thought to call the verification surface,
and asking "what did I spend on this card" about an account whose institution stopped listing it is
exactly that agent. `accounts_without_coverage` already fires on the same call for the coverage axis,
which is the precedent. Covered for both the derived value and the operator-declared one, since they
share a branch.

**Three defects delegates found outside their own maps, all fixed here.** Chunk 00's guidance for
`sign_convention_unverified` described a broader trigger than the check fires on; the client guide had
fallen four kinds behind with no guard on it; and the DDL comment beside `transactions_pending_link`
still carried the claim `data-model.md` had withdrawn — correctable without touching the frozen hash,
because the comment sits between the statement strings rather than inside one.

**A guard was added for a drift class that has now recurred twice**: the contract table and the client
guide are both reconciled against `envelope.WARNING_KINDS`. The client-guide reader counts *definition*
lines only, because the kind that went missing was named in the guide's prose — a substring check would
have called that documented.

**One phrase was reworded rather than narrowing a rule.** A test message collided with a roster
institution name on a word boundary and `check-no-personal-data.sh` stopped the run. The check was
right; weakening a security guard to keep a turn of phrase is a bad trade, and the script says so
itself.

---

## What this plan does NOT close

🔴 **None of the three issues closes here.** Each carries a production-data tail —
AC-13.8/13.9, AC-14.7/14.8/14.9 — and #40's own tail is the operator declaring an account inactive
against real data. `blocks:production` comes off when VRF-005 and VRF-006 are performed, not when
this branch merges. Stating it here because "all chunks ticked" reads as "the blockers are closed",
and it is not what this plan delivers.
