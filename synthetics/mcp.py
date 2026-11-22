"""Small stdio MCP boundary for one employee's durable operations.

Run with ``python -m synthetics.mcp --home PATH``. Protocol output alone goes to
stdout; both the operator and the CLI adapter may launch this process.
"""

import argparse
import fcntl
from datetime import datetime, timezone
import json
import os
import re
import sys

from . import boundedreads, ledgerline, sharedstatus
from .store import Store


def text_property(description=""):
    return {"type": "string", "description": description}


def tool(name, description, properties=None, required=None):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object", "properties": properties or {},
            "required": required or [], "additionalProperties": False,
        },
    }


# F2: compact retrieval defaults for the model. The store returns full records unless asked (CLI, internal
# callers and tests are unchanged); the client's display limit is not measured, so 32000 is a margin we chose.
# Through MCP it is also the most a caller may ask for (#64): the client saves a result too large to show to a
# file outside the home.
SEARCH_BODY_CHARS = 500
SEARCH_MAX_BYTES = 32000
EVENT_CHUNK_CHARS = 30000

KEY = text_property("Stable descriptive key for this intended operation. Reuse after interruption; a different payload with the same key is rejected.")
TOOLS = [
    tool("employee_context", "Read budgeted context entries; sections lists selectable names. Follow next_cursor or concatenate JSON chunks. A changed source invalidates continuation.", {
        "section": text_property("Select a top-level context section."),
        "cursor": text_property("Continuation returned by this tool."),
        "max_bytes": {"type": "integer", "minimum": 1024, "maximum": 32000},
    }),
    tool("event_get", "Read the original source event by ID, including messages already processed in earlier sessions. Use this to verify the evidence behind a memory. A unique ID prefix of 8+ hex characters is accepted. Without body_offset/body_chars the whole event is returned; when its body_len is over ~30 000, read it in chunks instead: body_offset=0 with body_chars, then body_offset=body_next until body_next is null.", {
        "id": text_property(),
        "body_offset": {"type": "integer", "minimum": 0, "description": "Character offset into the original body; the result has that slice plus body_len, body_offset and body_next (null at the end)."},
        "body_chars": {"type": "integer", "minimum": 1, "description": "Characters of the body to return from body_offset (default %d when only body_offset is given)." % EVENT_CHUNK_CHARS},
    }, ["id"]),
    tool("event_search", "Search original source events across sessions, including processed events. Filter by thread when tracing an ongoing conversation. With since, read in time order with a capped flag (the dream's retrieval). The result is {events, capped, truncated_from, ...}; every event has body_len and body_truncated. A body_truncated event is a preview, not a read: fetch it with event_get. Without since, bodies are cut to body_chars (default %d). Every result stops at limit and before max_bytes (default %d). capped=true means matching events exist that this result leaves out, whichever of the two cut it; capped=false means none does." % (SEARCH_BODY_CHARS, SEARCH_MAX_BYTES), {
        "query": text_property(), "thread": text_property(),
        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        "body_chars": {"type": "integer", "minimum": 0, "description": "Cut each body to this many characters (default %d without since; full bodies with since unless given)." % SEARCH_BODY_CHARS},
        "max_bytes": {"type": "integer", "minimum": 1024, "maximum": SEARCH_MAX_BYTES, "description": "Byte budget of the serialized result (default and maximum %d). The result stops before the item that would exceed it and returns at least one item; a single item too large alone is cut and marked body_truncated." % SEARCH_MAX_BYTES},
        "since": text_property("ISO timestamp with timezone. Only items created at or after it, oldest first, every query word required; the result is {events, since, capped, unread_from, next_cursor, truncated_from}. capped=true means the limit or max_bytes cut the page: unread_from is the created_at of the first item not returned (an omission's from); pass next_cursor as cursor to read the next page. truncated_from is the created_at of the first body_truncated item (null if none): unless you fetch that item with event_get, it bounds your coverage too."),
        "cursor": text_property("next_cursor of the previous since= page: continues strictly after its last item, so items with the same created_at are neither repeated nor skipped."),
    }),
    tool("thread_history", "Read a conversation's incoming messages and your outgoing messages/questions, including processed history. Items are chronological; the first page contains the latest items. Use next_before as before to retrieve older pages. A bound work session excludes inputs outside its captured batch.", {
        "thread": text_property("Stable conversation ID from the incoming event or your outgoing message."),
        "limit": {"type": "integer", "minimum": 1, "maximum": 500},
        "before": {"type": "integer", "minimum": 1},
    }, ["thread"]),
    tool("entity_put", "Remember a person, project, organization, or concept under a stable ID. The employee's own identity cannot be overwritten.", {
        "id": text_property(), "name": text_property(), "kind": text_property(), "description": text_property(),
    }, ["id", "name", "kind"]),
    tool("entities", "Find known people, projects, organizations, and concepts.", {"query": text_property()}),
    tool("memory_put", "Retain a sourced fact, belief, preference, or procedure about an entity. Correct older understanding with replaces. predicate working_context (subject: yourself) is the dream's one bounded working context, shown on its own in every snapshot; only a dream session writes it, replacing the active one, with kind belief or procedure (a reflection, never fact).", {
        "subject": text_property("Existing entity ID, including your identity ID for yourself."),
        "predicate": text_property("What this memory says about the subject."),
        "value": text_property(),
        "kind": {"type": "string", "enum": ["fact", "belief", "preference", "procedure"]},
        "source": text_property("Input event ID, colleague, file, or other supporting evidence."),
        "key": KEY, "replaces": text_property("ID of the obsolete memory this corrects."),
    }, ["subject", "predicate", "value", "source"]),
    tool("memory_search", "Search your memory across projects. Active memories by default; inspect superseded history explicitly when needed. A full memory ID (or a 15+ character prefix) returns that memory first. With since, read in time order with a capped flag (the dream's retrieval). The result is {memories, capped, ...}; it stops at limit and before max_bytes (default %d) with at least one memory. capped=true means matching memories exist that this result leaves out, whichever of the two cut it (narrow the query, or page with since=); capped=false means none does. With match=any, ranked matches count too. Memories are never cut." % SEARCH_MAX_BYTES, {
        "query": text_property(), "subject": text_property(),
        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        "max_bytes": {"type": "integer", "minimum": 1024, "maximum": SEARCH_MAX_BYTES, "description": "Byte budget of the serialized result (default and maximum %d)." % SEARCH_MAX_BYTES},
        "include_superseded": {"type": "boolean"},
        "match": {"type": "string", "enum": ["any", "all"],
                  "description": "any (default): every-word matches first, then ranked matches on any distinctive word. all: every word must appear."},
        "since": text_property("ISO timestamp with timezone. Only items created at or after it, oldest first, every query word required; the result is {memories, since, capped, unread_from, next_cursor, truncated_from}. capped=true means the limit or max_bytes cut the page: unread_from is the created_at of the first item not returned (an omission's from); pass next_cursor as cursor to read the next page."),
        "cursor": text_property("next_cursor of the previous since= page: continues strictly after its last item, so items with the same created_at are neither repeated nor skipped."),
    }),
    tool("work_put", "Create or update ongoing work, including commitments and progress. Pass id to update; omitted fields retain their values. Waiting needs a blocker or future wake-up. A final summary alone does not update work. id, question IDs in waiting_for and evidence refs accept a unique prefix of 8+ hex characters; the full ID is stored.", {
        "title": text_property(), "description": text_property(), "id": text_property(),
        "status": {"type": "string", "enum": ["ready", "waiting", "done", "cancelled"]},
        "waiting_for": text_property(), "wake_at": text_property("ISO 8601 timestamp with an explicit timezone."),
        "priority": {"type": "integer"}, "note": text_property("Progress, evidence, uncertainty, and the next step for a fresh session."),
        "key": KEY,
        "evidence": {
            "type": "array",
            "description": "Completion evidence appended to this work. Required to mark it done: at least one item at level observed or verified. Refs: an event, run, question, message, memory or work ID; a file path (absolute, or relative to your workspace; an ID-like name such as work-notes.md is read as an ID, so give its absolute path); or receipt:/https: references. Your own messages, memories and unanswered questions count only as attempted; cite the source a memory records, a delivered message or a colleague's reply instead. verified needs a colleague's reply or an authenticated answer. The current run is not evidence.",
            "items": {
                "type": "object",
                "properties": {
                    "level": {"type": "string", "enum": ["attempted", "observed", "verified"]},
                    "ref": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["level", "ref"],
            },
        },
        "cancel_reason": text_property("Why this work is being cancelled. Required when status is cancelled."),
        "publish": text_property("Optional: project whose Board backlog shows this work (you need a post grant). Published title, status, waiting_for, evidence refs and priority are visible to that project; description and note never are. \"\" unpublishes. Published or not, the title, status, priority, waiting_for and wake_at of every open work (not done or cancelled) also appear on the host's local Lookout status page (synthetics web); keep private details in the description or note."),
        "origin": {"type": "string", "enum": ["self", "assigned"], "description": "Optional: whether you found this work yourself or it was assigned."},
    }),
    tool("work_list", "Inspect ongoing or completed work.", {"status": text_property()}),
    tool("work_get", "Read one assignment and its latest state. shared_with (present only when shared) lists, per linked question, your other waiting works that share it.", {"id": text_property()}, ["id"]),
    tool("floor_accept", "MTG Floor F3: turn ONE promise you issued in a meeting (a cid from the meeting's wrap event, field issued) into work. The only way to record a meeting promise as work: it copies the promise text verbatim (you choose only title and priority), sets origin meeting, cites the wrap event and journal line, and is refused unless the meeting has ended, its journal chain matches the wrap head, and the human did not decline it on the panel. Repeating it for the same cid returns the same work. A promise without a cid is not accepted here: ask the human (question_ask) before recording it.", {
        "meeting": text_property("The meeting ID from the wrap event."),
        "cid": text_property("The issued cid (cid-...)."),
        "title": text_property("Short title for the work."),
        "priority": {"type": "integer", "description": "Optional priority (default 0)."},
    }, ["meeting", "cid", "title"]),
    tool("question_ask", "Queue a question addressed to a colleague and remember that you await their answer. Configured adapters may deliver it; queuing is not proof of receipt. Linked work waits until the answer arrives.", {
        "to": text_property(), "body": text_property(), "work_id": text_property(), "key": KEY,
        "recipient_work_id": text_property("Optional: the recipient's own work ID this concerns, if they gave you one. work_id is always your own work; recipient_work_id is theirs and only a hint to them unless the delivery is authenticated."),
        "thread": text_property("Preserve the incoming conversation ID when asking in that conversation."),
        "in_reply_to": text_property("Message/event being replied to; separate from the question ID used to correlate its eventual answer."),
        "project": text_property("Optional: one project this message is for, when you hold a write grant on it. The recipient captures it into that project only if the channel is unbound; your own body is captured too. Omitted: when work_id is your own work published to a project you can write, that project is used (the result's project_default says so, or why not). Pass \"\" for a message about several projects or none."),
        "work_ids": {"type": "array", "items": {"type": "string"}, "description": "Optional: several of your own works this one question blocks (a shared blocker). Each waits on it and any answer wakes all of them; work_id defaults to the first."},
        "relays": {"type": "array", "items": {"type": "string"}, "description": "Optional: when this question carries others onward (you ask David what a colleague asked you), the colleague's question IDs from the question-id: header of their received messages. Each asker then sees where their question is (with-<recipient>, answered-pending-relay); you still answer them yourself."},
    }, ["to", "body", "key"]),
    tool("question_relays", "Link received questions (their question-id: header IDs) to one of your own open or answered questions that carries them onward. Each asker is told the hop automatically; an answered question means you owe them the relay (relay_owed).", {
        "question_id": text_property("Your question that carries them."),
        "add": {"type": "array", "items": {"type": "string"}, "description": "Question IDs from received messages' question-id: headers."},
    }, ["question_id", "add"]),
    tool("message_send", "Queue a message from your own identity in the outbox for a configured delivery adapter. Queuing is not proof of delivery. Check earlier messages before repeating one.", {
        "to": text_property(), "body": text_property(), "work_id": text_property(), "key": KEY,
        "recipient_work_id": text_property("Optional: the recipient's own work ID this concerns, if they gave you one. work_id is always your own work; recipient_work_id is theirs and only a hint to them unless the delivery is authenticated."),
        "thread": text_property("Preserve the incoming conversation ID when replying in that conversation."),
        "in_reply_to": text_property("Message/event being replied to; this metadata does not answer an open question."),
        "project": text_property("Optional: one project this message is for, when you hold a write grant on it. The recipient captures it into that project only if the channel is unbound; your own body is captured too. Omitted: when work_id is your own work published to a project you can write, that project is used (the result's project_default says so, or why not). Pass \"\" for a message about several projects or none."),
    }, ["to", "body", "key"]),
    tool("question_resolve", "Record that a message you already received answers one of your open questions, when it arrived without correlation (an ordinary reply). The message must come from the question's recipient and arrive after the question; linked work wakes. Do not use it for messages from anyone else.", {
        "question_id": text_property("Your open question."),
        "event_id": text_property("The received message event that answers it."),
    }, ["question_id", "event_id"]),
    tool("question_close", "Close one of your own open questions without the recipient's answer. With event_id: someone other than the recipient answered for them (a received message after the ask); status answered_by_proxy, the answer's auth is kept as received and stays secondhand, linked work wakes. Without event_id: status withdrawn (obsolete question); work never wakes, unlinked_from lists every work it was removed from, and work_unlinked those left with no linked question, which then need a real blocker or wake_at. reason is required. The recipient's own reply belongs in question_resolve.", {
        "question_id": text_property("Your open question."),
        "reason": text_property("Why it is closed: who answered for the recipient, or why the question is obsolete."),
        "event_id": text_property("Optional: the received message that answers it on the recipient's behalf."),
    }, ["question_id", "reason"]),
    tool("runtime_restart", "Ask for your own worker to restart, optionally with changed runtime settings. The worker restarts only after this session ends, at an idle point; it never kills a running session. You may change model and claude (within the allowed lists), budget (up to your baseline), timeout, poll and retry, and lower daily_limit. The first session after the restart is probation: if it fails, the previous settings return. See runtime in employee_context for current settings and request outcomes.", {
        "reason": text_property("Why the restart or change is needed."),
        "key": KEY,
        "settings": {"type": "object", "additionalProperties": True, "description": "Optional changes: model, claude, budget, timeout, poll, retry, daily_limit. Omit to restart with the current settings."},
    }, ["reason", "key"]),
    tool("outbox_list", "Inspect queued messages and questions as budgeted entries or JSON chunks. Follow next_cursor until null; older_before selects an older window. Queued does not mean delivered; each item's delivery shows pending, uncertain, failed or delivered as recorded by the channel that carries it.", {
        "status": {"type": "string", "enum": ["pending", "uncertain", "failed", "delivered"],
                   "description": "Only items whose delivery state is this."},
        "id": text_property("Select one own message ID, including older messages."),
        "before": text_property("Read messages older than this outbox ID, exclusively."),
        "cursor": text_property("Continue concatenating JSON chunks; new messages do not shift the page."),
        "max_bytes": {"type": "integer", "minimum": 1024, "maximum": 32000},
        "limit": {"type": "integer", "minimum": 1, "maximum": 500, "description": "Newest N items (default 50)."},
    }),
    tool("docket", "Read-only: your own open questions to one person (default: this home's default human), oldest first, with asked-age, delivery ack and age, and the works each blocks. text is a paste-ready block. Only your home's data.", {
        "person": text_property("Person ID, name or alias; default the home's default human."),
    }),
    tool("routine_rearm", "Prove a routine's review and re-arm it (RTN, docs/routines.md). Call it only after actually doing the review its guidance in routines_due asks for: goals, blockers and next steps checked, or \"no change\" and why. One call stores the review and sets the routine waiting at its next anchor-based wake (routines_due shows rearm_wake_at). Accepted only while routines_due lists the routine (fired, quarantined, or due within 15 min). work_put cannot change a routine's status or wake_at. A routine still ready when your session ends is quarantined and counted as a discipline failure.", {
        "work_id": text_property("The routine work ID from routines_due."),
        "cycle": text_property("routines_due[].cycle for this routine, unchanged. A re-arm is compare-and-set on it: a repeat or stale call (the cycle was already closed) is refused, never applied twice."),
        "review": text_property("What you reviewed and concluded (at most 2048 characters)."),
        "covered_through": text_property("Dream only: the timestamp your review actually covered up to. At most this session's evidence_cutoff (its start) and never past an omission's from; omit it to leave the checkpoint unchanged."),
        "omissions": {"type": "array", "description": "Dream only: what you did not read (a truncated, capped or since= retrieval that did not reach the cutoff). Each {reason, from?}; from = the timestamp of the earliest unread material.",
                      "items": {"type": "object", "properties": {"reason": {"type": "string"}, "from": {"type": "string"}},
                                "required": ["reason"]}},
    }, ["work_id", "review", "cycle"]),
    tool("reminder_set", "Arrange a future wake-up for something you owe. Time must have an explicit timezone. Reminders survive restarts.", {
        "at": text_property(), "reason": text_property(), "work_id": text_property(), "key": KEY,
    }, ["at", "reason", "key"]),
]

from . import projecttools  # noqa: E402  (M21 project memory tools)

TOOLS.extend(projecttools.TOOLS)
from . import rostertools  # noqa: E402  (M26 roster tools)

TOOLS.extend(rostertools.TOOLS)
from . import glance  # noqa: E402  (RW-1 Glance)

TOOLS.extend(glance.TOOLS)
from . import click  # noqa: E402  (stage 2: one approved, named click)

TOOLS.extend(click.TOOLS)
from . import floortools  # noqa: E402  (MTG F2: meeting runs only)

METHODS = {entry["name"]: entry for entry in TOOLS + floortools.TOOLS}
MUTATIONS = {"entity_put", "memory_put", "work_put", "floor_accept", "question_ask", "question_resolve", "question_close", "question_relays", "message_send", "reminder_set", "routine_rearm",
             "runtime_restart"} | projecttools.MUTATIONS | rostertools.MUTATIONS | glance.MUTATIONS \
    | click.MUTATIONS | floortools.MUTATIONS


def validate_arguments(specification, arguments):
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be an object.")
    schema = specification["inputSchema"]
    for name in schema["required"]:
        if name not in arguments:
            raise ValueError("Missing required argument: %s" % name)
    for name, value in arguments.items():
        if name not in schema["properties"]:
            raise ValueError("Unexpected argument: %s" % name)
        validate_value(name, schema["properties"][name], value)


def validate_value(name, field, value):
    expected = field["type"]
    correct_type = (
        (expected == "string" and isinstance(value, str))
        or (expected == "integer" and isinstance(value, int) and not isinstance(value, bool))
        or (expected == "boolean" and isinstance(value, bool))
        or (expected == "array" and isinstance(value, list))
        or (expected == "object" and isinstance(value, dict))
    )
    if not correct_type:
        raise ValueError("Argument %s must be %s." % (name, expected))
    if "enum" in field and value not in field["enum"]:
        raise ValueError("Invalid value for %s." % name)
    if "minimum" in field and value < field["minimum"]:
        raise ValueError("Argument %s is too small." % name)
    if "maximum" in field and value > field["maximum"]:
        raise ValueError("Argument %s is too large." % name)
    if expected == "array":
        for index, item in enumerate(value):
            validate_value("%s[%d]" % (name, index), field["items"], item)
    if expected == "object":
        properties = field.get("properties", {})
        for required in field.get("required", []):
            if required not in value:
                raise ValueError("Argument %s is missing %s." % (name, required))
        for key, item in value.items():
            if key not in properties and field.get("additionalProperties") is True:
                continue  # Free-form object, validated by the store method.
            if key not in properties:
                raise ValueError("Unexpected field %s in %s." % (key, name))
            validate_value("%s.%s" % (name, key), properties[key], item)


def offered(home):
    """The tools this home is offered: screen_glance only when the home opts in (RW-1 C1, glance.json), the click
    tools only when it opts in to clicks (click.json)."""
    hidden = (set() if glance.enabled(home) else glance.NAMES) | (set() if click.enabled(home) else click.NAMES)
    return [entry for entry in TOOLS if entry["name"] not in hidden]


# VIS C1: tools whose success rewrites <home>/shared/status.json.
STATUS_TOOLS = frozenset({"work_put", "floor_accept", "routine_rearm", "question_ask", "question_close", "question_resolve", "question_relays",
                          "screen_glance",  # RW-1: Lookout's Recent glances
                          "screen_click_request", "screen_click"})  # stage 2: Lookout's Recent clicks


class Server:
    def __init__(self, store, run_id=None):
        self.store = store
        self.run_id = run_id
        self.tool_use_id = None  # E4a: set by respond() for the duration of one tools/call
        # MTG F2 (C5): a meeting run sees only floortools.MEETING_TOOLS. Decided from the run row the attach created,
        # never from anything the model sends.
        self.meeting = None
        self.text_mode = "full"
        if run_id:
            try:
                run = store.run_get(run_id)
            except Exception:  # noqa: BLE001 - unknown run: ordinary tools; ownership is checked on each write
                run = None
            if run and run.get("kind") == "meeting":
                self.meeting = run.get("meeting")
                self.text_mode = run.get("text_mode") or "full"  # C8: the meeting's heard-text mode

    def tools(self):
        if self.meeting:
            return floortools.filter_tools(TOOLS + floortools.TOOLS)
        return offered(self.store.home)

    def call(self, name, arguments):
        # #64 S1e: a board_read result counts the notice in its byte bound, so it gets the one read here and no later one.
        early = self._yield_notice() if name == "board_read" else self._NO_VALUE
        try:
            if (name not in METHODS or (name in glance.NAMES and not glance.enabled(self.store.home))
                    or (name in click.NAMES and not click.enabled(self.store.home))):
                raise ValueError("Unknown tool: %s" % name)
            if (self.meeting and name not in floortools.MEETING_TOOLS) or (
                    not self.meeting and name in floortools.NAMES):
                raise ValueError("Unknown tool: %s" % name)  # MTG F2 C5: the meeting allow-list, in code
            validate_arguments(METHODS[name], arguments)
            self.store.resolved = {}
            # Store(home, run_id=...) validates run ownership within the same write
            # transaction. A separate read here would race recovery of an orphan run.
            if name in projecttools.NAMES:
                value = projecttools.call(self.store, name, arguments,
                                          notice=early if name == "board_read" else None)
            elif name in rostertools.NAMES:
                value = rostertools.call(self.store, name, arguments)
            elif name in glance.NAMES:
                value = glance.call(self.store, name, arguments)
            elif name in click.NAMES:
                value = click.call(self.store, name, arguments)
            elif name in floortools.NAMES:
                value = floortools.call(self.store.home, self.meeting, name, arguments, text_mode=self.text_mode)
            elif name in ("employee_context", "outbox_list"):
                value = boundedreads.page(self.store, name, arguments, self._yield_notice())
            else:
                method = getattr(self.store, name)
                arguments = self._compact_defaults(name, arguments)
                value = method(**arguments)
        except Exception as exc:
            self.receipt(name, arguments, success=False, error=str(exc))
            raise
        if name in STATUS_TOOLS:
            sharedstatus.write(self.store)  # VIS C1: Lookout sees in-session work/question changes; never fails the call
        if self.store.resolved and isinstance(value, dict):
            # Backlog 24 (K6): show the full IDs short prefixes were expanded to.
            value = dict(value, resolved=dict(self.store.resolved))
        if isinstance(value, dict) and name not in ("employee_context", "outbox_list"):
            notice = self._yield_notice() if early is self._NO_VALUE else early
            if notice is not None:
                value = dict(value, **{"yield": notice})  # MTG F1 (C2): before the receipt, so it records it
        self.receipt(name, arguments, success=True, value=value)  # i17 E1: after the value is final
        return value

    YIELD_TEXT = ("a meeting is starting: start no new outbound action (message, question, push); save your next "
                  "step with work_put and end now (stopped at %s)")  # MTG F2: Rowan G6

    def _yield_notice(self):
        """MTG F1: the yield notice when a pending meeting names this session's run. None otherwise (C1: then the
        result is unchanged). A failure to read it never fails the tool call."""
        try:
            pending = self.store.meeting_pending()
        except Exception:  # noqa: BLE001
            return None
        if not pending or self.store.run_id is None or pending.get("yield_run") != self.store.run_id:
            return None
        return self.YIELD_TEXT % pending.get("deadline")

    @staticmethod
    def _compact_defaults(name, arguments):
        """F2: search results fit tool output. Event bodies are previews in query/thread mode (since= pages keep
        full bodies), every search result has a byte budget, and a bare body_offset reads one chunk."""
        if name in ("event_search", "memory_search"):
            arguments = dict({"max_bytes": SEARCH_MAX_BYTES}, **arguments)
        if name == "event_search" and arguments.get("since") is None and arguments.get("cursor") is None:
            arguments = dict({"body_chars": SEARCH_BODY_CHARS}, **arguments)
        if name == "event_get" and "body_offset" in arguments and "body_chars" not in arguments:
            arguments = dict(arguments, body_chars=EVENT_CHUNK_CHARS)
        return arguments

    PRIVATE_TOOLS = ("view_put", "self_note")

    @staticmethod
    def _redact(value):
        """M26 A7: private view/self text never reaches runs/*.mcp.jsonl; keep shape, IDs and sources only."""
        if isinstance(value, dict):
            return {k: (Server._redact(v) if k not in ("text", "note", "reason") or not isinstance(v, str)
                        else "<redacted %d bytes>" % len(v.encode("utf-8"))) for k, v in value.items()}
        if isinstance(value, list):
            return [Server._redact(v) for v in value]
        return value

    _NO_VALUE = object()

    @staticmethod
    def _result_facts(name, value):
        """i17 E1: facts about the text tools/call sends for this value (respond() serialises it the same way).
        Never raises: a failure is recorded as result_error and the call's result is untouched (C4)."""
        try:
            return ledgerline.result_facts(name, value, json.dumps(value, ensure_ascii=False)), None
        except Exception as exc:  # noqa: BLE001 - evidence must never change the tool result
            return None, type(exc).__name__

    def receipt(self, name, arguments, success, error=None, value=_NO_VALUE):
        """Diagnostic evidence of attempted tools, separate from committed effects.

        A successful read receipt proves that a source was requested. It does not
        prove the employee understood it. Durable state remains in SQLite; a log
        failure is visible on stderr without undoing an already-committed effect.
        """
        if not isinstance(self.run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", self.run_id):
            return
        try:
            self.store.run_get(self.run_id)
        except ValueError:
            return
        record = {
            "at": datetime.now(timezone.utc).isoformat(), "run_id": self.run_id,
            "tool": name, "arguments": self._redact(arguments) if name in self.PRIVATE_TOOLS else arguments,
            "success": bool(success),
        }
        if getattr(self, "tool_use_id", None):
            record["tool_use_id"] = self.tool_use_id
        if error is not None:
            record["error"] = error[:1000]
        if value is not self._NO_VALUE:
            facts, problem = self._result_facts(name, value)
            if facts is not None:
                record["result"] = facts
            else:
                record["result_error"] = problem
        try:
            folder = self.store.home / "runs"
            folder.mkdir(exist_ok=True, mode=0o700)
            path = folder / (self.run_id + ".mcp.jsonl")
            descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                os.fchmod(output.fileno(), 0o600)
                fcntl.flock(output.fileno(), fcntl.LOCK_EX)  # i17 E1: read the tail and append as one step
                previous = ledgerline.last_line(path)
                record["prev_sha256"] = ledgerline.sha256_text(previous) if previous is not None else None
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
                output.flush()
                os.fsync(output.fileno())
        except Exception as exc:  # noqa: BLE001 - i17 C4: a receipt failure never fails or alters the call
            print("synthetics MCP receipt failed: %s" % type(exc).__name__, file=sys.stderr)

    def respond(self, message):
        identifier = message.get("id")
        method = message.get("method")
        # Notifications, including cancellation and initialized, have no response.
        if "id" not in message:
            return None
        if message.get("jsonrpc") != "2.0":
            return {"jsonrpc": "2.0", "id": identifier, "error": {"code": -32600, "message": "Invalid JSON-RPC request."}}
        if method == "initialize":
            requested = (message.get("params") or {}).get("protocolVersion")
            protocol = requested if requested in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25") else "2024-11-05"
            result = {"protocolVersion": protocol, "capabilities": {"tools": {}},
                      "serverInfo": {"name": "synthetics", "version": "0.1.0"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": self.tools()}
        elif method == "tools/call":
            params = message.get("params") or {}
            # E4a: the client's tool_use_id joins this call's receipt to what the executor stream shows it
            # displayed. Read only; the response bytes are the same with or without _meta.
            meta = params.get("_meta") if isinstance(params, dict) else None
            use_id = meta.get("claudecode/toolUseId") if isinstance(meta, dict) else None
            self.tool_use_id = use_id if isinstance(use_id, str) and ledgerline.TOOL_USE_ID.fullmatch(use_id) else None
            try:
                value = self.call(params.get("name"), params.get("arguments", {}))
                result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
            except (ValueError, TypeError, KeyError) as exc:
                result = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
            finally:
                self.tool_use_id = None
        else:
            return {"jsonrpc": "2.0", "id": identifier, "error": {"code": -32601, "message": "Method not found."}}
        return {"jsonrpc": "2.0", "id": identifier, "result": result}


def serve(home, run_id=None, input_stream=None, output_stream=None):
    incoming = input_stream or sys.stdin
    outgoing = output_stream or sys.stdout
    store = Store(home, run_id=run_id)
    server = Server(store, run_id=run_id)
    try:
        for line in incoming:
            try:
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("Request must be an object.")
                response = server.respond(message)
            except (ValueError, TypeError):
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error."}}
            except Exception as exc:
                # Keep the service available after a tool/storage failure, while
                # not dumping tracebacks or internal content into protocol output.
                print("synthetics MCP: %s" % type(exc).__name__, file=sys.stderr)
                response = {"jsonrpc": "2.0", "id": message.get("id"), "error": {"code": -32603, "message": "Internal server error."}}
            if response is not None:
                outgoing.write(json.dumps(response, ensure_ascii=False) + "\n")
                outgoing.flush()
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True)
    parser.add_argument("--run-id")
    args = parser.parse_args()
    serve(args.home, args.run_id)


if __name__ == "__main__":
    main()
