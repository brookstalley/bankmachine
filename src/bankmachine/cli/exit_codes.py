"""The CLI's exit codes, which are a contract rather than ergonomics.

🔴 `0` success, `1` ran and found a problem, `2` could not run. **The 1/2 split
is not collapsible** (`api-contract.md` § Direction): the scheduled job invokes
this CLI, so these are a machine interface. Collapsing them makes a broken
scheduler indistinguishable from a degraded feed -- which is this product's
primary failure mode arriving through the operational door, and the one place
where an ops shortcut reproduces the exact bug the product exists to prevent.

Their own module so that a command can name one without importing the package
that imports the command. The alternative was a deferred import inside each
handler, which hides a cycle rather than removing one.
"""

from __future__ import annotations

from typing import Final

EXIT_OK: Final = 0
EXIT_UNHEALTHY: Final = 1
EXIT_ERROR: Final = 2
