# Change Log — bankmachine

<!-- Append new entries at the top. Each entry is a ## section.
     This file is separate from project-state.yaml to reduce merge conflicts
     when multiple branches add entries simultaneously.

     # Tagged entries

     This file is PROSE. Its body is what a reader — and a release note —
     actually gets. Two machine-read keys ride in a tag-line directly under
     the ## header, and `check-releasability` is the only thing that reads
     them:

         ## YYYY-MM-DD: title (vN.M.P)

         <!-- prawduct: scope=v1.4 | release=v1.3.18 -->

         **Why:** ...

     Recognized keys:
       scope    - rollup identifier (e.g., v1.4), matching the `scope:`
                  frontmatter of the build plan that governs the work.
       release  - the version that carried this entry. Its ABSENCE is what
                  marks the entry release-pending, so write NO release= on
                  the feature branch and add it at release. Any value at all
                  — including a placeholder naming the absence, e.g.
                  `release=unreleased` — drops the whole scope out of the
                  release-pending set and silently unships the work.

     Nothing else is read. `chunks=` and `status=` were retired along with the
     derived views they fed; entries in older logs still carry them and are
     parsed as inert — leave them. Which chunks an entry shipped belongs in
     the entry BODY, where release notes and readers actually find it: a
     deliverable omitted from the body ships invisibly, and no tag ever
     caught that either. -->

## 2026-09-08: The exception stops crossing the boundary, and a misspelled bound stops lying

<!-- prawduct: scope=sync-v1 -->

**Why:** the Critic found the fix above had left the leak it named. The change-log entry called out
a `StatementError` carrying the SELECT as the defect, and the new test pinned `"SELECT" not in
message` — but only on the date path. Every other failure still rendered
`f"{type(exc).__name__}: {exc}"` onto `isError`.

**What changed:**

- 🔴 **No exception crosses the boundary.** `api-contract.md` § Error Model: no stack traces, no
  internal identifiers. A SQLAlchemy error stringifies to the failing SELECT *and its bound
  parameters* — the schema and the operator's own money, handed to whatever is reading. The detail
  goes to the log, where redaction applies; the caller gets a stable code and a remedy sentence,
  which is what that section specifies and what nothing implemented.
- 🔴 **A test asserted the forbidden behaviour**, again:
  `test_a_failing_tool_reports_an_error_without_closing_the_session` asserted the raw exception
  message reached the client. Rewritten against the contract, and its fixture now raises a message
  containing `SELECT` so the assertion has something real to catch. Second instance in one day of a
  test pinning a defect; the first was AC-3.2's capability read.
- **`additionalProperties: False` is advertised on all four tools and was enforced on none.** A
  misspelled `sinceX` was silently dropped and `spending_summary` returned the ALL-TIME aggregate —
  byte-identical to the windowed answer the caller thought it had asked for. Unknown keys are now
  refused, naming the offending key and what the tool accepts, with the permitted set read back off
  `_tool_definitions()` rather than restated.
- **`except KeyError` wrapped the handler call**, so a `KeyError` from anywhere beneath the query
  layer was answered `no tool named 'spending_summary'` as JSON-RPC -32601 — a false statement about
  a tool that exists. The tool name is resolved before the call now.
- Tool errors carry a stable code: `invalid_argument` is worth retrying with a corrected call,
  `internal_error` is not. A consumer could not previously tell those apart.
- `limit` was narrowed eagerly for all four tools, ahead of the unknown-tool check, with an
  unreachable fallback. Moved to where it is used.

**Verified through the live MCP channel**, which is what found the original defect and what the
recorded suite evidence could not speak to: an unknown key returns `invalid_argument` naming it, and
a real window returns rows.

## 2026-09-08: Every windowed question was unanswerable, and the checker was told to say so

<!-- prawduct: scope=sync-v1 -->

**Why:** `spending_summary` and `query_transactions` failed on *any* `since` or `until` with
`StatementError: (TemporalError) a calendar date must be a date, got str`. That is every question
the MCP server exists to answer. Found by a second session driving the tools; reproduced here
before anything was changed.

**What changed:**

- **Dates are parsed at the MCP boundary.** JSON has no date type, so `since`/`until` arrive as
  text and were passed straight to a `CalendarDate` column that refuses anything but a `date`.
  🔴 The `inputSchema` was never wrong — it advertises a string, and a string is what arrives; the
  gap was purely the missing narrowing at dispatch.
- **`query.list_transactions` and `spending_by_category` were annotated `since: str | None`** while
  their bodies required a `date`. The annotation was the lie that made the call site look correct.
  They take `date | None` now.
- 🔴 **`_dispatch_tool`'s argument bag is `dict[str, object]`, not `dict[str, Any]`.** This is the
  mechanism, not tidiness: under `Any` every JSON value flows into the query layer unchallenged and
  mypy strict is silent, which is exactly how this shipped. Typed `object`, an unnarrowed value
  cannot be passed at all — and the checker immediately found the same latent defect in `limit`
  (`int()` on whatever arrived) and `account_id` (forwarded unchecked). Both are narrowed now, with
  `bool` refused for `limit` because a JSON `true` is an `int` in Python and would have meant 1.
- **A malformed date now reads as a sentence**: `since must be a calendar date in YYYY-MM-DD form,
  got 'August 2024'` on `isError`, where a model can correct itself — not a `StatementError`
  carrying a SELECT. Deliberately not a JSON-RPC error code: the schema declares a *string* and
  "August 2024" is one, so this is the tool reporting on its input rather than a protocol violation.
- **Ten tests where there were none.** No existing test called either tool with a date at all —
  every one passed `{}`. The window tests assert *discrimination* (a window that excludes the data
  returns empty while one that includes it returns rows), because a parse that silently produced
  the wrong date would satisfy "it did not error". Two go-red cases (114 total).

🔴 **The positive control earned its place.** The mypy-snippet test named its files `narrowed.py`
and `unnarrowed.py` — and `"narrowed.py" in line` matches both, so the control matched the negative
file. Same containment trap recorded in `learnings.md` hours earlier, third instance in one day.
The files are renamed so neither contains the other; a cleverer match would have left the trap.

**Also corrected:** the mypy test pins `query.py`'s signature, not `_dispatch_tool`'s bag — widening
the bag back to `Any` leaves it green, because the snippets declare their own signature. The
annotation is pinned by its own test, and the docstring no longer claims otherwise.

## 2026-09-08: Capabilities are both product lists, and AC-3.2 stops inverting

<!-- prawduct: scope=sync-v1 -->

**Why:** the first live enrollment against a second sandbox institution printed
`capabilities: balance` for a connection that was, at that moment, syncing transactions and
holding a 401k. Running the product found it; 547 tests did not.

**What changed:**

- `capabilities_of` reads the **union** of `products` and `available_products`. The aggregator
  documents `available_products` as *"products available for the Item that have not yet been
  accessed"*, mutually exclusive with `billed_products` — so a product already initialized is
  guaranteed absent from it, and reading it alone recorded a connection as incapable of exactly
  what it was doing. AC-3.2 pulls investments for any connection whose recorded capabilities
  include investments, so the criterion was inverted for precisely the connections that have
  investments. Measured against the archived `/item/get` body: `["balance"]` before,
  `["balance", "investments", "transactions"]` after.
- 🔴 **A test asserted the defect and had to be corrected, not satisfied.**
  `test_capabilities_answer_what_the_connection_could_do_not_what_we_asked_for` asserted
  `"transactions" not in capabilities` as its tell that the wrong field had been read. The
  guarantee it was protecting is real — a read of `products` alone discovers nothing — but the
  assertion it chose contradicted the AC-3.2 it cited. It now asserts the union, which catches a
  wrong read from *both* sides where it previously caught one, and the go-red case that guards it
  was re-pointed accordingly. This is a strengthening; it is recorded here because "the test was
  failing so I changed it" is the shape a genuine weakening also has.
- `capabilities_of` had **no direct unit test** — it was reached only through enrollment. Four now
  cover it: a product initialized-only, a product available-only, the union, and a refusal naming
  which list the aggregator withheld. A new go-red case (111 total) breaks the union in the
  direction the old one could not see.
- The history-shortfall line read `a 8-day gap`. Reworded to `a gap of N days`, which is correct
  for every number rather than for the ones nobody had hit yet.
- `docs/connecting-an-mcp-client.md` claimed a sandbox connection "typically grants 90 days
  against 730 requested". A live one granted **722**. The doc now says the grant varies and cannot
  be predicted, and cites the measurement with its date and institution.
- 🔴 **VRF-004 step 2 was written around that 90-day figure** and could not be run as specified.
  Rewritten to ask about a period *outside* the granted window — the sharp form, which works at any
  grant size, where an 8-day shortfall is far too small to probe whether a model reads warnings.

**Deliberately not done:** the already-enrolled sandbox connection keeps `["balance"]`.
`connections` carries no `derivation_version_id` — capabilities are written at enrollment, not
derived — so no rebuild recomputes it, and nothing reads the column until build step 5. There are
no production connections. A migration path here would be backwards compatibility for a deployment
that does not exist; re-enrolling the sandbox connection corrects it for free.

## 2026-09-08: Transaction sync, and the first MCP slice — the product can be asked questions

<!-- prawduct: scope=sync-v1 -->

**Why:** build step 4, and the first slice of step 7. Enrollment gave the product a connection;
this gives it data, and a way to be asked about it.

**What shipped.** `bankmachine sync run` pages each connection's changes since its cursor, applying
additions, modifications and removals, and `bankmachine mcp` serves the result to an MCP client over
stdio, read-only. Verified end to end against Plaid's sandbox rather than only fixtures: 14
accounts, 50 transactions, 14 daily balances, and real aggregates answering real questions.

🔴 **The probe reshaped the loop before a line of it was written.** A `NOT_READY` reply carries
`has_more: false` *and* an empty cursor, so the obvious `while has_more:` terminates on the first
sync of every new connection and records a **successful run with zero transactions**. Nothing
raises; `last_success_at` gets stamped; the account reports no activity. That is a successful
response computed over data that has not materialized — this product's named primary failure mode —
and the trap is that the naive loop satisfies AC-2.1's *"loop until the source reports no more
pages"* literally while being wrong. The status is read before `has_more`.

**The cursor commits with the rows because it is derived from the same body**, not because a caller
remembered to wrap them. AC-2.1 and AC-2.5 are one requirement stated twice, and every
implementation satisfies or violates both at that seam.

