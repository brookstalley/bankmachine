---
artifact: build-plan
version: 2
scope: transaction-filters
branch: feature/transaction-filters
depends_on:
  - artifact: api-contract
    file_path: .prawduct/artifacts/api-contract.md
  - artifact: system-requirements
    file_path: docs/system-requirements.md
  - artifact: discovery
    file_path: .prawduct/artifacts/discovery-mcp-answer-scope.md
governed_by:
  - artifact: api-contract
    dispositions:
      - "the MCP surface is read-only; no mutation tool reaches a derived row → inapplicable because every deliverable is a predicate on an existing read path; no tool is added and nothing writes"
      - "incompleteness rides the success path as a warning field, never an exception → conforms, and Chunk 02 is an application of it: a text search that misses a row under an abbreviated description is a successful response computed over less than the caller thinks, so every answer to a search carries `search_is_literal`. Additive by the vocabulary's own evolution rule"
      - "CLI three-way exit code → inapplicable because nothing here touches the CLI"
      - "a tool's boundary is drawn where the answer shape changes → conforms; four new OPTIONAL arguments on `query_transactions`, the row schema is untouched, and no tool is added. `_refuse_colliding_parameters` must still pass: none of `category`, `search`, `min_amount_minor_units`, `max_amount_minor_units` is declared by another tool with a different type"
      - "a stored balance is reported with its lifecycle → inapplicable because no total over balances is emitted or changed"
      - "🔴 window-scoped coverage rides BESIDE the store-wide figure, never replacing it → BINDING CONSTRAINT ON BOTH CHUNKS. `coverage.transactions_in_effective_window` counts the WINDOW, not the request. The filters narrow `truncation.matching`, which is defined as what the request selects, and must NOT narrow the coverage figures — a caller comparing the two is how it sees what its filter excluded. A filter that leaked into `_coverage` would repurpose a shipped field"
      - "🔴 a cursor carries a fingerprint of the predicate it was issued for → BINDING, and extended rather than restated: every new filter joins the fingerprint, or page two of a `category=TRAVEL` walk resumes against `category=FOOD_AND_DRINK` and answers a question nobody asked, with a payload that reads as a continuation"
      - "raw-row cap ~500, hard → conforms; unchanged"
  - artifact: data-model
    dispositions:
      - "every stored amount is signed from the account holder's point of view → conforms, and it is the owner's ruling for the amount range: the bounds compare against the SIGNED amount, the same sign every row carries, so a reader can check each row against the bound it asked for"
      - "all monetary values are integer minor units, no floats → conforms; both bounds are narrowed as whole numbers and a float or a bool is refused, never truncated"
      - "`category_override` is operator-owned and wins over the source category → conforms; the filter matches the EFFECTIVE category, override first, which is exactly the expression `money_summary` groups by"
      - "a transaction is never hard-deleted; removal is a soft delete → conforms; the filters join the shared predicate list, which already excludes removed rows"
      - "a migration's DDL is frozen once written → inapplicable because no schema changes and no index is added (see the latency step in Chunk 02 for when that would change)"
      - "calendar dates and UTC instants are distinct types and never mix → inapplicable because the only dates read are the existing window bounds, untouched here; every new predicate compares a string or an integer"
      - "every silver row carries the evidence for which it is → inapplicable because this plan writes no row; the filters read derived rows as they stand"
      - "the daily balance and holdings series are append-only → inapplicable because this plan neither reads nor writes either series"
partition: serial — both chunks edit `query._transaction_filters`, `query.list_transactions`, the `query_transactions` definition in `mcp.py` and the cursor fingerprint in `envelope.py`; 02's `search` extends the filter value 01 introduces, so a second delegate would be writing into a type the first had not settled.
last_validated: 2026-09-14
---

# Build plan — filters on `query_transactions`, so a refund can be found

Closes what is left of brookstalley/bankmachine#20. The owner's 2026-09-08 ruling on that issue is
**reachability only, no netting**, and two of its three deliverables have shipped: `money_summary`
(which absorbed the specified `cashflow_summary`) and cursor pagination. What remains is the
filters AC-9.1 names for `query_transactions` — **category, amount range, merchant search** — whose
absence is recorded as a descope in `api-contract.md` and advertised to every agent in the server's
own instructions: *"THIS SERVER CANNOT ANSWER: … any filter on amount, text or category."* So
"did I get a refund from Walmart?" has no route today except paging the whole window by hand.

