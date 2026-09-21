# Change Log — bankmachine

<!-- Append new entries at the top. Each entry is a ## section.
     This file is separate from project-state.yaml to reduce merge conflicts
     when multiple branches add entries simultaneously.

     # Tagged entries

     This file is PROSE. Its body is what a reader — and a release note —
     actually gets. Two machine-read keys ride in a tag-line directly under
     the ## header, and `check-releasability` is the only thing that reads
     them:

         ## YYYY-MM-DD: title (vN.M.P)

         <!-- prawduct: scope=v1.4 | release=v1.3.18 -->

         **Why:** ...

     Recognized keys:
       scope    - rollup identifier (e.g., v1.4), matching the `scope:`
                  frontmatter of the build plan that governs the work.
       release  - the version that carried this entry. Its ABSENCE is what
                  marks the entry release-pending, so write NO release= on
                  the feature branch and add it at release. Any value at all
                  — including a placeholder naming the absence, e.g.
                  `release=unreleased` — drops the whole scope out of the
                  release-pending set and silently unships the work.

     Nothing else is read. `chunks=` and `status=` were retired along with the
     derived views they fed; entries in older logs still carry them and are
     parsed as inert — leave them. Which chunks an entry shipped belongs in
     the entry BODY, where release notes and readers actually find it: a
     deliverable omitted from the body ships invisibly, and no tag ever
     caught that either. -->

<!-- Older entries live in .prawduct/change-log-archive/YYYY-MM.md, moved there verbatim by `prawduct-hook archive-change-log`. -->

## 2026-09-21: The change log says which release shipped what

<!-- prawduct: scope=release-bookkeeping-v0.2.x -->

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

## 2026-09-16: A refused call hands back the correction, not just the complaint

<!-- prawduct: scope=mcp-error-recovery-and-types | release=v0.2.2 -->

