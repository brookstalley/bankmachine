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
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
GATE = REPO_ROOT / "scripts" / "check.sh"
PREFERENCES = REPO_ROOT / ".prawduct" / "artifacts" / "project-preferences.md"

#: The distinct tools the gate must run. `ruff` appears once here and twice in
#: EXPECTED_ORDER: `ruff check` and `ruff format --check` are different halves, and
#: the lint rules never reach layout -- learnings.md records seven files drifting
#: behind a clean `ruff check`.
DECLARED_TOOLS = ["pytest", "ruff", "mypy"]

#: The order of invocations. Asserted, not incidental: pytest is first so its JUnit
#: report exists even when a linter goes red, which is what lets a red linter be
#: written into that report rather than left in an exit code nothing stores.
EXPECTED_ORDER = ["pytest", "ruff", "ruff", "mypy"]

_STUB_UV = """#!/usr/bin/env bash
# Stands in for `uv`. Records the invocation, honours --junit-xml so the real
# script's report-ordering can be observed, and fails only the named tool.
echo "$*" >> "$STUB_LOG"
for arg in "$@"; do
    case "$arg" in
        --junit-xml=*)
            printf '%s' \
                '<?xml version="1.0" encoding="utf-8"?><testsuites>' \
                '<testsuite name="pytest" errors="0" failures="0" skipped="0" tests="3">' \
                '<testcase classname="t" name="a"/></testsuite></testsuites>' \
                > "${arg#--junit-xml=}" ;;
    esac
done
for bad in $STUB_FAIL; do
    if [ "$2" = "$bad" ]; then
        echo "stub: pretending the requested tool failed" >&2
        exit 1
    fi
done
exit 0
"""


GateRun = tuple[subprocess.CompletedProcess[str], Path, Path]


def _run_gate(tmp_path: Path, *fail: str) -> GateRun:
    """Run the real gate with a stub `uv`, failing each tool in `fail`."""
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
            "STUB_FAIL": " ".join(fail),
            "HOME": str(tmp_path),
        },
    )
    return result, log, junit


def _suite(junit: Path) -> ET.Element:
    """The `<testsuite>` element, wherever the writer put it."""
    root = ET.parse(junit).getroot()
    suite = root.find("testsuite") if root.tag == "testsuites" else root
    assert suite is not None, f"no testsuite element in {junit}"
    return suite


def test_the_expected_checks_are_the_ones_the_preferences_declare() -> None:
    """Grounds this module's copy of the list.

    `EXPECTED_ORDER` is a third statement of the same set, after
    `project-preferences.md` and `scripts/check.sh`. Without this, the cases below
    could agree with the script forever while both drifted away from the norm they
    claim to enforce, and the failure messages would still cite the preferences.
    """
    declared = PREFERENCES.read_text()

    for tool in DECLARED_TOOLS:
        assert f"uv run {tool}" in declared, (
            f"project-preferences.md no longer declares `uv run {tool}`; the gate and "
            f"this test are enforcing a set the norm does not state"
        )


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


@pytest.mark.parametrize("tool", DECLARED_TOOLS)
def test_a_red_check_fails_the_gate_and_is_named(tmp_path: Path, tool: str) -> None:
    """The contract that distinguishes a deliberate red baseline from a regression.

    A gate that exits 1 without saying which command was unhappy sends the reader to
    re-run three commands by hand -- which is the state this gate exists to end.
    """
    result, _, _ = _run_gate(tmp_path, tool)

    assert result.returncode == 1, f"a red {tool} must fail the gate"
    # The stub is deliberately mute about WHICH tool it failed, so the name can
    # only have reached stderr from the gate. Asserted at that level rather than
    # against one of the gate's two naming sites, so rewording either is free and
    # dropping both is not.
    named = [line for line in result.stderr.splitlines() if f"uv run {tool}" in line]
    assert named, f"the gate did not name {tool} as the failing command:\n{result.stderr}"


def test_every_check_runs_even_after_an_earlier_one_fails(tmp_path: Path) -> None:
    """No `set -e`: one invocation reports everything red, not just the first thing."""
    _, log, _ = _run_gate(tmp_path, "pytest")
    invoked = [line.split()[1] for line in log.read_text().splitlines()]

    assert invoked == EXPECTED_ORDER, (
        f"a red pytest stopped the gate at {invoked}; the linters must still run"
    )


