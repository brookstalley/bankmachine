"""Account lifecycle on the wire: FR-9, AC-12.1 through AC-12.9. #40.

🔴 The bug this file exists to keep closed is the quietest one this surface can
produce. An account stops being listed by its institution, the pipeline reports
nothing wrong — there is nothing wrong with it — and the last balance that
account ever had keeps arriving as a current balance forever. Nothing throws,
the number is well-formed, and its only defect is that it stopped being true on
a date nobody published. A paid-off card still reads as debt owed; an emptied
account whose money moved into another enrolled account is counted twice.

🔴 **#40's own body says this is "a read-path change… not a data-model change
and not a migration", and the correction is most of the item.** Nothing in this
product had ever written a non-`active` `lifecycle_status`: the deriver
hardcoded `"active"` on insert and excluded the column on update, and no CLI or
MCP path could set it. So this is a *population* path (migration 003's
`last_seen_date`, written as a monotone maximum), a *read* path (the four row
fields and the derived verdict), and one migration — and the tests below are
grouped that way.

The fixtures replay REAL archived responses through the REAL derivers, for the
reason every fixture in this repo does: hand-inserted rows would encode this
file's assumptions about the schema's constraints instead of exercising them,
and the whole claim under test is that a shrinking roster produces a state
change. A hand-written row would assert the state, not the change.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from bankmachine import query
from bankmachine.config import Config
from bankmachine.connector import ACCOUNTS_GET, TRANSACTIONS_SYNC
from bankmachine.derivers import ALL_DERIVERS
from bankmachine.store.derivation import apply_response
from bankmachine.store.engine import writer_connection
from bankmachine.store.rebuild import rebuild
from bankmachine.store.schema import accounts, connections, institutions
from bankmachine.store.types import CalendarDate, UtcInstant, calendar_date, now_utc

# 🔴 Imported rather than copied, exactly as `test_account_coverage.py` does it:
# `_call` drives the REAL server loop over string buffers, and a second copy here
# would be a second description of the protocol that drifts where it matters.
from test_mcp import _call

KEPT = "acct-kept"
DROPPED = "acct-dropped"


def _accounts_body(source_ids: list[str], *, balance: str = "110.94") -> bytes:
    """One `/accounts/get` response listing exactly the accounts named.

    Shrinking this list between two calls is the entire fixture AC-12.9 asks
    for: an institution that stops listing an account says so by not saying it,
    which is why the state change is invisible to anything reading one response
    at a time.
    """

    def account(source_id: str) -> dict[str, Any]:
        return {
            "account_id": source_id,
            "name": f"Account {source_id}",
            "mask": "0000",
            "type": "depository",
            "subtype": "checking",
            "balances": {
                "current": balance,
                "available": None,
                "limit": None,
                "iso_currency_code": "USD",
            },
        }

    return json.dumps(
        {
            "accounts": [account(source_id) for source_id in source_ids],
            "item": {"item_id": "item-life"},
            "request_id": f"req-{'-'.join(source_ids) or 'empty'}",
        }
    ).encode()


def _enroll(config: Config, *, source_connection_id: str = "item-life") -> int:
    """One institution and one live connection, so a roster has somewhere to land."""
    now = now_utc()
    with writer_connection(config) as conn:
        institution_pk = conn.execute(
            institutions.insert().values(
                source_institution_id=f"ins-{source_connection_id}",
                name=f"Bank of {source_connection_id}",
                first_seen_at=now,
                last_seen_at=now,
            )
        ).inserted_primary_key
        assert institution_pk is not None
        connection_pk = conn.execute(
            connections.insert().values(
                institution_id=int(institution_pk[0]),
                source_connection_id=source_connection_id,
                credential_ref=f"connection:sandbox:{source_connection_id}",
                capabilities="[]",
                requested_history_days=730,
                granted_history_days=730,
                status="active",
                last_success_at=now,
                enrolled_at=now,
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key
        assert connection_pk is not None
        return int(connection_pk[0])


def _observe(
    config: Config,
    connection_id: int,
    source_ids: list[str],
    *,
    at: UtcInstant,
    balance: str = "110.94",
) -> None:
    """One successful roster observation, replayed through the real deriver."""
    with writer_connection(config) as conn:
        apply_response(
            conn,
            connection_id=connection_id,
            endpoint=ACCOUNTS_GET.path,
            body=_accounts_body(source_ids, balance=balance),
            received_at=at,
            derivers=ALL_DERIVERS,
        )


def _shrinking_roster(config: Config) -> tuple[UtcInstant, UtcInstant]:
    """Two observations, thirty days apart, the second one account shorter.

    🔴 This is AC-12.9's fixture, and it is the reason #40 is the buildable one
    of the three production blockers. #22 needs a real settlement cycle and #23
    needs a real deposit; a shrinking roster is just two archived responses, and
    the sandbox's inability to *produce* one is not an inability to *replay* one.
    """
    later = now_utc()
    earlier = UtcInstant(later - timedelta(days=30))
    connection_id = _enroll(config)
    _observe(config, connection_id, [KEPT, DROPPED], at=earlier)
    _observe(config, connection_id, [KEPT], at=later)
    return earlier, later


def _sync_body(source_id: str, dates: list[str]) -> bytes:
    """Transactions for one account, so it has a cadence to fall silent against.

    🔴 The lifecycle tests that touch `silence_exceeds_cadence` NEED this. An
    account with no transactions has a null `silence_ratio` and therefore a false
    flag no matter what its lifecycle says, so a suppression test built on a
    transaction-less account passes whether the suppression exists or not.
    Measured, not reasoned: the first version of the AC-12.7 test below was
    exactly that, and it stayed green with the suppression deleted.
    """

    def txn(index: int, day: str) -> dict[str, Any]:
        return {
            "account_id": source_id,
            "transaction_id": f"{source_id}-{index}",
            "amount": "10.00",
            "iso_currency_code": "USD",
            "date": day,
            "authorized_date": None,
            "pending": False,
            "pending_transaction_id": None,
            "name": "Coffee",
            "merchant_name": None,
            "personal_finance_category": {
                "primary": "FOOD_AND_DRINK",
                "detailed": "FOOD_AND_DRINK",
            },
        }

    return json.dumps(
        {
            "accounts": [],
            "added": [txn(i, day) for i, day in enumerate(dates)],
            "modified": [],
            "removed": [],
            "next_cursor": f"cursor-{source_id}",
            "has_more": False,
            "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE",
            "request_id": f"req-sync-{source_id}",
        }
    ).encode()


def _post_transactions(
    config: Config,
    connection_id: int,
    source_id: str,
    *,
    at: UtcInstant,
    spacing_days: int = 30,
    count: int = 4,
    silent_days: int = 0,
) -> None:
    """A regular cadence that then stops, so `silence_ratio` is a real number."""
    days = [
        str(at.date() - timedelta(days=spacing_days * offset + silent_days))
        for offset in reversed(range(count))
    ]
    with writer_connection(config) as conn:
        apply_response(
            conn,
            connection_id=connection_id,
            endpoint=TRANSACTIONS_SYNC.path,
            body=_sync_body(source_id, days),
            received_at=at,
            derivers=ALL_DERIVERS,
        )


def _rows(config: Config, tool: str = "list_accounts") -> dict[str, dict[str, Any]]:
    wire = _call(config, tool, {})["structuredContent"]
    key = "name" if tool == "list_accounts" else "account"
    return {row[key]: row for row in wire["rows"]}


def _wire(config: Config, tool: str = "list_accounts") -> dict[str, Any]:
    result: dict[str, Any] = _call(config, tool, {})["structuredContent"]
    return result


def _kinds(wire: dict[str, Any]) -> set[str]:
    return {caveat["kind"] for caveat in wire["warnings"]}


def _declare_closed(
    config: Config, source_account_id: str, closed_on: CalendarDate | None = None
) -> None:
    """What the operator does, standing in for the command that does it.

    🔴 This write is the one thing AC-12.6 rests on and the one thing no shipped
    code path performs — see this file's closing test, which asserts that gap
    rather than papering over it.

    🔴 **It writes the two operator-owned columns and deliberately does NOT touch
    `updated_at`**, and the reason belongs to whoever builds the command this
    stands in for. `updated_at` is derivation-owned: `_upsert_account` writes it
    from `response.received_at` on every sync, and `_OPERATOR_OWNED` does not
    shield it. A retire command that bumped it would be reverted by the next
    sync, and — worse — `store rebuild` would then find content it could not
    reproduce and REFUSE the whole rebuild, because `updated_at` is inside
    `content_digest`. Measured here, not assumed: bumping it in this helper is
    what made this file's rebuild test raise `RebuildNotReproducibleError`.
    """
    with writer_connection(config) as conn:
        account_id = conn.execute(
            select(accounts.c.account_id, accounts.c.first_seen_date).where(
                accounts.c.source_account_id == source_account_id
            )
        ).one()
        conn.execute(
            update(accounts)
            .where(accounts.c.account_id == account_id[0])
            .values(
                lifecycle_status="inactive",
                closed_date=closed_on or calendar_date(account_id[1]),
            )
        )


# --------------------------------------------------------------------------
# AC-12.4 · The population path — the part #40 says does not exist
# --------------------------------------------------------------------------


def test_a_roster_observation_records_when_each_account_was_last_listed(
    initialized_config: Config,
) -> None:
    """AC-12.4. Without this column there is no non-`active` state to read at all."""
    earlier, later = _shrinking_roster(initialized_config)

    with writer_connection(initialized_config) as conn:
        seen = {
            str(row[0]): (str(row[1]), str(row[2]))
            for row in conn.execute(
                select(
                    accounts.c.source_account_id,
                    accounts.c.first_seen_date,
                    accounts.c.last_seen_date,
                )
            ).all()
        }

    assert seen[KEPT] == (str(earlier.date()), str(later.date())), (
        "the account listed twice should carry the earliest mention at one end and the "
        "latest at the other"
    )
    assert seen[DROPPED] == (str(earlier.date()), str(earlier.date())), (
        "the account the second roster did not list should not have moved"
    )


def test_the_last_listed_date_is_a_maximum_so_a_replay_lands_the_same_either_way(
    initialized_config: Config,
) -> None:
    """🔴 AC-12.4's monotone maximum, which is what makes the record a fact.

    `first_seen_date` is a minimum and `last_seen_date` is a maximum, so
    replaying an archive in ANY order converges on the same pair. Replaying the
    older response last is not a hypothetical: `store rebuild` orders by
    `received_at`, but a repaired connection, a backfill, and a manual re-derive
    all deliver responses in whatever order they were archived — and the
    deriver's own note recorded this as the specific care retirement was waiting
    on rather than the reason it could not be built.
    """
    later = now_utc()
    earlier = UtcInstant(later - timedelta(days=30))
    connection_id = _enroll(initialized_config)

    # The SHORTER, LATER roster first, then the older fuller one.
    _observe(initialized_config, connection_id, [KEPT], at=later)
    _observe(initialized_config, connection_id, [KEPT, DROPPED], at=earlier)

    rows = _rows(initialized_config)
    assert rows[f"Account {KEPT}"]["lifecycle"] == "active"
    assert rows[f"Account {DROPPED}"]["lifecycle"] == "no_longer_reported", (
        "replaying the two observations in the other order changed the verdict, so the "
        "record is a fact about the replay rather than about the responses"
    )
    assert rows[f"Account {DROPPED}"]["last_seen_in_roster"] == str(earlier.date())


# --------------------------------------------------------------------------
# AC-12.9 · The shrinking roster, end to end — the assertion seen red
# --------------------------------------------------------------------------


def test_an_account_the_roster_stopped_listing_is_reported_no_longer_reported(
    initialized_config: Config,
) -> None:
    """🔴 AC-12.9, and the one assertion this whole item exists to make true.

    Two archived roster observations for one connection, the second listing one
    fewer account. The account's lifecycle changes, and its evidence dates are
    what the observations imply — which is the part that makes the verdict
    auditable rather than asserted.
    """
    earlier, later = _shrinking_roster(initialized_config)

    rows = _rows(initialized_config)
    kept = rows[f"Account {KEPT}"]
    dropped = rows[f"Account {DROPPED}"]

    assert kept["lifecycle"] == "active"
    assert kept["last_seen_in_roster"] == str(later.date())
    assert kept["roster_last_observed"] == str(later.date())

    assert dropped["lifecycle"] == "no_longer_reported"
    assert dropped["last_seen_in_roster"] == str(earlier.date())
    assert dropped["roster_last_observed"] == str(later.date()), (
        "the roster observation date must come from the CONNECTION, not from the account; "
        "read off the account it would equal `last_seen_in_roster` and nothing could ever "
        "be absent"
    )
    assert dropped["closed_date"] is None, (
        "nothing declared this account closed, and the derived signal must never write one"
    )


def test_the_verdict_is_re_derivable_from_the_row_without_a_second_call(
    initialized_config: Config,
) -> None:
    """AC-12.3: the evidence rides beside the verdict, as dates rather than a flag.

    The `silence_ratio` ruling applied on this axis — a consumer must be able to
    check the server's arithmetic from the payload it already holds.
    """
    _shrinking_roster(initialized_config)

    for row in _rows(initialized_config).values():
        derived = row["last_seen_in_roster"] < row["roster_last_observed"]
        assert (row["lifecycle"] == "no_longer_reported") is derived, row


# --------------------------------------------------------------------------
# AC-12.1, AC-12.2 · The row, and the vocabulary on it
# --------------------------------------------------------------------------


def test_every_account_row_carries_the_lifecycle_fields(initialized_config: Config) -> None:
    """🔴 AC-12.1: always present, never behind a parameter, on every row.

    The same argument #19 settled for coverage. The consumer this fails is the
    agent that never thought to ask the verification surface, and a field a
    caller must opt into is a field that caller still does not have.
    """
    _shrinking_roster(initialized_config)
    fields = {"lifecycle", "closed_date", "last_seen_in_roster", "roster_last_observed"}

    for tool in ("list_accounts", "get_coverage_report"):
        rows = _rows(initialized_config, tool)
        assert rows, tool
        for row in rows.values():
            assert fields <= set(row), f"{tool} dropped {sorted(fields - set(row))}"


def test_the_two_tools_never_disagree_about_an_accounts_lifecycle(
    initialized_config: Config,
) -> None:
    """🔴 One producer, two readers — AC-12.7's second half.

    A verification surface that contradicts the analysis surface is worse than
    one that is absent, which is why `_account_lifecycle` exists rather than each
    tool computing its own.
    """
    _shrinking_roster(initialized_config)
    _declare_closed(initialized_config, KEPT)

    listed = {row["account_id"]: row for row in _wire(initialized_config)["rows"]}
    reported = {
        row["account_id"]: row for row in _wire(initialized_config, "get_coverage_report")["rows"]
    }

    assert set(listed) == set(reported)
    for account_id, row in listed.items():
        for field in ("lifecycle", "closed_date", "last_seen_in_roster", "roster_last_observed"):
            assert row[field] == reported[account_id][field], (account_id, field)


def test_the_published_vocabulary_is_the_one_the_server_computes_from() -> None:
    """🔴 AC-12.2: the enum is taken from the constant, never retyped in `mcp.py`.

    `FLOW_CLASSES` and `GROUPINGS` are the precedent, and the failure a copy
    produces is specific: the schema starts refusing answers this server sends,
    the first time a fourth value is classified.
    """
    from bankmachine import mcp

    published = {
        definition["name"]: definition["outputSchema"]["properties"]["rows"]["items"]["properties"][
            "lifecycle"
        ]["enum"]
        for definition in mcp._tool_definitions()
        if "lifecycle" in definition["outputSchema"]["properties"]["rows"]["items"]["properties"]
    }

    assert set(published) == {"list_accounts", "get_coverage_report"}
    for name, enum in published.items():
        assert enum == list(query.LIFECYCLE_VALUES), name


def test_no_value_in_the_vocabulary_asserts_a_closure_the_aggregator_reported() -> None:
    """🔴 AC-12.2: the value naming absence is named for the OBSERVATION.

    The aggregator publishes no closure signal, so a value called `closed`
    computed from absence would be a confident wrong number of exactly the class
    this surface exists to refuse. `closed` exists, and it is reachable only from
    the operator's own declaration.
    """
    assert "no_longer_reported" in query.LIFECYCLE_VALUES
    assert query.LIFECYCLE_VALUES == ("active", "closed", "no_longer_reported")


def test_the_schema_says_what_absence_does_and_does_not_mean() -> None:
    """AC-12.2's second clause: the caveat rides the payload, not the docs.

    A consumer reading the published schema — which is the one description a
    context budget does not trim — has to meet the ambiguity there.
    """
    from bankmachine import mcp

    description = mcp._lifecycle_row_fields()["lifecycle"]["description"]

    assert "de-selected from sharing" in description
    assert "FROZEN" in description


# --------------------------------------------------------------------------
# AC-12.5 · Absence is measured against a successful observation, never silence
# --------------------------------------------------------------------------


def test_a_connection_whose_roster_was_not_fetched_marks_nothing_absent(
    initialized_config: Config,
) -> None:
    """AC-12.5's first clause. A pipeline failure is not a household event.

    A connection that simply stopped answering moves no date at all, so nothing
    under it becomes older than a maximum that did not move. The staleness this
    IS already has a home in `get_pipeline_health`.
    """
    at = now_utc()
    first = _enroll(initialized_config, source_connection_id="item-a")
    second = _enroll(initialized_config, source_connection_id="item-b")
    _observe(initialized_config, first, [KEPT, DROPPED], at=UtcInstant(at - timedelta(days=60)))
    # Only the second connection is observed again. The first goes quiet.
    _observe(initialized_config, second, ["acct-other"], at=at)

    rows = _rows(initialized_config)

    assert {row["lifecycle"] for row in rows.values()} == {"active"}, (
        "one connection going quiet marked another connection's accounts absent, so absence "
        "is being measured across connections rather than within one"
    )


def test_a_connection_whose_whole_roster_vanishes_marks_nothing_absent(
    initialized_config: Config,
) -> None:
    """🔴 AC-12.5's important clause, and it holds by construction.

    Fourteen simultaneous closures is not a thing that happens. If every account
    of a connection stops being listed at once, the connection's own maximum
    moves with them and nothing is ever older than it — which is a property of
    how the maximum is computed rather than a guard someone remembered to write.
    """
    later = now_utc()
    earlier = UtcInstant(later - timedelta(days=30))
    connection_id = _enroll(initialized_config)
    _observe(initialized_config, connection_id, [KEPT, DROPPED], at=earlier)
    # A successful fetch that lists nothing at all.
    _observe(initialized_config, connection_id, [], at=later)

    rows = _rows(initialized_config)

    assert {row["lifecycle"] for row in rows.values()} == {"active"}
    assert {row["roster_last_observed"] for row in rows.values()} == {str(earlier.date())}


def test_an_import_only_account_has_no_roster_to_be_absent_from(
    initialized_config: Config,
) -> None:
    """AC-12.5's third clause, and the reason `unknown` is not in the vocabulary.

    An FR-7 import-only account is fully operator-owned: it is honestly `active`
    until the operator says otherwise, and the two null dates carry "no roster
    basis" — a fact the row can state rather than a state it would have to
    invent.
    """
    now = now_utc()
    with writer_connection(initialized_config) as conn:
        institution_pk = conn.execute(
            institutions.insert().values(
                source_institution_id=None,
                name="Imported",
                first_seen_at=now,
                last_seen_at=now,
            )
        ).inserted_primary_key
        assert institution_pk is not None
        conn.execute(
            accounts.insert().values(
                institution_id=int(institution_pk[0]),
                connection_id=None,
                source_account_id=None,
                name="Imported account",
                account_type="depository",
                balance_class="asset",
                currency="USD",
                lifecycle_status="active",
                first_seen_date=calendar_date(now.date()),
                source="manual",
                created_at=now,
                updated_at=now,
            )
        )

    row = _rows(initialized_config)["Imported account"]

    assert row["lifecycle"] == "active"
    assert row["last_seen_in_roster"] is None
    assert row["roster_last_observed"] is None


# --------------------------------------------------------------------------
# AC-12.6 · The operator's declaration outranks the derived signal
# --------------------------------------------------------------------------


def test_the_operators_declaration_outranks_the_derived_observation(
    initialized_config: Config,
) -> None:
    """AC-12.6. `closed` is a claim, and only the operator can make it."""
    _shrinking_roster(initialized_config)
    _declare_closed(initialized_config, DROPPED, closed_on=None)

    row = _rows(initialized_config)[f"Account {DROPPED}"]

    assert row["lifecycle"] == "closed"
    assert row["closed_date"] is not None
    assert row["last_seen_in_roster"] < row["roster_last_observed"], (
        "the observation must still ride the row as evidence even where the declaration "
        "overrides the verdict"
    )


def test_a_later_sync_does_not_revert_the_operators_declaration(
    initialized_config: Config,
) -> None:
    """AC-12.6, against the path that would actually undo it.

    `_OPERATOR_OWNED` keeps the accounts deriver out of this column. Applying one
    `values` dict to both the insert and the update arms would pin
    `lifecycle_status` to `active` by the very code that is supposed to be able
    to retire it — and the revert would happen on the next scheduled sync, hours
    later, with nobody watching.
    """
    earlier, later = _shrinking_roster(initialized_config)
    _declare_closed(initialized_config, KEPT)

    connection_id = int(_wire(initialized_config)["coverage"]["connections"])
    assert connection_id == 1
    _observe(initialized_config, 1, [KEPT], at=UtcInstant(later + timedelta(days=1)))

    assert _rows(initialized_config)[f"Account {KEPT}"]["lifecycle"] == "closed"


def test_a_rebuild_does_not_undo_the_operators_declaration(
    initialized_config: Config,
) -> None:
    """AC-12.6's second half, asserted rather than assumed.

    `accounts` carries no `derivation_version_id`, so `store.rebuild` classifies
    it as a dimension and never empties it. The property is nearly free — and it
    is only *true* now that something can set the column, which is why it needs
    an assertion rather than a sentence.
    """
    _shrinking_roster(initialized_config)
    _declare_closed(initialized_config, DROPPED)

    rebuild(initialized_config, derivers=ALL_DERIVERS)

    assert _rows(initialized_config)[f"Account {DROPPED}"]["lifecycle"] == "closed"


# --------------------------------------------------------------------------
# AC-12.7 · A retired account's silence is closure, not a hole
# --------------------------------------------------------------------------


def test_a_non_active_accounts_silence_is_not_reported_as_a_coverage_finding(
    initialized_config: Config,
) -> None:
    """🔴 AC-12.7, and the defect `data-model.md` named and shipped anyway.

    A non-active account's `silence_ratio` grows without bound and its flag is
    permanently true, so the tool whose whole job is to notice a feed going quiet
    reports a finding no operator can ever clear. The RATIO stays as measured —
    it is the evidence, and `silence_ratio`'s own ruling is that a number shows
    what a boolean destroys — and only the ASSERTION stops.

    🔴 The account must have a real cadence and a real silence, or the flag is
    false for a reason that has nothing to do with the lifecycle: a
    transaction-less account has a null ratio and a false flag whatever its
    lifecycle. The assertion below is checked against that, so the test cannot
    pass by accident.
    """
    later = now_utc()
    earlier = UtcInstant(later - timedelta(days=120))
    connection_id = _enroll(initialized_config)
    _observe(initialized_config, connection_id, [KEPT, DROPPED], at=earlier)
    _post_transactions(initialized_config, connection_id, DROPPED, at=earlier)
    _observe(initialized_config, connection_id, [KEPT], at=later)

    dropped = _rows(initialized_config, "get_coverage_report")[f"Account {DROPPED}"]

    assert dropped["lifecycle"] == "no_longer_reported"
    assert dropped["silence_ratio"] is not None and dropped["silence_ratio"] > 1.0, (
        "the account is not actually overdue, so the flag below would be false whether or "
        "not the suppression exists and this test would prove nothing"
    )
    assert dropped["silence_exceeds_cadence"] is False, (
        "a non-active account's trailing silence was reported as a coverage failure, which "
        "is the finding no operator can ever clear"
    )

    # And again for the operator's own declaration, which is the other half of
    # "non-active" and reaches the same branch by a different route.
    _declare_closed(initialized_config, DROPPED)
    reclosed = _rows(initialized_config, "get_coverage_report")[f"Account {DROPPED}"]
    assert reclosed["lifecycle"] == "closed"
    assert reclosed["silence_exceeds_cadence"] is False


def test_the_flag_still_fires_for_an_account_that_is_still_being_reported(
    initialized_config: Config,
) -> None:
    """🔴 The positive control, without which the test above passes forever.

    Suppressing the flag for every account would satisfy AC-12.7 and destroy the
    tool. This is the case that must still fire.
    """
    from test_account_coverage import _seed

    _seed(initialized_config, spacing_days=30, count=4, silent_days=95)

    rows = _rows(initialized_config, "get_coverage_report")
    flagged = {name for name, row in rows.items() if row["silence_exceeds_cadence"]}

    assert flagged, "no account was flagged, so the suppression above proved nothing"
    for name in flagged:
        assert rows[name]["lifecycle"] == "active"


def test_a_closed_account_with_no_transactions_carries_both_warnings(
    initialized_config: Config,
) -> None:
    """The two axes meet, and neither suppresses the other.

    They are both true and they say different things: one is "no transaction was
    ever recorded", the other is "this balance stopped being a fact about today".
    A consumer that acted on one alone would be right about half the answer.
    """
    _shrinking_roster(initialized_config)
    _declare_closed(initialized_config, DROPPED)

    kinds = _kinds(_wire(initialized_config))

    assert {"accounts_without_coverage", "account_no_longer_active"} <= kinds


# --------------------------------------------------------------------------
# AC-12.8 · The ruled treatment: include, and state what was included
# --------------------------------------------------------------------------


def test_the_envelope_counts_every_account_and_says_how_many_are_not_active(
    initialized_config: Config,
) -> None:
    """🔴 AC-12.8, and the fifth norm's one named retroactivity debt.

    `coverage.accounts` was an unfiltered `COUNT(*)` sitting between two counts
    that both filter. It is not grandfathered: it keeps counting everything, and
    it gains the figure that makes the count legible.
    """
    _shrinking_roster(initialized_config)

    coverage = _wire(initialized_config)["coverage"]

    assert coverage["accounts"] == 2, "the count must still include the non-active account"
    assert coverage["accounts_not_active"] == 1


def test_the_flagged_magnitude_is_present_and_zero_when_nothing_qualifies(
    initialized_config: Config,
) -> None:
    """🔴 AC-12.8: present and zero rather than absent.

    A consumer branching on a key's presence must never learn something about
    the store's contents from the key set. Absent, `accounts_not_active` would be
    indistinguishable from a server that forgot the field.
    """
    connection_id = _enroll(initialized_config)
    _observe(initialized_config, connection_id, [KEPT], at=now_utc())

    for tool in ("list_accounts", "get_coverage_report", "get_pipeline_health"):
        coverage = _wire(initialized_config, tool)["coverage"]
        assert coverage["accounts_not_active"] == 0, tool
        assert coverage["not_active_balance_minor_units"] == [], tool


def test_the_magnitude_is_signed_and_grouped_by_currency(
    initialized_config: Config,
) -> None:
    """🔴 The load-bearing half of the ruling, not the flag.

    Include-and-flag is only safe because the reader can perform the subtraction
    the server refuses to perform for them, and a magnitude with no sign would
    leave them unable to tell whether removing the account raises or lowers the
    figure. Per currency, because a summed integer over two currencies is not a
    wrong number — it is not a number.
    """
    _shrinking_roster(initialized_config)

    magnitude = _wire(initialized_config)["coverage"]["not_active_balance_minor_units"]

    assert magnitude == [{"currency": "USD", "current_minor_units": 11094}]

    rows = _rows(initialized_config)
    assert rows[f"Account {DROPPED}"]["current_minor_units"] == 11094, (
        "the flagged magnitude must be the same figure the row carries, or a reader "
        "subtracting one from the other gets a number neither of them means"
    )


def test_an_answer_over_a_non_active_account_carries_the_warning(
    initialized_config: Config,
) -> None:
    """AC-12.8's last clause: the treatment reaches a consumer that read only the envelope."""
    _shrinking_roster(initialized_config)

    wire = _wire(initialized_config)
    caveat = next(c for c in wire["warnings"] if c["kind"] == "account_no_longer_active")

    assert "no longer listed" in caveat["detail"]
    assert "de-selection from sharing" in caveat["detail"], (
        "the warning must carry AC-12.2's ambiguity, or a reader takes it as a closure claim"
    )
    assert "not_active_balance_minor_units" in caveat["detail"], (
        "a warning without the figure tells a consumer something is wrong and leaves it "
        "unable to do anything about it"
    )


