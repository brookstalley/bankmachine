# Build plan — MCP surface alignment

Origin: a review of three sibling repos' MCP practice (`../hallucinote`, `../cordyceps`,
`../3tears`) against this server, plus a direct reading of the `mcp` SDK's own types at
2.2.0 — the same version `api-notes-plaid.md` §18 cited when it recorded the no-SDK
decision.

**What this plan is not.** It is not a conformance pass. Two of the three siblings'
strongest positions are ones this repo should keep departing from, and the Departures section
records why.
The work here is the subset that is either a verified protocol defect or a gap all three
siblings independently found worth closing.

Requirements confidence: **High** for chunks 01, 02, 04 and 06 — each is a defect verified
against the SDK's own type definitions or a guard over a property that already holds.
**Medium** for chunk 03, which rewrites agent-facing prose; the destination is measured
but the editorial judgment inside it is not derivable from evidence.

Critic mode: `cumulative-final` — chunks 01 and 02 are dispatched in parallel and land
close together, so a per-chunk review would split one small diff across two reviewers.

partition: **One parallel pair, then a serial chain.** `src/bankmachine/mcp.py` has exactly
one owner at any moment; that is the whole partition. Chunks 01 and 02 run concurrently
because 02 creates only new files under `tests/preferences/` and touches nothing 01 owns.
Everything after that edits `mcp.py`, so it runs in order: **06 → 05 → 03**.

That order is a dependency, not a preference. 03 compresses agent-facing prose out of
`instructions`, and 05 builds the surface that prose moves *to* — so 03 last is the only
sequence where it can cut against a real destination instead of deleting. 06 is
independent of both and goes first because it is the narrowest.

Delegate verification ceiling: `uv run pytest tests/test_mcp.py` for chunks 01 and 06,
`uv run pytest tests/preferences` for chunk 02, `uv run pytest tests/test_mcp.py
tests/test_mcp_resources.py` for 05. No delegate runs the full suite — the coordinator
owns the combined run at integration.

---

## Status

- [x] **01 — The handshake tells the truth about itself** (delegated, parallel)
- [x] **02 — Two structural guards** (delegated, parallel)
- [ ] **06 — The envelope is machine-checkable** (delegated, after 01)
- [ ] **05 — A resources surface** (delegated, after 06)
- [ ] **03 — The envelope tells the agent what to do about it** (coordinator, after 05)
- [ ] **04 — The artifacts say what the code now does** (coordinator, last)
- [ ] **07 — The go-red harness learns the two new guards** (coordinator, last)

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

### The `serverInfo` keys, and the norm that looks like it forbids them

Chunk 01 removed `commit` and `dirty` from `serverInfo`. `api-contract.md`
§ Deprecation & Compatibility carries 🔴 *"Never remove or repurpose an existing field."*
Adjudicated rather than assumed, and written down so a reviewer can disagree:

**Not a departure.** Two independent grounds. The Surface Inventory grades tools and CLI
commands and does not list the handshake, and *"members not listed are internal and carry
no promise."* And the norm's own rationale — *"a consumer that still reads it gets a wrong
answer instead of an error"* — cannot bite, because no conforming consumer could ever read
these keys: `Implementation` does not declare them and the SDK's wire base leaves
`extra="ignore"` in force, so they were discarded before any client saw them.

Read at the level of what a consumer can observe, this is not a removal at all. It is a
relocation from a channel that dropped the value to one that delivers it, and the
observable surface strictly grows: build identity at handshake time was previously
unreachable and now is not.

🔴 **This is not licence to treat the norm as soft.** The exception rests entirely on
those keys being unobservable in principle. A field a consumer *can* read stays covered by
the norm in full, and the fact that this one was invisible is the defect chunk 01 fixed —
not a precedent for removing things.

---

## 5. Chunk 06 — The envelope is machine-checkable

Ratified 2026-09-08 by the owner, having been raised as a deferred decision. Publish
`outputSchema` on every tool.

🔴 **No sibling does this**, so it does not ride on their precedent and the argument has
to stand alone. It is this: the envelope is currently described to the agent *only* in
prose, in `instructions` and in each tool's description, and prose is what chunk 03 is
about to compress. A schema is the carrier that does not thin out when the prose does,
and `Tool.output_schema` exists at 2.2.0 for exactly this.

**The design content, which is why this is not a transcription.** `Answer.to_wire` emits
`effective_window` and `truncation` *conditionally*, and their absence is load-bearing —
`api-contract.md`: an absent `effective_window` says this tool takes no window, an absent
`truncation` says it returns every row it found. A single schema with both keys optional
would say the opposite of the truth: that any tool might carry either. So the schema is
**per-tool**, and a windowed tool's schema requires `effective_window` while an
unwindowed tool's forbids it.

