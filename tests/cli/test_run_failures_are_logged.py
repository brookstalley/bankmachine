"""A failed run has to be visible in the log file, not just on stderr.

The scheduled sync is the reason. Nobody watches stderr on a launchd run, so the
log file is the only durable record of what happened -- and until these tests
existed it recorded a failed run and a run that found nothing to do identically.
For a product whose named primary failure mode is silent staleness (AC-ARCH.2),
those two outcomes looking alike in the durable record is the defect.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from bankmachine.cli import run
from bankmachine.config import Config


@pytest.fixture
def cli_env(config: Config, monkeypatch: pytest.MonkeyPatch) -> Iterator[Config]:
    monkeypatch.setenv("BANKMACHINE_DATASTORE_PATH", str(config.datastore_path))
    monkeypatch.setenv("BANKMACHINE_LOG_DIR", str(config.log_dir))
    monkeypatch.setenv("BANKMACHINE_KEYCHAIN_SERVICE", config.keychain_service)
    monkeypatch.setenv("BANKMACHINE_ENVIRONMENT", config.environment)
    monkeypatch.setenv("BANKMACHINE_CONFIG", str(config.datastore_path.parent / "absent.toml"))
    yield config
    logging.shutdown()


def _log_text(config: Config) -> str:
    logging.shutdown()
    log_file = config.log_dir / "bankmachine.log"
    return log_file.read_text(encoding="utf-8") if log_file.exists() else ""


def test_a_failed_run_leaves_an_error_record_in_the_log(cli_env: Config) -> None:
    """`connector check` against a datastore that was never initialised."""
    assert run(["connector", "check"]) == 2

    text = _log_text(cli_env)
    assert "ERROR" in text, "a failed run wrote no error record to the log file"
    assert "connector" in text, "the record does not say which command failed"


def test_a_no_op_run_is_distinguishable_from_a_failed_one(cli_env: Config) -> None:
    """The acceptance criterion is the *difference*, so assert on the difference.

    `store status` on a missing datastore exits 1 -- it ran fine and the answer
    is "unhealthy", which is not a failure. If both outcomes wrote ERROR the log
    would be no more use than the exit code alone.
    """
    assert run(["store", "status"]) == 1

    assert "ERROR" not in _log_text(cli_env), "a healthy no-op run logged an error"


def test_an_unexpected_failure_reaches_the_log_and_still_propagates(
    cli_env: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The crash case is the one most likely to leave no other trace.

    It must gain a log record without changing what the caller sees: the
    exception still escapes, so the exit status and the interactive traceback
    are untouched.
    """
    from bankmachine.cli import store as store_commands

    def explode(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("bad-thing-happened")

    monkeypatch.setattr(store_commands, "cmd_status", explode)

    with pytest.raises(RuntimeError, match="bad-thing-happened"):
        run(["store", "status"])

    text = _log_text(cli_env)
    assert "failed unexpectedly" in text
    assert "RuntimeError" in text, "an unexpected failure logged no traceback"
