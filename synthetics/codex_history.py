"""Read user-visible history from one explicit Codex thread for a sourced handoff.

This does not resume a Codex session or attach to its live executor. The supported
app-server read API can inspect persisted history while the desktop owns a session;
another executor must retain its own durable employee state and recorded handoff.
No old messages are fed to an inbox or replayed through an outbox by this module.
"""

from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import tempfile
import time
from typing import Any, Dict


class CodexHistoryError(RuntimeError):
    """History could not be read completely; do not publish a partial snapshot."""


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _original_time(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise CodexHistoryError("Codex history contains an invalid item timestamp.")
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError) as exc:
        raise CodexHistoryError("Codex history contains an invalid item timestamp.") from exc


class _ReadOnlyAppServer:
    METHODS = frozenset(("initialize", "thread/read", "thread/items/list"))

    def __init__(self, executable, timeout, max_response_bytes):
        self.executable = str(executable)
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.process = None
        self.selector = None
        self.buffer = b""
        self.request_id = 0

    def __enter__(self):
        try:
            self.process = subprocess.Popen(
                [self.executable, "app-server", "--listen", "stdio://"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0, start_new_session=True,
            )
        except OSError as exc:
            raise CodexHistoryError("Could not start Codex app-server for history export.") from exc
        self.deadline = time.monotonic() + self.timeout
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        return self

    def __exit__(self, *unused):
        if self.selector:
            self.selector.close()
        if not self.process:
            return
        # A CLI or authentication helper must not survive this bounded read.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(self.process.pid, sig)
            except ProcessLookupError:
                pass
            if sig == signal.SIGTERM:
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        self.process.wait()
        for pipe in (self.process.stdin, self.process.stdout):
            if pipe:
                pipe.close()

    def _write(self, value):
        try:
            self.process.stdin.write((json.dumps(value) + "\n").encode("utf-8"))
            self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise CodexHistoryError("Codex app-server closed during history export.") from exc

    def initialized(self):
        self._write({"method": "initialized", "params": {}})

    def _read(self):
        while b"\n" not in self.buffer:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise CodexHistoryError("Codex history export timed out.")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise CodexHistoryError("Codex app-server closed during history export.")
            self.buffer += chunk
            if len(self.buffer) > self.max_response_bytes:
                raise CodexHistoryError("Codex history response exceeds the configured size limit.")
        line, self.buffer = self.buffer.split(b"\n", 1)
        try:
            value = json.loads(line)
        except (ValueError, UnicodeError) as exc:
            raise CodexHistoryError("Codex app-server returned invalid JSON.") from exc
        if not isinstance(value, dict):
            raise CodexHistoryError("Codex app-server returned an invalid response.")
        return value

    def request(self, method, params):
        if method not in self.METHODS:
            raise ValueError("The history client permits read-only methods only.")
        self.request_id += 1
        request_id = self.request_id
        self._write({"id": request_id, "method": method, "params": params})
        while True:
            value = self._read()
            if "method" in value:
                if "id" in value:
                    raise CodexHistoryError("Codex requested interaction; history export is read-only.")
                # Notifications can precede a requested response.
                continue
            if value.get("id") != request_id:
                raise CodexHistoryError("Codex app-server returned an unexpected response ID.")
            if "error" in value:
                # Do not include provider error text: it can contain credentials or
                # another part of the user's environment.
                raise CodexHistoryError("Codex app-server rejected %s." % method)
            result = value.get("result")
            if not isinstance(result, dict):
                raise CodexHistoryError("Codex app-server returned an invalid result.")
            return result


def _message(entry, thread_id, human_sender, assistant_sender):
    if not isinstance(entry, dict) or not isinstance(entry.get("item"), dict):
        raise CodexHistoryError("Codex history contains an invalid item entry.")
    item = entry["item"]
    kind = item.get("type")
    if not isinstance(kind, str) or not kind:
        raise CodexHistoryError("Codex history contains an invalid item type.")
    if kind not in ("userMessage", "agentMessage"):
        return None
    item_id, turn_id = item.get("id"), entry.get("turnId")
    if not isinstance(item_id, str) or not item_id or not isinstance(turn_id, str) or not turn_id:
        raise CodexHistoryError("Codex visible history is missing stable item or turn IDs.")
    started, completed = entry.get("startedAtMs"), entry.get("completedAtMs")
    # Validate both timestamps, even when only the first is used for ordering.
    started_iso, completed_iso = _original_time(started), _original_time(completed)
    provenance = {
        "source": "codex-app-server", "thread_id": thread_id,
        "item_id": item_id, "turn_id": turn_id,
        "started_at_ms": started, "completed_at_ms": completed,
    }
    if kind == "userMessage":
        content = item.get("content")
        if not isinstance(content, list) or any(not isinstance(part, dict) for part in content):
            raise CodexHistoryError("Codex user history has invalid content.")
        text = []
        for part in content:
            if not isinstance(part.get("type"), str):
                raise CodexHistoryError("Codex user history has an invalid content type.")
            if part["type"] == "text":
                if not isinstance(part.get("text"), str):
                    raise CodexHistoryError("Codex user history has invalid text.")
                text.append(part["text"])
        body = "\n".join(text)
        provenance.update({"role": "user", "content": content})
        direction, sender = "incoming", human_sender
    else:
        body = item.get("text")
        if not isinstance(body, str):
            raise CodexHistoryError("Codex assistant history has invalid text.")
        phase = item.get("phase")
        if phase is not None and not isinstance(phase, str):
            raise CodexHistoryError("Codex assistant history has an invalid phase.")
        provenance.update({"role": "assistant", "phase": phase})
        direction, sender = "outgoing", assistant_sender
    return {
        "id": item_id, "turn_id": turn_id, "sender": sender,
        "direction": direction, "body": body,
        "occurred_at": started_iso or completed_iso,
        "provenance": provenance,
    }


class CodexHistoryExporter:
    """Export only an explicitly selected thread, without any reasoning calls.

    Page exhaustion means all history made available by this installed app-server
    was read. It does not prove deleted/compacted/unpersisted material exists, and
    the live tail can advance while pages are being read. Hidden reasoning and
    tool records are excluded rather than represented as assistant statements.
    """

    def __init__(self, executable="codex", timeout=30, max_pages=1000,
                 max_response_bytes=32 * 1024 * 1024):
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("History export timeout must be positive.")
        for name, value in (("max_pages", max_pages), ("max_response_bytes", max_response_bytes)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("%s must be a positive integer." % name)
        self.executable = executable
        self.timeout = timeout
        self.max_pages = max_pages
        self.max_response_bytes = max_response_bytes

    def export(self, thread_id, human_sender, assistant_sender="codex-manager") -> Dict[str, Any]:
        for label, value in (("thread_id", thread_id), ("human_sender", human_sender), ("assistant_sender", assistant_sender)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("%s must be an explicit nonempty string." % label)
        started_at = _utc_now()
        messages = []
        omitted = Counter()
        cursors = set()
        item_ids = set()
        missing_times = 0
        with _ReadOnlyAppServer(self.executable, self.timeout, self.max_response_bytes) as server:
            server.request("initialize", {
                "clientInfo": {"name": "synthetics_history_export", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            })
            server.initialized()
            result = server.request("thread/read", {"threadId": thread_id, "includeTurns": False})
            thread = result.get("thread")
            if not isinstance(thread, dict) or thread.get("id") != thread_id:
                raise CodexHistoryError("Codex returned a different or invalid thread.")
            metadata = {key: thread.get(key) for key in ("id", "cwd", "source", "status", "createdAt", "updatedAt")}
            params = {"threadId": thread_id, "sortDirection": "asc", "limit": 100}
            for page_number in range(1, self.max_pages + 1):
                result = server.request("thread/items/list", params)
                data = result.get("data")
                if not isinstance(data, list):
                    raise CodexHistoryError("Codex history page is invalid.")
                for entry in data:
                    message = _message(entry, thread_id, human_sender, assistant_sender)
                    if message is None:
                        omitted[entry["item"]["type"]] += 1
                        continue
                    if message["id"] in item_ids:
                        raise CodexHistoryError("Codex history repeated a visible item ID.")
                    item_ids.add(message["id"])
                    messages.append(message)
                    if message["occurred_at"] is None:
                        missing_times += 1
                cursor = result.get("nextCursor")
                if cursor is None:
                    break
                if not isinstance(cursor, str) or not cursor or cursor in cursors:
                    raise CodexHistoryError("Codex history returned an invalid or repeated page cursor.")
                cursors.add(cursor)
                params["cursor"] = cursor
            else:
                raise CodexHistoryError("Codex history exceeds the configured page limit; no complete snapshot produced.")
        warnings = [
            "This is a sourced handoff of available user-visible history, not a resumed desktop execution session.",
            "Hidden reasoning, tool records and binary attachments are excluded; attachment references are retained.",
            "The server may omit unavailable history; active conversations can advance after or during export.",
        ]
        if omitted.get("contextCompaction"):
            warnings.append("The available history contains context-compaction markers; page exhaustion cannot certify earlier unavailable content.")
        if missing_times:
            warnings.append("%d visible messages have no recorded original timestamp; none was invented." % missing_times)
        return {
            "schema_version": 1, "source": "codex-app-server",
            "thread_id": thread_id, "thread": "codex:" + thread_id,
            "started_at": started_at, "exported_at": _utc_now(),
            "metadata": metadata, "messages": messages,
            "available_history_exhausted": True, "page_count": page_number,
            "omitted_item_types": dict(omitted), "warnings": warnings,
        }

    def export_to(self, thread_id, path, human_sender, assistant_sender="codex-manager"):
        snapshot = self.export(thread_id, human_sender, assistant_sender)
        write_snapshot(snapshot, path)
        return snapshot


def write_snapshot(snapshot, path):
    """Atomically publish a complete private snapshot; errors preserve old content."""
    if not isinstance(snapshot, dict) or snapshot.get("available_history_exhausted") is not True:
        raise ValueError("Only an exhausted history snapshot can be published.")
    target = Path(path).expanduser().absolute()
    encoded = (json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix="." + target.name + ".", dir=str(target.parent))
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
        # Make the rename durable on filesystems that implement directory fsync.
        directory = os.open(str(target.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            os.unlink(temporary)