🔴 **AC-11.8 earned itself on the first real sync.** The connection requested 730 days of history
and the aggregator granted **90** — a 640-day gap, reported rather than the returned window being
read as complete. It is measured only at `HISTORICAL_UPDATE_COMPLETE`, because at
`INITIAL_UPDATE_COMPLETE` the backfill is still arriving and the number would be plausible, well
formed and wrong.

**Every MCP answer carries its own caveats and its own environment.** `query.Answer` cannot be
constructed without warnings — they are computed in the same call as the rows — because the consumer
is an agent that cannot see a caveat which is not in the payload. And a flag selects while the
envelope confesses: a server pointed at sandbox and one pointed at real money are otherwise
identical in their output, so the environment rides every response and the server's own title.

**No `mcp` dependency**, and that is a decision (`api-notes-plaid.md` §18). The official SDK resolves
to 29 packages including uvicorn, starlette and httpx2 — an HTTP server *and* client stack — against
five direct dependencies and a ratified norm that the aggregator is the only network destination.
That norm's own recorded limit is that the import scan cannot see a dependency phoning home, so
adopting the SDK would load the mechanism exactly where it is weakest, for transports this server
does not use. The wire format was read from the SDK's own types instead.

**Four of the ten specified tools ship**, recorded as a dated descope rather than left to be
discovered by comparing a contract claiming ten against code answering four.

**What this cost, and the pattern in it.** Five review rounds. The recurring defect was not in the
code but in the checks: **six times a test asserted something it did not exercise.** A fixture that
never reached the guard it named; a race test that blinded the check it was testing; a
credential-absence assertion reading a capture that collected nothing; the go-red harness itself,
which counted a mutation that did not parse as caught, hiding two always-passing cases; a
cursor-atomicity claim that held positionally, so turning `ROLLBACK` into `COMMIT` left the suite
green; and a test whose docstring said *"the server refused to start"* which never called the
function that refused. The last one guarded a requirement I had implemented **backwards** —
AC-ARCH.3 says the MCP server starts against a missing datastore and reports it, and I made it
refuse, then documented the refusal as a feature.

**And the bug no test found at all**: `parse_float=str` keeps decimals as text but leaves JSON
*integers* as `int`, so a whole-dollar amount arrives as `500`. Every fixture used a fractional
amount. 505 tests passed while the first real sandbox sync refused every whole-dollar transaction it
fetched. Running the product is not a formality.

**Verification queued rather than claimed:** VRF-004 — every tool is tested and the warnings are in
every payload, but no test can say whether an *agent* reads them.

## 2026-09-07: Enrollment — one institution linked, and the states that exist after the token is spent

<!-- prawduct: scope=enrollment-v1 -->

**Why:** build step 3. The product could reach the aggregator and archive what it said, but it could
not link an institution, so every table below `connections` had nothing to hang from.

**What shipped.** `bankmachine enroll` prints a hosted enrollment URL, waits while the operator
completes Link in a browser, and polls for the result — no local web server and no frontend, which
is what AC-1.1 asks for. `bankmachine connections list` and `connections retire` came with it rather
than after it, because the cap refusal has to name a command that exists. The connection cap is
configuration (AC-1.5), retirement keeps every row the connection produced (AC-1.6), and
re-enrolling converges on one live connection per institution (AC-1.4).

**The window is confirmed before the exchange**, because that is the last moment AC-1.2 is
reversible. The required argument on `link_token_create` stops a caller *forgetting* a window;
nothing but a human reading it stops one sending the wrong window, and the result is immutable for
the life of the connection. The window prints before the URL, since an operator who has already
opened the browser has stopped reading the terminal.

**AC-1.3 was split, and the schema had been right all along.** It required enrollment to record "the
history window actually granted", and no response in the enrollment path carries that value —
verified against the pinned SDK at all three candidates. `core_schema.py` had carried a comment
saying so since build step 1, so the DDL and the requirement had disagreed from the day both
existed. **AC-1.3a** homes the granted window where it is first knowable, at the initial backfill,
and gives null an explicit meaning: *not yet known*, never *no shortfall*.

**Retirement is two-sided.** Setting `retired_at` frees a slot in this product's own cap and does
nothing at the aggregator, where the Item keeps counting against the plan and keeps billing. So
retiring calls `/item/remove`, and so does a re-enrollment — Link mints a *new* Item, and
overwriting `source_connection_id` would otherwise drop the only reference to the old one while this
side showed one tidy connection and reported success. AC-1.4 says re-enrolling updates rather than
duplicates; without that call the duplication merely moves to the far end, where nothing here can
see it.

**The post-exchange window is the part worth remembering.** Past the exchange the aggregator holds an
Item the operator is billed for, and any local failure leaves them paying for a connection nothing
here records. The access token reaches the keychain before the first write, so every failure in that
window can name the item and the credential; the cap race *releases* the Item it just minted, because
refusing to record a connection while leaving the operator billed for it is not a refusal but a charge.

**What this cost, and what it taught.** Four review rounds. Every substantive defect had one shape —
an Item that exists at the aggregator, spent and billable, invisible from here — and once that harm
was named precisely, three more instances of it were findable by looking for the harm rather than for
bugs. Four separate checks turned out to be claims: a test whose fixture never reached the guard it
named, a race test that blinded the very check it was testing, a credential-absence assertion reading
a log capture that collected nothing, and — worst — the go-red harness itself, which judged "caught"
by pytest's exit code and so counted a mutation that did not even parse. That last one had two
always-passing cases hiding behind it, one of which had been green since the day it was written.

**Verification queued rather than claimed:** VRF-003 — a Hosted Link session cannot be completed
programmatically, so whether the printed page reads unambiguously to someone about to make an
irreversible choice is a human's judgement, not a test's.

## 2026-09-07: What the cumulative review changed about the derivers

<!-- prawduct: scope=connector-v1 -->

**Why:** the bundle review of build step 2 found one blocking defect and two design errors, and
all three were mine to have caught.

🔴 **A day's balance was being overwritten, and three ratified records say it is rejected.**
AC-3.1, `data-model.md` and the DDL comment above `balances_daily` all say the same thing: a
second capture on a day already recorded is *rejected*, so the series does not depend on what time
of day anyone happened to look. My deriver upserted, and the reinterpretation that justified it —
"AC-3.1 is about the series" — lived only in a comment in the deriver itself. **That is a
normative change, and a comment is not where one gets made.** The rule now conforms: the first
capture for a day wins, decided by comparing captures rather than by arriving first, so a replay
in any order lands on the same row. AC-2.4 was measurably breached too — re-deriving rewrote
`raw_response_id` and `captured_at`, and the idempotence test compared only the two tables where
it held. The sharpest case had no test at all: a manual-import row was overwritten into an
aggregator row, which the *next* rebuild deletes, so an operator's hand-entered balance would
vanish one rebuild later with nothing connecting the loss to the sync that caused it.

🔴 **The institutions deriver was writing a catalogue page into the roster.** `/institutions/get`
serves the aggregator's *production* catalogue — 10,083 US institutions with real names and
routing numbers — and `institutions` is the table `connections` hangs off. After a
`connector check` and a rebuild, the roster would hold banks the operator never linked, with
nothing to tell them apart and nothing that removes them, because a rebuild never empties that
table. The institution now comes from `/item/get`, which carries exactly the one this connection
belongs to. `/institutions/get` is registered as deriving **nothing**, by name: "archived and
implies no rows" is a real answer, and left unregistered it is indistinguishable from the endpoint
nobody got round to.

**`/item/get` was archivable with no deriver**, which would have made every later rebuild refuse
the whole archive — on a row that cannot be removed. The test named for that check asserted a
hand-written pair, which is why adding the endpoint did not turn it red; it now derives the
expected set from the endpoints that can produce a `FetchedResponse`, so the next one fails in the
commit that adds it.

**Two more where a shape held for a reason narrower than the claim above it.** The retry channel's
safety argument — nothing inside it writes — is about the *local* side, and an exchange spends a
single-use token at the far end; `Endpoint.retry_safe` now carries that. "The connector persists
nothing" stopped being true when the derivers landed: a deriver writes rows through a handle the
caller owns. The property that actually holds, and the one AC-5.1 rests on, is that nothing under
`connector/` can *obtain* a handle — narrower, and true.

**Smaller, and each one a thing that would have read as fine:** `_upsert_account` applied one
values dict to insert and update, so derivation permanently owned `balance_class`, which
`data-model.md` declares operator-correctable; the registry argument was optional everywhere with
a default that could only fail; `get_logger` doubled its own prefix, so every new log line read
`bankmachine.bankmachine.…`; the terminal failure — the one that ends a sync — was the one going
unlogged; and the credential guard's exemption read one line at a time, so a secret inside a
multi-line string was exempt on every line but the first. That last one is now answered with
`ast` rather than quote-counting, after the counting version started reporting its own tests.

**Three obligations this plan cannot discharge are written into it** rather than left to be
noticed: wiring `config.history_days` into enrollment, the granted window that is unobservable
until step 3's first real connection, and account retirement.

## 2026-09-07: Enrollment, the derivers, and the archive exemption made structural

<!-- prawduct: scope=connector-v1 -->

**Why:** build step 2's remaining half. Chunk 03 adds the calls enrollment will make —
`/link/token/create`, `/item/public_token/exchange`, `/item/get`, `/accounts/get` — and Chunk 04
registers the first derivers, so `store rebuild` runs end-to-end over an archive of real responses
instead of refusing one for want of a deriver. Build step 2 is complete.

**Three things are held by construction here, because each is a mistake nobody notices making.**

- 🔴 **The history window is a required argument with no default.** AC-1.2 makes it immutable after
  enrollment and the requirements call a vendor-default build a failed build, so forgetting it is a
  type error — and the test runs mypy, because asserting at runtime that a window was passed tests
  the call site in front of it rather than the property that no call site can omit it. The maximum,
  730, is read off the SDK's own request validation and a test compares the two, since
  `plaid-python` is not pinned and the number can move under us.
- 🔴 **A credential-bearing response cannot become an archivable one.**
  `Endpoint.issues_credential` marks the two endpoints whose body carries a credential, and
  `FetchedResponse` refuses to exist for such an endpoint — so there is no object to hand the
  archive. A list of exempt paths beside the archive would be an enumeration standing in for a
  property, and this project has been burned once already by a rule matching a name where it meant a
  relationship. `raw_responses` is append-only, so a token written there is written permanently and
  travels with every backup.
