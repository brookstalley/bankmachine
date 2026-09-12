"""CI calls the gate; it does not keep its own list of what the gate runs.

`scripts/check.sh` is the single declaration of which checks this project gates
on. A workflow that spelled out `uv run pytest`, `uv run ruff check` and `uv run
mypy` in YAML would be a SECOND declaration, free to drift from the first --
which is brookstalley/bankmachine#92 (a declared check that nothing runs) rebuilt
one layer out. The drift would also be invisible in the direction that matters:
adding a check to the script would silently leave CI running the old set, and CI
would stay green while doing less.

🔴 **The forbidden list is READ OUT OF THE GATE, never written here.** A
hand-kept copy would be one more declaration of the gated set inside the module
whose entire purpose is that there be only one -- and it would fail in the
direction that hides: add a fifth tool to `scripts/check.sh` and a hardcoded list
never learns about it, so the guard goes on passing while CI is free to restate
the new check.

🔴 **This reads the workflow as text, and that bounds what it can promise.**
There is no YAML parser in this dependency set and one is not worth adding for
this, so the test can see which commands the file *contains* -- it cannot see
step ordering, `if:` conditions, or a command built at runtime from a variable.
It holds the property that actually decays (someone edits the run step to invoke
a tool directly) and not the ones that do not.

Comment lines are stripped before anything is matched. Without that the test
fails on a workflow that merely *documents* the rule it enforces -- the most
likely author of a `# don't run uv run pytest here` line is someone honouring the
norm -- and, worse, a comment mentioning the script would satisfy the
gate-is-called check on its own.

The workflows are found by glob rather than by name, so deleting or renaming the
file fails this rather than quietly satisfying it.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
GATE = REPO_ROOT / "scripts" / "check.sh"

#: The one command a workflow may use to run this project's checks.
THE_GATE = "scripts/check.sh"

#: How the gate names each check it runs: `check "uv run mypy" uv run mypy`. The
#: quoted first argument is the label the gate reports and the exact string a
#: workflow author would copy, which is what makes it the right thing to forbid.
_CHECK_CALL = re.compile(r'^\s*check\s+"([^"]+)"', re.MULTILINE)


def checks_the_gate_owns() -> list[str]:
    """The gated set, read from the gate itself.

    Derived rather than declared: this module must not become the fifth place the
    list is written down. If the regex ever matches nothing the tests below say so
    loudly rather than passing on an empty forbidden-list, which would be the
    silent failure -- a guard that forbids nothing looks exactly like a guard that
    found nothing wrong.
    """
    return _CHECK_CALL.findall(GATE.read_text(encoding="utf-8"))


def _commands_only(text: str) -> str:
    """The workflow with comment lines removed. See the module docstring."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _workflows() -> list[Path]:
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


def test_there_is_a_workflow_at_all() -> None:
    assert _workflows(), (
        f"no workflow under {WORKFLOWS.relative_to(REPO_ROOT)}. CI is what runs the gate "
        f"somewhere other than the author's machine -- the pre-push hook is per-clone "
        f"opt-in and `--no-verify` bypasses it."
    )


def test_the_gate_declares_the_checks_this_module_reads() -> None:
    """Grounds the derivation. An empty list would make the guard below vacuous."""
    owned = checks_the_gate_owns()

    assert owned, (
        f'no `check "..."` calls found in {GATE.relative_to(REPO_ROOT)}; this module '
        f"derives the forbidden set from them, so it is now forbidding nothing"
    )


def test_some_workflow_runs_the_gate() -> None:
    callers = [
        path
        for path in _workflows()
        if THE_GATE in _commands_only(path.read_text(encoding="utf-8"))
    ]

    assert callers, (
        f"no workflow invokes {THE_GATE}. Every declared check is behind that script; "
        f"a workflow that runs anything else is gating on a different set than the "
        f"governance gate does."
    )


def test_no_workflow_restates_the_checks_the_gate_owns() -> None:
    owned = checks_the_gate_owns()

    for path in _workflows():
        text = _commands_only(path.read_text(encoding="utf-8"))
        restated = [check for check in owned if check in text]

        assert not restated, (
            f"{path.relative_to(REPO_ROOT)} invokes {restated} directly instead of going "
            f"through {THE_GATE}. That is a second declaration of the gated set: add a "
            f"check to the script and this workflow keeps running the old one, green the "
            f"whole time. Call the script."
        )
