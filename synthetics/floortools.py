"""MTG Floor F2: the meeting session's tool set (Codex C5, P0 §2 E3).

A meeting run (run row kind ``meeting``) is offered exactly MEETING_TOOLS: five read-only Synthetics tools plus
floor_commit and floor_note. Everything else (work_put, memory_put, message_send, question_ask, Bash, ...) is
unavailable: mcp.offered()/Server.call() filter by this allow-list in code, and the meeting process is started
with no built-in tools. Durable writes wait for the wrap session (F3 floor_accept).
"""
from __future__ import annotations

from . import floormeeting

READ_ONLY = ("memory_search", "work_get", "work_list", "project_memory_search", "entities")
FLOOR_TOOLS = ("floor_commit", "floor_note")
MEETING_TOOLS = frozenset(READ_ONLY + FLOOR_TOOLS)


def _text(description=""):
    return {"type": "string", "description": description}


TOOLS = [{
    "name": "floor_commit",
    "description": "Issue ONE promise you are making in this meeting, while answering an ask. It is journaled as "
                   "floor-issued with a cid, returned to the voice with your answer, and is the only kind of "
                   "promise the wrap may turn into work. Never promise on the human's or anyone else's behalf.",
    "inputSchema": {"type": "object", "additionalProperties": False, "required": ["ask", "text"], "properties": {
        "ask": _text("The id of the ask you are answering."),
        "text": _text("The promise, one sentence (≤1 KB), e.g. \"I'll send the review notes by Friday\"."),
        "work_id": _text("Optional: the work it belongs to."),
        "due": _text("Optional: when, as said."),
    }},
}, {
    "name": "floor_note",
    "description": "Journal a correction or observation in this meeting (≤8 KB). A correction makes any answer still "
                   "in flight stale, so it isn't spoken.",
    "inputSchema": {"type": "object", "additionalProperties": False, "required": ["text"], "properties": {
        "text": _text("The correction or observation."),
    }},
}]
NAMES = {t["name"] for t in TOOLS}
MUTATIONS = set(NAMES)


def call(home, meeting_id, name, arguments, text_mode="full"):
    if name == "floor_commit":
        cid = floormeeting.commit(home, meeting_id, arguments["ask"], arguments["text"],
                                  work_id=arguments.get("work_id"), due=arguments.get("due"), text_mode=text_mode)
        return dict(cid=cid, meeting=meeting_id)
    if name == "floor_note":
        return dict(line=floormeeting.floor_note(home, meeting_id, arguments["text"], text_mode=text_mode),
                    meeting=meeting_id)
    raise ValueError("Unknown tool: %s" % name)


def filter_tools(tools):
    """The tools/list a meeting run sees: only MEETING_TOOLS, in the given order."""
    return [t for t in tools if t["name"] in MEETING_TOOLS]
