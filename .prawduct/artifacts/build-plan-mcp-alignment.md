# Build plan — MCP surface alignment

Origin: a review of three sibling repos' MCP practice (`../hallucinote`, `../cordyceps`,
`../3tears`) against this server, plus a direct reading of the `mcp` SDK's own types at
2.2.0 — the same version `api-notes-plaid.md` §18 cited when it recorded the no-SDK
decision.

**What this plan is not.** It is not a conformance pass. Two of the three siblings'
strongest positions are ones this repo should keep departing from, and §5 records why.
The work here is the subset that is either a verified protocol defect or a gap all three
siblings independently found worth closing.

Requirements confidence: **High** for chunks 01, 02 and 04 — each is a defect verified
against the SDK's own type definitions or a guard over a property that already holds.
**Medium** for chunk 03, which rewrites agent-facing prose; the destination is measured
but the editorial judgment inside it is not derivable from evidence.

Critic mode: `cumulative-final` — chunks 01 and 02 are dispatched in parallel and land
close together, so a per-chunk review would split one small diff across two reviewers.

partition: **Two delegates, not five.** Chunks 01 and 03 both edit `src/bankmachine/mcp.py`
and `tests/test_mcp.py`, so they cannot run concurrently — 01 is delegated, 03 is the
coordinator's and runs after it merges. Chunk 02 creates only new files under
`tests/preferences/` and is genuinely independent, so it runs beside 01. Chunk 04 is the
coordinator's. `mcp.py` has exactly one owner at any moment; that is the whole partition.

Delegate verification ceiling: `uv run pytest tests/test_mcp.py` for chunk 01,
`uv run pytest tests/preferences` for chunk 02. Neither delegate runs the full suite —
the coordinator owns the combined run at integration.

---

## Status

- [ ] **01 — The handshake tells the truth about itself** (delegated)
- [ ] **02 — Two structural guards** (delegated)
- [ ] **03 — The envelope tells the agent what to do about it** (coordinator)
- [ ] **04 — The artifacts say what the code now does** (coordinator)

---

## 1. Chunk 01 — The handshake tells the truth about itself

Three defects, each verified against `mcp_types` 2.2.0 rather than recalled. All live in
`src/bankmachine/mcp.py`'s handshake and read loop.

### 01a. `2026-07-28` is offered on a path it does not exist on

`mcp_types.version` partitions the registry, and the partition is the point:

```
HANDSHAKE_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
MODERN_PROTOCOL_VERSIONS    = ("2026-07-28",)
LATEST_PROTOCOL_VERSION     = KNOWN_PROTOCOL_VERSIONS[-1]   # "newest ... (any era)"
```

`InitializeRequestParams` and `InitializeResult` both carry: *"Removed in protocol
2026-07-28; sent/received on sessions negotiating <= 2025-11-25."* 2026-07-28 uses a
stateless per-request envelope reached by a `server/discover` probe, not by `initialize`.

`mcp.py` names `2026-07-28` as `LATEST_PROTOCOL_VERSION` and lists it first in
`SUPPORTED_PROTOCOL_VERSIONS`, so `initialize` will echo it back to any client that asks
— agreeing to a protocol era this server does not implement. The reading error is
identifiable: `LATEST_PROTOCOL_VERSION` is documented as the newest revision the SDK
speaks *in any era*, and was taken as the newest one a handshake can reach.

**Do:** the handshake offers handshake versions only. Newest offered becomes
`2025-11-25`. Keep `FALLBACK_PROTOCOL_VERSION` at `2025-03-26`.

**Also:** `ListToolsResult` is a `CacheableResult`, and on 2026-07-28 `ttlMs` and
`cacheScope` are *required* on the wire. This server sends neither. That is a second,
independent reason the claim was never serviceable — worth a comment so nobody restores
the constant on the grounds that the handshake now "works".

### 01b. `2025-11-25` is missing, so current clients are downgraded two revisions

`SUPPORTED_PROTOCOL_VERSIONS` skips it. A client asking for the newest handshake revision
matches nothing and falls to `2025-03-26`.

**Do:** the supported tuple becomes the SDK's handshake set. `2024-11-05` is included or
excluded on one stated ground, not left to fall out of an edit.

### 01c. An explicit `"id": null` is treated as a notification, so its client hangs

JSON-RPC 2.0: a Notification is a Request object *without* an `id` member. An `id` present
and null is a request, and it gets a response carrying `"id": null`.

`_handle` does `message_id = message.get("id")` and then treats `message_id is None` as a
notification — which conflates *absent* with *present and null*. A client sending
`{"jsonrpc":"2.0","id":null,"method":"ping"}` is never answered and waits forever.

Cordyceps pins exactly this distinction (`Core/JsonRpcEnvelope.cs:43-63`) after its own
version of the bug — there, a mangled id destroyed the response *after* the tool ran, so
clients retried and double-applied mutations. Nothing here mutates, so the blast radius
is a hang rather than a double-write. Fix it anyway: the read loop's whole job is that a
client is never left waiting, which is already why the UTF-8 and recursion guards exist.

