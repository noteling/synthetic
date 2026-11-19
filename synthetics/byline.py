"""Byline (#15 slice A): opt-in, owner-set attribution on outbound artifacts.

One file per home, ``<home>/attribution.json``, written by the owner (``synthetics byline set``; a session is
refused, owner_only). Without it every path here is a no-op and the prompt and delivery are byte-identical to
before Byline. With it:

- the session prompt gains one block naming the attribution line, the commit trailer and the in-scope repos
  and channels (``prompt_block``);
- ``synthetics byline trailer`` prints ``Synthetic-Employee: <name> (<employee-id>)`` for a handoff, so whoever
  builds the commit from a frozen tree can carry it (the push flow, not a hook in the builder's checkout);
- ``synthetics byline hook install --repo PATH`` writes a ``prepare-commit-msg`` hook into a repo where the
  employee commits itself; it adds the trailer only inside a session of this home, only for an in-scope
  remote, never on merges, and never touches author, committer, user.name/email, copyright or LICENSE;
- an adapter whose channel is in scope appends the line as a footer to what it sends (the stored outbox
  body stays unchanged) and records ``footer:<sha8>`` in its receipt (``footer_for``).

Commit identity, copyright and licensing stay as they are (under David, event-46e007a5 via Codex
event-29e08672): Byline adds provenance metadata only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

FILE = "attribution.json"
VERSION = 1
TRAILER = "Synthetic-Employee"
CHANNELS = ("telegram",)          # channels whose adapter knows how to add a footer
MAX_LINE = 200                    # footer/line length cap (Telegram parts stay within 4096 with room)
HOOK_MARK = "# synthetics-byline-hook v1"
KEYS = ("v", "line", "trailer", "scopes")


class BylineError(ValueError):
    pass


def path(home) -> Path:
    return Path(home) / FILE


def validate(data) -> dict:
    """The normalized config or BylineError. Unknown keys are refused so a typo cannot silently do nothing."""
    if not isinstance(data, dict):
        raise BylineError("attribution.json must be a JSON object")
    extra = sorted(set(data) - set(KEYS))
    if extra:
        raise BylineError("unknown keys: %s (allowed: %s)" % (", ".join(extra), ", ".join(KEYS)))
    if data.get("v") != VERSION:
        raise BylineError("v must be %d" % VERSION)
    line = data.get("line")
    if line is not None and (not isinstance(line, str) or not line.strip() or "\n" in line
                             or len(line) > MAX_LINE):
        raise BylineError("line must be one non-empty line of at most %d characters" % MAX_LINE)
    trailer = data.get("trailer", TRAILER)
    if not isinstance(trailer, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,40}", trailer):
        raise BylineError("trailer must be a git trailer token (letters, digits, dash)")
    scopes = data.get("scopes") or {}
    if not isinstance(scopes, dict) or set(scopes) - {"repos", "channels"}:
        raise BylineError("scopes must be an object with only repos and channels")
    repos, channels = scopes.get("repos") or [], scopes.get("channels") or []
    if not isinstance(repos, list) or not all(isinstance(r, str) and r.strip() for r in repos):
        raise BylineError("scopes.repos must be a list of repository names like github.com/owner/repo")
    if not isinstance(channels, list) or not all(c in CHANNELS for c in channels):
        raise BylineError("scopes.channels may name only: %s" % ", ".join(CHANNELS))
    return {"v": VERSION, "line": line.strip() if line else None, "trailer": trailer,
            "scopes": {"repos": sorted({normalize_remote(r) for r in repos}), "channels": sorted(set(channels))}}


def load(home):
    """The validated config, or None when the home has no attribution.json. Raises BylineError when invalid."""
    p = path(home)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BylineError("%s unreadable: %s" % (p, exc))
    return validate(data)


def identity(home):
    """(name, employee id) from the home's identity row, read-only (never opens a Store)."""
    from .sqlro import connect_ro
    conn = connect_ro(Path(home) / "state.sqlite3")
    try:
        row = conn.execute("SELECT data FROM identity LIMIT 1").fetchone()
    finally:
        conn.close()
    if row is None:
        raise BylineError("%s has no identity" % home)
    ident = json.loads(row[0])
    return ident["name"], ident["id"]


def resolved(home):
    """None when unconfigured, else the config plus name, id, line and trailer text."""
    cfg = load(home)
    if cfg is None:
        return None
    name, eid = identity(home)
    cfg = dict(cfg, name=name, id=eid)
    cfg["line"] = cfg["line"] or "— %s (synthetic employee)" % name
    cfg["trailer_line"] = "%s: %s (%s)" % (cfg["trailer"], name, eid)
    return cfg


