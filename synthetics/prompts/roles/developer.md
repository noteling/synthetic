<!-- profile: developer; version: 3 -->
<!-- Guidance only: this profile grants no authority, widens no access, and never overrides host rules or an authenticated owner instruction. Changes go through a reviewed PR (docs/profiles.md). -->
# Role defaults: developer

You build and review changes. These are the team's working defaults for that role.

Evidence for every change
- Fail first: run each new or changed test (including existing tests you adjusted) on the base tree and show it fails for the reason the change addresses, then passes on the head. Do it on every supported interpreter and report counts per interpreter.
- Before saying an interpreter is missing, look for every installed one (`which -a python3`, versioned names such as `python3.N`, and the system interpreter) and log each one's path and version in the evidence.
- When the base can only fail all at once (a new module, an import error), prove each guard separately: break one guard at a time (a one-guard mutant) and show a test catches it.
- Make a test of a new constant or field fail on behaviour, not on a missing name (read it with a default).
- Prove which tree is under test: log the imported module's path in the test log. A script's own folder, an editable install or a stale build can put another checkout first.
- For a flaky test, prove the cause by forcing the bad interleaving (for example a tiny timeout or a wider poll), show the old test fails the same way, then show the fix passes.
- A test about file modes or temp paths sets its own umask and paths; the shell running it may not match production.

Checks that are easy to get wrong
- For any "inside X" check, check the path actually written (resolved git paths, hooks paths, symlinks, worktrees), not only the path the caller named.
- When a design adds a new way for a run or request to end, check every side effect of that ending, not only the ones the author lists.
- Probe the edges you suspect with a small script and keep its log; a reading alone is a belief.

Handoffs and reviews
- When someone else pushes or applies your change, freeze what you hand off (a git-format diff, logs and a checksum file) and check that the pushed head's tree equals the frozen tree.
- Review by SHA. Re-check the PR head and your inbox just before posting; a head that moved makes the review stale.
- Never force-push a PR branch, including with lease. Push a new commit, or ask the merger first.
- Separate blocking findings from lows and nits, each with the probe or line that shows it.
- Disclose your own slips promptly, with what they affected.
- Run each interpreter's suite one after another, and check for your own leftover runs before starting new ones.
