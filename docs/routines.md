# Routines (RTN)

A routine is a recurring review that an employee does on a fixed cadence: an hourly check of its own work, a
manager's team review, and (from P2) a daily dream that consolidates memory. It is built on what already exists:
an ordinary work record, the clock that fires a waiting work's `wake_at`, and a normal work session. There is no
scheduler and no new database.

## How it works

- **The record.** `routine seed` creates one work per kind with a store-written tag `work.routine`:
  `{v, kind, every_minutes, anchor_at, last_fired_at, previous_fired_at, last_rearmed_at, rearmed_by_run,
  last_review, quarantine, failures, covered_through, coverage}`. Only the store and the `routine` CLI write it.
- **Firing.** When `wake_at` passes, `due_events` fires it like any waiting work (a `work_due` event, status
  ready). It touches no other work, wake_at or question link.
- **What the session sees.** Every snapshot carries `routines_due`: routines that have fired, are quarantined,
  or are due within 15 minutes. Each item has `work_id`, `kind`, `state` (`fired`, `quarantined`, `due-soon`),
  `cycle` (pass it to `routine_rearm` unchanged), `rearm_wake_at` (the ready-made next wake), `report_to` (E2 below), `guidance` (that kind's section of
  `synthetics/prompts/routines.md`) and, for dream, `covered_through` and `evidence_cutoff`.
- **The only proof of review: MCP `routine_rearm(work_id, review, cycle, covered_through?, omissions?)`.** One
  transaction stores `last_review {run, at, state, text}` (at most 2048 characters), sets the routine waiting at
  the next target `anchor_at + k·every` (smallest k ≥ 1 in the future, so neither a long session nor an early
  review drifts the cycle), writes `last_rearmed_at` and `rearmed_by_run` (the calling run) and clears
  `quarantine`. It is accepted only while `routines_due` lists the routine. Nothing else writes
  `rearmed_by_run`.
- **Compare-and-set (P1-r1, addendum (a)).** `cycle` is the routine's `anchor_at` as `routines_due` showed it.
  A re-arm moves the anchor, so a repeat call, or a second session holding the same snapshot, names a closed
  cycle and is refused ("stale re-arm"), never applied twice. This matters most when `every` is below the
  15-minute fold window (minimum 5), where the new target is immediately due-soon again.
- **Dream evidence (E1).** The cutoff is the run's `started_at`. `covered_through` T must satisfy previous ≤ T
  ≤ cutoff and T ≤ every omission's `from`. An omission without `from` (a capped or truncated retrieval whose
  oldest unread item is unknown) means T can't advance at all; `since=` alone never justifies an advance.
  Omissions are stored in `coverage`.
- **Absorb, never skip.** Any session may absorb a due routine, but only by doing its review and calling
  `routine_rearm`. A session that doesn't changes nothing: the routine still fires on time. Recent activity,
  another session or a shared timestamp never suppresses or re-arms it.
- **work_put can't change a routine.** MCP `work_put` (and CLI `work update`) refuse a change to a routine's
  status or wake_at, naming `routine_rearm` and the CLI. Notes and titles may change.
- **Quarantine (D1).** At run end (finished, failed or timed out, interrupted), a routine that the run captured
  and that is still ready is quarantined: `quarantine {run, at, reason}` plus a `failures[]` entry that is
  never removed. It gets no wake, and `runnable_work` excludes it, so it can't start sessions. Unrelated work
  goes on. The next session sees it as `state: quarantined`. Only `routine_rearm` or `routine restore` clears
  it; neither clears `failures`.
- **A held input never starts a session (P1-r1 F1).** A failed or timed-out run leaves its inputs pending for
  retry. A pending `work_due` for a routine that is quarantined or no longer ready is *held*
  (`Store.admission_held`): the engine's idle check, `run_begin` and the limit-pause selection skip it, so it
  can't start another session or build a failure streak. It stays pending as evidence. The next session
  started by any other input shows the quarantine in `routines_due`; `routine_rearm` consumes the held input.
  After an operator `routine restore` the old input stays pending and held (it never runs).
- **Quarantine is never silent (addendum (c)).** It shows in that home's `routines_due`, in `doctor`
  (`routine_quarantined`, `routine_failures`) and on Lookout's Routines block.
- **working_context (addendum (b), P2).** Written only by the employee's own dream session through the normal
  memory path (`memory_put` with `replaces`), never by another home or the CLI.
- **Cost.** A quiet session costs about the same as any single-input session ($0.5-1.7 measured on 11-19).
  Hourly for three employees plus a daily dream is roughly $80-95/day of usage, measured against each home's
  enforced daily limit. Defaults stay at 60 minutes. If a limit pause holds a routine, it shows as
  `routine_overdue`.

## Manager routing (E2)

`report_to` is resolved from configuration only:
1. this home's escalation identity, `limit_notify`: the running worker's effective setting
   (`runtime.state.json`) first, then `runtime.json`;
2. canonicalised through the OFFICIAL company roster (`roster.company_state_for_home`: people and aliases),
   never this home's private views, when that name has a profile or alias.

