"""Redaction at the formatter (AC-10.3) and the loud environment banner (AC-10.6)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from bankmachine.config import Config
from bankmachine.logging_setup import (
    REDACTED,
    RedactingFormatter,
    configure_logging,
    get_logger,
    log_startup,
    redact,
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
