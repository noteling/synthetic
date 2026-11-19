# Authority: delegated actions instead of a blanket "operator" gate (M28 "Warrant")

David asked that work he has already authorized is not held behind a new "needs operator permission" step
(event-a114a287, codex-desktop, 2026-11-18). Sessions now act on **recorded, sourced, scoped delegations**.
What stays refused names the authority that is really missing.

## Actor and target

- **Actor** (`authority.actor`): no `SYNTHETICS_SESSION_RUN` → the owner (host account / desktop).
  `SYNTHETICS_SESSION_RUN` + `SYNTHETICS_HOME` → that home's identity, and only while the run is `running`
  there. Any other combination is refused (`actor_unverified`, `run_inactive`); there is no fallback to owner.
  `SYNTHETICS_HOME` alone is the CLI's ordinary `--home` default and does not make a session (P1a-D1).
- **Target**: the `--home` a command writes, checked separately. `--by` is a display label: for a session it
  must name the actor or is refused (`by_mismatch`). Only the owner can produce an `operator:*` author.
- Access grants, humans.json `operators` and `--by` text are never read as authority.
- Honest limit: the shell is not sandboxed. A session that strips its environment looks like the owner.
  Warrant makes the supported path scoped and audited; it is not an OS boundary.

## Delegations (company log kind `delegation`)

`{grantee, actions, scope, sources | parent, context_refs?, note?}` in `<root>/company/memory.sqlite3`,
public in every profile (`roster_get`, people files) and retractable.

- **Root**: cites human words. Written by the **owner only** (`root_requires_owner`, D1). Each source is a
  `message` event in `--home` with non-null `auth`, role `human` under that home's humans.json (secretary,
  operator relays and Board events are refused), and an `--excerpt` that is an exact substring of its body.
  `auth` is copied from the stored event, never supplied or upgraded. `context_refs` (D5) are kept for
  context only and never satisfy the source check.
- **Child**: `parent` = an active root; written from a session that holds `delegation.grant` under that
  root, has the company write grant and is fenced on its run. Actions ⊆ parent minus `delegation.grant`
  (depth 1), scope ⊆ parent, grantee ≠ writer, no sources. Errors: `exceeds_parent`, `self_promotion`,
  `not_delegated`, `unknown_parent`.
