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
from conftest import use_cli_env


@pytest.fixture
def cli_env(config: Config, monkeypatch: pytest.MonkeyPatch) -> Iterator[Config]:
    use_cli_env(monkeypatch, config)
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


def test_a_failure_is_reported_to_the_terminal_exactly_once(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 Making the log honest must not make the terminal worse.

    `configure_logging` attaches a stderr handler, so a log record emitted
    beside the CLI's own `bankmachine: ...` line would print a second
    timestamped copy of the same sentence directly under the first. The file
    wants the record; the terminal already had one.
    """
    assert run(["connector", "check"]) == 2

    err = capsys.readouterr().err
    assert err.count("not ready") == 1, f"the failure was reported more than once:\n{err}"


def test_a_refusal_is_not_recorded_as_a_failure(cli_env: Config) -> None:
    """The `EnrollmentError` arm is the one place a non-zero code means "refused".

    Its subclasses carry EXIT_UNHEALTHY for "ran and found a problem" — the
    roster is full, the operator walked away. Recording those at ERROR would
    conflate exactly the two outcomes this logging exists to separate, which
    would leave the log no more use than the exit code it duplicates.
    """
    from bankmachine.cli import enroll as enroll_commands
    from bankmachine.cli.enroll import EnrollmentError
    from bankmachine.cli.exit_codes import EXIT_UNHEALTHY

    class OperatorWalkedAwayError(EnrollmentError):
        exit_code: int = EXIT_UNHEALTHY

    def refuse(*_args: object, **_kwargs: object) -> int:
        raise OperatorWalkedAwayError("the operator did not finish linking")

    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as patch:
        patch.setattr(enroll_commands, "cmd_enroll", refuse)
        assert run(["enroll", "--yes"]) == EXIT_UNHEALTHY

    text = _log_text(cli_env)
    assert "refused" in text, "the refusal left no record at all"
    assert "ERROR" not in text, "a refusal was recorded as a failure"


def test_the_new_log_line_cannot_carry_a_credential_into_the_file(
    cli_env: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 Logging a failure means logging an exception message someone else wrote.

    The existing formatter test covers a traceback attached as `exc_info`. This
    covers the other half and the one this file introduced: an exception passed
    as a `%s` argument, interpolated by `record.getMessage()` before redaction
    sees it. Both halves must be scrubbed, and only one of them was exercised.

    Writing failures to a durable file is worth nothing if the file becomes the
    place credentials end up.
    """
    from bankmachine.cli import store as store_commands
    from bankmachine.store.connection import StoreError

    leaked = "a" * 40

    def explode(*_args: object, **_kwargs: object) -> int:
        raise StoreError(f"could not open the datastore with access_token={leaked}")

    monkeypatch.setattr(store_commands, "cmd_status", explode)

    assert run(["store", "status"]) == 2

    text = _log_text(cli_env)
    assert "ERROR" in text, "the failure did not reach the log at all"
    assert leaked not in text, "a credential in an exception message reached the log file"


def test_an_unexpected_failure_is_could_not_run_rather_than_found_a_problem(
    cli_env: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 A crash used to escape, and an escaping exception exits `1`.

    `1` is the code reserved for a command that ran to the end and found a
    problem — one whose output can be trusted as far as it goes. A crash means
    the command did not finish and nothing it printed can be relied on. To the
    scheduled job, which reads the code and nothing else, those two were the same
    fact. `2` is the one that already means "could not run".

    The crash case is also the one most likely to leave no other trace, so the
    two records it used to leave must survive the change: the traceback in the
    log file, which is all a scheduled run leaves behind, and the traceback on
    stderr, which is what a developer running this by hand reads.
    """
    from bankmachine.cli import store as store_commands

    def explode(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("bad-thing-happened")

    monkeypatch.setattr(store_commands, "cmd_status", explode)

    assert run(["store", "status"]) == 2, "a crash was reported as a run that found a problem"

    err = capsys.readouterr().err
    assert "bad-thing-happened" in err, "the developer lost the traceback"
    assert "RuntimeError" in err

    text = _log_text(cli_env)
    assert "failed unexpectedly" in text
    assert "RuntimeError" in text, "an unexpected failure logged no traceback"
