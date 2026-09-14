---
artifact: build-plan
version: 2
scope: pre-production-fixes
branch: feature/pre-production-fixes
depends_on:
  - artifact: api-contract
  - artifact: security-model
  - artifact: data-model
governed_by:
  - artifact: api-contract
    dispositions:
      - "the CLI's three-way exit code is a contract → conforms; the tuning-flag refusal still exits 2, which is what argparse's refusal exited. Only the route changes: `run()` now returns it instead of `SystemExit` escaping past the handlers that log"
      - "the MCP surface is read-only → conforms; no tool gains a mutation and no tool is added"
      - "every response carries a freshness stamp, and incompleteness rides the success path as a warning → inapplicable because no answer's shape or warning set changes; `last_error_code` keeps its type and nullability and only its values become what the contract said they were"
      - "a tool's boundary is drawn where the answer shape changes → inapplicable because no tool is added and no answer shape changes"
      - "a stored balance is reported with its lifecycle → inapplicable because no balance path is touched"
  - artifact: security-model
    dispositions:
      - "log redaction happens at the formatter and over-redacts by design → conforms, and #90 is this norm held rather than narrowed: the audit sentence is reworded so the redactor has nothing to scrub, and `_SENSITIVE_KEY` is left exactly as it is. The new usage-error arm in `run()` prints through `redact`, the same scrub `RedactingParser.error` applies, so the refusal route this plan adds is not a surface the norm has not reached"
      - "secrets live only in the OS keychain, and nothing returns one into a log line or exception → conforms; no new log line interpolates a value an operator typed, and the rounding summary carries amounts, currencies and a response id, none credential-shaped"
      - "no tracked file carries a credential-shaped string → inapplicable because no fixture or data file is added"
      - "the aggregator's API is the only network destination → inapplicable because no call is added"
  - artifact: data-model
    dispositions:
      - "a migration's DDL is frozen once written → conforms; #102 edits migration 007's module docstring, not its DDL, and removes a read-path claim that has no business in a migration"
      - "all monetary values are stored as integer minor units → conforms; #111 changes how a rounding is REPORTED and nothing about the value stored. Half-even, the currency's own exponent and the refusal of floats are untouched, so `DERIVATION_VERSION` does not move"
      - "every stored amount is signed from the operator's point of view → inapplicable because no amount is derived differently"
      - "calendar dates and UTC instants never mix → inapplicable because no date or instant is written differently"
      - "the daily balance and holdings series are append-only → inapplicable because neither series is touched"
      - "every silver row carries its evidence → inapplicable because no silver row is written differently"
      - "a source value is never overwritten in place → conforms; `last_error_code` is operational state on `connections` and `sync_state`, not a source value, and it is written by the same statements as before"
      - "a transaction is never hard-deleted → inapplicable because no transaction path is touched"
partition: serial — #93 and #103 both change `cli/sync_run.py` and `tests/cli/test_sync_run.py`, and the other three are small enough that briefing a delegate costs more than the change
last_validated: 2026-09-14
---

# Build Plan: Five Fixes Before the First Production Connection

Closes brookstalley/bankmachine#90, #93, #102, #103 and #111.

## Why these, and why now

The owner's ruling (2026-09-14): **the first production connection does not happen with these
open.** Each one makes the durable record of an unattended run say something untrue, or bury what it
says. On a sandbox store that costs a confused afternoon. Against real accounts it is the record
someone reads to find out what happened to their money.

## Confidence check

1. **Problem.** Every issue below has a written repro, and each ends in a log, a stored column or a
   docstring that misstates what happened.
2. **Success.** Each issue's Expected holds, as the decisions below settle it, with a test that goes
   red on the old code.
3. **Out of scope.** #113 and #10, which are pre-production too but need a design pass and a
   requirement respectively; #60's MCP error-code guard, which is a different vocabulary; the
   sandbox store's already-recorded class-name codes (see #93).

## Decisions (owner, 2026-09-14)

### [DECISION] #93 — `last_error_code` is the aggregator's code where it sent one, otherwise one of bankmachine's own

Rejected: aggregator's code with a Python class name as the fallback (it leaks implementation
detail onto the MCP surface and changes under a rename), and two columns (a schema-13 migration and
a rebuild for a small fix).

- **Verbatim wherever the aggregator sent one.** An `AggregatorError` whose `error_code` is set
  records it unchanged. That is what `api-contract.md` already publishes and what
  `AggregatorError.error_code`'s docstring already promises.
