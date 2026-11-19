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
2. A page whose `result.bytes` exceeds the spill threshold (default 32000, F2's `max_bytes`) is
   `possibly_spilled`: the chain stops at its `created_first`. **This is predicted, not observed** (C2): only an
   executor stream (E4, not built) would show what the client displayed. The output says so in `spill_check`.
   A smaller re-read of the same range (same `since`, or the same `cursor`, at a lower `max_bytes`, as the dream
   guidance says) heals the break: verify keeps scanning, and a spill counts only if no seen page carries the
   chain past its `from`; healed spills are listed under the stream's `healed` (B1, r2).
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
`possibly_spilled` / `unread_truncated` when a break in the chain has no recorded omission at or before it;
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

## Not in this slice

E4, the executor stream (`--output-format stream-json`, needs its own redaction design), and `runs/` retention
(none exists; it must never prune a run named by a routine's `coverage.run` or a work's evidence) are separate
proposal-first follow-ups.

## Tests

`tests/test_i17_ledgerline.py` (E1, chain, C4) and `tests/test_i17_export_verify.py` (E2 allow-list, static
credential names, unchanged home bytes; E3 fixtures incl. the run-30546289 shape; C5 fence).
