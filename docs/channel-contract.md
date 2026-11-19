# Channel contract v1 (M25)

Synthetics does not depend on any one messaging provider. A company connects
its own channels (mail gateway, ticket system, chat bot, a shell script) through
one versioned, transport-neutral contract: the `synthetics channel` commands,
speaking JSON lines. Telegram is one optional adapter, not a requirement.

An adapter never imports the Store or opens SQLite. It runs

```sh
python3 -m synthetics --home HOME channel COMMAND --key-file KEY [--instance ID] [options]
```

once per employee home it serves. The maintained reference adapter is Postern
(docs/postern.md).

## 0. From nothing to a first reply (operator)

`--home` has two places. Before the subcommand it selects the one home a
command acts on (default `SYNTHETICS_HOME`, else `~/.synthetics/rowan`).
`channel provision` instead takes its homes **after** the subcommand, one
`--home` per home (repeatable, required).

```sh
H=/path/to/homes/alex-assistant
# 1. Create the employee. --name and --role are required. --human writes
#    humans.json for this fresh home (sender + default_human); each
#    --human-channel adds a source prefix whose events count as that human's.
python3 -m synthetics --home "$H" init --name "Alex's assistant" --role "assistant" \
    --human alex --human-channel postern:
# 2. Provision the channel (§1). The key goes to ~/.config/synthetics-channels
#    unless you pass --key-dir.
python3 -m synthetics channel provision postern --source-prefix postern: \
    --route alex --home "$H"
# 3. Run the employee (§6), then the adapter (docs/postern.md).
python3 -m synthetics --home "$H" worker install --runtime-config --limit-notify alex
```

- **Prerequisite:** the worker runs the Claude CLI (`--claude`, default
  `~/.local/bin/claude`, then `PATH`) as the same OS user, and that CLI must
  already be logged in **for the `HOME` the worker gets**. `worker install` writes the
  installer's own `HOME` into the plist (§6), so run `claude` once by hand in that
  same environment and check it answers before you install the worker. Do not
  install from a shell with a scratch `HOME`: claude there answers "Not logged in"
  and every event stays `accepted`. For a throwaway test company keep only the
  *keys* apart: `channel provision … --key-dir DIR` and an absolute `key_file` in
  `postern.json`.
- **Importing `synthetics`.** `python3 -m synthetics` works when the current
  directory is the Synthetics checkout or `PYTHONPATH` names it. The installed
  plists do not depend on your shell: they run the installer's interpreter
  (`sys.executable`, absolute) with `PYTHONPATH` and `WorkingDirectory` set to the
  checkout the installing command was imported from (§6).
- **Human prefixes.** `humans.json` `channel_prefixes` (or `--human-channel`) are
  matched against the inbound line's `source`, the same field the channel's
  `--source-prefix` checks. An event counts as a human's when its `sender` is in
  `senders` or its `source` starts with one of these prefixes. Use one prefix for
  both, e.g. `postern:`, and send `source` values such as `postern:shell`.
- **Reply address.** The employee answers with its own `message_send` /
  `question_ask`. It normally addresses the inbound event's `sender` (the prompt
  asks it to keep the thread and set `in_reply_to`). The adapter only sees a reply
  if that address is one of the channel's `--route`s. Route every sender you feed
  in. Otherwise the reply is still queued, but it waits undelivered: the
  employee's tool result shows `routed_by: []`; after 10 minutes `doctor` warns
  `outbox_stuck` and, because no channel routes the address,
  `outbox_unroutable`. `channel pending` reads the routes each time it runs, so
  adding the route later (`channel register`) releases the message on the next
  cycle.
- **Inside an employee session.** Every `channel` command and `init` (owner-only)
  are refused when `SYNTHETICS_SESSION_RUN` is set. A delegated session can run
  these steps only through a frozen kit that passes the `kit.run` check and acts
  on a scratch company it creates (docs/authority.md, "Kits"). On real homes they
  stay operator steps.

## 1. Provisioning (operator)

