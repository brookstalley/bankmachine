---
artifact: measurement
scope: investments-followups
measured_on: 2026-09-14
measured_against: feature/investments-followups, Chunk 03 working tree
decides: "whether AC-5.4's derivation-version check can ride every MCP answer, and whether migration 012's indexes are what makes it cheap"
---

# What the derivation-version check costs, measured

AC-5.4 puts a question on every MCP answer: does any derived row carry a derivation version other
than this build's? It is asked per recorded version and per derived table, as an `EXISTS` probe. The
plan refused to assume that was free, because a probe for a version with no rows left has to prove
absence, and without an index that is a scan of the table.

## Method

Synthetic SQLCipher stores at two volumes: transactions spread over 730 days on one account, derived
through the real derivers from one generated sync page. Six versions are recorded in
`derivation_versions`, and only this build's still has rows. That matches the sandbox store, which
records six, and it is the worst realistic case, since five of the six probes per table must prove
absence. Each figure is the median of 9 runs in one process against a warm store, after one discarded
warm-up call. Read the 10,000-row line as the expected case, as `mcp-search-latency-2026-09-14.md`
does.

"Isolated check" opens a reader handle and runs `derivation_versions_present` over every derived
table, so it includes the handle's open. Inside an answer the check reuses the answer's own handle,
so the isolated figure is an upper bound on what the check adds. "Indexes dropped" is the same store
after dropping migration 012's six indexes.

The probe was a throwaway test file and is not committed; its method is this section.

## Results

| rows | call | min | median | max |
|---|---|---|---|---|
| 10,000 | isolated check, indexed | 6.2ms | **6.5ms** | 7.3ms |
| 10,000 | isolated check, indexes dropped | 13.7ms | **15.6ms** | 17.2ms |
| 10,000 | `get_pipeline_health` | 26.6ms | **28.2ms** | 32.2ms |
| 10,000 | `query_transactions` 24mo `limit=100`, MCP call | 34.2ms | **37.2ms** | 42.5ms |
| 50,000 | isolated check, indexed | 6.1ms | **6.3ms** | 8.1ms |
| 50,000 | isolated check, indexes dropped | 133.1ms | **134.5ms** | 150.8ms |
| 50,000 | `get_pipeline_health` | 164.1ms | **166.1ms** | 167.5ms |
| 50,000 | `query_transactions` 24mo `limit=100`, MCP call | 246.5ms | **250.1ms** | 271.0ms |

## What it decides

🔴 **The check rides every answer, and migration 012 is what lets it.** With the indexes the check
is flat, 6.5ms at 10,000 rows and 6.3ms at 50,000, because each probe is a seek. Without them it
grows with the store, from 15.6ms to 134.5ms, and at 50,000 rows it would be more than half of a
`get_pipeline_health` call. That is the cost the migration removes, and it is paid on every answer
of every tool.

**At the expected volume the check is a visible share of a cheap call, not of the target.** Its
6.5ms upper bound is under a quarter of `get_pipeline_health`'s 28ms and a sixth of a 24-month
`query_transactions`, against a ~1s target. Most of it is the reader handle's open, which an
answer does not pay twice.

**What would reopen this:** many more recorded derivation versions. The probe count is versions ×
derived tables, and `derivation_versions` only grows. At one bump a build cycle that is years away.
If it arrives, the check can skip versions that `store rebuild` has already proven absent.
