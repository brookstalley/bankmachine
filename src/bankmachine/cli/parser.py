"""The argument parser that does not echo credentials back.

Its own module for the same reason `exit_codes` is: a command needs to name
this class, and importing the package that imports the command is a cycle.
"""

from __future__ import annotations

import argparse
from typing import NoReturn, TypeVar

from bankmachine.logging_setup import redact

#: The parser class a `_SubParsersAction` hands back, left open.
#:
#: `add_subparsers(parser_class=...)` decides it at the call site, and
#: `_SubParsersAction` is INVARIANT in it -- so a command module that named one
#: concrete class would reject every caller that chose the other, and the CLI and
#: the tests choose differently. The modules only ever call `add_parser`, so they
#: are genuinely agnostic to which class comes back and this says exactly that.
AnyParser = TypeVar("AnyParser", bound=argparse.ArgumentParser)


class RedactingParser(argparse.ArgumentParser):
    """An `ArgumentParser` that does not echo credentials back at the operator.

    🔴 argparse is the one place in this product that prints raw user input
    without passing it through the log formatter -- `invalid choice: '<value>'`,
    `unrecognized arguments: <value>` -- and it does so on stderr BEFORE
    `configure_logging` has installed the redaction, so the formatter's guard is
    not merely bypassed, it does not exist yet.

    That matters because an operator CAN type a secret at a command line: the
    `store key` verbs prompt for the datastore key precisely so it never lands
    in shell history, and the mistake they are guarding against is exactly the
    one that reaches this code path. Reprinting it onto a stderr that a
    scheduled runner captures is the product repeating a leak the operator has
    already made.

    Redaction is by credential SHAPE, reusing the same `redact` the formatter
    uses, so this is the existing norm applied at a surface it had not reached
    rather than a second, divergent rule. It over-redacts by design: a
    32-character path segment in a usage error is blanked along with the keys.
    """

    #: Said whenever a usage error turned out to be carrying a credential. The
    #: redaction keeps the value off this stderr; it does nothing about the copy
    #: already in the operator's shell history, and only they can clear that.
    REMEDIATION = (
        "one of those arguments looked like a credential and was not repeated back. "
        "If it was a secret, it is in your shell history now -- clear it there. "
        "No command in this product takes a datastore key as an argument; the ones "
        "that need it prompt for it."
    )

    def error(self, message: str) -> NoReturn:
        scrubbed = redact(message)
        if scrubbed != message:
            # 🔴 Redaction alone leaves the operator worse off than a plain
            # error would: they see `[REDACTED]`, learn that something they
            # typed was sensitive, and are told nothing about the copy sitting
            # in their history. The remediation fires off the fact that the
            # scrub CHANGED something, so it reaches every command and every
            # argument shape rather than the handful anybody thought to guard.
            scrubbed = f"{scrubbed}\n{self.REMEDIATION}"
        super().error(scrubbed)
