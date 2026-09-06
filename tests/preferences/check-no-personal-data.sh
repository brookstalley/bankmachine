#!/usr/bin/env bash
#
# Leak guard: no roster or operator identity in anything being pushed.
#
# This repository is a general-purpose tool. The engine knows nothing about any
# particular financial institution (system-requirements.md §0.1), and the set of
# institutions an operator actually connects is configuration living outside git
# (docs/deployment-requirements.template.md §0.1).
#
# WHAT IT CHECKS, AND WHY THAT SCOPE
#
# Given a revision range, this scans EVERY COMMIT in the range, not the working
# tree. That is deliberate and it is the whole point: the leak this project
# actually suffered was three documentation paths sitting in already-pushed
# history behind a clean tip. A tip-only or worktree-only check passes that case,
# and would have reported "clean" on the exact exposure it exists to stop.
#
# Invoked with no arguments it scans the working tree instead, which is the
# weaker guarantee -- useful while editing, not sufficient before a push. The
# pre-push hook always passes a range.
#
# COST: a range scan greps every reachable commit's full tree, so the first push
# of a large history is slow. That is the price of the guarantee, but a hook that
# takes minutes is a hook people learn to `--no-verify` past. If this repository's
# history ever grows enough for that to bite, narrow the range rather than
# weakening the check -- scanning `git rev-list --max-count` of the newest commits
# plus a one-off audit of the rest is a real answer; skipping the scan is not.
#
# TOKENS
#
# Every token comes from the gitignored `deployment/` directory. NONE are
# hardcoded here, for two reasons: a script carrying the names it hunts for
# cannot scan itself (and the file it would skip is the one file guaranteed to
# contain them), and anyone who clones this published repository would otherwise
# inherit a guard protecting a stranger's identity while protecting none of their
# own. A checkout with no `deployment/` directory has nothing to leak and says so.
#
# Tokens are matched case-insensitively on word boundaries. They are validated at
# load, because a token that cannot match is worse than a missing one: it still
# counts toward a reassuring total. See docs/deployment-requirements.template.md §8.
#
# FAILING CLOSED
#
# Every error path here aborts. A guard whose only bad-news channel is the absence
# of output cannot report that it stopped guarding, so before scanning anything
# real this runs a positive control: a canary string through the same pattern
# build, the same grep and the same slug-strip. If the canary is not caught, the
# guard exits non-zero instead of printing "clean".
#
# Exit 0 = clean. Exit 1 = a leak. Exit 2 = the guard could not do its job.
#
# Bypassable with `git push --no-verify` by design: a guardrail against slips,
# not an access control.

set -euo pipefail

die() {
    echo "check-no-personal-data: ABORT -- $*" >&2
    exit 2
}

repo_root=$(git rev-parse --show-toplevel) || die "not in a git repository"
cd "$repo_root"

TOKEN_DIR="deployment"
ROSTER_FILE="$TOKEN_DIR/roster-tokens.txt"
IDENTITY_FILE="$TOKEN_DIR/identity-tokens.txt"

usage() {
    cat >&2 <<'USAGE_EOF'
usage:
  check-no-personal-data.sh                    scan the working tree (weaker: not push-safe)
  check-no-personal-data.sh --rev <rev>        scan the tree at one revision
  check-no-personal-data.sh --range <a> <b>    scan every commit in a..b (all of b if a is all-zero)
USAGE_EOF
    exit 2
}

# --- token loading, with validation --------------------------------------
#
# A token must be a single word of [A-Za-z0-9_-]. Two reasons, both learned the
# hard way: an ERE metacharacter makes the whole alternation invalid, which under
# a naive implementation silences every file at once; and a multi-word name
# collapsed into one token can never match the spaced form it was added to catch.
# Both fail loudly here instead.

tokens=()

