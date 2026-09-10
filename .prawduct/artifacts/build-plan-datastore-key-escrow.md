---
artifact: build-plan
version: 2
scope: datastore-key-escrow
branch: feature/datastore-key-escrow
critic_mode: cumulative-final
depends_on:
  - artifact: api-contract
    file_path: .prawduct/artifacts/api-contract.md
  - artifact: security-model
    file_path: .prawduct/artifacts/security-model.md
  - artifact: operational-spec
    file_path: .prawduct/artifacts/operational-spec.md
governed_by:
  - artifact: security-model
    dispositions:
      - "🔴 'secrets live only in the OS keychain, and nothing returns one into a log line, an exception message, or a `repr`' → this plan is the amendment's implementation, not a departure from it. The amendment (2026-09-10, owner ruling) permits a deliberate, operator-initiated export of the DATASTORE KEY ONLY. The log/exception/`repr` half is untouched and AC-17.6 is its criterion here"
      - "the norm is `Status: in-transition` tracking #61, and this plan is what flips it to steady-state: chunk 02 removes the shell recipe the interim rule preserved"
      - "aggregator secrets and access tokens remain unexportable — no verb in this plan reaches them"
      - "log redaction happens at the formatter, over-redacts by design, keyed to credential shape → INAPPLICABLE, because nothing in this plan writes a log record carrying a secret. The redactor is untouched; AC-17.6 asserts the key reaches no log at all, which is a stronger property than redacting it once there"
      - "no tracked file carries a credential-shaped string; ignore rules cover data, logs and credential paths → conforms, and the export is the first thing that can write a key to a file. It refuses a destination inside the data directory and the operator names the path, so no product-chosen location can end up tracked; `tests/preferences/test_no_credentials_tracked.py` is unchanged and still passes"
      - "the aggregator's API is the only network destination → INAPPLICABLE; nothing in this plan reaches a network transport"
  - artifact: api-contract
    dispositions:
      - "CLI exit codes `0`/`1`/`2` are a contract and the 1/2 split is not collapsible → conforms; `verify` on a mismatch is `1` (ran, found a problem), a refusal to run is `2` (could not run), and neither is collapsed into the other"
      - "the CLI command inventory is a published surface → chunk 02 adds three entries at `experimental`, by the inventory's own *shipped and depended on* criterion"
      - "the MCP surface is read-only; adding a mutation tool is not a chunk-level decision → INAPPLICABLE, and worth stating rather than skipping: these commands write the KEYCHAIN, never the datastore, and they are CLI-only. No MCP tool is added, and nothing here is reachable from the MCP surface at all"
      - "every response carries a freshness stamp, and incompleteness rides the success path as a warning → INAPPLICABLE; this plan emits no `Answer` envelope. Its CLI-shaped analogue is honoured instead — a refusal never rides the success path, and `verify` reports a mismatch as exit 1 rather than as a warning on exit 0"
      - "a tool's boundary is drawn where the answer shape changes, never where the question changes → conforms, and it is why this is three verbs rather than one with a mode flag: export returns a secret, verify returns a verdict, import performs a write. Three answer shapes"
      - "a stored balance is reported with its lifecycle, and no total over balances is emitted without it → INAPPLICABLE; this plan reports no balances and emits no totals"
---

# Build plan — the operator can export, check, and restore the datastore key

## Why

The datastore key cannot be recovered from the datastore, and `balances_daily` is the one series no
re-sync rebuilds — so a lost key is permanent data loss with no remedy. The product says so on three
surfaces and has never had a way for the operator to act on it.

Two consequences, and the second is the one nobody had named. There is **no export**, so the
product's own instruction points at a shell recipe that puts the key in shell history — which
AC-10.1 forbids, making the criterion something the product routes around rather than obeys. And
there is **no check**, so an operator who transposes two hex digits transcribing 64 characters
produces a value that is 64 characters, is pure hex, passes `_validate` completely, and is simply a
different key. Nothing tells them until the store will not open, which is the first moment nothing
can be done.

