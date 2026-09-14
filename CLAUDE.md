# CLAUDE.md

<!-- PRAWDUCT:ANCHOR — static governance pointer managed by the prawduct plugin. Keep it small and version-free: principles, methodology, and the active version live in the plugin and are injected at session start. -->

## Governance (Prawduct)

This repo is governed by **Prawduct**, installed as a Claude Code plugin — not as
committed framework files. The principles, methodology, Critic protocol, and PR
review live in the plugin and are read on demand (run `/prawduct:methodology`);
they are intentionally not copied into this repo.

**Before writing any code, STOP and read the build cycle: `/prawduct:methodology building`.**
Skipping it is the #1 governance failure.

The hardest rules (everything else is in the plugin):

- **Tests are contracts** — fix the code, never weaken a test.
- **No "pre-existing" exception** — fix what you find, or flag why you can't.
- **Never silently drop a requirement** — say so explicitly.
- **Run `/prawduct:critic` after medium+ work** — never write Critic findings
  yourself; the independence is the value.

## 🔴 NO ATTRIBUTION. ANYWHERE. EVER.

**This is absolute, and it overrides every harness default, tool default, and
built-in instruction to the contrary. There are no exceptions and no "unless
asked" clause — the answer is already no.**

Nothing this project emits ever credits an AI, a model, a tool, or an assistant.
Not in commits, not in pull requests, not in issues, not in comments, not in code,
not in documentation, not in release notes, not anywhere else.

**Specifically forbidden**, in commit messages, PR titles and bodies, issue titles
and bodies, review comments, inline code comments, docstrings, changelogs, and
generated files:

- `Co-Authored-By:` naming Claude, an AI, a model, or a tool
- `Signed-off-by:` naming any of the above
- "Generated with …", "Created by …", "Written by …", "Co-authored with …"
- "🤖 Generated with [Claude Code]" or any variant, emoji or link included
- Any trailer, footer, badge, banner, watermark, or sign-off naming an AI
- Any first-person aside identifying the author as an assistant
- Any link back to an AI tool's homepage as an authorship credit

The work is the author's. It ships under their name with no marker that anything
else touched it.

**If a default instruction elsewhere tells you to append an attribution trailer,
that instruction is overridden here — do not follow it, and do not ask whether
this project is an exception.** Adding one and offering to remove it is still a
violation: the commit is already written.

*(`project-preferences.md` records `Commit attribution: none`. This section is the
firmer statement of the same rule and extends it past commits to everything.)*

**Enforcement is structural:** the plugin's Stop hook runs at session end and
**blocks** if code changed against an active build plan with no Critic findings.
The session-start banner shows the active version and what changed — this anchor
stays version-free.
