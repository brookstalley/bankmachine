"""Every caveat this product can emit spells a kind the vocabulary declares.

🔴 A published `outputSchema` closes the `kind` enum to `envelope.WARNING_KINDS`,
and a validating client rejects a payload whose enum does not match. So a
construction site that invents a kind does not produce an unrecognized warning
-- it produces a REJECTED ANSWER, and the warning takes the whole response down
with it. `api-contract.md` § Direction puts incompleteness on the success path
precisely so it cannot reach the error channel; an invented kind routes it there
through the validator.

`Caveat.kind` is a bare `str`, so nothing in the type system prevents this. The
scan is what stands in for the constraint the dataclass cannot express, in the
same spirit as the read-only file handle: the refusal lives somewhere a future
author cannot forget it rather than in a rule they have to remember.

The contract also says a consumer must tolerate a kind it does not recognize.
That is not in tension with the closed enum, and the reason is worth stating
because it is the thing that would change: schema and payload leave one process
in one session, so no client can hold a copy older than the answer it is
validating. Should the schema ever be published separately from the answers it
describes -- cached, vendored, written to a file -- the enum must open, and this
test's premise is what breaks first.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from bankmachine import envelope

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "bankmachine"


def _kinds_in(source: str) -> list[tuple[int, str | None]]:
    """Every `Caveat(...)` in one module's source, with the kind it names.

    🔴 Takes source rather than reading the tree, so the positive control below
    can drive THIS function over a known-bad module instead of asserting
    something adjacent to it. A control that does not run the scanner proves the
    fixture, not the scan -- which is the same defect this file exists to catch,
    one level up.

    A `None` kind is a site whose kind is not a literal -- computed, passed in,
    or read from somewhere. Those are reported separately, because the scan can
    prove nothing about them and silently skipping one would make this test
    weaker exactly where the risk is highest.
    """
    found: list[tuple[int, str | None]] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None)
        if name != "Caveat":
            continue
        kind: ast.expr | None = node.args[0] if node.args else None
        for keyword in node.keywords:
            if keyword.arg == "kind":
                kind = keyword.value
        if isinstance(kind, ast.Constant) and isinstance(kind.value, str):
            found.append((node.lineno, kind.value))
        else:
            found.append((node.lineno, None))
    return found


def _constructed_kinds() -> list[tuple[Path, int, str | None]]:
    """`_kinds_in` over every module the product ships."""
    found: list[tuple[Path, int, str | None]] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        for line, kind in _kinds_in(path.read_text(encoding="utf-8")):
            found.append((path, line, kind))
    return found


def test_every_caveat_names_a_kind_the_vocabulary_declares() -> None:
    vocabulary = set(envelope.WARNING_KINDS)
    strays = [
        f"{path.relative_to(SOURCE_ROOT.parents[1])}:{line} says {kind!r}"
        for path, line, kind in _constructed_kinds()
        if kind is not None and kind not in vocabulary
    ]
    assert not strays, (
        "these caveats name a kind outside the vocabulary, so a client validating "
        "against the published outputSchema would reject the whole answer rather "
        "than merely fail to recognize the warning:\n  " + "\n  ".join(strays)
    )


def test_no_caveat_builds_its_kind_somewhere_this_scan_cannot_read() -> None:
    """A computed kind defeats the scan above, so it has to be refused outright.

    Not a style rule. The scan is the only thing standing between an invented
    kind and a rejected answer, and a site that computes its kind is one the
    scan reports as fine while proving nothing about it.
    """
    opaque = [
        f"{path.relative_to(SOURCE_ROOT.parents[1])}:{line}"
        for path, line, kind in _constructed_kinds()
        if kind is None
    ]
    assert not opaque, (
        "these caveats do not name their kind as a literal, so nothing can check "
        "it against the vocabulary before a client does:\n  " + "\n  ".join(opaque)
    )


def test_the_scan_reaches_the_sites_it_claims_to_check() -> None:
    """A scan over zero call sites passes forever, so prove it found them.

    The positive control below proves the assertion can fail; this proves it is
    aimed at the real source tree rather than at nothing.
    """
    found = _constructed_kinds()
    assert len(found) > 5, f"expected the product's caveat sites, found {len(found)}"
    assert {kind for _, _, kind in found} >= {"stale", "degraded", "gapped"}, (
        "the scan did not reach the pipeline warnings, so it is not reading "
        "the module that emits them"
    )


def test_the_scan_catches_a_kind_the_vocabulary_does_not_declare() -> None:
    """The positive control: a checker that matches nothing passes forever.

    🔴 Drives `_kinds_in` over a module that is known bad, rather than asserting
    a property of the fixture and calling that a control. The earlier version
    checked only that its invented kind was absent from `WARNING_KINDS` -- true
    whether or not the scanner worked at all, which is the failure this whole
    file exists to prevent, reproduced inside its own control.
    """
    known_bad = (
        'warnings.append(Caveat("rate_limited", "slow down"))\n'
        'warnings.append(Caveat(kind="stale", detail="fine"))\n'
        'warnings.append(Caveat(kind=_computed(), detail="opaque"))\n'
    )
    vocabulary = set(envelope.WARNING_KINDS)
    found = _kinds_in(known_bad)

    assert len(found) == 3, f"the scan did not reach all three sites: {found}"
    strays = [kind for _, kind in found if kind is not None and kind not in vocabulary]
    assert strays == ["rate_limited"], f"the scan did not catch the invented kind: {found}"
    assert [line for line, kind in found if kind is None] == [3], (
        "the scan did not report the computed kind as unreadable"
    )
    assert "rate_limited" not in vocabulary, (
        "this control has to name a kind the vocabulary does NOT declare; if the "
        "vocabulary grew to include it, pick another"
    )


#: The contract's own table of warning codes. `api-contract.md` is the canonical
#: description of the wire, and the table is the half a consumer's author reads
#: before writing a branch on `kind`.
CONTRACT = Path(__file__).resolve().parents[2] / ".prawduct" / "artifacts" / "api-contract.md"

#: The heading the table sits under. Matched rather than the table's own shape,
#: because `api-contract.md` holds several tables and a row-shape match would
#: silently start reading a different one if this section moved.
_VOCABULARY_HEADING = "### The warning vocabulary"


def _kinds_in_the_contract_table(document: str) -> list[str]:
    """The code named in the first cell of each row of the vocabulary table.

    Takes the document text rather than reading the file, so the control below
    can drive THIS function over a known-bad document instead of asserting
    something adjacent to it -- the same reason `_kinds_in` above takes source.

    Reads the FIRST backticked token per row and ignores the rest of the cell,
    which is what lets a row carry an annotation (`*(declared; no emitter yet)*`)
    without the reconciliation having an opinion about its wording. The kind's
    NAME is the contract; how far along it is is bookkeeping.
    """
    lines = document.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith(_VOCABULARY_HEADING))
    except StopIteration:
        return []

    found: list[str] = []
    for line in lines[start:]:
        if not line.startswith("|"):
            if found:
                break  # past the table
            continue
        cell = line.split("|")[1]
        match = re.search(r"`([a-z_-]+)`", cell)
        if match:
            found.append(match.group(1))
    return found


def test_the_contract_table_lists_every_kind_the_vocabulary_defines() -> None:
    """🔴 The table and `WARNING_KINDS` are two descriptions of one thing.

    The scan above closes one direction: no module emits a kind the vocabulary
    does not declare. NOTHING closed the other -- a kind could enter the
    vocabulary and never reach the document a consumer's author actually reads,
    and twice now one did. `accounts_without_coverage` was absent from the table
    for a full work cycle after it shipped, and the three kinds added for the
    production-blocker items went stale in their annotations within one commit.

    Both directions are asserted, because they fail differently. A kind missing
    from the TABLE is a consumer told to branch on something nobody documented.
    A kind in the table and not in the vocabulary is worse: it reads as shipped,
    and a consumer that branches on it waits for a warning that can never arrive.
    """
    documented = _kinds_in_the_contract_table(CONTRACT.read_text(encoding="utf-8"))
    vocabulary = set(envelope.WARNING_KINDS)

    assert documented, (
        f"no rows were read from {CONTRACT.name} under {_VOCABULARY_HEADING!r} -- the section "
        f"was renamed or moved, and this check is now reconciling nothing"
    )

    undocumented = sorted(vocabulary - set(documented))
    assert not undocumented, (
        f"{undocumented} are in `envelope.WARNING_KINDS` but not in the contract's warning "
        f"table; a consumer's author reads that table, so a kind absent from it is one nobody "
        f"was told to branch on"
    )

    phantom = sorted(set(documented) - vocabulary)
    assert not phantom, (
        f"the contract's warning table lists {phantom}, which `envelope.WARNING_KINDS` does not "
        f"declare; a documented kind that cannot be emitted is worse than an undocumented one, "
        f"because a consumer will branch on it and wait forever"
    )


def test_the_table_reader_reports_a_kind_the_vocabulary_does_not_declare() -> None:
    """The positive control: proof the table is PARSED rather than assumed empty.

    A reader that returned `[]` on every input would satisfy the `phantom` half
    above forever and would report every real kind as undocumented -- so it
    would be caught. A reader that dropped only ANNOTATED rows would not be:
    it would silently stop reconciling exactly the rows most likely to drift,
    which is the shape of the defect this file exists to catch. So the fixture
    annotates one row and expects it read anyway.
    """
    known_bad = (
        f"{_VOCABULARY_HEADING} — stable, machine-readable\n"
        "\n"
        "| Code | Means |\n"
        "|---|---|\n"
        "| `stale` | Last sync older than expected |\n"
        "| `rate_limited` *(declared; no emitter yet)* | The aggregator asked us to slow down |\n"
        "\n"
        "Prose after the table, holding a `gapped` mention that must NOT be read as a row.\n"
    )

    documented = _kinds_in_the_contract_table(known_bad)

    assert documented == ["stale", "rate_limited"], (
        f"the reader did not return the table's rows in order: {documented}"
    )
    assert "rate_limited" not in set(envelope.WARNING_KINDS), (
        "this control has to name a kind the vocabulary does NOT declare; if the "
        "vocabulary grew to include it, pick another"
    )
