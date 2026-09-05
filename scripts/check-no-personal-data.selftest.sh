#!/usr/bin/env bash
#
# Self-test for the leak guard.
#
# The guard is the sole enforcement of a ratified norm, and its Done-when claims
# it was "exercised in both directions". This is what makes that claim
# reproducible rather than a sentence in a commit message. It is a shell script
# rather than a pytest case because no Python scaffold exists yet and creating
# one would fix the package name ahead of the rename decision; it moves under
# `tests/preferences/` when the scaffold lands.
#
# Every case runs in a THROWAWAY REPOSITORY under $TMPDIR. Nothing here touches
# the real repo, so a failing case cannot leave the working tree dirty.
#
# Exit 0 = all cases pass.

set -euo pipefail

GUARD=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/check-no-personal-data.sh
[[ -x $GUARD ]] || { echo "selftest: guard not executable at $GUARD" >&2; exit 1; }

pass=0
fail=0

# Runs the guard in the sandbox and compares its exit code to what the case expects.
check() {
    local name=$1 expected=$2; shift 2
    local rc=0
    "$GUARD" "$@" >/dev/null 2>&1 || rc=$?
    if [[ $rc == "$expected" ]]; then
        printf '  ok    %-58s (exit %s)\n' "$name" "$rc"
        pass=$((pass + 1))
    else
        printf '  FAIL  %-58s (exit %s, wanted %s)\n' "$name" "$rc" "$expected"
        fail=$((fail + 1))
    fi
}

sandbox=$(mktemp -d)
trap 'rm -rf "$sandbox"' EXIT
cd "$sandbox"

git init -q .
git config user.email selftest@example.invalid
git config user.name "Self Test"
mkdir -p scripts deployment
cp "$GUARD" scripts/check-no-personal-data.sh
GUARD="$sandbox/scripts/check-no-personal-data.sh"
printf 'deployment/\n' >.gitignore
printf 'examplebank\n' >deployment/roster-tokens.txt
printf 'someoperator\n' >deployment/identity-tokens.txt

printf 'Clean engine documentation.\n' >docs.md
git add -A && git commit -qm "clean baseline"
clean_tip=$(git rev-parse HEAD)

echo "leak guard self-test"
echo

echo "worktree mode"
check "clean tree passes" 0
printf 'ExampleBank holds four accounts.\n' >>docs.md
check "roster token is caught" 1
git checkout -q docs.md
printf 'Verified by someoperator.\n' >>docs.md
check "identity token is caught" 1
git checkout -q docs.md
printf 'someoperator/somerepo is the upstream.\n' >>docs.md
check "identity-owned slug alone is not a hit" 0
printf 'someoperator/somerepo -- ExampleBank roster\n' >>docs.md
check "slug plus roster token on one line is a hit" 1
git checkout -q docs.md
printf 'See docs/examplebank-notes.md\n' >>docs.md
check "path containing a roster token is not exempted" 1
git checkout -q docs.md

echo
echo "history mode -- the case a worktree-only guard passes"
printf 'ExampleBank holds four accounts.\n' >>docs.md
git add -A && git commit -qm "leak"
leak_tip=$(git rev-parse HEAD)
git rm -q docs.md && printf 'Sanitized.\n' >docs.md
git add -A && git commit -qm "sanitize the tip"
sanitized_tip=$(git rev-parse HEAD)
check "worktree is clean after sanitizing the tip" 0
check "tip revision alone is clean" 0 --rev "$sanitized_tip"
check "RANGE still catches the leak behind a clean tip" 1 --range "$clean_tip" "$sanitized_tip"
zero=$(printf '%040d' 0)
check "new-branch push scans all reachable history" 1 --range "$zero" "$sanitized_tip"

echo
echo "failing closed"
printf 'Example Federal Bank\n' >deployment/roster-tokens.txt
check "multi-word token aborts, never silently mismatches" 2
printf 'bad[token\n' >deployment/roster-tokens.txt
check "ERE metacharacter in a token aborts" 2
printf 'examplebank' >deployment/roster-tokens.txt   # no trailing newline
git checkout -q "$clean_tip" -- docs.md 2>/dev/null || printf 'Clean.\n' >docs.md
check "token file with no trailing newline still loads" 0
printf 'examplebank' >deployment/roster-tokens.txt
printf 'ExampleBank appears here.\n' >>docs.md
check "  ...and that last token actually matches" 1
git checkout -q docs.md 2>/dev/null || true

echo
echo "no roster present -- the state every other clone is in"
rm -f deployment/roster-tokens.txt deployment/identity-tokens.txt
printf 'ExampleBank holds four accounts.\n' >>docs.md
check "no token files: passes, nothing to leak" 0

echo
printf 'selftest: %d passed, %d failed\n' "$pass" "$fail"
(( fail == 0 )) || exit 1