def test_the_warning_names_the_accounts_and_which_kind_each_one_is(
    initialized_config: Config,
) -> None:
    """ "Some accounts are inactive" is a warning nobody can act on.

    The two values mean different things to a reader — one is the operator's own
    declaration and the other is only an observation — so the warning splits
    them rather than pooling them.
    """
    _shrinking_roster(initialized_config)
    _declare_closed(initialized_config, KEPT)

    detail = next(
        c["detail"]
        for c in _wire(initialized_config)["warnings"]
        if c["kind"] == "account_no_longer_active"
    )

    assert "declared closed by the operator" in detail
    assert "no longer listed by their institution" in detail


def test_a_request_that_holds_no_non_active_account_does_not_carry_the_warning(
    initialized_config: Config,
) -> None:
    """🔴 Request-scoped, not connection-scoped — the `gapped` defect, refused again.

    A kind riding every response equally arrived character-for-character
    identical on four unrelated questions: true, and useless for telling a caller
    whether THIS answer was the degraded one.
    """
    connection_id = _enroll(initialized_config)
    _observe(initialized_config, connection_id, [KEPT], at=now_utc())

    assert "account_no_longer_active" not in _kinds(_wire(initialized_config))


# --------------------------------------------------------------------------
# The gap this build does NOT close, asserted so it cannot be mistaken for closed
# --------------------------------------------------------------------------


