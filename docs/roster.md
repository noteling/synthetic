# Roster: self, company profiles and private views (M26)

Every employee keeps three separate layers of identity knowledge:

| Layer | Who writes | Authority | Stored in | Rendered as |
|---|---|---|---|---|
| Self | the employee (`self_note`) | the employee's own sourced notes | `<home>/state.sqlite3` table `roster_log` | `<home>/roster/self.md` |
| Company profile | operator (CLI) or a holder of the company write grant (`profile_put`) | official | `<root>/company/memory.sqlite3` (append-only log, like a project) | `<root>/company/people/<id>.md` |
| Private view | the employee (`view_put`) | one employee's opinion, never official | `<home>/state.sqlite3` table `roster_log` | `<home>/roster/views/<id>.md` |

`<root>` is the directory that holds the homes. `company` is a reserved name: it is never a project and
never appears in project lists, `project_uncovered` or project snapshot sections.

The logs are the authority. The Markdown files are generated; `.rendered.json` records each file's
sha256, and a hand edit is reported by `doctor` (`roster_view_drift`) and overwritten by the next render.

## Rules

- Every claim needs a source. Profile entries record the `auth` of the cited event; it is never taken
  from an argument and never upgraded. A role change needs an authenticated source or the operator CLI;
  an unauthenticated source may support a description edit only. Raising certainty needs a new source.
- Nobody changes their own company role. Corrections go to the manager as a question.
- A view is stale when the person's `role_rev` is newer than the view's basis. The snapshot lists it in
  `views_stale`; a `view_put(..., reconciles=<rev>)` clears it. Staleness never wakes a session.
- Names and aliases resolve to one canonical company ID. `memory_put` stores the canonical ID (the given
  name stays in the audit row); `memory_search(subject=…)` expands to all aliases.
- Every view file starts with `PRIVATE VIEW — not official (private to <name>)`. Employees say
  "in my view…" and never paste view text into messages or project memory.
- When a memory and a view disagree, the memory wins: memories are the evidence, a view is an
  interpretation that cites them, and a claim whose cited memory was superseded renders
  "basis superseded → <id>" (A2).
- A profile entry by `operator:*` whose source is an identity row renders as "operator seed (identity
  row)" instead of "unauthenticated source"; its recorded `auth` stays null and nothing is upgraded.

## What does not leave the private layer

- `project_promote` refuses any file inside a home's `roster/` directory (`private_roster`).
- MCP receipts (`<home>/runs/*.mcp.jsonl`) record `view_put` and `self_note` with every `text`, `note`
  and `reason` replaced by `<redacted N bytes>`.
- Audit rows for roster writes carry only the entry ID, seq, person and section.
- No other employee's context, the company log, people files or project logs carry view text.
- A document collector must never watch a home or a `roster/` directory (`doctor --collector-plist`).

The host account owner is not excluded: `synthetics roster show` (operator only, refused in a session)
prints a home's self file and views, and the files are readable by the host user (0600 in 0700
directories). Private means private between employees, not hidden from the operator.

## Tools (employee sessions)

- `roster_get(person)` — official profile, its rendered text and your own view; accepts IDs, names, aliases.
  `via` is how the text resolved (`id`, `name` or `alias`; alias wins over name when both name the same
  person) and `matched_as` lists every way it names that person.
  `profile.history` lists the provenance (entry, seq, by, at, source, certainty, auth) of every active
  profile entry; alias, unalias and retire entries add `change` plus the `alias` text or retire `reason`.
- `view_put(person, claims=[{cid, text, kind, source, certainty, ref?}], retract=[cid], reconciles=rev, key)`
- `self_note(section, text, source, certainty, key)`
- `profile_put(...)` — company write grant required; fenced on the session run.

## CLI

```sh
synthetics --home H roster company put <id> --name N --kind employee|human --role R --by operator --source S
synthetics --home H roster company alias|unalias|retire … --by … --source …
synthetics --home H roster company grant|revoke|import|redact …   # operator only
synthetics --home H roster company grants|state|export
synthetics --home H roster render              # self/views for H and company people files
synthetics --home H roster show                # operator only
synthetics --home H roster seed --dry-run      # proposals from sourced memories (counts per section)
synthetics --home H roster seed --apply        # operator only; idempotent
```

Seeding (§9, C7): each proposal is the memory's predicate plus gist, cites the memory ID, is authored
`roster-seed`, and is never strengthened (belief → inferred; otherwise observed). Memories without a
source are skipped and counted. Memories about other people become claims in the seeding employee's
view of them. Self memories are never seeded (counted as `skipped_self`): they already appear once in
the snapshot, in the `self` index or in `memory_index`, and self notes come only from `self_note`.

## Doctor

`synthetics --home H doctor` adds a `roster` summary and these findings:

| Code | Level | Meaning |
|---|---|---|
| `company_log_missing` | info | no company log yet |
| `company_log_unreadable` | error | the company log can't be read or folded |
| `human_without_profile` | warning | a humans.json sender has no company profile |
| `alias_collision` | error | an alias also names another person |
| `view_unresolvable` | warning | a private view names someone with no profile |
| `view_stale` | info | views older than the person's role |
| `roster_view_drift` / `roster_file_missing` | warning | rendered file hand-edited or missing |
| `collector_covers_home` | error | `--collector-plist`/`--collector-dir` source dir contains a home or is inside `roster/` |
