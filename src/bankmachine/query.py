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

from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import Text, and_, case, cast, func, or_, select
from sqlalchemy.engine import Connection as SAConnection
from sqlalchemy.sql import ColumnElement

from bankmachine import signs
from bankmachine.config import Config
from bankmachine.envelope import (
    MAX_ROWS,
    STALE_AFTER,
    Answer,
    Caveat,
    Cursor,
    Truncation,
    Window,
    iso_or_none,
    resolve_window,
)
from bankmachine.store.connection import inspect
from bankmachine.store.engine import reader_connection
from bankmachine.store.schema import (
    PROVENANCE_SOURCES,
    TRANSACTIONS_DOMAIN,
    accounts,
    balances_daily,
    connections,
    institutions,
    sync_state,
    transactions,
)
from bankmachine.store.types import CalendarDate, UtcInstant, calendar_date, now_utc


def _is_short(granted: Any, requested: Any) -> bool:
    """Whether the aggregator granted less history than was asked for.

    A named predicate rather than an inline conjunction, because the null case is
    the one that matters: null granted is NOT a shortfall of zero, it is an
    unmeasured window, and the caller reports the two differently (AC-1.3a).
    """
    return granted is not None and requested is not None and int(granted) < int(requested)


def _pipeline_warnings(conn: SAConnection, now: UtcInstant) -> list[Caveat]:
    """Everything wrong with the data underneath any answer.

    Computed per call rather than cached: an answer's warnings describe the
    datastore at the moment it was read, and a cache would make them describe
    some earlier moment while the rows described this one.
    """
    warnings: list[Caveat] = []
    rows = conn.execute(
        select(
            connections.c.connection_id,
            institutions.c.name,
            connections.c.status,
            connections.c.last_success_at,
            connections.c.last_error_code,
            connections.c.requested_history_days,
            connections.c.granted_history_days,
        )
        .select_from(connections.join(institutions))
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

    for row in rows:
        connection_id, name = int(row[0]), str(row[1])
        status, last_success = str(row[2]), row[3]
        requested, granted = row[5], row[6]

        if status == "degraded":
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
        if last_success is None:
            warnings.append(
                Caveat(
                    kind="partial",
                    detail=f"{name} has never completed a sync, so it contributes no data yet",
                    connection_id=connection_id,
                    institution=name,
                )
            )
        elif now - last_success > STALE_AFTER:
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
        if granted is None and last_success is not None:
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
                    detail=(
                        f"{name} granted {int(granted)} days of history against "
                        f"{int(requested)} requested, so anything older than that is absent "
                        f"rather than zero"
                    ),
                    connection_id=connection_id,
                    institution=name,
                )
            )
    return warnings


