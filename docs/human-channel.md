# Human conversations

The first external channel is a dedicated Telegram bot talking to one explicitly
paired human in a private conversation. The bot is an address for an employee;
Telegram is a replaceable transport. The employee's identity, work, questions,
memory and conversation belong to Synthetics.

Telegram is David's personal adapter on this host, not a requirement. Any
company channel connects through the transport-neutral contract in
docs/channel-contract.md; Postern (docs/postern.md) is the maintained
reference adapter. Who counts as human in each home comes from
`<home>/humans.json`, not from provider prefixes in the code.

## Current human workflow

From November 17, David gives orders, decisions, and answers in the Synthetics
desktop chat. The active desktop operator forwards them into the same manager
inbox with their original message ID, time, source, and continuing conversation
thread. Team questions and status updates go to David through Telegram, with
the manager identifying the employee involved. Answers given in the desktop chat
are linked to the exact tracked question using `reply_to`.

This is an operating policy over the existing bidirectional adapter. Desktop
handoffs happen during active desktop turns; an automatic background desktop
bridge is not installed. Imported past discussion is reference history rather
than a new instruction or proof of a delivered message.

Worker launch agents on the example host explicitly use `~/.local/bin/claude`.
The native 2.1.326 installation passed signature, official checksum, and startup
checks when the Homebrew 2.1.311 executable was blocked before application startup
on November 17. Preserve that executable choice when replacing worker settings.

## Authenticated input (M22)

Only input stamped by an authenticated channel can be `verified` evidence or let
David's message through a daily-limit pause. Each channel holds a key file under
`~/.config/synthetics-channels/` (0600, outside every employee grant). Every home
stores only the key's sha256. The operator provisions a channel once, outside any
worker session. The command never prints the key and is refused when
`SYNTHETICS_SESSION_RUN` is set:

    python3 -m synthetics channel provision codex-desktop --source-prefix codex-desktop: \
        --home <manager home> [--home <other home> ...]

The desktop operator forwards David's desktop messages with exactly:

    python3 -m synthetics --home <manager home> feed "<David's text>" --from david \
        --source codex-desktop:<desktop thread id> --thread codex:<conversation id> \
        --external-id <desktop item id> [--reply-to question-<id>] \
        --credential-file ~/.config/synthetics-channels/codex-desktop.key

Operator context notices (sender `codex-desktop`, free-form sources such as
`operator-runtime-cutover`) are not David's words. Feed them without
`--credential-file`; the key is refused for a source outside its prefix.
Telegram adapters read the key from `credential_file` in their `telegram.json`,
and Fieldpost reads it from `credential_file` in its route config. Unauthenticated
input still arrives, still answers a question and wakes its work, and shows
`auth: null` to the employee. It supports at most `observed` evidence.

## Conversation continuity

Incoming and outgoing messages retain a local `thread` and generic `in_reply_to`.
Generic reply metadata does not answer a tracked question. An authenticated reply
to a delivered question can additionally resolve its exact local `question_id`,
which resumes the linked work. An unrelated message cannot answer an arbitrary
open question.

The work engine includes the latest 30 items from each conversation represented
in its captured input batch. It preserves older items in full. `thread_history`
and the CLI page through both sides, including processed messages and sourced
historical imports. The snapshot exposes truncation and an older-page cursor.
Inputs arriving during a work session remain for the next session. Pin a continuing
conversation ID in employee memory or work so a later reminder can retrieve it.

```sh
python3 -m synthetics --home EMPLOYEE_HOME thread CONVERSATION_ID --limit 30
python3 -m synthetics --home EMPLOYEE_HOME send david "An update from me." \
  --thread CONVERSATION_ID --key unique-update-key
```

`send` queues an actual employee-authored outgoing message. It does not manually
inject an incoming event or certify external delivery. Models use the same
durable `message_send` and `question_ask` tools.

## A sourced handoff from the desktop conversation

The installed Codex CLI can read one explicitly selected conversation through
the supported app-server history API. Its default shared-daemon socket is absent;
the desktop app exposes no reachable named socket for attaching another client
to the live session. Starting another executor against the desktop's active
conversation would create a competing owner. We do not do that.

