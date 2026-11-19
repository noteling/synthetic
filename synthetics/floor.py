"""MTG Floor: an employee in a meeting (docs/floor.md). F1: the yield and the attach hold.

`synthetics --home H floor attach --key-file KEY` is started by Stand (the provisioned `stand` channel). It speaks
JSON lines, one object per line, each with "v": 1:

    in:  {"type": "begin", "meeting": ID[, "grace": SECONDS]}   first line
         {"type": "end"}                                          or EOF
    out: {"type": "waiting", "meeting", "yield_run", "deadline"}  a session is running; it was told to yield
         {"type": "ready", "meeting", "hold_expires_at"}           the idle boundary is held for the meeting
         {"type": "ended", "meeting", "reason"}
         {"type": "error", "code", "message"}

While attached it renews the hold every RENEW_SECONDS; the hold expires FLOOR_HOLD_SECONDS after the last renewal,
so a killed attach frees the worker. F2 adds the meeting session and the remaining line types.
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time

from .store import SESSION_ENV, ChannelError, Store, read_credential_file

VERSION = 1
CHANNEL = "stand"           # E5: the provisioned key's channel name
RENEW_SECONDS = 60.0
WAIT_POLL = 0.5             # seconds between attach attempts while the yield target finishes
WAIT_EXTRA = 60.0           # beyond the grace: how long attach waits for the stopped session to be recorded
MEETING_CAP = 90 * 60.0     # F2: a meeting session ends after 90 minutes whatever Stand sends
LINE_POLL = 1.0             # seconds between cap/hold/session checks while waiting for Stand's next line


class Refused(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _emit(stdout, obj):
    stdout.write(json.dumps(dict(v=VERSION, **obj), sort_keys=True) + "\n")
    stdout.flush()


def _audit_refusal(home, command, code):
    """C4: a refused attach is audited in the home (no key, no line content)."""
    try:
        with Store(home) as store:
            store.audit_note("floor.refused", command, dict(code=code, in_session=bool(os.environ.get(SESSION_ENV))))
    except Exception:  # noqa: BLE001 - the refusal itself still stands
        pass


def _authenticate(store, key_file):
    if not key_file:
        raise Refused("auth_failed", "--key-file is required")
    try:
        credential = read_credential_file(key_file)
        channel = store._channel_by_key(credential)
    except (OSError, ValueError, ChannelError) as exc:
        raise Refused("auth_failed", str(exc))
    if channel["id"] != CHANNEL:
        raise Refused("auth_failed", "floor attach needs the provisioned %r channel key." % CHANNEL)
    return channel, credential


def _read_line(stdin):
    line = stdin.readline()
    if not line:
        return None
    try:
        item = json.loads(line)
    except ValueError:
        return {"type": "_bad"}
    if not isinstance(item, dict) or item.get("v") != VERSION:
        return {"type": "_bad"}
    return item


def attach(home, key_file, stdin=None, stdout=None, renew_seconds=None, sleep=time.sleep, session_factory=None,
           cap_seconds=None):
    """Run one attach. Returns the exit status: 0 after a meeting ends, 2 when refused.

    F2: a begin line with `goal` runs the meeting session (_meeting); without one the attach only holds the idle
    boundary until end/EOF, as in F1."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    renew_seconds = RENEW_SECONDS if renew_seconds is None else renew_seconds
    try:
        if os.environ.get(SESSION_ENV):
            raise Refused("in_session", "floor attach is started by the stand channel, never inside an employee "
                                        "session (%s is set)." % SESSION_ENV)
        with Store(home) as store:
            channel, credential = _authenticate(store, key_file)
    except Refused as exc:
        _audit_refusal(home, "attach", exc.code)
        _emit(stdout, dict(type="error", code=exc.code, message=str(exc)))
        return 2
    first = _read_line(stdin)
    if not first or first.get("type") != "begin":
        _emit(stdout, dict(type="error", code="bad_line", message="the first line must be a begin line"))
        return 2
    meeting = first.get("meeting")
    goal = first.get("goal")
    text_mode = first.get("text_mode") or "full"
    if goal is not None and (not isinstance(goal, str) or not goal.strip()):
        _emit(stdout, dict(type="error", code="bad_line", message="goal must be the human's brief (text)"))
        return 2
    if text_mode not in ("full", "hash"):
        _emit(stdout, dict(type="error", code="bad_line", message="text_mode must be full or hash"))
        return 2
    try:
        with Store(home) as store:
            pending = store.meeting_begin(meeting, channel["id"], grace=first.get("grace"))
    except (ValueError, TypeError) as exc:
        _emit(stdout, dict(type="error", code="refused", message=str(exc)))
        return 2
    hold = None
    try:
        if pending.get("yield_run"):
            _emit(stdout, dict(type="waiting", meeting=meeting, yield_run=pending["yield_run"],
                               deadline=pending["deadline"]))
        limit = time.monotonic() + float(pending.get("grace_seconds") or 0) + WAIT_EXTRA
        last_renew = time.monotonic()
        while hold is None:
            with Store(home) as store:
                hold = store.meeting_attach(meeting)
                if hold is None and time.monotonic() - last_renew >= renew_seconds:
                    store.meeting_wait_renew(meeting)
                    last_renew = time.monotonic()
            if hold is None:
                if time.monotonic() > limit:
                    # #74 L3: nobody will attach, so drop the pending row now (audited floor.ended, reason busy)
                    # instead of leaving it to block runs until it expires.
                    with Store(home) as store:
                        store.meeting_end(meeting, reason="busy")
                    _emit(stdout, dict(type="error", code="busy", message="the running session was not recorded "
                                                                          "as ended in time"))
                    return 2
                sleep(WAIT_POLL)
        if goal is not None:
            return _meeting(home, channel, credential, meeting, hold, goal, text_mode, first.get("started_by"),
                            stdin, stdout, renew_seconds, session_factory,
                            MEETING_CAP if cap_seconds is None else cap_seconds)
        _emit(stdout, dict(type="ready", meeting=meeting, hold_expires_at=hold["expires_at"]))
        stop = threading.Event()
        lost = []

        def renew():
            while not stop.wait(renew_seconds):
                try:
                    with Store(home) as store:
                        if store.meeting_renew(hold["token"]) is None:
                            lost.append(True)
                            return
                except Exception:  # noqa: BLE001 - retried; the hold's own expiry is the backstop
                    continue

        renewer = threading.Thread(target=renew, name="floor-renew", daemon=True)
        renewer.start()
        reason = "eof"
        try:
            while True:
                item = _read_line(stdin)
                if item is None:
                    break
                if item.get("type") == "end":
                    reason = "end"
                    break
                _emit(stdout, dict(type="error", code="unsupported", message="F1 accepts only begin and end lines"))
        finally:
            stop.set()
            renewer.join(timeout=5)
        if lost:
            reason = "hold_lost"
        return _end(home, meeting, hold, reason, stdout)
    except BaseException:
        _end(home, meeting, hold, "error", None)
        raise


