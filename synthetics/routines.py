"""RTN P1: routines on self-reminders (docs/routines.md).

A routine is an ordinary work record carrying ``work["routine"]``. The clock fires it as any waiting work
(``Store.due_events``); a session follows the kind's guidance (prompts/routines.md) and proves its review
only with MCP ``routine_rearm``, which re-arms the routine at the next anchor-based target. No scheduler.

Pure helpers only: no database access, so doctor (raw rows), sharedstatus and Store share them.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

KINDS = ("hourly-check", "team-review", "dream")
SEED_KINDS = KINDS  # P2: the dream guidance has shipped, so every kind is seedable
DEFAULT_EVERY = {"hourly-check": 60, "team-review": 60, "dream": 1440}
MIN_EVERY = 5
DEFAULT_DREAM_AT = "01:30"
FOLD_IN_MINUTES = 15  # a waiting routine due within this window rides along in any session (routines_due)
REVIEW_MAX = 2048
FAILURE_WINDOW_DAYS = 7
GUIDANCE_FILE = Path(__file__).with_name("prompts") / "routines.md"
WORKING_CONTEXT = "working_context"  # D3: the memory predicate the dream writes; the snapshot shows it on its own
WORKING_CONTEXT_MAX = 4000
WORKING_CONTEXT_KINDS = ("belief", "procedure")  # H3: a derived reflection, never fact


def quarantine_key(work_id, cycle):
    """N3: one notice per quarantined cycle. A repeat or second failure of the same cycle reuses this key; a
    repair (routine_rearm or restore) moves anchor_at, so a fresh quarantine gets a new key."""
    return "routine-quarantine-%s-%s" % (work_id, cycle)


LABELS = {"hourly-check": "hourly check", "team-review": "team review", "dream": "daily dream"}


def quarantine_notice(kind, name):
    """N3/H4: the brief owner-level notice (no model call, no commands or paths): which scheduled review failed,
    that it needs recovery, and that other work continues. Recovery steps live in quarantine_recovery, doctor and
    docs/routines.md."""
    return ("%s's scheduled %s did not finish and needs recovery; it stays paused until then. Other work continues."
            % (name, LABELS.get(kind, kind)))


def quarantine_recovery(kind, work_id, home):
    """H4: structured recovery metadata kept on quarantine.notice (not in the message body)."""
    return {"session": "routine_rearm(work_id=%s, review, cycle) after doing the review" % work_id,
            "owner": "synthetics --home %s routine restore %s" % (home, kind),
            "doctor": "routine_quarantined", "docs": "docs/routines.md"}


def parse(value):
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("Timestamps need an explicit timezone: %r" % value)
    return moment


def iso(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def now_utc():
    return datetime.now(timezone.utc)


def is_routine(work):
    return isinstance(work, dict) and isinstance(work.get("routine"), dict)


def check_every(kind, minutes):
    if kind not in KINDS:
        raise ValueError("Unknown routine kind %r; kinds are %s." % (kind, ", ".join(KINDS)))
    if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes < MIN_EVERY:
        raise ValueError("every must be a whole number of minutes, at least %d." % MIN_EVERY)
    return minutes


def first_wake(kind, every, now=None, dream_at=DEFAULT_DREAM_AT):
    """Seed's initial wake: now+every, or for dream the next HH:MM UTC (default 01:30Z)."""
    now = now or now_utc()
    if kind != "dream":
        return now + timedelta(minutes=every)
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", dream_at or ""):
        raise ValueError("--dream-at must be HH:MM (UTC).")
    hour, minute = (int(x) for x in dream_at.split(":"))
    at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return at if at > now else at + timedelta(days=1)


def parse_first(value, now, every, kind=""):
    """G1: `--first KIND=<ISO-8601Z | +MIN>` -> a datetime from now+1 min to now+every (inclusive), else refused."""
    text = str(value).strip()
    if re.fullmatch(r"\+\d+", text):
        at = now + timedelta(minutes=int(text[1:]))
    else:
        try:
            at = parse(text)
        except (TypeError, ValueError):
            raise ValueError("--first %s takes an ISO-8601 UTC time or +MINUTES, got %r" % (kind, value))
    if not now + timedelta(minutes=1) <= at <= now + timedelta(minutes=int(every)):
        raise ValueError("--first %s=%s is outside now+1 min .. now+%d min (its cadence)" % (kind, text, int(every)))
    return at


def rearm_target(routine, now=None):
    """anchor_at + k*every for the smallest k >= 1 in the future: the reviewed cycle (the one at the anchor)
    never fires again, and a long or early session never drifts the schedule."""
    now = now or now_utc()
    anchor = parse(routine["anchor_at"])
    every = timedelta(minutes=int(routine["every_minutes"]))
    k = 1
    if anchor + every <= now:
        k = int((now - anchor) / every) + 1
        while anchor + k * every <= now:
            k += 1
    return anchor + k * every


def due_state(work, now=None):
    """fired | quarantined | due-soon | None (not due)."""
    if not is_routine(work) or work.get("status") in ("done", "cancelled"):
        return None
    if work["routine"].get("quarantine"):
        return "quarantined"
    if work.get("status") == "ready":
        return "fired"
    wake = work.get("wake_at")
    now = now or now_utc()
    if work.get("status") == "waiting" and wake and parse(wake) <= now + timedelta(minutes=FOLD_IN_MINUTES):
        return "due-soon"
    return None