Requirements: **FR-12 / AC-17.1–17.8** (`docs/system-requirements.md` § 6), derived in
`.prawduct/artifacts/discovery-datastore-key-escrow.md`, on the owner's ruling of 2026-09-10
recorded in AC-10.1's amendment.

## Requirements confidence

**High.** AC-17.1–17.8 are written, and the two decisions that would have been expensive to reverse
were put to the owner and answered before the requirement was drafted: export refuses a non-TTY
stdout, and verification test-opens the datastore rather than comparing against the keychain. What
is left is command shape and prompt copy, which the contract deliberately does not constrain.

**Do not re-open either decision mid-build.** Both are recorded with their reasoning in the
discovery document; a build that revisits them is re-litigating a settled ruling.

## The one design fact that shapes both chunks

🔴 **The probe is not `reader()`.** `reader()` fetches the key from the keychain itself, which is
precisely the thing a candidate key must not do. The seam it needs is one level down:
`_key_and_prepare` (`store/connection.py:277`) already takes an explicit `key`, and
`_diagnose_first_read` (`:290`) already separates a wrong key (`SQLITE_NOTADB` →
`DatastoreKeyRejectedError`) from a datastore that cannot be read at all (`DatastoreUnreadableError`
— the measured hot-WAL case). **The build adds an opener, not a diagnosis.**

🔴 **The probe must not require a supported schema.** `reader()` defaults to
`require_supported_schema=True`; the probe passes `False`. Restoring an older backup onto a newer
build is a real path, the key is correct there, and refusing to answer "does this key work" because
the schema is old would fail the operator in the exact scenario the command exists for.

## Chunk 01 — the probe, and the three verbs

**Delivers**

- `store/connection.py`: `opens_with(config, key) -> bool` — a read-role open with a supplied key,
  `require_supported_schema=False`. Returns `True` on success, `False` on
  `DatastoreKeyRejectedError`; lets `DatastoreMissingError` and `DatastoreUnreadableError`
  propagate, because neither is an answer about the key.
- `secrets.py`: `KeyEscrowRefusedError(SecretsError)` — an escrow operation refused because
  performing it would leak the key. It belongs here: it is the guard that keeps AC-10.1 true.
- `cli/store.py`: a `store key` group with `export`, `verify`, `import`, wired beside `init`,
  `status`, `rebuild`, `backup`.
- `tests/cli/test_store_key_commands.py`.

**Acceptance criteria**

1. **AC-17.1** — `store key export` with a TTY stdout renders the key; with a non-TTY stdout it
   refuses and writes nothing. 🔴 The refusal is the point of the command, not a detail: without it
   this is the shell recipe with a nicer name.