def normalize_remote(url: str) -> str:
    """git remote URL or scope name → host/owner/repo (lower-case host, no scheme, user, port or .git)."""
    # Keep in step with norm() inside HOOK (test_i15_byline.NormParity runs both on the same URL table).
    u = re.sub(r"^[a-z][a-z0-9+.-]*://", "", url.strip())
    u = re.sub(r"^[^@/]+@", "", u)
    if ":" in u.split("/", 1)[0]:
        host, rest = u.split(":", 1)
        rest = rest.split("/", 1)[1] if re.match(r"^\d+/", rest) else rest.lstrip("/")
        u = host + "/" + rest
    u = re.sub(r"\.git/?$", "", u).rstrip("/")
    head, _, tail = u.partition("/")
    return head.lower() + ("/" + tail if tail else "")


def prompt_block(home) -> str:
    """The prompt addition, or "" when unconfigured or invalid (an invalid file is reported by `byline show`)."""
    try:
        cfg = resolved(home)
    except Exception:
        return ""
    if cfg is None:
        return ""
    repos = ", ".join(cfg["scopes"]["repos"]) or "none"
    channels = ", ".join(cfg["scopes"]["channels"]) or "none"
    return ("\n\nAttribution (Byline; owner-set %s in your home):\n"
            "- In-scope repositories: %s. For commits you make there, end the message with the trailer\n"
            "  `%s`; in pull request, issue and review text you draft there, sign with: %s\n"
            "  When someone else builds the commit from your frozen tree, list the trailer in the handoff\n"
            "  (`synthetics byline trailer`).\n"
            "- In-scope channels (the adapter adds the line as a footer itself; do not add it): %s.\n"
            "- Attribution is provenance only: never change the commit author or committer, git user.name or\n"
            "  user.email, copyright notices or LICENSE files. Outside these scopes follow the owner's rules.\n"
            % (FILE, repos, cfg["trailer_line"], cfg["line"], channels))


def footer_for(home, channel):
    """The footer text for ``channel``, or None (unconfigured, invalid, or channel not in scope)."""
    try:
        cfg = resolved(home)
    except Exception:
        return None
    if cfg is None or channel not in cfg["scopes"]["channels"]:
        return None
    return cfg["line"]


def footer_sha8(footer: str) -> str:
    return hashlib.sha256(footer.encode("utf-8")).hexdigest()[:8]


def with_footer_receipt(receipt, footer_sha):
    """Append ``;footer:<sha8>`` to a receipt when a footer was sent; unchanged otherwise."""
    if not footer_sha or receipt is None:
        return receipt
    return "%s;footer:%s" % (receipt, footer_sha)


# --- owner and CLI operations -------------------------------------------------------------------------------

def set_config(home, data) -> dict:
    """Owner-only (the CLI calls authority.owner_only first). Validates, writes 0600 atomically."""
    cfg = validate(data)
    p = path(home)
    text = json.dumps(dict(data), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    _atomic_write(p, text, 0o600)
    return {"written": str(p), "config": cfg, "sha8": footer_sha8(text)}


def _atomic_write(target: Path, text: str, mode: int) -> None:
    """A fresh mkstemp file (0600 from creation) in the target's directory, then chmod and rename into place.
    A fixed temp name could be pre-planted (or be a symlink); mkstemp uses O_EXCL with a random name."""
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix="." + target.name + ".", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, str(target))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def clear_config(home) -> dict:
    p = path(home)
    existed = p.exists()
    if existed:
        p.unlink()
    return {"removed": str(p) if existed else None}


def show(home) -> dict:
    try:
        cfg = resolved(home)
    except BylineError as exc:
        return {"configured": True, "valid": False, "error": str(exc), "file": str(path(home))}
    if cfg is None:
        return {"configured": False, "file": str(path(home))}
    return {"configured": True, "valid": True, "file": str(path(home)), "line": cfg["line"],
            "trailer": cfg["trailer_line"], "scopes": cfg["scopes"]}


def repo_remotes(repo) -> list:
    out = subprocess.run(["git", "-C", str(repo), "remote", "-v"], capture_output=True, text=True)
    if out.returncode != 0:
        return []
    return sorted({normalize_remote(line.split()[1]) for line in out.stdout.splitlines() if len(line.split()) >= 2})


def trailer(home, repo=None) -> str:
    """The trailer line, or "" when unconfigured or (with ``repo``) when no remote of the repo is in scope."""
    cfg = resolved(home)
    if cfg is None:
        return ""
    if repo is not None and not set(repo_remotes(repo)) & set(cfg["scopes"]["repos"]):
        return ""
    return cfg["trailer_line"]