The maintained exporter preserves the available user-visible text, authorship,
original item/turn identifiers, timestamps and attachment references. It excludes
hidden reasoning, tool records and binary attachments. The export reports
compaction markers and availability limits rather than claiming to recover
deleted or unavailable history.

```sh
python3 -m synthetics --home EMPLOYEE_HOME handoff-codex EXACT_CODEX_THREAD_ID \
  --snapshot /absolute/private/path/conversation.json
```

This imports reference history without creating pending inputs, outgoing messages,
questions or assignments. It pins the local conversation ID in the employee's
self memory. Bind the messenger to that same local conversation ID to continue it.
The employee keeps its existing permanent ID. A headless work session is a new
execution session, possibly using a different model backend; it is not a remote
connection to the running desktop model.

The snapshot is dated, not ongoing synchronization with future desktop messages.
An active source turn may change after export. Conflicting reimports are rejected
rather than overwriting earlier evidence. Conversation through the connected
messenger subsequently accumulates directly in the employee's store.

**Project capture (M21).** Since 2026-11-17 (binding `pb-557d0220…`, active from
manager seq 177), every message on the manager's desktop thread
`codex:00000000-0000-4000-8000-000000000001` whose source starts with
`codex-desktop:00000000` or `desktop-secretary-` is captured automatically into
project `synthetics`, in both directions, with its auth as received. The desktop
operator decides nothing per message: no tag, no selection, and a mention of
another project does not make it private. Earlier history enters only by deliberate
promotion. Telegram, operator notices and other channels stay unbound; list the
current bindings with `python3 -m synthetics --home MANAGER_HOME project bindings`.

## Work references between employees (M23)

`work_id` on `send`, `message_send` and `question_ask` is always the sender's own
work. `recipient_work_id` names the recipient's work and is only a hint unless
authenticated. Fieldpost passes it through and adds a `for-work:` header line.
The recipient's Store stamps the event's `work_ref` (`found`,
`status_at_receipt`, `effect`). An unknown or malformed ID is still delivered,
with `found: false`, and audited. A ref changes state only when the event is
channel-authenticated, from another sender, and names waiting work that is not
waiting on an open question. Then that work goes from waiting to ready, and
nothing else changes. The replaced wait stays on the work as `woken_by_ref`
(`prior_waiting_for`, `prior_wake_at`), and the audit row is
`work.awakened_by_ref`. Question-linked work resumes only through its question.

## Provider and host boundaries

The installed first channel is bound to the manager employee. Individual workers
currently communicate with that manager through the existing local peer relay.
Direct worker-to-human routing with trustworthy author labels is a separate next
increment; the manager must not present its own forwarded words as another
employee's direct message.

