"""The gate runs every check `project-preferences.md` declares, and names the red one.

`project-preferences.md` § Dev commands declares `uv run pytest`, `uv run ruff check`,
`uv run ruff format --check` and `uv run mypy`. For most of this project's life the gate
ran only the first, so the rest were a claim rather than a check -- and mypy drifted from
clean to 12 errors across 15 commits with nothing to notice
(brookstalley/bankmachine#92). `EXPECTED_ORDER` below is the list; no count is written
out anywhere here, because a count is the copy that rots first.

These cases drive the REAL `scripts/check.sh` with a stub `uv` on `PATH`, the same
shape as the leak guard's tests: a reimplementation in Python would test a copy, and a
case that shelled out to the true `uv` would take six minutes to assert one line of
reporting. The stub records each invocation and fails whichever tool the case names,
which is what lets the failure-reporting contract be tested at all.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
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
#
# 🔴 The report's CONTENT is a parameter, because the gate's correct behaviour
# DIFFERS between two shapes a red pytest can leave behind, and a stub that only
# ever produced one of them could not tell them apart:
#
#   STUB_REPORT_FAILURES=1  pytest failed and said so in its own report. The gate
#                           must NOT append a second case; that would double-count.
#   STUB_REPORT_FAILURES=0  pytest exited non-zero having written a clean report.
#                           Real, not hypothetical: exit 5, "no tests collected",
#                           which one bad -k or -m in addopts produces. The gate
#                           MUST record it, or the only trace of the run is an exit
#                           code that `test-evidence record` does not store.
echo "$*" >> "$STUB_LOG"
failures="${STUB_REPORT_FAILURES:-0}"
if [ "$failures" -gt 0 ]; then
    body='<testcase classname="t" name="a"><failure message="stub">red</failure></testcase>'
else
    body='<testcase classname="t" name="a"/>'
fi
for arg in "$@"; do
    case "$arg" in
        --junit-xml=*)
            printf '%s' \
                '<?xml version="1.0" encoding="utf-8"?><testsuites>' \
                '<testsuite name="pytest" errors="0" skipped="0" tests="3" failures="' \
                "$failures" '">' "$body" '</testsuite></testsuites>' \
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


KEYCHAIN_MARKER = "BANKMACHINE_KEYCHAIN_SWAPPED"


#: 🔴 EVERY exec of the real gate goes through this, and that is the point rather
#: than tidiness. The suite this gate launches contains THIS MODULE, so each of
#: those execs is a nested gate run. Without the re-entry marker a nested run sees
#: the default already pointing at the suite's keychain, reads it as a killed
#: previous run, restores the OUTER run's default, deletes the keychain its xdist
#: workers are mid-`keyring` call against, and removes its state file.
#:
#: `export` in the script is not enough: these sites pass CLOSED env dicts, so a
#: child inherits nothing it is not handed. A helper is the construction that
#: makes "every site is covered" checkable instead of remembered.
def _gate_env(
    tmp_path: Path, bin_dir: Path, *, reentrant: bool = False, **extra: str
) -> dict[str, str]:
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        # Never the repo's own file: a nested run writing it corrupts the run
        # that launched it. See BMTEST_STATE in the gate.
        "BANKMACHINE_KEYCHAIN_STATE": str(tmp_path / "keychain-restore"),
        # No operator is watching a test run, so the interrupt window is dead
        # time. Zeroed here rather than per-case so a case added later cannot
        # quietly reintroduce a 3s sleep.
        "BANKMACHINE_KEYCHAIN_GRACE": "0",
    }
    if not reentrant:
        env[KEYCHAIN_MARKER] = "1"
    env.update(extra)
    return env


def _run_gate(tmp_path: Path, *fail: str, report_failures: int = 0) -> GateRun:
    """Run the real gate with a stub `uv`, failing each tool in `fail`.

    `report_failures` is how many failures the stubbed pytest writes into its own
    JUnit report, which is independent of whether it exits non-zero. See `_STUB_UV`.
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
        ["bash", str(GATE), str(junit)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=_gate_env(
            tmp_path,
            bin_dir,
            STUB_LOG=str(log),
            STUB_FAIL=" ".join(fail),
            STUB_REPORT_FAILURES=str(report_failures),
        ),
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
    re-run every check by hand -- which is the state this gate exists to end.
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
        ["bash", str(GATE)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        # Routed through the builder although this run exits at the usage check
        # before the swap: one rule with no exemptions, because an exemption is
        # what the next site copies.
        env=_gate_env(tmp_path, tmp_path),
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


def test_a_red_pytest_that_reported_its_own_failure_is_not_counted_twice(
    tmp_path: Path,
) -> None:
    """When pytest's report already carries the failure, the gate must not add one.

    Appending a gate case as well would overstate the persisted count, which matters
    because the record is what every later reader believes.

    🔴 The stub writes a real `<failure>` here, and that is the whole point of the
    case. Asserting this against a report with `failures="0"` would assert the
    silent-green outcome instead of the no-double-count one -- the two are
    indistinguishable from the outside, and only one of them is correct.
    """
    _, _, junit = _run_gate(tmp_path, "pytest", report_failures=1)
    gate_cases = [
        case.get("name")
        for case in _suite(junit).iter("testcase")
        if case.get("classname") == "gate"
    ]

    assert "uv run pytest" not in gate_cases, (
        f"pytest was appended on top of its own report: {gate_cases}"
    )
    assert int(_suite(junit).get("failures", "0")) == 1, (
        "the failure count moved, so the run is double-counted in the evidence"
    )


def test_a_red_pytest_that_wrote_a_clean_report_is_still_recorded(tmp_path: Path) -> None:
    """The case that separates "the report parsed" from "the report says it failed".

    🔴 These come apart in the wild and the gap is silent. **pytest exits 5 when it
    collects no tests** -- one bad `-k` or `-m` in `addopts` does it -- and writes a
    perfectly parseable report saying `tests="0" failures="0"`. An interrupted run,
    or a plugin that dies in `sessionfinish` after a green session, does the same.

    In every one of those, pytest is the only red command. If its entry is dropped
    because the report merely PARSED, nothing is appended, the recording step
    SUCCEEDS so no warning fires, and `test-evidence record` -- which builds
    `.test-evidence.json` from this report and never stores the exit status --
    writes `failed: 0`. Terminal red, evidence green, Stop gate satisfied: the exact
    defect this whole mechanism exists to close, surviving in the pytest lane.
    """
    result, _, junit = _run_gate(tmp_path, "pytest", report_failures=0)

    assert result.returncode == 1, "a red pytest must fail the gate"
    gate_cases = [
        case.get("name")
        for case in _suite(junit).iter("testcase")
        if case.get("classname") == "gate"
    ]

    assert "uv run pytest" in gate_cases, (
        f"pytest exited non-zero and its report carried no failure, so the gate was "
        f"the only thing that could record the run -- and did not: {gate_cases}"
    )
    assert int(_suite(junit).get("failures", "0")) >= 1, (
        "the report claims zero failures after a red run, so the evidence record "
        "will read failed: 0 while the terminal shows red"
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
        env=_gate_env(tmp_path, bin_dir),
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
        env=_gate_env(
            tmp_path, bin_dir, PATH=f"{bin_dir}:/bin", STUB_LOG=str(log), STUB_FAIL="mypy"
        ),
    )

    assert result.returncode == 1
    assert "could not write the red checks" in result.stderr, (
        f"the gate failed to record and said nothing about it:\n{result.stderr}"
    )


#: Where parallelism is allowed to be configured, and where it is not.
#: `pyproject.toml` rather than `PREFERENCES`: `addopts` is the key that would
#: carry it, and the key is what this guards.
PYPROJECT = REPO_ROOT / "pyproject.toml"

#: Both spellings pytest accepts. Guarding only `-n` would leave the long form as
#: a silent way back in, and the whole point of this case is that the way back in
#: is silent.
_PARALLEL_FLAGS = ("-n", "--numprocesses")


def _addopts_tokens() -> list[str]:
    """Every token of `addopts`, parsed as TOML rather than scanned as text.

    🔴 Parsed, for the reason this case exists at all. A line scan sees one line
    and is blind to a section: an `addopts` that became a multi-line array, or a
    second `addopts` under another table, would carry `-n` on a line the scan
    never reads -- which is precisely the silent re-entry this case is here to
    block, reproduced inside the block itself.

    Tokenised rather than substring-searched for the neighbouring reason. `-n`
    occurs INSIDE `--no-header`, so `"-n" in value` reddens on a flag that has
    nothing to do with parallelism. A guard that cries wolf is one somebody
    eventually deletes, and it would take the real rule with it.
    """
    with PYPROJECT.open("rb") as handle:
        data = tomllib.load(handle)
    try:
        addopts = data["tool"]["pytest"]["ini_options"]["addopts"]
    except KeyError:
        raise AssertionError(
            f"no `[tool.pytest.ini_options] addopts` in {PYPROJECT} -- this case asserts "
            "what that key may NOT contain, so an absent key means the case is guarding "
            "nothing and must be re-pointed, not deleted."
        ) from None
    values = addopts if isinstance(addopts, list) else [addopts]
    return [token for value in values for token in str(value).split()]


def test_parallelism_is_configured_on_the_gate_and_never_in_addopts() -> None:
    """🔴 The placement of `-n auto` is the whole of brookstalley/bankmachine#44.

    It is asserted here because **the failure it prevents is silent**. `addopts`
    applies to every pytest invocation in the repo, and
    `tests/preferences/verify_norms_go_red.py` shells out one single-test run per
    norm case: under `addopts` each would spin up a worker pool to run one test.
    The harness gets SLOWER, never red -- so the documented reaction is to switch
    it off, and the repo loses its norm-break guard without anything failing.
    `-m sandbox` would likewise fan LIVE aggregator calls across workers.

    The neighbouring cases cannot catch this. They read `line.split()[1]` off the
    stub log, which is the TOOL name, so adding or dropping `-n auto` anywhere
    leaves every one of them green. A rule whose only enforcement is the comment
    beside it decays to a comment; this project has that written down as a
    learning and it applies to its own tooling.
    """
    tokens = _addopts_tokens()
    for flag in _PARALLEL_FLAGS:
        # `startswith`, not equality: `-n4` and `-nauto` are ordinary pytest
        # spellings and equality misses both, which would leave the guard's own
        # subject a silent way back in. It does NOT reintroduce the `--no-header`
        # false positive the docstring names -- that token starts `--`, so it
        # cannot start with `-n`.
        offending = [t for t in tokens if t.startswith(flag)]
        assert not offending, (
            f"`{flag}` appears in pyproject.toml's addopts: {offending!r}\n"
            "addopts follows EVERY pytest invocation, including the per-case single-test "
            "runs verify_norms_go_red.py makes and an explicit `-m sandbox`. Configure "
            "parallelism on the invocation that wants it -- scripts/check.sh -- not here."
        )

    gate = GATE.read_text()
    pytest_line = next(
        (ln for ln in gate.splitlines() if ln.strip().startswith('check "uv run pytest"')),
        None,
    )
    assert pytest_line is not None, (
        f'no `check "uv run pytest"` line in {GATE} -- this case asserts that line carries '
        "the parallel flag, so its absence means the case is guarding nothing."
    )
    gate_tokens = pytest_line.split()
    assert any(t.startswith(f) for t in gate_tokens for f in _PARALLEL_FLAGS), (
        f"the gate's pytest invocation carries no parallel flag: {pytest_line.strip()!r}\n"
        "The suite is meant to run in parallel FROM THE GATE. If parallelism was "
        "deliberately removed, remove this case in the same commit and say why -- do not "
        "leave it asserting a rule the repo no longer holds."
    )


#: A stand-in for `security(1)` that reports the default keychain as the suite's
#: OWN temporary one -- the state a nested gate run actually meets. Every call is
#: logged, so a case can assert what the script tried to do to the keychain
#: WITHOUT the machine having a real swap in progress.
_STUB_SECURITY = """#!/usr/bin/env bash
printf '%s\\n' "$*" >>"$SECURITY_LOG"
case "$1" in
    show-keychain-info)
        # Exit 0 = a leftover keychain exists. `STUB_NO_LEFTOVER=1` says none does.
        [ "${STUB_NO_LEFTOVER:-}" = "1" ] && exit 1
        exit 0
        ;;
    default-keychain)
        # `-s` is a write; bare is a read. Only the read answers.
        # `STUB_DEFAULT_IS_OURS=0` reports an ORDINARY default, which is what a
        # run meets when the previous one restored properly.
        if [ "$2" != "-s" ]; then
            if [ "${STUB_DEFAULT_IS_OURS:-1}" = "0" ]; then
                printf '    "/Users/x/Library/Keychains/login.keychain-db"\\n'
            else
                printf '    "/Users/x/Library/Keychains/bankmachine-test.keychain-db"\\n'
            fi
        fi
        ;;
    list-keychains)
        case "$*" in
            *-s*) ;;
            *) printf '    "/Users/x/Library/Keychains/login.keychain-db"\\n' ;;
        esac
        ;;
esac
exit 0
"""


def _stub_env(tmp_path: Path, *, with_security: bool = False) -> tuple[Path, Path]:
    """The stub `uv` and its invocation log, built the way `_run_gate` builds them.

    Separate from `_run_gate` because these cases vary the ENVIRONMENT rather than
    which tool goes red, and `_run_gate` fixes the env it passes.

    `with_security` adds a stubbed `security(1)` reporting our own keychain as the
    default. 🔴 Without it these cases are VACUOUS: the real `security` cannot
    resolve a default under a fake `HOME`, so the script bails for a reason that
    has nothing to do with the property under test, and the case passes with the
    guard deleted.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "uv"
    stub.write_text(_STUB_UV)
    stub.chmod(0o755)
    if with_security:
        sec = bin_dir / "security"
        sec.write_text(_STUB_SECURITY)
        sec.chmod(0o755)
    log = tmp_path / "invocations.log"
    log.touch()
    return log, bin_dir


# --- the gate's keychain swap ------------------------------------------------
#
# The gate makes an empty keychain the default for the length of a run, because
# the developer's populated login keychain costs ~332ms per `keyring` round trip
# against ~12ms for an empty one -- 542s of suite against 56s. It changes a
# USER-LEVEL setting, so what it puts back matters more than what it takes.
#
# These cases drive the REAL script, the way every other case in this file does
# and the way `project-preferences.md` rules shell behaviour is tested. None of
# them touches the machine's actual keychain: each either stubs `security` on
# PATH, opts out with `BANKMACHINE_NO_KEYCHAIN_SWAP=1`, or puts `security` out of
# reach -- so what the stub records is what the script TRIED to do.


def _gate_source() -> str:
    return GATE.read_text()


def test_a_nested_gate_run_does_not_touch_the_outer_run_s_keychain(tmp_path: Path) -> None:
    """🔴 The suite this gate launches execs this gate. Without the re-entry
    marker each nested run sees the default already pointing at our keychain,
    reads that as a killed previous run, restores the OUTER run's default,
    deletes the keychain nine xdist workers are mid-`keyring` call against, and
    removes the outer state file -- leaving its own SIGKILL path nothing to
    restore from.

    `security` is STUBBED to report our keychain as the default, which is the
    state a nested run meets. Without the stub this case is vacuous: the real
    `security` cannot resolve a default under a fake HOME, so the script would
    bail for an unrelated reason and the case would pass with the guard deleted.
    """
    log, bin_dir = _stub_env(tmp_path, with_security=True)
    security_log = tmp_path / "security.log"
    security_log.touch()
    result = subprocess.run(
        ["bash", str(GATE), str(tmp_path / "r.xml")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=_gate_env(tmp_path, bin_dir, STUB_LOG=str(log), SECURITY_LOG=str(security_log)),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    calls = security_log.read_text().strip()
    assert calls == "", (
        "a nested gate run invoked `security` despite the re-entry marker; it would "
        f"have altered the outer run's keychain. Calls:\n{calls}"
    )
    assert "Repairing" not in result.stderr


def test_without_the_marker_a_run_meeting_our_keychain_does_act(tmp_path: Path) -> None:
    """The control for the case above, and the reason it is not vacuous.

    The same stubbed state WITHOUT the marker must reach the repair path. If this
    ever goes green, the case above is proving nothing -- it would be passing
    because the script never gets that far, not because the guard works.
    """
    log, bin_dir = _stub_env(tmp_path, with_security=True)
    security_log = tmp_path / "security.log"
    security_log.touch()
    result = subprocess.run(
        ["bash", str(GATE), str(tmp_path / "r.xml")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=_gate_env(
            tmp_path, bin_dir, reentrant=True, STUB_LOG=str(log), SECURITY_LOG=str(security_log)
        ),
    )
    assert "Repairing" in result.stderr, (
        "with no re-entry marker and our keychain reported as default, the gate did "
        "NOT take the repair path -- so the guarded case is vacuous:\n" + result.stderr
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_swap_is_skipped_when_opted_out(tmp_path: Path) -> None:
    """`BANKMACHINE_NO_KEYCHAIN_SWAP=1` is the documented escape hatch, so it is
    asserted rather than assumed -- an opt-out nobody checks is an opt-out that
    silently stops working."""
    log, bin_dir = _stub_env(tmp_path)
    result = subprocess.run(
        ["bash", str(GATE), str(tmp_path / "r.xml")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=_gate_env(tmp_path, bin_dir, STUB_LOG=str(log), BANKMACHINE_NO_KEYCHAIN_SWAP="1"),
    )
    assert "Repairing" not in result.stderr
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_gate_still_runs_where_security_is_unavailable(tmp_path: Path) -> None:
    """A non-macOS checkout has no `security`. The gate must run its four checks
    anyway rather than aborting, so the swap is an optimisation and never a
    prerequisite."""
    log, bin_dir = _stub_env(tmp_path)
    result = subprocess.run(
        ["bash", str(GATE), str(tmp_path / "r.xml")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        # No /usr/bin: `security` is unreachable, which is what a Linux box looks
        # like from this script's point of view.
        env=_gate_env(tmp_path, bin_dir, PATH=f"{bin_dir}:/bin", STUB_LOG=str(log)),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    invoked = [line.split()[1] for line in log.read_text().splitlines()]
    assert invoked == EXPECTED_ORDER, (
        f"with no `security` on PATH the gate ran {invoked}, not {EXPECTED_ORDER}"
    )


def test_the_restore_puts_back_the_whole_search_list_not_just_the_default() -> None:
    """🔴 `list-keychains -s` REPLACES the list. Restoring only the default would
    drop every other keychain the user had searchable -- silently, and not
    obviously connected to having run the tests.

    Asserted against the script's text rather than by running it, because the
    alternative is mutating the machine's real search list inside a test. The
    structural claim is what can decay: that restore and repair both put back a
    SAVED list rather than a single path.
    """
    gate = _gate_source()
    assert "keychain_current_list" in gate, (
        "the gate no longer captures the user search list before swapping"
    )
    for func, var in (
        ("restore_keychain", "$keychain_list_saved"),
        ("repair_stale_keychain", "$recorded_list"),
    ):
        body = gate.split(f"{func}() {{", 1)[1].split("\n}", 1)[0]
        assert "list-keychains -d user -s" in body, f"{func} does not restore the search list"
        assert var in body, (
            f"{func} restores a search list that is not the saved one ({var} absent) -- "
            "restoring a single path here is the defect this case exists for"
        )


def test_the_env_builder_redirects_the_state_file(tmp_path: Path) -> None:
    """🔴 The redirect is what stops a nested run corrupting the outer one.

    Stubbing `security` keeps these cases off the real keychain; it does not keep
    them off the STATE FILE, which is a real path in the repo root. An early
    version overwrote the outer run's copy with the stub's fake paths mid-suite,
    undoing the outer swap and leaving its SIGKILL path nothing to restore from —
    which surfaced only as a gate that took eight minutes instead of one.

    Asserted on the builder rather than per-case, now that every exec goes
    through it (the case below enforces that), so one assertion covers sites that
    do not exist yet.
    """
    env = _gate_env(tmp_path, tmp_path / "bin")
    redirect = env.get("BANKMACHINE_KEYCHAIN_STATE")
    assert redirect, (
        "_gate_env does not set BANKMACHINE_KEYCHAIN_STATE, so every nested gate run "
        "writes the repo's own state file and corrupts the run that launched it."
    )
    assert str(tmp_path) in redirect, (
        f"_gate_env points the state file at {redirect!r}, which is not under tmp_path"
    )


def test_the_swap_actually_engages_when_nothing_prevents_it(tmp_path: Path) -> None:
    """🔴 A swap that fails SILENTLY is the failure mode this case exists for.

    Every abort path in `use_test_keychain` is a bare `return 0`, deliberately —
    the swap is an optimisation and must never fail a gate run. The cost is that
    a broken swap is indistinguishable from a correct decline: nothing errors,
    and the only symptom is that the suite takes nine minutes instead of one.

    That is not hypothetical. The password was first generated with
    `tr -dc ... </dev/urandom | head -c 32`; `head` exits at its byte count, `tr`
    takes SIGPIPE, and the pipeline returns 141 under this script's `pipefail`.
    The `|| return 0` fired and the swap stopped happening, with a green gate and
    no message.

    So this asserts the swap REACHES `default-keychain -s`, with `security`
    stubbed so the assertion costs the machine nothing.
    """
    log, bin_dir = _stub_env(tmp_path, with_security=True)
    security_log = tmp_path / "security.log"
    security_log.touch()
    result = subprocess.run(
        ["bash", str(GATE), str(tmp_path / "r.xml")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=_gate_env(
            tmp_path, bin_dir, reentrant=True, STUB_LOG=str(log), SECURITY_LOG=str(security_log)
        ),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    calls = security_log.read_text()
    assert "create-keychain" in calls, (
        "the gate never created its keychain — the swap aborted before it, so the "
        f"suite would run against the real one. security calls:\n{calls}"
    )
    assert "default-keychain -s" in calls, (
        "the gate created a keychain but never made it the default, so nothing is "
        f"faster and the keychain leaks. security calls:\n{calls}"
    )


def test_no_subprocess_in_this_module_builds_its_own_env(tmp_path: Path) -> None:
    """🔴 One hand-written env dict is the whole defect back.

    Every `subprocess.run` here execs the real gate, and the suite the gate
    launches collects this module — so each one is a NESTED gate run. `_gate_env`
    is what hands them the re-entry marker and a redirected state file; `export`
    in the script cannot, because these sites pass CLOSED environments and a
    child inherits nothing it is not given.

    ONE rule: every `subprocess.run` block must NAME the builder. Inverted from
    "look for a bad form" deliberately — a literal scan for the dict misses
    `env = {` with spaces and misses `env=<variable>` assembled on an earlier
    line, and that second one is the closed-env case that actually does the
    sabotage. Requiring the name catches both and is not defeated by a pre-built
    `cmd` list the way looking for `GATE` inside the call was. The dict pattern
    below is not a second rule; it only words which mistake the failure names.
    """
    src = Path(__file__).read_text()

    # Scoped to CALL SITES, not the whole file. A file-wide scan for the dict
    # form matched `_gate_env`'s own construction and this docstring describing
    # it — legitimate text, reported as offenders. Naming the builder is the only
    # property a call site needs, and it is not defeated by a pre-built `cmd`
    # list the way looking for `GATE` inside the call was.
    builder = "_gate" + "_env("
    dict_form = re.compile("env" + r"\s*=\s*\{")
    sites = list(re.finditer(r"subprocess\.run\(", src))
    # 🔴 A scan that matches nothing passes forever. If the call spelling ever
    # changes — a direct `run` import, an alias, a helper wrapper — the loop body
    # never executes and this case reports a clean bill over zero sites. That is
    # the shape it exists to catch, one level up.
    #
    # The prose here deliberately does not write the needle out: this scan reads
    # its own file, so a comment quoting the pattern would be a site the scan
    # then reports.
    assert len(sites) >= 10, (
        f"only {len(sites)} exec site(s) found in a module that launches the gate a "
        "dozen times. The needle has stopped matching how these calls are written, so "
        "this case is checking almost nothing — re-point it rather than lowering the "
        "floor."
    )
    missing = []
    for m in sites:
        depth, end = 0, m.start()
        for i in range(m.start(), len(src)):
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        block = src[m.start() : end + 1]
        if builder in block:
            continue
        line = src[: m.start()].count("\n") + 1
        how = "a hand-written dict" if dict_form.search(block) else "an env built elsewhere"
        missing.append(f"{line} ({how})")
    assert not missing, (
        f"subprocess.run at line(s) {missing} does not name _gate_env. Every subprocess "
        "here is a nested gate run; without the re-entry marker it would repair, restore "
        "and delete the OUTER run's keychain mid-suite. Use "
        "_gate_env(tmp_path, bin_dir, ...) and pass extras as keywords."
    )

    assert "def _gate_env(" in src, "the env builder is gone; the rules guard nothing"
    assert KEYCHAIN_MARKER in _gate_env(tmp_path, tmp_path / "bin"), (
        "_gate_env no longer sets the re-entry marker, so routing every site through "
        "it buys nothing"
    )


def test_repair_restores_the_recorded_default_not_the_fallback(tmp_path: Path) -> None:
    """🔴 The branch that decides which keychain the operator gets back.

    `repair_stale_keychain` has two branches: use the state file's recorded
    default, or — when there is no readable state file — fall back to the
    conventional login path. Only the fallback was ever executed by a test,
    because the case that reached repair pointed the state file at something it
    never created. So the `sed -n '1p'` / `2p` parsing, which is what restores a
    non-default setup, ran nowhere.

    Here the state file EXISTS and names a distinctive path, and the stubbed
    `security` records what the gate asked for.
    """
    log, bin_dir = _stub_env(tmp_path, with_security=True)
    security_log = tmp_path / "security.log"
    security_log.touch()
    state = tmp_path / "keychain-restore"
    recorded_default = "/Users/someone/Library/Keychains/notlogin.keychain-db"
    recorded_list = f"{recorded_default} /Users/someone/Library/Keychains/extra.keychain-db"
    state.write_text(f"{recorded_default}\n{recorded_list}\n")

    result = subprocess.run(
        ["bash", str(GATE), str(tmp_path / "r.xml")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=_gate_env(
            tmp_path,
            bin_dir,
            reentrant=True,
            STUB_LOG=str(log),
            SECURITY_LOG=str(security_log),
            BANKMACHINE_KEYCHAIN_STATE=str(state),
        ),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Repairing" in result.stderr, "the gate never took the repair path:\n" + result.stderr
    assert "NO recorded default" not in result.stderr, (
        "the gate fell back although a state file was present and readable — the "
        "recorded-default branch is not being taken:\n" + result.stderr
    )
    calls = security_log.read_text()
    assert f"default-keychain -s {recorded_default}" in calls, (
        f"repair did not restore the RECORDED default {recorded_default!r}. "
        f"security calls:\n{calls}"
    )
    assert "extra.keychain-db" in calls, (
        "repair restored a search list without the second recorded keychain, so a "
        f"user with more than one searchable keychain loses the rest. Calls:\n{calls}"
    )


def test_neither_teardown_path_destroys_the_keychain() -> None:
    """🔴 A write that landed on the default during the run is in that keychain.

    For this product that can be the datastore key, which `secrets.py` documents
    as unrecoverable. Deleting on the way out turns "misdirected" into
    "destroyed", and the random password means a later reboot relocks it under a
    secret that died with the process — so not deleting is the only thing
    preserving any recovery chance at all.

    Deletion belongs at the START of a run, where the operator caused it and is
    told. Asserted on the script's structure because the alternative is a test
    that deletes a real keychain to prove it does not.
    """
    gate = GATE.read_text()
    for func in ("restore_keychain", "repair_stale_keychain"):
        body = gate.split(f"{func}() {{", 1)[1].split("\n}", 1)[0]
        assert "delete-keychain" not in body, (
            f"{func} deletes the test keychain, destroying anything that landed on the "
            "default during the run rather than merely having misdirected it."
        )
    setup = gate.split("use_test_keychain() {", 1)[1].split("\n}", 1)[0]
    assert "delete-keychain" in setup, (
        "nothing removes a leftover keychain at the start of a run, so they accumulate "
        "in the user's keychain directory forever."
    )


def test_a_leftover_from_an_unclean_run_is_announced_before_it_is_deleted(
    tmp_path: Path,
) -> None:
    """🔴 The interrupt window is the only thing between a stray write and its loss.

    A leftover keychain beside a SURVIVING state file means the run that owned it
    was killed — the case most likely to hold a write nobody has seen. That one is
    announced and pauses.
    """
    log, bin_dir = _stub_env(tmp_path, with_security=True)
    security_log = tmp_path / "security.log"
    security_log.touch()
    state = tmp_path / "keychain-restore"
    state.write_text("/Users/someone/Library/Keychains/notlogin.keychain-db\n")

    result = subprocess.run(
        ["bash", str(GATE), str(tmp_path / "r.xml")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=_gate_env(
            tmp_path,
            bin_dir,
            reentrant=True,
            STUB_LOG=str(log),
            SECURITY_LOG=str(security_log),
            BANKMACHINE_KEYCHAIN_STATE=str(state),
        ),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "did NOT exit cleanly" in result.stderr, (
        "a leftover beside a surviving state file was deleted with no warning — the "
        "operator got no chance to keep it:\n" + result.stderr
    )
    assert "Ctrl-C" in result.stderr, "the announcement offers no way to act on it"
    calls = security_log.read_text()
    assert "show-keychain-info" in calls, (
        "the gate deleted without first checking a leftover exists, so it would print "
        f"the warning on a run that had nothing to remove. Calls:\n{calls}"
    )
    assert "delete-keychain" in calls, f"the leftover was never removed. Calls:\n{calls}"


def test_a_leftover_from_a_clean_run_goes_quietly(tmp_path: Path) -> None:
    """The other half, and the reason the case above is worth reading.

    A clean run removes its state file. A leftover with none beside it is the
    ordinary residue of a run that exited properly, and it is removed silently —
    because a banner that fires on every single run is the one the operator
    learns to skip, and this banner guards the datastore key.
    """
    log, bin_dir = _stub_env(tmp_path, with_security=True)
    security_log = tmp_path / "security.log"
    security_log.touch()
    # No state file written, and an ordinary default: the previous run exited
    # cleanly and left only its keychain behind.
    result = subprocess.run(
        ["bash", str(GATE), str(tmp_path / "r.xml")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=_gate_env(
            tmp_path,
            bin_dir,
            reentrant=True,
            STUB_LOG=str(log),
            SECURITY_LOG=str(security_log),
            STUB_DEFAULT_IS_OURS="0",
        ),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "did NOT exit cleanly" not in result.stderr, (
        "the ordinary residue of a clean run was announced as suspect. A warning that "
        "fires every run is one nobody reads:\n" + result.stderr
    )
    assert "delete-keychain" in security_log.read_text(), (
        "the leftover was not removed, so they accumulate in the keychain directory"
    )


def test_nothing_is_deleted_when_there_is_no_leftover(tmp_path: Path) -> None:
    """`show-keychain-info` is the guard. Without it the gate would issue a delete
    on a first-ever run — harmless today, and the kind of unconditional destructive
    call that stops being harmless when the name is ever reused."""
    log, bin_dir = _stub_env(tmp_path, with_security=True)
    security_log = tmp_path / "security.log"
    security_log.touch()
    result = subprocess.run(
        ["bash", str(GATE), str(tmp_path / "r.xml")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        env=_gate_env(
            tmp_path,
            bin_dir,
            reentrant=True,
            STUB_LOG=str(log),
            SECURITY_LOG=str(security_log),
            STUB_DEFAULT_IS_OURS="0",
            STUB_NO_LEFTOVER="1",
        ),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "delete-keychain" not in security_log.read_text(), (
        "the gate issued a delete although `show-keychain-info` reported no leftover"
    )