## Advisory — what I would do differently

**On scope: nothing. The remaining piece is small and well bounded, and the backlog's `effort: L`
priced the whole bundle rather than this remainder — M is the honest size.**

**The risk the requirements do not price is the false negative, and it is why Chunk 02 is not only
a `LIKE`.** A text filter is the first thing on this surface whose *miss* is silent in the direction
this product treats as dangerous: `walmart` does not match `WM SUPERCENTER #1234`, and a refund
total summed over what a search found reads as the whole figure. An overcount gets questioned; an
undercount gets believed — the argument that decided #18 and #20. The owner ruled (2026-09-14) that
every search answer carries a request-scoped warning saying so. That is the part of this plan I
hold most strongly; the SQL is the easy half.

**Scope I am deliberately NOT adding, though each is adjacent:** a `currency` filter; an exact
merchant-group filter that drills a `money_summary` merchant row down to its rows (offered, not
chosen); `money_summary`'s period-over-period comparison (the other half of the same descope note,
still descoped); fuzzy or token matching; a text index. Each is its own requirement if it is ever
wanted.

## Requirements Confidence

**Level:** High

**Why:** the problem is one sentence (no filter reaches a specific inflow), success is one sentence
(an agent finds a named refund with one call and is told the match is literal), and scope is three
named filters from AC-9.1. The three semantic choices that shape what an agent types were put to the
owner and ruled on 2026-09-14: one `search` over `description` and `merchant`; a signed
`min_amount_minor_units`/`max_amount_minor_units`; and a warning on every search.

**Open assumptions / unknowns:**

- [ASSUMPTION: `category` is an EXACT match on the effective category (`category_override`, else the
  source category, else `UNCATEGORIZED`) — the value `money_summary(group_by=category)` reports as
  `group_key` — and a category no transaction in the store carries is REFUSED, naming the categories
  that do exist | HIGH impact | user can veto] — The `account_id` precedent: a misspelled
  `TRAVL` returning `[]` is a believable "no travel", which is #19's defect shape. The set of
  categories is small (9 in the sandbox), so listing them in the refusal is a correction the caller
  can act on. Matched case-sensitively because the key is an identifier, and the refusal names the
  right spelling.
- [ASSUMPTION: `search` is a case-insensitive substring match with `%`, `_` and the escape character
  taken literally; an empty or whitespace-only term is refused rather than matching everything; a
  term over 200 characters is refused | MED impact | user can override] — An empty term that
  silently matched every row would turn a typo into the unfiltered answer.
- [ASSUMPTION: case folding covers non-ASCII letters, not only ASCII | LOW impact | user can defer]
  — SQLite's `LIKE` and `lower()` fold ASCII only, so `café` would not match `CAFÉ`. **Settled by
  measurement (`mcp-search-latency-2026-09-14.md`): the registered Unicode fold costs about 2ms per
  10,000 rows over ASCII `lower()`, so it ships.**
- [ASSUMPTION: the amount bounds apply to each row in its OWN currency's minor units, with no
  currency filter | LOW impact | user can override] — Rows carry `currency`, the sandbox holds one,
  and the tool description says a bound is not converted. A `currency` filter is out of scope above.
- [ASSUMPTION: `min_amount_minor_units > max_amount_minor_units` is refused, like `until` before
  `since` | LOW impact | user can override] — It selects nothing, and nothing is a believable answer.

**What would raise confidence:** N/A at High.

## Status

- [x] Chunk 01: `category` and the amount range, end to end — the filter value, the fingerprint, the requirement
- [x] Chunk 02: `search`, the `search_is_literal` warning, and retiring every "cannot filter" claim

## Scaffolding

None. Every seam exists: `_transaction_filters` is the single predicate list the row query and both
counts share, `_permitted_arguments` derives accepted keys from the tool definition, and
`envelope._request_fingerprint` is the one site a cursor's predicate is described.

### Verification Strategy

Tests against the sandbox-shaped fixtures, then the product itself: relaunch the sandbox MCP server
on the branch build (confirm by `build.commit`) and ask it the three repro questions from #20. Chunk
02 enqueues an operator verification for the one thing no test can speak to — whether a model
reading a search answer reports the literal-match caveat rather than a confident "no refund".

## Build Chunks

