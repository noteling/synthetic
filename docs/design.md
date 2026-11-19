# A continuing employee: first implementation

## Product premise

Synthetics creates a digital employee that enters an organization under its own
identity. Its statements and actions are its own contributions, rather than an
attempt to impersonate an existing employee. The larger goal includes its own
organizational account, access groups, communication accounts, working environment,
and ability to do the work of an online remote colleague.

The first role is a Noteling maintainer. The employee starts as a new hire: it needs
knowledge transfer, should investigate what it can discover, and should ask focused
questions for intentions, history, or decisions it cannot establish independently.
The expectation is that onboarding accumulates and changes later behavior.

The employee acts on its own from then on: it follows work, encounters signals,
chooses useful next steps, asks questions, and resumes. It must remember itself,
colleagues, company language, shared experiences, relationships, and obligations
beyond the boundaries of one project.

## Implemented structure

```text
manual feed / future adapters          clock / unfinished work
               |                              |
               +----------> durable inbox <---+
                                  |
                             work engine
                                  |
                    fresh Claude work session
                     /                      \
            local work tools        Synthetics MCP tools
                     |                      |
                 artifacts         memory / work / questions
                                            |
                                         outbox
```

The durable store uses SQLite transactions, WAL, and FULL synchronous mode. Identity,
entities, memories, work, questions, messages, reminders, runs, events, operation keys,
and audit records live in one employee home. Workspace artifacts remain ordinary
files. There is one active worker per home, enforced by a kernel file lock.

The engine reconciles timers, chooses whether there is work, captures an input batch,
and starts a bounded reasoning session. Claude handles the local reasoning/tool loop.
Synthetics exposes employee-specific operations over stdio MCP. No separate planner,
supervisor swarm, event bus, or speculative knowledge service is introduced.

One identity can ultimately support concurrent investigations and real-time
interaction. This increment serializes work sessions and lets the model choose among
open assignments. Higher priority and older updates sort first. There is no hard
fairness guarantee yet.

## Durable boundaries

- The employee ID survives model replacement, compaction, or restart. Name, role, and
  purpose are provisioned. Its developing self-understanding lives in sourced memory.
  Updating provisioned identity/role through an organizational workflow remains future work.
- Entity IDs provide stable references to self, people, projects, organizations, and
  concepts. Memories describe their properties and relationships, with evidence and
  explicit fact/belief/preference/procedure distinctions. A claim that something is
  absent cites the recorded search (query and scope) or is kept as a belief; the M16
  evaluation found an unsupported absence claim stored as fact and forwarded onward.
- Current memory and original experience remain separate. Superseded entries stay
  available; processed incoming events remain searchable. Snapshot limits do not
  delete old memory. Self memory is included separately so new project activity does
  not crowd it out entirely.
- A work snapshot shows the newest 60 active memories in full. Older active memories
  appear in `memory_index` ({id, subject, predicate, kind, date, gist}), newest first,
  capped at 300 entries / 24 KB with `memory_index_truncated`. `memory_search` returns
  memories containing every query word first, then fills to its limit with any-word
  matches ranked by rarity (`match: "all"` restores the strict search). A query equal
  to a memory ID, or a 15+ character prefix, returns that memory first (M24).
- Work persists independently of model output and conversation history. A model turn
  ending only acknowledges that session's captured inputs. It never implicitly marks
  an assignment done.
- Asking a work-linked question atomically queues the message, records the question,
  and makes that work wait. A correlated answer atomically records the evidence and
  wakes the matching work. Terminal work is not revived by a late answer.
  A separate `waiting_question_id` preserves the dependency when the employee edits
  its human-readable waiting label. A session cannot put work back to sleep over an
  answer that arrived outside its captured input batch; the next session handles it.
