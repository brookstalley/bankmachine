#!/usr/bin/env bash
#
# Self-test for the leak guard.
#
# The guard is the sole enforcement of a ratified norm, and its Done-when claims
# it was "exercised in both directions". This is what makes that claim
# reproducible rather than a sentence in a commit message. It stays a shell
# script now that the scaffold has landed: the guard it exercises is shell, and
# the cases drive real `git push` refspecs through a real hook in a throwaway
# repository -- a translation to Python would test a reimplementation rather
# than the thing that actually runs. `test_no_personal_data.py` runs it, so the
# pytest suite is the single entry point the preferences table asks for.
#
# Every case runs in a THROWAWAY REPOSITORY under $TMPDIR. Nothing here touches
# the real repo, so a failing case cannot leave the working tree dirty.
#
# Exit 0 = all cases pass.

set -euo pipefail

SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# Resolved through git rather than by counting `..` hops from SRC_DIR: this file
# has already moved once, and a hardcoded hop turns that into a case that skips
# rather than a case that runs.
REPO_ROOT=$(cd "$SRC_DIR" && git rev-parse --show-toplevel)
GUARD="$SRC_DIR/check-no-personal-data.sh"
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
mkdir -p tests/preferences deployment
cp "$GUARD" tests/preferences/check-no-personal-data.sh
GUARD="$sandbox/tests/preferences/check-no-personal-data.sh"
mkdir -p .githooks
cp "$REPO_ROOT/.githooks/pre-push" .githooks/pre-push 2>/dev/null || true
HOOK="$sandbox/.githooks/pre-push"
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
# A backwards force-push yields an empty range. Expanding an empty array under `set -u`
# is an error on bash 3.2, which is /bin/bash on macOS -- so this case is about the
# guard surviving, not about what it finds.
check "empty range (backwards force-push) is not an error" 0 --range "$sanitized_tip" "$sanitized_tip"

echo
echo "the zero-remote range -- what the first push of a branch publishes"
# A push publishes what no remote-tracking ref already reaches, so modelling the
# zero-remote case at all needs a published base. `$sanitized_tip` carries the leak
# behind it: that is the whole point, because it is history the remote already holds
# and no push can fix.
git update-ref refs/remotes/origin/develop "$sanitized_tip"
printf 'Still clean.\n' >>docs.md
git add -A && git commit -qm "new branch, clean commit"
new_branch_tip=$(git rev-parse HEAD)
check "new branch scans its own commits, not published history" 0 --range "$zero" "$new_branch_tip"
printf 'ExampleBank holds four accounts.\n' >>docs.md
git add -A && git commit -qm "new branch, unpublished leak"
unpublished_leak_tip=$(git rev-parse HEAD)
check "new branch carrying an UNPUBLISHED leak is still blocked" 1 --range "$zero" "$unpublished_leak_tip"
# Kept on a ref so the later hook case addresses a commit the repository still
# names, rather than one only the reflog remembers.
git branch -q unpublished-leak "$unpublished_leak_tip"
git reset -q --hard "$new_branch_tip"

echo
echo "case-sensitive tokens -- the file for a name that is also an English word"
printf 'Sparrow\n' >deployment/roster-tokens-cased.txt
printf 'The sparrow flew past the window.\n' >>docs.md
check "cased token does not match the ordinary English word" 0
git checkout -q docs.md
printf 'Sparrow holds four accounts.\n' >>docs.md
check "cased token matches its capitalized whole-word form" 1
git checkout -q docs.md
printf 'A Sparrowhawk is a bird.\n' >>docs.md
check "cased token does not match inside a longer word" 0
git checkout -q docs.md
printf 'Two Words\n' >deployment/roster-tokens-cased.txt
check "cased file honours the same single-word validator" 2
rm -f deployment/roster-tokens-cased.txt

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
echo "pre-push hook -- the guard's only real consumer"
ZERO40=$(printf '%040d' 0)

# The hook reads `<local_ref> <local_sha> <remote_ref> <remote_sha>` on stdin. These
# cases exercise that parsing, the deletion skip, and the fail-closed branch -- none of
# which the guard's own cases can reach.
hook_check() {
    local name=$1 expected=$2 stdin=$3
    local rc=0
    printf '%s\n' "$stdin" | "$HOOK" >/dev/null 2>&1 || rc=$?
    if [[ $rc == "$expected" ]]; then
        printf '  ok    %-58s (exit %s)\n' "$name" "$rc"
        pass=$((pass + 1))
    else
        printf '  FAIL  %-58s (exit %s, wanted %s)\n' "$name" "$rc" "$expected"
        fail=$((fail + 1))
    fi
}

