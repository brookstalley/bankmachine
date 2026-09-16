#!/usr/bin/env bash
#
# The whole gate: every check `project-preferences.md` § Dev commands names, run
# under one exit code, each named when it is the one that failed. The list lives
# in the `check` calls below and nowhere else in this file -- a count in this
# header would be a second copy, and the last one said three while four ran.
#
# WHY A SCRIPT AND NOT `test_commands:`
#
# `test_commands:` is prawduct's multi-command form and looks like the vehicle
# for this. It is not: its contract makes the `{junit_xml}` literal MANDATORY in
# every entry, and neither ruff nor mypy emits a JUnit report. `test_command:`
# refuses shell operators for the same reason -- one launch, one report. Its own
# documentation names the way out ("point the command at a script for compound
# runs"), which is this file.
#
# WHY PYTEST RUNS FIRST, AND WHY A RED LINTER IS WRITTEN INTO ITS REPORT
#
# The JUnit report at $1 is not a side effect -- it IS the durable evidence.
# `prawduct-hook test-evidence record` writes `.test-evidence.json` from that
# report and only afterwards consults the command's exit status, which it does
# not store. So a script that merely EXITS non-zero on a red linter leaves a
# session-fresh record reading `failed: 0`: the terminal shows red, `test-status`
# says `current`, and the Stop gate passes. That is the very defect #92 was filed
# about -- a declared check nothing notices -- moved one consumer along.
#
# So pytest runs first, its report always exists, and each red linter is then
# appended to that report as a failing case. The evidence says what the terminal
# says.
#
# WHY EVERY CHECK RUNS EVEN AFTER ONE FAILS
#
# There is no `set -e`. An operator who has just been told about a type error
# should not have to fix it and re-run to discover the three lint findings
# underneath; one invocation reports everything that is red.
#
# WHY `ruff format --check` IS HERE, THOUGH #92 DID NOT NAME IT
#
# It was left out at first, on the reasoning that #92 names two commands and a
# third would widen the contract. `learnings.md` settles it the other way, with an
# instance: on 2026-09-08, merging `feature/sync-v1`, SEVEN files had drifted
# across two build steps that both reported "ruff clean" at every close -- because
# both ran `ruff check` and neither ran `--check` on the formatter. The two are
# different halves and the lint rules never reach layout.
#
# A gate that exists because declared checks were going unrun, which then omits the
# one declared check with a recorded instance of going unrun, is not scoped -- it
# is the same defect with a smaller blast radius. So it runs here, and this note is
# the record that the widening was deliberate.

set -uo pipefail

if [ "$#" -ne 1 ]; then
    echo "usage: ${0##*/} <junit-xml-path>" >&2
    echo "  the gate substitutes prawduct's {junit_xml} here; pytest writes its report to it" >&2
    exit 2
fi

junit_xml="$1"
failed=""
ran=""

# Resolved rather than assumed: the gate is launched as `bash scripts/check.sh`
# by `test_command:`, by absolute path from the tests, and from inside `scripts/`
# by hand. `${0%/*}` is wrong for the last of those.
gate_dir="$(cd "$(dirname "$0")" && pwd)"

# Newline-delimited rather than a bash array: macOS ships bash 3.2, where
# expanding an empty array under `set -u` is itself an error.
check() {
    name="$1"
    shift
    ran="${ran:+$ran, }${name#uv run }"
    if "$@"; then
        return 0
    fi
    printf '\n*** FAILED: %s\n' "$name" >&2
    failed="${failed}${name}"$'\n'
}

