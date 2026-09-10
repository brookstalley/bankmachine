"""The CLI's exit codes, which are a contract rather than ergonomics.

🔴 `0` success, `1` ran and found a problem, `2` could not run, `75` ran and did
not finish. **The 1/2 split is not collapsible** (`api-contract.md` § Direction):
the scheduled job invokes this CLI, so these are a machine interface. Collapsing
them makes a broken scheduler indistinguishable from a degraded feed -- which is
this product's primary failure mode arriving through the operational door, and
the one place where an ops shortcut reproduces the exact bug the product exists
to prevent.

🔴 `75` is that same argument one step further, and an ADDITION to the vocabulary
rather than a collapse of it. A sync whose backfill is still arriving has neither
succeeded nor found a problem, and the exit code is the ONLY channel a scheduled
runner reads: prose on a terminal nobody is watching cannot tell it to come back.
`EX_TEMPFAIL` from `sysexits.h` rather than a fourth small integer, because 75
already means "temporary failure, retry" to operational tooling that reads exit
codes at all.

Their own module so that a command can name one without importing the package
that imports the command. The alternative was a deferred import inside each
handler, which hides a cycle rather than removing one.
"""

from __future__ import annotations

from typing import Final

EXIT_OK: Final = 0
EXIT_UNHEALTHY: Final = 1
EXIT_ERROR: Final = 2

#: Ran, nothing is wrong, and more is still owed -- come back. One code rather
#: than two, because splitting "the history is complete" from "it is still
#: arriving" would encode an internal distinction the caller cannot act on
#: differently: either way the answer is run again.
EXIT_RUN_AGAIN: Final = 75
