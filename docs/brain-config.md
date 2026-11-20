# Configure a reasoning runtime and model connection

Synthetics owns the continuing employee. A host-owned JSON file selects the agent
runtime and its model connection. Companies using a supported gateway protocol
change configuration, without changing the employee engine or migrating memory.

```sh
synthetics --home EMPLOYEE_HOME run --brain-config /company/synthetics/brain.json
synthetics --home EMPLOYEE_HOME serve --brain-config /company/synthetics/brain.json
synthetics --home EMPLOYEE_HOME doctor --brain-config /company/synthetics/brain.json --json
synthetics --home EMPLOYEE_HOME worker install --brain-config /company/synthetics/brain.json
```

The runtime supplies its reasoning and tool loop. The connection supplies the
protocol, endpoint, model and authentication references. Synthetics supplies its
recorded employee/role instructions, current context and run-bound MCP tools.
Connection configuration cannot replace those instructions or grant authority.

## Company gateway

```json
{
  "version": 1,
  "executor": {
    "adapter": "codex-cli",
    "executable": "/company/bin/codex"
  },
  "connection": {
    "protocol": "openai-responses",
    "base_url": "https://models.company.example/v1",
    "model": "engineering-default",
    "auth": {
      "type": "bearer-env",
      "variable": "COMPANY_MODEL_TOKEN"
    },
    "headers_from_env": {
      "X-Employee-Account": "COMPANY_ACCOUNT"
    }
  }
}
```

The host makes the referenced variables available to the worker through its own
credential mechanism. Configuration contains variable names, never their secret
values. Credentials are not added to process arguments, employee context or run
provenance. The selected variables are excluded from the runtime's shell-tool
environment; MCP and host-account execution are still not an external isolation
boundary. See [executor boundaries](executors.md).

`base_url` is the gateway's API base, without `/responses`. HTTPS is required
except for literal loopback/localhost HTTP used in local testing. URL credentials,
queries and fragments are rejected. This version supports bearer credentials
from an environment variable and additional headers from environment references.
Extra `Authorization`, `Cookie` and `Host` headers are rejected. Native OAuth,
credential-helper commands, custom CA settings and header-only authentication
need additional explicit connection support; unknown fields fail validation.

The `openai-responses` protocol is a wire contract, not a restriction on the
gateway's upstream model vendor. The server and selected model must implement the
Responses streaming and tool-call/result loop used by the runtime. A Chat
Completions or Anthropic Messages endpoint alone does not satisfy it. Other wire
protocols require an adapter implementation; they are not silently translated.
[Official gateway requirements](https://learn.chatgpt.com/docs/enterprise/gateway-compatibility).

Gateway sessions use a private temporary runtime home and ephemeral credential
storage. They do not load or copy the host's saved ChatGPT account, and do not fall
back to it. Missing credentials, routing errors and incomplete streams fail the
session, keeping unfinished work and already-recorded tool effects for recovery.

## Existing saved logins through the same interface

```json
{
  "version": 1,
  "executor": {"adapter": "codex-cli", "executable": "codex"},
  "connection": {"protocol": "chatgpt-login"}
}
```

This uses the runtime's saved ChatGPT login and its default model. An optional
`connection.model` selects a named model. A read-only login-status preflight
refuses an incompatible or missing saved login before starting the session.
Inherited API-key and endpoint overrides are excluded.

```json
{
  "version": 1,
  "executor": {"adapter": "claude-cli", "executable": "claude"},
  "connection": {"protocol": "claude-login", "model": "claude-opus-5-5"}
}
```

The Claude connection uses its CLI's saved login with inherited API/cloud routing
overrides excluded. Its model defaults to `claude-opus-5-5`. Login connections
reject gateway/auth/header fields. Install and sign in to the chosen CLI before
starting the worker; Synthetics does not perform login automatically.

## Validation and lifecycle

Version 1 accepts the two adapter names and three protocols shown above. Both
adapters support bounded `run`/`serve` sessions. Configured persistent meeting
sessions are currently refused before yielding ordinary work, including for the
configured Claude adapter. Codex does not enforce a USD budget or report USD cost;
doctor reports that difference. Existing explicit `--brain claude|codex` launches
remain compatible.

The loader rejects unknown/duplicate fields, unsupported adapter/protocol pairs,
invalid types and files over 64 KiB. A bare executable name is resolved on PATH;
a relative executable path containing `/` is resolved relative to the config
file. Absolute paths avoid host PATH differences. Configuration is read into an
immutable snapshot; credential values are resolved by the selected runtime at
session startup, without changing that snapshot.

Do not combine `--brain-config` with provider/model/executable selection flags or
their runtime allow-lists. Put those choices in the file. Operational options
such as timeouts and polling remain ordinary worker settings.

A configured worker pins the normalized config path, fingerprint and model.
Its private runtime state publishes this selection so other components can
identify it. Runtime requests may change permitted operational settings; they
cannot select another connection. Editing the selected file causes refusal
rather than reloading midway or falling back to another provider. The host must
deliberately restart/reinstall the worker with its selected config. Whitespace
and JSON key-order changes do not change the fingerprint. Credential rotation
through the same environment references does not change the config identity.

Runs record adapter, protocol, model and normalized-config fingerprint. They do
not record credential values, variable names, header mappings or the endpoint in
that provenance. Doctor inspects configuration and capabilities without calling
a model or resolving secrets. A successful doctor result does not certify that
the gateway/model implements the actual tool loop.

## Acceptance checks

The maintained local acceptance fixture exercises an actual installed Codex CLI
through the public config-path interface. It verifies configured route, bearer
authentication, model alias, a durable employee memory write, matching tool-call
output, final completion, unchanged employee identity, temporary-home cleanup and
preservation of a bystander's saved-login fixture. It performs no model inference
and uses no real provider key:

```sh
SYNTHETICS_TEST_CODEX=/absolute/path/to/codex \
  python3 -m unittest tests.test_brainconfig_cli_acceptance -v
```

The regular suite skips this opt-in installed-CLI check. Qualify a company's real
gateway and chosen models separately for streaming, tool behavior, credential
renewal, networking and policy. The config interface is shared; a successful
local fixture does not certify an untested deployment.
