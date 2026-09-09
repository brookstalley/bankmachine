---
artifact: build-plan
version: 2
scope: mcp-answer-scope-completion
branch: feature/mcp-answer-scope-completion
depends_on:
  - artifact: discovery
    file_path: .prawduct/artifacts/discovery-mcp-answer-scope.md
  - artifact: discovery
    file_path: .prawduct/artifacts/discovery-mcp-tool-surface.md
  - artifact: api-contract
    file_path: .prawduct/artifacts/api-contract.md
governed_by:
  - artifact: api-contract
    dispositions:
      - "the MCP surface is read-only, no mutation tools → conforms; every deliverable is a read path or a response field, and the one tool added (`get_coverage_report`) reads"
      - "incompleteness rides the success path as a warning field, never as an exception → conforms, and C2 is its per-account application: an account with no transaction coverage is incomplete data, and it becomes a field plus a warning rather than an empty list a caller must interpret"
      - "CLI three-way exit code → inapplicable; nothing here touches the CLI"
      - "🔴 a tool's boundary is drawn where the answer shape changes → BINDING, and this plan is the norm's first application AND its one migration. C4 merges `spending_summary` into the grouped aggregate. C2's split of `list_accounts` from `get_coverage_report` is the norm's second guardrail deciding, not a preference"
      - "additive changes only; never remove or repurpose an existing field → 🔴 DEPARTURE, recorded: C4 removes the tool `spending_summary` and its `spent_minor_units` row field. Permitted because § Surface Inventory grades every MCP tool `experimental`, where 'removing one is the policy working, not a violation', and the § Direction norm that licenses the merge states the migration explicitly. Single consumer, pre-production, no deprecation window owed"
      - "raw-row cap ~500 hard; aggregates unpaginated → conforms; no cap moves and the merged aggregate stays unpaginated, bounded by its grouping"
      - "🔴 a caller-supplied `group_by` reaches SQL → conforms by CONSTRUCTION, stated because the shape invites the opposite reading. `group_by` is never interpolated: it selects among expressions this module builds, it is checked against the closed `GROUPINGS` tuple before any expression is chosen, and an unknown value raises `BadGroupingError` at the boundary rather than reaching the statement. `test_an_unknown_grouping_is_refused_by_name_rather_than_defaulted` and `test_the_advertised_groupings_are_the_ones_the_query_layer_accepts` hold both halves, the second deriving the advertised enum from `GROUPINGS` so the surface cannot advertise a value the query layer refuses"
  - artifact: data-model
    dispositions:
      - "🔴 every stored amount is signed from the operator's point of view → BINDING ON C4. `inflow_minor` and `outflow_minor` are reported as positive magnitudes with direction carried by the field NAME, while `net_minor` stays operator-signed. Stated because the merged tool is the first place both conventions meet in one row, and a row mixing them silently is exactly the defect this cluster exists to remove"
      - "all monetary values are integer minor units, no floats → conforms; every new amount field is `int` and says so in its name"
      - "🔴 calendar dates and UTC instants are distinct types and never mix → BINDING ON C2. `first_transaction_date` / `last_transaction_date` are calendar dates from `posted_date`; the gap walk compares dates to dates. `as_of` remains the only instant in the envelope"
      - "a source value is never overwritten in place; local interpretation lives in its own column → conforms, and C3 depends on it: `flow_class` is local interpretation derived at read time from `source_category_*` and account type, and writes nothing"
      - "a transaction is never hard-deleted; removal is a soft delete → conforms; every new read filters `removed_at IS NULL`, including the coverage walk. 🔴 A coverage report that counted soft-deleted rows would report coverage the analysis surface cannot see"
      - "the daily balance and holdings series are append-only → inapplicable; this plan writes no row"
      - "every silver row is either aggregator-sourced or manually imported and carries its evidence → conforms, and it is READ here rather than merely respected: `get_coverage_report`'s `source_breakdown` reports every account's rows by `source`, derived from `PROVENANCE_SOURCES` so a third provenance cannot appear on the wire without appearing in that constant first"
      - "a migration's DDL is frozen once written, and the Core metadata and that DDL describe the same tables → inapplicable; this plan adds no column, no table and no migration. Every field it ships is computed at read time"
  - artifact: nonfunctional-requirements
    dispositions:
      - "🔴 MCP aggregate tool response under ~1s over 24 months → MEASURED IN C2, not assumed. C2 puts a per-account aggregate on `list_accounts`, the most-called tool. `mcp-count-latency-2026-09-08.md` prices comparable grouped reads at ~17ms at 10k rows, but that figure was measured for a different statement and `learnings.md` forbids inheriting it. C2's Done-when measures the walk"
  - artifact: security-model
    dispositions:
      - "no personal data in the repo → conforms; every figure in this plan comes from the sandbox fixture and the preferences guard covers it"
      - "🔴 secrets live only in the OS keychain and nothing returns one into a log, an exception or a payload → conforms, and it is load-bearing on this diff rather than paperwork: the two registration guards raise `ToolRegistrationError` carrying a tool name, a field name and a schema path, and `cmd_mcp` logs that refusal with `logger.exception` on a subprocess whose stderr the operator reads. None of those values is caller-supplied or credential-derived — they are this module's own literals — and no new code path touches `secrets.py` or a connection's `credential_ref`"
      - "log redaction happens at the formatter and over-redacts by design → conforms, unchanged; the one new log line added by this plan goes through the same formatter as every other, and nothing here configures, bypasses or narrows redaction"
      - "🔴 the aggregator's API is the only network destination → conforms, and it is checked rather than asserted: every deliverable reads the local datastore, `mcp.py` and `query.py` gain no import that can reach a socket, and `test_only_the_connector_reaches_the_network.py` holds that for the whole tree with a go-red case that plants an `ssl` import in `query.py`"