- **Use**: `require()` finds an active delegation for the actor whose scope covers the target and walks
  child → parent; a retracted root invalidates its children. The owner retracts anything; a delegate
  retracts only children it wrote (`not_author`). Only the owner retracts a `retract` or `retire` entry
  (`owner_only`: undoing a revocation or a retirement); a session's retract of a `person` entry follows the role
  rules (`own_role`, `unauthenticated_role`) and may not remove a profile (`owner_only`) (issue #2). Only the
  owner writes a `retire` entry (`owner_only`, i2b); sessions holding the company write grant keep `person`,
  `alias` and `unalias`.

### Catalogue (no owner-only or credential item, nothing about private views — static test)

| Action | Scope keys | Phase |
|---|---|---|
| `project.grant` | `projects` (list), `max_mode` (read < post < write) | P1a |
| `project.redact` | `projects` | P1a |
| `company.grant` | `max_mode` (read < write) | P1a |
| `runtime.request` | `homes` — never `daily_limit` (`use_limit_raise`, D6a) | P1a |
| `delegation.grant` | — | P1a |
| `worker.manage` | `homes`, `ops` (⊆ install, start, stop, restart, remove; `kickstart` = restart) | P1b |
| `limit.resume` | `homes` | P1b |
| `limit.raise` | `homes`, `max_usd_per_day` (a number ≤ the `limit.ceiling` policy, initially 150; or `"policy"`, D18), `durations` (today, persistent) | P1b |
| `adapter.manage` | `labels` (label entries, see below, naming existing labels; start, restart, reinstall with ProgramArguments unchanged) | P1b |
| `kit.run` | `kits` (absolute paths; a kit file equals or sits under one, after `resolve()`) | P1b |
| `adapter.install` | `labels` (label entries, see below; the label itself must still be `com.synthetics.adapter.*`), `roots` (absolute paths the adapter's WorkingDirectory and code must sit under, after `resolve()`) | P1c-b |

Scope semantics (D6):
- `homes`: identity IDs, `employees` or `*`, always limited to **member homes**: after `resolve()`, the
  target sits directly under the actor's own company root and has a `state.sqlite3` (not `company/`), and
  (D8b) its identity is a registered, non-retired person of kind `employee` in the actor's company state.
  Another company's home, a stray temp home, a symlink pointing outside the root, a fresh unregistered
  home under the root, an unknown ID and a retired person are never covered.
- Attenuation is semantic: `employees` covers a named member; a named member does not cover `employees`;
  `*` is never allowed in a child.
- `labels` (D19): each entry is an exact launchd label, `*` (root only), or a **prefix pattern** ending in
  `.*`, matched on a dot boundary: `com.synthetics.adapter.*` covers `com.synthetics.adapter.fieldpost` and
  `com.synthetics.adapter.a.b`, never `com.synthetics.adapterX` or `com.synthetics.adapter.` itself. No other
  `*` is allowed (`bad_scope`). The same rule applies to `adapter.manage`, `adapter.install`, `authority settle`
  and `authority check --item`. A child entry is within its parent when it is an exact label the parent
  covers, or a pattern equal to or narrower than a parent pattern (`com.synthetics.adapter.fieldx.*` under
  `com.synthetics.adapter.*`); `*` and a wider pattern are `exceeds_parent`.
- A session never targets itself for `project.grant`/`company.grant` (`self_promotion`) or for
  `runtime.request` (its own worker uses `runtime_restart`, M19).

## Transactional enforcement and receipts (D4)

`require()` returns a warrant. Every delegated write to shared or target state ATTACHes the actor's Store
(and the company log) before `BEGIN IMMEDIATE` and re-checks, inside the target's write transaction, that
the actor's run is still `running` and that every delegation in the chain is still active in the company fold
(the same fold `covering()` reads, so a retract that was itself retracted no longer counts). It then writes a
receipt `{actor, run, delegation, chain, action, key}` in the target: project/company DB `audit` action
`warrant`, home audit `authority.receipt`. If the run finished or the chain was retracted in between,
nothing is written and the actor home audits `authority.refused` with `stage: write`.
Launchd effects (P1b `worker.manage`, `adapter.manage`) record the scoped request in the D14 ledger, execute at
idle and record confirmed / failed / uncertain; they are never blind-retried.

### Lifecycle ledger for launchd effects (D14)

The `authority.*` audit rows are best-effort diagnostics: a failed audit write never stops a command. The
lifecycle of a delegated worker or adapter op is therefore kept in a separate, **mandatory** ledger, the
`launch_ops` table of a home (worker ops: the target home; adapter ops: the actor home, since an adapter belongs
to no employee home).
- **D14a** Before any launchd effect the op is written as `pending` with a stable ID `op-<hex>` (actor, run,
  delegation, op, label, plist, `pid_before`, argv and `no_start` for installs). **D16b:** `pid_before` is the
  live PID, `null` when launchd reported no process, or `"unknown"` when it could not be read; unknown is never
  treated as absent. If that write fails, nothing is executed and the command fails with `ledger_unavailable`.
- **D14b** After the effect the op is settled `confirmed`, `failed` or `uncertain` (same evidence rules as D10b).
  If that write fails, the result is `uncertain` with `pending_op: <id>`, never confirmed, and the op stays
  pending. While an op on a label is pending, a new op on that label is refused (`pending_op`); nothing retries.
  **D16c:** a D4 refusal at the fence (raised before the effect began) settles the op `failed` and the error is
  raised. An exception raised once the effect may have started (e.g. plist written, then launchctl raised)
  settles it `uncertain`, never `failed`, and is returned as a result `{outcome: uncertain, error, op_id}`. In
  either case, if the settle write itself fails the op stays pending and the result/error carries
  `pending_op: <id>`; nothing retries.
- **Reconcile**: `authority ops [--all]` lists the ledger of `--home`; `doctor` warns `launch_op_pending`.
  `authority settle OP` judges the op from independent evidence only and executes nothing: install → the
  plist's Label and ProgramArguments, and unless the row records `no_start: true` also the start rule below
  (**D16b**: a plist alone never confirms an install+start; a row without `no_start` is judged as one); remove → plist gone and job unloaded; stop → unloaded; start/restart → a
  live PID different from `pid_before` (any live PID when it was `null`; never when `"unknown"`). Anything less
  settles `uncertain` (reconciled, so the label is free again). The owner may settle any op; a session needs
  the action that could have run it. **D16a:** for a worker op that is `worker.manage` whose `homes` cover the
  ledger's home (same `homes_cover` rule as the op) and whose `ops` cover the op; for an adapter op it is
  `adapter.manage` naming the label, and only in the session's own home (the ledger the op was written to;
  otherwise `not_own_ledger`). The settle write re-checks the session's run and delegation chain inside its
  own transaction: a run that ended or a delegation retracted after the evidence read refuses the write
  (`run_inactive` / `delegation_retracted`, audited with `stage: write`) and the op stays pending.
- **Relation to M19 runtime requests.** `runtime_requests` are settings changes that the target's own
  controller applies at its next idle point and records itself (applied/failed); D10c's deferred restart of a
  busy worker still uses them. The ledger records effects the **actor** executes now. They are not merged:
  a runtime request lives in, and is settled by, the target home, while an adapter op has no target home, and a
  request row is applied asynchronously, while a ledger row must exist before the actor's own launchctl call.

## Team policies (P1c, company log kind `policy`)

`{name, reason, context_ref?, ...fields}` in the company log, written with `authority policy NAME --body JSON
--reason R`. The owner may write any; a session needs the company write grant, an active run (fenced like
every company write) and the action that owns the policy: `limit.ceiling` → `limit.raise`,
`worker.defaults` → `worker.manage` (`not_delegated` otherwise). The **newest active entry per name wins**;
retracting it restores the previous one, and with none the initial value applies. Each session write is
audited in the actor home as `authority.policy` (`name`, `entry`). A log written before P1c simply has no
policies.
- `limit.ceiling` `{max_usd_per_day: number > 0 | "none", allow_disable: bool}`. Initially $150 (current
  team policy, D13). It bounds `limit raise` from a session, `max_usd_per_day` in new `limit.raise`
  delegations (`bad_scope`) and a delegated worker install's `--daily-limit`. **Lowering clamps, it does not
  invalidate**: existing delegations above it stay valid and are capped at use; `doctor` reports
  `limit_ceiling` and lists them under `limit_clamped` (info `limit_clamped`). `allow_disable: true` lets a
  delegated `limit raise 0` switch the guard off for today.
- `worker.defaults` `{allowed_models: [..], allowed_claude: [absolute paths]}`: the allowed lists a delegated
  worker install uses when the target publishes no runtime policy (see Workers). **D15:** every model and every
  `allowed_claude` realpath must appear in the lists that one owner-installed member worker publishes in its
  `runtime.state.json` (`authority.owner_allow_lists`: a registered, non-retired employee home under the
  company root, excluding any home whose own `launch_ops` ledger records a delegated worker `install`, so a
  delegated install can never vouch for its own defaults; an owner reinstall afterwards does not restore it).
  Checked when a session writes the entry and again at every use; an entry that has dropped out of every
  published list is refused at use. Owner writes are not checked at write but are at use (accepted deviation,
  Codex review-P1c-a.md: the owner may stage defaults before the worker that publishes them is installed).
  Refusal: `bad_policy`.
- `adapter.reviews` `{sums_path: absolute path, sums_sha256: 64 hex, review_ref}` (P1c-b, Q3): one accepted
  frozen review. Written like the others; a session needs `adapter.install`. **Unlike the other policies every
  active entry counts** (a list of accepted reviews, `authority.adapter_reviews`): a newer entry does not
  displace an older one, and retracting an entry removes only that review. See Adapters.

- `host.rules` `{text}` (PUB-1 G3): the host rules every session in a home under this company root sees, appended
  to the shipped prompt as "Host rules (host-assigned; company policy host.rules <entry>)" (protected checkouts,
  host paths: whatever must not ship in `synthetics/prompts/employee.md`). Newest active entry wins; retracting it
  restores the previous one. A session writer needs `runtime.request` whose `homes` are `employees` or `*` (the
  rules reach every home; named homes are `not_delegated`). `text` is non-empty, at most 8192 bytes. A home's own
  untracked `prompt.local.md` is appended after it; it adds rules, never replaces the company's. **Fail closed:**
  when the company log exists but cannot be read, `brain.system_prompt` raises and no session starts.
  `synthetics --home H system-prompt` prints what a new session of H gets (read-only; opens no Store).

## Spend limits (P1b, D7; P1c ceiling)

- `limit raise USD [--persistent]` from a session needs `limit.raise`: `0 < USD <= max_usd_per_day` and the
  duration in `durations`, and `USD <=` the effective `limit.ceiling` policy (`0` only with `allow_disable`).
  The $150 is **current team policy (manager-set), not an owner ceiling** (D13).
  **Lowering** `limit.ceiling` applies to every delegation at once (clamped at use). **Raising** it lets a session
  go higher only through a delegation whose `max_usd_per_day` is `"policy"` (D18); a numeric delegation keeps its
  own number, so with only the numeric bootstrap root a higher ceiling changes nothing until the owner records a
  `"policy"` root. `"policy"` means "bounded only by the effective `limit.ceiling`": with `limit.ceiling`
  `"none"`, a `"policy"` delegation has **no daily bound at all**. A child may be `"policy"` only under a
  `"policy"` parent (`exceeds_parent` under a numeric one); at use a child is also held to its parent's resolved
  bound. `doctor`'s `limit_clamped` never lists `"policy"` delegations. When several delegations cover the
  actor, the first that allows the amount is used (an additive root with a higher bound works). Any target, including the actor's
  **own home** (self-raise), must be covered by `homes` (D10a). Without `--persistent` it writes today's
  override in `usage_limit.json` (with `delegation`, `set_run`, `self_raise`); `--persistent` files a
  delegated runtime request for `daily_limit`, applied at idle. This is the only session path to a persistent
  `daily_limit` (`runtime request` refuses it, D6a). Above the effective ceiling, or `0` without `allow_disable`, is
  refused under team policy with `exceeds_ceiling` (D18-r2): the message names the current ceiling (its policy entry,
  or the initial $150) and that the manager can change it with `authority policy limit.ceiling`; the refusal is
  audited (`authority.refused`, `usd`, `ceiling`, `entry`). A `--date` other than today stays `owner_only` (not yet
  implemented under delegation).
- **Finite amounts only (D18-r2).** `limit raise` refuses `nan`, `inf` and `-inf` at argument parse time (exit 2), on
  the owner and the session path, today, `--date` and `--persistent`, before any authorization or state change.
  `limit.ceiling` `max_usd_per_day` and a delegation's `max_usd_per_day` must be finite numbers (`bad_policy` /
  `bad_scope`), and the `authority policy --body` / `authority delegate --scope` JSON refuses the `NaN`, `Infinity`
  and `-Infinity` tokens Python's parser accepts by default (`bad_json`).
