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

#: A remote endpoint's path is not a location on this machine, and AC-ARCH.4 is
#: about locations on this machine. `/institutions/get` and `/Users/someone/store.db`
#: are indistinguishable as string literals, so the discriminator is what the
#: literal is declared to BE: an argument to `connector.Endpoint` is the
#: aggregator's vocabulary, and anything else starting with a separator is still
#: a path. This is a relationship rather than a per-file exemption on purpose --
#: an allowlist of modules permitted to hold absolute strings would decay on the
#: first module someone forgot to add.
ENDPOINT_CONSTRUCTOR = "Endpoint"

#: The other thing a leading separator can be declared to be. A resource URI is
#: assembled from a scheme and a path segment, and the segment is indistinguishable
#: from a filesystem path on its own -- the same problem the endpoint relationship
#: above solves, arriving through a different door. The discriminator is again what
#: the literal is declared to be: a piece of an f-string whose earlier text already
#: carries a `://` is continuing a URI, and there is no filesystem path it could be.
#:
#: Kept as a relationship rather than an exemption for the reason stated above --
#: a module allowlist decays on the first module nobody remembered to add.
URI_SCHEME_SEPARATOR = "://"


def _source_files() -> list[Path]:
    return [p for p in SOURCE_ROOT.rglob("*.py") if "__pycache__" not in p.parts]


def _uri_continuations(tree: ast.AST) -> set[int]:
    """`id()` of every string constant continuing a URI inside one f-string.

    🔴 Scoped to a single `JoinedStr`, and to parts that FOLLOW the scheme within
    it. A constant elsewhere in the file is untouched however many URIs the module
    builds, so this cannot become a blanket pardon for a module that happens to
    mention one.
    """
    continuations: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        scheme_seen = False
        for part in node.values:
            if scheme_seen and isinstance(part, ast.Constant) and isinstance(part.value, str):
                continuations.add(id(part))
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                scheme_seen = scheme_seen or URI_SCHEME_SEPARATOR in part.value
            elif isinstance(part, ast.FormattedValue):
                # The scheme can arrive through an interpolation -- which is the
                # shape that keeps the scheme written once -- so a name standing
                # where one belongs is read as one. Only a name whose own text
                # says so: `_SCHEME`, `BASE_URI`. Anything else leaves the parts
                # after it checked, because a bare `{value}` is not a claim.
                inner = part.value
                label = inner.attr if isinstance(inner, ast.Attribute) else getattr(inner, "id", "")
                scheme_seen = scheme_seen or label.upper().endswith(("SCHEME", "URI", "URL"))
    return continuations


def _declared_endpoints(tree: ast.AST) -> set[int]:
    """`id()` of every string constant passed directly to `Endpoint(...)`."""
    declared: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != ENDPOINT_CONSTRUCTOR:
            continue
        for argument in [*node.args, *(kw.value for kw in node.keywords)]:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                declared.add(id(argument))
    return declared


def test_no_absolute_path_is_baked_into_the_source() -> None:
    offenders: list[str] = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        endpoints = _declared_endpoints(tree)
        uri_parts = _uri_continuations(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and ABSOLUTE_PATH.match(node.value)
                and id(node) not in endpoints
                and id(node) not in uri_parts
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


def test_the_uri_relationship_reads_a_scheme_but_not_a_bare_interpolation() -> None:
    """The positive control for the URI half, both directions.

    A relationship that pardons everything is an exemption wearing a better name,
    so this pins the pardon to parts that actually follow a scheme -- and pins
    that a path built from an ordinary variable is still caught.
    """
    pardoned = ast.parse('X = f"{_SCHEME}/warnings"\nY = f"bankmachine://ref{n}/envelope"\n')
    pardoned_values = {
        node.value
        for node in ast.walk(pardoned)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) in _uri_continuations(pardoned)
    }
    assert pardoned_values == {"/warnings", "/envelope"}

    # A leading segment, before any scheme, is not continuing anything.
    leading = ast.parse('X = f"/Users/someone/{name}.db"\n')
    assert not [
        node
        for node in ast.walk(leading)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and ABSOLUTE_PATH.match(node.value)
        and id(node) in _uri_continuations(leading)
    ], "a path segment before any scheme was pardoned"

    # A bare interpolation is not a claim to be a scheme.
    bare = ast.parse('X = f"{root}/Users/someone/store.db"\n')
    assert not _uri_continuations(bare), "a bare interpolation was read as a scheme"


def test_the_endpoint_exemption_is_narrow() -> None:
    """The exemption exempts endpoints, and nothing else on the same line.

    Both directions matter. A path handed to `Path(...)` must still be caught
    even in a module that declares endpoints, or the exemption would have
    quietly become a per-file allowlist -- which is the failure this project has
    already been burned by, in the form of a rule that matched on a name instead
    of a relationship.
    """
    module = ast.parse(
        'A = Endpoint("/institutions/get")\n'
        'B = Path("/Users/someone/store.db")\n'
        'C = Endpoint(path="/accounts/get")\n'
    )
    declared = _declared_endpoints(module)
    exempt = {
        node.value
        for node in ast.walk(module)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) in declared
    }
    assert exempt == {"/institutions/get", "/accounts/get"}
    assert "/Users/someone/store.db" not in exempt
