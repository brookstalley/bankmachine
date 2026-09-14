---
artifact: measurement
scope: mcp-answer-scope-completion
measured_on: 2026-09-09
measured_against: feature/mcp-answer-scope-completion, chunk A working tree
decides: "whether per-account coverage may ride every `list_accounts` row"
---

# What the per-account coverage walk costs, measured

Chunk A puts a per-account aggregate on `list_accounts`, which is the most-called tool on
this surface, and the plan refused to assume it was free:

> `mcp-count-latency-2026-09-08.md` prices comparable grouped reads at ~17ms at 10k rows,
> but that figure was measured for a different statement and `learnings.md` forbids
> inheriting it.

🔴 **The prohibition is the point.** That learning was written after this project quoted a
derived figure as though it were the finding and proposed three mitigations against 21% of
a cost whose 78% was elsewhere. A number measured for `matching`'s `COUNT(*)` says nothing
about an outer-joined `MIN`/`MAX`/`COUNT` grouped over every account.

## Method

Synthetic SQLCipher stores at two volumes, 14 accounts, transactions spread over 730 days.
🔴 **Only 5 of the 14 accounts carry rows**, because a store where most accounts are empty
is the shape the real sandbox has — nine of fourteen there have never had a transaction —
and it is also the shape that exercises the outer join this walk depends on. Rows are
bulk-inserted with valid provenance (the schema's `CHECK` refuses anything else, which is
how the fixture stays honest); the question is read latency and the write path is not under
test. Each figure is the median of 9 runs in one process against a warm store.

Read the 10,000-row line as the expected case: a real 24-month store for 14 accounts lands
near 10k.

## Results

| rows | call | min | median | max |
|---|---|---|---|---|
| 10,000 | `_account_coverage` (isolated) | 3.0ms | **3.0ms** | 9.0ms |
| 10,000 | trivial account count (floor) | 0.0ms | 0.0ms | 0.5ms |
| 10,000 | `list_accounts` end-to-end | 17.1ms | **18.2ms** | 21.1ms |
| 10,000 | `get_coverage_report` end-to-end | 34.5ms | **38.0ms** | 57.6ms |
| 50,000 | `_account_coverage` (isolated) | 20.3ms | 22.1ms | 40.0ms |
| 50,000 | trivial account count (floor) | 0.0ms | 0.1ms | 0.5ms |
| 50,000 | `list_accounts` end-to-end | 59.9ms | 65.1ms | 68.3ms |
| 50,000 | `get_coverage_report` end-to-end | 170.5ms | 178.8ms | 218.0ms |

## What it decides

🔴 **Coverage rides every `list_accounts` row. The parameter that would have made it
opt-in is not needed and is not built.**

The walk costs **3ms at the expected volume**, against an 18ms end-to-end call and a ~1s
target — roughly a sixth of one tool call, and under 0.5% of the budget. The design
question was never really cost; it was whether an agent should have to *ask* for
completeness, and #19 is the record of what happens when it must. The measurement removes
the only argument for the opt-in.

**`get_coverage_report` is the more expensive tool and that is the right place for it.**
38ms at expected volume, and it scales worse than the walk it shares — 4.7× the time for 5×
the rows — because it pulls every `posted_date` into Python to derive each account's median
interval. It is the verification surface: called deliberately, before trusting an answer,
not on every question.

**What would reopen this.** The cadence derivation is linear in transactions and holds them
all in memory, so `get_coverage_report` reaches the ~1s target somewhere near 250k rows in
one store — roughly twenty-five times a realistic one. A windowed or incremental median
would be the first thing to try, and nothing needs it now; building it against a
hypothetical would be tuning a mechanism no measurement has implicated.

**A caveat on the numbers themselves.** Single-process, warm-cache medians on one developer
machine, answering "is this the right order of magnitude" — which is the question the plan
asked. They are not a benchmark to compare across machines.
