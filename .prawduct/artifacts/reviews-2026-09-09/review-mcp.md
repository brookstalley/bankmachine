# MCP surface review — bankmachine @ 4b78550 (develop)

Read-only review. Method: full read of `mcp.py`, `mcp_resources.py`, `envelope.py`, the
`query.py` read paths, the four named tests, the five named documents; plus ~10 live
stdio drives of `BANKMACHINE_ENVIRONMENT=sandbox uv run bankmachine mcp` and one
in-process `serve()` fault-injection. Every claim below is backed by quoted code or
captured JSON. Nothing was edited.

Cross-checked against the owner's sibling standards:
`hallucinote/docs/archive/mcp-tool-design.md` §§2.4–2.6, 3, 7–9;
`cordyceps/.../McpTestingGuide.md`; `3tears/packages/mcp/README.md` § Stdio discipline.

---

## Ranked findings

### 1. HIGH — 69% of `instructions` never reaches the model: the client truncates it, and the cut lands exactly on the guidance the product exists to deliver
`src/bankmachine/mcp.py:1331-1450`

`_instructions()` returns 6,673 characters (measured off the wire). This session's own
Claude Code system prompt contains the bankmachine-sandbox instructions and ends:

```
| `counted_during_change` | a write landed while the answer was assembled | rows and counts
  are from adjacent moments; re-ask if the two must reconcile exactly |
| … [truncated]
```

Measured cut point: **2,045 of 6,673 chars delivered (30.6%); 4,628 chars dropped.** What
is on the dropped side:

- 6 of the 11 request-scoped warning kinds — `accounts_without_coverage`,
  `account_no_longer_active`, `roster_observed_empty`, `includes_pending_rows`,
  `sign_convention_unverified` (and the tail of `counted_during_change`);
- the entire "WHAT EVERY ANSWER CARRIES" table (`environment`, `as_of`, `build`,
  `coverage`, `accounts_not_active`, `not_active_balance_minor_units`);
- the WINDOWED / CAPPED / CLASSIFYING paragraphs, including **"Quote
  `external_spend_outflow_minor_units` when asked what was spent"**;
- the closing pointer to `bankmachine://reference/envelope` and
  `bankmachine://reference/warnings` — so the agent is never told the resources exist.

The very first casualty is `accounts_without_coverage`, whose guidance is *"Do not answer
'no payments found' about it"* — the mortgage failure named in
`docs/connecting-an-mcp-client.md` as the reason the verification surface was built.

`docs/connecting-an-mcp-client.md` asserts *"The server's own `instructions` are the
authority on that list — a test holds them against the union of every tool's live envelope
and against the warning vocabulary, so they cannot fall behind the wire."* Two tests
(`tests/test_mcp.py:1451`, `:1506`) do hold the **string in the process**. Neither can hold
what the client hands the model, and in the only client measured, it hands over a third.

**Failure scenario.** Tomorrow, on real accounts: the agent is asked "did my mortgage go
out this month?", the mortgage account has never had a transaction recorded, the
`accounts_without_coverage` warning rides the payload with a correct `detail` — and the
agent has no instruction saying that kind means *data not present, never no activity*. It
answers "no payments found." That is precisely the failure the README's §7 deliverable
names.

**Fix.** Invert the layering the sibling doc §7.1 prescribes: cut `instructions` to a
~250-token primer that survives any client's budget — the sign/minor-unit convention, the
one sentence "read `warnings` before concluding", the five tool names, and **the two
resource URIs first, not last** — and let the two already-derived resources carry the
tables. Then move the load-bearing per-kind guidance onto the payload where nothing can
trim it: put the `act` sentence from `mcp_resources._GUIDANCE` into each `Caveat.detail`
(or add a `guidance` field to the warning object, which the published schema already
closes with `additionalProperties: False` so it must be added there too). The instructions
completeness tests then need re-pointing at the resource, and a new test should assert the
primer stays under a stated byte budget.

---

### 2. HIGH — the boundary's broad catch does not cover the whole boundary: an exception after `_dispatch_tool` kills the session
`src/bankmachine/mcp.py:1620-1689`, `:1738-1742`

The `try` guarding a tool call ends at line 1677. Serialization happens outside it:

```python
1677            )
1678        wire = answer.to_wire()
...
1685                "content": [{"type": "text", "text": json.dumps(wire, indent=2)}],
```

and `serve()` calls `_write` with no guard at all:

```python
1738    for message in _read_messages(stdin, stdout):
1739        reply = _handle(config, message)
1740        if reply is not None:
1741            _write(stdout, reply)
```

Verified by fault injection (temporary test, since removed) — `query.list_accounts`
returning an object whose `to_wire()` raises:

