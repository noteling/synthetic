# Satchel: extract, verify and recreate an employee home

An employee's memory, work and history live only in its home on this machine. Satchel writes the home into one
archive that can be kept elsewhere, checked later, and recreated into a fresh home (on this or another machine).

Owner rulings: archives are **not encrypted**; secrets and passwords are **masked**, fail-closed (David,
event-2f2c8899). Migration is rare: there is no schedule, retention or built-in destination (event-40c88c60).

## Commands (operator only; refused inside an employee session)

```
synthetics --home H home extract --out DIR [--timeout SECONDS] [--move]
synthetics home verify ARCHIVE
synthetics home recreate ARCHIVE --into NEW (--from-move | --source-lost --attest "<why>") [--map OLD=NEW ...]
synthetics --home H home held | release ID [--by HUMAN] | drop ID --reason TEXT [--by HUMAN] | release-captures
```

`extract` writes `DIR/satchel-<employee-id>-<UTC stamp>.tar` (mode 0600) and prints the archive path, its sha256 and
the number of masks per kind (never the values). It is read-only on the home and does not stop the worker.

## What goes in

| Member | Content |
|---|---|
| `manifest.json` | format 1, employee id and name, store `user_version`, hashed host label, code commit, row counts, member sums, excluded list with reasons, re-provision list (channel names, prefixes and routes; grants; worker launchd label), `masks[]`, pre-mask journal heads |
| `state.sqlite3` | snapshot of the home database, `channels` rows deleted, content masked, compacted |
| `runs.jsonl` | Ledgerline export of every run (docs/ledgerline.md), raw values scanned |
| `config/…` | `profile.json`, `attribution.json`, `humans.json`, `floor.json`, `click.json`, `glance.json`, `runtime.json`, `prompt.local.md` when present, masked |
| `meetings/<id>/journal.jsonl` | meeting journals, masked line by line |
| `SHA256SUMS` | sha256 of every member above |

Never archived: channel key files, the Telegram token file and `telegram.json` / `telegram_state.json`, CLI login
state (`~/.claude`, `~/.codex`), answer-desk pairing, raw `runs/` side files, `workspace/`, per-process state and
locks, other homes, the company and project stores. Satchel does not open the key or token files at all.

A file or directory at the home root that Satchel does not know, or a database table it does not know, makes the
extract **refuse** (`unknown_home_file`, `unknown_table`); move the file out of the home or extend Satchel.

## How the snapshot is taken

1. `VACUUM INTO` from a read-only connection: one consistent transaction, no worker stop. A timeout (default 600 s)
   aborts with `snapshot_timeout`.
2. In the copy: delete `channels` rows (their names, prefixes and routes go to the manifest), mask every text value
   of every table, then `VACUUM INTO` again so freed pages holding deleted hashes or pre-mask text are not carried
   (the copy also runs with `PRAGMA secure_delete=ON`, so freed cells are overwritten as they are freed).
3. Config files, journals and the run export are masked the same way.
4. All of this happens in a private 0700 directory under the system temp directory (`satchel-tmp-*`), never under
   `--out`, so a synced `--out` folder never sees unmasked data. Only the finished tar is copied into `--out` under a
   temporary name (`.satchel-partial-*`) and renamed. On any error `--out` holds nothing new and the temp directory is
   removed. Limit: a SIGKILLed extract leaves its local temp directory behind; the next extract removes temp
   directories of its own whose owning process is gone.
5. Before the tar leaves the temp directory, extract runs `verify` on it with the home's channel key hashes added (a
   pasted key or a stored hash anywhere, raw database bytes included). Any problem refuses with `leak`, names kinds
   and members only, and writes nothing to `--out`: extract never ships an archive its own verify rejects.

## Masking

Detectors (kind names appear in markers and in `masks[]`):