# --- the test keychain: why the gate swaps the default, and how it puts it back
#
# 🔴 THIS CHANGES A USER-LEVEL SETTING FOR THE DURATION OF THE RUN, and that is
# not done lightly. A populated login keychain costs roughly TWENTY-FIVE TIMES
# what an empty one does per `keyring` round trip, and the suite spends most of
# its wall clock there. The measured figures live in the change-log entry for
# `scope=fast-keychain-suite` and are not restated here -- they were, and the two
# copies disagreed within a day, which is the second-copy failure this file's own
# header warns about.
#
# Most of the tests hold `keychain_service` against the REAL keychain, so the
# suite spent most of its wall clock inside `securityd` rather than in this
# product. `-n auto` could not reach it: ten workers queue on one daemon, so
# parallelism alone bought very little.
#
# WHY NOT TARGET A KEYCHAIN PER PROCESS, which would need no swap at all:
# `keyring` exposes `KEYCHAIN_PATH` and a `Keyring.keychain` attribute, and as of
# keyring 25.7 the macOS backend IGNORES BOTH -- it warns "Specified keychain is
# ignored. See #623". Measured rather than read: a probe targeting an empty
# keychain by path ran at 321.9ms, i.e. it went to the login keychain anyway. The
# default keychain is the only lever that works.
#
# WHY NOT FAKE `keyring` INSTEAD: `secrets.py` is the only module that imports it
# (AC-10.1) and the security model leans on genuine keychain behaviour. A fake
# would make most of the suite stop exercising the real integration. A DEDICATED
# keychain is still a real keychain, so this buys the speed without trading the
# coverage away.
#
# WHAT THE RISK ACTUALLY IS, stated as it holds rather than as it reads best: for
# the length of the run, another process writing to the DEFAULT keychain writes
# to ours instead. Reads are unaffected -- the original search list is kept and
# still searched.
#
# 🔴 That write is not destroyed ON THE WAY OUT, but it IS removed at the start
# of the next run, and only SOME of those removals are announced. The
# announcement fires when the previous run did not exit cleanly, which is a
# proxy: the real risk -- did a foreign process write during the window -- is
# independent of how the run ended, so a write absorbed by a run that exited
# normally is deleted at the next start WITHOUT a warning.
#
# That narrowing is deliberate and it is a trade, not an oversight. Every run
# after the first leaves a keychain behind, so announcing every removal means
# announcing on every run, and a banner that fires every time is the one the
# operator stops reading -- which costs more than it buys, because this banner
# guards the only interrupt window there is. Measuring the real risk directly was
# tried and does not discriminate either: the suite's own tests leave entries
# behind, so "the keychain is non-empty" is true after every clean run too.
#
# What bounds the exposure instead: the window is roughly the suite's runtime,
# and
# `BANKMACHINE_NO_KEYCHAIN_SWAP=1` declines the whole mechanism.
#
# CI does exactly this already (`.github/workflows/check.yml` creates
# `ci.keychain`); the restore below captures whatever was default, so running
# under CI restores CI's keychain rather than assuming a login one.
BMTEST_KEYCHAIN="bankmachine-test.keychain"
# 🔴 Overridable, and the tests MUST override it. A case exercising the unguarded
# path is by construction a nested run doing the sabotage this guard prevents:
# stubbing `security` keeps it off the real keychain, but the STATE FILE is a real
# path, and clobbering the outer run's copy leaves that run's own SIGKILL path
# with nothing to restore from. Measured, not theorised -- an early version of
# those cases overwrote it with the stub's fake paths mid-suite.
BMTEST_STATE="${BANKMACHINE_KEYCHAIN_STATE:-$gate_dir/../.bankmachine-keychain-restore}"
keychain_swapped=""
keychain_list_saved=""

keychain_current_default() {
    security default-keychain 2>/dev/null | sed -e 's/^[[:space:]]*"//' -e 's/"$//'
}

# The WHOLE user search list, space-joined. `list-keychains -s` REPLACES the list
# rather than adding to it, so restoring only the default would silently drop
# every other keychain the user had searchable. `.github/workflows/check.yml`
# captures it the same way for the same reason.
keychain_current_list() {
    security list-keychains -d user 2>/dev/null \
        | sed -e 's/^[[:space:]]*"//' -e 's/"$//' | tr '\n' ' '
}

restore_keychain() {
    [[ -n $keychain_swapped ]] || return 0
    security default-keychain -s "$keychain_swapped" 2>/dev/null || true
    # shellcheck disable=SC2086 -- the saved list is space-joined and must expand
    [[ -n $keychain_list_saved ]] && security list-keychains -d user -s $keychain_list_saved 2>/dev/null
    # 🔴 NOT deleted here. Anything written to the DEFAULT keychain during the run
    # landed in this one, and for this product that can be the datastore key,
    # which `secrets.py` documents as unrecoverable. Deleting on the way out
    # removes it at a moment nobody caused and nobody sees. It is left in place
    # and removed at the START of the next run instead -- announced there only
    # when the previous run died, for the reason set out in this file's header.
    rm -f "$BMTEST_STATE"
    keychain_swapped=""
}

