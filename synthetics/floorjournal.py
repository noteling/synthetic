"""MTG Floor F2: the meeting journal (docs/floor.md).

``HOME/meetings/<meeting_id>/journal.jsonl``, append-only, one JSON object per line, each with ``prev_sha256`` (the
sha256 of the previous raw line, None on the first): the Ledgerline chain idea, checked with the same rules.

Speaker classes come from the line's op, set by this module, never from the input (P0 §1: a class is never
upgraded). A `heard` line is always ``room-unidentified``; only a panel line (`note`, `confirm`) is the human's.

Heard text is stored in ONE place, ``_stored_text`` (Codex C8), so a hash-only mode (§9 "don't keep it") is one switch:
``text_mode="hash"`` keeps {text_bytes, text_sha256} instead of the words.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from . import ledgerline

MEETING_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")   # store.MEETING_ID; first char alnum: never . or ..
TEXT_MODES = ("full", "hash")

# op -> speaker class. Anything not listed is refused (closed set).
ROOM, PLAYED, UNPLAYED, PANEL, ISSUED, FLOOR = (
    "room-unidentified", "employee-played", "employee-generated-unplayed", "human-panel", "floor-issued", "floor")
SPEAKERS = (ROOM, PLAYED, UNPLAYED, PANEL, ISSUED, FLOOR)
FIXED = {"heard": ROOM, "note": PANEL, "confirm": PANEL, "commit": ISSUED,
         "begin": FLOOR, "ask": FLOOR, "answer": FLOOR, "stale": FLOOR, "cancel": FLOOR, "cancelled": FLOOR,
         "floor_note": FLOOR, "end": FLOOR, "late": FLOOR}
OPS = frozenset(FIXED) | {"spoken"}
# Ops whose `text` is room speech (other people's words): the only text that hash mode removes.
ROOM_TEXT_OPS = frozenset({"heard"})
# Fields a caller may pass; everything else is dropped (no free-form keys reach the journal).
FIELDS = frozenset({"seq", "t", "text", "played", "cut_at", "id", "rev", "cid", "yes", "reason", "question",
                    "work_id", "due", "goal_sha256", "started_by", "commitments"})


class JournalError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def path(home, meeting_id):
    if not isinstance(meeting_id, str) or not MEETING_ID.match(meeting_id):
        raise JournalError("meeting_id must be 1-64 letters, digits or _.:- characters, starting with a letter or digit.")
    return Path(str(home)) / "meetings" / meeting_id / "journal.jsonl"


def speaker_of(op, fields):
    if op not in OPS:
        raise JournalError("unknown journal op %r" % (op,))
    if op == "spoken":
        return PLAYED if fields.get("played") is True else UNPLAYED
    return FIXED[op]


def _stored_text(op, text, text_mode):
    """The one place heard text is stored (C8)."""
    if text is None:
        return {}
    if not isinstance(text, str):
        raise JournalError("text must be a string")
    if op in ROOM_TEXT_OPS and text_mode == "hash":
        data = text.encode("utf-8")
        return {"text_bytes": len(data), "text_sha256": hashlib.sha256(data).hexdigest()}
    return {"text": text}


def append(home, meeting_id, op, fields=None, text_mode="full", now=None, guard=None):
    """Append one line; returns {line, sha256, speaker}. Line numbers start at 1.

    guard(lines) runs under the journal lock with the parsed lines so far and may raise to refuse the append, so a
    check-then-append from another process (the meeting session's MCP server) is atomic with the attach process."""
    if text_mode not in TEXT_MODES:
        raise JournalError("text_mode is full or hash")
    fields = dict(fields or {})
    speaker = speaker_of(op, fields)
    record = {k: v for k, v in fields.items() if k in FIELDS and k != "text"}
    record.update(_stored_text(op, fields.get("text"), text_mode))
    target = path(home, meeting_id)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(target), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)  # read the tail and append as one step (as i17 E1)
        if guard is not None:
            guard(lines(home, meeting_id))
        previous = ledgerline.last_line(target)
        head = ledgerline.chain_head(target) or {"lines": 0}
        line = dict(at=now or _now(), op=op, speaker=speaker, meeting=meeting_id,
                    prev_sha256=ledgerline.sha256_text(previous) if previous is not None else None, **record)
        raw = json.dumps(line, sort_keys=True, ensure_ascii=False)
        handle.write(raw + "\n")
        handle.flush()
    return {"line": head["lines"] + 1, "sha256": ledgerline.sha256_text(raw), "speaker": speaker}


def ask_state(all_lines, ask_id):
    """(line number of the ask, state) from the journal: open | answered | stale | cancelled | late | None."""
    number, state = None, None
    for n, line in enumerate(all_lines, 1):
        if line.get("id") != ask_id:
            continue
        op = line.get("op")
        if op == "ask":
            number, state = n, "open"
        elif number and op in ("answer", "stale", "cancelled", "late") and state == "open":
            state = {"answer": "answered"}.get(op, op)
    return number, state


def lines(home, meeting_id):
    try:
        with open(str(path(home, meeting_id)), encoding="utf-8") as handle:
            return [json.loads(raw) for raw in handle if raw.strip()]
    except FileNotFoundError:
        return []


def head(home, meeting_id):
    return ledgerline.chain_head(path(home, meeting_id))


def verify(home, meeting_id, anchor=None):
    """chain_ok / chain_broken / unanchored / no_receipts, as Ledgerline's check_chain. Without an anchor (meeting
    still running, or wrap event not yet written) an intact chain reports `unanchored`."""
    return ledgerline.check_chain(path(home, meeting_id), anchor)