def _coverage(conn: SAConnection) -> dict[str, Any]:
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
    not_active = [entry for entry in _account_lifecycle(conn).values() if not entry.active]
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

    @property
    def uncovered(self) -> bool:
        """No transaction has ever been recorded for this account.

        A property rather than a comparison written at each site: "counts as
        uncovered" is a rule, and a rule spelled three times is a rule that
        stops agreeing with itself.
        """
        return self.transaction_count == 0

    def to_wire(self) -> dict[str, Any]:
        """The three fields every account row carries, in every tool that carries them."""
        return {
            "first_transaction_date": iso_or_none(self.first_transaction_date),
            "last_transaction_date": iso_or_none(self.last_transaction_date),
            "transaction_count": self.transaction_count,
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
    """
    result = conn.execute(
        select(
            accounts.c.account_id,
            func.min(transactions.c.posted_date),
            func.max(transactions.c.posted_date),
            func.count(transactions.c.transaction_id),
        )
        .select_from(
            accounts.outerjoin(
                transactions,
                (transactions.c.account_id == accounts.c.account_id)
                & transactions.c.removed_at.is_(None),
            )
        )
        .group_by(accounts.c.account_id)
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
    is re-derivable from the row without a second call -- `last_seen_in_roster <
    roster_last_observed` is the whole derivation. That is the `silence_ratio`
    ruling applied again: a number lets a reader see a borderline case, and a
    bare flag is what destroys that.
    """

    account_id: int
    lifecycle: str
    closed_date: CalendarDate | None
    last_seen_in_roster: CalendarDate | None
    roster_last_observed: CalendarDate | None

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

    **AC-12.5's three clauses hold by construction, not by three guards.**
    `roster_last_observed` for a connection is the maximum of its own accounts'
    `last_seen_in_roster`, so:

    * a connection whose roster could not be fetched moves no date at all, and
      nothing becomes older than a maximum that did not move;
    * a connection whose *entire* roster vanishes at once moves the maximum with
      it, so nothing is ever older than it -- fourteen simultaneous closures is
      not a thing that happens, and the connection-level failure it really is
      already has a home in `get_pipeline_health`; and
    * an account with no connection (the FR-7 import path) has no roster to be
      absent from, so both dates are null and the comparison is never reached.

    A `connections.roster_observed_at` column would have needed each of those
    three written into it by hand. This is `learnings.md` § *Guarantees by
    construction*: the property is a consequence of how the number is computed.
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

    observed: dict[int, CalendarDate | None] = {}
    seen: dict[int, CalendarDate | None] = {}
    for row in result:
        connection_id = None if row[1] is None else int(row[1])
        # 🔴 The transitional rule migration 003 traded a backfill for. A null
        # `last_seen_date` predates the migration, and reading it as
        # `first_seen_date` is true by construction: the account was listed at
        # least once, on that date. It retires itself -- one post-migration sync
        # repopulates every account its rosters still name, and a null surviving
        # that belongs to an account no roster has listed since, which is exactly
        # what this function is looking for.
        last_seen = None if connection_id is None else calendar_date(row[5] or row[4])
        seen[int(row[0])] = last_seen
        if connection_id is not None and last_seen is not None:
            current = observed.get(connection_id)
            observed[connection_id] = last_seen if current is None else max(current, last_seen)

    lifecycle: dict[int, AccountLifecycle] = {}
    for row in result:
        account_id = int(row[0])
        connection_id = None if row[1] is None else int(row[1])
        last_seen = seen[account_id]
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
        elif last_seen is not None and roster is not None and last_seen < roster:
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
        )
    return lifecycle


def _not_active_caveat(lifecycle: list[AccountLifecycle]) -> list[Caveat]:
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
            detail=(
                "; ".join(parts) + ". Their balances froze on the date each row names and are "
                "not facts about today. Any total over balances INCLUDES them on purpose -- "
                "`coverage.accounts_not_active` and "
                "`coverage.not_active_balance_minor_units` are what they contributed, so quote "
                "that magnitude beside the total rather than presenting the total alone"
            ),
        )
    ]


def _uncovered_caveat(uncovered: list[AccountCoverage]) -> list[Caveat]:
    """The warning that names the accounts an answer could not have data for.

    🔴 Request-scoped: it fires only when THIS request's scope holds an
    uncovered account. A kind riding every response equally is the defect
    `CONNECTION_SCOPED_KINDS` records above -- the `gapped` notice arrived
    character-for-character identical on four unrelated questions, true and
    useless for telling a caller whether this answer was the degraded one.

    Names the ids, because "some accounts have no data" is a warning nobody can
    act on and the caller's next move is to ask about a different account.
    """
    if not uncovered:
        return []
    ids = ", ".join(
        str(coverage.account_id) for coverage in sorted(uncovered, key=lambda c: c.account_id)
    )
    return [
        Caveat(
            kind="accounts_without_coverage",
            detail=(
                f"no transaction has ever been recorded for account(s) {ids}; an empty or "
                f"absent result for them means DATA NOT PRESENT, never no activity"
            ),
        )
    ]


def _covered_rows(conn: SAConnection, *, since: date, until: date) -> int:
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
            .where(*_transaction_filters(since=since, until=until, account_id=None, after=None))
        ).scalar_one()
    )


