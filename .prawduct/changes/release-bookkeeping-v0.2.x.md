---
schema: 1
type: chore
scope: release-bookkeeping-v0.2.x
date: 2026-09-21
title: The change log says which release shipped what
---
**Why:** v0.2.0 through v0.2.2 were cut without tagging their change-log entries, so
`check-releasability` reported six shipped scopes as still pending, and finished build plans read as
live work.

**What shipped:**

- Ten entries carry the release that first contained them, found by the commit that introduced each
  heading: eight in v0.2.0, two in v0.2.2. v0.2.1 shipped none.
- The investment-activity and MCP-error-recovery plans are archived as shipped. The
  reconciliation-and-status plan stays live: its Chunk 03 (`bankmachine status`) never finished.
- The shipped history moved verbatim into `.prawduct/change-log-archive/2026-09.md`, and the one
  test docstring citing a story that moved now points at the archive.

## Consumer note
none: governance bookkeeping only; it tags which release carried earlier changes and changes nothing an operator or MCP client sees.
