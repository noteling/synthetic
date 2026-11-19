# Routines (RTN)

A routine is a recurring review that an employee does on a fixed cadence: an hourly check of its own work, a
manager's team review, and a daily dream that consolidates memory and leaves the next session a working context. It is built on what already exists:
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
- **Nothing else moves a routine (P2-fix).** Its status and wake_at change only through `routine_rearm`, the
  quarantine below, or the `routine` CLI. `question_ask` (`work_id` or `work_ids`), `work_put` `waiting_for`
  naming a question, and `reminder_set` all refuse to link to a routine: link the question or reminder to the
  real assignment work, or leave it unlinked. An authenticated message naming a routine as `recipient_work_id`
  is received as usual but changes nothing (`work_ref.reason` says why). A reminder or question linked before
  this rule only loses its link when it fires or is answered.
- **Quarantine (D1).** At run end (finished, failed or timed out, interrupted), a routine that the run captured
  any input for (its `work_due`, a reminder, a work ref), or that is ready with no pending input of its own,
  and that is still ready is quarantined: `quarantine {run, at, reason}` plus a `failures[]` entry that is
  never removed. It gets no wake, and `runnable_work` excludes it, so it can't start sessions. Unrelated work
  goes on. The next session sees it as `state: quarantined`. Only `routine_rearm` or `routine restore` clears
  it; neither clears `failures`.
- **A held input never starts a session (P1-r1 F1).** A failed or timed-out run leaves its inputs pending for
  retry. A pending `work_due` for a routine that is quarantined or no longer ready is *held*
  (`Store.admission_held`): the engine's idle check, `run_begin` and the limit-pause selection skip it, so it
  can't start another session or build a failure streak. It stays pending as evidence. The next session
  started by any other input shows the quarantine in `routines_due`; `routine_rearm` consumes the held input.
  An operator `routine restore` consumes it too (P2 n3: marked processed by `routine.restored`, never run), so
  held inputs don't pile up.
- **Quarantine is never silent (addendum (c)).** It shows in that home's `routines_due`, in `doctor`
  (`routine_quarantined`, `routine_failures`) and on Lookout's Routines block.
- **One notice per quarantine (P2 N3).** In the same run-end transaction, the home queues one ordinary outbox
  message from the employee to `report_to` (Manager routing below). It is brief and owner-level (H4): which
  scheduled review did not finish, that it needs recovery, and that other work continues, e.g. "Rowan's scheduled
  hourly check did not finish and needs recovery; it stays paused until then. Other work continues." It carries
  no commands or paths; the recovery steps are in doctor (`routine_quarantined`), this page and
  `quarantine.notice.recovery {session, owner, doctor, docs}`. It is keyed `routine-quarantine-<work id>-<anchor_at>`, so later polls, a worker
  restart or a second failure of the same cycle send nothing more; a repair (`routine_rearm` or `restore`) moves
  `anchor_at`, so only a fresh quarantine sends a new one. There is no model call and no re-arm. When no
  recipient resolves, nothing is sent and doctor warns `routine_no_manager`. `quarantine.notice` records
  `{key, sent, message_id, to, recovery | reason}`.
- **working_context (addendum (b), D3).** Written only by the employee's own dream session through the normal
  memory path (`memory_put` with `replaces`), never by another home or the CLI. See "The dream" below.
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

**The whole provisioning sequence is owner-only.** `init`, `routine seed`, `cadence`, `restore`
and `teardown` are refused to a session on every home, its own or another, including a disposable home under
`/tmp`. `worker install` is owner-only, except a delegated `worker.manage` reinstall of the standard worker
plist on another home that the delegation covers (never the session's own home, which uses
`runtime_restart`; see [authority.md](authority.md)). A session may run only `routine list` on its own home,
`routine verify` (read-only, fenced like doctor; [ledgerline.md](ledgerline.md)) and MCP `routine_rearm`.

