# Synthetics: gaps and the OpenClaw comparison

Recorded 2026-11-15 from the implemented Synthetics engine, its current employee
workflows, and OpenClaw's official documentation reviewed the same day.

## What overlaps already

Persistent agent names, workspaces, memory, tools, multiple agents, and scheduled or
proactive execution are established agent-platform capabilities. They are not evidence
of a unique Synthetics product. OpenClaw documents separate agent state/authentication,
memory across sessions, channels, and scheduled work. Its enterprise project also
addresses persistent agents and organizational controls.

Sources:

- [OpenClaw multi-agent routing](https://docs.openclaw.ai/concepts/multi-agent)
- [Memory overview](https://docs.openclaw.ai/concepts/memory)
- [Main-session model](https://docs.openclaw.ai/concepts/main-session)
- [Scheduled/proactive execution](https://docs.openclaw.ai/gateway/heartbeat)
- [Enterprise platform and its current pilot status](https://openclaw.ai/enterprise)

OpenClaw's personal-agent starting point does not prevent configuring it for a worker
role. Names, persona instructions, separate accounts, and autonomy are not exclusive
to Synthetics. Absence of an OpenClaw feature has not been established merely because
we have chosen to implement a similar mechanism ourselves.

Synthetics' intended product contract is a continuing organizational colleague:
its own identity and attributable actions, direct working relationships with several
colleagues, onboarding that accumulates, and responsibility for work across time.
Much of that contract still needs to be implemented and demonstrated.

## Gap register

These entries describe open product/engineering questions. They are not completed
tasks or claims that a particular implementation is already the answer.

| ID | Gap and current evidence | What would resolve it |
| --- | --- | --- |
| SG-01 | **Runtime reuse has not been evaluated.** We are implementing agent plumbing while OpenClaw already documents channels, sessions, memory, automation, and deployment facilities. | Compare actual extension points against our employee/work contracts. Record which components can be reused, what cannot, and why. Use the onboarding/maintenance scenario as the acceptance case; do not justify custom infrastructure with branding. |
| SG-02 | **Organizational identity remains mostly a local profile.** Employee IDs and names persist, but Claude and local tools use the host account. No corporate account/access lifecycle is provisioned. See `docs/platform-assumptions.md` and `synthetics/brain.py`. | An employee acts through its own configured organizational principals/accounts, with attributable activity and a defined provisioning, role-change, and offboarding process. Assess existing platforms before implementing this ourselves. |
| SG-03 | **Communication is local and not authenticated as a corporate sender.** M02 Fieldpost now delivers ordinary messages between explicitly provisioned local peers with identity checks and durable inbox receipts. Incoming manual sender labels are still caller-supplied; correlated questions, external delivery and corporate authentication remain open. | Verified channel identities, reliable incoming/reply correlation, and explicit delivery/failure states. Several colleagues can work directly with the employee while preserving authorship and context. |
| SG-04 | **Declared access is not enforced execution isolation.** Filesystem grants express intended scope; unrestricted host tools can reach beyond it. Revoking a grant does not revoke a running host process. | Choose an execution/account boundary appropriate for a digital worker, with tested isolation, resource ownership, and revocation behavior. A human desktop OS is an option rather than a product requirement. |
| SG-05 | **Memory mechanics are ahead of evidence for long-term learning.** We retain sources, corrections, entities, and self-memory; small fixtures and live sessions are not proof of reliable professional memory over weeks. | Observe onboarding, changing roles/relationships, corrections, and later assignments. Measure repeated-question avoidance, appropriate investigation and questions, retained decisions, and useful cross-project knowledge without flattening all colleagues into one owner. |
| SG-06 | **Work completion still depends heavily on model judgment.** Work/question states are durable and race-guarded, but final model claims and generic shell effects are not independent proof of the intended result. | Assignment-specific completion evidence, review outcomes, and reconciliation for uncertain side effects. Preserve the distinction between attempted, observed, and verified outcomes. |
| SG-07 | **Worker availability and resources need operational ownership.** Workers are local processes; session budgets exist, but managed lifecycle, aggregate policy, and employee resource allocation remain incomplete. Fieldstone's cost views are visibility rather than enforcement. | Explicit hosting/restart behavior, attributable resource/usage records, practical budgets and limits, and a way for the employee to recognize and communicate resource problems. |
| SG-08 | **Repository perception is only partly implemented.** M01 now provides an operated collector for one configured Markdown source, with actual publication-to-Vesper triage evidence. The engine itself does not inspect source changes; broader code/source freshness and reliable collector state-loss recovery remain open. | Trace a new source artifact through visibility/freshness, signal delivery, attention and work selection. A maintainer should discover relevant changes through an intentional mechanism, with evidence of what it saw. Instructions to be proactive do not create that mechanism. |
| SG-09 | **The organizational-colleague proposition is not yet demonstrated.** We have functioning local employees and short maintenance episodes. We have not shown sustained collaboration under their own organizational identities. | A real role over time, several colleagues working with the employee, accumulated KT, reliable follow-through, and useful outcomes with decreasing context reconstruction by humans. Compare this behavior fairly with a configured existing platform. |

## How to use this review

Investigate before assuming that another subsystem must be built. Close a gap with
evidence: implementation/source inspection, an appropriate maintained change, or an
observed workflow. Describe what an existing platform supplies as well as any concrete
mismatch with the product contract. Keep future aspirations distinct from delivered
behavior.

Repository references are relative to the current Synthetics source. The employee's
working snapshot may not contain a newly published document until an explicit source
discovery or synchronization mechanism makes it available. That is itself a gap to
observe, rather than silently compensate for when assessing autonomous discovery.

## Discovery observation: 2026-11-15

The review was published in the main repository and linked from README. We then
observed Vesper without sending a message, assigning work, copying the document,
synchronizing its source, changing its timer, restarting its worker, or manually
waking it.

- Observation: 21:26:57 to 21:29:09 UTC, approximately 132 seconds.
- Its worker remained alive (PID 13599).
- Run count stayed at 4, received-event count at 4, pending input at 0, and ready
  work at 0. No unassisted document-linked activity appeared.
- The document was absent from its source snapshot. Its only open assignment was
  the scheduled discovery review on November 22 at 20:00 UTC.

**Self-pickup was not observed.** Combined with the reviewed admission code and
one-time snapshot mechanism, this identifies a missing perception/activation path.
It does not show that Vesper received the review and then failed to understand or
prioritize it. It also does not establish what it might do at a later scheduled review.

As explicitly requested, we then created diagnostic work
`work-cd906b720ee8452b972209008ce14692`, provided a read grant for the current main
repository's docs, and delivered assignment event
`event-69a404d3025144f6862f9262294b1238`. It asks Vesper to trace publication through
source visibility, signal delivery, execution admission, attention, and work creation;
identify what is missing and why; triage the review; and report through its outbox.
Any response to this assignment is **assigned work**, not autonomous discovery.

The raw before/after observation and assignment IDs are retained locally in
`.synthetics/observations/openclaw-gap-discovery.json`. Diagnosis, implementation,
and a subsequent fresh-artifact acceptance check remain separate outcomes.

## Assigned diagnosis and first remedy

Vesper completed the assigned diagnosis and queued a report to David. It identified
two missing links: the provisioned source snapshot does not refresh, and filesystem
changes do not enter the engine's event/ready-work admission paths. Its next scheduled
review was November 22; even that would require explicitly inspecting current docs.
The report is outbox message `message-0a371cea11fe409290a92e946e13a755`.

It then independently created follow-up work
`work-fb962589cc6d4badb1d1ed09118cec11` and implemented Fieldstone `sourcefeed` in
v0.2.1. The command compares Markdown content with a manifest and submits source-update
events through the existing inbox API. The full Fieldstone fixture suite was checked
independently: 70 tests pass.

This is a **partial remedy**, not a passed autonomous-discovery test. The command
requires manual invocation or a separately configured scheduler, and no automatic
collector was connected during this result check. Its tests establish aspects of the
command/event bridge; they do not establish publication-to-attention behavior without
operator intervention. The original passive observation remains a failed pickup.
The OpenClaw reuse evaluation and other organizational-product gaps remain open.

## Managed increments after the original observation

David subsequently asked Codex to manage Vesper through outcome assignments and
independent reviews. The original failed passive observation above remains failed;
these are later, separately observed outcomes.

- **M01 accepted:** Fieldstone 0.2.4's running collector delivered a freshly
  published product brief to actual Vesper in approximately 0.81 seconds. Vesper
  identified the SG-03 issue, retained its source, created candidate work and sent
  justified triage. Collector and employee were idle before publication; no
  per-publication scanner, feed, task, wake or restart was used. 87 Fieldstone tests
  pass, including the actual collector process and restart path. This establishes
  one configured Markdown source, not general repository perception or reliable
  recovery from lost/corrupt collector state.
- **M02 accepted:** Vesper built **Fieldpost** for ordinary messages between
  provisioned local peers. An actual Codex-outbox message reached Vesper (~0.96 s),
  which authored its own reply; that reply reached manager inbox (~0.31 s after
  queueing). Both have durable delivery receipts. 45 tests pass after route and
  identity corrections. Receipt means inbox acceptance, not recipient processing.
  Question/reply correlation remains unsupported in this phase.

SG-08 and SG-03 are therefore partially addressed by observed maintained increments.
Corporate principals/authentication, external channels, correlated questions,
manager hosting, OS supervision, aggregate resources, state-loss recovery and the
longitudinal employee proposition remain open. Manager availability currently
depends on the active Codex chat; ordinary inbox delivery does not invoke that chat.
See `docs/manager-loop.md` and the local evidence under `.synthetics/management/`.