```sh
python3 -m synthetics channel provision NAME \
    --source-prefix NAME: --route ADDR [--route ADDR ...] \
    --home HOME [--home HOME ...] [--key-dir DIR]
```

writes `DIR/NAME.key` (32 random bytes, hex, mode 0600, never printed; `DIR`
defaults to `~/.config/synthetics-channels`, and `~` is expanded). If that file
already exists it is kept and re-registered, so provisioning is idempotent. Only
its SHA-256 is registered in each home. `channel register NAME --key-file F
--source-prefix ... --route ...` re-registers an existing key; the record keeps
its identity and the given routes replace the old ones. Both are refused inside
an employee session.

- **source prefixes**: an inbound line's `source` must start with one of them.
- **routes**: the recipient addresses (`to` of outgoing messages) this channel
  may see and deliver. Each channel only ever sees messages routed to it, which
  gives per-credential isolation. A channel with no routes delivers nothing
  (fail closed).

## 2. Common rules

- Every request and response line carries `"v": 1`. `channel version` (no key)
  prints `{"v":1,"supported":[1],"synthetics":"…"}`. A future v2 adds
  commands or fields; v1 lines keep working while `supported` lists 1.
- Every other command needs `--key-file`. The key selects the channel record.
- Exit status: `0` when every line was processed, including per-line
  rejections; `2` for a whole-command failure (`auth_failed`, `in_session`,
  usage). A whole-command failure prints one line
  `{"v":1,"result":"rejected","error":{"code":…,"message":…}}`.
- Commands are refused inside an employee session (`SYNTHETICS_SESSION_RUN`
  set). They are adapter and operator operations, never model tools.
- `--instance ID` names this adapter process. When a new instance first uses
  the channel, claims held by the previous instance become `uncertain` (§4).

### Error codes (closed set)

| code | meaning |
|---|---|
| `bad_version` | line `v` is not supported |
| `bad_line` | not JSON, not an object, or a field is missing or malformed |
| `auth_failed` | key file missing, unreadable, or not registered in this home |
| `source_not_allowed` | `source` does not start with one of the channel's prefixes |
| `not_routed` | the message (or event) does not belong to this channel |
| `unknown_message` | no such message or question in this home |
| `conflict` | the same idempotency key with different content, or a competing claim/outcome |
| `invalid_state` | the operation is not allowed in the current delivery state |
| `in_session` | refused inside an employee session |

## 3. Inbound

```sh
channel inbound --key-file KEY < lines.jsonl
```

One request line per message:

| field | required | meaning |
|---|---|---|
| `v` | yes | `1` |
| `external_id` | yes | the provider's stable message ID; the dedup key with `source` |
| `sender` | yes | the sender's identity as configured in `humans.json` or a colleague ID |
| `body` | yes | message text |
| `source` | yes | must start with a channel source prefix, e.g. `mail:inbox-1` |
| `occurred_at` | no | ISO 8601 time at the provider |
| `thread` | no | stable conversation ID |
| `in_reply_to` / `reply_to` | no | generic reply metadata; never answers a question |
| `question_id` | no | answers that open question (correlation) |
| `routed` | no | `true` only: see §3.1 |
| `recipient_work_id` | no | the receiving employee's work this concerns (M23) |
| `project` | no | single-project tag for unbound channels (M21) |
| `kind` | no | `"message"` (the only v1 kind) |

One response line per request line, in order:

```json
{"v":1,"external_id":"…","result":"accepted|duplicate|rejected","event_id":"event-…|null","error":null}
```

Every accepted event is stamped `auth: {channel: …}`. Redelivering the same
`(source, external_id)` with the same content returns `duplicate` and the
original `event_id`; different content returns `conflict`. Adapters may
therefore retry a whole file safely.

`channel event-status --event ID` prints one line, written by `json.dumps` with
its default separators (a space after each `:` and `,`), for example

```json
{"v": 1, "event_id": "event-…", "state": "accepted", "run_id": null, "question_id": "question-…", "in_reply_to": null}
{"v": 1, "event_id": "event-…", "state": "processed", "run_id": "run-…", "question_id": null, "in_reply_to": null}
```