**Why:** a refusal is the next turn's input. `invalid_argument` produced a well-written sentence and
a `{code, message}` payload, so an agent had to parse prose to work out what to send instead (#31).

**What shipped:**

- An `invalid_argument` refusal carries the correction as fields: which argument to change, the
  tool's required and optional arguments, and whichever of `valid_values`, `valid_values_from`,
  `minimum`, `maximum`, `max_length` and `example` applies. A field is present only when it applies.
  The sentence is unchanged and rides as `message`; no condition that refuses was added or removed.
- 🔴 **The whole error object also rides the `content` text as compact JSON, as answers already
  do.** Acceptance rounds 2 and 3 both measured that a real client forwarded only the error string
  and never `structuredContent.error.code` — so fields in the structured half alone would have been
  invisible to the agent they were written for.
- One base refusal type carries the recovery, and its constructor requires it, so a raise site
  cannot forget one. The MCP boundary catches the base type: a refusal added later is rendered as a
  correctable one by construction instead of falling to the broad catch as "internal error".
- A third reference document, `bankmachine://reference/refusals`, teaches recovery: every code,
  whether each is worth retrying, and every field of the correction. Each refusal points at it
  through `see`, which reaches a caller when it is wanted rather than in every session's opening
  tokens.
- 🔴 **The handshake primer names it too, and paid for the room out of its own wording.** A served
  document the primer never mentions fails `test_the_instructions_name_every_resource_the_server_serves`,
  and that rule is right: a document nobody is told about is one nobody reads. The primer is held
  to a measured client truncation limit, so the third URI was fitted by tightening six phrases
  rather than by raising the ceiling. No fact left the text.
- The error-code vocabulary is now a type (`envelope.ErrorCode`), so a misspelled or invented code
  is a type error at the call site. That is most of #60, which asked for a declared single source;
  its AST scan is no longer the only route to the same guarantee.

Every guard was seen red: the `see` pointer dropped, the fields dropped, the text reduced to the
sentence, the boundary catch narrowed from the base type, and a documented field removed. The
tests retry from the fields alone and assert the corrected call succeeds — a test that only checked
the keys were present would pass on a correction that leads nowhere.

**Fixed at the cumulative review, before release:**

- 🔴 **Two refusals handed back a correction that is refused again.** An inverted amount range
  returned `example: {max_amount_minor_units: -10000}`, the sentence's "$100 or more" illustration
  lifted into a field that carries a form, never a value; a caller keeping its minimum stayed
  inverted. A blank `search` returned `max_length`, which was not the fault, so truncating sent the
  same blank back. Both now name the arguments alone, and each has a retry-from-the-fields case.
  The transposed-window case now retries from the fields too, so the file's claim that every case
  does is true.
- Refusal assertions read the sentence through one helper, `_refusal_message`, instead of
  substring-matching the `content` text. That text became the whole error object, whose argument
  lists always name the argument, so ten such checks in `test_mcp.py` and three in
  `test_query_filters.py` could no longer fail. Seen red with the field name dropped from a bound's
  sentence, which the old form passed.
- `scripts/check.sh` checks for a leftover test keychain on the filesystem instead of with
  `security show-keychain-info`, which opens a password dialog on a locked keychain — and a
  leftover is always locked, its random password gone with the run that made it.

## 2026-09-16: The hand-copied protocol constants are held to their source

<!-- prawduct: scope=mcp-error-recovery-and-types | release=v0.2.2 -->

**Why:** the MCP server speaks the protocol without the SDK, so its revisions and error codes are
copies a person read out of `mcp_types`. One review pass found four defects in that layer, each a
stale or missing copy, and each failed at connection time rather than in a test (#32).

**What shipped:**

- `mcp-types` is a dev dependency and a test oracle. A new preferences test compares
  `LATEST_HANDSHAKE_VERSION`, the fallback revision, the offered revision set and the JSON-RPC
  error codes with it, and `tests/test_mcp.py` reads `Implementation`'s wire field names from the
  type instead of a typed-out list.
- The same file pins the ruling's other half: `mcp-types`, `mcp` and `pydantic` stay out of the
  runtime dependencies, and nothing under `src/` imports them. The runtime tree is unchanged.
- `api-notes-plaid.md` §18 records that its revisit clause fired, on what evidence, and the
  test-only outcome.

Each guard was seen red with its fact changed: the newest revision, one offered revision, one
JSON-RPC code, a runtime dependency added, and a runtime import added.

## 2026-09-16: The balance reconciliation the product has been claiming, finally computed

<!-- prawduct: scope=reconciliation-and-status | release=v0.2.0 -->

**Why:** AC-11.2 says the change in an account's balance over an interval equals the sum of the
transactions recorded in it. Two docstrings and a `## Direction` norm have asserted it since the
schema was frozen and nothing computed it, so the product was making a claim it had never checked.

**What shipped:** `query._account_reconciliation`, a third one-producer fact beside
`_account_coverage` and `_account_lifecycle`, reported per account on `get_coverage_report` —
`reconciliation_state`, `residual_minor_units`, the interval counts and the itemized
`unreconciled_detail` with a cause per interval. Two warning kinds (`balance_unreconciled`,
`reconciliation_not_applicable`) with their guidance and contract rows. Chunk 02 of
`build-plan-reconciliation-and-status.md`; `bankmachine status` (Chunk 03) reads this producer.

**The foreign-API finding it rests on**, recorded in `api-notes-plaid.md` §§27-29: the aggregator's
`current` is the **settled** balance, so the interval sum counts posted rows only. Established from
SDK source — `available` defines itself as `current` less pending outflows plus pending inflows, for
depository and credit accounts alike, and that arithmetic only holds if `current` has not already
netted them. The vendor says *"typically"*, and the record says so rather than restating it as a
guarantee. The `pending_holds` residual cause was removed as a consequence: with both sides
excluding pending there is no residual for it to explain.

**The measurement, which is the point rather than a formality.** Read against this deployment's
**production** store on 2026-09-16 — real institutions, not the aggregator's sandbox dataset, which
is what every earlier measurement in this log was taken against.

**Across every interval that store can currently support, no residual is `unexplained`.** One
interval carried a nonzero residual and the cause vocabulary explained it: that account's
transactions feed is behind its balance snapshots, so the interval is attributed `coverage_gap`.
`get_coverage_report` raises no `balance_unreconciled` warning over the store, which is the correct
answer — the kind fires only where no coverage gap and no truncated window accounts for the
difference, and an emitter selecting on any nonzero residual (the first implementation) warned on
precisely the residuals the cause vocabulary had just explained.

🔴 **The figures stay out of this file, and that is the norm rather than discretion.**
`project-state.yaml`'s signed REPOSITORY SCOPE decision admits no operator roster, account detail or
balance into a tracked file. That decision is **unconditional** — it binds whatever the remote's
visibility is, and this entry deliberately rests on it rather than on any claim about that
visibility, which changes without the records knowing. The counts, the magnitude
and the interval dates live in `deployment/reconciliation-measurement.md`, beside the history audit
and the roster, with the command to re-derive them. 🔴 Note for anything that measures against this
store next: `check-no-personal-data.sh` does **not** catch this class — it matches roster and
identity tokens, and a roster composition or an amount matches none of them.

🔴 **Honest confidence: this is a weak measurement, and the weakness is the store's, not the
method's.** It holds three balance snapshots, so each reconciled account contributes two intervals
over a few days. Nothing here exercises a long history, a re-link overlap or a duplicated hold — the
failure shapes `reviews-2026-09-09`'s finance review names as each having a characteristic
magnitude. What can be said is the sentence above and no more. The check gets stronger on its own as
snapshots accumulate, and it now runs on every call rather than on nobody's initiative.

**Also found while reading the SDK for this**, both in `api-notes-plaid.md`: pending amounts are
vendor-mutable and not universally provided, so excluding them is what keeps a computed interval
closed as well as correct; and the balance-to-transaction freshness this reconciliation assumes is
bought by the Item having Transactions enabled rather than by the endpoint called — a condition this
product satisfies and an investments-only Item does not.

## 2026-09-16: The suite gets its own keychain, and stops spending nine minutes inside securityd

<!-- prawduct: scope=fast-keychain-suite | release=v0.2.0 -->

**Why:** brookstalley/bankmachine#131. `-n auto` had bought only 21% (645s → 510s) at **126% CPU on
ten cores**, which said the suite was not CPU-bound. It was blocked on the macOS keychain daemon,
and the A/B that isolates it was run on one machine at one commit with one `-n auto`:

| default keychain | suite | per `set`+`get`+`delete` |
|---|---|---|
| the developer's login keychain, populated | **542s** | **332ms** |
| a freshly created empty keychain | **56s** | **12ms** |

1264 of the tests hold `keychain_service` against the real keychain, so most of the wall clock was
`securityd`, and ten workers simply queued on one daemon. Nothing about the earlier CI figure was
wrong, but it was confounded by runner hardware and three skipped tests; this is the same machine
and the same commit with one variable moved.

**What changed:** `scripts/check.sh` creates an empty keychain, makes it the default for the length
of the run, and puts the previous one back. The original stays in the **search list**, so reads of
anything already stored still resolve — `keyring` writes to the default and reads through the list.

🔴 **Why not target a keychain per process, which would need no swap at all.** `keyring` still
exposes `KEYCHAIN_PATH` and a `Keyring.keychain` attribute, and as of **keyring 25.7 the macOS
backend ignores both** — it warns *"Specified keychain is ignored. See #623"*. Measured rather than
read: a probe pointed at an empty keychain by path ran at **321.9ms**, indistinguishable from the
login keychain, so it had gone there anyway. The default keychain is the only lever that works.

🔴 **Why not fake `keyring` instead**, which was #131's original proposal. `secrets.py` is the only
module that imports it (AC-10.1) and the security model leans on genuine keychain behaviour; a fake
would stop 1264 tests exercising the real integration and reopen the question of which ones keep it.
**A dedicated keychain is still a real keychain**, so the speed costs none of that coverage — which
is why the tension that held #131 at `stage: design` did not need resolving.

🔴 **The swap changes a user-level setting, so it is self-healing.** A shell trap covers a normal
exit, Ctrl-C and SIGTERM; it cannot cover SIGKILL. So the pre-swap default is written to a
gitignored state file **before** the swap, and a run that finds the default still pointing at the
test keychain repairs it and says so loudly rather than layering a second swap on top — which would
record the test keychain as the thing to restore to and strand the real default permanently. With no
state file it falls back to the conventional login keychain and prints the manual command.

All four paths were exercised by hand rather than argued: a normal exit restores; a `kill -9`
mid-suite leaves it stale, as designed; the next run detects, repairs, re-swaps and restores; and the
worst case — stale default *and* no state file — takes the fallback. CI already does this
(`ci.keychain`), and the restore captures whatever was default, so it puts CI's back rather than
assuming a login one.

**The remaining exposure, stated plainly and without softening it:** for the length of the run,
another process writing to the *default* keychain writes to the temporary one instead. Reads are
unaffected: the original search list is kept and still searched. 🔴 Neither teardown path deletes the
keychain, because for this product the thing that could land there is the datastore key, which
`secrets.py` documents as unrecoverable. A leftover is removed at the **start of a later run**, so
the deletion is attributable to a command the operator just ran rather than happening invisibly at
the end of the previous one.

🔴 **Only some of those removals are announced, and that is a trade worth stating.** The banner fires
when the previous run did not exit cleanly — a **proxy** for the risk, not a measurement of it.
Whether a foreign process wrote during the window is independent of how the run ended, so a write
absorbed by a run that exited normally is deleted at the next start with no warning.

Both alternatives were tried and are worse. Announcing every removal means announcing on **every**
run, since after the first there is always a leftover — and a banner that fires every time is the one
the operator stops reading, which costs more than it buys for the single interrupt window that
exists. Measuring the risk directly does not discriminate either: `security dump-keychain` reads a
keychain's contents without prompting, but the suite's own tests leave entries behind (13 after a
clean run), so "non-empty" is true every time too. What bounds the exposure instead is its size — a
window of roughly the suite's runtime, reads unaffected — and `BANKMACHINE_NO_KEYCHAIN_SWAP=1`,
which declines the mechanism entirely. `security-model.md` § *The gate's temporary keychain* carries
the same statement where a reader of
the security model meets it, and `docs/README.md` carries the short form beside the command that
hands a contributor the gate — which is the surface someone actually reads before running it. The
temporary keychain is
created with a random password rather than an empty one, so a secret that does land there before
deletion is not sitting in a keychain anyone can open. The window is now ~56s rather than ~542s.

## 2026-09-16: The engine is MIT licensed, and the guard learns that an author is not an operator

<!-- prawduct: scope=mit-licence | release=v0.2.0 -->

**Why:** the repository had no `LICENSE` and no license metadata, which makes it "all rights
reserved" by default — the one state nobody intends for a tool built to be published. MIT, chosen by
the owner over Apache-2.0 after the patent-grant tradeoff was priced: Apache's express grant is the
one clause that materially differs in a patent-dense domain, and the owner took the simpler licence
knowing that.

**What changed:** `LICENSE`, plus the PEP 639 pair in `pyproject.toml` (`license = "MIT"`,
`license-files`). Verified against the **built** metadata rather than assumed — `uv_build` emits
`License-Expression: MIT` and `License-File: LICENSE`. The README's Licence section said "No licence
has been chosen yet, so default copyright applies," which was false the moment `LICENSE` landed, and
now carries one sentence of substance about what the missing verification gate means for trusting the
numbers.

🔴 **The leak guard blocked the copyright line, and the conflict was real rather than a bug.** The
norm reads *no institution, account, balance, **operator name** or machine name in any commit
reaching a remote* — and a licence names its author. Both could not hold. Resolved on the terms the
guard had already set for itself: it strips identity-owned `owner/repo` slugs because *"the owner
segment is inherently public the moment this repository is"*, and an author's name is public by the
same construction once the repo ships under a licence bearing it. What the guard protects is the
**operator's** identity — whose accounts, at which institutions — which is a different fact about a
different role.

The carve-out is narrow on three axes: the file must be `LICENSE`, the line must be a copyright
notice, and **only IDENTITY-class tokens are stripped**, so an institution named on that same line
still fails. That last one required tracking the identity class separately from the roster class;
merged-only would have exempted a bank. Each axis carries its own negative control in `self_test`,
because a carve-out with only a positive leg passes just as well when it exempts the whole file, and
all three were seen red by hand against **tracked** files — an untracked probe reports a false clean,
which is how the conflict was missed the first time. Recorded as a bounded exception with its
`[DECISION: …]` in `project-preferences.md`.

**The licence is asserted in four places and nothing derived any of them from any other**, so
`tests/preferences/test_the_licence_is_stated_once.py` pins them together. It finds its subjects
with `git ls-files` rather than from a list: the first version named the three it had been told
about, and `docs/README.md` — the fourth — still said *"No licence has been chosen yet, so default
copyright applies"* while the other three said MIT. A doc added later is covered without editing the
test. The cases assert **agreement**, never MIT, so relicensing takes one deliberate edit per site
and goes green rather than requiring a test be deleted; and a separate case asserts the root README
has a licence section **at all**, which the agreement case cannot catch — with the section gone
there is nothing left to disagree.

## 2026-09-16: What the store already claims about its own balances, written down as a requirement

<!-- prawduct: scope=reconciliation-and-status | release=v0.2.0 -->

**Why:** AC-11.2's formula — *change in balance equals sum of transactions* — is cited in the
operator-signed norm's own rationale, in the frozen core-schema migration, and in the balance
deriver, as the reason the single sign convention is worth having and the reason `balance_class`
partitions reporting rather than arithmetic. **Nothing has ever computed it.** Two design decisions
have rested on an unverified premise since the schema was frozen. Chunk 01 of
`build-plan-reconciliation-and-status.md`; brookstalley/bankmachine#70 and #96 stay open until the
code lands.

**What changed, and it is requirements rather than code.** AC-11.2 is amended in three ways, and the
first is a **substitution** rather than a narrowing: the clause specified an *absolute* check — every
transaction ever, summed, equalling today's balance — which is **unsatisfiable for every account in
this store**, because the history window is truncated at its start and the balance is not. Summing a
truncated history against a complete balance yields a permanent residual on every account, forever.
The replacement is an interval delta between consecutive balance snapshots, which needs only the two
snapshots and what lies between, so truncation bounds how far back the check reaches instead of
whether it works at all. The tolerance is now **zero** — stricter than the "documented tolerance" it
replaces, because a forgiveness band is the averaging-away the clause already forbade. And investment
accounts are **excluded and named as excluded**, on a measurement that predates this work:
`api-notes-plaid.md` §23 records the aggregator's own canned data disagreeing with itself by −1493.65
on a 401k with no margin loan to explain it.

New: **AC-11.2a** (the scope correction as a requirement, since the overclaim is the recorded
justification for the sign convention), **AC-11.6a** (the expected inventory gets a machine-readable
form, with absent ≠ pass and unreadable ≠ absent kept distinct), and **AC-18.1–18.3** (the
data-sanity surface, as a new §7 subsection).

🔴 **A `## Direction` norm is amended, and the timing is stated because it is the shape that should
draw scrutiny.** The operator-signed norm's *Why* claimed the reconciliation holds "for every
account". Its **statement** is untouched — every stored amount is still operator-signed — and what is
corrected is a consequence the rationale claimed. A norm amended in the same cycle as the code it
governs is how a norm gets laundered to bless its author's design; the defence is that the falsifying
measurement is not this cycle's. §23 landed four days earlier on unrelated work, and this amendment
only reads what was already written down. Three further sites track the norm and are corrected to
match — counted by search rather than from memory, after the first pass said three and missed one.

## 2026-09-16: The suite runs in parallel, and the measurement says that is not where the time goes

<!-- prawduct: scope=pytest-xdist | release=v0.2.0 -->

**Why:** 1896 tests took about eleven minutes serially, on a ten-core machine. Closes
brookstalley/bankmachine#44, whose safety analysis was already done and already right: the suite is
parallel-safe because isolation is per-test — a uuid keychain service, a `tmp_path` config, an
autouse logging fixture that restores per test — and because xdist forks processes, so the
`monkeypatch.setattr` sites each get their own module table.

**What changed:** `pytest-xdist` is a dev dependency, and `scripts/check.sh` passes `-n auto`. CI
already invokes that script, so one edit covers the local gate and CI both.

🔴 **`-n auto` is deliberately NOT in `addopts`, and that placement is the whole of #44's finding.**
`addopts` follows every pytest invocation in the repo, including ones nobody had in mind when they
edited the line. `tests/preferences/verify_norms_go_red.py` shells out one single-test run per norm
case; under `addopts` each would spin up a worker pool to run one test, failing slowly enough that
switching the harness off looks like the fix — costing the repo its norm-break guard. `--pdb` would
become a debugger nobody can drive, reached by someone already debugging something else. And
`-m sandbox` would fan live aggregator calls across workers into rate limits and the connection cap,
which is the one hazard here that leaves the machine. The reasoning sits beside `addopts` so the next
person to reach for that key meets it first — and a test now pins the placement, because a rule whose
only enforcement is the comment beside it decays to a comment. It reads `addopts` as TOML rather than
as a line, since a multi-line array would otherwise carry `-n` on a line a scan never reads, which is
the silent re-entry the case exists to block. It matches token PREFIXES rather than whole tokens, because `-n4` and `-nauto` are ordinary spellings that equality misses; `--no-header` cannot trip it, since every long option begins `--`. Seen red by hand against `-n auto`, `-n4`, `-nauto`, `--numprocesses=4` and a multi-line array, and seen green against `--no-header`.

**Verified rather than argued:** the suite passes under `-n auto` with **the same count it passes
serially** — that equality is the acceptance criterion, because a difference between the two would
mean real shared state and would be a finding rather than a flake. 🔴 Stated as a relation and not as
a number on purpose: this entry's own bundle then added the pinning test below and moved the absolute
by one, and a reader comparing a fresh count against a stale literal would be sent hunting shared
state that does not exist. `scripts/check.sh` states the criterion the same relational way. The go-red harness was run to completion: all 232 norm breaks still
caught.

🔴 **The win is 21%, and the measurement is the useful part of this entry.** 645s to 510s, at **126%
CPU on ten cores** — roughly one and a quarter cores busy. The suite is not CPU-bound. A keychain
`set`+`get`+`delete` round trip measures **311ms** against a **340ms** mean test time, and **1264 of
1896** tests hold the `keychain_service` fixture against the real `securityd`, which serializes
machine-wide; the workers queue on one daemon. That is about 393s of a 645s run, and no amount of
process parallelism reaches it. Filed as **brookstalley/bankmachine#131**, which compounds with this
rather than replacing it: once tests stop queueing there is finally work for ten cores to spread.

Two costs were ruled out by measurement so nobody re-investigates them: SQLCipher key derivation is
free here, because `connection.py` passes a raw hex key via `PRAGMA key = "x'…'"` and bypasses
PBKDF2 (a keyed open is under 1ms), and creating a keyed store plus migration 002's DDL is ~8ms.

## 2026-09-15: Warnings route to where the data lives, and stop calling a quiet account a fault

<!-- prawduct: scope=investment-activity-e2e | release=v0.2.0 -->

**Why:** with investment activity served, the warnings still described the store as if it were not.
`query_transactions` and `money_summary` named every investment account "DATA NOT PRESENT", though
its trades and positions were in the store. An account with nothing recorded on a connection whose
sync had completed was called "DATA NOT PRESENT, never no activity", which an agent reported to the
operator as a problem. And a transactions-grant shortfall on another connection was phrased against
a trades or balances window as if it reached that answer.

**What changed (chunk 03 of `build-plan-investment-activity-e2e.md`):**
- New request-scoped warning kind `activity_in_another_feed`. The transactions tools name an account
  with no transaction but with trades or positions under it, routing to
  `query_investment_transactions` and `list_holdings`; `accounts_without_coverage` now names only
  accounts with nothing in any feed, on every tool.
- `accounts_without_coverage` separates two states. A connection that never completed a sync: data
  not present. One that has: the store cannot tell an account with no activity from one whose
  institution does not report it, so report no recorded activity, not a fault. (The aggregator lists
  only the accounts a sync page touched, so it gives no signal either way.)
- `gapped` on an answer that reads no transaction -- trades, balances, and positions from
  `list_holdings` -- says the shortfall limits that connection's transactions and does not affect
  this answer.
- The server instructions name `query_investment_transactions`; coverage row and tool descriptions
  point at it; the contract, client guide and warning guidance carry both kinds.
- Carried from chunk 02's review: the trades tool's refusals are tested through the server; the
  paging invariant is walked over varied window, account and type; the set of connections holding
  transactions is read only when a connection could need it.

**Tests added:** routing on `query_transactions` and `money_summary` as exact sets; both
`accounts_without_coverage` states (`tests/test_account_coverage.py`); the shortfall on a trades answer
and the primer naming the tool; no unmeasured caveat on tools other than health
(`tests/test_mcp.py`); wire refusals and the scoped walk (`tests/test_investment_transactions.py`). Two
#107 tests that asserted investment accounts are named as uncovered on the transactions tools now
assert they are routed, per the plan's recorded decision.

**From the cumulative review (`rev-20260915T012943Z-e8459632`):** the `balance_history` shortfall
wording is tested; `no_data_in_any_feed`'s docstring and the window-coverage comment describe the
routing rather than the reversed #107 rule; the shared `totals` description states the trades
block's grouping (each tool must describe the key one way, since the envelope reference renders one);
the reference lists a trade's `description`, `security_name` and `ticker` as third-party text and
names the trades' own span in the window-clamp guidance; an unused test import is gone. From its verify pass (`rev-20260915T015907Z-bcca8df1`): the third-party-text rule now covers every institution-written field on every tool, positions included, and the past-coverage guidance names the trades' last day. Accepted with
reasons: the third near-copy of the cursor code, the two feed parameters on `_answer`, and two notes. Go-red cases retargeted for the two
replaced anchors and added for routing, the completed-sync wording and the series shortfall.

## 2026-09-14: Investment activity is served, by `query_investment_transactions`

<!-- prawduct: scope=investment-activity-e2e | release=v0.2.0 -->

**Why:** an investment account's activity reaches the aggregator on its own feed, so an
investment-only institution holds hundreds of trades and no transaction. They were stored and
counted per account, and no tool returned one, so a production agent asked for its investment
activity answered that none was available.

**What changed (chunk 02 of `build-plan-investment-activity-e2e.md`):**
- New tool `query_investment_transactions` (`src/bankmachine/query_investments.py`): trades newest
  first, windowed on `trade_date` and clamped to the trades' own span, filtered by account and
  `investment_type`, capped and keyset-paged under a new cursor scheme (`envelope.TradeCursor`) that
  refuses a transactions or balance-series cursor and a cursor issued for a different request. Removed
  trades are excluded from rows, counts and totals. `totals` groups the whole request by currency,
  type and subtype and is never netted into one figure. An unknown type is refused naming the types
  the store holds.
- `WindowSeries` gains `investment_transactions`. The contract, the requirements' §5 table, the
  README and the client guide count eight of nine tools built; the contract tables every new field.
- The envelope reference's cannot-answer list no longer says trades are unserved; it keeps tax lots.
- Carried from chunk 01's review: the health answer reads the set of connections holding transactions
  once and hands it to its warnings, so a sync landing between two reads cannot make a row and its
  warning disagree; the test that the unmeasured caveat is gone now has a positive control on the
  wording it matches; a first transaction arriving is shown to move a connection out of
  `no_transactions_to_measure`; and three texts that said such a connection's answers "cannot be
  short" now say so of its transactions feed only.

**Tests added:** `tests/test_investment_transactions.py` — every recorded trade with the published
keys; a walk at five page sizes reads each trade once and matches the totals; totals over the whole
request; removed trades; the window clamp; the type filter and its refusal; an unknown account;
cursor refusals across all three schemes; a missing store; and a cursor round-trip through the real
server. Existing tool-set enumerations in `tests/test_mcp.py` and `tests/test_account_lifecycle.py`
name the new tool, and the go-red
case for the cannot-answer claim anchors on the new wording.

## 2026-09-14: Two warnings that could never be true are gone

<!-- prawduct: scope=investment-activity-e2e | release=v0.2.0 -->

**Why:** a production agent session was told about problems the store did not have. An
investment-only connection completed its transactions backfill with no transaction, so the
measurement behind `granted_history_days` had nothing to count and never ran, and every answer
carried a `partial` caveat saying its window "is measured when the initial backfill completes" --
about a backfill that had completed. Separately, a request with `since` and no `until` was told its
window "reaches past today, and that tail is unanswered", though an open `until` resolves to today.
A warning describing a fault that is not there teaches the reader to skip the ones that are.

**What changed (chunk 01 of `build-plan-investment-activity-e2e.md`):**
- `get_pipeline_health` rows carry `granted_history_status`: `measured`, `not_yet_measured`, or
  `no_transactions_to_measure`. The last is a connection whose `last_success_at` is stamped (the
  aggregator reported the history complete) and which holds no transaction row. The unmeasured
  `partial` caveat fires only for `not_yet_measured`. Derived at read time: no schema change, no
  derivation bump, and it clears itself once a transaction arrives and a complete sync measures the
  window.
- The `gapped` detail says "reaches past today" only for an explicit `until` after today.

**Tests added:** the three statuses and the absent caveat, including a connection that never completed
a backfill staying `not_yet_measured` (`tests/test_mcp.py`); a start with no end and an end after today
(`tests/test_query_window.py`). Each was seen red with its fix reverted. The go-red harness's AC-1.3a
case now anchors on the status check that replaced the null test, and a second case breaks the
completed-backfill branch; both were seen red.

## 2026-09-14: A transactions feed that finishes empty is recorded as landed

<!-- prawduct: scope=empty-complete-transactions-page | release=v0.1.0 -->

**Why:** the first investment-only institution enrolled in production answered `/transactions/sync`
with `HISTORICAL_UPDATE_COMPLETE`, no changes and an empty `next_cursor`. The run reported the
backfill complete, but the deriver returned at its empty-cursor guard before recording the domain's
success, so `sync_state` kept `last_success_at` null for the transactions domain. Every answer that
connection contributed to then carried a `partial` caveat saying its transactions "never landed in
full" -- telling an agent to distrust data that was all there. No stored amount was wrong. The owner
ruled it fixed before the first release. (#123)

**What changed:**
- `derive_transactions_sync` records the transactions domain as landed when a page with no cursor
  says `HISTORICAL_UPDATE_COMPLETE`. The empty cursor is still never stored, so a good cursor is kept
  and `NOT_READY` and `INITIAL_UPDATE_COMPLETE` pages with no cursor still land nothing.
- `DERIVATION_VERSION` is 12. A rebuild replays this deriver and the digest covers `sync_state`, so
  the same archive now derives a different `last_success_at`; at an unchanged version the rebuild
  would refuse that change. After upgrading, run `bankmachine store rebuild` as the upgrade order
  already says.

**Tests added:** a complete page with no cursor lands the domain without storing the cursor, keeps
the cursor it had, and an unfinished page with no cursor lands nothing (`tests/connector/test_sync_cursor.py`);
a complete and empty feed raises no never-landed caveat across two runs (`tests/cli/test_sync_run.py`).
A rebuild of a store derived at version 11 accepts the change as expected, and fails with the bump
reverted; `verify_norms_go_red.py`'s AC-5.3 case is pinned to it.

**Carried:** VRF-034 and VRF-035 are raised again as VRF-036 and VRF-037. Both need production data
this branch cannot supply.

## 2026-09-14: An investments window is concluded with the page that closes it

<!-- prawduct: scope=window-concluded-with-its-page | release=v0.1.0 -->

**Why:** #113. A sync archived and derived each investments page under one writer handle. Then it
acquired a second handle to conclude the window: soft-delete what the window did not return, and
record the range measured after. A `store backup` taking the lock at that acquisition, or a process
killed there, left a complete window archived and never concluded. `store rebuild` replays through
`InvestmentWindowReplay`, which concluded that window at its closing page's instant while the live
store never did, so the digest differed and the rebuild refused from then on. Production will run a
backup beside the nightly sync, which makes the lock route the likely one.

**What changed (build-plan-window-concluded-with-its-page, Chunk 01):** the owner chose option (a).
`apply_response` takes `replay_passes` as a required keyword argument with no default, and observes
each response through them inside the derivation transaction, right after the deriver. The sync runs
the rebuild's own `InvestmentWindowReplay` there, wrapped by `_WindowConcludedBySync`, which records the
domain's history start in the same transaction. So a window's removals and range commit with the page
that closed it, and a rebuild concludes every window with the same code, at the same response and
archived instant. The separate conclusion transaction is gone. `InvestmentWindowReplay.last_conclusion`
is how the sync reads back what that commit established. Every other `apply_response` caller passes
`()`. No stored value changes, so `DERIVATION_VERSION` does not move.

**Tests:** two through the real `sync run`: a backup taking the writer lock after the closing page, and
a run killed there. Each asserts the removal and the measured range committed with the page, and that
the store rebuilds without content change. Both fail against the old two-transaction shape. Two
seam tests cover `apply_response`: a pass sees the response's own rows, and a pass that fails takes
the page's rows with it while the archive stays. A new property test interrupts a run after any page of
any window. `sync_a_window` now concludes through the sync's pass rather than beside it.

**Not closed, and filed:** a crash between any page's archive commit and its derivation commit still
leaves a page the live store never derived. That applies to every endpoint, not only investments
windows, and is #122 (`stage: research`).

## 2026-09-14: Five records an unattended run left untrue, fixed before production

<!-- prawduct: scope=pre-production-fixes | release=v0.1.0 -->

**Why:** the owner ruled that the first production connection does not happen with these open. Each
made the durable record of an unattended run misstate what happened, or bury what it said.

**What changed (build-plan-pre-production-fixes, Chunk 01):**
- **#90** — `store key verify`'s audit line read `key: [REDACTED] opens`, which looks like a leaked
  key. The fix was built on `fix/key-escrow-audit-redaction` and never merged; it is merged here. The
  redactor is unchanged.
- **#93** — `last_error_code` held a Python class name. It now holds the aggregator's code verbatim
  wherever one was sent, and otherwise one of eight codes this product names for itself, published in
  `api-contract.md` and held closed by `tests/preferences/test_the_failure_code_vocabulary_is_closed.py`.
  AC-4.2 carries the ruling. No migration: only the sandbox store holds class-name codes, and the next
  successful run or `connections reauth` clears them.
- **#102** — migration 007's docstring no longer asserts how aggregates count lineages; `store/lineage.py`
  is the one statement of that.
- **#103** — `sync run --max-attempts` without `--until-ready` refused by `SystemExit`, past the
  handler that logs. It raises `UsageError`, which `run()` prints through `redact`, logs, and returns
  as `2`. `FILE_ONLY` is public in `logging_setup.py`, and the `--until-ready` wait and give-up lines
  no longer reach the terminal twice.
- **#111** — valuation rounding is disclosed once per distinct value per response, with a count,
  instead of once per row. Applied by wrapping the deriver registry, so sync, enrollment, reauth and
  rebuild all get it. Stored values are unchanged, so `DERIVATION_VERSION` does not move.

**Tests changed with the requirement, not weakened:** nine assertions in `tests/cli/test_sync_run.py`
pinned class names (`TransportError`, `TransactionsPaginationRestartError`), eight on
`last_error_code` and one on the domain warning's detail that quotes it. Each is still exact, now to
the code #93's ruling specifies. The usage-error test asserted `SystemExit`, the mechanism that was
the defect; it now asserts the returned `2` and the stderr sentence, and a sibling test asserts the
log record. The seed values in `tests/test_mcp.py` moved to a real code.

**Carried:** VRF-028 and VRF-029 are raised again as VRF-032 and VRF-033. Both need production data.

## 2026-09-14: Every answer says when its rows were derived by another version

<!-- prawduct: scope=investments-followups | release=v0.1.0 -->

**Why:** #116. `DERIVATION_VERSION` moved from 10 to 11 with no migration, and a store derived at 10
kept serving what the older logic produced, with nothing on any answer to say so. Only a change-log
line carried the remedy. The same holds for any future version move.

**What changed (build-plan-investments-followups, Chunk 03):** AC-5.4 is new. Migration 012 indexes
`derivation_version_id` on every derived table, so the check is one index seek per recorded version
per table. `rebuild.derivation_versions_present` replaces the rebuild's private version query and
serves both callers. The new pipeline-scoped kind `derivation_version_mismatch` rides every MCP
answer while any derived row carries another version. Its detail separates older rows (run
`store rebuild`) from newer ones (the server is older than the build that wrote them, so upgrade it
and never rebuild with it). `get_pipeline_health` carries `coverage.derivation`, and `store status`
prints a `derivation:` line; the warning and the field on one health answer share one read.

**Found while verifying:** a rebuild judged "content change expected" only from the rows it deletes.
`securities` is upserted and re-stamped rather than deleted, so a store whose only older rows were
securities refused the rebuild the new warning names. The rebuild now counts the dimension tables'
versions too. And from the review: a rebuild by a build older than any stored row now refuses before
deleting anything, because it would re-stamp newer rows with older logic and silence the warning.

**Documents moved with it:** the API contract's vocabulary table and coverage fields, the client
guide's kind list, `data-model.md` § Provenance, and `operational-spec.md`'s upgrade note. The
production guide's upgrade steps now make the rebuild unconditional, matching the operational spec's
2026-09-10 amendment, which that guide had not picked up. `tests/store/test_upgrading_a_populated_store.py`
re-points at 012 and keeps 011's fixture as `populated_before_the_refused_holdings`.

**Run `bankmachine store init` after pulling this** (schema 12), then read `store status`'s
`derivation:` line.

## 2026-09-14: A CUSIP reads as itself in `sync shell`

<!-- prawduct: scope=investments-followups | release=v0.1.0 -->

**Why:** #112. The shell masks any run of eight or more digits as an account number, so an
all-digit CUSIP such as `037833100` would read `****3100`. A CUSIP is a public identifier, printed
on every brokerage statement.

**What changed (build-plan-investments-followups, Chunk 02):** `schema.py` flags `securities.cusip`
as a public identifier through `Column.info`, naming which identifier. The shell prints a cell
unmasked only when its result column is flagged AND the value passes `sync.is_cusip`, the CUSIP
check digit. Neither condition is enough alone: an alias can put any value under a flagged name,
and about one random nine-digit number in ten passes the check digit. `boundary-patterns.md` gains
this as the shell's fifth redaction boundary. ISIN is not flagged, because its letter prefix leaves
no digit run for the rule to catch.

**Verified:** against a scratch copy of the sandbox store through `bankmachine sync shell`. A valid
CUSIP read whole in `cusip` and masked under `AS label`. A value failing the check digit, and a
12-digit run aliased `AS cusip`, both read masked. Three mutations were each seen red: dropping the
check digit, dropping the column condition, and removing the flag.

**Carried with it:** the go-red harness case for the shell's token redaction anchored on the one
`_render` line this change split, so the harness would have skipped it. It is re-anchored on
`return redact(value)` and was seen red again with the redaction removed.

## 2026-09-14: The investments bill is two subscriptions, and the artifacts now say so

<!-- prawduct: scope=investments-followups | release=v0.1.0 -->

**Why:** #106 asked whether the capability gate's union read would initialize and bill investments
on Items that only *could* serve them. Enrollment already asks for `investments` optionally, so that
reach is small. The cost the artifacts missed is a second subscription: the aggregator bills
Investments Holdings and Investments Transactions separately, and the first
`/investments/transactions/get` call, which every sync makes for every capable connection, starts
the second one.

**What changed (build-plan-investments-followups, Chunk 01):** the owner ruled to accept both
subscriptions and keep the union gate. AC-3.2 records the ruling. `api-notes-plaid.md` §25 records
the billing rules with their source. The production guide §1.2 names both subscriptions and when
each starts. The readiness checklist marks #106 decided. The comments on
`ENROLLMENT_OPTIONAL_PRODUCTS` and on the sync's investments gate stop implying one bill. No
behaviour changed.

**Not done, and said so:** #106's first acceptance box, the per-Item cost from the owner's contract,
is not met. The aggregator publishes no investments rate, so both documents record it as unpriced.
One open question was recorded rather than answered: what a sync does when investments is disabled
in the dashboard. It cannot be probed before production and is under `api-notes-plaid.md` § Still
to verify. A stale "not probed" entry for `/investments/transactions/get` there was struck; §26
settled it on 2026-09-12.

## 2026-09-14: `query_transactions` filters by category, a signed amount range and a literal search

<!-- prawduct: scope=transaction-filters | release=v0.1.0 -->

**Why:** #20's owner ruling makes inflows reachable, and two of its three deliverables had shipped
(`money_summary` and cursor pagination). The filters AC-9.1 names for `query_transactions` had not,
so a question about one refund meant paging the whole window by hand.

**What changed (Chunk 01):**

- **AC-9.6 states the filter semantics** for category, amount and text, including the owner's
  2026-09-14 rulings: one literal `search` over `description` and `merchant`, signed amount bounds,
  and a warning on every search.
- **`category`** matches the effective category (override, else source, else `UNCATEGORIZED`) through
  one shared expression, which `money_summary`'s category grouping now also uses. A group key passed
  back selects exactly the transactions that group counted. A category no live transaction carries
  is refused, naming the categories that exist.
- **`min_amount_minor_units` / `max_amount_minor_units`** bound the signed amount inclusively. A
  transposed range and an integer SQLite cannot bind are refused as `invalid_argument`.
- **One `TransactionFilter` value** feeds both the SQL predicates and the cursor fingerprint, so a
  cursor resumes only a request with the same filters. An unfiltered request hashes exactly what it
  did before, so cursors issued by the previous build still resume.
- The filters narrow the rows and `truncation.matching`, never the coverage figures. The cursor
  wording in the tool schema, the truncation remedy, the envelope reference, the client guide and
  the contract now say "the same window, account and filters".
- **VRF-025 and VRF-026** re-raise VRF-020 and VRF-021 on the term their acceptance set.

**What changed (Chunk 02):**

- **`search`** matches its text as a literal, case-insensitive substring of `description` or
  `merchant`, through `instr`, so `%` and `_` mean themselves. Case folds over Unicode through a
  function registered on read-role handles, because SQLite's own folding stops at ASCII. A blank or
  over-200-character term is refused. The fold's cost was measured at about 2ms per 10,000 rows
  (`mcp-search-latency-2026-09-14.md`).
- **`search_is_literal`**, a new request-scoped warning, rides every answer a search ran for, empty
  or not. Its detail names the term and the whole request's match count, so it reads the same on
  every page. An unreadable store's answer carries only `partial`, since no match ran.
- **Every claim that filtering is impossible is retired:** the server instructions, the envelope
  reference's "cannot answer" list (which now names what `search` cannot do), the contract's
  descope note and AC-9.1's build status. The warning is defined in the contract table, the
  warnings reference and the client guide. A go-red case pins the retired primer claim.
- **From Chunk 01's review:** the category existence check now reads through the row query's own
  predicates and join, so a category only a superseded generation carries is refused. The SQLite
  integer refusal is tested on all four bounds.
- **VRF-027** asks whether a model in a real client uses `search` and reports a miss as a literal
  miss rather than as "no refund".

## 2026-09-13: A stopped balance names the account that took it over, and a disclosure names only what the request reaches

<!-- prawduct: scope=investments-v1 | release=v0.1.0 -->

**Why:** a real client read of the sandbox store (VRF-023) found two warnings stating something
false. `account_no_longer_active` on `balance_history` said a net worth read across a stopped
account's last day "moves by that account's last balance". All 14 stopped accounts there were
re-linked at the same balances, so the net worth is flat. And the superseded-generation `rule-applied`
disclosure named every span in the store, so `query_transactions(account_id=21)` named accounts 1–5.

**What changed (Chunk 11):**

- **`account_no_longer_active` names each stopped account's successor.** A successor shares the
  stopped account's `lineage` identity partition and currency and was first captured strictly later.
  Each stopped account is named with that account, its first day and its first balance. Across the
  handover net worth moves only by the difference. A successor two stopped accounts share, or a tie,
  claims no handover. A net-worth day between a stop and its successor is named with the balance it
  leaves out. The figure that stopped counting stays whole and still agrees with
  `coverage.not_active_balance_minor_units`. The move claim is made only of the unreplaced part.
- **The superseded disclosure is request-scoped.** It names only spans on the request's account whose
  range meets the requested window. The exclusion still sees every span, so an answer's totals are
  unchanged. This behaviour predates the branch and is fixed here.

## 2026-09-13: An investment account's trades and positions count as coverage

<!-- prawduct: scope=investments-v1 | release=v0.1.0 -->

**Why:** an account whose only activity is trades and positions read `transaction_count` 0, and both
listings raised `accounts_without_coverage` for it. That told an agent to distrust an account whose
data is in the store (#107).

**What changed (Chunk 10):**

- **`list_accounts` and `get_coverage_report` gain `investment_transaction_count`** (soft-deleted trades
  excluded, 0 not null) and **`holdings_as_of`** (the newest captured holdings day, nullable; a refused
  position is not a capture). Both come from per-account grouped subqueries, never a widened join.
- **Both listings raise `accounts_without_coverage` only for accounts with nothing in any feed.**
  `query_transactions(account_id=N)` and `money_summary` keep the transactions-feed predicate, because
  an empty answer from either really is "no data for this tool".
- **The envelope reference's cannot-answer list now says trades are counted per account and served as
  rows by no tool.** Its guard test was re-aimed at that claim, and it can still fail.
- `list_holdings` and `balance_history` moved out of `query.py` into their own modules (#115, Chunk
  09). Nothing a client sees changed.

## 2026-09-13: Net worth that says what stopped counting, and a holdings total that is not a second balance

<!-- prawduct: scope=investments-v1 | release=v0.1.0 -->

**Why:** a net-worth total is the most believable wrong number this product can emit, and it goes
wrong silently in two ways. It can count an investment account twice, once as its balance and again
as its positions. And a non-active account's frozen balance can enter or leave the figure with
nothing said. Chunk 07 stated that exclusion without the figure, and the lifecycle norm requires the
figure.

**What changed (Chunk 08):**

- **Net worth over time, on the owner's ruling.** An account no longer active counts through its last
  capture and not after. `account_no_longer_active` names each such account's last day and signed last
  balance, plus the per-currency count and sum that stopped counting. The alternative, carrying the last
  balance forward, was measured first: on the sandbox store, 14 relinked accounts' last balances
  equal their replacements', so every later net worth would read exactly 2×. The ruling is recorded
  at the edge of the lifecycle norm in `api-contract.md` and under AC-12.8.
- **`list_holdings` carries `totals`**, the owner's choice over settling the ruling with no total. It
  has one entry per currency (`positions`, `market_value_minor_units`, `not_active_positions`,
  `not_active_market_value_minor_units`), every key present and zero where nothing qualifies.
  Positions on non-active accounts stay in the total, with their count and value stated beside it.
  The total decomposes balances that net worth already counts, and the schema, the tool description
  and the contract all say never to add it to one. `_output_schema` now takes each tool's own totals
  schema under one shared `totals` description.
- **The double-count guard.** Net worth reads the balance series alone. A test and a property hold it
  unmoved by any positions, and a go-red case that adds the positions in goes red.
- **Five review findings carried from Chunk 07:**
  - **R-1:** the balance-history cursor is walked through the MCP boundary, with cross-tool refusal.
  - **R-2:** completeness is judged for every currency on every captured day.
  - **R-3:** "stopped" means the last investments attempt archived no holdings reply, and a
    left-out account is judged against the newest archived reply's own day, so a sync crossing
    midnight UTC names nothing.
  - **R-4:** a position's day is claimed across `holdings` and `refused_holdings` at write time
    (`DERIVATION_VERSION` 11), so `list_holdings` no longer tie-breaks two tables.
  - **R-5:** `SeriesCursor.position()` delegates to `series_position`.

Run `bankmachine store rebuild` after pulling this, so rows derived at version 10 converge on one
record per position per day.

## 2026-09-13: Net worth over time, and each account's balance history, from one series

<!-- prawduct: scope=investments-v1 | release=v0.1.0 -->

**Why:** the server had no answer to "what was my net worth last month". Only the latest balance per
account was served, and the primer told an agent the question was unanswerable, although every
day's capture has been kept in `balances_daily` since build step 3.

**What changed (Chunk 07):** `balance_history` is built and keeps its name. The owner accepted the
cost of a name that is harder to find for the net-worth question; the tool description opens with
that question to compensate. One read returns the series two ways under one strict row shape: a row
per account per day a balance was captured, and a net-worth row per day per currency, marked by a
null `account_id`. Assets and liabilities split by `balance_class` rather than by the sign of the
balance, so an overdraft is negative assets. `net = assets - liabilities` holds at both levels.

- **A net-worth row is given only for a complete day.** Every account it counts must have been
  captured that day. Otherwise the row is withheld and named under `rule-applied`, and the account
  rows for that day remain.
- **Which accounts a day counts.** An active account counts from its first capture onward, so a
  connection that stopped syncing withholds every later net worth instead of dropping out of it.
  An account no longer active counts only through its last capture, and `account_no_longer_active`
  says so. Both refine the owner's option ("between its first and last capture") and are recorded
  in the plan as vetoable.
- **A day with no capture is absent at both levels.**

The tool is windowed, capped and paged like `query_transactions`, which meant the envelope had to
learn which series it describes:

- `resolve_window` clamps against the days balances were captured, not the transactions' span.
- `SeriesCursor` is a keyset over the series order, with its own scheme tag, so each tool refuses
  the other's cursor.
- `Truncation` names what it counts. That also corrects `money_summary`, whose `rows_truncated`
  sentence called its groups "transactions".
- `transactions_in_effective_window` rides only a window over transactions.

`find_recurring` is now the one unbuilt tool on the wire, in the primer and in every document. The
tool-surface guard's build-status regex accepts "one is specification only". Carried from Chunk 06:
`list_holdings` now also names a stopped investments feed for an account left out of a newer
capture, rather than calling that feed working.

## 2026-09-13: A holdings answer says what it cannot vouch for, and names the positions it refused

<!-- prawduct: scope=investments-v1 | release=v0.1.0 -->

**Why:** `list_holdings` served three well-formed answers that were quietly wrong. It presented a
2021 price on a 2026 capture as a current value. It dropped any position in a unit this build
cannot denominate and left only a log line nobody calling a tool can see, so the account read as
a smaller portfolio than it was. And it said nothing about a position on an account that had
stopped being reported. Separately, the coverage surface counts the transactions feed alone, so it
reported an investment account holding positions as having no data. Nothing said which feed it
counted.

**What changed (Chunk 06):** a new request-scoped warning, `positions_not_current`, names every
account holding a position that is not a current value. It keeps three reasons apart: a price more
than four calendar days older than the day it was captured, a price date the store does not have,
and a capture older than the day the connection's transactions last landed. The four days are an
assumption the owner can override. Migration 011 adds `refused_holdings`, so a position refused
for its unit (or for stating none) is recorded where a read can see it. `DERIVATION_VERSION` 9 → 10
lets `store rebuild` fill it from the archive. `list_holdings` names each refusal under
`rule-applied` by account, security and unit, and reads an account's latest capture day across both
tables, so an account whose newest capture refused everything answers from that day. The answer
also carries `account_no_longer_active` and `roster_observed_empty` for positions on accounts in its
scope. `transaction_count`, `accounts_without_coverage`, their guidance and the client guide now say
they speak for the transactions feed; the fix that changes those row shapes stays open as #107. The
new kind reached the scope tuple, the guidance map, the contract table and the client guide
together. The new table is declared in `LATER_TABLES` beside FR-6's thirteen. The populated-store
upgrade tests track 011 and keep 010's fixture. The go-red harness has a case for each new guard,
and all 183 go red.

**Verified against the sandbox:** migrated to schema 11 and rebuilt (content changed as expected at
derivation version 10). `list_holdings` returned all 13 positions, captured 2026-09-13 at a
2021-05-25 price, and `positions_not_current` named both investment accounts with their position
counts. No `rule-applied` fired, which is correct: every sandbox position is in USD.

**Fixed after the cumulative review:** `list_holdings` read every day of holdings history and
filtered to each account's latest in Python; the latest day is now a union of both tables in SQL,
and both reads join it. The capture clause of `positions_not_current` blamed a stopped investments
feed for an account a newer capture of its own connection simply listed no position for, and for a
closed account; it now names the first as positions the account may no longer hold and leaves the
second to `account_no_longer_active`. And when two captures on one day disagreed about a
position's unit, the answer could serve the position while naming it absent; the day's first
capture now decides.

## 2026-09-13: `list_holdings` serves positions, with the date of the price each is valued at

<!-- prawduct: scope=investments-v1 | release=v0.1.0 -->

**Why:** wave 1 stored what is inside an investment account and no tool read it, so an agent asked
about positions was told the server could not answer. And the one date the aggregator puts on a
position, `institution_price_as_of`, was dropped: every sandbox position is valued at a 2021 price,
while `holdings.as_of_date` is always the sync day, so a stored row presented a years-old value as
current and nothing downstream could say otherwise.

**What changed:** migration 010 adds nullable `holdings.price_as_of` in its own module, and the
holdings deriver fills it (`DERIVATION_VERSION` 8 → 9, so `store rebuild` fills existing rows from
the archive). `list_holdings` reads each account's latest capture day, not today's and not one day
store-wide, and returns one strict row per position: security identity, quantity as exact decimal
text, value and cost basis in minor units (cost basis present and null when unknown), currency,
capture date, price date (served null, never coalesced to the capture date) and the account's
lifecycle. No total. The tool left `UNBUILT_TOOLS`, the primer stopped calling positions
unanswerable, and the contract, client guide, requirements and README now count six built tools.
The populated-store upgrade tests now track migration 010, and their newest-version check reads the
highest version rather than the last row, which sorts `(10, …)` before `(9, …)`.

**Verified against the sandbox:** migrate, rebuild and sync, then `list_holdings` returned all 13
positions across both investment accounts with `price_as_of` 2021-05-25 beside a 2026-09-13
capture date.

**Also fixed, found by re-proving the norms red:** the go-red harness reported AC-4.1 (one
connection's failure never aborts another) as unguarded. The guard was fine; the case was not. Its
anchor, the `_degrade` return line, also closes the expired-login handler just above the broad
catch, and the harness breaks only the FIRST occurrence, so it mutated a path the named test never
takes. Ten of the harness's cases anchored on text that occurs twice; the other nine happened to hit
the right copy. Every anchor is now widened until it names one place, the harness refuses an
ambiguous anchor as `AMBIGUOUS`, and the sub-second reach test fails on one.

## 2026-09-13: Stop telling every agent that holdings are not stored

<!-- prawduct: scope=investments-v1 | release=v0.1.0 -->

**Why:** the envelope reference's "what this server cannot answer" said "No security, quantity or
cost basis is stored" — true before wave 1, false after it, and served to every agent that reads
the reference. Found while running VRF-014 against the build under test.

**What changed:** the bullet is rewritten for the finished investments surface, at the owner's
direction, since nothing ships to an operator before `list_holdings` does. It names what stays
unanswerable once positions are served: tax lots, which the aggregator sends and only the verbatim
response archive keeps, and the buys, sells, dividends and fees behind a position, which are stored
and read by no tool — so an investment account empty in `query_transactions` may still have traded. Until the tool
registers, the bullet names `list_holdings` while `UNBUILT_TOOLS` still lists it; a comment at the
site says so. Two tests hold the claims to the code in both directions: the tax-lot sentence against
every table and column name, the unserved-trades sentence against the SQL every registered tool
actually executes over a seeded store.

**Plan:** chunks 05 and 07 each gained the half of the wire text no test forces — the primer's
hand-typed "cannot answer" phrases, and the matching `_CANNOT_ANSWER` bullets.

**Also:** VRF-014 verified on 2026-09-13 across all six steps, with the grader caveat on steps 2-4
and the corrected step 5 and 6 procedures recorded in the entry.

## 2026-09-12: Wave 1 verified against the real sandbox, and what that found

<!-- prawduct: scope=investments-v1 | release=v0.1.0 -->

**Why:** three operator-verification entries (VRF-017, VRF-018, VRF-019) stood between wave 1
and its PR, and each names a claim no test in this repo can make: that the whole path works
when the parts are joined by the real command, against the payload the aggregator actually
sends. They were drained end to end. The pass found one defect, and it was in the surface the
entries exist to read through rather than in anything the tests cover.

**What changed:**

- 🔴 **`sync shell` was masking every high-precision position, and now is not.**
  `holdings.quantity` and `investment_transactions.quantity` are exact decimal TEXT — a
  fractional share carries more precision than a scaled integer could hold — and the shell's
  account-number rule blanks any run of eight digits, so a sandbox Bitcoin holding of
  `0.00293644` rendered `0.****3644` and a sale of `-430.80867509953123` rendered
  `-430.****3123`. Nothing was wrong with the stored value; the operator simply could not read
  the number the column exists to state, which is the one reading VRF-017 asks for. A cell
  that is wholly digits-point-digits is now rendered verbatim. **The decimal point is the
  entire exemption**: an account number, a digest and a token are each one unbroken run, so
  none can match it, and a quantity spelled without a fractional part is still masked. The
  premise this corrects — that every arithmetic quantity here is an INTEGER, so redacting text
  protects account numbers and costs nothing — was true until investments added a column that
  is both. `boundary-patterns.md` carries it as a fourth boundary.
- **Both investments report lines moved out of the healthy branch.** `_pull_investments` runs
  before the page loop, so a completed window sits behind a connection the pager then reports
  as degraded or as still materializing — and appended to the pages sentence, `positions
  recorded` and the retired-transaction count said nothing at all in exactly those runs. For
  the retired count that is the invisibility it exists to end: a soft delete leaves no trace
  in a page count.
- **A re-link no longer leaves a success stamp beside the domain rows it cleared.**
  `cli/enroll.py` deletes every `sync_state` row when an item is replaced; the health surface
  publishes an empty domain list as *nothing has ever been attempted* and published
  `connections.last_success_at` beside it, so a client told to prefer the domain stamps found
  none, fell back to the connection's, and read a history being refetched from zero as
  current. The stamp goes with the rows.
- **One statement of the three conditions the connection caveats fire on.** `_domain_caveats`
  says a thing only where the connection has not already said it, and both passes now read a
  `_ConnectionFreshness` record built once. Matching on the caveats actually emitted would not
  have been faithful — `partial` carries three unrelated connection-level meanings, so a
  connection whose granted window is merely unmeasured would have suppressed a domain that has
  genuinely never landed.
- **`store rebuild`'s refusal names its third cause.** It named an impure deriver and a pruned
  archive, and neither fits an investments run that archived a complete window and stopped
  before concluding its removals — which leaves the replay retiring rows at an instant the
  live store never recorded. That failure is permanent and its only escape was
  `--accept-content-change`, a flag documented for something else. The message now names the
  case and says which columns to compare. **The divergence itself is filed, not fixed** (#113):
  closing it is a choice between three shapes with different lock-in, and the cheap one is
  blocked by `apply_response`'s deliberate archive-then-derive split.

**Filed rather than answered:** #113 (the reconciliation's atomicity, above), #111 (a sync
prints 157 log lines to 2 of report, 150 of them two rounding notices repeated per row — a
documented decision whose volume against a cursorless feed was never measured), #112 (an
all-digit CUSIP will render masked; unmeasured, the sandbox sends null). #93 was updated
rather than answered: the same `type(exc).__name__` now reaches a newly published contract
field, and which vocabulary that field carries is the owner's call.

**Not drainable, and the recorded blocker was wrong:** VRF-014 needs an MCP server on the
build under test, and an MCP server outlives `/clear` — the reachable one answers
`build.commit: efd64ad` where this checkout answers `41b58af`. The merge it was said to wait
on never bore on it. Only relaunching the client moves it.

**Verified:** VRF-017/018/019 recorded verbatim with their readings, including the step that
failed and its re-read after the fix. Reviewed by `rev-20260913T021056Z-81d120ad` (0 blocking,
5 warning, 8 note — four fixed, one filed, the rest accepted), verified clean across three
`verify-resolutions` rounds, the last of which caught a new test that was passing off the
run-level summary rather than the connection line it named.
