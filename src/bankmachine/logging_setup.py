"""Logging: redaction at the formatter, and the loud environment banner.

Redaction lives in the formatter rather than at each call site because AC-10.3
is a property of every log line, and a rule enforced at call sites is a rule
every future author has to remember. The formatter is the last thing every
record passes through, so a secret that reaches a handler has already been
scrubbed no matter which module logged it.

The patterns are shape-based and name no provider: the engine is
provider-agnostic, and a redaction rule keyed to one aggregator's token prefix
would silently stop redacting the day a second one is added.

They over-redact, and that is the chosen direction. A filesystem path holding a
32-character segment is blanked along with the tokens, which costs a little log
legibility; the alternative -- requiring high entropy before redacting -- trades
that back for the chance of a real token slipping through. Under the documented
default paths no ordinary path is long enough in one segment to trip it.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Final

from bankmachine.config import Config

REDACTED: Final = "[REDACTED]"

#: The root of the application's logger tree; one handler set serves everything under it.
APP_LOGGER: Final = "bankmachine"

#: Anything introduced as a credential, whatever its value looks like.
_SENSITIVE_KEY = re.compile(
    r"""(?ix)
    \b (
        (?: access[_-]?token | refresh[_-]?token | client[_-]?secret | client[_-]?id
          | api[_-]?key | secret | password | passwd | token | key )
    )
    (\s* [=:] \s* ) (?: ["'] )? ( [^\s"',;}\]]+ )
    """
)

#: Opaque high-entropy strings -- tokens, hex keys, base64 blobs -- whatever they
#: were labelled. 32 is chosen so a 64-character SQLCipher key is always caught
#: and ordinary prose never is.
_OPAQUE = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")

#: Account numbers. The last four are explicitly acceptable (AC-10.3), so they
#: are what survives -- a fully redacted number would make logs useless for the
#: one question an operator actually asks them.
_LONG_DIGITS = re.compile(r"\b\d{8,}\b")


def redact(text: str) -> str:
    """Scrub credentials and account numbers from a line of text."""
    text = _SENSITIVE_KEY.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = _OPAQUE.sub(REDACTED, text)
    return _LONG_DIGITS.sub(lambda m: f"****{m.group(0)[-4:]}", text)


class RedactingFormatter(logging.Formatter):
    """A formatter that scrubs its own output before anyone can write it."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class _NotAlreadyOnStderr(logging.Filter):
    """Keeps records off stderr that the caller has already written there itself.

    🔴 **The stderr handler and the file handler want different things, and
    without this only one of them can be right.** The CLI prints its own
    `bankmachine: <message>` line for a human, and separately needs the failure
    in the log file so a scheduled run leaves a durable record. Logging normally
    puts a second, timestamped copy of the same sentence on stderr directly under
    the first -- so making the file honest made the terminal worse.

    A record marked `file_only` is one whose author has taken responsibility for
    the stderr copy. It is not a general severity filter: everything else,
    including the startup banner, still reaches both.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not getattr(record, "file_only", False)


def configure_logging(config: Config, *, level: int = logging.INFO) -> None:
    """Attach a redacting stderr handler, and a file handler when the log dir is usable.

    Logging failures never take the process down: a product that refuses to run
    because it could not open a log file has converted an observability problem
    into an outage.
    """
    formatter = RedactingFormatter(
        fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    root = logging.getLogger(APP_LOGGER)
    root.setLevel(level)
    for existing in list(root.handlers):
        root.removeHandler(existing)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    stream.addFilter(_NotAlreadyOnStderr())
    root.addHandler(stream)

    try:
        config.log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(config.log_dir / f"{APP_LOGGER}.log", encoding="utf-8")
    except OSError as exc:
        root.warning(
            "log directory %s is unusable, logging to stderr only: %s", config.log_dir, exc
        )
    else:
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    """A logger under the application's root, so one handler set serves everything.

    🔴 **Accepts a `__name__` as well as a bare label**, because `__name__` is
    what every Python author reaches for and it already starts with the
    application's own package. Prefixing it blindly produced
    `bankmachine.bankmachine.connector.plaid.errors` -- which still logs, still
    routes to the same handlers, and reads as a typo in every line it writes.
    The two new modules that pass `__name__` were the first to do so, so this
    surfaced only once something outside `store/` started logging.
    """
    if name == APP_LOGGER or name.startswith(f"{APP_LOGGER}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{APP_LOGGER}.{name}")


def log_startup(config: Config) -> None:
    """Announce the environment at every startup (AC-10.6).

    Both environments are announced at WARNING, not just production. The
    accident this guards against runs in both directions -- fixture data into
    the real datastore, and a real sync that quietly went to the sandbox store
    and looks like it did nothing -- so neither state is the quiet one.
    """
    logger = get_logger(APP_LOGGER)
    logger.warning(
        "environment=%s datastore=%s",
        config.environment.upper(),
        _display_path(config.datastore_path),
    )


def _display_path(path: Path) -> str:
    """A path with the operator's home elided -- logs are pasted into bug reports."""
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)