Telegram ordinary bot messaging uses its Bot API; incoming updates use long
polling, without opening a public port. Its [API documentation](https://core.telegram.org/bots/api#getupdates)
describes cursor acknowledgement and webhook incompatibility. A provider-accepted
outgoing message is not evidence the human read it or that the employee processed
a reply. An ambiguous send timeout must remain uncertain until reconciled.

Pairing must prove possession of a one-time link generated locally, then bind the
immutable Telegram user and private chat IDs. A supplied display name is not
authentication. Bot tokens stay in private local files and must never be printed
in logs, error messages or delivery receipts.

The Mac remains a resource requirement. A background process can outlive the
desktop UI, but sleeping, offline or powered-off hardware cannot receive input or
reason. Telegram retains undelivered updates for at most 24 hours, according to
its [update contract](https://core.telegram.org/bots/api#getting-updates).
Always-on hosting is a later deployment decision; a local channel is not a
promise of continuous availability.

The adapter supports text and explicit attachment notices, not attachment
downloads or voice understanding. Use Telegram's Reply action when answering a
tracked question so the exact question can resume its work. An ordinary new
message remains part of the conversation without automatically closing a question.

The initial pairing wakeup is best-effort if the employee store fails at that
moment; the authenticated binding is retained. Ordinary human message writes are
retryable and cannot be acknowledged past a failed item in the batch; the retry
backs off (1 s doubling to 60 s) instead of polling the same update every second.
Feed errors are classified (issue #5): a channel key that is not registered in the
home, or whose source prefixes do not allow `telegram:<bot id>`, stops the adapter
with that reason, at startup and if it happens later, without moving the cursor.
Any other refusal (`ValueError`) of one update is recorded in the state's
`feed_rejections` (the last 50: update ID, time, error; never the text) and the
cursor moves on, so later messages are not blocked. `telegram status` shows the
count and the latest record. Ambiguous
outbound sends remain uncertain without automatic resending. Keep the channel
state and its adjacent initialization guard together when backing them up.

## Operating the installed manager

Configuration and private delivery state live in the manager employee home, while
the token is stored separately. `telegram status` reads pairing and receipt counts
without reading the token. `thread` retains both sides and full older pages.

```sh
python3 -m synthetics --home .synthetics/manager telegram status
python3 -m synthetics --home .synthetics/manager thread \
  codex:00000000-0000-4000-8000-000000000001 --limit 30
```

The adapter's macOS launch agent restarts on exit, throttled to 30 seconds. The
worker starts at login, retains its three-failure stop and is not automatically
restarted into repeated failed model calls. The current manager uses Claude Opus 5.5
through Claude CLI, a 600-second timeout and a $100 API-equivalent usage cap per reasoning session. Idle polling
does not call the model; there is no aggregate lifetime usage cap yet.

The adapter checks outgoing work at one-second intervals under normal provider
behavior, including while incoming requests are backing off. Model reasoning,
provider rate limits and network request timeouts can add their own latency. A
ready reply no longer waits behind a 30-second incoming poll.

While a live employee session handles a captured Telegram message, the adapter
shows Telegram's temporary typing indicator in that conversation. It stops when
the employee queues its reply or the run ends. This shows session activity;
authored replies and delivery receipts remain the evidence of a response.

For an explicit code restart, let launchd own the transition:

```sh
python3 -m synthetics --home .synthetics/manager telegram restart --worker-only
# Or restart the adapter first and the worker last:
python3 -m synthetics --home .synthetics/manager telegram restart --with-worker
```

Save deployment progress before requesting a self-restart: the current model
session may be interrupted and its input replayed. A worker must not stop itself
and then try to start its replacement from the child process being terminated.
An explicit `install --with-worker` now starts a loaded-but-stopped job instead of
mistaking its presence in launchd for a running process. Manual stop and the
three-failure stop retain their meaning; the worker is not set to restart forever.

Restart uses the job's loaded configuration. For changed model/budget arguments,
an outside operator must unload the worker and install the revised configuration.
The November 16 review recovery used a $3/600-second bound after repeated $1 budget
stops. Budget-stop results now report that reason explicitly rather than a missing
final summary. David subsequently requested a $20 cap instead of the restrictive
$1–$3 caps, then upgraded all employees to Opus 5.5 with a $100 cap. The current
runtime defaults and live workers use `claude-opus-5-5` and $100 per session.
This is a CLI token-cost guard, not a subscription charge.

The installed labels are `com.synthetics.telegram.c3467cce258dfd7e` and
`com.synthetics.worker.c5988b50a869cc30`. Private process logs are in
`.synthetics/manager/services/`. Operator evidence and acceptance are under
`.synthetics/management/M03/`. Tests use temporary homes and fake HTTP endpoints;
the optional `scripts/human_channel_model_check.py` separately exercises a real
Claude session and consumes model allocation.

## Several managers in one Telegram chat (#100 P2 + P3, not live until P4)

A config with a `homes` list (`"version": 2`) lets one bot and one `getUpdates` poller serve several employee
homes. **Not enabled live until P2 and P3 are both merged and running together** (Codex F8, R4); the first
live two-home config is the P4 check (secretary walk). Until then keep the live config single-home (today's v1
file, which still runs the unchanged single-home adapter).

```json
{"version": 2, "token_file": "token", "credential_file": "telegram.key",
 "state_file": "telegram.state.json", "lock_file": "telegram.lock",
 "homes": [
  {"key": "synthetics", "home": "/abs/manager", "employee_id": "employee-…", "colleague_id": "david",
   "label": "🟦 Codex · Synthetics", "button": "🟦 Synthetics · Codex", "shortcut": "c", "thread": "codex:<uuid>",
   "primary": true},
  {"key": "noteling", "home": "/abs/quill", "employee_id": "employee-…", "colleague_id": "david",
   "label": "🟨 Quill · Noteling", "button": "🟨 Noteling · Quill", "shortcut": "q", "thread": null,
   "not_before": "<ISO>"}]}
```

- Paths are relative to the config file. Exactly one entry is `primary`; key, home, employee_id, label and
  button are unique; every entry names the same `colleague_id` (one paired human). Unknown fields are refused.
- **Every home, the primary included, is reached only through the channel contract** (`channel pending`,
  `claim`, `ack`, `inbound`, `identity`), never the Store (F2a). Register the same telegram key in each home
  (`channel provision telegram --source-prefix telegram: --route david --home A --home B`).
- **Pinned identity (C4).** At start each home's `channel identity` must equal its `employee_id`. A home that
  fails (or whose outbox lines name another sender) is stopped alone; the others keep working, and David gets
  one `↪ Router` notice naming the unreachable label. A message meant for a stopped home is held with a notice
  saying that manager can't be reached. With a single configured home there is no other manager, so (as in v1)
  the message is not held: the cursor stays put and the update is retried with backoff until the home is back
  (an adapter restart re-checks the pin; Telegram keeps an update up to 24 hours).
- **Header.** Every part of a delivered message starts with that home's `label` on its own line, rendered by the
  adapter from the home whose outbox row it delivers, never from the model's text. A body whose first line is
  exactly the label loses that one line (`header_deduped`). The Byline footer stays on the last part only;
  every part is at most 4096 characters. The receipt is `telegram:<chat>:<ids>;header:<sha8>[;footer:<sha8>]`.
- **Replies.** Every sent part and every fed message is mapped (`route_map`: Telegram message id → home,
  employee, local message or event id, question); it keeps the newest 2000 ids, and a Reply to an older one
  counts as unknown. A Reply goes to the mapped home if it is still configured with the same employee: a Reply
  to a question's part answers that question; if that question is closed, the same
  line is fed again without `question_id`, as ordinary context. `in_reply_to` is the replied-to local message
  (or event) id, else `tg:<message_id>`. A Reply to an unknown target, to a pre-upgrade message that carried no
  question, or to a home no longer configured is **held** (at most 20, durable) and never guessed; the chooser
  below releases held messages. With a single configured home there is nothing to choose, so it goes to that
  home.
- **Provider ids.** When `channel version` lists `inbound.provider`, each fed line carries `provider`
  (`message_id`, `chat_id`, `reply_to_message_id`); against an older Synthetics it is left out (C3).
- **Upgrade.** The state file moves from schema 3 to 4 on first start: each old `question_map` entry becomes a
  route to the primary home's exact question; old deliveries are tagged with the primary home; the old
  bindings (employee, colleague, thread) now belong to the primary entry and keep their refusals. The bindings
  are checked before anything is written, so a refused start leaves the state file as it was.
- **No flood.** A home sends only rows created after its cutoff: the later of its `not_before` and the baseline
  the state keeps (the primary keeps today's `outbox_baseline_at`; a home first seen with neither gets the start
  time).
- **Typing.** From each home's `shared/status.json` (session running) while that home owes David an answer
  (it was fed a message and has delivered nothing since). No status file: no typing.
- `telegram install --with-worker --config F` installs the primary home's worker, notifying the primary
  entry's `colleague_id`.
- `telegram status --config F` on the primary home shows the homes (key, label, primary), schema, held count,
  the selected home's key and route-map size.
- Limit: an accepted send whose `ack` failed is marked in the local journal (no `acked`) and its claim lapses to
  `uncertain` in that home; nothing is resent, and `channel reconcile` settles it.

### The chooser (#100 P3)

David never has to type a routing word. With two or more homes:

- **Keyboard.** Every message the bot sends to the paired chat (a manager's message on its last part, every
  `↪ Router` notice) carries a persistent reply keyboard (`is_persistent`, `one_time_keyboard: false`): one
  button per home, in config order. The **saved selection** has a `✓ ` mark and sets the input placeholder
  `To: <button>` (`Choose who to talk to` with none). A Quill message delivered while Codex is selected still
  shows ✓ on Codex. A single-home v2 config sends no keyboard.
- **Selecting.** Tapping a button sends its text as a message. The adapter treats a message as a selection only
  when it passed the paired gate (private chat, not a bot, the paired user, the paired chat) and its whole text,
  trimmed, is exactly a button's text (with or without `✓ `), with no caption, forward, `via_bot`, quote or
  Reply. It is saved first, never fed to any home, journaled (`route_map` `kind: "select"`, no body), and one
  Router notice `Talking to: <button>` confirms it. David typing the exact button text himself is the same
  choice and is treated the same (stated limit).
- **Plain messages** go to the saved selection. With none (or a selection whose home is gone or re-pinned) the
  message is held.
- **Reply** rules are unchanged and win over the selection. A plain Reply never changes the selection (F6); a
  Reply that also starts with a directive naming the same home does (it selects like any directive, T21).
- **Held messages** get one Router notice each, `Held: not sent to any manager yet. Who should get this?`, with
  inline buttons `sel:<key>:<gen>`. Tapping one, a keyboard button or a directive (alone or with content) saves
  that selection and feeds the held messages to that home in arrival order, before any new content; each held
  notice is then edited to `Sent to: <button>` (its buttons go). Over 20 held, the new message is not kept and a
  distinct Router notice says so.
- **A Reply to a manager that can't be reached** is held for that manager only: its notice says so and offers no
  other manager, another manager's tap leaves it held, and it is fed (answering its question) when David next
  chooses that manager after the operator has fixed it. A message held for any other reason (no selection,
  unknown Reply target, a Reply/directive conflict, a plain message to an unreachable manager) goes to whichever
  manager David chooses.
- **A failed release** (the home's store refuses for a moment) keeps that message and the rest held. A tapped
  inline button's callback id is recorded only after its release finished, and the generation moves on only
  then, so Telegram's redelivery of the same tap finishes the job instead of being refused as a replay; the
  redelivery does not send `Talking to:` a second time. A directive with content waits the same way: if an
  earlier held message can't be released, the new content is not fed either (the update is retried), so it never
  jumps ahead of what was held before it.
- **C1.** A held message's body leaves the state file once it is fed or refused; `held_done` keeps only
  `update_id`, home, event id, outcome and time (last 200).
- **Callbacks (C2).** Accepted only from the paired user in the paired chat, not a bot, for a configured key, the
  current generation (every release starts a new one, so older inline buttons are stale) and a callback id not
  seen in the last 200. Beyond that window a replay can't arrive: callback updates share the `getUpdates`
  `update_id` cursor (Telegram does not resend a confirmed update), and the adapter itself never processes an
  update at or below its saved cursor. Refusals are recorded like other rejections and the button gets a short
  "out of date" answer.
- **Optional shortcuts.** A home may set `shortcut` (1-8 `[a-z0-9]`, unique). `for <shortcut>` at the very start
  of a message (case-insensitive, leading space allowed), followed by end of text, a newline or one of
  `: , . - —` (a plain space is **not** a separator, so `for quality` and `for c reasons` route nothing):
  alone it selects like a tap; with content it selects that home and feeds the content with the directive
  removed, and the line's `provider.directive` keeps the exact removed prefix (prefix + body = what David typed).
  A Reply to one home with a directive naming another is held for the chooser; agreeing ones route normally.
  Nothing tells David to use it.
- **Config check.** A button that would read as a directive, a button equal to a shortcut, a button starting
  with `✓` or `/`, or one too long for the 64-character placeholder stops the adapter at load with a clear
  message; nothing is skipped silently.
- `/to` (also `/to@<bot>`) re-sends the Router notice with the keyboard; `/to <shortcut|key|button>` selects. Optional; no text
  ever tells David to type it.
- **Restart** keeps the selection and held messages; the adapter sends nothing by itself on start, and the next
  bot message refreshes the keyboard.
