# Discovery — Getting the datastore key out, checking it, and putting it back

**Work cycle:** datastore key escrow · medium · requirement
**Opened:** 2026-09-10
**Closes (proposed):** brookstalley/bankmachine #61
**Follows from:** the owner's ruling of 2026-09-10 — *the operator owns exporting and preserving the
key, and the product owes a mechanism to export, check, and restore it*

---

> **Scope.** This document settles the *contract* for operator key escrow and writes the requirement
> #61 asks for. It does **not** implement `bankmachine store key`. It also amends AC-10.1, which
> currently forbids the only thing the product actually tells its operator to do.
>
> It does not touch #10 (scheduling a backup, rehearsing the restore). The two items are adjacent
> and their positions disagreed; § *The disagreement, and how the ruling settles it* records how.

---

## The questions this item has to answer

**What problem are we solving?** The datastore key cannot be recovered from the datastore, and
`balances_daily` is the one series no re-sync rebuilds — so a lost key is permanent data loss with
no remedy. The product knows this and says so loudly on three surfaces. What it has never had is a
way for the operator to *act* on the instruction: no export, no way to check that what they wrote
down is right, and no way to put a saved key back.

**What does success look like?** An operator can get the key out through a product path that does
not route it through shell history, confirm that the copy they stored actually opens their
datastore, and restore it onto a new machine — with the restore refusing to write a key that
doesn't work.

