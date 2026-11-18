# First increment: validation on 2026-11-15

## Offline checks

Run `python3 -m unittest discover -v`. The suite uses temporary homes and fake model
executables; it does not consume model usage or read an employee's actual memory.
The final run for this increment passed 55 tests.

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
suite passes 64 tests, including canonicalization, persistence, operator-only grants,
revocation, adapter directory arguments, and CLI provisioning.

The new employee chose its own name, Vesper, in a tool-less bootstrap call. That
decision is retained before identity provisioning. Its home is separate from Rowan's;
its source copy excludes employee state, generated files, credentials, and original
Git metadata, with supplied file hashes recorded in SOURCE.json.

It was given the standing role of maintaining Synthetics and actively finding real
missing capabilities, a write grant for `/Users/david/projects/synthetics-internal`,
initial investigation/implementation assignments, and a scheduled next-day review.
It independently identified interface/conversation/overview/resource gaps and selected
Fieldstone as the name for its first internal tool. Its implementation work is recorded
in its own work engine; this record does not claim the tool is complete.
