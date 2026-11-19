"""MTG Floor F3: the gate (docs/floor.md F3). `floor_accept` turns an issued meeting promise into a work;
`floor verify RUN` reports what a wrap session recorded about a meeting without going through it.

P0 §3: only a promise the meeting session issued itself (a `commit` line, speaker floor-issued, with a cid) may
become work. floor_accept is the one path to a work with origin "meeting" (work_put refuses that origin), it
copies the promise text verbatim, and it is refused unless the meeting has ended and its journal chain matches
the head anchored in the wrap event.

verify makes a violation visible; it does not prevent it (P0 honest limit). Its matching rules are exact and
documented here and in docs/floor.md:

- A work_put receipt *cites the meeting* when any of its title, description, note, waiting_for or evidence
  ref/note contains the meeting ID as a whole token (not inside a longer run of ID characters [A-Za-z0-9_.:-]),
  the journal path segment ``meetings/<id>``, or the wrap event ID (in full, or ``event-`` plus 8 or more of its
  hex characters).
- Such a work_put is *attributed* (no violation) when its target work is one floor_accept created or extended for
  this meeting (audit rows ``floor.accepted``), or when its evidence cites a question that is answered (status
  ``answered``, not proxy, not withdrawn), was addressed to this home's default human, and whose answer event has
  a non-null auth (Codex Q2). Any other citing work_put is ``unattributed_meeting_work``.
- A memory_put receipt with ``replaces`` whose source cites the meeting and names ``line N`` (or ``lines N``,
  ``line #N``) where journal line N is ``room-unidentified`` is ``room_correction_replaces``.
"""
from __future__ import annotations

import json
import re

from . import floorjournal as fj
from . import ledgerline

ORIGIN = "meeting"
WRAP_SOURCE_SUFFIX = ":meeting"
CODES = ("unknown_cid", "declined", "meeting_running", "chain_broken", "no_wrap", "unknown_work", "work_closed",
         "bad_meeting")
_ID_CHARS = "A-Za-z0-9_.:-"
LINE_REF = re.compile(r"\blines?\s*#?\s*(\d+)", re.IGNORECASE)


class AcceptRefused(ValueError):
    def __init__(self, code, message):
        super().__init__("%s: %s" % (code, message))
        self.code = code


def wrap_external_id(meeting):
    return "floor:%s:wrap" % meeting


def wrap_event(rows, meeting):
    """The wrap event for `meeting` among event rows (dicts): external_id floor:<id>:wrap, source <prefix>:meeting,
    an authenticated channel stamp, and a JSON body naming the meeting with a chain head. None otherwise."""
    for event in rows:
        if event.get("external_id") != wrap_external_id(meeting):
            continue
        if not str(event.get("source") or "").endswith(WRAP_SOURCE_SUFFIX) or not event.get("auth"):
            continue
        try:
            body = json.loads(event.get("body") or "")
        except ValueError:
            continue
        if isinstance(body, dict) and body.get("meeting") == meeting and isinstance(body.get("head"), dict):
            return event, body
    return None


def wrap_rows(db, meeting):
    """Event rows whose data mentions the wrap external_id (prefilter; wrap_event decides)."""
    needle = "%" + wrap_external_id(meeting) + "%"   # `_` is a LIKE wildcard: it only widens the prefilter
    out = []
    for row in db.execute("SELECT data FROM events WHERE data LIKE ? ORDER BY seq", (needle,)):
        try:
            out.append(json.loads(row["data"]))
        except ValueError:
            continue
    return out


def numbered(home, meeting):
    return list(enumerate(fj.lines(home, meeting), 1))


def commit_line(lines, cid):
    for number, line in lines:
        if line.get("op") == "commit" and line.get("cid") == cid:
            return number, line
    return None, None


def latest_confirm(lines, cid):
    found = None
    for number, line in lines:
        if line.get("op") == "confirm" and line.get("cid") == cid:
            found = (number, line.get("yes") is True)
    return found


def description(line):
    text = line.get("text") or ""
    due = line.get("due")
    return text + ("\n\nDue (as said): %s" % due if isinstance(due, str) and due.strip() else "")


# -- verify ------------------------------------------------------------------------------------------------------

def _cites(meeting, wrap_id):
    token = re.compile(r"(?<![%s])%s(?![%s])" % (_ID_CHARS, re.escape(meeting), _ID_CHARS))
    path = "meetings/" + meeting
    prefix = None
    if isinstance(wrap_id, str) and wrap_id.startswith("event-"):
        prefix = re.compile(r"event-([0-9a-f]{8,})")

    def cites(text):
        if not isinstance(text, str) or not text:
            return False
        if token.search(text) or path in text:
            return True
        if prefix is not None:
            hexpart = wrap_id[len("event-"):]
            return any(hexpart.startswith(m.group(1)) for m in prefix.finditer(text))
        return False
    return cites


def _work_texts(arguments):
    out = [arguments.get(k) for k in ("title", "description", "note", "waiting_for")]
    for item in arguments.get("evidence") or []:
        if isinstance(item, dict):
            out += [item.get("ref"), item.get("note")]
    return out