If neither file names one, the result is `{resolved: false, reason: "no manager resolved: ..."}`. Seed prints
it, `routines_due` items carry it as `route_problem`, and doctor warns `routine_no_manager`. When the home is
itself the manager, its team review reports to `default_human` from humans.json, and says so if none is set.
There are no hardcoded IDs, and nothing is inferred from role text.

## Commands and who may run them (E3)

| Command | Owner (host account, operator) | A session (SYNTHETICS_SESSION_RUN set) |
|---|---|---|
| `synthetics --home H routine seed --kinds K[,K] [--every K=MIN] [--dream-at HH:MM] [--first K=WHEN]` | yes | refused `owner_only`, own home or any other |
| `routine cadence KIND MIN` | yes | refused `owner_only` |
| `routine restore KIND` | yes | refused `owner_only`; a session clears its own quarantine with `routine_rearm` |
| `routine teardown KIND… \| --all [--reason R]` | yes | refused `owner_only` |
| `routine list` | yes | own home only; another home is refused `own_home_only` |
| MCP `routine_rearm` | no: it needs a run, and operators use `restore` | yes, own home (the MCP server is bound to the session's run) |

These reuse the existing guards. The CLI checks the caller with `authority.actor()`/`owner_only` before it
opens a Store, as `init` and `access grant` do. G2 (P1-r1): there is no delegated path; the owner (Root's or the
operator's shell) runs seed, cadence, restore and teardown for every home. The store methods refuse inside a run
(`routine_seed`, `routine_cadence`, `routine_restore` and `routine_teardown` need run_id None), and
`routine_rearm` refuses outside one. As `docs/authority.md` §0 says, the shell is not a sandbox: a session that
strips its environment looks like the owner. The fence keeps the supported path scoped and audited
(`authority.refused` rows).

`--first KIND=<ISO-8601Z | +MIN>` (G1) sets only the first wake of a kind that seed creates, from now+1 min to
now+every; anything else is refused. For a kind that already exists it is ignored and listed under `existing`
with `first_ignored`. The anchor stays on the regular grid, so the first `routine_rearm` returns to the normal
cadence (dream: the next `--dream-at`).

## Provisioning a home

1. **Order.** `init` and provision the home, then `routine seed`, then `worker install`. A worker that is
   already running must restart to load the new code and `prompts/routines.md` (after the merge, restart every
   worker anyway).
2. **Seed.** `synthetics --home H routine seed --kinds hourly-check` (developer), or `--kinds team-review` for
   the manager (team review includes the manager's own check, so it doesn't also need hourly-check). Kinds come
   from the flag only; nothing defaults from the role. `dream` is refused until P2.
3. **Initial wake.** Each routine starts waiting with `wake_at = now + every` (dream: the next `--dream-at`,
   default 01:30Z, just after the UTC daily-limit reset). `anchor_at` starts there.
4. **Cadence.** `--every` is in minutes, minimum 5 (use 5 in a test home to watch two fires). The default is 60
   for hourly-check and team-review, and daily for dream. `routine cadence` changes `every_minutes` from the
   next re-arm.
5. **Verify.** `synthetics --home H routine list` shows each routine (status, wake_at, last fired, last
   re-armed, failures), its flags and the manager route. `synthetics --home H doctor` reports the flags below,
   and Lookout's Team page has a Routines block.
6. **Teardown.** Before `worker remove`, run `routine teardown --all --reason "..."`. It cancels the routines,
   so nothing fires into a removed home and no `routine_closed` flag is raised. Seeding again later creates
   fresh routines.

## Discipline flags (doctor, status.json `routine_flags`, Lookout)

| Code | Level | Meaning |
|---|---|---|
| `routine_quarantined` | error | left ready at run end; needs attention (re-arm or restore) |
| `routine_failures` | warning | count of quarantines, all time and in the last 7 days; never cleared |
| `routine_left_ready` | error | fired more than 5 minutes ago, no session running |
| `routine_unarmed` | error | waiting without a wake_at |
| `routine_overdue` | warning | wake_at passed by more than max(10 min, every/2) without firing (worker stopped or paused) |
| `routine_closed` | warning | done or cancelled without teardown; restore with `routine restore KIND` |
| `routine_no_manager` | warning | no manager resolved (E2) |

## Dream checkpoint (D2, E1; guidance in P2)

`routine_rearm(covered_through=T, omissions=[{reason, from?}])` is accepted only for the dream routine, and
only when:
- T ≤ the session's evidence cutoff, which is its run's `started_at`, shown as `evidence_cutoff`;
- T ≥ the previous `covered_through` (it never moves back);
- T ≤ the earliest `from` among the omissions (it never passes unread material).

Each omission needs a reason (truncated, capped, tool limit, or a `since=` search that didn't reach the
cutoff). The store records `coverage {run, at, cutoff, through, advanced, omissions}`. Leaving out T keeps the
checkpoint where it is, as does a failed or interrupted dream, which is also quarantined. A since= search or
reaching a tool limit alone never justifies advancing it: the store can't see what was read, so that rule
lives in the guidance and in the `from` bound.
