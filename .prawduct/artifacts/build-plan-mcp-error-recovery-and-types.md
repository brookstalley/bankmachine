---
artifact: build-plan
version: 1
scope: mcp-error-recovery-and-types
branch: feature/mcp-error-recovery-and-types
depends_on:
  - artifact: api-contract
  - artifact: api-notes-plaid
  - artifact: security-model
governed_by:
  - artifact: api-contract
    dispositions:
      - "MCP surface is read-only, no mutation tools → conforms; nothing here adds a tool or opens a handle"
      - "freshness stamp, incompleteness rides the success path as a warning → conforms; this changes only the refusal path, which the norm leaves to § Hard errors"
      - "CLI three-way exit code → inapplicable because no CLI surface changes"
      - "tool boundary drawn where answer SHAPE changes → inapplicable because no tool is added or split"
      - "stored balance reported with its lifecycle → inapplicable because no balance is reported"
  - artifact: security-model
    dispositions:
      - "the aggregator's API is the only network destination → conforms; `mcp-types` enters the DEV group only, has no transport, and no runtime module imports it — pinned by a test, not asserted"
      - "secrets live in the OS keychain, never in the datastore or a file → inapplicable because nothing here reads, writes or transports a secret"
      - "logs are redacted → conforms; the refusal path logs what it already logged, and chunk 02 adds no exception text to it. The recovery fields are argument NAMES, the caller's own values and closed sets this server publishes — none is operator data"
      - "no committed file holds a credential-like string → conforms; the only new committed strings are `uv.lock` package hashes, which `tests/preferences/check-no-personal-data.sh` already passes over as lock metadata"
partition: serial — both chunks edit `mcp.py` and `tests/test_mcp.py`, and chunk 01 is small enough that a delegate's briefing would cost more wall clock than the work
last_validated: 2026-09-16
---

# Build plan: structured recovery on refusals (#31), and `mcp-types` as a test oracle (#32)

## Requirements Confidence

**Level:** High for chunk 01, Medium-High for chunk 02.

**Why:** #32's design, acceptance and scope were ruled by the owner on 2026-09-16 and are quoted
in the issue body. #31 was ruled *built* on 2026-09-10 with its expected outcome stated (*"an agent
should be able to construct a valid retry from the payload without parsing prose. No change to
which conditions raise an error, or to the wording of the human sentence"*), but not its field
names. The field vocabulary is a published wire shape, so it is written into `api-contract.md`
§ Hard errors before code in chunk 02, and recorded here as decisions the owner can veto.

**Open assumptions / decisions:**

- `[DECISION: recovery fields ride ONLY `invalid_argument` | `datastore_unservable` and
  `internal_error` have no caller-side retry to construct; fields that are always null on two of
  three codes are noise | user can veto/override]`
- `[DECISION: the block is `arguments`, `required`, `optional`, `valid_values`,
  `valid_values_from`, `minimum`, `maximum`, `max_length`, `example`, `see` — each key present
  only when it applies | this is hallucinote's shape, whose `valid_actions`/`required`/`optional`/
  `example` fields are the practice #31 cites, and its rule that a field is serialized only when
  set. A null-filled block would carry eight nulls on the commonest refusal, and absence is already
  how this surface says "not applicable" for warning kinds | user can veto/override]`
- 🔴 `[DECISION: the refusal's `content` text carries the compact JSON of the error object, as
  every successful answer already does, instead of the bare sentence | MEASURED, not assumed:
  `mcp-acceptance-round-2.md` and `-3.md` both record that through a real client only the error
  STRING was visible and `structuredContent.error.code` was not — so recovery fields in
  `structuredContent` alone would be invisible to the agent they are for, and #31 would ship
  without its own outcome. The sentence is unchanged and rides as `message` | user can
  veto/override]`
