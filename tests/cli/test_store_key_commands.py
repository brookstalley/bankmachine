"""`bankmachine store key export / verify / import`, over a real encrypted datastore.

These commands are the product's answer to the one secret with no other source
(FR-12, AC-17.1-17.8). The properties under test are mostly REFUSALS, so each
one here is written to fail if the refusal stops happening -- a test that only
exercises the happy path would pass against a command that had lost its guard.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from bankmachine.cli import run
from bankmachine.config import Config
from bankmachine.secrets import (
    DatastoreKeyMissingError,
    delete_datastore_key,
    generate_datastore_key,
    get_datastore_key,
    set_datastore_key,
)
from bankmachine.store import connection


@pytest.fixture
def cli_env(config: Config, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("BANKMACHINE_DATASTORE_PATH", str(config.datastore_path))
    monkeypatch.setenv("BANKMACHINE_LOG_DIR", str(config.log_dir))
    monkeypatch.setenv("BANKMACHINE_KEYCHAIN_SERVICE", config.keychain_service)
    monkeypatch.setenv("BANKMACHINE_ENVIRONMENT", config.environment)
    monkeypatch.setenv("BANKMACHINE_CONFIG", str(config.datastore_path.parent / "absent.toml"))
    return config


@pytest.fixture
def initialized(cli_env: Config) -> Config:
    assert run(["store", "init"]) == 0
    return cli_env


@pytest.fixture
def outside_the_data_directory(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Somewhere an operator might actually keep an escrow file.

    NOT `tmp_path`: that is the datastore's own directory here, and the export
    refuses to write the plaintext key beside the ciphertext it decrypts.
    """
    return tmp_path_factory.mktemp("escrow")


