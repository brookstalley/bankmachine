"""The gate runs every check `project-preferences.md` declares, and names the red one.

`project-preferences.md` § Dev commands declares three: `uv run pytest`, `uv run ruff
check` and `uv run mypy`. For most of this project's life the gate ran only the first,
so the other two were a claim rather than a check -- and mypy drifted from clean to 12
errors across 15 commits with nothing to notice (brookstalley/bankmachine#92).

These cases drive the REAL `scripts/check.sh` with a stub `uv` on `PATH`, the same
shape as the leak guard's tests: a reimplementation in Python would test a copy, and a
case that shelled out to the true `uv` would take six minutes to assert one line of
reporting. The stub records each invocation and fails whichever tool the case names,
which is what lets the failure-reporting contract be tested at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
GATE = REPO_ROOT / "scripts" / "check.sh"

#: The tools the gate must run, in the order it must run them. The order is asserted,
#: not incidental: pytest is first so its JUnit report exists even when a linter goes
#: red, and the case below that fails a linter and checks for the report is what holds
#: that reasoning to the code.
EXPECTED_ORDER = ["pytest", "ruff", "mypy"]

_STUB_UV = """#!/usr/bin/env bash
# Stands in for `uv`. Records the invocation, honours --junit-xml so the real
# script's report-ordering can be observed, and fails only the named tool.
echo "$*" >> "$STUB_LOG"
for arg in "$@"; do
    case "$arg" in
        --junit-xml=*) printf '<testsuite/>' > "${arg#--junit-xml=}" ;;
    esac
done
if [ "$2" = "$STUB_FAIL" ]; then
    echo "stub: pretending $2 failed" >&2
    exit 1
fi
exit 0
"""


GateRun = tuple[subprocess.CompletedProcess[str], Path, Path]


def _run_gate(tmp_path: Path, fail: str = "") -> GateRun:
    """Run the real gate with a stub `uv`, failing `fail` (empty = everything passes)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "uv"
    stub.write_text(_STUB_UV)
    stub.chmod(0o755)

    log = tmp_path / "invocations.log"
    log.touch()
    junit = tmp_path / "report.xml"

    result = subprocess.run(
        ["bash", str(GATE), str(junit)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "STUB_LOG": str(log),
            "STUB_FAIL": fail,
            "HOME": str(tmp_path),
        },
    )
    return result, log, junit


def test_the_gate_is_present_and_executable() -> None:
    """A gate that cannot run is the failure mode this norm's history is made of."""
    assert GATE.exists(), f"the gate script is missing from {GATE}"
    assert GATE.stat().st_mode & 0o111, "the gate script is not executable"


def test_it_runs_every_declared_check_in_order(tmp_path: Path) -> None:
    invoked = []
    result, log, _ = _run_gate(tmp_path)
    for line in log.read_text().splitlines():
        # `run pytest --junit-xml=... -q` -> "pytest"
        invoked.append(line.split()[1])

    assert result.returncode == 0, f"a wholly green run must pass: {result.stderr}"
    assert invoked == EXPECTED_ORDER, (
        f"the gate ran {invoked}; project-preferences.md declares {EXPECTED_ORDER}"
    )


@pytest.mark.parametrize("tool", EXPECTED_ORDER)
def test_a_red_check_fails_the_gate_and_is_named(tmp_path: Path, tool: str) -> None:
    """The contract that distinguishes a deliberate red baseline from a regression.

    A gate that exits 1 without saying which command was unhappy sends the reader to
    re-run three commands by hand -- which is the state this gate exists to end.
    """
    result, _, _ = _run_gate(tmp_path, fail=tool)

    assert result.returncode == 1, f"a red {tool} must fail the gate"
    assert tool in result.stderr, f"the gate did not name {tool} as the failing command"


def test_every_check_runs_even_after_an_earlier_one_fails(tmp_path: Path) -> None:
    """No `set -e`: one invocation reports everything red, not just the first thing."""
    _, log, _ = _run_gate(tmp_path, fail="pytest")
    invoked = [line.split()[1] for line in log.read_text().splitlines()]

    assert invoked == EXPECTED_ORDER, (
        f"a red pytest stopped the gate at {invoked}; the linters must still run"
    )


def test_the_report_is_written_even_when_a_linter_fails(tmp_path: Path) -> None:
    """Why pytest is ordered first.

    The evidence record is parsed from the JUnit report. If a linter ran before pytest
    and failed the script, that report would never be written and the record would fail
    as "unparseable" -- an error naming no tool at all.
    """
    _, _, junit = _run_gate(tmp_path, fail="mypy")

    assert junit.exists(), "a red linter left the JUnit report unwritten"


def test_it_refuses_to_run_without_a_report_path(tmp_path: Path) -> None:
    """`{junit_xml}` is mandatory in this key's contract; a silent default would hide
    a mis-declared `test_command:` until someone went looking for the evidence."""
    result = subprocess.run(
        ["bash", str(GATE)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 2, "a missing report path must be a usage error"
    assert "usage" in result.stderr.lower()


def test_the_declared_gate_command_is_this_script() -> None:
    """The wiring follows the script.

    A gate nothing launches is exactly the defect #92 was filed about, one level up.
    """
    state = (REPO_ROOT / ".prawduct" / "project-state.yaml").read_text()
    declared = [line for line in state.splitlines() if line.startswith("test_command:")]

    assert len(declared) == 1, f"expected one live test_command:, found {declared}"
    assert "scripts/check.sh" in declared[0], (
        f"test_command: no longer launches the gate: {declared[0]}"
    )
    assert "{junit_xml}" in declared[0], "test_command: must pass the report path through"
