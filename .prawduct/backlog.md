# Backlog — bankmachine

<!-- Structured backlog (Prawduct v1.7+). Managed with the `/backlog` skill:
     /backlog            summary + menu
     /backlog pick       what to work on next (filters + natural language)
     /backlog add        file a new item (searches for duplicates first)
     /backlog find <q>   search title/metadata/body
     /backlog list       tabular view (default: open, added within 90d)
     /backlog update ID  change metadata or status
     /backlog migrate    convert legacy unstructured items to this format

     Items move between the three sections below via `/backlog update ID status=...`.
     The framework never infers status from build plans or change logs — an agent
     or human makes the call explicitly (see backlog-system-requirements.md D4/§5).

== Item shape ==

  EVERY ITEM STARTS AT COLUMN 0. The parser takes a column-0 `- ` bullet as the
  item boundary and treats everything indented as that item's body, so an item
  written with leading spaces is invisible: it parses as no item at all, and a
  whole backlog written that way migrates as zero issues. The example below is
  therefore flush-left on purpose — do not indent it back to match this comment.

- **[PFX-XXXX]** One-line title
  `effort: M · impact: M · area: stop-hook · source: reflection · added: 2026-05-29 · status: open`

  Free-form body of any length — a single sentence or multi-paragraph analysis
  with file refs, fix-shape, and open questions. The author chooses what fits.

  ID format `[PFX-XXXX]`:
    PFX = 2–3 uppercase letters naming the work-space the item was filed from.
          Derive a sensible prefix from the item's area; reuse existing ones so
          related items share a prefix. Starter vocabulary (extend freely):
            STH stop-hook · CRT critic · SYN sync · LLM prompt/LLM · BKL backlog
            MIG migration · JNT janitor · MET methodology · DOC docs · TST tests
          A project may optionally declare its prefix vocabulary as
          `backlog_prefixes:` in project-state.yaml for validation — not required.
    XXXX = 4-char random alphanumeric (base36). Random IDs avoid cross-branch
           collisions; ~1.7M combinations per prefix.

  Metadata bar (one backticked, dot-separated line; required on new items):
    effort: S | M | L     S = <30 min · M = hours · L = multi-chunk
    impact: S | M | L     S = cosmetic · M = quality-of-life · L = user-felt/structural
    area:   <tag>         free-form topic tag; reuse existing tags to enable grouping
    source: builder | critic | reflection | janitor | user
    added:  YYYY-MM-DD
    status: open | promoted | shipped | dropped
  Optional, on the same line (distinct concepts — keep them straight):
    related:   PFX-XXXX, PFX-XXXX   cross-references to related items
    closes:    PFX-XXXX             this item supersedes another backlog item (item → item)
    closed-by: <chunk-id | scope/branch | tag>  what shipped this item (item → release), set on
                                    status=shipped; a handle that exists before the commit —
                                    never a bare commit SHA (dangles on --amend) or unassigned PR#
    reviewed:  YYYY-MM-DD           last-touched timestamp (auto-set on any update)
    accepted-by: @actor             soft claim "someone is on this" so others don't
                                    double-pick; pick/list exclude claimed items.
                                    Does NOT auto-expire; auto-cleared on ship/drop.
                                    Not a lock (backlog.md is eventually-consistent).
    stage: <lifecycle>              idea | research | requirements | design | ready.
                                    Where the item sits in the feature lifecycle;
                                    only `ready` is implementable. Absent/early =>
                                    pick routes to discovery/planning, not code.
    refs: <doc#section>, <doc>      links to governing artifacts (requirements /
                                    arch / design docs). Distinct from `related:`
                                    (which is item -> item).

  Legacy items (no metadata) remain valid — tools treat them as
  `effort: ? · impact: ? · area: untagged · status: open` and rank them lower.
  Run `/backlog migrate` to add structure at your own pace; nothing is forced. -->

## Open

<!-- Items available to pick up. -->

- **[ARC-7K2M]** Build the enforcement tests for the four architecture Direction norms
  `effort: M · impact: L · area: architecture · source: builder · added: 2026-09-05 · status: open · stage: ready · refs: .prawduct/artifacts/architecture.md#direction, docs/system-requirements.md#AC-ARCH.7`

  The four norms born in `.prawduct/artifacts/architecture.md` § Direction each carry `Mechanism:
  Test` in the preferences norm index, and none of those tests exist — there is no Python scaffold
  yet. This item is the filed mechanism the norm-birth rule requires, so the norms are not
  aspirational. It is build-step-1 work and should be promoted into that plan rather than picked
  independently.

  All four are behavioural, and all four have strong tests available — none needs a greppy
  heuristic, so none falls through to Critic:

  1. **One writer at a time.** Start a second writer-role process while the first holds the lock;
     assert it exits non-zero with a message naming the running sync, and that it did not write.
     Plus a structural check that every write path opens its connection through the one writer
     helper.
  2. **Reader is `query_only` and holds no cross-call snapshot.** Assert a write through the MCP
     server's connection raises; and assert `wal_checkpoint(PASSIVE)` moves all frames while the
     MCP server is connected but between tool calls — that is the test that actually catches a
     snapshot leaked across calls, and it is the one worth getting right.
  3. **No implicit creation.** Point the config at a nonexistent path, start the MCP server, assert
     the file is still absent afterwards and that `get_pipeline_health` reports the datastore
     missing rather than empty.
  4. **Schema-version refusal.** Stamp a schema version above the reader's supported range; assert
     the reader refuses to serve and says so, rather than answering from tables it half-recognises.

  Note for whoever writes these: the probe evidence behind the norms lives in the architecture
  artifact's Decision Log. Test 2's checkpoint assertion is the one that reproduces the measured
  0-of-93 starvation, so build it against a WAL with real frames in it, not an empty one — a
  checkpoint over zero frames passes trivially and would be exactly the fail-open control this
  project has already been burned by twice.

## Promoted

<!-- Items currently being addressed in an active build plan. /backlog pick
     skips these by default (work is already in flight). -->

## Archive

<!-- Shipped and dropped items, kept for searchability. Never deleted. -->
