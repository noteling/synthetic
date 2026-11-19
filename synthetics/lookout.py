"""VIS P1 "Lookout": a local, read-only page that shows a human what the team is doing (docs/lookout.md).

Boundary (design reviews/VIS/P0 §2, Codex C5): Lookout reads only
  - project logs        <root>/projects/<p>/log.jsonl      (shared exports; never memory.sqlite3)
  - the company log     <root>/company/log.jsonl
  - per home            runtime.state.json, worker.lock (pid text only, no flock) and shared/status.json
  - launchd             `launchctl list`
It never opens a SQLite database, and never reads a home's state.sqlite3, roster/, runs/, workspace/, humans.json or
runtime.json. Every page is generated from those files on each request; nothing is cached or written.
Served on 127.0.0.1 only, GET/HEAD only, every value passed through html.escape.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import projectlog, projectview, roster, sharedstatus

BIND = "127.0.0.1"
DEFAULT_PORT = 8765
SUSPECT_MARGIN = 300  # C2: seconds past the effective timeout before a "running" session is labelled suspect
ADAPTER_LABELS = re.compile(r"^com\.synthetics\.(adapter\..+|fieldpost.*|fieldstone.*|telegram.*|relay.*)$")
HEADER = re.compile(r"^([a-z][a-z0-9-]*): (.*)$")
PROJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
HOME_FILES = ("runtime.state.json", "worker.lock")
THREAD_PAGE = 50  # VIS G10: messages per thread page


# ---------------------------------------------------------------- reading the allowed records


def _jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict) and "seq" in value and "kind" in value:
                    out.append(value)
    except OSError:
        return []
    return out


def _json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            value = json.load(fh)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def projects(root):
    base = Path(root) / "projects"
    try:
        names = sorted(p.name for p in base.iterdir() if p.is_dir() and PROJECT_NAME.match(p.name))
    except OSError:
        return []
    return names


def project_entries(root, project):
    """(entries without bytes, {source id: bytes or None if redacted}). A project with no log.jsonl is empty (C3)."""
    rows = _jsonl(Path(root) / "projects" / project / "log.jsonl")
    redacted = {e["body"].get("target") for e in rows if e["kind"] == "redact" and isinstance(e.get("body"), dict)}
    texts, entries = {}, []
    for e in rows:
        e = dict(e)
        text = e.pop("bytes", None)
        if e["kind"] == "source":
            texts[e["id"]] = None if e["id"] in redacted else text
        entries.append(e)
    entries.sort(key=lambda e: e["seq"])
    return entries, texts


def company(root):
    try:
        return roster.company_fold(_jsonl(Path(root) / roster.COMPANY / "log.jsonl"))
    except Exception:  # noqa: BLE001 - an unreadable company log leaves the page usable
        return roster.company_fold([])


def homes(root):
    """Directories directly under root holding runtime.state.json or shared/status.json."""
    out = []
    try:
        children = sorted(Path(root).iterdir())
    except OSError:
        return out
    for child in children:
        if child.is_dir() and ((child / "runtime.state.json").exists() or sharedstatus.path(child).exists()):
            out.append(child)
    return out


def launchctl_list():
    """{label: pid or None} of loaded launch agents; {} when launchctl is unavailable."""
    try:
        text = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    out = {}
    for line in text.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) == 3:
            out[parts[2]] = int(parts[0]) if parts[0].isdigit() else None
    return out


def _alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _parse(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _lock_pid(home):
    try:
        text = (Path(home) / "worker.lock").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def liveness(home, agents, now=None):
    """The three states of design §4 for one home, from runtime.state.json, worker.lock, launchd and status.json."""
    now = now or datetime.now(timezone.utc)
    state = _json(Path(home) / "runtime.state.json") or {}
    status = sharedstatus.read(home)
    label = state.get("label")
    loaded = label in agents if label else None
    agent_pid = agents.get(label) if label else None
    lock_pid = _lock_pid(home)
    worker_pid = state.get("pid") if isinstance(state.get("pid"), int) else None
    worker_alive = bool(lock_pid) and _alive(lock_pid) and (worker_pid is None or lock_pid == worker_pid)
    service = dict(label=label, loaded=loaded, agent_pid=agent_pid, lock_pid=lock_pid, worker_pid=worker_pid,
                   worker_alive=worker_alive)
    if loaded and agent_pid and not worker_alive:
        service["mismatch"] = "launchd has a pid but the worker lock is not held by it"
    elif loaded is False and worker_alive:
        service["mismatch"] = "worker running but its launch agent is not loaded"
    session = dict(state="unknown", run=None, started_at=None, elapsed_s=None, paused=None)
    if status is None:
        session["state"] = "no status.json"
    else:
        sess = status.get("session") or {}
        written = _parse(status.get("written_at"))
        started = _parse(state.get("started_at"))
        if written is not None and started is not None and "." not in str(status.get("written_at")):
            started = started.replace(microsecond=0)  # a whole-second written_at (pre-r2 file): compare at seconds
        stale = (not worker_alive) or (started is not None and written is not None and written < started)
        session.update(paused=sess.get("paused"), written_at=status.get("written_at"))
        if sess.get("state") == "running" and not stale:
            began = _parse(sess.get("started_at"))
            elapsed = int((now - began).total_seconds()) if began else None
            timeout = (state.get("effective") or {}).get("timeout")
            suspect = isinstance(timeout, (int, float)) and elapsed is not None and elapsed > timeout + SUSPECT_MARGIN
            session.update(state="suspect" if suspect else "running", run=sess.get("run"),
                           started_at=sess.get("started_at"), elapsed_s=elapsed, timeout_s=timeout)
        elif stale:
            session["state"] = "stale (worker not running)"
        else:
            session["state"] = "paused" if sess.get("paused") else "idle"
    return dict(service=service, session=session, status=status, timeout=(state.get("effective") or {}).get("timeout"))


# ---------------------------------------------------------------- threads (design G4 grouping)


def _headers(text):
    head, sep, body = (text or "").partition("\n---\n")
    if not sep:
        return {}, text or ""
    found = {}
    for line in head.splitlines():
        m = HEADER.match(line.strip())
        if not m:
            return {}, text
        found[m.group(1)] = m.group(2).strip()
    return found, body.lstrip("\n")


def canonical(state):
    """Map an address (ID, name or alias such as codex-manager) to the company person ID, else itself."""
    def canon(name):
        if not name:
            return name
        hit = roster.resolve(state, name)
        return hit["id"] or name
    return canon


def messages(entries, texts, canon=None):
    """Captured messages of one project, deduplicated by message id (G5), each with sender/recipient/work ids."""
    canon = canon or (lambda name: name)
    out = {}
    for e in entries:
        if e["kind"] != "source":
            continue
        body = e.get("body") or {}
        origin = body.get("origin") or {}
        text = texts.get(e["id"])
        heads, shown = _headers(text) if text is not None else ({}, None)
        direction = origin.get("direction", "in")
        mid = heads.get("message-id") or (origin.get("event_id") if direction == "out" else None) \
            or origin.get("external_id") or e["id"]
        row = dict(id=mid, entry=e["id"], seq=e["seq"], at=heads.get("sent-at") or origin.get("occurred_at") or e.get("at"),
                   sender=canon(heads.get("from") or origin.get("sender")), to=canon(heads.get("to")),
                   works=sorted({w for w in (heads.get("work-id"), heads.get("for-work")) if w and w.startswith("work-")}),
                   question=heads.get("question-id") or None, thread=origin.get("thread"),
                   source=origin.get("source"), role=origin.get("role"), auth=(origin.get("auth") or {}).get("channel"),
                   captured_by=origin.get("home"), text=shown, redacted=text is None and e["id"] in texts,
                   entries=[e["id"]])
        prior = out.get(mid)
        if prior is None:
            out[mid] = row
        else:  # keep the richer copy (an inbound Fieldpost copy has headers), remember both captures
            richer = row if (row["to"] and not prior["to"]) else prior
            other = prior if richer is row else row
            for key in ("to", "works", "question", "thread"):
                if not richer.get(key) and other.get(key):
                    richer[key] = other[key]
            richer["captured_by"] = ", ".join(sorted({str(prior["captured_by"]), str(row["captured_by"])}))
            richer["entries"] = prior["entries"] + row["entries"]  # VIS G5: every capture links to this thread
            out[mid] = richer
    return sorted(out.values(), key=lambda m: (m["at"] or "", m["seq"]))


def thread_key(message):
    if message.get("thread"):
        return "t:" + message["thread"]
    pair = "~".join(sorted(str(p) for p in (message.get("sender"), message.get("to") or "?")))
    if message.get("works"):
        return "w:%s:%s" % (pair, "+".join(message["works"]))
    return "p:" + pair


def threads(msgs):
    """{key: [messages]}: thread id, else unordered pair + {work-id, for-work} (both directions match), else pair."""
    groups, alias = {}, {}
    for m in msgs:
        key = thread_key(m)
        # A reply that names only one of the two works joins the conversation that already holds that work.
        if key.startswith("w:") and key not in groups:
            pair = key.split(":")[1]
            for work in m["works"]:
                if (pair, work) in alias:
                    key = alias[(pair, work)]
                    break
        groups.setdefault(key, []).append(m)
        if key.startswith("w:"):
            for work in m["works"]:
                alias.setdefault((key.split(":")[1], work), key)
    return groups


# ---------------------------------------------------------------- pages

E = html.escape
STYLE = ("body{font:14px/1.45 -apple-system,system-ui,sans-serif;margin:1.5em;max-width:1200px}"
         "table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:3px 6px;vertical-align:top;text-align:left}"
         "pre{white-space:pre-wrap;background:#f6f6f6;padding:6px}.in{background:#f2f7ff}.out{background:#f7fff2}"
         ".muted{color:#777}.bad{color:#a00}.ok{color:#070}nav a{margin-right:1em}")


def page(title, body, root_projects=(), caption=None):
    """VIS-O: every page has the same short nav (Overview, Projects, Diagnostics) and may open with a one-line muted
    caption saying whom it is for and what it is for. ``root_projects`` is kept for callers; the project list is on
    /projects."""
    nav = '<nav><a href="/">Overview</a><a href="/projects">Projects</a><a href="/diagnostics">Diagnostics</a></nav>'
    lead = "<p class='muted'>%s</p>" % E(caption) if caption else ""
    return ("<!doctype html><html><head><meta charset='utf-8'><title>%s</title><style>%s</style></head><body>%s"
            "<h1>%s</h1>%s%s<p class='muted'>Lookout: generated %s from shared records only. Read-only.</p></body></html>"
            % (E(title), STYLE, nav, E(title), lead, body, E(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"))))


PROJECT_TABS = (("", "Open work"), ("threads", "Threads"), ("board", "Board"), ("memory", "Memory"),
                ("direction", "David's direction"))


def _tabs(project, current):
    """VIS-O: the project sub-tabs; /p/<project> (open work) is the landing page."""
    out = []
    for path, label in PROJECT_TABS:
        href = "/p/%s%s" % (quote(project), "/" + path if path else "")
        out.append("<b>%s</b>" % E(label) if path == current else "<a href='%s'>%s</a>" % (href, E(label)))
    return "<p class='tabs'>%s: %s</p>" % (E(project), " · ".join(out))


def _v(value):
    return "" if value is None else E(str(value))


def _age(ts, now):
    at = _parse(ts)
    if at is None:
        return ""
    return _span(max(0, int((now - at).total_seconds() // 60)))


def _span(minutes):
    hours, mins = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    return "%dd %dh" % (days, hours) if days else ("%dh %dm" % (hours, mins) if hours else "%dm" % mins)


def _rel(ts, now):
    """VIS G9: a relative time ("5m ago", "in 2h 3m", "just now") with the stored timestamp as its tooltip.
    A value that is not a timestamp is shown as it is."""
    if ts is None or ts == "":
        return ""
    at = _parse(ts)
    if at is None:
        return _v(ts)
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    seconds = (now - at).total_seconds()
    minutes = int(abs(seconds) // 60)
    text = "just now" if minutes == 0 else (_span(minutes) + " ago" if seconds > 0 else "in " + _span(minutes))
    return "<time datetime='%s' title='%s'>%s</time>" % (E(str(ts)), E(str(ts)), E(text))


def _who(people, pid):
    """VIS G6: a person's name with the raw ID as its tooltip; an address with no profile shows as itself."""
    if not pid:
        return "?"
    name = (people.get(pid) or {}).get("name")
    if not name:
        return "<code>%s</code>" % E(str(pid))
    return "<span title='%s'>%s</span>" % (E(str(pid)), E(name))