**Do:** decide notification-ness on `"id" in message`, not on the value's truthiness.
Note in the comment that the two are different questions, so it is not re-collapsed.

### 01d. The build identity in `serverInfo` never reaches an SDK-based client

`Implementation` — the `serverInfo` type — declares `name`, `title`, `version`,
`description`, `websiteUrl`, `icons`. Nothing else. The wire base is
`ConfigDict(populate_by_name=True)`, so pydantic's default `extra="ignore"` applies and
unknown keys are dropped silently.

`_server_info` sends `commit` and `dirty`. They are discarded before any client sees them,
which makes this comment false as written:

> *"The handshake carries it too, so a client can show which build it connected to before
> any tool is called."*

`InitializeResult` inherits `meta: Meta | None = Field(alias="_meta")` from `Result`, and
`_meta` is the sanctioned extension point.

**Do:** move build identity to the initialize result's `_meta`. Correct the comment to say
what is actually true. The per-answer `build` key in `Answer.to_wire` is untouched and
remains the load-bearing carrier — it was never the part that was broken.

### 01e. Tool annotations

Add `annotations` to every tool definition: `readOnlyHint: true`, `idempotentHint: true`,
`openWorldHint: false`, `destructiveHint: false`.

🔴 **This one has no sibling precedent and is not being copied from anyone.** All three
reviewed repos deliberately publish no annotations. It earns its place on a different
argument: `api-contract.md` § Direction ratifies *"the MCP surface is read-only; there are
no mutation tools, and adding one is not a decision this norm leaves open"*, and that norm
currently has no machine-readable expression anywhere on the wire. A client cannot ask.
`ToolAnnotations` is the field the protocol provides for exactly that statement, and the
norm is already true, so the annotation is a description of fact rather than a promise.

The SDK's own caveat is worth carrying in the comment: annotations are *hints*, and
"clients should never make tool use decisions based on ToolAnnotations received from
untrusted servers." It is a declaration, not an enforcement — the enforcement stays where
it is, in the `mode=ro` file handle.

### Done when

- The handshake offers only versions reachable through `initialize`.
- A client requesting `2025-11-25` is answered `2025-11-25`.
- A request with an explicit null id is answered; a message with no id member is not.
- Build identity reaches the client through `_meta` and a test proves it survives a
  strict reading of `Implementation`.
- Every tool carries read-only annotations, and a test derives the expectation from
  `_tool_definitions()` rather than restating the list.
- `uv run pytest tests/test_mcp.py` green.

---

## 2. Chunk 02 — Two structural guards

New files under `tests/preferences/` only. Both pin properties that **already hold** —
this chunk should not change any behaviour, and if it goes red on first run that is a
finding to report rather than a thing to fix by editing the product.

### 02a. Nothing in the server's path writes to stdout

3tears (`packages/mcp/tests/unit/test_no_stdio_writes.py`) walks the AST of every module
in the server package and rejects `print(...)` — *with or without* a `file=` kwarg,
"because the function name itself is the smell" — and `sys.stdout.write` /
`sys.stderr.write`.

The reason this matters more here than the current clean state suggests: stdout **is** the
transport. One stray write corrupts the framing mid-session, and the operator sees the
tool disappear rather than fail — the exact outcome `cmd_mcp`'s docstring already says it
exists to prevent. `cli/` modules print to stdout freely and legitimately; the guard's
value is that it holds the line between them.

Scope it to the modules the server actually reaches, and let `mcp.py`'s own
`stdout.write` through by construction — it is the transport, addressed through the
injected handle rather than the module global. A guard that cannot express that
distinction is not ready.

Carry a **positive control**, as `test_no_provider_identity.py` does: a scan over zero
files, or with a pattern that matches nothing, passes forever.

### 02b. The advertised tool surface matches what the docs claim

Hallucinote pins its tool count against the registry in four separate places
(`test_primer_tool_count_matches_actual_registry` and siblings), and its note on the
marketplace one is the interesting part: that surface used different phrasing from the
README, so no existing regex covered it, and it was *"the number could drift silently on
the most public surface of all."*

Here the count appears in `README.md`, `docs/connecting-an-mcp-client.md` and
`api-contract.md`, all currently saying four-of-ten. Six more tools are specified and
unbuilt, so this number is going to move.

**Do:** derive the built set from `_tool_definitions()` and assert each documenting
surface agrees — on the names, not only the count, since a rename drifts a count-only
check right past it.

### Done when

- Both guards pass, each with a positive control proving it is scanning something.
- `uv run pytest tests/preferences` green.
- No file outside `tests/preferences/` is touched.

---

## 3. Chunk 03 — The envelope tells the agent what to do about it

Coordinator's, after 01 merges. The one chunk here that is judgment rather than defect.