if [[ -x $HOOK ]]; then
    printf 'examplebank\n' >deployment/roster-tokens.txt
    printf 'someoperator\n' >deployment/identity-tokens.txt

    hook_check "clean range passes" 0 \
        "refs/heads/develop $clean_tip refs/heads/develop $clean_tip"
    hook_check "range carrying the leak is blocked" 1 \
        "refs/heads/develop $sanitized_tip refs/heads/develop $clean_tip"
    hook_check "new branch whose commits are already published passes" 0 \
        "refs/heads/develop $new_branch_tip refs/heads/develop $ZERO40"
    hook_check "new branch carrying an unpublished leak is blocked" 1 \
        "refs/heads/develop $unpublished_leak_tip refs/heads/develop $ZERO40"
    hook_check "branch deletion pushes no content, so it passes" 0 \
        "refs/heads/develop $ZERO40 refs/heads/develop $sanitized_tip"

    chmod -x tests/preferences/check-no-personal-data.sh
    hook_check "non-executable guard BLOCKS rather than passing silently" 1 \
        "refs/heads/develop $clean_tip refs/heads/develop $clean_tip"
    chmod +x tests/preferences/check-no-personal-data.sh

    mv tests/preferences/check-no-personal-data.sh tests/preferences/.stashed
    hook_check "missing guard BLOCKS rather than passing silently" 1 \
        "refs/heads/develop $clean_tip refs/heads/develop $clean_tip"
    mv tests/preferences/.stashed tests/preferences/check-no-personal-data.sh
else
    printf '  FAIL  %-58s (hook not found at %s)\n' "pre-push hook present" "$HOOK"
    fail=$((fail + 1))
fi

echo
echo "the LICENSE copyright carve-out -- the guard's ONLY exemption"
#
# 🔴 These run through the REAL scan path, in BOTH modes. The carve-out judges a
# location string that `scan_worktree` builds with `cut -d: -f1-2` and
# `scan_revs` with `cut -d: -f1-3`; a control that hand-writes "LICENSE:3" never
# touches that derivation, so it can pass while the thing that actually runs
# does not. `--range` is the mode `.githooks/pre-push` uses, and before these
# cases it had never met a LICENSE at all.
# The base for the range case is captured HERE, not `clean_tip`: the history-mode
# section above deliberately committed a leak and then sanitized the tip, so a
# range from `clean_tip` legitimately catches THAT leak and would report a green
# carve-out as red for a reason having nothing to do with the carve-out.
pre_licence_tip=$(git rev-parse HEAD)
printf 'MIT License\n\nCopyright (c) 2026 someoperator\n' >LICENSE
git add -A && git commit -qm "add a licence"
licence_tip=$(git rev-parse HEAD)
check "LICENSE copyright naming the author passes" 0
check "  ... and passes in --rev mode" 0 --rev "$licence_tip"
check "  ... and passes in --range mode (what pre-push runs)" 0 --range "$pre_licence_tip" "$licence_tip"

printf 'MIT License\n\nCopyright (c) 2026 ExampleBank\n' >LICENSE
check "a ROSTER token as the copyright holder is still caught" 1

# 🔴 The case the strip's word-exactness exists for. `someoperatorbank` contains
# the identity token `someoperator`, and an UNBOUNDED substring strip would cut
# the identity part out of it -- leaving `bank`, which the roster pattern no
# longer matches, silently exempting the institution. Dropping only whole words
# that ARE identity tokens keeps this one intact and caught.
printf 'someoperatorbank\n' >>deployment/roster-tokens.txt
printf 'MIT License\n\nCopyright (c) 2026 someoperatorbank\n' >LICENSE
check "a roster token CONTAINING an identity token survives the strip" 1
printf 'examplebank\n' >deployment/roster-tokens.txt   # drop the extra token again

# 🔴 `git add` is load-bearing: the guard scans TRACKED files, so an untracked
# probe is never read and the case reports a false pass. That shape cost this
# branch a wrong claim once already.
printf '# Copyright (c) 2026 someoperator\n' >notalicence.py
git add notalicence.py
check "a copyright line OUTSIDE LICENSE is not exempted" 1
git rm -q -f notalicence.py

printf 'MIT License\n\nmaintained by someoperator\n' >LICENSE
check "a NON-copyright line in LICENSE is not exempted" 1

# Restored in the working tree only. No commit: the content equals what
# `licence_tip` already holds, so `git commit` would find nothing to do and exit
# non-zero, which under `set -e` kills the harness AFTER it has printed a clean
# tally -- a green run reported as a failure.
printf 'MIT License\n\nCopyright (c) 2026 someoperator\n' >LICENSE
git add -A

echo
echo "no roster present -- the state every other clone is in"
rm -f deployment/roster-tokens.txt deployment/identity-tokens.txt deployment/roster-tokens-cased.txt
printf 'ExampleBank holds four accounts.\n' >>docs.md
check "no token files: passes, nothing to leak" 0

echo
printf 'selftest: %d passed, %d failed\n' "$pass" "$fail"
(( fail == 0 )) || exit 1