- 🔴 **A call the far end cannot absorb twice is never retried.** The Critic caught this: the retry
  channel's safety argument — that the connector persists nothing, so a second attempt has no
  partial write to interleave against — is about the *local* side only. An exchange spends a
  single-use public token and mints a durable Item at the aggregator, so a retry after a
  transport failure either fails on a spent token or enrolls twice. `retry_safe` is now a property
  of the endpoint, with a control proving reads still retry.

**Capabilities read `available_products`, not `products`.** AC-3.2 pulls investments for any
connection whose capabilities include them and never for a named institution — and `products`
answers "what did we already ask for". A discovery reading it would report back this product's own
request, discover nothing, and pass every test asserting that discovery happened. Measured against a
live item: `products` is `['transactions']`, `available_products` has fourteen entries.

**The derivers, and the one place this build rounds.** Institutions and accounts converge on their
natural keys rather than inserting, because a rebuild never empties them — their local ids are what
every row of history references (AC-6.3), and reassigning them would orphan it. `first_seen_at` is a
minimum and `last_seen_at` a maximum, so replay order cannot change the result, which is what makes
the order-independence property a property of the arithmetic rather than of today's `ORDER BY`.

Money never touches a float: bodies are parsed with `parse_float=str`, so an amount arrives as the
digits the aggregator sent. The exception is deliberate and was the owner's call. Plaid's own
sandbox institution returns a 401k balance of `23631.9805` USD — canned data, so the aggregator is
deliberately exercising the case and sub-cent valuations are a production shape. An investment
`current` is price times quantity, computed rather than transacted, and no brokerage statement
reports hundredths of a cent, so it is rounded **half-even** (half-up would bias a portfolio upward a
fraction of a cent at a time, forever), **logged every time**, and the archive keeps the exact
original. The distinction that makes this legitimate — a *valuation* is not a *ledger amount* — is
recorded, not assumed.

