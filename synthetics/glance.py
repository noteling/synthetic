"""RW-1 "Glance": an employee looks at the human's front browser page through Noteling (read-only).

`screen_glance {find?, role?, app?}` asks the installed Noteling for `--read-page --json` and, with `find`, for
`--find NAME [--role R]`. Noteling answers with one JSON object (HeadlessRead.Report in noteling
Sources/Familiar/App/Commands/HeadlessRead.swift): {ok, exit, error?, page?: Counts, find?: Found}. Exit codes:
0 read/found, 1 not found, 2 usage, 3 no browser window, 4 Accessibility not granted, 5 partial.

Privacy (v1, intent pm-c12016e1 and Noteling's counts-only default for agents):
- `--full` and `--contains` are never passed, so page text never leaves Noteling.
- The employee's tool result carries Noteling's counts and its find hits (role, kind, label, visible, enabled, frame).
  A hit's label can carry page words (an "alike" hit names the control as this page does), so labels and frames
  go to the tool result only.
- The stored record (Store table ``glances``, the audit log, status.json and Lookout) keeps only whitelisted fields:
  counts (numbers and booleans; ``kinds`` as role-kind counts), the name and role the employee asked for, the match
  kind, count, unique, and visible/enabled per hit, and ``site`` cut to the host (no port, path, query or fragment;
  D1 approved by Codex 2026-11-19, message-ed00558e).
- Opt-in (C1): the tool is offered only in a home whose ``glance.json`` says {"enabled": true}. It reads the human's
  screen, so no employee gets it implicitly.
- The tool RESULT (with hit labels and frames) persists only in the calling session's own transcript; the home's
  runs/*.mcp.jsonl receipt keeps the arguments (find, role, app), not the result. See docs/glance.md.

The reader is a seam: ``glance.reader`` (default LaunchReader) is any callable(args: list[str]) -> dict that
returns Noteling's report or raises GlanceError. Tests replace it with a fake.
"""
from __future__ import annotations

import fcntl
import json
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
import uuid
from pathlib import Path

DEFAULT_APP = "/Applications/Noteling.app"
MIN_VERSION = (0, 10, 5)
TIMEOUT = 15.0
RECENT = 5
OPT_IN = "glance.json"
TOO_OLD = "Noteling too old (need ≥ 0.10.5)."
NO_ANSWER = "Noteling exited without answering (it crashed or was stopped). A later glance may work."
# Host-wide for this user: every home on this Mac runs as the same user, so all glances take turns (review F2).
# The lock lives in the user's own Darwin temp folder, not world-writable /tmp, so another local user can't hold
# or plant it (review L1). None: found on first use with user_temp_dir(); tests set a path.
LOCK = None
LOCK_NAME = "synthetics-glance.lock"
# Unicode control (incl. DEL), format, line- and paragraph-separator categories: never part of a control's
# name as a person says it (review L3).
UNPRINTABLE = {"Cc", "Cf", "Zl", "Zp"}
ROLE = re.compile(r"[A-Za-z]{1,40}\Z")
BUNDLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*(\.[A-Za-z0-9][A-Za-z0-9-]*)+\Z")

PAGE_FIELDS = ("app", "bundle", "site", "documents", "tabs", "sheetOpen", "kinds", "withPageID", "elements",
               "onScreen", "loading", "truncated", "ms")
COUNT_FIELDS = ("documents", "tabs", "sheetOpen", "withPageID", "elements", "onScreen", "loading", "truncated", "ms")
EXITS = {0: "ok", 1: "not_found", 2: "usage", 3: "no_browser", 4: "no_permission", 5: "partial"}
SENTENCES = {
    1: "The page was read, but no control by that name is on it.",
    2: "Noteling rejected the request as a usage mistake.",
    3: "There is no browser window with a page in front, so there is nothing to read. Don't retry until the "
       "human brings a browser page forward.",
    4: "Noteling has no Accessibility permission. Don't retry: tell your manager, who can ask the human to grant it.",
    5: "The page was only partly read (still loading, or too large to walk). A later glance may see more.",
}


