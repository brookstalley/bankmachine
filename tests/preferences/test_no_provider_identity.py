"""The engine names no financial institution, account or product (AC-0.3).

The roster changes over the product's life. Hardcoding it makes every roster
change a code change, and turns a general-purpose tool into one operator's
script. The roster, its per-account rules and its import-format adapters are
configuration.

The aggregator is expressly carved out -- it is a single named dependency in v1
(`system-requirements.md` §0.1), so its client package, the keychain service name
and the product name may name it.

Tokens come from the gitignored `deployment/roster-tokens.txt`, never from a list
in this file: a test carrying the names it hunts for cannot scan itself, and a
clone of the published repository would inherit a check protecting a stranger's
roster while protecting none of its own.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
TOKEN_FILE = REPO_ROOT / "deployment" / "roster-tokens.txt"
SCANNED_ROOTS = ("src", "tests")
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def _tokens() -> list[str]:
    if not TOKEN_FILE.exists():
        return []
    tokens = []
    for raw in TOKEN_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        assert TOKEN_PATTERN.match(line), (
            f"{TOKEN_FILE.name} holds {line!r}, which cannot match on a word boundary. "
            f"A token that cannot match is worse than a missing one: it still counts "
            f"toward a reassuring total."
        )
        tokens.append(line)
    return tokens


def _scanned_files() -> list[Path]:
    files: list[Path] = []
    for root in SCANNED_ROOTS:
        base = REPO_ROOT / root
        if base.exists():
            files.extend(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)
    return files


def test_the_source_and_schema_name_no_roster_institution_or_account() -> None:
    tokens = _tokens()
    if not tokens:
        pytest.skip("no deployment/roster-tokens.txt in this checkout -- nothing to leak")

    pattern = re.compile(r"\b(" + "|".join(re.escape(t) for t in tokens) + r")\b", re.IGNORECASE)
    offenders: list[str] = []
    for path in _scanned_files():
        if path == Path(__file__):
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}")

    assert not offenders, (
        "roster identity appears in the engine; the roster is configuration:\n  "
        + "\n  ".join(offenders)
    )


def test_the_scan_can_actually_find_a_token() -> None:
    """The positive control. A scan that has never matched is not known to work."""
    tokens = _tokens()
    if not tokens:
        pytest.skip("no deployment/roster-tokens.txt in this checkout")
    pattern = re.compile(r"\b(" + "|".join(re.escape(t) for t in tokens) + r")\b", re.IGNORECASE)
    assert pattern.search(f"an account at {tokens[0]} was reconciled")


def test_something_is_actually_scanned() -> None:
    """A scan over zero files passes forever and means nothing."""
    assert len(_scanned_files()) > 5