- **A closed set of bankmachine codes everywhere else**, resolved from the exception's class by
  walking its MRO, so a subclass nobody mapped still resolves to its nearest mapped ancestor rather
  than to nothing. Each code is named for what the operator must do, following
  `connector/__init__.py`'s taxonomy:

  | Code | Raised by |
  |---|---|
  | `CREDENTIAL_UNREADABLE` | the connection's access token could not be read (`SecretsError`); already recorded under this name today |
  | `AGGREGATOR_UNREACHABLE` | `TransportError` — the aggregator did not answer |
  | `AGGREGATOR_NOT_CONFIGURED` | `AggregatorNotConfiguredError` |
  | `AGGREGATOR_REFUSED_WITHOUT_CODE` | an `AggregatorError` whose body carried no `error_code` |
  | `RESPONSE_UNUSABLE` | `MalformedResponseError`, `CredentialBearingResponseError`, `UnreadableWindowError`, and any other `ConnectorError` |
  | `DATASTORE_LOCKED` | `AnotherWriterRunningError` — typically a `store backup` holding the writer lock |
  | `DERIVATION_FAILED` | `DerivationError` (including `UndenominableAmountError`) and `UnknownEndpointError` |
  | `DATASTORE_FAILED` | any other `StoreError` |

- **One resolver, four call sites.** The connection-row degrade (credential, login, connector and
  store arms) and the investments domain failure all call it; no site builds a code of its own.
- **No migration and no backfill.** Only the sandbox store holds class-name codes. The next
  successful run clears them and `connections reauth` clears them, and there is no production store
  to migrate (Common Traps: unnecessary backwards compatibility).
- **[ASSUMPTION] The aggregator will never issue one of these eight names.** Its codes are
  product-scoped (`ITEM_LOGIN_REQUIRED`, `PRODUCT_NOT_READY`) and none begins `DATASTORE_`,
  `DERIVATION_` or `RESPONSE_`. A test holds the set disjoint from every code `errors.py` knows
  today. It cannot hold it against codes the aggregator adds later.

**Boundary.** `last_error_code` is published by `get_pipeline_health` at connection and domain
scope, printed by `connections list`, and its values are pinned by the tests listed under Chunk 01.
The published type is unchanged (`string | null`); `api-contract.md` already tells consumers to treat
an unrecognised value as a value. What changes is the contract's sentence: it currently says the
field is only ever the aggregator's vocabulary, and after this it names the bankmachine set too.

### [DECISION] #103 — a `UsageError` that `run()` logs, and `FILE_ONLY` made public

- `UsageError` lives in `cli/parser.py` beside `RedactingParser`, the other home of "the command line
  was wrong". `run()` catches it: prints `bankmachine: <message>` through `redact`, logs a
  `command … refused` warning with `FILE_ONLY`, and returns `EXIT_ERROR`.
- `_FILE_ONLY` moves to `logging_setup.py` as `FILE_ONLY`, beside `_NotAlreadyOnStderr`, the filter
  that reads it. `cli/__init__.py` and `sync_run.py` import it from there.
- **Three narration sites, not the two the issue names.** The retry wait (logged, then printed to
  stdout) and the give-up message (logged at WARNING, then printed to stderr) both reach the terminal
  twice today. Both get `FILE_ONLY`. `finished at attempt N` has no printed twin and stays on both
  handlers.

### [DECISION] #111 — one grouped disclosure per response

- `to_minor` stops logging per row. It records `(what, original, currency, rounded)` into a tally,
  and the tally is flushed once per response after the deriver returns, one INFO line per distinct
  group, in first-seen order:
  `raw response 7: an investment transaction price of 94.808 USD rounded to 94.81 for storage, 31 times (a valuation, not a ledger amount; the archive keeps the original)`.
- **Scoped by wrapping each entry of `PLAID_DERIVERS`**, so the sync, `connections reauth`, enrollment
  and `store rebuild` all get it through the one registry. `to_minor` gains no parameter (learning:
  a defaulted parameter on a deriver is a trap).
- **Flushed only when the deriver returns.** A deriver that raises rolls its transaction back, so
  nothing was rounded "for storage", and the failure has its own log line in `apply_response`.
- **Outside a wrapped deriver, `to_minor` still discloses**, as a group of one logged immediately.
  Only tests call it that way today, and no path ever rounds silently.

### #90 and #102 — no decision needed

- **#90** was built on `fix/key-escrow-audit-redaction` (`7390b0b`) and never merged. It merges
  cleanly onto this branch, 44 commits later, and is merged rather than re-done.
- **#102** removes the read-path claim from migration 007's docstring and points at `store/lineage.py`.
  The natural-key rejection stays: that is write-path reasoning and belongs in the migration.

## Chunk 01 — The five fixes