`state` is `accepted` or `processed`, and `run_id` is `null` while `accepted`.
`question_id` is the question the event answered (its line's `question_id`), and
`in_reply_to` its generic reply metadata; both are `null` when absent (#25 P2,
additive). The output is IDs and state only, never a body or sender.

`channel event-status --source S --external-id X` finds the event this channel fed
with that `source` and `external_id`, so an adapter that lost an `inbound`
response can learn whether the line arrived. It answers only for this channel's
own events: a missing event and another channel's event get the same
`not_routed` refusal. Every lookup is audited as `channel.event_lookup` with the
channel, source, external ID and result, never a body. Give either `--event` or
both `--source` and `--external-id`; anything else is `bad_line`.
Parse the line as JSON; don't grep for `"state":"processed"`, which has no space
and never matches. The same applies to every contract response line. **processed** means the employee session that captured
the event has *finished*, not merely started. An event this channel did not
deliver is `not_routed`.

Expected latency: a running worker polls every `--poll` seconds (default 2) and
starts a session for new events. That session may take minutes, up to the
worker's `--timeout` (default 600 s), so poll `event-status` (every few seconds
is enough) rather than waiting a fixed time. Without a running worker
(`worker install`, §6, or `serve`) an event stays `accepted` indefinitely.

Inbound and delivery are separate steps. A reply exists only after the session
that read the message has run, so the adapter cycle that feeds a message never
also delivers its reply. Run the adapter continuously (Postern's default loop,
or as a launch agent), or run a later delivery cycle after `event-status` says
`processed`.

### 3.1 Answers relayed by an operator (C1)

An answer to a question must come from the question's recipient. When an
operator or secretary relays a human's answer (the human addressed by the
question did not type it into this channel), the line must say so:
`"question_id": "question-…", "routed": true`. Synthetics then records
`routed_by = channel:<id>` and `to_id = sender` on the question (audited
`question.routed`) in the same transaction that accepts the answer, so a line
rejected for any reason (for example a reused `external_id` with a different
body) leaves the question unstamped. Without `routed: true` such a
line is rejected with `invalid_state`. `routed: true` is honoured only when
`sender` is listed in the home's `humans.json` `operators.senders`; from any
other sender (or with no humans.json) the line is rejected with
`invalid_state` and the question is left unchanged. Senders listed under `operators` in
`humans.json` are never treated as the human, so a relayed answer is visible as
secondhand, not silently promoted.

### 3.2 Reading one question in full (`channel question`, #25)

```sh
channel question --key-file KEY --id question-…
```

prints one line with the full text of an open question addressed to this
home's human, so an answer surface (docs/answer-desk.md) can show what is
being answered; `status.json` carries only the first line.

```json
{"v": 1, "question_id": "question-…", "to": "david", "body": "…", "work_ids": ["work-…"], "created_at": "…", "thread": null, "error": null}
```

- It returns the body only when the question is `open`, its `to` is the
  home's `humans.json` `default_human`, it has not been routed to anyone else
  (`to_id` empty or that human), and the key belongs to a channel registered in
  this home. Every other case (unknown ID, answered or withdrawn, addressed to
  an employee, relayed to an operator, no `humans.json`) gets the same
  `unknown_message` line with the same message, so the response never says
  whether the question exists.
- It is read-only. Every call writes one audit row `channel.question_read`
  with the question ID, the channel and `returned|refused`, never the body.
  Only a full ID (`question-` plus 32 hex) is looked up; any other `--id`
  (a prefix, an over-long or odd string) gets the same refusal and is audited
  with the record ID `(malformed id)` instead of the string.
- A key from another home is `auth_failed` (exit 2, no audit row); inside an
  employee session it is `in_session`.
- A consumer must not log or cache the body.

## 4. Outbound

Delivery state is kept per message × channel:

```
pending → claimed → delivered | failed
                  ↘ uncertain → (reconcile) delivered | pending
failed (retryable) → pending after 60 s, attempt + 1
```

