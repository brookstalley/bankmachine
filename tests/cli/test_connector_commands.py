"""`bankmachine connector check` end to end, over a real encrypted datastore.

The aggregator itself is the one thing not real here: the live round-trip is
`tests/connector/test_sandbox.py`, which is marked `sandbox` and deselected by
default. What these tests own is everything around the call -- the order the
preconditions are checked in, and that the bytes reach the archive unaltered.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from bankmachine.cli import connector as connector_cli
from bankmachine.cli import run
from bankmachine.config import Config
from bankmachine.connector import INSTITUTIONS_GET, FetchedResponse
from bankmachine.secrets import delete_plaid_secret, get_plaid_secret, set_plaid_secret
from bankmachine.store.engine import reader_connection
from bankmachine.store.raw import load_response
from bankmachine.store.schema import raw_responses
from bankmachine.store.types import now_utc

BODY = b'{"institutions": [{"institution_id": "ins_1"}], "total": 42, "request_id": "abc"}'


@pytest.fixture
def cli_env(config: Config, monkeypatch: pytest.MonkeyPatch) -> Iterator[Config]:
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


class RecordingClient:
    """A stand-in for `PlaidClient` that answers without a network."""

    instances: list[RecordingClient] = []

    def __init__(self, config: Config, secret: str, **kwargs: Any) -> None:
        self.config = config
        self.secret = secret
        self.calls: list[dict[str, Any]] = []
        self.last_context: str | None = None
        RecordingClient.instances.append(self)

    def __enter__(self) -> RecordingClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def institutions_get(
        self, *, count: int, offset: int, country_codes: list[str]
    ) -> FetchedResponse:
        self.calls.append({"count": count, "offset": offset, "country_codes": country_codes})
        self.last_context = f"count={count} offset={offset} country_codes={','.join(country_codes)}"
        return FetchedResponse(
            endpoint=INSTITUTIONS_GET,
            body=BODY,
            received_at=now_utc(),
            request_context=self.last_context,
        )


@pytest.fixture(autouse=True)
def _reset_recording() -> None:
    RecordingClient.instances = []


@pytest.fixture
def offline_client(monkeypatch: pytest.MonkeyPatch) -> type[RecordingClient]:
    monkeypatch.setattr("bankmachine.cli.connector.PlaidClient", RecordingClient)
    return RecordingClient


def test_check_archives_the_response_verbatim(
    cli_env: Config,
    offline_client: type[RecordingClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(["store", "init"]) == 0
    capsys.readouterr()

    assert run(["connector", "check"]) == 0
    out = capsys.readouterr().out

    with reader_connection(cli_env) as conn:
        rows = conn.execute(raw_responses.select()).fetchall()
        assert len(rows) == 1
        stored = load_response(conn, rows[0].raw_response_id)

    assert stored.body == BODY, "the archived body is not the bytes that were received"
    assert stored.endpoint == INSTITUTIONS_GET.path
    assert stored.connection_id is None, "an institution search belongs to no connection"
    assert "42 matching" in out


def test_check_refuses_a_missing_datastore_before_reaching_the_aggregator(
    cli_env: Config,
    offline_client: type[RecordingClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ordering, not just the refusal.

    `store init` is deliberately not run. If the aggregator were called first, a
    typo'd datastore path would cost a live round-trip -- and rate limits are per
    client, on a command whose whole purpose is being run when something is
    already wrong.
    """
    assert run(["connector", "check"]) == 2
    err = capsys.readouterr().err

    assert "not ready" in err
    assert not RecordingClient.instances, "the aggregator was reached before the datastore check"


