---
artifact: measurement
scope: transaction-filters
measured_on: 2026-09-14
measured_against: feature/transaction-filters, Chunk 02 working tree
decides: "whether `search` folds case over Unicode or over ASCII only, and whether any filter needs an index"
---

# What the `query_transactions` filters cost, measured

Chunk 02 matches `search` through a Python case-fold function registered on the read handle,
because SQLite's own `lower()` and `LIKE` fold ASCII letters only. A Python function called per row
is the kind of cost that is invisible on a 40-row fixture, so the plan refused to assume it was free,
and it refused to borrow `mcp-count-latency-2026-09-08.md`'s figures for a different statement.

## Method

Synthetic SQLCipher stores at two volumes, 14 accounts with rows on 5 of them, transactions spread
over 730 days, derived through the real derivers from generated sync pages. Descriptions carry a
merchant name, a store number and a per-row reference, the shape that makes a substring scan read
every character. Each figure is the median of 9 runs in one process against a warm store, after one
discarded warm-up call. Read the 10,000-row line as the expected case: a real 24-month store for 14
accounts lands near 10k.

The probe was a throwaway test file and is not committed; its method is this section.

## Results

| rows | call | min | median | max |
|---|---|---|---|---|
| 10,000 | `query_transactions` 24mo `limit=100`, unfiltered | 29.1ms | **30.3ms** | 31.9ms |
| 10,000 | + `category=TRAVEL` | 31.0ms | **32.4ms** | 37.5ms |
| 10,000 | + `max_amount_minor_units=-10000` | 29.3ms | **30.1ms** | 37.5ms |
| 10,000 | + `search='walmart'` | 38.0ms | **38.7ms** | 46.2ms |
| 10,000 | + `search='zzzz'` (matches nothing) | 37.9ms | **39.5ms** | 46.1ms |
| 10,000 | + `search`, `limit=500` | 39.9ms | **41.7ms** | 45.1ms |
| 10,000 | isolated `COUNT`, `instr(casefold(description))` | 10.5ms | **10.9ms** | 16.2ms |
| 10,000 | isolated `COUNT`, `instr(lower(description))` — ASCII only | 8.4ms | **9.0ms** | 10.0ms |
| 50,000 | `query_transactions` 24mo `limit=100`, unfiltered | 293.0ms | **295.7ms** | 310.0ms |
| 50,000 | + `category=TRAVEL` | 318.6ms | **328.3ms** | 347.7ms |
| 50,000 | + `max_amount_minor_units=-10000` | 291.1ms | **292.9ms** | 300.6ms |
| 50,000 | + `search='walmart'` | 330.9ms | **339.6ms** | 359.8ms |
| 50,000 | + `search='zzzz'` (matches nothing) | 332.6ms | **335.5ms** | 345.9ms |
| 50,000 | + `search`, `limit=500` | 335.3ms | **336.8ms** | 343.1ms |
| 50,000 | isolated `COUNT`, `instr(casefold(description))` | 37.4ms | **38.3ms** | 57.8ms |
| 50,000 | isolated `COUNT`, `instr(lower(description))` — ASCII only | 31.2ms | **32.3ms** | 35.0ms |

## What it decides

🔴 **`search` folds over Unicode.** The registered fold costs about 2ms more than ASCII-only
`lower()` per 10,000 rows scanned, and about 6ms at 50,000. Against a ~1s target that is noise, and
what it buys is a search for `café` that finds `CAFÉ`. An ASCII-only fold would miss that row with
nothing on the answer to say so, which is the silent false negative this chunk's warning exists to
qualify, not to add to.

**A searched call costs about 9ms more than an unfiltered one at the expected volume**, 39ms against
30ms, and about 40ms more at 50,000 rows. The two counts and the row query each scan the text, so
the cost is roughly three isolated scans. It does not grow with `limit`.

**No filter needs an index.** Category and amount cost no more than the unfiltered call at either
volume. The row query is dominated by the ordered window scan the unfiltered call already pays.
`transactions.posted_date` has no index either (#71), and nothing here changes that item's priority.

**What would reopen this:** a store well past 50,000 rows, or a caller searching on every turn of a
long analysis. At 50,000 rows the unfiltered call is already 296ms, so the ceiling this approaches is
the existing one, not one `search` introduced.
