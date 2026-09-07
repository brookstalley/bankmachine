---
artifact: observability-strategy
version: 1
depends_on:
  - artifact: system-requirements
    file_path: docs/system-requirements.md
  - artifact: nonfunctional-requirements
    file_path: .prawduct/artifacts/nonfunctional-requirements.md
  - artifact: api-contract
    file_path: .prawduct/artifacts/api-contract.md
last_validated: null
---

# Observability Strategy — bankmachine

**Scope:** how this product tells anyone — operator or analyst agent — that its data is not to be
trusted, and how a failure is diagnosed after the fact.

**Dependency note.** The template's usual product-brief upstream does not exist as a separate
artifact; §0 of `docs/system-requirements.md` carries that content.

**No `## Direction` section, and that is a recorded outcome rather than an omission.** The norm this
document rests on — *incompleteness rides the success path as a warning field, never only as an
exception* — is ratified in `api-contract.md` § Direction, because it is a property of the response
contract and belongs in one home. Everything else here is description, which tracks rather than
binds.

---

## The premise this whole document rests on

🔴 **This product's primary failure mode is not a crash. It is a confidently wrong answer.**

AC-4.4 states it: *silent staleness is the primary failure mode of this entire system* — a connection
that quietly stopped three weeks ago produces plausible, well-formatted, wrong analysis. Nothing
throws. Nothing pages. The numbers just quietly stop being true.

Two consequences run through every section below:

1. 🔴 **The observability surface and the product surface are deliberately the same thing.** The
   primary operational signal is not the log; it is `get_pipeline_health`, a **product tool** queried
   by the analyst agent. The consumer that most needs to know the data is stale is the one asking
   questions of it. Most products bolt agent-accessible observability on; here it is the requirement.
2. 🔴 **Incompleteness travels on the success path.** A warning field on a 200-equivalent response,
   not an exception (`api-contract.md` § Error Model). Anything that only speaks through the error
   channel is invisible to the failure this product actually has.

**Proportionality note.** By the template's scale this is a low-risk product — single user, single
machine, no uptime obligation — and most of it *is* handled in a sentence: no metrics backend, no
tracing, no alerting infrastructure, no dashboards. The sections that get real depth are health
signals and sensitive-data filtering, because that is where this product's actual risk lives. That
asymmetry is the design, not an omission.

---

## What You Get

The contract. If these scenarios do not work, this strategy has failed.

**1. A connection broke three weeks ago and nobody noticed.**
→ `get_pipeline_health` reports that connection `degraded`, with its error code **and its last
successful sync timestamp** — so the size of the hole is *computable, not guessed* (AC-4.5). Any
aggregate whose window overlaps the hole carries a `degraded` or `gapped` warning. 🔴 **The analyst
agent cannot answer over that window without receiving the warning in the payload** — it cannot see a
caveat that is not in the response.

**2. Last night's sync did not run.**
→ `store status` / `get_pipeline_health` show `last_success_at` older than the daily cadence and flag
`stale`. The launchd job's own stdout/stderr land in the configured log directory. 🔴 The **CLI exit
code distinguishes "ran and found a problem" (1) from "could not run" (2)** — so a broken scheduler is
never mistaken for a degraded feed.

**3. A number looks wrong and the operator wants to know where it came from.**
→ `sync shell` — an authenticated SQLCipher REPL — reaches the datastore directly. Every silver row
carries `derivation_version_id` and a provenance pair (`raw_response_id` **or** `manual_import_id`), so
any row traces back to the verbatim response or the imported file that produced it. `store rebuild`
re-derives from bronze to test whether the fault is in derivation or in the source.

**4. A sync run failed partway through and the operator needs to know what it got.**
→ Per-run structured logs with a correlation id: per-connection outcome, rows added / modified /
removed, cursor advancement, duration. 🔴 A crash mid-sync **does not advance the cursor**, so the log
and the datastore agree about where it stopped.

**5. The wrong environment was used.**
→ A loud banner at **every** startup naming sandbox or production (AC-10.6) — not inferred from which
credentials happen to be present, which is silent and easy to get wrong.

Each of these is a runbook trigger; none has a runbook written yet. See "Verification".

