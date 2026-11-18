# Synthetics

Synthetics creates continuing digital employees with their own identity, relationships,
memory, communication, and work. A model session is one period of their work. It is not
the employee's lifetime. Read README.md and docs/design.md before changing behavior.

## Working rules

- Develop the maintained product in useful increments. A POC requires a specific
  unanswered question and a bounded experiment that informs an implementation decision.
- Noteling and Stand are reference projects. Do not modify either sibling checkout.
- Initial ignorance is expected. Investigate what can be established, ask people for
  what cannot, retain their answers, and resume work when information arrives.
- Memory belongs to the employee, across projects. Distinguish evidence, beliefs,
  preferences, procedures, and current commitments; preserve provenance and corrections.
- Durable state belongs to Synthetics. Model/CLI context is replaceable working context.
- A completed model turn is not a completed assignment. Preserve unfinished work,
  waiting questions, scheduled follow-ups, and uncertain action outcomes.
- Keep sender, source, thread, and reply information on incoming events. Manual sender
  labels are not authentication. Outbox entries are not proof of external delivery.
- Do not invent work merely to keep busy. Silence and waiting are useful states.
- Record human-centric platform assumptions in docs/platform-assumptions.md. A small
  first increment does not reduce the ambition of the product.

## Development

Python 3.9+ and its standard library. Run `python3 -m unittest discover -v`.
Use temporary employee homes for tests. Never run tests against someone's live memory.
Claude CLI is an execution adapter, not the durable store. See `python3 -m synthetics --help`.
Example host configuration (the development Mac, not a requirement): worker jobs use
the native `~/.local/bin/claude` (verified 2.1.326). There the Homebrew 2.1.311
executable was blocked before startup on November 17; preserve an explicit native
executable and PATH in deployments.

## Management loop

David has asked Codex to act as Synthetics' manager. Read `docs/manager-loop.md`
and the current manager/employee work records before assigning another phase.
Assign outcomes toward the continuing digital-employee goal, let Vesper implement,
and independently review the delivered behavior. Preserve acceptance decisions,
review feedback, and unfinished obligations. The manager's existing identity now
also runs through a headless worker and paired Telegram channel. Its saved desktop
conversation was imported as reference history; future desktop messages are not
automatically synchronized. See docs/human-channel.md and coordinate with the
running worker before starting another executor for that employee home.

## Human communication

David's primary input is the Synthetics desktop chat: orders, decisions, and answers
to team questions. The active desktop operator forwards these to the existing
manager inbox, preserving sender, original message ID, time, and conversation
context. Telegram carries team questions and status updates through the manager,
with worker attribution. Correlate answers given here to the exact tracked question
using `reply_to`; generic reply metadata alone does not resolve a question.
This is an operator-mediated handoff during desktop turns, not an automatic
background desktop bridge. Historical discussions remain reference material;
do not replay them as new assignments.
