# Synthetics

A digital employee with its own identity, memory, working relationships, communication,
and continuing work. The employee exists across projects and model sessions.

The first increment is a local work engine and CLI. Claude Code or Codex CLI supplies reasoning
and local execution; Synthetics owns durable identity, memory, inbox/outbox,
assignments, questions, follow-ups, and recovery. Voice and desktop interaction come
later. Noteling and Stand have been studied as reference projects and remain untouched.

Repeated event data is sent to reasoning executors using a [lossless context view](docs/prompt-context.md).
The employee's full durable records remain intact.

## Start an employee

Requires macOS or Linux, Python 3.9+, and an installed Claude CLI or Codex CLI with a saved
login or a [configured model connection](docs/brain-config.md). Claude remains
the default executor. There are no Python
runtime dependencies. Run from this repository, or install with `pip install .`
(`pip install -e .` needs pip 21.3 or newer; the stock macOS Command Line Tools pip is older).

Choose an employee home. Every command below uses the same directory. `--home` comes
before the subcommand. Without it, the default is `~/.synthetics/default`; setting
`SYNTHETICS_HOME` is another way to choose the employee. The examples below use a
home for an example employee called Rowan.

```sh
python3 -m synthetics --home .synthetics/rowan init \
  --name Rowan --role "Noteling maintainer" --human david \
  --purpose "Learn the product, maintain it thoughtfully, investigate independently, and collaborate with David."

python3 -m synthetics --home .synthetics/rowan run
python3 -m synthetics --home .synthetics/rowan outbox
```

Initialization creates a permanent employee ID, a self entity, an initial colleague
(`david`), a workspace, and an onboarding message. On a fresh home `--human ID` is required (unless you
pass `--by ID`): it writes `<home>/humans.json` naming that person as the home's human (`default_human` and
sender), and that person becomes the initial colleague. Without either flag, init fails with `no --by given
and <home>/humans.json has no default_human` and leaves an empty humans.json behind; a rerun with `--human`
then refuses because the file exists. To recover, set `"default_human": "david"` and `"senders": ["david"]`
in that humans.json and rerun init (no `--human`), or remove that empty file and rerun with `--human`. The first reasoning session can
introduce the employee and ask what it needs. Initial project ignorance is expected.
No Noteling checkout or company accounts are assigned automatically.

To use Codex for bounded work, select it explicitly:

```sh
python3 -m synthetics --home .synthetics/rowan run --brain codex
python3 -m synthetics --home .synthetics/rowan serve --brain codex
```

`--codex /absolute/path/to/codex` selects an executable, and `--model` optionally selects a
Codex model. Without a model, Codex uses its own default. This adapter uses saved ChatGPT login;
it does not require an API key and does not fall back to API billing. Choosing an executor
does not create another employee or change its durable identity and work. Codex has no equivalent
USD session-budget cap or USD cost report; token usage is retained separately and cost stays
unknown. Its persistent live-meeting session is not supported in this increment. See
[executors](docs/executors.md) for setup, capabilities and restrictions.

For a company-provided model connection, supply one configuration path:

```sh
python3 -m synthetics --home .synthetics/rowan run --brain-config /company/synthetics/brain.json
```

The file selects the agent runtime separately from the model endpoint, protocol, model alias and
credential references. A supported company gateway requires configuration rather than changes to
the employee engine. The same interface supports saved CLI logins. See
[reasoning configuration](docs/brain-config.md) for the format, supported protocols and worker setup.

The output includes queued messages and questions. Reply to a question using its
`question_id` from the outbox (or its `id` from status.questions):

```sh
python3 -m synthetics --home .synthetics/rowan feed \
  "Let us start with the release process. I decide when a prepared release is published." \
  --from david --reply-to question-REPLACE_WITH_ID

python3 -m synthetics --home .synthetics/rowan run --turns 3
```

The sender must match the question's recipient. Answers are recorded as source events
and wake the linked work. A new conversation that is not answering a question can
simply omit `--reply-to`.

## Stay available and feed in signals

```sh
python3 -m synthetics --home .synthetics/rowan serve
```

Leave this process running and communicate from another terminal. It calls the model
when events, ready work, or due follow-ups need attention. Idle polling does not call
the model. Ready work continues without another prompt. Waiting work resumes on a
correlated answer or a scheduled wake-up. Ctrl-C and SIGTERM stop the worker and its
model process; restarting keeps the employee's state.

`status` reports whether a worker is running. From another terminal,
`python3 -m synthetics --home .synthetics/rowan stop` requests a graceful stop.

```sh
python3 -m synthetics --home .synthetics/rowan feed \
  "A colleague reported that a hint moved after a page reload. Investigate once a working copy is assigned." \
  --from david --thread hint-reload --external-id example-message-1

python3 -m synthetics --home .synthetics/rowan work add \
  "Learn Noteling's architecture" --description "Read the assigned sources and ask about undocumented decisions."

python3 -m synthetics --home .synthetics/rowan status
python3 -m synthetics --home .synthetics/rowan outbox
```