def guidance(kind, path=None):
    """The ``## <kind>`` section of prompts/routines.md (company-level, maintained like employee.md)."""
    try:
        text = Path(path or GUIDANCE_FILE).read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r"^## %s[ \t]*\n(.*?)(?=^## |\Z)" % re.escape(kind), text, re.M | re.S)
    return match.group(1).strip() if match else ""


def check_omissions(omissions):
    """E1: omissions is a list of {reason, from?}; from = the timestamp of the earliest unread material."""
    if omissions is None:
        return []
    if not isinstance(omissions, list):
        raise ValueError("omissions must be a list of {reason, from?} objects.")
    out = []
    for item in omissions:
        if not isinstance(item, dict) or not isinstance(item.get("reason"), str) or not item["reason"].strip():
            raise ValueError("each omission needs a reason (what was truncated, capped or skipped) and, when known, "
                             "from = the timestamp of the earliest unread material.")
        entry = {"reason": item["reason"].strip()[:500]}
        if item.get("from") is not None:
            entry["from"] = iso(parse(item["from"]))
        out.append(entry)
    return out


def configured(home):
    """{limit_notify} this home escalates to: the running worker's effective settings (runtime.state.json) first,
    then runtime.json as written (only limit_notify is read, so a config the worker would refuse still names its
    manager). None when neither names one."""
    import json
    from . import runtime
    state = runtime.read_state(home)
    effective = state.get("effective") if isinstance(state, dict) else None
    if isinstance(effective, dict) and isinstance(effective.get("limit_notify"), str) and effective["limit_notify"]:
        return {"limit_notify": effective["limit_notify"]}
    try:
        raw = json.loads((Path(home) / runtime.CONFIG_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(raw, dict) and isinstance(raw.get("limit_notify"), str) and raw["limit_notify"]:
        return {"limit_notify": raw["limit_notify"]}
    return None


def route(runtime_config, roster_state, self_id, default_human=None):
    """E2: where routine reports go. The manager is this home's configured escalation identity (runtime.json
    limit_notify), canonicalised through the company roster when it has a profile. Never a hardcoded ID, never
    a guess from role text. No limit_notify -> an explicit unresolved result."""
    notify = (runtime_config or {}).get("limit_notify") or None
    if not notify:
        return {"resolved": False, "manager": None, "via": None, "report_to": None,
                "reason": "no manager resolved: runtime limit_notify is not set for this home"}
    canonical, via = notify, "limit_notify"
    if roster_state:
        from .roster import resolve
        hit = resolve(roster_state, notify)
        if hit["id"]:
            canonical, via = hit["id"], "limit_notify+roster"
    if canonical == self_id:
        # This home is the manager: its team-review reports to the default human (humans.json), if configured.
        result = {"resolved": True, "manager": canonical, "via": via, "self_is_manager": True,
                  "report_to": default_human or None}
        if not default_human:
            result["reason"] = "this home is the manager and humans.json has no default_human"
        return result
    return {"resolved": True, "manager": canonical, "via": via, "self_is_manager": False, "report_to": canonical}


def flags(works, now=None, running=False):
    """Discipline flags (doctor, sharedstatus/Lookout). works: work dicts; running: a session is active now
    (a fired routine is 'left ready' only when no session is running and 5 min have passed)."""
    now = now or now_utc()
    out = []
    week = now - timedelta(days=FAILURE_WINDOW_DAYS)
    for work in works:
        if not is_routine(work):
            continue
        r = work["routine"]
        kind, wid = r.get("kind"), work.get("id")
        fails = r.get("failures") or []
        if fails:
            recent = [f for f in fails if f.get("at") and parse(f["at"]) >= week]
            out.append({"code": "routine_failures", "severity": "warning", "work_id": wid, "kind": kind,
                        "total": len(fails), "last_7_days": len(recent)})
        status = work.get("status")
        if status == "cancelled" and r.get("torn_down_at"):
            continue
        if status in ("done", "cancelled"):
            out.append({"code": "routine_closed", "severity": "warning", "work_id": wid, "kind": kind,
                        "detail": "restore with: synthetics --home HOME routine restore %s" % kind})
            continue
        if r.get("quarantine"):
            q = r["quarantine"]
            out.append({"code": "routine_quarantined", "severity": "error", "work_id": wid, "kind": kind,
                        "detail": "needs attention: %s (run %s at %s); cleared by the employee's routine_rearm "
                                  "or `routine restore %s`" % (q.get("reason"), q.get("run"), q.get("at"), kind)})
            continue
        if status == "ready":
            since = parse(work.get("updated_at") or iso(now))
            if not running and now - since > timedelta(minutes=5):
                out.append({"code": "routine_left_ready", "severity": "error", "work_id": wid, "kind": kind,
                            "detail": "fired at %s and no session has picked it up" % r.get("last_fired_at")})
            continue
        wake = work.get("wake_at")
        if not wake:
            out.append({"code": "routine_unarmed", "severity": "error", "work_id": wid, "kind": kind,
                        "detail": "waiting without a wake_at"})
        elif parse(wake) <= now - timedelta(minutes=max(10, int(r.get("every_minutes") or 60) // 2)):
            out.append({"code": "routine_overdue", "severity": "warning", "work_id": wid, "kind": kind,
                        "detail": "wake_at %s passed and it has not fired (worker stopped or paused?)" % wake})
    return out