def _end(home, meeting, hold, reason, stdout):
    with Store(home) as store:
        store.meeting_end(meeting, token=hold["token"] if hold else None, reason=reason)
    if stdout is not None:
        _emit(stdout, dict(type="ended", meeting=meeting, reason=reason))
    return 0


def _default_session(home, run_id, meeting, brief, on_answer, on_error):
    from . import runtime
    from .floorsession import MeetingSession
    effective = ((runtime.read_state(home) or {}).get("effective") or {})
    return MeetingSession(home, run_id, meeting, brief, on_answer, on_error=on_error,
                          executable=effective.get("claude") or "claude",
                          model=effective.get("model") or "claude-opus-5-5",
                          budget=float(effective.get("budget") or 100.0))


def _stdin_lines(stdin, out):
    try:
        while True:
            item = _read_line(stdin)
            out.put(item)
            if item is None:
                return
    except Exception:  # noqa: BLE001 - a broken stdin ends the meeting like EOF
        out.put(None)


def _meeting(home, channel, credential, meeting, hold, goal, text_mode, started_by, stdin, stdout, renew_seconds,
             session_factory, cap_seconds):
    """F2: one meeting under the floor hold (P0 §1-§4). Persona, run row, line dispatch, cap, wrap event."""
    import hashlib

    from . import channel as channel_mod
    from . import floormeeting, floorpersona

    emit_lock = threading.Lock()

    def emit(obj):
        with emit_lock:
            _emit(stdout, obj)

    try:
        with Store(home) as store:
            persona = floorpersona.build(store, goal)
            run = store.meeting_run_begin(meeting, hold["token"], text_mode=text_mode)
    except (floorpersona.PersonaError, ValueError) as exc:
        emit(dict(type="error", code="refused", message=str(exc)))
        return _end(home, meeting, hold, "refused", stdout)
    holder = {}
    failed = []

    def on_answer(ask_id, text):
        room = holder["meeting"]
        lines = room.finish(ask_id, text) if text else room.fail(ask_id, "no usable answer")
        for line in lines:
            emit(line)

    def on_error(reason):
        failed.append(reason)

    factory = session_factory or _default_session
    session = factory(home, run["id"], meeting, persona["brief"], on_answer, on_error)
    room = floormeeting.Meeting(home, meeting, session, text_mode=text_mode, channel=channel["id"])
    holder["meeting"] = room
    room.begin(started_by=started_by if isinstance(started_by, str) else None,
               goal_sha256=hashlib.sha256(goal.encode("utf-8")).hexdigest())
    reason, cost = "error", None
    stop = threading.Event()
    lost = []

    def renew():
        while not stop.wait(renew_seconds):
            try:
                with Store(home) as store:
                    if store.meeting_renew(hold["token"]) is None:
                        lost.append(True)
                        return
            except Exception:  # noqa: BLE001 - retried; the hold's own expiry is the backstop
                continue

    renewer = threading.Thread(target=renew, name="floor-renew", daemon=True)
    lines = queue.Queue()
    reader = threading.Thread(target=_stdin_lines, args=(stdin, lines), name="floor-stdin", daemon=True)
    try:
        try:
            session.start()
        except OSError as exc:
            failed.append("could not start the meeting session: %s" % exc)
        renewer.start()
        if not failed:
            emit(dict(type="ready", meeting=meeting, hold_expires_at=hold["expires_at"], run=run["id"],
                      **persona["ready"]))
        reader.start()
        deadline = time.monotonic() + cap_seconds
        while True:
            if lost:
                reason = "hold_lost"
                break
            if failed:
                emit(dict(type="error", code="session_failed", message=str(failed[0])[:200]))
                reason = "session_failed"
                break
            if time.monotonic() >= deadline:
                reason = "cap"
                break
            try:
                item = lines.get(timeout=min(LINE_POLL, max(0.0, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if item is None:
                reason = "eof"
                break
            if item.get("type") == "end":
                reason = "end"
                break
            if item.get("type") == "_bad":
                emit(dict(type="error", code="bad_line", message="not a v1 JSON object line"))
                continue
            try:
                for line in room.handle(item):
                    emit(line)
            except floormeeting.LineError as exc:
                emit(dict(type="error", code=exc.code, message=str(exc), id=item.get("id")))
    finally:
        room.end()                       # answers from now on are journaled late, never emitted
        try:
            cost = session.stop()
        except Exception:  # noqa: BLE001 - the meeting still closes and the hold is still released
            cost = None
        stop.set()
        if renewer.is_alive():
            renewer.join(timeout=5)
        for ask_id in room.open_asks():
            room.fail(ask_id, "meeting " + reason)
        room.close(reason)
        summary = room.summary()
        wrap = None
        try:
            with Store(home) as store:
                store.run_finish(run["id"], "meeting %s ended (%s)" % (meeting, reason), cost=cost)
                body = json.dumps(dict(summary, reason=reason, run=run["id"]), sort_keys=True)
                prefix = (channel.get("source_prefixes") or [CHANNEL + ":"])[0]
                wrap = channel_mod._inbound_one(store, channel, credential, dict(
                    external_id="floor:%s:wrap" % meeting, sender=channel["id"], body=body,
                    source=prefix + "meeting"))
        except Exception as exc:  # noqa: BLE001 - reported; the hold is released below regardless
            emit(dict(type="error", code="wrap_failed", message=str(exc)[:200]))
        with Store(home) as store:
            store.meeting_end(meeting, token=hold["token"], reason=reason)
        emit(dict(type="ended", meeting=meeting, reason=reason, head=summary.get("head"),
                  wrap_event=(wrap or {}).get("event_id")))
    return 0


def add_parsers(sub):
    floor = sub.add_parser("floor", help="MTG Floor: an employee in a meeting (docs/floor.md); started by the stand "
                                         "channel, refused in sessions")
    floor_sub = floor.add_subparsers(dest="floor_command", required=True)
    attach_parser = floor_sub.add_parser("attach", help="JSON lines on stdin/stdout: begin, then hold the idle "
                                                         "boundary for the meeting until end or EOF")
    attach_parser.add_argument("--key-file", help="the provisioned stand channel key (0600)")
