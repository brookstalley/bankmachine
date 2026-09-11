"""`bankmachine store init` / `status` end to end, over a real encrypted datastore."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from bankmachine.cli import run
from bankmachine.config import Config
from bankmachine.store.connection import SUPPORTED_SCHEMA_VERSION
from bankmachine.store.rebuild import RebuildReport
from conftest import use_cli_env


@pytest.fixture
def cli_env(config: Config, monkeypatch: pytest.MonkeyPatch) -> Config:
    use_cli_env(monkeypatch, config)
    return config


def test_status_reports_a_missing_datastore_without_creating_one(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = run(["store", "status"])
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "present:         no" in out
    assert "datastore missing" in out
    assert not cli_env.datastore_path.exists()


def test_init_creates_an_encrypted_datastore_at_the_current_schema_version(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = run(["store", "init"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "datastore created at" in out
    assert "journal mode:    wal" in out
    assert f"schema version:  {SUPPORTED_SCHEMA_VERSION}" in out
    assert "healthy:         yes" in out
    assert cli_env.datastore_path.read_bytes()[:16] != b"SQLite format 3\x00"


def test_init_is_idempotent(cli_env: Config, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["store", "init"]) == 0
    capsys.readouterr()

    assert run(["store", "init"]) == 0
    out = capsys.readouterr().out
    assert "datastore already present" in out
    assert "no migrations to apply" in out


def test_status_after_init_is_healthy(cli_env: Config, capsys: pytest.CaptureFixture[str]) -> None:
    run(["store", "init"])
    capsys.readouterr()

    assert run(["store", "status"]) == 0
    assert "healthy:         yes" in capsys.readouterr().out


def test_the_startup_banner_names_the_environment(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    run(["store", "status"])
    assert "environment=SANDBOX" in capsys.readouterr().err


def test_init_tells_the_operator_to_back_the_minted_key_up_and_how_to_read_it(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one moment the operator is guaranteed to be looking at this command.

    A datastore key cannot be recovered from the datastore, so the instruction
    has to name the keychain entry AND a command that acts on it, or the operator
    is told to preserve a value they have no route to. On stdout rather than in
    the log because the log is the channel nobody reads on the day they run
    `store init`.

    🔴 This asserted a `security find-generic-password` recipe until FR-12
    shipped `store key`. Re-pointed, not relaxed: AC-17.7 requires every surface
    to name the product's own command, and the recipe it replaced put the key in
    shell history, which AC-10.1 forbids. Both commands are asserted because
    exporting without checking is the half that fails silently.
    """
    assert run(["store", "init"]) == 0
    out = capsys.readouterr().out

    assert "unrecoverable" in out.lower()
    assert cli_env.keychain_service in out
    assert cli_env.keychain_account in out
    assert "bankmachine store key export" in out
    assert "bankmachine store key verify" in out
    assert "find-generic-password" not in out