---

## Architecture

```
  sync run ──┬──> stderr  (redacting formatter, always)
             └──> $LOG_DIR/*.log  (redacting formatter, when the dir is usable)
                    │
                    └── read by: the operator, `tail`, and any agent with file access

  sync run ──> datastore: sync_state, connections.status/last_success_at/last_error_code
                    │
                    ├──> `store status`        (operator, exit code 0/1)
                    ├──> `sync shell`          (operator, ad-hoc SQL)
                    └──> `get_pipeline_health` (analyst agent)  ← the primary signal
                             │
                             └──> warning fields on EVERY tool response
```

🔴 **There is no collector, no exporter, no backend, and there will not be one.** AC-10.4 forbids
telemetry, analytics, and third-party error reporting outright; the only network destinations are the
aggregator's API hosts. This is not a cost decision that could be revisited if a free tier appeared —
it is a confidentiality requirement, and it is what makes the health-state-in-the-datastore design the
*right* answer rather than a budget one.

**The durable signal is the datastore, not the log file.** Logs are for diagnosis after the fact;
`sync_state` and `connections` are the health record, they are queryable, and they survive log
rotation. A log-only health story would be unreadable by the consumer that most needs it.

---

## Signal Types

### Logging

**Format:** structured, with per-run correlation. **Destinations:** stderr always; a file in the
configured log directory when that directory is usable. **Level:** `INFO` by default, `DEBUG` under
`--verbose`.

Logged per sync run: run correlation id, environment (sandbox/production), per-connection outcome,
rows added / modified / removed, cursor advancement, duration, and every error with its code.

🔴 **An unusable log directory does not take the process down.** The run continues on stderr. A
pipeline that refuses to sync because it cannot open a log file has converted an observability problem
into a data problem — and this product's whole thesis is that missing data is the expensive failure.
*(Tested: `tests/test_logging_setup.py`.)*

**Retention:** none automated. Local files, trivial volume, operator's to prune.

### Metrics

🔴 **No metrics system, and none is wanted.** There is no time-series backend to send to, and the
numbers that matter — rows per run, coverage per account, gap sizes — are *already in the datastore*,
queryable in SQL with full history. A metrics pipeline would be a second, lossier copy of data the
product is already the system of record for.

The counts that would be metrics elsewhere live in `sync_state`, `connections`, and the coverage
report.

### Tracing

**Not applicable.** Two processes with no lifecycle relationship, no request fan-out, no distributed
call graph. The per-run correlation id covers the one real question — *which run did this?* — and
`raw_responses` gives per-call lineage where a trace span would otherwise be the answer.

---

## Instrumentation Layers

Layers 1 and 3 carry essentially all of it, which is the intended distribution.

1. **Automatic** — Python's `logging`, with the redacting formatter attached at configuration time so
   it applies to every record regardless of which module emitted it. Nothing has to remember.
2. **Declarative** — none. Not enough surface to justify decorators.
3. **Contextual** — 🔴 **where the real work is.** The run correlation id, connection identity, domain,
   and environment enrich every record. And the *durable* contextual instrumentation is schema-level:
   `derivation_version_id`, `raw_response_id` / `manual_import_id`, `first_seen_at`, `updated_at`,
   `removed_at` on every silver row. Provenance is instrumentation that outlives the process.
4. **Manual** — the sync run summary and the health computation. Deliberately few.

---

## Correlation Context

| Scope | Id | Propagation |
|---|---|---|
| **Run-scoped** | A per-sync-run correlation id | A log field on every record for the run; surfaced through `get_pipeline_health` so a health report traces to the run that produced it |
| **Connection-scoped** | `connection_id` | On every log record touching that connection, and the key of `sync_state` and `connections` |
| **Domain-scoped** | `domain` (`transactions` \| `balances` \| `investments`) | Paired with `connection_id`; domains advance independently and fail independently |
| **Row-scoped** | `raw_response_id` / `manual_import_id` / `derivation_version_id` | Columns on the row itself — permanent, not a log retention question |
| Session-scoped | — | Not applicable: one operator, no sessions |

