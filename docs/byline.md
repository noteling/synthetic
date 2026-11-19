# Byline: attribution on outbound artifacts (#15 slice A)

Byline lets the owner state, per employee home, where that employee's work is marked as its own. It adds
provenance metadata only. Commit author and committer, git `user.name`/`user.email`, copyright notices and
LICENSE files are never touched: AI contributions stay under David (event-46e007a5, a CLA/licensing answer;
Codex ruling event-29e08672).

## Off by default

Without `<home>/attribution.json`, nothing changes: the system prompt, Telegram send parameters, splitting
and receipts are byte-identical to before Byline (`tests/test_i15_byline.py` NoConfigGuards). Noteling and
Stand stay unconfigured; extending attribution there is David's call (KT and direction alignment).

## The file (owner-only)

```json
{"v": 1,
 "line": "— Vesper (synthetic employee)",
 "trailer": "Synthetic-Employee",
 "scopes": {"repos": ["github.com/noteling/synthetic"], "channels": []}}
```

- `line` (optional, one line, at most 200 characters) defaults to `— <name> (synthetic employee)`.
- `trailer` (optional) is the git trailer token; the trailer line is `<trailer>: <name> (<employee-id>)`.
- `scopes.repos` are `host/owner/repo` names. Remote URLs are normalized (scheme, user, port and `.git`
  dropped), so `git@github.com:noteling/synthetic.git` matches `github.com/noteling/synthetic`. Only the
  host is lower-cased: `owner/repo` matching is case-sensitive (GitHub's isn't), so write scopes in the case
  the remote uses. An scp-style remote whose path starts with digits and a slash (`git@host:2020/repo`) is
  read as a port, as in `ssh://host:2020/repo`.
- `scopes.channels` may name only adapters that can add a footer: today `telegram`. Default: none (Telegram
  DMs to David stay without a footer, Codex Q4).
- Unknown keys are refused, so a typo can't silently do nothing.

`synthetics byline set --file F` validates and writes it atomically (a fresh `mkstemp` file, mode 0600,
renamed into place). `byline clear` removes it. Both are owner-only. A session is refused (`owner_only`) and
the refusal is audited. The owner's set is audited as `byline.set` with the sha8 of the written file, and
clear as `byline.cleared`. `byline show` reports
the resolved config, or `valid: false` with the error. An invalid file adds nothing to the prompt and no
footer.

Honest limit: like `runtime.json`, the file is only fenced at the CLI. A session running as the same OS user
could write it with the shell. The prompt doesn't tell it to. `byline show` shows the current file, and a
file whose sha8 matches no `byline.set` audit row wasn't written through the CLI.

## What a configured home gets

1. **Prompt.** `brain.system_prompt` appends one block after the host rules. It names the in-scope repos and
   channels, the trailer line, and the signature line for PR, issue and review text drafted there. It also
   says never to change author, committer, user.name/email, copyright or LICENSE.
2. **Handoff trailer.** `synthetics byline trailer [--repo PATH]` prints the trailer line. It prints nothing
   when the home is unconfigured or, with `--repo`, when no remote of that repo is in scope. Under the PR
   workflow the manager builds commits from an employee's frozen tree. The employee lists this line in the
   handoff and the manager's commit carries it. The manager's own merge commits carry no trailer. No hook
   runs in the manager's checkout, since it would fire under the manager's session and attribute the work
   wrongly (Codex Q2).
3. **Commit hook.** In a repo where an employee commits itself, `synthetics byline hook install --repo PATH`
   writes the repo's `prepare-commit-msg` hook (in the directory `git rev-parse --git-path hooks` names). It
   may be run by the owner, or by a session for its own home when both the repo and the resolved hook file
   are inside that home: a linked worktree of an outside repo, `core.hooksPath` or a symlink pointing
   outside is refused (`own_home_only`, audited, nothing written). It refuses to replace a hook Byline did
   not write. The hook adds the trailer only when all of these hold:
   - `SYNTHETICS_SESSION_RUN` is set and `SYNTHETICS_HOME` is the installing home;
   - the commit isn't a merge or squash, doesn't reuse an existing message (`-c`, `-C`, `--amend
     --no-edit`: git passes `commit`), and isn't a cherry-pick (`CHERRY_PICK_HEAD` exists): those carry
     work the session didn't write. A rebase runs no `prepare-commit-msg`. `--amend -m` looks like a new
     commit to the hook and is trailed; a revert is the session's own and is trailed;
   - a remote of the repo is in the home's current `scopes.repos`; scope is read at commit time, so removing
     the file stops it.

   It uses `git interpret-trailers --if-exists addIfDifferent`, so the trailer is added once. The trailer
   text and the Python path are fixed at install time. Reinstall after renaming the trailer. If that Python
   is gone (an upgrade removed it) the hook does nothing rather than aborting the commit.
4. **Footer.** An in-scope adapter appends `\n\n<line>` to the last part it sends. The body is split with
   room for the footer, so every Telegram part stays within 4096 characters. The stored outbox body is
   unchanged. The delivery record gets `footer: <sha8>` and the receipt becomes
   `telegram:<chat>:<ids>;footer:<sha8>` (sha8 = first 8 hex of sha256 of the footer), so what was sent is
   provable.

## Not in slice A

- A pre-push tripwire (dropped entirely: it is a new gate and contradicts the session push flow, Codex Q3).
- Per-employee git identities or emails.
- History rewriting.
- Signing GitHub API comments.
- A footer for Postern, which writes machine records, not human text.
