"""AC-10.4: the aggregator's API is the only network destination.

No telemetry, no analytics, no third-party error reporting. The requirement is
absolute -- "any other destination is a finding, not a feature" -- and until now
nothing checked it. A single `httpx.post` to a crash reporter would have passed
every guard this repository has, which is the shape of failure that makes a
requirement decorative.

WHAT THIS CHECKS, AND WHAT IT CANNOT

It checks that nothing outside `connector/` can *reach* a network transport, by
import. That is a structural property with a real edge: it cannot see a
subprocess shelling out to `curl`, and it cannot see a dependency phoning home
on its own. Those stay judgment calls under the same norm, reviewed by the
Critic. What it does remove is the accidental case -- the one where somebody
adds a perfectly reasonable-looking client to a module that has no business
holding one.

It is deliberately about TRANSPORT rather than about the aggregator. The
aggregator SDK's containment is `test_connector_is_contained.py`'s job, and
duplicating it here would mean two tests failing for one cause and neither
saying which rule was actually broken.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
SOURCE_ROOT = REPO_ROOT / "src" / "bankmachine"
CONNECTOR_ROOT = SOURCE_ROOT / "connector"

#: Top-level modules that can open a socket. Standard-library and third-party
#: alike: the norm is about reaching the network, not about who shipped the
#: code that does it.
NETWORK_MODULES = frozenset(
    {
        "socket",
        "ssl",
        "http",
        "urllib3",
        "requests",
        "httpx",
        "aiohttp",
        "websockets",
        "grpc",
        "boto3",
        "botocore",
        "paramiko",
        "pycurl",
        "ftplib",
        "smtplib",
        "poplib",
        "imaplib",
        "xmlrpc",
        "telnetlib",
    }
)

#: `urllib` is split rather than banned. `urllib.parse` is string manipulation
#: and opens nothing; `urllib.request` is a client. Banning the package whole
#: would push someone toward hand-rolling URL parsing, which is a worse outcome
#: than the rule prevents.
SAFE_URLLIB_SUBMODULES = frozenset({"urllib.parse"})

#: The aggregator SDK. Named here only for the positive control below -- the
#: rule about where it may be imported lives in `test_connector_is_contained`.
AGGREGATOR_SDK = "plaid"


def _python_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def _imported_modules(source: str, filename: str) -> set[str]:
    tree = ast.parse(source, filename=filename)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def _network_imports(modules: set[str]) -> set[str]:
    found: set[str] = set()
    for module in modules:
        top = module.split(".")[0]
        if top == "urllib":
            if module not in SAFE_URLLIB_SUBMODULES:
                found.add(module)
        elif top in NETWORK_MODULES:
            found.add(module)
    return found


def test_nothing_outside_the_connector_can_reach_the_network() -> None:
    scanned = 0
    offenders: list[str] = []
    for path in _python_files(SOURCE_ROOT):
        if path.is_relative_to(CONNECTOR_ROOT):
            continue
        scanned += 1
        reachable = _network_imports(_imported_modules(path.read_text(encoding="utf-8"), str(path)))
        if reachable:
            offenders.append(f"{path.relative_to(REPO_ROOT)} imports {sorted(reachable)}")

    assert scanned, f"scanned no files outside {CONNECTOR_ROOT}; the scan is not testing anything"
    assert not offenders, (
        "a network transport is reachable outside connector/, so AC-10.4's "
        "'the aggregator is the only network destination' is no longer structural:\n  "
        + "\n  ".join(offenders)
        + "\n\nTelemetry, analytics and third-party error reporting are a finding, not a feature."
    )


def test_the_connector_really_does_reach_the_network() -> None:
    """The positive control.

    A containment scan passes trivially when there is nothing to contain, and
    would keep passing if the connector were emptied or renamed. This asserts
    the thing being contained exists -- so a green run above means "contained",
    never "absent".
    """
    reaching = [
        path
        for path in _python_files(CONNECTOR_ROOT)
        if _network_imports(_imported_modules(path.read_text(encoding="utf-8"), str(path)))
        or AGGREGATOR_SDK
        in {m.split(".")[0] for m in _imported_modules(path.read_text(encoding="utf-8"), str(path))}
    ]
    assert reaching, (
        f"nothing under {CONNECTOR_ROOT.relative_to(REPO_ROOT)} reaches the network, so the "
        "test above is passing because there is nothing to contain"
    )


def test_the_scan_can_actually_find_a_network_import() -> None:
    """The patterns must match the thing they are looking for."""
    assert _network_imports({"httpx"}) == {"httpx"}
    assert _network_imports({"urllib.request"}) == {"urllib.request"}
    assert _network_imports({"http.client"}) == {"http.client"}
    # ...and must not flag the parsing half of urllib, or unrelated modules.
    assert not _network_imports({"urllib.parse"})
    assert not _network_imports({"pathlib", "json", "sqlalchemy"})
