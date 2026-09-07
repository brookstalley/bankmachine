"""`bankmachine enroll` -- FR-1's acceptance criteria, driven end to end.

The aggregator is faked here, and the reason is worth stating rather than
assumed: a Hosted Link session cannot be completed programmatically. Plaid's
`/sandbox/public_token/create` bypasses Link entirely, so it mints a public
token without ever creating a session `/link/token/get` would report. What a
live test can prove -- that the hosted URL comes back, and that an unfinished
session omits `link_sessions` -- is proven in `tests/connector/test_sandbox.py`.
What only a human can prove is queued in `.prawduct/operator-verification.md`.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest

from bankmachine.cli import run
from bankmachine.config import Config
from bankmachine.connector import ITEM_GET, AccessGrant, FetchedResponse, LinkSession, LinkToken
from bankmachine.secrets import (
    AccessTokenMissingError,
    SecretsError,
    delete_access_token,
    delete_plaid_secret,
    get_access_token,
    set_plaid_secret,
)
from bankmachine.store.engine import reader_connection
from bankmachine.store.schema import connections, institutions, raw_responses
from bankmachine.store.types import now_utc

ACCESS_TOKEN = "access-sandbox-fake-for-tests"
PUBLIC_TOKEN = "public-sandbox-fake-for-tests"
ITEM_ID = "item-fake-for-tests"
INSTITUTION_ID = "ins_109508"

#: The item body enrollment derives its institution and capabilities from. Keys
#: and shape follow the live one recorded in `api-notes-plaid.md` §13; the
#: institution is the aggregator's own fictional sandbox bank, never one from
#: `deployment/`.
ITEM_BODY = json.dumps(
    {
        "item": {
            "item_id": ITEM_ID,
            "institution_id": INSTITUTION_ID,
            "institution_name": "First Platypus Bank",
            "products": ["transactions"],
            "available_products": ["investments", "liabilities", "identity"],
            "billed_products": ["transactions"],
            "error": None,
            "update_type": "background",
            "webhook": "",
            "consent_expiration_time": None,
        },
        "status": {},
        "request_id": "req-fake",
    }
).encode()


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
        # Specific, not broad: `delete_access_token` already tolerates absence, so
        # the only thing left to suppress is a keychain failure during teardown,
        # which must not mask the result of the test that just ran.
        with contextlib.suppress(SecretsError):
            delete_access_token(config, config.connection_keychain_account(ITEM_ID))


class FakeClient:
    """A `PlaidClient` that answers without a network, and remembers what it was asked."""

    polls_before_finished = 0
    instances: list[FakeClient] = []

    def __init__(self, config: Config, secret: str, **kwargs: Any) -> None:
        self.config = config
        self.requested_history_days: int | None = None
        self.requested_products: list[str] | None = None
        self.polls = 0
        self.exchanged: str | None = None
        FakeClient.instances.append(self)

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def link_token_create(
        self, *, history_days: int, client_user_id: str, country_codes: list[str],
        products: list[str], **kwargs: Any,
    ) -> LinkToken:
        self.requested_history_days = history_days
        self.requested_products = products
        return LinkToken(
            token="link-sandbox-fake",
            expires_at="2026-09-08T00:00:00Z",
            requested_history_days=history_days,
            hosted_link_url="https://secure.example/hl/session",
        )

    def link_token_get(self, link_token: str) -> LinkSession:
        self.polls += 1
        if self.polls <= FakeClient.polls_before_finished:
            return LinkSession(public_token=None, session_id="session-1", institution_id=None)
        return LinkSession(
            public_token=PUBLIC_TOKEN, session_id="session-1", institution_id=INSTITUTION_ID
        )

    def exchange_public_token(self, public_token: str) -> AccessGrant:
        self.exchanged = public_token
        return AccessGrant(access_token=ACCESS_TOKEN, source_connection_id=ITEM_ID)

    def item_get(self, access_token: str, **kwargs: Any) -> FetchedResponse:
        return FetchedResponse(
            endpoint=ITEM_GET, body=ITEM_BODY, received_at=now_utc(), request_context=None
        )


@pytest.fixture(autouse=True)
def _reset_fake() -> None:
    FakeClient.instances = []
    FakeClient.polls_before_finished = 0


@pytest.fixture
def offline_client(monkeypatch: pytest.MonkeyPatch) -> type[FakeClient]:
    monkeypatch.setattr("bankmachine.cli.enroll.PlaidClient", FakeClient)
    monkeypatch.setattr("bankmachine.cli.enroll.time.sleep", lambda _seconds: None)
    return FakeClient


def _rows(config: Config, table: Any) -> list[Any]:
    with reader_connection(config) as conn:
        return list(conn.execute(table.select()).all())


# --------------------------------------------------------------------------
# AC-1.3 / AC-1.3a — what one enrollment records
# --------------------------------------------------------------------------


def test_enrollment_records_the_requested_window_and_leaves_granted_unknown(
    cli_env: Config, offline_client: type[FakeClient], capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 Null granted is the CORRECT post-enrollment state, not a gap.

    No response in the enrollment path reports what the aggregator granted
    (AC-1.3a), so a connection that claimed a granted window here would be
    claiming something nothing measured.
    """
    assert run(["enroll", "--yes"]) == 0

    rows = _rows(cli_env, connections)
    assert len(rows) == 1
    row = rows[0]._mapping
    assert row["requested_history_days"] == cli_env.history_days
    assert row["granted_history_days"] is None
    assert row["status"] == "active"
    assert row["retired_at"] is None
    assert row["source_connection_id"] == ITEM_ID

    out = capsys.readouterr().out
    assert "not yet known" in out, "the operator must not read null as a full grant"