def _answered_by_human(conn, ref, human):
    """Codex Q2: an answered (not proxy, not withdrawn) question to the default human with an authenticated answer."""
    if not isinstance(ref, str) or not ref.startswith("question-") or not human:
        return False
    row = conn.execute("SELECT data FROM questions WHERE id=?", (ref,)).fetchone()
    if row is None:
        return False
    question = json.loads(row["data"])
    if question.get("status") != "answered" or not question.get("answer_event_id"):
        return False
    if (question.get("to_id") or question.get("to")) != human:
        return False
    answer = conn.execute("SELECT data FROM events WHERE id=?", (question["answer_event_id"],)).fetchone()
    return bool(answer and json.loads(answer["data"]).get("auth"))


def _accepted(conn, meeting):
    """{cid: work_id} from this home's floor.accepted audit rows for the meeting."""
    out = {}
    for row in conn.execute("SELECT data FROM audit WHERE action='floor.accepted' ORDER BY seq"):
        data = json.loads(row["data"])
        if data.get("meeting") == meeting:
            out[data.get("cid")] = data.get("work")
    return out


def verify(home, run_id):
    """floor verify RUN (read-only: sqlro, never a Store). See the module docstring for the exact rules."""
    from pathlib import Path
    from .capture import default_human
    conn = ledgerline._connect(home)
    try:
        row = conn.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            return dict(run=run_id, verdict="no_evidence", reason="no such run in this home")
        run = json.loads(row["data"])
        wraps = []
        for event_id in run.get("event_ids") or []:
            erow = conn.execute("SELECT data FROM events WHERE id=?", (event_id,)).fetchone()
            event = json.loads(erow["data"]) if erow else {}
            if str(event.get("source") or "").endswith(WRAP_SOURCE_SUFFIX) and str(
                    event.get("external_id") or "").startswith("floor:"):
                try:
                    body = json.loads(event.get("body") or "")
                except ValueError:
                    body = None
                meeting = body.get("meeting") if isinstance(body, dict) else None
                if isinstance(meeting, str) and fj.MEETING_ID.match(meeting) and wrap_event([event], meeting):
                    wraps.append((event, body))
        receipts = ledgerline._receipt_lines(Path(str(home)) / "runs" / (run_id + ".mcp.jsonl"))
        if not wraps:
            return dict(run=run_id, verdict="no_evidence", reason="the run's inputs hold no stand:meeting wrap event")
        if receipts is None:
            return dict(run=run_id, verdict="no_evidence", reason="the run has no MCP receipts",
                        meetings=[b["meeting"] for _, b in wraps])
        human = default_human(home)
        records = [(n, r) for n, _, r in receipts if r and r.get("success")]
        meetings, violations = [], []
        for event, body in wraps:
            meeting = body["meeting"]
            lines = numbered(home, meeting)
            chain = fj.verify(home, meeting, anchor=body["head"]).get("chain")
            accepted = _accepted(conn, meeting)
            accepted_works = set(w for w in accepted.values() if w)
            cites = _cites(meeting, event["id"])
            declined = {c for c in {l.get("cid") for _, l in lines if l.get("op") == "confirm"}
                        if latest_confirm(lines, c)[1] is False}
            issued = [l.get("cid") for _, l in lines if l.get("op") == "commit"]
            for number, record in records:
                args = record.get("arguments") if isinstance(record.get("arguments"), dict) else {}
                if record.get("tool") == "work_put" and any(cites(t) for t in _work_texts(args)):
                    if args.get("id") in accepted_works:
                        continue
                    refs = [i.get("ref") for i in args.get("evidence") or [] if isinstance(i, dict)]
                    if any(_answered_by_human(conn, ref, human) for ref in refs):
                        continue
                    violations.append(dict(kind="unattributed_meeting_work", meeting=meeting, receipt_line=number,
                                           work=args.get("id")))
                if record.get("tool") == "memory_put" and args.get("replaces") and cites(args.get("source")):
                    speakers = {n: l.get("speaker") for n, l in lines}
                    room = [int(m) for m in LINE_REF.findall(args.get("source") or "")
                            if speakers.get(int(m)) == fj.ROOM]
                    if room:
                        violations.append(dict(kind="room_correction_replaces", meeting=meeting,
                                               receipt_line=number, lines=room, replaces=args.get("replaces")))
            meetings.append(dict(meeting=meeting, wrap_event=event["id"], chain=chain,
                                 accepted=sorted(c for c in accepted if c in issued),
                                 declined=sorted(c for c in declined if c in issued and c not in accepted),
                                 unaccepted=sorted(c for c in issued if c not in accepted and c not in declined)))
            if chain != "chain_ok":
                violations.append(dict(kind="chain", meeting=meeting, chain=chain))
        return dict(run=run_id, verdict="violations" if violations else "clean", meetings=meetings,
                    violations=violations, receipts=len(receipts),
                    limit="verify makes a violation visible; it does not prevent it")
    finally:
        conn.close()
