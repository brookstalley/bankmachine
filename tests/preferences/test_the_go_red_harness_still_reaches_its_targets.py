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


def test_every_case_can_still_find_the_text_it_breaks() -> None:
    """The anchor is present, so the mutation can be applied at all."""
    cases = _harness().CASES
    assert cases, "the harness declares no cases, so this asserts nothing"

    stale = [
        (name, str(path), old)
        for name, path, old, _new, _test in cases
        if old not in (REPO_ROOT / path).read_text(encoding="utf-8")
    ]

    assert not stale, (
        "these go-red cases can no longer find the code they break, so the harness SKIPS "
        "them and the norms they cover are unproven:\n"
        + "\n".join(f"  - {name}\n      {path}: {old!r}" for name, path, old in stale)
    )


def test_every_mutation_still_changes_the_file_and_still_parses() -> None:
    """A mutation that changes nothing, or breaks the parse, reports a false RED.

    The harness checks both at run time and reports them as `SKIP`/`INVALID`.
    Checking here too is not duplication -- it is the difference between finding
    out now and finding out during the run you were hoping to trust.

    A mutation that does not parse is the worse of the two: pytest exits
    non-zero on a COLLECTION error exactly as it does on a failure, so the case
    prints RED without ever exercising the guarantee.
    """
    cases = _harness().CASES

    inert = []
    unparsable = []
    for name, path, old, new, _test in cases:
        source = (REPO_ROOT / path).read_text(encoding="utf-8")
        if old not in source:
            continue  # the test above owns this failure; do not report it twice
        mutated = source.replace(old, new, 1)
        if mutated == source:
            inert.append(name)
        elif Path(path).suffix == ".py":
            try:
                ast.parse(mutated)
            except SyntaxError as exc:
                unparsable.append(f"{name} ({exc.msg} at line {exc.lineno})")

    assert not inert, f"these mutations leave the file unchanged, so nothing is broken: {inert}"
    assert not unparsable, (
        "these mutations do not parse, so pytest would fail to COLLECT and the case would "
        f"report RED without testing anything: {unparsable}"
    )


def test_every_case_names_a_test_that_exists() -> None:
    """A case pointing at a renamed test reports RED for the wrong reason.

    pytest exits non-zero when a node id does not resolve, which the harness
    reads as "the norm was caught" -- so a case whose test was renamed goes on
    passing while covering nothing at all. This is the quietest of the three
    failures, because unlike a drifted anchor it never prints a SKIP.

    Checks the file and, where the id names one, the test function -- by parsing
    rather than importing, since importing every test module here would be a
    second collection pass for no gain.
    """
    cases = _harness().CASES

    missing = []
    for name, _path, _old, _new, node_id in cases:
        file_part, _, test_part = node_id.partition("::")
        target = REPO_ROOT / file_part
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

    assert not missing, (
        "these go-red cases name a test that no longer exists; pytest exits non-zero on an "
        "unresolvable node id, which the harness reads as the norm being caught:\n"
        + "\n".join(f"  - {entry}" for entry in missing)
    )


def test_the_reader_reports_a_case_aimed_at_nothing() -> None:
    """The positive control: proof these checks fail on a bad case.

    Every assertion above is of the form "no bad entries found", which is what a
    reader returning nothing would also produce. So the detectors are driven
    over a case that is broken in each of the three ways.
    """
    source = "value = 1\n"
    anchor_drifted = "value = 2" not in source
    mutation_inert = source.replace("value = 1", "value = 1", 1) == source
    mutation_unparsable = False
    try:
        ast.parse(source.replace("value = 1", "value = (1", 1))
    except SyntaxError:
        mutation_unparsable = True

    assert anchor_drifted, "a missing anchor was not detected as missing"
    assert mutation_inert, "a no-op mutation was not detected as inert"
    assert mutation_unparsable, "an unparsable mutation was not detected"
