"""AC-5.4: a store holding rows derived by another version says so, until they are re-derived.

A version can move without a migration. The rows already stored keep what the
older logic produced until `store rebuild` replays the archive, and nothing in a
row says it would come out differently now -- so an answer over them reads as
current. These tests hold the three surfaces to saying otherwise, and hold the
remedy to clearing it.

🔴 **Seeded through the shipped derivers under a stood-in version**, never by
editing a stamp afterwards, for the reason the upgrade tests give: a hand-edited
stamp can satisfy a check that a derived row would not.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text, update
from test_rebuild_investments import _archive, _holdings_body, enrolled, instant, rebuilt

from bankmachine import query
from bankmachine.cli import run
from bankmachine.config import Config
from bankmachine.connector import INVESTMENTS_HOLDINGS_GET
from bankmachine.store import derivation
from bankmachine.store.derivation import ensure_derivation_version
from bankmachine.store.engine import reader_connection, writer_connection
from bankmachine.store.rebuild import (
    RebuildByAnOlderBuildError,
    derivation_versions_present,
    derived_tables,
    rebuildable_tables,
)
from bankmachine.store.schema import securities
from conftest import use_cli_env

__all__ = ["enrolled"]  # the fixture is used by name

KIND = "derivation_version_mismatch"


def _mismatches(config: Config) -> list[str]:
    return [w.detail for w in query.pipeline_health(config).warnings if w.kind == KIND]


def _derive_a_position(config: Config, monkeypatch: pytest.MonkeyPatch, *, version: int) -> None:
    """One capture of one position -- `securities` and `holdings` -- under `version`."""
    with monkeypatch.context() as that_build:
        that_build.setattr(derivation, "DERIVATION_VERSION", version)
        _archive(
            config,
            INVESTMENTS_HOLDINGS_GET.path,
            _holdings_body(quantity="4", value="100"),
            at=instant(1),
        )


def test_a_store_derived_by_this_build_carries_no_mismatch(
    enrolled: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _derive_a_position(enrolled, monkeypatch, version=derivation.DERIVATION_VERSION)

    assert _mismatches(enrolled) == []
    assert query.pipeline_health(enrolled).coverage["derivation"] == {
        "current_version": derivation.DERIVATION_VERSION,
        "versions_in_store": [derivation.DERIVATION_VERSION],
    }


def test_an_empty_store_carries_no_mismatch_and_says_it_holds_no_version(
    initialized_config: Config,
) -> None:
    assert _mismatches(initialized_config) == []
    assert query.pipeline_health(initialized_config).coverage["derivation"] == {
        "current_version": derivation.DERIVATION_VERSION,
        "versions_in_store": [],
    }


def test_rows_from_an_older_version_raise_it_and_name_the_rebuild(
    enrolled: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    older = derivation.DERIVATION_VERSION - 1
    _derive_a_position(enrolled, monkeypatch, version=older)

    [detail] = _mismatches(enrolled)
    assert str(older) in detail and "store rebuild" in detail
    assert older in query.pipeline_health(enrolled).coverage["derivation"]["versions_in_store"]


def test_rows_from_a_newer_version_say_the_server_is_older_and_never_prescribe_a_rebuild(
    enrolled: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    newer = derivation.DERIVATION_VERSION + 1
    _derive_a_position(enrolled, monkeypatch, version=newer)

    [detail] = _mismatches(enrolled)
    assert str(newer) in detail and "older than the build" in detail
    assert "store rebuild`" not in detail.replace("never to rebuild", "")


def test_a_rebuild_clears_it_including_the_dimension_rows_a_replay_upserts(
    enrolled: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 The remedy the warning names, asserted rather than prescribed.

    `securities` is not emptied by a rebuild -- it is a dimension, upserted by
    the replay -- so a rebuild that re-stamped only the tables it empties would
    leave the warning standing forever on a store the operator has already
    repaired. The account rows `enrolled` archived are also older here, so the
    check reaches more than one table.
    """
    older = derivation.DERIVATION_VERSION - 1
    with monkeypatch.context() as that_build:
        that_build.setattr(derivation, "DERIVATION_VERSION", older)
        _archive(
            enrolled,
            INVESTMENTS_HOLDINGS_GET.path,
            _holdings_body(quantity="4", value="100"),
            at=instant(1),
        )
    with reader_connection(enrolled) as conn:
        stamped = {
            table.name: derivation_versions_present(conn, (table,)) for table in derived_tables()
        }
    assert stamped["securities"] == (older,), "the seeding did not reach the dimension table"
    assert _mismatches(enrolled), "nothing to clear, so the rebuild below proves nothing"

    rebuilt(enrolled)

    assert _mismatches(enrolled) == []
    with reader_connection(enrolled) as conn:
        assert derivation_versions_present(conn, derived_tables()) == (
            derivation.DERIVATION_VERSION,
        )