**What is out of scope?** Scheduling backups and rehearsing the restore procedure (#10). Aggregator
secrets and access tokens, whose remedies are a vendor dashboard and a re-enrolment. Any
product-chosen escrow location, passphrase-wrapping, or key rotation.

---

## Premise verification — what #61 claimed, and what the tree says

#61's body was written 2026-09-09T22:42Z. Three of its load-bearing claims have since gone stale,
and one was wrong when filed. The requirement below is written against the tree, not the issue.

🔴 **"The product offers no way to do what the warning instructs" — stale.** Three surfaces now
print the exact recipe. `_print_key_backup_instruction` (`src/bankmachine/cli/store.py:114`) prints a
block at the moment a key is minted, and its docstring is precise about why *there*: minting is "the
one moment the operator is certainly looking at this terminal, and it is also the last moment at
which losing the key costs nothing — there is no data yet." `cmd_status` (`:181`) repeats it as one standing
line (`:188`), with its own comment on why one line and not the block. `docs/first-production-connection.md`
carries it twice — § 2.2 as a rehearsal on the sandbox key, which does not matter, and § 3.3 at
cutover, deliberately placed before any data exists.

🔴 **"Undocumented, platform-specific" — stale, and moot.** Documented in the three places above.
And `docs/system-requirements.md:104` scopes this product to macOS on Apple Silicon; there is no
second platform for the recipe to be specific about.

🔴 **"The paired import already exists in effect (`set_datastore_key`), so restore is the cheaper
half" — wrong, and it inverts the priority.** `set_datastore_key` has exactly one caller in `src/`:
`ensure_datastore_key` (`src/bankmachine/secrets.py:240`), which hands it a **freshly generated**
key on the mint path and is reachable only from `store init`. **No path anywhere accepts a key the
operator supplies.** And `_obtain_key` (`cli/store.py:150`) deliberately refuses to mint for a
datastore that already exists, so the restored-machine path is: `store init` correctly refuses, and
its error says *"Restore the keychain entry from your backup"* — an instruction with no command
behind it, which is the same defect the issue filed about the export direction. **Both directions
are missing at the operator surface.** Restore is not the cheaper half; it is the half that runs
during an incident.

🔴 **New, and not in the issue: validation catches malformed, never wrong.** `_validate`
(`src/bankmachine/secrets.py:65`) checks two things — exactly 64 characters, and a full match on the
hex alphabet. It is careful about it, with a long comment on why a regex rather than `int(key, 16)`,
since `int` would accept `"0x" + "a" * 62`. But an operator who transposes two hex digits copying 64
characters into a password manager produces a value that is 64 characters, is pure hex, and passes
validation completely. It is simply a different key.

The consequence is a timeline with no signal in it. `store init` mints; the operator runs the recipe
and transcribes, one character off. § 3.3 says *"verify you can read them back from where you put
them"* — they can; they read back the wrong thing they wrote. Months of `balances_daily` accumulate.
The keychain goes (new machine, wiped login keychain, a Time Machine restore that did not carry the
item). They paste what they saved, and SQLCipher raises rather than returning garbage — which is
correct behaviour, and useless, because the first moment the error is available is the first moment
nothing can be done about it. **There is no point at which the operator can discover that their
backup is wrong while it is still fixable.**

🔴 **AC-10.1 is contradicted in code, not merely in docs.** AC-10.1 says the datastore key is "never
in the repo, never in a plaintext dotfile, **never in shell history**, never in a log line."
`security find-generic-password -s bankmachine -a datastore:production -w` prints the key to stdout
and puts its invocation in shell history — and `cli/store.py:137` prints that command to the
operator as the product's own instruction. The docstring is candid about the bind: it names the
recipe "because otherwise the instruction asks for a value the operator has no route to."

That is an honest compromise, and it means AC-10.1 is not being honoured — it is being routed
around, by the product, in the product's own output. **An AC that ships contradicted is worse than
an amended one**, because the next reader believes it.

---

## The disagreement, and how the ruling settles it

Two positions were on record and they disagreed:

- **#10:** *"writing the datastore key anywhere the product controls defeats the keychain, so it
  stays an operator instruction."*
- **#61:** that position leaves the operator with no actionable instruction at all.

#61 asked for the disagreement to be resolved explicitly rather than built over. **The owner ruled
on 2026-09-10: it is up to the owner to export and preserve the key, and there must be a mechanism
to export, check, and restore.**

🔴 **The ruling keeps #10's position and narrows its scope.** "Anywhere the *product* controls" — a
dotfile, the data directory, a log — remains forbidden, and AC-17.2 below states it. What the ruling
establishes is that a path the *operator* names, chosen by them, in an act they initiated, is not
the product controlling anything. AC-10.1's single sentence currently forbids all three cases
together; the amendment separates the deliberate act from the incidental leak.

---

## The model: three verbs, one shared question

The three commands look like three features and are really one, asked three ways:

**Does this candidate key open this datastore?**

- **export** does not ask it — it reads the keychain and hands the value over.
- **verify** asks it and reports the answer.
- **import** asks it and writes the keychain entry only on yes.

🔴 **Verification is against the datastore, not against the keychain**, and the reason is the state
the operator is in when they need it. A comparison against the live keychain entry answers "does my
copy match what is stored" — useful before an incident, and unanswerable during one, because
keychain-entry-absent is the whole incident. Opening the store with the candidate answers the
property the operator actually cares about, and answers it in both states.

**The mechanism already exists.** `_key_and_prepare` (`src/bankmachine/store/connection.py:277`)
takes an explicit `key` parameter rather than reaching for the keychain itself, and
`_diagnose_first_read` (`:290`) already distinguishes a wrong key from a datastore that cannot be
read at all — SQLCipher reports a wrong key as `SQLITE_NOTADB`, *"file is not a database"*, which
"reads as corruption and sends the operator to the wrong recovery procedure unless it is renamed."
The probe this contract needs is a read-role open with a supplied key, and the diagnosis it should
produce is already written.

---

## Requirements — AC-17.1 through AC-17.8

**AC-17.1 · The product hands the operator the key, and refuses to do it by accident.** An export
path exists. It renders the key to an interactive terminal and **refuses a non-TTY stdout**; it
writes a file only when the operator explicitly asks with a destination, at `0600`. *Why:* the
instruction `store backup` prints has had no command behind it, so every operator who followed it
used a shell recipe that puts the key in history and in the process table. A refused pipe is the
entire difference between the product path and the recipe it replaces — without it, the new command
is the old defect with a nicer name.

**AC-17.2 · The export names no destination of its own.** The operator supplies the path; the
product never chooses one, never defaults to one, and never writes inside the data directory.
*Why:* #10's position survives the amendment intact. What the ruling changed is that the operator
may *ask*, not that the product may *decide* — and a default path is the product deciding, on the
run where the operator did not think about it.

**AC-17.3 · A candidate key is checked by opening the datastore with it.** Verification reports
whether the candidate opens the store, and does not rest on comparing it to the keychain entry.
*Why:* two independent reasons, either sufficient. Validation catches malformed and never wrong — a
transposition is 64 characters and pure hex and passes `_validate`. And the check has to answer in
the state an incident actually presents, which is keychain absent; a comparison has nothing to
compare against precisely then.

**AC-17.4 · A candidate key never arrives on a command line.** Verification and restore read their
candidate by prompt, not by argument. *Why:* an argument lands in shell history and in the process
table — the failure AC-10.1 names, and the reason the `security … -w` recipe was never an
acceptable permanent answer. A command that fixed the export direction and reintroduced the same
leak on the input direction would have moved the defect, not closed it.

**AC-17.5 · Restore verifies before it writes.** The restore path opens the datastore with the
candidate and writes the keychain entry only if it opens. Where no datastore is present to check
against, it says so rather than silently accepting. *Why:* `_obtain_key` refuses to mint a key for
an existing datastore for a stated reason — a fresh key "would decrypt nothing and would hide this
diagnosis behind an authentication failure." A restore that wrote first would reintroduce exactly
that, from the other direction, and would additionally overwrite a working entry with a typo.

**AC-17.6 · The secrecy guarantee holds across all three paths.** The key reaches no log line, no
exception message, and no `repr` on any of them. The export's rendered output is the only place a
key is ever emitted, and it is printed, never logged. *Why:* `secrets.py`'s guarantee is currently
**structural** — one module imports the keychain, and no caller prints. Adding the first printing
caller is the moment a structural guarantee becomes a rule somebody has to keep, and the moment to
say so is before the caller exists.

**AC-17.7 · The printed instruction names the product's own command.** Every surface that tells the
operator to back the key up names the command rather than a shell recipe — `store init` at minting,
`store status` as the standing line, and `store backup`, which today names the chore and no command
at all. *Why:* three surfaces print the recipe and a fourth prints none, so which answer the
operator meets depends on which command they happened to run. One instruction, one answer.

**AC-17.8 · The round trip is covered by a test, and the human half stays owed.** A test exports a
key, restores it into a keychain that does not hold it, and opens a datastore written under it.
This does **not** discharge the operator rehearsal: `operational-spec.md` § Restore records that a
restore into the production path, against a key read back from wherever the operator stored it, is
still unwalked, and sandbox cannot rehearse it. *Why:* the automated half and the human half fail
differently. The test catches a broken round trip; only a person catches an instruction that reads
clearly and cannot be followed under pressure. Claiming the first discharges the second is how #10's
rehearsal quietly stops being owed.

---

## Design — proposed, and deliberately thin

A `store key` command group beside `init`, `status`, `rebuild` and `backup`, with three verbs:

```
bankmachine store key export [--to <path>]
bankmachine store key verify
bankmachine store key import
```

One shared primitive underneath — *does this candidate open the datastore* — built on the existing
`_key_and_prepare` seam and returning `_diagnose_first_read`'s existing distinction between a wrong
key and an unreadable file. `export` reads `get_datastore_key`; `import` calls the already-written
and currently-uncalled `set_datastore_key` after the probe succeeds.

Everything above the primitive is CLI shape and is the build's to settle. The contract does not
constrain flag names, prompt wording, or whether the group is `store key <verb>` or
`store key-<verb>`.

---

## Assumptions, vetoable

**A1 · Plain export, no passphrase-wrapped escrow.** `[MED impact | user can correct]` The ruling
put preservation on the operator, so protecting the key in storage is the password manager's job.
A passphrase-wrapped export would add a second secret with the same recovery problem and no keychain
to hold it.

**A2 · AC-10.1 is amended, not given a bounded exception.** `[MED impact | user can correct]` The
blanket sentence is what is wrong, and the product already contradicts it at `cli/store.py:137`. An
exception would leave the false sentence standing for the next reader to believe.

**A3 · Verification does not also report keychain agreement.** `[LOW impact | user can defer]` A
keychain holding a key that no longer opens the store should not be reachable — `_obtain_key`
refuses to mint over an existing store, and nothing else writes the entry. Reporting the pair would
answer a question nobody has. If the restore path ever gains a `--force`, revisit this.

**A4 · No key rotation.** `[LOW impact | user can defer]` Rotation means re-encrypting the store,
which is a migration with its own failure modes, and nothing has asked for it. Out of scope; not
forbidden.

---

## Requirements confidence

**High on the contract, medium on the CLI shape.**

High for AC-17.1–17.8: each rests on a measured fact in the tree rather than on reasoning — the
missing `set_datastore_key` caller, `_validate`'s malformed-not-wrong gap, the `cli/store.py:137`
contradiction, and the `_key_and_prepare` seam were each read out of the source while writing this.

Medium for the command surface. The two decisions that would have been expensive to change later —
what export does with a non-TTY stdout, and what verification checks against — were put to the owner
and answered before this was written. What is left is naming and prompt copy, which the build can
settle and the contract does not constrain.

The stakes are the highest in the product (Principle: a lost key is unrecoverable data loss), which
is why the round trip is an AC rather than left to the build's judgment.

---

## Shared-artifact deltas for the integrator

Three homes carry this and they must stay in step:

1. **`docs/system-requirements.md` § 6** — AC-10.1 amended with the ruling; FR-12 added carrying
   AC-17.1–17.8. Contract of record.
2. **`.prawduct/artifacts/security-model.md` § Direction** — the *"secrets live only in the OS
   keychain"* norm gains the amendment, since an export path is a deliberate departure from
   *"nothing returns one into a log line, an exception message, or a `repr`"* as a reader will
   generalize it.
3. **`.prawduct/artifacts/project-preferences.md`** — the norm-index row for that norm points at the
   Direction entry, so it needs no restatement, but its mechanism note now has a second thing to
   cover: AC-17.6 is what keeps the printing caller from becoming a logging one.

`operational-spec.md` § Backup & Recovery and `docs/first-production-connection.md` §§ 2.2 / 3.3
carry the shell recipe and are **descriptive** — they track the code and get updated by the build
that lands the command, not by this pass.