def test_no_shipped_code_path_can_declare_an_account_closed(
    initialized_config: Config,
) -> None:
    """🔴 `closed` is reachable in the read path and UNREACHABLE in the product.

    Every test above that produces a `closed` account writes the column from the
    test itself. There is no `bankmachine accounts retire`, and the MCP surface
    is read-only by norm, so the operator has no way to confirm what the system
    suspects and AC-12.2's honest ambiguity never resolves for any account. The
    discovery document calls this "the piece most likely to be dropped under
    schedule pressure"; it was dropped, deliberately and outside this chunk's
    ownership, and this test is what keeps that from being discovered rather than
    read.

    🔴 **Delete this test when the command lands.** It asserts an absence, and an
    absence-assertion that outlives its subject starts failing the fix.
    """
    from bankmachine.cli import build_parser

    rendered = build_parser().format_help()

    assert "retire" not in rendered or "accounts" not in rendered, (
        "an account-retirement command appears to exist now; delete this test and write the "
        "one that exercises it"
    )


@pytest.mark.parametrize("tool", ["list_accounts", "get_coverage_report"])
def test_a_store_that_cannot_be_read_still_carries_the_lifecycle_key_set(
    config: Config, tool: str
) -> None:
    """AC-ARCH.3 meets AC-12.8: the key set never depends on the store's health.

    The unreadable-datastore path builds its envelope in a different function,
    and it is the answer a consumer meets before anything else has gone right. A
    key that disappeared there would move the wire shape at exactly the moment
    someone is working out what went wrong.
    """
    assert not config.datastore_path.exists()

    coverage = _wire(config, tool)["coverage"]

    assert coverage["accounts_not_active"] == 0
    assert coverage["not_active_balance_minor_units"] == []


