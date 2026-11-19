# Lookout: the human visibility page (VIS P1)

Lookout is a local, read-only web page. It shows a human what the team is doing without
Telegram and without access to any employee's private store.

```sh
python3 -m synthetics --home HOME web [--root ROOT] [--port 8765]
```

`ROOT` is the directory that holds the employee homes, `projects/` and `company/` (default: the
directory holding `--home`). The server binds to `127.0.0.1` only; any other `--bind` is
refused. It answers GET and HEAD only; other methods get 405. Every page is rendered from the
records on each request, nothing is cached or written, and every value is HTML-escaped. The
pages need no JavaScript, and a Content-Security-Policy forbids scripts.

## Pages

Every page answers three questions: who it is for, what they come for, and how they get there
quickly (VIS-O, David intent pm-09b11d39). Each page opens with a one-line caption saying so. The
nav is Overview · Projects · Diagnostics; a project's pages share the sub-tabs Open work · Threads ·
Board · Memory · David's direction.

| Path | For whom | What for |
|---|---|---|
| `/` | David (human management) | **Overview**, in plain words, without labels, PIDs, run IDs or costs (IDs only as tooltips). **Problems**, only when there are any, one linked line each: a worker not loaded or not running or inconsistent, a session past its timeout, a home without status, an out-of-date status, a pause, a quarantined routine or an error routine flag, stuck messages that no channel can deliver or whose delivery was not confirmed (pending on a routed channel, such as a pull channel a secretary drains, is not a problem and shows on the details page only), an adapter loaded but not running. **Needs you**: every home's open asks of the human (the Docket; a home without a Docket contributes its open questions to that human), oldest first: who asks, the first line, when, the works it holds up, and how to answer in one phrase from the delivery route ("reply on Telegram", or "not delivered to you yet"). **Goals**: the manager's open non-routine works, at most 6 by priority (then most recently updated), with status in words; work and question IDs in waiting_for read as the work's title or "<name>'s answer". The manager is the employee the homes' runtime `limit_notify` names (routines E2's source, via `runtime.state.json`), resolved through company aliases; none or a tie is said plainly. **Team**: one line per employee: name, short role, working now / idle / needs attention, open and waiting counts (and how many wait on the human), the top ready work. Footer: each project's David's direction, Projects, Diagnostics. |
| `/e/<home>` | Maintainers (manager, Synthetics maintainer, the employee) | **Details** of one employee, as the pre-VIS-O Team page showed it: three liveness states, work with IDs, routines and flags, Docket, open questions (relay hops linked, G3), last 5 sessions with cost, last 5 glances (RW-1), stuck outbox with how many are a problem and the drill-down. Sections have anchors (`#service`, `#work`, `#routines`, `#docket`, `#questions`, `#sessions`, `#glances`, `#outbox`) that the overview's problem lines link to. An unknown home is 404. |
| `/diagnostics` | Maintainers | Every home's worker row (label, loaded, launchd pid, lock pid, alive, session, status age), then other people from the company log, then loaded adapters. |
| `/team` | Maintainers | Every home's details block on one page, then People and Adapters (the pre-VIS-O `/`). |
| `/projects` | Everyone | The project list, each with its open work and David's direction. |
| `/p/<project>` | David and the manager | **Open work** (the landing page; same as `/p/<project>/work`, VIS G1): published works that are ready or waiting, grouped by owner name, with status, priority, title (work ID as tooltip), waiting_for and blocked_by in plain words, the age of the owner's latest projection, and the count of closed rows left to the board. Unpublished work shows only on its owner's details page. |
| `/p/<project>/direction` | Everyone | David's own words: the "Standing direction" section of the project memory view (current intents and claims) only. |
| `/p/<project>/threads` | David or a reviewer | Captured conversations, newest first. "About" shows the titles of the conversation's works (G-a1), else its thread ID. |
| `/p/<project>/thread/<key>` | David or a reviewer | One conversation, both directions in time order, with sender, recipient, time, auth, channel and which home captured it. Paged at 50 messages (G10): the newest page by default, `?page=N` (1 = oldest) for the others; an out-of-range or malformed N shows the nearest page. |
| `/p/<project>/board` | The team | Board backlog (with blocked_by) and posts. |
| `/p/<project>/memory` | Auditors | The M21 project memory view (projectview.render), then its sources (VIS G5) in a collapsed section: every source the view counts, newest first, with seq, time, sender, role, auth channel, coverage (considered ×N / not considered), a redacted mark, and a link to the thread that shows it. Two captures of one message link to the same thread. Text is never in the table; it stays on the thread page. |

A project with no `log.jsonl` (for example an empty `noteling` store) renders as empty.

## Names, titles and times (VIS lows)

- **Names (G6).** Senders, recipients, thread participants, board owners and post authors, open-question
  recipients and stuck-message recipients show the company profile name; the raw ID is the tooltip. An address
  with no profile is shown as it is.
- **Work titles (G-a1).** A work ID is shown by its title: first from the project's backlog (published works), else
  from a home's `status.json` works (open works, already shown on the details pages). An unknown work stays an ID.
- **Times (G9).** Timestamps read "5m ago", "in 2h 3m" or "just now", with the stored timestamp as the tooltip
  (`<time datetime=…>`). The open-work page's "updated" column keeps its plain age.
- **Updated (G7).** The details page work table and the board backlog have an "updated" column: the work's
  `updated_at` from status.json, and the backlog row's latest projection time.