---

# Build plan — completing the answer-scope cluster

Origin: `discovery-mcp-answer-scope.md` chunks C2, C3 and C4, whose C1 shipped in
`feature/mcp-effective-window`; plus `discovery-mcp-tool-surface.md`, whose norm re-points
C3 and C4 onto one merged tool rather than two.

**One batch, one PR.** The owner ruled 2026-09-09 that the norm and the build ship together:
pre-production, single consumer, nobody else reads `develop`, and splitting them buys review
ceremony rather than safety. Quality gates are unchanged — suite, Critic, go-red proof.

Requirements confidence: **High** on C2, C3 and C4 — every requirement predates the finding
(AC-9.1, AC-9.3, AC-9.5, AC-11.1), the owner's rulings on gap threshold, currency and
`flow_class` are recorded in the discovery, and the tool-boundary norm is ratified. **Medium**
on the merged tool's NAME only, which is a selection question no evidence here settles; it is
this plan's one vetoable decision and is called out below.

Critic mode: `cumulative-final`. The chunks land close together on one branch and share
`mcp.py`, so per-chunk reviews would split one diff across reviewers who each see a third of it.

partition: **serial, coordinator-owned, no delegation.** `src/bankmachine/mcp.py` and
`src/bankmachine/query.py` are both touched by C2, C3 and C4, so there is no partition that
gives each chunk sole ownership of a file — which is the only partition that made the previous
wave's parallelism safe. Delegating against a shared file would buy wall-clock and pay for it in
integration conflicts.

---

## Status

<!--
🔴 Headings are `### Chunk NN:` because that is the form the record-lint's
deliverable check addresses. With `## A — …` it found no chunk at all and
produced NO answer — not a pass, not a failure — so nothing in the diff was ever
graded against a declared deliverable set, and a check that could not run is
indistinguishable from one that passed. The letters stay in the heading and in
the boxes below because every commit message, the change-log and both handoffs
already cite them; renumbering those would be rewriting history to match a
parser.

