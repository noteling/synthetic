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
from urllib.parse import quote, unquote, urlsplit

from . import projectlog, projectview, roster, sharedstatus

BIND = "127.0.0.1"
DEFAULT_PORT = 8765
SUSPECT_MARGIN = 300  # C2: seconds past the effective timeout before a "running" session is labelled suspect
ADAPTER_LABELS = re.compile(r"^com\.synthetics\.(adapter\..+|fieldpost.*|fieldstone.*|telegram.*|relay.*)$")
HEADER = re.compile(r"^([a-z][a-z0-9-]*): (.*)$")
PROJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
HOME_FILES = ("runtime.state.json", "worker.lock")


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
                   captured_by=origin.get("home"), text=shown, redacted=text is None and e["id"] in texts)
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


def page(title, body, root_projects=()):
    nav = '<nav><a href="/">Team</a>' + "".join(
        '<a href="/p/%s/threads">%s threads</a><a href="/p/%s/board">board</a><a href="/p/%s/memory">memory</a>'
        % (quote(p), E(p), quote(p), quote(p)) for p in root_projects) + "</nav>"
    return ("<!doctype html><html><head><meta charset='utf-8'><title>%s</title><style>%s</style></head><body>%s"
            "<h1>%s</h1>%s<p class='muted'>Lookout: generated %s from shared records only. Read-only.</p></body></html>"
            % (E(title), STYLE, nav, E(title), body, E(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"))))


def _v(value):
    return "" if value is None else E(str(value))


def _age(ts, now):
    at = _parse(ts)
    if at is None:
        return ""
    minutes = max(0, int((now - at).total_seconds() // 60))
    hours, mins = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    return "%dd %dh" % (days, hours) if days else ("%dh %dm" % (hours, mins) if hours else "%dm" % mins)


def _name(people, pid):
    profile = people.get(pid) or {}
    return profile.get("name") or pid or "?"


def label_owner(people, label):
    """The person whose default worker label (install.worker_label) is ``label``, else None."""
    for pid in people:
        if label == "com.synthetics.worker." + hashlib.sha256(pid.encode()).hexdigest()[:16]:
            return pid
    return None


def team_page(root, agents=None, now=None):
    now = now or datetime.now(timezone.utc)
    agents = launchctl_list() if agents is None else agents
    state = company(root)
    people = state.get("people", {})
    plist = projects(root)
    authors = {}
    for p in plist:
        entries, _ = project_entries(root, p)
        for e in entries:
            authors.setdefault(e.get("author"), set()).add(p)
            home = ((e.get("body") or {}).get("origin") or {}).get("home") if e["kind"] == "source" else None
            if home:
                authors.setdefault(home, set()).add(p)
    out = ["<h2>Employees</h2>"]
    seen = set()
    for home in homes(root):
        live = liveness(home, agents, now)
        status = live["status"] or {}
        pid = status.get("employee") or label_owner(people, live["service"]["label"])
        seen.add(pid)
        profile = people.get(pid) or {}
        svc, sess = live["service"], live["session"]
        out.append("<h3>%s <span class='muted'>(%s)</span></h3>" % (E(profile.get("name") or status.get("name") or home.name),
                                                                   E(profile.get("role") or status.get("role") or "")))
        out.append("<p>Home <code>%s</code>; employee <code>%s</code>; projects: %s</p>" % (
            E(home.name), _v(pid), E(", ".join(sorted(authors.get(pid, ()))) or "none (no shared project log yet)")))
        svc_text = ("<span class='ok'>loaded</span>" if svc["loaded"] else
                    "<span class='bad'>not loaded</span>" if svc["loaded"] is False else "unknown")
        svc_text += " — label %s, launchd pid %s, worker lock pid %s (%s)" % (
            _v(svc["label"]), _v(svc["agent_pid"]), _v(svc["lock_pid"]), "alive" if svc["worker_alive"] else "not running")
        if svc.get("mismatch"):
            svc_text += " <span class='bad'>mismatch: %s</span>" % E(svc["mismatch"])
        sess_text = E(sess["state"])
        if sess.get("run"):
            sess_text += " — %s since %s (%s)" % (E(sess["run"]), _v(sess["started_at"]), _age(sess["started_at"], now))
        if sess["state"] == "suspect":
            sess_text = "<span class='bad'>%s</span> — longer than the timeout (%ss) plus %ss" % (
                sess_text, _v(sess.get("timeout_s")), SUSPECT_MARGIN)
        if sess.get("paused"):
            sess_text += " — paused: %s" % E(json.dumps(sess["paused"], sort_keys=True))
        if sess.get("written_at"):
            sess_text += " <span class='muted'>(status.json written %s)</span>" % E(sess["written_at"])
        out.append("<table><tr><th>1. Service</th><td>%s</td></tr><tr><th>2. Session</th><td>%s</td></tr>"
                   % (svc_text, sess_text))
        works = status.get("works") or []
        waiting = [w for w in works if w.get("status") == "waiting"]
        out.append("<tr><th>3. Waiting on</th><td>%s</td></tr></table>" % ("<br>".join(
            "%s — %s%s%s" % (E(w.get("title") or w["id"]), E(w.get("waiting_for") or "?"),
                             " (wake %s)" % E(w["wake_at"]) if w.get("wake_at") else "",
                             " [%s]" % E(", ".join(w.get("waiting_question_ids") or [])) if w.get("waiting_question_ids") else "")
            for w in waiting) or "nothing"))
        if works:
            out.append("<p><b>Work</b></p><table><tr><th>status</th><th>pri</th><th>title</th><th>waiting for</th>"
                       "<th>published</th><th>id</th></tr>")
            for w in works:
                out.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td><code>%s</code></td></tr>" % (
                    _v(w.get("status")), _v(w.get("priority")), _v(w.get("title")), _v(w.get("waiting_for")),
                    _v(w.get("published")), _v(w.get("id"))))
            out.append("</table>")
        dock = status.get("docket")
        if dock:
            out.append("<p><b>Waiting on %s</b> (%d open)</p>" % (E(_name(people, dock.get("person"))), dock.get("count", 0)))
            if dock.get("items"):
                out.append("<table><tr><th>question</th><th>asked</th><th>ack</th><th>now</th><th>blocks</th></tr>")
                for item in dock["items"]:
                    out.append("<tr><td><code>%s</code> %s</td><td>%s (%s)</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                        _v(item.get("question_id")), _v(item.get("summary")), _v(item.get("asked_at")),
                        _age(item.get("asked_at"), now), _v(item.get("ack")), _v(item.get("hop")),
                        E("; ".join(w.get("title") or w.get("id") for w in item.get("works") or []))))
                out.append("</table>")
        questions = status.get("questions_open") or []
        if questions:
            out.append("<p><b>Open questions</b></p><ul>" + "".join(
                "<li><code>%s</code> to %s, %s ago: %s%s</li>" % (
                    _v(q.get("id")), E(_name(people, q.get("to"))), _age(q.get("asked_at"), now), _v(q.get("first_line")),
                    " — %s" % E(q["hop"]) if q.get("hop") else "") for q in questions) + "</ul>")
        runs = (status.get("runs") or [])[:5]
        if runs:
            out.append("<p><b>Recent sessions</b></p><table><tr><th>run</th><th>status</th><th>started</th>"
                       "<th>finished</th><th>cost</th><th>events</th><th>failed tools</th></tr>")
            for r in runs:
                cost = "$%.2f" % r["cost"] if isinstance(r.get("cost"), (int, float)) else "?"
                out.append("<tr><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                           % (_v(r.get("id")), _v(r.get("status")), _v(r.get("started_at")), _v(r.get("finished_at")),
                              E(cost), _v(r.get("events")), _v(r.get("failed_tool_count"))))
            out.append("</table>")
        glances = (status.get("glances") or [])[:5]
        if glances:
            out.append("<p><b>Recent glances</b> <span class='muted'>(screen_glance: counts and find only)</span></p>"
                       "<table><tr><th>at</th><th>run</th><th>status</th><th>site</th><th>elements / on screen</th>"
                       "<th>find</th></tr>")
            for g in glances:
                counts, find = g.get("counts") or {}, g.get("find") or {}
                seen_text = "%s / %s" % (counts.get("elements", "?"), counts.get("onScreen", "?")) if counts else ""
                find_text = ("“%s”%s → %s ×%s" % (find.get("name"), " (%s)" % find["role"] if find.get("role") else "",
                                                   find.get("match"), find.get("count"))) if find else ""
                out.append("<tr><td>%s</td><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                           % (_v(g.get("at")), _v(g.get("run_id")), _v(g.get("status")), _v(counts.get("site")),
                              E(seen_text), E(find_text)))
            out.append("</table>")
        if (status.get("outbox_stuck") or {}).get("count"):
            out.append("<p class='bad'>Outbox: %d message(s) stuck over 10 minutes.</p>" % status["outbox_stuck"]["count"])
    others = [pid for pid, p in sorted(people.items()) if pid not in seen and p.get("kind") != "system"]
    if others:
        out.append("<h2>People</h2><table><tr><th>name</th><th>kind</th><th>role</th><th>projects</th><th>about</th></tr>")
        for pid in others:
            p = people[pid]
            out.append("<tr><td>%s <code>%s</code></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                _v(p.get("name")), E(pid), _v(p.get("kind")), _v(p.get("role")),
                E(", ".join(sorted(authors.get(pid, ())))), _v(p.get("description"))))
        out.append("</table>")
    adapters = sorted(label for label in agents if ADAPTER_LABELS.match(label))
    out.append("<h2>Adapters (service state only)</h2>")
    out.append("<ul>%s</ul>" % "".join("<li><code>%s</code> loaded, pid %s</li>" % (E(a), _v(agents[a])) for a in adapters)
               if adapters else "<p class='muted'>none loaded</p>")
    return page("Team", "\n".join(out), plist)


