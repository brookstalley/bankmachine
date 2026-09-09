# Connecting an MCP client

`bankmachine mcp` speaks JSON-RPC over stdin/stdout — the MCP **stdio** transport. It is read-only:
there are no mutation tools, and the refusal lives in the file handle (`mode=ro`) rather than in a
rule a future tool author has to remember.

## 🔴 Selecting sample data or real data

`BANKMACHINE_ENVIRONMENT` is the selector, and it is **not only a launch flag**. It already chooses
three separate things, so the two worlds cannot mix:

| | `sandbox` | `production` |
|---|---|---|
| Aggregator host | Plaid sandbox | Plaid production |
| Datastore file | `store-sandbox.db` | `store.db` |
| Keychain accounts | `datastore:sandbox`, `plaid:sandbox`, `connection:sandbox:<item>` | the `production` equivalents |

**A flag selects; the envelope confesses.** The failure a flag alone cannot prevent is not picking
the wrong one — it is *not knowing you did*. A server pointed at sandbox and one pointed at real
money return identically-shaped answers. So the environment rides every response (`"environment":
"sandbox"`) and the server's own title (`bankmachine (sandbox)`), which is what a client shows when
listing configured servers.

The asymmetry in the filenames is deliberate (AC-10.6): production is the *unsuffixed* default, so
syncing fixture data into the real datastore takes an explicit override rather than a forgotten
flag. Verify with `bankmachine store status`, which prints the path it resolved.

Configure both if you want both; they share no state.

## Configuration

The server is launched by the client as a subprocess. It needs the environment variables that
resolve its datastore — the same ones the CLI uses.

```json
{
  "mcpServers": {
    "bankmachine-sandbox": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/bankmachine", "bankmachine", "mcp"],
      "env": {
        "BANKMACHINE_ENVIRONMENT": "sandbox"
      }
    }
  }
}
```

Add a second entry with `"BANKMACHINE_ENVIRONMENT": "production"` when you have real connections.
Name them distinctly — the server's title carries the environment, but the key is what you will see
first.

## Before connecting

```sh
bankmachine store init          # once per environment
bankmachine connector set-secret
bankmachine enroll              # opens a hosted URL; complete it in a browser
bankmachine sync run            # fetches accounts and transactions
```

`bankmachine mcp` starts even without a datastore and reports that state (AC-ARCH.3); it never
creates one, because an empty encrypted store would answer every question with a confident zero.

## The tools

| Tool | Answers |
|---|---|
| `list_accounts` | every account with its latest recorded balance |
| `query_transactions` | transactions in a date window, newest first |
| `money_summary` | money in and out over a window, grouped by category, merchant, account, month or flow class — split by flow class under every grouping, and carrying the totals block described below |
| `get_pipeline_health` | every connection, when it last synced, what is wrong |
| `get_coverage_report` | per account: what data exists, and how long it has been silent |

🔴 **Five of the eight specified tools.** `balance_history`, `list_holdings` and `find_recurring`
are not built yet — the descope is recorded in `.prawduct/artifacts/api-contract.md`.

🔴 **The verification surface is now whole.** `get_pipeline_health` tells you whether the pipeline
is healthy; `get_coverage_report` tells you what data actually exists, per account. Nine of the
fourteen sandbox accounts have never had a transaction recorded, and before this pair an empty
answer about one of them was indistinguishable from a quiet month — so "am I paying down my
mortgage?" answered "no payments found", which looked honest and was false. Any answer touching
such an account now carries an `accounts_without_coverage` warning naming it.

The server also starts against a **missing or empty datastore** and reports that through
`get_pipeline_health` rather than refusing (AC-ARCH.3) — so a tool that returns nothing tells you
whether it found nothing or could read nothing.

## 🔴 Reading the answers

`build` says which code answered you. The server is a subprocess your client launches, so it runs
whatever existed at connect time — after changing the code, restart the client session and check
that `commit` moved. A null `commit` means the build could not be identified (not a checkout, or git
unavailable), and `dirty` is then null too, never false. `dirty: true` means the running code has
uncommitted changes, so the commit alone does not describe it.