def work_titles(root, entries=()):
    """VIS G-a1/G6: {work_id: title} from the given project entries' backlog rows (published works), then from each
    home's status.json works (open works; the details pages already show those titles). The backlog wins."""
    titles = {}
    for home in homes(root):
        for w in (sharedstatus.read(home) or {}).get("works") or []:
            if isinstance(w, dict) and w.get("id") and w.get("title"):
                titles[w["id"]] = w["title"]
    if entries:
        for row in projectlog.fold(entries)["backlog"].values():
            if row.get("work_id") and row.get("title"):
                titles[row["work_id"]] = row["title"]
    return titles


def _anchor(qid):
    """Page anchor of an open question, so relay hops can link both sides on /team (G3)."""
    return "q-" + re.sub(r"[^0-9A-Za-z_-]", "", str(qid or ""))


def _qlink(qid):
    return "<a href='#%s'><code>%s</code></a>" % (_anchor(qid), _v(qid))


def _relay_links(people, q):
    """G3: the asker's side shows where its question is now (hop) and links the onward question; the carrier's
    side shows which askers' questions it carries and links them. The link resolves when the other home's open
    question is on the same page; otherwise it is just the ID."""
    return "".join(" — " + part for part in _relay_parts(people, q))


def _relay_parts(people, q):
    """The hop (with the onward question it went via) and each carried question, as linked HTML fragments.
    Shared by the open-questions list (G3) and the Docket table's "now" cell (Docket links)."""
    parts = []
    if q.get("hop"):
        parts.append(E(q["hop"]) + (" via " + _qlink(q["hop_via"]) if q.get("hop_via") else ""))
    for link in q.get("carries") or []:
        if isinstance(link, dict) and link.get("question_id"):
            parts.append("carries %s for %s" % (_qlink(link["question_id"]), E(_name(people, link.get("asker")))))
    return parts


def _name(people, pid):
    profile = people.get(pid) or {}
    return profile.get("name") or pid or "?"