load_tokens() {
    local file=$1 line n=0
    [[ -f $file ]] || return 0
    # `|| [[ -n $line ]]` so a file with no trailing newline does not lose its
    # last token -- read returns non-zero at EOF with data still in $line.
    while IFS= read -r line || [[ -n $line ]]; do
        n=$((n + 1))
        line=${line%%#*}
        # Trim only the edges. Deleting all whitespace would silently concatenate
        # a multi-word name into a token that matches nothing.
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        [[ -n $line ]] || continue
        if [[ ! $line =~ ^[A-Za-z0-9_-]+$ ]]; then
            die "$file:$n: invalid token '$line' -- a token is a single word of letters, digits, _ or -.
       Multi-word names are listed as the spellings that actually appear in prose
       (one line per word, or the concatenated form), never as one multi-word line.
       See docs/deployment-requirements.template.md §8."
        fi
        tokens+=("$line")
    done <"$file"
    return 0
}

load_tokens "$ROSTER_FILE"
load_tokens "$IDENTITY_FILE"

real_token_count=${#tokens[@]}

note=""
if [[ ! -f $ROSTER_FILE && ! -f $IDENTITY_FILE ]]; then
    note="  (no $TOKEN_DIR/ token files on this checkout -- nothing to leak)"
fi

# --- pattern construction -------------------------------------------------

# Word boundaries are spelled out rather than written `\b`. `git grep -E` does NOT
# honour `\b` -- it silently matches nothing -- while system grep does, so a `\b`
# pattern makes the two engines disagree: the scan finds nothing while a control
# run through system grep passes. That is precisely the fail-open shape this guard
# exists to refuse, and it was caught by the self-test rather than by reading.
boundary_start='(^|[^A-Za-z0-9_])'
boundary_end='([^A-Za-z0-9_]|$)'

build_pattern() {
    local joined
    joined=$(printf '%s|' "$@")
    printf '%s(%s)%s' "$boundary_start" "${joined%|}" "$boundary_end"
}

# Nothing to scan for. Say so and stop -- this is the ordinary state of every
# checkout that is not this deployment's, and it is not a failure.
if (( real_token_count == 0 )); then
    echo "check-no-personal-data: clean (0 tokens)${note}"
    exit 0
fi

pattern=$(build_pattern "${tokens[@]}")

# Identity-owned GitHub slugs (`owner/repo`) are stripped before a line is judged.
# The owner segment is inherently public the moment this repository is -- it is in
# the clone URL -- and both `.claude/settings.json` and `project-state.yaml` carry
# real slugs. Only slugs whose OWNER is a known token are stripped, never
# `word/word` generally: a path like `docs/<institution>-notes.md` must still be
# caught, and a general rule would exempt exactly that.
#
# Stripping rather than skipping keeps the rest of the line in scope, so
# `<owner>/<repo> -- <institution> roster` still fails on the institution.
#
# BSD sed (macOS) has no `\b`, hence anchoring on the token alternation instead.
token_alt=$(printf '%s|' "${tokens[@]}")
token_alt=${token_alt%|}

strip_public_slugs() {
    local out
    out=$(sed -E "s#(${token_alt})/[A-Za-z0-9_.-]+##gI" <<<"$1") \
        || die "slug-strip failed (sed error) -- refusing to report clean"
    printf '%s' "$out"
}

# --- positive control -----------------------------------------------------
#
# Run the canary through the SAME path a real line takes. If any part of the
# machinery is broken -- an invalid pattern, a sed incompatibility, a grep that
# cannot run -- this fails here rather than printing "clean" on an unscanned tree.

# Positive control. Deliberately probes with a REAL token through the REAL
# pattern: a control built from a synthetic sentinel proves only that the
# sentinel matches, which is how a guard comes to report "clean" over a scan
# that matched nothing. Both legs must pass, because the two engines below have
# already been observed to disagree.
self_test() {
    local probe="harmless prefix ${tokens[0]} harmless suffix" stripped probe_dir rc=0

    # Leg 1: the engine that actually SCANS.
    probe_dir=$(mktemp -d) || die "positive control: cannot create a probe directory"
    printf '%s\n' "$probe" >"$probe_dir/probe.txt"
    ( cd "$probe_dir" && git grep --no-index -a -i -n -E -e "$pattern" -- probe.txt ) \
        >/dev/null 2>&1 || rc=$?
    rm -rf "$probe_dir"
    (( rc == 0 )) || die "positive control FAILED (git grep exit $rc) -- a known token was not
       matched by the scanning engine. The guard cannot prove it is scanning;
       refusing to report clean."

    # Leg 2: the engine that JUDGES each hit, including the slug strip.
    stripped=$(strip_public_slugs "$probe")
    grep -qiE "$pattern" <<<"$stripped" \
        || die "positive control FAILED -- the judging path did not match a known token.
       Refusing to report clean."
}

self_test

# --- scanning -------------------------------------------------------------
#
# `git grep` is used rather than a file loop so that commits can be scanned
# directly, and so that path quoting (core.quotePath) never turns a non-ASCII
# filename into a silently skipped file.

found=0

report() {
    local location=$1 text=$2 stripped
    stripped=$(strip_public_slugs "$text")
    # If nothing matches once identity-owned slugs are removed, the only hit was
    # a slug. Test explicitly rather than treating any failure as a false positive.
    if ! grep -qiE "$pattern" <<<"$stripped"; then
        return 0
    fi
    if (( found == 0 )); then
        echo "check-no-personal-data: BLOCKED -- roster or operator identity found" >&2
        echo >&2
        found=1
    fi
    echo "  $location: $text" >&2
}

git_grep() {
    local rc=0 out
    out=$(git grep -a -i -n -E -e "$pattern" "$@" 2>&1) || rc=$?
    if (( rc >= 2 )); then
        die "git grep failed (exit $rc): $out"
    fi
    printf '%s' "$out"
}

scan_worktree() {
    local out line
    out=$(git_grep)
    [[ -n $out ]] || return 0
    while IFS= read -r line; do
        [[ -n $line ]] || continue
        report "$(cut -d: -f1-2 <<<"$line")" "$(cut -d: -f3- <<<"$line")"
    done <<<"$out"
}

scan_revs() {
    local out line
    (( $# > 0 )) || return 0
    out=$(git_grep "$@")
    [[ -n $out ]] || return 0
    while IFS= read -r line; do
        [[ -n $line ]] || continue
        # `<rev>:<path>:<lineno>:<text>`
        report "$(cut -d: -f1-3 <<<"$line")" "$(cut -d: -f4- <<<"$line")"
    done <<<"$out"
}

mode=worktree
revs=()
case "${1:-}" in
    "")    mode=worktree ;;
    --rev)
        [[ -n ${2:-} ]] || usage
        mode=revs
        revs=("$2")
        ;;
    --range)
        [[ -n ${3:-} ]] || usage
        mode=revs
        zero=$(git hash-object --stdin </dev/null | tr '0-9a-f' '0')
        if [[ $2 == "$zero" ]]; then
            # New branch on the remote: every commit reachable from the tip is
            # being published, so every one of them is in scope.
            while IFS= read -r r; do revs+=("$r"); done < <(git rev-list "$3")
        else
            while IFS= read -r r; do revs+=("$r"); done < <(git rev-list "$2..$3")
        fi
        ;;
    -h|--help) usage ;;
    *)         usage ;;
esac

case $mode in
    worktree) scan_worktree ;;
    # `${revs[@]+...}` because expanding an empty array under `set -u` is an error on
    # bash 3.2, which is what /bin/bash still is on macOS. Reachable on a backwards
    # force-push, where an empty range is the correct answer rather than a fault.
    revs)     scan_revs ${revs[@]+"${revs[@]}"} ;;
esac

if (( found == 1 )); then
    cat >&2 <<'BLOCKED_EOF'

This repository is a general-purpose tool and carries no operator's roster.
Institution names, account details and operator identity belong in `deployment/`,
which is gitignored -- see docs/deployment-requirements.template.md.

If a commit already in history carries this, sanitizing the tip is not enough:
the history itself has to be rewritten before the branch is pushed.

If a match is a genuine false positive, narrow the rule in this script rather
than dropping the token -- a token removed from the list stops guarding every
file, not just this one.
BLOCKED_EOF
    exit 1
fi

scope_desc="working tree"
(( ${#revs[@]} > 0 )) && scope_desc="${#revs[@]} commit(s)"
echo "check-no-personal-data: clean ($real_token_count tokens over $scope_desc)${note}"
exit 0
