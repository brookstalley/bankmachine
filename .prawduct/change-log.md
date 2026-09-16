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

## 2026-09-16: The suite gets its own keychain, and stops spending nine minutes inside securityd

<!-- prawduct: scope=fast-keychain-suite -->

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
unaffected: the original search list is kept and still searched. 🔴 That write is **not destroyed** —
neither teardown path deletes the keychain, because for this product the thing that could land there
is the datastore key, which `secrets.py` documents as unrecoverable. A leftover is removed at the
START of a later run, announced, so the loss is attributable to a command the operator just ran
rather than happening invisibly at the end of the previous one. The temporary keychain is
created with a random password rather than an empty one, so a secret that does land there before
deletion is not sitting in a keychain anyone can open. The window is now ~56s rather than ~542s.
`BANKMACHINE_NO_KEYCHAIN_SWAP=1` opts out entirely.

## 2026-09-16: The engine is MIT licensed, and the guard learns that an author is not an operator

<!-- prawduct: scope=mit-licence -->

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

<!-- prawduct: scope=reconciliation-and-status -->

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

<!-- prawduct: scope=pytest-xdist -->

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

<!-- prawduct: scope=investment-activity-e2e -->

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

<!-- prawduct: scope=investment-activity-e2e -->

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

<!-- prawduct: scope=investment-activity-e2e -->

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

## 2026-09-12: A rebuild stops resurrecting the transactions the source dropped

<!-- prawduct: scope=investments-v1 | release=v0.1.0 -->

**Why:** AC-5.2 says the normalized tables are rebuildable from the raw responses alone, and
with investments in the store that had become false in one direction no test could see. A
removal on `/investments/transactions/get` is a row's ABSENCE from a window that came back
whole — the feed sends no removal signal of any kind *(`api-notes-plaid.md` §26)* — and a
deriver sees one page. So replaying the archive through the derivers re-upserted every row
that had ever appeared and cleared `removed_at` on each. The rebuilt store held transactions
the synced store had retired, every total silently grew, and `store rebuild` reported
success. `operational-spec.md` has been telling the operator to rebuild once this build step
landed; that instruction was a claim, and nothing asserted it.

**What changed (Chunk 04):**

- **A rebuild now runs each window's reconciliation again, from the archive.**
  `connector/plaid/window.py` reassembles a window from the `request_context` its pages carry
  — the only record of the question a page answered, since the reply does not echo the window
  back — and calls the same `store.investments` reconciliation the sync calls, with the same
  evidence. `store.rebuild` gained one seam for this: `ReplayPass`, for a fact that is a
  property of a SEQUENCE of responses rather than of any one body.
- 🔴 **Re-run at the page that CLOSED each window, not once over the finished tables.** Once
  every page is replayed, each surviving row carries the id of the last page it appeared on —
  so an early window's reconciliation run at the end would find the rows that only arrived
  later absent from it and retire every one of them, a conclusion no run ever reached. The
  replay reproduces the sequence of window conclusions, which is the only thing that
  reproduces the store.
- **A window whose opening page the archive no longer holds concludes nothing**, and says so.
  Reconciling on the pages that survived would soft-delete every row that sat on the ones that
  did not, on the code path that believes the window was whole. The rebuild's content digest
  then reports the removal it could not reproduce, which is a refusal an operator can act on
  rather than a deletion nobody sees.
- **A page that does not record its window refuses the whole rebuild.** Skipping it would
  leave that window's reconciliation unrun and every retired row back in the totals, under a
  rebuild that reported success — the silent incompleteness that still adds up.
- 🔴 **The measured history range moved out of the store function and into the sync.**
  `record_investment_transaction_window` now returns the range it measured; the sync command
  records it, in the same transaction as the removals it was measured after. A replay must not
  stamp a domain's progress: the sync stamps `last_success_at` from the clock once both feeds
  are in, so a rebuild that rewound `sync_state` to the archive's instant would make the
  digest refuse a rebuild that had reproduced every row correctly. It is the same rule that
  took `last_success_at` out of the derivers in Chunk 03, and it matches how the transactions
  domain has always recorded its own range.
- **One reader for what a page says about its window**, used by the sync loop and by the
  replay. The offset a page was fetched at is the count of rows that came before it, so both
  reach the same exhaustion verdict without either trusting the other's arithmetic. The
  exhaustion predicate itself is one function in `store.investments`.
- **The sync tests' fake client now archives the real `request_context`**, built by the
  production formatter. A fake that archived `None` left the entire replay path exercised by
  nothing while every sync test stayed green.

**Verified:** new tests across the rebuild, the archived window and the sync path. Five
mutations were run against them and each reddened only the
tests that name it: dropping the replay pass, moving it to the end of the replay, dropping the
unopened-window guard, making the window reader default instead of refuse, and putting the
`sync_state` write back inside the store function. Property tests state rebuild losslessness
over generated sequences of windows and the valuation rounding bound, the half-even boundary
and sign symmetry across every minor-unit width this build knows. `VRF-019` is queued for the
rebuild on the operator's own sandbox store, which is the claim no fake can make.

**What the review round changed.** Zero blocking findings; these are warnings taken as
defects rather than accepted.

- 🔴 **An investments shortfall no longer withholds the CONNECTION's freshness stamp.** It was
  reported through `stopped_short`, the transactions pager's flag, which `unfinished` reads to
  decide `history_complete` -- the only thing that stamps `connections.last_success_at`. So a
  short window made the whole connection read stale, the per-domain caveat was then suppressed
  *because* the connection read stale, and the operator was pointed at the connection for a
  condition belonging to one domain. Exactly the mis-attribution Chunk 03's decision exists to
  prevent, reintroduced through the exit-code plumbing. The window has its own flag, its own
  report line, and its own path to exit 75.
- 🔴 **A window that states no total is asked for once, not five hundred times.** Routing the
  loop's exits through the shared exhaustion predicate dropped the unstated-total exit: a
  predicate that answers "no" for its own good reason is not the same as an exit. Measured at
  4.4 seconds of fake aggregator calls for one page of one row.
- **An attempt that came back short now records that it did not fail.** `last_error_code` is
  published as "what the last attempt failed with", and with nothing written for an attempt
  that neither failed nor finished, a previous run's code stood on a row whose last attempt had
  come back clean.
- **The investments-failure line no longer prints under a connection reported as degraded**,
  where "no re-authentication is needed" sat two lines under the instruction to
  re-authenticate. Both failures in one run is an ordinary shape, and no test covered it.
- **`replay_passes` is required, not defaulted.** `boundary-patterns.md` records `derivers`
  losing its default because one reachable outcome made omission a runtime failure; omitting
  the passes is worse, because it fails silently. `no_replay_passes` is the value that says so.
- **The warning vocabulary's guidance follows the caveats that widened.** `stale`, `degraded`
  and `partial` are emitted at two scopes since Chunk 03, and their published guidance still
  described the connection -- a domain-stale caveat fires precisely when the connection's stamp
  is fresh, so an agent following the old text reported the figure as of today, which is the
  conclusion the caveat exists to prevent.
- **`boundary-patterns.md` § Derivation Seam gains the clause `ReplayPass` needs**, so the next
  windowed feed's reconciliation is not written into a deriver and lost on every rebuild.
- **A window that came back complete and empty retires everything in it** -- the widest removal
  this feed can express, and it had no test on either the sync path or the replay.

