# Reasoning executors

The employee's identity, memory, inbox/outbox, questions, assignments and authority belong to
Synthetics. An executor runs one bounded reasoning session against that state. Changing the
executor does not provision a new employee or complete unfinished work.

Claude CLI remains the default. Codex CLI is an explicitly selected alternative for ordinary
`run` and `serve` sessions. There is no automatic fallback between providers, and installing this
version does not switch existing workers.

## Select an executor

Install the selected CLI and sign in before starting an employee. For Codex, use its ChatGPT
sign-in. Synthetics pins that authentication method for this adapter and removes inherited API-key
and endpoint overrides; it does not copy credentials or run a login flow. A missing login fails the
session with its input retained for reconciliation. ChatGPT plan limits still apply.

```sh
synthetics --home EMPLOYEE_HOME run --brain codex
synthetics --home EMPLOYEE_HOME serve --brain codex --codex /absolute/path/to/codex
```

`--model` is optional for Codex: omitting it uses the CLI's default, not a Claude model name.
Claude keeps its existing default model. To return to Claude, explicitly choose `--brain claude`.
Do not run a second worker against the same employee home; the existing worker lock remains in force.

For a macOS background worker, the owner can select the executor at installation:

```sh
synthetics --home EMPLOYEE_HOME worker install --brain codex --codex /absolute/path/to/codex
```

Workers using `--runtime-config` retain their executor choice in the runtime settings. Older
runtime files without a `brain` field continue to select Claude. Changing the provider or the
Codex executable is an owner-controlled operation, not a model's implicit fallback. Stop or
restart through the supported worker lifecycle rather than editing an active process.

## Capabilities and limits

`synthetics --home EMPLOYEE_HOME doctor --json` reports the selected executor and its capabilities
without launching a model or checking provider usage. An invalid active executable is an error;
an inactive Claude path does not prevent a Codex-only installation.

| Capability | Claude CLI | Codex CLI |
| --- | --- | --- |
| Bounded work with the employee's Synthetics MCP tools | Yes | Yes |
| Role defaults, host rules and sourced employee context | Yes | Yes |
| Configured USD session-budget cap | Yes | Unsupported |
| Reported monetary session cost | Yes, CLI usage telemetry | Unavailable; stays unknown |
| Token usage | Provider result/events | Provider result/events |
| Native tool allow-list matching the Claude flags | Yes | No equivalent |
| Shell execution boundary | Host account and tool settings | Workspace-write sandbox |
| Persistent live meeting reasoning session | Yes | Unsupported in this increment |

The `budget` setting is not a Codex spending cap. A positive USD daily limit cannot measure
Codex sessions either, because their monetary cost is unknown. Doctor reports these differences;
Synthetics does not convert token counts into invented dollar amounts. The session timeout,
failure handling, worker lock and durable operation keys continue to apply. Actual provider quota
errors remain failures, with unresolved work retained. These guarantees are independent of whether
the selected account uses subscription allowance or another billing arrangement; this Codex adapter
currently requires ChatGPT authentication.

Codex live meeting startup refuses before pausing the ordinary employee worker. It does not silently
start a Claude meeting session for a Codex-selected employee. Meeting support needs a separate
interactive adapter; changing the one-shot executor does not establish it.

## Context, tools and execution scope

The adapter supplies the same recorded role profile, current employee snapshot and Synthetics MCP
server that the employee would receive with Claude. The MCP server is scoped to the employee home
and run ID, so the existing transactional run fence and authority checks still govern tool writes.
Only that MCP server is configured by the adapter, and startup fails if it cannot initialize.

Codex is started with user configuration and execution rules excluded, ephemeral session history,
application integrations disabled, and project-instruction loading disabled. This avoids inheriting
another employee's or the host user's personal agent instructions. Managed host requirements may
still constrain execution. The adapter uses workspace-write shell sandboxing with networking enabled;
only assigned write roots become additional writable roots. Read grants remain contextual instructions
and do not create a separate OS identity or prevent all host-file reads.

MCP servers are host processes. The shell sandbox is not a sandbox around the Synthetics store,
arbitrary MCP behavior, the host account or the company's credentials. Use an externally isolated
account, container or machine when those boundaries are required. An instruction or role profile never
grants additional authority.

Output is consumed as JSONL events. The adapter records filtered activity and token usage; raw thinking
and tool results do not become display records. A successful process exit alone is not enough:
the session must have a successful terminal turn and a final agent message. A quota error, malformed
stream, timeout or missing completion is a failed session even if it emitted earlier text. As with
Claude, side effects may already exist, so retries must reconcile durable state first.

## Official CLI references

- [Non-interactive Codex execution](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp)