- `limit resume` needs `limit.resume` with `homes` covering the target (it was ungated before P1b).
- File writes are fenced like DB writes: `BEGIN IMMEDIATE` over the attached actor Store and company log,
  re-check run and chain, write, commit. A raise after the run ended is refused (`run_inactive`).
- Every self-raise is audited as `authority.used` with `self_raise: true`; `doctor` lists them under
  `self_raises` (info `limit_self_raise`).

## Workers (P1b, D9)

`worker <op>` from a session needs `worker.manage` whose `homes` cover the member target and whose `ops`
name that `worker` subcommand (D9a; `kickstart` is accepted as an alias for `restart`, any other name is
`bad_scope`). A session never targets its own home (`own_home`: its own worker uses `runtime_restart`, M19).
- **install** (D9b) writes only the standard worker plist: label `com.synthetics.worker.<sha256(id)[:16]>`
  (`label_not_standard`), ProgramArguments = this Python `-m synthetics --home <target> serve …`. The model
  and claude must be in the target's published allowed lists (`runtime.state.json` policy). A target that
  publishes none (never installed, or no `--runtime-config`) takes the company `policy worker.defaults`
  (result `policy_source` names which); with neither → `no_policy`, "not yet implemented under delegation"
  (P1c). Another executable is `foreign_executable` (adapter install
  stays owner-only). `--allow-model/--allow-claude` may not widen those lists (default: copied), budget
  ≤ 100 (`budget_exceeds`), 0 < daily limit ≤ the `limit.ceiling` policy (`limit_exceeds`), `--limit-allow` is owner-only
  (`limit_allow_owner_only`), `--launch-agents-dir` is the default or under the system temp dir (`agents_dir`).
  An install over a label launchd already has loaded replaces a running worker, so it holds the idle boundary
  like stop (P1c); busy → `not_idle`, never deferred. Under that hold the label is booted out, its unload awaited
  (≤ 5 s) and the new plist bootstrapped (**G1**: `kickstart` of a loaded label would keep launchd's old
  definition); no unload in time → `uncertain`. **G2:** when a standard worker plist is already installed, the
  delegated install keeps its interpreter (`ProgramArguments[0]`, e.g. CLT `/usr/bin/python3`) instead of the
  delegate's own Python.
