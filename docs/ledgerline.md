# Ledgerline: checkable evidence of a run's tool calls (issue #17)

Reviewers could not establish what a session had actually read: an MCP receipt recorded the call's arguments
and `success`, but nothing about the result. A dream whose `event_search` page spilled out of the client's
display still showed `success: true` (Vesper's run-30546289). Ledgerline adds result facts to the receipts,
a JSON-lines export and a check of a dream's checkpoint. Everything is local, read-only and stdlib; there is no
continuous sink and no OTLP. Approved as P0 by Codex (event-5ced3aac, conditions C1-C5).

## E1: receipt result facts and the hash chain (`synthetics/mcp.py`, `synthetics/ledgerline.py`)

Each line of `<home>/runs/<run>.mcp.jsonl` keeps `{at, run_id, tool, arguments, success, error?}` (view_put and
self_note text still redacted) and gains:

- `result` (successful calls): `{bytes, sha256}` of the exact text `tools/call` sends back, plus, never bodies:
  - `event_search`, `memory_search`: `ids` (in order, at most 200, `ids_cut` beyond), `count`, `created_first`,
    `created_last`, and as returned `capped`, `since`, `unread_from`, `next_cursor`, `truncated_from`;
    `truncated` lists the items whose `body_truncated` is true with `created_at` and `body_len`.
  - `event_get`: `id`, `created_at`, `body_len`, `body_offset`, `returned_chars`.
  - `employee_context`: `bytes` and `sha256` only. Any other tool: the result's record `id` if it has one.
- `result_error`: the exception type, if computing the facts failed.
- `prev_sha256`: sha256 of the previous line's bytes (`null` on the first line), written under an exclusive
  `flock` together with the append.

A receipt failure (hashing, tail read, write, fsync) never fails or alters the tool result (C4); it is reported
on stderr as before. At run end (`run_finish`, `run_fail`, `recover_runs`) the run record gets
`receipt_chain {head_sha256, lines}` (`{null, 0}` when the run made no call), so the head is anchored outside
`runs/`, in the run row and its `run.finished` / `run.failed` / `run.interrupted` audit row.

## E2: `synthetics --home H export runs [--run R | --since T] [--audit] [--out FILE]`

Writes JSON lines (stdout, or `--out` with mode 0600). It opens `state.sqlite3` read-only (`sqlro`), never a
Store, and writes nothing in the home. Lines:

- `kind: run`: `{run_id, status, started_at, finished_at, cost, event_ids, receipt_chain}`.
- `kind: mcp.call`: `{run_id, seq (line number), at, tool, success, args, args_sha256, line_sha256, prev_sha256,
  result, error_code | error}`. `args` is an **allow-list**: IDs, references, enums, numbers, timestamps and `key`
  (`ledgerline.ARG_KEEP`) are kept; free text (`body`, `value`, `note`, `description`, `text`, `reason`, `review`,
  `query`, `title`, ...) and any key not named becomes `{bytes, sha256}`; omissions keep only `from`.
  `args_sha256` covers the arguments as stored, so view_put/self_note are hashed only in their redacted form.
  An error exports its leading `code:` token, otherwise `{bytes, sha256}` of the text.
- `kind: audit` (with `--audit`): `{seq, at, action, record_id, data_sha256, fields}`; `fields` holds only the
  per-action allow-list in `ledgerline.AUDIT_FIELDS` (`authority.*`, `run.*`, `event.received`, `work.*` without
  title/description/note, `routine.*` checkpoint fields, `roster.view`/`roster.self`, `channel.registered` id).
  An action with no entry exports no `fields`. No allow-listed name matches a credential pattern (static test).

Query strings and work titles are hashed (Codex Q2); there is no clear-text flag.

## E3: `synthetics --home H routine verify RUN [--spill-bytes N]`

For a dream run, reads its `routine.rearmed` audit row (`coverage {run, cutoff, through, omissions}`), the
previous dream checkpoint (the preceding `routine.rearmed` row of the same routine; none means 1970, C3) and its
receipts, and builds the contiguous read chain per stream (events, memories):

1. A `since=` page continues the chain when its `since` ≤ the chain end, a `cursor=` page when its cursor is the
   previous page's `next_cursor`. Its end is `created_last`, or the cutoff when `capped` is false.
2. A page the client did not display whole stops the chain at its `created_first`. With an executor stream
   (E4 below) this is **observed**: the receipt's `tool_use_id` joins the stream's `displayed` fact, and the page
   counts as seen only when its shape is `full` and its sha256 equals the returned `result.sha256`; otherwise the
   break is `spilled` (`spill: observed`). A receipt without a join (a run before E4, a `stream_output: false`
   session, an `unknown` shape) falls back to the prediction: a page whose `result.bytes` exceeds the spill
   threshold (default 32000, F2's `max_bytes`) is `possibly_spilled`. **That is predicted, not observed** (C2);
   `spill_check` says which applied (`observed`, `mixed: N observed, M predicted`, or the predicted note).
   A smaller re-read of the same range (same `since`, or the same `cursor`, at a lower `max_bytes`, as the dream
   guidance says) heals the break: verify keeps scanning, and a spill counts only if no seen page carries the
   chain past its `chain_at` (where the spill stopped the chain: its `from`, unless the chain already stood further
   on); healed spills are listed under the stream's `healed` (B1, r2).
3. A `truncated` item is unread unless `event_get` receipts for its ID cover `0..body_len`; the chain stops at its
   `created_at`. An `event_get` receipt covers only when its `result.bytes` is at or under the spill threshold: a
   plain `event_get` of a large body can spill too (listed under `spilled_event_get`). For a plain `event_get`,
   `body_len` and `returned_chars` come from the returned body.

**Scope: a dream's own reads (G1).** Verify reads only this run's receipts. Reads made in another session (for
example an ordinary catch-up session) never support this dream's checkpoint, so a dream that advances
`covered_through` over material it did not read itself is `overclaimed`. The dream guidance says the same; the
output carries it as `scope`.

**What is verifiable (G3).** Only MCP reads leave receipts. Opening a spilled result's saved file with the
client's own file tool leaves none, so it can't support a checkpoint: re-read with a smaller `max_bytes`, or
read a large body with `event_get` chunks (`body_offset`). Each break and each stream also prints the page's
`unread_from` (G4), to match against an omission's `from`.

Verdict: `overclaimed` (with `first_unread`) when `through` is past the smaller chain end or an omission's `from`;
`spilled` (observed) / `possibly_spilled` (predicted) / `unread_truncated` when a break in the chain has no recorded omission at or before it;
`consistent` otherwise; `no_evidence` for an unknown run, no `routine.rearmed` row for a dream by this run (C3),
no receipt file, or `since=` receipts written before E1. `chain` reports `chain_ok`, `chain_broken`
(an edited line, or a head that differs from the anchored `receipt_chain`), `unanchored` (still running,
finished before E1, or lines without `prev_sha256`) or `no_receipts`.

## Access (C5)

`export runs` and `routine verify` use doctor's G4 fence (`cli._doctor_gate`): the owner on any home; a session
on its own home, or on another home only with an active `worker.manage` / `adapter.manage` / `runtime.request`
delegation covering it (`adapter.manage` is scoped by labels, so today it covers no home)
(audited `authority.used`); otherwise `own_home_only`, audited `authority.refused` with `what` naming the command.

## Honest limits

- A receipt proves what the MCP server returned, not what the model attended to.
- A session can rewrite its own `runs/` files and, by stripping its environment (it then looks like the owner), the run record too
  (`docs/authority.md` §0). The chain and anchor make an edit visible; they are not a sandbox.
- `covered_through` is one timestamp for both streams, so the verdict uses the smaller chain end.
- A capped page's chain end is its `created_last`, though items tied on that timestamp may sit on the next page.
  A `through` equal to it is accepted because `since=` is inclusive: the next dream re-reads every item at that
  timestamp, so nothing is skipped (L1).
- Verify judges one run (G1): a catch-up read in another session doesn't count for this dream (L3).
- E4's `displayed` facts are what the client reported in its stream, not what the model attended to; a session
  can rewrite `stream.jsonl` like its receipts (the `stream_chain` anchor makes that visible). The join needs the
  client to pass `claudecode/toolUseId`; a client that stops sending it leaves receipts unjoined, and verify
  falls back to the prediction.

## E4: the executor stream and runs/ retention (Codex event-8b40b227, C1-C6)

Proposal `reviews/E4/proposal.md` (Vesper's workspace), approved with Q1 E4-B (no raw stream on disk).

- **E4a, join key.** The MCP server reads `params._meta["claudecode/toolUseId"]` (a plain token, at most 128
  characters) and adds it to the receipt as `tool_use_id`. The response bytes are the same with or without it.
- **E4b, display facts.** `brain.py` runs the session with `--output-format stream-json --verbose`. stdout is a
  pipe read line by line by a reducer thread (`ledgerline.StreamReducer`); raw lines, which hold everything the
  model saw (other people's messages, memory values, private views, shell output), are never written to disk. The
  reducer appends facts to `runs/<run>.stream.jsonl` (0600, `prev_sha256` chain, head anchored in the run record
  as `stream_chain`, only when the file exists). One line per event, keys limited to `ledgerline.STREAM_ALLOWED`:
  `init {model, claude_code_version, tools, mcp_servers [{name, status}]}`; `tool_use {tool_use_id, name,
  input_bytes, input_sha256}`; `displayed {tool_use_id, shape, bytes, sha256, reported_chars?,
  saved_outside_home?}` with shape `full`, `spilled` (the client's "exceeds maximum allowed tokens" form),
  `preview` (`<persisted-output>`), `error`, or `unknown` (never guessed); `turn {text_blocks, text_bytes}`;
  `result {subtype, is_error, num_turns, total_cost_usd, duration_ms}`; `stream_error {stage, type}`. Never text,
  input, thinking, paths, cwd, session ID, socket path, credential expiry or memory paths (static test,
  `STREAM_FORBIDDEN`). The stream's `result` line is kept apart before any reduction and written to
  `<run>.claude.json` as json mode wrote it, so cost, budget-cap and error handling are unchanged; a reducer or
  stream-file failure only means fewer facts.
- **Fallback (C1).** `runtime.json` `"stream_output": false` restores the pre-E4 `--output-format json` path
  exactly (same argv and files). It is optional: a config without it stays valid and keeps its digest. Change it
  with `synthetics --home H runtime request --set stream_output=false --reason R --by ID` (not employee
  self-service); it applies when the worker restarts or loads the config.
- **E4c.** `routine verify` judges each joined page by what was displayed (step 2 above). The display threshold
  is host-specific: on Vesper's host Claude Code 2.1.326 showed 40 105-character MCP results whole and spilled
  70 240 (token-based), so the predicted 32 000 over-flags; it stays for runs without a join (C5), since
  over-flagging is the safe direction.
- **Export.** `export runs` adds `kind: stream` lines: `{run_id, seq, stream_kind, line_sha256, prev_sha256,
  facts}`; a field not allowed for its kind becomes `{bytes, sha256}`. Same fence as the rest of export.
- **What the facts say about other people.** Only sha256 of displayed text that may contain their words (the
  same exposure as E1's `result.sha256`). Claude Code's own spill files outside the home are issue #64, out of
  scope here.

**runs/ retention (Codex Q2, C3).** `Store.runs_prune`: keep 30 days and at most 200 MB per home's `runs/`
(`runtime.json` `runs_keep_days`, `runs_max_mb`, both optional). Only regular files named `<run-id>.<suffix>`
directly in `runs/` for a known run that is `finished`, `failed` or `interrupted`, oldest first; never a symlink,
a directory, a file of an unknown run or anything outside `runs/`. Never pruned: a running run, the newest 50, a
run named by a routine's `coverage.run`, `rearmed_by_run` or `last_review.run`, a run cited as work evidence, a
failed run whose cost is unknown (cost backfill reads its `claude.json`). The run rows and their
`receipt_chain` anchors stay, so a pruned run verifies `no_receipts` with the anchor visible; cost comes from the
row. The worker prunes at an idle point at most once per UTC day; `synthetics --home H prune-runs [--dry-run]
[--keep-days N] [--max-mb N]` runs it by hand: the owner on any home, a session on its own home only (refused
`own_home_only` otherwise, audited `authority.refused`; an allowed session prune is audited `authority.used`).
No approval step. A prune that removes something writes audit `runs.pruned {runs, files, bytes, oldest, newest,
kept_pinned, size_before, size_after, ...}`. Doctor reports info `runs_size` when `runs/` is over 80% of its
bound.

Measurements (C4) are in the E4 build handoff.

## Tests

`tests/test_i17_ledgerline.py` (E1, chain, C4) and `tests/test_i17_export_verify.py` (E2 allow-list, static
credential names, unchanged home bytes; E3 fixtures incl. the run-30546289 shape; C5 fence).
`tests/test_e4_stream.py` (E4a join key, reducer shapes and allow-list, C1 json fallback and identical results,
timeout, E4c observed verdicts, stream export) and `tests/test_e4_retention.py` (bounds, pins, file rules, dry
run, C3 unchanged costs/Lookout/doctor/recovery, CLI fence, once-a-day idle prune).