| Command | Owner (host account, operator) | A session (SYNTHETICS_SESSION_RUN set) |
|---|---|---|
| `synthetics --home H init --name N --role R [--purpose P] --human ID` | yes | refused `owner_only` (`docs/authority.md`) |
| `synthetics --home H routine seed --kinds K[,K] [--every K=MIN] [--dream-at HH:MM] [--first K=WHEN]…` | yes | refused `owner_only`, own home or any other |
| `synthetics --home H routine cadence KIND MIN` | yes | refused `owner_only` |
| `synthetics --home H routine restore KIND` | yes | refused `owner_only`; a session clears its own quarantine with `routine_rearm` |
| `synthetics --home H routine teardown KIND… \| --all [--reason R]` | yes | refused `owner_only` |
| `synthetics --home H routine list` | yes | own home only; another home is refused `own_home_only` |
| `synthetics --home H routine verify RUN [--spill-bytes N]` (i17, [ledgerline.md](ledgerline.md)) | yes | fenced exactly like doctor: own home, or another home with a covering delegation (audited); else `own_home_only` |
| `synthetics --home H prune-runs [--dry-run]` (E4 runs/ retention, [ledgerline.md](ledgerline.md)) | yes | own home only (audited); another home is refused `own_home_only` |
| `synthetics --home H doctor` | yes | own home; another home only with a covering `worker.manage` or `runtime.request` delegation (audited), else `own_home_only`; host-path flags own home only (G4, "doctor on another home" below) |
| MCP `routine_rearm` | no: it needs a run, and operators use `restore` | yes, own home (the MCP server is bound to the session's run) |

A refused command prints `synthetics: owner_only: routine seed is owner-only: provisioning (RTN,
docs/routines.md); …` (or `own_home_only: …`), exits 1 and writes an `authority.refused` audit row.

These reuse the existing guards. The CLI checks the caller with `authority.actor()`/`owner_only` before it
opens a Store, as `init` and `access grant` do. G2 (P1-r1): there is no delegated path; the owner (Root's or the
operator's shell) runs seed, cadence, restore and teardown for every home. The store methods refuse inside a run
(`routine_seed`, `routine_cadence`, `routine_restore` and `routine_teardown` need run_id None), and
`routine_rearm` refuses outside one. As `docs/authority.md` §0 says, the shell is not a sandbox: a session that
strips its environment looks like the owner. The fence keeps the supported path scoped and audited
(`authority.refused` rows).

`--first KIND=<ISO-8601Z | +MIN>` (G1) sets only the first wake of a kind that seed creates, from now+1 min to
now+every; anything else is refused. Repeat the flag once per kind (`--first hourly-check=+2 --first
dream=+5`). A comma list in one flag is refused, because everything after the first `=` is read as the time.
If a kind is given twice, the last one wins; a `--first` for a kind not in `--kinds` is checked and then
ignored. For a kind that already exists it is ignored and listed under `existing`
with `first_ignored`. The anchor stays on the regular grid, so the first `routine_rearm` returns to the normal
cadence (dream: the next `--dream-at`).

## Provisioning a home

Every step below runs in an owner shell (no `SYNTHETICS_SESSION_RUN`); see the table above.

1. **Order.** `synthetics --home H init --name N --role R --human ID` (add `--purpose P`). On a fresh home
   `--human ID` is required: it writes `H/humans.json` with that person as `default_human` and sender, and
   makes them the initial colleague. Without `--human` (or `--by ID`) init fails with `no --by given and
   H/humans.json has no default_human` and leaves an empty humans.json, after which `--human` is refused
   (`already exists; edit it instead`): set `"default_human"` and `"senders"` in that file and rerun init
   without `--human`, or delete the empty file and rerun with `--human`. `--by ID` alone initializes the
   home but writes no default_human (the humans.json stays empty, so doctor warns `humans_empty`) and later commands such
   as `access grant` need `--by`. Then `worker install --runtime-config
   --limit-notify MANAGER` (see Manager route below), then `routine seed`. Seeding before the worker is
   installed also works; only the route reads differently (step 2). A worker that is already running must
   restart to load the new code and `prompts/routines.md` (after the merge, restart every worker anyway).
   **Which code runs:** the worker plist runs the absolute Python that ran `worker install`, as
   `python -u -m synthetics --home H serve …`, with `PYTHONPATH` set to the checkout that `worker install`
   was run from. So it runs that tree, fixed at install time: update that checkout and restart the worker to
   load new code; to run a different checkout, reinstall from it.
2. **Seed.** `synthetics --home H routine seed --kinds hourly-check` (developer), or `--kinds team-review` for
   the manager (team review includes the manager's own check, so it doesn't also need hourly-check). Add
   `dream` for any employee whose memory should be consolidated daily (`--kinds hourly-check,dream`,
   `--dream-at HH:MM`). Kinds come from the flag only; nothing defaults from the role.
   **Output.** Seed prints one JSON object:
   `{"created": [{kind, work_id, wake_at, every_minutes}], "existing": [{kind, work_id, status,
   first_ignored?}], "route": {resolved, manager, via, self_is_manager?, report_to, reason?}}`
   (`self_is_manager` is absent on an unresolved route; `reason` appears on an unresolved route, or when this
   home is the manager and humans.json has no `default_human`). A second seed
   with the same kinds creates nothing: each kind is listed under `existing` (with `first_ignored: true` when
   `--first` named it). A kind counts as existing while any of its routine works is not cancelled.
   **Manager route.** The route is worked out each time it is used (seed, `routine list`, doctor,
   `routines_due`, a quarantine notice), never stored at seed. `limit_notify` lives in the worker's runtime
   configuration, which the worker writes on its first start from `worker install --runtime-config
   --limit-notify MANAGER`. Install the worker that way before seeding to see `resolved: true` in seed's
   output; seeding first shows `resolved: false` ("no manager resolved: runtime limit_notify is not set for
   this home") and doctor warns `routine_no_manager` until the worker's first start. To fix
   `routine_no_manager` on a home whose worker already runs, set it with `synthetics --home H runtime request
   --set limit_notify=MANAGER --reason R --by ID` (owner, or a session holding `runtime.request` on that
   home, `docs/authority.md`); the change applies when the worker restarts.
3. **Initial wake.** Each routine starts waiting with `wake_at = now + every` (dream: the next `--dream-at`,
   default 01:30Z, just after the UTC daily-limit reset). `anchor_at` starts there.
4. **Cadence.** `--every` is in minutes, minimum 5 (use 5 in a test home to watch two fires). The default is 60
   for hourly-check and team-review, and daily for dream. `routine cadence` changes `every_minutes` from the
   next re-arm.
   **Restore** (`routine restore KIND`) works on any routine that wasn't torn down, not only a quarantined
   one: it sets the routine waiting at the next target after its current `anchor_at`, clears `quarantine`
   (`failures` stay), reopens a done routine and consumes a held `work_due`. On a routine that is waiting
   and not yet due, the next wake becomes `anchor_at + every`. Normally the anchor is the pending wake, so
   this moves the next wake one cycle later. After `--first`, seed sets `wake_at` to the `--first` time but
   `anchor_at` to the regular first wake minus `every`, so a restore before the first fire drops the
   `--first` time and returns to the regular first wake. A torn-down routine is refused ("seed it again").
5. **Verify.** `synthetics --home H routine list` shows each routine (status, wake_at, last fired, last
   re-armed, failures), its flags and the manager route. `synthetics --home H doctor` reports the flags below,
   and Lookout's Team page has a Routines block.
   **doctor on another home (G4).** The owner shell runs doctor on any home, unchanged. A session runs it on
   its own home. On another home H it runs only while the session holds an active `worker.manage`,
   `adapter.manage` or `runtime.request` delegation whose `homes` cover H (`homes_cover`: a registered member
   of the same company). `adapter.manage` is scoped by `labels`, not `homes`, so today it covers no home. An
   allowed read writes an `authority.used` row (`what: doctor`, target, action and the covering delegation
   ID) in the session's home; anything else is refused `own_home_only` with an `authority.refused` row and
   prints nothing of H. A retracted delegation, or a session whose run has ended, covers nothing. In a
   session the flags that read host paths or host state outside the target home are own-home only, even
   when delegated: `--key-file`, `--collector-plist`, `--compare-legacy`, `--relay-routes` / `--relay-channel`,
   `--collector-dir` and `--label`. So the quarantine notice's doctor hint (`routine_quarantined`) means the
   owner, the employee on its own home, or a manager holding a delegation that covers that home.
   What doctor shows (metadata, never message, memory, view or event bodies): identity ID and name, schema
   version, humans.json `default_human` and senders, runtime settings (model, budget, daily limit,
   `limit_notify`, `limit_allow`, claude path, timeout), the `usage_limit.json` override as stored, worker and
   launch-agent state and pending launch ops, outbox and capture counts and ages (activity timing) and for
   stuck messages the newest message ID and recipients, owed relays (question and asker IDs), the latest
   failed run ID, daily-limit self-raises (amount, run, delegation, duration), the limit-ceiling entry, roster
   findings (the count of private views and the person IDs they name, for `view_unresolvable` and
   `view_stale`), path strings in findings, and routine work IDs, kinds, flags and route. Team-wide health
   is on Lookout and each home's `shared/status.json`.
6. **Teardown.** Before `worker remove`, run `synthetics --home H routine teardown --all --reason "..."`. It cancels the routines,
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

## Dream checkpoint (D2, E1)

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

## The dream (P2)

The `## dream` section of `synthetics/prompts/routines.md` is the guidance; the mechanics are:

- **Retrieval in time order (since=).** MCP `memory_search(since=S)` and `event_search(since=S)` return
  `{memories|events, since, capped, unread_from, next_cursor, truncated_from}`: items created at or after S
  (inclusive, by `created_at`), oldest first, with every query word required (no ranked fill). Ties on
  `created_at` are ordered by memory ID or event arrival (`seq`). `capped: true` means the limit or the byte
  budget (below) cut the page; `unread_from` is
  the `created_at` of the first item not returned (the `from` of an omission if the dream stops there), and
  `next_cursor` continues strictly after the last item returned (`cursor=`), so items sharing a timestamp are
  never repeated or skipped (H2). Without `since` or `cursor` both tools behave and return exactly as before.
- **Results fit tool output (F2).** Tool output has a size ceiling (not measured: 25 KB showed in full and
  80 KB spilled), and event bodies range up to ~90 KB, so no fixed `limit` fits. Through MCP every
  `event_search` and `memory_search` result stops before `max_bytes` (default 32 000 bytes of serialized
  JSON) and returns at least one item; `capped` says it stopped. Query/thread mode then returns
  `{events|memories, capped, ...}` instead of a bare list. On since= pages bodies stay whole: the page holds
  as many complete records as fit, and only a first item too large alone is cut to fit. Without since,
  event bodies are cut to `body_chars` (default 500): search finds events, `event_get` reads them. Every
  event carries `body_len` and `body_truncated`, and `truncated_from` is the `created_at` of the first cut
  item (null if none). A cut item keeps the longest body prefix that fits, measured in serialized bytes. It
  still counts as returned, so `next_cursor` moves past it. Memories are
  never cut. `event_get(id, body_offset=, body_chars=)` returns a slice of the original body with
  `body_len`, `body_offset` and `body_next` (null at the end; MCP default chunk 30 000 characters;
  `body_chars` is at least 1, so a chunk loop always advances); a plain
  `event_get(id)` is unchanged. **Reading rule:** a `body_truncated` item is unread until `event_get` has
  covered its body from 0 to `body_len`; until then `truncated_from` bounds `covered_through` and is the
  omission's `from`. The store's own defaults are unchanged (no `body_chars`, no `max_bytes`), so the CLI
  and internal callers still get full records.
- **Bounded dreams (H5).** Each dream reads at most about 200 event records, 100 memory records and 10 topical
  older lookups; these are ceilings, not targets. Read them in pages with `cursor=next_cursor`; the byte
  budget (F2, above) sizes each page, so a generous `limit` is fine. Output that comes back spilled to a file,
  truncated or shown only in part is **not read**: seeing only a saved-file path, a preview or prefix
  (including a `body_truncated` search item), or a generated summary does not count. Fetch each cut item with
  `event_get` (in `body_offset` chunks when `body_len` is over ~30 000), keeping its ID; until a range is read
  in full, record it as an omission with `from` = its first unread item's `created_at` (a page's
  `truncated_from`), and don't advance `covered_through` past it. If a result still spills (the ceiling may be
  lower on another host), pass a smaller `max_bytes` and re-read the same range. Summarizing *after* the
  complete originals have been read is fine. It looks at current goals and the newest consequential feedback
  and outcomes first, and spends what is left on chronological backfill from `covered_through`. Coverage is
  contiguous: reading newer material first never moves `covered_through` past an older unread gap, so it
  advances only to the end of the unbroken run read from the previous checkpoint, or stays put, with the gap
  recorded as an omission. A current-day lesson may still be kept. A home with a null or old checkpoint
  catches up over several dreams. Nothing claims lifetime coverage and no raw history is deleted. The bound is
  guidance only: no new cursor, checkpoint field or service, and the runtime guards (E1 validation in
  `routine_rearm`, the next self-rearm) are unchanged.
- **Checkpoint.** The dream passes `covered_through` (≤ `evidence_cutoff`, ≤ every omission's `from`) and its
  omissions to `routine_rearm`, as in "Dream checkpoint" above. The inclusive `since` means a T equal to an
  omission's `from` re-reads the items at that timestamp next time and skips nothing.
- **working_context (D3).** One memory, predicate `working_context`, subject the employee's own identity, kind
  `belief` or `procedure` (a derived reflection, never `fact`, evidence or authority; H3), at most 4000
  characters. `memory_put` accepts it only inside a session whose `routines_due` lists the dream, and, while
  one is active, only with `replaces=<that id>`; the CLI (no run) is refused. The active record can't be
  superseded any other way: a `replaces=<its id>` with another predicate is refused in every session (H1), and
  a working_context can't replace an ordinary memory. Every snapshot carries it as
  `working_context {id, kind, value, created_at, source, replaces}` (null when none), outside `memories`,
  `memory_index` and `memory_context_truncated`, so it survives a worker restart and any number of newer
  memories. The full record also stays retrievable with `memory_search`.
- **Authority is not derived.** The dream and team-review guidance both keep original authorizations,
  recommendations, unanswered decisions and self-derived hypotheses apart. A memory or working_context never
  answers a decision or grants authority; a required decision counts as answered only with the original answer
  or a valid delegation, otherwise the wait stays. Existing standing permissions stay valid without new gates.
- **Seeding.** `routine seed --kinds dream [--dream-at HH:MM]` (default 01:30Z, just after the daily-limit
  reset); `--first dream=+MIN` for a test home.

## Test map (E1-E3, D3; P2 addendum)

| Rule | Tests |
|---|---|
| E1 evidence cutoff, omissions bound covered_through | `test_rtn_p1_routines.DreamCheckpoint`, `test_rtn_p1r1.F3UnknownOmission`, `test_rtn_p2_dream.Since` (capped page through the dream path) |
| E2 manager resolved from configuration | `test_rtn_p1_routines.ManagerRoute`, `test_rtn_p1r1.F4OfficialRoster`, `test_rtn_p2_dream.QuarantineNotice` (no manager: nothing sent) |
| E3 session fencing, audited refusals | `test_rtn_p1_routines.SessionFencing` (incl. the `authority.refused` rows) |
| D3 working_context consumed by a later session | `test_rtn_p2_dream.WorkingContext` (new Store, 61 newer memories, a real Engine session) |
| N3 one quarantine notice | `test_rtn_p2_dream.QuarantineNotice` (finish, fail + restart, interrupt + second failure, repair + re-quarantine) |
| n3 restore consumes the held input | `test_rtn_p2_dream.RestoreConsumesHeldInput` |
| H1 the reserved record is replaced only by the dream | `test_rtn_p2_dream.H1ReservedReplace` |
| H2 since= paging with tied `created_at` | `test_rtn_p2_dream.H2SinceTies` |
| H3 working_context kind and provenance | `test_rtn_p2_dream.H3WorkingContextKind` |
| H4 brief owner-level quarantine notice | `test_rtn_p2_dream.H4BriefNotice` |
| H5 bounded dream, current first, contiguous checkpoint | prose only (`## dream` and "Bounded dreams" above); read review, no wording test; runtime unchanged |
| Dream page size; spilled/summarized output is not read (P3 finding) | prose only (`## dream` step 1 and "Bounded dreams"); read review, no wording test |
| F2 results fit tool output: byte budget, body previews, cut first item, `event_get` chunks, store defaults unchanged | `test_f2_compact_search` (QueryPreview, Budget incl. C1 query mode and C3 cursor past a cut item, Chunks incl. C2 plain `event_get`, StoreDefaultsUnchanged C4) |
| Fresh-home init needs `--human` (README, Provisioning step 1) | existing `tests/test_humans_cli.py` (`test_no_file_and_no_flag_fails_clearly`, `test_init_human_writes_config_and_is_default_everywhere`) |
