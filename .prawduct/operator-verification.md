# Operator Verification

Pre-merge checks a human has to make with their own eyes, for changes whose
correctness a test cannot speak to. Entries are appended by the chunk that
creates them and drained with `prawduct-hook verify-operator-verification <ID>`.

## VRF-001 — Chunk 04 — `bankmachine sync shell` output and redaction

**Status:** pending

**Why a human:** the chunk's acceptance criterion is that an operator can open
the shell against a real datastore, run a query, and *read the result*. The
tests assert that rows come back, that writes are refused and that tokens are
redacted; none of them can say whether the output is legible to a person who is
already having a bad day, which is the only reason this command ships in build
step 1.

**Where to verify:** any real datastore.

```
bankmachine store init          # if you do not already have one
bankmachine sync shell
```

**Verify:**

1. The banner names the environment, the datastore path, the read-only role and
   the redaction — before the first prompt, not after.
2. A join across `accounts` and `balances_daily` renders as aligned columns that
   line up, with `(N rows)` under them.
3. A balance in minor units comes back **whole**. Six figures must not be
   masked: `48750000` is $487,500.00, not `****0000`.
4. An account `mask` (four digits) survives; a long digit run does not.
5. A `credential_ref` or any 32-plus character opaque string reads `[REDACTED]`.
6. `PRAGMA query_only = OFF;` reports `0` — the flag really does flip — and the
   next `UPDATE` still fails with `attempt to write a readonly database`.
7. `BEGIN;` prints the rolled-back note, and the prompt comes back.
8. A bad column name prints one line and leaves the session open.
9. Ctrl-D leaves. Ctrl-C abandons a half-typed statement without leaving.

**Recorded session** (run 2026-09-06 against a scratch datastore seeded with two
accounts; the datastore path is elided):

```
bankmachine sync shell -- environment SANDBOX
datastore: /tmp/.../opcheck/store.db
role:      read-only at the file; writes are refused and no PRAGMA changes that
output:    access tokens and account numbers redacted (AC-10.3)
type .help for the command list, .quit to leave
bankmachine> SELECT a.name, a.mask, a.balance_class, b.current_minor
         ...>   FROM accounts a JOIN balances_daily b USING (account_id)
         ...>  ORDER BY a.account_id;
name               mask  balance_class  current_minor
-----------------  ----  -------------  -------------
Everyday Checking  4021  asset          428317
Mortgage           8830  liability      48750000
(2 rows)
bankmachine> SELECT source_connection_id, credential_ref, granted_history_days FROM connections;
source_connection_id  credential_ref              granted_history_days
--------------------  --------------------------  --------------------
conn-abc              datastore:token:[REDACTED]  365
(1 row)
bankmachine> PRAGMA query_only = OFF;
(no rows)
bankmachine> PRAGMA query_only;
query_only
----------
0
(1 row)
bankmachine> UPDATE accounts SET name = 'tampered' WHERE account_id = 1;
error: attempt to write a readonly database
bankmachine> BEGIN;
(no rows)
note: that statement left a transaction open; it was rolled back so the prompt holds no snapshot. Nothing was lost -- this handle cannot write.
bankmachine> SELECT COUNT(*) AS accounts FROM accounts;
accounts
--------
2
(1 row)
bankmachine> SELECT nope FROM accounts;
error: no such column: nope
bankmachine> .quit
```

The same file read by stock `sqlite3` reports `file is not a database`, which is
the reason this command exists.

Items 1-8 were exercised in that session. Item 9 is the terminal-only half:
`input()` handles it, and a piped session cannot show it.
