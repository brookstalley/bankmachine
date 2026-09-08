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

`bankmachine mcp` refuses to start against a datastore that does not exist, rather than creating an
empty one — an empty encrypted store would answer every question with zero.

## The tools

| Tool | Answers |
|---|---|
| `list_accounts` | every account with its latest recorded balance |
| `list_transactions` | transactions in a date window, newest first |
| `spending_by_category` | outflow per category in a window |
| `pipeline_health` | every connection, when it last synced, what is wrong |

## 🔴 Reading the answers

Every response carries `environment`, `as_of`, `coverage` and `warnings`. **Read the warnings before
drawing a conclusion**: an answer can be perfectly well-formed and still be computed over incomplete
data, and that is the failure this product exists to prevent.

- `stale` — a connection has not synced recently.
- `degraded` — a connection is failing; its data stops at the last successful run.
- `gapped` — the institution granted **less history than was asked for**, so older data is *absent
  rather than zero*. A sandbox connection typically grants 90 days against 730 requested.
- `partial` — something is not yet known, such as a granted window that has not been measured. 🔴 A
  null granted window means *not yet measured*, never *no shortfall*.

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