# 🔴 Self-healing, and it is the half that makes the swap acceptable. A shell trap
# covers a normal exit, a Ctrl-C and a SIGTERM; it cannot cover SIGKILL or a power
# cut. So the original default AND search list are written to a state file BEFORE
# the swap, and a run that finds a stale one repairs it rather than layering a
# second swap on top -- which would otherwise record OUR keychain as the thing to
# restore to, and strand the real default permanently.
repair_stale_keychain() {
    local current recorded_default recorded_list
    current=$(keychain_current_default)
    case "$current" in
        *"$BMTEST_KEYCHAIN"*) ;;
        *) return 0 ;;
    esac
    printf '\n*** the default keychain is this suite'"'"'s temporary one, so a previous run\n' >&2
    printf '*** was killed before it could restore yours. Repairing.\n' >&2
    recorded_default=""
    recorded_list=""
    if [[ -r $BMTEST_STATE ]]; then
        recorded_default=$(sed -n '1p' "$BMTEST_STATE")
        recorded_list=$(sed -n '2p' "$BMTEST_STATE")
    fi
    if [[ -n $recorded_default ]]; then
        printf '***   restoring the recorded default: %s\n' "$recorded_default" >&2
    else
        recorded_default="$HOME/Library/Keychains/login.keychain-db"
        printf '***   NO recorded default found; falling back to %s\n' "$recorded_default" >&2
        printf '***   if that is not yours: security default-keychain -s <path>\n' >&2
    fi
    [[ -n $recorded_list ]] || recorded_list="$recorded_default"
    security default-keychain -s "$recorded_default" 2>/dev/null || true
    # shellcheck disable=SC2086 -- space-joined list, must expand
    security list-keychains -d user -s $recorded_list 2>/dev/null || true
    # 🔴 Emphatically not deleted on THIS path. The run that owned this keychain
    # was killed, so it is the one most likely to hold a write nobody has seen.
    rm -f "$BMTEST_STATE"
}

use_test_keychain() {
    # 🔴 RE-ENTRY GUARD, and it is load-bearing rather than defensive. The suite
    # this gate launches contains `test_the_gate_runs_the_declared_checks.py`,
    # which execs THIS SCRIPT. Without the marker each nested run would see the
    # default already pointing at our keychain, read that as a killed previous
    # run, restore the OUTER run's default, delete the keychain nine xdist
    # workers are mid-`keyring` call against, and remove the outer run's state
    # file -- leaving its own SIGKILL path with nothing to restore from.
    #
    # The marker has to be HANDED to each nested run, not just exported: those
    # call sites pass closed environments, and a child inherits nothing it is not
    # given. `tests/preferences/test_the_gate_runs_the_declared_checks.py`
    # routes every one of them through a single builder, and a case there fails
    # on any exec that writes its own.
    [[ -z ${BANKMACHINE_KEYCHAIN_SWAPPED:-} ]] || return 0
    command -v security >/dev/null 2>&1 || return 0
    [[ ${BANKMACHINE_NO_KEYCHAIN_SWAP:-} != 1 ]] || return 0

    # 🔴 Captured HERE, before this run writes its own state file and before
    # `repair_stale_keychain` removes any it finds. Read later, it is always
    # true, and the leftover warning would fire on every single run -- which is
    # the warning nobody reads.
    local had_state=0
    [[ -e $BMTEST_STATE ]] && had_state=1

    repair_stale_keychain

    local original original_list
    original=$(keychain_current_default)
    [[ -n $original ]] || return 0
    original_list=$(keychain_current_list)
    [[ -n $original_list ]] || original_list="$original"

    # Written BEFORE the swap. A state file that appears after it is useless to
    # exactly the run that dies between the two. Line 1 default, line 2 list.
    printf '%s\n%s\n' "$original" "$original_list" >"$BMTEST_STATE" || return 0

    # 🔴 A random password, not an empty one. Anything written to the DEFAULT
    # keychain during the run lands here, including -- through `secrets.py`, the
    # one seam that stores it -- a datastore key. An empty-password keychain
    # holding that would sit outside the "protected by the keychain's own unlock"
    # boundary `security-model.md` states. The password lives only in this
    # process, which is also why the keychain is NOT deleted on the way out: a
    # later reboot relocks it under a secret that died with this run, so leaving
    # it is the only thing preserving any chance of recovering a stray write.
    #
    # 🔴 NOT `tr -dc ... </dev/urandom | head -c 32`. `head` exits at its byte
    # count, `tr` takes SIGPIPE, and the pipeline returns 141 under the
    # `pipefail` this script sets -- so the `|| return 0` fired, the swap never
    # happened, and the suite silently ran against the developer's real keychain
    # at nine minutes instead of one. It looked like a performance regression,
    # not like an error, because nothing failed. `openssl rand` is one process
    # with no pipe and no signal.
    local pw
    pw=$(openssl rand -hex 16 2>/dev/null)
    # No password source: swap nothing rather than create a keychain anyone can
    # open. The suite stays slow, which is the safe direction to fail.
    [[ -n $pw ]] || { rm -f "$BMTEST_STATE"; return 0; }

    # The one place the keychain is destroyed, and whether it is announced
    # depends on whether it is SUSPECT.
    #
    # 🔴 A warning that fires every run is the one nobody reads, and this one
    # guards the only window between a stray write and its deletion. Since
    # neither teardown path deletes any more, a leftover exists at the start of
    # every run after the first -- so "a leftover exists" cannot be the trigger.
    #
    # The state file is the discriminator, and it is a PROXY rather than a
    # measurement: a run that exits cleanly removes it, a run that is killed does
    # not. So it answers "did the previous run die", where the risk is "did a
    # foreign process write during it" -- two different questions that happen to
    # correlate, since a killed run is the one whose leftover nobody has looked
    # at. A leftover with no state file is therefore removed QUIETLY even though
    # it could, in principle, hold such a write. The header says why that trade
    # is taken; `security-model.md` says it where a reader of the security model
    # will meet it.
    if security show-keychain-info "$BMTEST_KEYCHAIN" >/dev/null 2>&1; then
        if (( had_state == 1 )); then
            # Harness-only, deliberately undocumented beside the operator-facing
            # opt-out: it exists so the suite's own cases do not pay three
            # sleeps, and an operator has no reason to shorten the one window
            # that protects them.
            local grace="${BANKMACHINE_KEYCHAIN_GRACE:-3}"
            printf '\n*** %s is left over from a run that did NOT exit cleanly.\n' "$BMTEST_KEYCHAIN" >&2
            printf '*** Anything that wrote to the DEFAULT keychain during it landed there,\n' >&2
            printf '*** and is about to be deleted. Ctrl-C within %ss to keep it.\n' "$grace" >&2
            (( grace > 0 )) && sleep "$grace"
        fi
        security delete-keychain "$BMTEST_KEYCHAIN" 2>/dev/null || true
    fi
    security create-keychain -p "$pw" "$BMTEST_KEYCHAIN" 2>/dev/null || { rm -f "$BMTEST_STATE"; return 0; }
    # No auto-lock: a keychain that relocks mid-suite fails every `keyring` write
    # after it, which reads as a product bug rather than as a harness one.
    security set-keychain-settings "$BMTEST_KEYCHAIN" 2>/dev/null || true
    security unlock-keychain -p "$pw" "$BMTEST_KEYCHAIN" 2>/dev/null || true
    security default-keychain -s "$BMTEST_KEYCHAIN" 2>/dev/null || { rm -f "$BMTEST_STATE"; return 0; }
    # The original list is KEPT and ours prepended: `keyring` writes to the
    # default but reads through the list, so anything already stored resolves.
    # shellcheck disable=SC2086 -- space-joined list, must expand
    security list-keychains -d user -s "$BMTEST_KEYCHAIN" $original_list 2>/dev/null || true

    keychain_swapped="$original"
    keychain_list_saved="$original_list"
    export BANKMACHINE_KEYCHAIN_SWAPPED=1
    trap restore_keychain EXIT INT TERM
}