class GlanceError(Exception):
    """A glance that could not reach Noteling; ``reason`` is a stable code, ``str()`` a sentence for the employee."""

    def __init__(self, reason, sentence):
        super().__init__(sentence)
        self.reason = reason


def app_version(app):
    """(major, minor, patch) from the app's Info.plist, or None when it is missing or unreadable."""
    try:
        with open(os.path.join(app, "Contents", "Info.plist"), "rb") as handle:
            text = plistlib.load(handle).get("CFBundleShortVersionString", "")
        parts = tuple(int(p) for p in str(text).split(".")[:3])
        return parts + (0,) * (3 - len(parts))
    except (OSError, ValueError, plistlib.InvalidFileException, AttributeError):
        return None


def enabled(home):
    """True only when <home>/glance.json is a JSON object with "enabled": true (C1: off by default)."""
    try:
        with open(os.path.join(str(home), OPT_IN), encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return False
    return isinstance(value, dict) and value.get("enabled") is True


def host(site):
    """The host of a site or address: no scheme, user, port, path, query or fragment."""
    if not isinstance(site, str):
        return None
    text = site.strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    for mark in "/?#":
        text = text.split(mark, 1)[0]
    text = text.rsplit("@", 1)[-1]
    if text.startswith("["):
        text = text.split("]", 1)[0] + "]"
    else:
        text = text.split(":", 1)[0]
    text = text.lower()[:253]
    return text or None


def user_temp_dir():
    """This user's own temp folder, the same in every home and worker whatever its environment.

    `getconf DARWIN_USER_TEMP_DIR` asks the system, so it doesn't depend on $TMPDIR: a worker started without
    $TMPDIR would otherwise fall back to /tmp and take a different lock than one started with it. Off macOS, or
    if getconf fails, tempfile.gettempdir()."""
    try:
        out = subprocess.run(["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5,
                             check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        out = ""
    return out if out and os.path.isdir(out) else tempfile.gettempdir()


def processes():
    """(pid, command line) of every process, for finding a glance instance left behind by a timeout."""
    out = subprocess.run(["ps", "-axo", "pid=,args="], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, check=False).stdout
    rows = []
    for line in out.splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid.isdigit():
            rows.append((int(pid), args.strip()))
    return rows


class LaunchReader:
    """Runs a second, background Noteling instance through LaunchServices so it reads with Noteling.app's own
    Accessibility grant, not the worker's process chain (P0 probe: exec'd from a worker shell it has none).

    `open -n -g -W -a APP -o OUT --stderr ERR --args ARGS`: -n a new instance (the running app is untouched),
    -g in the background (no focus change), -W wait for it to exit. `open` itself always exits 0, so the answer is
    the JSON in OUT."""

    def __init__(self, app=DEFAULT_APP, timeout=TIMEOUT, opener="open", processes=processes, kill=os.kill,
                 lock=None):
        self.app, self.timeout, self.opener = app, timeout, opener
        self.processes, self.kill = processes, kill
        self.lock = lock or LOCK or os.path.join(user_temp_dir(), LOCK_NAME)

    def _hold(self):
        """The host-wide glance lock, waited for up to the timeout. A timeout kills the instances that appeared
        since this glance began, so two glances must not overlap or one could kill the other's instance."""
        # O_NOFOLLOW: a symlink planted at the lock path is refused rather than followed (review L1).
        try:
            fd = os.open(self.lock, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError:
            raise GlanceError("lock_failed", "The glance lock %s couldn't be opened safely, so nothing was read. "
                              "Tell your manager." % self.lock)
        handle = os.fdopen(fd, "w")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return handle
            except OSError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise GlanceError("busy", "Another glance is still running on this Mac. Try again shortly.")
                time.sleep(0.1)

    def instances(self):
        """PIDs of glance instances of this app: its executable run with --json (the running app has no args)."""
        binary = os.path.join(self.app, "Contents", "MacOS") + os.sep
        return {pid for pid, args in self.processes() if args.startswith(binary) and " --json" in args}

    def __call__(self, args):
        if not os.path.isdir(self.app):
            raise GlanceError("noteling_missing", "Noteling isn't installed at %s, so the screen can't be read."
                              % self.app)
        version = app_version(self.app)
        if version is None or version < MIN_VERSION:
            raise GlanceError("noteling_too_old", TOO_OLD)
        held = self._hold()
        try:
            return self._launch(args)
        finally:
            held.close()   # closing releases the flock

    def _launch(self, args):
        before = self.instances()
        folder = tempfile.mkdtemp(prefix="glance-")
        os.chmod(folder, 0o700)
        out, err = os.path.join(folder, "out.json"), os.path.join(folder, "err.txt")
        try:
            try:
                subprocess.run([self.opener, "-n", "-g", "-W", "-a", self.app, "-o", out, "--stderr", err,
                                "--args"] + list(args), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=self.timeout, check=False)
            except subprocess.TimeoutExpired:
                # subprocess.run already killed `open`; the instance LaunchServices started is not its child.
                for pid in self.instances() - before:
                    try:
                        self.kill(pid, 9)
                    except OSError:
                        pass
                raise GlanceError("timeout", "Noteling didn't answer within %g s." % self.timeout)
            except OSError:
                raise GlanceError("launch_failed", "Noteling couldn't be launched.")
            try:
                with open(out, encoding="utf-8") as handle:
                    text = handle.read()
            except OSError:
                text = ""
            return parse(text)
        finally:
            shutil.rmtree(folder, ignore_errors=True)


def parse(text):
    """Noteling's report: the last line of output that is a JSON object with an integer ``exit``.

    C4: an older Noteling silently ignores --json and prints a human line; that shape (no JSON report) means too
    old. The human line is never parsed. No output at all means the instance crashed or was stopped (review F3)."""
    if not (text or "").strip():
        raise GlanceError("no_answer", NO_ANSWER)
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get("exit"), int) and not isinstance(value["exit"], bool):
            return value
    raise GlanceError("noteling_too_old", TOO_OLD)


reader = None  # None: LaunchReader(DEFAULT_APP). Tests set a fake.


def check(find=None, role=None, app=None):
    """Refuse values that could become Noteling flags (review F1). Noteling picks its mode with
    CommandLine.arguments.contains(...), and --ask, --record-clicks and others are tested before --find, so a value
    such as "--ask" or "--full" would change what Noteling does. find may be any name that doesn't start with "-";
    role is letters only; app is a bundle-ID shape."""
    for name, value in (("find", find), ("role", role), ("app", app)):
        if value is not None and not isinstance(value, str):
            raise ValueError("%s must be text." % name)
    if find is not None:
        if not find.strip():
            raise ValueError("find must name a control, e.g. \"Add to cart\".")
        if find.lstrip().startswith("-"):
            raise ValueError("find can't start with \"-\"; name the control as a person would say it.")
        if any(unicodedata.category(c) in UNPRINTABLE for c in find):
            raise ValueError("find can't contain line breaks, control characters or invisible format characters.")
    if role is not None:
        if find is None:
            raise ValueError("role narrows find; give find too.")
        if not ROLE.match(role):
            raise ValueError("role is a single word of letters, e.g. button or link.")
    if app is not None and not BUNDLE.match(app):
        raise ValueError("app is a browser bundle ID such as com.google.Chrome.")


def arguments(find=None, role=None, app=None):
    """Noteling's command line for a glance. Never --full or --contains (v1 is counts and find only)."""
    check(find=find, role=role, app=app)
    args = ["--read-page", "--json"]
    if find is not None:
        args = ["--find", find] + (["--role", role] if role is not None else []) + ["--json"]
    if app is not None:
        args += ["--app", app]
    return args


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def stored_counts(page):
    """The whitelisted counts: numbers and booleans only, kinds as {kind: count}, site as its host only."""
    if not isinstance(page, dict):
        return None
    counts = {}
    for name in COUNT_FIELDS:
        value = page.get(name)
        if isinstance(value, bool) or _number(value) is not None:
            counts[name] = value
    kinds = page.get("kinds")
    if isinstance(kinds, dict):
        counts["kinds"] = {str(k)[:40]: v for k, v in kinds.items() if _number(v) is not None}
    site = host(page.get("site"))
    if site:
        counts["site"] = site
    return counts


def stored_find(found, asked, role):
    """What may be kept of a find: the asked name and role, match kind, count, unique, visible/enabled per hit."""
    if not isinstance(found, dict):
        return None
    hits = found.get("hits") if isinstance(found.get("hits"), list) else []
    match = found.get("match") if found.get("match") in ("exact", "alike", "none") else None
    return dict(name=asked, role=role, match=match, count=_number(found.get("count")),
                unique=found.get("unique") if isinstance(found.get("unique"), bool) else None,
                hits=[dict(visible=h.get("visible") is True, enabled=h.get("enabled") is True)
                      for h in hits if isinstance(h, dict)])


def answer(report):
    """The employee's tool result: Noteling's report without page text, plus a status word and a sentence."""
    code = report.get("exit")
    result = dict(ok=report.get("ok") is True and code == 0, exit=code, status=EXITS.get(code, "unknown"))
    if isinstance(report.get("page"), dict):
        page = {k: v for k, v in report["page"].items() if k in PAGE_FIELDS}
        result["page"] = page
    if isinstance(report.get("find"), dict):
        result["find"] = report["find"]
    if code in SENTENCES:
        result["explanation"] = SENTENCES[code]
    if isinstance(report.get("error"), str):
        result["error"] = report["error"][:300]
    return result


def screen_glance(store, find=None, role=None, app=None):
    if not enabled(store.home):
        raise ValueError("Unknown tool: screen_glance")
    check(find=find, role=role, app=app)
    read = reader or LaunchReader()
    try:
        report = read(arguments(find=find, role=role, app=app))
        result = answer(report)
        reason = None
    except GlanceError as exc:
        report, reason = {}, exc.reason
        result = dict(ok=False, exit=None, status=reason, explanation=str(exc))
    record = dict(id="glance-" + uuid.uuid4().hex, run_id=store.run_id, ok=result["ok"], exit=result["exit"],
                  status=result["status"], counts=stored_counts(report.get("page")),
                  find=stored_find(report.get("find"), find, role) if find is not None else None,
                  browser=app if app is not None else None)
    with store._tx():
        from .store import timestamp
        record["at"] = timestamp()
        store._save("glances", record, "glance.recorded")
    result["glance_id"] = record["id"]
    return result


def recent(store, limit=RECENT):
    """The newest glances as stored (already whitelisted), newest first."""
    rows = store.db.execute("SELECT data FROM glances ORDER BY rowid DESC LIMIT ?", (int(limit),)).fetchall()
    return [json.loads(row["data"]) for row in rows]


def _text(description=""):
    return {"type": "string", "description": description}


TOOLS = [{
    "name": "screen_glance",
    "description": "Look at the human's front browser page through Noteling, read-only: what kinds of things it "
                   "has (counts, never its text) and, with find, whether a named control is there (exact or "
                   "alike match, unique, visible, enabled). A glance is recorded without labels or page words. "
                   "Browsers only. On no_permission or noteling_missing, tell your manager rather than retrying.",
    "inputSchema": {"type": "object", "additionalProperties": False, "required": [], "properties": {
        "find": _text("A control's name as a person would say it, e.g. \"Add to cart\"."),
        "role": _text("Optional role for find, e.g. button, link."),
        "app": _text("Optional browser bundle ID (com.google.Chrome, com.apple.Safari); default the front browser."),
    }},
}]
NAMES = {t["name"] for t in TOOLS}
MUTATIONS = {"screen_glance"}


def call(store, name, arguments):
    return screen_glance(store, **arguments)
