"""MTG Floor F2: one meeting's line handling (P0 §1, §3, §4), independent of the model process.

`Meeting.handle(line)` takes one Stand line (heard, spoken, note, ask, cancel, confirm) and returns the out lines to
emit now. An `ask` is handed to the session (``session.submit(turn)``); the session later calls
``Meeting.finish(ask_id, text, commitments)`` from its own thread, which returns the out lines for that answer.

Rules:
- Every in/out line is journaled (floorjournal) with its fixed speaker class.
- Revision (§4): an ask carries `rev`, the last seq Stand sent. If a panel `note` correction is journaled after the
  ask and before its answer, the answer becomes `stale` (not spoken). A `floor_note` never does: the one session
  answers asks in order, so it wrote any floor_note itself before or while generating this answer, and the answer
  already accounts for it (the prompt asks it to journal a correction and say it; #76 r1, Rowan L4).
- Cancel (§4): `cancel` journals `cancelled`; a later answer for that id is journaled `late` and never emitted.
  The session is told to stop the turn (``session.cancel(id)``).
- After Stand's `end` (``ended``), an answer is journaled `late` only. ``close()`` (after the session has been
  stopped) writes the final `end` line and returns the chain head the wrap event anchors; nothing is written after it,
  so the anchor stays the file's head.
- Commitments (§3): only `commit` lines the session issued during that turn (floor-issued, with a cid) are returned
  with the answer. `confirm` from the panel journals `human-panel` and is the human confirming a promise; it never
  answers a question, approves a click or stands in for the default human anywhere else (Rowan G4): it is journal
  data only, with auth = channel stand.
"""
from __future__ import annotations

import secrets
import threading

from . import floorjournal as fj

IN_OPS = ("heard", "spoken", "note", "ask", "cancel", "confirm")
TEXT_MAX = 8192
QUESTION_MAX = 2048
CORRECTION_OPS = ("note",)   # a panel correction journaled after an ask makes its answer stale (§4; L4: not floor_note)


class LineError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _int(value, name):
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise LineError("bad_line", "%s must be a non-negative integer" % name)
    return value


def _str(value, name, limit):
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > limit:
        raise LineError("bad_line", "%s must be 1 to %d bytes of text" % (name, limit))
    return value


