"""A datastore this build cannot serve refuses, rather than answering zero.

🔴 The failure this file exists to prevent was MEASURED, not imagined. A store
holding 14 accounts and 388 transactions, at schema version 2 against a build
serving 4, answered every MCP tool with `isError: False`, `rows: []` and every
`coverage` figure zero. An agent that does not parse `warnings` reported that the
household owned nothing — a plausible, structurally valid, wrong answer, which is
the exact shape `api-contract.md` § Hard errors and `architecture.md` § Direction
both name as the reason to refuse instead.

The line these tests pin is which unhealthy states refuse and which one answers.
A datastore that is **not there** has no data to misreport, and AC-ARCH.3 asks
for that state to be reported rather than crashed on. Every other unservable
state means data exists and could not be read.
"""

from __future__ import annotations

import io
import json
import logging
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from bankmachine import envelope, mcp, query
from bankmachine.cli import run
from bankmachine.config import Config
from bankmachine.secrets import (
    delete_datastore_key,
    generate_datastore_key,
    set_datastore_key,
)
from bankmachine.store.connection import (
    SUPPORTED_SCHEMA_VERSION,
    DatastoreProblem,
    initializing_writer,
    inspect,
    remedy_for,
    stamp_schema_version,
    writer,
)
from bankmachine.store.migrations import MIGRATIONS, migrate
from bankmachine.store.types import now_utc
from conftest import make_config


def _every_tool() -> tuple[str, ...]:
    """Every built tool, DERIVED from the registry rather than listed here.

    🔴 A literal list of tools goes stale by SILENCE — the loop keeps passing
    over the tools it still names while the one that was added is covered by
    nothing. `get_coverage_report` was missed by five such loops in
    `test_mcp.py`, which is the recorded reason this file derives instead.

    The refusal is a property of the datastore rather than of the question, so
    every tool must refuse; a tool that refused only for some is the defect
    returning by a side door.
    """
    names = tuple(sorted(d["name"] for d in mcp._tool_definitions()))
    assert names, "the registry produced no tools, so every loop over this checks nothing"
    return names


#: One way of breaking a datastore, applied to a config that names it.
type _Builder = Callable[[Config], None]

#: A window, offered only to the tools that advertise one.
_A_WINDOW: dict[str, object] = {"since": "2026-01-01", "until": "2026-03-31"}


def _arguments_for(tool: str) -> dict[str, object]:
    """The window, for the tools whose published schema accepts it.

    🔴 Derived from `inputSchema`, not chosen per tool by hand. An unadvertised
    argument is REFUSED by name — correctly — so handing every tool a window
    makes the zero-argument tools fail with `invalid_argument` and the test then
    proves the argument check works rather than that the store refused. That is a
    green-looking test of the wrong thing, and it is what the first draft did.
    """
    for definition in mcp._tool_definitions():
        if definition["name"] == tool:
            accepted = definition["inputSchema"].get("properties", {})
            return {k: v for k, v in _A_WINDOW.items() if k in accepted}
    raise AssertionError(f"no tool named {tool!r} in the registry")


#: The query-layer entry points, so the refusal is asserted where it is raised as
#: well as where it is rendered.
QUERY_CALLS: tuple[str, ...] = (
    "list_accounts",
    "list_transactions",
    "money_summary",
    "pipeline_health",
    "coverage_report",
)


def _at_version_two(config: Config) -> None:
    """A datastore stopped at the core schema, the version this machine met.

    Uses `migrations=` — the override that exists to make a partial migration
    constructible, and which nothing exercised before this file.
    """
    set_datastore_key(config, generate_datastore_key())
    migrate(config, migrations=MIGRATIONS[:2])


def _seed_one_institution(config: Config, name: str) -> None:
    """One row, written through the initializing writer.

    The ordinary `writer()` refuses an unsupported schema version — which is the
    norm under test — so seeding a store at an older version has to use the one
    handle permitted to open it. That is the migration runner's handle, and this
    is the only other legitimate caller: a test standing in for the operator's
    existing data.
    """
    stamp = str(now_utc())
    with initializing_writer(config) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO institutions (source_institution_id, name, first_seen_at, last_seen_at)"
            " VALUES (?, ?, ?, ?)",
            ("ins_test", name, stamp, stamp),
        )
        conn.execute("COMMIT")