- `channel pending [--limit N]` (default 50, max 200) lists messages routed to
  this channel that are pending on it and not delivered by any channel:
  `{"v":1,"message_id","kind","question_id","to","body","thread","in_reply_to",
  "recipient_work_id","project","sender","created_at","attempt"}`.
- `channel claim --message ID` must succeed **before** any external side
  effect. It leases the message to this instance for 600 s and returns the same
  view with `"state":"claimed"`. A second claim gets `conflict`.
- `channel ack` reads outcome lines from stdin:
  `{"v":1,"message_id":…,"outcome":"delivered|failed|uncertain","receipt":"…","retryable":false,"detail":{}}`
  and answers `{"v":1,"message_id":…,"result":"recorded|duplicate|rejected","state":…,"error":…}`.
  Ack only a claimed message. Repeating the same outcome and receipt before
  the message is claimed again is `duplicate`; the same outcome on a later
  claim (a second retryable failure with no receipt, say) is that attempt's
  own outcome and is `recorded`. A different outcome after a final one is
  `conflict`.
- **No blind resend.** An expired lease, or a claim left by a replaced
  instance, becomes `uncertain`, never `pending`. `channel uncertain` lists
  them; `channel reconcile --message ID --outcome delivered|failed [--receipt R]
  [--note N] [--by WHO]` is the only way out. `failed` returns the message to
  pending as a recorded decision. Reconcile also re-opens a final `failed`.
- Once any channel records `delivered`, the message leaves every channel's
  pending list. A late ack from another channel is recorded, not a conflict.
  `outbox_list` shows the overall `delivery` state and `channel_states`.
- When should you ack `uncertain`? When the provider call timed out or its
  answer was lost: you do not know whether the human received it.

### 4.1 Hop notices (`kind: "hop"`, Docket P1b)

A store queues `kind: "hop"` rows by itself (never a session) when a question it
was asked is carried onward: the recipient asked someone else with `relays`,
that onward question was answered, or it was withdrawn. The row adds a
structured `"hop": {"question_id": <asker's question>, "hop":
"with-person|answered-pending-relay|with-manager", "person", "since", "via":
<the onward question>}`; `body` is only a fixed rendering of those fields.
Deliver it only into the asker's store with `Store.question_hop` after checking
that the asker asked `question_id` of this sender (Fieldpost: its correlation
table). It is never an inbox event, never wakes work and never answers. An
adapter that does not support hops acks `failed` (non-retryable); it must not
deliver one as a message.

Receipts are free text supplied by the adapter (e.g. `telegram:chat:ids`,
`file:<sha256>`). A receipt reference cited as work evidence counts at most as
*attempted* unless it is corroborated (M22).

## 5. Human identities

`<home>/humans.json` (v1) configures who is human in that home instead of
provider prefixes or a literal name in the code:

```json
{"version": 1, "default_human": "alex",
 "senders": ["alex"], "channel_prefixes": ["mail:"], "secretary_prefixes": [],
 "operators": {"senders": ["ops-desk"], "source_prefixes": ["operator-"]},
 "limit_bypass": {"senders": ["alex"], "channel_prefixes": ["mail:"], "require_auth": true}}
```

A home without `humans.json` has no humans. Operators map to secretary, never
human. The daily-limit bypass needs an authenticated event from a listed sender
on a listed channel, and the sender must also be allowed by the worker's
runtime settings. While the limit holds, every pending event is checked (not
only the oldest 50), so held peer traffic never hides an admissible message;
one session takes at most the 50 oldest admissible ones (issue #6).
`python3 -m synthetics --home HOME doctor` validates the
file. It exits `0` when it found no `error`-level finding, even if it printed
warnings such as `outbox_stuck`, and `1` when there is at least one error
(`--json` prints the findings with their `level`); `doctor --compare-legacy FILE` checks that a migrated home behaves as the
legacy configuration did.

Stuck delivery (M27). A channel record's routes say which addresses it may
deliver, but a relay that moves messages between homes keeps its own route
file. `doctor --relay-routes FILE [--relay-channel fieldpost]` compares the two
for this home: `route_without_relay` (the channel routes an address the relay
never carries from this home, so messages stay pending) and
`relay_without_channel_route` (the relay carries it but the channel would
answer `not_routed`). Every doctor run also warns `outbox_stuck` for this
home's messages still pending or uncertain after 10 minutes (younger than 48
hours), and `outbox_unroutable` when no channel routes their address. The
employee's own snapshot shows the same list as `outbox_stuck`.
Runtime-request outcome notices (applied, failed, reverted) are not queued
to an address that no channel record in the home routes, for example a request
filed `--by operator`; the request record already holds the outcome, and an
audit row `runtime.notice_unroutable` notes the skip. A home with no channel
records keeps queuing them as before.

