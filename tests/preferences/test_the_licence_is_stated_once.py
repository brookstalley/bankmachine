"""The licence is asserted in three places, and they say the same thing.

`LICENSE` carries the grant, `pyproject.toml` carries the SPDX expression every
installer and package index reads, and `README.md` tells a human which licence
they are getting. Nothing derives any of them from the others, so all three can
drift apart and each one on its own still reads as authoritative -- which is the
shape this repository tests rather than trusts everywhere else it holds one fact
in two files (`store/schema.py` against the frozen DDL, the documented tool
surface against the built one).

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
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
LICENSE = REPO_ROOT / "LICENSE"
PYPROJECT = REPO_ROOT / "pyproject.toml"
README = REPO_ROOT / "README.md"

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


def test_the_readme_names_the_same_licence() -> None:
    """A human reading the README is told what the metadata says.

    Matched on the SPDX token near a link to the licence file rather than on a
    fixed sentence: the surrounding prose is free to be rewritten, and a case
    that pinned the wording would redden on an edit that changed nothing about
    the licence.
    """
    spdx = _declared_spdx()
    readme = README.read_text()
    section = re.search(r"^##+\s*Licen[cs]e\s*$(.*?)(?=^##\s|\Z)", readme, re.M | re.S)
    assert section is not None, (
        f"{README} has no `## Licence` (or `## License`) section. The one place a human "
        "is told which licence they are getting is missing."
    )
    text = section.group(1)
    assert spdx in text, (
        f"the README's licence section does not name `{spdx}`, which is what "
        f"pyproject.toml declares. Section text: {text.strip()[:200]!r}"
    )
    assert "LICENSE" in text, (
        "the README's licence section does not link to LICENSE, so a reader is told the "
        "name of the licence with no path to its terms."
    )