def test_each_version_probe_is_an_index_seek_not_a_scan(initialized_config: Config) -> None:
    """AC-5.4's cost clause: the check rides every answer, so it may not walk a table."""
    with reader_connection(initialized_config) as conn:
        for table in derived_tables():
            plan = conn.execute(
                text(
                    f"EXPLAIN QUERY PLAN SELECT EXISTS (SELECT 1 FROM {table.name} "
                    f"WHERE derivation_version_id = 1)"
                )
            ).all()
            details = " ".join(str(row[-1]) for row in plan)
            assert f"{table.name}_by_derivation_version" in details, f"{table.name}: {details}"


def test_store_status_says_no_version_on_a_store_that_has_derived_nothing(
    initialized_config: Config,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    use_cli_env(monkeypatch, initialized_config)

    assert run(["store", "status"]) == 0
    out = capsys.readouterr().out

    assert (
        f"derivation:      none derived yet (this build derives {derivation.DERIVATION_VERSION})"
        in out
    )
    assert "store rebuild" not in out


def test_store_status_names_the_rebuild_for_older_rows_without_failing_the_check(
    enrolled: Config,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A store awaiting a rebuild is a healthy FILE, so the exit code stays 0."""
    older = derivation.DERIVATION_VERSION - 1
    _derive_a_position(enrolled, monkeypatch, version=older)
    use_cli_env(monkeypatch, enrolled)

    assert run(["store", "status"]) == 0
    out = capsys.readouterr().out

    assert f"derivation:      {older}, {derivation.DERIVATION_VERSION}" in out
    assert "run `bankmachine store rebuild`" in out
    assert "NEWER" not in out


def test_store_status_tells_an_older_build_to_upgrade_rather_than_rebuild(
    enrolled: Config,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    newer = derivation.DERIVATION_VERSION + 1
    _derive_a_position(enrolled, monkeypatch, version=newer)
    use_cli_env(monkeypatch, enrolled)

    assert run(["store", "status"]) == 0
    out = capsys.readouterr().out

    assert "NEWER" in out and "upgrade it, and do not rebuild with it" in out
    assert "run `bankmachine store rebuild`" not in out


def test_a_rebuild_commits_and_clears_it_when_only_a_dimension_row_is_older(
    enrolled: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 The remedy the warning names must not be the thing that refuses.

    A rebuild refuses a content change at an unchanged derivation version, and it
    judged "unchanged" only from the rows it deletes. `securities` is upserted by
    the replay rather than deleted, and re-stamped as it is -- so a store whose
    only older rows were securities read as unchanged, the digest moved, and the
    rebuild rolled back while the warning kept telling the operator to run it.

    The one stamp here is written by hand, which this module otherwise refuses:
    the derivers reach this state only by way of older rows in the tables a
    rebuild empties, and those would mask the defect this test exists to catch.
    """
    _derive_a_position(enrolled, monkeypatch, version=derivation.DERIVATION_VERSION)
    older = derivation.DERIVATION_VERSION - 1
    with writer_connection(enrolled) as conn:
        older_id = ensure_derivation_version(conn, version=older)
        conn.execute(update(securities).values(derivation_version_id=older_id))
    with reader_connection(enrolled) as conn:
        assert derivation_versions_present(conn, rebuildable_tables(), archived_rows_only=True) == (
            derivation.DERIVATION_VERSION,
        ), "a table the rebuild empties is older too, so the dimension case is not isolated"
    assert _mismatches(enrolled), "nothing to clear, so the rebuild below proves nothing"

    report = rebuilt(enrolled)

    assert report.change_was_expected
    assert _mismatches(enrolled) == []


def test_a_rebuild_by_an_older_build_refuses_and_leaves_the_newer_rows_alone(
    enrolled: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 Rebuilding with a build older than the rows would erase the evidence of it.

    Every row a rebuild writes is stamped with this build's version, so an older
    build that went ahead would leave newer rows re-derived by older logic, the
    warning silenced, and no stamp left to say so. It must refuse before deleting.
    """
    newer = derivation.DERIVATION_VERSION + 1
    _derive_a_position(enrolled, monkeypatch, version=newer)
    with reader_connection(enrolled) as conn:
        before = derivation_versions_present(conn, derived_tables())
    assert newer in before, "the seeding did not leave a newer row, so nothing is refused"

    with pytest.raises(RebuildByAnOlderBuildError, match="newer than this build"):
        rebuilt(enrolled)

    with reader_connection(enrolled) as conn:
        assert derivation_versions_present(conn, derived_tables()) == before
    assert _mismatches(enrolled), "the refusal silenced the warning it exists to protect"
