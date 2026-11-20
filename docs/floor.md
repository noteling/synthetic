# Floor: an employee in a meeting (MTG)

Floor is the Synthetics side of an employee taking part in a meeting by voice. Stand is the real-time
interface (audio, speech, the human's panel); Synthetics is the brain. The design is the MTG P0
(Vesper, approved by Codex 2026-11-19, review `management/realworld/meeting/p0-review.md`). It is built in
four Synthetics-only slices:

- **F1 yield** (this document's first part): a meeting takes the idle boundary from ordinary work.
- **F2 attach + meeting session**: the meeting's Claude session, journal, persona and brief.
- **F3 gate**: `floor_accept` and `floor verify`, so no invented commitment enters the records.
- **F4 opt-in and summary**: meetings only for homes the owner enabled (`floor.json`), and one compact
  `meeting summary` memory per meeting that `floor verify` checks; the journal is kept.

There is still only one Claude process per employee home at a time. A meeting does not run beside a
coding session; the coding session yields.

## F1: the yield and the attach hold

### Who starts it

`floor attach` is started by Stand with the provisioned `stand` channel key:

```sh
python3 -m synthetics channel provision stand --source-prefix stand: --home HOME   # operator, once
python3 -m synthetics --home HOME floor attach --key-file ~/.config/synthetics-channels/stand.key
```

- Only a key registered as the channel named `stand` is accepted; any other key, or none, is refused with
  `auth_failed`.
- Inside an employee session (`SYNTHETICS_SESSION_RUN` set) `floor attach` is refused with `in_session`.
- A home that hasn't opted in is refused with `not_enabled` (F4, below), before the begin line is read.
- Every refusal is audited as `floor.refused` (the code only; never the key or a line's content).
- The meeting store calls (`meeting_begin`, `meeting_attach`, …) refuse a Store bound to a session run, so
  no tool path can start or hold a meeting.

### Lines (JSON, one object per line, each with `"v": 1`)

| direction | line | meaning |
|---|---|---|
| in | `{"type": "begin", "meeting": ID, "grace": S}` | first line. `grace` is optional (default 45 s, 0-600) |
| in | `{"type": "end"}` or EOF | the meeting is over |
| out | `{"type": "waiting", "meeting", "yield_run", "deadline"}` | a session is running and has been asked to yield |
| out | `{"type": "ready", "meeting", "hold_expires_at"}` | the idle boundary is held for the meeting |
| out | `{"type": "ended", "meeting", "reason"}` | `end`, `eof` or `hold_lost` |
| out | `{"type": "error", "code", "message"}` | `auth_failed`, `in_session`, `bad_line`, `refused`, `busy`, `unsupported` |

F1 accepts only `begin` and `end`; other lines get `unsupported`. F2 adds `heard`, `spoken`, `note`, `ask`,
`cancel` and `confirm`.

### What happens

1. **begin** writes a pending meeting (audited `floor.meeting_pending`: meeting, channel, grace, the running
   run if any, its deadline). While it is pending the worker starts no new session (`run_begin` refuses
   with `WorkerHeld`). One meeting at a time per home; repeating `begin` for the same meeting is a no-op.
2. **Yield.** If a session was running at `begin`, every dict result of its Synthetics tools carries
   `"yield": "a meeting is starting: start no new outbound action (message, question, push); save your next step with work_put and end now (stopped at <deadline>)"`.
   The field is added before the i17 receipt is written, so the receipt hashes what the session saw.
   List results get no field; the stop covers them.
3. **Stop.** The worker checks every second. Once the deadline passes it stops the session's process group
   (`ClaudeBrain.yield_stop`, the same `_stop_group` as a timeout). The run ends `failed` with error
   `yielded: meeting <id>` and field `yielded: <id>`, audited `floor.yield` with outcome `stopped`.
   A session that ends by itself first is recorded normally and audited `floor.yield` outcome `ended`. If the
   stop was already sent but the CLI still returns a successful result (the turn completed as the stop landed),
   the run is recorded finished and the audit outcome is `stopped_completed`.
4. A yielded run is **not a verdict**: `failure_streak` skips it (it neither fails nor resets the streak),
   it decides no runtime probation, the worker's serve loop does not count it, and it quarantines no
   routine. Its inputs stay pending and go to the first session after the meeting.
5. **Attach.** As soon as no run is running, `floor attach` takes the idle hold as its own holder kind
   (`kind: floor`, audited `floor.attached`) and prints `ready`. It renews the hold every 60 s. The hold
   expires 5 min after the last renewal, so a killed attach frees the worker by itself.
6. **End.** `end` or EOF releases the hold (audited `floor.ended`); ordinary work resumes.

A pending meeting nobody attaches to lapses after its grace plus 5 min.
If the yield target isn't recorded as ended within grace + 60 s, attach exits with `busy` and drops the pending
meeting itself (audited `floor.ended`, reason `busy`), so ordinary work isn't held for nobody (#74 L3). The
worker's stop is retried every second until it reaches a running CLI process; a CLI not yet spawned when the
deadline passes is stopped as soon as it is (#74 L2).

### Without a meeting

Nothing changes: no tool result gains a field, no `floor.*` audit row is written, and run records keep
their shape (Codex C1, tested).

### Honest limits

- The yield is cooperative first, then a stop. A tool call in flight when the group is stopped may or may
  not have committed; as for a timeout, effects that exist stay recorded.
- A delegated worker op (`worker.manage`, D10c) that takes the hold while a meeting is pending makes the
  attach wait until that hold is released or expires.
- The `stand` key is a channel key like any other: a process running as the same OS user can read it
  (docs/channel-contract.md §7).

## F2: the meeting

A `begin` line with `goal` (the human's brief for this meeting) runs the meeting under the floor hold. Without
`goal` the attach only holds the idle boundary, as in F1.

    in:  {"type": "begin", "meeting", "goal", "started_by"?, "text_mode"?: "full"|"hash", "grace"?}
         {"type": "heard", "seq", "t"?, "text"}       room speech: speaker unidentified, never a verified person
         {"type": "spoken", "seq", "text", "played", "cut_at"?}
         {"type": "note", "seq", "text"}              the human's private panel note (a correction)
         {"type": "ask", "id", "rev", "question"}     rev = last seq Stand sent
         {"type": "cancel", "id"}   {"type": "confirm", "cid", "yes"}   {"type": "end"} or EOF
    out: {"type": "ready", "meeting", "run", "name", "role", "persona", "aliases", "brief_sha256", ...}
         {"type": "answer", "id", "rev", "text", "commitments": [{"cid", "text"}]}
         {"type": "stale", "id", "reason"}             a panel note arrived after the ask's rev; not spoken
         {"type": "error", "code", "message", "id"?}   bad_line, conflict, invalid_state, no_answer, session_failed
         {"type": "ended", "meeting", "reason", "head", "wrap_event"}

- **Persona (G3).** `ready` carries only identity and the goal plus the works the goal names by ID.
- **Run.** One run row of kind `meeting` (`store.meeting_run_begin`, needs this meeting's hold token), with
  `text_mode`. A worker restart doesn't recover it while its hold is live; it never counts toward failure_streak.
- **Session.** One persistent `claude --input-format stream-json --output-format stream-json` process
  (`floorsession.MeetingSession`) with `--tools ""` (no built-in tools) and `--allowedTools` limited to
  `floortools.MEETING_TOOLS`; its MCP server offers only those tools for a meeting run, in code (C5). Asks are
  answered one at a time, in order. Cancelling the ask in flight restarts the process with the journal tail.
- **Journal.** `HOME/meetings/<id>/journal.jsonl`, hash-chained, speaker class fixed by the line type.
  `text_mode: hash` stores only the size and sha256 of room words: a `heard` line's text and an `ask` line's
  question (C8). Panel notes and the employee's own answers are kept. The session itself still sees the question in
  memory while it answers, and a restarted session's journal tail then has no question text.
- **Stale (§4).** Only a panel `note` journaled after an ask makes that ask's answer `stale`. A `floor_note` the
  session writes never does: it answers asks in order, so the answer already accounts for its own correction, which
  the prompt asks it to say aloud (#76 r1 L4).
- **Memory (E3).** `memory_search` is one of the read-only meeting tools, so anything in the employee's memory can
  be spoken in the room. That is approved (E3); keep it in mind when a meeting has outside guests.
- **End.** `end`, EOF, the 90 min cap, a lost hold or a dead session: stop emitting, stop the session, journal
  still-open asks as `late`, close the journal, finish the run (with its cost), feed one wrap event through the
  stand channel (source `stand:meeting`, sender `stand`, body = pointer facts and the chain head, no text, no
  `question_id`), release the hold.
- **Panel lines (G4).** `note` and `confirm` live only in the journal (`human-panel`). They never become store
  events, so they can't answer a question, approve a click or stand in for the default human. The wrap event's
  sender is the channel, not the human.
- **Retention (G5, R3).** Meeting journals have no automatic expiry. E4 runs pruning leaves `meetings/` intact.

## F3: the gate (floor_accept and floor verify)

No invented commitment enters the records (P0 §3). Approved by Codex with Q1-Q4 and K1-K4
(`management/realworld/meeting/f3-review.md`).

### floor_accept(meeting, cid, title, priority?)

An MCP store tool for ordinary sessions (the wrap session). It is not in `floortools.MEETING_TOOLS`, so a meeting
run is never offered it, and the store refuses it from a meeting run as well.

- **Checks** (in this order): `cid` is a `commit` line in `HOME/meetings/<meeting>/journal.jsonl` (`unknown_cid`);
  the meeting has ended: an `end` line exists, and there is no pending row and no floor hold for it
  (`meeting_running`); the wrap event exists (external_id `floor:<meeting>:wrap`, source `<prefix>:meeting`, with an
  auth stamp from the `stand` channel: `no_wrap`; a `floor:<meeting>:wrap` row stamped by any other channel is
  ignored, so it can neither stand in for the wrap nor make every accept fail `chain_broken`, F3-lows L2); the journal chain verifies against the wrap event's `head` (`chain_broken`); the latest
  panel `confirm` for the cid is not `yes: false` (`declined`).
- **Effect**: one work with `origin: "meeting"`, description = the promise text verbatim (plus `Due (as said): …`
  when the commit had `due`), the caller's title and priority, a `meeting` field {id, cid, line, wrap_event}, and
  one `observed` evidence item citing the wrap event with note `meeting <id> journal line N, <cid> (<confirmed_by>)`.
  `confirmed_by` is `human-panel line N` (latest confirm yes) or `issued`.
- **Existing work** (K2): a commit naming `work_id` adds that evidence item to the work instead. `unknown_work` when
  it isn't a work of this home, `work_closed` when it is done or cancelled; it is never reopened.
- **Atomic and unique** (K1): the checks, the work write, the idempotency row (namespace `floor_accept`, key
  `<meeting>:<cid>`) and the audit row are one `BEGIN IMMEDIATE` transaction. A second call (or a concurrent one)
  returns the same work with `accepted.repeated: true`.
- **Audit**: `floor.accepted {meeting, cid, line, work, confirmed_by, existing, run}`; a refusal `floor.refused
  {command: floor_accept, code, cid, run}`. Never text.
- `work_put` refuses `origin: "meeting"`; the MCP enum stays `self|assigned`. A published meeting work shows
  `origin: meeting` in the backlog, and Lookout's Board marks it "(from a meeting)".

### floor verify RUN

`synthetics --home HOME floor verify RUN` (read-only, sqlro; fenced exactly like `doctor` and `routine verify`).
For each `stand:meeting` wrap event among the run's inputs it reads the run's MCP receipts (`runs/RUN.mcp.jsonl`):

- `chain`: the journal against the wrap head (`chain_ok`; anything else is a violation).
- `unattributed_meeting_work` (violation): a successful `work_put` whose title, description, note, waiting_for or an
  evidence ref/note **cites the meeting**: contains the meeting ID as a whole token (not inside a longer run of
  `[A-Za-z0-9_.:-]`), the path segment `meetings/<id>`, or the wrap event ID (full, or `event-` + 8 or more of its
  hex). Not a violation when the work_put's `id` is a work floor_accept created or extended for this meeting
  (`floor.accepted` audit rows), or when an evidence ref is a question that is `answered` (not proxy, not
  withdrawn), was addressed to the default human, whose answer event has a non-null auth (Codex Q2), and that was
  created at or after the meeting's journal `begin` line (F3-lows L1; compared as instants). The run's wrap events
  are only those stamped by the `stand` channel (L2).
- `room_correction_replaces` (violation): a successful `memory_put` with `replaces` whose `source` cites the meeting
  and names `line N` / `lines N` / `line #N` where journal line N is `room-unidentified`.
- Per meeting: `accepted`, `declined` and `unaccepted` cids (info).
- Verdict `clean`, `violations`, or `no_evidence` (no such run, no wrap event in its inputs, or no receipts).

### Honest limits

- verify makes a violation visible; it does not prevent it. Memories are flagged only (Q4), no new gate.
- The match is textual: a work that describes the meeting without its ID, path or wrap event ID is not seen.
- A spoken promise without a cid is not accepted by floor_accept (Q2 deferred): ask the human, cite the answered
  question. Panel-authored promises come with the Stand slice (Q3).
- If the store raises while attach drops a pending meeting on the busy path, attach still prints `busy` (exit 2)
  and makes one separate attempt to audit `floor.ended_failed` (#76 nit, K4).

## F4: opt-in per home, the meeting summary, retention

David (intent on the Stand question, 11-19): meetings are not for every employee for now, maybe the manager
only; the transcript stays in its own place, and the manager keeps a compact summary in memory with a link to
it. Codex's rulings R1-R4 (first live attendee: the manager home).

### Opt-in (R2)

- `HOME/floor.json` = `{"enabled": true}` opts a home in. Off by default: no file, `{"enabled": false}`, any other
  value, or a file that can't be read as a JSON object all mean not enabled. The owner or operator writes it
  (there is no CLI setter, like `click.json`); no name list is committed anywhere.
- `floor attach` checks it right after the key check, before reading the begin line: refused `not_enabled`
  (exit 2, audited `floor.refused` with the code), so there is no pending row, no yield of a running session and
  no hold.
- `doctor` shows `floor: {"floor_json": "enabled"|"absent"|"disabled"|"unreadable", "meetings": true|false}` and
  warns `floor_json_unreadable` for a file it can't read.
- Honest limit: like `click.json`, the file is not fenced against a session writing it with the shell. `floor
  attach` itself is still refused inside a session.

### Meeting summary (R4)

The wrap guidance (the Meetings block of the employee prompt) asks the wrap session for exactly one `memory_put`
with predicate `meeting summary` and the wrap event ID as `source`, whose value names the meeting ID, the journal
`meetings/<id>/journal.jsonl` with its line range and the wrap head sha256, what was decided (panel lines by
number) and open items. No new tool: the link fields are copied from the wrap event.

`floor verify RUN` reports per meeting `summary`:

- A summary is a successful `memory_put` whose predicate is `meeting summary` (case and surrounding space ignored)
  and whose source or value cites the meeting (the rule above). Idempotent retries returning the same memory ID
  count once; separate memories count separately.
- `present`: exactly one, and it is anchored: its source cites the wrap event ID (`event-` + 8 or more of its hex),
  and its value holds `meetings/<id>/journal.jsonl` (a relative or absolute path, with no filename suffix or extra
  path component) and at least the first 12 hex characters of the wrap head's sha256.
- `missing` (none), `duplicate` (more than one), `unanchored` (one, not anchored). Each is a `summary` violation
  with the receipt lines. verify only reports it; the wrap is never refused for it.
- A wrap run with no MCP receipts at all stays `no_evidence`, as before.

### Retention (R3)

The journal has no automatic expiry: nothing outside the floor modules touches `meetings/`, and the E4 runs
prune never does (guard test). The full transcript stays in the journal and never goes into project logs or
messages; durable memory holds only the summary, corrections and promises, each pointing at journal lines.
