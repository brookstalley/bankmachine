"""`bankmachine sync shell` end to end, over a real encrypted datastore.

The shell is the product's only surface that runs operator-supplied SQL, so
these are the tests where the two read-role norms are load-bearing rather than
theoretical. The one this command exists to get right is the checkpoint pair at
the bottom: it holds the shell at its prompt -- genuinely idle, between
statements, the way it sits overnight -- and measures whether a writer can move
every WAL frame past it.
"""

from __future__ import annotations

import contextlib
import io
import threading
from collections.abc import Iterator
from typing import Final

import pytest

from bankmachine.cli import run, sync
from bankmachine.cli.sync import run_shell
from bankmachine.config import Config
from bankmachine.store import connection
from bankmachine.store.connection import writer

#: Long enough that a wedged shell fails the test instead of hanging the suite.
_TIMEOUT: Final = 15.0


class _Script:
    """A stdin that feeds prepared lines and then ends."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)

    def isatty(self) -> bool:
        return False

    def readline(self) -> str:
        return f"{self._lines.pop(0)}\n" if self._lines else ""


def _run(config: Config, lines: list[str]) -> str:
    out = io.StringIO()
    assert run_shell(config, stdin=_Script(lines), stdout=out) == 0
    return out.getvalue()


# --------------------------------------------------------------------------
# The acceptance criterion: an operator opens it, runs a query, reads the result
# --------------------------------------------------------------------------


def test_a_query_runs_and_returns_rows(initialized_config: Config) -> None:
    with writer(initialized_config) as w:
        w.execute("CREATE TABLE probe (label TEXT, amount_minor INTEGER)")
        w.execute("INSERT INTO probe VALUES ('rent', 150000000)")

    out = _run(initialized_config, ["SELECT label, amount_minor FROM probe;"])

    assert "label" in out
    assert "rent" in out
    assert "(1 row)" in out
    # Money is an INTEGER of minor units here, and this one is nine digits. It
    # must survive intact: an account-number rule that also blanks a six-figure
    # balance has redacted the number the operator opened the shell to read.
    assert "150000000" in out


def test_dot_tables_lists_the_real_schema(initialized_config: Config) -> None:
    out = _run(initialized_config, [".tables"])

    assert "accounts" in out
    assert "raw_responses" in out
    assert "sqlite_" not in out


def test_dot_schema_prints_ddl_for_one_table(initialized_config: Config) -> None:
    out = _run(initialized_config, [".schema accounts"])

    assert "CREATE TABLE" in out
    assert "balance_class" in out


def test_dot_schema_shows_identifiers_the_value_rule_would_blank(
    initialized_config: Config,
) -> None:
    """Schema text is structure, so the redactor does not run over it.

    These names are all longer than the 32-character run `_OPAQUE` blanks, and
    a short-name assertion cannot see the difference -- which is why the first
    version of this test passed while `.schema` was printing
    `CREATE UNIQUE INDEX [REDACTED]`.
    """
    out = _run(initialized_config, [".schema connections", ".schema investment_transactions"])

    assert "[REDACTED]" not in out
    assert "connections_one_live_per_institution" in out  # 36 characters
    assert "source_investment_transaction_id" in out  # exactly 32


def test_a_long_opaque_value_in_a_row_is_still_redacted(initialized_config: Config) -> None:
    """The other half of the split: values keep the full rule, cost included.

    A 64-hex digest is blanked. That is the chosen direction -- an unlabelled
    token in a row would otherwise reach the terminal -- and this pins it so a
    later reader sees a decision rather than an oversight.
    """
    digest = "9f" * 32
    with writer(initialized_config) as w:
        w.execute("CREATE TABLE probe (body_sha256 TEXT)")
        w.execute("INSERT INTO probe VALUES (?)", (digest,))

    out = _run(initialized_config, ["SELECT body_sha256 FROM probe;"])

    assert digest not in out
    assert "[REDACTED]" in out


def test_an_unknown_dot_command_is_reported_rather_than_ignored(
    initialized_config: Config,
) -> None:
    out = _run(initialized_config, [".nonsense"])

    assert "unknown command .nonsense" in out


def test_an_error_does_not_end_the_session(initialized_config: Config) -> None:
    """A typo costs the statement, never the session.

    The shell is what an operator reaches for when something is already wrong;
    dropping them back to a re-authentication because they mistyped a column
    name is the opposite of usable under pressure. The error is still shown --
    silence is the one outcome this project disallows.
    """
    out = _run(initialized_config, ["SELECT * FROM no_such_table;", "SELECT 1 AS ok;"])

    assert "error:" in out
    assert "no_such_table" in out
    assert "ok" in out


def test_a_statement_may_span_lines(initialized_config: Config) -> None:
    out = _run(initialized_config, ["SELECT", "  1 AS ok", ";"])

    assert "ok" in out
    assert "(1 row)" in out


def test_input_that_ends_mid_statement_does_not_run_it(initialized_config: Config) -> None:
    out = _run(initialized_config, ["SELECT 1 AS ok"])

    assert "input ended mid-statement" in out
    assert "(1 row)" not in out


def test_a_blob_is_summarised_rather_than_printed(initialized_config: Config) -> None:
    """`raw_responses.body_gzip` is the one that comes up in practice."""
    with writer(initialized_config) as w:
        w.execute("CREATE TABLE blobby (payload BLOB)")
        w.execute("INSERT INTO blobby VALUES (?)", (b"\x1f\x8b" + b"\x00" * 400,))

    out = _run(initialized_config, ["SELECT payload FROM blobby;"])

    assert "<blob, 402 bytes>" in out
    assert "\x1f" not in out


# --------------------------------------------------------------------------
# Norm 2: the read role refuses writes, and the refusal is not a session flag
# --------------------------------------------------------------------------


def test_a_write_is_refused_in_the_read_role(initialized_config: Config) -> None:
    out = _run(initialized_config, ["CREATE TABLE nope (a INTEGER);"])

    assert "error:" in out
    assert any(word in out.lower() for word in ("readonly", "read-only", "attempt to write"))


def test_a_write_is_still_refused_after_pragma_query_only_off(
    initialized_config: Config,
) -> None:
    """The norm's whole point, at the surface that can actually type it.

    `query_only` is a reversible session flag and this prompt is the one place
    an operator can turn it off. The refusal has to live somewhere SQL cannot
    reach -- `mode=ro` at the file handle -- so this asserts the flag really did
    flip and that the write failed anyway. Asserting only the failure would pass
    just as well against a shell where the PRAGMA silently did nothing.
    """
    out = _run(
        initialized_config,
        [
            "PRAGMA query_only = OFF;",
            "PRAGMA query_only;",
            "CREATE TABLE nope (a INTEGER);",
        ],
    )

    lines = [line.strip() for line in out.splitlines()]
    assert "0" in lines, "query_only did not actually turn off, so the refusal proves nothing"
    assert "error:" in out
    assert any(word in out.lower() for word in ("readonly", "read-only", "attempt to write"))
    with connection.reader(initialized_config) as conn:
        present = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'nope'").fetchone()[
            0
        ]
    assert present == 0


# --------------------------------------------------------------------------
# AC-10.3: what comes out of the shell is redacted
# --------------------------------------------------------------------------


def test_output_redacts_tokens_and_account_numbers_but_keeps_masks(
    initialized_config: Config,
) -> None:
    """Expected output is written out literally rather than computed.

    Running the redactor to produce what the test expects would assert only
    that the function equals itself. These strings are what an operator should
    see, written independently of the code that produces them.
    """
    token = "sbx-a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"  # credential-shape: test vector
    with writer(initialized_config) as w:
        w.execute("CREATE TABLE probe (note TEXT, mask TEXT, number TEXT)")
        w.execute(
            "INSERT INTO probe VALUES (?, ?, ?)",
            (f"access_token={token}", "1234", "999999999999"),
        )

    out = _run(initialized_config, ["SELECT note, mask, number FROM probe;"])

    assert token not in out
    assert "access_token=[REDACTED]" in out
    # The last four are explicitly acceptable, and are the thing the operator
    # actually reads an account row for.
    assert "999999999999" not in out
    assert "****9999" in out
    assert "1234" in out


def test_an_error_message_quoting_a_token_is_redacted_too(initialized_config: Config) -> None:
    """SQLite quotes the offending value back; a mistyped token is still a token."""
    token = "sbx-a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"  # credential-shape: test vector

    out = _run(initialized_config, [f"SELECT {token};"])

    assert "error:" in out
    assert token not in out


def test_a_token_typed_into_the_shell_is_redacted_in_the_transcript(
    initialized_config: Config,
) -> None:
    """A piped session echoes its input, and the echo is output like any other.

    The transcript is the thing here most likely to be committed or pasted into
    a bug report -- the operator-verification entry for this command is one --
    so a token typed into a query must not survive in it.
    """
    token = "sbx-a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"  # credential-shape: test vector

    out = _run(initialized_config, [f"SELECT '{token}' AS t;"])

    assert token not in out
    # Once for the echoed statement, once for the value it returned.
    assert out.count("[REDACTED]") >= 2


# --------------------------------------------------------------------------
# The norm this chunk exists to get right: nothing is pinned between statements
# --------------------------------------------------------------------------


class _Paced:
    """A stdin that holds the shell at its prompt until the test releases it.

    Blocking inside `readline` is the only way to observe the shell *between*
    statements. That is the state the norm is about -- a prompt sitting idle
    overnight, with the nightly sync running against the same file.
    """

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)
        self._at_prompt = threading.Event()
        self._resume = threading.Event()

    def isatty(self) -> bool:
        return False

    def readline(self) -> str:
        self._at_prompt.set()
        if not self._resume.wait(timeout=_TIMEOUT):
            raise AssertionError("the shell was left blocked at its prompt")
        self._resume.clear()
        return f"{self._lines.pop(0)}\n" if self._lines else ""

    def wait_at_prompt(self) -> None:
        assert self._at_prompt.wait(timeout=_TIMEOUT), "the shell never reached its prompt"
        self._at_prompt.clear()

    def resume(self) -> None:
        self._resume.set()


@contextlib.contextmanager
def _shell_idle_after(config: Config, lines: list[str]) -> Iterator[io.StringIO]:
    """Run every line, then yield with the shell genuinely idle at its prompt."""
    paced = _Paced(lines)
    out = io.StringIO()
    exit_codes: list[int] = []

    def _drive() -> None:
        exit_codes.append(run_shell(config, stdin=paced, stdout=out))

    thread = threading.Thread(target=_drive, daemon=True)
    thread.start()
    try:
        for _ in lines:
            paced.wait_at_prompt()
            paced.resume()
        paced.wait_at_prompt()
        yield out
    finally:
        paced.resume()
        thread.join(timeout=_TIMEOUT)

    assert not thread.is_alive(), "the shell did not exit at end of input"
    assert exit_codes == [0]


def _checkpoint_past_the_shell(config: Config) -> tuple[int, int]:
    """Append frames while the shell sits idle, then try to move them all.

    Returns (frames in the log, frames checkpointed). The writer opens after the
    shell is already idle, so every frame it appends is one the shell would pin
    if its snapshot outlived the statement that opened it.
    """
    with writer(config) as w:
        _fill_wal(w)
        busy, log_frames, checkpointed = w.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
    assert busy == 0
    assert log_frames > 0, "the WAL held no frames, so this proved nothing"
    return int(log_frames), int(checkpointed)


def test_the_shell_pins_no_snapshot_while_it_sits_at_its_prompt(
    initialized_config: Config,
) -> None:
    """A passive checkpoint moves every frame past an idle shell.

    A shell left open overnight is open during the nightly sync, and a
    read-role handle takes no writer lock, so nothing else serialises the two.
    The measured failure this guards against is 0 frames of 93 moved.
    """
    with writer(initialized_config) as w:
        _create_filler(w)

    with _shell_idle_after(initialized_config, ["SELECT COUNT(*) FROM wal_filler;"]) as out:
        log_frames, checkpointed = _checkpoint_past_the_shell(initialized_config)

    assert checkpointed == log_frames
    assert "(1 row)" in out.getvalue()


def test_a_transaction_typed_at_the_prompt_is_released_before_the_prompt_returns(
    initialized_config: Config,
) -> None:
    """The operator can type BEGIN. The prompt still comes back holding nothing.

    This is why the release is a property of the connection rather than a list
    of statements to watch for: `BEGIN` opens a transaction, so does `SAVEPOINT`,
    and the shell asks the handle instead of parsing the input.
    """
    with writer(initialized_config) as w:
        _create_filler(w)

    with _shell_idle_after(
        initialized_config, ["BEGIN;", "SELECT COUNT(*) FROM wal_filler;"]
    ) as out:
        log_frames, checkpointed = _checkpoint_past_the_shell(initialized_config)

    assert checkpointed == log_frames
    assert "rolled back" in out.getvalue()


def test_the_checkpoint_probe_can_detect_a_shell_that_pinned_a_snapshot(
    initialized_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative control for the two tests above.

    A probe that only ever confirms what was expected is the one to distrust.
    This disables the release, holds the shell at its prompt inside an open
    transaction, and asserts the checkpoint starves -- so the passing tests
    above are distinguishing a released snapshot from a held one, rather than
    reporting that this harness cannot see a held one at all.
    """
    monkeypatch.setattr(sync, "_release_snapshot", lambda conn, out: None)

    with writer(initialized_config) as w:
        _create_filler(w)

    with _shell_idle_after(initialized_config, ["BEGIN;", "SELECT COUNT(*) FROM wal_filler;"]):
        log_frames, checkpointed = _checkpoint_past_the_shell(initialized_config)

    assert checkpointed < log_frames, (
        "a shell holding an open read transaction did not starve the checkpointer, so the "
        "tests above cannot tell a released snapshot from a held one"
    )