🔴 So the two vocabularies are BOTH live and they are not interchangeable at the
command line: `--chunk 01` resolves and `--chunk D` does not, reproducing the
"chunk 'D' not found" this comment exists to explain. The boxes below keep the
letters because prose cites them; anything addressing a chunk by argument uses
the number.
-->

- [x] **A — The per-account coverage signal, and the two tools that read it** (C2 · #19, #35)
- [x] **B — One aggregate tool, both directions, currency-grouped** (C4 · #20, #21)
- [x] **C — `flow_class`, so "spending" stops meaning three different things** (C3 · #18)
- [x] **D — The guards that make the norm self-enforcing, and the go-red cases nothing had**
- [x] **E — The claim-site sweep, and the artifacts that describe an eight-tool surface** *(pulled forward into B — see below)*

---

## The decision this plan asks the owner to veto or let stand

🔴 **The merged aggregate tool is named `money_summary`.**

`spending_summary` is the shipped name and becomes a lie the moment the tool returns inflows —
#20's whole finding is that refunds and income are unreachable, and TRAVEL reports $12,000
against a true $0 because 24 offsetting credits are invisible. `cashflow_summary` is the spec's
name for the other half and is a finance term of art for exactly in-versus-out, but it is a
worse selection target for "how much did I spend on groceries", which is the most common
question this surface will be asked.

`money_summary` is neutral to direction and reads naturally for both questions, with the
description carrying the vocabulary an agent actually matches on. The cost of a third name is
the contract's tool table and the guard's claim sites — **both of which chunk E rewrites
anyway**, so the marginal cost is zero.

`[ASSUMPTION: money_summary over cashflow_summary | LOW impact | user can override — one
rename, confined to chunk B and chunk E]`

---

### Chunk 01: The per-account coverage signal, and the two tools that read it (chunk A)

**Closes #19 and #35.** `discovery-mcp-answer-scope.md` C2, plus the split ruled by
`api-contract.md` § Direction's fourth norm.

Nine of fourteen sandbox accounts have zero transactions ever — 82% of the balance sheet by
magnitude — and nothing in any payload says so. `query_transactions(account_id=9)` returns `[]`,
which is indistinguishable from a quiet month. Three fields actively imply the opposite:
`coverage.accounts: 14` reads as fourteen covered, health reports the connection `active`, and
the only warning is about depth rather than breadth.

**Do:**

1. 🔴 **One producer** in `query.py` computes the per-account coverage facts — `first_transaction_date`,
   `last_transaction_date`, `transaction_count`, all over `removed_at IS NULL`, one grouped read
   against `transactions_by_account_date`. Both tools below consume it. This is #35's second
   acceptance criterion and the reason #19 and #35 were told to be scoped together: built twice
   they can disagree, and a verification surface that contradicts the analysis surface is worse
   than one that is absent.
2. **`list_accounts` carries those three fields on every row, always present** — dates nullable
   where the account has none, count `0` rather than null. Not behind a parameter: an agent that
   never thought to call the verification tool is exactly the failure #19 describes, and § Direction's
   second norm is that incompleteness rides the answer rather than a channel nobody reads.
3. **`get_coverage_report` ships**, carrying those same facts plus the verification detail:
   per-account gaps against **the account's own median interval** (ruled in the discovery; AC-9.1
   and AC-11.1 amended), and the source breakdown by `source`.
4. **The three-way distinction becomes reachable**: *no such account* already refuses by name;
   *account with no coverage at all* is new and is what #19 asks for; *account genuinely quiet in
   this window* already answers empty. A warning names the accounts with no coverage, so a caller
   reading only `warnings` still learns it.

### Amendment, written before it was coded: the warning is REQUEST-scoped

#19 asks for "a `warnings` entry naming the accounts with no transaction coverage", and the
kind is new — a domain term, so it is recorded here rather than settled in the editor.

**Kind: `accounts_without_coverage`, and it joins `REQUEST_SCOPED_KINDS`, not the connection
scope.** The obvious reading is that "this account has never had a transaction" is standing state
of the store and therefore connection-scoped. 🔴 **That reading reproduces the defect the
connection scope's own comment records**: an acceptance round measured the `gapped` notice arriving
character-for-character identical on a window inside coverage, one outside it, a future window,
and a query for an account that does not exist — "true, and useless for telling a caller whether
THIS answer is the degraded one." A fifth kind riding every response equally would be the same
warning nobody can act on.

Request-scoped instead, firing only when **this** request's scope actually contains an uncovered
account: on `list_accounts` when the listing holds one, and on `query_transactions(account_id=9)`
when the account asked about has no coverage at all — which is #19's own repro and the exact call
whose `[]` reads as "no payments found" and is false. Its presence is information and **so is its
absence**, which is what the request scope exists for.

Additive by `api-contract.md`'s evolution rule: new warning kinds need no version bump and
consumers must tolerate an unrecognized one, so AC-9.3's list needs no amendment. The published
enum, the derived warning resource and the decision table all pick it up from `WARNING_KINDS`
without being edited — that derivation is chunk 05's property from the previous wave, and this is
its first test.

### Amendment, also written before it was coded: the gap rule measures TRAILING SILENCE

Reading the owner's 2026-09-08 ruling closely enough to implement it caught a design already
half-written in the wrong instrument. The ruling and `mcp-fact-find-ac91.md` say the informative
quantity is **trailing silence measured against the account's own cadence** — days since the last
transaction, against the median interval — **not** an enumeration of gaps *between* transactions.

🔴 **The distinction is the whole point of the ruling.** Inter-transaction gaps are what "gaps > 7
days" measured, and measurement falsified it: every sandbox account is monthly, so CD and Money
Market exceed 7 days on **100%** of their 23 intervals and Saving on 50% — ~146 findings and zero
signal. Re-deriving that same enumeration with a per-account threshold instead of a fixed one
would have reproduced the noise in a more defensible-looking form. The one thing in this dataset
worth a second look is CD and Money Market sitting **28 days silent on a 30-day cycle**.

**Ruled here: report the quantity, and flag only a full missed cycle.** Every row carries
`median_interval_days` (the cadence; null under two transactions, because no interval exists and a
zero would read as "posts daily"), `days_silent`, and `silence_ratio` — the ratio *is* "trailing
silence measured against the account's own cadence", stated as a number rather than collapsed into
a boolean. `silence_exceeds_cadence` is true when `days_silent > median_interval_days`.

🔴 **The multiple is 1.0 because one missed cycle is the only non-arbitrary unit**, and picking
0.9 to make CD/MM flag would be inventing the constant the ruling just removed. Those two accounts
surface as `silence_ratio` 0.93 — visibly the highest in the store — which is exactly the
"genuinely borderline" the fact-find describes, and a boolean is what would have destroyed it.

**Done when:** the three-way distinction has a test per case; `get_coverage_report`'s real payload
validates against its own published `outputSchema` through the live loop, like every other tool;
🔴 **the `list_accounts` walk is MEASURED at the 10k-row volume and the figure recorded** —
`mcp-count-latency-2026-09-08.md` measured a different statement and `learnings.md` forbids
inheriting it; and `api-contract.md`'s per-account-coverage section describes what the code does.

**Done, 2026-09-09.** Measured in `mcp-coverage-latency-2026-09-09.md`: the walk costs **3.0ms**
at the expected 10k-row volume against an 18.2ms end-to-end `list_accounts` and a ~1s target.
🔴 **That killed the opt-in parameter the design was holding in reserve** — cost was the only
argument for making completeness something an agent had to ask for, and #19 is the record of what
happens when it must. `get_coverage_report` is the more expensive tool at 38ms and scales worse
(4.7× for 5× rows, because the cadence derivation is linear and in-memory); it is the verification
surface, called deliberately, and that is the right place for the cost.

---

### Chunk 02: One aggregate tool, both directions, currency-grouped (chunk B)

**Closes #21; advances but does NOT close #20.** `discovery-mcp-answer-scope.md` C4, re-pointed by
the tool-boundary norm. 🔴 The plan claimed #20 and had not earned it: #20's Expected also names
merchant/text, category and amount-range filters on `query_transactions`, and this chunk builds none
of them — so "did I get a refund from Walmart" is still unanswerable. Corrected here as well as in
the change-log, because a plan that outlives its branch is where the next reader checks what shipped.
🔴 **This chunk IS the norm's migration** — the one departure this plan records against
"never remove a field".

`spending_summary` filters `amount_minor < 0`, so inflows are not merely unaggregated, there is
**no row for them to appear in**. Cashflow-by-month is the same aggregate with inflows kept, which
is why the norm makes them one tool rather than two.

**Do:**

1. **`money_summary` replaces `spending_summary`.** One row shape, every field present for every
   grouping: `{group_key, group_label, currency, transactions, inflow_minor, outflow_minor, net_minor}`.
2. **`group_by` selects `category | merchant | account | month`** as a top-level keyword argument
   — **not** `params={...}` (#30's A2, carried over regardless of which half of #30's conflict
   survived).
3. 🔴 **Direction lives in the field name; sign lives in `net_minor`.** `inflow_minor` and
   `outflow_minor` are positive magnitudes; `net_minor` is operator-signed. `data-model.md`'s
   convention and the reporting convention meet in this row for the first time, and a row that
   mixed them silently would be the exact class of defect this cluster removes.
4. **Currency is a grouped field, never summed across** — the owner's 2026-09-08 ruling, made once
   for all aggregate tools. A window spanning two currencies returns per-currency rows. Conversion
   is explicitly not chosen.

**Done when:** the TRAVEL case reports $0 net against $12,000 gross and both figures are visible in
one row; a multi-currency window returns per-currency rows rather than one summed integer (or, if
the sandbox cannot express it, that limitation is recorded here rather than discovered later —
the discovery already flags this as vetoable); `spending_summary` is gone from
`_tool_definitions()` and nothing in `src/` still references it.

---

### Chunk 03: `flow_class`, so "spending" stops meaning three different things (chunk C)

**Closes #18.** `discovery-mcp-answer-scope.md` C3, landing on chunk B's row.

`LOAN_PAYMENTS` across 24 months is 100% credit-card repayment; not one dollar of servicing
appears for the four loan accounts carrying $158,275.89. Internal transfers and debt service are
counted as spending, and nothing says so.

**Do:** `flow_class` of `external_spend` / `internal_transfer` / `debt_service` on every
`money_summary` row, available as a `group_by` value, plus `total_external_spend`. Precedent to
follow is `balance_class` on `list_accounts`, which an acceptance tester named as the single field
that let it state net worth with no hedging.

🔴 **Trap, recorded so it is not rediscovered:** chunk C alone does **not** fix TRAVEL. That is
gross-versus-net and it is chunk B. Two independent acceptance passes reached the same $12,000 by
different routes — after C, treat a TRAVEL check as confirming the linkage, not as evidence the
fix failed.

### Amendment: the `rule-applied` assumption is FALSIFIED, and #34 stays open

The plan assumed that classifying rows would let chunk C emit `rule-applied` and close #34 as a
side effect. 🔴 **It does not, and the reason is the ruling itself.** `rule-applied` is defined in
the vocabulary, the decision table and the resources surface as *"an account rule filtered rows
out of an aggregate — the total excludes them ON PURPOSE"*. The owner's ruling on #18 is
**classify, do not filter**: nothing is dropped, precisely so there is no invisible undercount.
Emitting `rule-applied` here would be a false statement about the answer it rode on.

#34 stays open and is NOT closed by this chunk. The honest cheap fix remains what its triage says
— record the kind as not-yet in all three carriers — and that is separate work.

### Amendment: the category mapping, written before it was coded

The three values are ruled; the mapping onto them is not, and a taxonomy invented mid-build is a
tripwire. **Classified from `source_category_primary`**, the aggregator's own taxonomy which
`data-model.md` keeps unmodified in its own column:

- `LOAN_PAYMENTS` → **`debt_service`** (the measured double-count: mostly credit-card payoff,
  overlapping card purchases already counted in the spending categories)
- `TRANSFER_IN`, `TRANSFER_OUT` → **`internal_transfer`** (61% of the two-year total — $164,400 of
  $267,693 — is the holder moving their own money between their own accounts)
- everything else → **`external_spend`**

🔴 **Read from the source column, never from `category_override`.** An override is local
interpretation of what a transaction was *for*; the flow class is about whose money moved and in
which direction, and letting a re-categorisation silently reclassify a transfer as spending would
reintroduce the overcount through the back door.

### Amendment: `flow_class` joins the GROUP BY of EVERY grouping, not only its own

The plan says `flow_class` sits "on every row" *and* is "available as a `group_by` value", and
those two sentences only compose one way. 🔴 **`flow_class` is not a function of the group for any
grouping this tool offers.** A merchant can take a refund and a purchase; an account can hold a
transfer, a card payment and a coffee; a month holds all three by definition. Even *category* can
split, because the category key is `category_override`-first while `flow_class` is ruled to read
`source_category_primary` only — so an override that renames a transfer does not move its class.

So the field cannot be attached to a group that spans classes without picking one row's answer and
stating it for the others. The two ways out are an *optional* field and a *finer grain*, and
🔴 **the optional field is refused by name**: `api-contract.md` § Direction's fourth norm merges
tools only where "one strict row schema covers every parameter value with no optional fields", and
chunk D is about to make that refusal structural at registration. A tool that had to break the
norm to carry this chunk's field would falsify the norm in the same batch that enforces it.

**Ruled: the class is a grouping dimension always.** Every query groups by
`(group_key, group_label, currency, flow_class)`, so `flow_class` is present, non-null and true of
its row under all five values of `group_by`. `group_by=flow_class` is then the degenerate case
where the key *is* the class — a roll-up, not a different shape.

The cost is real and is accepted: **a month now returns up to three rows per currency where it
returned one.** That is the finding rather than a side effect. 61% of the two-year total is the
holder moving their own money, so a single monthly outflow figure is the exact number #18 says
cannot be read as spending; splitting it is what makes the month answerable at all. The tool
description carries the consequence, because a caller who sums blindly across a month's rows gets
back precisely the conflated figure they had before.

### Amendment: `total_external_spend` is per currency, and it does not ship alone

Two facts make the bare scalar the discovery names ("the response gains `total_external_spend`")
unwritable as one integer.

🔴 **One number would sum across currencies**, which the owner's 2026-09-08 ruling forbids for
every aggregate tool, and forbids it here for the reason it was made: a summed integer over two
currencies is not a wrong number, it is not a number. So the total is **per currency**, exactly as
the rows are.

🔴 **One number alone has nothing to check it against**, and the Done-when asks for agreement "by
construction rather than by coincidence". A lone external total agrees with the rows only if
someone re-derives it; the three class totals *together* agree with the window's total outflow by
partition, because every transaction has exactly one class. That identity is the check, it is
cheap, and it fails loudly if the classification ever drops a row instead of classifying it.

**The envelope gains `totals`: one entry per currency, carrying the OUTFLOW of all three classes.**
`external_spend_outflow_minor_units` is `total_external_spend` under a name that says which half of
the row it is — necessary because 🔴 **`external_spend` also holds external INCOME**: salary is
neither an internal transfer nor debt service, so it falls to "everything else" and lands there as
*inflow*. Taking the outflow half is what makes the field a spending figure rather than a net one;
the inflow half stays visible on the rows, where the refund-versus-purchase reading chunk B built
still works.

Emitting the sibling two is not scope the plan did not ask for. It is what turns `164400` from a
number a reader must go and find into the headline #18 is about — *"$267,693 moved, of which
$164,400 was you paying yourself"* — on every answer, whatever the grouping, which is the precise
sense in which "nothing says so" stops being true.

**Not-yet, stated rather than assumed:** an unclassified row (`source_category_primary` null)
falls to `external_spend`. That is the conservative direction on this surface's own principle —
an overcount gets questioned and an undercount gets believed — and it is measured at 0.00% null
in the sandbox, so it is a rule about the future rather than about today's data.

**Done when:** each of the three classes has a test over fixture rows that actually exercise it;
`total_external_spend` and the summed rows agree by construction rather than by coincidence.

---

### Chunk 04: The guards that make the norm self-enforcing, and the go-red cases nothing had (chunk D)

Two halves, both about proof rather than behaviour.

**Registration guards** — #30's carried obligations, made structural:

1. **A3** — two parameters sharing a name with different types are refused at registration, not at
   runtime.
2. 🔴 **Guardrail 1** — a tool whose row schema would need an *optional* field to cover every
   parameter value is refused at registration. This is what makes § Direction's fourth norm
   self-enforcing rather than a sentence the next builder has to remember, and it is why the norm
   survives the thirtieth capability. It belongs beside A3 because both fail for one reason: a
   surface that cannot be described strictly should not be advertised.

**The go-red gap, found by auditing rather than by a failure.** The harness holds 120 cases, and
their distribution does not match where the judgment calls live: 10 target `test_mcp.py`'s 100
tests, while `test_query_truncation.py` (36 tests) and `test_query_window.py` (22 tests) have
**none**. The eight cases mutating `query.py` prove the older semantics can fail — environment
naming, shortfall-as-warning, unmeasured ≠ no-shortfall, outflow-only — and nothing mutates the
clamp, `truncation`, `next_cursor`-iff-truncated, the window-scoped coverage sibling, or the
absence rules. Those are C1's deliverables, shipped one wave ago, with no proof any of their 58
tests can go red.

**Do:** a case per new guard, and cases closing the C1 gap above.

🔴 **Two operational rules, both from `learnings.md` and both already paid for once.** The harness
sabotages the working tree while it runs — no suite, no review, no commit during the window. And
it mutates by literal `str.replace`, so **assert the anchor applied before believing a green run**:
a mutation that never landed is indistinguishable from a guard that works, and this project has
been fooled by exactly that twice. Prefer a whole statement over a line fragment.

**Done when:** every new case is verified red for the right reason, with the anchor confirmed
applied rather than assumed.

**Done, 2026-09-09.** All **136** cases red, sixteen of them new; every anchor asserted present
before the run rather than inferred from its result.

🔴 **The audit found a seventeenth thing: a case that was stale in BOTH halves at once.**
"AC-4.2: spending sums outflow only" had its anchor moved by chunk B's reformat *and* named a test
chunk B had deleted along with `spending_summary`. Either alone would have been caught — the
harness reports a missing anchor as a SURVIVOR, which is the design working — but neither had been
run since the merge. Retargeted to `test_the_aggregate_reports_both_directions_as_magnitudes`
rather than deleted: the guarantee did not change, only the code and the test carrying it.

**The registration guards live in `_tool_definitions()`, not beside `serve`.** Written while
implementing, because "refused at registration" assumes a registration event this server does not
have: `tools/list`, the derived reference documents and every test each build the surface by
calling that function. A check at startup would be one three callers route around. In the producer,
no code path can obtain a definition nothing validated — which is also why the go-red case for it
is an `inspect.getsource` assertion rather than a behavioural one.

**Guardrail 1 is checked over ROWS only, and the scope was found by trying the stronger rule.**
"Every declared property is required, everywhere in the schema" is the tempting invariant and it
**refuses the shipped surface**: a warning carries `connection_id` and `institution` only when it
is about one connection. That is absence-as-information at the envelope level, which the norm does
not speak about — the norm is about the row shape a merge has to unify. Checked before the guard
was written rather than discovered by a red suite.

---

### Chunk 05: The claim-site sweep, and the artifacts that describe an eight-tool surface (chunk E)

🔴 **Amendment: this chunk could not be last, and the guard is what proved it.** The plan
sequenced E last so every count would be written against the final built set. But
`test_the_documented_tool_surface_is_the_built_one` derives *specified* from the contract's tool
table and asserts built ⊆ specified — so the moment chunk B renamed the tool, four of its cases
went red and the suite could not be green again until the table moved. The sweep is therefore
**atomic with the merge, not downstream of it**, and it landed inside chunk B's commit.

That coupling is the guard working exactly as designed: the specification and the code are not
allowed to disagree for even one commit. The plan's reasoning was sound and its ordering was
wrong, which is the kind of thing only the build finds.

**Originally: last, because every count it writes depends on the final built set.**

`test_the_documented_tool_surface_is_the_built_one.py` derives the *specified* set from
`api-contract.md`'s tool table and the *built* set from `_tool_definitions()`, then asserts
built ⊆ specified across three documents and ten claim sites. Chunk B removes a tool and adds
one; chunk A adds another. **The table and every spelled count move here, in one commit.**

**Do:**

1. The contract's tool table goes from ten rows to eight — `spending_summary` and
   `cashflow_summary` collapse into `money_summary`; `net_worth` and `balance_history` collapse
   into the time series. 🔴 **The time-series merge is specification only** — neither tool is
   built by this plan and the owner's 2026-09-08 ruling explicitly excludes them.
2. Every spelled count on all three surfaces, per the guard's own site list.
3. The "two of these ten are the verification surface" sentence, which becomes *of these eight* —
   and is now TRUE of the code for the first time, because chunk A builds the half that was missing.
4. `api-contract.md`'s descope amendment, § Surface Inventory tiers, the README status paragraph,
   and the client guide's tool table and not-built sentence.

**Done when:** the surface guard passes without its patterns being loosened. 🔴 **A guard that
stops matching FAILS rather than passing quietly, and that is the guard working** — if a claim
pattern must change, change the claim, not the pattern, and say why here.

---

## Assumptions, vetoable

- `[ASSUMPTION: money_summary over cashflow_summary or keeping spending_summary | LOW impact |
  user can override]` — argued above; the rename is confined to chunks B and E.
- `[ASSUMPTION: the multi-currency refusal path ships exercised only against a synthetic
  fixture, because the sandbox is single-currency | MED impact | user can defer #21]` — carried
  forward unchanged from `discovery-mcp-answer-scope.md`, which flagged it before C1 shipped.
- `[ASSUMPTION: emitting rule-applied from C3's classification is enough to close #34 | LOW
  impact | user can correct]` — if the classification ships without announcing itself as a rule,
  #34 stays open and must be closed in all three carriers instead.
- `[ASSUMPTION: no consumer depends on spending_summary's name or its spent_minor_units field |
  LOW impact | user can correct]` — single user, pre-production, `experimental` tier.

## Out of scope, and why

`balance_history`, `net_worth`, `find_recurring` and `list_holdings` stay unbuilt — the owner's
2026-09-08 ruling on #19/#20 excludes them, three are "analysis conveniences whose data is
already in the datastore" and `list_holdings` waits on build step 5. **Nothing here moves
`mcp-production-readiness.md`'s go/no-go**: #22 (pending settlement), #23 (sign convention on a
real inflow), multi-currency and closed accounts are the gate on real money, and this plan
finishes the answer-scope cluster rather than that gate.
