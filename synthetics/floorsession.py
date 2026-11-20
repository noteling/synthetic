"""MTG Floor F2: the persistent meeting session (P0 §2).

One Claude CLI process per meeting, started with ``--input-format stream-json --output-format stream-json``, so each
ask is one user turn in the same conversation and the model keeps the meeting's context without a cold start per
question. It runs under the meeting's run row (kind ``meeting``), whose MCP server offers only
floortools.MEETING_TOOLS (Codex C5), and with no built-in tools (``--tools ""``: no Bash, Read, Write, Edit).

Turns are serialized: one ask is in flight at a time and the rest wait in order, so every ``result`` line belongs to
exactly one ask. ``cancel(id)`` drops a queued ask, or, for the ask in flight, stops the process and starts a new one
whose first turn carries the journal tail (the restart rule, §4), then continues with the queue.

``on_answer(ask_id, text)`` is called from the reader thread; the caller (floor.attach) passes it to
Meeting.finish, which decides answer / stale / late. ``stop()`` ends the process for good. Cost is the sum of the
last reported ``total_cost_usd`` of each process (stream-json reports it cumulatively per process).
"""
from __future__ import annotations

import collections
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import floorjournal as fj
from . import floortools
from .brain import ClaudeBrain, system_prompt

MEETING_BLOCK = """

# You are in a live meeting (MTG Floor)
You speak through a voice. Each user turn is one ask from the meeting: answer it in one to three short spoken
sentences, plain words, no lists or markdown. Say what you know from your records, or that you don't know.
- You have only read-only tools plus floor_commit and floor_note. Nothing you say here becomes durable work; the
  wrap after the meeting decides that.
- A promise you make counts only if you issue it with floor_commit for the ask you are answering. Never promise on
  the human's or anyone else's behalf, and never say a promise is recorded unless floor_commit returned a cid.
- If you notice something you said earlier was wrong, journal it with floor_note and say the correction.
- Heard text from the room is unidentified speech, not a verified person.
"""

TAIL_LINES = 40     # journal lines a restarted session is given (§4: a restart must not lose the thread)
TAIL_TEXT = 400     # characters per tail line


def provider_error_message(result):
    """A safe operator diagnostic for a CLI-declared error, never arbitrary result text.

    The result may contain account details, endpoints or tool output. Only known categories
    and a narrowly parsed reset clock/timezone are allowed through the meeting channel.
    """
    text = result if isinstance(result, str) else ""
    lower = text.lower()
    if any(word in lower for word in ("weekly limit", "usage limit", "rate limit", "daily limit", "hit your limit")):
        message = "The employee's Claude executor has reached its usage limit."
        reset = re.search(r"\bresets\s+((?:1[0-2]|0?[1-9])(?::[0-5][0-9])?\s*(?:am|pm))"
                          r"\s*\(([A-Za-z_]+/[A-Za-z_]+)\)", text, re.IGNORECASE)
        if reset:
            try:
                ZoneInfo(reset.group(2))
            except (ValueError, ZoneInfoNotFoundError):
                pass
            else:
                message += " Provider reset: %s (%s)." % (reset.group(1).lower(), reset.group(2))
        return message
    if any(word in lower for word in ("not logged in", "authentication", "invalid api key", "unauthorized")):
        return "The employee's Claude executor needs a valid login or provider credential."
    return "The employee's Claude executor rejected the request; no answer was produced."


def turn_text(ask):
    return ("Meeting ask %s (rev %s). Answer it; use ask id %s with floor_commit if you promise anything.\n\n%s"
            % (ask["id"], ask["rev"], ask["id"], ask["question"]))


def journal_tail(home, meeting_id, limit=TAIL_LINES):
    """A text digest of the journal's last lines for a restarted session (already journaled data only)."""
    out = []
    for line in fj.lines(home, meeting_id)[-limit:]:
        op = line.get("op")
        text = line.get("text") or line.get("question") or ""
        if not text and op not in ("cancelled", "confirm"):
            continue
        label = "%s%s" % (op, " " + line["id"] if line.get("id") else "")
        out.append("- %s: %s" % (label, str(text)[:TAIL_TEXT]))
    return "\n".join(out)