- patterns: `pem` (private key blocks), `anthropic` (`sk-ant-…`), `openai` (`sk-…`), `github` (`ghp_`/`gho_`/`ghu_`/
  `ghs_`/`ghr_`, `github_pat_…`), `aws` (`AKIA…`), `slack` (`xox?-…`), `telegram` (bot tokens `<digits>:<35 chars>`,
  also inside their API URL `…/bot<token>/getUpdates`; the whole digit run must be the bot id),
  `bearer` (`Authorization:` / `Bearer …`), `jwt`, `assign` (the value after `password`, `passwd`, `pwd`, `secret`,
  `secret_key`, `token`, `api_key`, `client_secret` followed by `=`, `:` or `is`; covers `.env` lines), `userinfo`
  (the password in `scheme://user:pass@`).
- JSON key names: in a config file, a journal line, a JSON row or a run line, an object member whose key names a
  secret (`password`, `passwd`, `passphrase`, `secret`, `secret_key`, `client_secret`, `secret_access_key`, `token`,
  `api_key`/`apikey`, `private_key`, `authorization`, also as the last word: `DB_PASSWORD`, `apiKey`, `X-Api-Key`,
  `access_token`) has its whole string or number value masked as `assign`; when the value is a list or an object,
  every non-empty string and number inside it, at any depth, is masked the same way (booleans and `null` are kept).
  `token_budget`, `input_tokens` or `token_file` are not secret names. `pwd` is not a key name here (shell environments use `PWD`
  for the working directory), though `pwd=` in text is still masked.
- exact channel keys: every token of 16+ characters, and every 64-hex window of a hex run (a key glued to `_`, `-` or
  other hex), is hashed and compared with the key hashes the home stored for its channels (current rows and every
  `channel.registered` / `channel.rotated` audit row) — `channel-key`; the stored hashes themselves are masked as
  `channel-key-hash`.

Overlapping hits are merged into one span (their union) with the kind of the longest, so no tail of a longer secret
survives next to a shorter hit.

A hit becomes `«masked:<kind>:<n>»` (n counts across the archive). `masks[]` records member, table, column, row,
JSON path or line, offset and length — never the value or a hash of it. Rows without a hit are not rewritten. In
`runs.jsonl` a free-text value whose raw form had a hit is exported as `{bytes, sha256: null, masked: [kinds]}`
instead of `{bytes, sha256}` (an unsalted hash of a short secret would let a guess be confirmed), and the run's
`kind: run` line carries `"masked": true`.

A marker-shaped text already in the home (a note quoting `«masked:assign:1»`) is not a mask: it is rewritten to
`«masked-literal:assign:1»` and listed in the manifest's `literal_markers`, so every `«masked:…»` in an archive is a
real mask and verify can account for it.

Fail-closed: a detector error, a value that is not UTF-8, an unknown table or home file, or a config file that does
not parse stops the extract with a named error; there is no skip option.

Limits, stated plainly:
- The archive holds markers, not secrets; nothing un-masks them. After a restore the operator re-provisions real
  credentials.
- A secret with no recognisable shape ("my wifi code is hunter2") is not masked.
- Over-masking is the chosen failure: "the token is expired" becomes "the token is «masked:assign:7»".
- Everything else — memories, messages, people's words — is plaintext and readable wherever the file is kept.
- Click and worker-hold records name their one-shot tokens `token`/`page_token`/`hit_token`; those values are masked
  in the archive, so a restored home's old click and hold records are not usable (they are stale after a move anyway).

## Verify

`home verify` (read-only) checks: every member against `SHA256SUMS` and the manifest; `PRAGMA integrity_check`; row
counts; run chains in `runs.jsonl` (prev/line hashes and the anchored receipt head; a run with masked values reports
`chain_ok (masked values)`); every marker is listed in `masks[]` and every entry has its marker; no detector pattern
hits any member. A meeting journal with masked lines reports `masked` with its pre-mask and archived heads instead of
failing. Exit 0 when `ok`, 1 otherwise.

