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
# WHAT A PUSH PUBLISHES
#
# A push is checked against the commits it actually publishes. When the remote sha
# is all-zero the remote has never seen this branch, and the commits being
# published are everything reachable from the tip that no remote-tracking ref
# already reaches -- not everything reachable, which on a branch cut from an
# existing one is almost entirely history the remote already holds.
#
# That narrowing rests on a PRECONDITION, and it is a real one: trusting
# remote-tracking refs means already-pushed history is never re-read, which is the
# exact blind spot this guard exists to close. A one-off full-history audit closes
# it instead, once, and its adjudicated matches are recorded with the operator's
# token lists in `deployment/`. The audit is per-deployment because the tokens are:
# an audit run against one operator's roster says nothing about another's. Run it
# before relying on the narrowed range, and again whenever a token is added.
# See docs/deployment-requirements.template.md §8.
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
# Three token files, two matching modes. `roster-tokens.txt` and
# `identity-tokens.txt` match case-insensitively on word boundaries.
# `roster-tokens-cased.txt` matches case-SENSITIVELY on word boundaries, for the
# institution whose name is also an ordinary English word: the leaked form of a
# proper noun is capitalized, so the cased file catches it while ordinary prose
# using the lowercase word goes free. That is a genuine reduction in coverage --
# the lowercased and embedded forms are no longer caught -- so a token belongs in
# the cased file ONLY when its lowercase form is an ordinary English word. The
# default is the strict files.
#
# Tokens are validated at load whichever file they come from, because a token that
# cannot match is worse than a missing one: it still counts toward a reassuring
# total. See docs/deployment-requirements.template.md §8.
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
ROSTER_CASED_FILE="$TOKEN_DIR/roster-tokens-cased.txt"