For another program, `ingest` reads one event object per line and returns one result
per line. For example:

```json
{"sender":"david","body":"Here is the answer to your question.","source":"manual","external_id":"kt-2","thread":"onboarding","reply_to":"question-...","occurred_at":"2026-11-15T15:00:00-05:00"}
```

Required fields are `sender` and `body`. Other fields are optional. The original event
time, receipt time, sender, thread, source, and reply correlation remain available.
Repeated `source` + `external_id` deliveries return the same event; conflicting reuse
is rejected. Feed this JSON through stdin to `python3 -m synthetics --home ... ingest`.

This is the integration boundary for chat, email, repository, and meeting adapters.
The CLI itself is a trusted local input interface: a supplied sender label is not
authentication. External delivery requires a separately configured adapter.

## Optional: talk through Telegram

Telegram is one optional adapter, not a requirement. Any channel can reach an
employee through the transport-neutral [channel contract](docs/channel-contract.md)
(`feed`/`ingest` in, outbox and delivery acknowledgements out). A dedicated bot can deliver private messages to an existing employee inbox and send
the employee's addressed messages and questions back to its paired human. The
employee worker must also run to read input and act. Idle transport polling does
not call a model. Identity, memory and both sides of the conversation remain in
Synthetics across restarts.

Create a bot through Telegram's `@BotFather`, save its token in a local file with
permissions `600`, then configure an existing employee:

```sh
python3 -m synthetics --home EMPLOYEE_HOME telegram init \
  --token-file /absolute/private/token-file --thread david-conversation
python3 -m synthetics --home EMPLOYEE_HOME telegram serve
```

Open the one-time pairing link printed by the adapter. A display name or an arbitrary
first message cannot claim the human identity. Keep the worker running with `serve`
in another terminal. On macOS, stop the foreground adapter before installing it:

```sh
python3 -m synthetics --home EMPLOYEE_HOME telegram install --with-worker
python3 -m synthetics --home EMPLOYEE_HOME telegram status
python3 -m synthetics --home EMPLOYEE_HOME thread david-conversation
```

The launch agents continue after the desktop UI closes, in the logged-in macOS user
session. A sleeping/offline Mac remains unavailable. The worker retains its bounded
failure stop; it does not endlessly restart failed model calls. `telegram stop
--with-worker` stops these services for the current login session.

To continue a saved Codex conversation, use `handoff-codex` before pairing and bind
Telegram to the returned local thread ID. This is a sourced handoff into the same
durable employee identity, using a new headless work session. It does not attach
to the running desktop model. See [human conversations](docs/human-channel.md) for
history, delivery, hosting and provider limits.

## Inspect memory and work

```sh
python3 -m synthetics --home .synthetics/rowan entities
python3 -m synthetics --home .synthetics/rowan recall "release"
python3 -m synthetics --home .synthetics/rowan history "release"
python3 -m synthetics --home .synthetics/rowan work list
python3 -m synthetics --home .synthetics/rowan runs
python3 -m synthetics --home .synthetics/rowan audit
```

Memory records connect a subject entity to a named relationship or property, a value,
kind, and source. Kinds distinguish facts, beliefs, preferences, and procedures.
Corrections supersede older records without losing their history. The employee can
look up processed source events long after the originating work session ended.
The first implementation uses explicit records and text search; it has no embeddings
or graph database. The snapshot pins self memories and makes truncation visible.

Work can be `ready`, `waiting`, `done`, or `cancelled`. A note carries progress,
evidence, uncertainty, and what comes next. Questions and messages use stable operation
keys to reconcile retries. A final model response alone does not finish an assignment
or save what the employee learned: its durable tools do that.

## Execution and limits

`run` executes at most one model session unless `--turns N` is supplied. `serve`
continues until stopped. Each model session defaults to Claude Opus 5.5
(`claude-opus-5-5`), a five-minute timeout, and a $100 API-equivalent usage cap.
Opus 5.5 requires Claude Code 2.1.280 or newer. Override with `--model`,
`--timeout`, and `--budget`. The dollar cap uses Claude CLI's token-cost estimate;
it is not a subscription charge or a change to the account's billing settings.
These limits are per session, not a total daemon budget. Failed sessions
retain inputs, back off for 60 seconds, and stop the worker after three consecutive
failures. Model usage may consume the host's subscription or API allocation.

The model receives the employee context and an MCP server with memory, work,
communication, and reminder operations. It also has Claude's Read, Write, Edit,
Grep, Glob, and Bash tools for local work. User/project Claude settings, skills,
browser integration, and unrelated MCP servers are excluded. Authentication still
uses the host's Claude installation.

**This is trusted local execution under the host account. The workspace is not an OS
sandbox.** The employee is instructed to keep artifacts in its workspace and to leave
the original Noteling and Stand checkouts unchanged. No separate OS, repository,
email, or directory-service account is provisioned yet. Assign a separate working
copy before actual maintenance. See [platform assumptions](docs/platform-assumptions.md).

