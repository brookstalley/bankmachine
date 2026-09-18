"""The refusal vocabularies cannot grow past the documents that define them.

🔴 **The class this closes.** A refusal is the one payload a consumer meets when
it has already got something wrong, and both halves of it are vocabularies: the
`code` it branches on, and the recovery fields it builds the corrected call
from. A code nothing declares, or a field described nowhere, fails the way this
repo's other vocabularies used to -- silently, on the consumer's side, with
every test still green.

The code vocabulary itself is closed by TYPE rather than by this file:
`mcp._tool_error` takes `envelope.ErrorCode`, so mypy refuses a fifth code at
the call site. What is left for a test is that the type, the contract table and
the served document describe the same three.

🔴 **What this does NOT check.** That a description is ACCURATE -- nothing
mechanical can, and saying so is how this file avoids implying coverage it does
not have. That is the Critic's, under `api-contract.md`'s Direction norms.
"""

from __future__ import annotations

import re
from pathlib import Path

from bankmachine import envelope, mcp_resources

CONTRACT = Path(__file__).resolve().parents[2] / ".prawduct" / "artifacts" / "api-contract.md"


def _contract_text() -> str:
    return CONTRACT.read_text(encoding="utf-8")


def _backticked(text: str) -> set[str]:
    """Every `name` the document sets in backticks, which is how it names a field."""
    return set(re.findall(r"`([a-z_]+)`", text))


def test_every_error_code_is_in_the_contract_table() -> None:
    """The three the type declares are the three the contract publishes."""
    documented = _backticked(_contract_text())
    missing = set(envelope.ERROR_CODES) - documented
    assert not missing, f"{sorted(missing)} is a code no consumer can look up"


def test_every_error_code_is_in_the_served_document() -> None:
    """🔴 A code the reference skips is one an agent meets with no guidance at all.

    The document renders from `ERROR_CODES` itself, so this cannot fail by
    omission of a heading -- what it catches is a code whose guidance was never
    written, which renders as the "unwritten" line instead of vanishing.
    """
    text = mcp_resources._refusals_reference()
    for code in envelope.ERROR_CODES:
        assert f"### `{code}`" in text, f"{code} is not a section of the refusals reference"
        assert code in mcp_resources._CODE_GUIDANCE, f"{code} has no guidance written for it"


def test_every_recovery_field_is_described_in_both_documents() -> None:
    """🔴 Two descriptions, compared -- the guard the published-wire test makes one surface over.

    Walked from the dataclass, so a field added to `Recovery` and documented
    nowhere reddens this rather than reaching the wire unexplained. The three
    keys the boundary adds itself (`required`, `optional`, `see`) are named here
    because they are not dataclass fields and would otherwise be checked by
    nothing.
    """
    from dataclasses import fields

    emitted = {field.name for field in fields(envelope.Recovery)} | {
        "required",
        "optional",
        "see",
    }
    documented = {name for name, _ in mcp_resources._RECOVERY_FIELDS}
    assert emitted == documented, (
        f"the served document and the emitted block disagree: "
        f"undescribed={sorted(emitted - documented)}, described-but-never-sent="
        f"{sorted(documented - emitted)}"
    )

    contract = _backticked(_contract_text())
    missing = emitted - contract
    assert not missing, f"{sorted(missing)} reach the wire and the contract never names them"