def label_owner(people, label):
    """The person whose default worker label (install.worker_label) is ``label``, else None."""
    for pid in people:
        if label == "com.synthetics.worker." + hashlib.sha256(pid.encode()).hexdigest()[:16]:
            return pid
    return None


def _authors(root, plist):
    """{person or home ID: {project}} from who wrote or captured entries in each project log."""
    authors = {}
    for p in plist:
        entries, _ = project_entries(root, p)
        for e in entries:
            authors.setdefault(e.get("author"), set()).add(p)
            home = ((e.get("body") or {}).get("origin") or {}).get("home") if e["kind"] == "source" else None
            if home:
                authors.setdefault(home, set()).add(p)
    return authors


def team(root, agents, now, people):
    """VIS-O: one record per employee home: {home, slug, pid, profile, name, live, status, state (runtime.state.json)}."""
    out = []
    for home in homes(root):
        live = liveness(home, agents, now)
        status = live["status"] or {}
        pid = status.get("employee") or label_owner(people, live["service"]["label"])
        profile = people.get(pid) or {}
        out.append(dict(home=home, slug=home.name, pid=pid, profile=profile, live=live, status=status,
                        name=profile.get("name") or status.get("name") or home.name,
                        runtime=_json(Path(home) / "runtime.state.json") or {}))
    return out


def _home_block(member, people, authors, now):
    """The per-home detail block (formerly the whole Team page), with section anchors the overview links to."""
    home, status, pid, profile, live = member["home"], member["status"], member["pid"], member["profile"], member["live"]
    out = ["<h3>%s <span class='muted'>(%s)</span></h3>" % (E(member["name"]),
                                                          E(profile.get("role") or status.get("role") or ""))]
    out.append("<p>Home <code>%s</code>; employee <code>%s</code>; projects: %s</p>" % (
        E(home.name), _v(pid), E(", ".join(sorted(authors.get(pid, ()))) or "none (no shared project log yet)")))
    svc, sess = live["service"], live["session"]
    svc_text = ("<span class='ok'>loaded</span>" if svc["loaded"] else
                "<span class='bad'>not loaded</span>" if svc["loaded"] is False else "unknown")
    svc_text += " — label %s, launchd pid %s, worker lock pid %s (%s)" % (
        _v(svc["label"]), _v(svc["agent_pid"]), _v(svc["lock_pid"]), "alive" if svc["worker_alive"] else "not running")
    if svc.get("mismatch"):
        svc_text += " <span class='bad'>mismatch: %s</span>" % E(svc["mismatch"])
    sess_text = E(sess["state"])
    if sess.get("run"):
        sess_text += " — %s since %s" % (E(sess["run"]), _rel(sess["started_at"], now))
    if sess["state"] == "suspect":
        sess_text = "<span class='bad'>%s</span> — longer than the timeout (%ss) plus %ss" % (
            sess_text, _v(sess.get("timeout_s")), SUSPECT_MARGIN)
    if sess.get("paused"):
        sess_text += " — paused: %s" % E(json.dumps(sess["paused"], sort_keys=True))
    if sess.get("written_at"):
        sess_text += " <span class='muted'>(status.json written %s)</span>" % _rel(sess["written_at"], now)
    out.append("<table id='service'><tr><th>1. Service</th><td>%s</td></tr><tr><th>2. Session</th><td>%s</td></tr>"
               % (svc_text, sess_text))
    works = status.get("works") or []
    waiting = [w for w in works if w.get("status") == "waiting"]
    out.append("<tr><th>3. Waiting on</th><td>%s</td></tr></table>" % ("<br>".join(
        "%s — %s%s%s" % (E(w.get("title") or w["id"]), E(w.get("waiting_for") or "?"),
                         " (wake %s)" % _rel(w["wake_at"], now) if w.get("wake_at") else "",
                         " [%s]" % E(", ".join(w.get("waiting_question_ids") or [])) if w.get("waiting_question_ids") else "")
        for w in waiting) or "nothing"))
    if works:
        out.append("<p id='work'><b>Work</b></p><table><tr><th>status</th><th>pri</th><th>title</th><th>waiting for</th>"
                   "<th>published</th><th>updated</th><th>id</th></tr>")
        for w in works:  # VIS G7: "updated" = age of the work's last change (status.json updated_at)
            out.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td><code>%s</code></td>"
                       "</tr>" % (_v(w.get("status")), _v(w.get("priority")), _v(w.get("title")),
                                  _v(w.get("waiting_for")), _v(w.get("published")), _rel(w.get("updated_at"), now),
                                  _v(w.get("id"))))
        out.append("</table>")
    rtn = [w for w in works if isinstance(w.get("routine"), dict)]
    rflags = [f for f in status.get("routine_flags") or [] if isinstance(f, dict)]
    if rtn or rflags:  # RTN P1: routines and their discipline flags (David's revisit trigger)
        out.append("<p id='routines'><b>Routines</b></p>")
        if rtn:
            out.append("<table><tr><th>kind</th><th>every</th><th>status</th><th>next</th><th>last fired</th>"
                       "<th>last re-armed</th><th>failures</th></tr>")
            for w in rtn:
                r = w["routine"]
                rstate = "<span class='bad'>quarantined</span>" if r.get("quarantined") else _v(w.get("status"))
                out.append("<tr><td>%s</td><td>%s min</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                           % (_v(r.get("kind")), _v(r.get("every_minutes")), rstate, _rel(w.get("wake_at"), now),
                              _rel(r.get("last_fired_at"), now), _rel(r.get("last_rearmed_at"), now),
                              _v(r.get("failures"))))
            out.append("</table>")
        if rflags:
            out.append("<ul>" + "".join(
                "<li class='%s'>%s %s: %s</li>" % ("bad" if f.get("severity") == "error" else "muted",
                                                  _v(f.get("code")), _v(f.get("kind")),
                                                  _v(f.get("detail") or "%s all time, %s in 7 days"
                                                     % (f.get("total"), f.get("last_7_days"))))
                for f in rflags) + "</ul>")
    dock = status.get("docket")
    if dock:
        out.append("<p id='docket'><b>Waiting on %s</b> (%d open)</p>" % (E(_name(people, dock.get("person"))), dock.get("count", 0)))
        if dock.get("items"):
            out.append("<table><tr><th>question</th><th>asked</th><th>ack</th><th>now</th><th>blocks</th></tr>")
            for item in dock["items"]:
                # Docket links: the question links to its open-question anchor in this block, and "now" shows
                # the hop with the onward question it went via and the carried questions, linked like G3.
                out.append("<tr><td>%s %s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                    _qlink(item.get("question_id")), _v(item.get("summary")), _rel(item.get("asked_at"), now),
                    _v(item.get("ack")),
                    "; ".join(_relay_parts(people, item)),
                    E("; ".join(w.get("title") or w.get("id") for w in item.get("works") or []))))
            out.append("</table>")
    questions = status.get("questions_open") or []
    if questions:
        out.append("<p id='questions'><b>Open questions</b></p><ul>" + "".join(
            "<li id='%s'><code>%s</code> to %s, %s: %s%s</li>" % (
                _anchor(q.get("id")), _v(q.get("id")), _who(people, q.get("to")), _rel(q.get("asked_at"), now),
                _v(q.get("first_line")), _relay_links(people, q)) for q in questions) + "</ul>")
    runs = (status.get("runs") or [])[:5]
    if runs:
        out.append("<p id='sessions'><b>Recent sessions</b></p><table><tr><th>run</th><th>status</th><th>started</th>"
                   "<th>finished</th><th>cost</th><th>events</th><th>failed tools</th></tr>")
        for r in runs:
            cost = "$%.2f" % r["cost"] if isinstance(r.get("cost"), (int, float)) else "?"
            out.append("<tr><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                       % (_v(r.get("id")), _v(r.get("status")), _rel(r.get("started_at"), now),
                          _rel(r.get("finished_at"), now),
                          E(cost), _v(r.get("events")), _v(r.get("failed_tool_count"))))
        out.append("</table>")
    glances = (status.get("glances") or [])[:5]
    if glances:
        out.append("<p id='glances'><b>Recent glances</b> <span class='muted'>(screen_glance: counts and find only)</span></p>"
                   "<table><tr><th>at</th><th>run</th><th>status</th><th>site</th><th>elements / on screen</th>"
                   "<th>find</th></tr>")
        for g in glances:
            counts, find = g.get("counts") or {}, g.get("find") or {}
            seen_text = "%s / %s" % (counts.get("elements", "?"), counts.get("onScreen", "?")) if counts else ""
            find_text = ("“%s”%s → %s ×%s" % (find.get("name"), " (%s)" % find["role"] if find.get("role") else "",
                                               find.get("match"), find.get("count"))) if find else ""
            out.append("<tr><td>%s</td><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                       % (_rel(g.get("at"), now), _v(g.get("run_id")), _v(g.get("status")), _v(counts.get("site")),
                          E(seen_text), E(find_text)))
        out.append("</table>")
    stuck = status.get("outbox_stuck") or {}
    if stuck.get("count"):
        flagged = _stuck_problems(stuck)
        out.append("<p id='outbox' class='%s'>Outbox: %d message(s) stuck over 10 minutes; %d of the newest %d have no channel or an unconfirmed delivery, the rest wait on a routed channel.</p>" % (
                "bad" if flagged else "muted", stuck["count"], len(flagged),
                len([i for i in stuck.get("items") or [] if isinstance(i, dict)])))
        items = [i for i in stuck.get("items") or [] if isinstance(i, dict)]
        if items:  # VIS G8: drill-down; status.json carries no message body
            out.append("<details><summary>Stuck messages (newest %d of %d)</summary><table><tr><th>message</th>"
                       "<th>to</th><th>kind</th><th>state</th><th>route</th><th>age</th></tr>" % (len(items), stuck["count"]))
            for i in items:
                routes = i.get("routed_by") or []
                out.append("<tr><td><code>%s</code>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                    _v(i.get("id")), " (question %s)" % _qlink(i["question_id"]) if i.get("question_id") else "",
                    _who(people, i.get("to")), _v(i.get("kind")), _v(i.get("state")),
                    E(", ".join(str(r) for r in routes)) if routes else "<span class='bad'>no channel routes this address</span>",
                    E(_span(i["age_minutes"])) if isinstance(i.get("age_minutes"), int) else ""))
            out.append("</table></details>")
    if stuck.get("older_than_48h"):
        out.append("<p class='muted'>Outbox: %d older message(s) still undelivered (over 48 h, counted only).</p>"
                   % stuck["older_than_48h"])
    return out