Every response carries `environment`, `as_of`, `build`, `coverage`, `warnings` and `rows`. A
**windowed** tool also carries `effective_window`; a **capped** tool also carries `truncation`; a
**classifying** tool also carries `totals`. Absence of a key means that tool takes no window,
returns every row it finds, or does not classify the money it reports.

🔴 **`totals` is the one to read before quoting a spending figure.** `money_summary` splits every
row by `flow_class` — `external_spend`, `internal_transfer`, `debt_service` — and `totals` carries
the window's outflow under each, one entry per currency. Only `external_spend_outflow_minor_units`
is spending: a transfer between the holder's own accounts never left, and a card payment settles
purchases already counted under the categories they were spent in. The three sum to the window's
total outflow, which is how you check them against the rows. Over the sandbox store the raw total
is five times the money that actually went out the door.

🔴 **Each `totals` entry also states how much of itself has not settled.** `pending_transactions`
and `pending_net_minor_units` are authorisation holds — money claimed but not yet taken, which can
settle at a different figure or expire without settling at all, so a figure carrying them can change
with no new activity. `expired_holds` counts the ones that dropped off without ever posting, and
`settled_from_hold` the ones that became real transactions in this window; those two are what let
you tell a total that shrank because a hold expired from one that shrank because data is missing.
All six are always present and zero rather than absent.

**`get_coverage_report` rows carry `stranded_holds`** — holds still outstanding past any ordinary
authorisation lifetime, with `oldest_stranded_hold` naming the one to go look at (null when there
are none, and null for a closed account, whose holds nobody can clear). **`get_pipeline_health` rows
carry `sign_convention`** per connection, with the counts it was judged on. **Every account row
carries `lifecycle`** with the dates behind it, and `coverage` states `accounts_not_active` and what
those accounts contributed — totals **include** them, so quote that figure beside any net worth.

The server's own `instructions` are the authority on that list — a test holds them against the union
of every tool's live envelope and against the warning vocabulary, so they cannot fall behind the
wire; this page is a copy and can. **Read the warnings before drawing a conclusion**: an answer
can be perfectly well-formed and still be computed over incomplete data, and that is the failure this
product exists to prevent.

Each tool also publishes an `outputSchema`, which says *per tool* whether it carries a window or a
cap — so a client can tell "this tool has no window" from "this answer happens not to have one"
without calling it. A client that validates will reject a payload that has drifted from the schema,
which turns silent envelope drift into a loud failure.

## Reference material, without spending a tool call

The server also serves two MCP **resources**. A client reads them by URI, so the detail costs
nothing until it is wanted:

| URI | What it is |
|---|---|
| `bankmachine://reference/warnings` | every warning kind, what it implies about the answer carrying it, and what to do about it |
| `bankmachine://reference/envelope` | every envelope field and which tools carry it |

Both are generated from the code that produces the answers — the warning reference walks the
vocabulary itself, the envelope reference renders from the published schemas — so neither can quietly
fall behind the wire the way this page can.

## What the handshake tells you

`initialize` carries the build in `_meta` under `bankmachine/build`, so you can see which code you
connected to before calling anything. It negotiates a protocol revision from `2024-11-05` through
`2025-11-25`, honouring yours when it is one of those. And every tool declares `readOnlyHint`, which
is this surface's read-only guarantee stated where a client can actually read it — though the
guarantee itself lives in the `mode=ro` file handle, not in the annotation.

🔴 **`coverage.transactions` is always store-wide.** It does not narrow with your question, so a
windowed answer carries a sibling — `coverage.transactions_in_effective_window` — counted over the
window the answer actually covered. Read the sibling against a windowed question; reading
`coverage.transactions` there gives you the whole store's count for a question that asked about a
slice of it. The sibling is not narrowed by `account_id` either, so it is a fact about the *window*
rather than about your filters; `truncation.matching` is the one that answers "how many rows did my
whole request select".