- `[DECISION: no free-text `hint` field; instead `see` carries a reference URI this server
  serves, and a new `bankmachine://reference/refusals` document teaches recovery | hallucinote
  pairs its fields with a served `error-recovery.md` guide and its hints route to
  `action='help'`; this server's equivalent of "help" is its reference documents, and a URI is a
  machine-followable pointer where a second sentence is more prose to parse | user can
  veto/override]`
- `[DECISION: an unknown TOOL name (`-32602`, a protocol error) is out of scope | the issue is
  about tool arguments; that refusal is a JSON-RPC error with its own `data` slot, a different
  surface | user can veto/override]`
- `[ASSUMPTION: every refusal class can carry its recovery from its raise site | verified by
  reading all raise sites 2026-09-16: each site already has the argument name and, where one
  exists, the closed set in hand]`

## Status

- [x] Chunk 01: `mcp-types` as a test-only oracle for the hand-copied protocol constants (#32)
- [x] Chunk 02: Structured recovery fields on `invalid_argument` (#31)

Context: plan written 2026-09-16 on `feature/mcp-error-recovery-and-types`, cut from `develop` at
`6a617be`.

Chunk 01 closed 2026-09-16 at `c08466a`. Gate green (1957 passed); five guards seen red by changing
the fact each holds. Review found 0 blocking:

**rev-20260916T225752Z-363710e8** — scope `mcp-error-recovery-and-types`, 2026-09-16T22:58:53Z

| Finding | Severity | State | Detail |
|---|---|---|---|
| R-1 | warning | accepted | Carried into chunk 02's commit, which edits mcp.py and the constants test anyway: the annotation keys join the comparison and the overbroad claim becomes true |
| R-2 | warning | accepted | Fixed in the plan rather than deferred: the three security-model norms now carry explicit dispositions |
| R-3 | note | accepted | Correct guess, no action: chunk 01 is the one reviewed, and it is now ticked |

**3 findings** (2 warning, 1 note) — accepted: 3.

Chunk 02 closed 2026-09-17. Gate green; five guards seen red. One existing test changed with the
wire it asserts on: the unknown-argument refusal's sentence is now read from `error.message` rather
than from the whole of `content[0].text`, which is the error object as JSON — the same claim, read
from the field that carries it, plus a new assertion that the sentence still reaches a text-only
client.

🔴 **Chunk 02's design changed after the owner reviewed the question the plan asked.** The owner
pointed at hallucinote's teaching errors as the standard. Two decisions above are the result: the
fields ride the `content` text as well, and the server grows a refusals reference document.

## Verification Strategy

The gate is `bash scripts/check.sh {junit_xml}` (pytest, `ruff check`, `ruff format --check`,
`mypy`). Each new guard is seen red with its mechanism removed before it is trusted — by editing the
source, running the one test, and restoring. The norm harness is not running while that happens.

Chunk 02 is proved from the consumer's side: for each refusal, a retry is built **from the
structured fields alone**, never from `message`, and must succeed. A test that only asserts the
fields exist would pass on fields that do not actually lead anywhere.

## Build Chunks

### Chunk 01: `mcp-types` as a test-only oracle for the hand-copied protocol constants (#32)

**Type:** code · **Critic mode:** chunk

- `mcp-types` goes in `[dependency-groups] dev`, and `uv.lock` is regenerated. The runtime
  `dependencies` are untouched.
- A test compares `mcp.py`'s copied constants with `mcp_types`: `LATEST_HANDSHAKE_VERSION`,
  `FALLBACK_PROTOCOL_VERSION` against `DEFAULT_NEGOTIATED_VERSION`, `SUPPORTED_PROTOCOL_VERSIONS`
  against `HANDSHAKE_PROTOCOL_VERSIONS`, and the JSON-RPC error codes against
  `mcp_types.jsonrpc`.
- `tests/test_mcp.py`'s hand-typed `_IMPLEMENTATION_FIELDS` is read from
  `mcp_types.Implementation`'s wire aliases instead. It is the same kind of copy, and it is where
  one of the four defects lived.
- A test fails if `mcp-types` or `pydantic` reaches the runtime `dependencies`, or if any module
  under `src/` imports `mcp_types`.
- Each guard seen red: a constant changed, a runtime dependency added, a runtime import added.
- Artifacts: `api-notes-plaid.md` §18 re-recorded (the revisit clause fired, on what evidence, the
  test-only outcome, and the footprint figure corrected); the `mcp.py` comments that say "read from
  `mcp_types` 2.2.0" name the test that now holds them to it.

**Done when:** gate green; each guard seen red; Critic chunk review has no blocking findings.

### Chunk 02: Structured recovery fields on `invalid_argument` (#31)

**Type:** code · **Critic mode:** chunk

- **Requirement first:** `api-contract.md` § Hard errors gains the recovery block's field table,
  meaning and null rule, before any code.
- One base refusal type in `envelope.py` carries a `Recovery` value, and every refusal class
  (`BadArgumentError`, `UnknownAccountError`, `UnknownCategoryError`,
  `UnknownInvestmentTypeError`, `InvertedWindowError`, `BadFilterError`, `MalformedCursorError`,
  `BadGroupingError`) derives from it. The constructor REQUIRES the recovery, so a raise site that
  forgets one is a type error rather than a silent null.
- The MCP boundary catches the base type, adds `required`/`optional` from the tool's own
  `inputSchema`, and emits the block beside `code` and `message`. The message wording does not
  change, and no condition that refuses is added or removed.
- The refusal's `content` text carries the compact JSON of the error object, the way
  `_tool_result` already mirrors an answer, so the fields reach the model in a client that
  forwards only text.
- A `bankmachine://reference/refusals` document joins the two this server already serves: the
  three error codes, every recovery field, and the corrected call each one points to. Derived from
  the same vocabulary the code emits, never a second hand-kept list.
- Tests, per refusal: the block carries the argument(s) at fault and the specific recovery, and a
  retry built only from the fields succeeds (unknown argument dropped, date replaced by `example`,
  bound replaced by `minimum`/`maximum`, category replaced from `valid_values`, account id looked up
  through `valid_values_from`, window swapped, cursor omitted). A key is present only when it
  applies. `datastore_unservable` and `internal_error` still carry `{code, message}` only.
- A guard that every recovery key is described in `api-contract.md`, the same shape as the
  documented-wire guard.
- Carried from chunk 01's review (warning R-1): `_READ_ONLY_ANNOTATIONS`' keys are also copied
  from `mcp_types`, and the constants test compares them against `ToolAnnotations`' wire names.
- Each guard seen red with its mechanism removed.
- Artifacts: `api-contract.md` § Hard errors; the envelope reference resource if it describes
  refusals; `docs/connecting-an-mcp-client.md` if it does.

**Done when:** gate green; each guard seen red; Critic chunk review has no blocking findings; a
cumulative review spans the branch.
