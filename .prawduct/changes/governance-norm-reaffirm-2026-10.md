---
schema: 1
type: governance
scope: governance-norm-reaffirm-2026-10
date: 2026-10-08
title: Three api-contract norms re-affirmed past the work that birthed them
---
**Why:** the `dead-why` advisory (`norm-lifecycle-dead-why-v1-fcdc48`) flagged four citations in
`api-contract.md`'s Direction norms whose backlog items are closed: #40, #84, #86 and
`brookstalley/bankmachine#30`. The owner ruled to keep all three norms.

**What shipped:**

- Each affected entry carries a `Re-affirmed:` field naming its ids and the argument that holds the
  norm without them. Every citation is provenance or a worked example, not a premise, so all are
  kept: dropping them would make the norm's history untraceable. No cleanup item is owed.
- Verified with `norm_probes.probe_dead_why`, which fires all four pairs on the unedited tree and is
  silent on the edited one.

## Consumer note
none: migrated from the legacy change log, which held no consumer note; replace this line with the note if consumers need one
