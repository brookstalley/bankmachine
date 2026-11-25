---
schema: 1
type: governance
scope: prawduct-advisories-2026-11
date: 2026-11-25
title: Governance catches up with prawduct v3.9
---
**Why:** after the plugin moved to v3.9, the session briefing raised advisories this repo had not
acted on: releases now read only change fragments, four session files were not ignored, a clone
without the plugin would run ungoverned with no warning, and #87's in-transition norm had gone
76 days without a recorded bound.

**What shipped:**

- Changes are recorded one file per scope under `.prawduct/changes/`. The two release-pending
  change-log entries became fragments; shipped history stays in the log. The `merge=union` line on
  `change-log.md` is gone with the `.gitattributes` it was the only line of, because no branch
  prepends to that file any more.
- `.gitignore` covers prawduct's four new session files.
- `.claude/settings.json` carries the unloaded-plugin tripwire: a SessionStart hook that prints a
  warning when this repo has no prawduct install, and nothing when it does.
- `api-contract.md`'s agent-write norm carries a `Stopgap:` expiring 2027-02-25. #87 is paused by
  its own sequencing (after production onboarding and #48), not stalled; the read-only interim
  rule is unchanged.
- `.prawduct/owner-rulings.md` records the owner's three decisions from this work.

## Consumer note
none: governance bookkeeping only; nothing an operator or MCP client sees changes.
