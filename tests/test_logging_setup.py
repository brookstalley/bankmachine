"""Redaction at the formatter (AC-10.3) and the loud environment banner (AC-10.6)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

import pytest

from bankmachine.config import Config
from bankmachine.logging_setup import (
    APP_LOGGER,
    REDACTED,
    RedactingFormatter,
    configure_logging,
    get_logger,
    log_startup,
    redact,
    redact_free_text,
)


@pytest.fixture
def formatted() -> object:
    formatter = RedactingFormatter(fmt="%(message)s")

    def _format(message: str, *args: object) -> str:
        record = logging.LogRecord(
            name="bankmachine.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg=message,
            args=args,
            exc_info=None,
        )
        return formatter.format(record)

    return _format


@pytest.mark.parametrize(
    "secret",
    [
        # These must carry real credential SHAPES or they prove nothing about
        # redaction, so each declares itself to `test_no_credentials_tracked`,
        # which otherwise reports them as a leak -- correctly, on shape alone.
        "access-sandbox-8f2c1d4e-1111-2222-3333-abcdefabcdef",  # credential-shape: test vector
        "a" * 64,
        "sk_live_9aZq3XcV8bNm2LpO7rTyU1wE5dFgH6jK",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9abcdefghijklmnop",
    ],
)
def test_a_token_shaped_value_never_reaches_a_log_line(formatted: object, secret: str) -> None:
    line = formatted("syncing with token %s", secret)  # type: ignore[operator]
    assert secret not in line
    assert REDACTED in line


def test_a_labelled_credential_is_redacted_whatever_its_shape(formatted: object) -> None:
    line = formatted("client_secret=hunter2 remaining")  # type: ignore[operator]
    assert "hunter2" not in line
    assert "remaining" in line


def test_an_account_number_keeps_only_its_last_four(formatted: object) -> None:
    line = formatted("posting to account 4111111111119876")  # type: ignore[operator]
    assert "4111111111119876" not in line
    assert "****9876" in line


def test_an_exception_message_is_redacted_too() -> None:
    """Redaction at the formatter covers text that never passed through a call site."""
    formatter = RedactingFormatter(fmt="%(message)s")
    try:
        raise ValueError("token " + "b" * 40)
    except ValueError:
        record = logging.LogRecord(
            name="bankmachine.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=__import__("sys").exc_info(),
        )
    assert "b" * 40 not in formatter.format(record)


def test_ordinary_prose_survives_redaction() -> None:
    text = "applied migration 1 (create schema_version) at version 1"
    assert redact(text) == text


def test_the_environment_is_announced_loudly_at_startup(
    config: Config, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="bankmachine"):
        log_startup(config)
    records = [r for r in caplog.records if "environment=" in r.getMessage()]
    assert records, "startup did not announce the environment"
    assert records[0].levelno >= logging.WARNING
    assert "SANDBOX" in records[0].getMessage()


def test_production_is_announced_just_as_loudly(
    config: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """Neither environment is the quiet one: both accidents run in both directions."""
    from dataclasses import replace

    with caplog.at_level(logging.WARNING, logger="bankmachine"):
        log_startup(replace(config, environment="production"))
    assert any("PRODUCTION" in r.getMessage() for r in caplog.records)


def test_an_unusable_log_directory_does_not_take_the_process_down(
    config: Config, tmp_path: Path
) -> None:
    from dataclasses import replace

    blocked = tmp_path / "not-a-directory"
    blocked.write_text("", encoding="utf-8")
    configure_logging(replace(config, log_dir=blocked / "logs"))
    get_logger("test").info("still running")


def test_configure_logging_writes_to_the_configured_directory(config: Config) -> None:
    configure_logging(config)
    get_logger("test").warning("hello")
    logging.shutdown()
    log_file = config.log_dir / "bankmachine.log"
    assert log_file.exists()
    assert "hello" in log_file.read_text(encoding="utf-8")


def test_a_dunder_name_is_not_prefixed_twice() -> None:
    """🔴 `__name__` already starts with this package, and prefixing it doubles it.

    Every module outside `store/` that logs passes `__name__`, and the result
    was `bankmachine.bankmachine.connector.plaid.errors` -- which still logs,
    still routes to the same handlers, and reads as a typo in every line it
    writes. Nothing caught it because the only existing call here was a short
    label, which exercises the other branch.
    """
    assert get_logger("bankmachine.connector.plaid.errors").name == (
        "bankmachine.connector.plaid.errors"
    )
    assert get_logger(APP_LOGGER).name == APP_LOGGER
    # The control: a short label is still placed under the application root, or
    # the fix would have been "stop prefixing" rather than "stop double-prefixing".
    assert get_logger("store.raw").name == "bankmachine.store.raw"
    # A name that merely starts with the same letters is not the same package.
    assert get_logger("bankmachinery").name == "bankmachine.bankmachinery"


# --------------------------------------------------------------------------
# Free text: a line that mixes this store's structure with operator values
# --------------------------------------------------------------------------

#: Exactly the 32 characters the opaque rule blanks, and a real column here.
_LONG_NAME: Final = "source_investment_transaction_id"


def test_a_name_this_store_holds_survives_free_text_redaction() -> None:
    """Structure is what the opaque rule cannot recognise by shape.

    The identifier set is what tells the two apart, so a statement built
    entirely out of this store's own names comes back byte for byte.
    """
    text = f"SELECT {_LONG_NAME} FROM investment_transactions;"

    assert redact_free_text(text, {_LONG_NAME, "investment_transactions"}) == text


def test_free_text_still_blanks_a_token_that_names_nothing() -> None:
    """The value beside the column is not structure, and the column is."""
    token = "sbx-a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"  # credential-shape: test vector

    out = redact_free_text(f"SELECT '{token}' AS {_LONG_NAME};", {_LONG_NAME})

    assert token not in out
    assert REDACTED in out
    assert _LONG_NAME in out


def test_a_token_merely_containing_a_known_name_is_still_blanked() -> None:
    """The exemption is the whole run or nothing.

    Sparing a run because a known name sits inside it would let anything
    through that a caller could prefix onto a column name.
    """
    token = f"{_LONG_NAME}_9f3ac1d2"

    out = redact_free_text(f"WHERE x = '{token}'", {_LONG_NAME})

    assert token not in out
    assert REDACTED in out


def test_a_labelled_credential_is_blanked_even_when_it_names_a_column() -> None:
    """Only the opaque rule takes the exemption; the named-key rule never does.

    A value introduced as a credential is a credential whatever it looks like,
    and that includes looking exactly like something in this schema.
    """
    assert redact_free_text(f"access_token={_LONG_NAME}", {_LONG_NAME}) == (
        f"access_token={REDACTED}"
    )


def test_an_account_number_is_still_masked_in_free_text() -> None:
    """The digits rule is untouched: no identifier here is a run of digits."""
    out = redact_free_text("posting to account 4111111111119876", {_LONG_NAME})

    assert "4111111111119876" not in out
    assert "****9876" in out


def test_the_exemption_ignores_the_case_the_operator_typed() -> None:
    """SQL identifiers are case-insensitive, so a shouted column is the same column."""
    shouted = _LONG_NAME.upper()

    assert redact_free_text(f"SELECT {shouted};", {_LONG_NAME}) == f"SELECT {shouted};"


def test_with_no_names_free_text_redaction_is_the_formatter_rule_exactly() -> None:
    """A log line names no store, so nothing is spared there.

    This is what keeps the narrowing to the one surface that can supply the
    set: the formatter's own rule has to stay where it is.
    """
    text = f"SELECT {_LONG_NAME} FROM investment_transactions;"

    assert redact_free_text(text, ()) == redact(text)
    assert _LONG_NAME not in redact(text)
