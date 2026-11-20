"""VIS P1 ("Lookout"): the one shared per-home export, <home>/shared/status.json.

The engine (and the MCP server after in-session work/question changes, Codex C1) writes it from the home's own
database. Lookout reads this file and never opens a home database. It holds exactly the fields below; it never
holds a work description or note, memories, views, self notes, run summaries or errors, transcripts, or event and
message bodies (docs/lookout.md has the boundary table). Docket items carry only the question's first line, as
`synthetics docket` does.

Fields (v1; add, never rename):
  v, employee, name, role, written_at, pid,
  session {state running|idle, run, started_at, paused: null|{reason, ...}},
  works [{id, title, status, priority, waiting_for, wake_at, waiting_question_ids, published, updated_at}]
           (not done/cancelled; updated_at added by VIS G7)
  docket (docket.build JSON for the default human, or null with docket_error)
  questions_open [{id, to, first_line, asked_at, hop, hop_via, carries}]   (G3: hop_via = the onward question
           named by the latest hop notice; carries [{question_id, asker}] = the askers' questions this one relays.
           IDs only, never a relayed body)
  runs [{id, status, started_at, finished_at, cost, events, failed_tool_count}]   (newest 10)
  outbox_stuck {count, older_than_48h, items [{id, to, kind, question_id, state, age_minutes, routed_by}]}
           (VIS G8: the newest Store.STUCK_ITEMS stuck items, as in the employee's snapshot; never a body)
  glances [{id, run_id, at, ok, exit, status, counts, find, browser}]   (RW-1, newest 5; already whitelisted by
           glance.py: counts and find summary only, never page text, hit labels or frames)
  clicks [{id, run_id, created_at, updated_at, name, role, browser, host, state, reason, question_id, guard, find,
           result}]   (stage 2, newest 5; whitelisted by click.recent: never tokens, why, labels or frames)
  works[].routine {kind, every_minutes, last_fired_at, last_rearmed_at, quarantined, failures}   (RTN P1, routine
           works only; never the review text)
  routine_flags [{code, severity, work_id, kind, detail?, total?, last_7_days?}]   (RTN P1, routines.flags)
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import click, docket, glance, routines

VERSION = 1
RUNS = 10
FILENAME = "status.json"
PAUSE_KEYS = ("reason", "day", "limit_usd", "cost_usd", "run_id", "kind", "next_try_at", "attempts")  # #98: provider pause
KEEP = object()
STUCK_KEYS = ("id", "to", "kind", "question_id", "state", "age_minutes", "routed_by")  # VIS G8: never the body
GLANCE_KEYS = ("id", "run_id", "at", "ok", "exit", "status", "counts", "find", "browser")


def path(home):
    return Path(home) / "shared" / FILENAME


def _pause(value):
    if not isinstance(value, dict) or not value.get("reason"):
        return None
    return {k: value[k] for k in PAUSE_KEYS if k in value}


def _routine(work):
    """RTN P1: routine works carry their tag summary (never the review text)."""
    r = work.get("routine")
    if not isinstance(r, dict):
        return {}
    return {"routine": dict(kind=r.get("kind"), every_minutes=r.get("every_minutes"),
                            last_fired_at=r.get("last_fired_at"), last_rearmed_at=r.get("last_rearmed_at"),
                            quarantined=bool(r.get("quarantine")), failures=len(r.get("failures") or []))}


def build(store, paused=None, now=None):
    now = now or datetime.now(timezone.utc)
    identity = store.identity()
    runs = store.run_list(RUNS)
    active = next((run for run in runs if run.get("status") == "running"), None)
    works = []
    for work in store.work_list():
        if work.get("status") in ("done", "cancelled"):
            continue
        works.append(dict(id=work["id"], title=work.get("title") or "", status=work.get("status"),
                          priority=work.get("priority"), waiting_for=work.get("waiting_for") or "",
                          wake_at=work.get("wake_at"),
                          waiting_question_ids=list(work.get("waiting_question_ids") or []),
                          published=work.get("publish") or None, updated_at=work.get("updated_at"),
                          **_routine(work)))
    try:
        dock, dock_error = docket.build(store, now=now), None
    except Exception as exc:  # noqa: BLE001 - e.g. no default_human; the rest of the export still matters
        dock, dock_error = None, type(exc).__name__
    questions = [dict(id=q["id"], to=q.get("to"), first_line=docket.summary(q.get("body")),
                      asked_at=q.get("created_at"), hop=store.hop_label(q), hop_via=q.get("hop_via"),
                      carries=[dict(question_id=link.get("question_id"), asker=link.get("asker"))
                               for link in q.get("relays") or []])
                 for q in store.question_list("open")]
    run_rows = []
    for run in runs:
        try:
            failed = store._failed_tools(run).get("failed_tool_count", 0)
        except Exception:  # noqa: BLE001
            failed = None
        run_rows.append(dict(id=run["id"], status=run.get("status"), started_at=run.get("started_at"),
                             finished_at=run.get("finished_at"), cost=run.get("cost"),
                             events=len(run.get("event_ids") or []), failed_tool_count=failed))
    stuck = store.outbox_stuck(now)
    try:
        glances = [{k: g.get(k) for k in GLANCE_KEYS} for g in glance.recent(store)]
    except Exception:  # noqa: BLE001 - an older home without the table; the rest of the export still matters
        glances = []
    try:
        clicks = click.recent(store)
    except Exception:  # noqa: BLE001 - an older home without the table
        clicks = []
    return dict(
        v=VERSION, employee=identity["id"], name=identity.get("name"), role=identity.get("role"),
        written_at=now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), pid=os.getpid(),
        session=dict(state="running" if active else "idle", run=active["id"] if active else None,
                     started_at=active.get("started_at") if active else None, paused=_pause(paused)),
        works=works, docket=dock, docket_error=dock_error, questions_open=questions, runs=run_rows,
        outbox_stuck=dict(count=stuck.get("count", 0), older_than_48h=stuck.get("older_than_48h", 0),
                          items=[{k: item.get(k) for k in STUCK_KEYS} for item in stuck.get("items") or []]),
        glances=glances, clicks=clicks, routine_flags=routines.flags(store.work_list(), now, active is not None))


def read(home):
    """The current export, or None when missing or unreadable."""
    try:
        value = json.loads(path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def write(store, paused=KEEP, now=None):
    """Atomically replace <home>/shared/status.json (0600). Returns True on success, False on any failure.

    paused=KEEP keeps the pause last written by the engine (the MCP server does not know it). A failed write never
    fails the caller: the file is derived and is rewritten at the next session boundary."""
    try:
        if paused is KEEP:
            previous = read(store.home) or {}
            paused = (previous.get("session") or {}).get("paused")
        value = build(store, paused=paused, now=now)
        target = path(store.home)
        target.parent.mkdir(mode=0o700, exist_ok=True)
        tmp = target.with_name(".%s.%d.tmp" % (FILENAME, os.getpid()))
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=1, sort_keys=True)
            handle.write("\n")
        os.chmod(str(tmp), 0o600)
        os.replace(str(tmp), str(target))
        return True
    except Exception:  # noqa: BLE001
        return False