```
SERVE DIED WITH: RuntimeError SECRET path=/Users/x/store.db password=hunter2
STDOUT WAS: ''
```

The `tools/call` got no reply, the following `ping` got no reply, and `serve()` unwound.
`tests/test_mcp.py:886` (`test_a_failing_tool_reports_an_error_without_closing_the_session`)
does not catch this: it patches `query.list_accounts` to raise, which is *inside* the try.

The same hole covers `initialize` (`_instructions`, `_server_info`), `tools/list`
(`_tool_definitions()` — `ToolRegistrationError` is pre-checked in `cmd_mcp` but the
`tools/list` path is unguarded), `_resource_entry`, and any `BrokenPipeError`/`OSError`
from `_write`.

This contradicts the module's own stated invariant, repeated four times: *"an unhandled
exception here would close the pipe mid-session and the operator would see their tool
'disappear' rather than fail"* (`mcp.py:1658-1663`), and `cmd_mcp`'s *"the operator's tool
vanishing mid-session, which is the one outcome this module names as worse than any wrong
answer."*

**Failure scenario.** SQLite's dynamic typing lets a BLOB sit in a TEXT column; a
`bytes` in `currency` or `description` makes `json.dumps` raise `TypeError` outside the
guard. The client's tool silently vanishes on one particular question, with a stack trace
on stderr the operator will not be looking at.

**Fix.** Move `wire = answer.to_wire()` and the `json.dumps` inside the existing `try`
(they belong to answering the call), and wrap the body of `_handle` and the `_write` in
`serve()` in a last-resort `except Exception` that emits `_error(message_id,
_INTERNAL_ERROR, …)` and continues the loop. Add the positive-control test the fault
injection above is: patch the *serializer*, not the query.

---

### 3. HIGH — `truncation.matching` silently changes meaning once a cursor is passed, and the `rows_truncated` warning states the wrong total in plain English
`src/bankmachine/query.py:1385-1394`; `src/bankmachine/envelope.py:786-792`;
`docs/connecting-an-mcp-client.md:171,174`

The keyset predicate is deliberately in the **shared** filter list, so `matching` is
recomputed per page. Measured walk over the sandbox store (390 rows, `limit=100`):

```
page 1 returned 100 matching 390 truncated True
page 2 returned 100 matching 290 truncated True
page 3 returned 100 matching 190 truncated True
page 4 returned  90 matching  90 truncated False
```

Page 2's `rows_truncated` detail, verbatim off the wire:

> **"290 transactions match this request** and the newest 100 are returned, so 190 are
> missing from this answer. …"

390 transactions match that request. The sentence an LLM will quote is false on every page
but the first, and `coverage.transactions_in_effective_window` sitting beside it still
reads 390 — so the answer contradicts itself and the agent has no way to know which number
to believe.