@pytest.fixture
def unservable_config(config: Config) -> Iterator[Config]:
    """A populated datastore this build cannot serve."""
    _at_version_two(config)
    _seed_one_institution(config, "Tartan Bank")
    yield config


# --------------------------------------------------------------------------
# `inspect` names the state, so a remedy can branch on it.
# --------------------------------------------------------------------------


def test_a_missing_datastore_is_named_missing(config: Config) -> None:
    status = inspect(config)
    assert status.reason is DatastoreProblem.MISSING
    assert status.exists is False


def test_a_schema_version_behind_the_build_is_named_as_such(unservable_config: Config) -> None:
    status = inspect(unservable_config)
    assert status.reason is DatastoreProblem.SCHEMA_BEHIND_BUILD
    assert status.schema_version == 2
    assert status.supported_schema_version > 2


def test_a_datastore_with_no_schema_version_is_named_as_such(config: Config) -> None:
    set_datastore_key(config, generate_datastore_key())
    with initializing_writer(config) as conn:
        conn.execute("SELECT 1").fetchone()
    assert inspect(config).reason is DatastoreProblem.NO_SCHEMA_VERSION


def test_a_datastore_whose_key_is_gone_is_named_key_missing(initialized_config: Config) -> None:
    delete_datastore_key(initialized_config)
    try:
        assert inspect(initialized_config).reason is DatastoreProblem.KEY_MISSING
    finally:
        # The fixture's teardown deletes the key; put one back so it finds one.
        set_datastore_key(initialized_config, generate_datastore_key())


def test_a_healthy_datastore_names_no_problem(initialized_config: Config) -> None:
    status = inspect(initialized_config)
    assert status.healthy is True
    assert status.reason is None


def _ahead_of_this_build(config: Config) -> None:
    """A datastore a NEWER bankmachine already migrated.

    Stamping forward is how `test_connection_norms.py` builds this state too: it
    is the only way to reach it without a build from the future.
    """
    with writer(config) as conn:
        conn.execute("BEGIN IMMEDIATE")
        stamp_schema_version(conn, SUPPORTED_SCHEMA_VERSION + 1)
        conn.execute("COMMIT")


def _unopenable_lock(config: Config) -> None:
    """A lock file this process cannot open — the state a restore under another user leaves.

    Uses `Config.lock_path` rather than re-deriving the suffix: the product owns
    where that file lives, and a helper that spells it independently keeps
    passing after the product moves it.
    """
    config.lock_path.write_text("")
    config.lock_path.chmod(0o000)


def _unbuildable_as_root(problem: DatastoreProblem) -> bool:
    """Whether the sweeps cannot construct this state, so they must skip it.

    Only UNREADABLE, and only as root: it is built by `chmod 0o000`, which root
    ignores. The store is then perfectly readable, `inspect` correctly reports it
    healthy, and the sweep fails on `assert status.healthy is False` — reading as
    a product defect when it is the test's own premise that did not hold.
    `tests/store/test_connection_norms.py` guards its three mode-000 tests the
    same way, with `skipif`; this is a per-state skip instead, so the OTHER five
    states keep their coverage under root rather than the whole sweep vanishing.

    One home, called by both sweeps, for the reason `_state_builders` is shared:
    a guard applied to one sweep and not the other is how they drift.
    """
    return problem is DatastoreProblem.UNREADABLE and os.geteuid() == 0


