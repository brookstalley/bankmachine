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
from bankmachine.secrets import (
    SecretsError,
    delete_access_token,
    delete_plaid_secret,
    set_access_token,
    set_plaid_secret,
)
from bankmachine.store.engine import reader_connection, transaction, writer_connection
from bankmachine.store.schema import (
    TRANSACTIONS_DOMAIN,
    accounts,
    connections,
    institutions,
    raw_responses,
    sync_state,
)
from bankmachine.store.types import calendar_date, now_utc

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
    """

    error_codes: list[str | None] = [None]
    item_id = ITEM_ID
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
        self.item_gets += 1
        return FetchedResponse(
            endpoint=ITEM_GET,
            body=_item_body(item_id=FakeClient.item_id, error_code=FakeClient.error_codes[index]),
            received_at=now_utc(),
            request_context=None,
        )


@pytest.fixture(autouse=True)
def _reset_fake() -> None:
    FakeClient.instances = []
    FakeClient.error_codes = [None]
    FakeClient.item_id = ITEM_ID


@pytest.fixture
def offline_client(monkeypatch: pytest.MonkeyPatch) -> type[FakeClient]:
    monkeypatch.setattr("bankmachine.cli.connections.PlaidClient", FakeClient)
    monkeypatch.setattr("bankmachine.cli.hosted_link.time.sleep", lambda _seconds: None)
    return FakeClient


@pytest.fixture
def cli_env(initialized_config: Config, monkeypatch: pytest.MonkeyPatch) -> Iterator[Config]:
    config = initialized_config
    monkeypatch.setenv("BANKMACHINE_DATASTORE_PATH", str(config.datastore_path))
    monkeypatch.setenv("BANKMACHINE_LOG_DIR", str(config.log_dir))
    monkeypatch.setenv("BANKMACHINE_KEYCHAIN_SERVICE", config.keychain_service)
    monkeypatch.setenv("BANKMACHINE_ENVIRONMENT", config.environment)
    monkeypatch.setenv("BANKMACHINE_CONFIG", str(config.datastore_path.parent / "absent.toml"))
    monkeypatch.setenv("BANKMACHINE_PLAID_CLIENT_ID", "test-client-id")
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
        conn.execute(
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

    out = capsys.readouterr().out
    assert "https://secure.example/hl/repair" in out
    assert "sync run" in out


def test_the_repair_waits_for_the_item_rather_than_reading_it_once(
    degraded_connection: Config, offline_client: type[FakeClient]
) -> None:
    """🔴 The answer must CHANGE between polls, or this cannot see a poll loop at all.

    A fake that always reports a healthy item passes against an implementation
    that never looks twice, which is the whole failure mode a one-shot fixture
    hides (`learnings.md` -- write the guard from the failure's point of view).
    """
    FakeClient.error_codes = [LOGIN_REQUIRED, LOGIN_REQUIRED, None]

    assert run(["connections", "reauth", "1"]) == 0
    assert FakeClient.instances[-1].item_gets == 3
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
    """
    FakeClient.item_id = OTHER_ITEM_ID

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
    assert FakeClient.item_id == ITEM_ID
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
