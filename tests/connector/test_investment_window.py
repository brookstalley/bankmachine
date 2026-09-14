"""The archived window: what the client writes down, and what a replay reads back.

`/investments/transactions/get` does not echo its window back *(measured,
`api-notes-plaid.md` §26)*, so the `request_context` archived beside a page is
the only record of the question that page answered -- and `store rebuild`
reassembles every window from it in order to reproduce that window's soft
deletes.

🔴 **Two halves, and they are tested as two.** The formatter and the reader
round-trip here as a property; the CLIENT actually calling the formatter is
anchored against the real client below, because a round-trip test alone stays
green with the call site reverted to a hand-built string.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from bankmachine.config import Config
from bankmachine.connector import INVESTMENTS_TRANSACTIONS_GET
from bankmachine.connector.plaid.client import INVESTMENT_TRANSACTIONS_PAGE_SIZE, PlaidClient
from bankmachine.connector.plaid.errors import RetryPolicy
from bankmachine.connector.plaid.window import (
    UnreadableWindowError,
    investment_window_context,
    read_investment_transaction_page,
)
from bankmachine.store.raw import RawResponse, body_digest
from bankmachine.store.types import calendar_date, utc_instant

RECEIVED = utc_instant(datetime(2026, 9, 12, 14, 0, tzinfo=UTC))


class FakeHttpResponse:
    """Stands in for the urllib3 response the SDK returns undecoded."""

    def __init__(self, data: object) -> None:
        self.data = data

    def release_conn(self) -> None:
        return None


def _archived(body: bytes, *, request_context: str | None) -> RawResponse:
    """One page as it sits in the archive, without going near a datastore."""
    return RawResponse(
        raw_response_id=7,
        connection_id=1,
        endpoint=INVESTMENTS_TRANSACTIONS_GET.path,
        received_at=RECEIVED,
        body=body,
        body_sha256=body_digest(body),
        request_context=request_context,
    )


def _body(entries: list[dict[str, Any]], *, total: object = None) -> bytes:
    payload: dict[str, Any] = {
        "investment_transactions": entries,
        "request_id": "req-1",
    }
    if total is not None:
        payload["total_investment_transactions"] = total
    return json.dumps(payload).encode()


# --------------------------------------------------------------------------
# The format, round-tripped
# --------------------------------------------------------------------------


@given(
    start=st.dates(min_value=date(2000, 1, 1), max_value=date(2099, 12, 31)),
    span=st.integers(min_value=0, max_value=730),
    offset=st.integers(min_value=0, max_value=100_000),
    count=st.integers(min_value=1, max_value=500),
)
def test_every_window_this_build_asks_for_reads_back_as_the_window_it_asked_for(
    start: date, span: int, offset: int, count: int
) -> None:
    """The property the replay rests on, stated as one.

    A window that formatted cleanly and parsed back as something else would not
    fail anywhere visible: the rebuild would reconcile against the wrong date
    bound and retire whatever fell outside it, and the only report would be a
    content digest that no longer matched.
    """
    end = start + timedelta(days=span)
    context = investment_window_context(start_date=start, end_date=end, offset=offset, count=count)

    page = read_investment_transaction_page(_archived(_body([]), request_context=context))

    assert page.window_start == calendar_date(start)
    assert page.window_end == calendar_date(end)
    assert page.offset == offset


def test_the_rows_a_page_carried_are_counted_from_its_own_body() -> None:
    """`offset` is how many came before, so the two add up to the window's progress."""
    context = investment_window_context(
        start_date=date(2024, 9, 12), end_date=date(2026, 9, 12), offset=500, count=500
    )
    body = _body([{"investment_transaction_id": "inv-1"}, {"investment_transaction_id": "inv-2"}])

    page = read_investment_transaction_page(_archived(body, request_context=context))

    assert page.rows == 2
    assert page.rows_through_this_page == 502


def test_a_page_that_states_no_total_is_an_unmeasured_window() -> None:
    """Nothing can be concluded about what is missing from a window with no size."""
    page = read_investment_transaction_page(
        _archived(_body([]), request_context=_context(offset=0))
    )
    assert page.stated_total is None


def test_a_total_that_is_not_a_number_leaves_the_window_unmeasured() -> None:
    """🔴 `true` is an `int` in Python, and a total of 1 is one a window can reach.

    A boolean read as a total would let a single-row page exhaust the window and
    soft-delete everything else in it, on the path that believes it saw the
    whole thing.
    """
    for total in (True, "12", None, [12]):
        page = read_investment_transaction_page(
            _archived(_body([], total=total), request_context=_context(offset=0))
        )
        assert page.stated_total is None, total


def test_a_page_that_does_not_say_what_window_it_answered_is_refused() -> None:
    """Refused, never defaulted: a guessed window is a guessed set of removals."""
    for context in (None, "", "offset=0", "window=yesterday offset=0 count=1"):
        with pytest.raises(UnreadableWindowError, match="does not record the window"):
            read_investment_transaction_page(_archived(_body([]), request_context=context))


def _context(*, offset: int) -> str:
    return investment_window_context(
        start_date=date(2024, 9, 12),
        end_date=date(2026, 9, 12),
        offset=offset,
        count=INVESTMENT_TRANSACTIONS_PAGE_SIZE,
    )


# --------------------------------------------------------------------------
# The client's own call site
# --------------------------------------------------------------------------


def test_the_client_archives_a_window_the_replay_can_read(config: Config) -> None:
    """🔴 Anchored on the CALL, because the round-trip above does not reach it.

    The formatter and the reader agree whatever the client does with them. What
    this asserts is that the string the client actually archives is one the
    replay can parse, and that it describes the window the client was asked for
    -- reverting the call site to a hand-built spelling reddens this and nothing
    else.
    """
    client_config = replace(config, plaid_client_id="test-client-id")
    start, end = date(2024, 9, 12), date(2026, 9, 12)

    def invoke(request: Any, **kwargs: Any) -> FakeHttpResponse:
        return FakeHttpResponse(_body([{"investment_transaction_id": "inv-1"}], total=9))

    with PlaidClient(
        client_config, "test-secret", retry_policy=RetryPolicy(attempts=1), sleep=lambda _: None
    ) as client:
        client._api.investments_transactions_get = invoke
        fetched = client.investments_transactions_get(
            "access-token", start_date=start, end_date=end, offset=300
        )

    page = read_investment_transaction_page(
        _archived(fetched.body, request_context=fetched.request_context)
    )
    assert page.window_start == calendar_date(start)
    assert page.window_end == calendar_date(end)
    assert page.offset == 300
    assert page.stated_total == 9
    assert fetched.request_context is not None
    assert "test-secret" not in fetched.request_context, (
        "the secret reached the archive's request context"
    )