def test_every_problem_the_enum_declares_is_constructed_and_remedied(
    tmp_path: Path, keychain_service: str
) -> None:
    """🔴 Derived from the enum, so a SIXTH state cannot be added uncovered.

    The first draft of this test walked three hand-built states and its docstring
    claimed it enumerated `inspect`'s branches. It did not: a new
    `DatastoreProblem` member with no remedy of its own would have passed it
    untouched, which is the exact failure the test was written to prevent, one
    level up. Now the enum drives the loop — add a member and this goes red until
    something builds a store in that state and asserts what the operator is told.

    Each state is checked for three things: `inspect` names it, the remedy is not
    the generic fallback wearing another state's clothes, and the remedy is not
    silently identical to another state's — which is how a `SCHEMA_AHEAD_OF_BUILD`
    that reused the behind-build sentence shipped a command that applies nothing.
    """
    builders = _state_builders()
    assert set(builders) == set(DatastoreProblem), (
        "a DatastoreProblem member has no builder here, so nothing checks what the operator is "
        f"told in that state: {set(DatastoreProblem) ^ set(builders)}"
    )

    remedies: dict[DatastoreProblem, str] = {}
    for problem, build in builders.items():
        if _unbuildable_as_root(problem):
            continue
        cfg = make_config(tmp_path / problem.value, keychain_service + problem.value)
        cfg.datastore_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            build(cfg)
            status = inspect(cfg)
            assert status.healthy is False, problem
            assert status.reason is problem, f"{problem}: inspect said {status.reason}"
            if problem is DatastoreProblem.MISSING:
                continue  # answers rather than refuses; covered above
            remedies[problem] = _remedy_for(cfg)
        finally:
            _cleanup(cfg)

    duplicated = {r for r in remedies.values() if list(remedies.values()).count(r) > 1}
    assert not duplicated, f"two states share one remedy, so one of them is wrong: {duplicated}"

    # 🔴 The third check, and it needs its own assertion rather than riding the
    # duplicate check above. Two states falling through to the fallback used to
    # produce DIFFERENT strings, because the fallback interpolated the problem
    # sentence -- so "they are not duplicates" was true of exactly the failure
    # this is meant to catch. The fallback is now a fixed sentence, and a state
    # that reaches it is a state whose remedy was never written.
    fallback = "exists but could not be opened"
    fell_through = {problem for problem, remedy in remedies.items() if fallback in remedy} - {
        DatastoreProblem.UNREADABLE
    }
    assert not fell_through, (
        f"these states have no remedy of their own and are wearing the generic one: {fell_through}"
    )


def _state_builders() -> dict[DatastoreProblem, _Builder]:
    """One builder per declared state, shared by every enum-driven guard here.

    Shared rather than repeated so the two sweeps cannot drift apart — a state
    covered by the reason sweep but missing from the path guard is the gap that
    let the path back onto the wire.
    """
    return {
        DatastoreProblem.MISSING: lambda c: None,
        DatastoreProblem.NO_SCHEMA_VERSION: _empty_but_keyed,
        DatastoreProblem.SCHEMA_BEHIND_BUILD: _at_version_two,
        DatastoreProblem.SCHEMA_AHEAD_OF_BUILD: _initialized_then(_ahead_of_this_build),
        DatastoreProblem.KEY_MISSING: _initialized_then(delete_datastore_key),
        DatastoreProblem.UNREADABLE: _initialized_then(_unopenable_lock),
    }


def _empty_but_keyed(config: Config) -> None:
    set_datastore_key(config, generate_datastore_key())
    with initializing_writer(config) as conn:
        conn.execute("SELECT 1").fetchone()


def _initialized_then(step: _Builder) -> _Builder:
    """A healthy store, then one step that breaks it in a specific way."""

    def build(config: Config) -> None:
        set_datastore_key(config, generate_datastore_key())
        migrate(config)
        step(config)

    return build


def _cleanup(config: Config) -> None:
    """Leave the keychain and the lock file as the test found them.

    `delete_datastore_key` documents absence as not an error, so no guard is
    needed around it — a `try/except pass` here would be suppressing an exception
    the function has already promised not to raise, and would hide a real
    keychain failure behind teardown.

    The lock file is chmodded BACK because one builder deliberately made it
    unopenable; leaving it that way would strand the tmp tree for cleanup.
    """
    lock = config.datastore_path.with_suffix(config.datastore_path.suffix + ".lock")
    if lock.exists():
        lock.chmod(0o600)
    delete_datastore_key(config)


# --------------------------------------------------------------------------
# The query layer refuses, except for the one state AC-ARCH.3 carves out.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("entry_point", QUERY_CALLS)
def test_every_query_entry_point_refuses_an_unservable_store(
    unservable_config: Config, entry_point: str
) -> None:
    with pytest.raises(query.DatastoreUnservableError):
        _call_query(unservable_config, entry_point)