- **stop, restart, remove** (D9c, D10c) hold the target's idle boundary through the launchd op: in one
  target write transaction the CLI checks that no run is `running` and stores an idle hold
  (`capture_meta worker_hold`, expiry 120 s so a crashed holder cannot block the worker for long). While
  the hold is unexpired `run_begin` raises `WorkerHeld` and the engine starts no session; the hold is
  released after the op, also on error. A busy target: `restart` becomes a **deferred** M19 runtime request
  (no settings, origin `delegated`) that the target's own runtime controller carries out at its next idle
  point and records applied/failed (result `outcome: deferred`, audit `stage: deferred`); a target without
  `--runtime-config` cannot record it → `not_idle`. **Asymmetry (D11):** restart is deferred, but `stop` and
  `remove` of a busy target are **refused** (`not_idle`, "target busy; ask again when idle"): a worker cannot
  record the outcome of stopping itself. The refusal is audited in the actor home (`authority.refused` with
  actor, run, target, op), takes no hold, writes no runtime request, leaves the plist untouched and is never
  retried by anything.
- **Controller label (D12).** A worker started on pre-P1b code ignores `worker_hold`. Each non-install op
  records `controller` in its `requested` audit row and result: `current` (the target's runtime.state.json
  pid is the live launchd PID and lists capability `worker_hold`), `old_controller` (same pid, no capability:
  the op ran on the old idle check only), `unknown` (PID unreadable, no runtime state — e.g. no
  `--runtime-config` — or a state for another PID) or `not_running`. Engines publish `capabilities` from
  `runtime.CAPABILITIES`. The label is informational; the op is not refused on it.
