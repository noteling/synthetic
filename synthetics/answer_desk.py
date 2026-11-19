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
The desk never logs, caches or writes a question body, an answer, the key, a cookie or a code.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import secrets
import subprocess
import sys
import time
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


def _write_private(path, value):
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(value, fh)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


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
    _write_private(state_dir(root) / "pairing.json",
                   dict(salt=salt, hash=_sha(salt + code), expires=now + PAIR_SECONDS, tries=0))
    return code


def _normal(code):
    return re.sub(r"[^a-z0-9]", "", str(code or "").lower())


def redeem(root, code, now=None):
    """Check a typed code. Success: a new session cookie value (the pairing is used up). Failure: None; the
    fifth wrong try, an expired pairing or no pairing leaves no pairing, so a new `pair` is needed."""
    now = time.time() if now is None else now
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
    for name in ("sessions.json", "pairing.json"):
        (state_dir(root) / name).unlink(missing_ok=True)


def csrf(cookie, qid, nonce):
    return hmac.new(cookie.encode("ascii"), ("%s|%s" % (qid, nonce)).encode("ascii"), hashlib.sha256).hexdigest()


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
    out = subprocess.run([sys.executable, "-m", "synthetics", "--home", str(home), "channel", command,
                          "--key-file", str(key_file)] + list(args),
                         input=stdin, capture_output=True, text=True, env=env, timeout=60)
    rows = []
    for text in out.stdout.splitlines():
        try:
            rows.append(json.loads(text))
        except ValueError:
            continue
    if not rows:
        return dict(result="rejected", error=dict(code="bad_line", message="no contract response"))
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


def index_page(root):
    rows = ["<li><a href='/q/%s/%s'>%s</a> — %s</li>" % (quote(slug), quote(item["question_id"]), E(name),
                                                          E(str(item.get("summary") or "")))
            for slug, name, item in asks(root)]
    return page("Open questions", "<ul>%s</ul>" % "".join(rows) if rows else "<p>No open questions.</p>")


def pair_page(message=""):
    return page("Pair this browser", "%s<p>Run <code>python3 -m synthetics answer-desk pair</code> in your own "
                "terminal and type the code it prints.</p><form method='post' action='/pair'>"
                "<input name='code' autocomplete='off' autofocus> <button>Pair</button></form>" % (
                    "<p class='muted'>%s</p>" % E(message) if message else ""))


def question_page(slug, question, cookie):
    nonce = secrets.token_hex(16)
    works = ", ".join(question.get("work_ids") or []) or "none"
    return page("Answer %s" % slug, (
        "<p class='muted'>Asked %s · blocks: %s</p><pre>%s</pre>"
        "<form method='post' action='/a/%s/%s'><input type='hidden' name='nonce' value='%s'>"
        "<input type='hidden' name='csrf' value='%s'><textarea name='answer' rows='8' maxlength='%d'></textarea>"
        "<p><button>Send this answer</button></p></form>") % (
            E(str(question.get("created_at") or "")), E(works), E(question.get("body") or ""), quote(slug),
            quote(question["question_id"]), nonce, csrf(cookie, question["question_id"], nonce), MAX_BODY))


def outcome(root, slug, qid, event_status):
    """C2: (employee read it, answered), each from a record: event-status says processed, and the home's
    status.json no longer lists the question as open. An HTTP 200 alone never makes either true."""
    read = isinstance(event_status, dict) and event_status.get("state") == "processed"
    status = sharedstatus.read(Path(root) / slug) or {}
    still_open = any(isinstance(q, dict) and q.get("id") == qid for q in status.get("questions_open") or [])
    answered = read and not still_open
    return read, answered


def status_page(root, slug, qid, event_id, event_status):
    read, answered = outcome(root, slug, qid, event_status)
    submitted = isinstance(event_status, dict) and event_status.get("event_id") == event_id
    steps = [
        ("Submitted", submitted, "event %s accepted through the desk" % event_id if submitted else
         "no record of this event for the desk"),
        ("Employee read it", read, "in run %s" % event_status.get("run_id") if read else
         "not yet: the employee's session has not finished reading it"),
        ("Outcome", answered, "the question is answered (no longer open in %s's status)" % slug if answered else
         "still open"),
    ]
    rows = "".join("<li>%s %s — <span class='muted'>%s</span></li>" % ("✓" if ok else "…", E(name), E(text))
                   for name, ok, text in steps)
    return page("Answer status", "<ol>%s</ol><p><a href='/s/%s/%s?q=%s'>Refresh</a> · <a href='/'>All questions</a>"
                "</p>" % (rows, quote(slug), quote(event_id), quote(qid)))


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
            parts, query = self._parts()
            if parts == ["pair"]:
                return self._send(200, pair_page(), head_only=head_only)
            if not valid_session(root, self._cookie()):
                return self._redirect("/pair")
            cookie = self._cookie()
            if not parts:
                return self._send(200, index_page(root), head_only=head_only)
            if len(parts) == 3 and parts[0] == "q" and QID.match(parts[2]):
                home = home_of(root, parts[1])
                row = contract(home, key_file, "question", "--id", parts[2]) if home else None
                if not row or row.get("error"):
                    raise Refused(404, "No open question for you with that ID.")
                return self._send(200, question_page(parts[1], row, cookie), head_only=head_only)
            if len(parts) == 3 and parts[0] == "s" and EID.match(parts[2]):
                home = home_of(root, parts[1])
                qid = (query.get("q") or [""])[-1]
                if not home or not QID.match(qid):
                    raise Refused(404, "No such page.")
                row = contract(home, key_file, "event-status", "--event", parts[2])
                return self._send(200, status_page(root, parts[1], qid, parts[2], row), head_only=head_only)
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
            answer = (fields.get("answer") or "").strip()
            if not answer or len(answer.encode("utf-8")) > MAX_BODY:
                raise Refused(400, "The answer must be 1 to %d bytes." % MAX_BODY)
            home = home_of(root, slug)
            question = contract(home, key_file, "question", "--id", qid) if home else None
            if not question or question.get("error"):
                raise Refused(409, "That question is not open for you any more.")
            # C1: the sender is the question's recipient as the contract returned it (the home's default_human).
            row = contract(home, key_file, "inbound", lines=[dict(
                v=1, kind="message", external_id="desk:%s:%s" % (qid, nonce), sender=question["to"], body=answer,
                source=SOURCE, question_id=qid, thread=question.get("thread"))])
            if row.get("result") not in ("accepted", "duplicate"):
                code = (row.get("error") or {}).get("code") or "rejected"
                raise Refused(409, "Not accepted (%s)." % code)
            return self._redirect("/s/%s/%s?q=%s" % (quote(slug), quote(row["event_id"]), quote(qid)))

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