The documents make it worse rather than better. `api-contract.md:424` states the real rule
(*"`cursor` narrows the request the way `since` does. `matching` counts what is left from
the cursor's position onward"*), but the two surfaces a consumer actually reads say the
opposite:

- `docs/connecting-an-mcp-client.md:171` — *"`truncation.matching` is the one that answers
  'how many rows did my whole request select'."*
- `mcp_resources.py:559` (the served envelope resource) — *"`truncation.matching` is the
  one that reflects your filters"* — no mention of the cursor.
- the published `outputSchema` (`mcp.py:350-357`) — *"how many rows matched, how many came
  back"*.

**Failure scenario.** Agent is asked "how many transactions hit the card this year?",
pages to the end because the instructions told it to, and reports `matching` from the last
page: 90 instead of 390. Or it stops at page 2 and reports 290. Both read as confident,
complete answers.

**Fix.** Keep the mechanism (it is right — `truncated` must go false) and fix the naming.
Either (a) rename the paged figure `remaining` and add `matching_total` computed without
the cursor predicate, or (b) keep `matching` as the whole-request count and derive
`truncated` from the presence of a further row rather than from `returned < matching`.
Then correct `envelope.py:788`'s detail sentence, `mcp_resources.py:559`, the
`outputSchema` description, and `connecting-an-mcp-client.md:171,174`. Sibling §2.6:
*if the surface changes, all the doc targets change together.*

---

### 4. HIGH — `money_summary` is unpaginated with no cap, and `group_by=merchant` falls back to the raw description, so one call can return an unbounded payload
`src/bankmachine/query.py:2216-2421` (`truncation=None` at `:2414`); `:2291`;
tool declared `capped=False` at `src/bankmachine/mcp.py:892`

The tool is fixed unpaginated by contract, on the stated ground that it is *"bounded by its
grouping"* (`mcp.py:118-120`). The grouping key is:

```python
2291            key = func.coalesce(transactions.c.merchant_name, transactions.c.description, "UNKNOWN")
```

`description` is the institution's raw string, which routinely embeds a per-transaction
reference. Sandbox already shows it — measured `group_by=merchant` rows:

```json
{"group_key": "ACH Electronic CreditGUSTO PAY 123456", ...}
```

So the row count is bounded by *distinct descriptions × currencies × up to 3 flow classes*,
which in the worst case approaches the transaction count. The sandbox store hides this
(390 transactions collapse to 14 groups because Plaid's fixtures reuse strings); a real
household will not. Order-of-magnitude: 12,000 transactions over five years with ~25% of
rows falling through to `description` gives roughly 3–4k groups × ~250 bytes × 2 (see
finding 5) ≈ 1.5–2 MB in a single tool result, with no `truncation` block and no warning —
because this tool publishes neither.

Note this is also a correctness problem independent of size: a "merchant" rollup that
splits one merchant across dozens of reference-numbered description strings understates
every merchant total, and nothing in the tool description or the `group_key` schema
description (`mcp.py:832-838`) mentions the fallback at all.

**Failure scenario.** "Where did my money go this year, by merchant?" returns a
multi-megabyte result that blows the agent's context (or is truncated by the client mid-
JSON), and the merchant totals it does contain are fragmented and low.

**Fix.** Two separate changes. (a) Give `money_summary` the same cap-and-report machinery
`query_transactions` has — an ordered `LIMIT` with a `truncation` block and a
`rows_truncated` warning; the ordering (`ORDER BY sum(amount_minor)`) is already total
enough to make "the largest N groups" a defensible page, and the `totals` block already
carries the un-truncated aggregate so the answer stays honest. (b) Either drop the
`description` fallback (group unresolved rows into a single `UNKNOWN (no merchant name)`
bucket, with the count) or state the fallback in the tool description and the `group_key`
schema, since an agent rolling up by merchant is entitled to know when it is really
rolling up by description.

---

### 5. MEDIUM — every answer is sent twice, and the text copy is pretty-printed: a full page is ~250 KB, a session costs ~47 KB before the first question
`src/bankmachine/mcp.py:1685-1686`; `:1603-1604`; `:1348`

Measured, one `query_transactions` at the sandbox's full 390 rows:

```
structuredContent compact bytes : 102,123
content text bytes (indent=2)   : 129,599
total JSON-RPC frame            : 248,746   (~637 bytes/row)
```

At `MAX_ROWS = 500` that is ~320 KB, ~80k tokens, from one tool call. The default
`limit: 100` still ships a ~64 KB frame. Sending both forms is spec-sanctioned (2025-06-18
recommends the serialized JSON in a text block for backwards compatibility), but
`indent=2` costs a further 27% over compact for a payload no human reads, and most clients
put *both* into the model's context.

Session fixed cost, measured: `tools/list` = 40,311 bytes, `instructions` = 6,673 bytes.
~47 KB / ~12k tokens for a five-tool server, before any question. Sibling principle 1
(tools are a budget) and §7.1 (~250-token primer) are both missed by a wide margin — the
tool count is right (5, well under 10), the *per-tool* schema prose is what is heavy.

**Fix.** `json.dumps(wire, separators=(",", ":"))` in the text block. Consider lowering the
default `limit` from 100 to something like 25 (paging is already correct and the cursor is
already advertised) and revisiting `MAX_ROWS = 500`, since no context window survives it.
The output schema field descriptions are excellent content in the wrong place — the
`bankmachine://reference/envelope` resource already renders from them, so the schemas
could carry short descriptions and let the resource carry the essays.

---

### 6. MEDIUM — `gapped` fires character-for-character identically on every answer, which is the exact defect `api-contract.md` diagnoses and then exempts it from
`.prawduct/artifacts/api-contract.md:928-936`; `src/bankmachine/query.py:82` (`_pipeline_warnings`)

Measured, three tools including a nine-day window (2026-09-01 → 2026-09-09) lying wholly
inside coverage:

```
list_accounts       -> Tartan Bank granted 722 days of history against 730 requested, ...
get_pipeline_health -> Tartan Bank granted 722 days of history against 730 requested, ...
money_summary       -> Tartan Bank granted 722 days of history against 730 requested, ...
distinct gapped details: 1
```

The contract itself writes the indictment:

> *"measurement found the `gapped` notice arriving character-for-character identical on a
> window wholly inside coverage, a window wholly outside it, a future window, and a query
> for an account that does not exist. It is therefore true and useless: it cannot tell a
> caller whether *this* answer is the degraded one, and **a field that fires on every
> response trains its reader to skip it**."*

…and then classifies `gapped` connection-scoped anyway, so the behaviour it measured is
still shipping. An 8-day shortfall on a 730-day grant is immaterial to essentially every
question, and it is riding 100% of answers.

**Failure scenario.** The agent learns within a few turns that `warnings` always contains
the same `gapped` line, starts skimming the array, and misses the `degraded` or
`sign_convention_unverified` entry that actually mattered. This directly undercuts
finding 1's mitigation (payload-carried warnings) and the README's §7 thesis.

**Fix.** Make `gapped` request-scoped, the way the contract argues every other kind should
be: fire it only when `effective_window.effective.since` (or the answer's coverage start)
actually reaches back past a contributing connection's `history_starts`. Both facts are
already computed — `history_starts` is on every `get_pipeline_health` row and
`effective_window` is on every windowed answer. Keep the standing state where it belongs:
on `get_pipeline_health`, which already publishes `requested_history_days` /
`granted_history_days` per connection.

---

### 7. MEDIUM — `query_transactions` rows carry no `account_id`, so a transaction cannot be joined back to the account it is on
`src/bankmachine/mcp.py:760-783`

Measured row keys:

```
['account', 'amount_minor_units', 'category', 'category_is_override', 'currency',
 'date', 'description', 'merchant', 'pending', 'transaction_id']
```

`account` is a bare display name (`"Plaid Checking"`), with no `account_id`, no
`institution`, and no `mask`. Every other tool keys on `account_id`: `list_accounts`
(`account_id`), `get_coverage_report` (`account_id`), `money_summary(group_by=account)`
(`group_key` = the id as a string). Nothing guarantees `name` is unique — real households
routinely hold two accounts named "Checking" (two banks, or joint + personal at one bank),
and `mask` is nullable.

**Failure scenario.** The agent finds a suspicious row, and the tool descriptions tell it
to follow up — *"read `lifecycle` before summing anything into a net worth"*, *"call
`get_coverage_report` before concluding an account has no activity"*. It cannot: it has a
name that matches two rows of `list_accounts`, and `query_transactions(account_id=…)`
needs an integer it was never given. It either guesses or silently reports the wrong
account.

**Fix.** Add `account_id: {"type": "integer"}` to the transaction row schema and payload.
It is additive, it costs ~8 bytes/row, and it closes the one join the whole five-tool
surface currently cannot make. Adding `institution` would also make the row self-
describing, at more cost.

---

### 8. MEDIUM — JSON-RPC batches are rejected with a null-id error while the server advertises the two revisions that require them
`src/bankmachine/mcp.py:1805-1807`

```python
1805        if not isinstance(message, dict):
1806            _write(stdout, _error(None, _INVALID_REQUEST, "a message must be an object"))
1807            continue
```

Measured, sending `[{"...","id":1,"method":"ping"},{"...","id":2,"method":"ping"}]`:

```json
{"jsonrpc": "2.0", "id": null, "error": {"code": -32600, "message": "a message must be an object"}}
```

`SUPPORTED_PROTOCOL_VERSIONS` (`mcp.py:81-86`) includes `2024-11-05` and `2025-03-26`.
Batching is base JSON-RPC 2.0 (so in scope for the former) and was explicitly mandatory in
the `2025-03-26` revision; it was removed in `2025-06-18`. A client that negotiates
`2025-03-26` and batches gets one error carrying `id: null` and **no reply for ids 1 or 2
at all** — the client's promises never settle. That is the hang `_read_messages`'s own
comments say the loop exists to prevent (*"a client that sent something unparseable is
waiting, and silence would look like a hang"*).

**Fix.** Cheapest correct option: drop `2024-11-05` and `2025-03-26` from
`SUPPORTED_PROTOCOL_VERSIONS` (leaving `2025-06-18` and `2025-11-25`, where batching is
removed), which also lets `structuredContent` and `title` be honestly advertised. If the
old revisions stay for compatibility, handle a top-level list: dispatch each element, drop
notification replies, and write back a single array — about ten lines in `serve()`.

---

### 9. MEDIUM — nothing on the wire says what this server cannot answer
`src/bankmachine/mcp.py:1348-1450` (instructions), `_tool_definitions()`

Three specified tools are descoped and clearly documented as absent in
`README.md:21-23`, `docs/connecting-an-mcp-client.md` ("Five of the eight specified
tools") and `api-contract.md:197`, held together by
`tests/preferences/test_the_documented_tool_surface_is_the_built_one.py`. That machinery is
excellent — and it points entirely at *human* surfaces. Nothing an **agent** receives
mentions `balance_history`, `list_holdings`, `find_recurring`, or the absent capabilities:
no holdings/positions, no balance history or net-worth-over-time, no recurring-charge
detection, no amount filter, no text search, no category filter on `query_transactions`.

Sibling principle 8: *hard constraints go in the tool help, the server instructions, a
resource, and the error message — repetition beats subtle.*

Note also that `docs/connecting-an-mcp-client.md` lists **"Show me every transaction over
$100 since August"** under *"Once connected, these are answerable directly."* There is no
amount parameter; answering it means paging the whole window and filtering client-side, at
~637 bytes/row.

**Failure scenario.** "What was my net worth a year ago?" There is no tool and no notice
that there is no tool. The agent improvises from `list_accounts` (today's balances) or by
summing transactions, and produces a number with no basis. Likewise "what subscriptions am
I paying for?" gets a hand-rolled recurring-charge guess presented as an answer.

**Fix.** Add a short `WHAT THIS SERVER CANNOT ANSWER` block to the primer (see finding 1)
and a fuller section to the `bankmachine://reference/envelope` resource — naming holdings,
balance history, recurring detection and the missing filters, with the instruction to say
so rather than derive it. Derive the list from the same `api-contract.md` tool table the
existing test already parses, so it cannot drift.

---

### 10. MEDIUM — the contract names "the change log" as the breaking-change channel, and no consumer-facing changelog exists
`.prawduct/artifacts/api-contract.md:1102-1106`; `.prawduct/change-log.md`

> *"How a breaking change is signalled: the operator is the only consumer, so the channel
> is the change log plus a CLI warning for a deprecated command for one release before
> removal."*

There is no `CHANGELOG.md` at the repo root (`find . -iname 'CHANGELOG*'` → nothing). The
only change log is `.prawduct/change-log.md`, a 2,105-line governance journal organised by
work cycle, not by surface, not referenced from `README.md` or
`docs/connecting-an-mcp-client.md`, and living under the framework directory. Every MCP
tool and both resources are graded `experimental`, and `serverInfo.version` is just the
package version (`0.1.0` — measured). So there is no tool-surface version handle at all,
and the sibling's §9 rule "*CHANGELOG is part of the API*" has nothing to point at.

**Fix.** Add a root `CHANGELOG.md` scoped to the public surface (MCP tools, tool
arguments, warning kinds, resource URIs, CLI, exit codes), link it from the README and the
client guide, and have `api-contract.md:1104` name it by path. Feed it from the existing
`.prawduct/change-log.md` entries rather than duplicating the work.

---

### 11. LOW — unknown tool answered `-32601` where the spec's own example uses `-32602`
`src/bankmachine/mcp.py:1619`

```python
1619            return _error(message_id, _METHOD_NOT_FOUND, f"no tool named {name!r}")
```

Measured: `{"code": -32601, "message": "no tool named 'nonexistent_tool'"}`. The MCP
specification's tools error-handling example returns `-32602` (Invalid params) with
`"Unknown tool: invalid_tool_name"`; `-32601` means the *method* `tools/call` is not
implemented. A client that classifies by code can conclude the server does not support
tool calls at all and stop calling.

**Fix.** Use `_INVALID_PARAMS`. Keep the message, which is already good.
`tests/test_mcp.py:934` pins the current code and would need updating with it.

---

### 12. LOW — `tools/call` with a non-object `arguments` returns `-32600` rather than `-32602`
`src/bankmachine/mcp.py:1612-1613`. Measured:
`{"code": -32600, "message": "tools/call needs a name and arguments"}`. This is a params
problem, not a malformed request object. Same one-constant fix.

---

### 13. LOW — no `jsonrpc` version check, no initialize-before-use enforcement
`src/bankmachine/mcp.py:1538-1542`. Measured: `{"jsonrpc":"1.0","id":4,"method":"ping"}`
and `{"id":5,"method":"ping"}` (no `jsonrpc` member at all) both answer
`{"jsonrpc":"2.0","id":N,"result":{}}`; and `tools/call` before any `initialize` answers
normally. Harmless in practice for a single-client stdio server — noted only so the
looseness is a decision. If tightened, reject a missing/wrong `jsonrpc` with `-32600`.

---

### 14. LOW — errors carry good recovery prose but no structured recovery fields and no did-you-mean
`src/bankmachine/mcp.py:1697-1719`, `:1246-1249`, `:1176-1185`

Measured refusals — these are genuinely strong against sibling §2.4/§8.1, and better than
most servers:

```
"since must be a calendar date in YYYY-MM-DD form, got 'August 2024'"
"until (2025-01-01) is before since (2025-01-31), so the window selects nothing. Did the two get swapped?"
"limit must be at most 500, got 100000"
"account_id 99999 does not exist. list_accounts reports the ids that do."
"group_by must be one of category, merchant, account, month, flow_class, not 'unicorn'"
"query_transactions has no argument 'sinse'. It accepts: account_id, cursor, limit, since, until"
```

Valid-value lists ✅, accepted-argument lists ✅, next action ✅, bounds ✅. What is
missing against the sibling gallery: (a) no **did-you-mean** — `'sinse'` is one edit from
`since` and the message makes the agent diff two lists itself; (b) no **example call**;
(c) everything is one prose sentence inside
`structuredContent.error.message`, so a consumer must parse English rather than read
`valid_values` / `required` / `example` / `hint` fields.

**Fix.** Add optional `valid_values`, `example` and `hint` keys alongside `code` and
`message` in `_tool_error`'s `structuredContent.error`, and a Levenshtein nearest-match
suggestion in `_dispatch_tool`'s unknown-argument branch. Additive, no schema break
(the error payload is deliberately outside the tool's `outputSchema` — see `mcp.py:1705`,
which is the right call).

---

### 15. LOW — `money_summary(group_by=account)` returns the account id as a string, and passing it back as `account_id` is refused
`src/bankmachine/mcp.py:832-838`; `src/bankmachine/query.py:2295`

`group_key` is declared `{"type": "string"}` and documented *"an account id when grouping
by account"*. Measured: `[("5","Plaid Money Market"), ("4","Plaid Credit Card"), …]`.
Handing that straight to the obvious next call:

```
query_transactions(account_id="4")
-> "account_id must be a whole number, got str"
```

Recoverable, but it is a wasted turn on the single most natural drill-down, and it
sidesteps the surface's own stated rule. `_refuse_colliding_parameters` (`mcp.py:564`)
enforces "one name must mean one thing" across *inputs* only; the same reasoning applies
to an output a caller is told to feed back. Sibling principle 10 also says explicitly to
*coerce string-encoded numbers*.

**Fix.** Either coerce a digit-string `account_id` in `_whole_number` (cheapest, and
matches the sibling lesson), or add an `account_id` integer field to the `group_by=account`
rows — though the strict row schema forbids a field present under one `group_by` and
absent under another (`_refuse_optional_row_fields`, `mcp.py:597`), so coercion is the
smaller change.

---

### 16. LOW — requests are strictly sequential, with no cancellation and no timeout
`src/bankmachine/mcp.py:1738-1742`

One thread, one message at a time. `notifications/cancelled` is silently dropped
(`:1544-1556`, correct for a notification, but nothing acts on it), and no query carries a
timeout. While a long `money_summary` runs, `ping` cannot be answered, so a client with a
liveness timeout will consider the server dead. At single-household scale this is almost
certainly fine; it becomes real only alongside finding 4 (an unbounded merchant rollup)
and finding 17.

**Fix.** No change needed for the stated scope; worth a line in
`docs/connecting-an-mcp-client.md` saying calls are serialized, so an operator who sees a
client time out knows why.

---

### 17. LOW — no index on `posted_date`, so every windowed read is a full scan plus a sort
`src/bankmachine/store/schema.py:230-265`

The only transaction indexes are `(account_id, source_transaction_id)`,
`(account_id, import_fingerprint)`, `(account_id, posted_date)` and the partial pending
link. `EXPLAIN QUERY PLAN` against the schema built into a scratch SQLite:

```
SELECT * FROM transactions WHERE removed_at IS NULL AND posted_date>=? AND posted_date<=?
  ORDER BY posted_date DESC, transaction_id DESC LIMIT 100
    SCAN transactions
    USE TEMP B-TREE FOR ORDER BY

SELECT count(*) FROM transactions WHERE removed_at IS NULL AND posted_date>=? AND posted_date<=?
    SCAN transactions
```

Every `query_transactions` without `account_id` scans and sorts the whole table, twice
(rows + `matching`), and each page of a paged walk repeats it — so an N-page walk is
O(N × full scan). At a household's ~10–20k rows this is milliseconds even through
SQLCipher; `api-contract.md:410` already records ~1 ms at 10k / ~89 ms at a larger size.
Not a problem at the stated scope, recorded because the cost is superlinear in pages.

**Fix.** `Index("transactions_by_date", transactions.c.posted_date,
transactions.c.transaction_id)` would serve both the range and the ORDER BY and remove the
temp B-tree. One migration, no behaviour change.

---

### 18. LOW — README states a Python floor the project does not accept
`README.md:27` — *"Needs Python ≥3.11 and uv."* — against `pyproject.toml`
`requires-python = ">=3.14"`. A reader on 3.11/3.12/3.13 follows the README and gets a
resolver failure with no explanation. `tests/preferences/test_python_floor_is_exercised.py`
holds `pyproject` to `.python-version` but nothing holds the README to either.

**Fix.** Say 3.14, and add the README to whatever the floor test already reads.

---

## Answers to the four specific questions

**(a) Is `instructions` far longer than the ~250-token primer the siblings recommend, and
would the content be better carried as resources?**
Yes, and it is worse than a style problem — see finding 1. 6,673 chars ≈ 1,700 tokens,
about 7× the sibling §7.1 target, and **measured truncation at 2,045 chars in the client
running this session**, cutting six warning kinds, the envelope table, the
`external_spend` rule and the resource pointers. The resource layer to move it into
already exists, is already derived rather than authored (`mcp_resources.py`), and is
already advertised — but the sentence advertising it is on the truncated side. Invert:
primer first, URIs early, tables in the resources, and the act-on-this guidance onto the
payload where nothing trims it.

**(b) Do tool errors carry recovery hints?**
Yes, and this is one of the stronger parts of the surface — every refusal measured names
the field, the received value, and either the valid set, the bound, or the next call to
make (finding 14 quotes all six). Gaps: no did-you-mean on a near-miss argument name, no
example call, and no structured `valid_values`/`example`/`hint` fields — the recovery
information is prose an agent must parse.

**(c) Is there a CHANGELOG or tool-surface versioning?**
No consumer-facing one. `.prawduct/change-log.md` exists but is a governance journal
organised by work cycle, unreferenced from the README or the client guide. All eight tools
and both resources are graded `experimental` in `api-contract.md`; `serverInfo.version` is
the package version (`0.1.0`). The contract names "the change log" as the breaking-change
channel and there is nothing at the path a consumer would look. See finding 10.

**(d) Does anything violate "tool failures are isError results, protocol errors only for
unknown tool / bad args"?**
No violation of the *split*; two wrong *codes*. All three tool-level failures
(`invalid_argument`, `datastore_unservable`, `internal_error`) ride `isError: true` with a
stable branchable code and a remedy — exactly the cordyceps contract, and the deliberate
choice to put bad arguments on `isError` rather than a protocol error is documented at
`mcp.py:1106-1115` and is the better behaviour (the model can self-correct). JSON-RPC
errors are used only for request-level problems: unknown tool, malformed frame, non-object
params, unknown method, unknown resource URI. Nothing returns `isError: false` alongside a
failure. The defects are numeric: unknown tool uses `-32601` where the spec example uses
`-32602` (finding 11), and a non-object `arguments` uses `-32600` where `-32602` fits
(finding 12).

---

## Verified as SOUND

- **Handshake and version negotiation.** Client's version echoed when in
  `SUPPORTED_PROTOCOL_VERSIONS`; unknown version falls back to `2025-03-26` rather than
  failing (measured with `2099-01-01`). The reasoning for excluding `2026-07-28` — that
  revision makes `ttlMs`/`cacheScope` required on `ListToolsResult`, which this server does
  not send — is correct and unusually well-reasoned for a hand-rolled server.
- **Capabilities.** Exactly `tools` and `resources`, both with `listChanged: false`,
  `subscribe: false`, all of which are true. No `logging` or `completions` advertised, and
  none served. `prompts/list` correctly errors because `prompts` is not advertised.
- **`notifications/initialized`.** Correctly detected by *absence of the `id` member*, not
  by `id is None` — and a request with an explicit `"id": null` is correctly answered
  (measured). This is a distinction most hand-rolled servers get wrong.
- **`ping`.** Answers `{}`.
- **Line framing.** `json.dumps` with default `ensure_ascii=True` plus a single `\n`; no
  embedded newlines possible, no Content-Length framing. Blank lines skipped. Verified over
  every probe.
- **Malformed input.** Bad JSON → `-32700` with `id: null` and the session continues;
  deeply nested JSON → `RecursionError` caught separately and answered in the server's own
  words; invalid UTF-8 → `-32700`, with a 3-consecutive-failure give-up ceiling so a dead
  stream cannot spin. `params` as an array → `-32600` rather than an `AttributeError`
  escaping the loop. All measured.
- **Stdout discipline.** `tests/preferences/test_the_server_never_writes_to_stdout.py` is
  **stronger than the 3tears AST test it parallels**: it scans the transitive import
  closure of `bankmachine.mcp` (so scope moves with the imports, not with a blessed-file
  list), adds a separate import-time scan over the whole package, distinguishes
  *addressing* `sys.stdout` from *handing it over* as a call argument, refuses
  `from sys import stdout` and refuses `print` regardless of `file=`, and carries positive
  controls that assert the check both fires and lets the transport's own injected write
  through. Confirmed live: every drive produced protocol-only stdout with all logging on
  stderr.
- **Input validation.** The best part of the surface. Bad dates, wrong JSON types,
  `bool`-as-int, inverted windows, `limit` outside `[1, 500]` (refused, never clamped —
  and the reasoning at `mcp.py:1179-1182` is right), `account_id` below 1, unknown
  `account_id`, unknown `group_by`, unadvertised argument names, garbage cursor: all twelve
  cases measured return a structured `isError` with a stable code and an actionable
  sentence. No crash, no silent empty, no silently-dropped argument.
- **Cursor design.** Keyset over `(posted_date, transaction_id)` rather than an offset;
  version-tagged; fingerprinted against `(since, until, account_id)` so a cursor from a
  different question is refused rather than answered; `limit` deliberately excluded from
  the fingerprint; base64url unpadded; `RecursionError` handled in the decoder. A four-page
  walk reassembled all 390 rows with no duplicate and no gap. `next_cursor` present iff
  there is more. This is textbook.
- **Cross-tool consistency.** Measured: `list_accounts` 14 rows = `coverage.accounts` 14 =
  `get_coverage_report` 14 rows, identical account-id sets; `money_summary(group_by=account)`
  transaction sum 390 = `query_transactions.matching` 390 = `coverage.transactions` 390;
  `effective_window` identical between the two windowed tools on the same request.
- **Read-only posture.** Enforced in the `mode=ro` file handle rather than by convention,
  with `readOnlyHint`/`destructiveHint`/`idempotentHint`/`openWorldHint` all present and
  all correct on all five tools, and the annotations-are-hints caveat correctly noted at
  `mcp.py:142-147`.
- **Schema strictness.** `additionalProperties: False` and full `required` at every object
  level of every row, enforced at definition time by `_refuse_optional_row_fields` and
  `_refuse_loose_object`, plus `_refuse_colliding_parameters` for input names — and both
  run inside `_tool_definitions()` so no code path can obtain an unvalidated definition.
  Enums are taken from the live vocabularies (`envelope.WARNING_KINDS`,
  `query.FLOW_CLASSES`, `query.LIFECYCLE_VALUES`, `signs.VERDICTS`,
  `PROVENANCE_SOURCES`), never retyped. `outputSchema` is per-tool so a key's *absence* is
  meaningful.
- **Error hygiene / no leaks.** Verified against a raised
  `RuntimeError("SELECT secret FROM vault …")`: the wire carries only a code, a remedy and
  `bankmachine store status`; no class name, no SQL, no path. `_unservable_remedy`
  (`query.py:1110-1193`) deliberately omits the datastore path from every branch — including
  a documented note about the one branch where it got back in once — because an absolute
  path on a personal machine carries the operator's account name. `_error` messages carry
  no internals. No secret, path or PII observed on any of ~40 measured frames.
- **Resources.** Two documents, stable custom-scheme URIs (`bankmachine://reference/…`,
  correctly chosen so a client does not try to fetch them itself), `mimeType:
  text/markdown` (right for model-read prose), `size` in bytes as the field specifies,
  `resources/templates/list` answered with `[]` rather than refused, unknown URI refused
  with `-32602` naming what *is* served. Both documents are derived from
  `envelope.WARNING_KINDS` and the published `outputSchema`s rather than authored, so they
  cannot drift; and `_reference_documents()` reads no datastore, so the reference surface
  answers even when the store is unreadable.
- **Unservable-datastore handling.** A *missing* store answers with zeroed coverage and a
  warning saying the zeroes mean nothing was read; every *other* unservable state refuses
  with `datastore_unservable` and a state-specific remedy, rather than answering zero over
  data that exists. The server still starts in both cases (AC-ARCH.3), so the tool is there
  to be asked why. `_readable()` is checked per call, so a store that goes bad mid-session
  is caught.
- **Build identity.** `_meta["bankmachine/build"]` on the handshake (correctly namespaced,
  and correctly *not* on `serverInfo`, whose type would drop it), the same three keys on
  every answer, captured once at import from one `lru_cache`d reading, `commit: null` and
  `dirty: null` together when the build cannot be identified. `_git` is timeout-bounded and
  catches `OSError`/`SubprocessError`.
- **Documentation guards.** `test_the_documented_tool_surface_is_the_built_one.py` holds
  four documents' tool names *and* counts against `_tool_definitions()`, with anchor
  phrases that fail rather than pass quietly when they stop matching, and a negative
  control proving a rename does not walk past the comparison.
  `test_the_warning_vocabulary_is_closed.py` reconciles `envelope.WARNING_KINDS` against
  the contract table in both directions and against the client guide, and has negative
  controls for both readers. This is the strongest documentation-as-contract machinery of
  the four repos compared (sibling §2.6) — it just does not extend to what the *client*
  delivers (finding 1) or to the README's Python floor (finding 18).
- **Environment confession.** `environment` on every answer, in `serverInfo.title`
  (`bankmachine (sandbox)` — measured), and in separate datastores/keychain accounts. A
  fixture cannot pass for real money.
