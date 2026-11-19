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
import sys
import threading
import time

from .store import SESSION_ENV, ChannelError, Store, read_credential_file

VERSION = 1
CHANNEL = "stand"           # E5: the provisioned key's channel name
RENEW_SECONDS = 60.0
WAIT_POLL = 0.5             # seconds between attach attempts while the yield target finishes
WAIT_EXTRA = 60.0           # beyond the grace: how long attach waits for the stopped session to be recorded


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
    return channel


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


def attach(home, key_file, stdin=None, stdout=None, renew_seconds=None, sleep=time.sleep):
    """Run one attach. Returns the exit status: 0 after a meeting ends, 2 when refused."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    renew_seconds = RENEW_SECONDS if renew_seconds is None else renew_seconds
    try:
        if os.environ.get(SESSION_ENV):
            raise Refused("in_session", "floor attach is started by the stand channel, never inside an employee "
                                        "session (%s is set)." % SESSION_ENV)
        with Store(home) as store:
            channel = _authenticate(store, key_file)
    except Refused as exc:
        _audit_refusal(home, "attach", exc.code)
        _emit(stdout, dict(type="error", code=exc.code, message=str(exc)))
        return 2
    first = _read_line(stdin)
    if not first or first.get("type") != "begin":
        _emit(stdout, dict(type="error", code="bad_line", message="the first line must be a begin line"))
        return 2
    meeting = first.get("meeting")
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
                    _emit(stdout, dict(type="error", code="busy", message="the running session was not recorded "
                                                                          "as ended in time"))
                    return 2
                sleep(WAIT_POLL)
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


def add_parsers(sub):
    floor = sub.add_parser("floor", help="MTG Floor: an employee in a meeting (docs/floor.md); started by the stand "
                                         "channel, refused in sessions")
    floor_sub = floor.add_subparsers(dest="floor_command", required=True)
    attach_parser = floor_sub.add_parser("attach", help="JSON lines on stdin/stdout: begin, then hold the idle "
                                                         "boundary for the meeting until end or EOF")
    attach_parser.add_argument("--key-file", help="the provisioned stand channel key (0600)")