def team_page(root, agents=None, now=None):
    """Every home's detail block on one page (the pre-VIS-O Team page), kept at /team."""
    now = now or datetime.now(timezone.utc)
    agents = launchctl_list() if agents is None else agents
    people = company(root).get("people", {})
    plist = projects(root)
    authors = _authors(root, plist)
    out = ["<h2>Employees</h2>"]
    members = team(root, agents, now, people)
    for member in members:
        out.extend(_home_block(member, people, authors, now))
    out.extend(_people_and_adapters(root, people, {m["pid"] for m in members}, agents, plist))
    return page("Team details", "\n".join(out), plist,
                caption="For maintainers: every employee's service, sessions, work, questions and outbox on one page.")


def employee_page(root, slug, agents=None, now=None):
    """VIS-O /e/<home>: one employee's detail block; None for an unknown home."""
    now = now or datetime.now(timezone.utc)
    agents = launchctl_list() if agents is None else agents
    people = company(root).get("people", {})
    member = next((m for m in team(root, agents, now, people) if m["slug"] == slug), None)
    if member is None:
        return None
    plist = projects(root)
    body = _home_block(member, people, _authors(root, plist), now)
    return page("%s: details" % member["name"], "\n".join(body), plist,
                caption="For maintainers: service, session, work with IDs, routines, Docket, questions, sessions "
                        "with cost, glances and stuck outbox for one employee.")


def _people_and_adapters(root, people, seen, agents, plist):
    authors = _authors(root, plist)
    out = []
    others = [pid for pid, p in sorted(people.items()) if pid not in seen and p.get("kind") != "system"]
    if others:
        out.append("<h2 id='people'>People</h2><table><tr><th>name</th><th>kind</th><th>role</th><th>projects</th>"
                   "<th>about</th></tr>")
        for pid in others:
            p = people[pid]
            out.append("<tr><td>%s <code>%s</code></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                _v(p.get("name")), E(pid), _v(p.get("kind")), _v(p.get("role")),
                E(", ".join(sorted(authors.get(pid, ())))), _v(p.get("description"))))
        out.append("</table>")
    adapters = sorted(label for label in agents if ADAPTER_LABELS.match(label))
    out.append("<h2 id='adapters'>Adapters (service state only)</h2>")
    out.append("<ul>%s</ul>" % "".join("<li><code>%s</code> loaded, pid %s</li>" % (E(a), _v(agents[a])) for a in adapters)
               if adapters else "<p class='muted'>none loaded</p>")
    return out


def diagnostics_page(root, agents=None, now=None):
    """VIS-O /diagnostics: every home's service and session in one table, then People and Adapters."""
    now = now or datetime.now(timezone.utc)
    agents = launchctl_list() if agents is None else agents
    people = company(root).get("people", {})
    plist = projects(root)
    members = team(root, agents, now, people)
    out = ["<h2 id='services'>Workers</h2><table><tr><th>employee</th><th>label</th><th>loaded</th><th>launchd pid</th>"
           "<th>lock pid</th><th>worker</th><th>session</th><th>status.json written</th></tr>"]
    for m in members:
        svc, sess = m["live"]["service"], m["live"]["session"]
        out.append("<tr><td><a href='/e/%s'>%s</a></td><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s</td>"
                   "<td>%s</td><td>%s%s</td><td>%s</td></tr>" % (
                       quote(m["slug"]), E(m["name"]), _v(svc["label"]), _v(svc["loaded"]), _v(svc["agent_pid"]),
                       _v(svc["lock_pid"]), "alive" if svc["worker_alive"] else "<span class='bad'>not running</span>",
                       E(sess["state"]), " " + E(sess["run"]) if sess.get("run") else "",
                       _rel(sess.get("written_at"), now)))
    out.append("</table><p><a href='/team'>All employees' details on one page</a></p>")
    out.extend(_people_and_adapters(root, people, {m["pid"] for m in members}, agents, plist))
    return page("Diagnostics", "\n".join(out), plist,
                caption="For maintainers: is every worker, session and adapter running, with labels and PIDs.")