@pytest.mark.parametrize("entry_point", QUERY_CALLS)
def test_a_missing_datastore_still_answers(config: Config, entry_point: str) -> None:
    """🔴 AC-ARCH.3's carve-out, unchanged. This is the half that must NOT move.

    A store that is not there has nothing to misreport, so the zeroes are honest
    and the warning is what says why. Asserted for every entry point because the
    refusal was added at the shared choke point all five pass through, and a
    change there could take this path with it.
    """
    answer = _call_query(config, entry_point)
    assert answer.rows == []
    assert [c.kind for c in answer.warnings] == ["partial"]
    assert answer.coverage["accounts"] == 0


def _call_query(config: Config, entry_point: str) -> envelope.Answer:
    if entry_point == "list_transactions":
        return query.list_transactions(config, since=None, until=None, account_id=None)
    if entry_point == "money_summary":
        return query.money_summary(config, since=None, until=None, group_by="category")
    answer: envelope.Answer = getattr(query, entry_point)(config)
    return answer


# --------------------------------------------------------------------------
# The refusal reaches the client as a coded tool error.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("tool", _every_tool())
def test_every_tool_refuses_an_unservable_store(unservable_config: Config, tool: str) -> None:
    """The norm's own words for `get_pipeline_health`, and its siblings beside it.

    🔴 `architecture.md` § Direction and `api-contract.md` § Hard errors both
    name `get_pipeline_health` specifically — *it answers `get_pipeline_health`
    with a refusal rather than serving queries*. Nothing covered that clause
    before this test: `test_connection_norms.py` § Norm 4 asserts the reader and
    the writer refuse and that `inspect` reports unhealthy, all of which stop at
    the store layer.
    """
    result = _call_tool(unservable_config, tool, _arguments_for(tool))
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "datastore_unservable"


def test_the_refusal_is_not_reported_as_an_internal_error(unservable_config: Config) -> None:
    """🔴 A fixable state must not wear the label that means "retry is pointless".

    `internal_error`'s remedy sentence is "the failure has been logged", which is
    what a caller is told when there is nothing they can do. This state is fixed
    by one command, and the whole reason the code vocabulary exists is to keep
    those two apart. The guard is that the refusal is caught AHEAD of the broad
    catch at the boundary; reorder them and this goes red.
    """
    result = _call_tool(unservable_config, "list_accounts", {})
    assert result["structuredContent"]["error"]["code"] != "internal_error"
    assert "has been logged" not in result["structuredContent"]["error"]["message"]


def test_a_refusal_leaves_the_session_usable(unservable_config: Config) -> None:
    """The pipe survives, so the operator sees a failed call rather than a vanished server."""
    replies = _converse(
        unservable_config,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "list_accounts", "arguments": {}},
            },
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
        ],
    )
    assert replies[-2]["result"]["isError"] is True
    assert [t["name"] for t in replies[-1]["result"]["tools"]] != []


def test_the_refusal_carries_no_stack_trace_or_sql(unservable_config: Config) -> None:
    """`api-contract.md` § Error Model: no stack traces, internal identifiers or PII."""
    message = _call_tool(unservable_config, "list_accounts", {})["structuredContent"]["error"][
        "message"
    ]
    for leak in ("Traceback", "SELECT", "sqlcipher", "sqlalchemy", "Error("):
        assert leak not in message