def threads_page(root, project):
    entries, texts = project_entries(root, project)
    groups = threads(messages(entries, texts, canonical(company(root))))
    if not groups:
        return page("%s: threads" % project, "<p class='muted'>No captured messages in this project.</p>", projects(root))
    rows = sorted(groups.items(), key=lambda kv: kv[1][-1]["at"] or "", reverse=True)
    out = ["<table><tr><th>last</th><th>messages</th><th>between</th><th>about</th></tr>"]
    for key, msgs in rows:
        people = sorted({str(m["sender"]) for m in msgs} | {str(m["to"]) for m in msgs if m["to"]})
        about = key.split(":", 2)[-1] if not key.startswith("p:") else ""
        out.append("<tr><td>%s</td><td>%d</td><td>%s</td><td><a href='/p/%s/thread/%s'>%s</a></td></tr>" % (
            _v(msgs[-1]["at"]), len(msgs), E(", ".join(people)), quote(project), quote(key, safe=""),
            E(about or "(no work id)")))
    out.append("</table>")
    return page("%s: threads" % project, "\n".join(out), projects(root))


def thread_page(root, project, key):
    entries, texts = project_entries(root, project)
    msgs = threads(messages(entries, texts, canonical(company(root)))).get(key)
    if msgs is None:
        return None
    out = []
    for m in msgs:
        out.append("<div class='%s'><p><b>%s</b> → %s — %s — auth %s — via %s — captured by %s%s</p>%s</div>" % (
            "in" if m["role"] != "out" else "out", _v(m["sender"]), _v(m["to"] or "?"), _v(m["at"]), _v(m["auth"]),
            _v(m["source"]), _v(m["captured_by"]), " — question %s" % E(m["question"]) if m["question"] else "",
            "<p class='muted'>(redacted)</p>" if m["text"] is None else "<pre>%s</pre>" % E(m["text"])))
    return page("%s: thread %s" % (project, key), "\n".join(out), projects(root))


