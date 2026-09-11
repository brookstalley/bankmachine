"""`bankmachine connections reauth` -- AC-4.3, driven end to end.

🔴 **These tests are written from the failure's point of view.** The requirement
is that a repair loses nothing, so every fixture here is built to HOLD the things
a re-link destroys -- a cursor, a measured granted window, an `enrolled_at`, an
account row, a transaction -- and the assertions are that they are still there
afterwards. A fixture with no cursor could not tell a repair from a re-link, and
would have passed against the very code this command exists to replace.

The aggregator is faked, for the reason `test_enroll.py` records: a Hosted Link
session cannot be completed programmatically. What a live call can prove -- that
an update-mode token comes back with a hosted URL -- is proven in
`tests/connector/test_sandbox.py`. What only a human can prove is queued in
`.prawduct/operator-verification.md`.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from sqlalchemy import insert, select, update

from bankmachine.cli import run
from bankmachine.config import Config
from bankmachine.connector import ITEM_GET, FetchedResponse, RepairSession
from bankmachine.derivers import ALL_DERIVERS
from bankmachine.secrets import (
    SecretsError,
    delete_access_token,
    delete_plaid_secret,
    set_access_token,
    set_plaid_secret,
)
from bankmachine.store.derivation import ensure_derivation_version
from bankmachine.store.engine import reader_connection, transaction, writer_connection
from bankmachine.store.rebuild import rebuild
from bankmachine.store.schema import (
    TRANSACTIONS_DOMAIN,
    accounts,
    connections,
    institutions,
    manual_imports,
    raw_responses,
    sync_state,
    transactions,
)
from bankmachine.store.types import calendar_date, minor_units, now_utc
from conftest import use_cli_env

# Declared per line, as any file must. The real token SHAPE is carried because a
# redaction sweep proves nothing against a value that could not have been
# mistaken for a credential in the first place.
ACCESS_TOKEN = "access-sandbox-fake-for-tests"  # credential-shape: test vector
ITEM_ID = "item-fake-for-tests"
OTHER_ITEM_ID = "item-fake-but-different"
INSTITUTION_ID = "ins_109508"
CURSOR = "cursor-issued-by-the-item-that-must-survive"
GRANTED_DAYS = 180
ENROLLED_ON = date(2024, 1, 1)

LOGIN_REQUIRED = "ITEM_LOGIN_REQUIRED"


def _item_body(*, item_id: str = ITEM_ID, error_code: str | None = None) -> bytes:
    """An `/item/get` body, complaining or not.

    Keys and shape follow the live one recorded in `api-notes-plaid.md` §13. The
    institution is the aggregator's own fictional sandbox bank, never one from
    `deployment/`.
    """
    error = (
        None
        if error_code is None
        else {
            "error_type": "ITEM_ERROR",
            "error_code": error_code,
            "error_message": "the login details are no longer valid",
        }
    )
    return json.dumps(
        {
            "item": {
                "item_id": item_id,
                "institution_id": INSTITUTION_ID,
                "institution_name": "First Platypus Bank",
                "products": ["transactions"],
                "available_products": ["investments"],
                "billed_products": ["transactions"],
                "error": error,
                "update_type": "background",
                "webhook": "",
                "consent_expiration_time": None,
            },
            "status": {},
            "request_id": "req-fake",
        }
    ).encode()


class FakeClient:
    """A `PlaidClient` that answers without a network, and remembers what it was asked.

    🔴 `error_codes` is a SCRIPT, one entry per `/item/get`, because the thing
    under test is a poll. A fake returning one fixed answer cannot tell polling
    from a single read -- the repair would pass against an implementation that
    never looked twice.

    🔴 **The first entry is the PRE-FLIGHT read, not a poll.** The command
    establishes that the login really is expired before it prints a URL, so a
    script starting at `None` describes a connection with nothing to repair and
    the command refuses it -- correctly. A repair therefore reads
    `[LOGIN_REQUIRED, ..., None]`: expired when asked, clear once the operator
    has finished.
    """

    error_codes: list[str | None] = [LOGIN_REQUIRED, None]
    #: A SCRIPT for the same reason `error_codes` is one. "The item came back
    #: different" is a change ACROSS the session -- this row's item before it,
    #: a successor after -- and a fake holding one fixed id cannot express that
    #: separately from "the stored credential never opened this item at all",
    #: which is a different refusal on a different path.
    item_ids: list[str] = [ITEM_ID]
    instances: list[FakeClient] = []

    def __init__(self, config: Config, secret: str, **kwargs: Any) -> None:
        self.config = config
        self.update_mode_access_token: str | None = None
        self.update_mode_kwargs: dict[str, Any] = {}
        self.item_gets = 0
        FakeClient.instances.append(self)

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def link_token_create_update(
        self, *, access_token: str, client_user_id: str, country_codes: list[str], **kwargs: Any
    ) -> RepairSession:
        self.update_mode_access_token = access_token
        self.update_mode_kwargs = {
            "client_user_id": client_user_id,
            "country_codes": country_codes,
            **kwargs,
        }
        return RepairSession(
            expires_at="2026-09-08T00:00:00Z",
            hosted_link_url="https://secure.example/hl/repair",
        )

    def item_get(self, access_token: str, **kwargs: Any) -> FetchedResponse:
        index = min(self.item_gets, len(FakeClient.error_codes) - 1)
        id_index = min(self.item_gets, len(FakeClient.item_ids) - 1)
        self.item_gets += 1
        return FetchedResponse(
            endpoint=ITEM_GET,
            body=_item_body(
                item_id=FakeClient.item_ids[id_index],
                error_code=FakeClient.error_codes[index],
            ),
            received_at=now_utc(),
            request_context=None,
        )


@pytest.fixture(autouse=True)
def _reset_fake() -> None:
    FakeClient.instances = []
    FakeClient.error_codes = [LOGIN_REQUIRED, None]
    FakeClient.item_ids = [ITEM_ID]


@pytest.fixture
def offline_client(monkeypatch: pytest.MonkeyPatch) -> type[FakeClient]:
    monkeypatch.setattr("bankmachine.cli.connections.PlaidClient", FakeClient)
    monkeypatch.setattr("bankmachine.cli.hosted_link.time.sleep", lambda _seconds: None)
    return FakeClient


@pytest.fixture
def cli_env(initialized_config: Config, monkeypatch: pytest.MonkeyPatch) -> Iterator[Config]:
    config = initialized_config
    use_cli_env(monkeypatch, config, BANKMACHINE_PLAID_CLIENT_ID="test-client-id")
    set_plaid_secret(config, "test-secret")
    try:
        yield config
    finally:
        delete_plaid_secret(config)
        # Specific, not broad: `delete_access_token` tolerates absence already, so
        # the only thing left to suppress is a keychain failure during teardown,
        # which must not mask the result of the test that just ran.
        with contextlib.suppress(SecretsError):
            delete_access_token(config, config.connection_keychain_account(ITEM_ID))


@pytest.fixture
def degraded_connection(cli_env: Config) -> Config:
    """A connection in exactly the state an expired login leaves behind.

    🔴 Everything a re-link would destroy is seeded HERE rather than left absent:
    the cursor, the measured granted window, the original `enrolled_at`, and an
    account. The preservation assertions are worthless without them -- an absent
    cursor cannot be shown to have survived.
    """
    config = cli_env
    now = now_utc()
    set_access_token(config, config.connection_keychain_account(ITEM_ID), ACCESS_TOKEN)
    with writer_connection(config) as conn, transaction(conn):
        institution_key = conn.execute(
            insert(institutions).values(
                source_institution_id=INSTITUTION_ID,
                name="First Platypus Bank",
                first_seen_at=now,
                last_seen_at=now,
            )
        ).inserted_primary_key
        assert institution_key is not None  # an INTEGER PRIMARY KEY insert always yields one
        institution_id = int(institution_key[0])
        connection_key = conn.execute(
            insert(connections).values(
                institution_id=institution_id,
                source_connection_id=ITEM_ID,
                credential_ref=config.connection_keychain_account(ITEM_ID),
                capabilities=json.dumps(["transactions"]),
                requested_history_days=730,
                granted_history_days=GRANTED_DAYS,
                status="degraded",
                last_error_code=LOGIN_REQUIRED,
                last_error_at=now,
                enrolled_at=now,
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key
        assert connection_key is not None  # an INTEGER PRIMARY KEY insert always yields one
        connection_id = int(connection_key[0])
        conn.execute(
            insert(sync_state).values(
                connection_id=connection_id,
                domain=TRANSACTIONS_DOMAIN,
                cursor=CURSOR,
                history_start_date=calendar_date(ENROLLED_ON),
                updated_at=now,
            )
        )
        account_key = conn.execute(
            insert(accounts).values(
                institution_id=institution_id,
                connection_id=connection_id,
                source_account_id="acct-1",
                name="Everyday Checking",
                account_type="depository",
                balance_class="asset",
                currency="USD",
                lifecycle_status="active",
                first_seen_date=calendar_date(ENROLLED_ON),
                source="aggregator",
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key
        assert account_key is not None  # an INTEGER PRIMARY KEY insert always yields one
        # 🔴 A transaction, because the command PRINTS that transactions are
        # unchanged and that half of the claim had nothing reading it. A repair
        # writes one `connections` row, so the risk is small -- but "small" is
        # the reason a claim goes unchecked, not a reason it should.
        import_key = conn.execute(
            insert(manual_imports).values(
                account_id=int(account_key[0]),
                adapter="csv",
                source_name="export.csv",
                file_sha256="0" * 64,
                file_bytes=100,
                imported_at=now,
                rows_seen=1,
                rows_applied=1,
            )
        ).inserted_primary_key
        assert import_key is not None  # an INTEGER PRIMARY KEY insert always yields one
        # Manual rather than aggregator-sourced: an aggregator row must carry the
        # `raw_response_id` it was derived from, and seeding one here would tie
        # this fixture to a transactions page the repair never fetches. What is
        # being asserted is that a repair leaves transaction rows alone, and a
        # row is a row.
        conn.execute(
            insert(transactions).values(
                account_id=int(account_key[0]),
                pending=0,
                posted_date=calendar_date(ENROLLED_ON),
                amount_minor=minor_units(-42_00),
                currency="USD",
                description="Coffee",
                source="manual",
                manual_import_id=int(import_key[0]),
                import_fingerprint="fp-reauth-1",
                derivation_version_id=ensure_derivation_version(conn),
                first_seen_at=now,
                updated_at=now,
            )
        )
    return config


def _connection(config: Config) -> Any:
    with reader_connection(config) as conn:
        return conn.execute(connections.select()).one()._mapping


def _cursor(config: Config) -> str | None:
    with reader_connection(config) as conn:
        return conn.execute(select(sync_state.c.cursor)).scalar_one_or_none()


def _account_ids(config: Config) -> list[int]:
    with reader_connection(config) as conn:
        return [int(row[0]) for row in conn.execute(select(accounts.c.account_id)).all()]


def _transaction_rows(config: Config) -> list[tuple[Any, ...]]:
    with reader_connection(config) as conn:
        return [
            tuple(row)
            for row in conn.execute(
                select(
                    transactions.c.transaction_id,
                    transactions.c.amount_minor,
                    transactions.c.description,
                    transactions.c.updated_at,
                )
            ).all()
        ]


# --------------------------------------------------------------------------
# AC-4.3 -- the repair, and what it must not touch
# --------------------------------------------------------------------------


def test_a_repair_clears_the_error_and_leaves_every_item_scoped_value_alone(
    degraded_connection: Config,
    offline_client: type[FakeClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """🔴 The whole requirement: repaired, and nothing a re-link destroys was destroyed."""
    config = degraded_connection
    before = _connection(config)
    accounts_before = _account_ids(config)
    transactions_before = _transaction_rows(config)

    assert run(["connections", "reauth", "1"]) == 0

    after = _connection(config)
    assert after["status"] == "active"
    assert after["last_error_code"] is None
    assert after["last_error_at"] is None
    # Each of these is a value `enroll`'s convergence clears on purpose.
    assert _cursor(config) == CURSOR
    assert after["granted_history_days"] == GRANTED_DAYS
    assert after["enrolled_at"] == before["enrolled_at"]
    assert after["source_connection_id"] == ITEM_ID
    assert after["credential_ref"] == before["credential_ref"]
    assert after["requested_history_days"] == before["requested_history_days"]
    assert _account_ids(config) == accounts_before
    # The command PRINTS that the transactions are unchanged; this is the half of
    # that claim that reads it back.
    assert _transaction_rows(config) == transactions_before

    out = capsys.readouterr().out
    assert "https://secure.example/hl/repair" in out
    assert "sync run" in out


def test_a_connection_that_is_not_reporting_an_expired_login_is_refused(
    degraded_connection: Config,
    offline_client: type[FakeClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """🔴 The command must establish the condition it claims to have repaired.

    Completion here is "the item is no longer reporting an expired login", and an
    item that was never reporting one satisfies that on the first look -- before
    the operator has opened the printed URL. Without a check before the URL, this
    fixture reaches `repaired connection 1` and a `status="active"` write for a
    session nobody completed.
    """
    FakeClient.error_codes = [None]

    assert run(["connections", "reauth", "1"]) == 1

    # Nothing was written: the row is exactly as degraded as it was.
    assert _connection(degraded_connection)["status"] == "degraded"
    err = capsys.readouterr().err
    assert "not reporting an expired login" in err
    # The stale-flag case has its own remedy, and it is not this command.
    assert "sync run" in err


def test_a_connection_locked_at_the_bank_is_refused_before_a_url_is_printed(
    degraded_connection: Config,
    offline_client: type[FakeClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The negative control's other half: a complaint update mode cannot clear.

    `ITEM_LOCKED`'s remedy is at the operator's own bank. Reached before the URL,
    it costs them nothing; reached after, the command tells them the aggregator
    "accepted the new login", which is an assertion about a session that never
    happened.
    """
    FakeClient.error_codes = ["ITEM_LOCKED"]

    assert run(["connections", "reauth", "1"]) == 1

    assert _connection(degraded_connection)["status"] == "degraded"
    captured = capsys.readouterr()
    assert "ITEM_LOCKED" in captured.err
    assert "https://secure.example/hl/repair" not in captured.out


