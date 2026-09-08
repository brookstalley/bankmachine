"""`requires-python` is a promise to anyone who installs this, so something must keep it.

The floor is not a preference; it is a claim that the package runs on every
version at or above it. Nothing here runs on more than one interpreter, so the
only floor this repo can honestly declare is the one it actually tests on.

🔴 **This test is written to be replaced, not kept forever.** When a CI matrix
lands (build step 8) and genuinely exercises a range, the honest check becomes
"the matrix covers the floor" and this file should become that. Until then the
floor and the pinned interpreter are the same fact written in two places, and
two copies of one fact drift unless something compares them.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]


def _declared_floor() -> str:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requires = pyproject["project"]["requires-python"]
    assert requires.startswith(">="), (
        f"requires-python is {requires!r}; this test reads a `>=` floor and the form has changed"
    )
    return requires.removeprefix(">=").strip()


def test_the_declared_floor_is_the_interpreter_that_is_actually_run() -> None:
    pinned = (REPO_ROOT / ".python-version").read_text(encoding="utf-8").strip()

    assert _declared_floor() == pinned, (
        f"pyproject declares requires-python >={_declared_floor()} but .python-version pins "
        f"{pinned}. Nothing in this repo runs on anything but the pinned version, so a lower "
        f"floor is an untested promise. Raise the floor to match, or land the CI matrix that "
        f"makes the wider claim true and rewrite this test against it."
    )
