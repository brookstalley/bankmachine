"""AC-10.2: `.gitignore` covers data, logs and credential paths, and no tracked
file carries a token-shaped string.

This is the *credential* guard. It is a different check from
`check-no-personal-data.sh`, and the difference has bitten before: that script
hunts ROSTER tokens supplied by the gitignored `deployment/` directory, so a
stray secret that matches no institution name walks straight past it. This one
knows nothing about the roster and looks only at credential SHAPE.

Both are needed. Neither subsumes the other: a roster name is not token-shaped,
and a leaked access token names no institution.

SCOPE: tracked files, from a fresh `git ls-files`, as AC-10.2 words it. Not the
working tree -- an untracked scratch file holding a token is not a leak, and
including them would make the check noisy enough to be turned off.

NO FILE IS EXEMPT, INCLUDING THIS ONE. An earlier version skipped itself -- the
file carrying the patterns "cannot scan itself" -- which made the repository's
credential guard the one place it never looked, and that is precisely the file
skip list the marker below exists to avoid. It is also the file where a
credential-shaped literal looks normal to a reviewer, so someone debugging a
pattern by pasting in a real token is a plausible path rather than an exotic
one. This file declares its own vectors per line, like any other file must.

FAILING CLOSED: every error path fails the test. A guard whose only bad-news
channel is the absence of output cannot report that it stopped guarding, which
is the rule `check-no-personal-data.sh` already states and this file inherits.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]

#: Paths that MUST be ignored. Each is a representative of one AC-10.2 clause:
#: data, logs, and any credential path.
MUST_BE_IGNORED = (
    ".env",
    ".env.local",
    "data/anything.db",
    "logs/sync.log",
    "store.db",
    "store.db-wal",
    "store.db-shm",
    "deployment/roster-tokens.txt",
)

#: The negative control for the ignore check. `.env.example` is deliberately
#: tracked -- it documents the variables and holds no values. If this became
#: ignored, the ignore rules would have grown teeth they should not have, and
#: every assertion above would still pass.
MUST_NOT_BE_IGNORED = (".env.example", "pyproject.toml")

#: Aggregator access tokens carry an environment-tagged prefix. Naming the
#: aggregator here is within §0.1's carve-out: it is a single named dependency,
#: and this is its credential format rather than a roster institution's identity.
_ACCESS_TOKEN = re.compile(r"access-(?:sandbox|development|production)-[0-9a-fA-F-]{16,}")

#: The datastore key is exactly 64 hex characters (256 bits, passed raw so
#: SQLCipher's KDF is skipped -- see `secrets.KEY_HEX_LENGTH`).
_RAW_KEY = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])")

#: A labelled credential with a value that is actually there. The empty
#: assignment in `.env.example` is the shape this must NOT match.
#: The quote is captured, not skipped, because whether the value was quoted is
#: the whole discrimination in `_is_a_variable_reference` below.
_LABELLED = re.compile(
    r"(?:access[_-]?token|client[_-]?secret|api[_-]?key|password|passwd)"
    r"\s*[=:]\s*([\"']?)([A-Za-z0-9_\-]{12,})",
    re.IGNORECASE,
)


def _is_a_variable_reference(quote: str, value: str, path: Path) -> bool:
    """`access_token=enrolled_item` names a variable; it does not carry one.

    🔴 **Scoped to Python source, and to unquoted values, deliberately.** In
    Python an unquoted bare identifier after `=` is a reference to something
    defined elsewhere -- a credential lives in the quoted literal it was
    assigned from, and that assignment is what this scan should catch. In a
    `.env`, `.toml` or shell file the same shape *is* a literal
    (`API_KEY=mysecret123`), so the exemption must not reach those, and the
    suffix check is what keeps it out.

    Without this the check fires on every function that accepts an access token
    and passes it on by keyword -- and a guard that fires on ordinary code is a
    guard someone narrows in irritation later, which is how a security check
    dies quietly. The alternative was renaming those parameters, which makes the
    product worse to appease the checker.
    """
    return not quote and path.suffix == ".py" and value.isidentifier()


#: A 64-hex run is also what every content hash looks like. Excluding by CONTEXT
#: rather than by file keeps the check alive inside lockfiles and schema
#: constants, where a real key would otherwise get a free pass. The trade is
#: stated rather than hidden: a key parked on a line that says "sha256" is
#: missed, which is a narrower hole than skipping those files wholesale.
_IS_A_HASH = re.compile(r"sha\d*|hash|digest|checksum|revision|commit", re.IGNORECASE)

#: Placeholders that are documentation, not credentials.
_PLACEHOLDER = re.compile(
    r"^(?:x{4,}|y{4,}|a{12,}|b{12,}|0{12,}|<[^>]+>|your[_-]|example|placeholder|redacted|"
    r"changeme|dummy|fake|sample|test[_-]?only)",
    re.IGNORECASE,
)

SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".ico", ".woff", ".woff2", ".zip"}

#: Some credential-shaped literals are load-bearing: the redaction tests need
#: real token SHAPES or they prove nothing. Those lines carry this marker, on
#: the line itself or the one above it.
#:
#: This is a DECLARATION, not a skip list, and the difference is the point. A
#: skip list exempts a file, so the next real secret to land in that file is
#: exempt too and nobody decides anything. A marker exempts one line, is written
#: by hand, and shows up in the diff of whoever adds it -- so exempting a real
#: credential is an act someone has to perform and a reviewer can see.
_DECLARED = re.compile(r"credential-shape:\s*test vector")


def _git(*args: str) -> str:
    try:
        done = subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - environment
        pytest.fail(f"could not run git ({exc}); this check fails closed rather than passing")
    if done.returncode != 0:
        pytest.fail(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


def _tracked_text_files() -> list[Path]:
    listing = _git("ls-files", "-z")
    files: list[Path] = []
    for name in listing.split("\0"):
        if not name:
            continue
        path = REPO_ROOT / name
        if path.suffix.lower() in SKIP_SUFFIXES or not path.is_file():
            continue
        files.append(path)
    return files


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _findings(text: str, path: Path) -> list[str]:
    found: list[str] = []
    lines = text.splitlines()
    for number, line in enumerate(lines, start=1):
        previous = lines[number - 2] if number >= 2 else ""
        # A marker on the previous line counts only when that line is a comment
        # and nothing else. A TRAILING marker on a line of code declares that
        # line, not the one after it -- without this, one declaration silently
        # covers its neighbour, which is how a per-line exemption quietly
        # becomes a range.
        declared_above = _DECLARED.search(previous) and previous.lstrip().startswith("#")
        if _DECLARED.search(line) or declared_above:
            continue
        where = f"{path.relative_to(REPO_ROOT)}:{number}"
        if _ACCESS_TOKEN.search(line):
            found.append(f"{where}: an aggregator access token")
        if _RAW_KEY.search(line) and not _IS_A_HASH.search(line):
            found.append(f"{where}: a 64-hex run, the datastore key's shape")
        for quote, value in _LABELLED.findall(line):
            if _PLACEHOLDER.match(value) or _is_a_variable_reference(quote, value, path):
                continue
            found.append(f"{where}: a labelled credential with a value")
    return found


# --------------------------------------------------------------------------- #
# AC-10.2, first clause: the ignore rules cover data, logs and credentials.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("candidate", MUST_BE_IGNORED)
def test_the_ignore_rules_cover_data_logs_and_credentials(candidate: str) -> None:
    done = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", candidate],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, (
        f"{candidate} is NOT ignored. AC-10.2 requires .gitignore to cover data, logs "
        f"and any credential path"
    )


@pytest.mark.parametrize("candidate", MUST_NOT_BE_IGNORED)
def test_the_ignore_rules_are_not_swallowing_tracked_files(candidate: str) -> None:
    """The negative control: rules that ignore everything would pass the test above."""
    done = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", candidate],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert done.returncode != 0, f"{candidate} is ignored, but it is meant to be tracked"


# --------------------------------------------------------------------------- #
# AC-10.2, second clause: no token-shaped string in any tracked file.
# --------------------------------------------------------------------------- #


def test_no_tracked_file_carries_a_token_shaped_string() -> None:
    offenders: list[str] = []
    for path in _tracked_text_files():
        text = _read(path)
        if text is None:
            continue
        offenders.extend(_findings(text, path))

    assert not offenders, (
        "a tracked file carries something shaped like a credential:\n  "
        + "\n  ".join(offenders)
        + "\n\nIf one of these is a false positive, narrow the pattern -- do not add "
        "the file to a skip list, because the next real one will land in it too."
    )


def test_the_scan_can_actually_find_each_shape() -> None:
    """The positive control. A scan that has never matched is not known to work."""
    planted = "\n".join(
        (
            # credential-shape: test vector
            "access_token = access-sandbox-11112222-3333-4444-5555-666677778888",
            "datastore key: " + "9f" * 32,
            # credential-shape: test vector
            'client_secret = "s3cr3tvalue_that_is_long"',
        )
    )
    found = _findings(planted, REPO_ROOT / "planted.txt")
    assert len(found) >= 3, f"the patterns missed a planted credential: {found}"


def test_the_placeholder_and_hash_exemptions_do_not_swallow_a_real_secret() -> None:
    """The exemptions must be narrow enough to still catch the real thing."""
    assert not _findings('client_secret = "<your-secret-here>"', REPO_ROOT / "x.txt")
    assert not _findings(f"sha256 = {'ab' * 32}", REPO_ROOT / "x.txt")
    # ...but the same key shape on an ordinary line is still caught.
    assert _findings(f"key = {'ab' * 32}", REPO_ROOT / "x.txt")


def test_a_variable_reference_is_exempt_but_only_in_python_and_only_unquoted() -> None:
    """The exemption has three edges, and each one is load-bearing.

    Python source, unquoted, and a bare identifier. Drop any of them and either
    the guard fires on ordinary code -- which is how it gets narrowed in
    irritation later -- or it stops seeing the file format where an unquoted
    value really is the secret.
    """
    assert not _findings("access_token=access_token", REPO_ROOT / "x.py")
    assert not _findings("    ItemGetRequest(access_token=enrolled_item),", REPO_ROOT / "x.py")

    token = "access-sandbox-11112222-3333-4444-5555-666677778888"  # credential-shape: test vector
    # Quoted is a literal, wherever it appears.
    assert _findings(f'access_token = "{token}"', REPO_ROOT / "x.py")
    # credential-shape: test vector
    assert _findings("access_token = 'supersecretvalue'", REPO_ROOT / "x.py")
    # 🔴 The same unquoted shape in a dotenv IS the secret, so the exemption must
    # not reach there. This is the edge that would silently un-guard `.env`.
    # credential-shape: test vector
    assert _findings("BANKMACHINE_API_KEY=mysecretvalue123", REPO_ROOT / ".env")
    # credential-shape: test vector
    assert _findings("client_secret: mysecretvalue123", REPO_ROOT / "config.toml")
    # A hyphenated value is not a Python identifier, so it is not a reference.
    # credential-shape: test vector
    assert _findings("access_token=not-an-identifier-value", REPO_ROOT / "x.py")


def test_the_declaration_marker_exempts_one_line_and_only_that_line() -> None:
    """The marker must work, and must not spill onto its neighbours.

    A marker that exempted a whole file, or leaked downward, would be the skip
    list this design refuses -- silently, and with the comment still claiming
    otherwise.
    """
    # credential-shape: test vector
    token = "access-sandbox-11112222-3333-4444-5555-666677778888"
    assert not _findings(f"{token}  # credential-shape: test vector", REPO_ROOT / "x.py")
    assert not _findings(f"# credential-shape: test vector\n{token}", REPO_ROOT / "x.py")
    # Two lines below the marker is NOT exempt.
    assert _findings(f"# credential-shape: test vector\nfiller\n{token}", REPO_ROOT / "x.py")
    # And an unmarked line in the same text is still caught.
    assert _findings(f"{token}  # credential-shape: test vector\n{token}", REPO_ROOT / "x.py")


def test_something_is_actually_scanned() -> None:
    """A scan over zero files passes forever and means nothing."""
    files = _tracked_text_files()
    assert len(files) > 20, f"only {len(files)} tracked files scanned; git ls-files is not working"
