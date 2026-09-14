# Discovery — Specifying a defect only production can show you

**Work cycle:** production-data semantics · medium · requirement
**Opened:** 2026-09-09
**Addresses:** brookstalley/bankmachine #22 (pending-transaction semantics), #23 (sign convention on
a real inflow). 🔴 **Neither closes here, and neither closes on this document's authority** — see
§ *The gate*.

---

## 🔴 Status: integrated 2026-09-09; two gates still need a human

**AC-13.1–13.9 and AC-14.1–14.9 now live in `docs/system-requirements.md` § 7** and are in force as
requirements. This document remains their derivation, not their home.

It was drafted by a delegate, so its **assumptions are vetoable** — they are listed in
§ *Assumptions, vetoable*, and an assumption the owner rejects retires the criteria that rest on it.
That is the mechanism; requirements are not held in limbo awaiting a signature.

🔴 **The two closure gates are different, and they are not integrated in the same sense.**
VRF-005 and VRF-006 are **enqueued** in `.prawduct/operator-verification.md`, blocked until
production data is connected. Enqueuing them is not a commitment to perform them: **both commit the
owner to doing something with real financial data on a real household's accounts**, and only the
owner can decide to do that. AC-13.1–13.7 and AC-14.1–14.6 gate on none of it and are ordinary build
work today.

---

## The three questions

**What problem are we solving?** Two filed blockers assert that their subject matter cannot be
specified or verified until production data exists, and therefore sit at `stage: requirements` with
their Expected sections deliberately empty. That is a stable state, not a plan: nothing in either
issue says what would move it, so the go/no-go in `mcp-production-readiness.md` has two entries that
can never be discharged by any act anyone could schedule.

**What does success look like?** For each item: a behavioural contract that is specifiable *today*;
a named mechanism that verifies it; and a closure gate stating what must be observed, by whom, and
where it is recorded — so that "connect production data" and "trust the current-month figure" are
two separately-earned states rather than one hope.

