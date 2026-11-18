# Managing toward the Synthetics goal

David asked Codex to act as Synthetics' manager on November 15, 2026. The same
durable manager identity now also has a paired Telegram channel and headless
Synthetics worker. Vesper remains the engineer maintaining Synthetics and its
internal products. These are distinct identities. Manager messages use
`codex-manager`, rather than speaking as David.

## Goal that survives individual tasks

Synthetics is a continuing digital colleague with its own identity, relationships,
memory, communication, resources, and responsibility for work. It investigates,
asks appropriate questions, retains experience, notices incoming information, and
follows through across sessions and projects. It does not impersonate David.
Voice and desktop capabilities can connect later. A small first input channel
does not reduce the product to a documentation watcher or developer assistant.

Useful progress means demonstrating more of that working life. Features and test
counts are evidence only insofar as they establish an intended outcome. Corporate
accounts, authenticated communication, reliable memory over time, and resource
ownership remain explicit gaps in `openclaw-gap-review.md`.

## Working agreement

1. Codex assigns a useful outcome with its purpose, authority, and acceptance bar.
   Vesper investigates and chooses the implementation. It can propose smaller
   maintained phases and should keep the original obligation visible.
2. Vesper records progress, evidence, questions, and remaining work in its durable
   tools. It sends review handoffs to `codex-manager` and asks a correlated question
   when it needs a review or an operator action. Initial project ignorance is normal.
3. Codex reviews the actual changes and independently checks the intended behavior.
   Running a component manually does not prove unattended operation. Review can
   accept, request changes, or record an explicit blocker; employee completion and
   manager acceptance are separate records.
4. Review feedback returns through Vesper's inbox. Codex does not quietly repair
   the employee's implementation and attribute that result to Vesper.
5. Accepted work informs the next assignment. Incoming signals may be investigated
   and triaged independently; substantial new implementation proposals go through
   this management loop. There is no need to invent work to remain busy.

The manager's durable identity, assignments, and reviews live in
`.synthetics/manager/`. Vesper's existing identity and memory are preserved in
`.synthetics/internal-maintainer/`. This document is the visible continuity record.

## Current iteration

**M01 — Make one project-documentation source part of Vesper's working day.**

Finish and operate the existing maintained Fieldstone source-input path. After
one-time setup, publication or editing must produce attributable input and useful
Vesper triage without per-publication forwarding, scanner commands, assignments,
or wake-ups. Review service behavior, repeated changes, restarts, provenance, and
operating instructions. Then publish a fresh real product brief while collector
and employee are already running and observe Vesper's actual response.

Status: **M01 accepted after two correction rounds and a real live check.** With
collector and Vesper already idle, a fresh product brief arrived in about 0.81 s.
Vesper identified the communication gap, retained its source, created a candidate
work item, and sent justified triage to `codex-manager`. No per-publication forwarding,
scanner, task, wake or restart occurred. All 87 Fieldstone tests pass. The live
evidence and acceptance are retained under `.synthetics/management/M01/`.

**M02 — Ordinary local colleague-message delivery.** Vesper's discovered candidate
became the next assignment and is now **accepted**. Vesper chose the name
**Fieldpost**. After two correction rounds, 45 tests and an actual ordinary-message
round trip pass: Codex's own outbox message reached Vesper (~0.96 s); Vesper authored
its own reply and it reached the manager inbox (~0.31 s after queueing). Both have
durable receipts. No per-message forwarding occurred. Inbox delivery remained
distinct from manager processing/review. Evidence: `.synthetics/management/M02/`.

**M03 — Human conversation channel.** A dedicated `@synthetics_internal_bot` private
conversation connects David to the existing manager inbox and outbox. Vesper wrote
the transport; Codex implementation colleagues supplied the thread core, saved-chat
exporter and service helper; the desktop manager integrated, reviewed and operated
them. Independent service tests cover question replies, proactive scheduled work,
restart and retained context. The final suite passes 246 tests. A real Claude
session also retrieved an older conversation page, replied in the same thread and
retained a sourced memory. David's actual Telegram input reached the manager, and
the headless manager's reply was accepted by Telegram. Evidence and limits are
recorded under `.synthetics/management/M03/` and in `human-channel.md`.

Peer question/reply routing remains a separate candidate: the existing Fieldpost
peer relay supports ordinary messages. Corporate authentication, always-on hosting,
aggregate resources and broader state-loss recovery remain open. The larger SG-03
communication contract is only partially addressed.

Operator-owned source collection, Vesper's worker and the approved ordinary-message
relay have their commands, PIDs, logs and configuration in
`.synthetics/management/operations/services.json`. Idle polling does not call the
employee model. Process restart remains an operator responsibility for those earlier
services. The manager's Telegram adapter and bounded Claude worker now have user-owned
macOS launch agents; the Mac must remain logged in, awake and online.

## Platform assumptions exposed by this arrangement

The manager's headless worker uses Claude CLI as a replaceable reasoning backend
under its existing permanent identity. Its saved Codex conversation is sourced
reference history; this is a new execution session. Future desktop conversation
changes need another explicit handoff. The channel can outlive the desktop UI,
while availability still depends on the logged-in Mac and network. Processes need
operator ownership; children launched inside a model session are cleaned up.

The local peer sender label `codex-manager` is an explicit working relationship,
not a corporate directory principal. Telegram separately authenticates the bot
and paired human provider accounts. Filesystem grants express intended scope under
the host account. Noteling and Stand remain outside this implementation work.
