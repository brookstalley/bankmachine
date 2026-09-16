# bankmachine

bankmachine pulls your own financial data from [Plaid](https://plaid.com) into an encrypted
datastore on your machine: accounts, balances, transactions and investment holdings. It then
serves that data read-only to an AI agent over [MCP](https://modelcontextprotocol.io). It runs
against Plaid's **sandbox**, which is free fake data, or **production**, which is your real
accounts. It never moves money, and it does no budgeting, categorization or advice.

**Status:** enrollment, sync and the MCP server work end to end. Eight of the nine specified MCP
tools are serving. Not built yet: scheduling (you run the sync yourself), the verification gate,
and file import. It is built for one person on one Mac.

## Getting started

### 1. Get a Plaid developer account

Sign up at [dashboard.plaid.com](https://dashboard.plaid.com). That gives you a client ID and a
sandbox secret, which is everything the Sandbox section needs. Production keys only exist once
Plaid approves production access for your account.

`enroll` links institutions through Plaid **Hosted Link**, so there is no redirect URI to
register. If enrollment fails with *"returned no hosted_link_url"*, Hosted Link is not enabled on
your Plaid account.

### 2. Install

You need [uv](https://docs.astral.sh/uv/). It fetches the pinned Python 3.14 for you.

```sh
git clone <your remote> && cd bankmachine
git config core.hooksPath .githooks   # per clone: turns on the pre-push guards
uv sync
```

The hooks line matters if you will ever push. A fresh clone has no guard against pushing
personal data until you run it. [`docs/README.md`](docs/README.md) covers tests and working on
the code.

## Sandbox: try it with Plaid's fake data

Declare the environment and your client ID once, in `~/.config/bankmachine/config.toml`:

```toml
environment = "sandbox"
plaid_client_id = "<your client id>"
```

Commands that write refuse to run until an environment is declared. Sandbox and production keep
separate datastores and keychain entries, and a guess could overwrite the wrong one.

```sh
uv run bankmachine connector set-secret    # paste the SANDBOX secret; it goes to the keychain
uv run bankmachine store init              # creates the encrypted sandbox datastore
uv run bankmachine connector check         # one authenticated call, to prove the credentials
uv run bankmachine enroll                  # prints a URL; link an institution in your browser
uv run bankmachine sync run --until-ready  # pulls accounts, balances, transactions, holdings
```

In the browser, pick a test institution that does not use OAuth, such as First Platypus Bank, and
log in with `user_good` / `pass_good`.

### Wire it to an MCP client

For Claude Code:

```sh
claude mcp add bankmachine-sandbox --env BANKMACHINE_ENVIRONMENT=sandbox \
  -- "$(which uv)" run --directory /absolute/path/to/bankmachine bankmachine mcp
```

- **The server runs the code that existed when the client connected.** After changing the code,
  restart the client session. Until then you are testing the old build.
- **`--directory` decides which checkout is served**, wherever the client is started from. Point
  it at the one you mean.

Then ask things like *"what did I spend last month?"* Every answer names the environment it came
from and carries warnings (`stale`, `gapped`, `partial` and others) when the data behind it is
incomplete. [`docs/connecting-an-mcp-client.md`](docs/connecting-an-mcp-client.md) covers Claude
Desktop, every tool, and how to read the warnings.

## Production: your real accounts

Read these before you connect anything real.

- 🔴 **Your financial data goes to your AI provider.** Every MCP tool call hands merchant names,
  amounts, balances and account names to the model your client uses. If that is a hosted model,
  your transactions go to that company under its terms. Check its training and retention settings
  first. bankmachine itself contacts nothing but Plaid's API, with no telemetry, so the MCP client
  is the one place your data leaves the machine. Verify that rather than trusting it:
  `tests/preferences/test_only_the_connector_reaches_the_network.py` enforces it and states its
  own limits.
- 🔴 **Two things cannot be undone.** The history window (`BANKMACHINE_HISTORY_DAYS`, default 730)
  is fixed for each institution when you link it, and institutions often grant less than you ask.
  The datastore key is the other: lose it, and the datastore and every backup are unreadable.
  If a keychain ever loses it, `uv run bankmachine store key import` puts your copy back.
- 🔴 **Tick every account when you link an institution.** An account left unticked never arrives,
  nothing reports it missing, and adding it later means re-linking. An institution that offers
  investments but not transactions does not appear in the list at all.
- **Nothing schedules the sync.** Each `sync run` also records that day's balances, so a day
  without a run is a day of balance history lost for good. Run it daily, by hand or from your own
  launchd job. Its exit codes: `0` done, `75` run again soon, `1` a connection needs you, `2` the
  run could not happen.
- **Transaction text is untrusted.** Descriptions and merchant names are written by whoever paid or
  charged you, and they land in your agent's context. Do not let an agent act on instructions it
  finds in one.
- **It cannot move money.** There is no write path to any bank, and no MCP tool changes anything.

### Switching to production

[`docs/first-production-connection.md`](docs/first-production-connection.md) is the full ordered
procedure, including what each step's failure looks like. In short:

1. **In the Plaid dashboard:** production access approved, `transactions` enabled (`investments`
   too, if you want holdings), and Hosted Link enabled.
2. **Rehearse in sandbox first:** key export and verify, then a backup and restore (§ 2 of the
   guide).
3. **Change the `environment` line** in `config.toml` to `"production"`. Edit the line; do not add
   a second one. For a one-off sandbox command, prefix it with `BANKMACHINE_ENVIRONMENT=sandbox`.
4. **Set up the production datastore, and back its key up before there is any data:**

   ```sh
   uv run bankmachine connector set-secret    # the PRODUCTION secret
   uv run bankmachine store init              # creates store.db and mints its key
   uv run bankmachine store key export        # put the key in a password manager
   uv run bankmachine store key verify        # paste it back: proves your copy is right
   uv run bankmachine connector check
   ```

5. **Enroll one institution, sync it and check what arrived.** Then add the rest, one at a time:

   ```sh
   uv run bankmachine enroll --timeout 1800   # a real bank login can take a while
   uv run bankmachine sync run --until-ready
   uv run bankmachine connections list
   ```

6. **Check the data against reality:** compare every balance with the institution's own app, and
   your own list of accounts with what arrived.
7. **Add a production MCP entry** with `BANKMACHINE_ENVIRONMENT=production`, named so you can tell
   it apart from the sandbox one.

### Every day after

```sh
uv run bankmachine sync run --until-ready
uv run bankmachine store backup ~/backups/bankmachine-$(date +%F).db
```

- **Use `store backup`, never `cp`.** Copying the file alone loses recent writes. Backups are
  encrypted, so they need the key.
- **Login expired** (`ITEM_LOGIN_REQUIRED`)? Run `uv run bankmachine connections reauth <id>`. Do
  not re-run `enroll`: that creates a second copy of every account and doubles your totals.
- **Before pulling an update that changes the schema**, follow the upgrade order in § 5.4 of the
  [production guide](docs/first-production-connection.md).

## Licence

[MIT](LICENSE).

It comes with no warranty, and that is not boilerplate here: this reads your real financial data,
and the checks that would tell you whether what it holds is complete and self-consistent are
[still being built](docs/system-requirements.md) — § 7 names them, and the verification gate is not
finished. Reconcile against your institution's own figures before you rely on any number this
produces.
