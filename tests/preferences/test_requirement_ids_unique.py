"""Requirement ids are unique within a requirements document.

The two requirements docs cite each other by id, and §7 of a roster document
calls itself "the checkable form of §0.2" -- which it cannot be against an
ambiguous key. A duplicate id silently resolves a citation to the wrong
requirement, and the one a reader lands on by accident is as likely to be a
scheduled job as the immutable enrollment parameter the doc calls its
highest-stakes one.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
REQUIREMENT_DOCS = ("docs/system-requirements.md", "docs/deployment-requirements.template.md")
#: A DECLARATION, not a citation. The engine's document writes `**AC-1.2** --`
#: and the deployment template writes ``**`DAC-1.1` --``; a cross-document
#: citation ("engine AC-0.2") carries no bold, which is what separates them.
ID_PATTERN = re.compile(r"\*\*`?(D?AC-[A-Za-z0-9.]*[A-Za-z0-9])`?")


@pytest.mark.parametrize("relative", REQUIREMENT_DOCS)
def test_requirement_ids_are_unique_within_the_document(relative: str) -> None:
    path = REPO_ROOT / relative
    if not path.exists():
        pytest.skip(f"{relative} is not in this checkout")

    ids = ID_PATTERN.findall(path.read_text(encoding="utf-8"))
    assert ids, f"{relative} declared no requirement ids -- the pattern has drifted"
    duplicates = sorted(i for i, count in Counter(ids).items() if count > 1)
    assert not duplicates, f"{relative} declares these ids more than once: {duplicates}"
