---
artifact: measurement
scope: mcp-answer-scope
measured_on: 2026-09-08
measured_against: feature/mcp-effective-window, Chunk 02 working tree
decides: "whether `matching` ships exact or approximate"
---

# What the row counts cost, measured

Chunk 02 of `build-plan-mcp-effective-window.md` adds two `COUNT(*)` statements to the
windowed read path, and its plan refused to assume they were free:

> The risk the requirements do not price is `matching`. It costs a second `COUNT(*)` on
> every `query_transactions` call, and `nonfunctional-requirements.md` promises under ~1s
> over 24 months. On this 388-row fixture that cost is unmeasurable.

The 388-row sandbox cannot price this, so the measurement runs against synthetic stores.

## Method

Synthetic SQLCipher stores at three volumes, 14 accounts, transactions spread over 730
days — the shape `nonfunctional-requirements.md`'s "24 months" describes. Rows are
bulk-inserted with valid provenance rather than driven through the derivers, because the
question is read latency and the write path is not under test. Each figure is the median
of 7–9 runs in one process against a warm store.

🔴 **200,000 rows is deliberately past plausible.** A real 24-month store for 14 accounts
lands nearer 10,000; the larger volumes exist to show the shape of the curve, not to
model a user. Read the 10k row as the expected case.

## End-to-end, through the public query functions

| rows | call | min | median | max |
|---|---|---|---|---|
| 10,000 | `query_transactions` 24mo `limit=100` | 16.5ms | 17.9ms | 32.3ms |
| 10,000 | `query_transactions` 24mo `limit=500` | 18.2ms | 19.5ms | 24.4ms |
| 10,000 | `query_transactions` unbounded | 14.1ms | 16.0ms | 17.6ms |
| 10,000 | `spending_summary` 24mo | 16.0ms | 17.2ms | 18.4ms |
| 50,000 | `query_transactions` 24mo `limit=100` | 112.5ms | 114.3ms | 151.2ms |
| 50,000 | `spending_summary` 24mo | 99.5ms | 103.7ms | 143.5ms |
| 200,000 | `query_transactions` 24mo `limit=100` | 431.5ms | 434.9ms | 497.5ms |
| 200,000 | `spending_summary` 24mo | 384.9ms | 390.7ms | 426.1ms |

## The marginal cost, isolated

Each statement timed on its own against the same store, so the two new counts can be
priced apart from the read path that already existed.

| rows | statement | median |
|---|---|---|
| 10,000 | row query (`limit 100`) — pre-existing | 1.5ms |
| 10,000 | `matching` COUNT — **new** | 1.1ms |
| 10,000 | window-scoped coverage COUNT — **new** | 0.9ms |
| 50,000 | row query — pre-existing | 24.5ms |
| 50,000 | `matching` COUNT — **new** | 21.4ms |
| 50,000 | window-scoped coverage COUNT — **new** | 20.7ms |
| 200,000 | row query — pre-existing | 93.9ms |
| 200,000 | `matching` COUNT — **new** | 88.7ms |
| 200,000 | window-scoped coverage COUNT — **new** | 84.4ms |

## What it decides

🔴 **`matching` ships exact.** The approximate-count fallback the plan held in reserve —
an estimated count behind an `approximate` flag — **is not needed and is not built.**

Each count costs about what the row query itself costs, so a `query_transactions` call
does roughly three times the database work it did before. That is the honest way to state
it, and it still lands at ~18ms at the expected volume and ~435ms at twenty times it,
inside the ~1s target with the margin coming from headroom rather than from luck.

**What would reopen this.** The counts scale linearly with rows scanned, so the ~1s target
is reached somewhere near 500k rows in a single window — roughly fifty times a realistic
store. A per-window index would be the first thing to try if that ever arrives; nothing
here needs it now, and adding it against a hypothetical would be tuning a mechanism no
measurement has implicated.

**A caveat on the numbers themselves.** These are single-process, warm-cache medians on
one developer machine. They answer "is this the right order of magnitude" — which is the
question the plan asked — and they are not a benchmark anyone should compare across
machines.
