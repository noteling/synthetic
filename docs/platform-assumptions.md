# Where existing software assumes a human

The first implementation is deliberately small. The target remains a digital
employee with its own identity, access, memory, communication, and work. Record a
platform mismatch here rather than quietly redefining the employee to fit it.

| Existing assumption | Current implementation | Consequence and direction |
| --- | --- | --- |
| An OS process acts as a logged-in human account. | Executors and Synthetics MCP servers run under the host account. Codex adds a workspace-write shell sandbox, while the Claude adapter relies on host privileges and tool settings. A private employee home is not an OS identity. | Eventually provision employee credentials and externally enforced resource boundaries. A separate account, VM, or container may be useful; a desktop OS is not a settled requirement. |
| An agent CLI serves an interactive developer with a personal login. | Bounded work can explicitly select Claude or Codex. The Codex adapter requires the host's saved ChatGPT login and excludes API-key/endpoint overrides, unrelated user configuration, app integrations and MCP servers. | Model access and provider quota are not the synthetic's own organizational identity. Keep the executor replaceable; no silent provider fallback or transfer of unavailable live context. See [executors](executors.md). |
| Every provider exposes the same budget and tool controls. | Claude reports monetary usage and accepts a session-dollar cap; Codex reports tokens without an equivalent dollar cap or native tool allow-list. Doctor and the employee snapshot expose those differences. | Unknown cost must not become zero, and a setting named budget must not imply enforcement. Use actual provider controls and external isolation where needed. |
| Ending a chat ends the interaction. | Synthetics persists work and questions, then wakes the next session. | Session history is an optimization, never the employee's identity or sole memory. |
| A human reads a notification and remembers to follow up. | The inbox, work records, and reminders survive restart. Optional macOS launch agents run a paired channel and bounded worker beyond the desktop UI. | Logged-in, awake hardware and network access remain requirements. Always-on hosting, aggregate model budgets and organizational provisioning remain open. |
| A displayed message has reached its intended person. | An outbox entry is queued intent. Configured adapters keep provider acceptance, failure and uncertainty distinct. | Acceptance is not human reading or employee processing. Uncertain external effects require reconciliation before retry. |
| A caller-supplied name establishes who spoke. | Manual feeds accept labels. The Telegram channel binds a private numeric user/chat through a one-time pairing link. | The paired provider principal is stronger than a label, but is not corporate directory membership or an organizational account. |
| An interactive chat is an employee's permanent working environment. | A saved Codex conversation can be imported as sourced, read-only history into the existing employee. New human messages continue in its durable thread. | The desktop live executor has no reachable shared endpoint here. A headless session is a separate executor; hidden reasoning and unavailable history cannot be transferred. |
| Access to another person's workstation is appropriate access for the assistant. | The model is instructed to use assigned sources and its workspace. No original Noteling or Stand checkout is assigned for writing. | A synthetic needs its own checkout, repository identity, and work accounts. Instructions and cwd do not enforce account boundaries against arbitrary shell access. |
| Killing a UI or parent process stops all work. | Normal cancellation kills the process group. Transactional run fences revoke late MCP writes after recovery. | SIGKILL may orphan shell children with filesystem/network effects. Stronger supervision and isolated execution are required before claiming complete revocation. |
| A response timeout means nothing happened. | Inputs remain pending; existing effects and uncertain outcomes are shown for reconciliation. | Arbitrary shell and remote actions need operation-specific reconciliation. Do not automatically repeat an uncertain send or write. |
| Project folders are the boundary of useful memory. | Employee memory spans self, people, organizations, concepts, projects, and their relationships. | Retrieval needs to follow relevance across project boundaries while honoring access and confidentiality. The current single-employee local store has no shared corporate ACL model. |

Things this increment does not yet provide: directory-service membership, employee
mail/chat/Git accounts, always-on managed hosting, organization-wide authentication,
an OS sandbox, a full semantic memory system, immediate
mid-session event injection, or continuous multi-activity coordination. Each is a
capability to add when it advances actual employee work. None is a reason to shrink
the product into a personal assistant.