# ---------------------------------------------------------------- VIS-O overview (audience: David)

GOALS_CAP = 6  # VIS-O D1(a): at most this many of the manager's open works, highest priority first; the rest are one click away
REF = re.compile(r"\b(question|work|event|message|run|memory|reminder)-[0-9a-f]{8,32}\b")
REF_WORDS = {"event": "(message)", "message": "(message)", "run": "(session)", "memory": "(memory)",
             "reminder": "(reminder)"}


def _by_priority(works):
    """The engine's order (design.md: higher priority first; store.work_list sorts by -priority): higher priority
    first, a missing or non-integer priority last, then the most recently updated first."""
    works = sorted(works, key=lambda w: str(w.get("updated_at") or ""), reverse=True)
    return sorted(works, key=lambda w: (0, -w["priority"]) if isinstance(w.get("priority"), int)
                  and not isinstance(w.get("priority"), bool) else (1, 0))


def _stuck_problems(stuck):
    """VIS-O point 1: stuck messages that are a real problem: no channel routes the address, or the delivery was
    attempted and left uncertain. A pending message on a routed channel (e.g. a pull channel a secretary drains) is
    not a problem; it is counted on the details page only. Only the newest items are exported (STUCK_ITEMS)."""
    items = [i for i in (stuck or {}).get("items") or [] if isinstance(i, dict)]
    return [i for i in items if not i.get("routed_by") or i.get("state") == "uncertain"]


def _lookup(root, members):
    """{work id: title} and {question id: recipient} across every home's shared status and project backlogs."""
    works, questions = {}, {}
    for p in projects(root):
        for row in projectlog.fold(project_entries(root, p)[0])["backlog"].values():
            if row.get("work_id") and row.get("title"):
                works[row["work_id"]] = row["title"]
    for m in members:
        for w in m["status"].get("works") or []:
            if isinstance(w, dict) and w.get("id") and w.get("title"):
                works[w["id"]] = w["title"]
        for q in m["status"].get("questions_open") or []:
            if isinstance(q, dict) and q.get("id"):
                questions[q["id"]] = q.get("to")
    return works, questions


def _find(table, token):
    if token in table:
        return token
    hits = [k for k in table if k.startswith(token)]
    return hits[0] if len(hits) == 1 else None


def _plain(text, lookup, people, canon):
    """VIS-O: waiting_for in plain words: a work ID becomes its title, a question ID "<name>'s answer", any other ID
    a short phrase; every raw ID stays in the tooltip. Escapes everything."""
    works, questions = lookup
    out, last = [], 0
    for m in REF.finditer(text or ""):
        out.append(E(text[last:m.start()]))
        token, kind = m.group(0), m.group(1)
        word = None
        if kind == "work":
            hit = _find(works, token)
            word = "“%s”" % works[hit] if hit else "another work"
        elif kind == "question":
            hit = _find(questions, token)
            word = "%s's answer" % _name(people, canon(questions[hit])) if hit else "an answer to a question"
        out.append("<span title='%s'>%s</span>" % (E(token), E(word or REF_WORDS.get(kind, "(record)"))))
        last = m.end()
    out.append(E((text or "")[last:]))
    return "".join(out)


def _manager(members, canon):
    """VIS-O D1: the manager is the employee the homes' runtime limit_notify names (routines E2's source, read here
    from runtime.state.json's effective settings), resolved through the company aliases. None when no home names
    another home's employee."""
    votes = {}
    by_pid = {m["pid"]: m for m in members if m["pid"]}
    for m in members:
        target = canon(((m["runtime"].get("effective") or {}).get("limit_notify")))
        if target in by_pid and target != m["pid"]:
            votes[target] = votes.get(target, 0) + 1
    if not votes:
        return None
    best = sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(best) > 1 and best[0][1] == best[1][1]:
        return None  # ambiguous: say so rather than guess
    return by_pid[best[0][0]]


def _human(members, people):
    """The human the Docket is for: the person the homes' Dockets name, else the company's only human."""
    named = next((m["status"]["docket"].get("person") for m in members
                  if isinstance(m["status"].get("docket"), dict) and m["status"]["docket"].get("person")), None)
    if named:
        return named
    humans = [pid for pid, p in people.items() if p.get("kind") == "human"]
    return humans[0] if len(humans) == 1 else None


def _asks(member, human, canon):
    """One home's open asks of the human: its Docket items, or (a home without a Docket, e.g. no default human
    configured) its open questions addressed to that human, with the works they hold up and no delivery facts."""
    if not human:
        return []
    status = member["status"]
    dock = status.get("docket")
    if isinstance(dock, dict):
        return [i for i in dock.get("items") or [] if isinstance(i, dict)] if dock.get("person") == human else []
    works = [w for w in status.get("works") or [] if isinstance(w, dict)]
    out = []
    for q in status.get("questions_open") or []:
        if isinstance(q, dict) and canon(q.get("to")) == human:
            out.append(dict(question_id=q.get("id"), summary=q.get("first_line"), asked_at=q.get("asked_at"),
                            ack=None, routes=None,
                            works=[w for w in works if q.get("id") in (w.get("waiting_question_ids") or [])]))
    return out


def _how_to_answer(item):
    """VIS-O point 2: one short phrase saying how the human answers a Docket ask, from its delivery route."""
    if item.get("ack") is None:
        return "delivery not known (this home publishes no Docket)"
    routes = [str(r) for r in item.get("routes") or []]
    if not str(item.get("ack") or "").startswith("delivered"):
        return "not delivered to you yet: no channel carries it" if not routes else \
            "not delivered to you yet (it waits on %s)" % ", ".join(routes)
    if any("telegram" in r for r in routes):
        return "reply on Telegram"
    if any("codex-desktop" in r for r in routes):
        return "reply in the Codex chat"
    return "reply through %s" % (", ".join(routes) or "the channel it reached you on")


def problems(members, agents):
    """VIS-O hard rule: each real problem as (plain sentence, link to its detail). Empty when all is well."""
    out = []
    for m in members:
        name, href = m["name"], "/e/%s" % quote(m["slug"])
        svc, sess = m["live"]["service"], m["live"]["session"]
        if svc["loaded"] is False and not svc["worker_alive"]:
            out.append(("%s's worker is not loaded and not running." % name, href + "#service"))
        elif not svc["worker_alive"]:
            out.append(("%s's worker is not running." % name, href + "#service"))
        elif svc.get("mismatch"):
            out.append(("%s's worker looks inconsistent: %s." % (name, svc["mismatch"]), href + "#service"))
        if sess["state"] == "suspect":
            out.append(("%s's session has run longer than its timeout." % name, href + "#service"))
        elif sess["state"] == "no status.json":
            out.append(("%s has not published its status yet." % name, href + "#service"))
        elif sess["state"].startswith("stale") and svc["worker_alive"]:
            out.append(("%s's status is out of date." % name, href + "#service"))
        if sess.get("paused"):
            reason = (sess["paused"] or {}).get("reason") if isinstance(sess["paused"], dict) else None
            out.append(("%s is paused%s." % (name, " (%s)" % reason if reason else ""), href + "#service"))
        for w in m["status"].get("works") or []:
            r = w.get("routine") if isinstance(w, dict) else None
            if isinstance(r, dict) and r.get("quarantined"):
                out.append(("%s's %s routine is quarantined and needs attention." % (name, r.get("kind") or "?"),
                            href + "#routines"))
        for f in m["status"].get("routine_flags") or []:
            if isinstance(f, dict) and f.get("severity") == "error":
                out.append(("%s has a routine problem (%s)." % (name, f.get("code") or "?"), href + "#routines"))
        stuck = m["status"].get("outbox_stuck") or {}
        bad = _stuck_problems(stuck)
        noroute = [i for i in bad if not i.get("routed_by")]
        unsure = [i for i in bad if i.get("routed_by")]
        more = len([i for i in stuck.get("items") or [] if isinstance(i, dict)]) < (stuck.get("count") or 0)
        if noroute:
            out.append(("%s has %s%d message%s that no channel can deliver." % (
                name, "at least " if more else "", len(noroute), "" if len(noroute) == 1 else "s"), href + "#outbox"))
        if unsure:
            out.append(("%s has %s%d message%s whose delivery was not confirmed." % (
                name, "at least " if more else "", len(unsure), "" if len(unsure) == 1 else "s"), href + "#outbox"))
    for label in sorted(agents):
        if ADAPTER_LABELS.match(label) and agents[label] is None:
            short = label.split(".")[2] if label.count(".") >= 2 else label
            out.append(("The %s adapter is loaded but not running." % short, "/diagnostics#adapters"))
    return out


