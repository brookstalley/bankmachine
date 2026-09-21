"""The project version is carried in `pyproject.toml` and derived everywhere else.

`pyproject.toml` is what the package metadata is built from, and
`build_id.build_identity()` reads that metadata at runtime. A module that spells
the version out instead is a second carrier of one fact: it goes stale at the
next release, and nothing reads it loudly enough for anyone to notice. The
package used to export exactly such a literal as `__version__`, which nothing in
`src/` or `tests/` ever read and which the MCP handshake had already stopped
consulting in favour of the metadata.

🔴 This case scans CODE, not prose. A changelog, a release plan or a README is
free -- and expected -- to name a version, because those records describe a
release that happened rather than claiming what is running now. The drift this
guards is a running program reporting a version its own package metadata
contradicts.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _declared_version() -> str:
    with PYPROJECT.open("rb") as handle:
        data = tomllib.load(handle)
    declared = data["project"].get("version")
    assert isinstance(declared, str), (
        "pyproject.toml's `project.version` is not a string, so there is no single "
        f"declared version for anything to be derived from; found {declared!r}."
    )
    return declared


def _package_modules() -> list[Path]:
    """Every tracked Python module under `src/`, FOUND not listed.

    Derived from `git ls-files` for the same reason the licence case is: a
    guarantee defined by an enumeration stops covering the file added tomorrow,
    and the enumeration is exactly what nobody updates.
    """
    # `-z`: a path containing a space stays one path rather than splitting into
    # two that both fail to open and are silently skipped.
    listed = subprocess.run(
        ["git", "ls-files", "-z", "src/*.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    return [REPO_ROOT / name for name in listed if name]


def test_no_module_spells_out_the_declared_version() -> None:
    """`pyproject.toml` is the only place in the shipped code stating the version."""
    declared = _declared_version()
    modules = _package_modules()
    # 🔴 A scan that finds nothing passes forever. This package has modules; zero
    # means the search broke, not that the code is clean.
    assert modules, (
        "`git ls-files src/*.py` matched no modules, so this case examined NOTHING "
        "and its green says nothing about where the version is carried."
    )
    # The quoted literal, so a version appearing inside a longer string (a URL, a
    # dependency specifier) is not mistaken for a module declaring its own.
    literal = re.compile(rf"""['"]{re.escape(declared)}['"]""")
    offenders: list[str] = []
    for path in modules:
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if literal.search(line):
                rel = path.relative_to(REPO_ROOT)
                offenders.append(f"{rel}:{number}: {line.strip()}")
    assert not offenders, (
        f"pyproject.toml declares version {declared!r}, and these modules spell it out "
        "as well, so the next release silently leaves them behind:\n  "
        + "\n  ".join(offenders)
        + "\n\nRead it from the package metadata instead -- `build_id.build_identity()` "
        "already does, and reports `unknown` rather than a stale guess when the tree "
        "was never installed."
    )