🔴 **`truncation` is the one to check before you sum anything.** It carries `matching` (how many rows
the request selects), `returned` (how many came back) and `truncated`. When `truncated` is true the
rows are the **newest ones only**, so adding them up describes what came back rather than the window
you asked about — measurement found a two-year card total understated by roughly 40% that way, with
nothing in the payload saying so.

**To read the rest, page.** A truncated answer also carries `truncation.next_cursor`; hand it back as
`query_transactions`'s `cursor` argument, with the same window and account, and keep going until
`truncated` is false — the last page carries no `next_cursor`, which is the signal to stop. The
cursor is opaque: pass it back unchanged, never build or edit one, and never send one issued for a
different question, because both are refused rather than answered. Narrowing the window or raising
`limit` moves the cap; paging is what gets past it.

Warnings come in two scopes, and the difference is why they are worth reading. The first group
describes the **pipeline**, so it rides every response equally:

- `stale` — a connection has not synced recently.
- `degraded` — a connection is failing; its data stops at the last successful run.
- `gapped` — the institution granted **less history than was asked for**, so older data is *absent
  rather than zero*. How much less varies by institution and cannot be predicted: read
  `granted_history_days` against `requested_history_days` rather than assuming a figure. *(Measured
  2026-09-08: a sandbox connection to `ins_109511` granted 722 days against 730 requested.)*
- `partial` — something is not yet known, such as a granted window that has not been measured. 🔴 A
  null granted window means *not yet measured*, never *no shortfall*.
- `rule-applied` — an account rule filtered rows out of an aggregate, so the total excludes them on
  purpose.

The second group describes **this request**, and fires only when the request actually crosses the
boundary it names — so the *absence* of one is information too:

- `window_starts_before_coverage` — the window you asked for reaches back past the first covered
  date. Anything before it is *absent rather than zero*.
- `window_extends_past_coverage` — the window reaches past the covered end (today, or the last
  transaction when that is later).
- `rows_truncated` — the request matched more rows than the cap returned, and the answer holds only
  the newest of them. The detail says how many are missing and what to do about it.
- `counted_during_change` — a write landed between the row read and the count read, so the two
  describe moments a fraction apart. The rows are accurate as of the `as_of` stamp.
- `accounts_without_coverage` — an account in scope has **never** had a transaction recorded. Its
  empty result means *data not present*, never *no activity*; call `get_coverage_report` for the
  per-account picture.
- `account_no_longer_active` — an account in scope is closed, or its institution stopped listing it.
  Its balance froze on the date the row carries and is **not a fact about today**. Totals over
  balances *include* it and say by how much, so quote that magnitude beside the total — the reader
  can subtract it and you cannot.
- `includes_pending_rows` — some contributing rows are authorisation holds that have not settled, so
  the figure can change **with no new activity at all**. Quote settled and pending separately; never
  present their sum as money spent.
- `roster_observed_empty` — a contributing connection's roster was read **successfully** and listed
  no accounts at all. The call worked; it came back empty. Every account on that connection reads as
  `no_longer_reported` with a frozen balance, and two very different things produce that: the
  operator de-selected every account from sharing, or the feed broke in a way that returns success.
  Do **not** report it as accounts having closed — name the connection, say its roster came back
  empty, and call `get_pipeline_health` before drawing any conclusion about the household.
- `sign_convention_unverified` — a contributing connection was measured against the sign convention
  and its amounts run the wrong way, so on that feed income reads as spending. Name the connection
  and say its direction is in question; do **not** correct it yourself.

**Amounts are integer minor units** (cents for USD) and the field names say so — `amount_minor_units`,
`current_minor_units`. They are signed from the account holder's point of view: negative is money
out, positive is money in, so a credit card balance is negative.

## Example queries

Once connected, these are answerable directly:

- "What did I spend on food last month?"
- "Which of my connections is stale?"
- "What's my current balance across all accounts?"
- "Show me every transaction over $100 since August."
- "Is any of this data incomplete?"