- **Stuck outbox (G8).** Under the stuck count, a collapsed table lists the newest stuck messages (as many as the
  employee's own snapshot shows): message ID, linked question, recipient, kind, state, the channels that route it
  (or "no channel routes this address") and age. Messages older than 48 hours are counted only. No body is shown or
  exported.

## Boundary

Lookout reads only these files. It never opens a SQLite database.

| Shown | Read from | Never shown (why) |
|---|---|---|
| Message text, sender, recipient, time, auth, direction | `projects/<p>/log.jsonl` sources (bytes of redacted sources are dropped) | Events and outbox rows in home databases: private; only tagged or bound captures are shared |
| Intents, lessons, history, provenance flags | `projects/<p>/log.jsonl` via projectview.render | — |
| Board backlog and posts | `projects/<p>/log.jsonl` (`backlog`, `post`) | — |
| Profiles, roles, aliases | `company/log.jsonl` (company fold) | Private views and self notes (`<home>/roster/`) |
| Work title, status, priority, waiting_for, wake_at, waiting question IDs, updated_at | `<home>/shared/status.json` | Work description and note |
| Stuck outbox items: message ID, recipient, kind, question ID, state, age, routing channel IDs (G8) | `<home>/shared/status.json` (`outbox_stuck.items`) | The message body |
| Docket asks (first line, ages, ack, hop and the onward question it went via, carried question IDs and askers, blocked works) | `<home>/shared/status.json` | The rest of the question body |
| Open questions: first line, recipient, age, hop and the onward question it went via (`hop_via`), the askers' questions it carries (`carries`: question ID and asker ID only) | `<home>/shared/status.json` | The rest of the question body; the body of a relayed question |
| Session id, status, times, cost, event count, failed tool count | `<home>/shared/status.json` | Run summary, error text, transcripts (`<home>/runs/`) |
| Screen glances (RW-1): time, run, status, Noteling's counts, the name/role asked, match kind, count, unique, visible/enabled per hit; `site` as its host only (no port, path, query or fragment) | `<home>/shared/status.json` (`glances`, from the home's `glances` table, already whitelisted by `synthetics/glance.py`) | Page text (never requested: no `--full`/`--contains`), hit labels and frames (the employee's tool result only; an alike label carries page words) |
| Liveness | `launchctl list`, `<home>/runtime.state.json`, `<home>/worker.lock` (its pid text; no lock is taken), `<home>/shared/status.json` | — |

Lookout never reads a home's `state.sqlite3`, `roster/`, `runs/`, `workspace/`, `humans.json` or
`runtime.json`. Memories, views, self notes and run summaries are never shown.

## The one file added under a home

`<home>/shared/status.json` is the only file VIS adds under an employee home. The engine writes it
from the home's own database (`synthetics/sharedstatus.py`), replacing it atomically with mode 0600:

- when the worker starts (after recovering interrupted runs);
- when a session starts, and when it finishes or fails;
- when admission pauses (daily limit, recent failures) or the pause ends;
- after a session's successful `work_put`, `question_ask`, `question_close`, `question_resolve` or
  `question_relays` (the MCP server writes it; it keeps the pause the engine last wrote).

A failed write never fails the operation that triggered it; the next trigger rewrites the file.
Its fields (v1; fields are added, never renamed): `v, employee, name, role, written_at, pid,
session {state, run, started_at, paused}, works[] (G7 adds updated_at), docket, docket_error, questions_open[] (G3 adds hop_via, carries), runs[]
(newest 10), outbox_stuck {count, older_than_48h, items[] (G8)}`.

## Liveness: three states

1. **Service.** Loaded or not loaded in launchd (by the label in `runtime.state.json`), its launchd
   pid, and the pid in `worker.lock` with whether that process is alive. A disagreement is shown as
   a mismatch.
2. **Session.** `running` when status.json says running and the worker is alive; `suspect` when it
   has been running longer than the effective timeout plus 5 minutes; `stale (worker not running)`
   when the worker is gone or status.json was written before the worker started; otherwise `idle`
   or `paused`. A stale file is never shown as running.
   `written_at` carries microseconds, like `runtime.state.json` `started_at`; a whole-second
   `written_at` (a file written before P1-r2) is compared with the start time truncated to the second.
3. **Waiting on.** Each waiting work's waiting_for, wake_at and open question IDs, plus any pause.

Adapters (`com.synthetics.adapter.*`, Fieldpost, Fieldstone, Telegram) show the service state only.

## Threads

Messages are deduplicated by message id (a message is often captured by one side only, sometimes
by both). Sender and recipient addresses are resolved to company person IDs (`codex-manager` →
Codex's ID). A conversation is the source's `thread` when it has one. Otherwise it is the
unordered sender/recipient pair plus the set of `work-id`/`for-work` headers, so both directions
of an exchange about the same pair of works join one conversation. Otherwise it is the pair alone.
Outgoing captures carry no recipient; when the recipient's inbound copy was captured too, its
headers fill it in, otherwise the recipient shows as `?`.

## Relay hops (G3)

When an employee asks someone else on behalf of a colleague (`question_ask relays=[…]`), the details pages link both
sides. The carrier's open question reads "carries <asker's question> for <asker>"; the asker's open question reads
"<hop> via <onward question>" once the hop notice has arrived. Each open question is an anchor `#q-<id>`, so the
link jumps to the other home's block on `/team` (where every home is on one page) when that question is still open;
on a single `/e/<home>` page a link to another home's question does not resolve, and otherwise it is just the ID.
The Docket table uses the same links: its question cell links to that question's entry under open questions,
and its "now" cell shows the hop with the question it went via and the questions it carries.