🔴 **Connection *and* domain, not connection alone.** A connection can be healthy for transactions and
failing for investments; a correlation scheme that stopped at the connection would report it simply
"degraded" and lose which class of data actually stopped arriving.

---

## Sensitive Data Filtering

The section that gets full weight, because every signal here is derived from financial data.

**Approach: structural + blocklist + entropy, applied at the formatter** — so it covers every record,
including ones written by third-party libraries and by exception messages that were never intended to
be logged.

| Rule | Catches |
|---|---|
| **Named-key blocklist** | `access_token`, `refresh_token`, `client_secret`, `client_id`, `api_key`, `secret`, `password`, `passwd`, `token`, `key` — whatever the value's shape |
| **Opaque high-entropy strings** | Tokens, hex keys, base64 blobs — whatever the label, or none |
| **Structural** | The secrets module never returns a secret into a log line, an exception message, or a `repr`. *A `KeyError` naming the account is fine; the value never is* |
| **Account numbers** | Only the last four survive. Masks are acceptable (AC-10.3) and deliberately preserved — a fully redacted number would make logs useless to the operator |

🔴 **The rules over-redact, and that is the chosen direction.** A filesystem path containing a
32-character segment is blanked along with the tokens, costing some legibility. The alternative —
requiring high entropy before redacting — trades that back for the chance of a real token slipping
through, and that trade is refused.

🔴 **The rules are provider-agnostic on purpose.** A redaction rule keyed to one aggregator's token
prefix would **silently stop redacting** the day a second aggregator is added — a failure with no
symptom until the leak.

**What is deliberately *not* redacted:** amounts, merchant names, and institution names in the local
log file. They are the content being debugged, the file never leaves the machine, and the directory is
gitignored. 🔴 The boundary that matters is the **push**, not the log — enforced by the fail-closed
pre-push guard (`security-model.md`).

*Tested:* token-shaped values, labelled credentials whatever their shape, exception messages, account
truncation, and a negative control that ordinary prose survives.

---

## Alerting

🔴 **No automated alerting, and this is a deliberate, examined decision rather than a gap.**

There is no channel that would not violate AC-10.4 (no third-party destinations), no on-call, and no
urgency — a stale feed is discovered and repaired at human pace, and nothing degrades further while it
waits.

**What replaces it — and why it is stronger here than a notification would be:** the alert fires *at
the moment of use*, in the payload. An operator ignores an email about a broken connection; an analyst
agent **cannot ignore a `degraded` warning field in the response it is reasoning over**, because it
has no way to render an answer that omits it without discarding data it was handed. Push notification
alerts the operator when they are not asking; in-band warnings alert whoever is asking, when they ask.

🔴 **This is the one design choice most worth re-examining if the product's use pattern changes.** It
holds precisely because every consumption path goes through a surface that carries warnings. A future
path that reads the datastore *without* going through the tools — a dashboard, an export, a second
agent querying SQL directly — would bypass the entire alerting story. **That is the trigger to revisit
this section**, and it is stated here so the revisit is prompted rather than remembered.

---

## Health Signals

**Healthy** means all of:

- Every active connection's `last_success_at` is within the expected daily cadence.
- No active connection has `status = 'degraded'`.
- Every account's coverage is continuous from enrollment back to its granted history window, with
  gaps > 7 days enumerated and explained (AC-11.1).
- The datastore exists, opens with the keychain key, and reports a **recognized schema version**.

**Degraded states — each visible, each distinct:**

| State | Signal | Meaning |
|---|---|---|
| `stale` | `last_success_at` older than cadence | The sync has not run, or ran and failed |
| `degraded` | `connections.status`, `last_error_code` | This connection is broken; 🔴 the others are unaffected (AC-4.1) |
| `gapped` | Coverage report, gaps > 7 days | A known hole in the window |
| `partial` | `granted_history_days` < `requested_history_days` | Bounded history — 🔴 recorded as a known gap, never treated as complete (AC-11.8) |
| `rule-applied` | Aggregate response field | An account rule filtered rows (AC-8.3) |
| **rule anomaly** | `get_pipeline_health` | An inbound credit to a `contribution_only` account from **outside** its configured contributor set (AC-8.4) — 🔴 surfaced, never silently classified |
| **empty / missing datastore** | `get_pipeline_health` | AC-ARCH.3 — the server **starts and reports this** rather than crashing |
| **schema unrecognized** | Hard error from health | Refuses to serve rather than returning plausible wrong answers |