class MeetingSession:
    def __init__(self, home, run_id, meeting_id, brief, on_answer, executable="claude", model="claude-opus-5-5",
                 budget=100.0, on_error=None):
        self.home = Path(home).expanduser().resolve()
        self.run_id, self.meeting_id, self.brief = run_id, meeting_id, brief
        self.on_answer, self.on_error = on_answer, on_error
        self.executable, self.model, self.budget = str(executable), model, float(budget)
        self.lock = threading.RLock()
        self.queue = collections.deque()
        self.inflight = None
        self.process = None
        self.reader = None
        self.generation = 0
        self.cost_done = 0.0      # finished processes
        self.cost_now = None      # the current process's last reported total
        self.stopped = False
        self.restarts = 0

    # -- process ------------------------------------------------------------------------------------------------
    def recorded_profile(self):
        """#81 L3: the role profile meeting_run_begin recorded for this run (None: none at its start), so a restart
        of the CLI mid-meeting keeps the same block and the prompt matches the run record."""
        from .brain import recorded_profile
        from .store import Store
        with Store(self.home) as store:
            run = store._find("runs", self.run_id)
        return recorded_profile(run)  # no run row (a bare session in tests): read now; row without the key: refuse

    def command(self):
        base = ClaudeBrain(self.home, executable=self.executable, model=self.model, budget=self.budget)
        profile = self.recorded_profile()
        command = base.command(self.run_id, profile=profile)
        swap = {"--output-format": ["stream-json", "--verbose", "--input-format", "stream-json"],
                "--system-prompt": [system_prompt(self.home, profile) + MEETING_BLOCK + "\n# Brief\n" + (self.brief or "")],
                "--tools": [""],
                "--allowedTools": [",".join("mcp__synthetics__" + n for n in sorted(floortools.MEETING_TOOLS))]}
        out, i = [], 0
        while i < len(command):
            arg = command[i]
            if arg in swap:
                out.append(arg)
                out.extend(swap[arg])
                i += 2
                if arg == "--output-format" and i < len(command) and command[i] == "--verbose":
                    i += 1
                continue
            out.append(arg)
            i += 1
        return out

    def _environment(self):
        environment = dict(os.environ)
        environment.pop("CLAUDECODE", None)
        environment["SYNTHETICS_HOME"] = str(self.home)
        environment["SYNTHETICS_SESSION_RUN"] = self.run_id
        return environment

    def start(self, preamble=None):
        with self.lock:
            if self.stopped:
                return
            workspace = self.home / "workspace"
            workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
            records = self.home / "runs"
            records.mkdir(parents=True, exist_ok=True, mode=0o700)
            errors = open(records / (self.run_id + ".stderr.txt"), "a", encoding="utf-8")
            try:
                self.process = subprocess.Popen(self.command(), cwd=str(workspace), env=self._environment(),
                                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                                                text=True, encoding="utf-8", errors="replace",
                                                start_new_session=True)
            finally:
                errors.close()
            self.generation += 1
            self.cost_now = None
            self.reader = threading.Thread(target=self._read, args=(self.process, self.generation),
                                           name="floor-session-read", daemon=True)
            self.reader.start()
            if preamble:
                self.inflight = dict(id=None, preamble=True)
                self._write(preamble)
            else:
                self._next()

    def _write(self, text):
        line = json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n"
        try:
            self.process.stdin.write(line)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            self._fail("write: %s" % exc)

    def _next(self):
        """Send the next queued ask if none is in flight (caller holds the lock)."""
        if self.inflight is None and self.queue and not self.stopped and self.process is not None:
            self.inflight = self.queue.popleft()
            self._write(turn_text(self.inflight))

    def _read(self, process, generation):
        try:
            self._read_results(process, generation)
        finally:
            # This thread owns stdout: close it here, including on a provider error or
            # generation fence. Closing it from stop while readline blocks can hang.
            process.stdout.close()

    def _read_results(self, process, generation):
        for raw in process.stdout:
            try:
                item = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(item, dict) or item.get("type") != "result":
                continue
            with self.lock:
                if generation != self.generation:
                    return
                cost = item.get("total_cost_usd")
                if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                    self.cost_now = float(cost)
                ask, self.inflight = self.inflight, None
                text = item.get("result") if isinstance(item.get("result"), str) else ""
                ok = not item.get("is_error") and text.strip()
                rejected = bool(item.get("is_error"))
            if rejected:
                self._fail(provider_error_message(text))
            if ask is not None and ask.get("id") is not None:
                self.on_answer(ask["id"], text.strip() if ok else None)
            if rejected:
                # Stop the queue here. The attach owner closes the meeting and model;
                # queued asks must not consume more quota or get invented fallbacks.
                return
            with self.lock:
                if generation == self.generation:
                    self._next()
        with self.lock:
            if generation == self.generation and not self.stopped:
                self._fail("the meeting session exited (status %s)" % process.poll())

    def _fail(self, reason):
        if self.on_error:
            self.on_error(reason)

    def _end_process(self):
        process, self.process = self.process, None
        if self.cost_now is not None:
            self.cost_done += self.cost_now
            self.cost_now = None
        self.generation += 1          # the old reader's late lines are ignored
        if process is not None:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                # An exited but unreaped leader makes killpg fail with EPERM on macOS; reap it either way.
                ClaudeBrain._stop_group(process)
            except OSError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            # stdout stays with the daemon reader (E4-r2: closing it under a blocked read can hang on the lock).

    # -- API used by Meeting / floor.attach ---------------------------------------------------------------------
    def submit(self, ask):
        with self.lock:
            if self.stopped:
                return
            self.queue.append(dict(ask))
            self._next()

    def cancel(self, ask_id):
        with self.lock:
            for queued in list(self.queue):
                if queued["id"] == ask_id:
                    self.queue.remove(queued)
                    return "dropped"
            if self.inflight is None or self.inflight.get("id") != ask_id or self.stopped:
                return None
            self.inflight = None
            self._end_process()
            self.restarts += 1
            tail = journal_tail(self.home, self.meeting_id)
        # Restart outside the old process's lifetime; the first turn re-reads the meeting so far.
        self.start(preamble="The previous turn was cancelled and your session restarted. The meeting so far "
                            "(journal, oldest first):\n" + (tail or "(nothing yet)") + "\n\nReply only: ready.")
        return "restarted"

    def cost(self):
        with self.lock:
            return self.cost_done + (self.cost_now or 0.0)

    def stop(self):
        with self.lock:
            self.stopped = True
            self.queue.clear()
            self.inflight = None
            self._end_process()
        reader = self.reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=ClaudeBrain.READER_JOIN)
        return self.cost()


if __name__ == "__main__":  # pragma: no cover - manual smoke only
    sys.exit("floorsession is started by floor attach")