2. **AC-17.1 / AC-17.2** — `--to <path>` writes the key at mode `0600` and **never overwrites** an
   existing path (`O_EXCL`, matching `store backup`'s refusal). There is no default destination and
   no write inside the data directory.
3. **AC-17.3** — `store key verify` reports whether the candidate **opens the datastore**. It
   answers with the keychain entry absent, and it answers with the schema at an unsupported version.
4. **AC-17.4** — `verify` and `import` read the candidate by no-echo prompt. 🔴 No verb in this
   group accepts a key as an argument, so no key reaches shell history or the process table.
5. **AC-17.5** — `import` opens the datastore with the candidate first and writes the keychain entry
   only on success. A wrong candidate leaves an existing entry untouched. With no datastore present
   it refuses and says so, rather than accepting an unverifiable key.
6. **Exit codes.** `verify` on a mismatch exits `1` — it ran and found a problem. A refusal to run
   (non-TTY export, destination exists, no datastore to check against) exits `2`. 🔴 The 1/2 split
   is a contract (`api-contract.md` § Direction) and is not collapsed here.
7. **AC-17.6** — a test asserts the key appears in no log file, no stderr, and no exception message
   across all three verbs, and that `export`'s stdout is the only place it is ever rendered.

**Done when** every criterion above has a test, and `uv run pytest tests/cli tests/store tests/test_secrets.py` is green.

## Chunk 02 — the instruction sweep, the artifacts, and the norm flip

**Delivers**

- `cli/store.py`: `_print_key_backup_instruction`, `cmd_status`'s standing line, and
  `_print_backup` all name the product's command. `_keychain_recipe` is **removed**, not left
  unused.
- `tests/cli/test_store_commands.py`: `test_init_tells_the_operator_to_back_the_minted_key_up_and_how_to_read_it`
  moves from asserting the shell recipe to asserting the command.
- `api-contract.md`: three entries added to the CLI inventory at `experimental`.
- `operational-spec.md` § Backup & Recovery, `docs/first-production-connection.md` §§ 2.2 and 3.3:
  the recipe is replaced by the commands.
- `security-model.md` § Direction and the `project-preferences.md` norm-index row: `in-transition`
  → `steady-state`, with the mechanism named.

**Acceptance criteria**

1. **AC-17.7** — every surface that tells the operator to back the key up names the command. No
   surface names `security find-generic-password`, and a test asserts its absence so the recipe
   cannot come back by accident.
2. 🔴 **The `store init` test is re-pointed, not weakened.** It asserted the exact recipe string;
   it now asserts the exact command string. Equally specific, and AC-17.7 is the warrant. **Record
   the reason in `.prawduct/change-log.md`** — a changed test assertion without a recorded warrant
   is indistinguishable from a weakened one.
3. **AC-17.8** — one test walks the round trip: export a key, delete the keychain entry, import it
   back, open a datastore written under it. The **operator** rehearsal is NOT discharged by this and
   stays owed (#10 and `operational-spec.md` § Restore).
4. The norm flips to steady-state **only because** the interim rule's condition is met — FR-12's
   paths exist and the recipe is gone. The row names what enforces AC-17.6.
5. 🔴 **`store backup`'s warning names the command** — it is the surface where the thought most
   naturally occurs and the only one that named no command at all.

**Done when** the full suite is green and no artifact still describes the shell recipe as the
operator's path.

## Out of scope, deliberately

- **Backup scheduling** — #10, whose premise was corrected on 2026-09-10: there is no launchd agent,
  launchd is a design choice rather than a requirement (AC-ARCH.2 requires a daily job that recovers
  a missed window; § 1 says "an OS-level user agent"), and nothing requires backup to ride on the
  sync's schedule at all.
- **Key rotation** — re-encrypting the store is a migration with its own failure modes and nothing
  has asked for it.
- **Passphrase-wrapped escrow** — the ruling put preservation on the operator; a wrapped export adds
  a second secret with the same recovery problem and no keychain to hold it.
- **Aggregator secrets and access tokens** — both have another source, so neither needs escrow.

## Status

- [x] Chunk 01 — the probe, and the three verbs
- [x] Chunk 02 — the instruction sweep, the artifacts, and the norm flip

**Context:** requirements landed on `develop` at `bd7ffd0` (FR-12, AC-10.1 amendment, norm
`in-transition`). Both chunks are built on `feature/datastore-key-escrow`; the norm is back to
`steady-state` and AC-17.6 now has an enforcement artifact. Critic runs once, `cumulative`.

**Two things the build added that the plan did not name, both recorded in their requirement:**

- **AC-17.2 grew a refusal.** The plan said the export "never writes inside the data directory" and
  the first implementation only declined to *default* there. A plaintext key beside the ciphertext
  it decrypts makes the directory self-decrypting, so one careless `cp -r` carries both halves. The
  export now refuses that destination and AC-17.2 says so.
- **AC-17.7 reached the recovery direction.** Found by running a recovery drill, not by reading:
  both "restore the keychain entry from your backup" messages still named no command — the exact
  defect #61 was filed about, fixed on the export side and left standing on the restore side. No
  test in the suite could see it, because two of them asserted the old prose.