### Chunk 01: `category` and the amount range, end to end — the filter value, the fingerprint, the requirement

- **Description:** An agent can ask for every `TRAVEL` transaction, or every inflow over $400, in
  one call, and page through the result without a cursor from one filter resuming another.
- **Depends on:** nothing
- **Artifacts consumed:** `docs/system-requirements.md` AC-9.1; `api-contract.md` § MCP tool surface,
  § Pagination and caps, § The published field shapes
- **Deliverables:**
  - **The requirement, written first.** A new AC-9.6 under §5 states the three filters' semantics
    as ruled and assumed above — exact effective category with refusal, signed inclusive bounds in
    minor units, the literal case-insensitive `search` over both text fields with its warning — so
    Chunk 02 builds against a criterion rather than against this plan. AC-9.1's build-status note
    moves with it.
  - **One filter value, not four more keyword arguments.** A frozen `TransactionFilter` (category,
    search, min, max; `search` unused until Chunk 02) threaded through `_transaction_filters`,
    `list_transactions`, `Cursor.issued_for` and `parse_cursor`. Today those sites each restate
    `since`/`until`/`account_id`; adding four fields to each by hand is the enumeration the shared
    predicate list exists to prevent, and a fingerprint that forgot one field is a silent wrong
    continuation.
  - `category` and `min_amount_minor_units`/`max_amount_minor_units` on the `query_transactions`
    input schema, narrowed in `_dispatch_tool` (`_text`; `_whole_number` with no minimum, so a
    negative bound is accepted), refused by name when malformed, and `min > max` refused together.
  - An unknown `category` refused with the categories the store holds, ordered AFTER the
    readability check for the reason `account_id`'s refusal is: an unreadable store knows nothing
    about which categories exist.
  - The fingerprint covers every filter. `SeriesCursor` (`balance_history`) is unaffected and keeps
    refusing a transactions cursor.
  - The tool description says what each filter matches and that bounds are signed, with the
    worked example (`max_amount_minor_units=-10000` is "spent $100 or more").
- **Tests:** unit — each filter selects exactly the rows its predicate names, including the
  boundary values (inclusive) and an override category; `truncation.matching` narrows with the
  filter while `coverage.transactions_in_effective_window` does NOT; an unknown category, a float
  bound, a bool bound and `min > max` are each refused naming the argument, every fixture valid in
  every respect but the one it exercises; the cross-product cursor test extends to requests that
  differ only by one filter. Multi-hop — a filtered walk across pages returns every matching row
  exactly once and ends with `truncated: false`.
- **Type:** code
- **Done when:**
  1. Acceptance criteria met and tests pass, `bash scripts/check.sh` green (ruff, format, mypy,
     pytest)
  2. Each new assertion verified by breaking what it names and watching it fail — in particular,
     drop one filter from the fingerprint and watch the cross-product test go red on that filter
  3. Committed, then `/prawduct:critic` run and blocking findings resolved
  4. Chunk marked `[x]` in Status

### Chunk 02: `search`, the `search_is_literal` warning, and retiring every "cannot filter" claim

- **Description:** "Did I get a refund from Walmart?" is one call, and the answer tells the agent
  that a miss is not proof of absence. Nothing in the product still says filtering is impossible.