def _answer_prompt(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    """Stand in for the operator typing at the no-echo prompt."""
    monkeypatch.setattr("bankmachine.cli.store.getpass", lambda _prompt: key)


def _stdout_is_a_terminal(monkeypatch: pytest.MonkeyPatch, *, terminal: bool) -> None:
    monkeypatch.setattr("bankmachine.cli.store.sys.stdout.isatty", lambda: terminal)


# --- AC-17.1: export renders to a terminal, and refuses anything else -------------


def test_export_renders_the_key_to_a_terminal(
    initialized: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stdout_is_a_terminal(monkeypatch, terminal=True)

    assert run(["store", "key", "export"]) == 0

    assert get_datastore_key(initialized) in capsys.readouterr().out


def test_export_refuses_a_stdout_that_is_not_a_terminal(
    initialized: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The refusal IS the command.

    Without it this is `security find-generic-password -w` with a nicer name: a
    key that can be redirected into a file or a pipe by accident has left the
    keychain incidentally, which is what AC-10.1 forbids.
    """
    _stdout_is_a_terminal(monkeypatch, terminal=False)

    assert run(["store", "key", "export"]) == 2

    captured = capsys.readouterr()
    assert get_datastore_key(initialized) not in captured.out
    assert get_datastore_key(initialized) not in captured.err
    assert "not a terminal" in captured.err


# --- AC-17.1 / AC-17.2: --to writes 0600, never overwrites, never defaults --------


def test_export_to_a_named_path_writes_a_file_only_the_operator_can_read(
    initialized: Config, outside_the_data_directory: Path
) -> None:
    destination = outside_the_data_directory / "escrow.key"

    assert run(["store", "key", "export", "--to", str(destination)]) == 0

    assert destination.read_text(encoding="utf-8").strip() == get_datastore_key(initialized)
    mode = stat.S_IMODE(destination.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {mode:o}"


def test_export_refuses_a_destination_that_already_exists(
    initialized: Config, outside_the_data_directory: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Same refusal `store backup` makes, for the same reason: never overwrite."""
    destination = outside_the_data_directory / "escrow.key"
    destination.write_text("something the operator cares about\n", encoding="utf-8")

    assert run(["store", "key", "export", "--to", str(destination)]) == 2

    assert destination.read_text(encoding="utf-8") == "something the operator cares about\n"
    assert get_datastore_key(initialized) not in capsys.readouterr().err


def test_export_writes_nothing_when_no_destination_is_named(
    initialized: Config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-17.2 -- there is no default destination, so nothing appears anywhere.

    The product never chooses a path. A default would be the product deciding on
    the run where the operator did not think about it.
    """
    key = get_datastore_key(initialized)
    _stdout_is_a_terminal(monkeypatch, terminal=True)

    assert run(["store", "key", "export"]) == 0

    # Asserted as "the key is in no file" rather than "no file appeared": that is
    # the property AC-17.2 is actually about, and a set-equality check over the
    # data directory would go amber on ordinary WAL churn instead.
    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert written, "nothing on disk at all -- this test would pass without checking anything"
    for path in written:
        assert key not in path.read_bytes().decode("utf-8", errors="ignore"), path


# --- AC-17.3: verify asks the datastore, not the keychain -------------------------


def test_verify_accepts_the_key_that_opens_the_datastore(
    initialized: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _answer_prompt(monkeypatch, get_datastore_key(initialized))

    assert run(["store", "key", "verify"]) == 0

    assert "MATCHES" in capsys.readouterr().out


def test_verify_rejects_a_well_formed_key_that_is_simply_a_different_key(
    initialized: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The failure this whole feature exists for.

    A transcription slip produces 64 characters of valid hex that pass every
    shape check the product makes and are simply a different key. Nothing before
    FR-12 could tell the operator that while it was still fixable.
    """
    other = generate_datastore_key()
    assert other != get_datastore_key(initialized)
    _answer_prompt(monkeypatch, other)

    assert run(["store", "key", "verify"]) == 1

    assert "DOES NOT MATCH" in capsys.readouterr().err


def test_verify_answers_when_the_keychain_entry_is_gone(
    initialized: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 The incident case, and the reason verification is not a keychain comparison.

    Keychain-entry-absent IS the emergency. A check that compares the candidate
    against the stored entry has nothing to compare against precisely then.
    """
    key = get_datastore_key(initialized)
    delete_datastore_key(initialized)
    _answer_prompt(monkeypatch, key)

    assert run(["store", "key", "verify"]) == 0

    assert "MATCHES" in capsys.readouterr().out


def test_verify_answers_when_the_schema_is_not_one_this_build_serves(
    initialized: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Restoring an older backup onto a newer build is a real path.

    The key is correct there. Refusing to say whether it works because the schema
    is old would fail the operator in the exact scenario the command exists for.
    """
    key = get_datastore_key(initialized)
    with connection.writer(initialized) as conn:
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (connection.SUPPORTED_SCHEMA_VERSION + 5, "2026-09-10T00:00:00+00:00"),
        )
    _answer_prompt(monkeypatch, key)

    assert run(["store", "key", "verify"]) == 0

    assert "MATCHES" in capsys.readouterr().out


def test_verify_says_a_malformed_candidate_is_malformed(
    initialized: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Not "does not open" -- that is true, useless, and misleading.

    SQLCipher treats anything that is not exactly 64 hex digits as a passphrase
    to run through its KDF, so an unchecked candidate with a typo'd length fails
    as an authentication error and is indistinguishable from a correct key
    offered against the wrong datastore.
    """
    _answer_prompt(monkeypatch, "not-a-key")

    assert run(["store", "key", "verify"]) == 2

    assert "9 characters" in capsys.readouterr().err


def test_verify_refuses_when_there_is_no_datastore_to_check_against(
    cli_env: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _answer_prompt(monkeypatch, generate_datastore_key())

    assert run(["store", "key", "verify"]) == 2

    assert "nothing to check a key against" in capsys.readouterr().err
    assert not cli_env.datastore_path.exists()


# --- AC-17.4: no verb takes a key as an argument ----------------------------------


@pytest.mark.parametrize("verb", ["verify", "import"])
def test_no_verb_accepts_a_key_on_the_command_line(
    initialized: Config, verb: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 An argument lands in shell history and in the process table.

    That is the failure AC-10.1 names and the reason the shell recipe was never
    an acceptable permanent answer. Fixing the export direction while
    reintroducing the same leak on the input direction would move the defect
    rather than close it.
    """
    key = get_datastore_key(initialized)

    assert run(["store", "key", verb, key]) == 2

    err = capsys.readouterr().err
    assert key not in err, "the product echoed the key back after refusing it"
    assert "shell history" in err


# --- AC-17.5: import verifies before it writes ------------------------------------


def test_import_restores_a_key_the_keychain_no_longer_holds(
    initialized: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = get_datastore_key(initialized)
    delete_datastore_key(initialized)
    _answer_prompt(monkeypatch, key)

    assert run(["store", "key", "import"]) == 0

    assert get_datastore_key(initialized) == key


def test_import_refuses_a_key_that_does_not_open_the_datastore(
    initialized: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 A typo must not be able to replace a working entry.

    `store init` refuses to mint a key for an existing datastore so a fresh key
    cannot hide "no key in the keychain" behind an authentication failure.
    Writing an unverified key here would reintroduce exactly that, from the
    other direction.
    """
    working = get_datastore_key(initialized)
    _answer_prompt(monkeypatch, generate_datastore_key())

    assert run(["store", "key", "import"]) == 2

    assert get_datastore_key(initialized) == working
    assert "NOT written to the keychain" in capsys.readouterr().err


def test_import_refuses_when_there_is_no_datastore_to_verify_against(
    cli_env: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unverifiable key is not stored, rather than stored on trust."""
    candidate = generate_datastore_key()
    _answer_prompt(monkeypatch, candidate)

    assert run(["store", "key", "import"]) == 2

    assert "nothing to check a key against" in capsys.readouterr().err
    with pytest.raises(DatastoreKeyMissingError):
        get_datastore_key(cli_env)


# --- AC-17.6: the secrecy guarantee holds across all three verbs -------------------


@pytest.mark.parametrize("verb", ["export", "verify", "import"])
def test_no_verb_puts_the_key_in_the_log(
    initialized: Config, monkeypatch: pytest.MonkeyPatch, verb: str
) -> None:
    """The guarantee that stops being structural the moment a caller prints.

    🔴 The negative control matters as much as the assertion: a log-absence check
    reading a file that was never written passes forever. This asserts the log
    exists and has content before it asserts what is not in it.
    """
    key = get_datastore_key(initialized)
    _stdout_is_a_terminal(monkeypatch, terminal=True)
    _answer_prompt(monkeypatch, key)

    run(["store", "key", verb])

    log_file = initialized.log_dir / "bankmachine.log"
    assert log_file.exists(), "no log file -- this test would pass without checking anything"
    contents = log_file.read_text(encoding="utf-8")
    assert contents.strip(), "empty log -- this test would pass without checking anything"
    assert key not in contents


def test_a_refused_export_names_neither_the_key_nor_a_way_to_get_it(
    initialized: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The refusal must not helpfully suggest the shell recipe it replaced."""
    _stdout_is_a_terminal(monkeypatch, terminal=False)

    run(["store", "key", "export"])

    err = capsys.readouterr().err
    assert get_datastore_key(initialized) not in err
    assert "find-generic-password" not in err


# --- AC-17.8: the round trip, end to end ------------------------------------------


def test_a_key_survives_export_and_import_onto_a_keychain_that_lost_it(
    initialized: Config, monkeypatch: pytest.MonkeyPatch, outside_the_data_directory: Path
) -> None:
    """Export -> the keychain forgets -> import -> the datastore opens again.

    This is the automated half of AC-17.8. It does NOT discharge the operator
    rehearsal: a restore into the production path, against a key read back from
    wherever the operator actually stored it, stays owed.
    """
    escrow = outside_the_data_directory / "escrow.key"
    assert run(["store", "key", "export", "--to", str(escrow)]) == 0
    stored = escrow.read_text(encoding="utf-8").strip()

    # The machine the operator is recovering onto: the datastore is there, the
    # keychain entry is not. Asserted, so a later change that leaves the key in
    # place cannot make the rest of this test pass for the wrong reason.
    delete_datastore_key(initialized)
    with pytest.raises(DatastoreKeyMissingError):
        get_datastore_key(initialized)

    _answer_prompt(monkeypatch, stored)
    assert run(["store", "key", "import"]) == 0

    assert run(["store", "status"]) == 0
    assert get_datastore_key(initialized) == stored


# --- the probe itself --------------------------------------------------------------


def test_opens_with_does_not_answer_for_a_datastore_that_is_not_there(
    cli_env: Config,
) -> None:
    """Neither True nor False -- absence is not an answer about a key."""
    with pytest.raises(connection.DatastoreMissingError):
        connection.opens_with(cli_env, generate_datastore_key())


def test_opens_with_never_consults_the_keychain(
    initialized: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 The property that makes this a candidate check rather than a self-check.

    If the probe fell back to `get_datastore_key`, it would report MATCHES for
    any candidate whenever the keychain happened to hold the right key -- which
    is every case except the one the command exists for.
    """
    key = get_datastore_key(initialized)

    def explode(_config: Config) -> str:
        raise AssertionError("the probe read the keychain instead of using the candidate")

    monkeypatch.setattr("bankmachine.store.connection.get_datastore_key", explode)

    assert connection.opens_with(initialized, key) is True
    assert connection.opens_with(initialized, generate_datastore_key()) is False


def test_a_restored_key_is_stored_lowercase_whatever_the_operator_typed(
    initialized: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A password manager may hand back uppercase hex; it is the same key."""
    key = get_datastore_key(initialized)
    delete_datastore_key(initialized)
    _answer_prompt(monkeypatch, key.upper())

    assert run(["store", "key", "import"]) == 0

    assert get_datastore_key(initialized) == key.lower()


def test_a_key_with_surrounding_whitespace_is_accepted(
    initialized: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pasting from a password manager brings a newline more often than not."""
    _answer_prompt(monkeypatch, f"  {get_datastore_key(initialized)}\n")

    assert run(["store", "key", "verify"]) == 0

    assert "MATCHES" in capsys.readouterr().out


def test_set_datastore_key_rejects_a_value_the_probe_would_have_accepted(
    initialized: Config,
) -> None:
    """The seam still validates, so the CLI's check is a better message, not the only one."""
    from bankmachine.secrets import SecretsError

    with pytest.raises(SecretsError):
        set_datastore_key(initialized, "0x" + "a" * 62)


# --- AC-17.7: one instruction, one answer, on every surface ------------------------


@pytest.mark.parametrize("argv", [["store", "init"], ["store", "status"]])
def test_no_operator_facing_command_hands_out_the_shell_recipe(
    initialized: Config,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    """🔴 The recipe put the key in shell history, which AC-10.1 forbids.

    It shipped because the product had no path of its own and an instruction
    with no route is worse than a leaky one. FR-12 removed that excuse. Asserted
    per surface rather than by grepping the source, because what AC-17.7
    constrains is what the operator is TOLD, and a source scan would also fire
    on the comment that explains why the recipe is gone.
    """
    assert run(argv) == 0

    captured = capsys.readouterr()
    assert "find-generic-password" not in captured.out
    assert "find-generic-password" not in captured.err


def test_every_surface_that_mentions_the_key_names_the_same_two_commands(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """One instruction, one answer.

    Three surfaces printed the recipe and a fourth printed no command at all, so
    which answer the operator met depended on which command they happened to run.
    """
    assert run(["store", "init"]) == 0
    minting = capsys.readouterr().out
    assert run(["store", "status"]) == 0
    standing = capsys.readouterr().out

    for surface in (minting, standing):
        assert "bankmachine store key export" in surface


def test_export_refuses_to_write_the_key_into_the_data_directory(
    initialized: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 A directory holding both halves is a directory that decrypts itself.

    The whole value of encryption at rest is that a copied data directory is
    noise. A key file sitting in it means one careless `cp -r`, backup sweep or
    synced folder carries the ciphertext and the key together.
    """
    inside = initialized.datastore_path.parent / "key.txt"

    assert run(["store", "key", "export", "--to", str(inside)]) == 2

    assert not inside.exists()
    err = capsys.readouterr().err
    assert "decrypts itself" in err
    assert get_datastore_key(initialized) not in err


def test_export_refuses_a_subdirectory_of_the_data_directory_too(
    initialized: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """One level down is the same directory for every purpose that matters here."""
    nested = initialized.datastore_path.parent / "logs" / "key.txt"

    assert run(["store", "key", "export", "--to", str(nested)]) == 2

    assert not nested.exists()
    assert get_datastore_key(initialized) not in capsys.readouterr().err


def test_the_missing_key_diagnosis_names_the_command_that_fixes_it(
    initialized: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 AC-17.7 reaches the RESTORE direction, not only the export one.

    This diagnosis is the most precise text the product has -- it refuses to
    mint, and says exactly why -- and until FR-12 it ended in "restore the
    keychain entry from your backup" with nothing to do that with. An
    instruction with no command behind it is the defect this feature was filed
    about; leaving it here would have fixed one direction and left the other.
    """
    delete_datastore_key(initialized)

    assert run(["store", "init"]) == 2

    err = capsys.readouterr().err
    assert "bankmachine store key import" in err
    assert "a fresh one would decrypt nothing" in err