use_test_keychain

# `-n auto` lives HERE rather than in `pyproject.toml`'s `addopts`, and the
# difference matters. `addopts` would follow every pytest invocation in the repo,
# including the single-test runs `tests/preferences/verify_norms_go_red.py`
# shells out per norm case -- each spinning up a worker pool to run one test --
# and including an explicit `-m sandbox`, which would parallelise live aggregator
# calls into rate limits. This is the full-suite run and is none of those. The
# reasoning in full is beside `addopts`.
#
# The suite is safe to run in parallel because isolation is per-test and already
# checked: the keychain service is a per-test uuid, config is `tmp_path`-scoped,
# the autouse logging fixture restores per test, and xdist forks processes, so
# the `monkeypatch.setattr` sites each get their own module table. A differing
# test COUNT between `-n auto` and serial would mean real shared state -- that is
# a finding to chase, never a flake to retry.
check "uv run pytest" uv run pytest --junit-xml="$junit_xml" -q -n auto
check "uv run ruff check" uv run ruff check
check "uv run ruff format --check" uv run ruff format --check
check "uv run mypy" uv run mypy

if [ -n "$failed" ]; then
    # Written into the report rather than left to the exit code: see the header.
    # Plain `python3`, not `uv run python` -- a red `uv` must not take the one
    # step that records the redness with it.
    if ! printf '%s' "$failed" | python3 "$gate_dir/record_red_checks.py" "$junit_xml"; then
        # Recording is the half nothing else can see. If it fails, the exit code
        # below still goes red for a human, but the evidence record would read
        # clean -- so say so on the one channel that is left.
        printf '\n*** gate: could not write the red checks into %s\n' "$junit_xml" >&2
        printf '*** the evidence record for this run will UNDERSTATE it\n' >&2
    fi

    printf '\n%s\n' "gate failed; these commands were red:" >&2
    printf '%s' "$failed" | while IFS= read -r name; do
        printf '  - %s\n' "$name" >&2
    done
    exit 1
fi

# Built from what actually ran, not typed out again. A hand-written list here
# would be one more copy of the set, and the one place nobody looks when they
# add a check -- it reports success, so it is never the line that fails.
echo "gate passed: $ran"