def test_the_institution_comes_from_the_item_not_the_catalogue(
    cli_env: Config, offline_client: type[FakeClient]
) -> None:
    """The roster holds the operator's institutions, one per connection."""
    assert run(["enroll", "--yes"]) == 0

    rows = _rows(cli_env, institutions)
    assert len(rows) == 1, "one enrollment adds one institution, not a catalogue page"
    assert rows[0]._mapping["source_institution_id"] == INSTITUTION_ID


def test_capabilities_are_what_the_connection_could_do(
    cli_env: Config, offline_client: type[FakeClient]
) -> None:
    """AC-3.2 reads `available_products`, so investments is discoverable."""
    assert run(["enroll", "--yes"]) == 0

    stored = json.loads(_rows(cli_env, connections)[0]._mapping["capabilities"])
    assert "investments" in stored


def test_the_configured_window_is_what_enrollment_asks_for(
    cli_env: Config, offline_client: type[FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 Driven from config, not a literal.

    AC-1.2 is irreversible per connection, so a command that ignored the
    operator's configured window would be discovered years later, by which time
    every institution needs re-linking.
    """
    monkeypatch.setenv("BANKMACHINE_HISTORY_DAYS", "365")

    assert run(["enroll", "--yes"]) == 0

    assert FakeClient.instances[0].requested_history_days == 365
    assert _rows(cli_env, connections)[0]._mapping["requested_history_days"] == 365


def test_both_products_are_requested_at_enrollment(
    cli_env: Config, offline_client: type[FakeClient]
) -> None:
    """The owner's decision of 2026-09-07, against the recommendation.

    Asserted because it is a billing consequence per connection and cannot be
    changed for an existing one without re-linking -- so a silent drift back to
    transactions-only would be both costly and invisible.
    """
    assert run(["enroll", "--yes"]) == 0

    assert FakeClient.instances[0].requested_products == ["transactions", "investments"]


# --------------------------------------------------------------------------
# AC-10.1 — the access token, and where it must never be
# --------------------------------------------------------------------------


def test_the_access_token_is_in_the_keychain_and_the_datastore_holds_a_handle(
    cli_env: Config, offline_client: type[FakeClient]
) -> None:
    """🔴 `credential_ref` names a keychain account; it is never the token.

    A datastore backup travels, and `raw_responses` is append-only, so a token
    written into either is written permanently.
    """
    assert run(["enroll", "--yes"]) == 0

    credential_ref = _rows(cli_env, connections)[0]._mapping["credential_ref"]
    assert ACCESS_TOKEN not in credential_ref
    assert get_access_token(cli_env, credential_ref) == ACCESS_TOKEN


def test_no_access_token_reaches_any_column_of_the_datastore(
    cli_env: Config, offline_client: type[FakeClient]
) -> None:
    """Swept across every table, not just the ones enrollment was expected to write.

    A test naming `connections` would pass while the token sat in the archive,
    which is the table where it would be permanent.
    """
    assert run(["enroll", "--yes"]) == 0

    with reader_connection(cli_env) as conn:
        tables = [
            row[0]
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        ]
        for table in tables:
            dumped = str(conn.exec_driver_sql(f"SELECT * FROM {table}").fetchall())  # noqa: S608
            assert ACCESS_TOKEN not in dumped, f"the access token reached {table}"
            assert PUBLIC_TOKEN not in dumped, f"the public token reached {table}"


def test_no_credential_reaches_the_enrollment_log(
    cli_env: Config, offline_client: type[FakeClient], caplog: pytest.LogCaptureFixture
) -> None:
    """Asserted over a real enrollment's records rather than over the formatter alone.

    The formatter redacts by credential shape, and inheriting that is the plan's
    claim -- but a claim about inheritance is exactly the kind this project has
    been burned by, so the run itself is what gets checked.
    """
    with caplog.at_level(logging.DEBUG, logger="bankmachine"):
        assert run(["enroll", "--yes"]) == 0

    emitted = "\n".join(record.getMessage() for record in caplog.records)
    assert ACCESS_TOKEN not in emitted
    assert PUBLIC_TOKEN not in emitted


def test_the_exchange_response_never_becomes_an_archived_row(
    cli_env: Config, offline_client: type[FakeClient]
) -> None:
    """Only the item response is archivable; the exchange cannot be."""
    assert run(["enroll", "--yes"]) == 0

    archived = {row._mapping["endpoint"] for row in _rows(cli_env, raw_responses)}
    assert archived == {ITEM_GET.path}


# --------------------------------------------------------------------------
# AC-1.4 — idempotency
# --------------------------------------------------------------------------


def test_re_enrolling_the_same_institution_updates_rather_than_duplicates(
    cli_env: Config, offline_client: type[FakeClient]
) -> None:
    """AC-1.4, and the partial unique index is what makes it structural."""
    assert run(["enroll", "--yes"]) == 0
    first = _rows(cli_env, connections)[0]._mapping
    first_id, enrolled_at = first["connection_id"], first["enrolled_at"]

    assert run(["enroll", "--yes"]) == 0

    rows = _rows(cli_env, connections)
    assert len(rows) == 1, "a second enrollment must not add a second live connection"
    second = rows[0]._mapping
    assert second["connection_id"] == first_id
    # Preserved: it records when the operator first linked this institution, and
    # re-linking after an expired login is that enrollment continuing.
    assert second["enrolled_at"] == enrolled_at
    assert len(_rows(cli_env, institutions)) == 1


# --------------------------------------------------------------------------
# AC-1.5 — the cap, and the exit-code contract
# --------------------------------------------------------------------------


def test_the_cap_refuses_before_anything_is_minted(
    cli_env: Config,
    offline_client: type[FakeClient],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """🔴 Refused before the link token exists, not after the token is spent.

    Checking after the exchange would leave the operator paying for an Item this
    side refuses to record -- and they would have completed a browser flow to
    get it.
    """
    monkeypatch.setenv("BANKMACHINE_CONNECTION_CAP", "1")
    assert run(["enroll", "--yes"]) == 0
    calls_after_first = len(FakeClient.instances)

    assert run(["enroll", "--yes"]) == 1

    assert len(FakeClient.instances) == calls_after_first, (
        "the cap refusal reached the aggregator, so a token was minted anyway"
    )
    err = capsys.readouterr().err
    assert "cap is 1" in err
    # AC-1.5 asks for the limit explained AND the connections listed, so the
    # operator can act on it rather than only be told no.
    assert "First Platypus Bank" in err
    assert "retire" in err


def test_a_cap_refusal_exits_one_because_the_command_ran(
    cli_env: Config, offline_client: type[FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 1/2 split is a machine interface and is not collapsible.

    A scheduled job reads these: `2` would make a full connection roster
    indistinguishable from a broken install.
    """
    monkeypatch.setenv("BANKMACHINE_CONNECTION_CAP", "1")
    assert run(["enroll", "--yes"]) == 0
    assert run(["enroll", "--yes"]) == 1


def test_an_absent_datastore_exits_two_and_never_reaches_the_aggregator(
    config: Config, monkeypatch: pytest.MonkeyPatch, offline_client: type[FakeClient]
) -> None:
    """`2` -- could not run. And refused before the network, so a typo costs nothing."""
    monkeypatch.setenv("BANKMACHINE_DATASTORE_PATH", str(config.datastore_path))
    monkeypatch.setenv("BANKMACHINE_LOG_DIR", str(config.log_dir))
    monkeypatch.setenv("BANKMACHINE_KEYCHAIN_SERVICE", config.keychain_service)
    monkeypatch.setenv("BANKMACHINE_CONFIG", str(config.datastore_path.parent / "absent.toml"))
    monkeypatch.setenv("BANKMACHINE_PLAID_CLIENT_ID", "test-client-id")

    assert run(["enroll", "--yes"]) == 2
    assert FakeClient.instances == []


# --------------------------------------------------------------------------
# The wait, and walking away from it
# --------------------------------------------------------------------------


def test_the_command_polls_until_the_operator_finishes(
    cli_env: Config, offline_client: type[FakeClient]
) -> None:
    FakeClient.polls_before_finished = 3

    assert run(["enroll", "--yes"]) == 0

    assert FakeClient.instances[0].polls == 4
    assert FakeClient.instances[0].exchanged == PUBLIC_TOKEN


def test_an_abandoned_session_exits_one_and_names_the_session(
    cli_env: Config, offline_client: type[FakeClient], capsys: pytest.CaptureFixture[str]
) -> None:
    """Not a fault: nothing is wrong, the operator walked away.

    `1` rather than `2`, and a sentence rather than a traceback -- the remedy is
    to run the command again, and the message says so.
    """
    FakeClient.polls_before_finished = 10_000

    assert run(["enroll", "--yes", "--timeout", "0"]) == 1

    err = capsys.readouterr().err
    assert "session-1" in err
    assert "Traceback" not in err
    assert "again" in err
    assert _rows(cli_env, connections) == []


def test_nothing_is_linked_when_the_window_is_not_confirmed(
    cli_env: Config, offline_client: type[FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 The confirmation is the last moment AC-1.2 is reversible.

    Declining must leave no connection and spend no token -- a confirmation that
    only printed a warning would be theatre.
    """
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    assert run(["enroll"]) == 1

    assert _rows(cli_env, connections) == []
    assert FakeClient.instances[0].exchanged is None


def test_the_window_is_printed_before_the_url(
    cli_env: Config, offline_client: type[FakeClient], capsys: pytest.CaptureFixture[str]
) -> None:
    """Order matters: an operator who has already opened the URL has stopped reading."""
    assert run(["enroll", "--yes"]) == 0

    out = capsys.readouterr().out
    assert out.index("history window") < out.index("https://secure.example")


def test_a_connection_whose_credential_vanished_says_so_by_name(cli_env: Config) -> None:
    """Its own error type, because its remedy is neither of the other two."""
    with pytest.raises(AccessTokenMissingError) as raised:
        get_access_token(cli_env, cli_env.connection_keychain_account("never-stored"))

    assert "re-enrol" in str(raised.value)


def test_sandbox_and_production_access_tokens_do_not_collide(cli_env: Config) -> None:
    """🔴 A sandbox re-enrollment must not overwrite a real connection's credential.

    The two environments can carry the same aggregator item id, and the failure
    would be silent until the next production sync.
    """
    from dataclasses import replace

    production = replace(cli_env, environment="production")
    assert cli_env.connection_keychain_account(ITEM_ID) != production.connection_keychain_account(
        ITEM_ID
    )
