#!/usr/bin/env bash
#
# The whole gate: the three commands `project-preferences.md` § Dev commands
# names, run under one exit code, each named when it is the one that failed.
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
# WHY PYTEST RUNS FIRST
#
# The evidence record is parsed from the JUnit report at $1. If a linter ran
# first and failed the script, that report would never be written and the record
# would fail as "unparseable" -- an error that says nothing about which tool was
# unhappy. Running pytest first means the report always exists, so a red linter
# fails the record with a message that names the linter.
#
# WHY EVERY CHECK RUNS EVEN AFTER ONE FAILS
#
# There is no `set -e`. An operator who has just been told about a type error
# should not have to fix it and re-run to discover the three lint findings
# underneath; one invocation reports everything that is red.
#
# WHAT IS DELIBERATELY NOT HERE
#
# `ruff format --check`. `project-preferences.md` records ruff format as this
# project's formatter, but brookstalley/bankmachine#92 gates the two commands it
# names -- `uv run ruff check` and `uv run mypy`. Adding a third check this issue
# did not ask for would make the gate's contract harder to argue about later.
# Adding it is a decision, not a formality.

set -uo pipefail

if [ "$#" -ne 1 ]; then
    echo "usage: ${0##*/} <junit-xml-path>" >&2
    echo "  the gate substitutes prawduct's {junit_xml} here; pytest writes its report to it" >&2
    exit 2
fi

junit_xml="$1"
failed=""

# Newline-delimited rather than a bash array: macOS ships bash 3.2, where
# expanding an empty array under `set -u` is itself an error.
check() {
    name="$1"
    shift
    if "$@"; then
        return 0
    fi
    printf '\n*** FAILED: %s\n' "$name" >&2
    failed="${failed}${name}"$'\n'
}

check "uv run pytest" uv run pytest --junit-xml="$junit_xml" -q
check "uv run ruff check" uv run ruff check
check "uv run mypy" uv run mypy

if [ -n "$failed" ]; then
    printf '\n%s\n' "gate failed; these commands were red:" >&2
    printf '%s' "$failed" | while IFS= read -r name; do
        printf '  - %s\n' "$name" >&2
    done
    exit 1
fi

echo "gate passed: pytest, ruff check, mypy"
