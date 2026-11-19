# Validation record

Sections from "First increment" down are a dated record of 2026-11-15. Their test counts
(55, then 64) were true then and are kept as history; they are not the current suite.

## Counting the suite now

Don't copy a count into this file; it drifts with every merge. Count it from the checkout:

```sh
env -u SYNTHETICS_SESSION_RUN -u SYNTHETICS_HOME python3 -m unittest discover 2>&1 | tail -3
ls tests/test_*.py | wc -l
```

Run it outside an employee session (or with those two variables cleared, as above), from the
checkout root, on both supported interpreters. For orientation only: main `858fb04`
(2026-11-19) ran 926 tests in 94 `test_*.py` modules on Python 3.9 and 3.14.

To check for test launchd labels after a run, use `python3 -m tests.test_service_recovery labels`, not a bare
`launchctl list | grep`. It prints each loaded `com.synthetics.test.*` label with its owner: `live pid N` (a
concurrent run, still working), `dead pid N` (an orphan the next run's sweep boots out) or `legacy` (no pid in
the label). The suite fails in `tests.test_service_recovery` teardown if it leaves one of its own labels loaded.

## Milestone index

Milestone IDs appear in `design.md`, `authority.md`, the docs below and test docstrings.
Review evidence for each milestone lives outside source control, under
`.synthetics/management/<ID>/` (the manager's) and the maintainer's `reviews/<ID>/`.
"Live check" means a real Claude session, or a real worker or adapter, exercised the change;
"offline" means only the unittest suite (temporary homes, fake model executables).

| ID | Name | Primary doc | Test modules | Live check |
|---|---|---|---|---|
| M01 | Project documentation source in the working day (source-watcher) | `manager-loop.md` | — | Live: accepted after a real live check (`manager-loop.md`) |
| M02 | Fieldpost: local colleague-message delivery | `manager-loop.md`, `openclaw-gap-review.md` (SG-03) | (Fieldpost's own suite, outside this repo) | Live: in daily use between employee homes |
| M03 | Human conversation channel (Telegram) | `human-channel.md` | `test_telegram*.py` | Live: in daily use by David |
| M04 | Telegram responsiveness hotfix | `human-channel.md` | `test_telegram.py` | Offline |
| M14 | Work completion needs checkable evidence (SG-06) | `design.md` | `test_evidence.py`, `test_evidence_refs.py` | Live: closed live through MCP on Vesper's real home (a bare done refused, done with evidence accepted) |
| M15 | Usage, daily soft limit, crash-loop guard, stop exit codes (SG-07) | `design.md` | `test_usage.py`, `test_telegram_cli.py` | Live: accepted with the M19 cutover; the daily soft limit is enforced on every running worker |
| M16 | Absence claims cite a recorded search | `design.md` | `test_evidence.py` | Live: the evaluation ran over Rowan's real onboarding runs (frozen exports), not over unittest |
| M18 | MG-03 failed tools in recent runs, MG-07 absence guidance | `design.md` | `test_mg03_mg07.py` | Live: audit rows, stuck-message delivery and live `question_resolve` (Vesper), plus the manager's own `question_resolve` |
| M19 | Runtime requests, idle restart, probation and revert | `authority.md` (relation) | `test_runtime_lifecycle.py`, `test_service_recovery.py` | Live: worker restarts under probation are routine |
| M21 | Project memory: shared log, capture, promotion, generated view | `design.md` | `test_project_*.py`, `test_projectlog_fold.py` | Live: phase 3 §8 scratch employees on a live-log copy, 12/12 |
| M22 | Authenticated inputs and recorded deliveries as evidence | `human-channel.md` | `test_auth_channels.py`, `test_evidence.py` | Live: keys deployed; authenticated arrivals on fieldpost and codex-desktop |
| M23 | Messages may name the recipient's work (MG-02) | `human-channel.md` | `test_work_refs.py` | Live: an authenticated for-work message woke the manager's waiting work |
| M24 | Memory recall by ID / prefix | `design.md` | `test_memory_recall.py` | Live: Vesper's live check plus the manager's snapshot `memory_index` |
| M25 | Transport-neutral channel contract, Postern, generic worker install | `channel-contract.md`, `postern.md` | `test_channel_contract.py`, `test_delivery_state.py`, `test_postern.py`, `test_humans_*.py`, `test_worker_install.py`, `test_doctor.py` | Live: P3 legs a-d (30 PASS) and f passed; only leg e, a natural Telegram round trip, is pending |
| M26 | Roster: self, company profiles, private views | `roster.md` | `test_roster_*.py` | Live: accepted |
| M27 | Board, stuck-outbox report, question close | `design.md`, `channel-contract.md` §5 | `test_board_*.py`, `test_outbox_stuck.py`, `test_question_close.py` | Live: Board acceptance on the real project log |
| M28 | Delegated authority ("Warrant") | `authority.md` | `test_authority.py`, `test_worker_install.py` | Live: delegated limit/worker operations by the manager |
| Docket | Waiting-on-David queue and relay hops | `channel-contract.md` §4.1 | `test_docket.py`, `test_docket_links.py`, `test_hops.py` | Live: deployed, Fieldpost carries hop notices |
| VIS | Lookout read-only team page | `lookout.md` | `test_lookout.py`, `test_vis_g*.py` | Live: running as `com.synthetics.lookout` |
| RW-1 | Glance (Noteling screen read) | `glance.md` | `test_glance.py` | Not yet: live P3 waits on a sitting with Noteling 0.10.5 |
| Click P1 | One approved, named click (engine slice) | `click.md` | `test_click.py` | Not yet: needs Noteling `--click` (P2) and a live sitting (P3) |

SG-01 to SG-09 are the standing gaps in `openclaw-gap-review.md`, not milestones; MG-xx are
manager-loop gaps. GitHub issue fixes carry their own modules (`test_issue<N>_*.py`).
Statuses change; the manager's tracker is authoritative for "accepted".

# First increment: validation on 2026-11-15

## Offline checks

Run `python3 -m unittest discover -v`. The suite uses temporary homes and fake model
executables; it does not consume model usage or read an employee's actual memory.
The final run for this increment passed 55 tests (count at the time).

It exercises persistent identity and sourced memory, supersession, self-memory under
heavy project activity, acknowledged source retrieval, event timestamps and duplicate
delivery, questions and correlated answers, terminal work handling, scheduled wake-ups,
captured-input acknowledgment, failure/restart recovery, stale process fencing,
autonomous continuation, zero idle model calls, repeated-failure shutdown, kernel
locking, stdio MCP, CLI input, child process timeout, and SIGTERM cleanup.

These checks establish runtime mechanics. They do not establish that the model always
asks a useful question, judges priority well, or records a correct interpretation.

## Real Claude exercise

The opt-in `scripts/live_acceptance.py` creates a new, explicitly fictional test
employee. It assigns only a small workspace note and real durable communication tools.
No real Noteling or Stand source is assigned for modification.

Two exercises completed in three fresh Claude sessions each. The employee read its
notes, retained a company-specific term and a colleague's role, asked about an
undocumented publication decision, waited, resumed after a correlated answer, and
completed its handoff artifact. A later assignment in another project used the retained
learning with the same employee ID. Six offline housekeeping runs moved the original
answer beyond recent session context.

The second exercise also required a successful `event_get` MCP receipt for that
original answer, rather than accepting the model's claim that it revisited evidence.
The assertion passed. Its model-reported cost was $0.35567275 across three sessions.
The earlier exercise reported $0.35768425; its source-citation assertion alone was
weaker and should not be treated as proof of a historical read.

Local reports, workspace artifacts, model outputs, and tool receipts are under
`.synthetics/live-check-1/` and `.synthetics/live-check-2/`, excluded from source control.
They are test experience only; the fictional facts are not copied into Rowan's memory.

## First actual employee

Rowan was provisioned separately as the Noteling maintainer under
`.synthetics/rowan/`. In its introduction session it inspected Noteling's documentation,
package structure, and history read-only, retained initial learning, and queued an
introduction with onboarding questions. The model reported $0.472697 for this session.
No working checkout was assigned and no source changes were made there.

That session exposed two model-behavior weaknesses despite the controlled exercise
passing: it embedded questions in an ordinary introduction message instead of creating
a correlated question, and used the welcome event as the source for facts it discovered
in files. It also called Swift targets "products". The employee prompt was tightened
and Rowan received explicit runtime feedback asking it to correct its records, create
the pending question, and retain the working lesson. This is actual onboarding
feedback, kept in its own history.

Rowan's next session corrected the four source attributions, corrected "products" to
"targets", registered its onboarding question, and retained a procedure memory about
source attribution. This session reported $0.5010765. Its subsequent human-readable
waiting label also exposed a runtime bug: free wording could overwrite the question
dependency. The runtime now keeps a separate question ID for resumption; changing the
label cannot sever that relationship.
An additional interleaving check caught the case where an answer arrives mid-session
and an older model decision tries to put the work back to sleep. A write guard now
rejects that decision until the pending answer is part of the session's captured
inputs. The regression also covers an input backlog larger than one batch.
This migration was also checked against a SQLite backup of Rowan's actual state:
answering the question resumed the backed-up work. Rowan's original state still
awaits a real answer. Its local worker is running in `serve` mode; it makes no model
calls while the inbox is empty and its work is waiting.

The store enforces persistence, nonempty provenance, and valid state transitions. It
does not establish that a cited source supports a claim or that the model chose the
right communication operation. Evaluation and working feedback remain necessary.

## Limits of the evidence

This is evidence of a small functioning identity and work engine. It is not evidence
of sustained autonomous product maintenance, authenticated organizational participation,
weeks of memory quality, or reliable recovery of arbitrary shell/network effects.
Those require subsequent work and observation in the actual employee role.

## Adding a maintainer for Synthetics

A subsequent change added durable operator-provisioned read/write scope and exposed
it through CLI, employee context, and Claude directory configuration. The expanded
suite passed 64 tests (count at the time), including canonicalization, persistence, operator-only grants,
revocation, adapter directory arguments, and CLI provisioning.

The new employee chose its own name, Vesper, in a tool-less bootstrap call. That
decision is retained before identity provisioning. Its home is separate from Rowan's;
its source copy excludes employee state, generated files, credentials, and original
Git metadata, with supplied file hashes recorded in SOURCE.json.

It was given the standing role of maintaining Synthetics and actively finding real
missing capabilities, a write grant for `~/projects/synthetics-internal`,
initial investigation/implementation assignments, and a scheduled next-day review.
It independently identified interface/conversation/overview/resource gaps and selected
Fieldstone as the name for its first internal tool. Its implementation work is recorded
in its own work engine; this record does not claim the tool is complete.
