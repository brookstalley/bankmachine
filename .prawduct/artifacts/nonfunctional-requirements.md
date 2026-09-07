---
artifact: nonfunctional-requirements
version: 1
depends_on:
  - artifact: system-requirements
    file_path: docs/system-requirements.md
last_validated: null
---

# Non-Functional Requirements — bankmachine

**Scope:** the performance, scale, availability, and cost envelope this product is built inside, and
what would force a redesign if it moved.

**Dependency note.** The template's usual upstream is a product brief, which does not exist as a
separate artifact here; §0 of `docs/system-requirements.md` and `product_definition` in
`.prawduct/project-state.yaml` carry that content.

**No `## Direction` section, and that is a recorded outcome.** Nothing here binds: these are targets
and limits, which change when the product's situation changes. The row cap that *does* bind is a
contract term and is ratified in `api-contract.md` § Direction.

---

## The calibration, stated first

🔴 **Scale is not a design driver here. Correctness is.**

One operator, one machine, roughly ten institutions, tens of thousands of transactions over
twenty-four months. Nothing in this document is about throughput under load, and a reader who finds
themselves specifying one should stop.

What *is* demanding is the failure mode. `docs/system-requirements.md` AC-4.4 names it: **silent
staleness** — a connection that quietly stopped three weeks ago produces confidently wrong analysis
rather than an error. So the non-functional budget this product actually spends is on **detectability
and loudness**, not on capacity. Where a conventional NFR document would put a latency SLO, this one
puts a requirement that the system be *unable* to answer without saying how fresh the answer is.

---

## Performance

| Target | Value | Why this number |
|---|---|---|
| MCP aggregate tool response | **under ~1s** over 24 months of data | The consumer is an interactive analyst agent; a multi-second tool call makes multi-step analysis unusable |
| Raw-row tool response | same, and **hard-capped at ~500 rows** (AC-9.1) | The cap is what keeps the target reachable, and it is a contract detail, not a tuning knob |
| Daily sync run | no target | Unattended and scheduled. It may take hours during initial backfill; nothing waits on it |
| Initial historical backfill | no target; **must tolerate not-yet-ready** (AC-2.6) | A multi-year backfill takes time to materialize; backoff-and-retry rather than failure |

**How the ~1s target is met, and the decision it justifies.** By doing the arithmetic **in SQL over
indexed columns** — `transactions_by_account_date` is the shape every spending, cashflow, and coverage
query reads in. This is not an implementation detail: it is the reason the datastore is **page-encrypted
rather than field-encrypted.** Application-level AES on sensitive columns would destroy exactly the
queryability this target depends on — no range scans on date, no `GROUP BY` category, no `SUM` in SQL,
no useful indexes. The performance requirement and the encryption choice are the same decision seen
from two sides; see `security-model.md`.

**The corollary requirement (AC-9.1).** Tools return **computed summaries by default, not raw rows.**
Dumping 24 months of transactions into an LLM context is slow, expensive, and *worse at arithmetic
than SQL is*. This is a performance requirement about the consumer, not about the server.

**Not measured yet.** These targets are stated, not verified — the MCP surface is build step 7 and
does not exist. The number to beat is recorded now so the tool layer is built against it rather than
measured after the fact.

---

## Scalability

| Dimension | Expected | Binding limit |
|---|---|---|
| Operators | **1** | Single user by design; multi-user is a `never` |
| Machines | **1** | Multi-machine is `later`, not v1 |
| Institutions | ~10 | 🔴 **Hard cap of 10 connections** — the aggregator's plan tier |
| Accounts | tens | none |
| Transactions | tens of thousands over 24 months | none at this order of magnitude |
| History window | **730 days requested** (AC-1.2) | Vendor maximum; **cannot be raised after enrollment** |

🔴 **The connection cap is configuration, not a literal** (AC-1.5). Plan tiers change. The enrollment
path must refuse to exceed the *configured* cap, explain the limit, and list current connections so
one can be removed.

