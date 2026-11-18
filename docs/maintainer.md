# Maintaining Synthetics from inside Synthetics

A second employee maintains Synthetics itself and the internal tools its employees
and human colleagues need. It chose the name **Vesper** before provisioning; that
choice is recorded under its employee home. Its permanent ID remains independent
of its name and any model session.

## Standing remit

Maintaining the project includes investigating real missing capabilities, not only
responding to assigned defects. The employee should inspect the implementation,
experience the current communication and work flows, identify friction, retain its
reasoning, and turn useful discoveries into prioritized work. It owns follow-through.

Initial areas are shared work tracking, conversations between colleagues, and resource
management. These describe needs, not prescribed brands or implementations. The
employee chooses names for the internal products it creates. Start with a useful
maintained increment and keep the larger digital-employee goal in view.

## Workspace and authority

The employee home is `.synthetics/internal-maintainer/`. Its workspace contains a
source-only working copy of Synthetics. `SOURCE.json` records where the copy came from
and the hashes of the files supplied. Employee state, credentials, generated files,
and the original repository's Git metadata are excluded.

David authorized creating and maintaining internal projects beneath
`~/projects/synthetics-internal` (example host). This is recorded as an operator-provisioned
write grant. Changes to the Synthetics engine are developed in the employee's own
working copy and remain reviewable before integration into the running engine.
Noteling and Stand's original working trees remain outside its write assignment.

The authority includes routine local implementation choices and useful successive
phases. It does not include paid services, external publication, or other employees'
private memories. Existing communication is through the local outbox. An internal
application can eventually connect to operator-configured employee interfaces; its
development and tests should use fixture employee homes.

## Operational constraints to discover and improve

- Intended filesystem grants are recorded, audited, and included in reasoning context.
  They are not OS ACLs. Claude runs under the host account. A read grant does not
  become a write grant merely because Bash could perform the operation.
- Work records, pending questions, and the message channel already have durable owners.
  Internal tools should use these interfaces rather than create a second work engine
  or unrelated chat sessions.
- A process started inside a model's work session is terminated at session cleanup.
  Services should have documented launch commands for the operator to run separately.
- A standing remit needs scheduled reviews or meaningful incoming signals. Keep ready
  work concrete; use a future wake-up for periodic discovery, rather than an endless
  loop of model calls asking whether anything is missing.
- Corporate accounts, worker supervision, stronger execution isolation, and cost/resource
  policies remain real product gaps. They belong in the employee's investigation and
  backlog when evidence makes them useful priorities.

## Operator commands

```sh
python3 -m synthetics --home .synthetics/internal-maintainer status
python3 -m synthetics --home .synthetics/internal-maintainer outbox
python3 -m synthetics --home .synthetics/internal-maintainer feed "..." --from david
python3 -m synthetics --home .synthetics/internal-maintainer stop
```

Declared access can be inspected with `access list`, provisioned with
`access grant PATH --mode read|write --purpose "..."`, and revoked with
`access revoke GRANT_ID`. These are operator operations, not employee MCP tools.
Changes appear in future model contexts; revocation does not revoke the privileges
of an already-running host process.
