"""The engine names no financial institution, account or product (AC-0.3).

The roster changes over the product's life. Hardcoding it makes every roster
change a code change, and turns a general-purpose tool into one operator's
script. The roster, its per-account rules and its import-format adapters are
configuration.

The aggregator is expressly carved out -- it is a single named dependency in v1
(`system-requirements.md` §0.1), so its client package, the keychain service name
and the product name may name it.

Tokens come from the gitignored files under `deployment/`, never from a list in
this file: a test carrying the names it hunts for cannot scan itself, and a clone
of the published repository would inherit a check protecting a stranger's roster
while protecting none of its own.

🔴 **Two token files, two match modes, and BOTH are scanned here.** A roster
token whose lowercase form is an ordinary English word lives in
`roster-tokens-cased.txt` and matches case-SENSITIVELY, so the word may be used
as a word while the capitalized institution name is still caught. Reading only
the case-insensitive file would leave every token that moved across entirely
unscanned in `src/` and `tests/` -- the scope this test owns -- while the push
guard still covered them, which is a hole that reads as coverage.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
TOKEN_FILE = REPO_ROOT / "deployment" / "roster-tokens.txt"
CASED_TOKEN_FILE = REPO_ROOT / "deployment" / "roster-tokens-cased.txt"
SCANNED_ROOTS = ("src", "tests")
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def _tokens_from(path: Path) -> list[str]:
    if not path.exists():
        return []
    tokens = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        assert TOKEN_PATTERN.match(line), (
            f"{path.name} holds {line!r}, which cannot match on a word boundary. "
            f"A token that cannot match is worse than a missing one: it still counts "
            f"toward a reassuring total."
        )
        tokens.append(line)
    return tokens


def _tokens() -> list[str]:
    return _tokens_from(TOKEN_FILE)


def _cased_tokens() -> list[str]:
    return _tokens_from(CASED_TOKEN_FILE)


def _pattern(tokens: list[str], *, ignore_case: bool) -> re.Pattern[str]:
    """One spelling of the match rule, so the scan and its control cannot diverge."""
    return re.compile(
        r"\b(" + "|".join(re.escape(t) for t in tokens) + r")\b",
        re.IGNORECASE if ignore_case else 0,
    )


def _scanned_files() -> list[Path]:
    files: list[Path] = []
    for root in SCANNED_ROOTS:
        base = REPO_ROOT / root
        if base.exists():
            files.extend(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)
    return files


def test_the_source_and_schema_name_no_roster_institution_or_account() -> None:
    tokens, cased = _tokens(), _cased_tokens()
    if not tokens and not cased:
        pytest.skip("no roster token file in this checkout -- nothing to leak")

    patterns = [_pattern(tokens, ignore_case=True)] if tokens else []
    if cased:
        patterns.append(_pattern(cased, ignore_case=False))
    offenders: list[str] = []
    for path in _scanned_files():
        if path == Path(__file__):
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if any(pattern.search(line) for pattern in patterns):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}")

    assert not offenders, (
        "roster identity appears in the engine; the roster is configuration:\n  "
        + "\n  ".join(offenders)
    )


def test_the_scan_can_actually_find_a_token() -> None:
    """The positive control, over EVERY class the scan will run.

    🔴 One control covering one mode while the other scans unproven is the same
    fail-open shape the control exists to close: the untested pattern could match
    nothing forever and the suite would stay green.
    """
    tokens, cased = _tokens(), _cased_tokens()
    if not tokens and not cased:
        pytest.skip("no roster token file in this checkout")
    if tokens:
        assert _pattern(tokens, ignore_case=True).search(
            f"an account at {tokens[0]} was reconciled"
        )
    if cased:
        assert _pattern(cased, ignore_case=False).search(
            f"an account at {cased[0]} was reconciled"
        )


def test_a_cased_token_does_not_match_the_ordinary_word_it_shares_a_spelling_with() -> None:
    """🔴 The reason the cased file exists at all, asserted rather than assumed.

    A token lands there precisely because its lowercase form is an ordinary
    English word that appears in this repo's own prose. If the scan matched that
    word, the file would buy nothing and the token would have to leave the roster
    instead -- so this is the property that makes the split honest.
    """
    cased = _cased_tokens()
    if not cased:
        pytest.skip("no deployment/roster-tokens-cased.txt in this checkout")
    pattern = _pattern(cased, ignore_case=False)
    assert not pattern.search(f"the hold was {cased[0].lower()} for three days")


def test_something_is_actually_scanned() -> None:
    """A scan over zero files passes forever and means nothing."""
    assert len(_scanned_files()) > 5