def _converse(config: Config, requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drive the real server loop over string buffers.

    Not a mock of the protocol: the same `serve` the CLI calls, reading the same
    line-delimited JSON a client writes. A refusal that only appears when the
    query layer is called directly would not be the thing under test.
    """
    stdin = io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n")
    stdout = io.StringIO()
    mcp.serve(config, stdin=stdin, stdout=stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]


def _call_tool(config: Config, tool: str, arguments: dict[str, object]) -> dict[str, Any]:
    replies = _converse(
        config,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            },
        ],
    )
    result: dict[str, Any] = replies[-1]["result"]
    return result


# --------------------------------------------------------------------------
# 🔴 The remedy is a claim, so it is asserted like one.
# --------------------------------------------------------------------------


def test_the_remedy_for_an_old_schema_version_actually_upgrades_the_store(
    unservable_config: Config,
) -> None:
    """Run what the sentence says, on a store in that state, and check it worked.

    🔴 `learnings.md`, *a documented remedy is a claim and is asserted like one*:
    the last remedy this project prescribed for a store in this state — a
    `store rebuild` step — ROLLED BACK when it was finally run, because replaying
    the archive moved the content digest while `DERIVATION_VERSION` had not been
    bumped. A remedy that fails spends the operator's trust on the way to
    failing, which is why this test runs the command rather than reading it.

    The seeded row is the point of the assertion, not scenery: an "upgrade" that
    reached the current version by discarding the operator's data would satisfy a
    version check and be the worst possible outcome.
    """
    message = _remedy_for(unservable_config)
    assert "bankmachine store init" in message

    applied = migrate(unservable_config)  # what `store init` does to an existing store

    status = inspect(unservable_config)
    assert status.healthy is True, status.problem
    assert applied, "the remedy claimed there were pending migrations"
    with initializing_writer(unservable_config) as conn:
        names = [row[0] for row in conn.execute("SELECT name FROM institutions").fetchall()]
    assert names == ["Tartan Bank"], "the upgrade must not lose the operator's rows"


def test_the_upgraded_store_answers_where_it_previously_refused(
    unservable_config: Config,
) -> None:
    """The refusal is a state, not a wall: the remedy clears it end to end."""
    assert _call_tool(unservable_config, "get_pipeline_health", {})["isError"] is True
    migrate(unservable_config)
    result = _call_tool(unservable_config, "get_pipeline_health", {})
    assert result["isError"] is False
    assert result["structuredContent"]["coverage"]["connections"] == 0


def test_the_key_missing_remedy_does_not_send_the_operator_to_store_init(
    initialized_config: Config,
) -> None:
    """🔴 `store init` REFUSES this state, and `secrets.py` says so deliberately.

    `cli/store.py::_obtain_key` raises rather than minting a key for a datastore
    that already exists, because a fresh key would decrypt nothing and would hide
    an exact diagnosis behind an authentication failure. `secrets.py` carries a
    comment recording that its own message is "deliberately not `run bankmachine
    store init`". This surface used to recommend that exact command for that
    exact state — one module's deliberate decision contradicted by another's
    hardcoded sentence.
    """
    delete_datastore_key(initialized_config)
    try:
        message = _remedy_for(initialized_config)
        assert "store init" not in message
        assert "restore the keychain entry" in message.lower()
        assert "cannot be recovered" in message.lower()
    finally:
        set_datastore_key(initialized_config, generate_datastore_key())


def test_the_remedy_says_nothing_was_changed(unservable_config: Config) -> None:
    """A refusal on a data-bearing path states that it left the data alone.

    The operator's first question on seeing a tool refuse against the store
    holding their finances is whether the refusal did anything to it.
    """
    assert "nothing was changed" in _remedy_for(unservable_config).lower()


def test_the_remedy_for_a_missing_schema_version_actually_migrates_the_store(
    config: Config,
) -> None:
    """The second remedy that names a command, asserted the same way as the first.

    🔴 Added on self-review: the schema-mismatch remedy had a test that ran it and
    this one did not, which is exactly the asymmetry *a documented remedy is a
    claim* warns about — the untested branch is the one that rots, because
    nothing reports when it stops being true.

    A store with no recorded schema version is what an interrupted migration
    leaves behind, and the runner's own atomicity guarantee is why `store init`
    can finish the job: DDL and version stamp commit together or not at all, so
    there is no half-applied migration for the re-run to trip over.
    """
    set_datastore_key(config, generate_datastore_key())
    with initializing_writer(config) as conn:
        conn.execute("SELECT 1").fetchone()
    assert inspect(config).reason is DatastoreProblem.NO_SCHEMA_VERSION

    message = _remedy_for(config)
    assert "bankmachine store init" in message

    migrate(config)  # what `store init` does to an existing store
    assert inspect(config).healthy is True


def test_a_store_newer_than_the_build_is_not_told_to_run_migrations(
    initialized_config: Config,
) -> None:
    """🔴 The remedy that would have done nothing at all.

    `migrate()` is forward-only — `if step.version <= current: continue` — so for
    a store a NEWER bankmachine already migrated, "run the pending migrations"
    applies nothing, reports nothing to do, and leaves the store exactly as
    unservable. The operator reads that as "I tried the fix and the fix is
    broken", which is worse than being told nothing.

    This direction is not the exotic one. `api-contract.md` § Hard errors gives
    it as the REASON the whole state refuses — *a reader running older code
    against a migrated schema* — so a remedy that only handles the other
    direction misses the case the requirement was written for. Both directions
    set `version != SUPPORTED_SCHEMA_VERSION`, which is how one sentence came to
    answer both.
    """
    _ahead_of_this_build(initialized_config)
    status = inspect(initialized_config)
    assert status.reason is DatastoreProblem.SCHEMA_AHEAD_OF_BUILD
    assert status.schema_version == SUPPORTED_SCHEMA_VERSION + 1

    message = _remedy_for(initialized_config)
    assert "NEWER" in message
    assert "do not run `bankmachine store init`" in message
    assert "apply the pending migrations" not in message


def test_no_refusal_in_any_state_carries_the_datastore_path(
    tmp_path: Path, keychain_service: str
) -> None:
    """🔴 The guard over every branch, because the leak came back through the one
    branch a single-state guard did not reach.

    The first version of this test ran only against a schema-2 store, which takes
    a branch that interpolates nothing. The FALLBACK branch interpolated
    `status.problem` — and for the unreadable state that string is `str(exc)`
    from the store layer, whose exceptions carry absolute paths by design
    ("could not probe the writer lock at <path>"). So the path was removed from
    four branches, documented as removed, and still reached the wire through the
    fifth.

    Driven from the enum for the same reason the state sweep is: a guard that
    names its states cannot cover the one added next.
    """
    for problem, build in _state_builders().items():
        if problem is DatastoreProblem.MISSING:
            continue  # answers rather than refuses
        if _unbuildable_as_root(problem):
            continue
        cfg = make_config(tmp_path / problem.value, keychain_service + problem.value)
        cfg.datastore_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            build(cfg)
            message = _remedy_for(cfg)
            assert str(cfg.datastore_path) not in message, problem
            assert str(cfg.datastore_path.parent) not in message, problem
            assert str(Path.home()) not in message, problem
            assert cfg.environment in message, problem
        finally:
            _cleanup(cfg)


def test_no_remedy_prescribes_store_rebuild(unservable_config: Config) -> None:
    """🔴 The one command this file may not recommend without running it.

    Recorded in `learnings-detail.md`: prescribed as the upgrade step, it rolled
    back on exactly this shape of store. If a future change has a reason to name
    it, that reason comes with a test that puts a store in the state and runs it
    — at which point this guard is the thing to update, deliberately.
    """
    assert "store rebuild" not in _remedy_for(unservable_config)


def _remedy_for(config: Config) -> str:
    result = _call_tool(config, "list_accounts", {})
    assert result["isError"] is True
    message: str = result["structuredContent"]["error"]["message"]
    # The rendered text and the structured message are the same sentence; a
    # client that shows only one must not show less.
    assert json.dumps(result["content"]).count(message[:40]) == 1
    return message


def test_the_unreadable_remedy_puts_the_diagnosis_where_it_says_it_does(
    tmp_path: Path, keychain_service: str, caplog: pytest.LogCaptureFixture
) -> None:
    """🔴 The one branch that makes a claim about somewhere else.

    Every other remedy names a command the operator can run and this file runs
    it. The fallback instead promises the file-level diagnosis "is also in the
    log, where redaction applies" — a claim about a second surface, which no
    assertion reached. It was false: `query` logged nowhere, and `cmd_mcp` writes
    `status.problem` only at startup, so for the state this branch exists for —
    a store that goes bad while a long-lived server runs — nothing had ever
    written what the sentence sends the operator to look for.

    The wire half is asserted beside it, because the two are a pair: the detail
    must be in the log AND must not be in the message. A fix that satisfied one
    by breaking the other is the trade this guards against.
    """
    if _unbuildable_as_root(DatastoreProblem.UNREADABLE):
        pytest.skip("root opens a mode-000 file anyway")

    cfg = make_config(tmp_path / "unreadable", keychain_service + "unreadable")
    cfg.datastore_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _initialized_then(_unopenable_lock)(cfg)
        with caplog.at_level(logging.WARNING, logger="bankmachine"):
            message = _remedy_for(cfg)

        logged = " ".join(r.getMessage() for r in caplog.records)
        # Positive control first: a caplog that collected nothing would satisfy
        # every "is not in" assertion below while proving none of them.
        assert logged, "nothing was logged at all, so this test asserts nothing"
        assert str(cfg.datastore_path) in logged, (
            "the remedy sends the operator to the log for the file-level diagnosis, "
            f"and it is not there: {logged}"
        )
        assert str(cfg.datastore_path) not in message, "the path reached the wire"
    finally:
        _cleanup(cfg)


#: The four commands that refuse before doing anything when the store is
#: unservable. Each raised its own hand-written remedy until 2026-09-09, and all
#: four said `store init` for every state.
_CLI_GUARDED = (
    ["connections", "list"],
    ["connector", "check"],
    ["sync", "run"],
    ["enroll", "--yes"],
)


@pytest.mark.parametrize("argv", _CLI_GUARDED, ids=lambda a: " ".join(a))
@pytest.mark.parametrize(
    "problem",
    [DatastoreProblem.SCHEMA_AHEAD_OF_BUILD, DatastoreProblem.KEY_MISSING],
    ids=lambda p: p.value,
)
def test_no_cli_command_prescribes_store_init_where_it_is_wrong(
    tmp_path: Path,
    keychain_service: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    problem: DatastoreProblem,
) -> None:
    """🔴 The two states where `bankmachine store init` is not merely unhelpful but wrong.

    For a store AHEAD of this build the command applies nothing — migrations are
    forward-only — and the operator learns only that the fix they were given did
    nothing. For a store whose key is gone `store init` REFUSES, on purpose,
    because a fresh key would decrypt nothing; sending them there trades an exact
    diagnosis for a turn-away.

    Parametrized over both surfaces of the product deliberately. The MCP layer got
    this right and the CLI did not, for months, because each of the four commands
    composed its own remedy — so this asserts the property of the STATE, across
    every command that reports it, rather than of the one caller that was fixed.
    """
    cfg = make_config(tmp_path / problem.value, keychain_service + problem.value)
    cfg.datastore_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BANKMACHINE_DATASTORE_PATH", str(cfg.datastore_path))
    monkeypatch.setenv("BANKMACHINE_KEYCHAIN_SERVICE", cfg.keychain_service)
    monkeypatch.setenv("BANKMACHINE_ENVIRONMENT", cfg.environment)
    monkeypatch.setenv("BANKMACHINE_PLAID_CLIENT_ID", "test-client-id")
    try:
        _state_builders()[problem](cfg)
        capsys.readouterr()

        assert run(argv) != 0, "an unservable datastore must not be reported as success"
        rendered = capsys.readouterr()
        message = rendered.err + rendered.out

        # Positive control: an empty capture would satisfy the assertion below
        # while proving nothing about what the operator was told.
        assert cfg.environment in message or "datastore" in message, (
            f"nothing recognisable was reported, so this asserts nothing: {message!r}"
        )
        # 🔴 Asserted on the REMEDY, not on whether the string "store init"
        # appears. It appears legitimately: the store layer's own diagnosis for
        # KEY_MISSING explains that `store init` creates a key only when no
        # datastore exists, which is the opposite of prescribing it. A bare
        # substring cannot tell a prescription from an explanation, and the first
        # draft of this test failed all eight cases against a CORRECT fix for
        # exactly that reason.
        remedy = remedy_for(problem)
        assert message.rstrip().endswith(remedy), (
            f"{problem.value} did not end with its own remedy, so this command is still "
            f"composing one of its own: {message!r}"
        )
        assert remedy != remedy_for(DatastoreProblem.SCHEMA_BEHIND_BUILD), (
            f"{problem.value} carries the run-the-migrations remedy, which is the defect: "
            f"{remedy!r}"
        )
    finally:
        _cleanup(cfg)