**Output too large to show is saved outside the employee home.** Claude Code saves such a tool result to a
file in its own storage, under `~/.claude` by default. One probe (2.1.326, 2026-11-19) saw this with session
persistence off, as Synthetics runs it. The file holds whatever the command or tool returned, which can include
messages, memories and private views. The home's `runs/` retention, `export` and Satchel do not cover it.

What follows is Claude Code's documentation as read on 2026-11-22, not tested here:

- A successful command's output over roughly 30,000 characters goes to a file in "the session directory". A
  successful MCP result over 25,000 tokens or 50,000 characters is saved too; for the token limit the docs name a
  `tool-results` folder under `~/.claude/projects/`.
- Claude Code removes old files after `cleanupPeriodDays` (30 by default), but pauses that sweep when user
  settings are excluded and no enabled source provides `cleanupPeriodDays`. Synthetics excludes user settings, so
  its sessions may not sweep. Managed settings could provide the value, and the owner's own Claude Code sessions
  on the same host may sweep the same files.

What Synthetics does: a model's call to either search tool can't ask for more than 32,000 bytes (a margin we
chose, not a measured limit; one memory larger than that is still returned whole). Other tool results are not all
bounded. Employees are told to send large command output to a workspace file. That is advice, not enforcement, and
`runs/` retention does not prune workspace files and Satchel does not archive them.

What the owner can do: treat `~/.claude` as holding employee data when you back up, share or retire the host.
Shortening `cleanupPeriodDays` or deleting folders under `~/.claude` also reaches the owner's own transcripts, so
neither removes only employee files.

Sources: Claude Code's [tools reference](https://code.claude.com/docs/en/tools-reference),
[MCP output limits](https://code.claude.com/docs/en/mcp), [application data](https://code.claude.com/docs/en/claude-directory)
and [retention sweep event](https://code.claude.com/docs/en/monitoring-usage).

Inputs received during reasoning are retained for the next session. This increment
does not interrupt a model turn to deliver them immediately. Questions and answers
are therefore asynchronous. The service must be running for timers to execute; due
timers are reconciled on restart. On macOS, `worker install` can run it as a launchd
agent (see the [channel contract](docs/channel-contract.md)).

The runtime records durable state changes, MCP call receipts (with result sizes, hashes and read IDs, hash-chained
and anchored in the run record; `export runs` and `routine verify`, [docs/ledgerline.md](docs/ledgerline.md)), and
model summaries. Built-in shell/file tool outcomes are not independently journaled. Model judgment,
truthful reporting, and useful prioritization are capabilities to evaluate through actual work. Database
recovery cannot prove that an arbitrary Bash or external effect happened exactly once.

Attribution on outbound artifacts is opt-in per home: the owner sets `attribution.json` with
`synthetics byline set`, scoped to named repos and channels. Without it nothing changes. It adds a commit
trailer or footer as provenance and never touches the commit author or copyright
([docs/byline.md](docs/byline.md)).

## Development

```sh
python3 -m unittest discover -v
```

The automated suite uses temporary employee homes, fake model processes, and a real
MCP subprocess. It covers restart/continuation, cross-project memory, questions and
answers, historical evidence, duplicate events, interrupted sessions, stale process
fencing, timers, idle behavior, process cleanup, and CLI communication.

An optional real-model exercise is `python3 scripts/live_acceptance.py --home PATH`.
It creates a separate test employee and consumes Claude usage. It never runs as part
of the automated test suite. The report describes exactly what was exercised.

[Design](docs/design.md) · [Platform assumptions](docs/platform-assumptions.md) · [Validation](docs/validation.md)

[OpenClaw comparison and open gaps](docs/openclaw-gap-review.md) records existing
overlap, unresolved product capabilities, and the source-discovery gap to investigate.

## Not yet included

This repository holds the core engine, CLI, MCP server, docs and tests. Not yet
included: the internal tools that relay messages between employees (Fieldpost) and
the operator console (Fieldstone), a team installer / `team init`, and support for
employees on different computers working as one team.

## The employee maintaining this project

The paragraph below describes the example host this project is developed on.

A second employee, Vesper, maintains Synthetics itself and actively investigates
missing capabilities. It has its own memory, source working copy, and permission
to create internal projects beneath a separate internal-tools directory
(`~/projects/synthetics-internal` on the example host).
It chooses the names of those tools. See [its remit and operation](docs/maintainer.md).

Operator-provisioned access is durable and visible to the employee:

```sh
python3 -m synthetics --home .synthetics/internal-maintainer access list
```

`access grant` and `access revoke` manage intended scope. These declarations guide
the employee and Claude's directory setup; they do not implement an OS sandbox.

## License

The code is licensed under the [Apache License 2.0](LICENSE) (© 2026 Yang Lu; see
[NOTICE](NOTICE)). Employee homes under `.synthetics/` — memories, transcripts,
databases and channel keys — are private operational data and are never part of
this repository.

Host-specific protected paths are not in the shipped prompt. They come from host
rules: the company policy `host.rules` and an optional untracked
`<home>/prompt.local.md` (see docs/authority.md). `synthetics --home H system-prompt`
shows the prompt a home's next session will get.