def test_the_cli_wires_the_shell_up(
    initialized_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`bankmachine sync shell` reaches the shell through the real parser."""
    monkeypatch.setenv("BANKMACHINE_DATASTORE_PATH", str(initialized_config.datastore_path))
    monkeypatch.setenv("BANKMACHINE_LOG_DIR", str(initialized_config.log_dir))
    monkeypatch.setenv("BANKMACHINE_KEYCHAIN_SERVICE", initialized_config.keychain_service)
    monkeypatch.setenv("BANKMACHINE_ENVIRONMENT", initialized_config.environment)
    monkeypatch.setenv(
        "BANKMACHINE_CONFIG", str(initialized_config.datastore_path.parent / "absent.toml")
    )
    monkeypatch.setattr("sys.stdin", _Script([".quit"]))

    assert run(["sync", "shell"]) == 0


# --------------------------------------------------------------------------


def _create_filler(conn: connection.Connection) -> None:
    """The table the checkpoint tests read, committed before the shell opens."""
    conn.execute("CREATE TABLE IF NOT EXISTS wal_filler (a INTEGER, pad TEXT)")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()


def _fill_wal(conn: connection.Connection) -> None:
    """Commit enough pages that a checkpoint has real frames to move."""
    conn.execute("BEGIN IMMEDIATE")
    conn.executemany(
        "INSERT INTO wal_filler (a, pad) VALUES (?, ?)",
        [(i, "x" * 512) for i in range(400)],
    )
    conn.execute("COMMIT")