- A work may wait on several open questions (`waiting_question_ids`; `waiting_question_id`
  is the newest). Re-asking the same canonical recipient (`to_id` when routed, else `to`)
  replaces that link; the older question stays open. Any linked answer wakes the work and
  records `woken_by_answer {question, event, at, remaining_open, prior_waiting_for,
  prior_wake_at}`; a work reference never wakes work that still has linked questions.
  `question_close` lets the asker close its own open question: with a received message
  from someone other than the recipient it becomes `answered_by_proxy` (auth kept as
  received, marked secondhand, never verified evidence, never the recipient's own words)
  and wakes linked work; without one it becomes `withdrawn`, never wakes work, and reports
  `work_unlinked` for waiting work left with no linked question. A later correlated reply
  to a closed question is delivered as an ordinary message. `question_resolve` with the
  already recorded answer returns `already: true`.
- Docket P1a: one open question may block several works of the asking home (a shared
  blocker, e.g. one checkout decision blocking R03, R04 and onboarding). `work_put` links
  an open question asked for another work (it keeps its origin `work_id`; answered
  questions are still refused), and `question_ask(work_ids=[...])` links several works at
  ask time (`work_id` defaults to the first; CLI `send --question --also-work W`). One
  answer wakes every linked work. `work_get` shows `shared_with` {question: [other
  waiting works]} when a linked question is shared; a withdraw reports `unlinked_from` (every work it was removed from)
  besides `work_unlinked` (those left with no linked question).
- `synthetics docket [--person P] [--format text|json]` and the read-only MCP tool
  `docket(person?)` list this home's own open questions to one person (default
  `default_human`; matched by ID, roster name and aliases, and for the default human the
  humans.json senders), oldest first. Each item has the first line of the question (60
  chars), asked time and age, the delivery ack time and age when one exists, otherwise
  `ack: "not acked (route ...)"` with the age taken from the ask (never dropped), outbox
  `stuck`, and the waiting works it blocks; `text` is a paste-ready block. Home-local: it
  never reads another home or private views. JSON fields (v1) are stable: add, never rename.
- Docket P1b (relays and hops): `question_ask(relays=[...])` and `question_relays(question_id, add)` link
  questions this home received (the `question-id:` header of an authenticated message, or of a Fieldpost
  event with auth null, stored as `link_auth` null) to the question that carries them onward. In the same
  transaction the store queues one structured `kind: hop` outbox row per carried question to its asker:
  `with-person` on ask/link, `answered-pending-relay` when the onward question is answered (or answered by
  proxy), `with-manager` when withdrawn. Fieldpost delivers a hop only for a question it correlated from
  that asker to this sender, into the adapter-only `Store.question_hop` (sender must be the question's
  recipient and the question open, else `hop_rejected` and no change; idempotent per via+hop; older
  notices ignored). A hop never wakes, answers or changes status. The asker's questions show `hop`,
  `hop_person`, `hop_since`, `hop_via`, `hop_history`; the docket shows `now: with-david since …`, what each
  question `carries`, and `relay_owed` (answered onward questions whose carried questions have had no reply
  from me since), which is also in the snapshot; doctor warns `relay_owed` after 4 h. Published works add
  `blocked_by` lines `"<question ID> <hop>"` to their backlog row (ID and hop only; queued / with-<to> when
  no notice has arrived); the project log refuses any other shape.
- Project memory (M21) is a per-project append-only log under `.synthetics/projects/<project>/`
  (`memory.sqlite3`, exported to `log.jsonl` after each write). Current intents, lessons and their history are a
  deterministic fold of it. `view.md` next to it is a generated read-only Markdown view (standing direction,
  lessons by status, history, open Board backlog), rewritten after each write; a failed rewrite never fails the
  write. `synthetics project view P [--as-of N] [--out F | --write]` renders it; a session needs read access.
  Corrections go through the log, never into the view.
- Message project tags (M21; VIS G3 delta): `message_send`/`question_ask(project=P)` tags a message for P when
  the sender holds a write grant on P (otherwise refused). With `project` omitted and `work_id` the sender's own
  work published to P, the message is tagged P with `project_by: "work"`; the recipient's `project_ref` shows
  `by: "work"` and checks the sender's write grant as for `by: "sender"`. An explicit `project` wins; `project=""`
  opts out; `recipient_work_id` is never used. The default never refuses a send: unpublished work tags nothing,
  and when P does not exist or the sender cannot write P the message goes untagged and the tool result's
  `project_default` (not stored) says `applied: false` with `reason` `unknown-project` or `no-write-grant`.
- Lookout (VIS P1, `docs/lookout.md`): `synthetics web` serves a read-only page on 127.0.0.1 from shared records
  only: project and company `log.jsonl`, launchd, `runtime.state.json`, the pid in `worker.lock`, and one engine-written
  export per home, `<home>/shared/status.json` (session state, work titles and waiting_for, Docket, run id/status/cost;
  never descriptions, notes, memories, views, summaries or bodies). It never opens a SQLite database.