def _desk_link(desk, member, item):
    """#25 D3: an "answer here" link to the separate answer desk, only when one is configured."""
    qid = str(item.get("question_id") or "")
    if not desk or not re.match(r"^question-[0-9a-f]{32}$", qid):
        return ""
    return " <a href='http://%s:%d/q/%s/%s'>Answer here</a>." % (BIND, int(desk), quote(member["slug"]), quote(qid))


def overview_page(root, agents=None, now=None, desk=None):
    """VIS-O /: for David. Problems (only when there are any), what needs him, goals, one line per employee."""
    now = now or datetime.now(timezone.utc)
    agents = launchctl_list() if agents is None else agents
    state = company(root)
    people = state.get("people", {})
    canon = canonical(state)
    plist = projects(root)
    members = team(root, agents, now, people)
    lookup = _lookup(root, members)
    out = []
    found = problems(members, agents)
    if found:
        out.append("<h2>Problems</h2><ul>%s</ul>" % "".join(
            "<li class='bad'><a href='%s'>%s</a></li>" % (E(href), E(text)) for text, href in found))
    human = _human(members, people)
    asks = []
    for m in members:
        asks.extend((m, item) for item in _asks(m, human, canon))
    asks.sort(key=lambda mi: str(mi[1].get("asked_at") or ""))
    out.append("<h2>Needs you%s</h2>" % (" (%d)" % len(asks) if asks else ""))
    if asks:
        rows = []
        for m, item in asks:
            holds = [w.get("title") for w in item.get("works") or [] if isinstance(w, dict) and w.get("title")]
            rows.append("<li><b>%s</b> asks: <span title='%s'>%s</span> — asked %s%s. <span class='muted'>"
                        "How to answer: %s.</span>%s</li>" % (
                            E(m["name"]), E(str(item.get("question_id") or "")), _v(item.get("summary")),
                            _rel(item.get("asked_at"), now),
                            " — holds up: " + E("; ".join(holds)) if holds else "", E(_how_to_answer(item)),
                            _desk_link(desk, m, item)))
        out.append("<ul>%s</ul>" % "".join(rows))
    else:
        out.append("<p>Nothing is waiting on %s.</p>" % E(_name(people, human) if human else "you"))
    manager = _manager(members, canon)
    if manager is None:
        out.append("<h2>Goals</h2><p class='muted'>No manager resolved: no home's runtime names another employee "
                   "to report to.</p>")
    else:
        goals = [w for w in manager["status"].get("works") or [] if isinstance(w, dict) and not w.get("routine")]
        goals = _by_priority(goals)
        out.append("<h2>Goals</h2><p class='muted'>%s's open work, top %d by priority.</p>" % (
            E(manager["name"]), GOALS_CAP))
        rows = []
        for w in goals[:GOALS_CAP]:
            rows.append("<li><span title='%s'>%s</span> — %s — updated %s</li>" % (
                E(str(w.get("id") or "")), _v(w.get("title")), _status_words(w, lookup, people, canon),
                _rel(w.get("updated_at"), now)))
        out.append("<ul>%s</ul>" % "".join(rows) if rows else "<p>No open goals.</p>")
        if len(goals) > GOALS_CAP:
            out.append("<p class='muted'>%d more on <a href='/e/%s#work'>%s's details</a>.</p>" % (
                len(goals) - GOALS_CAP, quote(manager["slug"]), E(manager["name"])))
    out.append("<h2>Team</h2><ul>")
    bad_homes = {href.split("#")[0] for _, href in found}
    for m in members:
        works = [w for w in m["status"].get("works") or [] if isinstance(w, dict) and not w.get("routine")]
        waiting = [w for w in works if w.get("status") == "waiting"]
        on_human = len(_asks(m, human, canon))
        ready = _by_priority(w for w in works if w.get("status") == "ready")
        sess = m["live"]["session"]["state"]
        href = "/e/%s" % quote(m["slug"])
        word = ("<span class='bad'>needs attention</span>" if href in bad_homes else
                "working now" if sess == "running" else "idle" if sess == "idle" else E(sess))
        role = (m["profile"].get("role") or m["status"].get("role") or "").split(" — ")[0].split(",")[0]
        out.append("<li><a href='%s'>%s</a>%s — %s — %d open, %d waiting%s%s</li>" % (
            href, E(m["name"]), " <span class='muted'>(%s)</span>" % E(role) if role else "", word, len(works),
            len(waiting), " (%d on %s)" % (on_human, E(_name(people, human))) if on_human else "",
            " — next: " + E(ready[0].get("title") or "") if ready else ""))
    out.append("</ul>")
    if plist:
        out.append("<p>David's direction: %s · <a href='/projects'>Projects</a> · <a href='/diagnostics'>Diagnostics</a>"
                   "</p>" % " · ".join("<a href='/p/%s/direction'>%s</a>" % (quote(p), E(p)) for p in plist))
    return page("Overview", "\n".join(out), plist,
                caption="For David: what needs you, where the goals stand, and who is doing what. "
                        "Details and diagnostics are one click away.")


def _status_words(work, lookup, people, canon):
    if work.get("status") == "ready":
        return "in progress"
    if work.get("status") != "waiting":
        return E(work.get("status") or "?")
    text = work.get("waiting_for") or ""
    return "waiting: " + _plain(text, lookup, people, canon) if text else "waiting"


def projects_page(root):
    plist = projects(root)
    rows = "".join("<li><a href='/p/%s'>%s</a> — <a href='/p/%s/direction'>David's direction</a></li>"
                   % (quote(p), E(p), quote(p)) for p in plist)
    return page("Projects", "<ul>%s</ul>" % rows if rows else "<p class='muted'>No projects.</p>", plist,
                caption="For everyone: each project's open work, conversations, board and memory.")


