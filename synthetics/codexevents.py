"""Bounded reduction of ``codex exec --json`` events.

Only a completed agent message followed by ``turn.completed`` is a final summary.
Provider failures win over a plausible answer. Raw JSONL is never persisted: the
optional display file holds structural facts, with Ledgerline's hash chain shape.
It does not claim that a tool's returned content was displayed in full.

Wire events are documented at https://learn.chatgpt.com/docs/non-interactive-mode.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import threading


MAX_LINE_BYTES = 1024 * 1024
MAX_SUMMARY_BYTES = 64 * 1024
TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
TOOL_TYPES = frozenset(("command_execution", "mcp_tool_call", "file_change", "web_search", "todo_list"))
QUOTA = re.compile(r"rate[ _-]?limit|usage[ _-]?limit|quota|too many requests|\b429\b", re.I)
AUTH = re.compile(r"authenticat|unauthori[sz]ed|not logged in|log[ -]?in|sign[ -]?in|\b401\b", re.I)


class CodexReducer:
    """One JSONL stream. ``feed`` and ``finish`` are safe across a reader thread.

    ``feed`` takes one complete str/bytes line and does not raise for bad input.
    The pipe reader must also bound its reads; rejecting an oversized line here
    cannot undo the allocation performed by an unbounded ``readline`` upstream.
    ``finish`` freezes late events and returns sanitized completion metadata.
    """

    def __init__(self, path=None):
        self.usage = {}
        self.completed = False
        self.error = None
        self.error_kind = None
        self.malformed_lines = 0
        self.oversized_lines = 0
        self._summary = ""
        self._finished = False
        self._accepting = True
        self._lock = threading.Lock()
        self._handle = None
        self._seq = 0
        self._previous = None
        if path is not None:
            try:
                fd = os.open(str(Path(path)), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                self._handle = os.fdopen(fd, "w", encoding="utf-8")
                os.fchmod(self._handle.fileno(), 0o600)
            except OSError:
                # Display facts are best effort, never a second owner of run success.
                self._handle = None

    @property
    def summary(self):
        return self._summary if self.completed and self.error is None else ""

    def feed(self, line):
        with self._lock:
            if self._finished or not self._accepting:
                return
            try:
                size = len(line) if isinstance(line, bytes) else len(line.encode("utf-8"))
                if size > MAX_LINE_BYTES:
                    self.oversized_lines += 1
                    self._fail("protocol", "Codex emitted an oversized JSON event.")
                    return
                if isinstance(line, bytes):
                    line = line.decode("utf-8")
                if not line.strip():
                    return
                value = json.loads(line)
                if not isinstance(value, dict) or not isinstance(value.get("type"), str):
                    raise ValueError("event type is absent")
            except (ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
                self.malformed_lines += 1
                self._fail("protocol", "Codex emitted an invalid JSON event.")
                return
            self._event(value)

    def _event(self, value):
        kind = value["type"]
        if kind == "thread.started":
            self._write({"kind": "init"})
        elif kind == "turn.started":
            # A subsequent turn cannot succeed using the preceding turn's answer.
            self._summary = ""
            self.usage = {}
            self.completed = False
        elif kind in ("turn.failed", "error"):
            self._provider_failure(value)
        elif kind == "turn.completed":
            self.completed = True
            usage = value.get("usage")
            if isinstance(usage, dict):
                self.usage = {key: usage[key] for key in TOKEN_FIELDS
                              if isinstance(usage.get(key), int) and not isinstance(usage[key], bool)
                              and usage[key] >= 0}
            self._write({"kind": "result", "subtype": "error" if self.error else "success",
                         "is_error": self.error is not None, "usage": dict(self.usage)})
        elif kind in ("item.started", "item.updated", "item.completed"):
            if self.completed:
                # Only messages in the completed turn can be its final answer.
                # A later turn must begin explicitly before accepting new items.
                return
            item = value.get("item")
            if not isinstance(item, dict):
                self.malformed_lines += 1
                self._fail("protocol", "Codex emitted an invalid item event.")
                return
            item_type = item.get("type")
            if not isinstance(item_type, str):
                self.malformed_lines += 1
                self._fail("protocol", "Codex emitted an invalid item event.")
                return
            if item_type == "agent_message" and kind == "item.completed":
                text = item.get("text")
                if not isinstance(text, str):
                    self._fail("protocol", "Codex emitted an invalid final message.")
                    return
                try:
                    size = len(text.encode("utf-8"))
                except UnicodeError:
                    self._fail("protocol", "Codex emitted an invalid final message.")
                    return
                if size > MAX_SUMMARY_BYTES:
                    self._fail("protocol", "Codex final summary exceeded the supported size.")
                    return
                self._summary = text.strip()
                self._write({"kind": "turn", "text_blocks": 1, "text_bytes": size})
            elif item_type in TOOL_TYPES:
                self._write({"kind": "tool", "item_type": item_type, "state": kind.split(".", 1)[1]})
            # Reasoning, tool content, arbitrary future fields and IDs are not retained.

    def _provider_failure(self, value):
        error = value.get("error")
        if isinstance(error, dict):
            parts = (error.get("message"), error.get("code"))
        elif isinstance(error, str):
            parts = (error,)
        else:
            parts = ()
        parts += (value.get("message"), value.get("code"))
        text = " ".join(part[:4096] for part in parts if isinstance(part, str))
        if QUOTA.search(text):
            self._fail("quota", "Codex provider quota or rate limit reached.")
        elif AUTH.search(text):
            self._fail("authentication", "Codex authentication failed; sign in with codex login.")
        else:
            self._fail("provider", "Codex reported a failed turn.")

    def _fail(self, kind, reason):
        if self.error is None:
            self.error_kind, self.error = kind, reason
            self._write({"kind": "stream_error", "stage": "codex", "type": kind})

    def _write(self, fact):
        if self._handle is None:
            return
        self._seq += 1
        fact = dict(fact, provider="codex", seq=self._seq, prev_sha256=self._previous)
        try:
            line = json.dumps(fact, ensure_ascii=False, sort_keys=True)
            self._handle.write(line + "\n")
            self._handle.flush()
            self._previous = hashlib.sha256(line.encode("utf-8")).hexdigest()
        except (OSError, ValueError):
            self._close()

    def detach(self, join_seconds):
        """A reader that did not end cannot later turn an incomplete run into success."""
        with self._lock:
            if self._finished or not self._accepting:
                return
            self._accepting = False
            self._fail("incomplete", "Codex event reader did not finish.")
            self._write({"kind": "reader_detached", "join_seconds": float(join_seconds)})

    def finish(self):
        with self._lock:
            if not self._finished:
                if not self.completed:
                    self._fail("incomplete", "Codex exited without a completed turn.")
                elif not self._summary:
                    self._fail("empty", "Codex completed without a final summary.")
                self._finished = True
                self._close()
            return {"summary": self.summary, "usage": dict(self.usage), "completed": self.completed,
                    "error": self.error, "error_kind": self.error_kind,
                    "malformed_lines": self.malformed_lines, "oversized_lines": self.oversized_lines}

    def close(self):
        self.finish()

    def _close(self):
        if self._handle is not None:
            try:
                self._handle.flush()
                os.fsync(self._handle.fileno())
            except OSError:
                pass
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None
