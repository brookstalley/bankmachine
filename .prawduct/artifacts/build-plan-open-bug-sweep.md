---
artifact: build-plan
version: 1
scope: open-bug-sweep
branch: fix/open-bug-sweep
partition: |
  delegated in waves, partitioned by FILE OWNERSHIP rather than by item, because the read path
  (`query.py`, 2589 lines, and `envelope.py`) is touched by seven of the twenty items and a
  partition by item would have collided on it every time. Each wave's delegates own disjoint file
  sets; the read path and the two foundation items (#34, #80) are never delegated — the coordinator
  holds them, serially, because everything else reads what they establish.

  Wave 1 is four genuinely independent file sets and is the only unconstrained fan-out in the plan.
  Waves 3 and 4 are narrower on purpose: after #80 lands, the remaining items converge on
  `derivers.py` and `query.py`, and the honest partition there is two delegates and a coordinator
  lane, not seven delegates and a merge.

  Delegate verification ceiling: the narrowest `uv run pytest` selection covering the delegate's own
  change, named per brief. Never the whole suite — the coordinator runs that at each wave's
  integration. (`project-preferences.md`'s `Delegate verification` row is unset; this plan states
  the ceiling in its absence and does not invent a project-wide vocabulary for it.)
critic_mode: cumulative-final
depends_on:
  - artifact: api-contract
    file_path: .prawduct/artifacts/api-contract.md
  - artifact: data-model
    file_path: .prawduct/artifacts/data-model.md
  - artifact: operational-spec
    file_path: .prawduct/artifacts/operational-spec.md
  - artifact: security-model
    file_path: .prawduct/artifacts/security-model.md
---

# Build Plan — The Open Bug Sweep

**Backlog items:** the twenty open `kind:bug` items that are genuinely defects and are buildable
without production data or an unratified ruling. The triage that produced that set — and the seven
items it excludes — is § Triage below, which is the part of this plan a later reader most needs.

**Type:** bugfix
**Size:** large — twenty items across five layers, two schema migrations, and one recorded
`api-contract.md` amendment

## Requirements Confidence

**Mixed, and stated per item rather than as one grade.**

- **High** for the six items whose requirements landed 2026-09-10 and are on the issues themselves:
  #80, #86, #68, #66, #69, #62. No chunk below designs a requirement for these; it reads one.
- **High** for #79/#75's exit-code contract: the requirements doc reserved the choice for the owner
  and the owner ruled on 2026-09-10 — **Option B**, a distinct "run again" code (75 /
  `EX_TEMPFAIL`), applied to `NOT_READY` as well as to the still-arriving states.
- **Medium** for the ten items whose problem statement is precise but whose remedy is a judgment
  the builder makes: #85, #84, #83, #82, #81, #74, #72, #56, #34, #28, #59, #6. Each names its own
  acceptance in the issue body; none introduces a domain term the artifacts do not already carry.

- *Problem:* twenty filed defects, several of which make this product do the one thing it exists to
  refuse — return a plausible, well-formed number computed over data that does not support it.
- *Success:* each item's own "Done when" / "Acceptance" is met, with a regression test that fails
  against the pre-fix code; the suite is green; and the three items this sweep proves are *not*
  bugs are relabelled rather than quietly dropped.
- *Out of scope:* the seven items in § Triage § Excluded. None is abandoned; each has a named
  reason and a named owner of the next move.

## Triage

Twenty-seven issues carry `kind:bug`. The label is not the verdict. Each was read against one
question — *does this produce wrong output, break a stated contract, or make a shipped artifact
assert something false?*

### Included — twenty genuine, buildable defects

| # | Layer | Why it is a bug |
|---|---|---|
| #80 | store | A settled hold takes the posting date; a June transaction silently leaves June's total. |
| #86 | store | A currency-less account cannot be created, and every transaction on it refuses to derive. |
| #59 | store | Nothing tests a *populated* store upgrading across schema versions — the operation every real operator performs, against the one series no re-sync can rebuild. |
| #68 | connector | A re-link duplicates accounts and the whole granted history; annual spending doubles. |
| #84 | connector | An unofficial currency is rounded to two places (0.04217 → 0.04) and disclosed only in a log line, against the exact-or-refuse norm the rest of the money path follows. |
| #83 | connector | `consent_expiration_time` and `item.error` are archived and never read, so a connection about to expire answers *healthy* right up to the sync that fails. |
| #82 | connector | A page that fails to derive is re-fetched and re-archived, identical, on every run. |
| #28 | connector | Five `json.loads` sites cover three unrelated failure modes unevenly, one of them on the path taken when something already went wrong. |
| #66 | mcp | `flow_class` classifies on the aggregator's primary category alone, so income reads $0 and spending understates by ~44% on the review's own scenario. |
| #85 | mcp | `money_summary` is uncapped and unpaginated, and the merchant key fragments on the description fallback. |
| #74 | mcp | `earliest_transaction` is one store-wide `min()`, so a window over a thinly-covered account returns $0 for uncovered months with no warning. |
| #72 | mcp | `gapped`'s detail is character-for-character identical on every answer, which is what trains a reader to skip it. |
| #69 | mcp | Only trailing silence is computed; a three-month interior hole reads as a spending drop that never happened. |
| #56 | mcp | The warning vocabulary's canonical comment contradicts one of that kind's two emitters. |
| #34 | mcp | `rule-applied` is declared in the vocabulary, named in the contract and explained to agents, and nothing can ever emit it. |
| #81 | mcp | Batches are refused while the server advertises two revisions that make batching mandatory — an array request gets one `id:null` and the batched ids get no reply at all, which is a client hang. |
| #79 | cli | An initial sync at `INITIAL_UPDATE_COMPLETE` prints "N pages applied" and exits 0 with ~30 days of a 730-day grant in hand. |
| #75 | cli | `still_materializing` and `stopped_short` both exit 0, and an unexpected exception exits 1 — the code the contract reserves for "ran and found a problem". |
| #6 | cli | `sync shell` blanks this schema's own 32+ character identifiers in the statement it echoes back and in driver error text. |
| #62 | ops | The pre-push leak guard blocks the *first* push of every new branch, on a match that lives in already-published history. |

### Excluded — seven, each with its reason and the next move

🔴 **None of these is dropped.** Three are features wearing a bug label; two are blocked on a ruling
or on production data; two are already built and stay open only for a production tail.

| # | Verdict | Reason | Next move |
|---|---|---|---|
| #20 | **feature, mislabelled** | Nothing misbehaves. `spending_summary` filters `amount_minor < 0` and does exactly what it says; the issue's own *Expected* is to build a new `cashflow_summary` tool plus merchant/text filters, category and amount-range filters, and cursor pagination. That is absent capability, not a defect. | Relabel `kind:feature`. |
| #31 | **feature, mislabelled — and wanted** | `BadArgumentError` produces a correct message and a `{code, message}` payload. No contract requires structured recovery *fields*; the issue argues from two sibling repos' practice, which is a good argument for an enhancement. 🔴 **The owner ruled 2026-09-10 that this gets built — just not in this sweep.** Excluded here to keep the sweep's diff reviewable, not because it was declined. | Relabel `kind:feature`, keep `stage: ready`, build next. |
| #40 | **built; remainder is a feature + an owed ruling** | AC-12.1–12.9 landed in `build-plan-production-blockers.md` chunk 01, merged as PR #54. What is left is an operator path to *declare* a retirement — filed separately as **#48**, `kind:feature` — and a product ruling on what net worth should do with a closed account. Its own § Scope-out says that ruling is the owner's. | Owner rules; #48 builds. |
| #22 | **built; production tail only** | AC-13.1–13.7 landed (same plan, chunk 02). AC-13.8/13.9 require one real settlement observed and turned into a fixture. | Production, via the VRF queue. |
| #23 | **built; production tail only** | AC-14.1–14.6 landed (same plan, chunk 03). AC-14.7–14.9 require a real deposit and a *second institution* — the sandbox is single-connection, so AC-14.2's check cannot be exercised at all here. | Production, via the VRF queue. |
| #27 | **blocked on a norm ruling** | Its own *Expected* says so: holding one deferred read snapshot for the life of an `Answer` departs from `store/connection.py`'s reader-autocommit norm, which exists with WAL checkpoint starvation as its stated reason. Building it without the ruling is the *silently invent a requirement* failure. | Owner rules under `docs/norms.md`. |
| #50 | **blocked on production data** | `stage: idea`. Its acceptance is *"AC-14.7 has been performed against a real deposit"*, and it is deliberately a contract decision to be taken with that result in hand. | Follows #23's tail. |

## Build Order — the two dependencies that are not negotiable

1. **#34 before #86, #84 and #68.** All three disclose an exclusion via `rule-applied`, which today
   has no emitter. #34 decides whether that kind is built or recorded as not-yet; the three items
   that need it cannot be built against an undecided vocabulary entry.
2. **#80 before #66, #68 and #69.** #80 establishes the `ledger_date` / `posted_date` split —
   economic questions read one, delivery questions the other. #66 and #68 define windows on
   `ledger_date`; #69 must measure gaps on `posted_date` and says so explicitly. Building any of
   them first would fix a date semantics that then changes underneath it.

🔴 **#66 requires a recorded amendment to `api-contract.md`**, not a doc-sync: the `flow_class` and
`category_is_override` rows assert the behaviour #66 changes. Under `docs/norms.md` that is an
amendment with its own rationale, and the coordinator writes it — not the delegate that builds #66.

## Status

- [x] **01 · Wave 1 — four independent file sets** *(delegated ×4)* — #62, #6, #81, #59
- [x] **02 · The foundation: `rule-applied` gets its meaning, `ledger_date` gets a column** *(coordinator)* — #34's amendment, #80
- [ ] **03 · Wave 2 — currency, connector hygiene, CLI exit codes** *(delegated ×3, in flight)*
- [ ] **04 · The read path** *(coordinator)*
- [ ] **05 · Convergence and the classifier** *(delegated + coordinator)*
- [ ] **06 · Integration, backlog reconciliation** *(coordinator)*

**Context.** Branch cut from `develop` at `ade9a15`. Baseline was green at 1102 before any
change. Six items landed across five commits; the suite was green at 1126 after chunk 02 and all
175 go-red cases still report RED.

🔴 **#34 is amended but not yet closed.** The vocabulary now says what the kind means; it has no
emitter until #86 and #84 land in chunk 03. Closing it before then would be closing it on a
carrier change alone, which is the half the issue explicitly says is not enough.

---

## Decisions

Recorded here because each was taken during the build, and three of them departed from what the
item as filed asked for.

**#80 · `ledger_date` lands NULLABLE, filled by `store rebuild` — owner's ruling, 2026-09-10.**
The landed requirements said NOT NULL. Every migration in this repo is pure `ALTER TABLE ADD
COLUMN` and the runner's contract is DDL only; there is no constant default that is correct,
because the value is `COALESCE(authorized_date, posted_date)` per row. *Rejected: a table rebuild
(CREATE/INSERT SELECT/DROP/RENAME)* — it would put DML inside a migration for the first time, on
the largest table in the store, against the contract that exists so a kill mid-migration leaves an
unrecognised version rather than a half-populated one. *Rejected: coalescing at read time* — it
returns exactly the number the column exists to stop being wrong, invisibly.
🔴 **The cost this buys, and how it is paid:** a null is silently EXCLUDED by every window
predicate, so between the migration and the rebuild every windowed total is a floor. That is
disclosed on every answer via `partial`, naming the count and the command, and
`test_the_rebuild_stamps_the_column_the_migration_could_only_leave_empty` holds the remedy.

**#80 · stranded holds measured on `ledger_date` — owner's ruling, 2026-09-10.** The requirements'
split table did not cover `_stranded_cutoff` / `_stranded_holds` (AC-13.5). How long an
authorisation has been outstanding is a question about the money. Changes no answer today, because
a pending row's two dates agree; stays correct if that ever stops being true.

**#79/#75 · exit code 75 (`EX_TEMPFAIL`) for "run again", applied to `NOT_READY` too — owner's
ruling, 2026-09-10.** The requirements doc reserved this for the owner because it contradicts
`first-production-connection.md` § 3.6. That paragraph changes in the same commit.

**#34 · the kind is widened, not recorded as not-yet-built.** Its own *Expected* offered both. The
account-rule path (FR-8) is unbuilt and building a rule engine inside a bug sweep would be
inventing a requirement — but #86's landed requirements already rule that `rule-applied` carries
their exclusion and call it "its first real emitter". So the meaning widens to what actually
emits it, recorded as an amendment with statement, why and retroactivity.

**Partition · the read path is never delegated.** `query.py` is 2589 lines and is touched by seven
of the twenty items. One delegate owns a narrow slice of it in chunk 03 (the aggregate exclusion);
the coordinator holds the rest and works serially, because a shared worktree gives two agents one
git index and whole-file writes.

---

## Chunks

### Chunk 01: Wave 1 — four independent file sets

Four items whose file sets do not intersect each other or anything later in the plan. This is the
only place in the sweep where a wide fan-out is honest.

| Delegate | Item | Owns |
|---|---|---|
| D1 | #62 | `.githooks/pre-push`, `tests/preferences/check-no-personal-data.sh` and its self-test, `deployment/` token config |
| D2 | #6 | `src/bankmachine/logging_setup.py`, `src/bankmachine/cli/sync.py`, `tests/test_logging_setup.py`, `tests/cli/test_sync_shell.py` |
| D3 | #81 | `src/bankmachine/mcp.py`, `tests/test_mcp.py` |
| D4 | #59 | `tests/store/` only — a new test module; **no `src/` change** |

**Done when:** each item's own acceptance is met, each delegate's narrow selection is green, and
the coordinator's integration run over the merged four is green.

### Chunk 02: The foundation

Coordinator-only, serial, because everything downstream reads what it establishes.

- **#34** — decide and execute: build the aggregate rule path that raises `rule-applied`, or record
  the vocabulary entry as not-yet-built in all three carriers (vocabulary, contract, served
  instructions). The three items that need it in chunk 03 make the first option the likely one, but
  the decision is taken here on its merits and recorded.
- **#80** — migration adding `transactions.ledger_date` (NOT NULL, `CalendarDate`), stamped
  `COALESCE(authorized_date, posted_date)` on insert and never moved by settlement; plus the read
  split, applied per the table in the issue's requirements — `money_summary` windows and
  `group_by=month`, `query_transactions` filtering/ordering/cursor and the `HoldTransitions` tallies
  move to `ledger_date`; `get_coverage_report`, `get_pipeline_health` freshness and anything
  answering *when did this arrive* stay on `posted_date`.

**Done when:** `rule-applied` is either emittable or recorded as not-yet in all three carriers; a
settled hold no longer changes period, proven by a test that fails against the pre-fix deriver; and
every read site is on the correct side of the split, enumerated rather than grepped.

### Chunk 03: Wave 2 — currency, connector hygiene, CLI exit codes

Dispatched after chunk 02 commits, so delegates read a `ledger_date` that exists.

| Delegate | Items | Owns |
|---|---|---|
| D5 | #86, #84 | the currency path: migration for nullable `accounts.currency`, `to_minor`'s minor-digit lookup, the per-row refusal, and the `rule-applied` disclosure on aggregates covering excluded rows |
| D6 | #82, #28 | `connector/plaid/client.py`, `errors.py`, the archive/derivation retry path, and the five `json.loads` sites |
| D7 | #79, #75 | `src/bankmachine/cli/` — exit code 75 per the owner's Option B ruling, plus the prose change, plus `docs/first-production-connection.md` § 3.6 which the ruling contradicts |

🔴 **D5 and D6 both reach `connector/plaid/derivers.py`.** The ownership split inside that file is
by function, stated in each brief: D5 owns `_operator_signed_amount`, `to_minor` and the account
upsert's currency handling; D6 owns the archive/re-archive path and the parse sites. If either finds
it needs the other's functions, it stops and reports rather than editing across the line.

### Chunk 04: The read path

Coordinator-only. Seven items on `query.py` and `envelope.py`, done serially in one lane because
that is what the file's size and their overlap actually permit.

#85 (cap, page, normalize the merchant key) · #74 (per-account window coverage against the
connection's `history_starts`, 7-day discriminator) · #72 (request-relative `gapped` detail, four
distinct strings) · #69 (interior gaps, `days > max(cadence,1)*3 AND days >= 7`, on `posted_date`,
capped at 10/account) · #83's read half (consent expiry → `partial` at 14 days, `degraded` once
passed; `item.error` → `degraded`) · #56 (relational scope sentence, three narrating comments swept,
one-pass `_account_lifecycle` with the null-is-load-bearing comment verbatim).

### Chunk 05: Convergence and the classifier

- **#68** — account convergence on `source_persistent_account_id` scoped to the institution;
  transaction convergence by **lineage exclusion, not natural-key dedupe** (the issue's requirements
  are explicit that dedupe can silently delete two genuinely identical real transactions, which is
  the wrong side of this product's asymmetry).
- **#66** — classify on `source_category_detailed`; `internal_transfer` and `debt_service` both
  require a matched opposite leg on an **enrolled** account; `INCOME_*` and `TRANSFER_IN_PAYROLL`
  are never `internal_transfer`; loan principal and interest deliberately not split. The four
  fixtures named in the review are the acceptance.

The `api-contract.md` amendment for #66 is the coordinator's and lands in this chunk.

### Chunk 06: Integration

Full suite, `verify_norms_go_red.py`, mypy strict, ruff. Artifact freshness across `api-contract.md`,
`data-model.md`, `operational-spec.md`. `/prawduct:critic cumulative`. Backlog reconciliation: close
the twenty, relabel #20/#31 to `kind:feature`, and leave #40/#22/#23/#27/#50 open with their reasons
recorded on the issues rather than in this plan alone.