def direction_page(root, project):
    """VIS-O D1(b): David's own direction for a project: the "Standing direction" section of the project memory view
    (current intents and their claims), without lessons, history or sources."""
    entries, texts = project_entries(root, project)

    def excerpt(entry_id):
        text = texts.get(entry_id)
        return None if text is None else text.encode("utf-8")
    text = projectview.render(entries, project, excerpt)
    start = text.find("## Standing direction")
    end = text.find("\n## ", start + 1) if start >= 0 else -1
    part = text[start:end if end >= 0 else len(text)] if start >= 0 else "No standing direction recorded."
    return page("%s: David's direction" % project, _tabs(project, "direction") + "<pre>%s</pre>" % E(part),
                projects(root), caption="For everyone: what David asked for in this project, in his own words.")


def _about(key, titles):
    """The "about" text of a thread key (VIS G-a1): its works' titles, its thread ID, or "(no work id)"."""
    if key.startswith("w:"):
        return ", ".join(titles.get(w) or w for w in key.split(":", 2)[-1].split("+"))
    if key.startswith("t:"):
        return key[2:]
    return "(no work id)"


THREADS_CAPTION = "For David or a reviewer: what was said about a piece of work, both directions in time order."


def threads_page(root, project, now=None):
    now = now or datetime.now(timezone.utc)
    entries, texts = project_entries(root, project)
    people = company(root).get("people", {})
    groups = threads(messages(entries, texts, canonical(company(root))))
    if not groups:
        return page("%s: threads" % project, _tabs(project, "threads") +
                    "<p class='muted'>No captured messages in this project.</p>", projects(root), caption=THREADS_CAPTION)
    titles = work_titles(root, entries)
    rows = sorted(groups.items(), key=lambda kv: kv[1][-1]["at"] or "", reverse=True)
    out = [_tabs(project, "threads"), "<table><tr><th>last</th><th>messages</th><th>between</th><th>about</th></tr>"]
    for key, msgs in rows:
        ids = sorted({str(m["sender"]) for m in msgs} | {str(m["to"]) for m in msgs if m["to"]},
                     key=lambda pid: (_name(people, pid).lower(), pid))
        out.append("<tr><td>%s</td><td>%d</td><td>%s</td><td><a href='/p/%s/thread/%s' title='%s'>%s</a></td></tr>" % (
            _rel(msgs[-1]["at"], now), len(msgs), ", ".join(_who(people, pid) for pid in ids), quote(project),
            quote(key, safe=""), E(key), E(_about(key, titles))))
    out.append("</table>")
    return page("%s: threads" % project, "\n".join(out), projects(root), caption=THREADS_CAPTION)


def _page_number(value, last):
    """VIS G10: the requested page (1 = oldest messages), clamped to 1..last; the newest page by default."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return last
    return min(max(number, 1), last)


def thread_page(root, project, key, page_no=None, now=None):
    now = now or datetime.now(timezone.utc)
    entries, texts = project_entries(root, project)
    msgs = threads(messages(entries, texts, canonical(company(root)))).get(key)
    if msgs is None:
        return None
    people = company(root).get("people", {})
    titles = work_titles(root, entries)
    last = max(1, -(-len(msgs) // THREAD_PAGE))
    number = _page_number(page_no, last)
    shown = msgs[(number - 1) * THREAD_PAGE:number * THREAD_PAGE]
    base = "/p/%s/thread/%s" % (quote(project), quote(key, safe=""))

    def link(n, text):
        return "<a href='%s?page=%d'>%s</a>" % (base, n, E(text))
    nav = ""
    if last > 1:  # VIS G10: big conversations are paged, newest page first
        nav = "<p>Page %d of %d (%d messages, %d per page) — %s</p>" % (
            number, last, len(msgs), THREAD_PAGE, " ".join(filter(None, (
                link(1, "oldest") if number > 1 else "", link(number - 1, "older") if number > 1 else "",
                link(number + 1, "newer") if number < last else "", link(last, "newest") if number < last else ""))))
    out = [_tabs(project, "threads"), "<p>About: %s</p>" % E(_about(key, titles)), nav]
    for m in shown:
        out.append("<div class='%s'><p><b>%s</b> → %s — %s — auth %s — via %s — captured by %s%s</p>%s</div>" % (
            "in" if m["role"] != "out" else "out", _who(people, m["sender"]), _who(people, m["to"]) if m["to"] else "?",
            _rel(m["at"], now), _v(m["auth"]), _v(m["source"]), _v(m["captured_by"]),
            " — question %s" % E(m["question"]) if m["question"] else "",
            "<p class='muted'>(redacted)</p>" if m["text"] is None else "<pre>%s</pre>" % E(m["text"])))
    out.append(nav)
    return page("%s: thread %s" % (project, key), "\n".join(out), projects(root), caption=THREADS_CAPTION)


def board_page(root, project, now=None):
    now = now or datetime.now(timezone.utc)
    entries, _ = project_entries(root, project)
    people = company(root).get("people", {})
    state = projectlog.fold(entries)
    bodies = {e["id"]: (e.get("body") or {}) for e in entries if e["kind"] == "post"}
    out = [_tabs(project, "board"), "<h2>Backlog</h2>"]
    backlog = sorted(state["backlog"].values(), key=lambda r: (r.get("priority") or 99, r["seq"]))
    if backlog:
        out.append("<table><tr><th>owner</th><th>status</th><th>pri</th><th>title</th><th>waiting for</th>"
                   "<th>blocked by</th><th>updated</th><th>work</th></tr>")
        for r in backlog:
            out.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                       "<td><code>%s</code></td></tr>" % (
                           _who(people, r.get("owner")), _v(r.get("status")), _v(r.get("priority")), _v(r.get("title")),
                           _v(r.get("waiting_for")), E("; ".join(r.get("blocked_by") or [])), _rel(r.get("at"), now),
                           _v(r.get("work_id"))))
        out.append("</table>")
    else:
        out.append("<p class='muted'>Empty.</p>")
    out.append("<h2>Posts</h2>")
    posts = sorted(state["posts"].values(), key=lambda p: p["seq"], reverse=True)
    for p in posts:
        text = "(redacted)" if p["redacted"] else (bodies.get(p["id"]) or {}).get("body", "")
        out.append("<div><p><b>%s</b> — %s — %s%s</p><pre>%s</pre></div>" % (
            _v(p["topic"]), _who(people, p["author"]), _rel(p["at"], now), " (reply to %s)" % E(p["reply_to"]) if p.get("reply_to") else "",
            E(text)))
    if not posts:
        out.append("<p class='muted'>None.</p>")
    return page("%s: board" % project, "\n".join(out), projects(root),
                caption="For the team: the published backlog (open and closed) and Board posts.")


OPEN_STATUSES = ("ready", "waiting")


def work_page(root, project, now=None):
    """VIS G1: the project's open published works (ready/waiting backlog rows), grouped by owner name.

    Built only from the project log's backlog projection, the same rows the board shows; unpublished work appears
    only on its owner's details page (/e/<home>). Closed rows (done/cancelled) are counted and left to the board."""
    now = now or datetime.now(timezone.utc)
    entries, _ = project_entries(root, project)
    rows = list(projectlog.fold(entries)["backlog"].values())
    people = company(root).get("people", {})
    open_rows = [r for r in rows if r.get("status") in OPEN_STATUSES]
    closed = len(rows) - len(open_rows)
    groups = {}
    for r in open_rows:
        groups.setdefault(r.get("owner"), []).append(r)
    members = team(root, {}, now, people)
    lookup = _lookup(root, members)
    canon = canonical(company(root))
    out = [_tabs(project, ""), "<p class='muted'>Open works published to this project (ready or waiting), by owner. "
           "Updated = age of the owner's latest projection of that work. Hover a title for its work ID.</p>"]
    if not open_rows:
        out.append("<p class='muted'>No open published work.</p>")
    for owner in sorted(groups, key=lambda o: (_name(people, o).lower(), o or "")):
        mine = sorted(groups[owner], key=lambda r: (r.get("priority") if isinstance(r.get("priority"), int) else 99,
                                                    r["seq"]))
        out.append("<h2>%s (%d)</h2>" % (E(_name(people, owner)), len(mine)))
        out.append("<table><tr><th>status</th><th>pri</th><th>title</th><th>waiting for</th><th>blocked by</th>"
                   "<th>updated</th></tr>")
        for r in mine:  # VIS-O: IDs move into tooltips; waiting_for in plain words
            out.append("<tr><td>%s</td><td>%s</td><td><span title='%s'>%s</span></td><td>%s</td><td>%s</td><td>%s</td>"
                       "</tr>" % (_v(r.get("status")), _v(r.get("priority")), _v(r.get("work_id")), _v(r.get("title")),
                                  _plain(r.get("waiting_for") or "", lookup, people, canon),
                                  "; ".join(_plain(b, lookup, people, canon) for b in r.get("blocked_by") or []),
                                  E(_age(r.get("at"), now))))
        out.append("</table>")
    out.append("<p class='muted'>%d closed (done or cancelled) on the <a href=\"/p/%s/board\">board</a>.</p>"
               % (closed, quote(project)))
    return page("%s: open work" % project, "\n".join(out), projects(root),
                caption="For David and the manager: what is open in this project, who owns it and what it waits on.")


