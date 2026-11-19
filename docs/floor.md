# Floor: an employee in a meeting (MTG)

Floor is the Synthetics side of an employee taking part in a meeting by voice. Stand is the real-time
interface (audio, speech, the human's panel); Synthetics is the brain. The design is the MTG P0
(Vesper, approved by Codex 2026-11-19, review `management/realworld/meeting/p0-review.md`). It is built in
three Synthetics-only slices:

- **F1 yield** (this document's first part): a meeting takes the idle boundary from ordinary work.
- **F2 attach + meeting session**: the meeting's Claude session, journal, persona and brief.
- **F3 gate**: `floor_accept` and `floor verify`, so no invented commitment enters the records.

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

## F2: the meeting (in progress, not frozen)

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
         {"type": "stale", "id", "reason"}             a correction arrived after the ask's rev; not spoken
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
  `text_mode: hash` stores only a hash of heard room text (C8).
- **End.** `end`, EOF, the 90 min cap, a lost hold or a dead session: stop emitting, stop the session, journal
  still-open asks as `late`, close the journal, finish the run (with its cost), feed one wrap event through the
  stand channel (source `stand:meeting`, sender `stand`, body = pointer facts and the chain head, no text, no
  `question_id`), release the hold.
- **Panel lines (G4).** `note` and `confirm` live only in the journal (`human-panel`). They never become store
  events, so they can't answer a question, approve a click or stand in for the default human. The wrap event's
  sender is the channel, not the human.
- **Retention (G5).** F2 never prunes `meetings/`. A pruning slice must pin journals cited by memories or work
  evidence first.