**What is out of scope?** Refund/purchase netting (descoped by the 2026-09-08 ruling and unchanged
here). Multi-connection warning attribution (#24). Multi-currency (readiness precondition 4).
Account lifecycle (#40) — but see § *Overlap with #40*. No implementation and no tests are written
here.

---

## Why these two are one document

Both issue bodies carry the same sentence — *"Expected: deliberately unspecified — `stage:
requirements`"* — and the same deferral reason, verbatim: the sandbox has zero pending rows and one
pre-inverted payroll row, so *"building against it would produce code whose only test is a fixture
that cannot express the failure."*

That shared reason is the real subject. Answered twice it produces two mechanisms that disagree
about when a production-only claim counts as verified, and the disagreement surfaces the first time
someone tries to close one of them. The question is singular:

> **How does a defect that only real production data can reveal get specified, verified, and gated
> — before we trust the answers?**

#22 and #23 are two instances of it. Everything below answers it once and then applies it twice.

---

## 🔴 The finding that shapes the work: the block is much narrower than either issue claims

`learnings.md` § *Blocked is a claim, and it is usually wider than the truth* says to ask which part
of a step can be checked without the missing thing. Asked of these two, the answer is: most of it.
Six premises were checked against the source tree and the live sandbox datastore before anything
here was written. **Three held, two are false as stated, and one is false outright.**

### What held

**The sandbox has zero pending rows.** Counted, not quoted — 388 transactions,
`pending = 0` on all 388. Stronger than the issue states: `source_pending_transaction_id` is NULL on
**all 388**, and `removed_at` is NULL on **all 388**. So neither the pending→posted link nor the
soft-delete path has ever carried a row in this datastore. (Measured 2026-09-09 against
`store-sandbox.db` through the product's own read-only handle.)

**`data-model.md` § Transaction lifecycle states the contract the issue quotes.** It does, in a
diagram and in prose: a posting row *"UPDATES the pending row, matched on
`source_pending_transaction_id` — never inserts a second row"*, and *"a removed transaction is
retained with a removal timestamp. Nothing in this schema hard-deletes a transaction."*
`docs/system-requirements.md` AC-2.2 and AC-2.3 say the same in requirement form.

**The index exists.** `transactions_pending_link ON (account_id, source_pending_transaction_id)
WHERE source_pending_transaction_id IS NOT NULL` is declared in both `store/schema.py` and the
frozen DDL in `store/migrations/core_schema.py`.

### What is false as stated

🔴 **"The field's behaviour has never run" is true of the datastore and false of the code.** The
contract is **implemented** — `connector/plaid/derivers.py::_existing_transaction` resolves a
posting entry to its pending row, and `_write_transaction` updates rather than inserts — and it is
**tested**, by two dedicated tests that assert exactly the two risks #22 names:

- `test_a_posting_transaction_updates_the_pending_row` — asserts one row survives, the local
  `transaction_id` is preserved, and `pending` flips to 0.
- `test_replaying_a_posting_transaction_does_not_insert_a_second_row` — the rebuild-replay case.
- `test_a_removed_transaction_is_soft_deleted` and
  `test_a_transaction_removed_then_sent_again_is_present_again` — the soft-delete half.

These run against **hand-built aggregator payloads**, not against the sandbox fixture. So the
deferral reason both issues give — *"code whose only test is a fixture that cannot express the
failure"* — does not describe this code. Its tests are not the fixture. **This matters because a
polished issue body is more likely to be taken on trust, not less**, and the previous work cycle
found #39's central premise about the code to be false the same way.

🔴 **"The normalization path is never taken" is false — there is no path.**
`derivers.py::_operator_signed_amount` ends in `return negate(exact)`: an **unconditional** negation
with no data-dependent branch. There is no direction it can fail to run in. And the mirror case is
explicitly asserted — `test_a_deposit_reported_negative_is_stored_positive` exists precisely so that
*"the rule above would [not] be satisfied by code that simply made every amount negative."* The
sandbox itself exercises it: **49 of 388 rows are stored positive** (24 United Airlines refunds at
+$500, 25 interest credits totalling +$105.50), which means the aggregator sent 49 negative amounts
and negation produced 49 positive stored amounts. The arithmetic has run in the inflow direction 49
times.

The convention is also a **ratified norm** with a declared mechanism: `project-preferences.md`
carries *"every stored amount is signed from the operator's point of view"* as `Test`-enforced,
flipped to steady state 2026-09-08 with both go-red runs recorded.

### What is false outright

🔴 **The index does not serve the match, and nothing reads it.** `data-model.md` line 341 says
`transactions_pending_link` exists so *"a posting transaction finds its pending row by the source's
own pending identifier"*, and #22 repeats it. The lookup does not do that.
`_existing_transaction` finds the pending row with:

```
transactions.c.account_id == account_id,
transactions.c.source_transaction_id == pending_source_id
```

— it matches the incoming `pending_transaction_id` against the pending row's **own**
`source_transaction_id`, which is served by the unique index `transactions_source_identity ON
(account_id, source_transaction_id)`. A grep for `source_pending_transaction_id` across `src/`
returns four sites: the two schema declarations, one write in the values dict, and one assertion in
a test. **Zero reads.** `transactions_pending_link` is write-side cost with no reader, and the
document that justifies it describes a query that does not exist.

The code is *correct* — matching the pending row by its own id is right, and the docstring on
`_existing_transaction` explains why the ordering matters for replay. The **specification is wrong
about how**, and a future builder optimising against `data-model.md` would tune an index nothing
reads.

---

## The three tiers, which is the whole design

Once the premises are checked, "unverified" resolves into three tiers that differ **in kind**, not
in degree. Conflating them is what produced two unclosable issues.

| Tier | What it is | Can a fixture settle it? |
|---|---|---|
| **1 — the read path** | No pending row has ever reached `query.py`. No aggregate distinguishes pending from posted. No answer says whether its total includes holds. | Yes, entirely. No new data needed. |
| **2 — this system's branch logic** | Settlement cases the existing tests do not cover: the amount changing on settlement; the pending row arriving in `removed` in the same page as its posting; a hold that expires without posting. | Yes — and legitimately, see below. |
| **3 — claims about the outside world** | Whether a real institution's delivery sequence matches the assumed one; whether a second institution's feed obeys the same sign convention as the first. | **No.** A fixture here is this project asserting what the aggregator does and then verifying against its own assertion. |

**Tier 1 is the largest and the most surprising.** It is the house's own defect class — *an answer
that does not state its own scope*, the finding that organised `discovery-mcp-answer-scope.md` — and
it has no fixture obstacle whatsoever. Measured: `pending` appears **nowhere** in `query.py` except
as a projected column in the transaction row (`query.py:743, 762`). No aggregate filters it, flags
it, or counts it. `api-contract.md` contains **no prose about pending at all**. So today
`money_summary` silently mixes authorisation holds into a spending total, and `mcp.py` publishes
`"pending": {"type": "boolean"}` on a row schema with no statement anywhere of what a total
containing them means. In production, where pending rows are constant and dominate any
"what did I spend this week" answer, that is precisely a plausible wrong number with no signal —
and it is buildable this week.

**Tier 2 is where the issues' objection was over-generalised, and this is the objection answered
head-on.** Both issues argue against "code whose only test is a fixture that cannot express the
failure." That argument is sound, and it lands on tier 3. It does not reach tier 2, and the
distinction is **who owns the claim**:

> A fixture is illegitimate when it *asserts what the aggregator does* and then verifies the code
> against that assertion. It is legitimate when it *stipulates an input* and checks what **this
> system's** branch does with it.

`test_a_posting_transaction_updates_the_pending_row` is already the second kind, and it is the test
#22 would be asking for if it knew it existed. Extending it to a settlement whose amount changed
makes no new claim about Plaid; it checks whether *our* UPDATE carries the new amount. That question
has a right answer knowable from `data-model.md` alone. **So yes, this document recommends extending
the synthetic fixtures — for tier 2 only, and never as evidence about tier 3.**

The distinction is not academic. The existing settlement tests use `amount="89.40"` on **both** the
pending entry and the posting entry. The ordinary real settlement — a restaurant tip, a fuel hold, a
hotel incidental — changes the amount, and that case is untested today. It is tier 2 and it is free.

---

## The verification mechanism — recommendation

Four options were weighed. The recommendation is **layered by tier, with a single load-bearing
choice**, and it is a recommendation rather than a survey because three of the four are already
half-built in this repo.

### 🔴 Primary: a runtime invariant on the success path, promoted to a regression fixture from the production archive

**The invariant, first.** A one-off operator observation on day one does not protect month seven,
when a fifth institution is enrolled. Tier 3 defects cannot be verified *before* the fact — they can
only be **detected when they happen**. And this product already has the right channel for that:
`api-contract.md` § Direction's norm that *incompleteness rides the success path as a warning field*,
implemented as `envelope.WARNING_KINDS`. An invariant that fires as a request-scoped caveat is the
same mechanism `window_starts_before_coverage` and `accounts_without_coverage` already use, and it
needs no new concept — only two new kinds in a vocabulary designed to be additive.

**Then the promotion, which is what stops this being an operator ritual forever.** The product
archives every raw aggregator response (`raw_responses`) and AC-5.2 already replays them
byte-identically. **A real production sync's own archive is a recorded cassette.** So the
cassette-replay option does not need building — it needs *authorising*: when the first real pending
settlement and the first real conventionally-signed deposit are observed, their raw pages are
extracted, redacted against `tests/preferences/check-no-personal-data.sh`, and committed as
regression fixtures. From that commit forward, tier 3 is machine-checked like everything else, and
the two blockers stop being perpetual.

That pairing is the answer to the shared design question. **Detect it at runtime, because you cannot
predict it; capture the first real instance, because after that you can.**

### The other three, and why each is subordinate rather than rejected

**Extended synthetic fixture** — recommended for tiers 1 and 2, refused for tier 3, on the
claim-ownership rule above. It is cheap, it closes the largest tier, and the issues' own objection
does not reach it.

**Recorded-cassette replay** — recommended, and it is the *same* thing as the promotion step. Built
from scratch it would be a new mechanism; built from `raw_responses` it is a redaction pass and a
fixture file. Do not build a second archive.

**Operator-run verification against live data** — necessary, insufficient, and **already written**.
`mcp-production-readiness.md` § "Day one in production" checks **3** and **5** are exactly these two
items ("check the sign of one known paycheck and one known bill"; "watch one pending transaction
across settlement"). What is missing is not the procedure but the **recording discipline**: nothing
says where the observation goes, so nothing can ever cite it as discharged. That is a gap in the
carrier, and `.prawduct/operator-verification.md` is the carrier this repo already uses for
"correctness a test cannot speak to." Two VRF entries, not a new mechanism. See § *Shared-artifact
deltas*.

### The invariant that was designed and then discarded, recorded so it is not re-proposed

The obvious sign check is a **per-row category/sign disagreement detector**: a row categorised
`INCOME` or `TRANSFER_IN` whose stored amount is negative. It was measured against the fixture
before being proposed, and it fails.

Measured category × sign over all 388 rows: `TRANSFER_IN` is positive on all 25; `TRANSFER_OUT`
negative on all 48; every consumer-spend category negative throughout; `TRAVEL` carries **both**
signs legitimately (24 charges and 24 refunds). **The disagreement count is zero.** Including the
GUSTO row — its `source_category_primary` is `TRANSFER_OUT` and its detailed category is
`TRANSFER_OUT_ACCOUNT_TRANSFER`, so the aggregator calls it an outflow in *both* structured fields
and only the free-text description says "Credit". Round 2's adjudication was right, and this
confirms it from a second direction.

So a per-row detector would ship with **no positive case anywhere in the data it runs on**, an
unknown false-positive rate (TRAVEL proves a category can legitimately carry both signs), and a
go-red case that exists only in a unit test — `learnings.md` § *Guarantees by construction*'s "a
check that cannot fail hides every problem in its blast radius." Rejected.

**What replaces it is a population-level, per-connection check.** The real tier-3 risk for #23 is
not one mis-signed row; it is that the negation is a **global constant applied to every feed**, and
institution B may not obey institution A's convention. Over the categories that are never plausibly
inflows — `FOOD_AND_DRINK`, `GENERAL_MERCHANDISE`, `PERSONAL_CARE`, `RENT_AND_UTILITIES`,
`TRANSPORTATION` — the sandbox's single connection is **219 of 219 stored negative**. A connection
whose same-category rows come back predominantly positive has an inverted feed, and at
population scale the signal is near-total rather than marginal. That baseline is measurable today
and is the negative control; the positive control is a synthetic tier-2 case.

**And the sandbox genuinely cannot exercise it, because it is single-connection.** That is #23's
true blocked claim, and it is much narrower than "the normalization has never run."

---

## Proposed requirements

House style, in the reserved blocks. 🔴 **All proposed; none ratified.** On placement, see
§ *Shared-artifact deltas* — the integrator, not this document, decides where they live.

### AC-13.x — pending-transaction semantics (#22)

**AC-13.1 · Pending is disclosed in the payload.** Every aggregate answer states how many of its
contributing rows are `pending` and their signed magnitude, as an **always-present** field — present
and zero, never absent. A total that mixes authorisation holds with settled amounts and does not say
so is the defect class this surface exists to refuse.

**AC-13.2 · Settlement may change the amount.** A posting entry whose amount differs from its
pending row updates the amount in place; the row count stays one and the local `transaction_id`
survives. The hold amount is not retained as a second figure.

**AC-13.3 · Order-independence across the change lists.** A pending row delivered in `removed` in
the same page as, or a later page than, its own posting resolves to exactly **one** non-removed row,
whichever order the three change lists are applied in and however the pages are split.

**AC-13.4 · An expired hold is attributable, not silent.** A pending row that is removed without ever
posting is retained with `removed_at` (AC-2.2, unchanged) **and** its exit is visible: a total that
shrank because a hold dropped off is distinguishable by the consumer from a total that shrank
because the data is incomplete.

**AC-13.5 · A stranded hold is reported.** A row still `pending` beyond a declared age threshold is
surfaced on the verification surface. The threshold is declared with its derivation, per the
precedent that a fixed constant against a variable cadence produces findings and no signal
(AC-9.1/AC-11.1's amendment).

**AC-13.6 · The pending link is described as it is implemented.** `transactions_pending_link` either
serves a query that exists or is removed. Schema, `data-model.md` § Constraints, and the resolving
query state **one** account of how a posting row finds its pending row.

**AC-13.7 · The read path is exercised against pending rows.** At least one query-layer and one
MCP-layer fixture carries `pending: true` rows. Every such fixture in the suite today hardcodes
`pending: False`, so no read-path assertion can currently distinguish correct handling from none.

**AC-13.8 · One real settlement is observed and recorded.** *(Production; gating — see § The gate.)*
A pending row is noted while pending and re-queried after it posts: it appears exactly once, at the
settled amount, with the local id preserved. Recorded in `.prawduct/operator-verification.md`.

**AC-13.9 · The observed settlement becomes a regression fixture.** The raw
`/transactions/sync` pages spanning that settlement are extracted from `raw_responses`, redacted,
and committed, and AC-13.2 and AC-13.3 are re-asserted against them.

### AC-14.x — sign convention on a real inflow (#23)

**AC-14.1 · The convention is a per-feed claim and is stated as one.** The operator-signed
convention rests on a measured premise about **one** aggregator on **one** connection
(`api-notes-plaid.md` §17). `data-model.md` § Direction records it as a claim whose scope is
per-connection, so a second institution is a new observation rather than a covered case.

**AC-14.2 · Per-connection sign-convention check.** Over a declared set of never-plausibly-inflow
categories, the stored sign distribution is computed **per connection**. A connection whose
distribution is inverted relative to the convention is reported.

**AC-14.3 · The check declares its category set, its threshold, and its controls.** The negative
control is the measured sandbox baseline (219 of 219 negative on one connection, 2026-09-09); the
positive control is a synthetic inverted feed. A check with no observed go-red is not coverage.

**AC-14.4 · An inverted feed is reported, never silently corrected.** Auto-inverting a suspect
connection is a heuristic that can be wrong in the direction that *understates* spending — the
argument that decided #18 and #20. The system refuses and names the connection.

**AC-14.5 · No aggregate is computed over a flagged connection without saying so.** The finding
rides the success path as a warning kind, not the error channel and not a log line
(`api-contract.md` § Direction).

**AC-14.6 · Free-text description is never a sign oracle.** The sandbox's payroll row reads
"ACH Electronic Credit" and is categorised `TRANSFER_OUT` in both structured fields. Round 2's
adjudication — faithful passthrough — is settled, and this criterion exists so it is not re-derived
from the description a fourth time.

**AC-14.7 · One real deposit and one real debit are checked by eye and recorded.** *(Production;
gating.)* A known paycheck reads positive and a known bill negative, against amounts the operator
already knows. Recorded in `.prawduct/operator-verification.md`.

**AC-14.8 · The check is exercised across at least two institutions.** *(Production; gating.)* The
per-connection check is meaningless on one connection, which is exactly why the sandbox cannot
exercise it. Two connections at different institutions is the minimum that makes AC-14.2 an
observation rather than a tautology.

**AC-14.9 · The observed deposit becomes a regression fixture.** Its raw page is extracted from
`raw_responses`, redacted, and committed, so the inflow direction is machine-checked from that
commit forward.

---

## The gate

🔴 **Proposed. Each of these commits the owner to an act against real accounts.**

**What is gating and what is not.** AC-13.1–13.7 and AC-14.1–14.6 are buildable now and gate
*nothing* on production — they should ship as ordinary work and are not what holds these issues
open. The gating criteria are AC-13.8/13.9 and AC-14.7/14.8/14.9, and they are the only ones that
require the owner to do anything with real money.

**#22 closes when**, and not before:

1. AC-13.1 through AC-13.7 have shipped and been reviewed.
2. **The owner** has watched one pending transaction across settlement on a real account and
   recorded the observation as **VRF-005** in `.prawduct/operator-verification.md`, drained with
   `prawduct-hook verify-operator-verification VRF-005`. This is readiness-doc day-one check **5**,
   given a home.
3. The raw pages of that settlement are committed as a redacted fixture and AC-13.2/13.3 assert
   against them (AC-13.9).

**#23 closes when**, and not before:

1. AC-14.1 through AC-14.6 have shipped and been reviewed.
2. **The owner** has checked one known paycheck and one known bill by eye and recorded the result as
   **VRF-006**. This is readiness-doc day-one check **3**, given a home.
3. The per-connection check has been run across **at least two** institutions (AC-14.8) and its
   output recorded in the same entry.
4. The observed deposit's raw page is committed as a redacted fixture (AC-14.9).

**What flips the go/no-go.** `mcp-production-readiness.md`'s verdict turns on preconditions 5 and 7
among others. This document proposes those two entries be annotated with the split above, so the
document says plainly what it currently leaves implicit: **connecting production data is permitted
before either closes; trusting a current-period spending figure is not.** Step 2 of each gate cannot
happen until production data is connected, so a readiness list that reads as a precondition to
connecting is unsatisfiable by construction. That ordering is the single thing most likely to be got
wrong by a reader in six months.

**Who records.** The operator, in the VRF entry, in the shape VRF-002 and VRF-004 already use — a
`**Verified:** <date>` line and the actual observed output pasted in, not a tick. VRF-002's own
record is the model: it found item 7's premise wrong and said so, and the correction was worth more
than the item.

---

## Overlap with #40 (account lifecycle) — flagged for audit

Another agent is writing #40 in parallel. Three points of contact, none of them a contradiction as
far as this document can see, all worth an integrator's eye:

1. **AC-14.2's per-connection scan must skip retired connections.** A connection retired mid-history
   still holds rows; scanning it produces a finding about a feed nobody is syncing. `connections`
   already carries `status`/`retired_at`, so this is a predicate, not a feature — but it is a
   predicate #40's work may be moving.
2. **AC-13.5's stranded-hold report must not fire on a closed account.** Same shape as
   `data-model.md`'s existing rule that *"a retired account's dormant period must not read as a
   permanent coverage gap"* — a pending row on an account closed since is not a stranded hold, it is
   history. If #40 introduces a lifecycle predicate for the coverage report, AC-13.5 should reuse it
   rather than write a second one; two producers of the same fact can disagree, which is the
   argument `discovery-mcp-tool-surface.md` already made about `_account_coverage`.
3. **Both add fields to a verification surface #40 is also changing.** AC-13.5 and AC-14.2 both
   report per-account or per-connection findings, and `get_coverage_report` is the natural home.
   Whether they land there or on `get_pipeline_health` is a shape question under
   `api-contract.md` § Direction's boundary norm, and it should be decided once across both
   discoveries rather than twice.

---

## Assumptions, vetoable

- `[ASSUMPTION: the aggregator delivers a posting transaction whose pending_transaction_id equals a
  transaction_id it previously delivered — which is what _existing_transaction relies on | HIGH
  impact | user can defer to AC-13.8's observation]` — `api-notes-plaid.md` §16 records that
  `Transaction.pending_transaction_id` exists and is what AC-2.3 matches on. It does **not** record
  an observed end-to-end settlement in which that equality held. This is the single largest
  unverified premise in the pending path, and AC-13.8 is the observation that settles it.
- `[ASSUMPTION: a redacted extract of real transaction pages can be committed under this repo's
  no-personal-data rule | HIGH impact | user can veto, which would remove AC-13.9 and AC-14.9]` —
  `check-no-personal-data.sh` is the arbiter and the roster tokens are gitignored, so the mechanism
  exists. But VRF-002 already found one fixture premise wrong on exactly this axis, and a real
  household's transaction page is a much harder case than an institution catalogue. **If the owner
  vetoes this, both items stay perpetually operator-verified and the runtime invariants become the
  only durable protection** — which is survivable, and should be a deliberate choice rather than a
  discovery made at commit time.
- `[ASSUMPTION: two new request-scoped warning kinds are additive and need no version bump | LOW
  impact | user can override]` — `envelope.py` states the vocabulary is additive by
  `api-contract.md`'s own evolution rule and that AC-9.3's list is a minimum. Both new kinds would
  need adding to `REQUEST_SCOPED_KINDS`, which `test_the_warning_vocabulary_is_closed.py` enforces.
- `[ASSUMPTION: AC-13.5's stranded-hold threshold is a declared constant rather than derived from
  the account's cadence | MED impact | user can correct]` — unlike a coverage gap, a hold's lifetime
  is a property of the card network rather than of the account's spending rhythm, so the AC-9.1
  cadence argument does not obviously transfer. Stated as an assumption because it is the kind of
  constant this project has already been bitten by once.
- `[ASSUMPTION: `transactions_pending_link` should be REMOVED rather than have a reader written for
  it | MED impact | user can override]` — nothing needs the reverse lookup today. Removing an index
  is a migration, so the cheaper resolution of AC-13.6 may be to correct `data-model.md` and leave
  the index; the criterion deliberately permits either, and the choice is the builder's.

---

## Requirements confidence

**High** on the finding and on tiers 1 and 2. Every premise was checked against the source tree or
measured against the live datastore rather than quoted from the issues, and three of six did not
survive. AC-13.1–13.7 and AC-14.1–14.6 rest on contracts that already exist in
`docs/system-requirements.md` and `data-model.md`, and on counts taken today.

**Medium** on the gating criteria (AC-13.8/13.9, AC-14.7/14.8/14.9). They are procedural
commitments against real accounts, and their shape depends on the two HIGH-impact assumptions above
— whether the delivery-sequence premise holds, and whether real pages can be committed at all. If
the second is vetoed, the fixture-promotion half of the recommendation falls and the gates become
recurring operator checks rather than one-time ones. That is a materially different proposal and the
owner should be the one to make it.

**Low** on nothing — but note what this document does **not** establish: no production data was
observed in producing it, and every claim about what production will do is reasoned from
`api-notes-plaid.md` and the aggregator's documented behaviour. That is the same epistemic position
`mcp-production-readiness.md` § "Where my confidence comes from" describes, and it is why the
recommendation leans on runtime detection rather than on prediction.

---

## Shared-artifact deltas for the integrator

🔴 This document edited **no** file but itself. Every change below is proposed, unapplied, and
subject to the ratification notice at the top.

### 1. `docs/system-requirements.md` § 7 — placement of the two AC blocks

**Anchor:** after **AC-11.8 · Shortfall recording**, before the `---` that closes § 7.

**Proposed:** the AC-13.x and AC-14.x blocks above belong in § 7, because § 7 *is* the verification
gate and both items are gate criteria. 🔴 **I did not renumber them into AC-11**, per the reserved-id
constraint — but the integrator should consider whether AC-13.8/13.9 and AC-14.7/14.8/14.9 are more
honestly numbered as `AC-11.9`, `AC-11.10`, … since they are operator-run gate checks of exactly the
kind AC-11.6 and AC-11.7 already are, while AC-13.1–13.7 and AC-14.1–14.6 are ordinary build
requirements that belong nearer §§ 2 and 5. Splitting them across two homes is probably right and is
the integrator's call.

### 2. `docs/system-requirements.md` — amendment note under AC-11.3

**Anchor:** immediately after the AC-11.3 paragraph.

**Read AC-11.3 first:** *"Zero duplicate transactions, tested across the pending→posted transition,
across a re-sync, and across overlapping file imports."* It already states the dedup contract #22
leads with. **My job is to say what it does not cover, not to restate it.** Proposed text:

> **Amendment (2026-09-09, proposed).** AC-11.3 covers *duplication* across the pending→posted
> transition, and that half is implemented and tested
> (`tests/connector/test_transaction_derivers.py`, the two AC-2.3 cases). It does **not** cover
> three things #22 is actually about, and each is a separate failure from a duplicate:
> a settlement that **changes the amount** (the ordinary case — a tip, a fuel hold — untested today,
> since both existing cases use the same amount on the pending and posted entries); a hold that
> **expires without posting**, which is a disappearance rather than a duplication; and whether a
> total **discloses** how much of itself is pending. Zero duplicates is compatible with all three
> going wrong. See `.prawduct/artifacts/discovery-production-data-semantics.md`.

### 3. `.prawduct/artifacts/data-model.md` — correct the index claim

🔴 **APPLIED 2026-09-09 — do not apply again.** Applied as a *flagged defect* rather
than as a rewrite: the line now records that the described query does not exist, and that
whether the index goes or the lookup moves onto it is open under AC-13.6. Describing the
index as merely unused would have recorded a defect as if it were the design.

**Anchor:** the bullet at line 341, § Constraints:

```
- `transactions_pending_link ON (account_id, source_pending_transaction_id) WHERE source_pending_transaction_id IS NOT NULL`
  — AC-2.3: a posting transaction finds its pending row **by the source's own pending identifier
  ...
```

**Proposed replacement:**

> - `transactions_pending_link ON (account_id, source_pending_transaction_id) WHERE
>   source_pending_transaction_id IS NOT NULL` — 🔴 **Correction, 2026-09-09: nothing reads this
>   index.** AC-2.3's match is served by `transactions_source_identity`: a posting entry's
>   `pending_transaction_id` is compared against the pending row's own `source_transaction_id`
>   (`connector/plaid/derivers.py::_existing_transaction`), which is right — the pending row answers
>   to its own id until it posts. This index would serve the *reverse* lookup ("which posted row came
>   from this pending id"), which no query performs. It is retained or dropped under AC-13.6; the
>   sentence claiming it serves the pending→posted match was wrong and is withdrawn.

### 4. `.prawduct/artifacts/data-model.md` § Direction — scope the sign norm

**Anchor:** the norm *"every stored amount is signed from the operator's point of view."*

**Proposed addition** (AC-14.1):

> **Scope (2026-09-09, proposed).** The negation this norm rests on is **unconditional** — every
> aggregator amount is negated on the way in — and the premise it rests on is measured on **one
> aggregator, one connection** (`api-notes-plaid.md` §17). The norm is therefore a per-feed claim:
> a second institution is a new observation, not a covered case. AC-14.2's per-connection check is
> the mechanism; until it has run across two institutions the norm holds for one feed only.

### 5. `.prawduct/artifacts/mcp-production-readiness.md` — annotate preconditions 5 and 7

🔴 **APPLIED 2026-09-09 — do not apply again.** Merged with the sibling document's
delta 4 into a single correction stating closability and buildability separately, as the
review required. The "misleading as written" framing was dropped: that block already draws
the connect-vs-trust distinction correctly.

**Anchor:** the `> **State on 2026-09-09:**` tracking block in § "What has to be true before yes".

**Proposed addition:**

> **Narrowed 2026-09-09.** Both remaining production-data items were checked against the source tree
> and the live datastore, and both are narrower than this list states. **Item 5:** the pending→posted
> contract is implemented and unit-tested (two dedicated cases); what is untested is a settlement
> whose *amount changed*, an expired hold, and — the largest part — the read path, which has never
> seen a pending row and does not disclose one. **Item 7:** normalization is an *unconditional*
> negation with no branch, and the sandbox already exercises it in the inflow direction 49 times; the
> genuinely blocked claim is narrower and is that the sandbox is **single-connection**, so a
> per-connection sign check cannot run at all. On ordering, this block already draws the
> connect-vs-trust distinction one sentence above, and that sentence is correct as written; what is
> left implicit is that the **numbered list** above it still reads as preconditions to *connecting*.
> Full derivation, proposed requirements and proposed gates:
> `.prawduct/artifacts/discovery-production-data-semantics.md`.
>
> 🔴 **This paragraph has a second proposed correction, from
> `discovery-account-lifecycle.md`, covering item 6.** The two are complementary and must be applied
> as one edit: that one shows item 6 is a population path plus a migration rather than a read-path
> change, and that it is closable *before* production data because a shrinking roster replays from
> the archive. Item 6 being the buildable one and items 5 and 7 being partly buildable are not in
> tension — 5 and 7 have build-side criteria that gate on nothing, and gating criteria that cannot
> close until real data is connected.

### 6. `.prawduct/operator-verification.md` — two new entries

**Anchor:** append after VRF-004.

```markdown
## VRF-005 — one real pending transaction watched across settlement

**Chunk:** production-data semantics (#22) · **Raised:** 2026-09-09 · **Status:** pending

**Why a human:** the sandbox has zero pending rows and has never had one — `pending` is 0 on all
388, `source_pending_transaction_id` is NULL on all 388. The deriver's pending→posted branch is
unit-tested against hand-built payloads, which proves what *this system* does with a stipulated
input and says nothing about what the aggregator actually sends. Only a real settlement can show
the delivery sequence.

**To verify**, once at least one production connection is syncing:

1. Make a card purchase you will recognise. Within a day, find it: it should appear with
   `pending: true`. Note the amount, the date, and the local `transaction_id`.
2. Ask a spending question covering that day and note the total.
3. After it posts (typically 1-3 days), re-query. Confirm **exactly one** row for that purchase —
   not two — that `pending` is now false, and that the local `transaction_id` is **unchanged**.
4. 🔴 **Confirm the amount is the settled one.** If the hold and the settlement differ (a tip, a
   fuel hold), the row must carry the settled figure. This is the case the existing tests do not
   cover — both use the same amount on both sides.
5. Re-ask the question from step 2 and confirm the total moved by exactly the difference, and that
   nothing else changed.
6. Paste the two observations below, and record the `raw_response` ids of the sync pages spanning
   the transition — those are what AC-13.9 promotes to a regression fixture.

**Drain with:** `prawduct-hook verify-operator-verification VRF-005`

## VRF-006 — the sign convention on a real inflow, across two institutions

**Chunk:** production-data semantics (#23) · **Raised:** 2026-09-09 · **Status:** pending

**Why a human:** normalization is an unconditional negation, and it is already exercised in both
directions by the suite and by the sandbox (49 of 388 rows are stored positive). What no test can
speak to is whether a **real institution's** feed obeys the convention the negation assumes — and
the sandbox is single-connection, so a per-connection comparison is impossible there. This needs
ground truth about money you already know about.

**To verify**, with at least two production connections at **different** institutions:

1. Pick a paycheck you know the amount of. Confirm it reads **positive** and matches to the cent.
2. Pick a bill you know the amount of. Confirm it reads **negative** and matches to the cent.
3. Repeat both at the second institution. 🔴 **This is the step the sandbox cannot rehearse**, and
   it is the whole point of the entry: one feed obeying the convention is not evidence about
   another.
4. Run the per-connection check (AC-14.2) and paste its output. A connection whose
   never-plausibly-inflow categories come back predominantly positive has an inverted feed;
   the sandbox's measured baseline on one connection is 219 of 219 negative.
5. 🔴 Do **not** use a transaction's description text to judge its direction. The sandbox's payroll
   row reads "ACH Electronic Credit" and is categorised `TRANSFER_OUT` in both structured fields;
   that was adjudicated as faithful passthrough and is settled.
6. Record the `raw_response` id of the page carrying the observed deposit (AC-14.9).

**Drain with:** `prawduct-hook verify-operator-verification VRF-006`
```

### 7. `.prawduct/artifacts/api-contract.md` — pending disclosure and two warning kinds

**Anchor:** the aggregate-row shape in § Surface Inventory (`money_summary`), and the warning
vocabulary section.

**Proposed:** (a) the `money_summary` row and/or its `totals` block gains an always-present pending
disclosure per AC-13.1 — the field name is the builder's, but *absent* must not be one of its
states, per `discovery-mcp-tool-surface.md`'s guardrail 1; (b) two request-scoped warning kinds are
added, one for an answer whose rows include pending holds and one for an answer computed over a
connection flagged by AC-14.2. Both are additive under the contract's own evolution rule.

### 8. `src/bankmachine/envelope.py` — `REQUEST_SCOPED_KINDS`

**Not edited** (source is out of bounds for this pass). The two kinds from delta 7 must be added to
`REQUEST_SCOPED_KINDS`, or `test_the_warning_vocabulary_is_closed.py` fails and — worse, per its own
docstring — a validating client rejects the whole answer. Flagged so it is not discovered at build
time.

### 9. GitHub issues #22 and #23

**Not modified** (out of bounds for this pass). Proposed for the main agent: both bodies contain
premises this document found false — #22's index claim and its "never run" framing, #23's "the
normalization path is never taken." A correcting comment on each is worth more than a stage
transition, because the next reader will otherwise inherit the same three false premises. The stage
move to `design` is the main agent's, and only after ratification.
