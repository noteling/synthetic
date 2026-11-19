"""#25 "answer desk": a local page where the human answers an employee's tracked question (docs/answer-desk.md).

Shape (P0 sha 65915708, approved by Codex 2026-11-19 with D1=(b), D2, D3 and C1-C5):
  - A separate process and port (default 8766). Lookout stays GET/HEAD-only and holds no key (D3).
  - An ordinary channel adapter: it never imports the Store or opens SQLite. Every read and write is a
    `synthetics --home H channel …` contract call with the desk's key: `question` (D1) to show the full
    question, `inbound` to submit the answer, `event-status` for the status page. The answer's sender is the
    question's `to` as `channel question` returned it, which is that home's default_human; `routed` is never
    set (C1).
  - Authentication (D2): `answer-desk pair` in the human's own terminal prints a one-time code (5 minutes,
    5 wrong tries kill it, compared in constant time; only a salted hash is on disk). Typing it at /pair sets
    an HttpOnly SameSite=Strict cookie whose SHA-256 alone is stored. Every request checks Host; every POST
    also checks Origin, Sec-Fetch-Site and a per-form HMAC token.
  - Status (C2): submitted / employee read it / outcome, each from a record (inbound result, event-status,
    status.json), never from the HTTP response alone.
  - P2 (P2.md sha 114e1ac1, approved by Codex with D1-D3): a "context, keep the question open" button (a plain
    inbound with in_reply_to = the question, no question_id); a desk journal (answer-desk/submissions.json, IDs
    and states only) so the same form sent again after a lost response reaches the idempotent inbound instead of
    the open check; failures re-render the form with the human's own text; `event-status` gives the question ID
    and finds an unconfirmed submit by its external_id.
The desk never logs, caches or writes a question body, an answer, the key, a cookie or a code.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import sharedstatus
from .store import SESSION_ENV

BIND = "127.0.0.1"
DEFAULT_PORT = 8766
SOURCE = "lookout:desk"
COOKIE = "desk"
SESSION_SECONDS = 43200
PAIR_SECONDS = 300
PAIR_TRIES = 5
MAX_BODY = 8192
CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
QID = re.compile(r"^question-[0-9a-f]{32}$")
EID = re.compile(r"^event-[0-9a-f]{32}$")
NONCE = re.compile(r"^[0-9a-f]{32}$")
SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
KINDS = ("answer", "context")
JOURNAL_DAYS = 7
JOURNAL_MAX = 200
CONTRACT_TIMEOUT = 60
# Vesper's #71 L1: a `pending` entry still missing this long after its claim can't be in flight any more (the
# inbound call is bounded by CONTRACT_TIMEOUT), so it settles `lost` and `/` stops looking it up on every view.
LOST_AFTER = 2 * CONTRACT_TIMEOUT
FINAL_CODES = ("conflict", "invalid_state")  # the store's settled refusals; anything else may be sent again
PACKAGE_ROOT = str(Path(__file__).resolve().parents[1])
CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"
E = html.escape


class Refused(Exception):
    def __init__(self, status, text):
        super().__init__(text)
        self.status = status


def refuse_in_session(command):
    if os.environ.get(SESSION_ENV):
        raise SystemExit("answer-desk %s is the human's own operation and is refused inside an employee session "
                         "(%s is set)." % (command, SESSION_ENV))


def state_dir(root):
    path = Path(root) / "answer-desk"
    path.mkdir(mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


_STATE_LOCK = threading.Lock()


@contextmanager
def _locked(root):
    """Serialize every read-modify-write of pairing.json and sessions.json: a thread lock for the threaded
    server, plus flock on answer-desk/.lock because `answer-desk pair` and `logout-all` are other processes.
    Without it, concurrent wrong codes each read tries=N and the 5-try cap fails (PR #67 review F1)."""
    with _STATE_LOCK:
        fd = os.open(state_dir(root) / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _write_private(path, value):
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(value, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _read_private(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- pairing and sessions


def new_code():
    return "-".join("".join(secrets.choice(CODE_ALPHABET) for _ in range(5)) for _ in range(4))


def pair(root, now=None):
    """Start a pairing: returns the one-time code for the human's terminal. Only its salted hash is stored."""
    now = time.time() if now is None else now
    code = new_code()
    salt = secrets.token_hex(16)
    with _locked(root):
        _write_private(state_dir(root) / "pairing.json",
                       dict(salt=salt, hash=_sha(salt + code), expires=now + PAIR_SECONDS, tries=0))
    return code


def _normal(code):
    return re.sub(r"[^a-z0-9]", "", str(code or "").lower())


def redeem(root, code, now=None):
    """Check a typed code. Success: a new session cookie value (the pairing is used up). Failure: None; the
    fifth wrong try, an expired pairing or no pairing leaves no pairing, so a new `pair` is needed."""
    now = time.time() if now is None else now
    with _locked(root):
        return _redeem_locked(root, code, now)


def _redeem_locked(root, code, now):
    path = state_dir(root) / "pairing.json"
    pairing = _read_private(path)
    if not pairing:
        return None
    if now > float(pairing.get("expires") or 0):
        path.unlink(missing_ok=True)
        return None
    typed = _normal(code)
    expected = str(pairing.get("hash") or "")
    salt = str(pairing.get("salt") or "")
    if hmac.compare_digest(_sha(salt + "-".join(typed[i:i + 5] for i in range(0, len(typed), 5))), expected):
        path.unlink(missing_ok=True)
        cookie = secrets.token_hex(32)
        sessions = _sessions(root, now)
        sessions[_sha(cookie)] = now + SESSION_SECONDS
        _write_private(state_dir(root) / "sessions.json", dict(sessions=sessions))
        return cookie
    pairing["tries"] = int(pairing.get("tries") or 0) + 1
    if pairing["tries"] >= PAIR_TRIES:
        path.unlink(missing_ok=True)
    else:
        _write_private(path, pairing)
    return None


def _sessions(root, now):
    value = _read_private(state_dir(root) / "sessions.json") or {}
    sessions = value.get("sessions") if isinstance(value.get("sessions"), dict) else {}
    return {k: v for k, v in sessions.items() if isinstance(v, (int, float)) and v > now}


def valid_session(root, cookie, now=None):
    now = time.time() if now is None else now
    if not cookie or not re.match(r"^[0-9a-f]{64}$", cookie):
        return False
    digest = _sha(cookie)
    return any(hmac.compare_digest(digest, known) for known in _sessions(root, now))


def logout_all(root):
    with _locked(root):
        for name in ("sessions.json", "pairing.json"):
            (state_dir(root) / name).unlink(missing_ok=True)


def csrf(cookie, qid, nonce):
    return hmac.new(cookie.encode("ascii"), ("%s|%s" % (qid, nonce)).encode("ascii"), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------- the desk journal (P2 §3): IDs and states only

JOURNAL_FIELDS = ("home", "question_id", "nonce", "kind", "sender", "thread", "work_ids", "state", "event_id",
                  "error_code", "at")


def _journal_path(root):
    return state_dir(root) / "submissions.json"


def _journal_read(root, now):
    value = _read_private(_journal_path(root)) or {}
    entries = [e for e in value.get("entries") or [] if isinstance(e, dict)]
    fresh = [e for e in entries if isinstance(e.get("at"), (int, float)) and e["at"] > now - JOURNAL_DAYS * 86400]
    return fresh[-JOURNAL_MAX:]


def journal(root, now=None):
    """All recent desk submissions (newest last). Never a body, cookie, code or token: only JOURNAL_FIELDS."""
    with _locked(root):
        return _journal_read(root, time.time() if now is None else now)


def journal_find(root, home, qid, nonce):
    for entry in reversed(journal(root)):
        if (entry.get("home"), entry.get("question_id"), entry.get("nonce")) == (home, qid, nonce):
            return entry
    return None


def journal_claim(root, home, qid, nonce, kind, sender, thread, work_ids=(), now=None):
    """Record a first send as pending, or return the entry a concurrent send of the same form already made."""
    now = time.time() if now is None else now
    with _locked(root):
        entries = _journal_read(root, now)
        for entry in entries:
            if (entry.get("home"), entry.get("question_id"), entry.get("nonce")) == (home, qid, nonce):
                if entry.get("state") in ("lost", "pending"):
                    # Sent again: in flight once more. A fresh `at` also makes a reconcile that read the old
                    # entry fail its compare-and-set (Vesper's #72 L1/n1), so it can't settle this send `lost`.
                    entry.update(state="pending", at=now)
                    _write_private(_journal_path(root), dict(entries=entries))
                return entry
        entry = dict(home=home, question_id=qid, nonce=nonce, kind=kind, sender=sender, thread=thread,
                     work_ids=list(work_ids or []), state="pending", event_id=None, error_code=None, at=now)
        entries.append(entry)
        _write_private(_journal_path(root), dict(entries=entries[-JOURNAL_MAX:]))
        return entry


def journal_settle(root, home, qid, nonce, state, event_id=None, error_code=None, now=None, expect_at=None):
    """Settle one entry. With expect_at (reconcile), only by compare-and-set: the entry must still be `pending`
    with that same `at`, so a send accepted meanwhile (another tab) is never overwritten (Vesper's #72 L1)."""
    now = time.time() if now is None else now
    with _locked(root):
        entries = _journal_read(root, now)
        for entry in entries:
            if (entry.get("home"), entry.get("question_id"), entry.get("nonce")) == (home, qid, nonce):
                if expect_at is not None and (entry.get("state") != "pending" or entry.get("at") != expect_at):
                    return False
                entry.update(state=state, event_id=event_id or entry.get("event_id"), error_code=error_code)
        _write_private(_journal_path(root), dict(entries=entries))
        return True


def external_id(qid, nonce):
    return "desk:%s:%s" % (qid, nonce)


# ---------------------------------------------------------------- the channel contract (never the Store)


def home_of(root, slug):
    if not SLUG.match(slug or ""):
        return None
    home = Path(root) / slug
    return home if home.is_dir() and sharedstatus.path(home).exists() else None


def contract(home, key_file, command, *args, lines=None):
    env = dict(os.environ)  # keeps SYNTHETICS_SESSION_RUN if set, so the contract refuses (fail closed)
    env["PYTHONPATH"] = PACKAGE_ROOT + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    stdin = "".join(json.dumps(item) + "\n" for item in lines) if lines is not None else None
    try:
        out = subprocess.run([sys.executable, "-m", "synthetics", "--home", str(home), "channel", command,
                              "--key-file", str(key_file)] + list(args),
                             input=stdin, capture_output=True, text=True, env=env, timeout=CONTRACT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return dict(result="rejected", error=dict(code="no_response", message="the contract call timed out"))
    rows = []
    for text in out.stdout.splitlines():
        try:
            rows.append(json.loads(text))
        except ValueError:
            continue
    if not rows:
        return dict(result="rejected", error=dict(code="no_response", message="no contract response"))
    return rows[0]


# ---------------------------------------------------------------- pages


def page(title, body):
    return ("<!doctype html><html><head><meta charset='utf-8'><title>%s · Answer desk</title>"
            "<style>body{font:15px -apple-system,sans-serif;max-width:46em;margin:2em auto;padding:0 1em}"
            "pre{white-space:pre-wrap;background:#f6f6f6;padding:.8em}.muted{color:#666}textarea{width:100%%}"
            "</style></head><body><h1>%s</h1>%s</body></html>" % (E(title), E(title), body))


def asks(root):
    """Each home's open asks of its human: the Docket items in status.json, first line only (the full body
    needs the key and `channel question`)."""
    out = []
    try:
        children = sorted(Path(root).iterdir())
    except OSError:
        return out
    for child in children:
        status = sharedstatus.read(child) if child.is_dir() else None
        dock = status.get("docket") if status else None
        if not isinstance(dock, dict):
            continue
        for item in dock.get("items") or []:
            if isinstance(item, dict) and QID.match(str(item.get("question_id") or "")):
                out.append((child.name, status.get("name") or child.name, item))
    return out


def _when(at):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(at)) if isinstance(at, (int, float)) else "?"


def lookup(home, key_file, qid, nonce):
    """D2(b): this form's event ID if the store has it; None only when the contract said `not_routed` (no such
    event of this channel); "no_response" if the contract is silent; "unknown" for any other refusal or a
    malformed row (auth_failed after a key rotation, bad_line, in_session...): the desk couldn't ask, so it
    must not conclude the store has no record (Vesper's #72 F1)."""
    row = contract(home, key_file, "event-status", "--source", SOURCE, "--external-id", external_id(qid, nonce))
    if not isinstance(row, dict):
        return "unknown"
    code = (row.get("error") or {}).get("code")
    if code == "no_response":
        return "no_response"
    if code == "not_routed":
        return None
    if not code and EID.match(str(row.get("event_id") or "")):
        return row["event_id"]
    return "unknown"


def reconcile_pending(root, key_file, now=None):
    """P2 §3: a submit left `pending` (the desk was killed, or the response was lost) is settled from a record:
    `event-status --source --external-id` finds the event only if this channel fed it (D2(b)). Vesper's #71
    L1: one still missing LOST_AFTER after its claim settles `lost`, so it costs one lookup (and one audit row)
    at most once more, not one per view for 7 days. A same-form resend of a lost entry still looks it up first."""
    now = time.time() if now is None else now
    for entry in journal(root, now):
        if entry.get("state") != "pending":
            continue
        home = home_of(root, entry.get("home") or "")
        if not home:
            continue
        at = entry.get("at")
        found = lookup(home, key_file, entry["question_id"], entry["nonce"])
        if found in ("no_response", "unknown"):
            continue  # couldn't ask: stays pending
        if found:
            journal_settle(root, entry["home"], entry["question_id"], entry["nonce"], "accepted", event_id=found,
                           expect_at=at)
        elif isinstance(at, (int, float)) and at <= now - LOST_AFTER:
            journal_settle(root, entry["home"], entry["question_id"], entry["nonce"], "lost", expect_at=at)


def index_page(root, key_file=None):
    if key_file is not None:
        reconcile_pending(root, key_file)
    rows = ["<li><a href='/q/%s/%s'>%s</a> — %s</li>" % (quote(slug), quote(item["question_id"]), E(name),
                                                          E(str(item.get("summary") or "")))
            for slug, name, item in asks(root)]
    body = "<ul>%s</ul>" % "".join(rows) if rows else "<p>No open questions.</p>"
    pending = [e for e in journal(root) if e.get("state") == "pending"]
    if pending:
        body += ("<h2>Unconfirmed submits</h2><p class='muted'>The desk sent these but has no record that they "
                 "arrived. If a question is no longer listed above, it was probably taken; open it to send the "
                 "same form again safely.</p><ul>%s</ul>") % "".join(
            "<li>%s · %s for %s in %s</li>" % (E(_when(e.get("at"))), E(str(e.get("kind"))),
                                               E(str(e.get("question_id"))), E(str(e.get("home"))))
            for e in pending)
    lost = [e for e in journal(root) if e.get("state") == "lost"]
    if lost:
        body += ("<h2>Not received</h2><p class='muted'>The desk sent these but the store has no record of them. "
                 "If the question is still listed above, answer it again.</p><ul>%s</ul>") % "".join(
            "<li>%s · %s for %s in %s</li>" % (E(_when(e.get("at"))), E(str(e.get("kind"))),
                                               E(str(e.get("question_id"))), E(str(e.get("home"))))
            for e in lost)
    return page("Open questions", body)


def pair_page(message=""):
    return page("Pair this browser", "%s<p>Run <code>python3 -m synthetics answer-desk pair</code> in your own "
                "terminal and type the code it prints.</p><form method='post' action='/pair'>"
                "<input name='code' autocomplete='off' autofocus> <button>Pair</button></form>" % (
                    "<p class='muted'>%s</p>" % E(message) if message else ""))


BUTTONS = {
    "answer": ("Send as the answer", "This closes the question."),
    "context": ("Send as context, keep the question open",
                "The question stays open. The employee reads it and decides whether it answers the question."),
}


def answer_form(slug, qid, cookie, nonce=None, text="", kinds=KINDS):
    """The form. A re-rendered form (after a failure) keeps its nonce, so sending it again can't count twice."""
    nonce = nonce or secrets.token_hex(16)
    buttons = "".join("<p><button name='kind' value='%s'>%s</button> <span class='muted'>%s</span></p>"
                      % (kind, E(BUTTONS[kind][0]), E(BUTTONS[kind][1])) for kind in kinds)
    return ("<form method='post' action='/a/%s/%s'><input type='hidden' name='nonce' value='%s'>"
            "<input type='hidden' name='csrf' value='%s'><textarea name='answer' rows='8' maxlength='%d'>%s"
            "</textarea>%s</form>") % (quote(slug), quote(qid), nonce, csrf(cookie, qid, nonce), MAX_BODY,
                                       E(text), buttons)


def context_rows(root, slug, question):
    """P2 §1: only what status.json already exports (Lookout shows it) plus the desk's own journal. Never thread
    messages, other questions' bodies, work notes or memories."""
    qid = question["question_id"]
    status = sharedstatus.read(Path(root) / slug) or {}
    rows = []
    if status.get("name"):
        rows.append("Asked by %s%s" % (status["name"], " (%s)" % status["role"] if status.get("role") else ""))
    item = next((i for i in ((status.get("docket") or {}).get("items") or [])
                 if isinstance(i, dict) and i.get("question_id") == qid), None)
    if item:
        routes = ", ".join(str(r) for r in item.get("routes") or [])
        delivery = "Delivery: %s" % (item.get("ack") or "unknown")
        if item.get("delivered_at"):  # review L1: ack says "delivered", the channel names are in routes
            delivery += " elsewhere too%s; only the first answer counts" % (" (routes: %s)" % routes if routes
                                                                            else "")
        rows.append(delivery)
    works = {w.get("id"): w for w in status.get("works") or [] if isinstance(w, dict)}
    for wid in question.get("work_ids") or []:
        work = works.get(wid) or {}
        rows.append("Blocks %s: %s (%s%s)" % (wid, work.get("title") or "?", work.get("status") or "no longer open",
                                             ", priority %s" % work["priority"] if work.get("priority") is not None
                                             else ""))
    open_q = next((q for q in status.get("questions_open") or [] if isinstance(q, dict) and q.get("id") == qid),
                  None)
    for link in (open_q or {}).get("carries") or []:
        rows.append("Carries %s from %s" % (link.get("question_id"), link.get("asker")))
    for entry in journal(root):
        if entry.get("home") == slug and entry.get("question_id") == qid:
            rows.append("Sent here earlier: %s · %s · %s%s" % (
                _when(entry.get("at")), entry.get("kind"), entry.get("state"),
                " · %s" % entry["event_id"] if entry.get("event_id") else ""))
    return rows


def question_page(root, slug, question, cookie):
    rows = "".join("<li>%s</li>" % E(text) for text in context_rows(root, slug, question))
    return page("Answer %s" % slug, (
        "<p class='muted'>Asked %s</p><ul class='muted'>%s</ul><pre>%s</pre>%s") % (
            E(str(question.get("created_at") or "")), rows, E(question.get("body") or ""),
            answer_form(slug, question["question_id"], cookie)))


def outcome(root, slug, qid, event_status):
    """C2: (employee read it, answered), each from a record: event-status says processed, and the home's
    status.json no longer lists the question as open. An HTTP 200 alone never makes either true."""
    read = isinstance(event_status, dict) and event_status.get("state") == "processed"
    status = sharedstatus.read(Path(root) / slug)
    if not isinstance(status, dict) or not qid:  # unreadable or missing status.json is no evidence (review F2)
        return read, False
    still_open = any(isinstance(q, dict) and q.get("id") == qid for q in status.get("questions_open") or [])
    answered = read and not still_open
    return read, answered


def event_kind(event_status):
    """From the event record (D2(a)), never from the URL: (kind, question ID)."""
    if not isinstance(event_status, dict) or event_status.get("error"):
        return None, None
    if QID.match(str(event_status.get("question_id") or "")):
        return "answer", event_status["question_id"]
    if QID.match(str(event_status.get("in_reply_to") or "")):
        return "context", event_status["in_reply_to"]
    return None, None


def status_page(root, slug, event_id, event_status):
    kind, qid = event_kind(event_status)
    read, answered = outcome(root, slug, qid, event_status)
    submitted = isinstance(event_status, dict) and event_status.get("event_id") == event_id
    status = sharedstatus.read(Path(root) / slug)
    if not submitted:
        sent = "no record of this event for the desk"
    elif kind == "answer":
        sent = "event %s accepted as the answer to %s; the question closed in the same step" % (event_id, qid)
    elif kind == "context":
        sent = "event %s accepted as context for %s; the question stays open" % (event_id, qid)
    else:
        sent = "event %s accepted through the desk" % event_id
    if kind == "context":
        still_open = isinstance(status, dict) and any(
            isinstance(q, dict) and q.get("id") == qid for q in status.get("questions_open") or [])
        result_ok = read and isinstance(status, dict)
        result = ("still open: the employee may close it with your message or ask again" if still_open else
                  "no longer open in %s's status" % slug) if result_ok else "can't tell yet"
    else:
        result_ok = answered
        result = "the question is no longer open in %s's status" % slug if answered else "still open"
        if answered:
            entry = next((e for e in journal(root) if e.get("event_id") == event_id), None)
            works = {w.get("id"): w for w in (status or {}).get("works") or [] if isinstance(w, dict)}
            for wid in (entry or {}).get("work_ids") or []:
                result += "; %s is %s" % (wid, (works.get(wid) or {}).get("status") or "no longer open")
    steps = [
        ("Submitted", submitted, sent),
        ("Employee read it", read, "in run %s" % event_status.get("run_id") if read else
         "not yet: the employee's session has not finished reading it"),
        ("Outcome", result_ok, result),
    ]
    rows = "".join("<li>%s %s — <span class='muted'>%s</span></li>" % ("✓" if ok else "…", E(name), E(text))
                   for name, ok, text in steps)
    return page("Answer status", "<ol>%s</ol><p><a href='/s/%s/%s'>Refresh</a> · <a href='/'>All questions</a>"
                "</p>" % (rows, quote(slug), quote(event_id)))


# ---------------------------------------------------------------- HTTP


def make_handler(root, key_file):
    root = str(root)

    class Handler(BaseHTTPRequestHandler):
        server_version = "AnswerDesk/1"

        def _send(self, code, body, extra=(), head_only=False):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            for name, value in extra:
                self.send_header(name, value)
            self.end_headers()
            if not head_only:
                self.wfile.write(data)

        def _redirect(self, location, extra=()):
            self._send(303, page("See other", "<p><a href='%s'>Continue</a></p>" % E(location)),
                       extra=[("Location", location)] + list(extra))

        def _cookie(self):
            for part in (self.headers.get("Cookie") or "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == COOKIE:
                    return value
            return None

        def _host(self):
            return "%s:%d" % (BIND, self.server.server_address[1])

        def _check_host(self):
            if self.headers.get("Host") != self._host():
                raise Refused(403, "Wrong Host.")

        def _check_post(self):
            self._check_host()
            if self.headers.get("Origin") != "http://" + self._host():
                raise Refused(403, "Cross-origin or missing Origin.")
            site = self.headers.get("Sec-Fetch-Site")
            if site is not None and site != "same-origin":
                raise Refused(403, "Not same-origin.")
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/x-www-form-urlencoded":
                raise Refused(400, "Form posts only.")
            try:
                length = int(self.headers.get("Content-Length") or "-1")
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY * 4:
                raise Refused(400, "Missing or oversized body.")
            fields = parse_qs(self.rfile.read(length).decode("utf-8", "replace"), keep_blank_values=True)
            return {k: v[-1] for k, v in fields.items()}

        def _authed(self):
            cookie = self._cookie()
            if not valid_session(root, cookie):
                raise Refused(401, "Not paired.")
            return cookie

        def _parts(self):
            split = urlsplit(self.path)
            return [unquote(p) for p in split.path.split("/") if p], parse_qs(split.query)

        def _get(self, head_only=False):
            self._check_host()
            parts, _query = self._parts()
            if parts == ["pair"]:
                return self._send(200, pair_page(), head_only=head_only)
            if not valid_session(root, self._cookie()):
                return self._redirect("/pair")
            cookie = self._cookie()
            if not parts:
                return self._send(200, index_page(root, key_file), head_only=head_only)
            if len(parts) == 3 and parts[0] == "q" and QID.match(parts[2]):
                home = home_of(root, parts[1])
                row = contract(home, key_file, "question", "--id", parts[2]) if home else None
                if not row or row.get("error"):
                    raise Refused(404, "No open question for you with that ID.")
                return self._send(200, question_page(root, parts[1], row, cookie), head_only=head_only)
            if len(parts) == 3 and parts[0] == "s" and EID.match(parts[2]):
                home = home_of(root, parts[1])
                if not home:
                    raise Refused(404, "No such page.")
                # The question comes from the event record (D2(a)); a ?q= from older links is ignored (P1 L1).
                row = contract(home, key_file, "event-status", "--event", parts[2])
                return self._send(200, status_page(root, parts[1], parts[2], row), head_only=head_only)
            raise Refused(404, "No such page.")

        def _post(self):
            fields = self._check_post()
            parts, _query = self._parts()
            if parts == ["pair"]:
                cookie = redeem(root, fields.get("code"))
                if cookie is None:
                    return self._send(401, pair_page("That code was not accepted. After 5 wrong tries, or 5 "
                                                     "minutes, run pair again."))
                return self._redirect("/", extra=[("Set-Cookie", "%s=%s; HttpOnly; SameSite=Strict; Path=/; "
                                                   "Max-Age=%d" % (COOKIE, cookie, SESSION_SECONDS))])
            cookie = self._authed()
            if not (len(parts) == 3 and parts[0] == "a" and QID.match(parts[2])):
                raise Refused(404, "No such page.")
            slug, qid = parts[1], parts[2]
            nonce = fields.get("nonce") or ""
            if not NONCE.match(nonce) or not hmac.compare_digest(csrf(cookie, qid, nonce), fields.get("csrf") or ""):
                raise Refused(403, "Bad form token.")
            kind = fields.get("kind") or "answer"
            if kind not in KINDS:
                raise Refused(400, "Unknown button.")
            answer = (fields.get("answer") or "").strip()
            if not answer or len(answer.encode("utf-8")) > MAX_BODY:
                raise Refused(400, "The answer must be 1 to %d bytes." % MAX_BODY)
            home = home_of(root, slug)
            if not home:
                raise Refused(409, "That question is not open for you any more.")
            entry = journal_find(root, slug, qid, nonce)
            if entry is not None and entry.get("kind") != kind:
                raise Refused(409, "This form was already sent as %s; open the question again for a new form."
                              % entry.get("kind"))
            if entry is not None and not entry.get("event_id"):
                # Vesper's P2 review F1: resend without the open check only when the store has this form's
                # event (D2(b) lookup). Otherwise the earlier send never arrived and this is a first send.
                found = lookup(home, key_file, qid, nonce)
                if found == "no_response":
                    return self._send(503, page("Not confirmed", (
                        "<p>The desk could not check whether this form arrived (no response). Your text is "
                        "kept below.</p>%s") % answer_form(slug, qid, cookie, nonce=nonce, text=answer,
                                                          kinds=(kind,))))
                if found in (None, "unknown"):
                    # not_routed: never arrived. Any other refusal: the open check below asks the same contract
                    # and fails closed (409), so nothing is sent on a guess.
                    entry = None
            if entry is None:
                # First send of this form: the question must be open for the human now (D1(b)).
                question = contract(home, key_file, "question", "--id", qid)
                if (question.get("error") or {}).get("code") == "no_response":
                    return self._send(503, page("Not confirmed", (
                        "<p>The desk could not check the question (no response). Nothing was sent. Your text is "
                        "kept below.</p>%s") % answer_form(slug, qid, cookie, nonce=nonce, text=answer,
                                                          kinds=(kind,))))
                if not question or question.get("error"):
                    # A concurrent send of this same form (a double click) may have closed the question between
                    # our journal/lookup read and this check: its journal entry and stored event say so. Then
                    # this is that send again (the inbound below answers `duplicate`), not a refusal. Only a
                    # found event counts (Vesper's P2 F1); anything else stays a 409.
                    entry = journal_find(root, slug, qid, nonce)
                    found = None
                    if entry is not None and entry.get("kind") == kind:
                        found = entry.get("event_id") or lookup(home, key_file, qid, nonce)
                    if found == "no_response":
                        return self._send(503, page("Not confirmed", (
                            "<p>The desk could not check whether this form arrived (no response). Your text is "
                            "kept below.</p>%s") % answer_form(slug, qid, cookie, nonce=nonce, text=answer,
                                                              kinds=(kind,))))
                    if not (isinstance(found, str) and EID.match(found)):
                        raise Refused(409, "That question is not open for you any more.")
                else:
                    # C1: the sender is the question's recipient as the contract returned it (the home's
                    # default_human). The journal keeps it so the same form sent again is the same line (P2 §3).
                    entry = journal_claim(root, slug, qid, nonce, kind, question["to"], question.get("thread"),
                                          question.get("work_ids"))
            line = dict(v=1, kind="message", external_id=external_id(qid, nonce), sender=entry["sender"],
                        body=answer, source=SOURCE, thread=entry.get("thread"))
            if kind == "answer":
                line["question_id"] = qid
            else:  # D1/D3: context never answers; it names the question only as generic reply metadata
                line["in_reply_to"] = qid
            row = contract(home, key_file, "inbound", lines=[line])
            if row.get("result") in ("accepted", "duplicate") and EID.match(str(row.get("event_id") or "")):
                journal_settle(root, slug, qid, nonce, "accepted", event_id=row["event_id"])
                return self._redirect("/s/%s/%s" % (quote(slug), quote(row["event_id"])))
            code = (row.get("error") or {}).get("code") or "rejected"
            if code in FINAL_CODES:
                if code == "invalid_state" and entry.get("state") == "pending":
                    journal_settle(root, slug, qid, nonce, "refused", error_code=code)
                text = ("This form was already sent with different text; the first text is the one on record."
                        if code == "conflict" else
                        "The question was answered elsewhere first (for example on Telegram), so this was not "
                        "recorded.")
                return self._send(409, page("Not recorded", "<p>%s</p><p class='muted'>Your text, for your own "
                                            "copy:</p><pre>%s</pre><p><a href='/'>All questions</a></p>"
                                            % (E(text), E(answer))))
            # Not confirmed (timeout, no response, a transient refusal): the same form again is safe, since the
            # store counts one external_id once. Re-render it with the human's own text (P2 §3 step 4).
            return self._send(503, page("Not confirmed", (
                "<p>Not confirmed (%s). Your text is kept below. Send again: the same form can't be counted "
                "twice.</p>%s") % (E(code), answer_form(slug, qid, cookie, nonce=nonce, text=answer,
                                                         kinds=(kind,)))))

        def _guard(self, fn, *args):
            try:
                fn(*args)
            except Refused as exc:
                self._send(exc.status, page("Refused", "<p>%s</p>" % E(str(exc))))
            except Exception as exc:  # noqa: BLE001 - never a traceback page
                self._send(500, page("Error", "<p>%s</p>" % E(type(exc).__name__)))

        def do_GET(self):
            self._guard(self._get)

        def do_HEAD(self):
            self._guard(self._get, True)

        def do_POST(self):
            self._guard(self._post)

        def _refuse(self):
            self._send(405, page("Method not allowed", "<p>GET and form POST only.</p>"))

        do_PUT = do_DELETE = do_PATCH = _refuse

        def log_message(self, fmt, *args):  # never log paths, forms or headers
            pass
    return Handler


def serve(root, key_file, port=DEFAULT_PORT, ready=None):
    key_file = Path(key_file).expanduser().resolve()
    mode = key_file.stat().st_mode
    if mode & 0o077:
        raise ValueError("Key file %s must not be readable by group or others (chmod 600)." % key_file)
    root = Path(root).resolve()
    state_dir(root)
    server = ThreadingHTTPServer((BIND, port), make_handler(root, key_file))
    if ready is not None:
        ready(server)
    try:
        server.serve_forever()
    finally:
        server.server_close()