- Each op is audited in the target home as `authority.worker` `stage: requested` (with `pid_before`), then
  `stage: outcome` with `confirmed`, `uncertain` or `failed` (the launchd call raised). Confirmation needs
  independent evidence: install → the plist's Label/ProgramArguments; remove → the plist gone and the job
  unloaded; stop → `launchctl print` unloaded, polled for up to 5 s after bootout (**G3**: bootout returns
  before the job is gone); start/restart (D10b) → a live PID (signal 0) that differs
  from the PID captured before the op, within 10 s — a merely loaded label, an unchanged PID or an unknown
  pre-op PID is `uncertain`. Nothing is retried. The effect runs under the D4 file fence; its lifecycle is in
  the target's D14 ledger (`op_id` in the result).
- Self-targets get no shortcut (D10a): a `limit.raise` on the actor's own home must be covered by the
  delegation's `homes` like any other target (the root's `employees` covers it; a child naming only another
  member does not).

## Adapters (P1b, D2)

`adapter <op> --label L` from a session needs `adapter.manage` whose `labels` cover L (exact, or a `.*` prefix
pattern, D19; `*` only in a root). Only these ops are delegated, and only for a label whose plist is already installed (`unknown_label`
otherwise; `--launch-agents-dir` as for workers):
- **start**, **restart**: confirmed only by a new live PID (as D10b), else `uncertain`.
- **install** as a reinstall: the given argv and `--keep-alive` must equal the installed plist's
  ProgramArguments and KeepAlive (`argv_changed`); confirmed by reading the plist back (and, unless
  `--no-start`, a new live PID).
- **stop**, **remove** stay owner-only (`owner_only`): they cut a channel and nobody would be told.
A new label, a new executable or changed arguments need `adapter.install` (below); without it they stay
owner-only `adapter install`.

### Installing a reviewed adapter (P1c-b, `adapter.install`)