# ---------------------------------------------------------------------------
# The upgrade window — migration 003 lands before any post-migration sync.
#
# 🔴 These exist because the first implementation of the transitional rule read
# a null `last_seen_date` as `first_seen_date` and fabricated closures with it.
# The fallback looks true for one account and is wrong across a connection: the
# verdict compares an account against the MAXIMUM over its siblings, and
# first-seen dates legitimately differ between them.
# ---------------------------------------------------------------------------


def _migrated_but_unsynced(config: Config) -> None:
    """Roll every account back to the state migration 003 leaves them in.

    Written by clearing the column rather than by skipping the observation,
    because the accounts have to EXIST -- which before migration 003 they did,
    each with a `first_seen_date` and no `last_seen_date` at all.
    """
    with writer_connection(config) as conn:
        cleared = conn.execute(update(accounts).values(last_seen_date=None)).rowcount
    assert cleared, "no account rows were reset, so this fixture is not the state it claims"


def test_no_account_is_called_closed_before_its_connection_is_observed_once(
    initialized_config: Config,
) -> None:
    """🔴 The upgrade window must not invent a closure, and it once did.

    Two accounts on one connection, first seen a year apart -- an ordinary
    shape: a second card, a savings account opened later. Immediately after
    migration 003 neither carries a roster observation. Reading the null as
    `first_seen_date` put the older account behind a maximum built out of
    first-seen dates and reported it CLOSED, for the whole window until that
    connection's next successful sync, which for a failing connection never
    arrives.

    Nothing here is absent. Nothing has been OBSERVED, which AC-12.5 says is not
    the same thing: absence is measured against a successful observation and
    never against silence.
    """
    connection_id = _enroll(initialized_config)
    _observe(initialized_config, connection_id, ["acct-old"], at=now_utc() - timedelta(days=400))
    _observe(
        initialized_config,
        connection_id,
        ["acct-old", "acct-new"],
        at=now_utc() - timedelta(days=5),
    )
    _migrated_but_unsynced(initialized_config)

    rows = _rows(initialized_config)

    assert len(rows) == 2, f"the fixture did not produce two accounts: {list(rows)}"
    for name, row in rows.items():
        assert row["lifecycle"] == "active", (
            f"{name} was called {row['lifecycle']!r} before its connection was ever observed; "
            f"a null last_seen_date is silence, and absence is never measured against silence"
        )
        assert row["last_seen_in_roster"] is None, (
            f"{name} reported a roster date that was never recorded: {row}"
        )
        assert row["roster_last_observed"] is None, row


