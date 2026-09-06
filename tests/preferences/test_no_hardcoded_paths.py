"""No filesystem path is hardcoded (AC-ARCH.4).

The datastore path, log directory and config location are configuration values
with documented defaults, and the repo's own location is not assumed by any code
path. A baked-in absolute path is what turns "runs on the author's machine" into
the product's definition of working.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
SOURCE_ROOT = REPO_ROOT / "src"

#: A string that opens with a path separator and continues into a path segment.
#: `"/"` alone, URL schemes and format templates are not absolute paths.
ABSOLUTE_PATH = re.compile(r"^/[A-Za-z0-9_.]")


def _source_files() -> list[Path]:
    return [p for p in SOURCE_ROOT.rglob("*.py") if "__pycache__" not in p.parts]


def test_no_absolute_path_is_baked_into_the_source() -> None:
    offenders: list[str] = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and ABSOLUTE_PATH.match(node.value)
            ):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {node.value!r}")

    assert not offenders, (
        "absolute filesystem paths are baked into the source; paths are configuration "
        "with documented defaults:\n  " + "\n  ".join(offenders)
    )


def test_the_source_does_not_locate_itself_relative_to_the_repository() -> None:
    """`Path(__file__).parents[...]` in shipped code assumes an install layout."""
    offenders: list[str] = []
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if "__file__" in line and "parent" in line:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}")
    assert not offenders, (
        "shipped code walks up from __file__, which assumes the repository layout:\n  "
        + "\n  ".join(offenders)
    )


def test_the_probe_would_catch_an_absolute_path() -> None:
    """The positive control -- a matcher that has never matched is not known to work."""
    assert ABSOLUTE_PATH.match("/Users/someone/store.db")
    assert ABSOLUTE_PATH.match("/etc/bankmachine.toml")
    assert not ABSOLUTE_PATH.match("file:{path}?mode=ro")
    assert not ABSOLUTE_PATH.match("/")
