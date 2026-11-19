# Role profiles

A role profile is a short, versioned prompt file of operating defaults for one role, shipped with the
package in `synthetics/prompts/roles/`. Today: `manager` and `developer`. The owner picks one per home; a
new session of that home starts with it. Nothing picks a profile by default.

## Guidance only

A profile grants no authority, widens no access and never overrides host rules or an authenticated owner
instruction. It sits right after the shipped employee prompt and before the rules that take precedence:

    employee.md → role profile → company host.rules → <home>/prompt.local.md → Byline block

Each profile file states this in its own header comment.

## Choosing a profile (owner)

    synthetics --home H init --name N --role R --human OWNER --profile developer   # new home
    synthetics --home H profile set manager                                          # existing home
    synthetics --home H profile clear                                                # no role defaults
    synthetics --home H profile show                                                 # read-only

`set` and `clear` are owner-only: a session can neither choose nor change its own defaults (refused with
`owner_only` and audited as `authority.refused`). Both write audit rows (`profile.set`, `profile.cleared`)
with the previous and new `{name, version, sha256}`. The choice is stored as `<home>/profile.json` =
`{"v": 1, "profile": "<name>"}` (0600). A running worker picks up a change at its next session; restart
the worker if you want it applied before any queued session.

Existing homes keep running without a profile until the owner sets one.

## Fail closed

If `profile.json` exists but is unreadable, invalid, or names a profile this package does not ship, the
session refuses to start (the same rule as unreadable host rules), and `doctor` reports `profile_invalid`.
Fix it with `profile set NAME` or `profile clear`.

## Seeing which defaults a session ran under

- `system-prompt` shows the block headed `Role defaults (profile developer v1, sha256 <12 hex>; …)`.
- `doctor` reports `profile: {name, version, sha256}`.
- Every run record (and its `run.started` audit row) carries `profile: {name, version, sha256}` from the
  moment the session starts; a home without a profile has no `profile` field. The sha256 is of the
  profile file's bytes, so a run can be tied to the exact text it ran under after the file changes.

## Changing a profile

Profiles change only through a reviewed PR, never from a session, routine or dream:

1. An employee restates a practice it learned as a generic project lesson with shareable evidence.
2. The manager accepts it, and decides on a promotion proposal (Board post) by three tests: it holds for
   the role on any project; it carries no installation values (paths, ports, numbers, names); it was seen
   in two projects or by two employees, or the manager records why one incident is enough.
3. A developer edits `prompts/roles/<role>.md` (or `employee.md` for every role), bumps `version`, and adds
   one line to `prompts/roles/CHANGES.md` with no record IDs. The PR body names the lessons it carries.
   A non-author reviews it.
4. After the merge the manager retires each lesson with reason `promoted: <role> vN at <merge sha>`.

## Portability rules (tested)

`tests/test_profiles.py` fails the suite when `employee.md` or any file in `prompts/roles/` contains an
absolute path, a record ID or a loopback port, or when a profile is over 4096 bytes or lacks its header.
Names of a particular installation's people are not committed anywhere: to check them too, point
`SYNTHETICS_PORTABILITY_NAMES` at a local file with one name per line (the test is skipped without it).
