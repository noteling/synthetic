"""Small stdio MCP boundary for one employee's durable operations.

Run with ``python -m synthetics.mcp --home PATH``. Protocol output alone goes to
stdout; both the operator and the CLI adapter may launch this process.
"""

import argparse
from datetime import datetime, timezone
import json
import os
import re
import sys

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


KEY = text_property("Stable descriptive key for this intended operation. Reuse after interruption; a different payload with the same key is rejected.")
TOOLS = [
    tool("employee_context", "Read current identity, relationships, memories, ongoing work, pending inputs, questions, and recent runs."),
    tool("event_get", "Read the original source event by ID, including messages already processed in earlier sessions. Use this to verify the evidence behind a memory. A unique ID prefix of 8+ hex characters is accepted.", {
        "id": text_property(),
    }, ["id"]),
    tool("event_search", "Search original source events across sessions, including processed events. Filter by thread when tracing an ongoing conversation.", {
        "query": text_property(), "thread": text_property(),
        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
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
    tool("memory_put", "Retain a sourced fact, belief, preference, or procedure about an entity. Correct older understanding with replaces.", {
        "subject": text_property("Existing entity ID, including your identity ID for yourself."),
        "predicate": text_property("What this memory says about the subject."),
        "value": text_property(),
        "kind": {"type": "string", "enum": ["fact", "belief", "preference", "procedure"]},
        "source": text_property("Input event ID, colleague, file, or other supporting evidence."),
        "key": KEY, "replaces": text_property("ID of the obsolete memory this corrects."),
    }, ["subject", "predicate", "value", "source"]),
    tool("memory_search", "Search your memory across projects. Active memories by default; inspect superseded history explicitly when needed. A full memory ID (or a 15+ character prefix) returns that memory first.", {
        "query": text_property(), "subject": text_property(),
        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        "include_superseded": {"type": "boolean"},
        "match": {"type": "string", "enum": ["any", "all"],
                  "description": "any (default): every-word matches first, then ranked matches on any distinctive word. all: every word must appear."},
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
        "publish": text_property("Optional: project whose Board backlog shows this work (you need a post grant). Published title, status, waiting_for, evidence refs and priority are visible to that project; description and note never are. \"\" unpublishes."),
        "origin": {"type": "string", "enum": ["self", "assigned"], "description": "Optional: whether you found this work yourself or it was assigned."},
    }),
    tool("work_list", "Inspect ongoing or completed work.", {"status": text_property()}),
    tool("work_get", "Read one assignment and its latest state. shared_with (present only when shared) lists, per linked question, your other waiting works that share it.", {"id": text_property()}, ["id"]),
    tool("question_ask", "Queue a question addressed to a colleague and remember that you await their answer. Configured adapters may deliver it; queuing is not proof of receipt. Linked work waits until the answer arrives.", {
        "to": text_property(), "body": text_property(), "work_id": text_property(), "key": KEY,
        "recipient_work_id": text_property("Optional: the recipient's own work ID this concerns, if they gave you one. work_id is always your own work; recipient_work_id is theirs and only a hint to them unless the delivery is authenticated."),
        "thread": text_property("Preserve the incoming conversation ID when asking in that conversation."),
        "in_reply_to": text_property("Message/event being replied to; separate from the question ID used to correlate its eventual answer."),
        "project": text_property("Optional: one project this message is for, when you hold a write grant on it. The recipient captures it into that project only if the channel is unbound; your own body is captured too. Leave it out for messages about several projects."),
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
        "project": text_property("Optional: one project this message is for, when you hold a write grant on it. The recipient captures it into that project only if the channel is unbound; your own body is captured too. Leave it out for messages about several projects."),
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
    tool("outbox_list", "Inspect messages and questions you have already queued. Queued does not mean delivered; each item's delivery shows pending, uncertain, failed or delivered as recorded by the channel that carries it.", {
        "status": {"type": "string", "enum": ["pending", "uncertain", "failed", "delivered"],
                   "description": "Only items whose delivery state is this."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 500, "description": "Newest N items (default 50)."},
    }),
    tool("docket", "Read-only: your own open questions to one person (default: this home's default human), oldest first, with asked-age, delivery ack and age, and the works each blocks. text is a paste-ready block. Only your home's data.", {
        "person": text_property("Person ID, name or alias; default the home's default human."),
    }),
    tool("reminder_set", "Arrange a future wake-up for something you owe. Time must have an explicit timezone. Reminders survive restarts.", {
        "at": text_property(), "reason": text_property(), "work_id": text_property(), "key": KEY,
    }, ["at", "reason", "key"]),
]

from . import projecttools  # noqa: E402  (M21 project memory tools)

TOOLS.extend(projecttools.TOOLS)
from . import rostertools  # noqa: E402  (M26 roster tools)

TOOLS.extend(rostertools.TOOLS)
METHODS = {entry["name"]: entry for entry in TOOLS}
MUTATIONS = {"entity_put", "memory_put", "work_put", "question_ask", "question_resolve", "question_close", "question_relays", "message_send", "reminder_set",
             "runtime_restart"} | projecttools.MUTATIONS | rostertools.MUTATIONS


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


class Server:
    def __init__(self, store, run_id=None):
        self.store = store
        self.run_id = run_id

    def call(self, name, arguments):
        try:
            if name not in METHODS:
                raise ValueError("Unknown tool: %s" % name)
            validate_arguments(METHODS[name], arguments)
            self.store.resolved = {}
            # Store(home, run_id=...) validates run ownership within the same write
            # transaction. A separate read here would race recovery of an orphan run.
            if name in projecttools.NAMES:
                value = projecttools.call(self.store, name, arguments)
            elif name in rostertools.NAMES:
                value = rostertools.call(self.store, name, arguments)
            else:
                method = self.store.context if name == "employee_context" else getattr(self.store, name)
                if name == "outbox_list":
                    arguments = dict({"limit": 50}, **arguments)  # backlog 6: never the whole outbox by default
                value = method(**arguments)
        except Exception as exc:
            self.receipt(name, arguments, success=False, error=str(exc))
            raise
        self.receipt(name, arguments, success=True)
        if self.store.resolved and isinstance(value, dict):
            # Backlog 24 (K6): show the full IDs short prefixes were expanded to.
            value = dict(value, resolved=dict(self.store.resolved))
        return value

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

    def receipt(self, name, arguments, success, error=None):
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
        if error is not None:
            record["error"] = error[:1000]
        try:
            folder = self.store.home / "runs"
            folder.mkdir(exist_ok=True, mode=0o700)
            path = folder / (self.run_id + ".mcp.jsonl")
            descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
                output.flush()
                os.fsync(output.fileno())
        except (OSError, TypeError, ValueError) as exc:
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
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = message.get("params") or {}
            try:
                value = self.call(params.get("name"), params.get("arguments", {}))
                result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
            except (ValueError, TypeError, KeyError) as exc:
                result = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
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