**The sign convention now has the mechanism it was ratified in-transition without** (issue #9). A
liability's balance is stored negative whatever sign the source used; several aggregators report a
card balance as a positive amount owed, and a consumer taking that at face value is wrong by twice
the debt, silently. `available_minor` and `limit_minor` are the documented exceptions and keep their
magnitudes — asserted explicitly, or a later change that signed every column alike would look like a
tidy-up and pass.

**A guard caught its own author, twice.** AC-10.2's credential scan fired on this work's test data,
which was right and the test data changed. It also fired on a keyword argument forwarding a
same-named variable in the product code — ordinary Python, and the shape that gets a guard narrowed
in irritation later — so the exemption is now principled: in Python source an unquoted bare
identifier is a reference, never a literal. The Critic then found the hole in *that*: an unquoted identifier inside
a comment or a docstring is text, and text is where a secret gets parked "temporarily". Four edges,
a control on each.

## 2026-09-07: The connector's error taxonomy, and the retry channel it feeds

<!-- prawduct: scope=connector-v1 -->

**Why:** FR-4 is written in four connection-health states — auth-required, locked,
institution-down, rate-limit — and until this chunk every aggregator failure reached the
caller as one `ConnectorError` carrying a sentence. A sync loop cannot honour AC-4.1's
"one broken connection never aborts another" against a single type, and cannot record
AC-4.2's error code or compute AC-4.5's data hole from a message.

**What shipped.** `connector/plaid/errors.py` maps the aggregator's vocabulary onto local
types defined in `connector/__init__.py` — outside the `plaid` subpackage, so catching an
aggregator failure never requires importing the aggregator. The types are organized by
**what the caller must do next** rather than by what the aggregator called it: Plaid's own
`ITEM_ERROR` spans re-link, go-to-your-bank and nothing-to-sync, and a consumer switching on
it would send the operator somewhere that cannot help them. Every failure carries its
connection, its code, its request id and the instant it happened. Alongside it,
backoff-and-retry on the one channel `architecture.md` permits it on (AC-2.6), with the
clock injected so the suite exercises the real schedule at full speed.

**Three decisions worth reading.**

- **Retryability is a property of each error type, never a list in the retry loop.** A list
  goes stale the moment someone adds a type without visiting that file, and it fails in both
  directions: an un-retried transient stops the nightly sync, a retried permanent one hammers
  the aggregator with a call that cannot work. `retryable` has no base-class default, and
  `__init_subclass__` refuses at *class creation* a type that never decided. The first version
  of this used a test walking `__subclasses__()`; the Critic pointed out that walk sees only
  modules that have been imported, so it would have guaranteed something about the types one
  test file happens to import — and the case it misses is the second aggregator this package
  is explicitly shaped for. That test is consolidated away, because the class-creation guard
  makes it a check that can no longer fail.
- **`ITEM_ERROR` is deliberately absent from the classification's coarse layer.** Mapping it
  would be confidently wrong two times in three, and a wrong remedy is worse than a refusal.
- **An unrecognized code gets its own type, not a neighbour's.** Filing an unknown refusal
  under "probably transient" hides a connection that will never recover; under "probably
  terminal" it retires one that only needed a retry. Codes this build *recognizes* but has no
  FR-4 class for say so in as many words, which is the difference between a gap someone chose
  and a gap nobody noticed.

**A dependency bug, contained narrowly.** `plaid-python` 44.0.0's `api_client.py` calls
`e.body.decode('utf-8')` on a body it just set to `None`, so one class of SSL failure leaves
the SDK as `AttributeError` rather than as anything catchable. It is caught here and
**re-raised untouched unless the SDK's own exception is standing behind it** — catching
`AttributeError` around a call would swallow every genuine typo in the module and report it
as a network problem, so the narrowing has its own negative control. Getting there took two
probes that disagreed with each other and with the source reading; `api-notes-plaid.md` §9
records which SSL failures actually reach which path.

**What the Critic caught, and it is the same shape twice.** A `Retry-After` header was
honoured as a floor with no ceiling, so the aggregator could ask this product to sleep for an
hour inside one nightly sync — bypassing `max_delay_seconds`, the field whose whole job is
bounding the wait — and `float("inf")` passed the only guard on it, which `time.sleep` turns
into an `OverflowError` past the boundary. Both are external input reaching a wait, and the
docstring one line above promised a bound the code did not enforce: **the prose was right and
the mechanism was weaker, so the mechanism was raised.** Separately, the response body was
read outside the exception mapping, so a reset connection escaped as a bare `OSError` from a
connector whose whole contract is that its failures are typed.

**Two amendments to the plan, both stated rather than absorbed.** The chunk's sandbox test
asked for `/sandbox/item/reset_login` to drive a real `ITEM_LOGIN_REQUIRED`; that needs an
enrolled Item, which needs Chunk 03's exchange call, so it moves there. What runs instead is
still live — invalid credentials reach the same host and come back with the real error shape,
and the taxonomy now tells a wrong secret from a malformed call against the real server, which
is the failure VRF-002 item 5 was written to catch. And the declared "error fixtures" now
exist and are recorded verbatim from real rejections, replacing three hand-written body shapes
that had drifted into two test modules; the offline suite builds every constructed body from
that one recorded shape, so a change in the aggregator's error body moves them all together.

## 2026-09-07: The strategy artifacts, and the two gaps writing them exposed

<!-- prawduct: scope=strategy-artifacts -->

**Why:** `/prawduct:doctor` reported the coverage chain stuck at layer 1 — six expected
strategy-class artifacts had never been created. They are now written, in the dependency
order the planning guide sets: data model, non-functional requirements, security model, API
contract, observability strategy, operational spec. This was **reconciliation, not
invention**: `docs/system-requirements.md` and `project-state.yaml` already held nearly all
of it, and what the artifacts add is the shape — each criterion sitting next to the decision
that motivated it and the code or test that discharges it. Each marks what is *built* versus
*specified*, because four of the six describe surfaces that do not exist yet.

**Writing them surfaced two real gaps, and both are closed here rather than filed.**

🔴 **AC-10.2 was never implemented.** The criterion asks for a test that greps a fresh
`git ls-files` for token-shaped strings; what existed was `check-no-personal-data.sh`, which
hunts *roster* tokens supplied by `deployment/`. **Neither subsumes the other** — a roster
name is not token-shaped, and a leaked access token names no institution, so a stray
credential matching no institution walked past every guard in the repository.
`tests/preferences/test_no_credentials_tracked.py` closes it: `git check-ignore` per AC-10.2
clause with a negative control that `.env.example` stays tracked, plus a shape scan for
access-token prefixes, 64-hex key runs, and labelled credentials with a real value.

Its one exemption is a **per-line declaration, not a file skip list**, and the distinction is
the design. A skip list exempts the *next* real secret to land in that file and nobody
decides anything; the marker `credential-shape: test vector` exempts one line and appears in
the diff of whoever adds it. It is used once, on the redaction test's own fixtures, which
must carry real credential shapes or they prove nothing. A test asserts the marker does not
spill onto neighbouring lines — **it caught exactly that bug while being written.**

🔴 **There was no backup at all**, against a datastore key that cannot be recovered once lost
and a `balances_daily` series no re-sync can rebuild. `bankmachine store backup` now writes a
verified, consistent, single-file encrypted copy.

**The measurements that shaped it, none of which came from documentation.** `VACUUM INTO`
from a read-role handle **fails** under `PRAGMA query_only=ON` (`SQLITE_READONLY`) *and
leaves a zero-byte file at the destination* — a file indistinguishable from a backup until
the day it is needed, which is the single most dangerous artifact this command could
produce. So the copy is taken from the writer factory, which is also the right answer for an
unrelated reason: it holds the exclusive lock, so consistency is a consequence of the lock
rather than of timing. The copy folds the WAL in — measured against a source holding a 2 MB
hot WAL, whose 300 rows all appear in the copy while a plain `cp store.db` is short of every
one of them. That comparison is a **negative control in the test**, so the command is known
to differ from `cp` rather than assumed to.

The tests also caught a defect in the first draft: `back_up` leaked a raw driver
`OperationalError` instead of a named `StoreError`, so the CLI would have printed a
traceback where every other failure in this repository prints a sentence.

**17 norms ratified** (`norm_registry_ratified: 2026-09-07`), homed in the `## Direction`
sections of the four artifacts that govern them, with pointer rows in
`project-preferences.md`. Sixteen are steady-state. **One is `in-transition` on purpose:**
the operator-POV sign convention, tracked by `#9`. Nothing tests it, and the connector that
must obey it is mid-build — and the failure it guards against has no symptom, since an
aggregator reporting a card balance as a positive amount owed makes a consumer wrong *by
twice the debt*, silently and plausibly. Ratifying it steady-state with no mechanism would
have been the aspirational failure the lifecycle exists to prevent.

Two norms bind the **not-yet-built** MCP surface — read-only, and freshness-plus-warnings on
every response. That follows this repo's own precedent: `architecture.md`'s four norms were
also born before their code, and the point is that step 7 is *built to* them rather than
discovering them.

Where a norm has no mechanism, the Enforcement row says `Critic` and names nothing. Two data
norms are recorded that way deliberately — the schema makes "never overwrite a source value"
and "never hard-delete" *possible* to obey, not *impossible* to break, and naming a
constraint that does not constrain would overstate the guarantee.

**AC-10.4 got a mechanism too:** `test_only_the_connector_reaches_the_network.py` asserts
nothing outside `connector/` imports a network transport — verified red by planting an
`httpx` import, then green. Its limit is recorded in the norm rather than left implied: it
cannot see a subprocess shelling out to `curl`, nor a dependency phoning home.

**What the cumulative Critic caught, and it was worth the round.** 0 blocking, 14 warnings,
8 notes across three reviewers, who converged independently on one theme: `store backup`
shipped ahead of its own governance. The fixes, in this same bundle:

🔴 **A test that could not fail.** `test_it_leaves_no_zero_byte_file_when_it_cannot_write`
chmod'd the parent to `0o500` and then asserted the destination did not exist — true
*before* `back_up` ran, in a directory nothing can create a file in. Worse, the guarantee
it claimed to check was **inherited from `VACUUM INTO`**, not enforced by this module,
whose own docstring records a measured failure that leaves a zero-byte file. Both halves
are fixed: the failure path now unlinks the destination itself, and the test reproduces
the real hazard in a *writable* directory by swapping the writer factory for the read-role
one. A negative control neutralizes the cleanup and confirms the driver genuinely does
leave debris — so the unlink is pinned rather than decorative. This is exactly the
vacuous-fixture failure the test-evidence prompt names, written by the same hand that
quoted it.

🔴 **The credential guard exempted its own file.** `if path == Path(__file__): continue` —
a file-level skip list, in the module whose docstring argues against file-level skip lists,
covering the one file where a credential-shaped literal looks normal to a reviewer. It now
scans itself and declares its own vectors per line, like any other file.

**`BackupDestinationExistsError` was raised for a missing parent directory** — a misnomer
that sends the operator looking for a file that is not there. Split into
`BackupDestinationUnusableError`; the remedies are opposite.

**`store backup` was graded `stable` on the day it was written**, against the inventory's
own criterion (*shipped and depended on*), while mid-build `connector` commands sat at
`experimental` — and the `Retention:` rule defers removal of a stable member to a major.
Now `experimental`, with the reasoning recorded.

**The append-only norm claimed `Test` for a mechanism that does not enforce it.** A
composite primary key rejects a duplicate INSERT but permits `UPDATE`, `DELETE` and upsert
— and AC-2.4's idempotency requirement is precisely what will tempt the sync writer toward
`ON CONFLICT DO UPDATE`. Recorded `Critic` with the partial structure named, matching the
discipline the same bundle applied to the source-overwrite and hard-delete norms. Claiming
`Test` would have had the janitor sweep read it as machine-checked, and the guard for the
one series no re-sync can rebuild would never have been written.

**Two coherence defects in the records themselves:** `project-state.yaml` asserted the new
artifacts declare no `## Direction` section thirty lines above a registry saying the 17
norms are homed in those sections; and `operational-spec.md` re-homed architecture.md's
implicit-creation norm while both files recorded that nothing was restated. One rule now
has one home — operational-spec keeps the backup half and cites architecture for the
datastore half. `architecture.md`'s canonical command table, which declares itself
canonical precisely so a command set is not restated in four places, has regained the three
commands it was missing.

Also added: the `store backup` CLI surface had no test at all (four now), and the backup
path wrote no log record, so an unattended failure left only an exit code.

**The verify pass then caught what the first round of fixes had left.** Three residuals,
all of the same shape — a claim with nothing behind it. `operational-spec.md` now states
as a *guarantee* that the failure message says which happened, so the message and the log
records are asserted (`match=` on the raise, `caplog` on both paths) rather than left as
prose a refactor can drop while staying green. The failure paths themselves were silent:
an unattended run left a "backup starting" line and then nothing, which reads exactly like
a run still in progress. And `README.md`'s "what exists today" paragraph still omitted
`store backup`, so the three command lists the canonical table exists to keep in agreement
were still disagreeing — the point of that table is that a command set restated in four
places is four places to disagree.

**One reviewer observation was wrong and is recorded as such rather than acted on.** It
reported that the R-13 disposition claimed here does not exist; running the command again
returned `supersedes disp:...:R-13:1`, so version 1 was on record all along. The manifest's
`prior_dispositions` evidently does not carry the full set. Checked rather than believed,
because a fix applied to a defect that is not there is a change with no reason.

**Accepted rather than fixed**, recorded as dispositions: the fourth copy of the AST import
scanner (extraction would edit three tests this bundle does not touch), and `data-model.md`
being a third uncompared description of the frozen DDL — a real drift risk that wants a
construction of its own, now tracked as `#11`. *(The absent parent requirement for
`store backup` was on this list until the PR review; it was fixed rather than accepted, and
leaving it here would have had the entry contradict itself two paragraphs later.)*

**The PR reviewer then caught the scope trace.** `store backup` had no parent
requirement anywhere: `docs/system-requirements.md` carries no backup criterion, and
`project-state.yaml`'s `scope.later` said *"Multi-machine or backup-restore automation"* —
which reads as deferring backup out of v1 entirely, in the bundle that ships it. The
capability was properly reached and consumed; only the trace was missing. `scope.v1` now
names the manual command and says why it is v1, and `later` is sharpened to the half that
genuinely is deferred: scheduling, retention, and a rehearsed restore.

It also measured a sentence in `pyproject.toml` that was simply false. The
`[tool.ruff.format]` rationale said *"`ruff check` still lints these files"* — it does not:
`ruff check` on a `.md` path reports "No Python files found" and lints nothing (confirmed
against the 0.16.6 this commit pins). The exclude is right and load-bearing; the sentence
explaining *why it is scoped to the formatter* was wrong, which is the sentence a reader
checks first. Replaced with the measured reason.

And three deferrals that existed only as prose are now filed — `#10` (schedule the backup,
rehearse the restore), `#11` (`data-model.md` is a third uncompared description of the
frozen DDL), `#12` (`verify_norms_go_red` does not know the three newest norms). The § Owed
table claimed its gaps were "filed rather than rediscovered" while the highest-value one had
no item; it now cites `#10`. `#11` and `#12` are not operational gaps and correctly do not
appear there.

**Still open, and named rather than quietly carried:** nothing *schedules* the backup, and
the key is still backed up by hand — the command cannot do that half without defeating the
keychain. Restore has no runbook and has not been rehearsed end to end by a human.

## 2026-09-06: VRF-002 discharged — the connector's live half, and what the sandbox really serves

<!-- prawduct: scope=connector-v1 -->

**Why:** Chunk 01 shipped unticked on purpose. Its success path had never been probed —
no sandbox credentials existed on this machine, so `tests/connector/fixtures/` was empty
and the two `sandbox`-marked tests skipped. Credentials arrived. This is what running the
gate produced, including the part the gate got wrong about itself.

**Chunk 01 is now `[x]`.** `bankmachine connector check` completed against the real
sandbox, archived 677 bytes as `raw_response 1`, and reported the aggregator's own `total`
of 10,085 institutions rather than the single record on the page.
`BANKMACHINE_RECORD_FIXTURES=1 uv run pytest -m sandbox` recorded
`tests/connector/fixtures/institutions_get.json`, closing Done-when 0b — the last of the
plan's `verify-api` findings, and the only one that needed a credential to reach.

**What the live call established that the fake could not.** The SDK hands the bytes over
undecoded against a real server, not only against a stub: `_preload_content=False` behaves
in the wild the way `api_client.py`'s source said it would. That is the half of AC-5.1
`project-state.yaml` refuses to accept mocked, and it is now evidence rather than a
reading.

🔴 **The verification's own premise was wrong, and the correction outlives the item.**
VRF-002 item 7 asked the operator to confirm the recorded fixture held "sandbox
institutions only". The sandbox's `/institutions/get` serves no such thing — it serves the
production institution catalogue, real names and real routing numbers, 10,085 of them for
`US` alone. Two consequences, both recorded in `api-notes-plaid.md` §7. Institution shapes
recorded from sandbox *are* production shapes, so the connector plan's §4 risk — "sandbox
shapes are not production shapes" — is narrower than written for this endpoint, while
standing exactly as written for the accounts and transactions Chunk 04 also depends on.
And what makes a recorded fixture safe to commit is the leak guard, not the word
"sandbox": `check-no-personal-data.sh` reports clean over the working tree with the fixture
in it, which is the check that was actually run.

**A second error code, for free, from a mistake.** The production secret was set against
the sandbox host first. That returns `400 INVALID_API_KEYS: invalid client_id or secret
provided` — a different code from the `INVALID_FIELD` a *malformed* credential returns.
Chunk 02's taxonomy now has both from observation rather than from the docs, and the
distinction is one an operator acts on: rotate the credential, or fix the call. VRF-002
item 5 exists to catch exactly this being reported as a network fault, and it was not.

**Also in this bundle:** `.env.example` — the client id and the optional overrides, as a
file to `source` rather than one anything reads silently. It carries no secret and says so
in its own text: the aggregator secret has no environment variable by design, and
`connector set-secret` puts it in the keychain, per environment.

## 2026-09-06: the connector's walking skeleton — the product reaches the outside world

<!-- prawduct: scope=connector-v1 -->

**Why:** build step 2 is the aggregator client, and every later step reads through it.
Chunk 01 proves the whole path before widening it: configuration resolves, the keychain
yields a secret, the aggregator answers, and the answer lands in the archive verbatim.
It asks for the smallest thing the aggregator will tell anyone — one page of the
supported-institution list — because that needs client credentials and nothing else, so
the path is provable before enrollment exists.

**What shipped:** `bankmachine connector check`, which reports what it fetched and logs the
archived response id so the unattended job later has a record; `connector set-secret`, which
prompts without echoing at a terminal and reads a pipe when given one, so a secret reaches
neither the shell history nor a process listing; the `connector/` package with the aggregator
SDK confined to `connector/plaid/`; aggregator credentials added to the existing Credential
Seam rather than a second one; and the `Endpoint` vocabulary that `DERIVERS`, the
credential-archive rule and AC-ARCH.4's guard all turn out to need.

**The boundary decision, and why it is a package rather than an interface.**
`system-requirements.md` §9.2 — is a second aggregator ever expected — is answered: one in
v1, contained so a second is a new module rather than a rewrite. A client `Protocol` with a
single implementation would encode that implementation and call it a contract; the honest
version cannot be written until a second aggregator exists to disagree with the first. The
mechanism is `tests/preferences/test_connector_is_contained.py`, holding two properties:
nothing outside `connector/plaid/` imports the SDK, and nothing in `connector/` imports a
module that hands out a datastore handle. The second is the load-bearing one — AC-5.1's
"archive before normalize" is not a rule anyone follows here, because the connector has no
way to write at all.

🔴 **The response is taken undecoded, and this was the finding that shaped the client.**
The SDK deserializes into generated models by default, and those models silently drop
fields they do not know about — which are exactly the fields a later `store rebuild` would
need to reproduce rows the aggregator has since started sending. Handing the archive a
model round-trip would have satisfied AC-5.1's letter and destroyed its point. Every call
passes `_preload_content=False`, verified against the SDK's own source rather than its
documentation, and held red by `verify_norms_go_red.py`.

**Two things reading the code first caught that drafting from documentation would not.**
The credential-archive exemption was already decided in build step 1 — `store/raw.py` says
so, and names build step 2 as where it stops being a decision and becomes a mechanism — so
it was withdrawn from this plan's open assumptions as an inherited obligation rather than a
departure to be argued for. And `plaid-python` ships no `py.typed`, so everything it
returns is `Any`; strictness was not relaxed, the override is scoped to the SDK alone, and
the untyped surface stops at the module that converts to local types.

**A norm's detector was corrected, not weakened.** AC-ARCH.4's guard reads any string
opening with a separator as an absolute filesystem path, and `/institutions/get` is not
one. The fix is the relationship rather than an exemption: a literal declared as an
`Endpoint` is the aggregator's vocabulary, anything else is still a path. A per-file
allowlist was rejected — it would decay on the first module someone forgot to add — and the
new test asserts both directions, including that a `Path("/Users/...")` in an
endpoint-declaring module is still caught.

🔴 **A rejected call names its cause, which took the Critic to notice.**
`ApiException.reason` is the HTTP reason phrase, so wrong credentials, a malformed field and
an unsupported country all read `400: Bad Request` — leaving the operator no way to tell a
rotated secret from a bug in this code, and the wrong guess costs a credential rotation that
was never the problem. The cause is in the response body. Verified by probing the real
sandbox host with deliberately invalid credentials, which needs no valid ones:
`error_code=INVALID_FIELD`, `error_message='client_id must be a properly formatted,
non-empty string'`, plus the `request_id` that makes a failure traceable in the aggregator's
dashboard. Also mapped: an unreachable host, which the SDK wraps only for SSL errors and
otherwise lets escape as a raw `urllib3.MaxRetryError` — a traceback from a library the
operator never chose.

**Not done, and the chunk is not ticked because of it.** The *success* path has never been
probed: no sandbox credentials exist on this machine, so `tests/connector/fixtures/` is
empty and the two `sandbox`-marked tests skip. The offline suite proves the bytes pass
through a fake unaltered; only a live call proves the SDK hands them over undecoded against
a real server, and `project-state.yaml` is explicit that the aggregator is verified against
rather than mocked at the layer under test. The build plan's acceptance criteria were split
to say so rather than leaving a done-when nobody could meet. Queued as VRF-002 — and
discharged the same day, once credentials arrived; see the entry above.

## 2026-09-06: `sync shell` — the operator gets to look inside their own datastore

<!-- prawduct: scope=datastore-v1 -->

**Why:** page encryption breaks every ad-hoc SQL tool — stock `sqlite3` reads this file as corrupt,
because the pages are ciphertext. Until this command existed there was no way for the operator to
look at their own data at all, which is why AC-ARCH.6 puts it in build step 1 rather than step 9: it
is the debugging affordance every later step is built over. It is also the product's only surface
that runs operator-supplied SQL, so it is where both read-role norms stop being theoretical.

**What landed:**

- **`bankmachine sync shell`** — an authenticated SQL prompt over a read-role handle. Statements may
  span lines, `.tables` / `.schema` / `.help` / `.quit` are there, a failed statement costs the
  statement and never the session, and results render as aligned columns. A blob is summarised
  (`<blob, 402 bytes>`) rather than dumped: `raw_responses.body_gzip` is the one that comes up, and a
  terminal full of gzip is not a debugging affordance.
- **The refusal to write stays in the file handle.** The shell adds nothing of its own — it asks
  `store/connection.py` for a read-role handle, which is `mode=ro`. An operator can type `PRAGMA
  query_only = OFF`, watch the flag flip to `0`, and still be refused. The test asserts both halves,
  because asserting only the refusal would pass just as well against a shell where the PRAGMA
  silently did nothing.
- **The prompt holds no snapshot between statements.** A shell left open overnight is open during
  the nightly sync, and a read-role handle takes no writer lock, so nothing else serialises the two;
  a pinned snapshot starves the checkpointer for hours. The release is a *property* of the handle —
  it asks whether a transaction is open and rolls it back — rather than a list of statements to watch
  for, because `BEGIN` opens one, so does `SAVEPOINT`, and the next thing that does would not have
  been on the list. Three tests hold it, including a negative control that disables the release and
  confirms the checkpoint genuinely starves; a probe that only ever confirms what was expected is
  the one to distrust.
- **AC-10.3 has one rule, not one per surface.** Everything the shell writes goes through
  `logging_setup.redact`, the same function the log formatter uses — including the statement echoed
  back in a piped session, because a transcript is the likeliest thing here to be committed or
  pasted into a bug report. Redaction runs over text and not over numbers: money here is an INTEGER
  of minor units and an account number is TEXT, so redacting integers would blank a six-figure
  balance — the number the operator opened the shell to read — while protecting nothing.
- **No writer shell.** The plan left one optional and it is declined: the product is read-only, a
  writer shell would hold the exclusive `flock` for its whole session so the overnight prompt above
  would block the nightly sync outright rather than merely starve it, and hand-typed rows have no
  raw response behind them, which is what `store rebuild`'s content digest exists to catch.

**The carried edge was staged, and staging it found a real misdiagnosis.** Since Chunk 01 the
no-fallback clause has had one case with no staged test: a hot WAL from a killed writer, no `-shm`,
in a directory the reader cannot write to. Staged here, it turned out the guard could never have
fired — SQLite opens lazily, so `connect()` succeeds and the failure lands on the *first read*,
where `_key_and_prepare` reported `SQLITE_CANTOPEN` as a rejected key. That told the operator to
restore a keychain entry that was never the problem, for a datastore that only needed its WAL
checkpointed — the exact wrong-recovery failure `DatastoreKeyRejectedError` was introduced to
prevent. `_diagnose_first_read` now separates the two on `SQLITE_NOTADB`, and the case has a real
test instead of a stand-in for one.

**The store layer grew two exports rather than the CLI growing a driver import.** The shell needs to
know when a statement is complete and how to catch a failed one; both now come from
`store.connection` (`statement_is_complete`, `DriverError`). The structural test caught the import
on the first full run — worth recording, because the norm it protects is exactly the kind that
degrades into a convention the moment a second module imports a DBAPI.

**The cumulative review returned 0 blocking, and six of its findings were worth fixing anyway.**
Two were real defects rather than polish. The **datastore key validator** tested hex with
`int(key, 16)`, which is a parser and not a predicate: it accepts an `0x` prefix, `_` separators, a
sign and surrounding whitespace, so `"0x" + "a" * 62` is 64 characters and passed both checks —
and SQLCipher treats anything that is not exact hex as a *passphrase*, runs its KDF over it, and
gives a store that works until those defaults change. That is the precise silent substitution the
validator exists to prevent. It is now a full match on the hex alphabet, with the four accepted-by-
`int` shapes as cases. And **filesystem `OSError` had no mapping into `StoreError`**, so a lock file
the process cannot open escaped as a traceback — including out of `inspect()`, whose entire contract
is to report a state rather than raise on one.

The third was in this chunk's own output: **the log-tuned redaction rule was applied to schema
text**, where it is wrong. `_OPAQUE` blanks any 32-plus character run, and
`source_investment_transaction_id` is exactly 32, so `.schema` printed `[REDACTED]` where column and
index names belong — eight unreadable lines of the real schema. Schema text is now not a redaction
surface at all, and that is a property rather than an exemption: everything in `sqlite_master` here
is authored by this repo's migrations, and AC-6.6 with
`tests/preferences/test_no_provider_identity.py` is what makes it carry no operator data. Row values
keep the full rule, bare-length matching included, and the cost is recorded — a 64-hex digest is
blanked, and the join back to the archive is the integer `raw_response_id`, which is not.
`.schema` also matches `tbl_name` now, so a table's indexes come with it.

The rest: `architecture.md`'s canonical command table still advertised the writer shell this chunk
declined, which would have had a step-7 builder implement the refused flag; the README's status
stopped at Chunk 02; and `reader()`'s connect-time branch still stated the hot-WAL cause that Chunk
04 measured false, so two operator-facing texts described one failure and the less-reached one made
the disproved claim. Log rotation, unlogged run failures and archive retention are filed as #3, #4
and #5 rather than fixed here.

Suite green, mypy strict and ruff clean. The norm-break harness runs 29 cases, five of them new and
all verified red. The by-hand check AC-ARCH.6 asks for is recorded as VRF-001 in
`.prawduct/operator-verification.md` with its session transcript, and is the one item still awaiting
the owner's own eyes.

## 2026-09-06: Raw preservation and rebuild — a bronze layer that checks its own work

<!-- prawduct: scope=datastore-v1 -->

**Why:** FR-5's bronze/silver split turns a categorization bug into a re-run instead of a re-fetch,
and a re-fetch is often impossible — an aggregator's history window does not come back. It is built
now, with no aggregator to feed it, because a sync path written first would normalize straight into
the tables and retro-fitting raw preservation around it afterwards means rewriting the part that
already worked.

**What landed:**

- **`store/raw.py`** — every response persisted verbatim, compressed and hashed, before anything
  reads it (AC-5.1). The digest is over the *plaintext*, so it identifies the response independently
  of how it was compressed, and `load_response` recomputes it: a body that no longer matches what
  was recorded is refused rather than derived from. The archive is append-only — two identical
  responses at two times are two facts, and collapsing them would destroy the evidence that the
  source repeated itself.
- **`store/derivation.py`** — the seam build step 2 plugs into, shipped empty. One normalization,
  two callers: the sync path and `store rebuild` run the same derivers over the same responses, so
  rebuild is not a second implementation that has to be kept in step with the first. A deriver is a
  pure function of its response — `DerivationContext` carries no clock, because a `first_seen_at`
  stamped `now()` is the one mistake that makes a rebuild unreproducible.
- **Persist first, then derive, in two transactions.** A deriver that raises must not take the
  archive down with it: the response may be unfetchable afterwards, while the derivation can be
  re-run at any time. A crash mid-derive leaves the response kept and no half-derived rows.
- **`bankmachine store rebuild`** (AC-5.2) — one transaction under the exclusive writer lock:
  delete every row the archive can recreate, replay the whole archive in received order, and then
  **check its own work**. It hashes the datastore's content before and after and refuses to commit a
  rebuild that did not reproduce what it replaced, unless the derivation version changed (AC-5.3,
  AC-11.5). Without that refusal a rebuild is an irreversible bulk operation whose only failure
  signal is analysis quietly turning wrong weeks later.
- **What it deletes is derived, not listed.** A table is rebuildable when it holds a foreign key
  *pointing at* a raw response. The first draft matched on the column name, which put `raw_responses`
  itself — whose primary key is `raw_response_id` — first in the list of tables to empty before
  replaying them. A test caught it; the fix was a better predicate, not a longer exception list.
- **Rows nothing can recreate are never deleted.** Imported rows name a file rather than a response,
  and accounts carry the local ids every row of history points at (AC-6.3). Both survive a rebuild
  untouched, and the content digest covers them, so a rebuild that orphaned or renumbered anything
  fails its own check.
- **"Byte-identically" (AC-11.5), read deliberately:** the digest covers every column of every table
  except a table's own single-column integer primary key where nothing references it. Those are
  rowid allocations, not facts about the world — requiring `transaction_id` to come back identical
  would make the criterion a statement about SQLite's allocator. Every id that *is* a fact is
  covered.
- **The sole-constructor norm got sharper, not looser.** `engine.connect()` is a pool checkout over
  a handle `store/connection.py` already keyed and locked, but the AST scan matched any call named
  `connect`. Rather than exempt a file, the rule now says what it always meant: a role is decided by
  the parameters a handle is opened with, and a checkout carries none. `engine.py` gained
  `writer_connection` / `reader_connection` so nothing outside the store layer checks one out, and a
  positive control fails if that carve-out ever stops exempting anything real.
- **Norm 4 now covers writers too.** Only the reader refused a schema version this build does not
  recognize; `store rebuild` is the first writer that is not the migration runner, and a writer that
  misunderstands a schema writes wrong answers down rather than merely returning them. The check
  moved into one helper both roles call, and `initializing_writer` still skips it — bringing an old
  datastore forward is the one job that has to open a version this build does not serve.
- **The Chunk 02 ride-along is discharged: the index drift guard now compares what a partial
  predicate *says*, not whether one exists.** It read `sqlite_where is not None` against
  `PRAGMA index_list.partial`, so a condition inverted to `retired_at IS NOT NULL` kept every other
  property of the index intact while making it enforce the opposite rule. Both sides' text is
  normalized only for the qualifier, whitespace and case — never for meaning — and a positive
  control fails if the normalizer ever starts returning nothing.
- **Six more cases in `verify_norms_go_red.py`**, covering the body-integrity refusal, the
  table-classification rule, the reproducibility refusal, the checkout carve-out, the writer's
  schema refusal and an inverted index predicate. All 24 breaks go red.

**The Critic round returned no blocking findings and tightened two seam decisions**, both of which
would have landed on build step 2 rather than here:

- **A derived table is now either rebuildable or a dimension, from one property.** `securities`
  carries a `derivation_version_id` but no raw provenance, so "derived" was being reconstructed from
  two signals that disagreed on exactly one table. The first deriver to write a security would have
  hit the `source_security_id` unique index on replay, or upserted and left stale rows that the
  content digest then reports as an unreproducible rebuild — sending the next reader hunting a
  purity bug that is really a classification gap. A table is *derived* when it references
  `derivation_versions`; of those, the ones referencing `raw_responses` are rebuilt and the rest are
  dimensions a deriver must upsert. Fixing it turned up the same trap a second time:
  `derivation_versions` names its own primary key `derivation_version_id`, exactly as
  `raw_responses` names `raw_response_id`, so both classifications now go through one
  reference test.
- **The archive does not hold credentials.** AC-5.1 keeps every response verbatim and AC-10.1 keeps
  every access token in the keychain; a token in `body_gzip` satisfies the first by breaking the
  second, permanently, because the table is append-only and a datastore backup travels. Recorded as
  clause 7 of the Derivation Seam, where step 2 meets it, and it is what keeps Chunk 04's AC-10.3
  redaction from needing to cover a table nobody planned to redact. Each of these guarantees is a refusal, so each fails silently and in the direction of
  looking finished.

## 2026-09-06: The core schema — thirteen tables, with the requirements built into them

<!-- prawduct: scope=datastore-v1 -->

**Why:** the schema is the format every later consumer depends on, and it was being designed before
any of those consumers exist. Chunk 02 is the plan's lock-in chunk: the last point at which changing
it is free. The columns were not drawn from taste — the enumerated queries of the ten MCP tools in
`system-requirements.md` §5 were written down first, and the tables answer them.

**What landed:**

- **`store/types.py`** — the typed vocabulary the schema is written in. `MinorUnits`, `CalendarDate`
  and `UtcInstant` are distinct to mypy, with validating constructors, SQLAlchemy column types, and
  a `from_decimal_string` that scales the digit tuple so no amount is too large to convert exactly
  and no fraction is ever silently rounded away.
- **Migration 002** — the thirteen tables of FR-6 as frozen DDL, applied inside the one transaction
  the runner owns. Three requirement classes are enforced *by the database* rather than by the code
  that writes to it: `typeof(x) = 'integer'` on every monetary column (AC-6.2 — SQLite stores a
  float in an INTEGER column without complaint), format constraints separating calendar dates from
  UTC instants (AC-6.4), and a provenance CHECK so no row can claim an origin it has no link to
  (AC-7.4). Identity is partial unique indexes, so idempotency (AC-1.4, AC-7.5) and the
  append-only balance series (AC-3.1) are properties of the store rather than disciplines of its
  callers.
- **`store/schema.py`** — SQLAlchemy Core metadata for the same tables, written independently of the
  DDL and compared to it column by column on every run. Generating one from the other would have
  been fewer lines and would have made drift undetectable.
- **`boundary-patterns.md` populated** — the datastore schema as its first contract surface,
  carrying the five parts of the contract that no column name implies, and naming the MCP tool
  surface and aggregator client as boundaries that do not exist yet.
- **The sign convention, decided and written down once:** every stored amount is signed from the
  operator's point of view, liabilities included. Net worth is then a plain sum and AC-11.2's
  reconciliation needs no per-type special case. `accounts.balance_class` partitions a *report*,
  never an arithmetic sign.
- **`tests/preferences/verify_norms_go_red.py` extended to cover the schema** as well as the
  connection layer — every new structural guarantee was verified red with its mechanism broken. A
  constraint that has never refused anything is a claim, not a check.
- **`frozen` became a mechanism.** The DDL is rendered from two shared constraint idioms, so
  "this never changes" rested on nobody editing them — and migration 003 will want the same two.
  A recorded SHA-256 of the rendered statements, compared by a test, is what now stops a later
  migration from silently redefining what version 2 means for every datastore that already ran it.
- **An aggregator row must name the response it came from.** The provenance CHECK originally
  permitted a row with `source = 'aggregator'` and no `raw_response_id`, while the comment above it
  claimed exactly one link is always set. Tightened to match the claim, on all four normalized
  tables: a row whose answer to *where did this come from* is silence looks identical to one that
  can be traced, and Chunk 03's rebuild is written against this constraint.
- **Provenance made symmetric across the normalized tables.** `holdings.source` accepted `'manual'`
  while having no column to name the import it came from, and neither `holdings` nor `balances_daily`
  carried the CHECK that `transactions` had. Found by scrub, not by the requirement: AC-7.4 is a
  property of every normalized row, and it had been implemented on one table.

**What the lock-in checkpoint caught:** re-reading the enumerated consumer questions against the
delivered tables found one they could not answer. `net_worth` needs assets separated from
liabilities, and `account_type` is the source's vocabulary rather than a classification — different
between sources, and absent entirely for an import-only account. Added as `accounts.balance_class`
while it was still free. One limitation is recorded rather than fixed: there is no FX table, so a
multi-currency net worth is out of scope until it is asked for.

**Also in this session:** the owner settled the open interpreter question — the product moves to
**Python 3.14**. The full suite, mypy strict and ruff were re-run green on 3.14.6 before the pin
moved, and `requires-python` stays `>=3.11` because nothing in the code needs more.

**Verified:** suite green (`prawduct-hook test-status`), mypy strict and ruff clean, every
structural break caught by the harness — which now covers the schema's guarantees, the frozen-DDL
hash, and the index guard's partial predicates as well as the connection layer's four norms. `store init` and `store status` were driven against a real
encrypted datastore, reporting schema version 2, with a known plaintext written through the schema
unrecoverable from the file's raw bytes. **The lock-in check was executed, not read:** the plan's
seven enumerated consumer questions were run as real SQL against a seeded datastore — fourteen
queries, because several questions take more than one and the remaining §5 tools were covered too —
and every one returned, including the per-account gap walk, the plain-sum net worth, and the
freshness stamp for an import-only account with no connection.

## 2026-09-06: The walking skeleton — an encrypted WAL datastore with its four norms enforced

<!-- prawduct: scope=datastore-v1 -->

**Why:** the repository held zero lines of Python. The architecture's four norms were prose claims
with an open issue (#1) standing in for their mechanism, and an operator had no way to create or
look at a datastore. Chunk 01 of the datastore-v1 plan is deliberately the widest chunk in that
plan because it is the one that proves the topology: config → keychain key → encrypted WAL
datastore → migration → read back through the reader role → print from the CLI.

**What landed:**

- The `uv` package (`bankmachine`, Python 3.11+), three runtime dependencies and four dev. The
  smallness is deliberate: a public tool that pulls real bank data on a stranger's machine wants a
  runtime dependency surface small enough to read.
- `config.py` — every path is configuration with a documented default (AC-ARCH.4), resolved by
  precedence from argument, environment, config file, default. Sandbox and production default to
  **different datastore files and different keychain accounts**, so putting fixture data in the real
  store needs an explicit override rather than a forgotten flag (AC-10.6).
- `secrets.py` — the only module importing `keyring` (AC-10.1). The datastore key is a 256-bit raw
  key, so SQLCipher's KDF is skipped and the value in the keychain *is* the key: no derivation whose
  parameters could drift between the process that created the store and the one that opens it.
- `store/connection.py` — the one module that constructs a connection, with exactly two roles.
  Writers route through a single factory that takes an advisory `flock` before it returns; readers
  open `mode=ro` and hold no snapshot beyond the statement that needs it. The SQLite open modes are
  **named constants**, because they are the norms rather than an implementation detail.
- `store/migrations/` — a ~50-line forward-only runner that owns its own transaction boundary, so a
  migration's DDL and its version stamp commit together or not at all.
- `store/engine.py` — SQLAlchemy Core over `create_engine(..., creator=...)`, so SQLAlchemy never
  opens a connection and every SQLCipher-specific step stays in the module that owns the norms.
- `logging_setup.py` — redaction at the formatter (AC-10.3) and a loud environment banner at every
  startup (AC-10.6). Both environments are announced at WARNING: the accident runs in both
  directions, so neither state is the quiet one.
- `cli/` and `__main__.py` — `bankmachine store init` (the only creator) and `store status` (which
  reports a missing or unrecognized datastore rather than crashing or creating one, AC-ARCH.3).
- Suite green, mypy strict clean, ruff clean (`prawduct-hook test-status`).

**The norms are now mechanisms, and issue #1's ask is delivered by this work** — its close is
owed at merge, because on the Issues backend a status change is an immediate API call with no
branch to be abandoned alongside. Each of the four has a test,
and each test was verified to go **red** with its norm deliberately broken —
`tests/preferences/verify_norms_go_red.py` keeps that reproducible rather than a sentence in a
commit message. Two things that came out of running it are worth recording:

- **A norm with two layers needs a test per layer.** Breaking only the `mode=rw` open mode left the
  no-implicit-creation test green, because the existence check still refused. Behaviour alone could
  not tell the layers apart, so the modes became named constants with their own assertions; either
  layer regressing is now caught.
- **The verification harness lied once, and the reason generalizes.** `"ro"` → `"rw"` is a
  same-length edit, and CPython validates a `.pyc` on (mtime, size) — two same-size writes inside
  one mtime second leave stale bytecode valid, so the test imported the *unbroken* module and
  reported green. Any tooling that mutates source in a loop has this failure mode.

**Also in this bundle:**

- `check-no-personal-data.sh` and its 22-case self-test **moved from `scripts/` to
  `tests/preferences/`**, discharging an obligation recorded in three places. The pre-push wiring
  followed the script, so push-time enforcement was kept rather than traded for test-time
  enforcement. The move initially broke five self-test cases by silently skipping them; the sandbox
  and the hook path now resolve through `git rev-parse --show-toplevel` rather than counting `..`
  hops, so a future move fails loudly instead of quietly testing less.
- `test_no_provider_identity.py` and `test_requirement_ids_unique.py` — both named in the norm index
  and both marked aspirational until the scaffold existed — are now written.
- `test_command:` is declared in `project-state.yaml`, deliberately left unset until a runner
  existed that could emit `{junit_xml}`.

**What the Critic caught, and it was worth the round.** One blocking (the plan's Deliverables line
still named the guard's pre-move path) and three warnings, all fixed:

- **`store init` minted a key for a datastore it could not decrypt.** On the restored-from-backup
  path — datastore present, keychain entry gone — it generated *and stored* a fresh key, migration
  then failed, and every later `store status` reported an authentication failure instead of a
  missing key. That is a recoverable state being reported as a corrupt one, which routes the
  operator to the wrong recovery. `store init` now refuses to mint a key for a store that already
  exists, and says why.
- **SQLAlchemy's transaction control is inert over these handles**, inherited from the deliberate
  `isolation_level=None`. Nothing recorded it, and Chunks 02 and 03 are exactly the two that would
  have assumed otherwise. Now documented at the module, pinned by a test, and flagged in the plan
  where those chunks will meet it.
- **`load_config`'s injected `env` seam stopped one step short of `HOME`**, so five config tests
  read as isolated while resolving against the developer's real home — and that branch is the
  documented macOS default.

The second review round found one more, and it is the more interesting of the two: **the `HOME`
seam fix shipped without a test that would catch its own regression.** Every other config test
either sets the XDG variables or asserts only that a path is absolute, so the fallback branch — the
documented macOS default — could have reverted to `Path.home()` with the suite still green. The
test now exists and was verified red against that exact revert. A fix without the check that
protects it is a fix with a shelf life.

Two smaller things rode that same round. `get_datastore_key`'s "run `bankmachine store init`"
advice was wrong in **every** path that reaches it: `writer()` and `reader()` both check the
datastore exists before asking for a key, and `store init` now correctly refuses to mint one for an
existing store — so the advice sent the operator in a circle. It names the state and both real
remedies instead. And the read-only reframing had reached `pyproject.toml` and the package
docstring but not `argparse`'s `description`, which is the one summary an operator actually reads
(`bankmachine --help`).

**One decision deliberately not taken:** `uv init` pinned `.python-version` to 3.14, so the
first run of everything above happened on an interpreter no artifact records. The pin was reverted to
3.12 — the version `project-preferences.md` records as verified — and the whole suite re-run
there. Moving this product's tested interpreter is the owner's call, not a side effect of
scaffolding.

**Trade-off accepted:** the redaction patterns over-redact. A filesystem path holding a
32-character segment is blanked along with the tokens. The alternative — requiring high entropy
before redacting — trades a little log legibility back for the chance of a real token slipping
through, and under the documented default paths no ordinary path is long enough in one segment to
trip it.

## 2026-09-05: AC-ARCH.7 resolved — the system architecture, measured rather than assumed

<!-- prawduct: scope=architecture -->

**Why:** `docs/system-requirements.md` AC-ARCH.7 deliberately deferred journal mode, locking
behaviour and reader isolation under encryption to "the system architecture" — a document that did
not exist. Build step 1 is the encrypted datastore, so step 1 would have been the component that
"encountered them first", which is precisely what the criterion forbids.

**What landed — two artifacts, not one.**

`.prawduct/artifacts/architecture.md`: topology, component responsibilities, the four channels (one
of which is the datastore file, and one of which is the import-file surface), data ownership,
failure modes, deployment and version skew, cross-cutting runtime concerns, and a decision log. It
is this product's first strategy-class artifact and its first `## Direction` section.

`.prawduct/artifacts/build-plan-datastore-v1.md`: build step 1 of system-requirements §8, in four
chunks — the walking skeleton (config, keyring, encrypted WAL datastore, the two connection roles),
the FR-6 core schema, the FR-5 raw-response layer and rebuild, and `sync shell`. Chunk 01 is
deliberately the widest because it proves the topology; Chunk 02 is the lock-in chunk, so the
questions its schema must answer are enumerated from the §5 tool table before any field is designed.
Chunk 01 also carries the `tests/preferences/` guard migration that three separate records have been
promising, and delivers what issue #1 asked for.

**A dependency decision rides with it.** The store layer uses **SQLAlchemy Core** — typed table
metadata and the query builder, no ORM, no session or identity map — decided by the owner over a
builder recommendation of hand-written SQL. It adds `sqlalchemy` as a runtime dependency at Chunk
01, taking the runtime surface to three packages. The objection behind the recommendation is
answered by construction rather than dropped: engines are built with `create_engine(..., creator=...)`
over our own keyed connection, so every SQLCipher-specific step — key first, WAL, `mode=ro`,
`query_only`, the writer lock — stays inside the module that owns the architecture norms, and
SQLAlchemy never opens a connection itself. Both that route and the built-in `sqlite+pysqlcipher`
dialect were verified against SQLCipher before the decision was taken. Risk surfaces were confirmed
in the same pass and are now recorded in `project-state.yaml`.

**The concurrency answer.** WAL journal mode, set after keying. Writer-role processes serialise on
a `flock` held for a whole run, above SQLite's own locking. The MCP reader opens `query_only` and
releases its snapshot at the end of every tool call. Nothing creates the datastore implicitly. A
process that does not recognise the schema version refuses to serve.

**Measured, not remembered.** Every concurrency claim was probed against `sqlcipher3-wheels` 0.5.7
(SQLCipher 4.12.0, SQLite 3.51.1) on this machine. The probes earned their keep three times: the
WAL and shm files are themselves encrypted, which had to be true or WAL would have traded AC-ARCH.5
away for AC-ARCH.7; a reader holding a snapshot starved a passive checkpoint at 0 of 93 frames and
93 of 93 the instant it released, which turned the reader's snapshot discipline from advice into a
norm; and a plain `connect()` to a missing path silently creates an empty encrypted store, which
under AC-ARCH.4's configurable path would answer every question confidently from nothing.

**One premise was falsified.** The design was going to route around a believed limitation — that a
`mode=ro` connection cannot read a WAL database without an existing `-shm`, and would fail against
a hot WAL from a crashed writer. It read correctly in every probed case, including after a
`SIGKILL` mid-write. `query_only` is still the choice, on its two surviving reasons; the reason
that did not survive is struck and recorded as struck, in the artifact's Decision Log.

**Norm bookkeeping.** Four norms born, all before any code exists, so no retroactivity decision
applies — there is nothing to migrate, contain or grandfather. Four pointer rows added to the
preferences norm index, and issue **#1** filed for the enforcement tests, because a mechanism named
and never built is the aspirational failure with extra steps.

**What the review changed, and it was two of the four norms.** The cumulative Critic returned 1
blocking, 12 warnings, 8 notes, and two findings were defects in the norms themselves rather than in
their presentation.

The writer norm **defined the writer role by enumerating commands**, and the list had already
omitted `store init` — which creates the file — and `store rebuild`, which rewrites every normalized
table. A list is a thing to forget. It is now defined by construction: every writable handle comes
from one writer factory, which takes the lock before it returns, so there is no way to be a writer
without passing through it.

The reader norm rested on `PRAGMA query_only`, which **is reversible** — re-probed on the finding,
`query_only=OFF` restores writes on a read-write handle, and `sync shell` ships the operator exactly
the SQL prompt that can type it. Read-role handles now open `mode=ro`, where the same sequence still
fails because the refusal lives in the file handle. Re-probing also scoped the earlier "falsified"
premise properly: `mode=ro` *does* fail against a hot WAL with no `-shm` under an unwritable
directory — the first probe had missed it because the crashed writer left its `-shm` behind. So the
norm carries a no-fallback clause: that state is a loud error, never a quiet downgrade to a writable
handle.

Also from the review: import files named as the foreign inbound surface they are (the artifact had
claimed none existed); one canonical CLI command table instead of four disagreeing lists; a logging
and AC-10.3 redaction rule placed in step 1 rather than step 8, because steps 1-7 all write log
lines; migration DDL and its version stamp required to commit in one transaction, since the version
is the *sole* signal a store is safe to serve; `source_root` and `risk_surfaces` set; and the
enforcement item re-filed from frozen markdown into the live Issues backend.


## 2026-09-05: Named — the product is `bankmachine`

<!-- prawduct: scope=rename -->

**Why:** The working name embedded a third-party trademark and locked the product to one
aggregator, and build step 1 is what fixes the Python package name, the keychain service name, the
scheduler label and the MCP server name. The keychain service name is the expensive one — changing
it after enrollment orphans stored access tokens. Settling the name while the repository still held
**zero lines of code** made this a documentation sweep rather than a migration; that timing was the
whole point of deciding the rename before step 1 rather than at publish.

Verified rather than assumed: PyPI returned 404 for `bankmachine`, so the package name was free at
the time of choosing.

**What changed:** titles and labels across the requirements doc, README, change-log, backlog,
boundary patterns and project-state; the name open questions in `system-requirements.md` §9 and
`project-state.yaml` closed; the aggregator-pluggability question's stale clause corrected, since
the product name no longer embeds the aggregator's name — one fewer reason that question is forced.

Six occurrences of the old name were deliberately **left in place**: two historical change-log and
archived-plan entries that record what was said on the day, the decision entry that names what was
renamed away from, and the GitHub repository slug, which is still accurate because the remote has
not been renamed.

**Carried through to the remote and the checkout.** The GitHub repository was renamed
`brookstalley/MCPlaid` → `brookstalley/bankmachine` (verified still private), `backlog_service_repo`
repointed at it rather than left to lean on GitHub's redirect, and the local checkout moved to
`~/source/bankmachine`. Verified after the move that `core.hooksPath` survived, the guard is clean,
the self-test still passes 22/22, and both branches are in sync with the renamed remote.

**Recorded caveat, raised once and accepted.** "Bank machine" is the ordinary term for an ATM in
Canada and parts of the UK — a name suggesting a device that *dispenses money*, for a product whose
§2 non-goals make "read-only, permanently" a headline commitment. This is a connotation risk, not a
technical one, and the mitigation is placement rather than a different name: the README now leads
with **"Read-only. It never moves money"** above the description, where a reader arriving from the
name meets the correction first.

## 2026-09-05: Repository made publishable — roster out of git, history purged, boundary guarded

<!-- prawduct: scope=repo-sanitization -->

**Why:** The operator restated the product. MCPlaid is a **general-purpose tool, not linked to
their personal finances** — consumed by Claude Cowork, and possibly released publicly, so nothing
specific to them may be in it. That resolved the open question the previous entry left standing,
and resolved it harder than either option on the table: the roster does not belong in this
repository at all.

The exposure was measured rather than estimated. `origin/develop` carried the operator's
institution names and balances in exactly three paths, and every other tracked file at that ref
was grepped clean.

**What changed:**

- The roster and its account-inventory evidence moved to the gitignored `deployment/` directory.
  `docs/deployment-requirements.template.md` keeps what was worth keeping — the zero-engine-change
  contract and the §7 traceability table — with no institution, balance or account count in it.
  That contract is what makes the engine spec trustworthy to a reader who is not this operator.
- `docs/build-vs-adopt-investigation.md` sanitized in place: operator name, machine name,
  institution names and account counts out; every technical finding, including the source-level
  vetting of the candidate MCP servers, kept intact.
- `scripts/check-no-personal-data.sh` added and wired into `pre-push` on **every** branch. It
  matches the roster's own explicit tokens (engine AC-0.3) plus operator identity, on word
  boundaries, over every tracked file. A checkout with no `deployment/` directory has no roster to
  leak and passes with a note — which is why the guard itself is safe to publish.
- `README.md` added, carrying the `git config core.hooksPath .githooks` step. A hooks directory is
  per-clone config, so a fresh clone pushes unguarded and nothing says so — and the only previous
  statement of the step lived inside the hook file the unset config prevents from running.
- **No attribution, anywhere** — stated absolutely in `CLAUDE.md` and homed as a norm row in
  `project-preferences.md`. This widens the already-ratified `Commit attribution: none` past
  commits to PRs, issues, comments, code, docstrings, documentation and release notes, and it
  overrides any harness default to the contrary. Recorded here because the widening previously
  existed only in `CLAUDE.md` while the preferences row still read narrower.
- **History purged.** `git filter-repo` removed five paths from every commit; `develop` and `main`
  were force-pushed. Verified by fresh clone: zero institution or operator tokens anywhere in the
  remote's history. Two paths were purged and re-added at their current content rather than
  scrubbed in place — the decision record and the guard itself, both of which carried in early
  revisions exactly what the purge exists to remove.
- **The fifth path was found by the guard, not by us.** The purge was planned as four paths. The
  finished guard, scanning all history, reported a fifth: Chunk 01's own first commit hardcoded
  identity tokens in the guard's source — the arrangement the Critic's R-9 had just made us
  remove. The check caught its author.
- Four decisions recorded with alternatives: repository scope; MCP transport is local stdio only
  and AC-10.5 holds; macOS for v1 with the credential store and scheduler behind seams; rename
  before build step 1.

**What the review changed, and it was the important half.** The first version of this guard
scanned the *working tree*. Critic pointed out that this passes the exact exposure the guard exists
to stop — a leak sitting in already-pushed history behind a sanitized tip — and that the operator
would read "clean" as "nothing I am pushing carries the roster", which was not what was checked. The
guard now takes the ref range the pre-push hook already receives and scans **every commit being
pushed**. Run against this repository's own history it correctly refuses: the roster is still back
there, which is what Chunk 02 is for.

The same review found the guard failed open at every error path, and that its hardcoded identity
tokens forced a carve-out where the one tracked file containing the operator's name was the one file
never scanned. Both are fixed by construction rather than by patching: **all** tokens now come from
gitignored `deployment/`, so the script carries none and needs no self-exclusion, and every error
condition aborts rather than reporting clean.

**The self-test earned itself immediately: 7 of its 15 cases failed on first run.** The cause was a
genuine defect — the positive control used system `grep` while the scan used `git grep`, which does
not honour `\b` in ERE. So the control passed while the scan matched nothing: precisely the
fail-open shape the guard was being rewritten to refuse, reproduced inside the fix. Word boundaries
are now spelled out explicitly, and the control runs through the same engine that scans, using a
real token rather than a synthetic sentinel.

**Note on the mechanism.** Guard and self-test are shell rather than `tests/preferences/` because no
Python scaffold exists yet and creating one would fix the package name ahead of the rename decision.
The migration obligation is recorded in `project-preferences.md` and in `system-requirements.md` §8
build step 1 — the step that lands the test runner, and therefore the moment it is triggered.

## 2026-09-05: Discovery captured; requirements split into engine and roster layers

**Why:** The repo held three substantial docs but a template-default `project-state.yaml`, so
governance could not calibrate rigor and the build gates could not engage. Discovery ran in
reconciliation mode — the material was read and backfilled rather than re-interviewed.

Mid-discovery the operator imposed a constraint that reshaped the frame: **no hardcoded account
providers; accounts are added and removed over the product's life.** The requirements doc was a
snapshot of one roster on one day, and that roster was already known wrong in detail. The operator's
own refinement settled where the line falls — the roster requirements are *genuine* requirements,
they simply belong to a different layer than the engine.

**What changed:**

- The v1 acceptance-criteria document split into `docs/system-requirements.md` (the
  provider-agnostic engine, no institution name in it) and a deployment-requirements document
  holding this operator's roster, as real acceptance criteria. All 47 v1 criteria land in one or
  the other, generalized or instantiated; one is explicitly superseded.
- A load-bearing rule connects them: every deployment requirement must be satisfiable by
  configuration plus an adapter with zero engine change. The traceability table is its checkable
  form — a deployment requirement that cannot be expressed that way is a gap in the engine spec.
- Two norms ratified: the provider-agnostic engine (with the aggregator expressly carved out), and
  uniqueness of requirement ids within a document.
- Decisions recorded with alternatives: SQLCipher over plain SQLite and over field-level AES;
  no hardcoded filesystem paths; lossless rebuild qualified by a recorded derivation version;
  account lifecycle so a retired account stops reading as a permanent coverage gap.
- Toolchain set: uv, pytest, ruff, mypy strict, hypothesis on the money and idempotency invariants.

**Open, and the operator's to decide:** where the roster lives. `origin/develop` already carries
institution names and balances, against the constraint this project records; the remote is private.
*(Closed 2026-09-05 by the entry above this one: the roster moved out of git and the history was
rewritten.)*

**Reviewed:** `rev-20260905T195208Z-c1f3c450` (2 blocking, 9 warning, 5 note — all resolved),
verified clean by `rev-20260905T200406Z-290b9dcb`.

