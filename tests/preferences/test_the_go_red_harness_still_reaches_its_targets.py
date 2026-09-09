"""Every norm break the harness knows how to make can still be made.

🔴 **A go-red case whose anchor has drifted proves nothing, and looks like it
passed.** `verify_norms_go_red.py` mutates a named string in a source file and
requires a named test to fail. When the string is no longer there -- a rename, a
reflow, a refactor that moved the line -- the harness prints `SKIP` and lists the
case as a survivor. That is honest reporting, and it is reported in the one place
nobody looks between releases: the harness takes minutes to run because it
spawns a pytest per case, so in practice it runs at a work-cycle boundary and
the drift sits undetected until then.

This is the same failure the harness itself exists to catch, one level up. A
check that has stopped reaching its subject passes forever, and the more cases
the list holds the more likely one of them is quietly aimed at nothing.

**Measured, not hypothetical.** This file exists because it happened: an anchor
written against `query.py` on 2026-09-09 was reflowed by `ruff format` in the
very commit that added it, and the case went from RED to SKIP without anyone
touching the norm it guards. The harness reported it correctly, ten minutes
later. This reports it in under a second, on every run.

🔴 **This does NOT replace running the harness.** It asserts the mutation can
still be APPLIED; only the harness asserts the guard still goes RED when it is.
The two answer different questions and the cheap one cannot stand in for the
expensive one.
"""

from __future__ import annotations

import ast
import importlib.util
from collections.abc import Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = REPO_ROOT / "tests" / "preferences" / "verify_norms_go_red.py"


def _harness() -> Any:
    """The harness imported as a module, so `CASES` is read rather than parsed.

    Importing is safe: everything at module scope is constant definitions, and
    the mutation loop lives behind `if __name__ == "__main__"`.
    """
    spec = importlib.util.spec_from_file_location("verify_norms_go_red", HARNESS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


Case = tuple[str, Any, str, str, str]


def stale_anchors(cases: Sequence[Case], root: Path) -> list[str]:
    """Cases whose anchor is no longer in the file, so the harness would SKIP them."""
    return [
        f"{name}\n      {path}: {old!r}"
        for name, path, old, _new, _test in cases
        if old not in (root / path).read_text(encoding="utf-8")
    ]


def inert_or_unparsable(cases: Sequence[Case], root: Path) -> tuple[list[str], list[str]]:
    """Cases whose mutation changes nothing, or does not parse.

    A drifted anchor is skipped here rather than double-reported: `stale_anchors`
    owns that failure.
    """
    inert: list[str] = []
    unparsable: list[str] = []
    for name, path, old, new, _test in cases:
        source = (root / path).read_text(encoding="utf-8")
        if old not in source:
            continue
        mutated = source.replace(old, new, 1)
        if mutated == source:
            inert.append(name)
        elif Path(path).suffix == ".py":
            try:
                ast.parse(mutated)
            except SyntaxError as exc:
                unparsable.append(f"{name} ({exc.msg} at line {exc.lineno})")
    return inert, unparsable


def missing_tests(cases: Sequence[Case], root: Path) -> list[str]:
    """Cases naming a test that no longer exists.

    The quietest of the three failures: pytest exits non-zero on an unresolvable
    node id, which the harness reads as the norm being caught, so unlike a
    drifted anchor it never even prints a SKIP.
    """
    missing: list[str] = []
    for name, _path, _old, _new, node_id in cases:
        file_part, _, test_part = node_id.partition("::")
        target = root / file_part
        if not target.is_file():
            missing.append(f"{name}: no such file {file_part}")
            continue
        if not test_part:
            continue
        wanted = test_part.split("[")[0]
        defined = {
            node.name
            for node in ast.walk(ast.parse(target.read_text(encoding="utf-8")))
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        if wanted not in defined:
            missing.append(f"{name}: {file_part} defines no {wanted}")
    return missing


def test_every_case_can_still_find_the_text_it_breaks() -> None:
    """The anchor is present, so the mutation can be applied at all."""
    cases = _harness().CASES
    assert cases, "the harness declares no cases, so this asserts nothing"

    stale = stale_anchors(cases, REPO_ROOT)

    assert not stale, (
        "these go-red cases can no longer find the code they break, so the harness SKIPS "
        "them and the norms they cover are unproven:\n" + "\n".join(f"  - {e}" for e in stale)
    )


def test_every_mutation_still_changes_the_file_and_still_parses() -> None:
    """A mutation that changes nothing, or breaks the parse, reports a false RED.

    The harness checks both at run time and reports them as `SKIP`/`INVALID`.
    Checking here too is the difference between finding out now and finding out
    during the run you were hoping to trust.

    A mutation that does not parse is the worse of the two: pytest exits non-zero
    on a COLLECTION error exactly as it does on a failure, so the case prints RED
    without ever exercising the guarantee.
    """
    inert, unparsable = inert_or_unparsable(_harness().CASES, REPO_ROOT)

    assert not inert, f"these mutations leave the file unchanged, so nothing is broken: {inert}"
    assert not unparsable, (
        "these mutations do not parse, so pytest would fail to COLLECT and the case would "
        f"report RED without testing anything: {unparsable}"
    )


def test_every_case_names_a_test_that_exists() -> None:
    """A case pointing at a renamed test reports RED for the wrong reason."""
    missing = missing_tests(_harness().CASES, REPO_ROOT)

    assert not missing, (
        "these go-red cases name a test that no longer exists; pytest exits non-zero on an "
        "unresolvable node id, which the harness reads as the norm being caught:\n"
        + "\n".join(f"  - {entry}" for entry in missing)
    )


def test_the_detectors_report_a_case_broken_in_each_of_the_three_ways(tmp_path: Path) -> None:
    """🔴 The positive control, DRIVING the detectors rather than restating them.

    The first version of this control re-implemented the three detections inline
    -- an `in` test, a no-op `replace`, an `ast.parse` of a broken string -- and
    so proved that Python's operators work rather than that the three tests above
    use them correctly. That is this file's own subject one level up, and it is
    what its two sibling guards were careful to avoid: each drives its real
    reader over a known-bad input.

    Extracting the detectors is what makes that possible here. The tests above
    and this control now run the same three functions; nothing is asserted twice
    in two spellings that can drift apart.
    """
    (tmp_path / "subject.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "t.py").write_text("def test_x() -> None:\n    pass\n", encoding="utf-8")
    subject = Path("subject.py")

    drifted: list[Case] = [("drifted", subject, "value = 2", "value = 3", "t.py::test_x")]
    inert_case: list[Case] = [("inert", subject, "value = 1", "value = 1", "t.py::test_x")]
    broken: list[Case] = [("bad", subject, "value = 1", "value = (1", "t.py::test_x")]
    renamed: list[Case] = [("renamed", subject, "value = 1", "value = 2", "t.py::test_gone")]

    assert stale_anchors(drifted, tmp_path), "a drifted anchor was not reported"
    assert not stale_anchors(inert_case, tmp_path), "a present anchor was reported as drifted"

    inert, unparsable = inert_or_unparsable(inert_case, tmp_path)
    assert inert and not unparsable, f"a no-op mutation was not reported: {inert}, {unparsable}"

    inert, unparsable = inert_or_unparsable(broken, tmp_path)
    assert unparsable and not inert, f"an unparsable mutation was not reported: {unparsable}"

    assert missing_tests(renamed, tmp_path), "a case naming a nonexistent test was not reported"
    assert not missing_tests(inert_case, tmp_path), "a case naming a real test was reported"
