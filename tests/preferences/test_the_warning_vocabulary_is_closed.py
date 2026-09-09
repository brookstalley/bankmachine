"""Every caveat this product can emit spells a kind the vocabulary declares.

🔴 A published `outputSchema` closes the `kind` enum to `query.WARNING_KINDS`,
and a validating client rejects a payload whose enum does not match. So a
construction site that invents a kind does not produce an unrecognized warning
-- it produces a REJECTED ANSWER, and the warning takes the whole response down
with it. `api-contract.md` § Direction puts incompleteness on the success path
precisely so it cannot reach the error channel; an invented kind routes it there
through the validator.

`Caveat.kind` is a bare `str`, so nothing in the type system prevents this. The
scan is what stands in for the constraint the dataclass cannot express, in the
same spirit as the read-only file handle: the refusal lives somewhere a future
author cannot forget it rather than in a rule they have to remember.

The contract also says a consumer must tolerate a kind it does not recognize.
That is not in tension with the closed enum, and the reason is worth stating
because it is the thing that would change: schema and payload leave one process
in one session, so no client can hold a copy older than the answer it is
validating. Should the schema ever be published separately from the answers it
describes -- cached, vendored, written to a file -- the enum must open, and this
test's premise is what breaks first.
"""

from __future__ import annotations

import ast
from pathlib import Path

from bankmachine import query

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "bankmachine"


def _constructed_kinds() -> list[tuple[Path, int, str | None]]:
    """Every `Caveat(...)` in the product, with the kind it names.

    A `None` kind is a site whose kind is not a literal -- computed, passed in,
    or read from somewhere. Those are reported separately, because the scan can
    prove nothing about them and silently skipping one would make this test
    weaker exactly where the risk is highest.
    """
    found: list[tuple[Path, int, str | None]] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None)
            if name != "Caveat":
                continue
            kind: ast.expr | None = node.args[0] if node.args else None
            for keyword in node.keywords:
                if keyword.arg == "kind":
                    kind = keyword.value
            if isinstance(kind, ast.Constant) and isinstance(kind.value, str):
                found.append((path, node.lineno, kind.value))
            else:
                found.append((path, node.lineno, None))
    return found


def test_every_caveat_names_a_kind_the_vocabulary_declares() -> None:
    vocabulary = set(query.WARNING_KINDS)
    strays = [
        f"{path.relative_to(SOURCE_ROOT.parents[1])}:{line} says {kind!r}"
        for path, line, kind in _constructed_kinds()
        if kind is not None and kind not in vocabulary
    ]
    assert not strays, (
        "these caveats name a kind outside the vocabulary, so a client validating "
        "against the published outputSchema would reject the whole answer rather "
        "than merely fail to recognize the warning:\n  " + "\n  ".join(strays)
    )


def test_no_caveat_builds_its_kind_somewhere_this_scan_cannot_read() -> None:
    """A computed kind defeats the scan above, so it has to be refused outright.

    Not a style rule. The scan is the only thing standing between an invented
    kind and a rejected answer, and a site that computes its kind is one the
    scan reports as fine while proving nothing about it.
    """
    opaque = [
        f"{path.relative_to(SOURCE_ROOT.parents[1])}:{line}"
        for path, line, kind in _constructed_kinds()
        if kind is None
    ]
    assert not opaque, (
        "these caveats do not name their kind as a literal, so nothing can check "
        "it against the vocabulary before a client does:\n  " + "\n  ".join(opaque)
    )


def test_the_scan_reaches_the_sites_it_claims_to_check() -> None:
    """A scan over zero call sites passes forever, so prove it found them.

    The positive control below proves the assertion can fail; this proves it is
    aimed at the real source tree rather than at nothing.
    """
    found = _constructed_kinds()
    assert len(found) > 5, f"expected the product's caveat sites, found {len(found)}"
    assert {kind for _, _, kind in found} >= {"stale", "degraded", "gapped"}, (
        "the scan did not reach the pipeline warnings, so it is not reading "
        "the module that emits them"
    )


def test_the_scan_catches_a_kind_the_vocabulary_does_not_declare() -> None:
    """The positive control: a checker that matches nothing passes forever."""
    tree = ast.parse('Caveat("rate_limited", "the aggregator asked us to slow down")\n')
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    invented = call.args[0]
    assert isinstance(invented, ast.Constant)
    assert invented.value not in set(query.WARNING_KINDS), (
        "this control has to name a kind the vocabulary does NOT declare; if the "
        "vocabulary grew to include it, pick another"
    )