HOOK = r'''#!/bin/sh
%(mark)s
# Installed by `synthetics byline hook install` for home %(home_q)s.
# Adds the Byline trailer to commits made inside a session of that home, for an in-scope remote only.
# Never edits author, committer, user.name/email; skips merges, squashes, -c/-C/--amend reuse of an existing
# message, and cherry-picks (work this session didn't write). Remove this file to stop it.
[ -n "$SYNTHETICS_SESSION_RUN" ] || exit 0
[ "$SYNTHETICS_HOME" = %(home_q)s ] || exit 0
case "$2" in merge|squash|commit) exit 0 ;; esac
gd=$(git rev-parse --git-dir 2>/dev/null) || exit 0
[ -e "$gd/CHERRY_PICK_HEAD" ] && exit 0
[ -x %(python_q)s ] || exit 0
exec %(python_q)s -I -c '
import json, os, re, subprocess, sys
home, msg = sys.argv[1], sys.argv[2]
try:
    cfg = json.load(open(os.path.join(home, "attribution.json"), encoding="utf-8"))
except (OSError, ValueError):
    sys.exit(0)
def norm(u):
    u = re.sub(r"^[a-z][a-z0-9+.-]*://", "", u.strip()); u = re.sub(r"^[^@/]+@", "", u)
    if ":" in u.split("/", 1)[0]:
        host, rest = u.split(":", 1)
        rest = rest.split("/", 1)[1] if re.match(r"^\d+/", rest) else rest.lstrip("/")
        u = host + "/" + rest
    u = re.sub(r"\.git/?$", "", u).rstrip("/"); a, _, b = u.partition("/")
    return a.lower() + ("/" + b if b else "")
scopes = {norm(r) for r in (cfg.get("scopes") or {}).get("repos") or []}
out = subprocess.run(["git", "remote", "-v"], capture_output=True, text=True).stdout
remotes = {norm(l.split()[1]) for l in out.splitlines() if len(l.split()) >= 2}
if not scopes & remotes:
    sys.exit(0)
trailer = sys.argv[3]
subprocess.run(["git", "interpret-trailers", "--in-place", "--if-exists", "addIfDifferent",
                "--trailer", trailer, msg], check=False)
' %(home_q)s "$1" %(trailer_q)s
'''


def _shq(s: str) -> str:
    return "'" + str(s).replace("'", "'\"'\"'") + "'"


class HookOutside(BylineError):
    """The hook file git would run for ``repo`` resolves outside the allowed directory (#70 F1)."""

    def __init__(self, target, within):
        super().__init__("the hook for this repo resolves to %s, outside %s" % (target, within))
        self.target, self.within = target, within


def hook_target(repo) -> Path:
    """The fully resolved file git runs as prepare-commit-msg for ``repo`` (hooks dir per --git-path hooks, so a
    linked worktree's common dir and core.hooksPath count; symlinks resolved). Nothing is created."""
    top = subprocess.run(["git", "-C", str(repo), "rev-parse", "--git-path", "hooks"],
                         capture_output=True, text=True)
    if top.returncode != 0:
        raise BylineError("%s is not a git repository" % repo)
    hooks = Path(top.stdout.strip())
    if not hooks.is_absolute():
        hooks = Path(repo) / hooks
    return (hooks.resolve() / "prepare-commit-msg").resolve()


def hook_install(home, repo, python=None, within=None) -> dict:
    """Write the repo's prepare-commit-msg hook. Refuses to replace a hook Byline did not write. With ``within``
    (a session installing), the resolved hook file must lie inside that directory (HookOutside, nothing
    written)."""
    import sys
    cfg = resolved(home)
    if cfg is None:
        raise BylineError("no %s in %s: the owner sets it first (synthetics byline set)" % (FILE, home))
    target = hook_target(repo)
    if within is not None:
        root = Path(within).resolve()
        if root not in target.parents:
            raise HookOutside(target, root)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and HOOK_MARK not in target.read_text(errors="replace"):
        raise BylineError("%s exists and was not written by Byline; refusing to replace it" % target)
    text = HOOK % {"mark": HOOK_MARK, "home_q": _shq(str(Path(home).resolve())),
                   "python_q": _shq(python or sys.executable), "trailer_q": _shq(cfg["trailer_line"])}
    _atomic_write(target, text, 0o755)
    in_scope = sorted(set(repo_remotes(repo)) & set(cfg["scopes"]["repos"]))
    return {"installed": str(target), "trailer": cfg["trailer_line"], "in_scope_remotes": in_scope}