def test_the_backup_instruction_is_printed_only_where_a_key_was_minted(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """A second `store init` mints nothing, so it has nothing to say about a new key.

    Repeating the block on every run is how an instruction stops being read.
    The standing reminder is `store status`'s one line, asserted below.
    """
    assert run(["store", "init"]) == 0
    assert "unrecoverable" in capsys.readouterr().out.lower()

    assert run(["store", "init"]) == 0
    assert "unrecoverable" not in capsys.readouterr().out.lower()


def test_status_reminds_the_operator_where_the_key_is_and_that_it_is_the_only_copy(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """`store status` is the command an operator runs when something looks wrong.

    One line, not the block: the block belongs to the minting moment. This is
    the standing reminder that the keychain entry is the only copy there is.
    """
    assert run(["store", "init"]) == 0
    capsys.readouterr()

    assert run(["store", "status"]) == 0
    out = capsys.readouterr().out

    assert "unrecoverable" in out.lower()
    assert f"{cli_env.keychain_service}/{cli_env.keychain_account}" in out
    # AC-17.7 -- the standing reminder names the commands, not a shell recipe.
    assert "bankmachine store key export" in out
    assert "find-generic-password" not in out


def test_the_datastore_key_is_never_printed(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    run(["store", "init"])
    captured = capsys.readouterr()

    from bankmachine.secrets import get_datastore_key

    key = get_datastore_key(cli_env)
    assert key not in captured.out
    assert key not in captured.err
    log_file: Path = cli_env.log_dir / "bankmachine.log"
    if log_file.exists():
        assert key not in log_file.read_text(encoding="utf-8")


def test_everything_the_product_creates_is_readable_only_by_the_operator(
    config: Config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The security model claims OS file permissions as a control; this is the claim.

    Rooted at a directory the PRODUCT creates rather than at `tmp_path`, which
    pytest already makes 0700 -- a test that asserted on `tmp_path` would pass
    with no umask set at all and prove nothing.

    Every file here is either the ciphertext, its journal, or the plaintext log
    that carries paths, institution ids and SQL text. The log is the one with
    real content at 0644, so it is asserted beside the datastore rather than
    left to a separate test.
    """
    datastore = tmp_path / "state" / "store.db"
    log_dir = tmp_path / "state" / "logs"
    monkeypatch.setenv("BANKMACHINE_DATASTORE_PATH", str(datastore))
    monkeypatch.setenv("BANKMACHINE_LOG_DIR", str(log_dir))
    monkeypatch.setenv("BANKMACHINE_KEYCHAIN_SERVICE", config.keychain_service)
    monkeypatch.setenv("BANKMACHINE_ENVIRONMENT", config.environment)
    monkeypatch.setenv("BANKMACHINE_CONFIG", str(tmp_path / "absent.toml"))

    assert run(["store", "init"]) == 0

    def mode(path: Path) -> str:
        return oct(stat.S_IMODE(path.stat().st_mode))

    assert mode(datastore) == oct(0o600)
    assert mode(datastore.parent) == oct(0o700)
    assert mode(log_dir) == oct(0o700)
    assert mode(log_dir / "bankmachine.log") == oct(0o600)
    # The journal files hold pages that have not reached the datastore yet, so
    # they are as sensitive as it is. They exist only while a writer has been
    # open, which `store init` guarantees.
    for journal in sorted(datastore.parent.glob("store.db-*")):
        assert mode(journal) == oct(0o600), journal


def test_init_refuses_to_mint_a_key_for_a_datastore_it_cannot_decrypt(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """The restored-from-backup path: datastore present, keychain entry gone.

    Generating a key here would store one that decrypts nothing, and the next
    `store status` would report an authentication failure instead of a missing
    key -- routing the operator to the wrong recovery for a state that is still
    fully recoverable.
    """
    from bankmachine.secrets import (
        DatastoreKeyMissingError,
        delete_datastore_key,
        get_datastore_key,
    )

    assert run(["store", "init"]) == 0
    capsys.readouterr()
    delete_datastore_key(cli_env)

    assert run(["store", "init"]) == 2
    err = capsys.readouterr().err
    assert "holds no key for it" in err
    assert "No key was generated" in err

    with pytest.raises(DatastoreKeyMissingError):
        get_datastore_key(cli_env)


def test_init_still_creates_a_key_when_there_is_no_datastore(cli_env: Config) -> None:
    """The refusal above must not break the ordinary first run."""
    from bankmachine.secrets import get_datastore_key

    assert not cli_env.datastore_path.exists()
    assert run(["store", "init"]) == 0
    assert get_datastore_key(cli_env)


def test_rebuild_on_a_fresh_datastore_reports_that_it_changed_nothing(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["store", "init"]) == 0
    capsys.readouterr()

    exit_code = run(["store", "rebuild"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "raw responses replayed:  0" in out
    assert "content:                 identical to what it replaced" in out


def test_rebuild_refuses_a_datastore_that_does_not_exist(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    # `store init` is the only thing that creates a datastore; a typo'd path is
    # reported rather than turned into an empty store that rebuilds to nothing.
    exit_code = run(["store", "rebuild"])

    assert exit_code == 2
    assert "run `bankmachine store init`" in capsys.readouterr().err
    assert not cli_env.datastore_path.exists()


def _report(*, previous: tuple[int, ...], changed: bool) -> RebuildReport:
    """A report whose digests differ or match, so the printer's branches are reachable.

    Constructed rather than provoked: the two branches below are pure formatting
    over a `RebuildReport`, and driving a real datastore into each state would
    test the rebuild again rather than the sentence an operator reads.
    """
    return RebuildReport(
        responses_replayed=4,
        rows_deleted={"transactions": 3, "balances_daily": 1, "holdings": 0},
        derivation_version=2,
        derivation_version_id=7,
        previous_derivation_versions=previous,
        digest_before="aaaa",
        digest_after="bbbb" if changed else "aaaa",
    )


def test_rebuild_says_a_new_derivation_version_explains_the_difference(
    cli_env: Config, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "bankmachine.cli.store.rebuild",
        lambda config, **kwargs: _report(previous=(1,), changed=True),
    )

    assert run(["store", "rebuild"]) == 0
    out = capsys.readouterr().out

    assert "rows replaced:           4" in out
    assert "  holdings" not in out  # a table with nothing to replace is not listed
    assert "previous version(s):     1" in out
    assert "changed, as expected at a new derivation version" in out


def test_rebuild_names_an_accepted_change_as_unexplained(
    cli_env: Config, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only reachable under --accept-content-change; without it the rebuild raises
    # and this line is never printed against a difference nobody accounted for.
    monkeypatch.setattr(
        "bankmachine.cli.store.rebuild",
        lambda config, **kwargs: _report(previous=(2,), changed=True),
    )

    assert run(["store", "rebuild", "--accept-content-change"]) == 0

    assert "CHANGED at an unchanged derivation version, accepted" in capsys.readouterr().out


def test_backup_writes_a_verified_copy_and_reports_it(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["store", "init"]) == 0
    capsys.readouterr()
    destination = cli_env.datastore_path.parent / "backup.db"

    exit_code = run(["store", "backup", str(destination)])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert destination.exists()
    assert f"backup:          {destination}" in out
    assert f"schema version:  {SUPPORTED_SCHEMA_VERSION}" in out
    assert "verified:        yes" in out


def test_backup_gives_the_ahead_copy_the_remedy_that_is_not_store_init(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 Two mismatch states, two remedies, and this is the one that inverts.

    Migrations are forward-only. A copy AHEAD of this build is not migrated
    forward by anything, so "run `store init`" sends the operator to a command
    that applies nothing, reports nothing to do, and reads as "I tried the fix
    and the fix is broken". The remedy is to upgrade the build instead, and this
    asserts the note says so rather than merely saying something.
    """
    from bankmachine.store import connection

    assert run(["store", "init"]) == 0
    with connection.writer(cli_env) as conn:
        conn.execute("BEGIN IMMEDIATE")
        connection.stamp_schema_version(conn, SUPPORTED_SCHEMA_VERSION + 1)
        conn.execute("COMMIT")
    capsys.readouterr()
    destination = cli_env.datastore_path.parent / "backup.db"

    assert run(["store", "backup", str(destination)]) == 0
    out = capsys.readouterr().out

    assert f"schema version:  {SUPPORTED_SCHEMA_VERSION + 1}" in out
    assert "verified:        yes" in out
    assert "Update this bankmachine to the build that wrote it" in out
    assert "do not run `bankmachine store init`" in out, (
        "the ahead copy must be told NOT to run the migration command -- naming it here "
        "is the defect this state exists to distinguish"
    )


def test_backup_says_nothing_about_the_version_when_this_build_serves_it(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """The negative half, without which the note above could be unconditional.

    An unconditional note trains the operator to skip it, and the ordinary
    backup is the one they take most.
    """
    assert run(["store", "init"]) == 0
    capsys.readouterr()
    destination = cli_env.datastore_path.parent / "backup.db"

    assert run(["store", "backup", str(destination)]) == 0

    assert "this build serves" not in capsys.readouterr().out


def test_backup_warns_that_the_copy_is_useless_without_the_key(
    cli_env: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """The one thing an operator must not miss.

    The datastore key has no other source -- an aggregator secret can be re-read
    from the vendor's dashboard, this cannot be re-read from anywhere. A copy
    without it is noise that looks like a backup, so the warning is
    unconditional rather than something the operator has to ask for.
    """
    assert run(["store", "init"]) == 0
    destination = cli_env.datastore_path.parent / "backup.db"

    with caplog.at_level("WARNING", logger="bankmachine"):
        assert run(["store", "backup", str(destination)]) == 0

    warnings = "\n".join(r.getMessage() for r in caplog.records if r.levelname == "WARNING")
    assert "USELESS WITHOUT THE DATASTORE KEY" in warnings
    assert cli_env.keychain_service in warnings
    # AC-17.7 -- this was the one surface that named the chore and no command,
    # so which answer the operator met depended on which command they had run.
    assert "bankmachine store key export" in warnings


def test_backup_refuses_an_existing_destination_with_exit_2(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["store", "init"]) == 0
    destination = cli_env.datastore_path.parent / "backup.db"
    destination.write_bytes(b"an earlier backup")
    capsys.readouterr()

    exit_code = run(["store", "backup", str(destination)])
    err = capsys.readouterr().err

    # EXIT_ERROR, not EXIT_UNHEALTHY: the command could not run. launchd reads
    # this distinction, so it is asserted rather than assumed.
    assert exit_code == 2
    assert "never overwrites a backup" in err
    assert destination.read_bytes() == b"an earlier backup"


def test_backup_reports_a_missing_datastore_rather_than_creating_one(
    cli_env: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    destination = cli_env.datastore_path.parent / "backup.db"

    exit_code = run(["store", "backup", str(destination)])
    err = capsys.readouterr().err

    assert exit_code == 2
    assert "no datastore at" in err
    assert not destination.exists()
    assert not cli_env.datastore_path.exists()
