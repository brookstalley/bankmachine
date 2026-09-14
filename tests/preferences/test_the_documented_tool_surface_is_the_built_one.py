"""The tools the documents advertise are the tools `_tool_definitions()` serves.

The specified surface is larger than the built one and the gap closes a tool at
a time, so every commit that lands a tool has to move a number and a list on
three separate surfaces -- the README, the client-connection guide and the API
contract, which states it in four places of its own. Nothing but this test
notices when one of them is missed, and the surface that drifts is the one a
reader trusts: an agent operator reads the guide to learn what it can ask for.

Two things are derived rather than restated here, so no count and no roster of
names lives in this file:

* the **specified** set is the API contract's own tool table, which is what
  decides how many there are; and
* the **built** set is `_tool_definitions()`, which is what the wire carries.

Everything else is checked against those two. Names are compared, not only
counts, because a rename is the drift most likely to happen and the one a count
cannot see -- a client binds to the name.

Each claim is anchored on the phrasing that states it, and a claim whose anchor
stops matching **fails** rather than passing quietly, because a guard cannot
check a sentence it can no longer find and a claim that has moved is exactly
what drift looks like. The counted claims are matched against whitespace-flat
text so that a reflowed paragraph is still the same claim; the named claims keep
the line structure, because one of them is a table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from bankmachine import mcp

REPO_ROOT = Path(__file__).parents[2]
README = REPO_ROOT / "README.md"
CLIENT_GUIDE = REPO_ROOT / "docs" / "connecting-an-mcp-client.md"
API_CONTRACT = REPO_ROOT / ".prawduct" / "artifacts" / "api-contract.md"
REQUIREMENTS = REPO_ROOT / "docs" / "system-requirements.md"

#: The surfaces that tell a reader what this server offers. A surface that
#: carries no claim at all is a failure of this list, not a pass.
#:
#: 🔴 `system-requirements.md` was ADDED after the merge shipped, because its
#: absence is what let the merge miss it. The sweep updated exactly the
#: sentences this guard parses and every unguarded sibling stayed at ten tools —
#: so §5 still listed `spending_summary` and `cashflow_summary` as separately
#: required and never named `money_summary`, while `boundary-patterns.md`
#: designates §5 "the product's public API contract" and `api-contract.md`
#: describes itself against it. The built tool therefore traced up to a
#: requirement specifying a different tool, and the next builder reading it
#: would have written `cashflow_summary` — the tool the norm merged away.
#: **The remedy for a claim site the guard cannot see is to give it to the
#: guard, not to remember it.**
DOCUMENTING_SURFACES = (README, CLIENT_GUIDE, API_CONTRACT, REQUIREMENTS)

#: The contract's tool table, which is the specification and therefore the
#: source of every count spelled anywhere else. Read from the section that
#: introduces it rather than from the first table in the file, since the
#: operator surface has tables too.
SPECIFICATION_TABLE = re.compile(r"### MCP tool surface.*?\n\n(\|.*?)\n\n", re.DOTALL)

#: A tool name as every one of these documents writes it: in backticks, and
#: alone in its span. The regions below are cut narrowly enough that the only
#: backticked words inside them are tool names.
TOOL_NAME = re.compile(r"`([a-z][a-z0-9_]*)`")

#: Documents spell their counts. A word this map does not know is reported
#: rather than skipped -- a count that cannot be read is not a count that agrees.
NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}


@dataclass(frozen=True)
class Claim:
    """One place a document states which tools exist.

    `says` names the derived set each capture group is claiming: `built` for the
    tools on the wire, `specified` for the ones the contract defines, `unbuilt`
    for the difference. A named claim carries one group holding backticked
    names; a counted claim carries one group per number word it spells.
    """

    path: Path
    where: str
    pattern: re.Pattern[str]
    says: tuple[str, ...]


NAMED_CLAIMS = (
    Claim(
        CLIENT_GUIDE,
        "the tool table under `## The tools`",
        re.compile(r"## The tools\n\n(\|.*?)\n\n", re.DOTALL),
        ("built",),
    ),
    Claim(
        CLIENT_GUIDE,
        "the sentence naming what is not built yet",
        re.compile(r"specified tools\.\*\*(.*?)are not built yet", re.DOTALL),
        ("unbuilt",),
    ),
    Claim(
        REQUIREMENTS,
        "the §5 tool table, which is the contract of record",
        re.compile(r"\| Tool \| Returns \|\n\|---\|---\|\n(\|.*?)\n\n", re.DOTALL),
        ("specified",),
    ),
    Claim(
        REQUIREMENTS,
        "the §5 build-status parenthetical",
        re.compile(r"\*\(Build status [\d-]+: (.*?) are implemented", re.DOTALL),
        ("built",),
    ),
    Claim(
        API_CONTRACT,
        "the descope amendment's `Built:` list",
        re.compile(r"\bBuilt: (`.*?)\.", re.DOTALL),
        ("built",),
    ),
    Claim(
        API_CONTRACT,
        "the descope amendment's `Not built:` list",
        re.compile(r"\bNot built: (`.*?)\.", re.DOTALL),
        ("unbuilt",),
    ),
    Claim(
        API_CONTRACT,
        "the surface inventory's implemented list",
        re.compile(r"are implemented \(([^)]*)\)", re.DOTALL),
        ("built",),
    ),
)

COUNTED_CLAIMS = (
    Claim(
        README,
        "the status paragraph",
        re.compile(r"(\w+) of the (\w+) specified MCP tools are serving"),
        ("built", "specified"),
    ),
    Claim(
        CLIENT_GUIDE,
        "the note under the tool table",
        re.compile(r"\*\*(\w+) of the (\w+) specified tools\.\*\*"),
        ("built", "specified"),
    ),
    Claim(
        API_CONTRACT,
        "the build-status paragraph",
        re.compile(
            r"(\w+) of the (\w+) tools below are implemented and "
            r"(\w+) (?:is|are) specification only"
        ),
        ("built", "specified", "unbuilt"),
    ),
    Claim(
        API_CONTRACT,
        "the tool-surface heading",
        re.compile(
            r"### MCP tool surface — the (\w+) tools \(§5\) · \*(\w+) built, (\w+) specified\*"
        ),
        ("specified", "built", "unbuilt"),
    ),
    Claim(
        API_CONTRACT,
        "the surface inventory's preamble",
        re.compile(r"(\w+) of the (\w+) are implemented"),
        ("built", "specified"),
    ),
)


def _specified_tools() -> frozenset[str]:
    text = API_CONTRACT.read_text(encoding="utf-8")
    table = SPECIFICATION_TABLE.search(text)
    assert table, (
        "the contract's MCP tool table is no longer where this guard looks for it, "
        "and it is the specification every other surface is checked against"
    )
    names: set[str] = set()
    for row in table.group(1).splitlines():
        cells = row.split("|")
        # The first cell is the tool; the header and the rule between them hold
        # neither a name nor backticks, so `fullmatch` drops them without a rule
        # about which row is which.
        named = TOOL_NAME.fullmatch(cells[1].strip()) if len(cells) > 1 else None
        if named:
            names.add(named.group(1))
    return frozenset(names)


def _tool_sets() -> dict[str, frozenset[str]]:
    specified = _specified_tools()
    built = frozenset(definition["name"] for definition in mcp._tool_definitions())
    return {"specified": specified, "built": built, "unbuilt": specified - built}


def _flattened(text: str) -> str:
    """One space between everything, so a reflowed sentence is the same claim."""
    return re.sub(r"\s+", " ", text)


def _claimed_names(claim: Claim, text: str) -> frozenset[str] | None:
    found = claim.pattern.search(text)
    return None if found is None else frozenset(TOOL_NAME.findall(found.group(1)))


def test_the_server_advertises_nothing_the_contract_does_not_specify() -> None:
    sets = _tool_sets()
    assert sets["built"], "the server advertises no tools at all"
    assert sets["specified"], "the contract specifies no tools at all"
    assert sets["built"] <= sets["specified"], (
        "the server serves tools the contract does not specify:\n  "
        + "\n  ".join(sorted(sets["built"] - sets["specified"]))
    )


def test_every_document_names_the_tools_that_are_actually_built() -> None:
    sets = _tool_sets()
    disagreements: list[str] = []
    for claim in NAMED_CLAIMS:
        text = claim.path.read_text(encoding="utf-8")
        claimed = _claimed_names(claim, text)
        where = f"{claim.path.relative_to(REPO_ROOT)}, {claim.where}"
        if claimed is None:
            disagreements.append(f"{where}: no longer says what this guard reads it for")
            continue
        expected = sets[claim.says[0]]
        if claimed != expected:
            disagreements.append(
                f"{where}: names {sorted(claimed)}, the code serves {sorted(expected)}"
            )

    assert not disagreements, (
        "a document advertises a different tool surface than the server serves:\n  "
        + "\n  ".join(disagreements)
    )


def test_every_document_counts_the_tools_that_are_actually_built() -> None:
    sets = _tool_sets()
    disagreements: list[str] = []
    for claim in COUNTED_CLAIMS:
        text = _flattened(claim.path.read_text(encoding="utf-8"))
        where = f"{claim.path.relative_to(REPO_ROOT)}, {claim.where}"
        matches = list(claim.pattern.finditer(text))
        if not matches:
            disagreements.append(f"{where}: no longer says what this guard reads it for")
            continue
        for match in matches:
            for word, names in zip(match.groups(), claim.says, strict=True):
                counted = NUMBER_WORDS.get(word.lower())
                if counted is None:
                    disagreements.append(f"{where}: {word!r} is not a number this guard can read")
                elif counted != len(sets[names]):
                    disagreements.append(
                        f"{where}: says {word} {names}, there are {len(sets[names])}"
                    )

    assert not disagreements, (
        "a document counts a different tool surface than the server serves:\n  "
        + "\n  ".join(disagreements)
    )


def test_every_documenting_surface_carries_a_claim() -> None:
    """A surface nobody checks drifts, and a claim table with a hole is silent."""
    claimed = {claim.path for claim in (*NAMED_CLAIMS, *COUNTED_CLAIMS)}
    assert claimed == set(DOCUMENTING_SURFACES)
    for path in DOCUMENTING_SURFACES:
        assert path.exists(), f"{path.relative_to(REPO_ROOT)} is gone and its claims went with it"


def test_a_rename_does_not_walk_past_the_comparison() -> None:
    """The positive control, and the reason names are compared rather than counts.

    Each named claim is re-read against a doctored copy of its own document: one
    tool renamed, then one removed. The renamed set is the same size as the real
    one, so a count-only check reads it as agreement.
    """
    sets = _tool_sets()
    for claim in NAMED_CLAIMS:
        if claim.says[0] != "built":
            continue
        text = claim.path.read_text(encoding="utf-8")
        assert _claimed_names(claim, text) == sets["built"], claim.where

        original = sorted(sets["built"])[0]
        renamed = _claimed_names(claim, text.replace(f"`{original}`", f"`{original}_v2`"))
        assert renamed is not None and renamed != sets["built"], claim.where
        assert len(renamed) == len(sets["built"]), claim.where

        # Removing a name either changes the set or leaves the anchor unable to
        # match. Both are reported as disagreement, and neither is agreement.
        without = _claimed_names(claim, text.replace(f"`{original}`", ""))
        assert without != sets["built"], claim.where