def test_no_warning_is_raised_before_the_connection_is_observed_once(
    initialized_config: Config,
) -> None:
    """The fabricated verdict took a warning and a magnitude with it.

    Under the include-and-flag ruling a false `no_longer_reported` also fires
    `account_no_longer_active` and reports a non-zero not-active balance -- a
    fictional figure on the balance sheet, which is worse than the wrong verdict
    that produced it.
    """
    connection_id = _enroll(initialized_config)
    _observe(initialized_config, connection_id, ["acct-old"], at=now_utc() - timedelta(days=400))
    _observe(
        initialized_config,
        connection_id,
        ["acct-old", "acct-new"],
        at=now_utc() - timedelta(days=5),
    )
    _migrated_but_unsynced(initialized_config)

    wire = _wire(initialized_config)

    assert "account_no_longer_active" not in _kinds(wire)
    assert wire["coverage"]["accounts_not_active"] == 0, wire["coverage"]
    assert wire["coverage"]["not_active_balance_minor_units"] == [], wire["coverage"]


def test_once_the_connection_is_observed_an_unlisted_account_is_still_caught(
    initialized_config: Config,
) -> None:
    """🔴 The other half, and the reason null is not filled in with a guess.

    After the fix a null means "no observation recorded". The moment ANY account
    on the connection carries one, the connection has been observed -- and an
    account still carrying none was not in that roster. That is the steady-state
    detection this whole item exists for, and it keeps working only because the
    null was left alone.
    """
    connection_id = _enroll(initialized_config)
    _observe(initialized_config, connection_id, ["acct-old"], at=now_utc() - timedelta(days=400))
    _observe(
        initialized_config,
        connection_id,
        ["acct-old", "acct-new"],
        at=now_utc() - timedelta(days=5),
    )
    _migrated_but_unsynced(initialized_config)

    # One post-migration sync, whose roster no longer lists the older account.
    _observe(initialized_config, connection_id, ["acct-new"], at=now_utc())

    rows = _rows(initialized_config)
    verdicts = {name: row["lifecycle"] for name, row in rows.items()}

    assert sorted(verdicts.values()) == ["active", "no_longer_reported"], verdicts