🔴 **A degraded record without `last_success_at` is insufficient** (AC-4.5) — a flag without a date
makes the size of the data hole unguessable. This was observed live in the incumbent system, where
two connections had been dead for an unknown duration. It is a hard requirement, and the database
enforces the flag/date pairing.

🔴 **Health is reported per account, never per institution** (AC-9.5). One institution may hold many
accounts with different coverage windows, and an institution-level roll-up hides exactly the account
that stopped.

---

## Infrastructure and Deployment

**None.** No observability infrastructure exists, is deployed, or is planned.

- **Technology:** Python's standard `logging`, a custom redacting formatter, and SQL over the product's
  own datastore.
- **Deployment model:** always-on and zero-configuration. Logging configures itself at CLI startup;
  the health record is written by the sync path as ordinary data.
- **Cost:** $0, structurally — see `nonfunctional-requirements.md`.
- **Zero-cost when unconfigured:** yes. An unusable or unset log directory degrades to stderr rather
  than failing.

---

## Agent-Accessible Observability

🔴 **In most products this is a bolt-on. Here it is the product.**

The analyst agent is a first-class observability consumer, not an afterthought — and its debugging loop
is the *user's* loop: run a query → suspect the answer → investigate freshness and coverage → decide
whether to answer at all.

| Agent need | Surface |
|---|---|
| Is the data fresh? | `get_pipeline_health`; freshness stamp on **every** response |
| Is it complete for *this* window? | `get_coverage_report` — per account, gaps enumerated |
| Was anything filtered out? | `rule-applied` warning; rule anomalies in health |
| Which run produced this state? | Run correlation id, surfaced through health |
| Can I trust this answer at all? | 🔴 The warning fields. **Present or absent, they are always answered** |

**The design rule that makes it work:** the agent **cannot see a caveat that is not in the response
payload.** Documentation, log files, and a health tool it did not think to call are all invisible at
the moment of answering. That is why warnings ride on every response rather than living in one
diagnostic tool — and why AC-9.3 makes analysis over incomplete data *impossible to do accidentally*
rather than merely inadvisable.

**For the human operator**, the same signals reach `store status` (exit-code-bearing, so launchd can
read it) and `sync shell` (arbitrary SQL over the real datastore, on a read-only handle). Duplication
between the agent and operator paths is accepted: they have different access patterns and both need to
be self-sufficient.

---

## Verification

How to confirm this strategy actually works.

**Covered by tests today:**

| Property | Evidence |
|---|---|
| Secrets never reach a log line | `tests/test_logging_setup.py` — token shapes, labelled credentials, exception messages, account truncation, and a prose negative control |
| The environment banner is loud in both environments | `tests/test_logging_setup.py` (AC-10.6) |
| An unusable log directory does not take the process down | `tests/test_logging_setup.py` |
| Logs go to the configured directory | `tests/test_logging_setup.py` |
| `store status` reports an unrecognized schema version | `tests/store/test_connection_norms.py` |
| A reader refuses to serve on schema mismatch | `tests/store/test_connection_norms.py` |

**Owed — recorded so it is filed rather than rediscovered.** All of it depends on surfaces that do not
exist yet (sync is step 4, health is step 6, MCP is step 7); none is a gap in shipped code:

- Every "What You Get" scenario reproduced end to end. 🔴 Scenario 1 is the one that matters: **break a
  connection, let the clock advance, and confirm an aggregate over that window carries the warning.**
- A degraded connection never lacks `last_success_at` once it has succeeded once (AC-4.5).
- Warning fields appear on **every** tool response path, not only the ones a developer remembered —
  this wants a structural test over the tool registry, not per-tool assertions.
- Per-run correlation ids appear on every record for a run and reach `get_pipeline_health`.
- A rule anomaly (AC-8.4) surfaces rather than being silently classified.
- **No runbooks are written yet.** Each "What You Get" scenario is a trigger; authoring them is
  `/prawduct:runbook`'s job once the surfaces they describe exist.