def memory_page(root, project):
    entries, texts = project_entries(root, project)

    def excerpt(entry_id):
        text = texts.get(entry_id)
        return None if text is None else text.encode("utf-8")
    text = projectview.render(entries, project, excerpt)
    return page("%s: project memory" % project, "%s<pre>%s</pre>\n<details><summary>Sources</summary>%s</details>" % (
        _tabs(project, "memory"), E(text), sources_table(root, project, entries, texts)), projects(root),
        caption="For auditors: intents, lessons, history and source coverage (sources collapsed).")


def sources_table(root, project, entries, texts):
    """VIS G5: the sources the view counts ("Sources: N"), newest first, each linked to the thread showing it.

    Same set as projectview's count (projectlog.fold over active entries). Shows provenance and coverage only; the
    text stays on the thread page, where a redacted source reads "(redacted)"."""
    state = projectlog.fold(entries)
    people = company(root).get("people", {})
    now = datetime.now(timezone.utc)
    canon = canonical(company(root))
    where = {}
    for key, msgs in threads(messages(entries, texts, canon)).items():
        for m in msgs:
            for entry_id in m["entries"]:
                where[entry_id] = key
    origins = {e["id"]: ((e.get("body") or {}).get("origin") or {}) for e in entries if e["kind"] == "source"}
    rows = sorted(state["sources"].values(), key=lambda s: s["seq"], reverse=True)
    out = ["<h2>Sources (%d)</h2>" % len(rows)]
    if not rows:
        out.append("<p class='muted'>None.</p>")
        return "\n".join(out)
    out.append("<table><tr><th>source</th><th>seq</th><th>at</th><th>from</th><th>role</th><th>auth</th>"
               "<th>coverage</th><th>thread</th></tr>")
    for s in rows:
        origin = origins.get(s["id"], {})
        n = len(state["coverage"].get(s["id"]) or [])
        key = where.get(s["id"])
        link = "<a href='/p/%s/thread/%s'>thread</a>" % (quote(project), quote(key, safe="")) if key else "—"
        out.append("<tr id='%s'><td><code>%s</code>%s</td><td>%d</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                   "<td>%s</td><td>%s</td></tr>" % (
                       E(s["id"]), E(s["id"]), " <span class='muted'>(redacted)</span>" if s["redacted"] else "",
                       s["seq"], _rel(origin.get("occurred_at"), now), _who(people, canon(origin.get("sender"))),
                       _v(s["role"]),
                       _v((s.get("auth") or {}).get("channel")),
                       "considered ×%d" % n if n else "<span class='bad'>not considered</span>", link))
    out.append("</table>")
    return "\n".join(out)


# ---------------------------------------------------------------- server


def route(root, path, agents=None, desk=None):
    """(status, html) for one GET path."""
    split = urlsplit(path)
    parts = [unquote(p) for p in split.path.split("/") if p]
    if not parts:
        return 200, overview_page(root, agents, desk=desk)
    if parts == ["team"]:
        return 200, team_page(root, agents)
    if parts == ["diagnostics"]:
        return 200, diagnostics_page(root, agents)
    if parts == ["projects"]:
        return 200, projects_page(root)
    if len(parts) == 2 and parts[0] == "e":
        body = employee_page(root, parts[1], agents)
        if body is not None:
            return 200, body
    if len(parts) == 2 and parts[0] == "p" and PROJECT_NAME.match(parts[1]) and parts[1] in projects(root):
        return 200, work_page(root, parts[1])  # VIS-O D2: a project lands on its open work
    if len(parts) >= 3 and parts[0] == "p" and PROJECT_NAME.match(parts[1]) and parts[1] in projects(root):
        project, what = parts[1], parts[2]
        if what == "direction" and len(parts) == 3:
            return 200, direction_page(root, project)
        if what == "threads" and len(parts) == 3:
            return 200, threads_page(root, project)
        if what == "thread" and len(parts) == 4:
            body = thread_page(root, project, parts[3], (parse_qs(split.query).get("page") or [None])[-1])
            if body is not None:
                return 200, body
        if what == "work" and len(parts) == 3:
            return 200, work_page(root, project)
        if what == "board" and len(parts) == 3:
            return 200, board_page(root, project)
        if what == "memory" and len(parts) == 3:
            return 200, memory_page(root, project)
    return 404, page("Not found", "<p>No such page.</p>", projects(root))


def make_handler(root, desk=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Lookout/1"

        def _send(self, code, body, head_only=False):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
            self.end_headers()
            if not head_only:
                self.wfile.write(data)

        def _route(self):
            try:
                return route(root, self.path, desk=desk)
            except Exception as exc:  # noqa: BLE001 - never a traceback page
                return 500, page("Error", "<p>%s</p>" % E(type(exc).__name__))

        def do_GET(self):
            self._send(*self._route())

        def do_HEAD(self):
            self._send(*self._route(), head_only=True)

        def _refuse(self):
            self._send(405, page("Method not allowed", "<p>Lookout is read-only.</p>"))

        do_POST = do_PUT = do_DELETE = do_PATCH = _refuse

        def log_message(self, fmt, *args):  # quiet by default
            pass
    return Handler


def serve(root, port=DEFAULT_PORT, bind=BIND, ready=None, desk=None):
    if bind != BIND:
        raise ValueError("Lookout binds to 127.0.0.1 only.")
    server = ThreadingHTTPServer((BIND, port), make_handler(str(Path(root).resolve()), desk=desk))
    if ready is not None:
        ready(server)
    try:
        server.serve_forever()
    finally:
        server.server_close()