**Files.** `src/bankmachine/cli/store.py` and its test (via the #90 merge);
`src/bankmachine/store/migrations/transaction_lineage.py`; `src/bankmachine/connector/plaid/derivers.py`;
`src/bankmachine/logging_setup.py`; `src/bankmachine/cli/__init__.py`; `src/bankmachine/cli/parser.py`;
`src/bankmachine/cli/sync_run.py`; a new `src/bankmachine/cli/failure_codes.py`; tests under
`tests/cli/`, `tests/connector/` and `tests/preferences/`; `.prawduct/artifacts/api-contract.md`;
`docs/system-requirements.md` (AC-4.2); `.prawduct/change-log.md`.

**Tests.**
1. **#90** — the merged `test_every_escrow_record_survives_redaction_word_for_word` passes on this branch.
2. **#103 refusal** — `sync run --max-attempts 20` returns 2, prints the message to stderr, makes no
   call, and leaves a WARNING `refused` line naming the message in the log file.
3. **#103 narration** — under `--until-ready`, the wait and give-up sentences each appear exactly once
   across stdout and stderr together, and are still in the log file.
4. **#111** — one response carrying N rows of one price yields exactly one disclosure naming N, the
   value, the currency, the result and the response id. Two distinct values yield two. A raising
   deriver yields none. The existing "rounded to" tests keep passing unchanged.
5. **#93 verbatim** — a refusal carrying `ITEM_LOGIN_REQUIRED` records exactly that on the connection
   row; one carrying a code on the investments domain records it on the domain row.
6. **#93 local** — every concrete subclass of `ConnectorError`, `StoreError` and `SecretsError` found
   under `src/` resolves to a member of the closed set, driven from the hierarchy rather than a list.
   The set equals the table published in `api-contract.md`, and it is disjoint from every code
   `errors.py` knows.
7. **#93 re-pinned** — the tests that pin class names (`test_sync_run.py`, and the seed data in
   `test_mcp.py` / `test_list_holdings.py`) change to the codes the decision specifies. **This is the
   requirement changing, not a test weakening**: each assertion stays exact, and #93 itself names
   `test_sync_run.py`'s class-name pin as a contract to change with the requirement.

**Done when.** All of the above pass, the full suite is green, a sandbox `sync run --no-wait` shows
the rounding disclosure grouped (stderr line count recorded against #111's 157), and
`/prawduct:critic` has run with its findings dispositioned.

**Owed on this branch before its PR (carried from PR #119).** VRF-028 (#22) and VRF-029 (#23) are
raised again as fresh pending entries. Both need production data, so this branch's PR will be
blocked by them unless they are accepted again.

## Status

- [x] Chunk 01 — The five fixes

## Context

Branched from `develop` at `4d28caa`. Baseline suite green on that tree (`test-status`).

Built and committed (`61d0c1e` merges #90's branch; `416a519` carries the other four). Every new test
was run against the pre-change `sync_run.py` and `derivers.py` and failed there.

Two findings from building, recorded here rather than silently absorbed:
1. **Nine class-name pins, not the eight the plan counted.** The ninth is the domain warning's
   `detail`, which quotes `last_error_code`. So the code reaches the MCP warning text as well as the
   field, and the re-pin covers both.
2. **#93 was exercised on the live sandbox store by accident.** A verification `sync run` from a
   shell without the aggregator client id degraded Tartan Bank. `get_pipeline_health` now reports
   `last_error_code: AGGREGATOR_NOT_CONFIGURED` and a `degraded` warning naming that code, which is
   the ruling holding end to end. `last_success_at` is untouched, and the next successful run
   restores the connection.

**"Done when", verified.** Full gated suite green (`test-status`). Critic
`rev-20260914T172751Z-b6d37bcb`: 0 blocking. R-2's stale `login_expired` comment was fixed in
`15f744b`. R-1 and R-3 were accepted, R-1 on the measurement below. **#111 measured on the sandbox**
by the owner at `15f744b`: 16 grouped rounding lines where the issue recorded ~150, and 17 stderr
lines in all against 157. The Critic's concern was real: the repeats span responses 109-111, each
carrying both repeated prices. Per-response grouping still cuts the volume about ninefold, and the
run's own report is readable again. The same run restored the connection the earlier verification
attempt had degraded.

`verify-resolutions` over `15f744b` came back clean, with R-2 confirmed. One observation was demoted
rather than fixed: the new `login_expired` comment calls the recorded code "the aggregator's
vocabulary", and it can also be one of the local codes. It is inert prose, so it is fixed with the
next change that touches `sync_run.py` rather than in a review round of its own.