- Completion evidence refs are an own-home ID, an absolute file path, `receipt:` or
  `https://`. As a last fallback, a ref with none of those prefixes that names an existing
  file relative to the home's workspace (never the process cwd; `../` and symlinks resolved
  first) is stored as the absolute resolved path and echoed in `resolved`; the `runs/`
  transcript refusal applies to the resolved path. A relative file named like an ID
  (`work-notes.md`) takes the ID branch, so cite it by absolute path. When a ref is capped
  below the requested level, the error says what to cite instead.
- Administrative CLI actions are authorized by recorded delegations, not by a blanket
  session refusal (M28, `docs/authority.md`): the actor is the caller's own home plus its
  running run, a root delegation cites an authenticated human message and is written by
  the owner, children only narrow it, and every delegated write re-checks run and chain
  inside the target transaction and leaves a receipt. Credential items stay refused.
- The employee can work on another ready assignment while one is waiting. Follow-ups
  can have explicit reminders. There is no ambient model loop when there is no work.

## Interruption and recovery

A successful run acknowledges only input IDs captured when it began. New messages
arriving during a long session remain pending. Failed or interrupted runs retain
their inputs and already-recorded tool effects. On restart the worker marks unfinished
runs interrupted and shows that history to the next session for reconciliation.

Every model-originated MCP mutation is bound to its run. Its write transaction checks
that the run is still active. An orphan model process cannot mutate durable employee
state after recovery revokes that run. The host terminates the model's process group
on timeout, Ctrl-C, or SIGTERM, including MCP/tool descendants.

An abrupt SIGKILL can leave raw shell children running. State fencing cannot revoke
their filesystem/network access. A model reporting successful work is also not proof
of a verified outcome. These are explicit execution-platform gaps, not properties
SQLite can solve. See platform-assumptions.md.

Messages, questions, reminders, and optional memory/work creation keys support retry
reconciliation. A repeated key with identical content returns the original record;
conflicting reuse is rejected. This does not guarantee exactly-once arbitrary external
effects, nor prevent a model from inventing a different key for the same intention.

## Lessons applied from the reference projects

Noteling contributes clear execution ownership, cleanup before completion, immutable
capability selection, deterministic job findings, attention judgment, source-backed
matters, and inspectable experience logs. Its task history is session-only; its log
may drop a failed write to keep a job running. Employee commitments need transactional
persistence instead.

Stand contributes a distinction between facts to know and points to say; promises
owed; different context and latency needs for lookup, preparation, memory, and watch;
and the importance of tracking actual audience experience instead of generated intent.
Its "continue" failure showed why new language must be able to revise the component
that owns ongoing work. Its stale watch checks showed why late decisions need context
validation. Its CLI lifetime is a meeting, while this employee must persist across them.

We keep the useful boundaries and avoid copying either application's feature-specific
lifecycle into Synthetics. There is no link-time or runtime dependency on those checkouts.

## Useful increments after this one

The engine supports independent employee homes, each with its own identity, memory,
worker, and work. Rowan maintains Noteling; Vesper now maintains Synthetics itself and
its internal tools. Communication is still through the local channel and explicit
operator delivery. Merely introducing another employee does not share their memories.

Operator-provisioned filesystem grants describe assigned read/write roots and purposes.
They are durable, audited, and visible in employee context. Provisioning and revocation
are not model MCP operations. Claude receives active existing roots for directory
access, but enforcement still relies on host privileges and the employee's instructions.
The new maintainer's remit and working-copy arrangement are in maintainer.md.

The next evaluation is actual onboarding and a concrete maintenance assignment in an
assigned working copy, followed by another assignment that tests retained learning.
The product can then add authenticated message adapters, delivered/read states,
organizational provisioning, stronger execution isolation, richer retrieval, and
responsive coordination of simultaneous activity as the work requires them.

Meetings and desktop interaction will connect to the same employee state. Their timing
and concurrency need separate care; a normalized event is useful for durable meaning
but is not a replacement for a low-latency audio path. No POC is needed merely because
the overall ambition is large. Experiments should answer a concrete question that
changes an implementation decision.