def board_page(root, project):
    entries, _ = project_entries(root, project)
    state = projectlog.fold(entries)
    bodies = {e["id"]: (e.get("body") or {}) for e in entries if e["kind"] == "post"}
    out = ["<h2>Backlog</h2>"]
    backlog = sorted(state["backlog"].values(), key=lambda r: (r.get("priority") or 99, r["seq"]))
    if backlog:
        out.append("<table><tr><th>owner</th><th>status</th><th>pri</th><th>title</th><th>waiting for</th>"
                   "<th>blocked by</th><th>work</th></tr>")
        for r in backlog:
            out.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td><code>%s</code></td></tr>" % (
                _v(r.get("owner")), _v(r.get("status")), _v(r.get("priority")), _v(r.get("title")),
                _v(r.get("waiting_for")), E("; ".join(r.get("blocked_by") or [])), _v(r.get("work_id"))))
        out.append("</table>")
    else:
        out.append("<p class='muted'>Empty.</p>")
    out.append("<h2>Posts</h2>")
    posts = sorted(state["posts"].values(), key=lambda p: p["seq"], reverse=True)
    for p in posts:
        text = "(redacted)" if p["redacted"] else (bodies.get(p["id"]) or {}).get("body", "")
        out.append("<div><p><b>%s</b> — %s — %s%s</p><pre>%s</pre></div>" % (
            _v(p["topic"]), _v(p["author"]), _v(p["at"]), " (reply to %s)" % E(p["reply_to"]) if p.get("reply_to") else "",
            E(text)))
    if not posts:
        out.append("<p class='muted'>None.</p>")
    return page("%s: board" % project, "\n".join(out), projects(root))


def memory_page(root, project):
    entries, texts = project_entries(root, project)

    def excerpt(entry_id):
        text = texts.get(entry_id)
        return None if text is None else text.encode("utf-8")
    text = projectview.render(entries, project, excerpt)
    return page("%s: project memory" % project, "<pre>%s</pre>" % E(text), projects(root))


# ---------------------------------------------------------------- server


def route(root, path, agents=None):
    """(status, html) for one GET path."""
    parts = [unquote(p) for p in urlsplit(path).path.split("/") if p]
    if not parts:
        return 200, team_page(root, agents)
    if len(parts) >= 3 and parts[0] == "p" and PROJECT_NAME.match(parts[1]) and parts[1] in projects(root):
        project, what = parts[1], parts[2]
        if what == "threads" and len(parts) == 3:
            return 200, threads_page(root, project)
        if what == "thread" and len(parts) == 4:
            body = thread_page(root, project, parts[3])
            if body is not None:
                return 200, body
        if what == "board" and len(parts) == 3:
            return 200, board_page(root, project)
        if what == "memory" and len(parts) == 3:
            return 200, memory_page(root, project)
    return 404, page("Not found", "<p>No such page.</p>", projects(root))


def make_handler(root):
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
                return route(root, self.path)
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


def serve(root, port=DEFAULT_PORT, bind=BIND, ready=None):
    if bind != BIND:
        raise ValueError("Lookout binds to 127.0.0.1 only.")
    server = ThreadingHTTPServer((BIND, port), make_handler(str(Path(root).resolve())))
    if ready is not None:
        ready(server)
    try:
        server.serve_forever()
    finally:
        server.server_close()
