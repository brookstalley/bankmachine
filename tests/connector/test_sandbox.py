"""Live calls to the aggregator's sandbox. Deselected by default.

    uv run pytest -m sandbox

A suite that needs network and credentials on every run stops being run, so the
offline suite is the one that has to stay honest and these are opt-in. What they
buy is the thing no fixture can: confirmation that the shape the fixtures were
recorded from is still the shape the aggregator sends.

**Recording a fixture.** With credentials in place:

    BANKMACHINE_RECORD_FIXTURES=1 uv run pytest -m sandbox

writes the response bodies under `tests/connector/fixtures/`. Without it, the
same tests *compare* the live response against what was recorded -- which is the
point of keeping both: the fixture and the live call are two independent
descriptions of the aggregator's shape, and only their disagreement can tell you
the aggregator changed.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from bankmachine.config import Config, load_config
from bankmachine.connector import INSTITUTIONS_GET
from bankmachine.connector.plaid.client import PlaidClient
from bankmachine.secrets import AggregatorCredentialMissingError, get_plaid_secret

pytestmark = pytest.mark.sandbox

FIXTURES = Path(__file__).parent / "fixtures"
RECORDING = os.environ.get("BANKMACHINE_RECORD_FIXTURES") == "1"


@pytest.fixture
def sandbox_client() -> Any:
    """A client against the real sandbox, from the operator's own configuration.

    Skips rather than fails when credentials are absent: a missing sandbox
    credential means this suite was selected on a machine that cannot run it,
    which is a different thing from the aggregator being wrong.
    """
    config: Config = load_config()
    if config.environment != "sandbox":
        config = replace(config, environment="sandbox")
    if not config.plaid_client_id:
        pytest.skip("no aggregator client id configured; set BANKMACHINE_PLAID_CLIENT_ID")
    try:
        secret = get_plaid_secret(config)
    except AggregatorCredentialMissingError:
        pytest.skip("no aggregator secret in the keychain; run `bankmachine connector set-secret`")
    with PlaidClient(config, secret) as client:
        yield client


def _fixture_path(name: str) -> Path:
    return FIXTURES / f"{name}.json"


def _record_or_compare(name: str, body: bytes) -> dict[str, Any]:
    """Write the fixture, or check the live shape still matches it."""
    payload: dict[str, Any] = json.loads(body)
    path = _fixture_path(name)
    if RECORDING:
        FIXTURES.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return payload
    if not path.exists():
        pytest.skip(f"no recorded fixture at {path}; run with BANKMACHINE_RECORD_FIXTURES=1")
    recorded: dict[str, Any] = json.loads(path.read_bytes())
    # Keys, not values: the sandbox's institution list changes as the aggregator
    # adds test institutions, and a test that pinned values would fail for a
    # reason that says nothing about this product.
    assert set(payload) == set(recorded), (
        f"the aggregator's {name} response no longer has the keys the fixtures were "
        f"recorded from; the derivers written against them may be reading fields that "
        f"are gone"
    )
    return payload


def test_institutions_get_answers_and_its_shape_is_unchanged(sandbox_client: Any) -> None:
    fetched = sandbox_client.institutions_get(count=1, offset=0, country_codes=["US"])

    assert fetched.endpoint == INSTITUTIONS_GET
    assert isinstance(fetched.body, bytes)
    assert fetched.body, "the aggregator answered with an empty body"

    payload = _record_or_compare("institutions_get", fetched.body)

    assert "institutions" in payload
    assert "request_id" in payload, (
        "the aggregator's request id is what makes a failure traceable in its dashboard"
    )


def test_the_body_is_undecoded_bytes_from_a_real_response(sandbox_client: Any) -> None:
    """The verbatim guarantee, against the live path rather than a fake.

    The offline suite proves `_fetch` passes the bytes through; this proves the
    SDK actually hands them over undecoded when talking to a real server, which
    is the half a fake cannot establish.
    """
    fetched = sandbox_client.institutions_get(count=1, offset=0, country_codes=["US"])

    assert isinstance(fetched.body, bytes)
    assert json.loads(fetched.body), "the archived bytes are not the JSON that was sent"
    assert fetched.body.strip().startswith(b"{")
