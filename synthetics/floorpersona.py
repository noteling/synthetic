"""MTG Floor F2: the `ready` persona and the meeting brief, built by code from a static allow-list (Codex C6).

What a voice in a room of non-employees may repeat aloud (P0 §1, Rowan's consumer read G3):
- identity: name, role, purpose; company aliases (for name-mishearing rules);
- the human's `goal` (≤ GOAL_MAX bytes, the human's own brief);
- ONLY the works the goal names by ID (full ID or a unique 8+ hex prefix): title, status, waiting_for.
The meeting session's own brief (never sent to Stand) adds those named works' notes: the human chose to bring them.
Never: other works, private views, self notes, memories, messages or their bodies, outbox, host rules, credentials.
During the meeting the model reaches anything further only through the read-only tools.

The allow-list is the constants below; tests check every emitted key against them and grep this module for the
store calls it may make (STORE_CALLS).
"""
from __future__ import annotations

import hashlib
import re

from . import roster

GOAL_MAX = 4096
PERSONA_MAX = 3072
IDENTITY_KEYS = ("name", "role", "purpose")
WORK_KEYS = ("id", "title", "status", "waiting_for", "note")
READY_KEYS = ("name", "role", "persona", "aliases", "brief_sha256")   # to Stand's voice: never a work note
BRIEF_MAX = 8192
STORE_CALLS = ("identity", "work_get", "home")   # the only Store attributes this module touches
WORK_REF = re.compile(r"\bwork-[0-9a-f]{8,32}\b")


class PersonaError(ValueError):
    pass


def _named_works(store, goal):
    works, seen = [], set()
    for ref in WORK_REF.findall(goal):
        try:
            work = store.work_get(ref)
        except ValueError:
            continue  # unknown or ambiguous: the goal named nothing usable
        if work["id"] in seen:
            continue
        seen.add(work["id"])
        works.append({k: work.get(k) for k in WORK_KEYS})
    return works


def _aliases(store, identity):
    try:
        company = roster.company_state_for_home(store.home)
        person = company.get("people", {}).get(identity["id"]) or {}
        return sorted(set(person.get("aliases") or []))
    except Exception:  # noqa: BLE001 - no company roster: no aliases
        return []


def _cut(text, limit):
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", "ignore").rstrip() + " …"


def build(store, goal):
    if not isinstance(goal, str) or not goal.strip():
        raise PersonaError("goal is the human's brief for this meeting (1 to %d bytes)." % GOAL_MAX)
    if len(goal.encode("utf-8")) > GOAL_MAX:
        raise PersonaError("goal is at most %d bytes." % GOAL_MAX)
    identity = store.identity()
    who = {k: identity.get(k) for k in IDENTITY_KEYS}
    works = _named_works(store, goal)
    lines = ["# Who you are", "You are %s, %s." % (who["name"], who["role"])]
    if who.get("purpose"):
        lines.append("Purpose: %s" % who["purpose"])
    lines += ["", "# This meeting", goal.strip()]
    for work in works:
        lines.append("- %s: %s (%s%s)" % (work["id"], work["title"], work["status"],
                                          ", waiting for %s" % work["waiting_for"] if work.get("waiting_for") else ""))
    persona = _cut("\n".join(lines), PERSONA_MAX)
    ready = dict(name=who["name"], role=who["role"], persona=persona, aliases=_aliases(store, identity),
                 brief_sha256=hashlib.sha256(goal.encode("utf-8")).hexdigest())
    notes = ["## %s note\n%s" % (w["id"], w["note"]) for w in works if w.get("note")]
    brief = _cut("\n\n".join([persona] + notes), BRIEF_MAX)
    return dict(ready=ready, brief=brief, work_ids=[w["id"] for w in works])
