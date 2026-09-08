"""Which build this process is running, captured once and never re-read.

🔴 **Captured at import, and that is the requirement rather than an optimization.**
This process serves the code it loaded when it started. Reading the hash per
request would report the *repository's* current HEAD, so a server still running
pre-merge code would answer with the merged commit and call itself current --
which is precisely the failure this stamp exists to expose. Re-reading it would
build the defect into the instrument meant to catch it.

The cost of that choice is the honest one: a long-lived process reports the build
it started with, forever, even after the checkout moves under it. That is the
true statement. The checkout's HEAD is a fact about the disk, not about this
process.

**This is build provenance, not API versioning.** `api-contract.md` § Versioning
records "scheme: none, deferred" for the consumer-facing contract, and that
decision stands; its revisit trigger is a consumer outside this machine. What is
reported here answers "which code answered me", which sits beside `as_of` and
`environment` rather than beside a version handle.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from importlib.resources import files as package_files
from pathlib import Path
from typing import Any


def _package_dir() -> Path:
    """Where this package's code lives, without assuming an install layout.

    🔴 `importlib.resources.files`, not a walk up from the module file.
    AC-ARCH.4 forbids shipped code locating itself relative to the repository,
    and the reason applies here rather than being a technicality: this has to
    work when the package is installed somewhere that is not a checkout, and the
    honest answer there is "no commit" rather than a guessed path.

    Anchored to the package rather than to the working directory because the
    server is launched with `uv run --directory <path>` -- cwd is whatever the
    client chose, while the package's own location is where the code being
    reported actually came from.
    """
    return Path(str(package_files("bankmachine")))


logger = logging.getLogger(__name__)

# 🔴 Short on purpose. Two invocations sit in front of `initialize`, so this is a
# budget the client waits out before the handshake returns. `rev-parse` and
# `status` on a local checkout are effectively instant; a git that has not
# answered in this long is wedged, and a fast "unknown" beats a slow one.
_GIT_TIMEOUT_SECONDS = 2


@dataclass(frozen=True)
class BuildIdentity:
    """The code this process is running. `None` means unknown, never assumed."""

    version: str
    commit: str | None
    dirty: bool | None

    def to_wire(self) -> dict[str, Any]:
        return {"version": self.version, "commit": self.commit, "dirty": self.dirty}


def _git(*arguments: str) -> str | None:
    """One git command in the package's own directory, or `None` if it cannot run.

    Narrow catches on purpose. `OSError` is git being absent from PATH;
    `SubprocessError` covers the timeout. A non-zero exit -- which is what "not a
    git checkout" looks like -- is read from `returncode` rather than raised, so
    the ordinary case is not routed through an exception.
    """
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=_package_dir(),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # 🔴 Logged, because `commit: null` is one value for several causes --
        # git absent, git wedged, not a checkout, package installed elsewhere --
        # and an operator asking "why does my server not report a commit" has
        # nothing else to read. The wire says only that it is unknown; the log
        # says why. Never raised: an unidentifiable build must still serve.
        logger.info("build identity: git %s could not run (%s)", arguments[0], exc)
        return None
    if completed.returncode != 0:
        # The ordinary "not a git checkout" case lands here, so this is debug
        # rather than info -- an installed copy outside a checkout is a correct
        # deployment, not a degradation worth a line in every startup log.
        logger.debug(
            "build identity: git %s exited %d (%s)",
            arguments[0],
            completed.returncode,
            completed.stderr.strip()[:200],
        )
        return None
    return completed.stdout.strip()


@lru_cache(maxsize=1)
def build_identity() -> BuildIdentity:
    """The running build, captured at import by the call at the foot of this module.

    🔴 `dirty` is `None` -- not `False` -- whenever the commit is unknown. `False`
    asserts the tree matches its commit, and a build we could not identify
    supports no such assertion. Reporting absence as absence rather than as a
    reassuring default is the same rule the warning vocabulary follows.
    """
    try:
        declared = package_version("bankmachine")
    except PackageNotFoundError:
        # Running from a source tree that was never installed. The code is real
        # even when its metadata is not, so this is reported rather than raised.
        declared = "unknown"

    commit = _git("rev-parse", "--short", "HEAD")
    if commit is None:
        return BuildIdentity(version=declared, commit=None, dirty=None)

    # 🔴 Scoped to the package directory with `-- .`, not the whole repository.
    # The question is whether the CODE differs from its commit, and an edited
    # README does not change what this process runs. Repo-wide status would leave
    # `dirty` true through most of an ordinary working day, and a signal that is
    # always on is one nobody reads.
    #
    # `--porcelain` respects .gitignore, so this is uncommitted work rather than
    # editor litter. Untracked files count: a new module that is not in the
    # commit is still code this process can import, and the commit alone would
    # not account for it.
    status = _git("status", "--porcelain", "--", ".")
    return BuildIdentity(
        version=declared,
        commit=commit,
        dirty=None if status is None else bool(status),
    )


# 🔴 Captured HERE, at import, rather than left to the first caller. `lru_cache`
# alone would capture on first *call*, which leaves exactly the window this
# module exists to close: the process starts, the operator merges, and the first
# request then reports the merged commit while the process serves pre-merge code.
# Every artifact describing this says "at process start", and this line is what
# makes that true rather than aspirational. The cache still backs it, so a test
# can clear and re-capture deliberately.
_ = build_identity()