def test_a_red_linter_is_recorded_in_the_report_not_just_the_exit_code(
    tmp_path: Path,
) -> None:
    """The durable half, and the reason this gate is more than an exit status.

    `test-evidence record` writes `.test-evidence.json` from the JUnit report and
    only afterwards consults the command's exit status, which it does not store. A
    gate that merely exits non-zero on a red linter therefore leaves a session-fresh
    record reading `failed: 0` -- the terminal red, the evidence green, and the Stop
    gate satisfied. That is the defect #92 is about, one consumer along.
    """
    _, _, junit = _run_gate(tmp_path, "mypy")

    assert junit.exists(), "a red linter left the JUnit report unwritten"
    failed_cases = [c.get("name") for c in _suite(junit).iter("testcase") if list(c)]

    assert failed_cases == ["uv run mypy"], (
        f"the report does not record the red linter as a failure: {failed_cases}"
    )
    assert int(_suite(junit).get("failures", "0")) >= 1, (
        "the report's failure count does not include the red linter, so the evidence "
        "record will read failed: 0"
    )


def test_every_red_check_is_reported_not_only_the_last(tmp_path: Path) -> None:
    """Guards the accumulator.

    Collapsing it to a single value keeps every single-failure case green, so only a
    run with two red tools can tell "reports everything red" from "reports one".
    """
    result, _, junit = _run_gate(tmp_path, "ruff", "mypy")

    assert "uv run ruff check" in result.stderr
    assert "uv run ruff format --check" in result.stderr
    assert "uv run mypy" in result.stderr
    expected = sum(1 for tool in EXPECTED_ORDER if tool in {"ruff", "mypy"})
    assert int(_suite(junit).get("failures", "0")) == expected, (
        "every red invocation must reach the report, or the evidence undercounts"
    )


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


def test_a_red_pytest_is_not_counted_twice(tmp_path: Path) -> None:
    """pytest's report already carries its own failures.

    Appending a gate case for it as well would overstate the persisted count, which
    matters because the record is what every later reader believes.
    """
    _, _, junit = _run_gate(tmp_path, "pytest")
    gate_cases = [
        case.get("name")
        for case in _suite(junit).iter("testcase")
        if case.get("classname") == "gate"
    ]

    assert "uv run pytest" not in gate_cases, (
        f"pytest was appended on top of its own report: {gate_cases}"
    )


def test_a_pytest_that_wrote_no_report_is_still_recorded(tmp_path: Path) -> None:
    """The exception to the rule above, and the reason it is conditional.

    When pytest dies before writing anything there is no report to carry its failure,
    so dropping its entry would turn a failed run into a clean record -- the defect
    this whole mechanism exists to prevent.

    pytest is the ONLY red command here, and it writes no report. If its entry were
    dropped unconditionally the synthesised report would claim zero failures, which
    is precisely what this asserts against.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "uv"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$2" = "pytest" ]; then exit 1; fi\n'  # red, and writes no report
        "exit 0\n"
    )
    stub.chmod(0o755)
    junit = tmp_path / "report.xml"

    result = subprocess.run(
        ["bash", str(GATE), str(junit)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)},
    )

    assert result.returncode == 1
    assert junit.exists(), "no report was synthesised, so the run records as nothing at all"
    assert int(_suite(junit).get("failures", "0")) == 1, (
        "the only red command wrote no report of its own, so the gate had to carry it and did not"
    )


def test_a_failure_to_record_is_announced(tmp_path: Path) -> None:
    """The recording step is the half nothing else can see.

    If it fails quietly the exit code still goes red for whoever is watching, while
    the evidence record reads clean -- the same shape as the defect the recording
    exists to close, reached through a missing interpreter instead of a missing line.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "uv"
    stub.write_text(_STUB_UV)
    stub.chmod(0o755)
    log = tmp_path / "invocations.log"
    log.touch()
    junit = tmp_path / "report.xml"

    result = subprocess.run(
        ["/bin/bash", str(GATE), str(junit)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        # No `python3` reachable: /bin has bash, /usr/bin is where python3 lives.
        env={
            "PATH": f"{bin_dir}:/bin",
            "STUB_LOG": str(log),
            "STUB_FAIL": "mypy",
            "HOME": str(tmp_path),
        },
    )

    assert result.returncode == 1
    assert "could not write the red checks" in result.stderr, (
        f"the gate failed to record and said nothing about it:\n{result.stderr}"
    )