def test_a_credential_that_opens_a_different_item_is_refused_before_any_session(
    degraded_connection: Config,
    offline_client: type[FakeClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Foreign from the first read is a different fault from foreign after the session.

    The stored credential no longer opens the item this row names, so there is
    nothing to repair in place and no session worth printing. Distinguished from
    the post-session guard because the message an operator can act on differs.
    """
    FakeClient.item_ids = [OTHER_ITEM_ID]

    assert run(["connections", "reauth", "1"]) == 1

    row = _connection(degraded_connection)
    assert row["status"] == "degraded"
    assert row["source_connection_id"] == ITEM_ID
    captured = capsys.readouterr()
    assert OTHER_ITEM_ID in captured.err
    assert "https://secure.example/hl/repair" not in captured.out


def test_a_foreign_item_does_not_derive_onto_this_connection(
    degraded_connection: Config, offline_client: type[FakeClient]
) -> None:
    """🔴 The refusal path says "Nothing was changed", so nothing may have changed.

    `derive_item_get` writes `consent_expires_at` onto whichever connection the
    response was fetched for, and `query.py` turns that column into the consent
    caveats the MCP surface attaches to every answer about this connection. A
    foreign item's consent date landing there would make the datastore warn about
    this connection on the strength of a body describing a different one.
    """
    FakeClient.item_ids = [ITEM_ID, OTHER_ITEM_ID]

    assert run(["connections", "reauth", "1"]) == 1

    assert _connection(degraded_connection)["consent_expires_at"] is None
    # Kept, though: FR-5 wants every response, and this one evidences the refusal.
    with reader_connection(degraded_connection) as conn:
        foreign = conn.execute(
            select(raw_responses.c.connection_id).where(raw_responses.c.connection_id.is_(None))
        ).all()
    assert len(foreign) == 1


def test_a_repaired_connection_can_still_be_rebuilt(
    degraded_connection: Config, offline_client: type[FakeClient]
) -> None:
    """🔴 `store rebuild` must not report a repaired connection as content-changed.

    This is the first path in the product that archives `/item/get` against a real
    connection id, which is what makes the item-standing deriver reachable on
    replay. If that deriver also owned `connections.updated_at` -- which the
    commands that change the row stamp with the clock -- the replay would
    overwrite the live value with the archived `received_at`, and `rebuild` would
    refuse at an unchanged derivation version, blaming an impure deriver or a
    pruned archive. `operational-spec.md` sends the operator here after exactly
    this repair.
    """
    assert run(["connections", "reauth", "1"]) == 0

    report = rebuild(degraded_connection, derivers=ALL_DERIVERS)

    assert not report.content_changed


def test_the_repair_waits_for_the_item_rather_than_reading_it_once(
    degraded_connection: Config, offline_client: type[FakeClient]
) -> None:
    """🔴 The answer must CHANGE between polls, or this cannot see a poll loop at all.

    A fake that always reports a healthy item passes against an implementation
    that never looks twice, which is the whole failure mode a one-shot fixture
    hides (`learnings.md` -- write the guard from the failure's point of view).
    """
    FakeClient.error_codes = [LOGIN_REQUIRED, LOGIN_REQUIRED, LOGIN_REQUIRED, None]

    assert run(["connections", "reauth", "1"]) == 0
    # One pre-flight read plus three polls: the last is the one that changed.
    assert FakeClient.instances[-1].item_gets == 4
    assert _connection(degraded_connection)["status"] == "active"


def test_an_abandoned_repair_writes_nothing_and_exits_one(
    degraded_connection: Config,
    offline_client: type[FakeClient],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The operator walked away: the connection is exactly as degraded as it was."""
    FakeClient.error_codes = [LOGIN_REQUIRED]
    clock = iter([0.0, 0.0, 10_000.0])
    monkeypatch.setattr("bankmachine.cli.hosted_link.time.monotonic", lambda: next(clock))

    assert run(["connections", "reauth", "1"]) == 1

    row = _connection(degraded_connection)
    assert row["status"] == "degraded"
    assert row["last_error_code"] == LOGIN_REQUIRED
    assert _cursor(degraded_connection) == CURSOR
    assert "reauth 1" in capsys.readouterr().err


def test_an_item_that_came_back_different_is_refused_and_nothing_is_repaired(
    degraded_connection: Config,
    offline_client: type[FakeClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """🔴 The guard the command rests on.

    A different item id means a SECOND item now stands behind this connection --
    every account re-issued, every transaction counted twice, every total doubled
    with no warning naming it. Left degraded is recoverable; marked active is not.

    🔴 The id changes ACROSS the session: this row's item on the pre-flight read,
    a successor once the operator has finished. An id that was foreign from the
    first read is a different fault with a different message, and a fixture that
    could not tell them apart would pass against a command that only had one.
    """
    FakeClient.item_ids = [ITEM_ID, OTHER_ITEM_ID]

    assert run(["connections", "reauth", "1"]) == 1

    row = _connection(degraded_connection)
    assert row["status"] == "degraded"
    assert row["source_connection_id"] == ITEM_ID
    assert _cursor(degraded_connection) == CURSOR
    err = capsys.readouterr().err
    assert OTHER_ITEM_ID in err
    assert "counted twice" in err


def test_the_matching_item_is_the_negative_control_for_that_guard(
    degraded_connection: Config, offline_client: type[FakeClient]
) -> None:
    """The same fixture with the item id left alone must succeed.

    Without this, the refusal above would still pass against code that refused
    every repair.
    """
    assert FakeClient.item_ids == [ITEM_ID]
    assert run(["connections", "reauth", "1"]) == 0
    assert _connection(degraded_connection)["status"] == "active"


def test_an_item_complaining_about_something_else_is_not_reported_as_repaired(
    degraded_connection: Config,
    offline_client: type[FakeClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Update mode renewed the login and the item is unwell for another reason.

    Not a failure of the command and not a success either, so the row keeps a
    complaint -- and the operator is told which one, because `ITEM_LOCKED`'s
    remedy is at their bank and no amount of re-authenticating reaches it.
    """
    FakeClient.error_codes = [LOGIN_REQUIRED, "ITEM_LOCKED"]

    assert run(["connections", "reauth", "1"]) == 1

    assert _connection(degraded_connection)["status"] == "degraded"
    assert "ITEM_LOCKED" in capsys.readouterr().err


def test_every_poll_is_archived(
    degraded_connection: Config, offline_client: type[FakeClient]
) -> None:
    """AC-5.1 holds on the repair path too: a repair is a state change, verbatim."""
    FakeClient.error_codes = [LOGIN_REQUIRED, None]

    assert run(["connections", "reauth", "1"]) == 0

    with reader_connection(degraded_connection) as conn:
        archived = conn.execute(
            select(raw_responses.c.endpoint, raw_responses.c.connection_id)
        ).all()
    # The pre-flight read and the one poll that answered.
    assert len(archived) == 2
    assert {row[0] for row in archived} == {ITEM_GET.path}
    assert {row[1] for row in archived} == {1}


def test_the_update_mode_request_carries_the_connections_own_token(
    degraded_connection: Config, offline_client: type[FakeClient]
) -> None:
    """Update mode is Item-scoped: the wrong token would repair the wrong connection."""
    assert run(["connections", "reauth", "1", "--timeout", "120"]) == 0

    client = FakeClient.instances[-1]
    assert client.update_mode_access_token == ACCESS_TOKEN
    # 🔴 One number, not two that agree: the URL must die when this side stops
    # watching, or the operator completes a session nothing is waiting for.
    assert client.update_mode_kwargs["hosted_url_lifetime_seconds"] == 120


# --------------------------------------------------------------------------
# The refusals, each naming a remedy that exists
# --------------------------------------------------------------------------


def test_an_unknown_connection_is_refused(
    cli_env: Config,
    offline_client: type[FakeClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(["connections", "reauth", "99"]) == 1
    assert "no connection 99" in capsys.readouterr().err


def test_a_retired_connection_has_no_login_to_repair(
    degraded_connection: Config,
    offline_client: type[FakeClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`retire` removed the item at the aggregator, so update mode has nothing to open.

    Naming `enroll` here is correct precisely because there is no live connection
    left to duplicate.
    """
    with writer_connection(degraded_connection) as conn, transaction(conn):
        conn.execute(update(connections).values(retired_at=now_utc(), status="retired"))

    assert run(["connections", "reauth", "1"]) == 1
    err = capsys.readouterr().err
    assert "retired" in err
    assert "bankmachine enroll" in err


def test_a_connection_with_no_stored_credential_says_so_rather_than_offering_a_retry(
    degraded_connection: Config,
    offline_client: type[FakeClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without the token there is no handle to the item, so nothing here can reach it."""
    delete_access_token(
        degraded_connection, degraded_connection.connection_keychain_account(ITEM_ID)
    )

    assert run(["connections", "reauth", "1"]) == 1
    err = capsys.readouterr().err
    assert "no stored credential" in err
    assert ITEM_ID in err


def test_a_body_without_an_item_id_cannot_support_the_comparison(
    degraded_connection: Config,
    offline_client: type[FakeClient],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The guard refuses to be skipped: an unidentifiable item is an error, not a pass.

    Exit `2` -- *could not run* -- rather than `1`, because nothing was found to
    be wrong with the connection; the aggregator answered in a shape this side
    cannot read. Collapsing the two is what the exit-code contract forbids.
    """

    def nameless(self: FakeClient, access_token: str, **kwargs: Any) -> FetchedResponse:
        body = json.loads(_item_body())
        del body["item"]["item_id"]
        return FetchedResponse(
            endpoint=ITEM_GET,
            body=json.dumps(body).encode(),
            received_at=now_utc(),
            request_context=None,
        )

    monkeypatch.setattr(FakeClient, "item_get", nameless)

    assert run(["connections", "reauth", "1"]) == 2
    assert "item_id" in capsys.readouterr().err
    assert _connection(degraded_connection)["status"] == "degraded"
