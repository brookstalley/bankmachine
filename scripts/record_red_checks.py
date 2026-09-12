"""Write each red gate check into the JUnit report as a failing case.

WHY THE EXIT CODE IS NOT ENOUGH

`prawduct-hook test-evidence record` builds `.test-evidence.json` from the JUnit
report and only afterwards consults the command's exit status, which it does not
store. A gate that merely exits non-zero therefore leaves a session-fresh record
reading `failed: 0` -- terminal red, evidence green, Stop gate satisfied. The
evidence has to say what the terminal says, so every red check is appended here as
a failing case.

WHY IT IS A FILE AND NOT A `python3 -c` STRING

It used to be the latter, which put the one component whose failure mode is
*silent under-recording* outside the reach of `ruff check`, `ruff format` and
`mypy` -- the very checks the script that calls it exists to run. `[tool.mypy]
files` includes `scripts` so that this module is type-checked like everything else.

WHY PLAIN `python3`, NOT `uv run python`

A red `uv` must not take the one step that records the redness with it. That is
also why nothing outside the standard library is imported here.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET

#: The gate's name for pytest, which is the one check that reports its own
#: failures into this file and so may already be represented in it.
PYTEST = "uv run pytest"


def report_already_carries_a_failure(suite: ET.Element) -> bool:
    """Whether the report itself records something red.

    🔴 The question is *not* "did pytest run" -- it is "did pytest's redness reach
    this report". Those come apart, and the gap is silent:

    - **exit 5, no tests collected.** One bad `-k` or `-m` in `addopts` deselects
      everything; pytest writes `tests="0" failures="0"` and exits 5.
    - an interrupted session, or a plugin that fails in `sessionfinish` after a
      green run.

    In each, pytest is red, the report parses, and it holds no failure. Dropping
    pytest's entry because the report merely *parsed* would erase the only record
    of the run -- reinstating, in the pytest lane, the exact terminal-red /
    evidence-green defect this module exists to close.
    """
    return suite.find(".//failure") is not None or suite.find(".//error") is not None


def _load(path: str) -> tuple[ET.ElementTree[ET.Element], ET.Element, bool]:
    """The report at `path`, or a fresh one when there is nothing usable there.

    Returns the tree, its `testsuite`, and whether the file on disk was parsed. A
    pytest that died before writing anything leaves no report; the linters' verdict
    still has to survive that, so a replacement is synthesised rather than lost.
    """
    try:
        tree = ET.parse(path)
        root = tree.getroot()
        suite = root.find("testsuite") if root.tag == "testsuites" else root
        if suite is None:
            raise ET.ParseError("no testsuite element")
    except (OSError, ET.ParseError):
        root = ET.Element("testsuites")
        suite = ET.SubElement(root, "testsuite", name="gate", tests="0", failures="0")
        return ET.ElementTree(root), suite, False
    return tree, suite, True


def names_to_append(names: list[str], suite: ET.Element, parsed: bool) -> list[str]:
    """Which red checks still need a case of their own in this report.

    Only pytest is ever dropped, and only when the report already carries its
    failure -- otherwise appending would double-count a run that is already
    represented. Every other check writes no JUnit at all, so each is always added.
    """
    if parsed and report_already_carries_a_failure(suite):
        return [name for name in names if name != PYTEST]
    return list(names)


def append_failures(suite: ET.Element, names: list[str]) -> None:
    """Add one failing case per name and move the suite's counters to match.

    The counters matter as much as the cases: the evidence record reads them, and a
    case nobody counted is a failure nobody sees.
    """
    for name in names:
        case = ET.SubElement(suite, "testcase", classname="gate", name=name)
        ET.SubElement(
            case, "failure", message=f"{name} exited non-zero"
        ).text = f"{name} reported errors; see the gate output for its own report"

    suite.set("tests", str(int(suite.get("tests", "0") or 0) + len(names)))
    suite.set("failures", str(int(suite.get("failures", "0") or 0) + len(names)))


def main(argv: list[str], stdin_text: str) -> int:
    if len(argv) != 2:
        print(
            f"usage: {argv[0] if argv else 'record_red_checks.py'} <junit-xml-path>",
            file=sys.stderr,
        )
        return 2

    path = argv[1]
    names = [line for line in stdin_text.splitlines() if line]

    tree, suite, parsed = _load(path)
    append_failures(suite, names_to_append(names, suite, parsed))
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv, sys.stdin.read()))