def test_asking_for_a_retired_accounts_transactions_says_it_is_retired(
    initialized_config: Config,
) -> None:
    """🔴 AC-12.1's rationale reaching `query_transactions`, which chunk 01 left open.

    The consumer AC-12.1 names is the agent that never thought to call the
    verification surface. Asking "what did I spend on this card" about an
    account the institution stopped reporting is exactly that agent: the rows
    stop on the day the account went quiet, and without this the answer offers
    no reason why. `accounts_without_coverage` already fires on this call for
    the coverage axis, and this is the same argument one axis over.
    """
    connection_id = _enroll(initialized_config)
    _observe(initialized_config, connection_id, ["gone", "kept"], at=now_utc() - timedelta(days=40))
    _observe(initialized_config, connection_id, ["kept"], at=now_utc())

    retired = _rows(initialized_config)["Account gone"]["account_id"]
    wire = _call(initialized_config, "query_transactions", {"account_id": retired})[
        "structuredContent"
    ]

    assert "account_no_longer_active" in _kinds(wire), (
        "a transaction query about an account its institution stopped listing said nothing "
        f"about that; warnings were {_kinds(wire)}"
    )


def test_asking_for_a_live_accounts_transactions_stays_quiet_about_lifecycle(
    initialized_config: Config,
) -> None:
    """🔴 The absence is information, so it is asserted.

    Scoped to the account ASKED ABOUT. Naming every retired account in the store
    on every page of every walk is the character-for-character noise the
    request-scoped tuple exists to refuse, and it is the failure this warning
    would drift into if the scoping were dropped.
    """
    connection_id = _enroll(initialized_config)
    _observe(initialized_config, connection_id, ["gone", "kept"], at=now_utc() - timedelta(days=40))
    _observe(initialized_config, connection_id, ["kept"], at=now_utc())

    live = _rows(initialized_config)["Account kept"]["account_id"]
    wire = _call(initialized_config, "query_transactions", {"account_id": live})[
        "structuredContent"
    ]

    assert "account_no_longer_active" not in _kinds(wire), (
        "a query about a live account was told about a different account's retirement"
    )


def test_asking_for_a_declared_closed_accounts_transactions_says_so_too(
    initialized_config: Config,
) -> None:
    """🔴 The `closed` value takes the same branch, and was covered by nothing.

    `_not_active_caveat` fires on `not entry.active`, which both `closed` and
    `no_longer_reported` satisfy — so the sibling test above passes whether the
    predicate reads the lifecycle value or only the derived half. An operator
    declaration is the case a consumer should trust MOST, since it is the one
    value in the vocabulary that is a statement of fact rather than an
    observation, and nothing asserted it reached this surface.
    """
    connection_id = _enroll(initialized_config)
    _observe(initialized_config, connection_id, ["retired", "kept"], at=now_utc())
    _declare_closed(initialized_config, "retired")

    rows = _rows(initialized_config)
    closed_id = rows["Account retired"]["account_id"]
    assert rows["Account retired"]["lifecycle"] == "closed", rows["Account retired"]

    wire = _call(initialized_config, "query_transactions", {"account_id": closed_id})[
        "structuredContent"
    ]

    assert "account_no_longer_active" in _kinds(wire), (
        "a transaction query about an account the OPERATOR declared closed said nothing about "
        f"it; warnings were {_kinds(wire)}"
    )
