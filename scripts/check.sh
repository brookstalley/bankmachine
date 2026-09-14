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

check "uv run pytest" uv run pytest --junit-xml="$junit_xml" -q
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
