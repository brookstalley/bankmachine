"""The read surface, and the envelope every answer carries.

🔴 **This module exists because of one norm.** `api-contract.md` § Direction:
*every response carries a freshness stamp, and incompleteness rides the success
path as a warning field rather than as an exception.* The dangerous case is not
an error — it is a **successful** answer computed over incomplete data, because
nothing throws and the numbers simply stop being true. The consumer is an
analyst agent that **cannot see a caveat which is not in the payload**:
documentation, log files and a health tool it did not think to call are all
invisible at the moment of answering.

So every function here returns an `Answer`, and an `Answer` cannot be built
without its warnings — they are computed from the datastore in the same call
that computes the rows.

🔴 **The environment is part of the envelope, not just the launch flag.** A
server pointed at sandbox data and one pointed at real money look identical in
their answers unless the answer says which it is. A flag selects; the envelope
confesses.

Read-role only: every handle here opens `mode=ro` at the file, so no query can
write whatever SQL reaches it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import Text, and_, case, cast, func, or_, select
from sqlalchemy.engine import Connection as SAConnection
from sqlalchemy.sql import ColumnElement

from bankmachine import signs
from bankmachine.config import Config
from bankmachine.envelope import (
    MAX_ROWS,
    STALE_AFTER,
    UNFILTERED,
    Answer,
    Caveat,
    Cursor,
    TransactionFilter,
    Truncation,
    Window,
    WindowSeries,
    iso_or_none,
    resolve_window,
)
from bankmachine.logging_setup import get_logger
from bankmachine.store import lineage, transfers
from bankmachine.store.connection import (
    DatastoreProblem,
    DatastoreStatus,
    inspect,
    remedy_for,
)
from bankmachine.store.engine import reader_connection
from bankmachine.store.lineage import SupersededSpan
from bankmachine.store.schema import (
    PROVENANCE_SOURCES,
    TRANSACTIONS_DOMAIN,
    accounts,
    balances_daily,
    connections,
    holdings,
    institutions,
    investment_transactions,
    sync_state,
    transactions,
)
from bankmachine.store.types import (
    CalendarDate,
    UtcInstant,
    calendar_date,
    has_minor_digits,
    now_utc,
    utc_instant,
)

logger = get_logger("query")


def _is_short(granted: Any, requested: Any) -> bool:
    """Whether the aggregator granted less history than was asked for.

    A named predicate rather than an inline conjunction, because the null case is
    the one that matters: null granted is NOT a shortfall of zero, it is an
    unmeasured window, and the caller reports the two differently (AC-1.3a).
    """
    return granted is not None and requested is not None and int(granted) < int(requested)


def _unstamped_ledger_date_caveat(conn: SAConnection) -> list[Caveat]:
    """The notice that this store holds rows no window can measure yet.

    🔴 **`ledger_date` is nullable, and a null is silently EXCLUDED by every
    window predicate.** `ledger_date >= since` is NULL on such a row, not true,
    so a store upgraded but not yet rebuilt answers every windowed question over
    a subset of its transactions -- and answers it as though that subset were
    all of them. An undercount that reads as complete is this product's named
    primary failure mode, so the one thing this must not do is stay quiet.

    🔴 **And it must not be repaired by coalescing to `posted_date` either.**
    That fallback returns exactly the number the column exists to stop being
    wrong -- a settled hold's posting date, in the month it moved to -- and
    returns it invisibly. Disclosed and excluded is recoverable; included and
    wrong is not.

    Connection-scoped rather than request-scoped, because it is standing state of
    the datastore rather than a property of the question asked. It names the
    remedy, because unlike most warnings on this surface there is one and the
    operator can run it: `bankmachine store rebuild` replays the archived
    responses and stamps every row.
    """
    unstamped = conn.execute(
        select(func.count())
        .select_from(transactions)
        .where(transactions.c.removed_at.is_(None), transactions.c.ledger_date.is_(None))
    ).scalar_one()
    if not unstamped:
        return []
    return [
        Caveat(
            kind="partial",
            detail=(
                f"{unstamped} transaction(s) carry no ledger date, so they predate this build "
                f"and the datastore has not been rebuilt since. Every windowed total here "
                f"EXCLUDES them and is therefore a floor, not a measurement -- run "
                f"`bankmachine store rebuild` to stamp them from the archived responses"
            ),
        )
    ]


@dataclass(frozen=True, slots=True)
class _ConnectionFreshness:
    """The three conditions the connection-level caveats fired on, for one connection.

    🔴 **One owner for a decision two passes read.** The domain caveats say a
    thing only where the connection has not already said it, and each of those
    conditions used to be re-derived beside the domain rows from the same
    columns. The copies agreed, and nothing held them there: a change to the
    connection rule -- a grace period before stale, a second status value, a
    standing complaint counting as degraded -- moves one and leaves the other,
    and both directions of that drift are defects the code already names. Two
    caveats describing one condition teaches a reader to skip the pair; a
    suppressed one that should have fired is silent staleness.

    Matching on the caveats actually emitted would be the other way to do it and
    is not faithful: `partial` carries three unrelated connection-level meanings,
    so a connection whose granted window is merely unmeasured would suppress a
    domain that has genuinely never landed.

    🔴 **Both passes read it, the connection pass included.** Building this and
    then re-writing the same three predicates at the connection caveats' own
    emit sites would leave the copy this record exists to remove -- a few lines
    further from its twin, and no easier to change together. The predicates live
    here; the sentences live at the sites.
    """

    #: `status` says so. The aggregator's STANDING complaint deliberately does
    #: not: an Item can be unwell while its last poll succeeded, so a domain
    #: failure beside one is a second fact and not a repetition.
    degraded: bool
    never_succeeded: bool
    stale: bool


def _pipeline_warnings(
    conn: SAConnection, now: UtcInstant, window: Window | None = None
) -> list[Caveat]:
    """Everything wrong with the data underneath any answer.

    Computed per call rather than cached: an answer's warnings describe the
    datastore at the moment it was read, and a cache would make them describe
    some earlier moment while the rows described this one.

    🔴 **`window` does not change WHICH warnings fire — only how `gapped` reads.**
    The connection-scoped kinds ride every response equally and that is the
    guarantee they exist to make; making one of them conditional would move it
    across the line the two tuples draw, after which the ABSENCE of a kind stops
    being information. What was wrong was never that `gapped` was always
    present, but that it arrived character-for-character identical on a window
    inside coverage, a window outside it, a future window and a query for an
    account that does not exist. A constant string is what teaches a reader to
    skip it; the same standing fact, phrased against the window in hand, is what
    makes the second answer worth reading.
    """
    warnings: list[Caveat] = _unstamped_ledger_date_caveat(conn)
    rows = conn.execute(
        select(
            connections.c.connection_id,
            institutions.c.name,
            connections.c.status,
            connections.c.last_success_at,
            connections.c.last_error_code,
            connections.c.requested_history_days,
            connections.c.granted_history_days,
            sync_state.c.history_start_date,
            connections.c.consent_expires_at,
            connections.c.source_error_code,
        )
        .select_from(
            connections.join(institutions).outerjoin(
                sync_state,
                (sync_state.c.connection_id == connections.c.connection_id)
                # 🔴 Pinned to the transactions domain because the value taken
                # from this join IS a transactions fact: `history_start_date`
                # here is the date `granted_history_days` was measured from, and
                # that measurement walks the `transactions` table. Every domain's
                # own freshness is reported beside these, per domain, by
                # `_domain_caveats` -- widening this join instead would multiply
                # one warning per domain rather than say anything new.
                & (sync_state.c.domain == TRANSACTIONS_DOMAIN),
            )
        )
        .where(connections.c.retired_at.is_(None))
    ).all()

    if not rows:
        warnings.append(
            Caveat(
                kind="partial",
                detail=(
                    "no connections are enrolled, so this answer is computed over an empty "
                    "datastore rather than over an absence of activity"
                ),
            )
        )

    freshness: dict[int, _ConnectionFreshness] = {}
    for row in rows:
        connection_id, name = int(row[0]), str(row[1])
        status, last_success = str(row[2]), row[3]
        requested, granted = row[5], row[6]
        said = _ConnectionFreshness(
            degraded=status == "degraded",
            never_succeeded=last_success is None,
            stale=last_success is not None and now - last_success > STALE_AFTER,
        )
        freshness[connection_id] = said

        # 🔴 The aggregator's STANDING complaint about the Item, which is not
        # the same fact as a failed sync attempt. An Item can be unwell while
        # the most recent poll happened to succeed, so this is checked
        # independently of `status` -- folding them together would let one
        # success bury a complaint nobody resolved.
        if row[9] is not None:
            warnings.append(
                Caveat(
                    kind="degraded",
                    detail=(
                        f"{name}'s connection carries a standing error from the aggregator "
                        f"({row[9]}), whether or not its last sync happened to succeed. Its "
                        f"data may stop without the next run failing"
                    ),
                    connection_id=connection_id,
                    institution=name,
                )
            )
        warnings.extend(
            _consent_caveats(
                name=name,
                connection_id=connection_id,
                expires_at=row[8],
                now=now,
            )
        )
        if said.degraded:
            warnings.append(
                Caveat(
                    kind="degraded",
                    detail=(
                        f"{name} last failed to sync with {row[4] or 'an unrecorded error'}; "
                        f"its data stops at the last successful run"
                    ),
                    connection_id=connection_id,
                    institution=name,
                )
            )
        if said.never_succeeded:
            warnings.append(
                Caveat(
                    kind="partial",
                    detail=f"{name} has never completed a sync, so it contributes no data yet",
                    connection_id=connection_id,
                    institution=name,
                )
            )
        elif said.stale:
            hours = int((now - last_success).total_seconds() // 3600)
            warnings.append(
                Caveat(
                    kind="stale",
                    detail=f"{name} has not synced successfully for {hours} hours",
                    connection_id=connection_id,
                    institution=name,
                )
            )
        # 🔴 The shortfall AC-11.8 exists to surface. Null granted is NOT no
        # shortfall -- it is not yet known -- and the two are reported
        # differently, because reading the first as the second is the exact
        # inference AC-1.3a forbids.
        if granted is None and not said.never_succeeded:
            warnings.append(
                Caveat(
                    kind="partial",
                    detail=(
                        f"{name}'s granted history window is not yet known; it is measured "
                        f"when the initial backfill completes"
                    ),
                    connection_id=connection_id,
                    institution=name,
                )
            )
        elif _is_short(granted, requested):
            warnings.append(
                Caveat(
                    kind="gapped",
                    detail=_gapped_detail(
                        name=name,
                        granted=int(granted),
                        requested=int(requested),
                        starts=None if row[7] is None else calendar_date(row[7]),
                        window=window,
                        today=calendar_date(now.date()),
                    ),
                    connection_id=connection_id,
                    institution=name,
                )
            )
    warnings.extend(_domain_caveats(conn, now, freshness))
    return warnings


def _domain_caveats(
    conn: SAConnection, now: UtcInstant, freshness: dict[int, _ConnectionFreshness]
) -> list[Caveat]:
    """What one sync DOMAIN is missing that the connection beside it is not.

    🔴 **The failure this exists to refuse.** `sync_state` is keyed on
    `(connection, domain)` and the domains advance on their own schedules, so a
    connection can be paging transactions happily every night while its
    investments have not returned since August. Every connection-level warning
    above reads `connections`, which cannot see that: the login works, the last
    run succeeded, and the answer comes back clean over a portfolio three weeks
    out of date. AC-4.4 names silent staleness as this system's primary failure
    mode, and a second sync domain that no surface reports is silent staleness
    with a new cause.

    🔴 **One condition, one caveat, said only where it is not already said.** A
    domain that is stale because the whole CONNECTION is stale is already named
    above, and a domain whose first attempt failed is both failing AND never
    landed -- so both cases would otherwise emit a second caveat describing a
    condition already described. Two of them riding every answer is what teaches
    a reader to skip the pair, which is the same defect as saying nothing. The
    tests are properties rather than lists of cases to exempt: a caveat fires
    when it adds something the caveats already emitted do not, so a third domain
    needs no new exemption and none can be forgotten.

    What "already said" means is READ rather than recomputed: `freshness` carries
    the conditions the connection pass fired on (`_ConnectionFreshness`), which
    is the one statement of each -- the connection caveats are emitted from the
    same record, so neither pass can drift from the other. A
    connection missing from the map is a programming error rather than a case --
    both passes select the same unretired connections -- and it is left to raise
    instead of taking a default, because either default silently answers one of
    the two questions wrongly.

    A domain with no row at all has never been attempted and raises nothing: the
    connection either cannot serve it or has not reached it, and both are the
    connection's own story rather than a hole in this one's history (AC-4.5's
    other half -- a row with a null `last_success_at` is a domain that HAS been
    tried and has never landed, which is a very different thing and does fire).
    """
    caveats: list[Caveat] = []
    rows = conn.execute(
        select(
            connections.c.connection_id,
            institutions.c.name,
            sync_state.c.domain,
            sync_state.c.last_success_at,
            sync_state.c.last_error_code,
        )
        .select_from(connections.join(institutions).join(sync_state))
        .where(connections.c.retired_at.is_(None))
        .order_by(connections.c.connection_id, sync_state.c.domain)
    ).all()
    for row in rows:
        connection_id, name = int(row[0]), str(row[1])
        domain, domain_success, domain_error = str(row[2]), row[3], row[4]
        said = freshness[connection_id]
        if domain_error is not None and not said.degraded:
            caveats.append(
                Caveat(
                    kind="degraded",
                    detail=(
                        f"{name}'s {domain} last failed with {domain_error}, while the "
                        f"connection itself is syncing normally. "
                        + (
                            f"Its {domain} have never landed in full, so it contributes "
                            f"none of that data at all"
                            if domain_success is None
                            else f"Its {domain} data stops at the last one that landed"
                        )
                        + ", and re-authenticating repairs nothing here"
                    ),
                    connection_id=connection_id,
                    institution=name,
                )
            )
        if domain_success is None:
            # 🔴 Only when nothing above has already spoken for this domain. A
            # domain whose first attempt failed satisfies both branches -- which
            # is the ORDINARY shape for a product an Item never initialized --
            # and two caveats describing one condition, riding every answer, is
            # what teaches a reader to skip the pair. The failing case says it
            # more usefully anyway: it names the error.
            if not said.never_succeeded and domain_error is None:
                caveats.append(
                    Caveat(
                        kind="partial",
                        detail=(
                            f"{name} syncs, but its {domain} have never landed in full, so "
                            f"it contributes none of that data yet"
                        ),
                        connection_id=connection_id,
                        institution=name,
                    )
                )
            continue
        behind = now - utc_instant(domain_success)
        if behind > STALE_AFTER and not said.stale:
            caveats.append(
                Caveat(
                    kind="stale",
                    detail=(
                        f"{name}'s {domain} have not landed for "
                        f"{int(behind.total_seconds() // 3600)} hours, though the connection "
                        f"itself is syncing"
                    ),
                    connection_id=connection_id,
                    institution=name,
                )
            )
    return caveats


#: How long before consent lapses the pipeline starts saying so.
#:
#: Fourteen days: long enough to act on -- re-linking an institution can involve
#: an OAuth trip, an SMS code and a bank that is down that evening -- and short
#: enough that the notice is not permanently lit, which is the state that teaches
#: a reader to skip it. The date itself always rides the detail, so a caller with
#: its own threshold reads that rather than this.
CONSENT_EXPIRY_WARNING_DAYS = 14


def _consent_caveats(
    *, name: str, connection_id: int, expires_at: object, now: UtcInstant
) -> list[Caveat]:
    """What an expiring authorisation does to an answer, before it does it.

    🔴 **The whole point is that this arrives EARLY.** The pipeline is poll-only,
    so a lapsed consent otherwise surfaces as a failure on the NEXT run -- and
    until then the connection answers *healthy* while its data quietly stops.
    The date to say so in advance is archived on every `/item/get`, and was read
    by nothing.

    🔴 **An approaching expiry is `partial`, NOT `stale`**, and the distinction is
    the decision rather than a wording choice. `stale` means "has not synced
    recently", which is a claim about the past; this is a claim about the future,
    and a reader acts on the two differently. `partial` is already defined as
    *something is not yet known, never read it as 'no shortfall'* -- and a
    consent about to lapse is exactly a known future gap in what will be known.

    🔴 **An expiry that has already PASSED is `degraded`**, because it has stopped
    being a warning about the future. The connection is not going to fail; it has
    failed, and nothing further will arrive.

    No new kind, by `api-contract.md`'s closed vocabulary. Fitting these to the
    kinds that exist is a constraint rather than a preference -- if neither could
    honestly carry the meaning that would be a finding to raise, not a licence to
    invent one.

    A null `expires_at` produces nothing at all, and that silence is honest: it
    means the Item has not been fetched since the column existed, which is not
    the same as consent that does not expire. `partial` already rides such a
    connection from the granted-window branch below.
    """
    if not isinstance(expires_at, datetime):
        return []
    expiry = utc_instant(expires_at)
    days = (expiry - now).days
    if days < 0:
        return [
            Caveat(
                kind="degraded",
                detail=(
                    f"{name}'s consent EXPIRED on {expiry.date().isoformat()}. Nothing further "
                    f"will arrive from it until the operator links it again, so its data stops "
                    f"there and every total over a later window is a floor"
                ),
                connection_id=connection_id,
                institution=name,
            )
        ]
    if days <= CONSENT_EXPIRY_WARNING_DAYS:
        return [
            Caveat(
                kind="partial",
                detail=(
                    f"{name}'s consent expires on {expiry.date().isoformat()}, in {days} day(s). "
                    f"After that its data stops arriving with no failure to notice, so what this "
                    f"connection will contribute past that date is NOT YET KNOWN -- re-link it "
                    f"before then"
                ),
                connection_id=connection_id,
                institution=name,
            )
        ]
    return []


def _gapped_detail(
    *,
    name: str,
    granted: int,
    requested: int,
    starts: CalendarDate | None,
    window: Window | None,
    today: CalendarDate,
) -> str:
    """One standing shortfall, phrased against the window actually asked about.

    🔴 **A reader who sees a different sentence on the second answer reads the
    third.** The shortfall is the same fact every time and is stated every time;
    what changes is whether this request went anywhere near it. Four requests
    that used to produce one identical string now produce four.

    🔴 **Read from the REQUESTED bounds, never the effective ones.**
    `resolve_window` clamps `since` up to the store's earliest transaction, so an
    effective window can never start before coverage — comparing against it would
    report every request as comfortably inside, which is the reassuring version
    of the bug rather than a fix for it. What a caller asked for is what says
    whether they were reaching into the ungranted span.

    🔴 And the span compared against is THIS CONNECTION'S, which is why the
    comparison is worth making at all: the clamp is store-wide, so a request can
    sit inside the store's coverage and still reach past one institution's own
    start. That connection is precisely the one whose absence reads as zero.

    The granted span is named in every branch, because a caller applying its own
    threshold needs the number whatever this answer concluded — the phrasing is
    a courtesy to a reader, never a replacement for the fact.
    """
    shortfall = f"{name} granted {granted} days of history against {requested} requested"
    if starts is None:
        return (
            f"{shortfall}, and the date its coverage begins has not been recorded yet, so "
            f"anything older than that is absent rather than zero"
        )
    begins = f"its coverage begins {starts.isoformat()}"
    if window is None:
        return (
            f"{shortfall}; {begins}. This request named no window, so it may reach past that "
            f"date -- anything older is absent rather than zero"
        )
    since, until = window.requested_since, window.requested_until
    if since is None:
        return (
            f"{shortfall}; {begins}. This request set no start, so it reaches back to that "
            f"date and no further -- anything older is absent rather than zero"
        )
    if since < starts:
        missing = (starts - calendar_date(since)).days
        return (
            f"{shortfall}, and THIS window reaches {missing} day(s) past where its data starts: "
            f"{begins}, so the part of the window before that is absent rather than zero"
        )
    if until is None or until > today:
        return (
            f"{shortfall}; {begins}, which this window starts inside. Its leading edge is "
            f"covered -- but the window reaches past today, and that tail is unanswered rather "
            f"than quiet"
        )
    return (
        f"{shortfall}; {begins}. THIS window lies wholly inside that span, so the shortfall "
        f"does not affect this answer"
    )


def _coverage(
    conn: SAConnection, *, lifecycle: dict[int, AccountLifecycle] | None = None
) -> dict[str, Any]:
    """What the datastore actually holds, so an empty answer can be told from an empty world.

    🔴 **`accounts` keeps counting every account, and states what the non-active
    ones contributed** (AC-12.8, and `api-contract.md` § Direction's fifth norm,
    whose one named retroactivity debt this is). It was an unfiltered `COUNT(*)`
    sitting between two counts that both filter, which is the norm's failure mode
    exactly: the figure was neither wrong nor legible, because nothing beside it
    said whether a frozen balance was in it.

    The treatment was ruled **include and flag**, over exclude-and-state, on the
    asymmetry in detectability: an included frozen balance is correctable by any
    reader handed the flag and the figure, while an exclusion is invisible by
    construction -- the total is simply smaller, and no field can point at what
    is not there. 🔴 **The magnitude is the load-bearing half, not the flag**, so
    both figures are present and zero rather than absent when nothing qualifies.
    A warning without the figure tells a consumer something is wrong and leaves
    it unable to do anything about it.
    """
    span = conn.execute(
        select(func.min(transactions.c.posted_date), func.max(transactions.c.posted_date)).where(
            transactions.c.removed_at.is_(None)
        )
    ).one()
    # 🔴 Reuses the lifecycle the caller already derived for its ROWS, and does
    # not re-derive one. The reader is autocommit and releases its snapshot per
    # statement, so a second walk is a second observation -- and this figure
    # qualifies the very rows beside it, so the two disagreeing is not a rare
    # race, it is one answer contradicting itself about the same account. Same
    # property, same remedy, as `signs.caveats(..., measured=...)` one surface
    # over: the parameter is what makes "one production per answer" expressible.
    # An unwindowed caller with no rows to qualify passes nothing and pays for
    # one walk, which is still one.
    entries = _account_lifecycle(conn) if lifecycle is None else lifecycle
    not_active = [entry for entry in entries.values() if not entry.active]
    return {
        "connections": conn.execute(
            select(func.count()).select_from(connections).where(connections.c.retired_at.is_(None))
        ).scalar_one(),
        "accounts": conn.execute(select(func.count()).select_from(accounts)).scalar_one(),
        # A count, so the reader can perform the subtraction this system refuses
        # to perform for them. 0 is a real answer and means every account is
        # still being reported.
        "accounts_not_active": len(not_active),
        "not_active_balance_minor_units": _not_active_balances(
            conn, [entry.account_id for entry in not_active]
        ),
        "transactions": conn.execute(
            select(func.count())
            .select_from(transactions)
            .where(transactions.c.removed_at.is_(None))
        ).scalar_one(),
        "earliest_transaction": None if span[0] is None else str(span[0]),
        "latest_transaction": None if span[1] is None else str(span[1]),
    }


def _not_active_balances(conn: SAConnection, account_ids: list[int]) -> list[dict[str, Any]]:
    """The signed magnitude the non-active accounts contribute to a balance total.

    🔴 **Per currency, never one integer across currencies**, by the ruling that
    governs every aggregate on this surface: a summed integer over two currencies
    is not a wrong number, it is not a number. An empty list is the "and zero
    rather than absent" case -- the key is always present, exactly as
    `money_summary`'s `totals` is present and empty when there is nothing to put
    in it, so the key set a consumer branches on never depends on the store's
    contents.

    🔴 **Signed from the operator's point of view**, like every stored amount, so
    a frozen credit-card balance subtracts and a frozen deposit balance adds. A
    reader handed a magnitude with the sign stripped could not tell whether
    removing these accounts would raise or lower the figure, which is the whole
    use the flag exists for.

    The latest recorded balance per account, which is the same figure
    `list_accounts` puts on the row -- the account stopped being reported, so its
    last capture is all there is and there will not be another.
    """
    if not account_ids:
        return []
    latest = (
        select(
            balances_daily.c.account_id,
            func.max(balances_daily.c.as_of_date).label("as_of_date"),
        )
        .where(balances_daily.c.account_id.in_(account_ids))
        .group_by(balances_daily.c.account_id)
        .subquery()
    )
    rows = conn.execute(
        select(balances_daily.c.currency, func.sum(balances_daily.c.current_minor))
        .select_from(
            balances_daily.join(
                latest,
                (balances_daily.c.account_id == latest.c.account_id)
                & (balances_daily.c.as_of_date == latest.c.as_of_date),
            )
        )
        .group_by(balances_daily.c.currency)
    ).all()
    # Sorted so two identical stores answer identically; the wire order of an
    # array is information a consumer may rely on even when it should not.
    return [
        {"currency": str(row[0]), "current_minor_units": int(row[1])}
        for row in sorted(rows, key=lambda r: str(r[0]))
    ]


#: How close an account's first transaction may sit to its connection's granted
#: start before the history is read as TRUNCATED rather than as genuinely
#: beginning there. Seven days: a grant boundary rarely lands exactly on an
#: account's first posting day, and inside that margin the conservative reading
#: is truncation, because calling absent data a true zero is the error that gets
#: believed rather than questioned.
GRANT_BOUNDARY_DAYS = 7


@dataclass(frozen=True, slots=True)
class AccountCoverage:
    """What the store holds for ONE account, so an empty answer about it is legible.

    🔴 The absence this describes is the finding. Nine of fourteen sandbox
    accounts have never had a transaction -- 82% of the balance sheet by
    magnitude -- and `query_transactions(account_id=9)` answered `[]` with
    nothing saying so, which reads as "no payments found" and is false. Three
    fields actively implied the opposite: `coverage.accounts` counted all
    fourteen, health reported the connection active, and the only warning was
    about depth rather than breadth (#19, AC-9.5).

    `transaction_count` is `0` rather than null for an account with nothing,
    because a null would be a second way to spell the same fact and a consumer
    would have to handle both. The dates are null because there is genuinely no
    date -- present and null, never dropped, like every other nullable on the
    wire.
    """

    account_id: int
    first_transaction_date: CalendarDate | None
    last_transaction_date: CalendarDate | None
    transaction_count: int
    #: How many investment trades the store holds for the account, removed ones
    #: excluded. Counted from their own table, never through the transactions
    #: join, and served as rows by no tool.
    investment_transaction_count: int
    #: The newest day this account's positions were captured; null when none ever
    #: was. A position refused for its unit is not a capture.
    holdings_as_of: CalendarDate | None
    #: Where this account's CONNECTION was granted history from. Null when the
    #: grant has not been measured yet, or for an import-only account that has
    #: no connection -- two different reasons, both meaning the comparison below
    #: cannot be made.
    history_starts: CalendarDate | None = None

    @property
    def truncated_by_the_grant(self) -> bool:
        """Whether this account's history was cut by the grant rather than by its age.

        🔴 **The discriminator the whole item turns on.** `first_transaction_date`
        alone cannot tell a recently-opened account -- a TRUE zero before that
        date -- from one whose history was truncated by what the institution
        granted, where everything earlier is ABSENT. Reporting either as $0 is
        the failure this product exists to refuse, and they are indistinguishable
        from the account row alone.

        Compared against the connection's own granted start: an account whose
        first transaction sits materially AFTER it genuinely has no earlier
        activity; one that starts at or near it was cut.

        🔴 Seven days, and the direction of the tie is deliberate. Inside that
        window this reports TRUNCATION, because calling absent data a true zero
        is the error that gets believed -- and a grant boundary rarely lands
        exactly on an account's first posting day.
        """
        if self.history_starts is None or self.first_transaction_date is None:
            return False
        return (self.first_transaction_date - self.history_starts).days <= GRANT_BOUNDARY_DAYS

    @property
    def unmeasured(self) -> bool:
        """The third state, which is neither a true zero nor a known truncation.

        A connection whose granted window has not been measured yet has no start
        to compare against, so this account is not *either* case -- and saying
        so is the point. It is common in the first hours of a real connection,
        and defaulting it to either branch would invent the answer.
        """
        return self.history_starts is None and self.transaction_count > 0

    @property
    def uncovered(self) -> bool:
        """No transaction has ever been recorded for this account.

        A property rather than a comparison written at each site: "counts as
        uncovered" is a rule, and a rule spelled three times is a rule that
        stops agreeing with itself.
        """
        return self.transaction_count == 0

    @property
    def no_data_in_any_feed(self) -> bool:
        """Nothing has been recorded for this account in ANY feed.

        🔴 What the LISTINGS name, where `uncovered` is what the transactions tools
        name. `list_accounts` and `get_coverage_report` describe the account
        itself, so an investment account whose trades or positions are stored has
        data, and naming it told an agent to distrust a true answer (#107).
        `query_transactions` and `money_summary` answer only from the transactions
        feed, and for them the same account's empty answer really is data not
        present -- so they keep `uncovered`. Two predicates because there are two
        questions, not one rule spelled twice.
        """
        return (
            self.transaction_count == 0
            and self.investment_transaction_count == 0
            and self.holdings_as_of is None
        )

    def to_wire(self) -> dict[str, Any]:
        """The fields every account row carries, in every tool that carries them."""
        return {
            "first_transaction_date": iso_or_none(self.first_transaction_date),
            "last_transaction_date": iso_or_none(self.last_transaction_date),
            "transaction_count": self.transaction_count,
            "investment_transaction_count": self.investment_transaction_count,
            "holdings_as_of": iso_or_none(self.holdings_as_of),
            # Present on every row, null where there is nothing to compare
            # against. It is what makes `first_transaction_date` READABLE: on its
            # own that date cannot say whether anything existed before it.
            "history_starts": iso_or_none(self.history_starts),
        }


def _account_coverage(conn: SAConnection) -> dict[int, AccountCoverage]:
    """The per-account coverage facts, for EVERY account, computed once.

    🔴 One producer with two readers, and that is the requirement rather than a
    tidiness preference. `list_accounts` needs these facts so an agent that never
    thought to call the verification surface still learns an account is empty
    (#19); `get_coverage_report` needs the same facts plus the gap analysis
    built on them (#35). Computed twice they can disagree, and a verification
    surface that contradicts the analysis surface is worse than one that is
    missing.

    🔴 **Every account appears, including the ones with no transactions**, which
    is why the join is an outer join and why `removed_at IS NULL` sits in the
    join condition rather than in a `WHERE`. In a `WHERE` it would filter away
    the null-transaction side of the outer join and the accounts this function
    exists to report would silently vanish from its own result -- the failure
    would look exactly like an account having coverage.

    Soft-deleted rows are excluded for the same reason every other reader
    excludes them: a coverage report counting rows the analysis surface cannot
    see would promise data no query can return.

    One grouped read against `transactions_by_account_date`, which is the index
    `data-model.md` names for exactly this walk.

    🔴 **The investment facts come from grouped subqueries joined per account,
    never from joining their tables beside `transactions`.** Each subquery yields
    at most one row per account, so the outer grouping is untouched. Joining the
    trade table directly would multiply each account's transaction count by its
    trades and its trades by its transactions, and a multiplied count looks
    exactly like a busy account.
    """
    trades = (
        select(
            investment_transactions.c.account_id,
            func.count(investment_transactions.c.investment_transaction_id).label("trades"),
        )
        # Removed trades are excluded for the reason removed transactions are: a
        # count of rows no reader can see promises data.
        .where(investment_transactions.c.removed_at.is_(None))
        .group_by(investment_transactions.c.account_id)
        .subquery()
    )
    positions = (
        select(holdings.c.account_id, func.max(holdings.c.as_of_date).label("captured"))
        .group_by(holdings.c.account_id)
        .subquery()
    )
    result = conn.execute(
        select(
            accounts.c.account_id,
            func.min(transactions.c.posted_date),
            func.max(transactions.c.posted_date),
            func.count(transactions.c.transaction_id),
            sync_state.c.history_start_date,
            trades.c.trades,
            positions.c.captured,
        )
        .select_from(
            accounts.outerjoin(
                transactions,
                (transactions.c.account_id == accounts.c.account_id)
                & transactions.c.removed_at.is_(None),
            )
            .outerjoin(
                sync_state,
                (sync_state.c.connection_id == accounts.c.connection_id)
                # 🔴 The transactions domain BY MEANING, not for want of a
                # second one. Every transaction fact this function computes is
                # drawn from the `transactions` table, and `history_start_date`
                # is the bound those counts are read against -- a second
                # domain's start date would be a different history compared to
                # the same rows. Widening the join instead would multiply every
                # account row by the number of domains its connection has and
                # take the counts with it. The investment facts are not read
                # against this bound at all.
                & (sync_state.c.domain == TRANSACTIONS_DOMAIN),
            )
            .outerjoin(trades, trades.c.account_id == accounts.c.account_id)
            .outerjoin(positions, positions.c.account_id == accounts.c.account_id)
        )
        # One row per account on each subquery's side, so grouping by them splits
        # nothing; they are named because a grouped select must name them.
        .group_by(
            accounts.c.account_id,
            sync_state.c.history_start_date,
            trades.c.trades,
            positions.c.captured,
        )
    ).all()
    return {
        int(row[0]): AccountCoverage(
            account_id=int(row[0]),
            # 🔴 Narrowed, never `_parse_coverage_date`. That helper reads a
            # bound back off the rendered wire dict and takes a `str`; an
            # aggregate over a `CalendarDateColumn` comes back as a plain
            # `date`, verified rather than assumed. Passing one to the other
            # returns None for every account and every coverage date on the
            # surface goes null -- a failure indistinguishable from a store with
            # no transactions, which is the exact answer this function exists to
            # tell apart.
            first_transaction_date=None if row[1] is None else calendar_date(row[1]),
            last_transaction_date=None if row[2] is None else calendar_date(row[2]),
            transaction_count=int(row[3]),
            investment_transaction_count=0 if row[5] is None else int(row[5]),
            holdings_as_of=None if row[6] is None else calendar_date(row[6]),
            history_starts=None if row[4] is None else calendar_date(row[4]),
        )
        for row in result
    }


#: 🔴 The lifecycle vocabulary, spelled ONCE. Every value names an OBSERVATION
#: and none of them names a conclusion the aggregator did not report (AC-12.2).
#:
#: `mcp.py` publishes this tuple as the row field's `enum` rather than retyping
#: it, on the `FLOW_CLASSES` and `GROUPINGS` precedent: a copy over there would
#: start refusing answers this server sends the first time a fourth value is
#: classified here.
#:
#: A fourth value -- `unknown` -- was considered and rejected as unreachable. An
#: aggregator account exists only because a roster listed it, so it always has a
#: roster basis; an import-only account is fully operator-owned and is honestly
#: `active` until the operator says otherwise. The null on `roster_last_observed`
#: carries "no roster basis" instead, which is a fact the row can state rather
#: than a state it would have to invent.
LIFECYCLE_VALUES: tuple[str, ...] = ("active", "closed", "no_longer_reported")


@dataclass(frozen=True, slots=True)
class AccountLifecycle:
    """Whether ONE account's balance is still a fact about today. FR-9.

    🔴 The failure this describes is the quietest one on this surface. A closed
    card's last recorded balance keeps arriving as a current balance forever:
    nothing throws, the number is well-formed, and the only thing wrong with it
    is that it stopped being true on a date nobody published. A liability that
    was paid off still reads as debt owed; an asset emptied into another enrolled
    account is counted twice.

    🔴 **The dates ride beside the verdict** (AC-12.3), so `no_longer_reported`
    is re-derivable from the row without a second call. The derivation reads both
    nulls as well as the comparison: a null `roster_last_observed` means this
    connection's roster has never been observed, so nothing on it is absent;
    otherwise the account is absent when its own `last_seen_in_roster` is null or
    is behind the recorded observation. The null cases are what carry the upgrade
    window and the newly-vanished account, so they are stated rather than left to
    the comparison. That is the `silence_ratio` ruling applied again: a number
    lets a reader see a borderline case, and a bare flag is what destroys that.

    🔴 **`roster_observed_empty` rides the record and NOT the wire.** It is the
    connection-level fact beside the account-level one -- this account's
    connection was read successfully and listed no account at all -- and it is
    what tells fourteen closures apart from a feed that returns success with no
    rows. It reaches a consumer as the warning of that name rather than as a row
    field, because it is a property of the connection and the row is about the
    account; `to_wire` therefore does not carry it, and neither does
    `connection_id`, which is here so the warning can name what it is about.
    """

    account_id: int
    lifecycle: str
    closed_date: CalendarDate | None
    last_seen_in_roster: CalendarDate | None
    roster_last_observed: CalendarDate | None
    connection_id: int | None
    roster_observed_empty: bool

    @property
    def active(self) -> bool:
        """Whether this account's balance and silence still describe today.

        A property rather than a comparison written at each site, for the reason
        `AccountCoverage.uncovered` is one: "counts as non-active" is a rule, and
        a rule spelled four times is a rule that stops agreeing with itself.
        """
        return self.lifecycle == "active"

    def to_wire(self) -> dict[str, Any]:
        """The four fields every account row carries, in every tool that carries them."""
        return {
            "lifecycle": self.lifecycle,
            "closed_date": iso_or_none(self.closed_date),
            "last_seen_in_roster": iso_or_none(self.last_seen_in_roster),
            "roster_last_observed": iso_or_none(self.roster_last_observed),
        }


def _account_lifecycle(conn: SAConnection) -> dict[int, AccountLifecycle]:
    """The per-account lifecycle facts, for EVERY account, computed once.

    🔴 One producer with two readers, exactly as `_account_coverage` is, and for
    the recorded reason rather than for tidiness: `list_accounts` needs these
    facts so an agent that never thought to call the verification surface still
    learns a balance is frozen (AC-12.1), and `get_coverage_report` needs the
    same facts to tell a retired account's silence from a hole (AC-12.7). Built
    twice they can disagree, and a verification surface that contradicts the
    analysis surface is worse than one that is absent.

    🔴 **`no_longer_reported` is derived here and never stored.** Storing it
    would make it a claim this system had written down, and the next replay of
    the archive could contradict it; derived, it is a statement about the
    observations the store currently holds, which is the only thing that is
    actually known.

    🔴 **Absence is measured against the RECORDED observation** --
    `connections.roster_observed_date`, written by the accounts deriver every
    time a roster is read -- and never against a maximum derived over the
    connection's own accounts. The two dates say different things:
    `roster_observed_date` says *we looked*, `accounts.last_seen_date` says *and
    this is what we found*. A derived maximum collapses them, and a collapsed
    pair cannot express "the roster was observed and this account was not in it"
    at any roster size, because a maximum over the accounts that were listed
    moves with them.

    **AC-12.5's clauses are each a separate fact rather than a consequence of
    one arithmetic:**

    * a connection whose roster could not be fetched records no observation, so
      there is nothing for its accounts to be behind and nothing is absent;
    * a connection whose roster came back EMPTY did record one, so every account
      on it is behind it and every one is marked absent -- and the connection
      raises `roster_observed_empty` beside them, because the account-level
      truth and the connection-level anomaly are two facts and publishing only
      one of them is what a single channel forced;
    * an account with no connection (the FR-7 import path) has no roster to be
      absent from, so both dates are null and the comparison is never reached.

    🔴 **The empty-roster case is derived from the pair, not from a third
    column.** A roster that listed something leaves at least one account whose
    `last_seen_date` equals the observation; a roster that listed nothing leaves
    none. So "no account on this connection matches the recorded observation" is
    exactly "the most recent roster read listed no account we hold", which is
    the fact AC-12.5a names. It reads the CURRENT state rather than a history:
    a later non-empty roster moves the observation and an account with it, and
    the connection stops being empty-rostered, which is correct.
    """
    result = conn.execute(
        select(
            accounts.c.account_id,
            accounts.c.connection_id,
            accounts.c.lifecycle_status,
            accounts.c.closed_date,
            accounts.c.first_seen_date,
            accounts.c.last_seen_date,
        )
    ).all()

    observed = _roster_observations(conn)

    # 🔴 The connection-level anomaly, from the ONE producer that defines it
    # rather than from a second predicate written here. `get_pipeline_health`
    # asks the same question of the same store, and two spellings of "the last
    # roster read listed nothing" can disagree -- which on this surface means a
    # health check calling a connection healthy while the answer beside it says
    # its roster came back empty. Handed the observations already read above, so
    # one answer reads them once.
    empty_rostered = _connections_with_an_empty_roster(conn, observed=observed)

    # 🔴 ONE pass. This read used to run two: the first accumulated the
    # connection's observation as a maximum over its OWN accounts, which is the
    # design `_roster_observations` replaced, and once that went the loop's only
    # remaining job was a dict the second pass re-derived a line later. Two
    # passes over one result set, agreeing by construction and able to drift by
    # edit.
    lifecycle: dict[int, AccountLifecycle] = {}
    for row in result:
        account_id = int(row[0])
        connection_id = None if row[1] is None else int(row[1])
        # 🔴 A null `last_seen_date` means NO ROSTER OBSERVATION IS RECORDED for
        # this account, and it is left null rather than read as anything else.
        #
        # Reading it as `first_seen_date` is the obvious simplification and it is
        # wrong -- recorded here so it is not re-proposed. It looks true
        # -- the account was listed at least once, on that date -- but the
        # comparison below is between accounts on one connection, and first-seen
        # dates legitimately differ across them: a second card, a savings account
        # opened later. So every account on a connection whose siblings arrived
        # later fell behind a maximum assembled out of first-seen dates and was
        # reported CLOSED, for the whole window between migration 003 and that
        # connection's next successful sync -- indefinitely, for a connection
        # that is failing. A fabricated closure on the balance sheet is the exact
        # failure this feature exists to refuse.
        #
        # A backfill cannot rescue it either: the honest backfill value is the
        # connection's last successful roster date, which is the observation
        # AC-12.4 exists because nothing ever recorded.
        #
        # Null is therefore load-bearing, and AC-12.5 is what makes it safe:
        # absence is measured against a successful observation and never against
        # silence. A pre-migration null IS silence.
        last_seen = None if connection_id is None or row[5] is None else calendar_date(row[5])
        roster = None if connection_id is None else observed.get(connection_id)
        # 🔴 AC-12.6: the operator's declaration outranks the derived signal, and
        # it is checked FIRST rather than merged with it. `_OPERATOR_OWNED` keeps
        # the accounts deriver from writing this column, and `accounts` carries no
        # `derivation_version_id`, so `store.rebuild` classifies it as a dimension
        # and never empties it -- the declaration survives a rebuild because of
        # what the table is, not because of a rule someone remembered. The
        # observation still rides the row beside it as evidence.
        if str(row[2]) != "active":
            value = "closed"
        elif roster is None:
            # This connection's roster has never been observed -- migration 004
            # recorded nothing and no sync has run since -- so nothing here can
            # be called absent. AC-12.5's first clause, which says a connection
            # whose roster could not be fetched marks nothing absent, reaching
            # the case where it has not been fetched YET.
            value = "active"
        elif last_seen is None or last_seen < roster:
            # `roster` is not None, so this connection HAS been observed. An
            # account behind that observation, or still carrying none at all,
            # was not in that roster -- which is the steady-state detection this
            # whole item is for, and it keeps working precisely because null was
            # not filled in with a guess.
            value = "no_longer_reported"
        else:
            value = "active"
        lifecycle[account_id] = AccountLifecycle(
            account_id=account_id,
            lifecycle=value,
            # Narrowed rather than passed through, for the reason
            # `_account_coverage` narrows: these come back as plain `date`
            # objects and the wire renderer beside them takes the typed one.
            closed_date=None if row[3] is None else calendar_date(row[3]),
            last_seen_in_roster=last_seen,
            roster_last_observed=roster,
            connection_id=connection_id,
            roster_observed_empty=connection_id in empty_rostered,
        )
    return lifecycle


def _roster_observations(conn: SAConnection) -> dict[int, CalendarDate]:
    """When each connection's roster was last successfully READ. AC-12.4.

    🔴 Read from `connections.roster_observed_date` -- the record the accounts
    deriver writes on every roster read, empty ones included -- and never
    derived from the accounts. A maximum taken over the accounts that were
    listed moves with them, so a connection whose roster lists nothing has no
    observation behind it at all and "we looked, and this account was not there"
    cannot be said. At a one-account connection that is the ordinary case, which
    is why the recorded column is the mechanism rather than a better formula
    over the rows already here (AC-12.4).

    A connection with no recorded observation is ABSENT from this mapping rather
    than carrying a null, so a caller cannot accidentally compare against one:
    absence is measured against a successful observation and never against
    silence.
    """
    return {
        int(connection_id): calendar_date(observed)
        for connection_id, observed in conn.execute(
            select(connections.c.connection_id, connections.c.roster_observed_date)
        ).all()
        if observed is not None
    }


def _roster_observed_empty_caveat(lifecycle: list[AccountLifecycle]) -> list[Caveat]:
    """The warning that tells a broken feed from a household closing its accounts.

    🔴 Request-scoped, on the same test as its neighbours: it fires only where
    THIS request's scope actually holds an account on such a connection. The
    scope handed in is the same non-active list `_not_active_caveat` gets, and
    that is exact rather than convenient -- every account on an empty-rostered
    connection is behind that connection's observation, so every one of them is
    non-active and none of them can be missing from it.

    🔴 **It names the accounts and asserts NO verdict over them.** The scope it
    is handed mixes both non-active values, and `closed` is the operator's own
    declaration while `no_longer_reported` is only the institution having
    stopped listing an account -- the same distinction `_not_active_caveat`
    refuses to flatten, for the same reason. Flattening here would tell an
    operator that an account they closed themselves may be the victim of a
    broken feed. Filtering the closed ones out instead would be worse: a
    connection whose only accounts are operator-closed still had its roster come
    back empty, and that connection would emit a caveat naming nobody.

    🔴 **One caveat per connection, naming the connection and its accounts.**
    The pair is the point: the account rows say each balance froze, and this
    says the roster came back empty, and a reader holding both can tell fourteen
    closures from a feed that returns success with no rows. Holding only the
    first, they cannot -- which is the ambiguity the old whole-roster clause
    avoided by suppressing the account-level truth, at the cost of a one-account
    connection never being able to report its only account absent.
    """
    by_connection: dict[int, list[int]] = {}
    for entry in sorted(lifecycle, key=lambda e: e.account_id):
        if entry.roster_observed_empty and entry.connection_id is not None:
            by_connection.setdefault(entry.connection_id, []).append(entry.account_id)
    return [
        Caveat(
            kind="roster_observed_empty",
            detail=(
                f"connection {connection_id}'s roster was read SUCCESSFULLY and listed no "
                f"accounts at all. Account(s) "
                f"{', '.join(str(account_id) for account_id in account_ids)} sit on it and "
                f"their balances froze on the dates their rows name. 🔴 Read each row's own "
                f"`lifecycle` rather than treating them alike: an account the operator "
                f"declared `closed` is not evidence about this roster, and saying otherwise "
                f"would ask them to re-confirm a closure they made themselves. "
                f"Two very different things produce this and the payload cannot tell them "
                f"apart: the operator de-selected every account from sharing, or the feed "
                f"broke in a way that returns success. Do NOT report it as accounts having "
                f"closed -- name the connection, say the balances beside it are frozen, and "
                f"ask the operator which it was"
            ),
            connection_id=connection_id,
        )
        for connection_id, account_ids in sorted(by_connection.items())
    ]


def _roster_observed_empty_findings(rows: list[dict[str, Any]], empty: set[int]) -> list[Caveat]:
    """The same anomaly on the verification surface, as a finding about a CONNECTION.

    🔴 **A health check's own envelope is not a request scope**, so this is not
    the request-scoped emitter reused: it fires for a named connection whichever
    accounts a caller happens to be asking about, and it fires for a connection
    holding no accounts at all -- the shape the request-scoped one can never
    reach, because there is no account to bring it into scope. An empty roster
    IS a connection-level anomaly, and this is the surface those live on.

    Carries `institution` as well as `connection_id`, the way this surface's
    sign-convention findings do: an operator with ten institutions cannot act on
    a bare connection number.
    """
    return [
        Caveat(
            kind="roster_observed_empty",
            detail=(
                f"{row['institution']}'s roster was read SUCCESSFULLY and listed no accounts "
                f"at all. That is not a failed fetch and it is not reported as one -- but it "
                f"is equally consistent with the operator having de-selected every account "
                f"from sharing and with a feed that broke and still returns success. Every "
                f"account on this connection has its balance frozen as of the date its row "
                f"names. 🔴 Ask the OPERATOR which of the two it is -- de-selecting accounts "
                f"from sharing is done in the aggregator's link flow and the institution has "
                f"no knowledge of it, so only they can say. The institution is worth calling "
                f"only if they confirm they de-selected nothing"
            ),
            connection_id=int(row["connection_id"]),
            institution=row["institution"],
        )
        for row in rows
        if int(row["connection_id"]) in empty
    ]


def _connections_with_an_empty_roster(
    conn: SAConnection, *, observed: dict[int, CalendarDate] | None = None
) -> set[int]:
    """Connections whose most recent recorded roster read listed no account at all.

    🔴 **One producer, two readers**, exactly as `_account_lifecycle` is: the
    answer surface asks this per account and `get_pipeline_health` asks it per
    connection, and two spellings of "the last roster read listed nothing" can
    disagree -- which here means a health check reporting a connection healthy
    while the answer beside it reports its roster empty.

    🔴 **Keyed on the connection, not on the accounts**, which is what lets it
    see the shape the per-account reader never can: a connection whose roster
    has come back empty since the very first read holds no account, so no
    request scope can contain one. A health check blind to the emptiest
    connection would be quietest exactly where it matters.

    The predicate is one comparison. A roster read that listed something leaves
    at least one account whose `last_seen_date` equals the observation; one that
    listed nothing leaves none. Same-day granularity is inherited from both
    sides being calendar dates, which is the point of the column's type: a full
    roster and an empty one on the same day read as one observation.

    `observed` is the parameter that makes "one production per answer"
    expressible, as `_coverage`'s `lifecycle` is: a caller that has already read
    the observations hands them over rather than making the reader -- which is
    autocommit, and releases its snapshot per statement -- take a second one
    that could differ.

    🔴 **A retired connection is excluded, because the condition could never
    end for one.** An empty roster is itself a reason to retire and re-enroll,
    and once retired there is no later non-empty read to clear it -- so every
    answer would carry the caveat forever, and the health surface would keep
    asking the operator to pursue a connection the product itself records as
    removed at the aggregator. That is the "true and useless" degradation the
    two warning tuples in `envelope.py` exist to prevent, and
    `_connection_caveats` already sets this repo's answer.
    """
    observed = _roster_observations(conn) if observed is None else observed
    if not observed:
        return set()
    live = {
        int(connection_id)
        for (connection_id,) in conn.execute(
            select(connections.c.connection_id).where(connections.c.retired_at.is_(None))
        ).all()
    }
    observed = {cid: date for cid, date in observed.items() if cid in live}
    if not observed:
        return set()
    matched = {
        int(connection_id)
        for connection_id, last_seen in conn.execute(
            select(accounts.c.connection_id, accounts.c.last_seen_date).where(
                accounts.c.connection_id.is_not(None)
            )
        ).all()
        if last_seen is not None and observed.get(int(connection_id)) == calendar_date(last_seen)
    }
    return {connection_id for connection_id in observed if connection_id not in matched}


#: What a non-active account means for an answer over BALANCES: the figure froze,
#: and any total includes it and states what it contributed.
_BALANCES_FROZE = (
    "Their balances froze on the date each row names and are not facts about today. Any total "
    "over balances INCLUDES them on purpose -- `coverage.accounts_not_active` and "
    "`coverage.not_active_balance_minor_units` are what they contributed, so quote that "
    "magnitude beside the total rather than presenting the total alone"
)


def _not_active_caveat(
    lifecycle: list[AccountLifecycle], *, consequence: str = _BALANCES_FROZE
) -> list[Caveat]:
    """The warning that names the accounts whose balances stopped being facts.

    🔴 Request-scoped, and deliberately not connection-scoped even though "this
    account is closed" is standing state of the store. `api-contract.md` records
    the defect a fifth always-on kind would reproduce: the `gapped` notice
    arrived character-for-character identical on four unrelated questions, true
    and useless for telling a caller whether THIS answer was the degraded one.
    So it fires only where this request's scope actually holds such an account --
    the same rule, and the same two call sites, as `_uncovered_caveat`.

    Names the ids and splits them by which value they carry, because the two
    mean different things to a reader: `closed` is the operator's own
    declaration, and `no_longer_reported` is only the institution having stopped
    listing the account, which is equally consistent with de-selection from
    sharing. A caller told "some accounts are inactive" can act on neither.

    `consequence` says what the freeze means for THIS answer's figures: an answer
    over balances includes such accounts in its totals and says what they
    contributed, one over positions says the positions froze and that its
    `totals` count and value them, and one over the balance series names what
    stopped counting.
    """
    if not lifecycle:
        return []

    def ids(value: str) -> str:
        return ", ".join(
            str(entry.account_id)
            for entry in sorted(lifecycle, key=lambda e: e.account_id)
            if entry.lifecycle == value
        )

    declared = ids("closed")
    unreported = ids("no_longer_reported")
    parts = []
    if declared:
        parts.append(f"account(s) {declared} are declared closed by the operator")
    if unreported:
        parts.append(
            f"account(s) {unreported} are no longer listed by their institution's most recent "
            f"successful roster observation, which is consistent with closure and equally "
            f"consistent with de-selection from sharing"
        )
    return [
        Caveat(
            kind="account_no_longer_active",
            detail="; ".join(parts) + ". " + consequence,
        )
    ]


def _uncovered_caveat(uncovered: list[AccountCoverage], *, listing: bool) -> list[Caveat]:
    """The warning that names the accounts an answer could not have data for.

    🔴 Request-scoped: it fires only when THIS request's scope holds an
    uncovered account. A kind riding every response equally is the defect
    `CONNECTION_SCOPED_KINDS` records above -- the `gapped` notice arrived
    character-for-character identical on four unrelated questions, true and
    useless for telling a caller whether this answer was the degraded one.

    Names the ids, because "some accounts have no data" is a warning nobody can
    act on and the caller's next move is to ask about a different account.

    `listing` says which question the scope asked, and it has no default because
    every caller must choose. A listing passes the accounts with nothing in any
    feed (`AccountCoverage.no_data_in_any_feed`); a tool answering from the
    transactions feed passes the accounts with no transaction
    (`AccountCoverage.uncovered`), and its detail says where an investment
    account's activity is recorded instead.
    """
    if not uncovered:
        return []
    ids = ", ".join(
        str(coverage.account_id) for coverage in sorted(uncovered, key=lambda c: c.account_id)
    )
    detail = (
        f"nothing has ever been recorded for account(s) {ids} in any feed -- no transaction, "
        f"no investment trade and no captured position -- so an empty or absent result for "
        f"them means DATA NOT PRESENT, never no activity"
        if listing
        else f"no transaction has ever been recorded for account(s) {ids}; an empty or "
        f"absent result for them means DATA NOT PRESENT, never no activity. This counts "
        f"the TRANSACTIONS feed alone -- an investment account's trades and positions are "
        f"recorded apart from it: `investment_transaction_count` and `holdings_as_of` on "
        f"`list_accounts` say what the store holds for it, and `list_holdings` serves its "
        f"positions"
    )
    return [Caveat(kind="accounts_without_coverage", detail=detail)]


def _unmatched_transfer_caveat(
    conn: SAConnection, *, since: date | None, until: date | None
) -> list[Caveat]:
    """The notice that the classifier FELL BACK rather than concluded.

    A transfer-shaped row with no counterparty leg in this store counts as money
    leaving the household. That is the conservative direction and it is usually
    right -- a payment to a person and rent to a landlord are both
    transfer-shaped and both gone. But it is a JUDGEMENT, and the other
    explanation is ordinary: the counterparty account exists and the operator has
    simply not enrolled it.

    🔴 An ATM withdrawal is NOT among these. It never becomes a candidate, so it
    is never a fallback -- counting it here would tell a reader the classifier
    was unsure about a row it was certain of.

    🔴 So the count rides the answer. A judgement made silently over hundreds of
    rows is one nobody audits, and the operator's move -- enrol the other side,
    or accept the figure -- depends on knowing it was made at all.

    `partial`, because what is unknown is real: whether those rows left the
    household is not established, only assumed in the direction that overstates
    spending rather than hiding it.
    """
    unmatched = transfers.unmatched_transfer_shaped(
        conn, transfers.TRANSFER_SHAPED_DETAILED, since=since, until=until
    )
    if not unmatched:
        return []
    return [
        Caveat(
            kind="partial",
            detail=(
                f"{unmatched} transfer-shaped row(s) in this window have no matching leg on "
                f"any account this store holds, so they are counted as money LEAVING the "
                f"household. That is the "
                f"conservative reading and it is not established: the counterparty may simply "
                f"be an account nobody enrolled. Enrol the other side to have them classified "
                f"as transfers instead"
            ),
        )
    ]


def _superseded_caveat(
    spans: Sequence[SupersededSpan],
    *,
    account_id: int | None,
    since: date | None,
    until: date | None,
) -> list[Caveat]:
    """The notice that this answer counted one lineage where the store holds two.

    🔴 **A silent correct total and a silent wrong total look identical to
    whoever reads them; only this tells them apart.** The exclusion is the whole
    reason the figure is right, so an answer that applied it and did not say so
    would be asking to be trusted for a reason it kept to itself.

    Names the account and the range each older generation no longer answers for,
    because the operator's next move is to look at that account over those
    dates -- and because the rows are still there. Nothing was deleted; a caller
    that wants them can ask for exactly the excluded set.

    🔴 **Says "generation", not "connection".** A re-link that converges its
    connection in place keeps one `connection_id` throughout and still re-issues
    every id behind it, so naming the connection would make this warning assert
    something untrue of the case it most often fires on.

    Rides `rule-applied`: rows excluded from this aggregate on purpose.

    🔴 **The disclosure is scoped to the request; the exclusion is not.** The
    caller filters with every span in the store, because narrowing what is
    COMPARED would leave a re-issued account looking unsuperseded. What is NAMED is
    only a span on the request's account whose range meets the requested window,
    since a span outside either excluded nothing from this answer. Naming one
    anyway would make a request-scoped kind ride answers it says nothing about,
    and a reader would learn to ignore it.
    """
    relevant = sorted(
        (
            span
            for span in spans
            if (account_id is None or span.account_id == account_id)
            and (since is None or span.end >= since)
            and (until is None or span.start <= until)
        ),
        key=lambda s: (s.account_id, s.start),
    )
    if not relevant:
        return []
    named = ", ".join(
        f"{span.account_id} ({span.start.isoformat()}..{span.end.isoformat()})" for span in relevant
    )
    return [
        Caveat(
            kind="rule-applied",
            detail=(
                f"account(s) {named} carry a SUPERSEDED generation of history over those "
                f"dates, because the institution was linked again and the aggregator re-issued "
                f"every transaction id -- and, where it also re-issued the account ids, under a "
                f"second account row describing the same real account. The NEWEST generation "
                f"answers for the overlap and the older one's rows are excluded ON PURPOSE -- "
                f"counting both would report that spending twice. Nothing was deleted: the "
                f"excluded rows are still stored and still reachable by asking about them "
                f"directly"
            ),
        )
    ]


def _window_coverage_caveat(coverage: list[AccountCoverage], since: date | None) -> list[Caveat]:
    """The warning an AGGREGATE owes about the accounts it could not cover.

    🔴 **`coverage.earliest_transaction` is one `min()` over every account**, so
    a single long-history account makes the whole store look well covered. A
    window over an account whose own data starts inside it then returns $0 for
    the uncovered months with NO warning at all -- absent-read-as-zero arriving
    through the one number a caller is most likely to trust as a coverage check.

    Per account, and only for accounts this window actually reaches past:

    * data starting materially after the grant is a real beginning, and a zero
      before it is a TRUE zero that may be reported as one;
    * data starting at the grant boundary was cut, and everything earlier is
      absent -- so a zero must not be reported for it;
    * a connection whose grant has not been measured yet is NEITHER, and says so.

    Rides the existing `accounts_without_coverage` kind. `api-contract.md` closes
    the vocabulary and this does not open it: the kind already means *an account
    in scope could not have data for what was asked*, and what changes is that
    aggregates emit it where only the row surfaces used to.

    Names WHICH accounts and from WHICH date each is covered. A count alone is
    not actionable -- the caller's next move is to look at the specific account,
    and with a store-wide minimum there was nothing to look at.
    """
    # 🔴 The REQUESTED start, not the effective one. `resolve_window` clamps
    # `since` up to the store's earliest transaction, so an effective window can
    # never begin before coverage -- comparing against it would report every
    # request as comfortably inside, which is the reassuring version of this bug
    # rather than a fix for it. And the clamp is store-WIDE, which is the whole
    # reason a per-account check is needed: a window can sit inside the store's
    # coverage and still reach past one account's own start.
    if since is None:
        return []
    asked_from = calendar_date(since)
    cut: list[str] = []
    for entry in sorted(coverage, key=lambda c: c.account_id):
        # Already named, in full, by `_uncovered_caveat`. Saying it twice in two
        # kinds would have a caller reconcile two lists describing one set of
        # accounts.
        if entry.uncovered:
            continue
        if (
            entry.truncated_by_the_grant
            and entry.history_starts is not None
            and asked_from < entry.history_starts
        ):
            cut.append(f"{entry.account_id} (from {entry.history_starts.isoformat()})")
    # 🔴 The UNMEASURED case is deliberately not emitted here, though it is the
    # third state this comparison has. A connection whose granted window has not
    # been measured yet already raises `partial` from `_pipeline_warnings`, once
    # per connection, saying exactly that -- and it is the ordinary state in the
    # first hours of a real connection, so emitting it per ACCOUNT as well would
    # put a second notice about one fact on nearly every answer. Two kinds
    # describing one condition is how a caller ends up reconciling two lists,
    # and an always-present notice is the "true and useless" failure the two
    # warning tuples exist to prevent. `unmeasured` stays on the row, where a
    # caller can read it per account without being told twice.
    if not cut:
        return []
    return [
        Caveat(
            kind="accounts_without_coverage",
            detail=(
                f"this window reaches back past where account(s) {', '.join(cut)} have any "
                f"data, and their history was TRUNCATED BY THE GRANT rather than beginning "
                f"there -- so the earlier part of the window is absent for them, never zero"
            ),
        )
    ]


@dataclass(frozen=True, slots=True)
class UndenominableAccount:
    """One account whose amounts cannot enter a total in minor units.

    Two states, one consequence. `currency is None` means the aggregator has
    never stated this account's unit; a currency present but unknown to
    `store/types.py` means the unit is named and its minor digits are not, so no
    amount in it can be expressed exactly. Either way an amount on this account
    cannot be added to a figure in a unit that IS known -- the arithmetic would
    succeed and the result would mean nothing -- so its rows are left out and the
    answer says which account and why.
    """

    account_id: int
    currency: str | None


def _undenominable_accounts(conn: SAConnection) -> list[UndenominableAccount]:
    """The accounts a minor-units aggregate must leave out.

    🔴 **Read from `accounts`, not from the rows an answer returned.** An
    account in an unknown unit typically has no derivable rows at all -- the
    deriver refuses each one it cannot express -- so a scan of the returned rows
    would find nothing to exclude and report nothing excluded, which is the
    silence this disclosure exists to break. Scanning the accounts is what makes
    the caveat fire for an account whose every row was refused.
    """
    return [
        UndenominableAccount(account_id=int(account_id), currency=currency)
        for account_id, currency in conn.execute(
            select(accounts.c.account_id, accounts.c.currency)
        ).all()
        if not has_minor_digits(currency)
    ]


def _undenominable_caveat(excluded: list[UndenominableAccount]) -> list[Caveat]:
    """🔴 The disclosure that makes the exclusion honest rather than silent.

    `rule-applied` means rows were left out of this aggregate ON PURPOSE, and
    `detail` names which and why. It names the account ids rather than a count,
    because the reader's next move is to go and look at that account -- "some
    rows were excluded" is a warning nobody can act on.

    Emitted only where an aggregate actually holds such an account, so its
    absence says the figures cover every account the store has.
    """
    if not excluded:
        return []

    def ids(entries: list[UndenominableAccount], label: bool) -> str:
        return ", ".join(
            f"{entry.account_id} ({entry.currency})" if label else str(entry.account_id)
            for entry in sorted(entries, key=lambda e: e.account_id)
        )

    unstated = [entry for entry in excluded if entry.currency is None]
    unknown = [entry for entry in excluded if entry.currency is not None]
    parts = []
    if unstated:
        parts.append(
            f"account(s) {ids(unstated, label=False)} are in no currency this datastore knows, "
            f"because the aggregator has never stated one for them"
        )
    if unknown:
        parts.append(
            f"account(s) {ids(unknown, label=True)} are in a currency whose minor unit this "
            f"build does not know, so no amount on them can be expressed exactly"
        )
    return [
        Caveat(
            kind="rule-applied",
            detail=(
                "; ".join(parts) + ". Their rows are excluded from every figure in this "
                "answer ON PURPOSE -- adding an amount whose unit or scale is unknown to a "
                "total in a unit that is known would produce a figure that means nothing. "
                "Quote this beside the totals, and ask about the named account(s) directly "
                "to see what is recorded for them"
            ),
        )
    ]


def _covered_rows(
    conn: SAConnection, *, since: date, until: date, spans: Sequence[SupersededSpan] = ()
) -> int:
    """How many transactions lie inside the window this answer actually covered.

    🔴 Store-wide, never narrowed by `account_id`. Per-account coverage is a
    different fact with a different shape and it rides the account ROW -- see
    `AccountCoverage` -- so narrowing this figure would leave a caller with two
    fields that disagree about what "coverage" counts.

    Counted over the EFFECTIVE bounds, which is what makes it a coverage fact
    rather than a restatement of `matching`: it answers "how much data does this
    window hold", against which a caller can read the account-scoped `matching`
    beside it. The effective bounds select the same rows the requested ones would
    -- the clamp only ever removes dates the store has no rows for -- so the
    choice costs nothing and ties the number to the window the answer names.

    Composed from `_transaction_filters`, like every other reader of this table:
    the promise that a filter added later reaches every reader is worth nothing
    if a reader rebuilds the predicates by hand. It passes `after=None` because
    this is a fact about the WINDOW rather than about the page — a figure that
    shrank as a caller paged would describe the walk instead of the store, and a
    caller comparing it against `matching` would read the difference as data
    going missing.

    🔴 **Takes two `date`s, never the `Window`.** A window covering nothing has
    null bounds, and SQLAlchemy's comparison operators accept `Any` -- so passing
    the window in would let a `None` bound reach the predicate with mypy raising
    nothing, and the guard against it would rest on a comment. Demanding `date`
    here moves that guard into the signature: the caller cannot omit the null
    check, because omitting it is a type error rather than a runtime surprise.
    The empty case is answered at the call site, where the zero belongs and where
    the caveat explaining it is already being assembled.
    """
    return int(
        conn.execute(
            select(func.count())
            .select_from(transactions)
            .where(
                *_transaction_filters(
                    since=since, until=until, account_id=None, after=None, spans=spans
                )
            )
        ).scalar_one()
    )


def _answer(
    config: Config,
    conn: SAConnection,
    rows: list[dict[str, Any]],
    *,
    requested_window: tuple[date | None, date | None] | None,
    spans: Sequence[SupersededSpan] = (),
    truncation: Truncation | None,
    totals: list[dict[str, Any]] | None = None,
    extra_caveats: list[Caveat] | None = None,
    lifecycle: dict[int, AccountLifecycle] | None = None,
    window_series: WindowSeries = "transactions",
) -> Answer:
    """One answer, and the one place a window is reconciled against coverage.

    `window_series` says which series a windowed answer read. 🔴 A balance window
    is clamped to the days balances were captured, never to the transactions'
    span, and carries no `transactions_in_effective_window`: that sibling counts
    transactions, and beside a balance series it would read as a count of the
    rows this answer is about.

    `requested_window` is a required keyword with no default: `None` means this
    tool is not windowed, and it has to be written. Resolution lives here rather
    than in each query function because this is where `_coverage` is already
    computed -- one read, one reconciliation, and no second mechanism for a
    later windowed tool to drift from.

    `extra_caveats` carries request-scoped warnings a caller derived from rows
    this function never sees -- the uncovered-account notice is the first. It
    defaults to `None` rather than being required, unlike `requested_window`,
    because "this tool takes no window" is a fact worth forcing a writer to
    state, while "this request raised nothing extra" is the ordinary case.

    `totals` defaults for the same reason and sits on the same side of that line:
    a tool that carries one is the exception here, and the block is computed from
    rows this function never sees. What it must NOT be given is a total this
    function could derive itself -- it is passed in precisely so it is summed
    from the caller's own returned rows and cannot disagree with them.
    """
    now = now_utc()
    coverage = _coverage(conn, lifecycle=lifecycle)
    span: tuple[date | None, date | None] | None = None
    if requested_window is not None and window_series == "balances":
        first, last = conn.execute(
            select(func.min(balances_daily.c.as_of_date), func.max(balances_daily.c.as_of_date))
        ).one()
        span = (
            None if first is None else calendar_date(first),
            None if last is None else calendar_date(last),
        )
    window = (
        None
        if requested_window is None
        else resolve_window(
            since=requested_window[0],
            until=requested_window[1],
            coverage=coverage,
            as_of=now,
            span=span,
        )
    )
    covered = None if window is None else window.covered_bounds()
    if window is not None and window_series == "transactions":
        # 🔴 A SIBLING, never a narrowing of `coverage["transactions"]`, which
        # keeps its store-wide meaning. `api-contract.md` forbids repurposing a
        # field in red for the reason that bites here: a consumer still reading
        # the old key would get a wrong number rather than an error, which is
        # this work cycle's own defect introduced by its own fix.
        coverage = {
            **coverage,
            # A window covering nothing counts nothing, and that zero is not a
            # silent one: the caveat that made the bounds null is already in
            # `window.caveats` and rides the warnings below.
            "transactions_in_effective_window": (
                0
                if covered is None
                else _covered_rows(conn, since=covered[0], until=covered[1], spans=spans)
            ),
        }
    return Answer(
        rows=rows,
        # 🔴 Connection-scoped warnings first, then this request's own. A
        # consumer reading top-down meets the standing state of the pipeline
        # before the thing that is specific to what they just asked.
        warnings=(
            _pipeline_warnings(conn, now, window)
            + ([] if window is None else window.caveats)
            + ([] if truncation is None else truncation.caveats)
            + (extra_caveats or [])
        ),
        environment=config.environment,
        as_of=now,
        effective_window=window,
        truncation=truncation,
        totals=totals,
        coverage=coverage,
    )


def _unusable(
    config: Config,
    problem: str,
    *,
    requested_window: tuple[date | None, date | None] | None,
    truncation: Truncation | None,
    totals: list[dict[str, Any]] | None = None,
    window_series: WindowSeries = "transactions",
) -> Answer:
    """🔴 An answer about a datastore that cannot be read. AC-ARCH.3.

    Zero rows and a warning, never an exception: the requirement is that the
    server reports this state rather than crashing, and a tool that raised would
    reach the client as a failed call rather than as an answer they can read.
    The coverage block is present and zero so a consumer sees the shape it
    expects, and the warning is what tells them the zeroes mean "nothing to read"
    rather than "nothing happened".
    """
    return Answer(
        rows=[],
        # 🔴 A windowed tool still reports a window here, with null effective
        # bounds. The contract fixes ABSENCE of the key as "this tool takes no
        # window", so omitting it on `query_transactions` would say something
        # false about the tool rather than about the store -- and a consumer
        # branching on the key's presence would take the wrong branch precisely
        # when the datastore is unreadable. Nulls say "your window and this
        # store do not overlap", which is true of a store that cannot be read.
        effective_window=(
            None
            if requested_window is None
            else Window(
                requested_since=requested_window[0],
                requested_until=requested_window[1],
                effective_since=None,
                effective_until=None,
                caveats=[],
            )
        ),
        # 🔴 Present with zeroes for a capped tool, matching what `coverage`
        # does two fields down and for the same reason: the consumer sees the
        # shape it expects, and the `partial` warning is what tells them the
        # zeroes mean "nothing could be read". Every number here is true --
        # nothing was returned, nothing was readable to match, and the answer was
        # not truncated. It is empty for a reason the warning states.
        truncation=truncation,
        # 🔴 An empty LIST for a tool that carries totals, never `None` and never
        # omitted -- the same rule the coverage zeroes below follow, for the same
        # reason. Dropping the key when the store is unreadable would move the
        # wire shape at exactly the moment a consumer is trying to work out what
        # went wrong, and one branching on the key would conclude this tool has
        # no totals rather than that there was nothing to total.
        totals=totals,
        warnings=[
            Caveat(
                kind="partial",
                detail=(
                    f"the {config.environment} datastore is not readable ({problem}), so this "
                    f"answer is empty because nothing could be read — not because there is "
                    f"nothing to report. {remedy_for(DatastoreProblem.MISSING)}"
                ),
            )
        ],
        environment=config.environment,
        as_of=now_utc(),
        coverage={
            "connections": 0,
            "accounts": 0,
            # 🔴 Present and zero for the reason every other figure here is:
            # AC-12.8 fixes these two as always-present, and a store that cannot
            # be read is precisely when a consumer branching on a key's presence
            # would take the wrong branch. The empty list is the honest zero for
            # a per-currency figure -- no currency held anything, because nothing
            # was readable, and the `partial` warning above is what says so.
            "accounts_not_active": 0,
            "not_active_balance_minor_units": [],
            "transactions": 0,
            "earliest_transaction": None,
            "latest_transaction": None,
            # 🔴 Keyed off the same condition that decides `effective_window`
            # above, so the two cannot disagree about whether this answer is
            # windowed. A windowed answer that carried `effective_window` but
            # dropped this sibling would move the wire shape precisely when the
            # store is unreadable, and a consumer branching on the key would
            # take the "unwindowed tool" branch for a tool that has a window.
            # Only on a window over transactions, which is what the count counts.
            **(
                {"transactions_in_effective_window": 0}
                if requested_window is not None and window_series == "transactions"
                else {}
            ),
        },
    )


class DatastoreUnservableError(RuntimeError):
    """This build cannot serve this datastore, and data is sitting in it.

    🔴 A refusal rather than an answer, and the distinction is the whole point.
    `api-contract.md` § Hard errors: *a schema version the process does not
    recognize is a hard error, including for the reader -- it answers
    `get_pipeline_health` with a refusal rather than serving queries, because a
    reader running older code against a migrated schema returns plausible,
    structurally valid, wrong answers.* `architecture.md` § Direction states the
    same rule as a norm, with the same why: refusing is recoverable, and a wrong
    number that looks right is not.

    Measured before this existed: a store holding 14 accounts and 388
    transactions, at a schema version this build does not serve, answered every
    tool with zeroed coverage on the SUCCESS path. An agent that does not read
    `warnings` reported that the household owned nothing.

    A datastore that is simply **not there** is the one unhealthy state that does
    NOT raise -- it has no data to misreport, and AC-ARCH.3 asks for it to be
    reported rather than crashed on. `_readable` draws that line.

    Carries the operator's remedy as its message. Raised from the query layer
    because only `inspect` knows which state the store is in; rendered at the MCP
    boundary, which owns how a caller is told.
    """


def _unservable_remedy(status: DatastoreStatus) -> str:
    """What the operator should actually do, chosen by state rather than by prose.

    🔴 Branches on `status.reason`, never on the wording of `status.problem`.
    The sentence is written for a human and is free to be reworded; a remedy
    picked by matching substrings of it would change behaviour silently the next
    time someone improves the English.

    Every branch names a command that is tested against a store actually put into
    that state -- `learnings.md`, *a documented remedy is a claim and is asserted
    like one*. No branch names `store rebuild`: that step is recorded as having
    rolled back on this very shape of store, and a remedy that fails spends the
    operator's trust on the way to failing.

    🔴 **No branch carries the datastore PATH**, and that is a deliberate
    subtraction rather than an oversight. Nothing else on this wire carries it:
    the path reaches the log, where the formatter's redaction applies, and it
    reaches the operator's own terminal through `store status`. A refusal is read
    by whatever model is driving the client, so putting an absolute path -- which
    on a personal machine contains the operator's account name -- into every
    refusal would newly export it from a product whose posture is that nothing
    leaves the machine. The environment names which of the two stores this is,
    which is the only thing the reader needs to act.

    Every branch says **nothing was read and nothing was changed**. The operator's
    first question, on seeing a tool refuse against the store holding their
    finances, is whether the refusal did anything to it.
    """
    if status.reason is DatastoreProblem.SCHEMA_BEHIND_BUILD:
        return (
            f"the {status.environment} datastore is at schema version {status.schema_version} and "
            f"this build serves {status.supported_schema_version}. Nothing was read and nothing "
            f"was changed. {remedy_for(status.reason)}"
        )
    if status.reason is DatastoreProblem.SCHEMA_AHEAD_OF_BUILD:
        # 🔴 The opposite remedy, because migrations are forward-only. Telling
        # this operator to run them would send them to a command that applies
        # nothing, reports nothing to do, and leaves the store exactly as
        # unservable as it was -- which is worse than silence, because it reads
        # as "I tried the fix and the fix is broken". The store is ahead: the
        # thing that is out of date is THIS BUILD.
        return (
            f"the {status.environment} datastore is at schema version {status.schema_version}, "
            f"which is NEWER than the {status.supported_schema_version} this build serves — a "
            f"newer bankmachine has already migrated it. Nothing was read and nothing was "
            f"changed, and migrations cannot run backwards. {remedy_for(status.reason)}"
        )
    if status.reason is DatastoreProblem.NO_SCHEMA_VERSION:
        return (
            f"the {status.environment} datastore records no schema version, so it is "
            f"uninitialized or a migration did not complete. Nothing was read and nothing was "
            f"changed. {remedy_for(status.reason)}"
        )
    if status.reason is DatastoreProblem.KEY_MISSING:
        # 🔴 Deliberately does NOT say `store init`, which refuses to mint a key
        # for a datastore that already exists -- and refuses correctly, because a
        # fresh key would decrypt nothing. `secrets.py` carries the same refusal
        # and the same two remedies; this sentence agrees with it rather than
        # sending the operator to a command that will turn them away.
        return (
            f"the {status.environment} datastore exists, but its key is not in the keychain. A key "
            f"cannot be recovered from the datastore. Nothing was read and nothing was changed. "
            f"{remedy_for(status.reason)}"
        )
    # 🔴 The fallback interpolates NOTHING from `status.problem`, and that is the
    # rule this whole function exists to hold rather than an omission. The
    # problem sentence for this state is `str(exc)` from the store layer, and
    # those exceptions carry absolute paths by design -- "could not probe the
    # writer lock at <path>", "no datastore at <path>". Passing it through would
    # put the operator's account name on the wire through the one branch that
    # looked too generic to check, which is exactly where it got back in once.
    # `_tool_error` performs no redaction; the log does, and that is where the
    # detail belongs.
    # The claim below has to be made true HERE. `query` logs nowhere else, and
    # `cmd_mcp` writes `status.problem` only at startup -- so for the state this
    # branch exists for, a store that goes bad while a long-lived server runs,
    # nothing had ever written the detail the sentence promises.
    logger.warning("%s datastore could not be opened: %s", status.environment, status.problem)
    return (
        f"the {status.environment} datastore exists but could not be opened. Nothing was read and "
        f"nothing was changed. {remedy_for(status.reason)} — it is also in the log, where "
        f"redaction applies"
    )


def _readable(config: Config) -> str | None:
    """The reason a MISSING datastore cannot be read, or None when it can be.

    Checked per call rather than once at startup: a datastore can be created,
    moved or corrupted while a long-lived server is running, and an answer must
    describe the store as it is at the moment of answering.

    🔴 Returns for exactly one unhealthy state and RAISES for every other.
    AC-ARCH.3's carve-out -- *the MCP server starts successfully when the
    datastore is empty or missing, and reports that state through
    `get_pipeline_health` rather than crashing* -- is scoped to a store that is
    not there, which is what its words say and what both go-red cases enforcing
    it anchor to. A store that IS there and cannot be served holds data, and
    answering zero about data that exists is the failure `api-contract.md`
    § Hard errors and `architecture.md` § Direction both forbid.

    This function used to collapse all five of `inspect`'s states into one
    string, which applied the missing-store carve-out to a populated store.
    Ruled 2026-09-09: honour the norm.
    """
    status = inspect(config)
    if status.healthy:
        return None
    if status.reason is DatastoreProblem.MISSING:
        return status.problem or "it is missing or unreadable"
    raise DatastoreUnservableError(_unservable_remedy(status))


def list_accounts(config: Config) -> Answer:
    """Every account, with its latest recorded balance."""
    problem = _readable(config)
    if problem is not None:
        return _unusable(config, problem, requested_window=None, truncation=None)
    with reader_connection(config) as conn:
        latest = (
            select(
                balances_daily.c.account_id,
                func.max(balances_daily.c.as_of_date).label("as_of_date"),
            )
            .group_by(balances_daily.c.account_id)
            .subquery()
        )
        result = conn.execute(
            select(
                accounts.c.account_id,
                institutions.c.name.label("institution"),
                accounts.c.name,
                accounts.c.mask,
                accounts.c.account_type,
                accounts.c.account_subtype,
                accounts.c.balance_class,
                balances_daily.c.current_minor,
                balances_daily.c.currency,
                latest.c.as_of_date,
            )
            .select_from(
                accounts.join(institutions)
                .outerjoin(latest, latest.c.account_id == accounts.c.account_id)
                .outerjoin(
                    balances_daily,
                    (balances_daily.c.account_id == accounts.c.account_id)
                    & (balances_daily.c.as_of_date == latest.c.as_of_date),
                )
            )
            .order_by(institutions.c.name, accounts.c.name)
        ).all()
        coverage = _account_coverage(conn)
        lifecycle = _account_lifecycle(conn)
        rows = [
            {
                "account_id": int(r[0]),
                "institution": r[1],
                "name": r[2],
                "mask": r[3],
                "type": r[4],
                "subtype": r[5],
                "balance_class": r[6],
                # 🔴 Minor units, and the field says so. A consumer that divided
                # by 100 without knowing would be wrong by two orders of
                # magnitude, and the number would still look plausible.
                "current_minor_units": None if r[7] is None else int(r[7]),
                "currency": r[8],
                "balance_as_of": None if r[9] is None else str(r[9]),
                # 🔴 On EVERY row, never behind a parameter. The failure #19
                # describes is an agent that never thought to ask the
                # verification surface, so a caller who has to opt in is a
                # caller who still gets the misleading answer. This is
                # `api-contract.md` § Direction's second norm applied per
                # account: incompleteness rides the answer, not a channel
                # nobody reads.
                **coverage[int(r[0])].to_wire(),
                # 🔴 On EVERY row too, and for the same argument #19 settled for
                # coverage (AC-12.1). The balance two fields up is the field this
                # one qualifies: a caller who has to opt into the lifecycle is a
                # caller who still sums a frozen balance into net worth. The
                # dates ride with the verdict so the verdict is re-derivable
                # from the row (AC-12.3).
                **lifecycle[int(r[0])].to_wire(),
            }
            for r in result
        ]
        # 🔴 A listing describes the ACCOUNT, so it names only the accounts with
        # nothing in any feed; an investment account's trades and positions are
        # data (#107).
        no_data = [c for c in coverage.values() if c.no_data_in_any_feed]
        not_active = [entry for entry in lifecycle.values() if not entry.active]
        return _answer(
            config,
            conn,
            rows,
            requested_window=None,
            truncation=None,
            extra_caveats=(
                _uncovered_caveat(no_data, listing=True)
                + _not_active_caveat(not_active)
                + _roster_observed_empty_caveat(not_active)
            ),
            lifecycle=lifecycle,
        )


class BadGroupingError(ValueError):
    """A `group_by` naming no grouping this tool implements.

    Refused rather than defaulted: silently falling back to `category` would
    answer a different question from the one asked, and the caller would have no
    way to tell -- the same failure mode as a misspelled `since` returning the
    all-time aggregate.
    """


class UnknownAccountError(ValueError):
    """An `account_id` naming no account, refused rather than answered empty.

    🔴 An id that names nothing and an account that was simply quiet in the
    window both select no rows, and `rows: []` cannot tell them apart. "No
    transactions" is an entirely ordinary thing for an account to have, so the
    wrong answer invites no second look -- which makes the typo that produces it
    more dangerous than a typo in an argument NAME, and that one is already
    refused by name.

    Raised from the query layer because only a datastore read knows which ids
    exist; rendered at the MCP boundary, which owns how a caller is told.
    """


class UnknownCategoryError(ValueError):
    """A `category` no transaction in the store carries, refused rather than answered empty.

    🔴 `UnknownAccountError`'s reasoning, one argument over. `TRAVL` and a
    category that was simply quiet in the window both select no rows, and "no
    travel this year" invites no second look. The categories are a small set, so
    the refusal names them -- a correction the caller can make without another
    call.

    Checked against the whole store rather than the window: a category that
    exists and has no rows in THIS window is an ordinary empty answer, not a typo.
    """


def _account_exists(conn: SAConnection, account_id: int) -> bool:
    found = conn.execute(
        select(accounts.c.account_id).where(accounts.c.account_id == account_id).limit(1)
    ).first()
    return found is not None


#: What a transaction with neither an override nor a source category counts under.
UNCATEGORIZED = "UNCATEGORIZED"


def _effective_category() -> ColumnElement[Any]:
    """The category a transaction counts under: the operator's, else the source's.

    🔴 The ONE spelling of it. `money_summary` groups by it and
    `query_transactions` filters by it, and a caller drills from a `group_key`
    into the rows behind it by passing that key back -- so if the two spelled
    the fallback differently, a key one tool reported would select nothing, or
    something else, in the other.
    """
    return func.coalesce(
        transactions.c.category_override,
        transactions.c.source_category_primary,
        UNCATEGORIZED,
    )


def _known_categories(conn: SAConnection) -> list[str]:
    """Every effective category a live transaction carries, for the refusal to name."""
    return sorted(
        str(value)
        for value in conn.execute(
            select(_effective_category()).where(transactions.c.removed_at.is_(None)).distinct()
        ).scalars()
    )


def _transaction_filters(
    *,
    since: date | None,
    until: date | None,
    account_id: int | None,
    after: Cursor | None,
    include_removed: bool = False,
    spans: Sequence[SupersededSpan] = (),
    narrowed_by: TransactionFilter = UNFILTERED,
) -> list[Any]:
    """🔴 The predicates of a transaction query, built once for both statements.

    The row query and the count MUST select the same set, and "remember to
    update both" is the enumeration failure this repo has already been bitten
    by: the two would drift apart silently, and the symptom would be a
    `matching` that contradicts the rows beside it -- a precise wrong number,
    which is worse than the vague one this chunk exists to remove. Built here,
    a filter added later reaches both by construction because there is only one
    place to add it.

    🔴 `include_removed` is for the ONE question that has to look at rows every
    answer excludes: an authorisation hold that expired without ever posting
    left the totals by being soft-deleted, and AC-13.4 requires that exit to be
    attributable rather than silent. It defaults to False and every row-and-count
    caller leaves it there, so the pair that must agree still cannot drift; a
    caller asking for removed rows is asking a different question and says so.
    """
    filters: list[Any] = [] if include_removed else [transactions.c.removed_at.is_(None)]
    # 🔴 Declared HERE, with the soft delete, because it is the same KIND of
    # rule: a row this answer must not count, for a reason that is a property of
    # the store rather than of the caller. A re-link re-issues every transaction
    # id, so the granted history arrives again as rows nothing can collide with
    # -- and a total that counted both would report a year of spending twice.
    #
    # Composed into the one shared list rather than added at each aggregate, so
    # a reader added later is covered by construction. That is the promise this
    # function exists to make, and the reader that rebuilt these predicates by
    # hand is the one that silently kept the old date column when the window
    # moved.
    #
    # 🔴 Appended only when there is a span to apply. With none, `counts_once`
    # is a bare `true` -- it constrains nothing, and every reader would carry a
    # `1` in its WHERE that means nothing to anyone reading the SQL. The branch
    # costs no safety: a caller that forgets to pass `spans` gets the default
    # empty tuple and excludes nothing, which is exactly what an unconditional
    # `true` would have done for it.
    if spans:
        filters.append(lineage.counts_once(spans))
    if since is not None:
        filters.append(transactions.c.ledger_date >= since)
    if until is not None:
        filters.append(transactions.c.ledger_date <= until)
    if account_id is not None:
        filters.append(transactions.c.account_id == account_id)
    # 🔴 Only the caller's own row query and its counts pass `narrowed_by`; the
    # coverage readers never do. `coverage.transactions_in_effective_window`
    # counts the WINDOW, and it rides beside `truncation.matching` so a caller
    # can see how much of the window its filter set aside. A filter that reached
    # the coverage count would make the two agree and erase exactly that.
    if narrowed_by.category is not None:
        filters.append(_effective_category() == narrowed_by.category)
    if narrowed_by.min_amount_minor is not None:
        filters.append(transactions.c.amount_minor >= narrowed_by.min_amount_minor)
    if narrowed_by.max_amount_minor is not None:
        filters.append(transactions.c.amount_minor <= narrowed_by.max_amount_minor)
    if after is not None:
        # 🔴 The keyset predicate belongs in the SHARED list, not on the row
        # query alone. `truncation.remaining` is what `truncated` turns on, and
        # a page whose count ignored the cursor would report the whole result
        # set behind every page — so `truncated` would stay true on the last one
        # and a caller paging until it went false would never stop. The
        # whole-request figure a caller QUOTES is taken separately, with this
        # predicate deliberately left off.
        #
        # Spelled as an explicit disjunction rather than as a row-value
        # comparison, because it mirrors the ORDER BY beside it one clause at a
        # time: strictly older, or the same day and further down the tie-break.
        filters.append(
            or_(
                transactions.c.ledger_date < after.ledger_date,
                and_(
                    transactions.c.ledger_date == after.ledger_date,
                    transactions.c.transaction_id < after.transaction_id,
                ),
            )
        )
    return filters


#: How long a transaction may stay `pending` before the hold behind it stops
#: being explicable as an ordinary authorisation. AC-13.5.
#:
#: 🔴 **A declared constant rather than a figure derived from the account's own
#: cadence, and the departure from AC-9.1's precedent is deliberate.** A coverage
#: gap is silence in a feed, so the only non-arbitrary yardstick for it is that
#: feed's own rhythm. A hold's lifetime is a property of the CARD NETWORK that
#: placed it: the same authorisation expires on the same day whether the account
#: it sits on posts twice a day or once a month. Measuring it against cadence
#: would make one hold stranded on a busy card and unremarkable on a quiet one,
#: which states something about the account rather than about the hold.
#:
#: 🔴 **30 days is the OUTER end of the ordinary range, not the typical one.**
#: Most merchants' authorisations release within a few days, but lodging, vehicle
#: rental and fuel routinely hold for weeks -- so a threshold set at the typical
#: value would fire on every hotel stay, which is the ~146-findings-and-no-signal
#: outcome AC-11.1's amendment records and AC-13.5 explicitly cites. At the outer
#: end, a row that is still pending has no ordinary hold lifetime left to explain
#: it.
#:
#: 🔴 **This is a claim about the card network, not a measurement taken here.**
#: No pending row has ever reached this datastore, so there is nothing local to
#: derive it from, and saying so is the honest half of "declared with its
#: derivation". AC-13.8's observation against a real settlement is what would let
#: it be re-derived from data rather than from documented network behaviour.
STRANDED_HOLD_AFTER_DAYS = 30


def _stranded_cutoff(today: CalendarDate) -> CalendarDate:
    """The newest `ledger_date` a still-pending row may carry and not be stranded.

    🔴 One definition of the boundary for two evaluators. The producer below
    tests it in SQL and a caller may test it against rows already in hand; two
    hand-written spellings of "older than the threshold" is how the SQL and the
    Python answers start disagreeing about the same row, and this file already
    carries the rule that two producers of one fact can contradict each other.
    """
    return calendar_date(today - timedelta(days=STRANDED_HOLD_AFTER_DAYS))


@dataclass(frozen=True, slots=True)
class StrandedHold:
    """One row that is still an authorisation hold long after it should have settled."""

    account_id: int
    transaction_id: int
    posted_date: CalendarDate
    #: 🔴 What the threshold is measured on, and what `days_pending` counts
    #: from. Beside `posted_date` rather than instead of it: the wire field
    #: already means the source's own date and a consumer reads it as that.
    ledger_date: CalendarDate
    days_pending: int
    amount_minor: int
    currency: str


def _stranded_holds(conn: SAConnection, *, today: CalendarDate) -> list[StrandedHold]:
    """Every hold in the STORE that is past any ordinary authorisation lifetime. AC-13.5.

    Row-level rather than a count, because "three accounts have stranded holds"
    is a finding nobody can act on: the operator's next move is to look at the
    transaction, so the id has to travel with the number.

    Ordered oldest first, so the caller naming one names the worst.

    🔴 **Takes no caller-supplied predicates, deliberately.** A stranded hold is
    `pending`, is not removed, and is older than the cutoff. None of the three is
    a caller's to choose, so none of them is a caller's to forget -- and the
    soft-delete clause is the one that matters most, because an expired hold is
    RETAINED with `removed_at` set and `pending` left at 1. That retention is what
    the `expired` tally reads, so a query that omits the clause reports every
    long-expired hold as still outstanding and sends the operator after money the
    institution already took back.
    """
    cutoff = _stranded_cutoff(today)
    rows = conn.execute(
        select(
            transactions.c.account_id,
            transactions.c.transaction_id,
            transactions.c.ledger_date,
            transactions.c.amount_minor,
            transactions.c.currency,
            transactions.c.posted_date,
        )
        .select_from(transactions.join(accounts))
        .where(
            transactions.c.pending == 1,
            # 🔴 Declared HERE rather than left to `filters`, because it is a
            # property of the question and not of the caller. `_transaction_filters`
            # takes `include_removed`, so a caller is entitled to ask for removed
            # rows -- but a withdrawn hold is not a stranded one, it is a finished
            # one. An expired hold is retained with `removed_at` set and `pending`
            # left at 1, which is exactly what the `expired` tally reads, so a
            # caller who merely FORGOT this predicate would publish every
            # long-expired hold as stranded and send the operator after money the
            # institution already took back. The guarantee belongs where it cannot
            # be forgotten.
            transactions.c.removed_at.is_(None),
            # 🔴 Measured on `ledger_date`: how long an authorisation has been
            # outstanding is a question about the money, not about delivery. For
            # a row that is still pending the two dates agree today, because the
            # source's date has not moved yet -- so this changes no current
            # answer and stays correct if it ever does.
            #
            # 🔴 A row whose `ledger_date` is null fails this predicate and drops
            # out, which is the same silent exclusion `_unstamped_ledger_date_
            # caveat` discloses on every answer. It is a floor here too: an
            # un-rebuilt store under-reports stranded holds rather than
            # inventing them, and the notice says the rebuild is owed.
            transactions.c.ledger_date < cutoff,
        )
        .order_by(transactions.c.ledger_date, transactions.c.transaction_id)
    ).all()
    return [
        StrandedHold(
            account_id=int(row[0]),
            transaction_id=int(row[1]),
            posted_date=calendar_date(row[5]),
            ledger_date=calendar_date(row[2]),
            days_pending=(today - calendar_date(row[2])).days,
            amount_minor=int(row[3]),
            currency=str(row[4]),
        )
        for row in rows
    ]


@dataclass(frozen=True, slots=True)
class HoldTally:
    """How many rows, and what they come to signed as stored."""

    transactions: int = 0
    net_minor: int = 0


@dataclass(frozen=True, slots=True)
class HoldTransitions:
    """The two ways a hold stops being one, per currency. AC-13.4, AC-13.6.

    🔴 **Both halves exist so a total that MOVED is attributable.** A figure over
    a window holding authorisation holds can change with no new activity
    whatsoever, and it changes in exactly two ways: a hold expires and its whole
    amount leaves the total, or a hold settles and its amount is replaced by the
    settled one. Neither is visible in the rows an answer returns -- an expired
    hold is soft-deleted and therefore excluded by construction, and a settled
    row looks like any other posted row -- so a consumer watching a number drift
    has nothing in the payload to explain it. That is the same defect class as a
    total that does not state its own scope, arriving one step later in time.

    🔴 Per currency, because every aggregate on this surface groups by currency
    and never sums across it.
    """

    expired: dict[str, HoldTally]
    settled: dict[str, HoldTally]

    def currencies(self) -> set[str]:
        return set(self.expired) | set(self.settled)

    def expired_for(self, currency: str) -> HoldTally:
        return self.expired.get(currency, HoldTally())

    def settled_for(self, currency: str) -> HoldTally:
        return self.settled.get(currency, HoldTally())


def _hold_transitions(
    conn: SAConnection,
    *,
    since: date | None,
    until: date | None,
    excluded: list[int],
    spans: Sequence[SupersededSpan] = (),
) -> HoldTransitions:
    """Holds that left this window, and rows in it that settled out of one.

    🔴 **Two reads rather than a projection off the answer's rows, and each for
    its own reason.** The expired half counts rows every other statement here
    excludes, so no arithmetic over the returned rows could reach it. The settled
    half could in principle be summed alongside the aggregate, but asking for it
    as `source_pending_transaction_id IS NOT NULL` is what lets SQLite answer
    from `transactions_pending_link` -- a partial index holding only the rows
    that carry a link, rather than a scan of every row in the window (AC-13.6;
    measured with `EXPLAIN QUERY PLAN` in `tests/test_pending_semantics.py`).

    🔴 These are NOT totals over the answer's rows and must never be summed with
    them. `_flow_class_totals`' identity -- the three classes add to the window's
    outflow -- holds over the returned rows alone; these two describe rows that
    left the answer and rows whose amount arrived by replacement.
    """

    def tally(where: list[Any], extra: list[Any]) -> dict[str, HoldTally]:
        rows = conn.execute(
            select(
                transactions.c.currency,
                func.count(),
                func.coalesce(func.sum(transactions.c.amount_minor), 0),
            )
            .select_from(transactions.join(accounts))
            # 🔴 The same exclusion the aggregate applies, because these two
            # tallies ride the same `totals` block. Leaving it off here would
            # let an account excluded from every figure put a currency ENTRY
            # into the totals it was excluded from -- an exclusion that
            # announced itself and then leaked.
            .where(*where, *extra, accounts.c.account_id.notin_(excluded))
            .group_by(transactions.c.currency)
        ).all()
        return {
            str(row[0]): HoldTally(transactions=int(row[1]), net_minor=int(row[2])) for row in rows
        }

    return HoldTransitions(
        # 🔴 `pending = 1` AND removed, which is precisely "expired without ever
        # posting". A hold that settled is no longer pending -- its own row was
        # updated in place -- so a later removal of the settled row carries
        # `pending = 0` and is an ordinary withdrawal rather than a hold that
        # never became anything.
        expired=tally(
            _transaction_filters(
                since=since,
                until=until,
                account_id=None,
                after=None,
                include_removed=True,
                spans=spans,
            ),
            [transactions.c.removed_at.is_not(None), transactions.c.pending == 1],
        ),
        settled=tally(
            _transaction_filters(
                since=since, until=until, account_id=None, after=None, spans=spans
            ),
            [
                transactions.c.source_pending_transaction_id.is_not(None),
                transactions.c.pending == 0,
            ],
        ),
    )


def _pending_caveat(pending: dict[str, HoldTally]) -> list[Caveat]:
    """The notice that an answer's figures include unsettled holds. AC-13.1.

    🔴 Request-scoped, so its ABSENCE is information too: an answer over rows
    that are all settled carries no such warning, and a consumer can therefore
    read a total with no `includes_pending_rows` beside it as a total of settled
    money. That only works because the disclosure is computed on every answer
    rather than when someone remembers to ask -- the always-present count in
    `totals` is the other half of the same statement, and it is what makes a
    zero distinguishable from a question nobody asked.

    Names the magnitude, not just the count: "some rows are pending" leaves a
    reader unable to tell a $4 coffee hold from a $2,000 hotel authorisation, and
    the whole point is whether the figure beside it can move enough to matter.

    🔴 **Says nothing about STRANDED holds, and that is AC-13.5 being obeyed
    rather than skipped.** The criterion puts a hold past its ordinary lifetime
    on the verification surface, and `api-contract.md` rules that a per-account
    finding belongs on `get_coverage_report`. Naming it here as well cost two
    real defects before it was removed: this caveat counts the rows THIS ANSWER
    drew on while a stranded query is scoped to the whole request, so a truncated
    walk claimed more stranded holds than the page contained -- a precise false
    statement about the payload beside it -- and the analysis path escaped the
    non-active suppression its sibling on the coverage report applies, offering a
    closed account's unclearable hold as a call to action forever. Both followed
    from putting the finding where no criterion asked for it.
    """
    total = sum(tally.transactions for tally in pending.values())
    if total == 0:
        return []
    magnitude = ", ".join(
        f"{tally.net_minor} {currency}" for currency, tally in sorted(pending.items())
    )
    detail = (
        f"{total} of the rows this answer drew on are authorisation holds that have not "
        f"settled, netting {magnitude} in minor units. A hold can settle at a different figure "
        f"or expire without settling at all, so a figure computed from this answer can change "
        f"with no new activity whatsoever"
    )
    return [Caveat(kind="includes_pending_rows", detail=detail)]


def list_transactions(
    config: Config,
    *,
    since: date | None = None,
    until: date | None = None,
    account_id: int | None = None,
    limit: int = 100,
    after: Cursor | None = None,
    narrowed_by: TransactionFilter = UNFILTERED,
) -> Answer:
    """Transactions in a window, newest first. Soft-deleted rows are excluded.

    `after` resumes a paged walk at the row a previous answer's `next_cursor`
    named. It narrows the rows and `truncation.remaining`, so `truncated` reads
    false on the page that exhausts the window and the caller has a terminating
    condition rather than a number to compare — while `truncation.matching`
    stays the count of what the whole request selects and reads the same on
    every page of the walk.

    `narrowed_by` selects by effective category and signed amount (AC-9.6). It
    narrows the rows and both counts, and never the coverage figures beside them.
    """
    problem = _readable(config)
    if problem is not None:
        return _unusable(
            config,
            problem,
            requested_window=(since, until),
            # Zero returned of zero matching: nothing was readable, so nothing
            # matched and nothing was dropped. The key stays present because its
            # absence would say this tool returns everything it finds, and there
            # is no page to resume from because there was no page.
            truncation=Truncation.over(
                counting="transactions", returned=0, remaining=0, matching=0, resume_from=None
            ),
        )
    with reader_connection(config) as conn:
        # 🔴 Ordered AFTER the readability check on purpose: an unreadable store
        # knows nothing about which accounts exist, and "that account does not
        # exist" is a claim about the data rather than about the connection.
        if account_id is not None and not _account_exists(conn, account_id):
            raise UnknownAccountError(
                f"account_id {account_id} does not exist. list_accounts reports the ids that do."
            )
        # Same ordering, same reason: which categories exist is a fact about the
        # data, and an unreadable store has already answered above.
        if narrowed_by.category is not None:
            known = _known_categories(conn)
            if narrowed_by.category not in known:
                raise UnknownCategoryError(
                    f"category {narrowed_by.category!r} is carried by no transaction in this "
                    f"store. The categories it holds: {', '.join(known) or 'none'}"
                )
        spans = lineage.superseded_spans(conn)
        filters = _transaction_filters(
            since=since,
            until=until,
            account_id=account_id,
            after=after,
            spans=spans,
            narrowed_by=narrowed_by,
        )
        # 🔴 Both statements take the SAME from-clause as well as the same
        # filters. The join to `accounts` is part of what selects a row -- an
        # inner join drops a transaction whose account is absent -- so a count
        # taken over the bare table would exceed the rows and report a
        # truncation that never happened.
        source = transactions.join(accounts)
        statement = (
            select(
                transactions.c.transaction_id,
                transactions.c.account_id,
                accounts.c.name.label("account"),
                transactions.c.posted_date,
                transactions.c.description,
                transactions.c.merchant_name,
                transactions.c.amount_minor,
                transactions.c.currency,
                transactions.c.pending,
                transactions.c.source_category_primary,
                transactions.c.category_override,
                # 🔴 Emitted BESIDE `posted_date`, never instead of it, and the
                # rows are ordered on this one. A caller handed rows sorted by a
                # date the payload does not carry cannot check the order it was
                # given, and the two differ exactly where it matters -- a hold
                # authorised in one month and posted in the next.
                transactions.c.ledger_date,
            )
            .select_from(source)
            .where(*filters)
            .order_by(transactions.c.ledger_date.desc(), transactions.c.transaction_id.desc())
            .limit(max(1, min(limit, MAX_ROWS)))
        )
        selected = conn.execute(statement).all()
        rows = [
            {
                "transaction_id": int(r[0]),
                # 🔴 Beside the display name, never instead of it. `account` is
                # the institution's own text and two accounts can carry the same
                # one, so it identifies nothing -- and every follow-up this
                # surface sends a caller on, from `get_coverage_report` to a
                # narrowed `query_transactions`, is keyed on the id.
                "account_id": int(r[1]),
                "account": r[2],
                "date": str(r[3]),
                # 🔴 ADDITIVE, and `date` keeps its meaning. `date` is the
                # source's own posting date and a consumer already reads it as
                # that; silently repointing it at the economic date would change
                # a shipped field's meaning without changing its name, which is
                # the one evolution this contract forbids.
                #
                # 🔴 Null means "this row predates the split and the store has
                # not been rebuilt" -- NOT "committed on the posting date". It
                # is not coalesced away, because the fallback would answer with
                # exactly the number this column exists to stop being wrong, and
                # would do it invisibly.
                "ledger_date": None if r[11] is None else str(r[11]),
                "description": r[4],
                "merchant": r[5],
                "amount_minor_units": int(r[6]),
                "currency": r[7],
                "pending": bool(r[8]),
                "category": r[10] or r[9],
                "category_is_override": r[10] is not None,
            }
            for r in selected
        ]
        remaining = conn.execute(
            select(func.count()).select_from(source).where(*filters)
        ).scalar_one()
        # 🔴 A SECOND count, taken without the keyset predicate, and only when a
        # cursor was passed. `remaining` answers "did this page leave anything
        # behind", which is the caller's loop condition and must fall to the
        # rows in hand on the last page. `matching` answers "how many rows does
        # my request select", which a caller quotes -- and a figure that fell
        # 390, 290, 190, 90 across a walk under that name gave an agent reading
        # the last page a confident "90" for a question about the year, with
        # `coverage.transactions_in_effective_window` beside it still saying
        # 390. An unpaged request asks one question, so it pays for one count.
        matching = (
            remaining
            if after is None
            else conn.execute(
                select(func.count())
                .select_from(source)
                .where(
                    *_transaction_filters(
                        since=since,
                        until=until,
                        account_id=account_id,
                        after=None,
                        spans=spans,
                        narrowed_by=narrowed_by,
                    )
                )
            ).scalar_one()
        )
        # 🔴 Built from the LAST ROW THE STATEMENT RETURNED, in the column types
        # the order clause sorts on -- never re-parsed from the wire dict beside
        # it, whose `date` is already a string. A cursor rebuilt from the
        # rendering of a row is a second description of the position, and the
        # ordering it has to agree with is SQL's.
        resume_from = (
            None
            if not selected
            else Cursor.issued_for(
                ledger_date=selected[-1][11],
                transaction_id=int(selected[-1][0]),
                since=since,
                until=until,
                account_id=account_id,
                narrowed_by=narrowed_by,
            )
        )
        # 🔴 Scoped to the account ASKED ABOUT, not to every empty account in the
        # store. `query_transactions(account_id=9)` returning `[]` is #19's own
        # repro -- "am I paying down my mortgage?" answering "no payments found",
        # honest-looking and false -- and this is the call where the notice has
        # to arrive. An unscoped query says nothing here: its empty window is an
        # ordinary result, and naming nine irrelevant accounts on every page of
        # every walk is the character-for-character noise the connection scope
        # already taught this codebase not to emit.
        uncovered = (
            [c for c in (_account_coverage(conn).get(account_id),) if c is not None and c.uncovered]
            if account_id is not None
            else []
        )
        # 🔴 Same scoping, same reason, on the lifecycle axis (AC-12.1's
        # rationale). An agent asking
        # "what did I spend on this card" about an account the institution
        # stopped reporting is the consumer AC-12.1 names: it never thought to
        # call the verification surface, and the rows it gets back end on the day
        # the account went quiet with nothing saying why. Scoped to the account
        # ASKED ABOUT for the reason directly above -- naming every closed
        # account in the store on every page of every walk is the noise the
        # request scope exists to refuse.
        # One walk, used twice: the scoped caveat below and the envelope's
        # non-active figures, which `_answer` would otherwise derive from a
        # second observation of the same fact.
        lifecycle = _account_lifecycle(conn)
        not_active = (
            [e for e in (lifecycle.get(account_id),) if e is not None and not e.active]
            if account_id is not None
            else []
        )
        # 🔴 Tallied from the rows THIS PAGE returned, not from `matching`.
        # AC-13.1's disclosure is about the rows an answer drew on, and a walk's
        # later page may hold no holds at all -- a caveat counting the whole
        # result set would then say this page mixes in holds it does not
        # contain, which is a precise false statement about the payload beside
        # it.
        pending: dict[str, HoldTally] = {}
        for row in rows:
            if not row["pending"]:
                continue
            currency = str(row["currency"])
            seen = pending.get(currency, HoldTally())
            pending[currency] = HoldTally(
                transactions=seen.transactions + 1,
                net_minor=seen.net_minor + int(row["amount_minor_units"]),
            )
        return _answer(
            config,
            conn,
            rows,
            requested_window=(since, until),
            spans=spans,
            # `returned` is derived from the rows themselves rather than from
            # `limit`, so it cannot claim a count the payload does not contain.
            truncation=Truncation.over(
                counting="transactions",
                returned=len(rows),
                remaining=remaining,
                matching=matching,
                resume_from=resume_from,
            ),
            extra_caveats=(
                _uncovered_caveat(uncovered, listing=False)
                + _superseded_caveat(spans, account_id=account_id, since=since, until=until)
                + _not_active_caveat(not_active)
                + _roster_observed_empty_caveat(not_active)
                + _pending_caveat(pending)
            ),
            lifecycle=lifecycle,
        )


#: How many interior gaps one account reports before the list is cut. A report
#: naming forty holes on one account is one nobody reads, and the operator's
#: move is the same after the first few: go look at that account. The COUNT is
#: not capped -- only the enumeration -- so a caller can still tell a truncated
#: list from a complete one.
MAX_INTERIOR_GAPS_PER_ACCOUNT = 10

#: An interior gap is a silence longer than three of this account's own cycles,
#: AND at least a week. Both conditions, because either alone misfires: the
#: multiple alone flags an ordinary long weekend on an account posting daily
#: (cadence 1, so any four quiet days), and the floor alone flags every normal
#: month on an account that posts monthly. One is noise on the busiest accounts,
#: the other noise on the quietest.
INTERIOR_GAP_CADENCE_MULTIPLE = 3.0
INTERIOR_GAP_MINIMUM_DAYS = 7


def _interior_gaps(dates: Sequence[CalendarDate], cadence: float | None) -> list[dict[str, Any]]:
    """The holes INSIDE an account's history, measured against its own cadence.

    🔴 **Only trailing silence was ever computed.** `days_silent` measures from
    the last transaction to today, so a three-month hole in the middle of a
    history leaves it at 1 and `silence_exceeds_cadence` false -- while
    `money_summary` with `group_by=month` shows a spending collapse that never
    happened. A feed that stopped and restarted is invisible to a measure that
    only looks at the end.

    🔴 **Measured on `posted_date`, never `ledger_date`.** A gap is a question
    about DELIVERY -- did the feed stop -- so it must be measured on the date
    that tracks arrival. On the economic date a settlement could fill a delivery
    gap that really happened, which is the same split `ledger_date` establishes
    read from its other side.

    🔴 **Reported as numbers, not flags**, for the reason `silence_ratio` is: a
    3.1x gap and a 40x gap are not the same finding, and a boolean says they
    are. Each carries its own ratio so a caller can weigh them.

    Cadence is the caller's already-floored divisor, reused unchanged. A second
    notion of cadence in one report is how the report starts contradicting
    itself.
    """
    if cadence is None or len(dates) < 2:
        return []
    threshold = max(cadence, 1.0) * INTERIOR_GAP_CADENCE_MULTIPLE
    gaps: list[dict[str, Any]] = []
    for earlier, later in zip(dates, dates[1:], strict=False):
        days = (later - earlier).days
        if days > threshold and days >= INTERIOR_GAP_MINIMUM_DAYS:
            gaps.append(
                {
                    "from": earlier.isoformat(),
                    "to": later.isoformat(),
                    "days": days,
                    "ratio": round(days / max(cadence, 1.0), 3),
                }
            )
    # Widest first, so a truncated list keeps the gaps worth looking at rather
    # than whichever happened to fall earliest in the history.
    #
    # 🔴 Returns EVERY gap; the cap is applied where the list is emitted, never
    # here. The caller reports the count from this list, so capping inside would
    # make the count agree with the truncated list and the truncation would stop
    # being visible -- an account with forty holes would report ten, which is
    # the undercount this whole report exists to surface.
    gaps.sort(key=lambda gap: (-int(gap["days"]), str(gap["from"])))
    return gaps


def _median_interval(days: list[int]) -> float | None:
    """The middle gap between consecutive transactions, or None when there is none.

    🔴 Median rather than mean, and per account rather than global, because the
    cadence it describes is a claim about THIS account. A salary account posting
    fortnightly and a card posting daily have nothing to say about each other,
    and a mean is dragged by the single long silence the report exists to find --
    the outlier would raise the very threshold meant to catch it.

    Fewer than two transactions means no interval exists at all. That is None
    rather than zero, because zero would read as "posts every day" and make
    every subsequent quiet hour a finding.

    🔴 Zero is a REAL answer here, distinct from that None: an account whose
    transactions cluster on the same dates has a median interval of 0 days, and
    it means "posts more than once a day" rather than "cadence unknown". The
    caller floors the divisor rather than this function flooring the
    measurement, so the number that rides out is the one that was measured.
    """
    if not days:
        return None
    ordered = sorted(days)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2


def _oldest_stranded(holds: Sequence[StrandedHold], *, active: bool) -> dict[str, Any] | None:
    """The worst stranded hold on one account, or null. AC-13.5.

    `_stranded_holds` orders oldest first, so the caller naming one names the
    worst; this reads that order rather than re-deriving it.

    Null rather than absent when there is nothing to report, and null for a
    non-active account whose holds can never settle. The count beside this field
    still reports what was measured, so nothing is concealed -- what is withheld
    is the CALL TO ACTION, which is the one thing a finding on a closed account
    cannot be.
    """
    if not active or not holds:
        return None
    worst = holds[0]
    return {
        "transaction_id": worst.transaction_id,
        "posted_date": iso_or_none(worst.posted_date),
        "ledger_date": iso_or_none(worst.ledger_date),
        "days_pending": worst.days_pending,
        "amount_minor_units": worst.amount_minor,
        "currency": worst.currency,
    }


def coverage_report(config: Config) -> Answer:
    """🔴 The verification surface's second half, per account. AC-9.5, #35.

    `api-contract.md` names `get_pipeline_health` and this tool together as "the
    verification surface, not the analysis surface", existing so an analyst agent
    can establish completeness BEFORE answering -- the product's headline goal
    being "answer it, or say why you should not." Only health shipped, so the
    product made a two-part promise and delivered one part.

    Per account, never per institution (AC-9.5): one institution may hold many
    accounts with different coverage windows, and an institution-level summary
    hides exactly that.

    🔴 **Trailing silence, not an enumeration of gaps between transactions.**
    AC-9.1 specified "gaps > 7 days" and measurement falsified it: every account
    in this store is monthly, so a 7-day rule fires on 100% of CD's and Money
    Market's intervals and yields ~146 findings with no signal. A per-account
    threshold applied to the same enumeration would reproduce that noise in a
    more defensible-looking form. What carries information is how long an account
    has been quiet measured against its own cadence, which is what
    `silence_ratio` reports.

    🔴 Shares `_account_coverage` with `list_accounts` rather than recomputing:
    two producers can disagree, and a verification surface that contradicts the
    analysis surface is worse than one that is absent.
    """
    problem = _readable(config)
    if problem is not None:
        return _unusable(config, problem, requested_window=None, truncation=None)
    with reader_connection(config) as conn:
        coverage = _account_coverage(conn)
        # 🔴 The same producer `list_accounts` reads, for the reason the line
        # above shares one: two producers can disagree, and a verification
        # surface that contradicts the analysis surface is worse than one that
        # is absent (AC-12.7).
        lifecycle = _account_lifecycle(conn)
        # 🔴 Unwindowed, unlike every other caller: this surface answers about
        # the store rather than about a window, and a hold stranded outside
        # whatever window a caller happened to ask about is precisely the one
        # nobody has noticed.
        #
        # The soft-delete exclusion a windowed caller would get from
        # `_transaction_filters` is declared inside `_stranded_holds` instead, so
        # passing no filters here narrows nothing it should not.
        # 🔴 One "today" for this whole report, read ONCE and here, before the
        # first reader of it. Reading the clock again lower down would let a
        # report straddling midnight measure its stranded holds against one day
        # and its coverage rows against the next -- two answers in one payload,
        # differing by a day, with nothing in the payload to explain it.
        today = calendar_date(now_utc().date())
        stranded_by_account: dict[int, list[StrandedHold]] = {}
        for hold in _stranded_holds(conn, today=today):
            stranded_by_account.setdefault(hold.account_id, []).append(hold)
        named = {
            int(account_id): name
            for account_id, name in conn.execute(
                select(accounts.c.account_id, accounts.c.name)
            ).all()
        }
        posted: dict[int, list[CalendarDate]] = {}
        for account_id, posted_date in conn.execute(
            select(transactions.c.account_id, transactions.c.posted_date)
            .where(transactions.c.removed_at.is_(None))
            .order_by(transactions.c.account_id, transactions.c.posted_date)
        ).all():
            if posted_date is not None:
                # Narrowed for the same reason `_account_coverage` narrows: this
                # column comes back a date, and the wire-dict parser beside it
                # takes a string. Silently emptying this map would make every
                # median null and every account look cadence-less.
                posted.setdefault(int(account_id), []).append(calendar_date(posted_date))
        sources: dict[int, dict[str, int]] = {}
        for account_id, source, count in conn.execute(
            select(transactions.c.account_id, transactions.c.source, func.count())
            .where(transactions.c.removed_at.is_(None))
            .group_by(transactions.c.account_id, transactions.c.source)
        ).all():
            sources.setdefault(int(account_id), {})[str(source)] = int(count)

        rows: list[dict[str, Any]] = []
        for account_id in sorted(coverage):
            facts = coverage[account_id]
            dates = posted.get(account_id, [])
            median = _median_interval(
                [(later - earlier).days for earlier, later in zip(dates, dates[1:], strict=False)]
            )
            days_silent = (
                None
                if facts.last_transaction_date is None
                else (today - facts.last_transaction_date).days
            )
            # 🔴 The DIVISOR is floored at one day; the reported median is not.
            # A busy feed -- a card or checking account posting several rows on
            # the same date -- has a median interval of literally 0, and the
            # branch that guarded the division by returning null answered
            # "cadence unknown, silence not exceeded" for it no matter how long
            # the feed had been dead. That is the account class MOST likely to
            # stop syncing, answered `false` by the tool whose whole job is to
            # notice. A day is the smallest unit `posted_date` can express, so
            # "posts daily or better" is the strongest cadence this data can
            # state, and one full day of silence is then a missed cycle.
            #
            # The median itself still rides out as measured, including 0: it is
            # the true cadence, and flooring what is REPORTED would be inventing
            # a number to make the arithmetic tidy.
            cadence = None if median is None else max(median, 1.0)
            # The quantity the ruling names, stated as a number rather than
            # collapsed into a boolean: 28 days silent on a 30-day cycle is
            # "genuinely borderline", and a flag is exactly what destroys that.
            ratio = (
                None if cadence is None or days_silent is None else round(days_silent / cadence, 3)
            )
            all_gaps = _interior_gaps(dates, cadence)
            gaps = all_gaps[:MAX_INTERIOR_GAPS_PER_ACCOUNT]
            rows.append(
                {
                    "account_id": account_id,
                    "account": named.get(account_id),
                    **facts.to_wire(),
                    **lifecycle[account_id].to_wire(),
                    # 🔴 AC-13.5, on the surface the contract puts it: a hold past
                    # any ordinary lifetime is a per-account finding, and
                    # `api-contract.md` rules a per-account finding belongs here
                    # rather than on an analysis answer. Present and zero on every
                    # row -- an account with no stranded hold is stating a fact,
                    # and a missing key would make a consumer guess whether it
                    # meant zero or unknown. The oldest is named because the
                    # operator's next move is to look at the transaction, so the
                    # id has to travel with the count.
                    "stranded_holds": len(stranded_by_account.get(account_id, ())),
                    "oldest_stranded_hold": _oldest_stranded(
                        stranded_by_account.get(account_id, ()),
                        # 🔴 Withheld for a non-active account, on AC-12.7's
                        # reasoning applied to the sibling criterion: a closed
                        # account's hold can never settle and can never be
                        # cleared, so naming it is the finding no operator can
                        # action -- the exact shape AC-12.7 removed from
                        # `silence_exceeds_cadence` a few lines below. The COUNT
                        # stays as measured, for the reason `silence_ratio` is
                        # left as measured: the number is the evidence, and the
                        # lifecycle fields on this row say why nothing is being
                        # asked of it.
                        active=lifecycle[account_id].active,
                    ),
                    "median_interval_days": median,
                    "days_silent": days_silent,
                    "silence_ratio": ratio,
                    # Present on every row, and an empty list is a real answer:
                    # an account with an unbroken history is stating that, and a
                    # missing key would make a consumer guess whether it meant
                    # none or not-measured. `interior_gaps` is the count as
                    # MEASURED; the list beside it may be shorter.
                    "interior_gaps": len(all_gaps),
                    "interior_gap_detail": gaps,
                    # 🔴 One full missed cycle, because it is the only
                    # non-arbitrary unit. Choosing 0.9 so the borderline pair
                    # flags would reinvent the constant the ruling removed;
                    # `silence_ratio` is what surfaces them instead.
                    #
                    # 🔴 **AC-12.7: a non-active account's trailing silence is
                    # identified as closure, not reported as a coverage finding.**
                    # The account stopped being reported, so its silence is the
                    # expected consequence of that rather than a hole in the
                    # data, and today the flag grows permanently true and can
                    # never go back. The RATIO is left as measured rather than
                    # nulled: it is the evidence, and `silence_ratio`'s own
                    # ruling is that a number lets a reader see what a boolean
                    # destroys. The lifecycle fields on this row are what say
                    # why the flag is false.
                    "silence_exceeds_cadence": (
                        False if ratio is None or not lifecycle[account_id].active else ratio > 1.0
                    ),
                    # Present and zeroed rather than omitted: a source with no
                    # rows for this account is a fact, and a missing key would
                    # make a consumer guess whether it meant zero or unknown.
                    "source_breakdown": {
                        source: sources.get(account_id, {}).get(source, 0)
                        for source in PROVENANCE_SOURCES
                    },
                }
            )
        return _answer(
            config,
            conn,
            rows,
            requested_window=None,
            truncation=None,
            extra_caveats=(
                _uncovered_caveat(
                    [c for c in coverage.values() if c.no_data_in_any_feed], listing=True
                )
                # Both fire together on a closed account that never had a
                # transaction. They are both true and they say different things,
                # and neither suppresses the other.
                + _not_active_caveat([e for e in lifecycle.values() if not e.active])
                + _roster_observed_empty_caveat([e for e in lifecycle.values() if not e.active])
            ),
            lifecycle=lifecycle,
        )


#: How a money aggregate may be grouped. A closed set because it reaches SQL:
#: the grouping is an expression this module builds, never a column name a
#: caller supplies, so an unknown value is refused at the boundary rather than
#: interpolated.
GROUPINGS: tuple[str, ...] = ("category", "merchant", "account", "month", "flow_class")

#: The three ways money can move, from the account holder's point of view.
#: Ordered as a reader wants them: what left the household first, then the two
#: kinds of movement that look like spending in a raw total and are not.
#:
#: 🔴 Local interpretation, derived at read time and written nowhere.
#: `data-model.md` keeps the aggregator's taxonomy unmodified in its own column,
#: and this reads that column rather than replacing it -- so a re-classification
#: is a code change with a diff, not a silent rewrite of history.
FLOW_CLASSES: tuple[str, ...] = ("external_spend", "internal_transfer", "debt_service")

#: What the aggregator categorised as a transfer. 🔴 **A transfer by ITS label,
#: not a movement verified between two enrolled accounts** -- nothing here
#: matches a counterparty leg, and this store's own payroll deposit arrives
#: categorised `TRANSFER_IN`. Measured at 61% of the two-year total -- $164,400
#: of $267,693 -- which is why a raw outflow figure over this store reads
#: several times what was actually spent, and why the split is published rather
#: than applied.
_INTERNAL_TRANSFER_CATEGORIES: frozenset[str] = frozenset({"TRANSFER_IN", "TRANSFER_OUT"})

#: Loan and card payments. 🔴 The primary category covers mortgage, auto,
#: student-loan and personal-loan payments as well as credit-card payoff -- so
#: only the last is the double count a card's own purchases create, and only
#: then if that card is enrolled. The rest is money out of the household.
_DEBT_SERVICE_CATEGORIES: frozenset[str] = frozenset({"LOAN_PAYMENTS"})


#: Every `source_category_primary` this mapping has actually been designed
#: against, observed in the sandbox datastore on 2026-09-09.
#:
#: 🔴 **An enumeration over a FOREIGN vocabulary, and therefore checked rather
#: than trusted.** The two sets above name three of these values; the rest reach
#: `external_spend` through the `else_` branch, and that branch is silent by
#: construction -- a category nobody classified is indistinguishable from one
#: deliberately left as spending. `tests/test_money_summary.py` holds this tuple
#: against the categories the store actually contains, so a value entering the
#: data without a decision being made about it goes RED rather than quietly
#: inflating the one figure this tool tells an agent to quote.
#:
#: 🔴 **The residual limit, stated rather than papered over:** this checks the
#: data this repo can see. A category arriving in an operator's own store that
#: has never appeared here still falls to `external_spend` with nothing saying
#: so. That is the conservative direction on this surface's own principle -- an
#: overcount gets questioned and an undercount gets believed -- but it is a
#: fallback, not a classification, and a real taxonomy feed is what would close
#: it properly.
KNOWN_SOURCE_CATEGORIES: tuple[str, ...] = (
    "FOOD_AND_DRINK",
    "GENERAL_MERCHANDISE",
    "INCOME",
    "LOAN_PAYMENTS",
    "PERSONAL_CARE",
    "RENT_AND_UTILITIES",
    "TRANSFER_IN",
    "TRANSFER_OUT",
    "TRANSPORTATION",
    "TRAVEL",
)


#: Every `source_category_detailed` this classifier has been designed against,
#: and the twin of `KNOWN_SOURCE_CATEGORIES` above.
#:
#: 🔴 **The detailed vocabulary is the one the class is now read from, so it is
#: the one that needs the guard.** The two sets above name the values that
#: change a row's class; every other detailed name reaches `external_spend`
#: through the `else_` branch, and that branch is silent by construction -- a
#: category nobody classified is indistinguishable from one deliberately left as
#: spending. `tests/test_money_summary.py` holds this tuple against what the
#: store actually contains, so a value entering the data without a decision goes
#: RED rather than quietly inflating the figure this tool tells an agent to
#: quote.
#:
#: 🔴 The same residual limit the primary tuple records applies here and is
#: larger, because the detailed vocabulary is: a name arriving in an operator's
#: own store that has never appeared here still falls to `external_spend` with
#: nothing saying so. Conservative, and a fallback rather than a classification.
KNOWN_SOURCE_CATEGORIES_DETAILED: tuple[str, ...] = tuple(
    sorted(
        transfers.TRANSFER_SHAPED_DETAILED
        | transfers.DEBT_SERVICE_DETAILED
        | {
            "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES",
            "FOOD_AND_DRINK_GROCERIES",
            "FOOD_AND_DRINK_RESTAURANT",
            "INCOME_WAGES",
            "INCOME_OTHER_INCOME",
            "PERSONAL_CARE_OTHER_PERSONAL_CARE",
            "RENT_AND_UTILITIES_RENT",
            "TRANSFER_IN_PAYROLL",
            "TRANSFER_OUT_WITHDRAWAL",
            "TRANSPORTATION_PUBLIC_TRANSIT",
            "TRAVEL_FLIGHTS",
        }
    )
)


#: How many groups `money_summary` puts in one payload.
#:
#: 🔴 A cap on the PAYLOAD, never on the arithmetic. The totals beside the rows
#: are summed from every group, so this bounds what a consumer receives without
#: changing what the answer says the window came to -- and `truncation` says the
#: list was cut, so a caller is never left inferring it from the length.
#:
#: Lower than `MAX_ROWS`: five hundred aggregate rows is already past what any
#: consumer reads, and the grouping most likely to reach this is the one whose
#: keys fragment, where the tail is single-transaction groups rather than
#: findings.
MAX_GROUPS = 200


def _flow_class() -> ColumnElement[str]:
    """Which side of the household boundary this money crossed.

    The question this answers is **"did this money cross the household boundary,
    and if not, where did it go instead"** -- not "what did the aggregator call
    this row". Every decision below follows from that one sentence.

    🔴 **Classify, do not filter** -- the owner's ruling on #18. Every row is
    kept and gains a class; nothing is dropped, precisely so there is no
    invisible undercount. That is also why the classification itself emits no
    `rule-applied` warning: that kind means rows were excluded from an aggregate
    on purpose, and classifying excludes nothing, so saying it would be a false
    statement about the answer carrying it. Other producers on the same answer
    do exclude rows and do raise it.

    🔴 **Read from `source_category_detailed`, NEVER from `category_override`.**
    The primary category is too coarse to carry the distinction that matters:
    `TRANSFER_IN` alone is why a payroll deposit landed in `internal_transfer`
    and income read $0 on this surface. The detailed name separates a paycheque
    from a movement between two accounts the household holds.

    An override is local interpretation of what a transaction was *for*; the
    flow class is about whose money moved and in which direction. Letting a
    re-categorisation reclassify a transfer as spending would reintroduce the
    overcount through the back door -- silently, and in the direction that
    inflates.

    An unrecognised or null category falls to `external_spend`. That is the
    conservative direction on this surface's own principle: an overcount gets
    questioned and an undercount gets believed. Null is measured at 0.00% here,
    so this is a rule about categories not yet invented rather than about
    today's data.
    """
    return case(
        (
            and_(
                transactions.c.source_category_detailed.in_(
                    sorted(transfers.TRANSFER_SHAPED_DETAILED)
                ),
                # 🔴 The evidence, not the label. A transfer-shaped row is only a
                # transfer when the other leg is HERE -- a matching, opposite row
                # on another enrolled account, recorded at derivation time. A
                # payment to a person and ACH rent are both transfer-shaped and
                # both money gone. An ATM withdrawal is not even transfer-shaped:
                # it reaches `external_spend` by classification.
                transactions.c.transfer_pair_id.is_not(None),
            ),
            "internal_transfer",
        ),
        (
            and_(
                transactions.c.source_category_detailed.in_(
                    sorted(transfers.DEBT_SERVICE_DETAILED)
                ),
                # 🔴 The liability has to be one THIS STORE HOLDS. A mortgage to
                # a lender the operator never enrolled is money out of the
                # household; a card payoff where the card is enrolled is not,
                # because that card's own purchases are already counted and
                # counting the payoff too is the double count this class exists
                # to prevent.
                transactions.c.transfer_pair_id.is_not(None),
            ),
            "debt_service",
        ),
        else_="external_spend",
    )


def _flow_class_totals(
    rows: list[dict[str, Any]], transitions: HoldTransitions
) -> list[dict[str, Any]]:
    """The window's money in and out, per currency, with the outflow split three ways.

    🔴 **The three flow classes are summed from the rows this answer returns,
    never from a second query.** A second read against a live store is taken at a
    different instant from the rows beside it, and a total that contradicts the
    rows under it is worse than no total at all -- a reader has no way to tell
    which of the two is wrong. Summed here, the three classes add to the window's
    total outflow by construction, and that identity is also the proof the
    classification PARTITIONS the rows rather than quietly dropping some. The
    pending pair follows the same rule for the same reason: it is summed from
    row fields the aggregate already computed, so it cannot disagree with them.

    🔴 **The two hold-transition pairs are the deliberate exception, and they are
    a different kind of figure rather than a relaxation of the rule.** They do
    not participate in the outflow identity above and must never be added to it:
    `expired_holds` counts rows this answer EXCLUDES -- a hold that was
    soft-deleted without ever posting -- so no arithmetic over the returned rows
    could reach it, and `settled_from_hold` counts rows whose amount arrived by
    replacing an earlier hold's. Both exist because AC-13.4 requires a total that
    moved to be attributable: the two are the only ways a figure over this window
    changes without any new activity.

    🔴 **Per currency, because the ruling on aggregates is that currency groups
    and never sums.** One integer spanning two currencies is not a wrong number,
    it is not a number.

    Every currency carries every key, zero where nothing appeared: a zero is a
    real answer -- "nothing serviced a debt this window", "no hold expired" --
    and a missing key would leave a reader unable to tell that from a figure this
    tool forgot to compute. AC-13.1 fixes that as the contract for the pending
    pair: present and zero, never absent.

    🔴 A currency that appears ONLY in an expired hold still gets an entry. Its
    rows are all excluded from the answer, so summing the returned rows would
    find no such currency at all -- and dropping the entry would hide the one
    fact that explains why a previous answer's figure is gone.
    """
    totals: dict[str, dict[str, int]] = {}

    def entry_for(currency: str) -> dict[str, int]:
        return totals.setdefault(
            currency,
            {f"{flow}_outflow_minor_units": 0 for flow in FLOW_CLASSES}
            | {
                "inflow_minor_units": 0,
                "outflow_minor_units": 0,
                "pending_transactions": 0,
                "pending_net_minor_units": 0,
            },
        )

    for row in rows:
        entry = entry_for(str(row["currency"]))
        entry[f"{row['flow_class']}_outflow_minor_units"] += int(row["outflow_minor_units"])
        # 🔴 The whole window's two directions, summed from the same rows as the
        # classes above so the identity between them holds by construction. They
        # exist because the questions a caller actually asks are "how much went
        # out" and "how much came in", and until they were published the only
        # answer to either was a class figure that excludes a mortgage payment
        # and an ATM withdrawal, or a sum the caller had to take over rows.
        entry["inflow_minor_units"] += int(row["inflow_minor_units"])
        entry["outflow_minor_units"] += int(row["outflow_minor_units"])
        entry["pending_transactions"] += int(row["pending_transactions"])
        entry["pending_net_minor_units"] += int(row["pending_net_minor_units"])
    for currency in transitions.currencies():
        entry_for(currency)
    for currency, entry in totals.items():
        expired = transitions.expired_for(currency)
        settled = transitions.settled_for(currency)
        entry["expired_holds"] = expired.transactions
        entry["expired_holds_net_minor_units"] = expired.net_minor
        entry["settled_from_hold"] = settled.transactions
        entry["settled_from_hold_net_minor_units"] = settled.net_minor
    # Sorted so two identical stores answer identically; the wire order of an
    # array is information a consumer may rely on even when it should not.
    return [{"currency": currency, **entry} for currency, entry in sorted(totals.items())]


def money_summary(
    config: Config,
    *,
    since: date | None = None,
    until: date | None = None,
    group_by: str = "category",
) -> Answer:
    """Money moved in a window, grouped, with BOTH directions on every row.

    🔴 **Both directions, because a one-directional aggregate cannot be corrected
    by its reader.** A tool that filtered to outflow would leave an inflow with
    no row to appear in, so a category of offsetting charges and credits reports
    its gross as though that were the cost: measured here, a travel category
    showed $12,000 spent against a true net of $0, and nothing in the payload
    could reveal the 24 credits behind it. Gross and net side by side is what
    makes that answerable.

    🔴 One tool rather than two, by `api-contract.md` § Direction's fourth norm:
    cashflow-by-month is this same aggregate grouped by month with inflows kept,
    so the two answer one row shape and a boundary between them would be drawn
    where the QUESTION changes rather than where the answer's shape does.

    🔴 **Direction is carried by the field NAME; sign is carried by `net_minor`.**
    `inflow_minor` and `outflow_minor` are positive magnitudes — "how much came
    in", "how much went out" — while `net_minor` stays signed from the account
    holder's point of view, as `data-model.md` requires of a stored amount. This
    row is the first place the two conventions meet, and a row that mixed them
    silently would be the very class of defect this work removes.

    🔴 **Currency groups; it never sums.** The owner's 2026-09-08 ruling, made
    once for every aggregate tool: a window spanning two currencies returns
    per-currency rows rather than one meaningless integer. Conversion is
    explicitly not chosen — it needs a rate source, a rate date policy, and
    somewhere to record that decision, which is a different scope from carrying
    a field.

    🔴 **Every figure here states how much of itself is an unsettled hold, and
    the statement is always present** (AC-13.1). An authorisation hold is a claim
    on the account rather than a completed amount: it can settle at a different
    figure, and it can expire without settling at all. So a total that mixes
    holds with settled money is the one kind of figure that changes when nothing
    happened, and re-asking will not explain it. `pending_transactions` and
    `pending_net_minor_units` ride every row and every totals entry, zero where
    nothing is pending, and `includes_pending_rows` fires only when something is
    — so its absence is a statement too. AC-13.4's other half rides `totals`
    beside them: `expired_holds` for the holds that dropped out of this window
    without ever posting, and `settled_from_hold` for the rows whose amount got
    here by replacing an earlier hold's.
    """
    if group_by not in GROUPINGS:
        raise BadGroupingError(f"group_by must be one of {', '.join(GROUPINGS)}, not {group_by!r}")
    problem = _readable(config)
    if problem is not None:
        # Both blocks are present and EMPTY, on one rule: this tool carries them,
        # and an unreadable store is a reason for them to hold nothing rather
        # than a reason for a key to vanish. A consumer branching on the presence
        # of `truncation` or `totals` must not have to handle a third state where
        # the store simply could not be read.
        return _unusable(
            config,
            problem,
            requested_window=(since, until),
            truncation=Truncation.over(
                counting="groups", returned=0, remaining=0, matching=0, resume_from=None
            ),
            totals=[],
        )
    with reader_connection(config) as conn:
        # 🔴 Read before the statement is built, because it is a PREDICATE on
        # this aggregate and not a note appended to it. An account whose unit or
        # whose unit's scale is unknown contributes nothing to a minor-units
        # figure -- see `_undenominable_accounts` -- and the caveat below names
        # every one of them, including the ones whose rows were refused at
        # derivation and so could never have appeared here anyway.
        undenominable = _undenominable_accounts(conn)
        # 🔴 Read ONCE per answer and threaded, never recomputed per statement.
        # The rows, the totals and the hold tallies must all be taken over the
        # same set: a second read at a different instant could name a different
        # boundary, and the answer would carry figures computed under two rules
        # while presenting them as one.
        spans = lineage.superseded_spans(conn)
        # Annotated as the general expression type both branches produce: the
        # first assignment would otherwise fix the name to `coalesce` and the
        # account branch's plain column would not fit it.
        key: ColumnElement[Any]
        label: ColumnElement[Any]
        if group_by == "category":
            key = _effective_category()
            label = key
        elif group_by == "merchant":
            # 🔴 Case- and whitespace-normalized, so "AMAZON", "Amazon" and
            # "Amazon  " are one merchant rather than three. The aggregator's
            # merchant string is unvalidated free text and this is the whole of
            # what can be normalized without guessing.
            #
            # 🔴 **Reference numbers are deliberately NOT stripped**, and that is
            # the fragmentation this grouping still has. The description fallback
            # carries per-transaction references, so a merchant with no
            # `merchant_name` fragments into one group per transaction -- which
            # the cap below makes bounded and visible rather than fatal. A
            # stripper would fix it by merging on a guess, and the guess fails
            # SILENTLY in the direction that loses money: store numbers and city
            # suffixes are how genuinely different merchants differ, so
            # collapsing them reports one total where there were two, with
            # nothing on the answer to say it happened. An overcount of groups
            # gets questioned; a merged one gets believed.
            key = func.replace(
                func.upper(
                    func.trim(
                        func.coalesce(
                            transactions.c.merchant_name, transactions.c.description, "UNKNOWN"
                        )
                    )
                ),
                "  ",
                " ",
            )
            label = key
        elif group_by == "account":
            # The id is the key a caller can act on; the name is for reading.
            key = cast(transactions.c.account_id, Text)
            label = accounts.c.name
        elif group_by == "flow_class":
            # The degenerate grouping: the class is already a dimension of every
            # row below, so grouping BY it is the roll-up to just the three. Key
            # and label are the same string because the class has no id and no
            # prettier name -- inventing one would be a second vocabulary for
            # the same three values.
            key = _flow_class()
            label = key
        else:
            # 🔴 The month a figure belongs to is an ECONOMIC question, so it is
            # taken from `ledger_date`. Grouped on `posted_date`, a hold
            # authorised on 06-28 and posted on 07-02 leaves June's total after
            # it settles, and the same window asked twice a week apart returns
            # two different Junes with nothing on the answer to say why.
            #
            # Stored as `YYYY-MM-DD` text that sorts as a date, so the month is
            # its first seven characters -- no date arithmetic, and no dialect
            # function to disagree about.
            key = func.substr(transactions.c.ledger_date, 1, 7)
            label = key

        statement = (
            select(
                key.label("group_key"),
                label.label("group_label"),
                transactions.c.currency,
                # 🔴 A GROUPING DIMENSION on every grouping, not a field bolted
                # onto one. The class is not a function of the group for any
                # value of `group_by`: a merchant takes both a purchase and a
                # refund, an account holds a transfer and a coffee, a month
                # holds all three by definition -- and even a category can split,
                # because the category key reads `category_override` first while
                # the class is ruled to read the source column only. Attaching
                # one row's class to a group that spans classes would state it
                # for the others; making it OPTIONAL is refused by
                # `api-contract.md` § Direction's fourth norm, which merges tools
                # only where one strict row schema covers every parameter value.
                # Grouping finer is the only way out that keeps both promises.
                _flow_class().label("flow_class"),
                func.count().label("transactions"),
                # 🔴 Positive magnitudes, both of them. `amount_minor` is
                # operator-signed, so outflow is the negative half negated --
                # and doing that here rather than in Python keeps the sum and
                # the count over one pass of one predicate set.
                func.coalesce(
                    func.sum(
                        case(
                            (transactions.c.amount_minor > 0, transactions.c.amount_minor), else_=0
                        )
                    ),
                    0,
                ).label("inflow"),
                func.coalesce(
                    func.sum(
                        case(
                            (transactions.c.amount_minor < 0, -transactions.c.amount_minor), else_=0
                        )
                    ),
                    0,
                ).label("outflow"),
                func.coalesce(func.sum(transactions.c.amount_minor), 0).label("net"),
                # 🔴 AC-13.1, computed in the SAME pass as the figures it
                # qualifies. A hold is not a settled amount, and a total that
                # mixes the two moves without any new activity -- so how much of
                # this row is a hold has to be a property OF this row rather than
                # a second count taken a moment later, which could disagree with
                # the very number it is supposed to qualify.
                func.coalesce(func.sum(case((transactions.c.pending == 1, 1), else_=0)), 0).label(
                    "pending_transactions"
                ),
                # Signed as stored, like `net` and unlike the two magnitudes
                # above: the question a reader has is which direction the figure
                # beside it can move when these holds settle or expire.
                func.coalesce(
                    func.sum(
                        case((transactions.c.pending == 1, transactions.c.amount_minor), else_=0)
                    ),
                    0,
                ).label("pending_net"),
            )
            .select_from(transactions.join(accounts))
            # 🔴 The shared predicates and NOTHING else -- in particular no
            # direction filter. A `WHERE amount_minor < 0` here would leave an
            # inflow with no row to appear in at all, which is unreachable
            # rather than merely unaggregated. Direction is a COLUMN on the row,
            # so both halves are always answerable.
            .where(
                *_transaction_filters(
                    since=since, until=until, account_id=None, after=None, spans=spans
                ),
                # 🔴 Excluded in the SQL rather than filtered out of the rows
                # afterwards, so `totals` -- which is summed from these rows by
                # construction -- cannot disagree with them about what the
                # answer covered.
                accounts.c.account_id.notin_([entry.account_id for entry in undenominable]),
            )
            .group_by("group_key", "group_label", transactions.c.currency, "flow_class")
            .order_by(func.sum(transactions.c.amount_minor))
        )
        # 🔴 EVERY group, then capped for the payload -- never capped in SQL.
        # `totals` and the pending tallies below are summed from these rows BY
        # CONSTRUCTION, which is what makes the three flow classes add to the
        # window's outflow and what stops a total contradicting the rows under
        # it. A `LIMIT` in the statement would silently shrink every one of them
        # to the visible groups, and the answer would still look complete: an
        # aggregate that under-reports its own total is the precise wrong number
        # this surface exists to refuse. So the cap bounds the PAYLOAD, not the
        # arithmetic.
        every_group = [
            {
                "group_key": str(r[0]),
                "group_label": str(r[1]),
                "currency": r[2],
                "flow_class": str(r[3]),
                "transactions": int(r[4]),
                "inflow_minor_units": int(r[5]),
                "outflow_minor_units": int(r[6]),
                "net_minor_units": int(r[7]),
                "pending_transactions": int(r[8]),
                "pending_net_minor_units": int(r[9]),
            }
            for r in conn.execute(statement).all()
        ]
        # Ordered by net ascending, so the largest outflows lead and a cut list
        # keeps the groups a caller asked the question for.
        rows = every_group[:MAX_GROUPS]
        pending = {
            currency: HoldTally(
                transactions=sum(
                    int(row["pending_transactions"])
                    for row in every_group
                    if row["currency"] == currency
                ),
                net_minor=sum(
                    int(row["pending_net_minor_units"])
                    for row in every_group
                    if row["currency"] == currency
                ),
            )
            for currency in {str(row["currency"]) for row in every_group}
        }
        # 🔴 Store-wide, because this tool's scope is store-wide: it takes no
        # `account_id`, so every account contributes to every group it belongs
        # in, and an account that stopped being reported mid-window contributes
        # nothing for the rest of it. That silence is what the two caveats name.
        # Without them the figure an agent is told to QUOTE was the one answer on
        # this surface carrying no lifecycle or coverage caveat at all: a card
        # de-selected on the first of a month reads as a 40% drop in spending,
        # well-formed and unexplained.
        # One walk, used twice -- the caveat below and the envelope's non-active
        # figures, which `_answer` would otherwise derive from a second
        # observation of the same fact.
        lifecycle = _account_lifecycle(conn)
        not_active = [entry for entry in lifecycle.values() if not entry.active]
        all_coverage = list(_account_coverage(conn).values())
        uncovered = [entry for entry in all_coverage if entry.uncovered]
        return _answer(
            config,
            conn,
            rows,
            requested_window=(since, until),
            spans=spans,
            # 🔴 Reported, where this tool used to say `None` because "an
            # aggregate is unpaginated by contract, bounded by the grouping".
            # That held only while the grouping bounded anything: keyed on a
            # merchant string that falls back to a per-transaction description,
            # the group count approaches the TRANSACTION count, and the answer
            # grows without limit while `capped` reads false.
            truncation=Truncation.over(
                counting="groups",
                returned=len(rows),
                remaining=len(every_group),
                matching=len(every_group),
                resume_from=None,
            ),
            totals=_flow_class_totals(
                # 🔴 EVERY group, not the capped list beside it. The three flow
                # classes must add to the window's outflow, and that identity is
                # also the proof the classification partitions the rows rather
                # than dropping some -- computing it over a truncated list would
                # break both, quietly, in the direction of a smaller total.
                every_group,
                _hold_transitions(
                    conn,
                    since=since,
                    until=until,
                    excluded=[entry.account_id for entry in undenominable],
                    spans=spans,
                ),
            ),
            # 🔴 AC-14.5. The two producers are independent and both belong here:
            # the pending caveat says this figure may still move, the sign caveat
            # says its DIRECTION may be wrong. A total can be wrong in both ways
            # at once, and a reader given only one of them would treat the other
            # as settled.
            extra_caveats=(
                _uncovered_caveat(uncovered, listing=False)
                + _superseded_caveat(spans, account_id=None, since=since, until=until)
                + _unmatched_transfer_caveat(conn, since=since, until=until)
                + _window_coverage_caveat(all_coverage, since)
                + _not_active_caveat(not_active)
                + _roster_observed_empty_caveat(not_active)
                + _undenominable_caveat(undenominable)
                + _pending_caveat(pending)
                + signs.caveats(conn, since=since, until=until)
            ),
            lifecycle=lifecycle,
        )


def _sync_domains(conn: SAConnection) -> dict[int, list[dict[str, Any]]]:
    """Every sync domain every connection has ever attempted, grouped by connection.

    🔴 **Read as its own query rather than joined into the health rows**, because
    the two answer different shapes: one connection has one health row and any
    number of domains, and a join would return the product of the two. Every
    count over `rows` on this surface -- and a consumer's own count of its
    connections -- would double the day a second domain landed, silently and in
    the direction of looking like more coverage rather than less.

    Ordered by domain name so two calls against an unchanged store return the
    same bytes; nothing reads the order for meaning.

    An entry exists for a domain that has been ATTEMPTED, whether or not it has
    ever succeeded, which is what makes AC-4.5's two cases distinguishable: a
    null `last_success_at` here means the domain has been tried and has never
    landed in full, while no entry at all means it has never been tried.
    """
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in conn.execute(
        select(
            sync_state.c.connection_id,
            sync_state.c.domain,
            sync_state.c.last_attempt_at,
            sync_state.c.last_success_at,
            sync_state.c.last_error_code,
            sync_state.c.last_error_at,
            sync_state.c.history_start_date,
        ).order_by(sync_state.c.connection_id, sync_state.c.domain)
    ).all():
        grouped.setdefault(int(row[0]), []).append(
            {
                "domain": str(row[1]),
                "last_attempt_at": None if row[2] is None else row[2].isoformat(),
                # 🔴 Null is NEVER LANDED IN FULL, never "fine". This is the
                # field the size of the hole is measured from (AC-4.5), and it
                # advances only when the domain got everything it asked for --
                # a pull whose positions arrived and whose transaction window
                # came back short leaves it exactly where it was.
                "last_success_at": None if row[3] is None else row[3].isoformat(),
                "last_error_code": row[4],
                "last_error_at": None if row[5] is None else row[5].isoformat(),
                "history_starts": None if row[6] is None else str(row[6]),
            }
        )
    return grouped


def pipeline_health(config: Config) -> Answer:
    """Every connection and what is wrong with it — the question AC-ARCH.3 names.

    Returns rows even when everything is fine, because "healthy" is an answer
    and an empty result would be indistinguishable from a broken query.
    """
    problem = _readable(config)
    if problem is not None:
        return _unusable(config, problem, requested_window=None, truncation=None)
    with reader_connection(config) as conn:
        result = conn.execute(
            select(
                connections.c.connection_id,
                institutions.c.name,
                connections.c.status,
                connections.c.last_success_at,
                connections.c.last_error_code,
                connections.c.requested_history_days,
                connections.c.granted_history_days,
                connections.c.retired_at,
                sync_state.c.history_start_date,
                connections.c.consent_expires_at,
                connections.c.source_error_code,
            )
            .select_from(
                connections.join(institutions).outerjoin(
                    sync_state,
                    (sync_state.c.connection_id == connections.c.connection_id)
                    # 🔴 Still one row per CONNECTION, and the domains ride it as
                    # a block. `sync_state` is keyed on (connection, domain), so
                    # joining it unfiltered would return one health row per
                    # domain and every consumer counting connections would count
                    # each one twice -- which is the regression a second domain
                    # makes available for the first time. The per-domain facts
                    # come from `_sync_domains` below, grouped under the
                    # connection they belong to.
                    #
                    # Pinned here because this column is the transactions grant:
                    # `history_starts` beside `granted_history_days` is the date
                    # that window was measured from. Each domain's own start
                    # rides its own entry.
                    & (sync_state.c.domain == TRANSACTIONS_DOMAIN),
                )
            )
            .order_by(connections.c.connection_id)
        ).all()
        # 🔴 The sign-convention measurement belongs on THIS surface, per
        # connection, because "we could not tell" is an answer only a
        # verification tool has room for. `signs.measure` returns one entry per
        # connection including the ones with nothing to judge, so every row here
        # carries the three fields and none of them is conditional.
        measured = signs.measure(conn)
        conventions = {m.connection_id: m for m in measured}
        domains = _sync_domains(conn)
        rows = [
            {
                "connection_id": int(r[0]),
                "institution": r[1],
                "status": r[2],
                "last_success_at": None if r[3] is None else r[3].isoformat(),
                "last_error_code": r[4],
                "requested_history_days": None if r[5] is None else int(r[5]),
                # 🔴 Null is reported as null, never as zero or as "complete".
                "granted_history_days": None if r[6] is None else int(r[6]),
                "history_starts": None if r[8] is None else str(r[8]),
                # 🔴 Null means the Item has not been fetched since this column
                # existed -- NEVER that consent does not expire, and never that
                # the aggregator reports nothing wrong. A reader that treats
                # either null as reassurance reproduces the defect these columns
                # were added to end.
                "consent_expires_at": None if r[9] is None else r[9].isoformat(),
                "source_error_code": r[10],
                "retired": r[7] is not None,
                "sign_convention": conventions[int(r[0])].verdict,
                "sign_convention_rows_judged": conventions[int(r[0])].rows_judged,
                "sign_convention_rows_positive": conventions[int(r[0])].rows_positive,
                # 🔴 AC-4.4 and AC-4.5, for a pipeline that is no longer one
                # stream per connection. Empty is a real answer and means
                # nothing has ever been attempted for this connection -- which
                # is why an attempted-and-failed domain is an ENTRY carrying a
                # null `last_success_at` rather than a missing one. A consumer
                # that read absence as health would reproduce the exact failure
                # this surface exists to catch.
                "domains": domains.get(int(r[0]), []),
            }
            for r in result
        ]
        return _answer(
            config,
            conn,
            rows,
            requested_window=None,
            truncation=None,
            # An inverted connection is reported here as well as on the
            # aggregates, because a health tool that showed the measurement in a
            # row and stayed silent in `warnings` would leave the one surface
            # built for this question quieter about it than every other.
            #
            # 🔴 Handed the measurement the rows above were built from, never
            # re-measured. The reader is autocommit and pins no snapshot, so a
            # second scan is a second observation -- and this answer would then
            # be able to publish `consistent` in a row while warning that the
            # same connection is unverified.
            #
            # 🔴 AC-12.5a's second surface. A roster that comes back empty is a
            # connection-level anomaly, and a broken feed that returns success
            # is exactly what a health check is for -- this tool reads
            # `connections`, `sync_state` and the sign measurement, none of
            # which can tell such a connection from a healthy one. Reported as a
            # finding about a NAMED connection rather than as a caveat on the
            # answer as a whole, like every other per-connection finding here.
            extra_caveats=(
                signs.caveats(conn, measured=measured)
                + _roster_observed_empty_findings(rows, _connections_with_an_empty_roster(conn))
            ),
        )
