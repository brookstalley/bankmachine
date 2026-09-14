"""The cursor boundary — AC-2.1 and AC-2.5, which are one requirement stated twice.

AC-2.1 says the cursor is persisted *transactionally with the data it
accompanies*; AC-2.5 says a crash mid-sync does not advance it. Every plausible
implementation satisfies both or violates both at a single seam, so the seam is
built and proved before anything writes a transaction row.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from bankmachine.cli import sync_run
from bankmachine.config import Config
from bankmachine.connector import ACCOUNTS_GET, TRANSACTIONS_SYNC, FetchedResponse
from bankmachine.derivers import ALL_DERIVERS, all_replay_passes
from bankmachine.store.derivation import DerivationError, apply_response
from bankmachine.store.engine import reader_connection, writer_connection
from bankmachine.store.schema import TRANSACTIONS_DOMAIN, connections, institutions, sync_state
from bankmachine.store.types import now_utc
from conftest import child_env

CURSOR_ONE = "cursor-after-page-one"
CURSOR_TWO = "cursor-after-page-two"


def _page(*, next_cursor: str | None, has_more: bool = False, status: str | None = None) -> bytes:
    """A `/transactions/sync` body in the shape the aggregator really sends.

    Key set follows the live probe recorded in `api-notes-plaid.md` §17, including
    the two that matter most here: a `NOT_READY` reply carries `has_more: false`
    and an EMPTY `next_cursor`.
    """
    body: dict[str, Any] = {
        "accounts": [],
        "added": [],
        "modified": [],
        "removed": [],
        "next_cursor": next_cursor if next_cursor is not None else "",
        "has_more": has_more,
        "request_id": "req-fake",
    }
    if status is not None:
        body["transactions_update_status"] = status
    return json.dumps(body).encode()


@pytest.fixture
def enrolled(initialized_config: Config) -> Config:
    """One connection, inserted directly — enrollment has its own tests."""
    now = now_utc()
    with writer_connection(initialized_config) as conn:
        primary_key = conn.execute(
            institutions.insert().values(
                source_institution_id="ins_109508",
                name="First Platypus Bank",
                first_seen_at=now,
                last_seen_at=now,
            )
        ).inserted_primary_key
        assert primary_key is not None
        institution_id = primary_key[0]
        conn.execute(
            connections.insert().values(
                institution_id=institution_id,
                source_connection_id="item-for-cursor-tests",
                credential_ref="connection:sandbox:item-for-cursor-tests",
                capabilities="[]",
                requested_history_days=730,
                status="active",
                enrolled_at=now,
                created_at=now,
                updated_at=now,
            )
        )
    return initialized_config


def _cursor(config: Config) -> str | None:
    with reader_connection(config) as conn:
        row = conn.execute(
            select(sync_state.c.cursor).where(sync_state.c.domain == TRANSACTIONS_DOMAIN)
        ).one_or_none()
    return None if row is None else row[0]


def _apply(config: Config, body: bytes) -> None:
    with writer_connection(config) as conn:
        apply_response(
            conn,
            connection_id=1,
            endpoint=TRANSACTIONS_SYNC.path,
            body=body,
            received_at=now_utc(),
            derivers=ALL_DERIVERS,
            replay_passes=(),
        )


def test_a_page_advances_the_cursor(enrolled: Config) -> None:
    _apply(enrolled, _page(next_cursor=CURSOR_ONE))

    assert _cursor(enrolled) == CURSOR_ONE


def test_a_second_page_moves_it_again(enrolled: Config) -> None:
    _apply(enrolled, _page(next_cursor=CURSOR_ONE, has_more=True))
    _apply(enrolled, _page(next_cursor=CURSOR_TWO))

    assert _cursor(enrolled) == CURSOR_TWO


def test_a_not_ready_response_does_not_move_the_cursor(enrolled: Config) -> None:
    """🔴 The measured shape that would silently reset a connection.

    A `NOT_READY` reply carries an EMPTY `next_cursor` (§17). Persisting it would
    store "start from the beginning" — or overwrite a good cursor with nothing,
    which on the next run re-fetches every transaction the connection has.
    """
    _apply(enrolled, _page(next_cursor=CURSOR_ONE))

    _apply(enrolled, _page(next_cursor=None, status="NOT_READY"))

    assert _cursor(enrolled) == CURSOR_ONE, "an empty cursor overwrote a good one"


def test_the_first_sync_of_a_new_connection_can_be_not_ready(enrolled: Config) -> None:
    """With no prior cursor, a NOT_READY page must leave no row rather than an empty one.

    An empty cursor stored here is indistinguishable from "never synced" on the
    next run, which is the right answer — but it would also carry a
    `last_success_at`, and a connection that has fetched nothing has not
    succeeded at anything.
    """
    _apply(enrolled, _page(next_cursor=None, status="NOT_READY"))

    assert _cursor(enrolled) is None
    with reader_connection(enrolled) as conn:
        assert conn.execute(select(sync_state)).all() == []


def test_a_page_archived_against_no_connection_is_refused(initialized_config: Config) -> None:
    """A sync page belongs to exactly one connection and nothing else can say which.

    Advancing a cursor against no connection would either write a row with a null
    key or silently pick one, and both are worse than refusing.
    """
    with writer_connection(initialized_config) as conn, pytest.raises(DerivationError) as raised:
        apply_response(
            conn,
            connection_id=None,
            endpoint=TRANSACTIONS_SYNC.path,
            body=_page(next_cursor=CURSOR_ONE),
            received_at=now_utc(),
            derivers=ALL_DERIVERS,
            replay_passes=(),
        )

    assert "without a connection" in str(raised.value)


def test_the_archive_survives_a_deriver_that_raises(enrolled: Config) -> None:
    """🔴 Two commits, and this is what the second one buys.

    The response may be unfetchable — a cursor is consumed once by the caller's
    own progress — while the derivation is always re-runnable. So the bytes land
    first and stay landed, and a rebuild replays what the failed derivation could
    not turn into rows.
    """
    with writer_connection(enrolled) as conn:
        with pytest.raises(DerivationError):
            apply_response(
                conn,
                connection_id=None,
                endpoint=TRANSACTIONS_SYNC.path,
                body=_page(next_cursor=CURSOR_ONE),
                received_at=now_utc(),
                derivers=ALL_DERIVERS,
                replay_passes=(),
            )
        archived = conn.exec_driver_sql("SELECT COUNT(*) FROM raw_responses").scalar_one()

    assert archived == 1, "the archive lost a response because its derivation failed"


# --------------------------------------------------------------------------
# AC-2.5 — a crash, run as a real process that really dies
# --------------------------------------------------------------------------

_KILL_MID_DERIVATION = textwrap.dedent(
    """
    import json, os
    from bankmachine.config import load_config
    from bankmachine.connector import TRANSACTIONS_SYNC
    from bankmachine.store.derivation import apply_response
    from bankmachine.store.engine import writer_connection
    from bankmachine.store.types import now_utc

    from bankmachine.connector.plaid.derivers import derive_transactions_sync

    def write_then_die(conn, response, context):
        # 🔴 The real deriver runs FIRST, so the cursor is genuinely written --
        # then the process dies before the transaction commits. A child that
        # merely exited would prove nothing: nothing would have tried to move the
        # cursor, so it would sit unchanged however the boundary was drawn.
        derive_transactions_sync(conn, response, context)
        os._exit(9)

    body = json.dumps({"next_cursor": "cursor-that-must-not-land", "has_more": False}).encode()
    config = load_config()
    with writer_connection(config) as conn:
        apply_response(
            conn,
            connection_id=1,
            endpoint=TRANSACTIONS_SYNC.path,
            body=body,
            received_at=now_utc(),
            derivers={TRANSACTIONS_SYNC.path: write_then_die},
            replay_passes=(),
        )
    """
)


def test_a_process_killed_mid_derivation_leaves_the_cursor_where_it_was(
    enrolled: Config,
) -> None:
    """🔴 AC-2.5, exercised by killing a real process rather than raising in one.

    An in-process exception unwinds through the same interpreter that opened the
    transaction, so it proves the rollback path and not the *database's* recovery.
    What must hold is a property of the file after a writer stops existing, and
    only a dead process demonstrates that.

    The archive survives — it is committed before derivation begins — and the
    cursor does not move, so the next run re-fetches the page nobody committed.
    That is what makes the re-fetch safe rather than merely likely: the same
    cursor returns the same page (`TRANSACTIONS_SYNC.retry_safe`).
    """
    _apply(enrolled, _page(next_cursor=CURSOR_ONE))
    assert _cursor(enrolled) == CURSOR_ONE

    result = subprocess.run(
        [sys.executable, "-c", _KILL_MID_DERIVATION],
        env={
            **child_env(
                enrolled,
                BANKMACHINE_CONFIG=str(Path(enrolled.datastore_path).parent / "absent.toml"),
            ),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0, result.stdout

    assert _cursor(enrolled) == CURSOR_ONE, (
        "the cursor advanced past a page whose derivation never committed, so those "
        "transactions are gone: the next run resumes after them"
    )
    with reader_connection(enrolled) as conn:
        archived = conn.exec_driver_sql("SELECT COUNT(*) FROM raw_responses").scalar_one()
    assert archived == 2, "the killed run's response should still be in the archive"


def test_a_rebuild_replays_to_the_same_cursor(enrolled: Config) -> None:
    """The cursor is derived, so a replay must reach the same one.

    It lives in a table the rebuild classifies as operator state and never
    empties, so this is not free — it holds because the last page replayed is the
    last page recorded, and it is asserted rather than assumed.
    """
    from bankmachine.store.rebuild import rebuild

    _apply(enrolled, _page(next_cursor=CURSOR_ONE, has_more=True))
    _apply(enrolled, _page(next_cursor=CURSOR_TWO))
    assert _cursor(enrolled) == CURSOR_TWO

    with writer_connection(enrolled) as conn:
        conn.execute(
            sync_state.update()
            .where(sync_state.c.domain == TRANSACTIONS_DOMAIN)
            .values(cursor="wrong-after-tampering")
        )
    rebuild(enrolled, derivers=ALL_DERIVERS, replay_passes=all_replay_passes)

    assert _cursor(enrolled) == CURSOR_TWO


def test_a_failed_row_write_leaves_the_cursor_where_it_was(enrolled: Config) -> None:
    """🔴 Chunk 01's Done-when 1, and it was not delivered until now.

    The atomicity was purely POSITIONAL — rows are written before the cursor, so
    reordering two statements would have shipped green. Both refusal tests
    asserted only the message. This drives the failure the plan named: the row
    write fails, and the cursor must not have moved.

    It only became exercisable in Chunk 02, when there were rows to fail at.
    """
    _apply(enrolled, _page(next_cursor=CURSOR_ONE))
    assert _cursor(enrolled) == CURSOR_ONE

    # A transaction for an account this connection has no row for: the deriver
    # refuses it, and the refusal has to take the cursor down with it.
    body = json.dumps(
        {
            "added": [
                {
                    "account_id": "acct-never-derived",
                    "transaction_id": "t1",
                    "amount": "12.00",
                    "iso_currency_code": "USD",
                    "date": "2026-09-07",
                    "pending": False,
                    "name": "Anything",
                }
            ],
            "modified": [],
            "removed": [],
            "next_cursor": CURSOR_TWO,
            "has_more": False,
        }
    ).encode()

    with pytest.raises(DerivationError):
        _apply(enrolled, body)

    assert _cursor(enrolled) == CURSOR_ONE, (
        "the cursor advanced past a page whose rows were never written, so those "
        "transactions are gone: the next run resumes after them"
    )


def test_a_cursor_written_then_abandoned_does_not_survive_the_transaction(
    enrolled: Config,
) -> None:
    """🔴 The rollback itself, which the sibling test does NOT exercise.

    In the shipped deriver the rows are written before the cursor, so a failing
    row write means the cursor line never runs — the guarantee holds
    *positionally*, and turning the transaction's ROLLBACK into a COMMIT leaves
    that test green. Reordering two statements would ship it.

    This drives the mechanism instead: a deriver that writes the cursor and then
    raises. If the transaction did not roll back, the cursor would survive a
    derivation that failed — and the next run would resume past a page whose rows
    were never written.
    """
    _apply(enrolled, _page(next_cursor=CURSOR_ONE))

    def write_then_fail(conn: Any, response: Any, context: Any) -> None:
        from bankmachine.connector.plaid.derivers import derive_transactions_sync

        derive_transactions_sync(conn, response, context)
        raise DerivationError("something went wrong after the cursor moved")

    with writer_connection(enrolled) as conn, pytest.raises(DerivationError):
        apply_response(
            conn,
            connection_id=1,
            endpoint=TRANSACTIONS_SYNC.path,
            body=_page(next_cursor=CURSOR_TWO),
            received_at=now_utc(),
            derivers={TRANSACTIONS_SYNC.path: write_then_fail},
            replay_passes=(),
        )

    assert _cursor(enrolled) == CURSOR_ONE, (
        "the cursor survived a derivation that failed, so the transaction is not "
        "protecting it — only the order the statements happen to be written in"
    )


# --------------------------------------------------------------------------
# AC-4.1 at the parse: an unreadable page belongs to one connection
# --------------------------------------------------------------------------


def _accounts_body() -> bytes:
    """One `/accounts/get` body that derives cleanly, so the page is what fails."""
    return json.dumps(
        {
            "accounts": [
                {
                    "account_id": "acct-for-parse-tests",
                    "name": "Plaid Checking",
                    "mask": "0000",
                    "type": "depository",
                    "subtype": "checking",
                    "balances": {
                        "current": "110.94",
                        "available": "100.00",
                        "limit": None,
                        "iso_currency_code": "USD",
                    },
                }
            ],
            "item": {"item_id": "item-for-cursor-tests"},
            "request_id": "req-accounts",
        }
    ).encode()


class _StubClient:
    """Answers the two calls `_sync_one` makes, with a scripted transactions page."""

    page: bytes = b""

    def __init__(self, config: Config, secret: str, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> _StubClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def accounts_get(self, access_token: str, **kwargs: Any) -> FetchedResponse:
        return FetchedResponse(
            endpoint=ACCOUNTS_GET,
            body=_accounts_body(),
            received_at=now_utc(),
            request_context=None,
        )

    def transactions_sync(self, access_token: str, **kwargs: Any) -> FetchedResponse:
        return FetchedResponse(
            endpoint=TRANSACTIONS_SYNC,
            body=_StubClient.page,
            received_at=now_utc(),
            request_context=None,
        )


@pytest.mark.parametrize(
    ("page", "mode"),
    [
        (b'{"has_more": tru', "malformed syntax"),
        (("[" * 100_000 + "]" * 100_000).encode(), "nested past the decoder's stack"),
        (b'{"next_cursor": "\xff\xfe", "has_more": false}', "bytes that are not UTF-8"),
    ],
    ids=["syntax", "depth", "encoding"],
)
def test_a_page_this_build_cannot_read_degrades_its_own_connection(
    enrolled: Config, monkeypatch: pytest.MonkeyPatch, page: bytes, mode: str
) -> None:
    """🔴 AC-4.1: one institution's unreadable reply must not end the run.

    `_sync_one` promises it never raises for its connection's sake, and the loop
    above it relies on that to reach every other connection. The page read had no
    guard at all, so all three parse failures went straight past a catch naming
    `ConnectorError` and `StoreError` -- none of `JSONDecodeError`,
    `RecursionError` or `UnicodeDecodeError` is either -- and out of the run.
    Every connection queued behind this one then went unsynced, which is the one
    thing AC-4.1 says must never happen.

    Anchored on `_sync_one` rather than on the parse helper, because a guard that
    raises the right type and a caller that catches it are two halves and only
    the caller's half is this requirement. Degrading rather than reading an
    unparseable page as an empty one is the other half: `{}` would report a page
    run that finished cleanly, and silence is the one disallowed outcome.
    """
    _StubClient.page = page
    monkeypatch.setattr(sync_run, "PlaidClient", _StubClient)
    monkeypatch.setattr(sync_run, "get_access_token", lambda _c, _r: "access-token-for-tests")

    outcome = sync_run._sync_one(
        enrolled,
        "secret",
        connection_id=1,
        institution_name="First Platypus Bank",
        credential_ref="connection:sandbox:item-for-cursor-tests",
        # Named rather than defaulted, because the parameter is: a caller that
        # could forget it would stop pulling a domain with nothing saying so.
        # This connection reports none, so only the domains every connection has
        # run -- which is what this test is about.
        capabilities=frozenset(),
        wait=False,
    )

    assert outcome.degraded, f"a page {mode} ended the run instead of this connection"
    assert outcome.reason is not None and outcome.reason != ""
    assert _cursor(enrolled) is None, "an unreadable page moved the cursor"
    with reader_connection(enrolled) as conn:
        status = conn.execute(
            select(connections.c.status).where(connections.c.connection_id == 1)
        ).scalar_one()
    assert status == "degraded", "the failure was not recorded on the connection's own row"


@pytest.mark.parametrize(
    ("body", "mode"),
    [
        (b'{"added": [], "next_cursor": "x"', "malformed syntax"),
        (("[" * 100_000 + "]" * 100_000).encode(), "nested past the decoder's stack"),
        (b'{"next_cursor": "\xff\xfe", "has_more": false}', "bytes that are not UTF-8"),
    ],
    ids=["syntax", "depth", "encoding"],
)
def test_a_body_the_deriver_cannot_read_refuses_one_response_not_the_run(
    enrolled: Config, body: bytes, mode: str
) -> None:
    """The deriver's own read, and the reason two of the three used to escape.

    A body that cannot be parsed is a refusal to derive ONE response, and every
    caller of this seam is written against `DerivationError` -- `sync run`
    degrades the connection it belongs to, `store rebuild` reports which response
    it could not replay. `RecursionError` and `UnicodeDecodeError` share no base
    with `ValueError`, so a clause naming `JSONDecodeError` let them past both.
    The bytes are archived either way, which is what makes the refusal survivable.
    """
    with writer_connection(enrolled) as conn:
        with pytest.raises(DerivationError) as caught:
            apply_response(
                conn,
                connection_id=1,
                endpoint=TRANSACTIONS_SYNC.path,
                body=body,
                received_at=now_utc(),
                derivers=ALL_DERIVERS,
                replay_passes=(),
            )
        archived = conn.exec_driver_sql("SELECT COUNT(*) FROM raw_responses").scalar_one()

    assert archived == 1, f"a body {mode} was refused before it was archived"
    assert "raw response 1" in str(caught.value), (
        "the refusal does not name the row an operator would go and read"
    )
    assert _cursor(enrolled) is None


def _transactions_domain(config: Config) -> Any:
    with reader_connection(config) as conn:
        return (
            conn.execute(select(sync_state).where(sync_state.c.domain == TRANSACTIONS_DOMAIN))
            .mappings()
            .one_or_none()
        )


def test_a_complete_page_with_no_cursor_lands_the_domain(enrolled: Config) -> None:
    """🔴 A finished backfill that held nothing is still a finished backfill.

    An Item whose institution holds no cash accounts answers
    `HISTORICAL_UPDATE_COMPLETE` with no changes and an EMPTY `next_cursor`
    (measured against a real investment-only institution). The empty cursor is
    still not stored -- that rule is about the cursor -- but the domain got
    everything it asked for. Leaving `last_success_at` null reports it as never
    landed on every answer the connection contributes to, for as long as it
    exists.
    """
    _apply(enrolled, _page(next_cursor=None, status="HISTORICAL_UPDATE_COMPLETE"))

    domain = _transactions_domain(enrolled)
    assert domain is not None, "a finished feed left no record that it was attempted"
    assert domain["last_success_at"] is not None, "a finished feed was left never-landed"
    assert domain["cursor"] is None, "an empty cursor was stored as if it were one"


def test_a_complete_page_with_no_cursor_keeps_the_cursor_it_had(enrolled: Config) -> None:
    """Landing the domain must not cost it the place it had reached."""
    _apply(enrolled, _page(next_cursor=CURSOR_ONE))

    _apply(enrolled, _page(next_cursor=None, status="HISTORICAL_UPDATE_COMPLETE"))

    assert _cursor(enrolled) == CURSOR_ONE, "an empty cursor overwrote a good one"


def test_an_unfinished_page_with_no_cursor_does_not_land_the_domain(enrolled: Config) -> None:
    """The mirror, without which the rule above is satisfied by landing every page.

    `INITIAL_UPDATE_COMPLETE` hands over the first stretch of a backfill with the
    rest still arriving, so a page at that status with no cursor has proved
    nothing about the domain being complete.
    """
    _apply(enrolled, _page(next_cursor=None, status="INITIAL_UPDATE_COMPLETE"))

    assert _transactions_domain(enrolled) is None
