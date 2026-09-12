"""CI calls the gate; it does not keep its own list of what the gate runs.

`scripts/check.sh` is the single declaration of which checks this project gates
on. A workflow that spelled out `uv run pytest`, `uv run ruff check` and `uv run
mypy` in YAML would be a SECOND declaration, free to drift from the first --
which is brookstalley/bankmachine#92 (a declared check that nothing runs) rebuilt
one layer out. The drift would also be invisible in the direction that matters:
adding a check to the script would silently leave CI running the old set, and CI
would stay green while doing less.

🔴 **This reads the workflow as text, and that bounds what it can promise.**
There is no YAML parser in this dependency set and one is not worth adding for
this, so the test can see which commands the file *contains* -- it cannot see
step ordering, `if:` conditions, or a command built at runtime from a variable.
It holds the property that actually decays (someone edits the run step to invoke
a tool directly) and not the ones that do not.

The workflows are found by glob rather than by name, so deleting or renaming the
file fails this rather than quietly satisfying it.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

#: The one command a workflow may use to run this project's checks.
THE_GATE = "scripts/check.sh"

#: Every check the gate declares, in the form a workflow would be tempted to
#: write it. Kept as the `uv run` prefix plus the tool because that is the shape
#: `project-preferences.md` § Dev commands documents and therefore the shape a
#: future author would copy; a bare `pytest` is not how anything here is invoked.
THE_CHECKS_CI_MUST_NOT_RESTATE = (
    "uv run pytest",
    "uv run ruff",
    "uv run mypy",
)


def _workflows() -> list[Path]:
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


def test_there_is_a_workflow_at_all() -> None:
    assert _workflows(), (
        f"no workflow under {WORKFLOWS.relative_to(REPO_ROOT)}. CI is what runs the gate "
        f"somewhere other than the author's machine -- the pre-push hook is per-clone "
        f"opt-in and `--no-verify` bypasses it."
    )


def test_some_workflow_runs_the_gate() -> None:
    callers = [path for path in _workflows() if THE_GATE in path.read_text(encoding="utf-8")]

    assert callers, (
        f"no workflow invokes {THE_GATE}. Every declared check is behind that script; "
        f"a workflow that runs anything else is gating on a different set than the "
        f"governance gate does."
    )


def test_no_workflow_restates_the_checks_the_gate_owns() -> None:
    for path in _workflows():
        text = path.read_text(encoding="utf-8")
        restated = [check for check in THE_CHECKS_CI_MUST_NOT_RESTATE if check in text]

        assert not restated, (
            f"{path.relative_to(REPO_ROOT)} invokes {restated} directly instead of going "
            f"through {THE_GATE}. That is a second declaration of the gated set: add a "
            f"check to the script and this workflow keeps running the old one, green the "
            f"whole time. Call the script."
        )
