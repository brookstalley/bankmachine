"""The licence is asserted in three places, and they say the same thing.

`LICENSE` carries the grant, `pyproject.toml` carries the SPDX expression every
installer and package index reads, and `README.md` tells a human which licence
they are getting, in every tracked doc that mentions one. Nothing derives any of
them from the others, so they can drift apart and each one on its own still reads
as authoritative -- which is the shape this repository tests rather than trusts
everywhere else it holds one fact in two files (`store/schema.py` against the
frozen DDL, the documented tool surface against the built one).

🔴 Drift here is not cosmetic. A `pyproject.toml` saying MIT over a `LICENSE`
saying something else is a **distribution** claim that disagrees with the grant
it ships beside, and the reader who acts on the wrong one is a stranger relying
on it.

The cases assert AGREEMENT, never a particular licence. Relicensing is the
owner's decision; it should take one deliberate edit per site and go green, not
require deleting a test that turns out to have pinned MIT by accident.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
LICENSE = REPO_ROOT / "LICENSE"
PYPROJECT = REPO_ROOT / "pyproject.toml"

#: A `## Licence` / `## License` heading and the section body under it.
_SECTION = re.compile(r"^(##+)\s*Licen[cs]e\s*$(.*?)(?=^\1\s|\Z)", re.M | re.S)


def _licence_sections() -> list[tuple[Path, str]]:
    """Every tracked Markdown file carrying a licence section, FOUND not listed.

    🔴 Derived from `git ls-files` rather than from a list in this file, and that
    is the whole construction. The first version of this case named three sites
    it had been told about; there were four, and `docs/README.md` sat there
    saying "No licence has been chosen yet, so default copyright applies" while
    the case went green over the other three. A guarantee defined by an
    enumeration decays the moment someone adds a file, and the enumeration is
    exactly what nobody updates.
    """
    listed = subprocess.run(
        ["git", "ls-files", "*.md"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    found: list[tuple[Path, str]] = []
    for name in listed:
        path = REPO_ROOT / name
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        for match in _SECTION.finditer(text):
            found.append((path, match.group(2)))
    return found


#: The SPDX identifiers this project knows how to recognise in a LICENSE body.
#: An identifier absent here is not a failure of the licence -- it is this map
#: needing an entry, and the assertion says so rather than reporting drift.
_LICENCE_HEADINGS = {
    "MIT": "MIT License",
    "Apache-2.0": "Apache License",
    "BSD-3-Clause": "BSD 3-Clause",
    "GPL-3.0-only": "GNU GENERAL PUBLIC LICENSE",
}


def _declared_spdx() -> str:
    with PYPROJECT.open("rb") as handle:
        data = tomllib.load(handle)
    licence = data["project"].get("license")
    assert isinstance(licence, str), (
        "pyproject.toml's `license` is not an SPDX string. PEP 639 expects an expression "
        f'like `license = "MIT"`; found {licence!r}. If this project moved to the legacy '
        "table form, this case must be re-pointed rather than deleted."
    )
    return licence


def test_the_licence_file_matches_the_spdx_expression() -> None:
    """The grant and the metadata that advertises it name the same licence."""
    spdx = _declared_spdx()
    assert spdx in _LICENCE_HEADINGS, (
        f"pyproject.toml declares `{spdx}`, which this case does not know how to find in a "
        f"LICENSE body. Add it to _LICENCE_HEADINGS with the heading its text opens with. "
        "🔴 Do NOT delete this case: an unrecognised identifier means the check cannot run, "
        "which is not the same as the licences agreeing."
    )
    body = LICENSE.read_text()
    heading = _LICENCE_HEADINGS[spdx]
    assert heading in body, (
        f"pyproject.toml declares `{spdx}` but {LICENSE} does not contain {heading!r}. "
        "The metadata every installer reads and the grant actually shipped disagree."
    )


def test_pyproject_points_license_files_at_the_file_that_exists() -> None:
    """`license-files` names a real path, so the built distribution carries the grant."""
    with PYPROJECT.open("rb") as handle:
        data = tomllib.load(handle)
    declared = data["project"].get("license-files")
    assert declared, (
        "pyproject.toml declares no `license-files`, so a built wheel or sdist ships the "
        "SPDX expression without the licence text it refers to."
    )
    for entry in declared:
        assert (REPO_ROOT / entry).exists(), (
            f"`license-files` names {entry!r}, which does not exist. The packaging metadata "
            "points at a grant that is not there."
        )


def test_every_licence_section_names_the_declared_licence() -> None:
    """Every tracked doc that states a licence states the SAME one.

    Matched on the SPDX token near a link to the licence file rather than on a
    fixed sentence: surrounding prose is free to be rewritten, and a case that
    pinned the wording would redden on an edit that changed nothing about the
    licence.
    """
    spdx = _declared_spdx()
    sections = _licence_sections()
    # 🔴 A scan that finds nothing passes forever. The repository has at least a
    # root README licence section; zero means the search broke, not that the
    # docs agree.
    assert sections, (
        "no tracked Markdown file has a licence section. Either the heading pattern no "
        "longer matches anything, or `git ls-files` returned nothing -- both mean this "
        "case examined NOTHING and its green says nothing."
    )
    for path, text in sections:
        rel = path.relative_to(REPO_ROOT)
        assert spdx in text, (
            f"{rel}'s licence section does not name `{spdx}`, which is what pyproject.toml "
            f"declares. Section text: {text.strip()[:200]!r}"
        )
        assert "LICENSE" in text, (
            f"{rel}'s licence section does not link to the licence file, so a reader is "
            "told the name of the licence with no path to its terms."
        )