usage() {
    cat >&2 <<'USAGE_EOF'
usage:
  check-no-personal-data.sh                    scan the working tree (weaker: not push-safe)
  check-no-personal-data.sh --rev <rev>        scan the tree at one revision
  check-no-personal-data.sh --range <a> <b>    scan every commit in a..b (if a is all-zero, every
                                               commit in b that no remote-tracking ref reaches)
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
cased_tokens=()
# The identity class, kept SEPARATELY as well as merged into `tokens`. The
# copyright carve-out below exempts an author's name and must never exempt an
# institution's, so it needs to know which class a token came from. Merged-only
# would make "Copyright (c) 2026 <bank>" pass.
identity_tokens=()

# The matching mode is a property of the FILE, never of the token line. A prefix
# or a second column would have to pass the single-word validator below, and that
# validator's strictness is what stops a name that matches nothing from counting
# toward a reassuring total -- loosening it to carry a mode would trade a
# guarantee for a feature. A file name states the semantics for everything in it,
# and it is visible at review time.
load_tokens() {
    local file=$1 mode=$2 line n=0
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
        if [[ $mode == cased ]]; then
            cased_tokens+=("$line")
        else
            tokens+=("$line")
        fi
        if [[ $mode == identity ]]; then
            identity_tokens+=("$line")
        fi
    done <"$file"
    return 0
}

load_tokens "$ROSTER_FILE" nocase
# `identity` is `nocase` plus membership of the identity class. The mode is still
# a property of the FILE, per the note above; this one names two facts about that
# file rather than carrying a per-line prefix.
load_tokens "$IDENTITY_FILE" identity
load_tokens "$ROSTER_CASED_FILE" cased

real_token_count=$(( ${#tokens[@]} + ${#cased_tokens[@]} ))

note=""
if [[ ! -f $ROSTER_FILE && ! -f $IDENTITY_FILE && ! -f $ROSTER_CASED_FILE ]]; then
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

# An empty class leaves its pattern empty, and every use is guarded on that: a
# pattern built from no tokens is `(...)()(...)`, which matches every line.
pattern=""
cased_pattern=""
if (( ${#tokens[@]} > 0 )); then
    pattern=$(build_pattern "${tokens[@]}")
fi
if (( ${#cased_tokens[@]} > 0 )); then
    cased_pattern=$(build_pattern "${cased_tokens[@]}")
fi

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
# Each class strips under its own case rule, so the slug exemption is exactly as
# wide as the match it exempts.
token_alt=""
cased_token_alt=""
if (( ${#tokens[@]} > 0 )); then
    token_alt=$(printf '%s|' "${tokens[@]}")
    token_alt=${token_alt%|}
fi
if (( ${#cased_tokens[@]} > 0 )); then
    cased_token_alt=$(printf '%s|' "${cased_tokens[@]}")
    cased_token_alt=${cased_token_alt%|}
fi

# --- the copyright carve-out ----------------------------------------------
#
# A copyright line in LICENSE names the AUTHOR, and an author's name is public by
# construction the moment this repository is published under a licence bearing
# it. That is the same reasoning the slug strip above rests on -- "the owner
# segment is inherently public the moment this repository is" -- applied to the
# one other place authorship is unavoidable. An MIT grant with nobody granting it
# is not a licence, so the alternative to this carve-out is not a safer LICENSE;
# it is no enforceable one.
#
# 🔴 What this guard exists to keep out is the OPERATOR's identity: whose accounts
# these are, at which institutions, holding what. Authorship is a different fact
# about a different role, and conflating the two is what made the norm and the
# licence look incompatible.
#
# Narrow on THREE axes, because any one alone leaks:
#   - the FILE must be LICENSE. A copyright header in a source file is not exempt.
#   - the LINE must be a copyright notice in the conventional shape.
#   - only IDENTITY-class tokens are removed. A roster token -- an institution --
#     on that same line still fails, which is the case the control below proves.
# Everything else on the line stays in scope, exactly as the slug strip does.
# `LICENSE` at the end of the path, whether the location is `LICENSE:3` from a
# working-tree scan or `<commit>:LICENSE:3` from a history scan.
copyright_location='(^|:)LICENSE:[0-9]+$'
copyright_shape='^[[:space:]]*Copyright[[:space:]]+([(][cC][)]|©)[[:space:]]+[0-9]{4}([[:space:]]*-[[:space:]]*[0-9]{4})?[[:space:]]'

# Everything up to and including the year. What follows it is the HOLDER.
copyright_prefix='^[[:space:]]*Copyright[[:space:]]+([(][cC][)]|©)[[:space:]]+[0-9]{4}([[:space:]]*-[[:space:]]*[0-9]{4})?[[:space:]]+'

is_identity_token() {
    local word=$1 lowered token
    lowered=$(printf '%s' "$word" | tr '[:upper:]' '[:lower:]')
    for token in "${identity_tokens[@]}"; do
        [[ $lowered == "$(printf '%s' "$token" | tr '[:upper:]' '[:lower:]')" ]] && return 0
    done
    return 1
}

strip_public_copyright() {
    local location=$1 out=$2 holder word kept=""
    (( ${#identity_tokens[@]} > 0 )) || { printf '%s' "$out"; return 0; }
    [[ $location =~ $copyright_location ]] || { printf '%s' "$out"; return 0; }
    grep -qE "$copyright_shape" <<<"$out" || { printf '%s' "$out"; return 0; }

    # Only the holder segment is considered; the prefix is boilerplate with no
    # token in it, and anything before `Copyright` is not part of the notice.
    # `#` as the delimiter, not `|`: the prefix pattern CONTAINS `|` for its
    # alternation, and a `|` delimiter closes the expression in the middle of it.
    # The guard caught this by failing closed rather than by reporting clean.
    holder=$(sed -E "s#${copyright_prefix}##" <<<"$out" 2>/dev/null) \
        || die "copyright-strip failed (sed error) -- refusing to report clean"

    # 🔴 WORD-EXACT, not substring. Every match pattern in this guard is anchored
    # on word boundaries, and a substring strip would not be: an identity token
    # occurring INSIDE a roster token would be cut out of it, leaving a mangled
    # remainder that the roster pattern no longer matches -- silently exempting
    # the institution this guard exists to catch. Dropping whole words that ARE
    # identity tokens, and keeping every other word intact, makes the exemption
    # exactly as wide as the tokens it is for.
    for word in $holder; do
        if ! is_identity_token "${word%%[.,;]}"; then
            kept="${kept:+$kept }$word"
        fi
    done
    printf '%s' "$kept"
}

strip_public_slugs() {
    local out=$1
    if [[ -n $token_alt ]]; then
        out=$(sed -E "s#(${token_alt})/[A-Za-z0-9_.-]+##gI" <<<"$out") \
            || die "slug-strip failed (sed error) -- refusing to report clean"
    fi
    if [[ -n $cased_token_alt ]]; then
        out=$(sed -E "s#(${cased_token_alt})/[A-Za-z0-9_.-]+##g" <<<"$out") \
            || die "slug-strip failed (sed error) -- refusing to report clean"
    fi
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
control_probe() {
    local token=$1 mode=$2
    local probe="harmless prefix ${token} harmless suffix" stripped probe_dir rc=0

    # Leg 1: the engine that actually SCANS.
    probe_dir=$(mktemp -d) || die "positive control: cannot create a probe directory"
    printf '%s\n' "$probe" >"$probe_dir/probe.txt"
    if [[ $mode == cased ]]; then
        ( cd "$probe_dir" && git grep --no-index -a -n -E -e "$cased_pattern" -- probe.txt ) \
            >/dev/null 2>&1 || rc=$?
    else
        ( cd "$probe_dir" && git grep --no-index -a -i -n -E -e "$pattern" -- probe.txt ) \
            >/dev/null 2>&1 || rc=$?
    fi
    rm -rf "$probe_dir"
    (( rc == 0 )) || die "positive control FAILED ($mode, git grep exit $rc) -- a known token was
       not matched by the scanning engine. The guard cannot prove it is scanning;
       refusing to report clean."

    # Leg 2: the engine that JUDGES each hit, including the slug strip.
    stripped=$(strip_public_slugs "$probe")
    if [[ $mode == cased ]]; then
        grep -qE "$cased_pattern" <<<"$stripped" \
            || die "positive control FAILED ($mode) -- the judging path did not match a known
       token. Refusing to report clean."
    else
        grep -qiE "$pattern" <<<"$stripped" \
            || die "positive control FAILED ($mode) -- the judging path did not match a known
       token. Refusing to report clean."
    fi
}

# Every class that will be scanned is proved first. A control that covers one mode
# while the other scans unproven is the same fail-open shape with a smaller hole.
# Defined here rather than beside `report`, because the controls below judge
# through it: a control that cannot reach the judging path proves nothing about
# the judging path. It depends only on the patterns, which are already built.
still_matches() {
    local stripped=$1
    if [[ -n $pattern ]] && grep -qiE "$pattern" <<<"$stripped"; then
        return 0
    fi
    if [[ -n $cased_pattern ]] && grep -qE "$cased_pattern" <<<"$stripped"; then
        return 0
    fi
    return 1
}

# The copyright carve-out's controls. A carve-out with no negative control is how
# an exemption silently widens: the positive leg alone passes just as well when
# the rule exempts the whole file.
copyright_control() {
    local holder=$1 line stripped

    # POSITIVE: the exempted shape, in the exempted file, is actually exempted.
    # Without this the carve-out could be inert and LICENSE would simply block.
    line="Copyright (c) 2026 ${holder}"
    stripped=$(strip_public_copyright "LICENSE:3" "$line")
    if still_matches "$stripped"; then
        die "copyright control FAILED -- a LICENSE copyright line naming the author still
       matches. The carve-out is not working; refusing to report clean."
    fi

    # NEGATIVE 1: the same line ANYWHERE ELSE still blocks. The carve-out is
    # scoped to LICENSE, and a copyright header in a source file is not exempt.
    stripped=$(strip_public_copyright "src/bankmachine/__init__.py:1" "$line")
    if ! still_matches "$stripped"; then
        die "copyright control FAILED -- the carve-out exempted a copyright line OUTSIDE
       LICENSE. It is scoped to LICENSE by design; refusing to report clean."
    fi

    # NEGATIVE 2: a NON-copyright line in LICENSE still blocks. The carve-out is
    # scoped to the notice shape, not to the file.
    stripped=$(strip_public_copyright "LICENSE:9" "contact ${holder} about this software")
    if ! still_matches "$stripped"; then
        die "copyright control FAILED -- the carve-out exempted a line in LICENSE that is
       not a copyright notice. It is scoped to the notice shape; refusing to report clean."
    fi

    # NEGATIVE 3 -- the one that matters most. A ROSTER token on the copyright
    # line still blocks. Only the identity class is public-by-authorship; an
    # institution name in a copyright notice is exactly the leak this guard is
    # for, and it would be the cheapest place to hide one.
    if (( ${#tokens[@]} > ${#identity_tokens[@]} )); then
        local roster_token=""
        local t
        for t in "${tokens[@]}"; do
            local is_identity=0 i
            for i in "${identity_tokens[@]}"; do
                [[ $t == "$i" ]] && is_identity=1 && break
            done
            (( is_identity == 0 )) && roster_token=$t && break
        done
        if [[ -n $roster_token ]]; then
            stripped=$(strip_public_copyright "LICENSE:3" "Copyright (c) 2026 ${roster_token}")
            if ! still_matches "$stripped"; then
                die "copyright control FAILED -- the carve-out exempted a ROSTER token on a
       LICENSE copyright line. Only the identity class is exempt there; refusing to
       report clean."
            fi
        fi
    fi
}

self_test() {
    if [[ -n $pattern ]]; then
        control_probe "${tokens[0]}" nocase
    fi
    if [[ -n $cased_pattern ]]; then
        control_probe "${cased_tokens[0]}" cased
    fi
    if (( ${#identity_tokens[@]} > 0 )); then
        copyright_control "${identity_tokens[0]}"
    fi
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
    stripped=$(strip_public_copyright "$location" "$stripped")
    # If nothing matches once identity-owned slugs and a LICENSE copyright holder
    # are removed, the only hit was one of those. Test explicitly rather than
    # treating any failure as a false positive.
    if ! still_matches "$stripped"; then
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
    local mode=$1; shift
    local rc=0 out
    if [[ $mode == cased ]]; then
        out=$(git grep -a -n -E -e "$cased_pattern" "$@" 2>&1) || rc=$?
    else
        out=$(git grep -a -i -n -E -e "$pattern" "$@" 2>&1) || rc=$?
    fi
    if (( rc >= 2 )); then
        die "git grep failed (exit $rc): $out"
    fi
    printf '%s' "$out"
}

# `git grep` takes one case flag per invocation, so the two matching modes are two
# passes. A line carrying tokens from both classes is one leak, not two, hence the
# de-duplication.
scan_output() {
    local out all=""
    if [[ -n $pattern ]]; then
        out=$(git_grep nocase "$@")
        if [[ -n $out ]]; then all="${all}${out}"$'\n'; fi
    fi
    if [[ -n $cased_pattern ]]; then
        out=$(git_grep cased "$@")
        if [[ -n $out ]]; then all="${all}${out}"$'\n'; fi
    fi
    printf '%s' "$all" | awk 'NF && !seen[$0]++'
}

scan_worktree() {
    local out line
    out=$(scan_output)
    [[ -n $out ]] || return 0
    while IFS= read -r line; do
        [[ -n $line ]] || continue
        report "$(cut -d: -f1-2 <<<"$line")" "$(cut -d: -f3- <<<"$line")"
    done <<<"$out"
}

scan_revs() {
    local out line
    (( $# > 0 )) || return 0
    out=$(scan_output "$@")
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
            # New branch on the remote. What this push publishes is what no
            # remote-tracking ref already reaches -- a branch cut from an existing
            # one publishes only its own commits, and refusing it over a match in
            # history the remote already holds is a refusal the push cannot act on.
            # The already-published side is covered by the one-off audit described
            # in the header, not by re-reading it on every push.
            while IFS= read -r r; do revs+=("$r"); done < <(git rev-list "$3" --not --remotes)
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

If a match is a genuine false positive, narrow the rule rather than dropping the
token -- a token removed from the list stops guarding every file, not just this
one. Where the token's lowercase form is an ordinary English word, moving it to
deployment/roster-tokens-cased.txt is that narrowing: it then matches only the
capitalized whole word.
BLOCKED_EOF
    exit 1
fi

# Keyed on the mode rather than on the revision count: a narrowed range is
# routinely empty, and reporting "clean over the working tree" for a scan that
# never looked at the working tree is the reassuring-total failure again.
scope_desc="working tree"
if [[ $mode == revs ]]; then
    scope_desc="${#revs[@]} commit(s)"
fi
echo "check-no-personal-data: clean ($real_token_count tokens over $scope_desc)${note}"
exit 0
