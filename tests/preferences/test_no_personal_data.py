"""The leak guard, run from the pytest suite.

The guard itself stays shell. It is invoked by `.githooks/pre-push` with a
revision range, and its cases drive real refspecs through a real hook in a
throwaway repository -- rewriting that in Python would test a reimplementation
rather than the thing that runs on every push. What the scaffold changes is that
the suite is now a second way to reach it, so the norm is checked at test time
as well as at push time rather than only by a hook each clone must configure.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).parent
GUARD = HERE / "check-no-personal-data.sh"
SELFTEST = HERE / "check-no-personal-data.selftest.sh"
REPO_ROOT = HERE.parents[1]


def _run(script: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(script)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=300)


def test_the_guard_is_present_and_executable() -> None:
    """A guard that cannot run is the failure mode this norm's history is made of."""
    assert GUARD.exists(), f"the leak guard is missing from {GUARD}"
    assert GUARD.stat().st_mode & 0o111, "the leak guard is not executable"


def test_the_pre_push_hook_still_points_at_the_guard() -> None:
    """The migration kept push-time enforcement.

    Losing push-time enforcement to gain test-time enforcement would be a
    straight downgrade, so the wiring follows the script.
    """
    hook = REPO_ROOT / ".githooks" / "pre-push"
    assert hook.exists(), "the pre-push hook is missing"
    assert str(GUARD.relative_to(REPO_ROOT)) in hook.read_text(encoding="utf-8"), (
        "the pre-push hook no longer references the guard at its current path"
    )


def test_the_guard_proves_itself_before_it_reports_clean() -> None:
    """Every case in a throwaway repository, including both fail-closed directions.

    The script prints its own tally; asserting "0 failed" rather than a total keeps
    a case that is added from having to be counted in two places.
    """
    result = _run(SELFTEST)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 failed" in result.stdout


@pytest.mark.filterwarnings("ignore")
def test_the_working_tree_carries_no_roster_or_operator_identity() -> None:
    result = _run(GUARD)
    assert result.returncode == 0, result.stdout + result.stderr