def _answer(
    config: Config,
    conn: SAConnection,
    rows: list[dict[str, Any]],
    *,
    requested_window: tuple[date | None, date | None] | None,
    truncation: Truncation | None,
    totals: list[dict[str, Any]] | None = None,
    extra_caveats: list[Caveat] | None = None,
) -> Answer:
    """One answer, and the one place a window is reconciled against coverage.

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
    coverage = _coverage(conn)
    window = (
        None
        if requested_window is None
        else resolve_window(
            since=requested_window[0],
            until=requested_window[1],
            coverage=coverage,
            as_of=now,
        )
    )
    covered = None if window is None else window.covered_bounds()
    if window is not None:
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
                0 if covered is None else _covered_rows(conn, since=covered[0], until=covered[1])
            ),
        }
    return Answer(
        rows=rows,
        # 🔴 Connection-scoped warnings first, then this request's own. A
        # consumer reading top-down meets the standing state of the pipeline
        # before the thing that is specific to what they just asked.
        warnings=(
            _pipeline_warnings(conn, now)
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
                    f"nothing to report. Run `bankmachine store init` to create it"
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
            **({} if requested_window is None else {"transactions_in_effective_window": 0}),
        },
    )


def _readable(config: Config) -> str | None:
    """The reason the datastore cannot be read, or None when it can.

    Checked per call rather than once at startup: a datastore can be created,
    moved or corrupted while a long-lived server is running, and an answer must
    describe the store as it is at the moment of answering.
    """
    status = inspect(config)
    return None if status.healthy else (status.problem or "it is missing or unreadable")


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
        uncovered = [c for c in coverage.values() if c.uncovered]
        not_active = [entry for entry in lifecycle.values() if not entry.active]
        return _answer(
            config,
            conn,
            rows,
            requested_window=None,
            truncation=None,
            extra_caveats=_uncovered_caveat(uncovered) + _not_active_caveat(not_active),
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


def _account_exists(conn: SAConnection, account_id: int) -> bool:
    found = conn.execute(
        select(accounts.c.account_id).where(accounts.c.account_id == account_id).limit(1)
    ).first()
    return found is not None


def _transaction_filters(
    *,
    since: date | None,
    until: date | None,
    account_id: int | None,
    after: Cursor | None,
) -> list[Any]:
    """🔴 The predicates of a transaction query, built once for both statements.

    The row query and the count MUST select the same set, and "remember to
    update both" is the enumeration failure this repo has already been bitten
    by: the two would drift apart silently, and the symptom would be a
    `matching` that contradicts the rows beside it -- a precise wrong number,
    which is worse than the vague one this chunk exists to remove. Built here,
    a filter added later reaches both by construction because there is only one
    place to add it.
    """
    filters: list[Any] = [transactions.c.removed_at.is_(None)]
    if since is not None:
        filters.append(transactions.c.posted_date >= since)
    if until is not None:
        filters.append(transactions.c.posted_date <= until)
    if account_id is not None:
        filters.append(transactions.c.account_id == account_id)
    if after is not None:
        # 🔴 The keyset predicate belongs in the SHARED list, not on the row
        # query alone. `matching` is the count of what this request selects, and
        # a page whose count ignored the cursor would report the whole result
        # set behind every page — so `truncated` would stay true on the last one
        # and a caller paging until it went false would never stop.
        #
        # Spelled as an explicit disjunction rather than as a row-value
        # comparison, because it mirrors the ORDER BY beside it one clause at a
        # time: strictly older, or the same day and further down the tie-break.
        filters.append(
            or_(
                transactions.c.posted_date < after.posted_date,
                and_(
                    transactions.c.posted_date == after.posted_date,
                    transactions.c.transaction_id < after.transaction_id,
                ),
            )
        )
    return filters


def list_transactions(
    config: Config,
    *,
    since: date | None = None,
    until: date | None = None,
    account_id: int | None = None,
    limit: int = 100,
    after: Cursor | None = None,
) -> Answer:
    """Transactions in a window, newest first. Soft-deleted rows are excluded.

    `after` resumes a paged walk at the row a previous answer's `next_cursor`
    named. It narrows this request the way `since` does — `matching` counts what
    is left from that position, so `truncated` reads false on the page that
    exhausts the window and the caller has a terminating condition rather than a
    number to compare.
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
            truncation=Truncation.over(returned=0, counted=0, resume_from=None),
        )
    with reader_connection(config) as conn:
        # 🔴 Ordered AFTER the readability check on purpose: an unreadable store
        # knows nothing about which accounts exist, and "that account does not
        # exist" is a claim about the data rather than about the connection.
        if account_id is not None and not _account_exists(conn, account_id):
            raise UnknownAccountError(
                f"account_id {account_id} does not exist. list_accounts reports the ids that do."
            )
        filters = _transaction_filters(since=since, until=until, account_id=account_id, after=after)
        # 🔴 Both statements take the SAME from-clause as well as the same
        # filters. The join to `accounts` is part of what selects a row -- an
        # inner join drops a transaction whose account is absent -- so a count
        # taken over the bare table would exceed the rows and report a
        # truncation that never happened.
        source = transactions.join(accounts)
        statement = (
            select(
                transactions.c.transaction_id,
                accounts.c.name.label("account"),
                transactions.c.posted_date,
                transactions.c.description,
                transactions.c.merchant_name,
                transactions.c.amount_minor,
                transactions.c.currency,
                transactions.c.pending,
                transactions.c.source_category_primary,
                transactions.c.category_override,
            )
            .select_from(source)
            .where(*filters)
            .order_by(transactions.c.posted_date.desc(), transactions.c.transaction_id.desc())
            .limit(max(1, min(limit, MAX_ROWS)))
        )
        selected = conn.execute(statement).all()
        rows = [
            {
                "transaction_id": int(r[0]),
                "account": r[1],
                "date": str(r[2]),
                "description": r[3],
                "merchant": r[4],
                "amount_minor_units": int(r[5]),
                "currency": r[6],
                "pending": bool(r[7]),
                "category": r[9] or r[8],
                "category_is_override": r[9] is not None,
            }
            for r in selected
        ]
        matching = conn.execute(
            select(func.count()).select_from(source).where(*filters)
        ).scalar_one()
        # 🔴 Built from the LAST ROW THE STATEMENT RETURNED, in the column types
        # the order clause sorts on -- never re-parsed from the wire dict beside
        # it, whose `date` is already a string. A cursor rebuilt from the
        # rendering of a row is a second description of the position, and the
        # ordering it has to agree with is SQL's.
        resume_from = (
            None
            if not selected
            else Cursor.issued_for(
                posted_date=selected[-1][2],
                transaction_id=int(selected[-1][0]),
                since=since,
                until=until,
                account_id=account_id,
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
        return _answer(
            config,
            conn,
            rows,
            requested_window=(since, until),
            # `returned` is derived from the rows themselves rather than from
            # `limit`, so it cannot claim a count the payload does not contain.
            truncation=Truncation.over(
                returned=len(rows), counted=matching, resume_from=resume_from
            ),
            extra_caveats=_uncovered_caveat(uncovered),
        )


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

        # 🔴 One "today" for every row. Reading the clock per account would let a
        # report straddle midnight and hand back rows measured against two
        # different days, which is a difference nobody could explain from the
        # payload.
        today = calendar_date(now_utc().date())
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
            rows.append(
                {
                    "account_id": account_id,
                    "account": named.get(account_id),
                    **facts.to_wire(),
                    **lifecycle[account_id].to_wire(),
                    "median_interval_days": median,
                    "days_silent": days_silent,
                    "silence_ratio": ratio,
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
                _uncovered_caveat([c for c in coverage.values() if c.uncovered])
                # Both fire together on a closed account that never had a
                # transaction. They are both true and they say different things,
                # and neither suppresses the other.
                + _not_active_caveat([e for e in lifecycle.values() if not e.active])
            ),
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

#: The holder moving their own money between their own accounts. Measured at 61%
#: of the two-year total -- $164,400 of $267,693 -- which is why a raw outflow
#: figure over this store reads several times what was actually spent.
_INTERNAL_TRANSFER_CATEGORIES: frozenset[str] = frozenset({"TRANSFER_IN", "TRANSFER_OUT"})

#: Servicing a debt rather than buying anything. Measured as ~100% credit-card
#: payoff, which is a DOUBLE count: the card purchases the payment settles are
#: already counted under the categories they were spent in.
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


def _flow_class() -> ColumnElement[str]:
    """Which side of the household boundary this money crossed.

    🔴 **Classify, do not filter** -- the owner's ruling on #18. Every row is
    kept and gains a class; nothing is dropped, precisely so there is no
    invisible undercount. That is also why this emits no `rule-applied` warning:
    that kind means "an account rule filtered rows OUT of an aggregate, the
    total excludes them on purpose", and nothing here excludes anything, so
    saying it would be a false statement about the answer carrying it.

    🔴 **Read from `source_category_primary`, NEVER from `category_override`.**
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
            transactions.c.source_category_primary.in_(sorted(_INTERNAL_TRANSFER_CATEGORIES)),
            "internal_transfer",
        ),
        (
            transactions.c.source_category_primary.in_(sorted(_DEBT_SERVICE_CATEGORIES)),
            "debt_service",
        ),
        else_="external_spend",
    )


def _flow_class_totals(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The window's OUTFLOW split three ways, per currency.

    🔴 **Summed from the rows this answer returns, never from a second query.**
    A second read against a live store is taken at a different instant from the
    rows beside it, and a total that contradicts the rows under it is worse than
    no total at all -- a reader has no way to tell which of the two is wrong.
    Summed here, the three classes add to the window's total outflow by
    construction, and that identity is also the proof the classification
    PARTITIONS the rows rather than quietly dropping some.

    🔴 **Per currency, because the ruling on aggregates is that currency groups
    and never sums.** One integer spanning two currencies is not a wrong number,
    it is not a number.

    Every currency present carries all three keys, zero where a class did not
    appear: a zero is a real answer -- "nothing serviced a debt this window" --
    and a missing key would leave a reader unable to tell that from a class this
    tool forgot to compute.
    """
    totals: dict[str, dict[str, int]] = {}
    for row in rows:
        entry = totals.setdefault(
            str(row["currency"]),
            {f"{flow}_outflow_minor_units": 0 for flow in FLOW_CLASSES},
        )
        entry[f"{row['flow_class']}_outflow_minor_units"] += int(row["outflow_minor_units"])
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
    """
    if group_by not in GROUPINGS:
        raise BadGroupingError(f"group_by must be one of {', '.join(GROUPINGS)}, not {group_by!r}")
    problem = _readable(config)
    if problem is not None:
        # An aggregate is unpaginated by contract, bounded by the grouping
        # rather than by a row cap, so there is no truncation to report. The
        # totals block is present and EMPTY: this tool carries one, and an
        # unreadable store is a reason for it to hold nothing rather than a
        # reason for the key to vanish.
        return _unusable(
            config, problem, requested_window=(since, until), truncation=None, totals=[]
        )
    with reader_connection(config) as conn:
        # Annotated as the general expression type both branches produce: the
        # first assignment would otherwise fix the name to `coalesce` and the
        # account branch's plain column would not fit it.
        key: ColumnElement[Any]
        label: ColumnElement[Any]
        if group_by == "category":
            key = func.coalesce(
                transactions.c.category_override,
                transactions.c.source_category_primary,
                "UNCATEGORIZED",
            )
            label = key
        elif group_by == "merchant":
            key = func.coalesce(transactions.c.merchant_name, transactions.c.description, "UNKNOWN")
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
            # `posted_date` is stored as `YYYY-MM-DD` text that sorts as a date,
            # so the month is its first seven characters -- no date arithmetic,
            # and no dialect function to disagree about.
            key = func.substr(transactions.c.posted_date, 1, 7)
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
            )
            .select_from(transactions.join(accounts))
            # 🔴 The shared predicates and NOTHING else -- in particular no
            # direction filter. A `WHERE amount_minor < 0` here would leave an
            # inflow with no row to appear in at all, which is unreachable
            # rather than merely unaggregated. Direction is a COLUMN on the row,
            # so both halves are always answerable.
            .where(*_transaction_filters(since=since, until=until, account_id=None, after=None))
            .group_by("group_key", "group_label", transactions.c.currency, "flow_class")
            .order_by(func.sum(transactions.c.amount_minor))
        )
        rows = [
            {
                "group_key": str(r[0]),
                "group_label": str(r[1]),
                "currency": r[2],
                "flow_class": str(r[3]),
                "transactions": int(r[4]),
                "inflow_minor_units": int(r[5]),
                "outflow_minor_units": int(r[6]),
                "net_minor_units": int(r[7]),
            }
            for r in conn.execute(statement).all()
        ]
        return _answer(
            config,
            conn,
            rows,
            requested_window=(since, until),
            truncation=None,
            totals=_flow_class_totals(rows),
        )


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
            )
            .select_from(
                connections.join(institutions).outerjoin(
                    sync_state,
                    (sync_state.c.connection_id == connections.c.connection_id)
                    # 🔴 Filtered on domain, like every other read of this table.
                    # `sync_state` is keyed on (connection, domain) and balances
                    # and holdings advance on their own schedules -- so the day a
                    # second domain lands, an unfiltered join silently returns
                    # one health row per domain and a consumer counts each
                    # connection twice.
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
        conventions = {m.connection_id: m for m in signs.measure(conn)}
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
                "retired": r[7] is not None,
                "sign_convention": conventions[int(r[0])].verdict,
                "sign_convention_rows_judged": conventions[int(r[0])].rows_judged,
                "sign_convention_rows_positive": conventions[int(r[0])].rows_positive,
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
            extra_caveats=signs.caveats(conn),
        )
