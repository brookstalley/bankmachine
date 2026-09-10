"""The argument parser that does not echo credentials back.

Its own module for the same reason `exit_codes` is: a command needs to name
this class, and importing the package that imports the command is a cycle.
"""

from __future__ import annotations

import argparse

from bankmachine.logging_setup import redact


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

    def error(self, message: str) -> None:
        super().error(redact(message))