`adapter install --label com.synthetics.adapter.NAME --cwd DIR --review SUMS [--keep-alive K] [--no-start] -- ARGV`
from a session needs `adapter.install` whose `labels` cover the label (D19) and whose `roots` contain DIR and every
code file. The owner may also pass `--cwd` (plist WorkingDirectory; default the Synthetics checkout, as before).
Checks, each refused with the code shown and audited (`authority.refused`, action `adapter.install`):
- **Executable (Q1):** `ARGV[0]` resolves to `/usr/bin/python3` or `/Library/Developer/CommandLineTools/usr/bin/python3`
  (`foreign_executable`). Any other executable — third-party or unreviewed — stays with David.
- **Form:** `python [-u] -m <module> [args]` (module found under `--cwd`) or `python [-u] <script.py> [args]`;
  `-c`, other interpreter flags and other forms → `bad_argv`. `--cwd` is required (`bad_cwd` if not a directory).
- **Code under roots:** `--cwd` and the code files after `resolve()` (so a symlink cannot escape) must sit under
  the scope's `roots` (otherwise no delegation covers it: `not_delegated`).
- **Reviewed code (Q3):** `--review` is a SHA256SUMS file whose path and sha256 equal an active `policy
  adapter.reviews` entry (`review_not_accepted`). Every code file must be listed — a module's file, or for a
  package **every** `*.py` under it, so an unreviewed file added beside reviewed ones is refused
  (`unreviewed_file`) — matched by its path relative to `--cwd` (a listed path equal to it or ending in `/` + it,
  e.g. `tree/fieldpost/relay.py` for `fieldpost/relay.py`), with the same sha256 now (`review_mismatch`).
- **Credentials:** a `--credential-file`/`--key-file` value must be an existing regular 0600 file of this user
  under the channel key dir (`bad_credential_file`). It is only `stat`ed: never read, created or copied; keys
  are minted host-side.
- **Whole package (D17b):** for `-m a.b.c` the enclosing local package is the outermost `a`, `a.b`, … that has an
  `__init__.py` under `--cwd`; importing the module runs it, so **every** `*.py` in that package (each parent
  `__init__.py` included) must be listed. A script inside a package directory brings in the package the same way
  (walking up while the parent has `__init__.py`, never above `--cwd`). Package files that resolve outside
  `--cwd` → `bad_argv`.
- **Re-check at the effect (D17c):** inside the D4 fence, just before the plist is written, the `adapter.reviews`
  entry must still be active, the sums file must still have its accepted sha256 and every planned code file must
  still have the sha256 it had at plan time (`review_not_accepted` / `review_mismatch`; nothing written, op settled
  `failed`). The package is also enumerated again with the same walk the plan used (D17d): a `.py` added to or removed
  from it since planning is `review_mismatch` too, even though every planned sha still matches. **Documented limit:** a file changed after that check — between the plist write and launchd exec'ing
  the code — is not detected.
- **Settle (D17a):** the ledger row records `action: adapter.install`, `cwd` and the planned `files`; `authority
  settle` judges it under that action (labels plus `roots` covering cwd and every ledgered file, own ledger only,
  fenced as D16a), and the plist evidence must also match `WorkingDirectory` = the recorded cwd (else `uncertain`).
- **Labels (Q2):** only `com.synthetics.adapter.*`. Replacing or relabelling the existing fieldpost, fieldstone
  or telegram plists is not yet implemented under delegation.

Effect and outcome as for the other adapter ops: D14 ledger row in the actor home (with argv, cwd, `no_start`,
review), D4 file fence, audit `authority.adapter` (with the review). An install over a loaded label reloads it
(bootout, wait, bootstrap: G1). Confirmed by the plist read back (Label, ProgramArguments, WorkingDirectory)
and, unless `--no-start`, a new live PID; otherwise `uncertain`. Nothing is retried. Adapters belong to no
single employee home, so each op is audited in the **actor** home as `authority.adapter` (`requested` with
`pid_before`, then `outcome`). The effect runs under the D4 file fence; nothing is retried. The lifecycle is
in the actor home's D14 ledger.

## Kits (P1b, item 16)