**The gap.** All three siblings independently ship a symptom → meaning → action table
(cordyceps `Knowledge/CommonErrorsGuide.md:46-51`; hallucinote `error-recovery.md` and
`gaps.md`, whose opening line is *"the server returns teaching errors that point here —
don't waste a turn discovering them"*). This server has the most precisely specified
warning vocabulary of the four — `stale`, `degraded`, `gapped`, `partial`, `rule-applied`,
`window_starts_before_coverage`, `window_extends_past_coverage`, `rows_truncated`,
`counted_during_change` — and tells the agent what not one of them means it should *do*.

Defining the vocabulary is the half this repo has already done well. The half that
decides whether an answer gets qualified is missing.

**The budget.** Measured on this build:

| surface | now | hallucinote's stated budget |
|---|---|---|
| server `instructions` | ~773 tokens | ≤500 |
| `query_transactions` description | ~413 tokens | ≤200 per tool |
| whole agent-facing surface | ~1,864 tokens, 4 tools | ~2,000, 10 tools |

`api-contract.md` specifies ten tools. At the current density that surface arrives near
4,600 tokens. Hallucinote's numbers carry a citation trail — Anthropic's tool-search
threshold, the Speakeasy Pet-Store result, Copilot's 40→13 consolidation — rather than a
preference.

**The move that resolves both**, and it is one move: cordyceps cut its agent-facing docs
49% (1,648 → 835 lines) by converting prose to tables, *"optimized for LLM consumption,
not human reading."* A table is denser than the paragraph explaining the same thing, so
replacing envelope *explanation* with a decision *table* buys the budget and closes the
gap at once.

🔴 **The constraint on this chunk.** `api-contract.md` § Direction ratifies that
incompleteness rides the success path where a consumer cannot miss it, on the ground that
*"the consumer is an analyst agent that cannot see a caveat which is not in the payload."*
Shortening instructions must not thin that out. Anything cut has to be cut because the
table says it more compactly, never because it fit the budget. If the two genuinely
conflict, the norm wins and the budget is missed with that recorded — hallucinote's own
§3.8 says hard constraints go in every carrier and that *"repetition beats subtle."*

### Done when

- Every warning kind in the vocabulary has a stated agent action.
- Instructions measurably smaller, with the number recorded.
- The existing drift tests (`test_the_instructions_name_every_warning_kind_the_vocabulary_defines`,
  `test_the_instructions_name_every_field_the_envelope_actually_carries`) still pass —
  they are contracts, and this chunk is exactly the edit they exist to catch.

---

## 4. Chunk 04 — The artifacts say what the code now does

- `api-notes-plaid.md` §18 records the wire format "read from `mcp.types` 2.2.0". Its
  `Tool` line omits `outputSchema`, and its protocol-version line is the source of 01a.
  Correct both, and record the handshake/modern split — §18 is where the next person will
  look.
- `api-contract.md` § Versioning: the negotiated protocol range is a contract and is now
  different.
- The §18 `[DECISION: ...]` record predicted this exact failure — *"a hand-rolled handshake
  can be subtly wrong in a way that fails at connection time rather than in a test."* It
  came true. Record that against the decision, since it is the evidence the revisit
  clause asked for.

---

## 5. Departures — reviewed and deliberately not adopted

Recorded so a later reader does not re-litigate them, and so the plan is honest that
"align to the siblings" was not the finding.

**Opaque pagination cursors.** Hallucinote names them an anti-pattern (`mcp-tool-design.md`
§13.5: *"State in the wire. No session tokens, no opaque cursors. Each tool call is
self-contained."*). This repo shipped one four commits ago and keeps it. Its tools are
self-contained control calls against a live host; `query_transactions` is a windowed read
over a dataset `mcp-production-readiness.md` estimates in the thousands, where the
alternative is a 1000-row ceiling that cannot be paged past at all. That ceiling is why
#17 blocked. Different problem, different answer.

**`structuredContent` as an unused feature.** Neither cordyceps nor hallucinote sends it.
This server does, which is 3tears' Rule D5 (*"a structured handler result rides BOTH
faces"*). Ahead, not behind. No change.

**Consolidating tools behind an `action` parameter.** Cordyceps (7 tools / 100+ actions)
and hallucinote (13 unified tools, consolidated from 52) both do this under token
pressure. Four tools — ten specified — is inside the band where selection accuracy holds,
so the consolidation would buy nothing and cost the flat, typed schemas that make this
surface's arguments checkable. Revisit only if the specified ten grows.

**MCP prompts.** Hallucinote removed theirs (`mcp-tool-design.md` §6.2): Claude Code
surfaces prompts only as user-facing slash commands, so an agent can never reach them.
Not adopted, on their evidence.

**A `resources` capability.** Genuinely attractive — hallucinote's case is that resources
do not compete with tools for selection and cost no turn, which is the natural home for
the deep envelope reference chunk 03 is compressing. Deferred rather than dismissed: it is
a new capability on a surface whose current capability declaration is deliberately minimal
(*"declaring a capability this server does not serve would have the client offer the
operator something that fails"*), and it wants its own decision.

**`outputSchema`.** `Tool.output_schema` exists at 2.2.0 and no sibling publishes one.
Mechanically derivable from `Answer.to_wire`, and it would make the envelope
machine-checkable instead of prose-described — but the envelope has conditional keys
(`effective_window` and `truncation` are present per-tool by design, and their *absence*
is load-bearing information), so the schema has real design content and is not a
transcription. Deferred to its own decision alongside resources.