🔴 **The history window is the highest-stakes parameter in the system.** It cannot be raised after
enrollment without removing and re-linking the connection, so a build that ships with the vendor
default has failed. It is configuration with a documented maximum, and what was actually *granted* is
stored separately from what was *requested* so a shortfall is visible rather than silent
(`data-model.md`, `connections`).

**What growth would force a redesign.** Nothing on this list is close, and each is recorded so a
future reader can tell a real limit from an unexamined assumption:

- **A second operator** — would require multi-party trust boundaries and data isolation that no part
  of this system has. It is a `never`, not a deferral.
- **A second machine** — the datastore is a single local file reached by two processes over `flock`;
  that mechanism does not cross a network filesystem. Multi-machine is a redesign of the concurrency
  model, not a deployment change.
- **Transaction volume two orders of magnitude higher** — would make the aggregate-in-SQL approach
  worth re-measuring. At the stated scale it is not close.
- **Multi-currency reporting** — recorded as out of scope in `data-model.md`; adding FX is a new table
  and a derivation version.

---

## Availability

**The sync is a scheduled job, not a service.** There is no uptime target, because there is nothing
listening. Two availability requirements matter, and both are about *behaviour at the edges* rather
than a percentage:

🔴 **AC-ARCH.2 — a missed window is recovered, never skipped.** A laptop asleep at the scheduled time
is the normal case, not the exception. Skipping is silent data staleness, which is the primary failure
mode. The scheduler must run on next wake.

🔴 **AC-ARCH.3 — the MCP server starts and answers when the datastore is empty or missing,** reporting
that state through `get_pipeline_health` rather than crashing. A reader that dies on an absent
datastore cannot *report* an absent datastore, and the report is the point.

**Independence (AC-ARCH.7).** The sync writer and the MCP reader have no lifecycle relationship at
all — the datastore file is their only contact. The MCP server must not depend on sync liveness, nor
the reverse. A socket or signal between them would create exactly the dependency this forbids.

**Degraded is a first-class state, not an outage.** One broken connection never aborts the sync for
the others (AC-4.1). The system continues, records the failure with its error code **and the timestamp
of last successful sync** (AC-4.2, AC-4.5), and surfaces it. Partial availability that reports itself
accurately is the designed behaviour.

**Recovery is the operator's, and it is not urgent.** There is no on-call, no alerting channel, and no
escalation path — see `operational-spec.md`. A degraded connection waits for the operator to notice it
through the health surface, which is why that surface has to be unmissable rather than quiet.

---

## Cost Constraints

| Item | Cost | Note |
|---|---|---|
| Aggregator plan | **$0** within Trial limits | 🔴 The **10-connection cap is the binding constraint, not price** |
| Local compute and storage | **$0** | Runs on an existing machine |
| Hosting | **$0** | Nothing is hosted. There is no server to pay for |
| Telemetry / monitoring services | **$0, and structurally so** | AC-10.4 forbids third-party destinations entirely |

**Storage.** `raw_responses` holds every response verbatim as compressed JSON, forever by default.
Volume is trivial at this scale, and it is the system of record for the rebuild guarantee (AC-5.2) —
so the bronze layer's disk cost is deliberately not optimized. *(Retention beyond "keep indefinitely"
is `docs/system-requirements.md` §9 open question 1; the recommendation there is to keep.)*

**Vendor terms are not pinned.** Plan limits and pricing are vendor-set and may have moved since they
were recorded on 2026-09-05. Confirm current terms before enrolling — this is a cost constraint that
changes without notice from outside the repository.

**The cost constraint that shapes the architecture** is not money. It is that **no financial data may
leave the machine except as an encrypted backup**, which rules out every hosted service that would
otherwise be the cheap answer to monitoring, alerting, and log aggregation. See
`observability-strategy.md`, where that constraint does most of the design work.
