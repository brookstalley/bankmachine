"""`requires-python` is a promise to anyone who installs this, so something must keep it.

The floor is not a preference; it is a claim that the package runs on every
version at or above it. Nothing here runs on more than one interpreter, so the
only floor this repo can honestly declare is the one it actually tests on.

🔴 **This test is written to be replaced, not kept forever.** When a CI matrix
genuinely exercises a range, the honest check becomes "the matrix covers the
floor" and this file should become that. Until then the floor and the pinned
interpreter are the same fact written in two places, and two copies of one fact
drift unless something compares them.

🔴 **CI existing is not the trigger; a matrix is.** `.github/workflows/check.yml`
runs the gate on every pull request, and it installs the ONE interpreter
`.python-version` pins -- so it exercises the floor rather than a range, and
nothing about it licenses widening `requires-python`. A `.github/` directory is
not the condition above being met.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]

#: The oldest interpreter the SOURCE can run on, which is a different and harder
#: constraint than the one below it. `call_with_retry[T]` in
#: `connector/plaid/errors.py` uses PEP 695 type parameters, which do not parse
#: before 3.12 — so lowering the floor to 3.11 would produce a package that
#: cannot import on the version it advertises.
#:
#: 🔴 Equality with `.python-version` alone does not catch that: both values
#: could be moved down together and the pair would still agree. This constant is
#: what makes the floor checkable rather than merely self-consistent, and it must
#: be raised by hand whenever newer syntax is adopted.
SYNTAX_FLOOR = (3, 12)


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def _declared_floor() -> str:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requires = pyproject["project"]["requires-python"]
    # 🔴 `tomllib.loads` returns `dict[str, Any]`, so every value read out of it
    # is `Any` and flows onward unchallenged -- `warn_return_any` is what makes
    # narrowing it mandatory rather than optional here, and it is the same rule
    # that keeps `_dispatch_tool`'s arguments typed `object` at the MCP
    # boundary. Asserting the type is what turns a malformed pyproject into a
    # named failure instead of an AttributeError three lines later.
    assert isinstance(requires, str), (
        f"requires-python is {requires!r}, not a string; this test reads a `>=` floor"
    )
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


def test_the_floor_is_not_below_what_the_source_syntax_needs() -> None:
    """The half a CI matrix would NOT make safe.

    A matrix over 3.11-3.14 would exercise the floor honestly and this repo
    could widen `requires-python` again — but the source would still fail to
    parse on 3.11, and the matrix would report that as a red run rather than as
    the packaging mistake it is. Naming the syntax floor separately says which
    of the two has to move first.
    """
    floor = _version(_declared_floor())

    assert floor >= SYNTAX_FLOOR, (
        f"requires-python declares {_declared_floor()}, but this source needs at least "
        f"{'.'.join(str(p) for p in SYNTAX_FLOOR)} to parse. Widening the floor means removing "
        f"the newer syntax first, not just changing the number."
    )
