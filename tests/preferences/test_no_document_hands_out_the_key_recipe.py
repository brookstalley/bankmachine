"""AC-17.7: no tracked document tells an operator to read the key out by shell.

The recipe `security find-generic-password -s <service> -a datastore:<env> -w`
prints the datastore key to stdout and puts its invocation in shell history,
which AC-10.1 forbids. It shipped anyway, because before FR-12 the product had
no path of its own and an instruction with no route is worse than a leaky one.
`store key export` removed that excuse.

🔴 THIS GUARD EXISTS BECAUSE THE FIRST ONE COULD NOT SEE THE DEFECT IT WAS
WRITTEN FOR. AC-17.7's original check reads COMMAND OUTPUT -- it runs
`store init` and `store status` and asserts the recipe is absent. That is the
right check for those two surfaces and structurally cannot see a third: a
tracked document. The README's quick start kept both the recipe and the claim
"nothing in this product ever prints it" through a review that passed, on the
first surface a new operator reads. A guard scoped to instances cannot catch a
class.

WHAT IS FORBIDDEN, precisely: the recipe inside a fenced code block, i.e. a
document handing the operator something to RUN. Prose that mentions the recipe
is not only allowed but wanted -- the AC-10.1 amendment, the discovery
document and the change-log all have to name what was replaced in order to
explain why, and a rule that forbade the string outright would force those
records to talk around their own subject.

WHAT IS OUT OF SCOPE, and why it is a directory rule rather than a file list:
DATED REVIEW RECORDS and ARCHIVED PLANS. A review from 2026-09-09 quotes the
README as it stood that day, and a build plan in `archive/` describes what was
true when it ran. Editing either to match today's tree destroys the thing that
makes it evidence -- these are history, and history is not doc-drift to be
fixed. The rule is keyed to directories whose names date them, not to
individual files, so it cannot quietly grow into "the file where the last
failure happened".

`delete-generic-password` is deliberately NOT matched: simulating the loss of a
keychain entry is how the recovery drill in `first-production-connection.md`
§ 2.3 is rehearsed, and that drill is something FR-12 made possible rather than
something it forbids.

FAILING CLOSED: a scan that reaches no files fails, rather than passing by
finding nothing in nothing.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]

#: The read-out recipe, in any of the spellings a document might use.
_RECIPE = re.compile(r"find-generic-password")

#: A fenced block, of any language or none. Documents run their commands here.
_FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)


#: Directories holding dated records of a past moment. Their contents are
#: evidence, not instruction, and correcting them would be falsifying them.
_HISTORY_PREFIXES = (".prawduct/artifacts/reviews-", ".prawduct/artifacts/archive/")


def _is_history(relative: str) -> bool:
    return relative.startswith(_HISTORY_PREFIXES)


def _tracked_markdown() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "*.md"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [
        REPO_ROOT / name for name in result.stdout.split("\0") if name and not _is_history(name)
    ]


def test_no_tracked_document_puts_the_key_recipe_in_a_runnable_block() -> None:
    documents = _tracked_markdown()
    assert len(documents) > 10, (
        f"only {len(documents)} markdown files found -- the scan is not reaching the "
        f"repository, so it would pass without checking anything"
    )

    scanned = {str(d.relative_to(REPO_ROOT)) for d in documents}
    # The exemption is a directory rule, so prove it did not swallow the live
    # documents this guard exists for. A scope that quietly widened would pass
    # by scanning nothing that matters.
    for live in ("README.md", "docs/first-production-connection.md", "docs/system-requirements.md"):
        assert live in scanned, f"{live} fell out of the scan -- the exemption is too wide"

    offenders: list[str] = []
    for document in documents:
        text = document.read_text(encoding="utf-8")
        for block in _FENCE.findall(text):
            if _RECIPE.search(block):
                offenders.append(str(document.relative_to(REPO_ROOT)))
                break

    assert not offenders, (
        "these documents hand the operator the shell recipe that leaks the datastore key "
        f"into shell history (AC-10.1, AC-17.7): {', '.join(sorted(offenders))}. "
        "`bankmachine store key export` is the supported path."
    )


def test_the_scan_would_notice_a_recipe_in_a_fenced_block(tmp_path: Path) -> None:
    """The positive control.

    A guard that has never been seen fire is a guard nobody can distinguish from
    a guard that cannot fire. This proves the fence-matching and the pattern both
    work, against a document shaped exactly like the one that got through.
    """
    planted = tmp_path / "quickstart.md"
    planted.write_text(
        "Back the key up now.\n\n```sh\n"
        "security find-generic-password -s bankmachine -a datastore:sandbox -w\n"
        "```\n",
        encoding="utf-8",
    )

    blocks = _FENCE.findall(planted.read_text(encoding="utf-8"))

    assert blocks, "the fence pattern matched nothing -- the real scan would be blind"
    assert any(_RECIPE.search(block) for block in blocks)


def test_prose_naming_the_recipe_is_left_alone() -> None:
    """The records have to be able to describe what they replaced.

    The AC-10.1 amendment, the discovery document and the change-log all name the
    recipe in prose to explain why it is gone. A rule that forbade the string
    outright would force every record to talk around its own subject, which is
    how a record stops being readable and then stops being written.
    """
    prose = "The recipe `security find-generic-password ... -w` put the key in shell history.\n"

    assert _RECIPE.search(prose), "the pattern should match the string itself"
    assert not _FENCE.findall(prose), "prose is not a runnable block and is not scanned"


@pytest.mark.parametrize(
    "document",
    ["README.md", "docs/first-production-connection.md"],
    ids=["readme", "operator-guide"],
)
def test_the_operator_facing_documents_name_the_supported_command(document: str) -> None:
    """The mirror of the absence check: something has to be there instead.

    Removing the recipe without putting the command in its place would satisfy
    the guard above and leave the operator exactly where FR-12 found them --
    told to preserve a value with no route to it.
    """
    text = (REPO_ROOT / document).read_text(encoding="utf-8")

    assert "bankmachine store key export" in text
    assert "bankmachine store key verify" in text