That is the same distinction `Answer` already enforces in the type system by giving both
fields no default, and the schema should be derived from the same source rather than
hand-maintained beside it — a second description of the envelope is one that stops
agreeing with the first.

**Watch:** `structuredContent` must continue to validate against what is published. A
schema that drifts from the payload is worse than none, because a validating client now
rejects good answers. The test that proves this should validate a *real* answer from each
tool against that tool's published schema, not a fixture shaped like one.

### Done when

- Every tool publishes an `outputSchema` derived from the envelope's own definition.
- A windowed tool's schema requires `effective_window`; an unwindowed tool's does not
  permit it. Same for `truncation` on the capped tool.
- A live answer from each of the four tools validates against that tool's schema.
- `uv run pytest tests/test_mcp.py` green.

---

## 6. Chunk 05 — A resources surface

Ratified 2026-09-08 by the owner. Declare the `resources` capability and serve
agent-facing reference material through it.

**The argument** (hallucinote `resources/__init__.py`): *"Tools are imperative; the agent
decides to call them. Resources are addressable content; the MCP client (or the agent)
reads them by URI without consuming a per-tool turn."* This is the destination for the
deep envelope reference that chunk 03 compresses out of `instructions` — the detail stops
costing every session's context and becomes something fetched when needed.

**What to serve.** At minimum the warning vocabulary in full (each kind: what it means,
what it implies about the answer, what the agent should do), and the envelope reference.
Both currently live as prose inside `_instructions()`.

🔴 **Derive them; do not retype them.** The warning vocabulary has a definition already —
`api-contract.md` § "The warning vocabulary — stable, machine-readable" — and
`tests/test_mcp.py` already pins that `instructions` names every kind the vocabulary
defines. A resource that restates the list by hand is a third copy, and the existing
drift test will not be watching it. Whatever mechanism keeps `instructions` honest must
cover the resource too.

**The capability declaration is currently deliberate and minimal**, and its comment says
why: *"Declaring a capability this server does not serve would have the client offer the
operator something that fails."* Adding `resources` is fine precisely because it will now
be served — keep the comment true by keeping `listChanged` honest about what is
implemented.

### Done when

- `resources/list` and `resources/read` are implemented, and the capability is declared
  only because they are.
- The warning vocabulary and envelope reference are served, derived from their existing
  definitions rather than restated.
- An unknown URI is refused the way an unknown tool is — a sentence the caller can act on,
  not a stack-shaped string.
- The whole resource surface answers against a missing datastore, as every tool already
  must (AC-ARCH.3).
- `uv run pytest tests/test_mcp.py tests/test_mcp_resources.py` green.

---

## 7. After this wave

Ruled 2026-09-08 by the owner: **finish the MCP surface next.** Six of the ten specified
tools are unbuilt, and `get_coverage_report` is the one to take first — `api-contract.md`
calls it half the verification surface, so `get_pipeline_health` is currently the whole of
it, and the product's headline goal is *"answer it, or say why you should not."*

Recorded here so the next session does not have to re-derive it: this does **not**
discharge `mcp-production-readiness.md`'s open blockers (#19, #20, #21, #22, #23, #24, and
items 4–7 of its "what has to be true before yes" list). Those remain the gate on real
accounts. The ruling sets the order of work, not the go/no-go.

---

## 7b. Chunk 07 — The go-red harness learns the two new guards

`verify_norms_go_red.py` proves each norm test actually goes red by writing the broken
version to disk, running the named test, and putting it back — fifty-five times. Chunk 02
added two guards and, correctly, did not touch that harness: it is an existing file and was
outside the delegate's boundary. So the two newest norm tests are currently the only ones
with no proof they can fail.

That is the exact gap `learnings.md` describes as this project's recurring shape — a check
whose only bad-news channel is the absence of output.

**Do:** add a case for each. A stdout write inside the server's import closure, and a tool
renamed in the docs but not the code.

🔴 **Two operational rules from `learnings.md`, both load-bearing.** The harness sabotages
the working tree while it runs, so nothing else may read the tree during the window — no
suite, no review, no commit. And it mutates by literal `str.replace`, so **prefer an anchor
naming a whole expression or keyword argument over one spanning a formatter-chosen line
break**; the latter is a hostage to the next reformat.

Chunk 02's delegate left the existing 2026-07-28 anchor byte-identical and it still matches
exactly once — verified at integration. Chunks 03 and 04 will move other anchors, so run
this after them, not before.

---

## 8. Departures — reviewed and deliberately not adopted

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

**A `resources` capability** and **`outputSchema`** were both raised here as deferred
decisions and were **ratified into this wave** on 2026-09-08 — they are chunks 05 and 06
above, not departures. Both are noted here only because a reader scanning this section for
"what did they decide about resources" should not conclude it was declined.