What a pass guarantees: the members are the extracted ones, the database is consistent, counts match, chains hold,
every mask is accounted for and no pattern is left. It does not mean the content equals the source home (masked
values differ by design), and a reader's verify does not repeat the exact channel-key check because the archive keeps
no key hashes (extract's own verify does, step 5).

## Move (`extract --move`)

A deliberate migration. `--move` refuses while anything of the home runs: the worker LaunchAgent label
`com.synthetics.worker.<sha256(id)[:16]>` is loaded, the worker lock is held, or a Telegram adapter holds
`telegram.lock` or the `lock_file` named in the home's `telegram.json` (relative paths taken from the home; a
`telegram.json` that does not parse refuses with `telegram_config`). Satchel never stops or unloads anything itself; the operator does (`synthetics uninstall`, stop the
adapter). It then holds the worker lock for the whole extract and, only once the archive is in `--out`, writes
`SEALED.json` (archive name and sha256) into the home. A failed extract leaves no seal. The archive's manifest says
`kind: move`.

A sealed home takes nothing new, so nothing is silently lost in a home nobody runs:
- a worker refuses to start (`WorkerLock`); `serve` exits 0 so a LaunchAgent's KeepAlive does not relaunch it in a loop;
- `Store.feed` (every inbound event) and the channel contract (`pending`, `claim`, `ack`, `reconcile`, `uncertain`,
  and any keyed `synthetics channel` command, which fails as a whole with exit 2) refuse with code `sealed`, so the
  sender's adapter sees a failure and keeps or reports the message instead of recording it delivered.

Limit: a v2 Telegram homes config kept outside this home is not checked; stop that adapter by hand.

## Recreate

`recreate` verifies the archive first and refuses when it doesn't verify, when the archive's store version is newer
than this code, when `--into` exists and is not empty, when this employee's worker LaunchAgent is loaded on this
machine, when a move archive lacks `--from-move` (or a backup archive has it), and when a backup archive lacks
`--source-lost --attest "<why>"`. A backup means the source may still be running somewhere, so it is restored only for
a lost source; the attestation goes into the new home's audit log (`satchel.restored`).

It builds the new home in a private 0700 staging directory next to `--into` (`.<name>.partial-*`, same filesystem),
writes the database (migrated by the normal store migration), the config files, meeting journals and
`restored/{manifest.json,runs.jsonl}` (the Ledgerline export, kept for reference), checks the database's identity, then
does the following in **one** database transaction and writes `REPROVISION.md`; only then is the staging directory
renamed to `--into`. On any error (identity mismatch, a failed hold, …) the staging directory is removed: `--into` is
never left half-built and a retry works.

- runs recorded `running` at the snapshot become `interrupted` ("restored: interrupted at extract");
- every live access grant is revoked (`revoked_by: satchel-restore`); nothing is re-granted;
- no channel exists (keys were never archived), so old adapters cannot authenticate to the new home;
- a `runtime.json` `claude` path that is not executable here is removed (the default applies);
- for a **backup**, every outbox message that some channel has not finished (`pending`, `claimed` or `uncertain` on
  any channel, so delivered on one channel but pending on another counts) or that no channel has handled yet is
  **held**: it may already have been delivered by the lost home. Adapters don't see held messages and can't claim them. The operator lists them with `home held` and
  either `home release ID` (adapters may deliver it) or `home drop ID --reason` (never sent; recorded failed, also on
  channels provisioned later). Pending project captures are held too, released with `home release-captures`.
- `REPROVISION.md` in the new home lists the operator's steps: the grant commands (paths rewritten by `--map`),
  the channels to provision with their prefixes and routes, Telegram and LaunchAgent install, held items, and the
  mask counts per kind. Satchel runs none of them and never installs or starts a worker.

Limits: "live elsewhere" cannot be proven absent from another machine. A move seals the source; a backup restore needs
a written attestation; channels are never carried; REPROVISION.md tells the operator to revoke the Telegram bot token
if the old machine might come back. Answers that reached a lost home after the snapshot are lost. Masked values stay
markers. A move seal is a file: deleting `SEALED.json` by hand un-seals the home.
