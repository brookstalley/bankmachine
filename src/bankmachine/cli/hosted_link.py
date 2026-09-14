"""The hosted Link session, shared by the two commands that open one.

AC-1.1 rules out a local web server, so both enrollment and repair work the same
way: print a URL, let the operator complete it in a browser, and watch for the
result from here. What they do NOT share is *what counts as finished* --
enrollment waits for a public token to exchange, and a repair waits for the
Item's own error to clear, because update mode mints no token. So the waiting is
generic over the question and each command asks its own.

Its own module rather than a corner of `enroll.py` for a structural reason:
`enroll.py` already imports from `connections.py`, so `connections.py` cannot
import back. Anything both need lives below both.
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable

#: Countries the institution picker offers, in both modes. Configuration would be
#: premature: the roster is one operator's, and a second country is a config knob
#: the day someone needs one rather than a setting nobody has ever set.
#:
#: Shared rather than enrollment's, because an update-mode session takes the same
#: argument and two lists naming one roster is how they come to disagree.
LINK_COUNTRIES: tuple[str, ...] = ("US",)

#: How often a hosted session is polled. The thing being waited on is a human in
#: a browser, so this is paced for them rather than for the aggregator.
#:
#: The ceiling on the waiting is the hosted URL's own lifetime, which is why the
#: two are one number at every call site: polling past the point the URL can
#: still be used would report a timeout the operator could no longer act on.
POLL_INTERVAL_SECONDS = 3.0

#: The floor on `--timeout`. It is also the hosted URL's lifetime, so a value
#: below this is not a short wait -- it is a URL that expires before anyone could
#: use it, and an aggregator rejection rather than a fast local abandon.
MIN_HOSTED_WAIT_SECONDS = 30


def positive_seconds(raw: str) -> int:
    """A wait that is also the URL's lifetime, so it must be a value the vendor accepts.

    Before the two numbers were unified, a zero or negative timeout only
    shortened a local loop. It is now sent as `url_lifetime_seconds`, where it
    would come back as an aggregator rejection -- exit 2, "could not run" -- for
    what is really a mistyped argument.
    """
    try:
        seconds = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected a whole number of seconds, got {raw!r}"
        ) from None
    if seconds < MIN_HOSTED_WAIT_SECONDS:
        raise argparse.ArgumentTypeError(
            f"must be at least {MIN_HOSTED_WAIT_SECONDS} seconds; the hosted URL is live for "
            f"exactly this long and nobody completes a bank login faster"
        )
    return seconds


def print_invitation(url: str, *, what_it_does: str, timeout_seconds: int) -> None:
    """The deadline is printed here because here is where it can still be changed.

    A production bank login can mean an OAuth redirect to the bank's own site, a
    password reset, an SMS code and a device registration -- none of which the
    sandbox has. When the wait runs out the URL dies with it, and an operator who
    only learns the number afterwards learns it from an abandoned session.

    `what_it_does` completes "open this URL to ...", because the two callers are
    asking for different things and an operator repairing a connection should not
    be told they are linking an institution.
    """
    print()
    print(f"open this URL to {what_it_does}:")
    print()
    print(f"    {url}")
    print()
    print(
        f"waiting up to {timeout_seconds} seconds for you to finish; the URL expires with "
        f"the wait.",
        flush=True,
    )
    print(
        "  a bank login with OAuth, MFA or a device registration can take longer -- "
        "cancel and re-run with `--timeout SECONDS` for more time",
        flush=True,
    )


def await_hosted_session[T](poll: Callable[[], T | None], *, timeout_seconds: int) -> T | None:
    """Poll until `poll` answers, or until waiting stops being useful. Returns `None` on timeout.

    The deadline is a wall-clock budget rather than an attempt count so that a
    slow aggregator does not shorten the operator's window to finish -- the thing
    being waited on is a human in a browser, not a request.

    🔴 **Returns rather than raises, and the callers are why.** Enrollment reports
    an abandoned session by raising into its own `EnrollmentError` hierarchy;
    `connections` returns exit codes directly, as `list` and `retire` already do.
    A timeout raised here would force one of them into the other's idiom, and the
    message an operator reads differs anyway -- nothing was linked in one case, a
    connection is still degraded in the other.

    🔴 Polled at least once before the deadline is consulted, so a timeout of
    exactly the elapsed time cannot skip the check that would have succeeded.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        answer = poll()
        if answer is not None:
            return answer
        if time.monotonic() >= deadline:
            return None
        time.sleep(POLL_INTERVAL_SECONDS)