An employee's own `message_send` or `question_ask` to such an address is still
queued, never refused. When the home has channel records, the tool result adds
`routed_by` (the channel IDs whose routes carry `to`) and, when that list is
empty, a `warning`; nothing extra is stored. `doctor` warns
`limit_notify_unroutable` when the runtime's `limit_notify` is routed by none of
the home's channels.

## 6. Installing workers and adapters

```sh
python3 -m synthetics --home HOME worker install --runtime-config --limit-notify ops-desk
python3 -m synthetics adapter install --label com.synthetics.adapter.mail -- /abs/python3 -m synthetics postern run --config /abs/postern.json
```

`worker install` writes a launch agent `com.synthetics.worker.<16 hex>`
(`--label` overrides) to `~/Library/LaunchAgents` (`--launch-agents-dir`
overrides) and starts it unless `--no-start`. The agent runs
`python3 -m synthetics --home HOME serve` with the given `--claude`, `--model`
(default `claude-opus-5-5`), `--budget` (per session, default 100), `--timeout`
(default 600 s), `--poll` (default 2 s) and `--daily-limit` (default 75). Its logs
go to `HOME/services/`. With `--runtime-config` the worker reads
`HOME/runtime.json` at each start. A missing file is seeded from these arguments,
and `--allow-model` / `--allow-claude` bound what the employee may later request
for itself. `worker start|stop|restart|remove [--label L]` control it; as with
every command, `--home` goes **before** `worker` and `--label` after the
subcommand, e.g. `python3 -m synthetics --home "$H" worker remove --label
com.synthetics.worker.<16 hex>` (without `--label` the default label of that
home's identity is used). `remove`
boots it out and deletes the plist; `remove --never-loaded` deletes a plist that
was written with `--no-start`.

**What the plist captures (both `worker install` and `adapter install`).** The
plist does not inherit your shell. At install time it records:
- `HOME`, `USER`, `LOGNAME` and `TMPDIR` of the installing user, as the
  installing process sees them;
- `PATH` = `$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin`;
- `PYTHONPATH` and `WorkingDirectory` = the Synthetics checkout the installing
  command was imported from;
- for a worker, `ProgramArguments[0]` = the installing interpreter
  (`sys.executable`, absolute), so the worker runs on the Python you installed with.
  An adapter runs exactly the argv you give after `--`.
The worker therefore uses the claude login of that `HOME`. If `HOME` was a
scratch directory when you installed, reinstall from a normal shell. An adapter's
`~` paths, such as `key_file` in `postern.json`, expand to that same `HOME`. For a quick foreground test, run
`python3 -m synthetics --home HOME serve --claude … --model …` in a terminal
instead.

A worker needs no adapter. When a worker stops after repeated failures it
queues an ordinary outbox message to `limit_notify`; whichever channel routes
that address delivers it. If the outbox write itself fails, the worker logs to
stderr and exits; there is no direct network fallback.

## 7. Security

- Anyone holding a channel key can speak as that channel within its source
  prefixes and routes. Keep key files 0600 and inbox directories not
  group/world-writable (Postern refuses to start otherwise).
- Enforcement is at the API level, not the OS level: processes running as the
  same OS user can read Synthetics state directly (MG-09).
- Unauthenticated input (no `auth` stamp) never wakes work, answers a
  verified-only check, or bypasses the daily limit.