A frozen operator kit no longer refuses every session outright. Its guard, when `SYNTHETICS_SESSION_RUN` is
set, runs `python3 -m synthetics --home "$SYNTHETICS_HOME" authority check kit.run --item <its own resolved
path>` and stops with exit 2 unless that succeeds. The check needs an existing kit file (`bad_item`) that
equals or sits under one of the delegation's `kits` after `resolve()` (so `..` and symlinks cannot escape);
a child's `kits` must each sit under a parent path (`exceeds_parent`), `*` never in a child. Passing the
guard grants nothing else to commands on member homes: they are checked again on their own (owner-only items
stay refused). A kit whose body acts only on a **scratch company it creates** (a fresh `mktemp -d` root; every
home, key and agents path refused unless under it) may `unset SYNTHETICS_HOME SYNTHETICS_SESSION_RUN` after
the check, so its init/provision/postern steps run on that scratch company; the delegation for that exact
reviewed file is the authority. The M25 P3 kit (`reviews/M28/P1b/kit-guard/p3-run.sh`) is such a kit. A kit
that touches member homes or live keys must keep the session variables. Each check is audited in the actor
home.

## Audit

Actor home: `authority.used` (allowed), `authority.refused` (with `code`), `authority.delegation.*`, `authority.policy`,
`authority.settle`. These rows are best effort; the launchd op lifecycle is the D14 ledger, not the audit.
Owner actions that name a target home: `authority.owner` there.

## What stays refused, and why (D2, D3)

| Command | Refused for a session because |
|---|---|
| `project import`, `roster company import` | recovery-only: rebuilds an empty store from an export |
| `roster company redact` | the company log has no source bytes; retract instead |
| `access grant`, `access revoke` | not yet implemented under delegation (owned: Vesper backlog, scoped read-only `access.grant`) |
| `work … --override` | evidence integrity: it closes work without citable evidence |
| `handoff-codex` (history import) | fabricated provenance: writes events with chosen senders into a home |
| `init` | defines who is human in a new home (humans.json) |
| `telegram init`, `serve`, `install`, `stop`, `restart` (`status` is allowed) | `init` takes a bot token file, `serve` is the adapter process, and the others write or drive launchd jobs; `install --with-worker` writes a worker plist with caller-chosen model, executable and budget, which `worker.manage` (`synthetics worker`) bounds (issue #8) |
| `limit raise` with a `--date` other than today | not yet implemented under delegation (`owner_only`). Above the `limit.ceiling` policy or `0` without `allow_disable` is not owner-only: it is the team policy refusal `exceeds_ceiling` (D13, D18-r2), changed with `authority policy limit.ceiling` |
| `adapter install` of a new label/executable or changed ProgramArguments without `adapter.install` and an accepted `--review`, or with another executable, code outside the roots or differing from the reviewed sums | third-party or unreviewed executables stay with David (D13); replacing non-`com.synthetics.adapter.*` labels: not yet implemented under delegation |
| `authority policy worker.defaults` with a model or executable no owner-installed worker publishes | an unreviewed executable or model stays with David (D13, D15): `bad_policy` |
| `adapter stop`, `adapter remove` | cuts a channel; delegated adapter ops are start/restart/reinstall only |
| `authority delegate` without `--parent` (a root) | the owner's judgement that a human's words authorize this scope (D1) |
| `roster show`, `roster seed --apply` on another home | private views stay private (own home only) |
| `channel provision/register/contract`, `feed/ingest --credential-file`, `postern run`, `auth backfill` | credential boundary: holds or mints channel keys |
| `question-routed` | it chooses who may answer a question (credential-class) |

## CLI

```sh
synthetics --home H authority delegate --grantee G --action A [--action …] --scope JSON \
    --source EVENT --excerpt TEXT [--context-ref EVENT] [--note N] --by LABEL [--key K] [--dry-run]   # root, owner
synthetics --home H authority delegate --parent ROOT --grantee G --action A --scope JSON           # child, session
synthetics --home H authority retract ID --reason R
synthetics --home H authority show [--grantee G]
synthetics --home H authority policy NAME --body JSON --reason R [--context-ref EVENT] [--by LABEL] [--key K]
synthetics --home H system-prompt                          # read-only: shipped prompt + host rules for H
synthetics --home H authority check ACTION [--project P] [--mode M] [--target-home T] [--item X]
synthetics --home H authority ops [--all]                  # D14 ledger (default: pending ops)
synthetics --home H authority settle OP                    # D14 reconcile from evidence; executes nothing
```
