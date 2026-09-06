"""AC-6.4's mechanism is the type checker, so this test runs the type checker.

"Transaction dates stored as dates, sync metadata as UTC timestamps. The two are
never mixed" is not a runtime assertion -- by the time a wrong value reaches a
column it has already been computed, compared and probably aggregated. The
mechanism is `mypy --strict`, and a mechanism nobody exercises is a claim.

So this asserts both directions: that mypy rejects the mixing, and that it
accepts the correct usage. The second half is not padding. Without it, the test
would pass just as happily if mypy failed because the import was broken, which
is the one way a type-check assertion goes quietly green.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]

_PREAMBLE = """
from datetime import UTC, date, datetime

from bankmachine.store.types import (
    CalendarDate,
    MinorUnits,
    UtcInstant,
    calendar_date,
    minor_units,
    utc_instant,
)


def takes_a_date(value: CalendarDate) -> None: ...


def takes_an_instant(value: UtcInstant) -> None: ...


def takes_money(value: MinorUnits) -> None: ...


an_instant = utc_instant(datetime(2026, 9, 6, tzinfo=UTC))
a_date = calendar_date(date(2026, 9, 6))
an_amount = minor_units(1234)
"""

MIXED = (
    _PREAMBLE
    + """
takes_a_date(an_instant)
takes_an_instant(a_date)
takes_a_date(date(2026, 9, 6))
takes_an_instant(datetime(2026, 9, 6, tzinfo=UTC))
takes_money(12.34)
takes_money(1234)
"""
)

CORRECT = (
    _PREAMBLE
    + """
takes_a_date(a_date)
takes_an_instant(an_instant)
takes_money(an_amount)
"""
)


@pytest.fixture(scope="module")
def mypy_report(tmp_path_factory: pytest.TempPathFactory) -> str:
    """One mypy run over both snippets, because mypy is the slow part."""
    workspace = tmp_path_factory.mktemp("temporal")
    (workspace / "mixed_kinds.py").write_text(MIXED, encoding="utf-8")
    (workspace / "correct_kinds.py").write_text(CORRECT, encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "--no-incremental",
            "--no-error-summary",
            "--cache-dir",
            str(workspace / ".mypy_cache"),
            str(workspace / "mixed_kinds.py"),
            str(workspace / "correct_kinds.py"),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, (
        "mypy accepted a calendar date used as an instant. AC-6.4's only enforcement is the "
        f"type checker, so this is the requirement failing:\n{result.stdout}{result.stderr}"
    )
    return result.stdout


def _errors_in(report: str, filename: str) -> list[str]:
    return [line for line in report.splitlines() if filename in line and ": error:" in line]


@pytest.mark.parametrize(
    ("line", "what"),
    [
        ("takes_a_date(an_instant)", "an instant passed where a calendar date belongs"),
        ("takes_an_instant(a_date)", "a calendar date passed where an instant belongs"),
        ("takes_a_date(date(2026, 9, 6))", "a bare date, which skipped the validating constructor"),
        (
            "takes_an_instant(datetime(2026, 9, 6, tzinfo=UTC))",
            "a bare datetime, which skipped the validating constructor",
        ),
        ("takes_money(12.34)", "a float where money belongs"),
        ("takes_money(1234)", "a bare int, which skipped the validating constructor"),
    ],
)
def test_mypy_rejects_the_mixing(mypy_report: str, line: str, what: str) -> None:
    reported = "\n".join(_errors_in(mypy_report, "mixed_kinds.py"))
    expected_line = MIXED.splitlines().index(line) + 1
    assert f"mixed_kinds.py:{expected_line}:" in reported, (
        f"mypy did not reject {what}:\n{reported}"
    )


def test_mypy_accepts_the_correct_usage(mypy_report: str) -> None:
    """The control. Without it, a broken import would make every case above pass."""
    assert not _errors_in(mypy_report, "correct_kinds.py"), (
        "mypy rejected correct usage, so the rejections above prove nothing about the types:\n"
        + "\n".join(_errors_in(mypy_report, "correct_kinds.py"))
    )
