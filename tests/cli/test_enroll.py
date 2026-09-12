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
from datetime import date
from typing import Any

import pytest
from sqlalchemy import insert, update

from bankmachine.cli import connections as connections_module
from bankmachine.cli import run
from bankmachine.cli.hosted_link import MIN_HOSTED_WAIT_SECONDS
from bankmachine.config import Config
from bankmachine.connector import (
    ITEM_GET,
    ITEM_REMOVE,
    AccessGrant,
    Endpoint,
    FetchedResponse,
    LinkSession,
    LinkToken,
    TransportError,
)
from bankmachine.connector.plaid.client import DEFAULT_HOSTED_URL_LIFETIME_SECONDS
from bankmachine.secrets import (
    AccessTokenMissingError,
    SecretsError,
    delete_access_token,
    delete_plaid_secret,
    get_access_token,
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
from conftest import use_cli_env

# Declared per line, as any file must. These carry the aggregator's real token
# SHAPES because the sweep below proves nothing against a value that could not
# have been mistaken for a credential in the first place.
ACCESS_TOKEN = "access-sandbox-fake-for-tests"  # credential-shape: test vector
PUBLIC_TOKEN = "public-sandbox-fake-for-tests"  # credential-shape: test vector
ITEM_ID = "item-fake-for-tests"
INSTITUTION_ID = "ins_109508"


def _item_body(item_id: str = ITEM_ID, institution_id: str = INSTITUTION_ID) -> bytes:
    """The item body enrollment derives its institution and capabilities from.

    Keys and shape follow the live one recorded in `api-notes-plaid.md` §13; the
    institution is the aggregator's own fictional sandbox bank, never one from
    `deployment/`. Parameterised by item id because a re-enrollment mints a NEW
    item, and a fixture that returned the same one could not show the difference.
    """
    return json.dumps(
        {
            "item": {
                "item_id": item_id,
                "institution_id": institution_id,
                "institution_name": f"Bank {institution_id}"
                if institution_id != INSTITUTION_ID
                else "First Platypus Bank",
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


ITEM_BODY = _item_body()


@pytest.fixture
def cli_env(initialized_config: Config, monkeypatch: pytest.MonkeyPatch) -> Iterator[Config]:
    config = initialized_config
    use_cli_env(monkeypatch, config, BANKMACHINE_PLAID_CLIENT_ID="test-client-id")
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
    item_id = ITEM_ID
    institution_id = INSTITUTION_ID
    instances: list[FakeClient] = []
    removed_tokens: list[str] = []

    def __init__(self, config: Config, secret: str, **kwargs: Any) -> None:
        self.config = config
        self.requested_history_days: int | None = None
        self.requested_products: list[str] | None = None
        self.requested_optional_products: list[str] | None = None
        self.hosted_lifetime: int | None = None
        self.polls = 0
        self.exchanged: str | None = None
        FakeClient.instances.append(self)

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def link_token_create(
        self,
        *,
        history_days: int,
        client_user_id: str,
        country_codes: list[str],
        products: list[str],
        optional_products: list[str] | None = None,
        **kwargs: Any,
    ) -> LinkToken:
        self.requested_history_days = history_days
        self.requested_products = products
        self.requested_optional_products = optional_products
        self.hosted_lifetime = kwargs.get("hosted_url_lifetime_seconds")
        return LinkToken(
            token="link-sandbox-fake",  # credential-shape: test vector
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
        return AccessGrant(
            access_token=f"{ACCESS_TOKEN}-{FakeClient.item_id}",
            source_connection_id=FakeClient.item_id,
        )

    def item_get(self, access_token: str, **kwargs: Any) -> FetchedResponse:
        return FetchedResponse(
            endpoint=ITEM_GET,
            body=_item_body(FakeClient.item_id, FakeClient.institution_id),
            received_at=now_utc(),
            request_context=None,
        )

    def item_remove(self, access_token: str, **kwargs: Any) -> FetchedResponse:
        FakeClient.removed_tokens.append(access_token)
        return FetchedResponse(
            endpoint=ITEM_REMOVE,
            body=b'{"request_id": "req-removed"}',
            received_at=now_utc(),
            request_context=None,
        )


@pytest.fixture(autouse=True)
def _reset_fake() -> None:
    FakeClient.instances = []
    FakeClient.polls_before_finished = 0
    FakeClient.item_id = ITEM_ID
    FakeClient.institution_id = INSTITUTION_ID
    FakeClient.removed_tokens = []


@pytest.fixture
def offline_client(monkeypatch: pytest.MonkeyPatch) -> type[FakeClient]:
    monkeypatch.setattr("bankmachine.cli.enroll.PlaidClient", FakeClient)
    # `release_at_aggregator` lives in the connections module and builds its own
    # client, so patching only the enroll module would leave the removal reaching
    # a real network -- and failing silently, which is exactly the bug below.
    monkeypatch.setattr("bankmachine.cli.connections.PlaidClient", FakeClient)
    monkeypatch.setattr("bankmachine.cli.hosted_link.time.sleep", lambda _seconds: None)
    return FakeClient


def _blind_the_preflight_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the FIRST `connection_rows` call see room, and later ones tell the truth.

    That is the race: the pre-flight check runs before a browser flow that takes
    a human minutes, and another enrollment can fill the last slot in between. A
    patch that blinded every call would also blind the in-transaction check --
    which is the one under test -- and the enrollment would simply succeed,
    proving nothing.
    """
    real = connections_module.connection_rows
    calls = {"n": 0}

    def counted(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        return [] if calls["n"] == 1 else real(*args, **kwargs)

    monkeypatch.setattr("bankmachine.cli.enroll.connection_rows", counted)


def _rows(config: Config, table: Any) -> list[Any]:
    with reader_connection(config) as conn:
        return list(conn.execute(table.select()).all())


def _seed_item_scoped_state(config: Config, *, connection_id: int) -> None:
    """Put a connection into the state a first sync leaves behind.

    Enrollment writes neither a cursor nor a granted window -- a sync does -- so a
    test about clearing them has to create them first, or it would assert against
    an absence that was never a presence.
    """
    now = now_utc()
    with writer_connection(config) as conn, transaction(conn):
        conn.execute(
            insert(sync_state).values(
                connection_id=connection_id,
                domain=TRANSACTIONS_DOMAIN,
                cursor="cursor-issued-by-the-old-item",
                history_start_date=calendar_date(date(2024, 1, 1)),
                updated_at=now,
            )
        )
        conn.execute(
            update(connections)
            .where(connections.c.connection_id == connection_id)
            .values(granted_history_days=180, updated_at=now)
        )


def _seed_account(
    config: Config,
    *,
    connection_id: int,
    source_account_id: str = "acct-1",
    lifecycle_status: str = "active",
    closed_date: date | None = None,
) -> int:
    """An account of the kind a sync would have derived for this connection.

    Written directly because `enroll` never produces one: the roster arrives with
    the first `sync run`, which these tests deliberately do not make.
    """
    now = now_utc()
    institution_id = next(
        int(row._mapping["institution_id"])
        for row in _rows(config, connections)
        if int(row._mapping["connection_id"]) == connection_id
    )
    with writer_connection(config) as conn, transaction(conn):
        result = conn.execute(
            insert(accounts).values(
                institution_id=institution_id,
                connection_id=connection_id,
                source_account_id=source_account_id,
                name="Everyday Checking",
                account_type="depository",
                balance_class="asset",
                currency="USD",
                lifecycle_status=lifecycle_status,
                first_seen_date=calendar_date(date(2024, 1, 1)),
                closed_date=None if closed_date is None else calendar_date(closed_date),
                source="aggregator",
                created_at=now,
                updated_at=now,
            )
        )
    primary_key = result.inserted_primary_key
    assert primary_key is not None
    return int(primary_key[0])


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
    """AC-3.2 stores the union of both product lists, so investments is discoverable.

    Which list each product arrives in, and why neither alone is the answer, is
    `capabilities_of`'s own contract -- exercised directly in
    `tests/connector/test_client.py`. This asserts only that enrollment persists
    what it was given.
    """
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


def test_only_transactions_is_required_and_investments_is_asked_for_optionally(
    cli_env: Config, offline_client: type[FakeClient]
) -> None:
    """Both products are still asked for; only one of them narrows the picker.

    This expectation CHANGED: it previously asserted `investments` in the
    REQUIRED set, per the operator's 2026-09-07 decision to have both. The
    required set is what Link filters the institution list by -- it offers only
    institutions supporting every member -- so most card issuers and credit
    unions vanished from the picker with no error. `optional_products` keeps the
    discovery that decision wanted and narrows nothing. See § Decisions,
    "`investments` moves from `products` to `optional_products`" in
    `.prawduct/artifacts/build-plan-production-cutover-hardening.md`.

    Asserted on both lists rather than on one, because a product that fell out of
    the request entirely and a product that moved into the required set are
    different failures and each is invisible in the sandbox: the aggregator's
    test bank supports everything, so it is filtered by nothing.
    """
    assert run(["enroll", "--yes"]) == 0

    assert FakeClient.instances[0].requested_products == ["transactions"]
    assert FakeClient.instances[0].requested_optional_products == ["investments"]


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
    assert get_access_token(cli_env, credential_ref) == f"{ACCESS_TOKEN}-{ITEM_ID}"


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


def _log_text(config: Config) -> str:
    """What the run actually wrote to disk.

    🔴 Read from the file rather than through `caplog`, because the assertions
    here are about ABSENCE: a capture that silently collected nothing would pass
    every one of them forever. The file is also the artefact that matters -- it
    is what an unattended run leaves behind and what a support paste contains.
    """
    return (config.log_dir / "bankmachine.log").read_text(encoding="utf-8")


def test_no_credential_reaches_the_enrollment_log(
    cli_env: Config, offline_client: type[FakeClient]
) -> None:
    """Asserted over a real enrollment's log file, not over the formatter alone.

    The formatter redacts by credential shape, and inheriting that is the plan's
    claim -- but a claim about inheritance is exactly the kind this project has
    been burned by, so the run itself is what gets checked.
    """
    assert run(["enroll", "--yes"]) == 0

    written = _log_text(cli_env)
    assert ACCESS_TOKEN not in written
    assert PUBLIC_TOKEN not in written
    # Positive control: the file has content and the run reached it, so the two
    # assertions above are about redaction rather than about an empty file.
    assert "enrolled" in written


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
    # a deliberate re-link to widen the window is that enrollment continuing.
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
    cli_env: Config,
    offline_client: type[FakeClient],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not a fault: nothing is wrong, the operator walked away.

    `1` rather than `2`, and a sentence rather than a traceback -- the remedy is
    to run the command again, and the message says so.
    """
    FakeClient.polls_before_finished = 10_000
    # The clock is driven rather than waited on. `--timeout` cannot be 0 any more
    # -- it is the hosted URL's lifetime, so a zero would be a URL nobody could
    # use -- and spinning until a real 30s elapsed would make this test take 30
    # seconds to assert a branch. Advancing the monotonic clock past the deadline
    # exercises exactly the comparison the loop makes.
    clock = iter([0.0, 0.0, 10_000.0])
    monkeypatch.setattr("bankmachine.cli.hosted_link.time.monotonic", lambda: next(clock))

    assert run(["enroll", "--yes", "--timeout", str(MIN_HOSTED_WAIT_SECONDS)]) == 1

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


# --------------------------------------------------------------------------
# The cap's two checks, and the one exit code they must agree on
# --------------------------------------------------------------------------


def test_both_cap_checks_report_the_same_exit_code(
    cli_env: Config, offline_client: type[FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 One condition, two checks, and they used to disagree.

    The pre-flight refusal returned 1 directly; the in-transaction refusal raised
    and fell through to the catch-all returning 2. A scheduler reading 2 cannot
    tell a full roster from a broken install, and which code it saw depended on
    which check happened to catch it.
    """
    monkeypatch.setenv("BANKMACHINE_CONNECTION_CAP", "1")
    assert run(["enroll", "--yes"]) == 0

    # The pre-flight path.
    assert run(["enroll", "--yes"]) == 1

    # The race path, reached by making the pre-flight check see room that the
    # transaction then does not. This is the check that closes the window between
    # two concurrent enrollments, and it must answer with the same code.
    _blind_the_preflight_only(monkeypatch)
    FakeClient.item_id = "item-second-institution"
    # A DIFFERENT institution, or the write takes the update branch and never
    # reaches the count -- which is what made the first version of this test pass
    # while proving nothing about the race path.
    FakeClient.institution_id = "ins_second"
    assert run(["enroll", "--yes"]) == 1


# --------------------------------------------------------------------------
# Past the spent token
# --------------------------------------------------------------------------


def test_a_failure_after_the_exchange_names_the_item_and_the_credential(
    cli_env: Config,
    offline_client: type[FakeClient],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """🔴 The state the operator must never discover from a bill.

    Past the exchange the aggregator holds an Item they are charged for. Any
    local failure there leaves them paying for a connection nothing here records,
    so the error carries the two handles a recovery needs -- the item id and the
    keychain account -- because neither is recoverable from a generic message.
    """

    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the disk went away")

    monkeypatch.setattr("bankmachine.cli.enroll.apply_response", explode)

    assert run(["enroll", "--yes"]) == 2

    err = capsys.readouterr().err
    assert f"item is {ITEM_ID}" in err
    assert cli_env.connection_keychain_account(ITEM_ID) in err
    assert "the disk went away" in err, "the cause must survive, not be swallowed"
    assert "enroll" in err, "the operator needs to be told what converges this"


def test_a_failure_after_the_exchange_releases_the_item_it_could_not_record(
    cli_env: Config,
    offline_client: type[FakeClient],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """🔴 The only recovery available on this path, so it is attempted.

    This is the one condition whose orphan cannot be given a row: the failure may
    be the archive write itself, so there may be no institution for a connection
    to hang from. Releasing is therefore the whole remedy — and when it works,
    nothing is billing and the credential is correctly gone.
    """

    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the disk went away")

    monkeypatch.setattr("bankmachine.cli.enroll.apply_response", explode)

    assert run(["enroll", "--yes"]) == 2

    assert FakeClient.removed_tokens == [f"{ACCESS_TOKEN}-{ITEM_ID}"]
    with pytest.raises(AccessTokenMissingError):
        get_access_token(cli_env, cli_env.connection_keychain_account(ITEM_ID))
    assert "nothing is billing" in capsys.readouterr().err


def test_an_unreleasable_item_says_the_dashboard_is_the_only_remedy(
    cli_env: Config,
    offline_client: type[FakeClient],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """🔴 The honest half: no command in this product can reach this Item.

    There is no row to retry from and `secrets.py` exposes no keychain
    enumeration, so implying a retry would be a lie. The credential survives
    because it is the only handle that exists, and the message sends the operator
    where the Item can actually be removed.
    """

    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the disk went away")

    def unreachable(*args: Any, **kwargs: Any) -> None:
        raise TransportError("the aggregator is unreachable", endpoint=ITEM_REMOVE)

    monkeypatch.setattr("bankmachine.cli.enroll.apply_response", explode)
    monkeypatch.setattr(FakeClient, "item_remove", unreachable)

    assert run(["enroll", "--yes"]) == 2

    err = capsys.readouterr().err
    assert "may still be billing" in err
    assert "dashboard" in err, "the operator must be told the one place it can be removed"
    assert f"item is {ITEM_ID}" in err
    # The credential survives: it is the only handle to that item that exists.
    assert get_access_token(cli_env, cli_env.connection_keychain_account(ITEM_ID)).startswith(
        ACCESS_TOKEN
    )


# --------------------------------------------------------------------------
# The orphan a re-enrollment leaves at the aggregator
# --------------------------------------------------------------------------


def test_re_enrolling_removes_the_item_it_superseded(
    cli_env: Config, offline_client: type[FakeClient]
) -> None:
    """🔴 Re-enrolling mints a NEW item; the old one does not stop existing.

    Overwriting `source_connection_id` drops the only local reference to it, and
    it keeps counting against the plan cap and keeps billing while this side
    shows one tidy connection and reports success. AC-1.4 says re-enrolling
    updates rather than duplicates -- and without this the duplication just moves
    to the far end, where nothing here can see it.
    """
    assert run(["enroll", "--yes"]) == 0
    first_token = f"{ACCESS_TOKEN}-{ITEM_ID}"

    FakeClient.item_id = "item-relinked"
    assert run(["enroll", "--yes", "--relink"]) == 0

    assert FakeClient.removed_tokens == [first_token]
    rows = _rows(cli_env, connections)
    assert len(rows) == 1
    assert rows[0]._mapping["source_connection_id"] == "item-relinked"


def test_removal_happens_only_after_the_replacement_is_committed(
    cli_env: Config, offline_client: type[FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 Two live items is a bill; none is a lost connection, so order decides which.

    Asserted by reading the datastore AT THE MOMENT of removal, from inside the
    removal itself. An earlier version checked only that the row survived and
    that something was removed -- neither of which constrains order, so it passed
    against a release that ran first. This is the test the plan cites for Chunk
    03's Done-when 6, and it was proving nothing about the property it named.
    """
    assert run(["enroll", "--yes"]) == 0

    observed: dict[str, object] = {}
    real_release = connections_module.release_at_aggregator

    def observing(config: Config, credential_ref: str, **kwargs: Any) -> bool:
        # What the committed datastore says while the old item is being released.
        observed["source_connection_id"] = _rows(config, connections)[0]._mapping[
            "source_connection_id"
        ]
        return real_release(config, credential_ref, **kwargs)

    monkeypatch.setattr("bankmachine.cli.enroll.release_at_aggregator", observing)
    FakeClient.item_id = "item-relinked"
    assert run(["enroll", "--yes", "--relink"]) == 0

    assert observed["source_connection_id"] == "item-relinked", (
        "the release ran before the replacement was committed, so a crash between "
        "them would leave no live item at all"
    )


def test_a_converging_re_run_against_the_same_item_removes_nothing(
    cli_env: Config, offline_client: type[FakeClient]
) -> None:
    """🔴 The failure this guard exists for.

    A re-run that lands on the SAME item -- which is what converging after a
    partial enrollment does -- must not remove the connection it just recorded.
    """
    assert run(["enroll", "--yes"]) == 0
    assert run(["enroll", "--yes"]) == 0

    assert FakeClient.removed_tokens == []
    assert len(_rows(cli_env, connections)) == 1


# --------------------------------------------------------------------------
# What a re-link invalidates: everything scoped to the item that went away
# --------------------------------------------------------------------------


def test_re_linking_to_a_new_item_clears_the_cursor_and_the_granted_window(
    cli_env: Config, offline_client: type[FakeClient]
) -> None:
    """🔴 A cursor belongs to the item that issued it, and the row outlives the item.

    Re-linking mints a NEW item behind the SAME connection row. Keeping the old
    cursor sends
    one item's bookmark with another item's credential, which the aggregator
    refuses permanently -- so every later sync degrades the connection and no
    command can clear it. The granted window goes for the same reason: it measures
    what the retired item granted, and the `gapped` warning built on it would then
    describe a connection that no longer exists.

    🔴 Driven through `--relink`, because this clearing is exactly what the flag
    exists to gate. It is correct and it is expensive, and it is no longer the
    remedy for an expired login -- `connections reauth` is, and it costs none of
    this.
    """
    assert run(["enroll", "--yes"]) == 0
    _seed_item_scoped_state(cli_env, connection_id=1)

    FakeClient.item_id = "item-relinked"
    assert run(["enroll", "--yes", "--relink"]) == 0

    assert _rows(cli_env, sync_state) == [], (
        "the new item's first sync must start from the beginning, not from a "
        "bookmark the new item never issued"
    )
    row = _rows(cli_env, connections)[0]._mapping
    assert row["source_connection_id"] == "item-relinked"
    assert row["granted_history_days"] is None, (
        "the recorded window belongs to the item that was replaced"
    )


def test_re_linking_to_a_new_item_says_so_in_the_log(
    cli_env: Config, offline_client: type[FakeClient]
) -> None:
    """The re-fetch is expensive and surprising, so it is announced rather than found.

    Without a line here the operator sees a sync that suddenly re-reads the whole
    window and has nothing to attribute it to.
    """
    assert run(["enroll", "--yes"]) == 0
    _seed_item_scoped_state(cli_env, connection_id=1)

    FakeClient.item_id = "item-relinked"
    assert run(["enroll", "--yes", "--relink"]) == 0

    written = _log_text(cli_env)
    assert "re-linked" in written
    assert "WARNING" in written


def test_a_re_run_against_the_same_item_keeps_the_cursor_and_the_window(
    cli_env: Config, offline_client: type[FakeClient]
) -> None:
    """🔴 The converging re-run must cost nothing.

    Re-running against the SAME item -- which is what converging after a partial
    enrollment does -- changes nothing item-scoped. Clearing the cursor here would
    make an idempotent command re-fetch the entire history every time it was run.
    """
    assert run(["enroll", "--yes"]) == 0
    _seed_item_scoped_state(cli_env, connection_id=1)

    assert run(["enroll", "--yes"]) == 0

    cursors = [row._mapping["cursor"] for row in _rows(cli_env, sync_state)]
    assert cursors == ["cursor-issued-by-the-old-item"]
    assert _rows(cli_env, connections)[0]._mapping["granted_history_days"] == 180


def test_a_re_link_leaves_the_accounts_and_transactions_alone(
    cli_env: Config, offline_client: type[FakeClient]
) -> None:
    """History survives a re-link; only the item-scoped bookkeeping is reset.

    The accounts the retired item reported are the same real-world accounts, and
    deleting them would discard the balances and rows they carry -- which is the
    one thing AC-6.5 does not allow a connection's lifecycle to do.
    """
    assert run(["enroll", "--yes"]) == 0
    account_id = _seed_account(cli_env, connection_id=1)

    FakeClient.item_id = "item-relinked"
    assert run(["enroll", "--yes", "--relink"]) == 0

    rows = _rows(cli_env, accounts)
    assert [row._mapping["account_id"] for row in rows] == [account_id]
    assert rows[0]._mapping["lifecycle_status"] == "active"


# --------------------------------------------------------------------------
# AC-1.5 / AC-1.6 — `connections list` and `connections retire`
# --------------------------------------------------------------------------


def test_the_command_the_cap_refusal_names_actually_exists(
    cli_env: Config, offline_client: type[FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 A refusal pointing at a command that does not exist is a dead end.

    AC-1.5 requires the operator to be able to act on the limit. Shipping the
    refusal without the remedy leaves them with argparse's "invalid choice" and
    exit 2 -- which reads as the product being broken rather than full.
    """
    monkeypatch.setenv("BANKMACHINE_CONNECTION_CAP", "1")
    assert run(["enroll", "--yes"]) == 0
    assert run(["enroll", "--yes"]) == 1

    assert run(["connections", "list"]) == 0
    assert run(["connections", "retire", "1"]) == 0
    # And the slot it freed is usable, which is the whole point of the advice.
    FakeClient.item_id, FakeClient.institution_id = "item-two", "ins_second"
    assert run(["enroll", "--yes"]) == 0


def test_retiring_keeps_every_row_the_connection_produced(
    cli_env: Config, offline_client: type[FakeClient]
) -> None:
    """AC-1.6 and AC-6.5: retired, not deleted."""
    assert run(["enroll", "--yes"]) == 0
    institutions_before = len(_rows(cli_env, institutions))
    archive_before = len(_rows(cli_env, raw_responses))

    assert run(["connections", "retire", "1"]) == 0

    rows = _rows(cli_env, connections)
    assert len(rows) == 1, "retiring must not delete the connection row"
    assert rows[0]._mapping["retired_at"] is not None
    assert rows[0]._mapping["status"] == "retired"
    assert len(_rows(cli_env, institutions)) == institutions_before
    assert len(_rows(cli_env, raw_responses)) == archive_before


def test_retiring_closes_the_accounts_it_stops_reporting(
    cli_env: Config, offline_client: type[FakeClient]
) -> None:
    """🔴 A retired connection's balances stop being current, and must stop reading so.

    Nothing observes this connection's roster again, so every absence-based signal
    stays silent: the accounts' last observation still matches the connection's,
    and the read path goes on calling them active and summing their last captured
    balance as a present-day one. A retirement is the operator declaring the
    connection over, and the stored declaration is what the read path honours.
    """
    assert run(["enroll", "--yes"]) == 0
    _seed_account(cli_env, connection_id=1)
    retired_on = now_utc().date()

    assert run(["connections", "retire", "1"]) == 0

    row = _rows(cli_env, accounts)[0]._mapping
    assert row["lifecycle_status"] == "inactive"
    assert row["closed_date"] == retired_on


def test_retiring_writes_the_accounts_in_the_retirement_transaction(
    cli_env: Config, offline_client: type[FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 One transaction, so no crash can leave a retired connection with live accounts.

    The removal at the aggregator runs after the local commit, and it is the first
    thing that can fail. Reading the accounts from inside it is what shows they
    were already committed rather than pending a second write nobody would retry.
    """
    assert run(["enroll", "--yes"]) == 0
    _seed_account(cli_env, connection_id=1)

    observed: dict[str, object] = {}
    real_release = connections_module.release_at_aggregator

    def observing(config: Config, credential_ref: str, **kwargs: Any) -> bool:
        observed["lifecycle_status"] = _rows(config, accounts)[0]._mapping["lifecycle_status"]
        return real_release(config, credential_ref, **kwargs)

    monkeypatch.setattr("bankmachine.cli.connections.release_at_aggregator", observing)

    assert run(["connections", "retire", "1"]) == 0

    assert observed["lifecycle_status"] == "inactive"


def test_retiring_leaves_an_account_that_was_already_closed_alone(
    cli_env: Config, offline_client: type[FakeClient]
) -> None:
    """An account closed earlier was closed on its own date, and that date is a fact.

    Overwriting it with the retirement date would move a real-world event to the
    day somebody happened to tidy up the connection.
    """
    assert run(["enroll", "--yes"]) == 0
    _seed_account(
        cli_env,
        connection_id=1,
        lifecycle_status="inactive",
        closed_date=date(2025, 3, 4),
    )

    assert run(["connections", "retire", "1"]) == 0

    row = _rows(cli_env, accounts)[0]._mapping
    assert row["lifecycle_status"] == "inactive"
    assert row["closed_date"] == date(2025, 3, 4)


def test_retiring_touches_only_its_own_connections_accounts(
    cli_env: Config, offline_client: type[FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other institution is still being synced, and its accounts are still current."""
    monkeypatch.setenv("BANKMACHINE_CONNECTION_CAP", "2")
    assert run(["enroll", "--yes"]) == 0
    _seed_account(cli_env, connection_id=1)
    FakeClient.item_id, FakeClient.institution_id = "item-two", "ins_second"
    assert run(["enroll", "--yes"]) == 0
    other = _seed_account(cli_env, connection_id=2, source_account_id="acct-2")

    assert run(["connections", "retire", "1"]) == 0

    lifecycles = {
        int(row._mapping["account_id"]): row._mapping["lifecycle_status"]
        for row in _rows(cli_env, accounts)
    }
    assert lifecycles[other] == "active"


def test_retiring_removes_the_item_at_the_aggregator(
    cli_env: Config, offline_client: type[FakeClient]
) -> None:
    """🔴 Local retirement alone leaves a connection that still bills.

    `retired_at` frees a slot in this product's cap and does nothing at the far
    end, where the Item keeps counting against the plan.
    """
    assert run(["enroll", "--yes"]) == 0

    assert run(["connections", "retire", "1"]) == 0

    assert FakeClient.removed_tokens == [f"{ACCESS_TOKEN}-{ITEM_ID}"]
    with pytest.raises(AccessTokenMissingError):
        get_access_token(cli_env, cli_env.connection_keychain_account(ITEM_ID))


def test_a_retired_connection_does_not_count_against_the_cap(
    cli_env: Config, offline_client: type[FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise retiring would free nothing and AC-1.6 would be decorative."""
    monkeypatch.setenv("BANKMACHINE_CONNECTION_CAP", "1")
    assert run(["enroll", "--yes"]) == 0
    assert run(["connections", "retire", "1"]) == 0

    FakeClient.item_id, FakeClient.institution_id = "item-two", "ins_second"
    assert run(["enroll", "--yes"]) == 0
    assert len([r for r in _rows(cli_env, connections) if r._mapping["retired_at"] is None]) == 1


def test_retiring_an_already_retired_connection_is_not_an_error(
    cli_env: Config, offline_client: type[FakeClient]
) -> None:
    """The operator asked for a state the system is already in.

    Reporting failure would invite them to try something more drastic against a
    connection that is already exactly as they want it.
    """
    assert run(["enroll", "--yes"]) == 0
    assert run(["connections", "retire", "1"]) == 0

    assert run(["connections", "retire", "1"]) == 0


def test_retiring_an_unknown_connection_exits_one_and_says_how_to_look(
    cli_env: Config, offline_client: type[FakeClient], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["connections", "retire", "999"]) == 1
    # stderr: failures go there, matching the sibling messages in `enroll`.
    assert "connections list" in capsys.readouterr().err


def test_listing_hides_retired_connections_unless_asked(
    cli_env: Config, offline_client: type[FakeClient], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["enroll", "--yes"]) == 0
    assert run(["connections", "retire", "1"]) == 0
    capsys.readouterr()

    assert run(["connections", "list"]) == 0
    assert "First Platypus Bank" not in capsys.readouterr().out

    assert run(["connections", "list", "--all"]) == 0
    listed = capsys.readouterr().out
    assert "First Platypus Bank" in listed
    assert "retired" in listed


def test_an_unreachable_aggregator_leaves_the_retirement_and_says_so(
    cli_env: Config,
    offline_client: type[FakeClient],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """🔴 The local write happens first, and its failure direction is the safe one.

    A removed Item with a live local row is a connection that stops syncing and
    says why. A retired local row with a live Item is a charge nobody can see --
    so the retirement stands and the operator is told to retry, rather than the
    whole thing rolling back.
    """
    assert run(["enroll", "--yes"]) == 0

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise TransportError("the aggregator is unreachable", endpoint=ITEM_REMOVE)

    monkeypatch.setattr(FakeClient, "item_remove", refuse)

    assert run(["connections", "retire", "1"]) == 1

    assert _rows(cli_env, connections)[0]._mapping["retired_at"] is not None
    err = capsys.readouterr().err
    assert "may still be billing" in err
    # 🔴 It names the item. The log cannot -- a production id is an opaque 37-char
    # run the formatter blanks -- and `connections list` cannot either, because
    # the connection is retired. This line is the only place it is ever visible.
    assert ITEM_ID in err
    # The credential survives, because it is the only handle left to that item.
    assert get_access_token(cli_env, cli_env.connection_keychain_account(ITEM_ID))


def test_a_piped_run_approves_the_window_without_a_prompt_but_still_prints_it(
    cli_env: Config,
    offline_client: type[FakeClient],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The irreversible window auto-approves off a tty, and that branch needs a test.

    Blocking on a question nobody can answer would hang an unattended enrollment
    forever, so a piped run proceeds. What it must NOT do is proceed quietly --
    the window still prints, so the transcript of an unattended run records what
    was asked for. Asserted without `--yes`, because `--yes` would prove nothing
    about the tty branch.
    """
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    assert run(["enroll"]) == 0

    out = capsys.readouterr().out
    assert f"{cli_env.history_days} days will be requested" in out
    assert "cannot be raised later" in out


def test_the_cap_refusal_reaches_the_log_not_only_stderr(
    cli_env: Config, offline_client: type[FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """stderr reaches whoever is watching; the log is what an unattended run leaves.

    An enrollment that refused and logged nothing is indistinguishable, afterwards,
    from one nobody ran.
    """
    monkeypatch.setenv("BANKMACHINE_CONNECTION_CAP", "1")
    assert run(["enroll", "--yes"]) == 0
    assert run(["enroll", "--yes"]) == 1

    assert "cap of 1" in _log_text(cli_env)


# --------------------------------------------------------------------------
# The post-exchange states, at every site that can reach them
# --------------------------------------------------------------------------


def test_a_keychain_failure_after_the_exchange_still_names_the_item(
    cli_env: Config,
    offline_client: type[FakeClient],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """🔴 The worst of the post-exchange states, and it sat outside the guard.

    The Item exists and is billable, and if the credential never reached the
    keychain this product holds no handle to it -- so not even
    `connections retire` could ever remove it. Naming the item in the error is
    the only recovery left, which is why storing the token belongs inside the
    guard rather than one line before it.
    """

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise SecretsError("the keychain is locked")

    monkeypatch.setattr("bankmachine.cli.enroll.set_access_token", refuse)

    assert run(["enroll", "--yes"]) == 2

    err = capsys.readouterr().err
    # 🔴 The phrase, not the bare id. `credential_ref` embeds the item id, so
    # `ITEM_ID in err` passes even when the item field is empty -- it cannot tell
    # "names the item" from "names the credential", which are the two different
    # handles a recovery needs.
    assert f"item is {ITEM_ID}" in err, "an item exists at the aggregator; say which"
    assert cli_env.connection_keychain_account(ITEM_ID) in err
    assert "the keychain is locked" in err


def test_the_cap_race_releases_the_item_it_just_minted(
    cli_env: Config, offline_client: type[FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 Refused past the exchange, so an Item exists that will never be used.

    The pre-flight check saw room and the transaction did not. Leaving the Item
    would bill the operator for a connection they were simultaneously told they
    could not have -- so it is released. The refusal still stands and still
    exits 1; releasing does not turn it into a success.
    """
    monkeypatch.setenv("BANKMACHINE_CONNECTION_CAP", "1")
    assert run(["enroll", "--yes"]) == 0
    FakeClient.removed_tokens = []

    _blind_the_preflight_only(monkeypatch)
    FakeClient.item_id, FakeClient.institution_id = "item-raced", "ins_second"

    assert run(["enroll", "--yes"]) == 1

    assert FakeClient.removed_tokens == [f"{ACCESS_TOKEN}-item-raced"], (
        "the item minted by the refused enrollment is still billing"
    )
    assert len(_rows(cli_env, connections)) == 1


def test_a_refusal_type_that_forgets_its_exit_code_says_could_not_run(
    cli_env: Config, offline_client: type[FakeClient]
) -> None:
    """The exit code is a property of the exception, not a tuple in `run`.

    A tuple would have to be extended by whoever adds the next refusal type, and
    would answer 2 when they forget. Carrying the default on the base class means
    forgetting produces the SAFE answer -- "could not run" -- rather than a
    silent claim that the command ran and found a problem.
    """
    from bankmachine.cli.enroll import (
        ConnectionCapReachedError,
        EnrollmentAbandonedError,
        EnrollmentError,
    )

    assert EnrollmentError.exit_code == 2
    assert ConnectionCapReachedError.exit_code == 1
    assert EnrollmentAbandonedError.exit_code == 1


def test_a_failed_release_of_the_superseded_item_is_reported_not_swallowed(
    cli_env: Config,
    offline_client: type[FakeClient],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The enrollment succeeded; the item it replaced is still billing.

    Reported rather than raised -- the operator has a working connection and
    losing it would be worse. But a success message that did not mention the
    orphan would be the silent outcome this project disallows, so the run exits
    1 and says which command shows what is enrolled.
    """
    assert run(["enroll", "--yes"]) == 0

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise TransportError("the aggregator is unreachable", endpoint=ITEM_REMOVE)

    monkeypatch.setattr(FakeClient, "item_remove", refuse)
    FakeClient.item_id = "item-relinked"

    assert run(["enroll", "--yes", "--relink"]) == 1

    # The enrollment still stands -- the new connection is usable.
    assert _rows(cli_env, connections)[0]._mapping["source_connection_id"] == "item-relinked"
    assert "may still be billing" in capsys.readouterr().err


def test_every_endpoint_declares_both_of_its_risk_properties_deliberately() -> None:
    """🔴 Every endpoint must appear here BY NAME, so a new one fails this test.

    An earlier version asserted only that each endpoint's flags matched a set of
    exceptions — which meant an endpoint whose defaults happened to be right
    passed silently, with nobody recording why either property was safe. That is
    the enumeration-for-a-property shape again, one level up: it forced a decision
    only for the unusual cases, which are not the ones that get forgotten.

    Comparing the NAME SET is what makes adding an endpoint fail here. Both
    properties are independent: `retry_safe` asks whether the far end can absorb
    the call twice, `issues_credential` whether its body may reach the append-only
    archive.
    """
    import bankmachine.connector as boundary

    #: name -> (retry_safe, issues_credential, why it is what it is)
    declared: dict[str, tuple[bool, bool, str]] = {
        "INSTITUTIONS_GET": (True, False, "a catalogue read; nothing is spent or returned"),
        "LINK_TOKEN_CREATE": (True, True, "opens a session; the body carries a link token"),
        "LINK_TOKEN_GET": (True, True, "a poll; a finished session's body carries a public token"),
        "ITEM_PUBLIC_TOKEN_EXCHANGE": (
            False,
            True,
            "spends a single-use token and mints a durable Item -- a retry mints two",
        ),
        "ITEM_GET": (True, False, "the item's own record; the token goes up, nothing comes down"),
        "ITEM_REMOVE": (
            False,
            False,
            "a retry cannot double-remove, but it can turn a success into a spurious "
            "ITEM_NOT_FOUND on a path where the operator is being told something worked",
        ),
        "TRANSACTIONS_SYNC": (
            True,
            False,
            "a cursor makes it idempotent at the far end -- the same cursor returns the "
            "same page, which is what lets a killed process resume",
        ),
        "ACCOUNTS_GET": (True, False, "a read of the connection's accounts"),
        "INVESTMENTS_HOLDINGS_GET": (
            True,
            False,
            "an unpaginated read of current positions -- no cursor is advanced and nothing "
            "at the far end is spent, and the body carries holdings, securities and the "
            "account roster with no token among them",
        ),
    }
    found = {name: value for name, value in vars(boundary).items() if isinstance(value, Endpoint)}

    assert set(found) == set(declared), (
        "an endpoint is missing from this test's record, or the record names one that no "
        "longer exists. Both properties are risk decisions and neither has a safe default: "
        f"undeclared={sorted(set(found) - set(declared))} "
        f"stale={sorted(set(declared) - set(found))}"
    )
    for name, endpoint in found.items():
        retry_safe, issues_credential, why = declared[name]
        assert endpoint.retry_safe is retry_safe, f"{name}.retry_safe disagrees with: {why}"
        assert endpoint.issues_credential is issues_credential, (
            f"{name}.issues_credential disagrees with: {why}"
        )


def test_the_url_lifetime_is_the_wait_not_a_second_number(
    cli_env: Config, offline_client: type[FakeClient]
) -> None:
    """🔴 Two numbers that happen to agree are one drift away from an orphan.

    If the hosted URL outlives the wait, this side stops polling while the URL is
    still usable. The operator completes Link, the aggregator mints an Item, and
    its public token is never exchanged -- no row, no log, no access token, so
    nothing in this product can ever remove it. Deriving the lifetime from the
    wait means the URL dies when we stop listening.
    """
    assert run(["enroll", "--yes", "--timeout", "120"]) == 0

    request = FakeClient.instances[0].hosted_lifetime
    assert request == 120, "the URL can outlive the wait, which orphans an Item"


def test_the_wait_message_names_its_own_deadline_and_how_to_raise_it(
    cli_env: Config, offline_client: type[FakeClient], capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 The one moment the operator can still act on the deadline is while waiting.

    A production first link can mean an OAuth redirect to the bank's own site, a
    password reset, an SMS code and a device registration. When the default runs
    out mid-login the URL dies with it and the operator is told the session was
    abandoned -- after the fact, with no hint that a longer wait was available.
    """
    assert run(["enroll", "--yes"]) == 0

    printed = capsys.readouterr().out
    assert f"{DEFAULT_HOSTED_URL_LIFETIME_SECONDS}" in printed
    assert "--timeout" in printed


def test_the_help_names_the_default_wait_and_the_flag_that_changes_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--help` is where an operator plans the run they are about to make.

    Asserted on the rendered help rather than on the argument's declaration,
    because a default carried in code and a default described in prose are two
    accounts of one number, and only the rendered one reaches the operator.
    """
    with pytest.raises(SystemExit) as raised:
        run(["enroll", "--help"])

    assert raised.value.code == 0
    printed = capsys.readouterr().out
    assert "--timeout" in printed
    assert f"{DEFAULT_HOSTED_URL_LIFETIME_SECONDS}" in printed


def test_an_unreadable_credential_does_not_collapse_a_cap_refusal_to_two(
    cli_env: Config, offline_client: type[FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 The consequence is two modules away from the cause.

    `release_at_aggregator` promises never to raise. When it caught only
    `AccessTokenMissingError`, a plain `SecretsError` from a locked keychain
    escaped -- and on the cap-race path it escaped from inside the `except
    ConnectionCapReachedError` arm, pre-empting the pending refusal before its
    `raise`. The cap then exited 2 instead of 1: a full roster reported as a
    broken install, which is the collapse the api-contract norm calls
    non-collapsible.
    """
    monkeypatch.setenv("BANKMACHINE_CONNECTION_CAP", "1")
    assert run(["enroll", "--yes"]) == 0

    def locked(*args: Any, **kwargs: Any) -> None:
        raise SecretsError("the keychain is locked")

    monkeypatch.setattr("bankmachine.cli.connections.get_access_token", locked)
    _blind_the_preflight_only(monkeypatch)
    FakeClient.item_id, FakeClient.institution_id = "item-raced", "ins_second"

    assert run(["enroll", "--yes"]) == 1, (
        "an unreadable credential turned a full roster into 'could not run'"
    )


def test_an_unreadable_credential_leaves_retirement_standing_and_says_so(
    cli_env: Config,
    offline_client: type[FakeClient],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The same arm on the retirement path: reported, never raised.

    The local retirement has already committed by then, so raising would leave
    the operator's decision half-applied with no message explaining it.
    """
    assert run(["enroll", "--yes"]) == 0

    def locked(*args: Any, **kwargs: Any) -> None:
        raise SecretsError("the keychain is locked")

    monkeypatch.setattr("bankmachine.cli.connections.get_access_token", locked)

    assert run(["connections", "retire", "1"]) == 1

    assert _rows(cli_env, connections)[0]._mapping["retired_at"] is not None
    assert "may still be billing" in capsys.readouterr().err


def test_a_production_length_item_id_is_redacted_from_the_log(
    cli_env: Config, offline_client: type[FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 The trap that makes a fixture-length id prove the wrong thing.

    The formatter blanks any opaque run of 32+ characters. A real aggregator item
    id is around 37 and is therefore redacted; this file's fixture id is 19 and
    is not. So a log assertion written against the fixture would pass over a
    production line reading `[REDACTED]` -- which is why the failure path logs
    the institution and points at the command's error output instead of pretending
    to log the id. Asserted here so nobody re-adds it believing it works.
    """
    long_id = "aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"  # credential-shape: test vector
    assert len(long_id) >= 32
    FakeClient.item_id = long_id

    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the disk went away")

    monkeypatch.setattr("bankmachine.cli.enroll.apply_response", explode)
    assert run(["enroll", "--yes"]) == 2

    written = _log_text(cli_env)
    assert long_id not in written, "an id this shape must not survive the formatter"
    # The positive control, and the reason this test exists: the SAME id, logged
    # deliberately, comes out redacted. Without this the assertion above passes
    # because nothing logs the id at all -- which is true today and says nothing
    # about what happens when someone adds it back.
    logger = logging.getLogger("bankmachine.cli.enroll")
    logger.error("deliberate probe of the formatter: %s", long_id)
    probed = _log_text(cli_env)
    assert "deliberate probe of the formatter" in probed, "the probe never reached the file"
    assert long_id not in probed, (
        "a production-length item id survived the formatter, so a log line carrying "
        "one would leak it -- and a fixture-length id would not have shown that"
    )
    assert "[REDACTED]" in probed


def test_a_timeout_below_the_floor_is_refused_before_the_aggregator(
    cli_env: Config, offline_client: type[FakeClient]
) -> None:
    """It is the URL's lifetime now, so a tiny value is not a short wait.

    Before the two numbers were unified a zero only shortened a local loop. It
    reaches the vendor as `url_lifetime_seconds` now, where it would come back an
    aggregator rejection -- exit 2, "could not run" -- for what is really a
    mistyped argument.
    """
    with pytest.raises(SystemExit) as raised:
        run(["enroll", "--yes", "--timeout", "0"])

    assert raised.value.code == 2  # argparse's own usage exit
    assert FakeClient.instances == [], "a bad argument must not reach the aggregator"


def test_retrying_a_retirement_whose_removal_never_confirmed_actually_retries(
    cli_env: Config,
    offline_client: type[FakeClient],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """🔴 The retry the message promises, which used to be unreachable.

    A re-run hit `if row.retired_at is not None:` first, printed "was already
    retired" and exited 0 -- so the Item kept billing while the operator was told
    it had worked. The surviving credential is what distinguishes "retired and
    confirmed" from "retired and never confirmed": `release_at_aggregator` deletes
    it only after the far end agrees.
    """
    assert run(["enroll", "--yes"]) == 0

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise TransportError("the aggregator is unreachable", endpoint=ITEM_REMOVE)

    # Saved and restored explicitly rather than with `monkeypatch.undo()`, which
    # would also revert this fixture's env vars -- and the run would then resolve
    # the OPERATOR'S real datastore path instead of the temporary one.
    original = FakeClient.item_remove
    monkeypatch.setattr(FakeClient, "item_remove", refuse)
    assert run(["connections", "retire", "1"]) == 1
    capsys.readouterr()

    # The network comes back.
    monkeypatch.setattr(FakeClient, "item_remove", original)
    FakeClient.removed_tokens = []

    assert run(["connections", "retire", "1"]) == 0

    assert FakeClient.removed_tokens == [f"{ACCESS_TOKEN}-{ITEM_ID}"], (
        "the retry never happened, so the item is still billing"
    )
    assert "no longer billing" in capsys.readouterr().out


def test_a_confirmed_retirement_reports_already_retired_and_retries_nothing(
    cli_env: Config, offline_client: type[FakeClient], capsys: pytest.CaptureFixture[str]
) -> None:
    """The other half: once the credential is gone, there is nothing left to do.

    Without this, "retry when the credential survives" would be satisfied by code
    that retried unconditionally -- calling the aggregator on every re-run of a
    connection that was cleanly retired months ago.
    """
    assert run(["enroll", "--yes"]) == 0
    assert run(["connections", "retire", "1"]) == 0
    FakeClient.removed_tokens = []
    capsys.readouterr()

    assert run(["connections", "retire", "1"]) == 0

    assert FakeClient.removed_tokens == []
    assert "was already retired" in capsys.readouterr().out


def test_an_item_orphaned_by_the_cap_race_becomes_a_retirable_connection(
    cli_env: Config, offline_client: type[FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 A pending release IS a retired connection, which is why this needs no new state.

    The cap race mints an Item and then refuses to record it. If the release also
    fails, the operator was previously told money may still be leaving and no
    command they could run would ever retry it — the credential sat in a keychain
    nothing can enumerate. Writing the retired row gives the obligation a home
    `connections retire` can find.
    """
    monkeypatch.setenv("BANKMACHINE_CONNECTION_CAP", "1")
    assert run(["enroll", "--yes"]) == 0

    def unreachable(*args: Any, **kwargs: Any) -> None:
        raise TransportError("the aggregator is unreachable", endpoint=ITEM_REMOVE)

    monkeypatch.setattr(FakeClient, "item_remove", unreachable)
    _blind_the_preflight_only(monkeypatch)
    FakeClient.item_id, FakeClient.institution_id = "item-raced", "ins_second"

    assert run(["enroll", "--yes"]) == 1

    rows = {r._mapping["source_connection_id"]: r._mapping for r in _rows(cli_env, connections)}
    orphan = rows.get("item-raced")
    assert orphan is not None, "the orphaned item has no row, so nothing can ever retry it"
    assert orphan["retired_at"] is not None
    assert orphan["status"] == "retired"
    # And the command that retries it can now find it.
    assert run(["connections", "list", "--all"]) == 0


# --------------------------------------------------------------------------
# The re-link guard (#67) -- `enroll` stops repointing a live connection by accident
# --------------------------------------------------------------------------


def test_an_unflagged_enrollment_that_would_replace_the_item_is_refused(
    cli_env: Config, offline_client: type[FakeClient], capsys: pytest.CaptureFixture[str]
) -> None:
    """🔴 The refusal sits on the branch that would commit the duplication.

    A new item re-issues every `source_account_id`, and the identity that would
    survive one is null at all but three institutions -- so the roster inserts a
    second row per account, the re-fetched window lands on those rows, and every
    total doubles with nothing saying so. Measured: 390 transactions became 784.
    """
    assert run(["enroll", "--yes"]) == 0
    _seed_item_scoped_state(cli_env, connection_id=1)

    FakeClient.item_id = "item-relinked"
    assert run(["enroll", "--yes"]) == 1

    rows = _rows(cli_env, connections)
    assert len(rows) == 1
    assert rows[0]._mapping["source_connection_id"] == ITEM_ID, "the row kept its original item"
    assert _rows(cli_env, sync_state) != [], "the cursor the refusal protects is still there"
    err = capsys.readouterr().err
    assert "connections reauth 1" in err
    assert "--relink" in err


def test_the_item_a_refused_re_link_minted_is_released_not_left_billing(
    cli_env: Config, offline_client: type[FakeClient]
) -> None:
    """The refusal still costs the operator a completed browser session.

    It must not also cost them a bill: an item exists at the aggregator that this
    side has just declined to record, and the only handle to it is the credential
    this run wrote.
    """
    assert run(["enroll", "--yes"]) == 0

    FakeClient.item_id = "item-relinked"
    assert run(["enroll", "--yes"]) == 1

    assert FakeClient.removed_tokens == [f"{ACCESS_TOKEN}-item-relinked"], (
        "the refused item was released; the original connection's token was not touched"
    )


def test_a_re_link_that_cannot_be_released_becomes_an_orphan_row(
    cli_env: Config, offline_client: type[FakeClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal path inherits the cap race's recovery, because it leaves the same wreckage."""
    assert run(["enroll", "--yes"]) == 0

    def unreachable(self: FakeClient, access_token: str, **kwargs: Any) -> FetchedResponse:
        raise TransportError("the aggregator is unreachable", endpoint=ITEM_REMOVE)

    FakeClient.item_id = "item-relinked"
    monkeypatch.setattr(FakeClient, "item_remove", unreachable)
    assert run(["enroll", "--yes"]) == 1

    retired = [
        row._mapping
        for row in _rows(cli_env, connections)
        if row._mapping["source_connection_id"] == "item-relinked"
    ]
    assert retired, "an item nothing can reach needs a row to be retried from"
    assert retired[0]["retired_at"] is not None


def test_a_converging_re_run_against_the_same_item_needs_no_flag(
    cli_env: Config, offline_client: type[FakeClient]
) -> None:
    """🔴 The case the guard must NOT catch, and the reason it is keyed on the item.

    Re-running `enroll` against the same institution when nothing has changed
    replaces nothing, so it re-issues nothing and costs nothing. A guard keyed on
    "this institution is already linked" would refuse this, which is both wrong
    and the shape that teaches an operator to pass `--relink` reflexively.
    """
    assert run(["enroll", "--yes"]) == 0
    _seed_item_scoped_state(cli_env, connection_id=1)

    assert run(["enroll", "--yes"]) == 0

    assert _rows(cli_env, sync_state) != [], "a converging re-run must not clear the cursor"
    assert _rows(cli_env, connections)[0]._mapping["granted_history_days"] == 180


def test_the_signpost_names_the_cheaper_command_before_the_url(
    cli_env: Config, offline_client: type[FakeClient], capsys: pytest.CaptureFixture[str]
) -> None:
    """Where it costs nobody a session, unlike the refusal it usually makes unnecessary."""
    assert run(["enroll", "--yes"]) == 0
    capsys.readouterr()

    FakeClient.item_id = "item-relinked"
    run(["enroll", "--yes"])

    out = capsys.readouterr().out
    assert "already linked:" in out
    assert "connections reauth" in out
    assert out.index("already linked:") < out.index("open this URL"), (
        "a warning after the URL reaches the operator only once the session is spent"
    )


def test_the_signpost_is_absent_when_nothing_is_enrolled(
    cli_env: Config, offline_client: type[FakeClient], capsys: pytest.CaptureFixture[str]
) -> None:
    """An unconditional notice is one an operator learns to skip -- including when it mattered."""
    assert run(["enroll", "--yes"]) == 0

    out = capsys.readouterr().out
    assert "already linked:" not in out
    assert "connections reauth" not in out