class Meeting:
    def __init__(self, home, meeting_id, session, text_mode="full", channel="stand"):
        self.home, self.id, self.session, self.text_mode, self.channel = home, meeting_id, session, text_mode, channel
        self.lock = threading.Lock()
        self.asks = {}          # id -> {rev, line, state: open|cancelled|answered|stale}
        self.ended = False      # Stand said end (or EOF/cap): answers are journaled late, never emitted
        self.closed = False     # final end line written: nothing more is journaled

    def _j(self, op, fields=None):
        return fj.append(self.home, self.id, op, fields, text_mode=self.text_mode)

    def begin(self, started_by=None, goal_sha256=None):
        with self.lock:
            return self._j("begin", dict(started_by=started_by, goal_sha256=goal_sha256))

    def handle(self, line):
        op = line.get("type")
        if op not in IN_OPS:
            raise LineError("bad_line", "unknown line type %r" % (op,))
        with self.lock:
            if self.ended or self.closed:
                raise LineError("invalid_state", "the meeting has ended")
            if op == "heard":
                self._j("heard", dict(seq=_int(line.get("seq"), "seq"), t=line.get("t"),
                                      text=_str(line.get("text"), "text", TEXT_MAX)))
                return []
            if op == "spoken":
                self._j("spoken", dict(seq=_int(line.get("seq"), "seq"), t=line.get("t"),
                                       text=_str(line.get("text"), "text", TEXT_MAX),
                                       played=line.get("played") is True, cut_at=line.get("cut_at")))
                return []
            if op == "note":
                self._j("note", dict(seq=_int(line.get("seq"), "seq"), t=line.get("t"),
                                     text=_str(line.get("text"), "text", TEXT_MAX)))
                return []
            if op == "confirm":
                cid = _str(line.get("cid"), "cid", 64)
                self._j("confirm", dict(cid=cid, yes=line.get("yes") is True))
                return []
            if op == "cancel":
                ask_id = _str(line.get("id"), "id", 64)
                ask = self.asks.get(ask_id)
                if ask is None or ask["state"] != "open":
                    return []
                ask["state"] = "cancelled"
                self._j("cancelled", dict(id=ask_id))
                cancel = getattr(self.session, "cancel", None)
                if cancel:
                    cancel(ask_id)
                return []
            # ask
            ask_id = _str(line.get("id"), "id", 64)
            if ask_id in self.asks:
                raise LineError("conflict", "ask id %s was already used" % ask_id)
            rev = _int(line.get("rev"), "rev")
            question = _str(line.get("question"), "question", QUESTION_MAX)
            out = self._j("ask", dict(id=ask_id, rev=rev, question=question))
            self.asks[ask_id] = dict(rev=rev, line=out["line"], state="open")
        self.session.submit(dict(id=ask_id, rev=rev, question=question))
        return []

    def commit(self, ask_id, text, work_id=None, due=None):
        return commit(self.home, self.id, ask_id, text, work_id=work_id, due=due, text_mode=self.text_mode)

    def floor_note(self, text):
        return floor_note(self.home, self.id, text, text_mode=self.text_mode)

    def finish(self, ask_id, text):
        """The session's answer for ask_id. Returns the out lines to emit (possibly none)."""
        with self.lock:
            ask = self.asks.get(ask_id)
            if ask is None or self.closed:
                return []
            if self.ended or ask["state"] != "open":
                self._j("late", dict(id=ask_id, text=text, reason="ended" if self.ended else ask["state"]))
                return []
            all_lines = fj.lines(self.home, self.id)
            commitments = [dict(cid=l["cid"], text=l.get("text")) for l in all_lines
                           if l.get("op") == "commit" and l.get("id") == ask_id]
            corrected = any(l.get("op") in CORRECTION_OPS for l in all_lines[ask["line"]:])
            if corrected:
                ask["state"] = "stale"
                self._j("stale", dict(id=ask_id, rev=ask["rev"], text=text, reason="corrected after rev"))
                return [dict(type="stale", id=ask_id, reason="corrected after rev")]
            ask["state"] = "answered"
            self._j("answer", dict(id=ask_id, rev=ask["rev"], text=text, commitments=commitments))
            return [dict(type="answer", id=ask_id, rev=ask["rev"], text=text, commitments=commitments)]

    def fail(self, ask_id, reason):
        """The session gave no usable answer for ask_id (error result, or the session died). Nothing is spoken and
        no text is invented: the journal records it as `late` with the reason, Stand gets an error line."""
        with self.lock:
            ask = self.asks.get(ask_id)
            if ask is None or self.closed or ask["state"] != "open":
                return []
            ask["state"] = "failed"
            self._j("late", dict(id=ask_id, reason=str(reason)[:64]))
            return [] if self.ended else [dict(type="error", code="no_answer", id=ask_id, message=str(reason)[:200])]

    def open_asks(self):
        with self.lock:
            return sorted(k for k, a in self.asks.items() if a["state"] == "open")

    def end(self):
        """Stand's end line (or EOF, or the cap): stop emitting; the caller then stops the session and calls close."""
        with self.lock:
            self.ended = True

    def close(self, reason):
        with self.lock:
            if self.closed:
                return fj.head(self.home, self.id)
            self.ended = self.closed = True
            self._j("end", dict(reason=reason))
            return fj.head(self.home, self.id)

    def summary(self):
        """Pointer facts for the wrap event (no text at all): chain head, issued cids, confirmations, corrections."""
        all_lines = fj.lines(self.home, self.id)
        numbered = list(enumerate(all_lines, 1))
        return dict(meeting=self.id, head=fj.head(self.home, self.id),
                    issued=[dict(cid=l["cid"], ask=l.get("id"), line=n) for n, l in numbered if l.get("op") == "commit"],
                    confirmed=[dict(cid=l.get("cid"), yes=l.get("yes"), line=n) for n, l in numbered
                               if l.get("op") == "confirm"],
                    corrections=[n for n, l in numbered if l.get("op") in CORRECTION_OPS],
                    cancelled=sorted(k for k, a in self.asks.items() if a["state"] == "cancelled"))


def _open_ask_guard(ask_id):
    def guard(all_lines):
        if fj.ask_state(all_lines, ask_id)[1] != "open":
            raise LineError("invalid_state", "no open ask %s in this meeting" % ask_id)
    return guard


def commit(home, meeting_id, ask_id, text, work_id=None, due=None, text_mode="full"):
    """floor_commit (meeting session, possibly another process): issue a promise during an open ask. Returns the cid.
    The open check runs under the journal lock, so a commit can never land after that ask's answer."""
    _str(ask_id, "ask", 64)
    cid = "cid-" + secrets.token_hex(8)
    fj.append(home, meeting_id, "commit", dict(cid=cid, id=ask_id, text=_str(text, "text", 1024), work_id=work_id,
                                                due=due), text_mode=text_mode, guard=_open_ask_guard(ask_id))
    return cid


def floor_note(home, meeting_id, text, text_mode="full"):
    """floor_note: the meeting session journals a correction or observation. Returns the line number."""
    return fj.append(home, meeting_id, "floor_note", dict(text=_str(text, "text", TEXT_MAX)), text_mode=text_mode)["line"]
