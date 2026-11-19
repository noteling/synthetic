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
   `"yield": "a meeting is starting: save your next step with work_put and end now (stopped at <deadline>)"`.
   The field is added before the i17 receipt is written, so the receipt hashes what the session saw.
   List results get no field; the stop covers them.
3. **Stop.** The worker checks every second. Once the deadline passes it stops the session's process group
   (`ClaudeBrain.yield_stop`, the same `_stop_group` as a timeout). The run ends `failed` with error
   `yielded: meeting <id>` and field `yielded: <id>`, audited `floor.yield` with outcome `stopped`.
   A session that ends by itself first is recorded normally and audited `floor.yield` outcome `ended`.
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