def test_check_asks_for_the_countries_it_was_given(
    cli_env: Config,
    offline_client: type[RecordingClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(["store", "init"]) == 0
    assert run(["connector", "check", "--country", "US", "--country", "CA"]) == 0

    assert RecordingClient.instances[0].calls == [
        {"count": 1, "offset": 0, "country_codes": ["US", "CA"]}
    ]


def test_check_reports_a_body_it_cannot_parse_rather_than_failing(
    cli_env: Config,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The archive holds bytes; the summary line is a convenience over them.

    A response shape this build does not recognize must still be archived --
    that is the whole point of keeping it verbatim -- so the parse is allowed to
    give up while the command succeeds.
    """

    class UnparseableClient(RecordingClient):
        def institutions_get(
            self, *, count: int, offset: int, country_codes: list[str]
        ) -> FetchedResponse:
            return FetchedResponse(
                endpoint=INSTITUTIONS_GET,
                body=b"\x00\x01 not json at all",
                received_at=now_utc(),
                request_context=None,
            )

    monkeypatch.setattr("bankmachine.cli.connector.PlaidClient", UnparseableClient)
    assert run(["store", "init"]) == 0
    capsys.readouterr()

    assert run(["connector", "check"]) == 0
    out = capsys.readouterr().out
    assert "institutions:" not in out

    with reader_connection(cli_env) as conn:
        rows = conn.execute(raw_responses.select()).fetchall()
        assert load_response(conn, rows[0].raw_response_id).body == b"\x00\x01 not json at all"


def test_the_secret_never_appears_in_the_output(
    cli_env: Config,
    offline_client: type[RecordingClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(["store", "init"]) == 0
    assert run(["connector", "check"]) == 0
    captured = capsys.readouterr()

    assert "test-secret" not in captured.out
    assert "test-secret" not in captured.err


def test_the_clients_request_context_reaches_the_archive_unaltered(
    cli_env: Config,
    offline_client: type[RecordingClient],
) -> None:
    """Plumbing only.

    Whether the context is free of credentials is a property of the client that
    builds it, and it is asserted there against the real one
    (`tests/connector/test_client.py`). Asserting it here, where the client is a
    stub, would only confirm the stub.
    """
    assert run(["store", "init"]) == 0
    assert run(["connector", "check"]) == 0

    with reader_connection(cli_env) as conn:
        rows = conn.execute(raw_responses.select()).fetchall()
        context = rows[0].request_context

    assert context == RecordingClient.instances[0].last_context


class FakeStdin:
    """A stdin whose terminal-ness is the thing under test.

    Under pytest the real `sys.stdin` is never a terminal, so the branch an
    operator actually takes would never execute -- which is how a prompt that
    echoes a secret ships green.
    """

    def __init__(self, *, tty: bool, line: str = "") -> None:
        self._tty = tty
        self._line = line

    def isatty(self) -> bool:
        return self._tty

    def readline(self) -> str:
        return self._line


def test_set_secret_prompts_without_echoing_at_a_terminal(
    cli_env: Config,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prompts: list[str] = []

    def fake_getpass(prompt: str) -> str:
        prompts.append(prompt)
        return "  typed-secret  \n"

    monkeypatch.setattr("sys.stdin", FakeStdin(tty=True))
    monkeypatch.setattr("getpass.getpass", fake_getpass)

    assert run(["connector", "set-secret"]) == 0
    captured = capsys.readouterr()

    assert prompts, "at a terminal the operator got no prompt and no idea it was waiting"
    assert cli_env.environment in prompts[0], (
        "the prompt must name the environment: storing a production secret while "
        "believing it disposable is the mistake this wording prevents"
    )
    assert get_plaid_secret(cli_env) == "typed-secret", "the secret was stored unstripped"
    assert "typed-secret" not in captured.out
    assert "typed-secret" not in captured.err


def test_set_secret_reads_a_piped_secret_without_prompting(
    cli_env: Config,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A password manager feeds this on stdin; prompting there would hang."""

    def refuse(prompt: str) -> str:
        raise AssertionError("a piped secret must not go through the terminal prompt")

    monkeypatch.setattr("sys.stdin", FakeStdin(tty=False, line="piped-secret\n"))
    monkeypatch.setattr("getpass.getpass", refuse)

    assert run(["connector", "set-secret"]) == 0
    captured = capsys.readouterr()

    assert get_plaid_secret(cli_env) == "piped-secret"
    assert "piped-secret" not in captured.out
    assert cli_env.plaid_keychain_account in captured.out, (
        "the operator should be told which keychain entry was written"
    )


def test_set_secret_refuses_an_empty_secret(
    cli_env: Config,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An empty line must not silently overwrite a working credential."""
    monkeypatch.setattr("sys.stdin", FakeStdin(tty=False, line="\n"))

    assert run(["connector", "set-secret"]) == 2
    assert "empty" in capsys.readouterr().err
    assert get_plaid_secret(cli_env) == "test-secret", "the existing secret was destroyed"


@pytest.mark.parametrize(
    ("body", "mode"),
    [
        (b'{"institutions": [], "total": 4', "malformed syntax"),
        (("[" * 100_000 + "]" * 100_000).encode(), "nested past the decoder's stack"),
        (b'{"total": 42, "name": "\xff\xfe"}', "bytes that are not UTF-8"),
    ],
    ids=["syntax", "depth", "encoding"],
)
def test_a_body_the_parser_cannot_read_costs_a_line_not_the_report(body: bytes, mode: str) -> None:
    """The count is read for the operator's benefit; the archive already has the bytes.

    🔴 That decision was only two-thirds implemented. `RecursionError` is a
    `RuntimeError`, so it fell through a clause naming `ValueError` and
    `UnicodeDecodeError` -- and by the time this runs the response is already
    archived, so letting it out would end `connector check` with a traceback in
    place of the line saying where the bytes went.
    """
    assert connector_cli._reported_total(body) is None, (
        f"a body {mode} took down the report instead of one line of it"
    )