- **Depends on:** Chunk 01
- **Artifacts consumed:** AC-9.6 as Chunk 01 wrote it; `api-contract.md` § The warning vocabulary
- **Deliverables:**
  - `search` on the input schema: case-insensitive substring over `description` OR `merchant`,
    with `%`/`_` escaped, empty and over-long terms refused.
  - `search_is_literal` in `REQUEST_SCOPED_KINDS`, emitted on every answer to a request carrying
    `search` and on no other, with `_GUIDANCE` (`means` / `for_this_answer` / `act`) in the warnings
    reference. The `detail` names the term and both fields searched. The `act` tells the agent to
    say the match is literal, and before reporting absence to widen the query: a shorter term, or
    the window and account without `search`, reading the rows.
  - 🔴 **Every claim that filtering is impossible, retired in the same commit**, because a stale
    "cannot answer" sends an agent to page 500 rows by hand for a question one argument answers:
    the handshake instructions in `mcp._instructions` (and its go-red guard in
    `tests/preferences/verify_norms_go_red.py`), the envelope reference's "What this server cannot
    answer" bullet in `mcp_resources.py`, `api-contract.md`'s descope note (which also still says
    `query_transactions` offers no pagination, and it does), and AC-9.1's build status. Found by
    `git grep` for the claim, re-run as a Done-when step rather than trusted from planning.
  - The consumer sweep: `docs/connecting-an-mcp-client.md`, `README.md`, and any test or doc that
    enumerates `query_transactions`' arguments.
  - Latency measured, not assumed: a filtered and a searched `query_transactions` over a synthetic
    two-year store against the ~1s target in `nonfunctional-requirements.md`, in the same form as
    `mcp-coverage-latency-2026-09-09.md`. The folding decision in the assumptions above is taken on
    that measurement.
  - An operator verification entry: a model in a real client, asked whether a named refund arrived,
    uses `search` and carries the literal-match caveat into its answer.
  - `[DECISION: an unreadable store's answer to a search carries only \`partial\`, not
    \`search_is_literal\` | no match ran, so a warning qualifying a match would describe a search that
    never happened, and its "0 transaction(s) matched" would read as a real result | AC-9.6's wording
    was sharpened to say so before its first review | user can veto]`
  - **Carried from Chunk 01's review (`rev-20260914T043729Z-506331da`), riding this chunk's commit:**
    the SQLite integer ceiling on the amount bounds is tested on one of its four bounds, so add the
    other three to the existing parametrize; and `_known_categories` must compose from
    `_transaction_filters` (soft delete, superseded spans) over the same `accounts` join as the row
    query, so a category carried only by rows the query excludes is refused as AC-9.6 says rather
    than answered empty — with a test that reaches it.
- **Tests:** unit — `search` matches on `description` alone, on `merchant` alone, case-insensitively,
  and treats `%` and `_` literally; the warning fires on every searched answer including a
  zero-row one and on no unsearched answer (its absence is information); every emitted kind is
  declared and documented (the existing vocabulary guards, extended). Integration — the handshake
  instructions and the envelope reference no longer name filtering as unanswerable, and the go-red
  harness goes red when the old claim is restored.
- **Type:** cumulative-final
  <!-- Last chunk: its review IS the one `/prawduct:critic cumulative` over
       merge-base...HEAD — commit first, run once, no separate `final`. -->
- **Done when:**
  1. Acceptance criteria met and tests pass, `bash scripts/check.sh` green
  2. `git grep` for the retired claim returns only history (change-log, archived plans, reviews)
  3. Each new assertion verified by breaking what it names and watching it fail
  4. The sandbox MCP server relaunched on this build and #20's three repro questions asked of it
     — **Run 2026-09-14** by launching `bankmachine mcp` fresh over stdio against the real sandbox
     store on the Chunk 02 working tree (`build.commit` `a33c1d9`, `dirty: true`), NOT through this
     session's attached client server, which predates the branch. `category=TRAVEL` with
     `min_amount_minor_units=1` returned the 25 United Airlines credits; `search="united"` matched
     49 rows and carried `search_is_literal`; the respelled `search="utd airlines"` matched 0 and
     still carried it; `max_amount_minor_units=-50000` returned only outflows of $500 or more; and
     `category="TRAVL"` was refused as `invalid_argument`, naming the nine real categories. A real
     client's reading of those answers is VRF-027's, and needs the client relaunched.
  5. Committed, then `/prawduct:critic cumulative` run and blocking findings resolved
  6. Chunk marked `[x]` in Status

## Early Feedback Milestone

**Milestone chunk:** 01
**What the user can do:** ask the running sandbox server for every `TRAVEL` inflow and see the 25
credits `money_summary` nets against the $12,000 of outflow, as rows, in one call.

## Governance Checkpoints

**Commit & PR cadence:** commit per chunk after its Critic review. Chunk 02's `cumulative` review
makes the branch PR-ready; `/prawduct:pr create` runs when the user asks. 🔴 **No commit message on
this branch says "Closes #20"** — `develop` is the default branch, and that keyword closed this issue
by mistake once already. The PR body closes it.

- **After Chunk 01 (architecture):** confirm `TransactionFilter` is the only route a predicate
  reaches both the statement and the fingerprint — the question is not "does the category filter
  work" but "what can add a predicate to one and not the other".
- **After Chunk 02 (cumulative):** full review, then the operator verification. VRF-025 and VRF-026
  (re-raised #22 and #23) stay pending and are the owner's to accept or discharge at PR time.