**Found and filed, not fixed:** `_upsert_account` takes the last-REPLAYED observation where
`_upsert_security` beside it takes the latest one (**#108**), so an archive whose `received_at`
order disagrees with its insertion order would make a rebuild unreproducible. Not reachable
through today's sync path — `received_at` is monotonic — and the failure direction is a refusal
rather than drift, so it is filed rather than folded in.

**Two older deferrals these records called "filed" were in no queue at all**, and the review
round is what found them: capabilities are never refreshed after enrollment, so a connection
enrolled before its institution gained the product never starts pulling it (**#109**), and
`cancel_transaction_id` has no settled ledger meaning (**#110**). Every "filed" claim in this
log and in the build plan now carries its id, which is what makes the word checkable rather
than asserted. The round also closed **#43**, whose revisit trigger was "build step 5 —
investment sync scoping": the investments write path is upsert-shaped rather than
graph-shaped, so it stays on SQLAlchemy Core — and the relational case the item named is not
modelled at all, since the feed sends tax lots and this build drops every one.

## 2026-09-12: A connection is no longer one stream, and the health surface says so

<!-- prawduct: scope=investments-v1 | release=v0.1.0 -->

**Why:** AC-4.4 names silent staleness as this system's primary failure mode, and a second
sync domain that no surface reports is silent staleness with a new cause. Two domains now
advance on their own schedules, but every read of `sync_state` pinned
`domain == 'transactions'` — correct while there was one, and blind the moment there were
two. A connection could sync nightly, report `active`, carry a fresh `last_success_at`, and
not have returned a position since August, with nothing anywhere saying so.

**What changed (Chunk 03):**

- **An investments failure is recorded against the DOMAIN, not the connection.**
  `connections.status` means credential health; a `PRODUCT_NOT_READY` on a product the Item
  never initialized is not a statement about the login, and marking the connection degraded
  for it sent the operator to `connections reauth`, which cannot fix it. A credential error
  reached through an investments call still degrades the connection, because that one *is*
  about the login. The run's exit code is still `1`: a connection that ran and found a
  problem, whichever column recorded it.
- **The failure is written where it is caught**, closing the residue Chunk 01 left. The page
  loop returns early while a first sync is still materializing its history, so a failure
  carried past it reached no column at all on exactly the run an operator most needs it.
- **`sync_state` has one writer.** `store/sync_domains.py` owns the `(connection, domain)`
  row for every caller — attempt, success, failure and measured range — each an insert-or-
  update, because a bare `UPDATE` against an absent row reports success and writes nothing.
- **The investments domain is stamped current only when BOTH its feeds are in.** Positions
  and the investment-transaction window share one domain key, and both derivers were
  stamping `last_success_at` per response — so a connection whose holdings landed while its
  window came back short read fresh. The stamp moved to the sync command, which is the only
  caller that knows the pull completed. A rebuild replaying an archived body therefore also
  cannot forge a freshness claim out of a year-old capture.
- **`get_pipeline_health` rows carry a `domains` array** — per domain: `last_attempt_at`,
  `last_success_at`, `last_error_code`, `last_error_at`, `history_starts`. Still ONE row per
  connection, which is the point: joining `sync_state` unfiltered would return a row per
  domain and every consumer counting connections would count each one twice. AC-4.5's two
  absences stay apart — no entry means the domain has never been attempted; an entry with a
  null `last_success_at` means it has been attempted and has never landed in full.
- **Warnings ride the success path per domain**, and only where the connection-level
  warnings do not already say it. The test is a property rather than a list of domains to
  exempt, so a third domain needs no new exemption and none can be forgotten.

**Tests changed, and why they are not weakened:** two deriver tests asserted that the
investments derivers stamp `last_success_at`, and one CLI test asserted that an investments
failure degrades the connection. All three pinned behaviour this chunk's recorded decisions
change, and the CLI test's own docstring said so ("recording the failure against the
investments domain instead is the next chunk's work"). Each was rewritten to assert the new
contract rather than deleted.

**Deliberately not done:** an account whose activity is investment transactions still
reports `transaction_count` 0 and `uncovered` on `list_accounts` and `get_coverage_report`.
Reporting investment coverage per account changes the meaning of a published field on two
row shapes, so it belongs with the tools that answer about positions (wave 2) rather than in
the sync work that created the rows. Recorded in the code at the site and filed as
brookstalley/bankmachine#107.

## 2026-09-12: Investment transactions, and a removal signal that had to be derived

<!-- prawduct: scope=investments-v1 | release=v0.1.0 -->

**Why:** AC-3.3 was the second of FR-3's two unimplemented acceptance criteria. Holdings say
what an account holds *today*; nothing said what moved it there. `investment_transactions`
had been created, constrained and empty since build step 1.

**What changed (Chunk 02):** `/investments/transactions/get` is pulled for the same
capability-gated connections as holdings, over `config.history_days` — the one configured
window — and paged to exhaustion by `options.offset` against the stated
`total_investment_transactions`. `derive_investment_transactions` writes the rows with the
identity and provenance `transactions` already carries, reusing `_exact_quantity` for
quantities and `_operator_signed_amount` for amounts rather than writing a second
normalization beside them.

**The `verify-api` step rewrote three of this chunk's deliverables, which is the argument for
running it first.** `api-notes-plaid.md` §26 carries the measurements; all three are absences,
and none would have been found by more reasoning:

- 🔴 **There is no settlement date.** `investment_transactions.settlement_date` gets nothing
  from this feed and stays null; the manual importer is now its only possible writer. Filling
  it with the trade date "for completeness" would manufacture a settlement no institution
  stated, and every later reader would take it for one.
- 🔴 **There is no removal signal of any kind** — no `removed` list, no tombstone, no flag.
  This is a windowed read, not a delta like `/transactions/sync`, so the plan's "handled the
  same way" had nothing to attach to. What makes the never-hard-delete norm satisfiable
  anyway is that the window comes back **whole**: a stored row inside the requested window
  whose id did not return has gone away, and that absence is the signal.
- 🔴 **There is no cursor.** Paging is offset/count, so the far-end idempotence
  `TRANSACTIONS_SYNC` relies on — and AC-2.5's crash-resume with it — is unavailable. A run
  that stops early leaves no partial progress and re-reads the window from its start next
  time, which converges only because every write is an upsert on the aggregator's own id.

**The removal reconciliation refuses to run on a window it did not see whole, and that
refusal is the whole safety argument.** A run stopped by its page ceiling, a transport
failure or a kill has fetched a *prefix*: every row it never reached is absent from what it
saw, and reconciling on that would soft-delete real history while leaving a store that looks
exactly as it should. So exhaustion is **derived inside**
`store.investments.record_investment_transaction_window` from the row count against the
stated total, rather than passed in as a flag each caller asserts separately — and a window
offered as complete with no archived pages raises instead of letting `NOT IN ()` match every
row and retire the lot. The two tests that matter assert the invariant rather than its
causes: a bounded run retires nothing and records no range.

**What is deliberately NOT here, with the measurement as the reason.** No shortfall warning is
derived from the returned range. Nothing states the granted window and the response does not
echo what was asked, so the only observable range is the span of the rows — and that answers
*when was this account last active*, not *how much history was granted*. An account granted
two years with no trades in the first eighteen months returns the same narrow span as one
granted six months, so a shortfall read off row dates would report every quiet brokerage as
truncated history on every run. AC-3.3 asks to "record the actual date range returned", and
that is what is recorded: computed from the rows, and only at exhaustion, because the rows
arrive newest-first and the earliest date is on the last page.

`cancel_transaction_id` is filed as **#110** rather than guessed. It was null on all 100 recorded rows,
and its ledger meaning is itself unsettled — in accounting a cancellation usually keeps both
rows so they net to zero, so treating it as a tombstone would change the arithmetic.

**A debt this chunk created and names rather than leaves:** because removal is a property of a
whole window and a deriver sees one page, replaying archived pages re-upserts every row that
ever appeared and **clears `removed_at` on each** — silently resurrecting every soft delete.
Chunk 04 owes the reconciliation re-run at the end of a rebuild, and the assertion that a
rebuild reproduces a soft delete rather than undoing it; the window's `request_context` is
archived carrying its offset and bounds for exactly that. Until then AC-5.2 is false in the
one direction no test here can see.

**Also fixed, and it was not a new defect.** `check-no-personal-data.sh` greps *tracked* files
in worktree mode, so Chunk 01's freshly recorded fixture was invisible to the gate that
passed it and became visible the moment it was committed — the "gate green" in that chunk's
handoff was blind rather than green. The aggregator's canned sandbox fund name carried a
roster token; the display name is sanitized in both fixtures, no token moved and no path was
exempted, so the guard keeps full strength. Nothing in code, tests or docs reads a security's
display name, and the structural facts the fixtures are the oracle for are untouched.

## 2026-09-12: What is inside an investment account, recorded for the first time

<!-- prawduct: scope=investments-v1 | release=v0.1.0 -->

**Why:** the schema for investments has existed since build step 1 and every one of its
tables was empty. `securities`, `holdings` and `investment_transactions` were created,
constrained and never written to; `connections.capabilities` has recorded which connections
could serve investments since build step 3 and **nothing read that column.** An operator
holding a 401k saw a brokerage account with a balance and nothing whatsoever about what was
inside it, and AC-3.2 was one of two acceptance criteria in FR-3 with no implementation.

**What changed (Chunk 01 — holdings, end to end):** `/investments/holdings/get` is pulled
for a connection whose recorded capabilities name the investments product — **capability,
never institution** — and derived into `securities` and `holdings`, with a `sync_state` row
at `domain = 'investments'` written inside the derivation's own transaction. The pull is
made before the transactions page loop, because that loop returns early while a backfill is
still materializing and positions share neither its cursor nor its window.

The first-capture-of-the-day rule is now **one implementation shared** by the balance series
and the holdings series rather than two written from the same paragraph: same comparison of
`(captured_at, raw_response_id)`, same refusal to replace a row the operator imported. Two
series disagreeing about what "first" means would have surfaced as a rebuild that could not
reproduce its own content.

**Measured before it was written.** The plan's `verify-api` step ran against the live sandbox
first, and the capture (`api-notes-plaid.md` §22-24, fixture committed) moved three things
the field mapping had reasoned about:

- **A position carries no date of its own.** The only date on a holding is the *price's*, and
  in the sandbox it is four years old. So `holdings.as_of_date` is this system's capture date,
  the same one the balance series is keyed on — deriving it from the price would have filed
  today's observation under 2021.
- **Sub-cent valuations are ordinary**, not an edge: four of thirteen positions carry more
  precision than the cent, so the half-even rounding runs on a third of a real payload.
- 🔴 **Holdings decompose an account's balance and do not have to add up to it.** The
  aggregator's own sandbox has one investment account reconciling exactly and another off by
  6%, with no margin loan to explain it. Net worth must therefore read one series or the
  other and never sum them — the guard wave 3 owes — and no reconciliation between the two is
  a test this product can write.

**What is NOT here, stated rather than left to be found:** an investments failure still
degrades the whole **connection**, because recording it against the investments *domain* —
so that a product error stops sending the operator to `connections reauth` — needs the
per-domain error columns and the health surface that reports them, which is the next chunk.
What it no longer does is cost that connection its transactions: the pull is carried rather
than raised, the page loop runs, and the degrade lands after the history is in. A credential
error is re-raised on the spot instead, because that one IS about the login and the operator
needs the repair printed.

The gate itself is silent by design — a connection that cannot serve investments simply does
not call — so the run's report names the connections that did, and an unreadable
`capabilities` record is logged rather than read as "cannot".

**The holdings reply's own account roster is derived**, through the one account deriver every
other endpoint's roster goes through. An account that has closed or been de-selected drops
out of `/accounts/get` while its positions keep arriving, and refusing the reply for it would
roll back every other position — permanently, since the archived body replays the same
refusal on every rebuild.

## 2026-09-12: The gate runs somewhere that is not the author's machine

<!-- prawduct: scope=ci-runs-the-gate | release=v0.1.0 -->

**Why:** `scripts/check.sh` runs every declared check, and exactly one thing launched it —
the governance gate on the machine of whoever was editing. `.githooks/pre-push` does not run
it: that hook refuses leaked identity and enforces the gitflow rule for `main`, and nothing
else. **No push, from any clone, had ever been checked against pytest, ruff or mypy by
anything but the author remembering to look.** The checks were run by the honour system of
one machine, which is the same shape as the defect the gate itself was built to close.

**What changed:** `.github/workflows/check.yml` runs `bash scripts/check.sh` on a
`macos-latest` runner, on `pull_request` into `develop` and `main` plus `workflow_dispatch`.
It is a **caller**, not a second list: restating the four checks in YAML would create a
second declaration of the gated set, free to drift from the first, which is
brookstalley/bankmachine#92 rebuilt in another language.
`tests/preferences/test_ci_runs_the_gate.py` fails if the workflow stops being a caller.

macOS rather than the ~10× cheaper `ubuntu-latest`, because `project-preferences.md` records
macOS as the only supported and tested platform and `tests/conftest.py` runs integration
tests against the real OS keychain. A green tick on a platform nothing has ever verified is
worth less than nothing. The cost that buys is real — macOS bills 10× on a private repo at
~10 minutes a run — and it is what decided the triggers: the local gate covers the edit loop,
so CI observes at the point the code tries to become someone else's problem, which is the
pull request.

Also here: ruff 0.16.6 → **0.16.7**. A non-event — nothing reformatted, no rule changed — and
recorded only so the lock movement in this bundle has a stated reason.

🔴 **Two gaps left open deliberately, stated rather than left to be discovered.** A merge
commit landing on `develop` re-runs nothing, so the PR run vouches for the merge result only
while the base has not moved under it. And **CI reports; it cannot block** — requiring a
status check needs branch protection, which is paid on private repos, so `.githooks/pre-push`
is still the only thing in this project that actually refuses a push, and it refuses on
identity and gitflow, never on a red check.

🔴 **Honest confidence: as this entry is written, this workflow has never run.** Everything
about it is reasoned from documentation. `workflow_dispatch` is invisible until the file
reaches `develop` — GitHub reads the dispatchable list from the default branch only — so the
pull request that carries it is the first and only way to observe it, which makes that PR a
verification step and not merely a delivery one. The keychain step is the likeliest to be
red: `keyring` writes to the default keychain but reads through the search list, so the
scratch keychain is **prepended to the search list** as well as made default, and that line
was checked against real `security list-keychains` output locally and never on a runner.

## 2026-09-12: mypy is green, and four declared checks now run under one exit code

<!-- prawduct: scope=mypy-green-and-gated | release=v0.1.0 -->

**Why:** `project-preferences.md` declared checks that nothing ran (brookstalley/bankmachine#92).
mypy was declared and red; `ruff format --check` was declared and, on 2026-09-08 merging
`feature/sync-v1`, **seven files had drifted across two build steps that both reported "ruff
clean" at every close** — because both ran `ruff check` and neither ran the formatter's
`--check`. The two are different halves and the lint rules never reach layout.

**What changed:** `src` and `tests` typecheck under `mypy strict`, and `scripts/check.sh` runs
all four declared checks — `pytest`, `ruff check`, `ruff format --check`, `mypy` — under one
exit code, naming whichever failed. There is no `set -e`: an operator told about a type error
should not have to fix it and re-run to find the three lint findings underneath.

The JUnit report is not a side effect — **it is the durable evidence.** `test-evidence record`
builds `.test-evidence.json` from that report and consults exit status only afterwards, which
it does not store. So a script that merely exits non-zero on a red linter leaves a
session-fresh record reading `failed: 0`: the terminal shows red, `test-status` says
`current`, and the Stop gate passes — #92's defect moved one consumer along. Pytest therefore
runs first, its report always exists, and each red linter is appended to it as a failing case.

🔴 **One silent-evidence bug was found by review inside that recorder and fixed here.** A red
pytest whose report carried **no `<failure>` element at all** — exit 5, "no tests collected",
which is what one bad `-k` in `addopts` produces — recorded as `failed: 0`. The exclusion
looked obviously correct ("pytest reports its own failures"), and the test that should have
caught it asserted the buggy outcome, because its stub could only produce one of the two
report shapes. The fix is mutation-proven. The recorder now lives in
`scripts/record_red_checks.py` rather than in a `python3 -c` string invisible to every check
this project gates on, and `scripts` is in `[tool.mypy] files` — so **anything added to that
directory is type-checked from now on.**

## 2026-09-11: A re-issued roster is counted once, and the exclusion is disclosed

<!-- prawduct: scope=duplicated-roster-double-count | release=v0.1.0 -->

**Why:** `money_summary` for 2026-08 returned `outflow_minor_units: 2229892` against a true
`1114946` — **exactly 2×** — with every account appearing twice at identical figures and
`list_accounts` reporting 28 accounts for a 14-account institution. Measured on the live sandbox
store, not inferred. Nothing in the payload named it: of the four caveats returned, only
`account_no_longer_active` was in the neighbourhood, and it speaks solely about balances. The 2×
landed on transactions, where no caveat looked.

`store/lineage.py` exists to prevent exactly this and could not fire. A converging re-enroll
updates the `connections` row in place, so `lineage_id` is unchanged; the new Item nonetheless
re-issues every `source_account_id`, and `source_persistent_account_id` is NULL everywhere outside
the three TAN banks, so `_match_account` matched nothing and inserted a **second generation of
account rows** that the re-fetched history landed on. `superseded_spans` compared overlaps within
one `account_id`; the twins had different `account_id`s and the same `lineage_id`, so no overlap
was ever detected and `counts_once` constrained nothing.

**What changed:** the overlap comparison partitions on **account identity** —
`(institution_id, mask, name, account_type, account_subtype)` — rather than on `account_id`.
`SupersededSpan`, `counts_once` and `only_superseded` kept their shape, so every reader is fixed at
the one chokepoint they already route through: `query_transactions`, `money_summary` and the
coverage paths. Verified against the live sandbox store, read through `query.money_summary` on the
store file rather than through a running MCP server: 2026-08 outflow reads `1114946`, each account
appears once, `list_transactions` returns 16 rows with zero duplicate
`(description, amount, ledger_date)` groups, and a `rule-applied` caveat names accounts 1–5 with
their exact superseded ranges.

🔴 **Ordering never licenses an exclusion — `still_reported` does.** Generation order is
`(enrolled_at, accounts.created_at, account_id)`, newest first, and it is built from two
**instants** on purpose: the calendar date that reads more naturally, `last_seen_date`, cannot
separate two generations born on the same day, and a re-link happens right after the sync it
replaces. But a total order alone would declare one of two **live** accounts older and delete money
really spent. Supersession across two account rows additionally requires the older one to have
stopped being listed by its institution.
`test_two_accounts_the_institution_still_lists_are_never_superseded` is what holds that invariant.

**The grouping tuple is `tuple[str | int, ...]`, and that is load-bearing.** These tuples are
sorted, so a `None` in one raises `TypeError` out of *every* read — not a mis-grouped total, every
query down, for questions that never mentioned the account. `mask` and `account_subtype` are both
nullable and both guarded, and adding another nullable column without a guard is now a mypy error.

**This is a read-path exclusion, and it is not precedent for a write-path merge.** Grouping two
account rows as "the same account" is what #95 is parked at `stage: research` over, and its own
production evidence says a checking account and its overdraft line share a mask at the one real
institution. The distinction that made this shippable: an exclusion is disclosed and reversible,
where a merge permanently fuses two real accounts' history. The write path is untouched — duplicate
account rows are still created, and the local `account_id` still does not survive a re-link.

**What is deliberately not fixed, and is filed:** where the aggregator publishes
`source_persistent_account_id` **and** the re-link updates the connection row in place, both
generations land on one `account_id` under one `lineage_id` — no second span to compare, no caveat,
and the doubling is undetectable. Reachable only at the three TAN banks and not reproducible in
sandbox, since no persistent id exists there. Filed as **#99**; separating them needs
transaction-level identity, which `lineage.py` rejects on the record. The module docstring now
enumerates all four re-link shapes and marks that one uncovered — read it before triaging any
"totals are doubled" report. **Balances** still include the stale generation, flagged, which is the
`api-contract` include-and-flag norm working as ruled rather than a gap; this work changed
transaction aggregates only.

**[RESIDUAL RISK] A reused mask can over-supersede.** An institution that closes an account and
later issues one reusing the same last-four *and* name *and* type *and* subtype, with overlapping
activity, has the older one's overlapping rows excluded. It is disclosed by the caveat, so it is
visible. The tightening that would close it — requiring the newer generation to start at or before
the older's — was rejected because it breaks the common case of a new Item granting *less* history.

## 2026-09-11: A write on an environment nobody chose is refused

<!-- prawduct: scope=environment-guard | release=v0.1.0 -->

**Why:** `environment` fell back to `sandbox` whenever nothing selected it, and every
per-environment container is keyed on that value — `plaid:<env>` and `datastore:<env>` in the
keychain, `connection:<env>:<item>`, and the datastore filename itself. So `connector set-secret`
run in a shell that had not exported `BANKMACHINE_ENVIRONMENT=production` stored the production
secret under `plaid:sandbox`, overwriting the sandbox one and reporting success. The production
runbook documented that footgun as an expected failure mode and made the operator the guard. There
is no undo: the replaced secret is gone.

**What changed:** `Config` records **who** chose the environment (`environment_source`), and
`require_chosen_environment` refuses when the answer is nobody. The rule is *opening the
per-environment datastore under the writer lock, or mutating a per-environment keychain entry*, and
it is enforced at the two chokepoints every such write already passes through —
`store.connection._writer` and the six `secrets` mutators — rather than at a list of guarded
commands, which would be short the first time somebody adds a seventh. Refusal is exit 2 with a
sentence naming both ways to choose, and nothing is written.

🔴 **Reads are deliberately exempt.** `store status`, `connections list`, `store key export|verify`,
`sync shell` and the MCP server keep falling back to sandbox: reading the wrong environment is
visible and free to correct, and writing to it is neither. That asymmetry is what let this ship
without breaking an existing sandbox workflow.

🔴 **`store backup` is guarded too, and that is the intended reading of the rule rather than an
accident of where the check sits.** Its handle takes the writer lock to fold the WAL in. A backup
silently taken against the wrong environment is indistinguishable from a good one and announces
itself only at a restore — the one moment there is nothing left to fall back on. `store key import`
is guarded for the plainer reason that it replaces the datastore key.

**Previously-working invocations now exit 2** on a machine that never declared an environment.
That is the point, but it reaches anything unattended: a cron or launchd entry inherits no login
shell, so `source .env` no longer suffices and the value belongs in
`~/.config/bankmachine/config.toml`. The README, `.env.example`, the production runbook and
`operational-spec.md` all say so now; the runbook's sandbox rehearsal declares the environment
before its first write rather than after.

🔴 **`enroll` refuses at its very first statement**, before the datastore check, the cap
pre-check, and any aggregator call. Not where the write happens: enrollment writes per-environment
state twice and both writes land AFTER the exchange has minted a durable, billable Item, so a
refusal there would burn a real Item and discard the only handle to it — leaving nothing, not even
`connections retire`, able to remove it. `release_at_aggregator` likewise swallows the refusal
rather than raising past its documented contract, because an escape there collapses a `1` into a
`2`.

🔴 **`connections retire` refuses at its first statement too, and for the same predicate.** Its
already-retired RETRY path ran only reads before `item_remove`, and reads are exempt -- so on a
defaulted environment the Item was really removed, `delete_access_token` was then refused, and
`release_at_aggregator` swallowed that refusal by contract. The command exited 0 saying "no longer
billing" while the credential survived, which `_credential_survives` reads as *removal never
confirmed*; every later retry hit `ITEM_NOT_FOUND` and reported "may still be billing" about an Item
that was gone, and nothing cleared it. `operational-spec.md` § Configuration now carries the
membership rule -- an irreversible remote or keychain effect reachable before that command's first
guarded write -- so a third command is decided by the predicate rather than by matching these two.
The chokepoints remain the guarantee; a front guard only moves where the refusal lands.

**Also in this scope:** `child_env` neutralises `BANKMACHINE_CONFIG` the way its in-process twin
already did, so a spawned test child cannot read the operator's real config file -- which stopped
being theoretical the moment this branch started telling operators to put
`environment = "production"` in exactly that file. Both twins take an explicit override from the
caller. The README's configure section no longer calls sandbox a default that needs nothing, forty
lines above the explanation of why a write refuses without a declared environment. And `ruff format`
ran over the two files this branch already touches, which takes `ruff format --check .` green
repo-wide (half of #92).

**Also:** `bankmachine sync run --until-ready` re-runs while exit 75 says history is still owed,
returns any other code unchanged (a `1` needs a person, not another attempt), and stops at a
bounded cap still reporting 75 — so the runbook no longer instructs the operator to be the loop.
`--max-attempts` and `--retry-delay` are bounded by argparse converters, so the bounds hold whether
or not the loop is enabled, and supplying either without `--until-ready` is a usage error rather
than a silent no-op.

**`.env` is still not read**, and this change does not revisit that: a file that is silently read
is a file whose contents are silently trusted (`operational-spec.md` § Configuration). The config
file is the durable answer for anyone who would rather not export.

## 2026-09-10: An expired login is repaired in place, not re-linked

<!-- prawduct: scope=connections-reauth | release=v0.1.0 -->

**Why:** the product's only answer to `ITEM_LOGIN_REQUIRED` — its most common production event —
was *re-run `bankmachine enroll` and pick the same institution*, and that operation destroys data
silently. Reproduced on the sandbox store: the re-link mints a new item, the new item re-issues
every `source_account_id`, `_match_account` matches nothing because
`source_persistent_account_id` is NULL, and 14 accounts become 28 with the re-fetched window
landing on the new ones. **390 transactions became 784**, and `money_summary` returned exactly
twice the true outflow with no warning naming it. The discovery behind #67 established that NULL is
the rule, not the exception: the field exists at three institutions, depository accounts only.

**What changed:** `bankmachine connections reauth <id>` opens an update-mode Link session against
the connection's existing item and waits for the operator to complete it. It clears `status`,
`last_error_code` and `last_error_at`, and stamps `updated_at` as every command that changes that
row does. Nothing else on the row moves: the cursor, the granted window,
`enrolled_at`, the accounts and the transactions are all left alone, which is AC-4.3's requirement
rather than an implementation detail.

🔴 **Completion is the item's error clearing, not a public token.** `LinkSession.finished` is
derived from `public_token` so the two cannot disagree — and update mode mints no public token,
because the item already exists and nothing is exchanged. So the repair polls `/item/get` and waits
for the item to stop reporting `ITEM_LOGIN_REQUIRED`. Not for *no* error: an absent `error` key and
a null one are different observations, and a complaint update mode was never going to fix would
otherwise be waited out to the timeout.

🔴 **The item id is compared before anything is written.** Update mode is supposed to repair the
item in place; if one came back different, a second generation of ids would stand behind the
connection and the next sync would duplicate everything. The repair refuses and leaves the
connection degraded, which is recoverable, rather than marking it active, which is not. Whether the
*accounts* beneath an unchanged item keep their ids is not assertable from one call — it is
**VRF-007**, and if they move then #91's identity fallback is needed regardless.

**Two methods on the client, not one with a flag.** `link_token_create` makes `history_days`
required with no default because AC-1.2 freezes the window at enrollment. Update mode requests no
window at all, so a single method would have to accept that argument and ignore it in one of its two
modes — which is exactly how a forgotten window reaches the path where it is irreversible.

🔴 **`enroll` no longer repoints a live connection at a new item by accident.** The repair only
helps if the destroying path stops being the one an operator falls into, so the branch that would
have committed the duplication now refuses, and releases the fresh item at the aggregator on the
way out so nothing is left billing. `--relink` is the deliberate override, and it exists because
AC-1.2 makes re-linking the only way to widen the history window. A re-run that lands on the **same**
item is untouched and needs no flag — the guard is keyed on the item changing, not on the
institution already being linked, because a guard on the latter would refuse a harmless re-run and
teach the operator to pass `--relink` reflexively. A signpost before the URL lists the live
connections and points at `connections reauth`, so the refusal is the backstop rather than the first
thing an operator meets: the institution is not known until Link has been completed in a browser,
so a refusal necessarily spends that session.

**Every surface that named `enroll` as the remedy now names the repair.** A superseded recovery is
a sweep, not an edit: `docs/system-requirements.md` (AC-4.3, and AC-1.4's idempotency bound),
`docs/first-production-connection.md`, the operational spec's failure-recovery table,
`ReauthRequiredError`'s docstring, the aggregator error taxonomy's `ITEM_LOGIN_REQUIRED` gloss, the
`degraded` guidance an MCP client reads, and `sync run`'s own degraded report — which now names
`bankmachine connections reauth <id>` with the connection id already filled in, and only for an
expired login, because a line printed under every degradation is one an operator learns to skip.

🔴 **`updated_at` has one owner, and the repair is why.** `connections reauth` is the first path in the product that archives `/item/get`
against a real connection id — enrollment archives it with none, and `sync run` archives only
accounts and transactions pages. That made the item-standing deriver reachable on replay, and it
was stamping `connections.updated_at`, a column the commands that change the row stamp with the
clock. So the first `store rebuild` after a repair would have found content changed at an unchanged
derivation version, rolled back, and told the operator a deriver was impure or the archive had been
pruned — neither true, and `operational-spec.md` sends them to `store rebuild` after exactly this
repair. The column now has one owner: the commands that change the row. The deriver writes what it
derives, and a repaired connection rebuilds.

**The condition is established before the URL is printed.** The repair reads the item once up
front and refuses what update mode cannot renew, naming what the aggregator actually says. Without
that read, completion would mean "the item is not reporting an expired login *now*" — which a
healthy connection satisfies on the first look, so the command would print a URL and then report
"repaired" for a session nobody opened, and tell an operator whose item was LOCKED that the
aggregator had accepted a new login.

**A foreign item body is archived, not derived.** The identity question is settled before the
derivation runs, so a body describing some other item is archived with no connection id. Deriving
first would write another item's consent date into the column the MCP consent warnings are built
from — on the very path that then prints "Nothing was changed". The expired-login code is asked of
`errors.py`, which owns that vocabulary, rather than held a second time as a string literal in the
CLI.

**Not built here:** reconciling a store that is *already* doubled, and an identity fallback for the
institutions that give no stable account id. Both are #91.

## 2026-09-10: The sync shell's role line stops stranding its reader

<!-- prawduct: scope=shell-banner-role-line | release=v0.1.0 -->

**Why:** the banner read `writes are refused and no PRAGMA changes that`. It was complete as
authored — `that` is a demonstrative, standing for the fact that writes are refused — and it still
read as cut off, because the same word parses just as readily as a conjunction and leaves the
sentence waiting for a clause that never comes. On the one line naming the guarantee an operator is
trusting, that is not a style quibble: a reader who doubts the sentence has reason to doubt the
guarantee.

**What changed:** `writes are refused, and no PRAGMA re-enables them`. The comma closes the first
clause before `and`, and naming writes outright states the property instead of pointing at it.

**How it was found, and what that says:** operator verification **VRF-001**, whose first check is
that the banner reads to a human. Every keyword that check names was present, so a scan for them
would have passed — it took someone reading the line as a sentence. The automated suite could not
have caught it, and did not: 🔴 **no test asserted this banner at all.** Three now do.

🔴 **And the guarantee is now written once.** The shell stated it on three surfaces — the banner,
`--help` and `.help` — in three separate literals, so this fix initially reached two of them and
left `--help` still ending on `changes that`: the same defect, the same sentence, one `--help` away.
A `READ_ONLY_SENTENCE` constant now carries it, asserted at every surface that renders it, which is
what makes the next rewrite unable to reach only some of them.

The remaining guard checks that the sentence does not end on one of five words that commonly read
as opening a clause. 🔴 **That is a heuristic and is labelled one** — it cannot know whether a
sentence strands its reader, and a rewrite ending on `so` or `while` would pass and still strand.
What protects the sentence is that there is only one of it. An earlier draft of this entry called it
a property test, which would have been the enumeration-shaped guarantee this repo has already been
bitten by (`learnings.md` § Guarantees by construction). Both guards seen red.

**Filed as #89.** The first diagnosis in the session that found it said the string was *truncated in
the source*; `git log -L` showed it byte-identical to its only commit. The symptom was real and the
diagnosis was not, which is why the issue records both.

## 2026-09-10: A datastore can be backed up at any schema version

<!-- prawduct: scope=backup-at-any-schema-version | release=v0.1.0 -->

**Why:** `store backup` opened through the ordinary writer factory, so it inherited the check that
stops a process serving a schema version it does not recognize — and a store already sitting at such
a version could not be copied at all. The documented upgrade procedure worked around this by
ordering the backup before `git pull`, taken with the old build. What it could not cover is the
order being reversed, which `git pull` as muscle memory produces and which every development
checkout produces on its own. The escape the spec offered for that case was `cp`, which the same
document marks 🔴 do-not-use and disproves with a measured case: a source with a 2 MB hot WAL holding
300 rows produced a `cp` copy short of every one of them. `balances_daily` is the series no
aggregator backfills, and a dropped WAL takes the newest of it.

**What changed:** `store backup` opens through a new `copying_writer` — the writer lock, no schema
check — and verification reads the copy back with the reader's existing `require_supported_schema=False`.
The command names a copy whose version this build cannot serve, so a sound backup is not mistaken
for a broken one when `store status` calls the restored file unhealthy.

🔴 **The note carries the remedy for its own state, not one sentence for both.** Migrations are
forward-only: a copy BEHIND this build is migrated forward by `store init`, and a copy AHEAD of it
is not — telling that operator to run it sends them to a command that applies nothing and reads as
"the fix is broken". A new `schema_problem_for()` is the single classifier, and the note takes its
action clause from `remedy_for`, the vocabulary every other unhealthy-store surface already uses.
`inspect()` asks the same classifier instead of repeating it.

🔴 **A datastore recording NO schema version is copied and reported, not refused.** That is a
migration that died between creating the file and stamping the version — the state most worth
holding a copy of, and one the old code refused only by accident, because the reader used to raise
first. Refusing a faithful copy while leaving it on disk is the zero-byte hazard in another costume.
`BackupReport.schema_version` is optional accordingly, rendered as "none recorded" at all three
surfaces: the report line, the note, and the verified-backup log record.

**The norm decision:** `architecture.md`'s fourth Direction norm governs this, and it is **ruled at
the edge rather than amended**. "Serve" means answering queries or writing derived data; `VACUUM
INTO` copies pages of ciphertext, reads no table and answers no question, so the norm's why — "it
would return plausible, structurally valid, wrong answers" — has no purchase on it. The reader, the
ordinary writer and `store rebuild` are unchanged. Amending the statement would have weakened a norm
that is correct for every other caller.

**How the exemption is held:** by discovery, not by a list. The enforcement test walks the module
for handles that skip the check and asserts the exempt set is exactly the two named roles, so a
third exemption fails the test rather than arriving quietly. Seen red through
`verify_norms_go_red.py` by making `copying_writer` enforce the check.

**Also asserted:** the WAL guarantee at an unservable version, with the `cp` negative control re-run
rather than assumed to carry over — without it the fix would buy nothing over the workaround it
replaces. And `operational-spec.md` § Rollback's "recovery is restore-from-backup" is now walked by
a test: a genuinely older store, built by applying a truncated migration list, is backed up,
restored to a fresh path, migrated forward by the ordinary runner, and its rows counted.

**Found:** while rehearsing `docs/first-production-connection.md` § 2.3 against a sandbox store at
schema 4 under a build serving 9. The limitation was already documented as a known one; what was
wrong was the workaround, not the absence of a note.

## 2026-09-10: The operator can get the datastore key out, check it, and put it back

<!-- prawduct: scope=datastore-key-escrow | release=v0.1.0 -->

**Why:** the datastore key cannot be recovered from the datastore, and `balances_daily` is the one
series no re-sync rebuilds — so a lost key is permanent data loss with no remedy. The product warned
about this on three surfaces and offered nothing to act on it.

Two consequences. The product's own instruction named a shell recipe
(`security find-generic-password … -w`) that puts the key in shell history, which **AC-10.1
forbids** — so the criterion was being routed around rather than obeyed, in the product's own
output. And there was no way to check a stored copy: shape validation catches a *malformed* key and
never a *wrong* one, because a transposed pair of hex digits is still 64 characters of valid hex and
is simply a different key. There was no moment at which an operator could learn their backup was bad
while it was still fixable.

**What shipped:** `bankmachine store key export | verify | import`, over one primitive — *does this
candidate open this datastore*. Export refuses a non-TTY stdout; `--to <path>` writes `0600` to a
path the operator names and never overwrites. Verify test-opens the datastore, so it answers with
the keychain entry gone, which is the situation an incident actually presents. Import opens the
store with the candidate before it writes the keychain, so a typo cannot replace a working key.
Neither input verb takes a key as an argument, and both swallow trailing arguments rather than let
argparse echo a mistyped key onto stderr.

**Requirements:** AC-10.1 amended on the owner's ruling of 2026-09-10 — the operator owns exporting
and preserving the key, the product owes the mechanism — and FR-12 / AC-17.1–17.8 written against
it. The `security-model.md` norm went `in-transition` with the requirement and back to
**steady-state** here, on the condition the transition itself set.

🔴 **Two tests changed their assertions, and the warrant is recorded rather than assumed.**
`test_init_tells_the_operator_to_back_the_minted_key_up_and_how_to_read_it` asserted the exact
`security find-generic-password` string; it now asserts the exact `bankmachine store key export` and
`store key verify` strings. `test_a_missing_key_names_the_state_and_both_remedies` asserted the prose
"restore the keychain entry"; it now asserts `bankmachine store key import`. **Re-pointed, not
relaxed:** each new assertion is at least as specific as the one it replaces, and AC-17.7 is what
requires the change. A changed test assertion with no recorded warrant is indistinguishable from a
weakened one, which is why this paragraph exists.

**Found by running the thing, not by reading it.** A recovery drill against a scratch datastore —
delete the keychain entry, confirm the store will not open, restore from an escrow file — showed
that both "restore the keychain entry from your backup" messages still named no command. That is the
exact defect #61 was filed about, fixed on the export side and left standing on the recovery side,
and no test then in the suite could see it because both asserted the old prose.

🔴 **The Critic caught a hole in the primitive that every test in the first cut walked straight
past.** `opens_with` treated "the first read succeeded" as "the key decrypts this datastore". SQLite
reads a **pageless** file — a truncated copy, an interrupted restore, a `store init` killed between
creating the file and writing to it — as a valid empty schema, so the read succeeds without page 1
ever being touched, SQLCipher's codec is never invoked, and **no key is tested**. `verify` would have
printed MATCHES for an arbitrary candidate in exactly the state an incident presents, and `import`
would then have stored that unverified key over a working keychain entry — the overwrite AC-17.5's
rationale exists to prevent. Reproduced before fixing: two different random keys both "opened" a
zero-length file. `opens_with` now requires positive evidence that a page was decrypted and raises on
a pageless file, because that is a fact about the file rather than an answer about the key. It also
validates the candidate at the seam, so a malformed value cannot come back as a confident False.

🔴 **A second Critic round found the same defect class on the surface a new operator reads first.**
The README's quick start still carried the shell recipe *and* the sentence "Nothing in this product
ever prints it" — a claim this very commit falsified. Three reviewers landed on it independently.
The root cause is worth keeping: the surface census in the discovery document enumerated three
surfaces (`store init`, `store status`, the operator guide) and never included the README, and the
guard written for AC-17.7 reads command **output**, so it structurally could not see a tracked
document. The fix is both — the README, and a guard that scans tracked markdown for the recipe
inside a runnable code fence, with a positive control and prose deliberately left alone so the
records can still describe what they replaced.

**Four more from the same round, each a real gap rather than a style note.** The never-an-argument
refusal was attached per-verb, so `store key <64-hex>`, `store key export <key>` and
`store key import --key=<value>` all still echoed the secret through argparse's own error — onto a
stderr a scheduled runner captures, *before* `configure_logging` installs the redaction. That is now
a `RedactingParser` at the root of the whole CLI, reusing the formatter's shape-keyed `redact`, so
the existing norm reaches a surface it had never covered and every future subcommand inherits it.
`opens_with` was hand-building a read-role handle and silently dropping `PRAGMA query_only = ON` and
the `OperationalError` translation. `import` reported a key mismatch as exit 2 while `verify`
reported the identical fact as exit 1 — it is 1 in both places now, because the command ran and
found a problem. And the escrow paths recorded nothing on success: a key leaving the keychain is the
most consequential thing this product does to a secret, and only the failures were legible.

🔴 **A third round caught the fix regressing the remediation it was written to deliver.** Closing
the argparse echo removed the per-verb catch-all, and with it the sentence telling an operator that
the key they just typed is now in their shell history — on exactly the two invocations that used to
say so. The assertion guarding that string was deleted in the same commit that widened the test from
two cases to five, so nothing was left to notice. **Redacting is half the job:** the value is kept
off stderr, and the copy already in the operator's history is theirs to clear and nobody else's to
find. The remediation now fires off the fact that the scrub CHANGED something, so it reaches every
command and every argument shape rather than the handful anyone thought to guard, and the assertion
is back on all five cases.

**The read-role open is modelled once.** `opens_with` had been a second hand-built copy, and it had
already drifted — losing `PRAGMA query_only` and the `OperationalError` translation. The two entry
points differ in exactly one thing, where the key comes from, and that is now an argument rather
than a second copy of the open. `connection.py`'s module docstring said `reader()` was the only
read-role construction site; it says what is true instead.

**Known limit, stated so it is not mistaken for coverage:** AC-17.8's automated half is a round-trip
test. The **operator** rehearsal — a restore into the production path, against a key read back from
wherever the operator actually stored it — is still owed and sandbox cannot rehearse it
(`operational-spec.md` § Restore, #10).

## 2026-09-10: Production cutover hardening — what five reviews found the night before real accounts

<!-- prawduct: scope=production-cutover-hardening | release=v0.1.0 -->

**Why:** the owner connects real accounts the next day. Five independent read-only reviews of
`develop` at `4b78550` — security, MCP surface, money model, sync/connector, docs — found no
credential or PII leak and a design that is strong where it is strong, and a set of defects that are
**invisible in sandbox and ordinary in production**: a wedged pipeline or a plausible wrong answer,
with no signal either way. The reports ride under `artifacts/reviews-2026-09-09/`; the plan is
`build-plan-production-cutover-hardening.md`, five chunks built in parallel on five branches.

**What changed:**

- 🔴 **Enrollment offers every bank.** `investments` moves from the REQUIRED product set to
  `optional_products`: Link offers only institutions supporting every required product, so requesting
  `investments` up front silently removed most card issuers and credit unions from the picker. The
  discovery the 2026-09-07 decision wanted still happens wherever the institution supports it.
- 🔴 **A re-link no longer wedges the connection.** Re-enrolling an institution that yields a NEW
  Item resets the sync cursor and the granted window in the same transaction that repoints the row;
  before, the old Item's cursor was sent with the new Item's token, forever, on the only remedy the
  product offers for `ITEM_LOGIN_REQUIRED`. `connections retire` now marks the connection's accounts
  inactive, so their frozen balances stop reading as current.
- 🔴 **Sync survives what production sends.** `TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION`
  restarts from the stored cursor instead of degrading the connection; the `accounts` array on every
  sync page is derived before the change lists, so a transaction on an account `/accounts/get` no
  longer lists cannot wedge derivation; a null `balances.current` records an absent balance instead
  of aborting the connection; a datastore failure on one connection degrades that connection and the
  run continues; a `modified` naming a hold that has already posted finds the merged row instead of
  inserting the purchase again; `include_original_description` is on, so the archive holds the bank's
  own memo from the first production page; `store rebuild` carries `category_override` across a
  version bump instead of dropping every operator re-categorisation; `certifi` pins the trust store.
- 🔴 **A card in credit is value held, not debt.** The Plaid connector negates every liability
  balance: the aggregator documents a credit/loan `current` as positive-when-owed and
  negative-when-in-credit, so the old conditional stored a refunded card's credit as money owed.
  The test that pinned the conditional changes its expectation and records why. **Owner ruling
  requested; applied provisionally.**
- 🔴 **The server's instructions fit what the client delivers.** Measured: Claude Code handed the
  model 2,045 of 6,673 characters, cutting mid-table and dropping the rule that prevents "no
  payments found" on a mortgage. The instructions are now a 1,693-character primer whose second line
  names the two reference resources; everything cut is served by URI, and the completeness test
  holds the union. The primer says what the server cannot answer, names the three unbuilt tools, and
  says that `description` and `merchant` are third-party text to quote and never follow.
- 🔴 **The money prose stops asserting what the classifier does not establish.** `flow_class`
  reads one aggregator category and matches no counterparty leg, so "an internal transfer never
  left" and "debt service settles purchases already counted" were false for the sandbox payroll row,
  for every mortgage, auto and student-loan payment, and for a card payment when the card is not
  enrolled. The definitions now say what each class IS; `totals` carries whole-window
  `inflow_minor_units` and `outflow_minor_units` and the three classes are asserted to sum to it.
  The counterparty-matching classifier is filed (#66).
- **The MCP boundary's catch covers the boundary** — serialization and the write itself — so a
  failing `to_wire()` answers `internal_error` and the pipe survives; `truncation.matching` is the
  whole-request count on every page and `remaining` is what falls; transaction rows carry
  `account_id`; `money_summary` carries the lifecycle and coverage caveats the other tools carry; the
  text form of every answer is compact JSON; unknown tool and non-object arguments answer `-32602`.
- **Files land owner-only.** `os.umask(0o077)` at the one process entry; before, the datastore, WAL,
  log and backups were 0644. `store init` prints the key-backup instruction with the keychain recipe
  (never the key); `store status` repeats a one-line reminder. The credential guard's label
  vocabulary matches the log redactor's, and a forwarded reference beside a string literal no longer
  trips it. Tests can no longer resolve config from the operator's real environment.
- **A stranger can get from clone to production.** `docs/first-production-connection.md` (dashboard
  prerequisites, the ordered cutover with what each step verifies, the day-one checks no test can
  perform, the standing routine), `docs/README.md` as an index, README's *What leaves this machine*,
  one Python truth (3.14) everywhere, the MCP client page followable from a GUI client, and the
  2026-09-08 readiness verdict marked superseded.

**Not closed, filed:** #66–#86 — the counterparty classifier, `connections reauth` via Link update
mode, re-link duplicate history, settled holds keeping their authorization date, interior gaps,
AC-11.2 reconciliation, `money_summary` paging, a `posted_date` index, batch requests, `gapped` on
every answer, per-account window coverage, `original_description` derivation, re-archiving on
failed derivation, sync exit codes, a writer per page, a consumer CHANGELOG, consent expiry,
log rotation, unofficial-currency rounding, `INITIAL_UPDATE_COMPLETE` reported as finished, and an
account with no currency anywhere.

## 2026-09-09: A store this build cannot serve refuses, instead of answering zero

<!-- prawduct: scope=unservable-datastore | release=v0.1.0 -->

**Why:** a populated datastore at a schema version this build does not serve was reported to the
client as a **success** — `isError` false, no JSON-RPC error, `rows: []`, every `coverage` figure
zero, with the diagnosis carried only in `warnings`. Measured against this machine's sandbox store —
schema 2, build serving 4, **14 accounts and 388 transactions present** — every tool answered as
though the household owned nothing. An agent that does not parse `warnings` reports "you have no
accounts" and "you spent nothing", which is the failure this product exists to prevent. Found by
driving the server, not by reading it: the acceptance session had probed a store the build could
serve, and that is the only state in which the defect is invisible.

**What changed:**

- 🔴 **`_readable`'s one-line collapse was the whole defect.** `connection.inspect` distinguished
  five states and returned one value, so the answer-with-zeroes carve-out written for a MISSING
  store reached a populated one. `inspect` now reports **which** state it found (`DatastoreProblem`)
  and `_readable` branches on that value rather than on the wording of a sentence.
- Every unservable state refuses through one choke point, reaching the client as `isError: true`
  with the stable code `datastore_unservable`. **A store that is merely MISSING keeps AC-ARCH.3's
  answer-with-zeroes carve-out** — it has no data to misreport — and that split is the owner's
  ruling of 2026-09-09, recorded in the build plan.
- 🔴 **`SCHEMA_BEHIND_BUILD` and `SCHEMA_AHEAD_OF_BUILD` are separate values, and collapsing them
  is a defect with a specific victim.** `migrate()` is forward-only, so "run the migrations" does
  literally nothing for a store a newer build already migrated — the operator runs the command, is
  told there was nothing to apply, and never learns the real fix is to upgrade the reader.
- 🔴 **One remedy map, because four callers each got the rare states wrong.** The cumulative review
  found `cli/connections.py`, `cli/connector.py`, `cli/sync_run.py` and `cli/enroll.py` each
  composing their own remedy, and all four prescribing `bankmachine store init` for *every*
  unhealthy state — including the two where it is actively wrong: it applies nothing to a store
  ahead of this build, and it refuses, correctly, to mint a key for a store that already exists.
  `_REMEDIES`/`remedy_for` now live beside `DatastoreProblem`, and both the MCP layer and all four
  commands compose their own diagnosis around the same action clause. An import-time guard fails the
  build if a sixth state ships without a remedy. **The durable part is why it survived:** each caller
  got the common state right and the rare ones wrong, so no single site looked defective on its own.
- **A remedy that promised a log entry nobody wrote.** The fallback told the operator the
  file-level diagnosis "is also in the log" while `query` logged nowhere and `cmd_mcp` wrote
  `status.problem` only at startup — so for the state that branch exists for, a store going bad
  while a long-lived server runs, the sentence sent them somewhere empty. Made true rather than
  deleted, and now asserted from both sides: the diagnosis IS in the log and IS NOT on the wire.
- **No remedy names `store rebuild`**, which is recorded as having rolled back on this shape of
  store, and a guard test holds that.

**Verification.** 1021 tests, 172 norm breaks red, ruff and mypy strict clean. Four review rounds
(one `chunk`, one `cumulative`, two `verify-resolutions`) ending 0 blocking / 0 findings. VRF-004
steps 1-5 run against the live sandbox; step 6's product half verified and its client half not run,
so VRF-004 remains pending along with VRF-003.

**Owed at merge:** #57 and #58 close as shipped. 🔴 **#57's scope grew during the cycle** — it now
covers the CLI as well as the MCP surface, and closing it silently would leave the issue reading
narrower than what shipped.

## 2026-09-09: The roster observation is recorded, and the contract describes what it publishes

<!-- prawduct: scope=production-blocker-findings | release=v0.1.0 -->

**Why:** the previous cycle closed three production blockers and produced three findings it
deliberately did not fix in place. One of them was a shipped answer that is knowably wrong at a real
shape: a connection holding a single account whose account stops being listed reported `active`
forever, beside a frozen balance, with no signal — the exact failure FR-9 exists to remove,
surviving inside FR-9.

**What shipped:**

- **The roster observation is recorded per connection, not derived (`#51`, AC-12.4/12.5/12.5a).**
  Migration 004 adds `connections.roster_observed_date`. 🔴 **A calendar date, and the name says so** —
  `roster_observed_at` was the obvious spelling and was rejected because the column's only use is a
  comparison against `accounts.last_seen_date`, and mixing the two types is a defect rather than a
  conversion.
- **AC-12.5's whole-roster clause was REPLACED, not annotated.** It said a connection whose entire
  roster is absent marks nothing absent, justified by scale. At one account that argument inverts —
  one closure is ordinary and it *is* the whole roster. The old criterion had **one channel**, so it
  had to choose between publishing the account-level truth and publishing the connection-level lie,
  and it suppressed the truth. There are now two: every account on an empty-rostered connection is
  marked absent, **and** the connection raises the new `roster_observed_empty` kind. A reader holding
  both can tell fourteen closures from a feed that returns success with no rows.
- **The API contract describes every field it publishes (`#52`).** The 34 undocumented published
  fields are described, `UNDOCUMENTED_AT_FREEZE` and its staleness guard are deleted, and the
  remaining assertion runs unconditionally over all 93 names. It also carries an **extraction
  contract** — which tables are authoritative, how a field name is spelled — validated by a parser
  that recovers the same 93 names from 16 tables.
- **A discovery for the hold-state model (`#53`), marked proposed and built by nothing.** It tested
  the prior opinion rather than confirming it and falsified its premise: `_flow_class()` is already a
  derived per-row enum computed in SQL, so "enum" never entailed "aggregation in Python" here. It
  recommends SQL predicate constructors and states the strongest argument against its own
  recommendation — that this is the shape which already failed once.
- **Two defects found while doing the above, both fixed with guards.** `coverage_report` derived its
  calendar day twice in one call, 32 lines apart, under a comment asserting it must not; a report
  assembled across midnight answered about two days. And `DERIVATION_VERSION` had not been bumped for
  either newly-populated column, so `store rebuild` — the remedy the upgrade procedure prescribes —
  would have rolled back with `RebuildNotReproducibleError` on exactly the store it is written for.

**Verification:** the go-red harness holds **171 cases, every one seen red** — 8 for the roster
observation, one each for the retired-connection clause, AC-12.6's no-verdict clause, and the
derivation-version bump. Suite 980.

**Upgrading:** migration 004 is additive, nullable and forward-only, and it opens a window 003 did
not. Accounts that a pre-004 store already reported absent read `active` again until their
connection's next successful sync — and for a connection that never syncs again, **until an operator
runs `bankmachine store rebuild`**, which the upgrade procedure now says to do. Expect it to report a
content change.

**What this does NOT close:** `#53` ends with a recommendation and nothing built. `#47` stays open
with its Expected narrowed — the contract guard is a whole-document backtick sweep, and **76 of 93
published names would keep it green even with their whole description deleted; 17 are load-bearing.**
Replacing the sweep with the extraction contract's parser fixes both that and `#47`'s own direction.

## 2026-09-09: Three answers that could be wrong in silence now say so on the wire

<!-- prawduct: scope=production-blockers | release=v0.1.0 -->

**Why:** three answers this product gives were plausible, well-formed, and capable of being
wrong with no signal at all. A balance frozen since an account stopped being reported still
read as current. A spending total silently mixed authorisation holds in with settled money.
A signed amount from a connection whose direction had never been observed against a known
inflow was trusted as if it had been. Each is the failure mode the §7 verification gate
exists to prevent — incompleteness that looks like completeness — and each was carrying a
`blocks:production` label for that reason.

**What shipped:**

- **An account says when it was last seen, and what that means (FR-9, AC-12.1-12.9).** Four
  always-present row fields and a three-value vocabulary that names the *observation* rather
  than the conclusion; `no_longer_reported` is derived at read time and never stored, because
  it is a statement about the gap between now and the last sync, not a fact about the account.
  One producer feeds both `list_accounts` and `get_coverage_report`, so the two cannot disagree.
  Migration 003 adds the column; migration 002's DDL is untouched.
- **The owner's include-and-flag ruling, and the norm born on it.** A total over account
  balances INCLUDES non-active accounts and states what they contributed — a count and a
  per-currency signed magnitude, both always present and empty rather than absent. The
  recommendation on file was the opposite and was not taken; both arguments survive in
  `discovery-account-lifecycle.md`. The magnitude is the load-bearing half: without it this
  is *classify, do not filter* arriving as its own failure mode.
- **Pending money is separated from settled money everywhere it is summed (AC-13.1-13.7).**
  Every aggregate says how much of itself is an unsettled hold. A settlement updates
  `amount_minor` in place — the source correcting its own earlier figure, not us
  reinterpreting it — and both raw responses stay in `raw_responses`, so the hold amount is
  never lost and the row stays rebuildable.
- **A per-connection sign-convention check (AC-14.1-14.6).** It reports a *measured*
  inversion against a known inflow; it never auto-corrects, because a stored source value is
  not ours to rewrite.
- **The go-red harness, and a guard on the harness itself.** 160 cases, every one seen red.
  A second test proves each case still reaches its target in about a second, which caught two
  anchors that `ruff format` had reflowed out from under.

**Upgrading: this entry ships a schema migration, and the order is not the usual one.**
Migration 003 (`accounts.last_seen_date`) is forward-only, and a build that does not recognize
the datastore's version refuses to serve — so the datastore and every process that reads it move
together, and `store backup` runs BEFORE the migration because the old build cannot back up a
store it will no longer open. The procedure is `operational-spec.md` § *Upgrading across a schema
migration*, which migration 003 is the first to have been written for. It backfills nothing: the
first sync after the upgrade repopulates the column, and until then every account reads as
still-reported — the pre-migration answer rather than a wrong one.

**What this does NOT close.** None of `#40`, `#22` or `#23` closes here, and merging this does
not take `blocks:production` off any of them. Each keeps a production-data tail — AC-13.8,
AC-13.9, AC-14.7, AC-14.8, AC-14.9 — enqueued as VRF-005 and VRF-006, and those are performed
by the operator against real accounts. `#40` additionally ships with its `closed` lifecycle
value reachable in the read path and unreachable in the product, filed as `#48`.

## 2026-09-09: The wire envelope becomes its own module, before the next tool lands

<!-- prawduct: scope=envelope-module-split | release=v0.1.0 -->

**Why:** `query.py` had grown to 2101 lines owning two separable things — the envelope
model every MCP answer is built from, and the SQL for all five tools. An envelope change
was reviewed inside a thousand lines of unrelated statements, and three specified tools
are still to land. The value is entirely in the ordering: doing this after the next tool
means re-reading everything that tool added.

**What shipped:**

- **`envelope.py`** holds what an answer *is* — `Answer`, `Caveat`, `Window`,
  `Truncation`, `Cursor`, the warning vocabulary, window resolution and cursor parsing.
  `query.py` keeps how *this store* fills one: the per-tool SQL, and the constructors
  that read while they build.
- **The boundary is direction, not subject matter.** "Envelope vs SQL" does not survive
  contact with the file, because `_answer` takes a live connection and reconciles the
  requested window against coverage it queries in the same call. What is checkable is
  that the envelope never reads.
- **A guard enforces it, as an allowlist rather than a ban list.** The enumerated form
  was written first and already had holes — `store.derivation`, `store.raw` and
  `store.rebuild` all reach the database and would have passed it. The guard carries a
  positive control too, because a containment scan passes trivially once there is
  nothing left to contain.
- **Per-tool vocabulary stays with its SQL.** `GROUPINGS` is a closed set *because* the
  grouping is an expression `query.py` builds and never a caller-supplied column name;
  moving it would separate the constraint from its reason. Collapsing the three-site
  conditional-key declaration is deferred to the arrival of a fourth such key, recorded
  as a trigger so it is not re-litigated on every reading.
- **Nine go-red anchors re-pointed** at the new module, none dropped: 141 cases before
  and after, all red.

**No wire change, verified rather than intended:** the five published tool schemas and
both reference documents are byte-identical to `develop`, diffed rather than eyeballed.

## 2026-09-09: One aggregate that answers spending, income and cashflow — and says which is which

<!-- prawduct: scope=mcp-answer-scope-completion | release=v0.1.0 -->

**Why:** three different things were all being called spending, and the surface could not tell them
apart. Measured over the sandbox store's full 24 months, `spending_summary` reported $267,692.77 of
outflow. $164,400.00 of that is the account holder moving money between their own accounts and
$50,484.00 is credit-card payoff settling purchases already counted under the categories they were
spent in. **Actual spending is $52,808.77 — one fifth of the number the tool returned.** Separately,
the tool filtered `amount_minor < 0`, so an inflow had no row to appear in at all: a travel category
of offsetting charges and credits reported $12,000 spent against a true net of $0, and nothing in the
payload could reveal the 24 credits behind it.

**🔴 Removed, and this is the one departure from "never remove or repurpose a field":** the tool
`spending_summary` and its row field `spent_minor_units`. Both are gone. `api-contract.md` §
Surface Inventory grades every MCP tool `experimental`, where removing one is the policy working
rather than a violation; single consumer, pre-production, no deprecation window owed.

**What ships:**

- **`money_summary`** replaces `spending_summary`. One row shape for every grouping —
  `category | merchant | account | month | flow_class` — carrying `inflow_minor_units` and
  `outflow_minor_units` as positive magnitudes with `net_minor_units` operator-signed, per currency.
  Currency groups and is never summed across.
- **`flow_class`** on every row: `external_spend`, `internal_transfer`, `debt_service`, read from
  `source_category_primary` and never from `category_override`. A grouping dimension under *every*
  `group_by`, because the class is not a function of the group — an account holds a transfer and a
  coffee — and an optional field is refused by the tool-boundary norm.
- **`totals`**, a new envelope key: the window's outflow split three ways, per currency. The three
  sum to the window's total outflow, which is what proves the classification keeps every row rather
  than filtering some away.
- **`get_coverage_report`** built, and per-account coverage on every `list_accounts` row. Nine of
  fourteen sandbox accounts have never had a transaction — 82% of the balance sheet by magnitude —
  and `query_transactions(account_id=9)` answered `[]`, which reads as "no payments found" and is
  false.
- **`accounts_without_coverage`**, a new request-scoped warning kind.
- **Two registration guards.** A parameter name may not mean two types across the surface (#30's
  A3), and a tool whose row schema declares a field it does not require is refused — which is what
  makes the tool-boundary norm self-enforcing rather than a sentence the next builder has to
  remember.

**Records, because they live only in a plan that archives:**
`test_spending_sums_outflow_only_and_reports_magnitudes` was **deleted** with the tool it named, and
the AC-4.2 go-red case was **retargeted** to
`test_the_aggregate_reports_both_directions_as_magnitudes` rather than dropped — the guarantee did
not change, only the code and the test carrying it. That case had gone stale in both halves at once,
its anchor moved by a reformat and its test deleted by the merge.

**Closes:** #18, #19, #21, #24, #30, #35.

🔴 **#20 does NOT close, and an earlier draft of this entry wrongly claimed it did.** Its Expected is
"builds `cashflow_summary`, and gives `query_transactions` merchant/text, category and amount-range
filters plus cursor pagination". The aggregate and the pagination landed; the three filters did not,
so of #20's own three repro questions only two are answerable — "did I get a refund from Walmart"
still is not, because there is no merchant or text filter to find it with.

**#34 stays open** — it asks for `rule-applied` to be emitted, and the ruling here is
classify-do-not-filter, so nothing may emit it: the kind means an account rule filtered rows *out*
of an aggregate, and this excludes nothing.

## 2026-09-09: Where a tool's boundary is drawn

<!-- prawduct: scope=mcp-tool-surface-norm | release=v0.1.0 -->

**Why:** #30 recorded an owner directive — "minimize tools, use actions to cover related
capabilities" — together with the conflict it implies. MCP allows one `outputSchema` per tool,
and the wave before this one made schemas per-tool precisely so a key's ABSENCE is information.
Behind an action parameter one schema must cover every action's envelope, so it degrades to the
loosest common shape and that property dies. The decision had to land before the remaining tools
were designed rather than during.

**The owner rejected all three options offered** — keep per-tool schemas and stay wide,
consolidate and lose them, or merge only where it happened to be free — on the ground that the
project is young and should accept no tech debt. That reframed the question from which horn is
cheaper at ten tools to which rule is still right at thirty capabilities.

**The finding is that the conflict is false, because both horns share a wrong premise.** Each
assumes a tool boundary tracks the *question asked* — one tool per question, or one tool for all
of them. Every answer here is one envelope plus `rows`, and what differs between tools is the row
shape: ten specified tools hold seven distinct row entities. Capabilities grow fast; row entities
do not.

**The norm: a tool's boundary is drawn where the answer shape changes, never where the question
changes.** Two guardrails carry it — a merge needs one strict row schema covering every parameter
value with no optional fields, and within a shape, merging only across questions sharing a domain.
The first is checkable at registration, so the norm enforces itself. The per-tool `outputSchema`
survives as a *consequence* rather than as something defended: a tool defined by its answer shape
has one answer shape by construction, and two tools can never become near-twins, because if they
were they would share a shape and already be one tool.

**Applied, ten specified tools become eight** with no schema loss and both near-twin pairs gone.
`cashflow_summary` merges into a grouped aggregate beside `spending_summary`; `net_worth` merges
into the time series beside `balance_history`. 🔴 **The time-series merge failed guardrail 1 on
the specified shapes and was admitted only after a unifying row was found** — which is the
guardrail working rather than being worked around. The one merge the rule was expected to make and
did not is `list_accounts` + `get_coverage_report`: same row entity, but verification and analysis
are different domains, and the coverage signal crosses the split instead, closing #19 by
construction.

**The contract's tool table was left unrewritten by this entry's own commit, deliberately.** It is a
load-bearing input — the surface guard derives the *specified* set from those rows and asserts
built ⊆ specified — so rewriting it ahead of the code would have dropped `spending_summary` from the
specification while it was still on the wire. The table, every spelled count, and the claim-site
sweep moved in the same commit as the merge, which is the `mcp-answer-scope-completion` entry above.

## 2026-09-08: The MCP surface says what it is, and what to do about what it says

<!-- prawduct: scope=mcp-alignment | release=v0.1.0 -->

**Why:** a review of three sibling projects' MCP practice, read against this server and
against the SDK's own type definitions at 2.2.0 — the same version this repo cited when it
decided not to take the SDK. The finding was not "conform". Two of the siblings' strongest
positions are ones this server keeps departing from, and the departures are recorded beside
the work rather than left for someone to re-litigate.

**The handshake stopped claiming things that were not true.** It offered `2026-07-28`, a
revision not reachable through `initialize` at all — the registry partitions handshake
revisions from that one, whose sessions use a different envelope entirely. It omitted
`2025-11-25`, so a client on the current revision was downgraded two steps. An explicit
`"id": null` was read as a notification, so a conformant client sending one waited forever;
`params` as a by-position array, which JSON-RPC permits, raised out of the read loop and
ended the session. And build identity was hung on `serverInfo`, whose type declares no field
for it and whose reader drops what it does not declare — so a comment claiming a client could
see which build answered was false for every SDK-based client. It now rides `_meta`.

**Every tool publishes an `outputSchema`, per tool rather than shared**, because the
conditional keys carry meaning by their absence: an unwindowed tool's schema now *forbids* a
window rather than merely not requiring one. A validating client turns envelope drift into a
loud failure instead of an agent quietly consuming a changed payload.

**A `resources` capability serves the reference material**, so the detail costs nothing until
it is wanted. Both documents are derived — the warning reference walks the vocabulary, the
envelope reference renders from the published schemas — so a kind added to the vocabulary
arrives unexplained rather than silently missing.

**The warnings now say what to DO.** The vocabulary was the half already done well: every kind
was defined and not one said whether the answer could still be quoted. `gapped` now means do
not answer about the ungranted period at all; `degraded` means treat the total as a floor.
This is the gap all three siblings had independently closed.

**Four guards, three of them structural.** Nothing on the server's import path may write to
stdout (stdout *is* the transport). The documented tool surface must match the built one, by
name rather than count. Every caveat must name a kind the vocabulary declares — because a
published enum turns an invented kind into a *rejected answer*, so the warning would destroy
the response it rode on. And the path guard learned that a URI segment is not a filesystem
path. Each has a go-red case.

**Not done, and filed rather than dropped — eight items, #30 through #37.** Consolidating the
tool surface behind action parameters (#30, which must decide the `outputSchema` conflict it
carries, and which binds the remaining tools before they are designed); teaching errors to hand
back structured recovery data (#31); adopting `mcp-types` for the wire facts without the SDK's
transport stack (#32); an eval harness that exercises the surface the way a model actually uses
it (#33); the instructions still enumerating fields three carriers now state (#36, a deliberate
keep-for-now whose revisit is tied to #30); a self-describing help surface on the tool layer
(#37); and `get_coverage_report`, half the verification surface, still unbuilt (#35).

🔴 **One of the eight is exposure this wave created rather than found: #34.** The decision table
added here tells an agent what to DO about a `rule-applied` warning, and `rule-applied` is
declared in the vocabulary, explained in the handshake table, and now carried in the resources
surface — while none of the eleven `Caveat` construction sites can emit it. Three carriers now
promise a warning nothing sends. The honest cheap fix is to mark it not-yet in all three places;
building the emitter is the aggregate-rule path and is not this wave's.

## 2026-09-08: A truncated answer carries the route to the rest

<!-- prawduct: scope=mcp-answer-scope | release=v0.1.0 -->

**Why:** the entry below made a capped answer stop reading as a complete one. It did not make the
missing rows reachable. The cap is a contract term, narrowing the window moves the boundary rather
than removing it, and at the ceiling the only advice left was one the payload's own `returned`
contradicted — so a caller could see that a two-year card total was understated by roughly 40% and
still have no way to get the right number. #17 asked for a total *or* a cursor; the total alone
leaves the honest answer unobtainable.

**What changed:**

- **`truncation.next_cursor`, present when and only when `truncated` is true**, and an optional
  `cursor` argument on `query_transactions`. **The key's presence is the loop condition** — page
  while it is there, stop when it is gone — so a consumer never has to compare two counts to know
  whether it is done. Both `next_cursor` and `truncated` derive from the same two counts, so they
  cannot disagree.
- **`cursor` narrows the request the way `since` does.** The keyset predicate lives in the shared
  `_transaction_filters`, so the count narrows with the page. A count taken over the whole result
  set behind every page would leave `truncated` true on the final page forever, and a caller paging
  until it went false would never stop.
- **Opaque keyset state over `(posted_date, transaction_id)`, never an offset.** That order is
  already total, so "everything after this row" is a predicate rather than a count of rows to skip.
  One insert from a concurrent sync shifts every offset page, so a caller would see one row twice
  and never see another — this work cycle's own defect arriving through a new door.
- **A cursor carries a fingerprint of the predicate it was issued for**, and one presented with a
  different `since`, `until` or `account_id` is refused. 🔴 **This is a requirement that surfaced
  during the build and is recorded as an amendment in the plan's Chunk 03 deliverables rather than
  designed in chat.** The reachable foreign cursor is not a forged string — it is the caller's own
  against a changed request, which selects real rows, in the right order, and answers a question
  nobody asked, with no error and no warning. `limit` is deliberately outside the fingerprint:
  changing page size between pages does not change which result set is being walked.
- **Every rejection path answers in one sentence that names `cursor`** and leaks nothing of the
  decoder, and none of them falls back to "start from the newest row" — that fallback returns page
  one under the name of page two.
- **The `rows_truncated` remedy leads with paging**, because it is the only route that reaches every
  matching row; "raise `limit`" is still offered only while `limit` has something left to give.
- **The cap did not move.** ~500 stays a contract term across `api-contract.md`,
  `nonfunctional-requirements.md` and `security-model.md`; the security model now records that a
  cursor narrows a request rather than lifting the cap, so each page stays bounded by it.

**The plan's Done-when 2 — "ask which existing tests now short-circuit" — asked and answered.** The
chunk changes control flow through the statement builder, and `learnings.md` records that mutation
testing is structurally blind to this, because reverting removes the damage along with the fix. Two
checks were narrowed by the change and both were widened rather than left:

- The guard comparing the row query's `WHERE` against the count's had no case carrying a cursor, so
  it would have agreed about a predicate *neither* statement held. It is now parametrized with two
  cursor cases, and asserts the keyset clause reached the row query **before** comparing the two —
  `learnings.md` § *Guarantees by construction* records this exact fail-open shape from the same
  guard one chunk ago.
- The invariant grid spanned every request the store could answer *before* the function grew a
  second half. It now carries the cursor as a dimension, with an oracle that spells the keyset out
  independently, so every invariant is checked on page two as well as page one.

**Done-when 3 — every new assertion broken and watched to fail — ran as ten mutations, and two of
them survived the first time.** Both survived for the same reason: the forged-cursor fixtures
carried a placeholder fingerprint, so `parse_cursor` refused them at the fingerprint check before
the branch under test was ever reached. Removing the bool guard and removing the scheme-tag check
both left the suite green. The fixtures now carry the real fingerprint, and every mutation goes red.
🔴 **A check that cannot fail hides every problem in its blast radius, not one** — the rule
`learnings.md` already records, met again in the fixture rather than in the assertion.

**The Critic caught a refusal path that answers "internal error".** `Cursor.decode` caught only
`ValueError`, on the stated grounds that every decode failure derives from it. One does not:
`json.loads` on a deeply nested payload raises `RecursionError`, a `RuntimeError`, so that cursor
escaped the refusal, escaped the boundary's narrowing, and landed in its broad catch — answering a
caller who mistyped an argument with "the failure has been logged; `bankmachine store status`
reports whether the datastore is readable". A false statement about a caller mistake is the exact
outcome `InvertedWindowError` exists to prevent. Reasoned by the Critic from the exception surface,
then reproduced, then fixed and pinned by a case that survives a future iterative JSON decoder.

**The same defect class was one layer out, in code this branch never touched, and it is fixed
here too.** `_read_messages` caught only `json.JSONDecodeError` around its `json.loads`, so a
deeply nested JSON-RPC frame ended `serve()` outright — the operator's tool disappearing
mid-session, which is what `cmd_mcp` exists to prevent, arriving one frame in rather than at
startup. Reproduced, then fixed. A second review pass then named the *adjacent* door: the bytes are
decoded by the **read**, one step before any parsing, so invalid UTF-8 raised `UnicodeDecodeError`
during iteration and escaped the same way. 🔴 **Worth recording as a pattern rather than as two
bugs: the class search was run and stopped at `json.loads`.** The read is now guarded too, and
because reporting-and-continuing is only right while the stream advances, a consecutive-failure
ceiling sits beside it — a hung server is less diagnosable than a dead one, and removing that
ceiling makes the test suite hang rather than fail, which is the shape the ceiling is there for.
The counter resets on every good read: counting a session's lifetime rather than a run would end a
long-lived session on its third bad frame ever. That reset survived its first mutation, because no
test recovered more than once; there is now one that does.

**Verified against the real sandbox store**, not only the fixture: account 4 over
2024-01-01..2026-12-31 still returns 100 of 144 truncated, and now pages to 144 distinct rows in two
pages, the last carrying no cursor and no `rows_truncated` warning. August 2026 still reports
1,114,946 minor units across 8 categories, so no figure moved. Suite green.

## 2026-09-08: A capped answer says how much it left behind

<!-- prawduct: scope=mcp-answer-scope | release=v0.1.0 -->

**Why:** `query_transactions` stops at its row cap and says nothing. An acceptance round measured
what that costs: the documented default of `limit: 100` silently dropped ~16 months of one
account's history, and a caller summing a two-year card total understated it by roughly 40% — with
a payload that read as a complete answer throughout. `rows` alone cannot say it, because "100 rows"
is a believable complete answer. This is the row axis of the same defect the entry below fixes on
the time axis: an answer that does not say what it was computed over.

**What changed:**

- **A `truncation` block on `query_transactions`** — `returned`, `matching`, `truncated` — present
  on capped tools only, so its absence truthfully says "this tool returns everything it found".
  `truncated` is a derived property rather than a stored field: a third number is a third thing
  that can disagree with the other two, and a derived one cannot.
- **`rows_truncated`**, a request-scoped warning kind. The block alone would not satisfy the
  contract's own direction that *incompleteness rides the success path as a warning field*, and a
  consumer reads `warnings` precisely when the numbers look wrong.
- **One predicate list feeding both the row query and the count**, from one `_transaction_filters`
  and one from-clause. "Remember to update both" is the enumeration failure this repo has already
  been bitten by, and its symptom here would be a *precise* wrong number — worse than the vague one
  being removed. A guard test reads the SQL the engine actually runs and compares the two.
- **`coverage.transactions_in_effective_window`**, a new sibling. `coverage.transactions` keeps its
  store-wide meaning: narrowing it in place is the repurpose `api-contract.md` forbids in red, where
  a consumer still reading it gets a wrong answer rather than an error.
- **`window_extends_past_today` renamed to `window_extends_past_coverage`.** 🔴 **This departs from
  the kind name the discovery specified**, deliberately: the old name states the wrong bound, since
  the covered end is today *or the last transaction when that is later*, so the name was stale by
  one word while the detail text beside it was accurate. Renaming a shipped warning code is a
  breaking change under the contract's evolution rules, which is exactly why it happened while the
  surface was still unreleased. It now mirrors `window_starts_before_coverage` — one boundary
  concept named from its two ends.
- **The warning vocabulary carries its own scope.** `CONNECTION_SCOPED_KINDS` and
  `REQUEST_SCOPED_KINDS` compose into `WARNING_KINDS`, so a kind cannot join without declaring which
  it is. This discharges a finding carried in from the previous chunk's review: the test helper
  derived "request-scoped" from a `window_` prefix, which was the same set only until a
  request-scoped kind arrived that is not about a window.

**The latency question the plan refused to assume, answered:** `matching` costs a second `COUNT(*)`
per call against a promise of under ~1s over 24 months, unmeasurable on the 388-row fixture.
Measured against synthetic stores (`mcp-count-latency-2026-09-08.md`), each count costs about what
the row query itself costs — ~1ms at 10k rows, ~89ms at 200k — and a full call lands at ~18ms and
~435ms. **`matching` ships exact; the approximate-count fallback held in reserve is not built.**

**Two defects found by hand-probing, which no mutation would have caught.** Both are the shape this
work cycle exists to remove — a well-formed sentence its own payload denies — and every test passed
while they were there. At the cap the caveat read *"the newest 500 are returned … raise `limit` (at
most 500)"*, telling a caller to raise a number to the value it already held; the remedy now depends
on whether `limit` has anything left to give. And a windowed answer against an unreadable store
carried `effective_window` but dropped the new coverage sibling, so the key set a consumer branches
on moved exactly when the datastore could not be read — the same shape as the blocking finding the
previous chunk's second review round raised, now pinned at both the query and the wire level.

**The Critic found the one thing I had asserted and not checked.** `Truncation` raised on
`returned > matching`, and its docstring called that unreachable "while the two statements share one
filter list". It is reachable: the read handle is autocommit — `store/connection.py`, *"every
statement is its own snapshot"* — so the row query and the count are two snapshots, and the scheduled
sync writer soft-deletes transactions while the MCP reader may be mid-query. Plaid removals are
typically recent pending rows, which are exactly the newest rows a default query returns, and the
*untruncated* case is where it bites, since `returned == matching` there and one removal suffices.
The raise would have refused a good question because a nightly sync landed mid-query — a failed tool
call where the contract's own Direction says incompleteness rides the success path. **Resolved by
treating it as the data condition it is:** `Truncation.over()` floors `matching` at `returned`,
`truncated` reads false because nothing is hidden, and a new `counted_during_change` warning
announces the skew rather than smoothing it away. 🔴 **A guarantee stated above its mechanism is
exactly what `learnings.md` warns fails silently later, and this one was mine.**

**Left open, and deliberately out of this chunk's scope — filed as #27.** One `Answer` is assembled
from eight or more statements, each its own snapshot, so this is not a property of the new counts:
`coverage.transactions` has had it since it shipped. Two consequences reach further than the
truncation block and are worth naming rather than discovering later. **Chunk 01's guarantee that
"every returned row lies inside `effective_window`" holds against the COVERAGE snapshot, not the ROW
snapshot** — `_coverage` filters `removed_at IS NULL`, so a soft delete of the store's oldest row
between the two reads moves `earliest_transaction` forward and can push `effective_since` past a row
already in `rows`. And **the three transaction counts are three reads at three times**, so the
relations a caller would assume between `coverage.transactions`, `transactions_in_effective_window`
and `matching` hold only per-snapshot. The wording in `query.py`, `api-contract.md` and
`tests/test_query_window.py` now states the conditional version rather than the one that reads
better; #27 is what would earn back the unqualified claim.

*(An earlier draft of this entry also named `as_of` as a consequence. It is not one, and the
ordering is the safe one: `as_of` is captured after the rows, so `today` — and therefore
`covered_end` — can only widen relative to what the rows saw, and a wider window still contains them.
Capturing it first would be the hazardous direction, and the code does not.)*

Not fixed here, and the reason is governance rather than effort. The fix is a per-answer read
snapshot, which departs from a documented norm — `store/connection.py` opens the reader autocommit
*specifically* so no read transaction spans two statements, with WAL checkpoint starvation as the
stated reason — and it changes read semantics for every query, not this one. A norm departure is a
recorded decision, and one this structural belongs to the owner rather than to a chunk already
mid-flight. The counter-argument is real and is recorded on #27: `query.py` is open now, so doing it
here would be cheaper than as a follow-on.

**Verification:** 22 mutations applied against the new assertions, every one caught. The invariants
were written before the hand matrix, on the previous chunk's evidence that a matrix inherits the
code's blind spot. One mutation exposed a defect in a *test*: the SQL guard partitioned on `" WHERE "`
against text SQLAlchemy compiles with newlines, so it compared empty string with empty string and
agreed with everything — it now normalizes first and refuses to pass on a failed parse.

## 2026-09-08: An answer says which window it actually covered

<!-- prawduct: scope=mcp-answer-scope | release=v0.1.0 -->

**Why:** `spending_summary` over a window that precedes coverage returned no rows, and nothing in
the payload distinguished "you spent nothing" from "this is not knowable". Four independent
acceptance rounds each arrived at the same judgement — an empty summary is the most believable wrong
answer this surface can produce — and each had to work the truth out by hand by comparing their
window against `coverage`. The connection-scoped `gapped` notice could not help: it arrives
character-for-character identical on a covered window, an uncovered one, a future one, and a query
for an account that does not exist, so it says nothing about any of them.

**What changed:**

- Every windowed response carries `effective_window` — the window requested beside the window
  actually covered. Unwindowed tools carry no such key, so absence means "takes no window" and null
  effective bounds mean "your window and this store do not overlap", which are different facts.
- Two request-scoped warning kinds, `window_starts_before_coverage` and `window_extends_past_today`,
  which fire only when this request crosses the boundary they name. Their absence is now
  information — the property the five connection-scoped kinds cannot have.
- The clamp is reportorial, not selective: no predicate narrowed, no figure moved. Verified against
  the real sandbox store, where August 2026 still reports 1,114,946 minor units across 8 categories,
  matching an independently recorded acceptance figure to the unit.
- The covered span ends at today *or the last transaction when that is later*, so that "every
  returned row lies inside `effective_window`" holds by construction. Bare `today` would have
  reported an answer stopping in September while returning a row dated in December, since the
  row-level predicate uses the caller's `until`. A forward-dated row is ordinary — a forward-posting
  authorization, or an institution a day ahead in local time — and the sandbox cannot express it.
- `_answer` gained a required keyword `requested_window` with no default, so every construction site
  must state whether its answer is windowed. Three more windowed tools are specified against this;
  a clamp each of them had to remember is a clamp that decays.
- Both windowed tool descriptions now share one sentence about the window, written once, so the two
  cannot drift into disagreeing.
- An inverted window (`until` before `since`) is refused by the resolver as its own
  `InvertedWindowError`, which the MCP boundary renders as a clean refusal alongside
  `UnknownAccountError`. Unreachable today — `_window` refuses the transposed pair first, and that
  remains the better place because the caller's own words are still in hand there — but a future
  windowed tool that skipped argument narrowing would otherwise have hit the boundary's broad catch
  and been told "internal error" for what is a caller mistake.
- Fixed while passing: `_declared_floor()` returned `Any` from a `-> str` function under
  `warn_return_any`, so `mypy --strict` had been failing on `tests/` since the floor test landed.

**Verified by breaking what each check names.** Ten mutations, each reverted after: the boundary
comparison loosened to `<=` (2 red); the two warning kind strings swapped, which is the shared-prefix
trap (3 red); `caveats` given a default, removing the structural guarantee (1 red); the covered end
pinned to the last transaction (8 red); `list_transactions` no longer declaring its window (3 red);
`effective_window` dropped from the wire (6 red); an unwindowed tool made to report one (2 red);
`spending_summary` stripped of the shared window sentence (1 red); the contradiction fix reverted
(1 red); and `_unusable` returned to omitting the window key (4 red). Every mutation was caught — which is the point of the entry below about what mutation
testing cannot do.

🔴 **Four bugs survived that mutation pass and a hand-written boundary matrix**, every one of them
an empty answer that failed to say why — the same shape as the defect being fixed. One was found
re-reading the diff, one by probing reachable inputs, and **two by the hypothesis property that was
only written because the first two had escaped**. The fourth was an inverted window, a shape the
property had no idea the MCP boundary already refuses; it is now refused in the resolver too, so
that function's invariant is total rather than resting on a boundary staying in front of it. Review
found two more, including the one below about forward-dated rows.

The lesson is recorded in `learnings.md`: mutation testing validates the checks you wrote and is
structurally blind to the case you did not think to write, and a hand-built matrix inherits the
code's blind spot because the same mind writes both at the same sitting. The invariant — *a window
that covers nothing always says why* — is the check that cannot be written from a model of which
windows are empty, which is why it kept finding what the model missed.

**Ruled during the work:** clamp and announce, rather than refuse. `operational-spec.md` refuses an
out-of-range *enrollment* window because a clamp would enroll at a window the operator never chose
and was never told about; on a read that reason points the other way, since refusing "show me 2024"
against a store beginning 2024-09-16 refuses an ordinary question and no history is lost. Silent
clamping was offered and rejected.

**Closes:** #16.

## 2026-09-08: The log can tell a failed run from a quiet one, and two claims stop being unchecked

<!-- prawduct: scope=observability | release=v0.1.0 -->

**Why:** A scheduled sync leaves one durable trace — the log file — and it recorded a failure and a
run with nothing to do identically. For a product whose named primary failure mode is silent
staleness, those two looking alike in the record is the defect, not a tidiness gap.

**What changed:**

- `run()` now writes a log record beside the stderr sentence on every failure arm, so the file a
  launchd job writes to can distinguish outcomes. Unexpected exceptions gain a traceback in the file
  and still propagate untouched — the exit status and the interactive traceback are unchanged.
- The record is marked file-only. `configure_logging` attaches a stderr handler, so logging beside a
  `print` would have put a second timestamped copy of every failure under the first: making the file
  honest would have made the terminal worse. A filter keeps the two destinations telling different
  readers what each needs.
- 🔴 A refusal is not recorded as a failure. `EnrollmentError` subclasses carry `EXIT_UNHEALTHY` for
  "ran and found a problem" — the roster is full, the operator walked away — and recording those at
  ERROR would conflate exactly the two outcomes this work exists to separate.
- `requires-python` narrowed from `>=3.11` to `>=3.14`, which is the only interpreter anything here
  runs on. The floor was a promise to every installer that no run had ever tested. Two tests hold it:
  one against the pinned interpreter, one against the syntax the source actually needs — the second
  because moving both numbers down together would satisfy the first and still break the package.
- `verify_norms_go_red` gained the three breaks it was missing: a credential-shaped literal in a
  tracked file (AC-10.2), a network import outside `connector/` (AC-10.4), and a backup overwriting
  its destination. AC-10.4's red run had previously been established by hand-planting an `httpx`
  import and removing it — the one-off that harness exists to replace.
- `ruff`'s `target-version` is now pinned rather than inferred. Raising the packaging floor had made
  the formatter rewrite `except (A, B):` into PEP 758's `except A, B:`: a field about installation
  was silently deciding how the source is written.

**Also recorded, and larger than this entry:** the six open MCP issues are one defect — a
well-formed answer that does not state its own scope, on six axes — and are planned as one work
cycle in `.prawduct/artifacts/discovery-mcp-answer-scope.md`. Measurement against the real fixture
falsified AC-9.1's "gaps >7 days" rule, which is amended here to measure against each account's own
cadence.

**Closes:** #4, #7, #12.

## 2026-09-08: A mistyped account id stops getting a confident empty answer

<!-- prawduct: scope=sync-v1 | release=v0.1.0 -->

**Why:** a fourth acceptance pass, again driving the tools blind, put it in one line — a typo in an
argument is loud and a typo in an account id is silent, and the silent one is the dangerous one.
`query_transactions{account_id: 999}` returned `rows: []` with no warning, byte-identical to a real
account that happened to be quiet in the window.

**What changed:**

- 🔴 **An `account_id` naming no account is refused.** The unadvertised-argument refusal already
  covered a mistyped argument *name*; this is the same mistake one field over, and quieter. An empty
  list is an entirely ordinary thing for an account to have, so the wrong answer invited no second
  look — while the caller most likely to be wrong, the one who mistyped, got the most
  confident-looking response.
- The check sits in the **query layer**, because only a datastore read knows which ids exist, and
  **after** the readability check, because an unreadable store knows nothing about accounts:
  "that account does not exist" is a claim about the data, not about the connection. The refusal is
  rendered at the **MCP boundary** beside its sibling, since how a caller is told is that boundary's
  to decide.
- **An account that exists and was simply quiet still answers `rows: []`**, held by its own test.
  A refusal that also swallowed the true empty answer would be a worse defect than the one fixed —
  and both tests were confirmed falsifiable by mutation before being trusted.

**Deliberately not fixed here:** an account that exists with *no feed at all* still answers `rows:
[]` with nothing to distinguish "no coverage" from "no activity". That is the per-account coverage
axis (#19), not this.

## 2026-09-08: Refuse the argument you cannot honour, instead of clamping it quietly

<!-- prawduct: scope=sync-v1 | release=v0.1.0 -->

**Why:** a second session driving the tools blind found that the previous round's `limit` fix had
replaced one silent answer with another, and that three more inputs returned a confident empty
result. Its framing is the one worth keeping: a clamp answers a question nobody asked.

**What changed:**

- 🔴 **`limit: 0` was served as one row.** The previous fix stopped it meaning 100; it then meant 1,
  which is *worse* — a single row reads as a plausible complete answer to a narrow question, where a
  hundred looked obviously wrong. Out-of-range values are refused now, not clamped, which is the
  rule the unknown-key refusal already follows.
- 🔴 **The row ceiling is declared, enforced, and set to the number the contract fixes.** It was an
  unnamed `1000` inside a `min()`, so a caller asking for 9999 silently got a page. Naming it
  surfaced that 1000 was never the contracted value: `api-contract.md` fixes the raw-row cap at
  ~500 under AC-9.1 ("a contract term, not a tuning knob"), `nonfunctional-requirements.md` makes
  it what keeps the sub-second target reachable, and `security-model.md` names it as the declared
  mitigation for unrestricted resource consumption (OWASP API4). `MAX_ROWS` is 500, quoted in the
  refusal and advertised in the `inputSchema`. One line of `security-model.md` had drifted to say
  1000 — a description tracking the code rather than the norm — and is reconciled.
  *(A tester read the absence of a visible cap as there being none; the cap existed, but nothing
  said so, which is the same defect from the caller's side.)*
- 🔴 **A window whose `until` precedes its `since` is refused.** It selects nothing, and "you spent
  nothing" is an entirely ordinary thing for a month to be — so the one slip a real person makes,
  swapping two bounds, returned a believable wrong answer. There is no window a transposed pair
  could mean, so there is nothing to guess at.
- `account_id` below 1 is refused; row ids start at 1, so 0 and negatives can only be a mistake.
- The window and both integers are narrowed **once**, ahead of the handler table. Referenced inside
  the lambdas they would re-parse per call and raise a refusal twice.

**A test was removed rather than kept**: `test_a_limit_of_zero_is_not_silently_a_hundred` asserted
that `limit: 0` serves one row. That premise is superseded — 0 is refused now, which is strictly
stricter — and the guarantee it protected is carried by a range test covering 0, negatives and the
ceiling together.

**Verified, and not fixed, because neither is ours:** `merchant_name: "FUN"` for `SparkFun` is what
the aggregator sends (`name` and `merchant_name` both read from the archived body); so is the
`GUSTO PAY` row whose description says "Credit" while its `amount` and its own
`personal_finance_category` both say money leaving. Sandbox fixture data, faithfully stored.

## 2026-09-08: The exception stops crossing the boundary, and a misspelled bound stops lying

<!-- prawduct: scope=sync-v1 | release=v0.1.0 -->

**Why:** the Critic found the fix above had left the leak it named. The change-log entry called out
a `StatementError` carrying the SELECT as the defect, and the new test pinned `"SELECT" not in
message` — but only on the date path. Every other failure still rendered
`f"{type(exc).__name__}: {exc}"` onto `isError`.

**What changed:**

- 🔴 **No exception crosses the boundary.** `api-contract.md` § Error Model: no stack traces, no
  internal identifiers. A SQLAlchemy error stringifies to the failing SELECT *and its bound
  parameters* — the schema and the operator's own money, handed to whatever is reading. The detail
  goes to the log, where redaction applies; the caller gets a stable code and a remedy sentence,
  which is what that section specifies and what nothing implemented.
- 🔴 **A test asserted the forbidden behaviour**, again:
  `test_a_failing_tool_reports_an_error_without_closing_the_session` asserted the raw exception
  message reached the client. Rewritten against the contract, and its fixture now raises a message
  containing `SELECT` so the assertion has something real to catch. Second instance in one day of a
  test pinning a defect; the first was AC-3.2's capability read.
- **`additionalProperties: False` is advertised on all four tools and was enforced on none.** A
  misspelled `sinceX` was silently dropped and `spending_summary` returned the ALL-TIME aggregate —
  byte-identical to the windowed answer the caller thought it had asked for. Unknown keys are now
  refused, naming **every** offending key at once and what the tool accepts, with the permitted set
  read back off `_tool_definitions()` rather than restated.
- **Four review observations closed in the same branch.** `limit=... or 100` was truthiness on an
  int, so `limit: 0` served the hundred-row default instead of the one row the query layer's
  `max(1, ...)` clamp defines. The refusal named only the first unrecognized key though it had
  sorted them all. The tool-name set was built three times in one call path, and the membership half
  of the dispatch guard was dead once the name began being resolved before dispatch. And an
  assertion read `"since" in message` against a message containing `'sinceX'` — a substring that
  could never fail, now an exact set.
- **`except KeyError` wrapped the handler call**, so a `KeyError` from anywhere beneath the query
  layer was answered `no tool named 'spending_summary'` as JSON-RPC -32601 — a false statement about
  a tool that exists. The tool name is resolved before the call now.
- Tool errors carry a stable code: `invalid_argument` is worth retrying with a corrected call,
  `internal_error` is not. A consumer could not previously tell those apart.
- `limit` was narrowed eagerly for all four tools, ahead of the unknown-tool check, with an
  unreachable fallback. Moved to where it is used.

**Verified through the live MCP channel**, which is what found the original defect and what the
recorded suite evidence could not speak to: an unknown key returns `invalid_argument` naming it, and
a real window returns rows.

## 2026-09-08: Every windowed question was unanswerable, and the checker was told to say so

<!-- prawduct: scope=sync-v1 | release=v0.1.0 -->

**Why:** `spending_summary` and `query_transactions` failed on *any* `since` or `until` with
`StatementError: (TemporalError) a calendar date must be a date, got str`. That is every question
the MCP server exists to answer. Found by a second session driving the tools; reproduced here
before anything was changed.

**What changed:**

- **Dates are parsed at the MCP boundary.** JSON has no date type, so `since`/`until` arrive as
  text and were passed straight to a `CalendarDate` column that refuses anything but a `date`.
  🔴 The `inputSchema` was never wrong — it advertises a string, and a string is what arrives; the
  gap was purely the missing narrowing at dispatch.
- **`query.list_transactions` and `spending_by_category` were annotated `since: str | None`** while
  their bodies required a `date`. The annotation was the lie that made the call site look correct.
  They take `date | None` now.
- 🔴 **`_dispatch_tool`'s argument bag is `dict[str, object]`, not `dict[str, Any]`.** This is the
  mechanism, not tidiness: under `Any` every JSON value flows into the query layer unchallenged and
  mypy strict is silent, which is exactly how this shipped. Typed `object`, an unnarrowed value
  cannot be passed at all — and the checker immediately found the same latent defect in `limit`
  (`int()` on whatever arrived) and `account_id` (forwarded unchecked). Both are narrowed now, with
  `bool` refused for `limit` because a JSON `true` is an `int` in Python and would have meant 1.
- **A malformed date now reads as a sentence**: `since must be a calendar date in YYYY-MM-DD form,
  got 'August 2024'` on `isError`, where a model can correct itself — not a `StatementError`
  carrying a SELECT. Deliberately not a JSON-RPC error code: the schema declares a *string* and
  "August 2024" is one, so this is the tool reporting on its input rather than a protocol violation.
- **Ten tests where there were none.** No existing test called either tool with a date at all —
  every one passed `{}`. The window tests assert *discrimination* (a window that excludes the data
  returns empty while one that includes it returns rows), because a parse that silently produced
  the wrong date would satisfy "it did not error". Two go-red cases (114 total).

🔴 **The positive control earned its place.** The mypy-snippet test named its files `narrowed.py`
and `unnarrowed.py` — and `"narrowed.py" in line` matches both, so the control matched the negative
file. Same containment trap recorded in `learnings.md` hours earlier, third instance in one day.
The files are renamed so neither contains the other; a cleverer match would have left the trap.

**Also corrected:** the mypy test pins `query.py`'s signature, not `_dispatch_tool`'s bag — widening
the bag back to `Any` leaves it green, because the snippets declare their own signature. The
annotation is pinned by its own test, and the docstring no longer claims otherwise.

## 2026-09-08: Capabilities are both product lists, and AC-3.2 stops inverting

<!-- prawduct: scope=sync-v1 | release=v0.1.0 -->

**Why:** the first live enrollment against a second sandbox institution printed
`capabilities: balance` for a connection that was, at that moment, syncing transactions and
holding a 401k. Running the product found it; 547 tests did not.

**What changed:**

- `capabilities_of` reads the **union** of `products` and `available_products`. The aggregator
  documents `available_products` as *"products available for the Item that have not yet been
  accessed"*, mutually exclusive with `billed_products` — so a product already initialized is
  guaranteed absent from it, and reading it alone recorded a connection as incapable of exactly
  what it was doing. AC-3.2 pulls investments for any connection whose recorded capabilities
  include investments, so the criterion was inverted for precisely the connections that have
  investments. Measured against the archived `/item/get` body: `["balance"]` before,
  `["balance", "investments", "transactions"]` after.
- 🔴 **A test asserted the defect and had to be corrected, not satisfied.**
  `test_capabilities_answer_what_the_connection_could_do_not_what_we_asked_for` asserted
  `"transactions" not in capabilities` as its tell that the wrong field had been read. The
  guarantee it was protecting is real — a read of `products` alone discovers nothing — but the
  assertion it chose contradicted the AC-3.2 it cited. It now asserts the union, which catches a
  wrong read from *both* sides where it previously caught one, and the go-red case that guards it
  was re-pointed accordingly. This is a strengthening; it is recorded here because "the test was
  failing so I changed it" is the shape a genuine weakening also has.
- `capabilities_of` had **no direct unit test** — it was reached only through enrollment. Four now
  cover it: a product initialized-only, a product available-only, the union, and a refusal naming
  which list the aggregator withheld. A new go-red case (111 total) breaks the union in the
  direction the old one could not see.
- The history-shortfall line read `a 8-day gap`. Reworded to `a gap of N days`, which is correct
  for every number rather than for the ones nobody had hit yet.
- `docs/connecting-an-mcp-client.md` claimed a sandbox connection "typically grants 90 days
  against 730 requested". A live one granted **722**. The doc now says the grant varies and cannot
  be predicted, and cites the measurement with its date and institution.
- 🔴 **VRF-004 step 2 was written around that 90-day figure** and could not be run as specified.
  Rewritten to ask about a period *outside* the granted window — the sharp form, which works at any
  grant size, where an 8-day shortfall is far too small to probe whether a model reads warnings.

**Deliberately not done:** the already-enrolled sandbox connection keeps `["balance"]`.
`connections` carries no `derivation_version_id` — capabilities are written at enrollment, not
derived — so no rebuild recomputes it, and nothing reads the column until build step 5. There are
no production connections. A migration path here would be backwards compatibility for a deployment
that does not exist; re-enrolling the sandbox connection corrects it for free.

## 2026-09-08: Transaction sync, and the first MCP slice — the product can be asked questions

<!-- prawduct: scope=sync-v1 | release=v0.1.0 -->

**Why:** build step 4, and the first slice of step 7. Enrollment gave the product a connection;
this gives it data, and a way to be asked about it.

**What shipped.** `bankmachine sync run` pages each connection's changes since its cursor, applying
additions, modifications and removals, and `bankmachine mcp` serves the result to an MCP client over
stdio, read-only. Verified end to end against Plaid's sandbox rather than only fixtures: 14
accounts, 50 transactions, 14 daily balances, and real aggregates answering real questions.

🔴 **The probe reshaped the loop before a line of it was written.** A `NOT_READY` reply carries
`has_more: false` *and* an empty cursor, so the obvious `while has_more:` terminates on the first
sync of every new connection and records a **successful run with zero transactions**. Nothing
raises; `last_success_at` gets stamped; the account reports no activity. That is a successful
response computed over data that has not materialized — this product's named primary failure mode —
and the trap is that the naive loop satisfies AC-2.1's *"loop until the source reports no more
pages"* literally while being wrong. The status is read before `has_more`.

**The cursor commits with the rows because it is derived from the same body**, not because a caller
remembered to wrap them. AC-2.1 and AC-2.5 are one requirement stated twice, and every
implementation satisfies or violates both at that seam.

🔴 **AC-11.8 earned itself on the first real sync.** The connection requested 730 days of history
and the aggregator granted **90** — a 640-day gap, reported rather than the returned window being
read as complete. It is measured only at `HISTORICAL_UPDATE_COMPLETE`, because at
`INITIAL_UPDATE_COMPLETE` the backfill is still arriving and the number would be plausible, well
formed and wrong.

**Every MCP answer carries its own caveats and its own environment.** `query.Answer` cannot be
constructed without warnings — they are computed in the same call as the rows — because the consumer
is an agent that cannot see a caveat which is not in the payload. And a flag selects while the
envelope confesses: a server pointed at sandbox and one pointed at real money are otherwise
identical in their output, so the environment rides every response and the server's own title.

**No `mcp` dependency**, and that is a decision (`api-notes-plaid.md` §18). The official SDK resolves
to 29 packages including uvicorn, starlette and httpx2 — an HTTP server *and* client stack — against
five direct dependencies and a ratified norm that the aggregator is the only network destination.
That norm's own recorded limit is that the import scan cannot see a dependency phoning home, so
adopting the SDK would load the mechanism exactly where it is weakest, for transports this server
does not use. The wire format was read from the SDK's own types instead.

**Four of the ten specified tools ship**, recorded as a dated descope rather than left to be
discovered by comparing a contract claiming ten against code answering four.

**What this cost, and the pattern in it.** Five review rounds. The recurring defect was not in the
code but in the checks: **six times a test asserted something it did not exercise.** A fixture that
never reached the guard it named; a race test that blinded the check it was testing; a
credential-absence assertion reading a capture that collected nothing; the go-red harness itself,
which counted a mutation that did not parse as caught, hiding two always-passing cases; a
cursor-atomicity claim that held positionally, so turning `ROLLBACK` into `COMMIT` left the suite
green; and a test whose docstring said *"the server refused to start"* which never called the
function that refused. The last one guarded a requirement I had implemented **backwards** —
AC-ARCH.3 says the MCP server starts against a missing datastore and reports it, and I made it
refuse, then documented the refusal as a feature.

**And the bug no test found at all**: `parse_float=str` keeps decimals as text but leaves JSON
*integers* as `int`, so a whole-dollar amount arrives as `500`. Every fixture used a fractional
amount. 505 tests passed while the first real sandbox sync refused every whole-dollar transaction it
fetched. Running the product is not a formality.

**Verification queued rather than claimed:** VRF-004 — every tool is tested and the warnings are in
every payload, but no test can say whether an *agent* reads them.

## 2026-09-07: Enrollment — one institution linked, and the states that exist after the token is spent

<!-- prawduct: scope=enrollment-v1 | release=v0.1.0 -->

**Why:** build step 3. The product could reach the aggregator and archive what it said, but it could
not link an institution, so every table below `connections` had nothing to hang from.

**What shipped.** `bankmachine enroll` prints a hosted enrollment URL, waits while the operator
completes Link in a browser, and polls for the result — no local web server and no frontend, which
is what AC-1.1 asks for. `bankmachine connections list` and `connections retire` came with it rather
than after it, because the cap refusal has to name a command that exists. The connection cap is
configuration (AC-1.5), retirement keeps every row the connection produced (AC-1.6), and
re-enrolling converges on one live connection per institution (AC-1.4).

**The window is confirmed before the exchange**, because that is the last moment AC-1.2 is
reversible. The required argument on `link_token_create` stops a caller *forgetting* a window;
nothing but a human reading it stops one sending the wrong window, and the result is immutable for
the life of the connection. The window prints before the URL, since an operator who has already
opened the browser has stopped reading the terminal.

**AC-1.3 was split, and the schema had been right all along.** It required enrollment to record "the
history window actually granted", and no response in the enrollment path carries that value —
verified against the pinned SDK at all three candidates. `core_schema.py` had carried a comment
saying so since build step 1, so the DDL and the requirement had disagreed from the day both
existed. **AC-1.3a** homes the granted window where it is first knowable, at the initial backfill,
and gives null an explicit meaning: *not yet known*, never *no shortfall*.

**Retirement is two-sided.** Setting `retired_at` frees a slot in this product's own cap and does
nothing at the aggregator, where the Item keeps counting against the plan and keeps billing. So
retiring calls `/item/remove`, and so does a re-enrollment — Link mints a *new* Item, and
overwriting `source_connection_id` would otherwise drop the only reference to the old one while this
side showed one tidy connection and reported success. AC-1.4 says re-enrolling updates rather than
duplicates; without that call the duplication merely moves to the far end, where nothing here can
see it.

**The post-exchange window is the part worth remembering.** Past the exchange the aggregator holds an
Item the operator is billed for, and any local failure leaves them paying for a connection nothing
here records. The access token reaches the keychain before the first write, so every failure in that
window can name the item and the credential; the cap race *releases* the Item it just minted, because
refusing to record a connection while leaving the operator billed for it is not a refusal but a charge.

**What this cost, and what it taught.** Four review rounds. Every substantive defect had one shape —
an Item that exists at the aggregator, spent and billable, invisible from here — and once that harm
was named precisely, three more instances of it were findable by looking for the harm rather than for
bugs. Four separate checks turned out to be claims: a test whose fixture never reached the guard it
named, a race test that blinded the very check it was testing, a credential-absence assertion reading
a log capture that collected nothing, and — worst — the go-red harness itself, which judged "caught"
by pytest's exit code and so counted a mutation that did not even parse. That last one had two
always-passing cases hiding behind it, one of which had been green since the day it was written.

**Verification queued rather than claimed:** VRF-003 — a Hosted Link session cannot be completed
programmatically, so whether the printed page reads unambiguously to someone about to make an
irreversible choice is a human's judgement, not a test's.

## 2026-09-07: What the cumulative review changed about the derivers

<!-- prawduct: scope=connector-v1 | release=v0.1.0 -->

**Why:** the bundle review of build step 2 found one blocking defect and two design errors, and
all three were mine to have caught.

🔴 **A day's balance was being overwritten, and three ratified records say it is rejected.**
AC-3.1, `data-model.md` and the DDL comment above `balances_daily` all say the same thing: a
second capture on a day already recorded is *rejected*, so the series does not depend on what time
of day anyone happened to look. My deriver upserted, and the reinterpretation that justified it —
"AC-3.1 is about the series" — lived only in a comment in the deriver itself. **That is a
normative change, and a comment is not where one gets made.** The rule now conforms: the first
capture for a day wins, decided by comparing captures rather than by arriving first, so a replay
in any order lands on the same row. AC-2.4 was measurably breached too — re-deriving rewrote
`raw_response_id` and `captured_at`, and the idempotence test compared only the two tables where
it held. The sharpest case had no test at all: a manual-import row was overwritten into an
aggregator row, which the *next* rebuild deletes, so an operator's hand-entered balance would
vanish one rebuild later with nothing connecting the loss to the sync that caused it.

🔴 **The institutions deriver was writing a catalogue page into the roster.** `/institutions/get`
serves the aggregator's *production* catalogue — 10,083 US institutions with real names and
routing numbers — and `institutions` is the table `connections` hangs off. After a
`connector check` and a rebuild, the roster would hold banks the operator never linked, with
nothing to tell them apart and nothing that removes them, because a rebuild never empties that
table. The institution now comes from `/item/get`, which carries exactly the one this connection
belongs to. `/institutions/get` is registered as deriving **nothing**, by name: "archived and
implies no rows" is a real answer, and left unregistered it is indistinguishable from the endpoint
nobody got round to.

**`/item/get` was archivable with no deriver**, which would have made every later rebuild refuse
the whole archive — on a row that cannot be removed. The test named for that check asserted a
hand-written pair, which is why adding the endpoint did not turn it red; it now derives the
expected set from the endpoints that can produce a `FetchedResponse`, so the next one fails in the
commit that adds it.

**Two more where a shape held for a reason narrower than the claim above it.** The retry channel's
safety argument — nothing inside it writes — is about the *local* side, and an exchange spends a
single-use token at the far end; `Endpoint.retry_safe` now carries that. "The connector persists
nothing" stopped being true when the derivers landed: a deriver writes rows through a handle the
caller owns. The property that actually holds, and the one AC-5.1 rests on, is that nothing under
`connector/` can *obtain* a handle — narrower, and true.

**Smaller, and each one a thing that would have read as fine:** `_upsert_account` applied one
values dict to insert and update, so derivation permanently owned `balance_class`, which
`data-model.md` declares operator-correctable; the registry argument was optional everywhere with
a default that could only fail; `get_logger` doubled its own prefix, so every new log line read
`bankmachine.bankmachine.…`; the terminal failure — the one that ends a sync — was the one going
unlogged; and the credential guard's exemption read one line at a time, so a secret inside a
multi-line string was exempt on every line but the first. That last one is now answered with
`ast` rather than quote-counting, after the counting version started reporting its own tests.

**Three obligations this plan cannot discharge are written into it** rather than left to be
noticed: wiring `config.history_days` into enrollment, the granted window that is unobservable
until step 3's first real connection, and account retirement.

## 2026-09-07: Enrollment, the derivers, and the archive exemption made structural

<!-- prawduct: scope=connector-v1 | release=v0.1.0 -->

**Why:** build step 2's remaining half. Chunk 03 adds the calls enrollment will make —
`/link/token/create`, `/item/public_token/exchange`, `/item/get`, `/accounts/get` — and Chunk 04
registers the first derivers, so `store rebuild` runs end-to-end over an archive of real responses
instead of refusing one for want of a deriver. Build step 2 is complete.

**Three things are held by construction here, because each is a mistake nobody notices making.**

- 🔴 **The history window is a required argument with no default.** AC-1.2 makes it immutable after
  enrollment and the requirements call a vendor-default build a failed build, so forgetting it is a
  type error — and the test runs mypy, because asserting at runtime that a window was passed tests
  the call site in front of it rather than the property that no call site can omit it. The maximum,
  730, is read off the SDK's own request validation and a test compares the two, since
  `plaid-python` is not pinned and the number can move under us.
- 🔴 **A credential-bearing response cannot become an archivable one.**
  `Endpoint.issues_credential` marks the two endpoints whose body carries a credential, and
  `FetchedResponse` refuses to exist for such an endpoint — so there is no object to hand the
  archive. A list of exempt paths beside the archive would be an enumeration standing in for a
  property, and this project has been burned once already by a rule matching a name where it meant a
  relationship. `raw_responses` is append-only, so a token written there is written permanently and
  travels with every backup.
- 🔴 **A call the far end cannot absorb twice is never retried.** The Critic caught this: the retry
  channel's safety argument — that the connector persists nothing, so a second attempt has no
  partial write to interleave against — is about the *local* side only. An exchange spends a
  single-use public token and mints a durable Item at the aggregator, so a retry after a
  transport failure either fails on a spent token or enrolls twice. `retry_safe` is now a property
  of the endpoint, with a control proving reads still retry.

**Capabilities read `available_products`, not `products`.** AC-3.2 pulls investments for any
connection whose capabilities include them and never for a named institution — and `products`
answers "what did we already ask for". A discovery reading it would report back this product's own
request, discover nothing, and pass every test asserting that discovery happened. Measured against a
live item: `products` is `['transactions']`, `available_products` has fourteen entries.

**The derivers, and the one place this build rounds.** Institutions and accounts converge on their
natural keys rather than inserting, because a rebuild never empties them — their local ids are what
every row of history references (AC-6.3), and reassigning them would orphan it. `first_seen_at` is a
minimum and `last_seen_at` a maximum, so replay order cannot change the result, which is what makes
the order-independence property a property of the arithmetic rather than of today's `ORDER BY`.

Money never touches a float: bodies are parsed with `parse_float=str`, so an amount arrives as the
digits the aggregator sent. The exception is deliberate and was the owner's call. Plaid's own
sandbox institution returns a 401k balance of `23631.9805` USD — canned data, so the aggregator is
deliberately exercising the case and sub-cent valuations are a production shape. An investment
`current` is price times quantity, computed rather than transacted, and no brokerage statement
reports hundredths of a cent, so it is rounded **half-even** (half-up would bias a portfolio upward a
fraction of a cent at a time, forever), **logged every time**, and the archive keeps the exact
original. The distinction that makes this legitimate — a *valuation* is not a *ledger amount* — is
recorded, not assumed.

**The sign convention now has the mechanism it was ratified in-transition without** (issue #9). A
liability's balance is stored negative whatever sign the source used; several aggregators report a
card balance as a positive amount owed, and a consumer taking that at face value is wrong by twice
the debt, silently. `available_minor` and `limit_minor` are the documented exceptions and keep their
magnitudes — asserted explicitly, or a later change that signed every column alike would look like a
tidy-up and pass.

**A guard caught its own author, twice.** AC-10.2's credential scan fired on this work's test data,
which was right and the test data changed. It also fired on a keyword argument forwarding a
same-named variable in the product code — ordinary Python, and the shape that gets a guard narrowed
in irritation later — so the exemption is now principled: in Python source an unquoted bare
identifier is a reference, never a literal. The Critic then found the hole in *that*: an unquoted identifier inside
a comment or a docstring is text, and text is where a secret gets parked "temporarily". Four edges,
a control on each.

## 2026-09-07: The connector's error taxonomy, and the retry channel it feeds

<!-- prawduct: scope=connector-v1 | release=v0.1.0 -->

**Why:** FR-4 is written in four connection-health states — auth-required, locked,
institution-down, rate-limit — and until this chunk every aggregator failure reached the
caller as one `ConnectorError` carrying a sentence. A sync loop cannot honour AC-4.1's
"one broken connection never aborts another" against a single type, and cannot record
AC-4.2's error code or compute AC-4.5's data hole from a message.

**What shipped.** `connector/plaid/errors.py` maps the aggregator's vocabulary onto local
types defined in `connector/__init__.py` — outside the `plaid` subpackage, so catching an
aggregator failure never requires importing the aggregator. The types are organized by
**what the caller must do next** rather than by what the aggregator called it: Plaid's own
`ITEM_ERROR` spans re-link, go-to-your-bank and nothing-to-sync, and a consumer switching on
it would send the operator somewhere that cannot help them. Every failure carries its
connection, its code, its request id and the instant it happened. Alongside it,
backoff-and-retry on the one channel `architecture.md` permits it on (AC-2.6), with the
clock injected so the suite exercises the real schedule at full speed.

**Three decisions worth reading.**

- **Retryability is a property of each error type, never a list in the retry loop.** A list
  goes stale the moment someone adds a type without visiting that file, and it fails in both
  directions: an un-retried transient stops the nightly sync, a retried permanent one hammers
  the aggregator with a call that cannot work. `retryable` has no base-class default, and
  `__init_subclass__` refuses at *class creation* a type that never decided. The first version
  of this used a test walking `__subclasses__()`; the Critic pointed out that walk sees only
  modules that have been imported, so it would have guaranteed something about the types one
  test file happens to import — and the case it misses is the second aggregator this package
  is explicitly shaped for. That test is consolidated away, because the class-creation guard
  makes it a check that can no longer fail.
- **`ITEM_ERROR` is deliberately absent from the classification's coarse layer.** Mapping it
  would be confidently wrong two times in three, and a wrong remedy is worse than a refusal.
- **An unrecognized code gets its own type, not a neighbour's.** Filing an unknown refusal
  under "probably transient" hides a connection that will never recover; under "probably
  terminal" it retires one that only needed a retry. Codes this build *recognizes* but has no
  FR-4 class for say so in as many words, which is the difference between a gap someone chose
  and a gap nobody noticed.

**A dependency bug, contained narrowly.** `plaid-python` 44.0.0's `api_client.py` calls
`e.body.decode('utf-8')` on a body it just set to `None`, so one class of SSL failure leaves
the SDK as `AttributeError` rather than as anything catchable. It is caught here and
**re-raised untouched unless the SDK's own exception is standing behind it** — catching
`AttributeError` around a call would swallow every genuine typo in the module and report it
as a network problem, so the narrowing has its own negative control. Getting there took two
probes that disagreed with each other and with the source reading; `api-notes-plaid.md` §9
records which SSL failures actually reach which path.

**What the Critic caught, and it is the same shape twice.** A `Retry-After` header was
honoured as a floor with no ceiling, so the aggregator could ask this product to sleep for an
hour inside one nightly sync — bypassing `max_delay_seconds`, the field whose whole job is
bounding the wait — and `float("inf")` passed the only guard on it, which `time.sleep` turns
into an `OverflowError` past the boundary. Both are external input reaching a wait, and the
docstring one line above promised a bound the code did not enforce: **the prose was right and
the mechanism was weaker, so the mechanism was raised.** Separately, the response body was
read outside the exception mapping, so a reset connection escaped as a bare `OSError` from a
connector whose whole contract is that its failures are typed.

**Two amendments to the plan, both stated rather than absorbed.** The chunk's sandbox test
asked for `/sandbox/item/reset_login` to drive a real `ITEM_LOGIN_REQUIRED`; that needs an
enrolled Item, which needs Chunk 03's exchange call, so it moves there. What runs instead is
still live — invalid credentials reach the same host and come back with the real error shape,
and the taxonomy now tells a wrong secret from a malformed call against the real server, which
is the failure VRF-002 item 5 was written to catch. And the declared "error fixtures" now
exist and are recorded verbatim from real rejections, replacing three hand-written body shapes
that had drifted into two test modules; the offline suite builds every constructed body from
that one recorded shape, so a change in the aggregator's error body moves them all together.

## 2026-09-07: The strategy artifacts, and the two gaps writing them exposed

<!-- prawduct: scope=strategy-artifacts | release=v0.1.0 -->

**Why:** `/prawduct:doctor` reported the coverage chain stuck at layer 1 — six expected
strategy-class artifacts had never been created. They are now written, in the dependency
order the planning guide sets: data model, non-functional requirements, security model, API
contract, observability strategy, operational spec. This was **reconciliation, not
invention**: `docs/system-requirements.md` and `project-state.yaml` already held nearly all
of it, and what the artifacts add is the shape — each criterion sitting next to the decision
that motivated it and the code or test that discharges it. Each marks what is *built* versus
*specified*, because four of the six describe surfaces that do not exist yet.

**Writing them surfaced two real gaps, and both are closed here rather than filed.**

🔴 **AC-10.2 was never implemented.** The criterion asks for a test that greps a fresh
`git ls-files` for token-shaped strings; what existed was `check-no-personal-data.sh`, which
hunts *roster* tokens supplied by `deployment/`. **Neither subsumes the other** — a roster
name is not token-shaped, and a leaked access token names no institution, so a stray
credential matching no institution walked past every guard in the repository.
`tests/preferences/test_no_credentials_tracked.py` closes it: `git check-ignore` per AC-10.2
clause with a negative control that `.env.example` stays tracked, plus a shape scan for
access-token prefixes, 64-hex key runs, and labelled credentials with a real value.

Its one exemption is a **per-line declaration, not a file skip list**, and the distinction is
the design. A skip list exempts the *next* real secret to land in that file and nobody
decides anything; the marker `credential-shape: test vector` exempts one line and appears in
the diff of whoever adds it. It is used once, on the redaction test's own fixtures, which
must carry real credential shapes or they prove nothing. A test asserts the marker does not
spill onto neighbouring lines — **it caught exactly that bug while being written.**

🔴 **There was no backup at all**, against a datastore key that cannot be recovered once lost
and a `balances_daily` series no re-sync can rebuild. `bankmachine store backup` now writes a
verified, consistent, single-file encrypted copy.

**The measurements that shaped it, none of which came from documentation.** `VACUUM INTO`
from a read-role handle **fails** under `PRAGMA query_only=ON` (`SQLITE_READONLY`) *and
leaves a zero-byte file at the destination* — a file indistinguishable from a backup until
the day it is needed, which is the single most dangerous artifact this command could
produce. So the copy is taken from the writer factory, which is also the right answer for an
unrelated reason: it holds the exclusive lock, so consistency is a consequence of the lock
rather than of timing. The copy folds the WAL in — measured against a source holding a 2 MB
hot WAL, whose 300 rows all appear in the copy while a plain `cp store.db` is short of every
one of them. That comparison is a **negative control in the test**, so the command is known
to differ from `cp` rather than assumed to.

The tests also caught a defect in the first draft: `back_up` leaked a raw driver
`OperationalError` instead of a named `StoreError`, so the CLI would have printed a
traceback where every other failure in this repository prints a sentence.

**17 norms ratified** (`norm_registry_ratified: 2026-09-07`), homed in the `## Direction`
sections of the four artifacts that govern them, with pointer rows in
`project-preferences.md`. Sixteen are steady-state. **One is `in-transition` on purpose:**
the operator-POV sign convention, tracked by `#9`. Nothing tests it, and the connector that
must obey it is mid-build — and the failure it guards against has no symptom, since an
aggregator reporting a card balance as a positive amount owed makes a consumer wrong *by
twice the debt*, silently and plausibly. Ratifying it steady-state with no mechanism would
have been the aspirational failure the lifecycle exists to prevent.

Two norms bind the **not-yet-built** MCP surface — read-only, and freshness-plus-warnings on
every response. That follows this repo's own precedent: `architecture.md`'s four norms were
also born before their code, and the point is that step 7 is *built to* them rather than
discovering them.

Where a norm has no mechanism, the Enforcement row says `Critic` and names nothing. Two data
norms are recorded that way deliberately — the schema makes "never overwrite a source value"
and "never hard-delete" *possible* to obey, not *impossible* to break, and naming a
constraint that does not constrain would overstate the guarantee.

**AC-10.4 got a mechanism too:** `test_only_the_connector_reaches_the_network.py` asserts
nothing outside `connector/` imports a network transport — verified red by planting an
`httpx` import, then green. Its limit is recorded in the norm rather than left implied: it
cannot see a subprocess shelling out to `curl`, nor a dependency phoning home.

**What the cumulative Critic caught, and it was worth the round.** 0 blocking, 14 warnings,
8 notes across three reviewers, who converged independently on one theme: `store backup`
shipped ahead of its own governance. The fixes, in this same bundle:

🔴 **A test that could not fail.** `test_it_leaves_no_zero_byte_file_when_it_cannot_write`
chmod'd the parent to `0o500` and then asserted the destination did not exist — true
*before* `back_up` ran, in a directory nothing can create a file in. Worse, the guarantee
it claimed to check was **inherited from `VACUUM INTO`**, not enforced by this module,
whose own docstring records a measured failure that leaves a zero-byte file. Both halves
are fixed: the failure path now unlinks the destination itself, and the test reproduces
the real hazard in a *writable* directory by swapping the writer factory for the read-role
one. A negative control neutralizes the cleanup and confirms the driver genuinely does
leave debris — so the unlink is pinned rather than decorative. This is exactly the
vacuous-fixture failure the test-evidence prompt names, written by the same hand that
quoted it.

🔴 **The credential guard exempted its own file.** `if path == Path(__file__): continue` —
a file-level skip list, in the module whose docstring argues against file-level skip lists,
covering the one file where a credential-shaped literal looks normal to a reviewer. It now
scans itself and declares its own vectors per line, like any other file.

**`BackupDestinationExistsError` was raised for a missing parent directory** — a misnomer
that sends the operator looking for a file that is not there. Split into
`BackupDestinationUnusableError`; the remedies are opposite.

**`store backup` was graded `stable` on the day it was written**, against the inventory's
own criterion (*shipped and depended on*), while mid-build `connector` commands sat at
`experimental` — and the `Retention:` rule defers removal of a stable member to a major.
Now `experimental`, with the reasoning recorded.

**The append-only norm claimed `Test` for a mechanism that does not enforce it.** A
composite primary key rejects a duplicate INSERT but permits `UPDATE`, `DELETE` and upsert
— and AC-2.4's idempotency requirement is precisely what will tempt the sync writer toward
`ON CONFLICT DO UPDATE`. Recorded `Critic` with the partial structure named, matching the
discipline the same bundle applied to the source-overwrite and hard-delete norms. Claiming
`Test` would have had the janitor sweep read it as machine-checked, and the guard for the
one series no re-sync can rebuild would never have been written.

**Two coherence defects in the records themselves:** `project-state.yaml` asserted the new
artifacts declare no `## Direction` section thirty lines above a registry saying the 17
norms are homed in those sections; and `operational-spec.md` re-homed architecture.md's
implicit-creation norm while both files recorded that nothing was restated. One rule now
has one home — operational-spec keeps the backup half and cites architecture for the
datastore half. `architecture.md`'s canonical command table, which declares itself
canonical precisely so a command set is not restated in four places, has regained the three
commands it was missing.

Also added: the `store backup` CLI surface had no test at all (four now), and the backup
path wrote no log record, so an unattended failure left only an exit code.

**The verify pass then caught what the first round of fixes had left.** Three residuals,
all of the same shape — a claim with nothing behind it. `operational-spec.md` now states
as a *guarantee* that the failure message says which happened, so the message and the log
records are asserted (`match=` on the raise, `caplog` on both paths) rather than left as
prose a refactor can drop while staying green. The failure paths themselves were silent:
an unattended run left a "backup starting" line and then nothing, which reads exactly like
a run still in progress. And `README.md`'s "what exists today" paragraph still omitted
`store backup`, so the three command lists the canonical table exists to keep in agreement
were still disagreeing — the point of that table is that a command set restated in four
places is four places to disagree.

**One reviewer observation was wrong and is recorded as such rather than acted on.** It
reported that the R-13 disposition claimed here does not exist; running the command again
returned `supersedes disp:...:R-13:1`, so version 1 was on record all along. The manifest's
`prior_dispositions` evidently does not carry the full set. Checked rather than believed,
because a fix applied to a defect that is not there is a change with no reason.

**Accepted rather than fixed**, recorded as dispositions: the fourth copy of the AST import
scanner (extraction would edit three tests this bundle does not touch), and `data-model.md`
being a third uncompared description of the frozen DDL — a real drift risk that wants a
construction of its own, now tracked as `#11`. *(The absent parent requirement for
`store backup` was on this list until the PR review; it was fixed rather than accepted, and
leaving it here would have had the entry contradict itself two paragraphs later.)*

**The PR reviewer then caught the scope trace.** `store backup` had no parent
requirement anywhere: `docs/system-requirements.md` carries no backup criterion, and
`project-state.yaml`'s `scope.later` said *"Multi-machine or backup-restore automation"* —
which reads as deferring backup out of v1 entirely, in the bundle that ships it. The
capability was properly reached and consumed; only the trace was missing. `scope.v1` now
names the manual command and says why it is v1, and `later` is sharpened to the half that
genuinely is deferred: scheduling, retention, and a rehearsed restore.

It also measured a sentence in `pyproject.toml` that was simply false. The
`[tool.ruff.format]` rationale said *"`ruff check` still lints these files"* — it does not:
`ruff check` on a `.md` path reports "No Python files found" and lints nothing (confirmed
against the 0.16.6 this commit pins). The exclude is right and load-bearing; the sentence
explaining *why it is scoped to the formatter* was wrong, which is the sentence a reader
checks first. Replaced with the measured reason.

And three deferrals that existed only as prose are now filed — `#10` (schedule the backup,
rehearse the restore), `#11` (`data-model.md` is a third uncompared description of the
frozen DDL), `#12` (`verify_norms_go_red` does not know the three newest norms). The § Owed
table claimed its gaps were "filed rather than rediscovered" while the highest-value one had
no item; it now cites `#10`. `#11` and `#12` are not operational gaps and correctly do not
appear there.

**Still open, and named rather than quietly carried:** nothing *schedules* the backup, and
the key is still backed up by hand — the command cannot do that half without defeating the
keychain. Restore has no runbook and has not been rehearsed end to end by a human.

## 2026-09-06: VRF-002 discharged — the connector's live half, and what the sandbox really serves

<!-- prawduct: scope=connector-v1 | release=v0.1.0 -->

**Why:** Chunk 01 shipped unticked on purpose. Its success path had never been probed —
no sandbox credentials existed on this machine, so `tests/connector/fixtures/` was empty
and the two `sandbox`-marked tests skipped. Credentials arrived. This is what running the
gate produced, including the part the gate got wrong about itself.

**Chunk 01 is now `[x]`.** `bankmachine connector check` completed against the real
sandbox, archived 677 bytes as `raw_response 1`, and reported the aggregator's own `total`
of 10,085 institutions rather than the single record on the page.
`BANKMACHINE_RECORD_FIXTURES=1 uv run pytest -m sandbox` recorded
`tests/connector/fixtures/institutions_get.json`, closing Done-when 0b — the last of the
plan's `verify-api` findings, and the only one that needed a credential to reach.

**What the live call established that the fake could not.** The SDK hands the bytes over
undecoded against a real server, not only against a stub: `_preload_content=False` behaves
in the wild the way `api_client.py`'s source said it would. That is the half of AC-5.1
`project-state.yaml` refuses to accept mocked, and it is now evidence rather than a
reading.

🔴 **The verification's own premise was wrong, and the correction outlives the item.**
VRF-002 item 7 asked the operator to confirm the recorded fixture held "sandbox
institutions only". The sandbox's `/institutions/get` serves no such thing — it serves the
production institution catalogue, real names and real routing numbers, 10,085 of them for
`US` alone. Two consequences, both recorded in `api-notes-plaid.md` §7. Institution shapes
recorded from sandbox *are* production shapes, so the connector plan's §4 risk — "sandbox
shapes are not production shapes" — is narrower than written for this endpoint, while
standing exactly as written for the accounts and transactions Chunk 04 also depends on.
And what makes a recorded fixture safe to commit is the leak guard, not the word
"sandbox": `check-no-personal-data.sh` reports clean over the working tree with the fixture
in it, which is the check that was actually run.

**A second error code, for free, from a mistake.** The production secret was set against
the sandbox host first. That returns `400 INVALID_API_KEYS: invalid client_id or secret
provided` — a different code from the `INVALID_FIELD` a *malformed* credential returns.
Chunk 02's taxonomy now has both from observation rather than from the docs, and the
distinction is one an operator acts on: rotate the credential, or fix the call. VRF-002
item 5 exists to catch exactly this being reported as a network fault, and it was not.

**Also in this bundle:** `.env.example` — the client id and the optional overrides, as a
file to `source` rather than one anything reads silently. It carries no secret and says so
in its own text: the aggregator secret has no environment variable by design, and
`connector set-secret` puts it in the keychain, per environment.

## 2026-09-06: the connector's walking skeleton — the product reaches the outside world

<!-- prawduct: scope=connector-v1 | release=v0.1.0 -->

**Why:** build step 2 is the aggregator client, and every later step reads through it.
Chunk 01 proves the whole path before widening it: configuration resolves, the keychain
yields a secret, the aggregator answers, and the answer lands in the archive verbatim.
It asks for the smallest thing the aggregator will tell anyone — one page of the
supported-institution list — because that needs client credentials and nothing else, so
the path is provable before enrollment exists.

**What shipped:** `bankmachine connector check`, which reports what it fetched and logs the
archived response id so the unattended job later has a record; `connector set-secret`, which
prompts without echoing at a terminal and reads a pipe when given one, so a secret reaches
neither the shell history nor a process listing; the `connector/` package with the aggregator
SDK confined to `connector/plaid/`; aggregator credentials added to the existing Credential
Seam rather than a second one; and the `Endpoint` vocabulary that `DERIVERS`, the
credential-archive rule and AC-ARCH.4's guard all turn out to need.

**The boundary decision, and why it is a package rather than an interface.**
`system-requirements.md` §9.2 — is a second aggregator ever expected — is answered: one in
v1, contained so a second is a new module rather than a rewrite. A client `Protocol` with a
single implementation would encode that implementation and call it a contract; the honest
version cannot be written until a second aggregator exists to disagree with the first. The
mechanism is `tests/preferences/test_connector_is_contained.py`, holding two properties:
nothing outside `connector/plaid/` imports the SDK, and nothing in `connector/` imports a
module that hands out a datastore handle. The second is the load-bearing one — AC-5.1's
"archive before normalize" is not a rule anyone follows here, because the connector has no
way to write at all.

🔴 **The response is taken undecoded, and this was the finding that shaped the client.**
The SDK deserializes into generated models by default, and those models silently drop
fields they do not know about — which are exactly the fields a later `store rebuild` would
need to reproduce rows the aggregator has since started sending. Handing the archive a
model round-trip would have satisfied AC-5.1's letter and destroyed its point. Every call
passes `_preload_content=False`, verified against the SDK's own source rather than its
documentation, and held red by `verify_norms_go_red.py`.

**Two things reading the code first caught that drafting from documentation would not.**
The credential-archive exemption was already decided in build step 1 — `store/raw.py` says
so, and names build step 2 as where it stops being a decision and becomes a mechanism — so
it was withdrawn from this plan's open assumptions as an inherited obligation rather than a
departure to be argued for. And `plaid-python` ships no `py.typed`, so everything it
returns is `Any`; strictness was not relaxed, the override is scoped to the SDK alone, and
the untyped surface stops at the module that converts to local types.

**A norm's detector was corrected, not weakened.** AC-ARCH.4's guard reads any string
opening with a separator as an absolute filesystem path, and `/institutions/get` is not
one. The fix is the relationship rather than an exemption: a literal declared as an
`Endpoint` is the aggregator's vocabulary, anything else is still a path. A per-file
allowlist was rejected — it would decay on the first module someone forgot to add — and the
new test asserts both directions, including that a `Path("/Users/...")` in an
endpoint-declaring module is still caught.

🔴 **A rejected call names its cause, which took the Critic to notice.**
`ApiException.reason` is the HTTP reason phrase, so wrong credentials, a malformed field and
an unsupported country all read `400: Bad Request` — leaving the operator no way to tell a
rotated secret from a bug in this code, and the wrong guess costs a credential rotation that
was never the problem. The cause is in the response body. Verified by probing the real
sandbox host with deliberately invalid credentials, which needs no valid ones:
`error_code=INVALID_FIELD`, `error_message='client_id must be a properly formatted,
non-empty string'`, plus the `request_id` that makes a failure traceable in the aggregator's
dashboard. Also mapped: an unreachable host, which the SDK wraps only for SSL errors and
otherwise lets escape as a raw `urllib3.MaxRetryError` — a traceback from a library the
operator never chose.

**Not done, and the chunk is not ticked because of it.** The *success* path has never been
probed: no sandbox credentials exist on this machine, so `tests/connector/fixtures/` is
empty and the two `sandbox`-marked tests skip. The offline suite proves the bytes pass
through a fake unaltered; only a live call proves the SDK hands them over undecoded against
a real server, and `project-state.yaml` is explicit that the aggregator is verified against
rather than mocked at the layer under test. The build plan's acceptance criteria were split
to say so rather than leaving a done-when nobody could meet. Queued as VRF-002 — and
discharged the same day, once credentials arrived; see the entry above.

## 2026-09-06: `sync shell` — the operator gets to look inside their own datastore

<!-- prawduct: scope=datastore-v1 | release=v0.1.0 -->

**Why:** page encryption breaks every ad-hoc SQL tool — stock `sqlite3` reads this file as corrupt,
because the pages are ciphertext. Until this command existed there was no way for the operator to
look at their own data at all, which is why AC-ARCH.6 puts it in build step 1 rather than step 9: it
is the debugging affordance every later step is built over. It is also the product's only surface
that runs operator-supplied SQL, so it is where both read-role norms stop being theoretical.

**What landed:**

- **`bankmachine sync shell`** — an authenticated SQL prompt over a read-role handle. Statements may
  span lines, `.tables` / `.schema` / `.help` / `.quit` are there, a failed statement costs the
  statement and never the session, and results render as aligned columns. A blob is summarised
  (`<blob, 402 bytes>`) rather than dumped: `raw_responses.body_gzip` is the one that comes up, and a
  terminal full of gzip is not a debugging affordance.
- **The refusal to write stays in the file handle.** The shell adds nothing of its own — it asks
  `store/connection.py` for a read-role handle, which is `mode=ro`. An operator can type `PRAGMA
  query_only = OFF`, watch the flag flip to `0`, and still be refused. The test asserts both halves,
  because asserting only the refusal would pass just as well against a shell where the PRAGMA
  silently did nothing.
- **The prompt holds no snapshot between statements.** A shell left open overnight is open during
  the nightly sync, and a read-role handle takes no writer lock, so nothing else serialises the two;
  a pinned snapshot starves the checkpointer for hours. The release is a *property* of the handle —
  it asks whether a transaction is open and rolls it back — rather than a list of statements to watch
  for, because `BEGIN` opens one, so does `SAVEPOINT`, and the next thing that does would not have
  been on the list. Three tests hold it, including a negative control that disables the release and
  confirms the checkpoint genuinely starves; a probe that only ever confirms what was expected is
  the one to distrust.
- **AC-10.3 has one rule, not one per surface.** Everything the shell writes goes through
  `logging_setup.redact`, the same function the log formatter uses — including the statement echoed
  back in a piped session, because a transcript is the likeliest thing here to be committed or
  pasted into a bug report. Redaction runs over text and not over numbers: money here is an INTEGER
  of minor units and an account number is TEXT, so redacting integers would blank a six-figure
  balance — the number the operator opened the shell to read — while protecting nothing.
- **No writer shell.** The plan left one optional and it is declined: the product is read-only, a
  writer shell would hold the exclusive `flock` for its whole session so the overnight prompt above
  would block the nightly sync outright rather than merely starve it, and hand-typed rows have no
  raw response behind them, which is what `store rebuild`'s content digest exists to catch.

**The carried edge was staged, and staging it found a real misdiagnosis.** Since Chunk 01 the
no-fallback clause has had one case with no staged test: a hot WAL from a killed writer, no `-shm`,
in a directory the reader cannot write to. Staged here, it turned out the guard could never have
fired — SQLite opens lazily, so `connect()` succeeds and the failure lands on the *first read*,
where `_key_and_prepare` reported `SQLITE_CANTOPEN` as a rejected key. That told the operator to
restore a keychain entry that was never the problem, for a datastore that only needed its WAL
checkpointed — the exact wrong-recovery failure `DatastoreKeyRejectedError` was introduced to
prevent. `_diagnose_first_read` now separates the two on `SQLITE_NOTADB`, and the case has a real
test instead of a stand-in for one.

**The store layer grew two exports rather than the CLI growing a driver import.** The shell needs to
know when a statement is complete and how to catch a failed one; both now come from
`store.connection` (`statement_is_complete`, `DriverError`). The structural test caught the import
on the first full run — worth recording, because the norm it protects is exactly the kind that
degrades into a convention the moment a second module imports a DBAPI.

**The cumulative review returned 0 blocking, and six of its findings were worth fixing anyway.**
Two were real defects rather than polish. The **datastore key validator** tested hex with
`int(key, 16)`, which is a parser and not a predicate: it accepts an `0x` prefix, `_` separators, a
sign and surrounding whitespace, so `"0x" + "a" * 62` is 64 characters and passed both checks —
and SQLCipher treats anything that is not exact hex as a *passphrase*, runs its KDF over it, and
gives a store that works until those defaults change. That is the precise silent substitution the
validator exists to prevent. It is now a full match on the hex alphabet, with the four accepted-by-
`int` shapes as cases. And **filesystem `OSError` had no mapping into `StoreError`**, so a lock file
the process cannot open escaped as a traceback — including out of `inspect()`, whose entire contract
is to report a state rather than raise on one.

The third was in this chunk's own output: **the log-tuned redaction rule was applied to schema
text**, where it is wrong. `_OPAQUE` blanks any 32-plus character run, and
`source_investment_transaction_id` is exactly 32, so `.schema` printed `[REDACTED]` where column and
index names belong — eight unreadable lines of the real schema. Schema text is now not a redaction
surface at all, and that is a property rather than an exemption: everything in `sqlite_master` here
is authored by this repo's migrations, and AC-6.6 with
`tests/preferences/test_no_provider_identity.py` is what makes it carry no operator data. Row values
keep the full rule, bare-length matching included, and the cost is recorded — a 64-hex digest is
blanked, and the join back to the archive is the integer `raw_response_id`, which is not.
`.schema` also matches `tbl_name` now, so a table's indexes come with it.

The rest: `architecture.md`'s canonical command table still advertised the writer shell this chunk
declined, which would have had a step-7 builder implement the refused flag; the README's status
stopped at Chunk 02; and `reader()`'s connect-time branch still stated the hot-WAL cause that Chunk
04 measured false, so two operator-facing texts described one failure and the less-reached one made
the disproved claim. Log rotation, unlogged run failures and archive retention are filed as #3, #4
and #5 rather than fixed here.

Suite green, mypy strict and ruff clean. The norm-break harness runs 29 cases, five of them new and
all verified red. The by-hand check AC-ARCH.6 asks for is recorded as VRF-001 in
`.prawduct/operator-verification.md` with its session transcript, and is the one item still awaiting
the owner's own eyes.

## 2026-09-06: Raw preservation and rebuild — a bronze layer that checks its own work

<!-- prawduct: scope=datastore-v1 | release=v0.1.0 -->

**Why:** FR-5's bronze/silver split turns a categorization bug into a re-run instead of a re-fetch,
and a re-fetch is often impossible — an aggregator's history window does not come back. It is built
now, with no aggregator to feed it, because a sync path written first would normalize straight into
the tables and retro-fitting raw preservation around it afterwards means rewriting the part that
already worked.

**What landed:**

- **`store/raw.py`** — every response persisted verbatim, compressed and hashed, before anything
  reads it (AC-5.1). The digest is over the *plaintext*, so it identifies the response independently
  of how it was compressed, and `load_response` recomputes it: a body that no longer matches what
  was recorded is refused rather than derived from. The archive is append-only — two identical
  responses at two times are two facts, and collapsing them would destroy the evidence that the
  source repeated itself.
- **`store/derivation.py`** — the seam build step 2 plugs into, shipped empty. One normalization,
  two callers: the sync path and `store rebuild` run the same derivers over the same responses, so
  rebuild is not a second implementation that has to be kept in step with the first. A deriver is a
  pure function of its response — `DerivationContext` carries no clock, because a `first_seen_at`
  stamped `now()` is the one mistake that makes a rebuild unreproducible.
- **Persist first, then derive, in two transactions.** A deriver that raises must not take the
  archive down with it: the response may be unfetchable afterwards, while the derivation can be
  re-run at any time. A crash mid-derive leaves the response kept and no half-derived rows.
- **`bankmachine store rebuild`** (AC-5.2) — one transaction under the exclusive writer lock:
  delete every row the archive can recreate, replay the whole archive in received order, and then
  **check its own work**. It hashes the datastore's content before and after and refuses to commit a
  rebuild that did not reproduce what it replaced, unless the derivation version changed (AC-5.3,
  AC-11.5). Without that refusal a rebuild is an irreversible bulk operation whose only failure
  signal is analysis quietly turning wrong weeks later.
- **What it deletes is derived, not listed.** A table is rebuildable when it holds a foreign key
  *pointing at* a raw response. The first draft matched on the column name, which put `raw_responses`
  itself — whose primary key is `raw_response_id` — first in the list of tables to empty before
  replaying them. A test caught it; the fix was a better predicate, not a longer exception list.
- **Rows nothing can recreate are never deleted.** Imported rows name a file rather than a response,
  and accounts carry the local ids every row of history points at (AC-6.3). Both survive a rebuild
  untouched, and the content digest covers them, so a rebuild that orphaned or renumbered anything
  fails its own check.
- **"Byte-identically" (AC-11.5), read deliberately:** the digest covers every column of every table
  except a table's own single-column integer primary key where nothing references it. Those are
  rowid allocations, not facts about the world — requiring `transaction_id` to come back identical
  would make the criterion a statement about SQLite's allocator. Every id that *is* a fact is
  covered.
- **The sole-constructor norm got sharper, not looser.** `engine.connect()` is a pool checkout over
  a handle `store/connection.py` already keyed and locked, but the AST scan matched any call named
  `connect`. Rather than exempt a file, the rule now says what it always meant: a role is decided by
  the parameters a handle is opened with, and a checkout carries none. `engine.py` gained
  `writer_connection` / `reader_connection` so nothing outside the store layer checks one out, and a
  positive control fails if that carve-out ever stops exempting anything real.
- **Norm 4 now covers writers too.** Only the reader refused a schema version this build does not
  recognize; `store rebuild` is the first writer that is not the migration runner, and a writer that
  misunderstands a schema writes wrong answers down rather than merely returning them. The check
  moved into one helper both roles call, and `initializing_writer` still skips it — bringing an old
  datastore forward is the one job that has to open a version this build does not serve.
- **The Chunk 02 ride-along is discharged: the index drift guard now compares what a partial
  predicate *says*, not whether one exists.** It read `sqlite_where is not None` against
  `PRAGMA index_list.partial`, so a condition inverted to `retired_at IS NOT NULL` kept every other
  property of the index intact while making it enforce the opposite rule. Both sides' text is
  normalized only for the qualifier, whitespace and case — never for meaning — and a positive
  control fails if the normalizer ever starts returning nothing.
- **Six more cases in `verify_norms_go_red.py`**, covering the body-integrity refusal, the
  table-classification rule, the reproducibility refusal, the checkout carve-out, the writer's
  schema refusal and an inverted index predicate. All 24 breaks go red.

**The Critic round returned no blocking findings and tightened two seam decisions**, both of which
would have landed on build step 2 rather than here:

- **A derived table is now either rebuildable or a dimension, from one property.** `securities`
  carries a `derivation_version_id` but no raw provenance, so "derived" was being reconstructed from
  two signals that disagreed on exactly one table. The first deriver to write a security would have
  hit the `source_security_id` unique index on replay, or upserted and left stale rows that the
  content digest then reports as an unreproducible rebuild — sending the next reader hunting a
  purity bug that is really a classification gap. A table is *derived* when it references
  `derivation_versions`; of those, the ones referencing `raw_responses` are rebuilt and the rest are
  dimensions a deriver must upsert. Fixing it turned up the same trap a second time:
  `derivation_versions` names its own primary key `derivation_version_id`, exactly as
  `raw_responses` names `raw_response_id`, so both classifications now go through one
  reference test.
- **The archive does not hold credentials.** AC-5.1 keeps every response verbatim and AC-10.1 keeps
  every access token in the keychain; a token in `body_gzip` satisfies the first by breaking the
  second, permanently, because the table is append-only and a datastore backup travels. Recorded as
  clause 7 of the Derivation Seam, where step 2 meets it, and it is what keeps Chunk 04's AC-10.3
  redaction from needing to cover a table nobody planned to redact. Each of these guarantees is a refusal, so each fails silently and in the direction of
  looking finished.

## 2026-09-06: The core schema — thirteen tables, with the requirements built into them

<!-- prawduct: scope=datastore-v1 | release=v0.1.0 -->

**Why:** the schema is the format every later consumer depends on, and it was being designed before
any of those consumers exist. Chunk 02 is the plan's lock-in chunk: the last point at which changing
it is free. The columns were not drawn from taste — the enumerated queries of the ten MCP tools in
`system-requirements.md` §5 were written down first, and the tables answer them.

**What landed:**

- **`store/types.py`** — the typed vocabulary the schema is written in. `MinorUnits`, `CalendarDate`
  and `UtcInstant` are distinct to mypy, with validating constructors, SQLAlchemy column types, and
  a `from_decimal_string` that scales the digit tuple so no amount is too large to convert exactly
  and no fraction is ever silently rounded away.
- **Migration 002** — the thirteen tables of FR-6 as frozen DDL, applied inside the one transaction
  the runner owns. Three requirement classes are enforced *by the database* rather than by the code
  that writes to it: `typeof(x) = 'integer'` on every monetary column (AC-6.2 — SQLite stores a
  float in an INTEGER column without complaint), format constraints separating calendar dates from
  UTC instants (AC-6.4), and a provenance CHECK so no row can claim an origin it has no link to
  (AC-7.4). Identity is partial unique indexes, so idempotency (AC-1.4, AC-7.5) and the
  append-only balance series (AC-3.1) are properties of the store rather than disciplines of its
  callers.
- **`store/schema.py`** — SQLAlchemy Core metadata for the same tables, written independently of the
  DDL and compared to it column by column on every run. Generating one from the other would have
  been fewer lines and would have made drift undetectable.
- **`boundary-patterns.md` populated** — the datastore schema as its first contract surface,
  carrying the five parts of the contract that no column name implies, and naming the MCP tool
  surface and aggregator client as boundaries that do not exist yet.
- **The sign convention, decided and written down once:** every stored amount is signed from the
  operator's point of view, liabilities included. Net worth is then a plain sum and AC-11.2's
  reconciliation needs no per-type special case. `accounts.balance_class` partitions a *report*,
  never an arithmetic sign.
- **`tests/preferences/verify_norms_go_red.py` extended to cover the schema** as well as the
  connection layer — every new structural guarantee was verified red with its mechanism broken. A
  constraint that has never refused anything is a claim, not a check.
- **`frozen` became a mechanism.** The DDL is rendered from two shared constraint idioms, so
  "this never changes" rested on nobody editing them — and migration 003 will want the same two.
  A recorded SHA-256 of the rendered statements, compared by a test, is what now stops a later
  migration from silently redefining what version 2 means for every datastore that already ran it.
- **An aggregator row must name the response it came from.** The provenance CHECK originally
  permitted a row with `source = 'aggregator'` and no `raw_response_id`, while the comment above it
  claimed exactly one link is always set. Tightened to match the claim, on all four normalized
  tables: a row whose answer to *where did this come from* is silence looks identical to one that
  can be traced, and Chunk 03's rebuild is written against this constraint.
- **Provenance made symmetric across the normalized tables.** `holdings.source` accepted `'manual'`
  while having no column to name the import it came from, and neither `holdings` nor `balances_daily`
  carried the CHECK that `transactions` had. Found by scrub, not by the requirement: AC-7.4 is a
  property of every normalized row, and it had been implemented on one table.

**What the lock-in checkpoint caught:** re-reading the enumerated consumer questions against the
delivered tables found one they could not answer. `net_worth` needs assets separated from
liabilities, and `account_type` is the source's vocabulary rather than a classification — different
between sources, and absent entirely for an import-only account. Added as `accounts.balance_class`
while it was still free. One limitation is recorded rather than fixed: there is no FX table, so a
multi-currency net worth is out of scope until it is asked for.

**Also in this session:** the owner settled the open interpreter question — the product moves to
**Python 3.14**. The full suite, mypy strict and ruff were re-run green on 3.14.6 before the pin
moved, and `requires-python` stays `>=3.11` because nothing in the code needs more.

**Verified:** suite green (`prawduct-hook test-status`), mypy strict and ruff clean, every
structural break caught by the harness — which now covers the schema's guarantees, the frozen-DDL
hash, and the index guard's partial predicates as well as the connection layer's four norms. `store init` and `store status` were driven against a real
encrypted datastore, reporting schema version 2, with a known plaintext written through the schema
unrecoverable from the file's raw bytes. **The lock-in check was executed, not read:** the plan's
seven enumerated consumer questions were run as real SQL against a seeded datastore — fourteen
queries, because several questions take more than one and the remaining §5 tools were covered too —
and every one returned, including the per-account gap walk, the plain-sum net worth, and the
freshness stamp for an import-only account with no connection.

## 2026-09-06: The walking skeleton — an encrypted WAL datastore with its four norms enforced

<!-- prawduct: scope=datastore-v1 | release=v0.1.0 -->

**Why:** the repository held zero lines of Python. The architecture's four norms were prose claims
with an open issue (#1) standing in for their mechanism, and an operator had no way to create or
look at a datastore. Chunk 01 of the datastore-v1 plan is deliberately the widest chunk in that
plan because it is the one that proves the topology: config → keychain key → encrypted WAL
datastore → migration → read back through the reader role → print from the CLI.

**What landed:**

- The `uv` package (`bankmachine`, Python 3.11+), three runtime dependencies and four dev. The
  smallness is deliberate: a public tool that pulls real bank data on a stranger's machine wants a
  runtime dependency surface small enough to read.
- `config.py` — every path is configuration with a documented default (AC-ARCH.4), resolved by
  precedence from argument, environment, config file, default. Sandbox and production default to
  **different datastore files and different keychain accounts**, so putting fixture data in the real
  store needs an explicit override rather than a forgotten flag (AC-10.6).
- `secrets.py` — the only module importing `keyring` (AC-10.1). The datastore key is a 256-bit raw
  key, so SQLCipher's KDF is skipped and the value in the keychain *is* the key: no derivation whose
  parameters could drift between the process that created the store and the one that opens it.
- `store/connection.py` — the one module that constructs a connection, with exactly two roles.
  Writers route through a single factory that takes an advisory `flock` before it returns; readers
  open `mode=ro` and hold no snapshot beyond the statement that needs it. The SQLite open modes are
  **named constants**, because they are the norms rather than an implementation detail.
- `store/migrations/` — a ~50-line forward-only runner that owns its own transaction boundary, so a
  migration's DDL and its version stamp commit together or not at all.
- `store/engine.py` — SQLAlchemy Core over `create_engine(..., creator=...)`, so SQLAlchemy never
  opens a connection and every SQLCipher-specific step stays in the module that owns the norms.
- `logging_setup.py` — redaction at the formatter (AC-10.3) and a loud environment banner at every
  startup (AC-10.6). Both environments are announced at WARNING: the accident runs in both
  directions, so neither state is the quiet one.
- `cli/` and `__main__.py` — `bankmachine store init` (the only creator) and `store status` (which
  reports a missing or unrecognized datastore rather than crashing or creating one, AC-ARCH.3).
- Suite green, mypy strict clean, ruff clean (`prawduct-hook test-status`).

**The norms are now mechanisms, and issue #1's ask is delivered by this work** — its close is
owed at merge, because on the Issues backend a status change is an immediate API call with no
branch to be abandoned alongside. Each of the four has a test,
and each test was verified to go **red** with its norm deliberately broken —
`tests/preferences/verify_norms_go_red.py` keeps that reproducible rather than a sentence in a
commit message. Two things that came out of running it are worth recording:

- **A norm with two layers needs a test per layer.** Breaking only the `mode=rw` open mode left the
  no-implicit-creation test green, because the existence check still refused. Behaviour alone could
  not tell the layers apart, so the modes became named constants with their own assertions; either
  layer regressing is now caught.
- **The verification harness lied once, and the reason generalizes.** `"ro"` → `"rw"` is a
  same-length edit, and CPython validates a `.pyc` on (mtime, size) — two same-size writes inside
  one mtime second leave stale bytecode valid, so the test imported the *unbroken* module and
  reported green. Any tooling that mutates source in a loop has this failure mode.

**Also in this bundle:**

- `check-no-personal-data.sh` and its 22-case self-test **moved from `scripts/` to
  `tests/preferences/`**, discharging an obligation recorded in three places. The pre-push wiring
  followed the script, so push-time enforcement was kept rather than traded for test-time
  enforcement. The move initially broke five self-test cases by silently skipping them; the sandbox
  and the hook path now resolve through `git rev-parse --show-toplevel` rather than counting `..`
  hops, so a future move fails loudly instead of quietly testing less.
- `test_no_provider_identity.py` and `test_requirement_ids_unique.py` — both named in the norm index
  and both marked aspirational until the scaffold existed — are now written.
- `test_command:` is declared in `project-state.yaml`, deliberately left unset until a runner
  existed that could emit `{junit_xml}`.

**What the Critic caught, and it was worth the round.** One blocking (the plan's Deliverables line
still named the guard's pre-move path) and three warnings, all fixed:

- **`store init` minted a key for a datastore it could not decrypt.** On the restored-from-backup
  path — datastore present, keychain entry gone — it generated *and stored* a fresh key, migration
  then failed, and every later `store status` reported an authentication failure instead of a
  missing key. That is a recoverable state being reported as a corrupt one, which routes the
  operator to the wrong recovery. `store init` now refuses to mint a key for a store that already
  exists, and says why.
- **SQLAlchemy's transaction control is inert over these handles**, inherited from the deliberate
  `isolation_level=None`. Nothing recorded it, and Chunks 02 and 03 are exactly the two that would
  have assumed otherwise. Now documented at the module, pinned by a test, and flagged in the plan
  where those chunks will meet it.
- **`load_config`'s injected `env` seam stopped one step short of `HOME`**, so five config tests
  read as isolated while resolving against the developer's real home — and that branch is the
  documented macOS default.

The second review round found one more, and it is the more interesting of the two: **the `HOME`
seam fix shipped without a test that would catch its own regression.** Every other config test
either sets the XDG variables or asserts only that a path is absolute, so the fallback branch — the
documented macOS default — could have reverted to `Path.home()` with the suite still green. The
test now exists and was verified red against that exact revert. A fix without the check that
protects it is a fix with a shelf life.

Two smaller things rode that same round. `get_datastore_key`'s "run `bankmachine store init`"
advice was wrong in **every** path that reaches it: `writer()` and `reader()` both check the
datastore exists before asking for a key, and `store init` now correctly refuses to mint one for an
existing store — so the advice sent the operator in a circle. It names the state and both real
remedies instead. And the read-only reframing had reached `pyproject.toml` and the package
docstring but not `argparse`'s `description`, which is the one summary an operator actually reads
(`bankmachine --help`).

**One decision deliberately not taken:** `uv init` pinned `.python-version` to 3.14, so the
first run of everything above happened on an interpreter no artifact records. The pin was reverted to
3.12 — the version `project-preferences.md` records as verified — and the whole suite re-run
there. Moving this product's tested interpreter is the owner's call, not a side effect of
scaffolding.

**Trade-off accepted:** the redaction patterns over-redact. A filesystem path holding a
32-character segment is blanked along with the tokens. The alternative — requiring high entropy
before redacting — trades a little log legibility back for the chance of a real token slipping
through, and under the documented default paths no ordinary path is long enough in one segment to
trip it.

## 2026-09-05: AC-ARCH.7 resolved — the system architecture, measured rather than assumed

<!-- prawduct: scope=architecture | release=v0.1.0 -->

**Why:** `docs/system-requirements.md` AC-ARCH.7 deliberately deferred journal mode, locking
behaviour and reader isolation under encryption to "the system architecture" — a document that did
not exist. Build step 1 is the encrypted datastore, so step 1 would have been the component that
"encountered them first", which is precisely what the criterion forbids.

**What landed — two artifacts, not one.**

`.prawduct/artifacts/architecture.md`: topology, component responsibilities, the four channels (one
of which is the datastore file, and one of which is the import-file surface), data ownership,
failure modes, deployment and version skew, cross-cutting runtime concerns, and a decision log. It
is this product's first strategy-class artifact and its first `## Direction` section.

`.prawduct/artifacts/build-plan-datastore-v1.md`: build step 1 of system-requirements §8, in four
chunks — the walking skeleton (config, keyring, encrypted WAL datastore, the two connection roles),
the FR-6 core schema, the FR-5 raw-response layer and rebuild, and `sync shell`. Chunk 01 is
deliberately the widest because it proves the topology; Chunk 02 is the lock-in chunk, so the
questions its schema must answer are enumerated from the §5 tool table before any field is designed.
Chunk 01 also carries the `tests/preferences/` guard migration that three separate records have been
promising, and delivers what issue #1 asked for.

**A dependency decision rides with it.** The store layer uses **SQLAlchemy Core** — typed table
metadata and the query builder, no ORM, no session or identity map — decided by the owner over a
builder recommendation of hand-written SQL. It adds `sqlalchemy` as a runtime dependency at Chunk
01, taking the runtime surface to three packages. The objection behind the recommendation is
answered by construction rather than dropped: engines are built with `create_engine(..., creator=...)`
over our own keyed connection, so every SQLCipher-specific step — key first, WAL, `mode=ro`,
`query_only`, the writer lock — stays inside the module that owns the architecture norms, and
SQLAlchemy never opens a connection itself. Both that route and the built-in `sqlite+pysqlcipher`
dialect were verified against SQLCipher before the decision was taken. Risk surfaces were confirmed
in the same pass and are now recorded in `project-state.yaml`.

**The concurrency answer.** WAL journal mode, set after keying. Writer-role processes serialise on
a `flock` held for a whole run, above SQLite's own locking. The MCP reader opens `query_only` and
releases its snapshot at the end of every tool call. Nothing creates the datastore implicitly. A
process that does not recognise the schema version refuses to serve.

**Measured, not remembered.** Every concurrency claim was probed against `sqlcipher3-wheels` 0.5.7
(SQLCipher 4.12.0, SQLite 3.51.1) on this machine. The probes earned their keep three times: the
WAL and shm files are themselves encrypted, which had to be true or WAL would have traded AC-ARCH.5
away for AC-ARCH.7; a reader holding a snapshot starved a passive checkpoint at 0 of 93 frames and
93 of 93 the instant it released, which turned the reader's snapshot discipline from advice into a
norm; and a plain `connect()` to a missing path silently creates an empty encrypted store, which
under AC-ARCH.4's configurable path would answer every question confidently from nothing.

**One premise was falsified.** The design was going to route around a believed limitation — that a
`mode=ro` connection cannot read a WAL database without an existing `-shm`, and would fail against
a hot WAL from a crashed writer. It read correctly in every probed case, including after a
`SIGKILL` mid-write. `query_only` is still the choice, on its two surviving reasons; the reason
that did not survive is struck and recorded as struck, in the artifact's Decision Log.

**Norm bookkeeping.** Four norms born, all before any code exists, so no retroactivity decision
applies — there is nothing to migrate, contain or grandfather. Four pointer rows added to the
preferences norm index, and issue **#1** filed for the enforcement tests, because a mechanism named
and never built is the aspirational failure with extra steps.

**What the review changed, and it was two of the four norms.** The cumulative Critic returned 1
blocking, 12 warnings, 8 notes, and two findings were defects in the norms themselves rather than in
their presentation.

The writer norm **defined the writer role by enumerating commands**, and the list had already
omitted `store init` — which creates the file — and `store rebuild`, which rewrites every normalized
table. A list is a thing to forget. It is now defined by construction: every writable handle comes
from one writer factory, which takes the lock before it returns, so there is no way to be a writer
without passing through it.

The reader norm rested on `PRAGMA query_only`, which **is reversible** — re-probed on the finding,
`query_only=OFF` restores writes on a read-write handle, and `sync shell` ships the operator exactly
the SQL prompt that can type it. Read-role handles now open `mode=ro`, where the same sequence still
fails because the refusal lives in the file handle. Re-probing also scoped the earlier "falsified"
premise properly: `mode=ro` *does* fail against a hot WAL with no `-shm` under an unwritable
directory — the first probe had missed it because the crashed writer left its `-shm` behind. So the
norm carries a no-fallback clause: that state is a loud error, never a quiet downgrade to a writable
handle.

Also from the review: import files named as the foreign inbound surface they are (the artifact had
claimed none existed); one canonical CLI command table instead of four disagreeing lists; a logging
and AC-10.3 redaction rule placed in step 1 rather than step 8, because steps 1-7 all write log
lines; migration DDL and its version stamp required to commit in one transaction, since the version
is the *sole* signal a store is safe to serve; `source_root` and `risk_surfaces` set; and the
enforcement item re-filed from frozen markdown into the live Issues backend.


## 2026-09-05: Named — the product is `bankmachine`

<!-- prawduct: scope=rename | release=v0.1.0 -->

**Why:** The working name embedded a third-party trademark and locked the product to one
aggregator, and build step 1 is what fixes the Python package name, the keychain service name, the
scheduler label and the MCP server name. The keychain service name is the expensive one — changing
it after enrollment orphans stored access tokens. Settling the name while the repository still held
**zero lines of code** made this a documentation sweep rather than a migration; that timing was the
whole point of deciding the rename before step 1 rather than at publish.

Verified rather than assumed: PyPI returned 404 for `bankmachine`, so the package name was free at
the time of choosing.

**What changed:** titles and labels across the requirements doc, README, change-log, backlog,
boundary patterns and project-state; the name open questions in `system-requirements.md` §9 and
`project-state.yaml` closed; the aggregator-pluggability question's stale clause corrected, since
the product name no longer embeds the aggregator's name — one fewer reason that question is forced.

Six occurrences of the old name were deliberately **left in place**: two historical change-log and
archived-plan entries that record what was said on the day, the decision entry that names what was
renamed away from, and the GitHub repository slug, which is still accurate because the remote has
not been renamed.

**Carried through to the remote and the checkout.** The GitHub repository was renamed
`brookstalley/MCPlaid` → `brookstalley/bankmachine` (verified still private), `backlog_service_repo`
repointed at it rather than left to lean on GitHub's redirect, and the local checkout moved to
`~/source/bankmachine`. Verified after the move that `core.hooksPath` survived, the guard is clean,
the self-test still passes 22/22, and both branches are in sync with the renamed remote.

**Recorded caveat, raised once and accepted.** "Bank machine" is the ordinary term for an ATM in
Canada and parts of the UK — a name suggesting a device that *dispenses money*, for a product whose
§2 non-goals make "read-only, permanently" a headline commitment. This is a connotation risk, not a
technical one, and the mitigation is placement rather than a different name: the README now leads
with **"Read-only. It never moves money"** above the description, where a reader arriving from the
name meets the correction first.

## 2026-09-05: Repository made publishable — roster out of git, history purged, boundary guarded

<!-- prawduct: scope=repo-sanitization | release=v0.1.0 -->

**Why:** The operator restated the product. MCPlaid is a **general-purpose tool, not linked to
their personal finances** — consumed by Claude Cowork, and possibly released publicly, so nothing
specific to them may be in it. That resolved the open question the previous entry left standing,
and resolved it harder than either option on the table: the roster does not belong in this
repository at all.

The exposure was measured rather than estimated. `origin/develop` carried the operator's
institution names and balances in exactly three paths, and every other tracked file at that ref
was grepped clean.

**What changed:**

- The roster and its account-inventory evidence moved to the gitignored `deployment/` directory.
  `docs/deployment-requirements.template.md` keeps what was worth keeping — the zero-engine-change
  contract and the §7 traceability table — with no institution, balance or account count in it.
  That contract is what makes the engine spec trustworthy to a reader who is not this operator.
- `docs/build-vs-adopt-investigation.md` sanitized in place: operator name, machine name,
  institution names and account counts out; every technical finding, including the source-level
  vetting of the candidate MCP servers, kept intact.
- `scripts/check-no-personal-data.sh` added and wired into `pre-push` on **every** branch. It
  matches the roster's own explicit tokens (engine AC-0.3) plus operator identity, on word
  boundaries, over every tracked file. A checkout with no `deployment/` directory has no roster to
  leak and passes with a note — which is why the guard itself is safe to publish.
- `README.md` added, carrying the `git config core.hooksPath .githooks` step. A hooks directory is
  per-clone config, so a fresh clone pushes unguarded and nothing says so — and the only previous
  statement of the step lived inside the hook file the unset config prevents from running.
- **No attribution, anywhere** — stated absolutely in `CLAUDE.md` and homed as a norm row in
  `project-preferences.md`. This widens the already-ratified `Commit attribution: none` past
  commits to PRs, issues, comments, code, docstrings, documentation and release notes, and it
  overrides any harness default to the contrary. Recorded here because the widening previously
  existed only in `CLAUDE.md` while the preferences row still read narrower.
- **History purged.** `git filter-repo` removed five paths from every commit; `develop` and `main`
  were force-pushed. Verified by fresh clone: zero institution or operator tokens anywhere in the
  remote's history. Two paths were purged and re-added at their current content rather than
  scrubbed in place — the decision record and the guard itself, both of which carried in early
  revisions exactly what the purge exists to remove.
- **The fifth path was found by the guard, not by us.** The purge was planned as four paths. The
  finished guard, scanning all history, reported a fifth: Chunk 01's own first commit hardcoded
  identity tokens in the guard's source — the arrangement the Critic's R-9 had just made us
  remove. The check caught its author.
- Four decisions recorded with alternatives: repository scope; MCP transport is local stdio only
  and AC-10.5 holds; macOS for v1 with the credential store and scheduler behind seams; rename
  before build step 1.

**What the review changed, and it was the important half.** The first version of this guard
scanned the *working tree*. Critic pointed out that this passes the exact exposure the guard exists
to stop — a leak sitting in already-pushed history behind a sanitized tip — and that the operator
would read "clean" as "nothing I am pushing carries the roster", which was not what was checked. The
guard now takes the ref range the pre-push hook already receives and scans **every commit being
pushed**. Run against this repository's own history it correctly refuses: the roster is still back
there, which is what Chunk 02 is for.

The same review found the guard failed open at every error path, and that its hardcoded identity
tokens forced a carve-out where the one tracked file containing the operator's name was the one file
never scanned. Both are fixed by construction rather than by patching: **all** tokens now come from
gitignored `deployment/`, so the script carries none and needs no self-exclusion, and every error
condition aborts rather than reporting clean.

**The self-test earned itself immediately: 7 of its 15 cases failed on first run.** The cause was a
genuine defect — the positive control used system `grep` while the scan used `git grep`, which does
not honour `\b` in ERE. So the control passed while the scan matched nothing: precisely the
fail-open shape the guard was being rewritten to refuse, reproduced inside the fix. Word boundaries
are now spelled out explicitly, and the control runs through the same engine that scans, using a
real token rather than a synthetic sentinel.

**Note on the mechanism.** Guard and self-test are shell rather than `tests/preferences/` because no
Python scaffold exists yet and creating one would fix the package name ahead of the rename decision.
The migration obligation is recorded in `project-preferences.md` and in `system-requirements.md` §8
build step 1 — the step that lands the test runner, and therefore the moment it is triggered.

## 2026-09-05: Discovery captured; requirements split into engine and roster layers

**Why:** The repo held three substantial docs but a template-default `project-state.yaml`, so
governance could not calibrate rigor and the build gates could not engage. Discovery ran in
reconciliation mode — the material was read and backfilled rather than re-interviewed.

Mid-discovery the operator imposed a constraint that reshaped the frame: **no hardcoded account
providers; accounts are added and removed over the product's life.** The requirements doc was a
snapshot of one roster on one day, and that roster was already known wrong in detail. The operator's
own refinement settled where the line falls — the roster requirements are *genuine* requirements,
they simply belong to a different layer than the engine.

**What changed:**

- The v1 acceptance-criteria document split into `docs/system-requirements.md` (the
  provider-agnostic engine, no institution name in it) and a deployment-requirements document
  holding this operator's roster, as real acceptance criteria. All 47 v1 criteria land in one or
  the other, generalized or instantiated; one is explicitly superseded.
- A load-bearing rule connects them: every deployment requirement must be satisfiable by
  configuration plus an adapter with zero engine change. The traceability table is its checkable
  form — a deployment requirement that cannot be expressed that way is a gap in the engine spec.
- Two norms ratified: the provider-agnostic engine (with the aggregator expressly carved out), and
  uniqueness of requirement ids within a document.
- Decisions recorded with alternatives: SQLCipher over plain SQLite and over field-level AES;
  no hardcoded filesystem paths; lossless rebuild qualified by a recorded derivation version;
  account lifecycle so a retired account stops reading as a permanent coverage gap.
- Toolchain set: uv, pytest, ruff, mypy strict, hypothesis on the money and idempotency invariants.

**Open, and the operator's to decide:** where the roster lives. `origin/develop` already carries
institution names and balances, against the constraint this project records; the remote is private.
*(Closed 2026-09-05 by the entry above this one: the roster moved out of git and the history was
rewritten.)*

**Reviewed:** `rev-20260905T195208Z-c1f3c450` (2 blocking, 9 warning, 5 note — all resolved),
verified clean by `rev-20260905T200406Z-290b9dcb`.

