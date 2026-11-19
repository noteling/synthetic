"""MTG F1 (Floor): a meeting yields the idle boundary. Codex conditions C1-C4, C9 (docs/floor.md)."""
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

try:
    from synthetics import floor
except ImportError:  # fail-first on a base without Floor: each test fails on its own
    floor = None
from synthetics.brain import BrainError, ClaudeBrain
from synthetics.engine import Engine, failure_streak
from synthetics.mcp import Server
from synthetics.store import SESSION_ENV, Store, WorkerHeld, timestamp

ROOT = Path(__file__).resolve().parent.parent
KEY = b"s" * 32
OTHER = b"o" * 32


class Blocking:
    """A brain whose session runs until the engine's yield stop (or `finish`) ends it."""

    def __init__(self, finish_when_pending=False):
        self.started = threading.Event()
        self.stopped = threading.Event()
        self.finish_when_pending = finish_when_pending
        self.home = None
        self.contexts = []

    def yield_stop(self):
        self.stopped.set()

    def run(self, context):
        self.contexts.append(context)
        self.started.set()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.stopped.wait(0.02):
                raise BrainError("Claude CLI exited with status -15.", cost=0.5)
            if self.finish_when_pending:
                with Store(self.home) as store:
                    if store.meeting_pending():
                        return SimpleNamespace(summary="saved my next step", cost=0.2)
        raise AssertionError("never stopped")


class Quick:
    def __init__(self):
        self.contexts = []

    def run(self, context):
        self.contexts.append(context)
        return SimpleNamespace(summary="done", cost=0.01)


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        with Store(self.home) as store:
            store.initialize("Vesper", "maintainer")
            store.channel_register("stand", Store.credential_hash(KEY), ["stand:"], "test")
            store.channel_register("other", Store.credential_hash(OTHER), ["other:"], "test")
        self.key_file = self._key(KEY, "stand.key")
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(SESSION_ENV, None)

    def _key(self, data, name):
        path = Path(self.temp.name) / name
        path.write_bytes(data)
        os.chmod(path, 0o600)
        return str(path)

    def audits(self, prefix="floor."):
        with Store(self.home) as store:
            rows = store.db.execute("SELECT action, record_id, data FROM audit ORDER BY seq").fetchall()
        return [(r["action"], r["record_id"], json.loads(r["data"])) for r in rows if r["action"].startswith(prefix)]

    def feed(self, n=1):
        with Store(self.home) as store:
            return [store.feed("david", "message %d" % i, external_id="m-%d-%f" % (i, time.time()))["id"]
                    for i in range(n)]

    def expire(self, key):
        with Store(self.home) as store:
            row = store.db.execute("SELECT v FROM capture_meta WHERE k=?", (key,)).fetchone()
            value = json.loads(row["v"])
            value["expires_at"] = "2000-01-01T00:00:00.000000Z"
            store.db.execute("UPDATE capture_meta SET v=? WHERE k=?", (json.dumps(value), key))
            store.db.commit()


class C1NoMeetingUnchanged(Base):
    def test_c1_tool_results_and_engine_unchanged_without_meeting(self):
        self.feed(2)
        seen = []

        class Tools:
            def run(inner, context):
                with Store(self.home, run_id=context["run"]["id"]) as bound:
                    server = Server(bound, run_id=context["run"]["id"])
                    seen.append(server.call("work_put", {"title": "x"}))
                    seen.append(server.call("employee_context", {}))
                    self.assertIsNone(getattr(server, "_yield_notice", lambda: None)())
                return SimpleNamespace(summary="ok", cost=0.01)

        finished = Engine(self.home, Tools()).run()
        self.assertEqual(finished[0]["status"], "finished")
        self.assertFalse(any("yield" in value for value in seen))
        self.assertNotIn("yielded", finished[0])
        self.assertEqual(self.audits(), [])
        with Store(self.home) as store:
            self.assertEqual(store.event_pending(), [])
            self.assertIsNone(getattr(store, "meeting_pending", lambda: None)())
            meta = {r["k"] for r in store.db.execute("SELECT k FROM capture_meta").fetchall()}
        self.assertFalse({"meeting_pending", "worker_hold"} & meta)


class C2YieldNotice(Base):
    def test_c2_notice_on_dict_results_and_in_receipt(self):
        self.feed()
        with Store(self.home) as store:
            run = store.run_begin()
            pending = store.meeting_begin("m1", "stand", grace=45)
        self.assertEqual(pending["yield_run"], run["id"])
        with Store(self.home, run_id=run["id"]) as bound:
            server = Server(bound, run_id=run["id"])
            value = server.call("work_put", {"title": "save my next step"})
            listed = server.call("work_list", {})
        self.assertIn("a meeting is starting", value["yield"])
        self.assertIn(pending["deadline"], value["yield"])
        self.assertIsInstance(listed, list)  # list results get no field; the stop covers them
        lines = (self.home / "runs" / (run["id"] + ".mcp.jsonl")).read_text().splitlines()
        receipt = json.loads(lines[0])
        text = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.assertEqual(receipt["result"]["sha256"], hashlib.sha256(text).hexdigest())
        audit = self.audits()
        self.assertEqual(audit[0][0], "floor.meeting_pending")
        self.assertEqual(audit[0][2]["yield_run"], run["id"])

    def test_c2_session_refuses_floor_store_calls(self):
        with Store(self.home) as store:
            run = store.run_begin()
        with Store(self.home, run_id=run["id"]) as bound:
            with self.assertRaises(ValueError):
                bound.meeting_begin("m1", "stand")
            with self.assertRaises(ValueError):
                bound.meeting_attach("m1")


class C3YieldedRun(Base):
    def run_engine_until_yield(self, brain, grace=0.2, meeting="m1"):
        brain.home = self.home
        engine = Engine(self.home, brain)
        engine.YIELD_POLL = 0.02
        out = {}
        thread = threading.Thread(target=lambda: out.setdefault("runs", engine.run()))
        thread.start()
        self.assertTrue(brain.started.wait(10))
        with Store(self.home) as store:
            store.meeting_begin(meeting, "stand", grace=grace)
        thread.join(30)
        self.assertFalse(thread.is_alive())
        return out["runs"][0], engine

    def test_c3_stopped_after_grace_is_yielded_not_failure_and_events_replay(self):
        events = self.feed(2)
        run, _ = self.run_engine_until_yield(Blocking())
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["error"], "yielded: meeting m1")
        self.assertEqual(run["yielded"], "m1")
        self.assertEqual(run["cost"], 0.5)
        stops = [a for a in self.audits() if a[0] == "floor.yield"]
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0][2], {"run": run["id"], "meeting": "m1", "grace_seconds": 0.2, "outcome": "stopped"})
        with Store(self.home) as store:
            self.assertEqual(sorted(e["id"] for e in store.event_pending()), sorted(events))
            hold = store.meeting_attach("m1")
            self.assertEqual(hold["kind"], "floor")
        quick = Quick()
        self.assertEqual(Engine(self.home, quick).run(), [])  # held: no session
        with Store(self.home) as store:
            store.meeting_end("m1", token=hold["token"])
        Engine(self.home, quick).run()
        self.assertEqual(sorted(quick.contexts[0]["run"]["event_ids"]), sorted(events))  # replayed after end

    def test_c3_three_yields_are_no_failure_streak(self):
        for i in range(3):
            self.feed()
            run, _ = self.run_engine_until_yield(Blocking(), meeting="m%d" % i)
            self.assertEqual(run["yielded"], "m%d" % i)
            with Store(self.home) as store:
                store.meeting_end("m%d" % i)
        with Store(self.home) as store:
            runs = store.run_list(50)
        self.assertEqual([r["status"] for r in runs[:3]], ["failed"] * 3)
        self.assertIsNone(failure_streak(runs))

    def test_c3_failure_streak_skips_yielded_both_ways(self):
        fail = {"id": "f", "status": "failed"}
        y = {"id": "y", "status": "failed", "yielded": "m"}
        ok = {"id": "ok", "status": "finished"}
        self.assertEqual(failure_streak([dict(fail, id="a"), y, dict(fail, id="b"), y, dict(fail, id="c")]), "a")
        self.assertIsNone(failure_streak([y, y, y, ok]))
        self.assertIsNone(failure_streak([dict(fail, id="a"), y, ok, fail]))

    def test_c3_yielded_run_is_no_probation_verdict(self):
        self.feed()
        calls = []
        brain = Blocking()
        brain.home = self.home
        engine = Engine(self.home, brain, runtime=SimpleNamespace(after_run=lambda s, r, error=None: calls.append(error)))
        engine.YIELD_POLL = 0.02
        thread = threading.Thread(target=engine.run)
        thread.start()
        self.assertTrue(brain.started.wait(10))
        with Store(self.home) as store:
            store.meeting_begin("m1", "stand", grace=0.1)
        thread.join(30)
        self.assertEqual(calls, [])  # probation still pending: the next real session decides it

    def test_c3_ended_itself_inside_grace(self):
        events = self.feed()
        run, _ = self.run_engine_until_yield(Blocking(finish_when_pending=True), grace=30)
        self.assertEqual(run["status"], "finished")
        stops = [a for a in self.audits() if a[0] == "floor.yield"]
        self.assertEqual(stops[0][2]["outcome"], "ended")
        with Store(self.home) as store:
            self.assertEqual(store.event_pending(), [])
            self.assertIsNotNone(store.meeting_attach("m1"))
        self.assertTrue(events)

    def test_c3_routine_left_ready_is_not_quarantined_by_a_yield(self):
        with Store(self.home) as store:
            from datetime import datetime, timedelta, timezone
            store.routine_seed(["hourly-check"], now=datetime.now(timezone.utc) - timedelta(minutes=5),
                               first={"hourly-check": "+1"})
        with Store(self.home) as store:
            store.due_events()
        run, _ = self.run_engine_until_yield(Blocking())
        with Store(self.home) as store:
            routine = next(w for w in store.work_list() if (w.get("routine") or {}).get("kind") == "hourly-check")
        self.assertIsNone(routine["routine"].get("quarantine"))
        self.assertEqual(run["yielded"], "m1")


class Pending(Base):
    def test_pending_meeting_blocks_new_runs_until_attach_or_expiry(self):
        self.feed()
        with Store(self.home) as store:
            store.meeting_begin("m1", "stand")
            with self.assertRaises(WorkerHeld):
                store.run_begin()
        self.assertEqual(Engine(self.home, Quick()).run(), [])
        self.expire(Store.MEETING_KEY)
        self.assertEqual(len(Engine(self.home, Quick()).run()), 1)

    def test_one_meeting_at_a_time_and_idempotent_begin(self):
        with Store(self.home) as store:
            first = store.meeting_begin("m1", "stand")
            self.assertEqual(store.meeting_begin("m1", "stand"), first)
            with self.assertRaises(ValueError):
                store.meeting_begin("m2", "stand")
            with self.assertRaises(ValueError):
                store.meeting_begin("bad id!", "stand")

    def test_renew_extends_and_wrong_token_does_not(self):
        with Store(self.home) as store:
            store.meeting_begin("m1", "stand")
            hold = store.meeting_attach("m1")
            self.assertIsNone(store.meeting_renew("nope"))
            later = store.meeting_renew(hold["token"])
            self.assertGreaterEqual(later["expires_at"], hold["expires_at"])


class C4Attach(Base):
    def attach(self, lines, key_file=None):
        stdin = io.StringIO("".join(json.dumps(dict(v=1, **line)) + "\n" for line in lines))
        stdout = io.StringIO()
        rc = floor.attach(self.home, key_file or self.key_file, stdin=stdin, stdout=stdout)
        return rc, [json.loads(line) for line in stdout.getvalue().splitlines()]

    def test_c4_attach_begin_ready_end(self):
        rc, out = self.attach([{"type": "begin", "meeting": "m1"}, {"type": "end"}])
        self.assertEqual(rc, 0)
        self.assertEqual([o["type"] for o in out], ["ready", "ended"])
        self.assertEqual(out[1]["reason"], "end")
        actions = [a[0] for a in self.audits()]
        self.assertEqual(actions, ["floor.meeting_pending", "floor.attached", "floor.ended"])
        attached = self.audits()[1][2]
        self.assertEqual((attached["kind"], attached["channel"]), ("floor", "stand"))
        with Store(self.home) as store:
            self.assertIsNone(store.worker_hold())
            self.assertIsNone(store.meeting_pending())

    def test_c4_refused_inside_session_and_audited(self):
        os.environ[SESSION_ENV] = "run-x"
        rc, out = self.attach([{"type": "begin", "meeting": "m1"}])
        self.assertEqual(rc, 2)
        self.assertEqual(out[0]["code"], "in_session")
        self.assertEqual(self.audits(), [("floor.refused", "attach", {"code": "in_session", "in_session": True})])
        with Store(self.home) as store:
            self.assertIsNone(store.meeting_pending())

    def test_c4_needs_the_stand_key(self):
        rc, out = self.attach([{"type": "begin", "meeting": "m1"}], key_file=self._key(OTHER, "other.key"))
        self.assertEqual((rc, out[0]["code"]), (2, "auth_failed"))
        rc, out = self.attach([{"type": "begin", "meeting": "m1"}], key_file=self._key(b"z" * 32, "unknown.key"))
        self.assertEqual((rc, out[0]["code"]), (2, "auth_failed"))
        self.assertEqual([a[2]["code"] for a in self.audits()], ["auth_failed", "auth_failed"])

    def test_c4_waits_for_running_session_and_says_waiting(self):
        with Store(self.home) as store:
            run = store.run_begin()

        def finish():
            time.sleep(0.6)
            with Store(self.home) as store:
                store.run_fail(run["id"], "yielded: meeting m1", yielded="m1")

        threading.Thread(target=finish).start()
        rc, out = self.attach([{"type": "begin", "meeting": "m1", "grace": 0.2}, {"type": "end"}])
        self.assertEqual([o["type"] for o in out], ["waiting", "ready", "ended"])
        self.assertEqual(out[0]["yield_run"], run["id"])

    def test_c4_killed_attach_hold_expires_and_frees_worker(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        proc = subprocess.Popen([sys.executable, "-m", "synthetics", "--home", str(self.home), "floor", "attach",
                                 "--key-file", self.key_file], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                text=True, cwd=str(ROOT), env=env)
        proc.stdin.write(json.dumps({"v": 1, "type": "begin", "meeting": "m1"}) + "\n")
        proc.stdin.flush()
        ready = json.loads(proc.stdout.readline())
        self.assertEqual(ready["type"], "ready")
        proc.kill()
        proc.wait()
        proc.stdin.close()
        proc.stdout.close()
        self.feed()
        with Store(self.home) as store:
            hold = store.worker_hold()
            self.assertEqual(hold["kind"], "floor")
            self.assertGreater(hold["expires_at"], timestamp())
        self.assertEqual(Engine(self.home, Quick()).run(), [])  # still held
        self.expire(Store.HOLD_KEY)  # 5 min without a renewal
        self.assertEqual(len(Engine(self.home, Quick()).run()), 1)


STUB = textwrap.dedent("""\
    #!{python}
    import os, sys, time
    open(os.environ["STUB_PIDFILE"], "w").write(str(os.getpid()))
    sys.stdin.read()
    time.sleep(60)
    """)


class RealBrainStop(Base):
    def test_yield_stop_ends_a_real_cli_process_group(self):
        stub = Path(self.temp.name) / "claude-stub"
        stub.write_text(STUB.format(python=sys.executable))
        os.chmod(stub, 0o700)
        os.environ["STUB_PIDFILE"] = str(Path(self.temp.name) / "pid")
        self.feed()
        for stream in (True, False):
            with self.subTest(stream=stream):
                brain = ClaudeBrain(self.home, executable=str(stub), timeout=120, stream_output=stream)
                engine = Engine(self.home, brain)
                engine.YIELD_POLL = 0.05
                out = {}
                started = time.monotonic()
                thread = threading.Thread(target=lambda: out.setdefault("runs", engine.run()))
                thread.start()
                pidfile = Path(os.environ["STUB_PIDFILE"])
                for _ in range(200):
                    if pidfile.exists() and pidfile.read_text():
                        break
                    time.sleep(0.05)
                meeting = "m-%s" % stream
                with Store(self.home) as store:
                    store.meeting_begin(meeting, "stand", grace=0.3)
                thread.join(60)
                self.assertLess(time.monotonic() - started, 30)
                self.assertEqual(out["runs"][0]["yielded"], meeting)
                pid = int(pidfile.read_text())
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
                pidfile.unlink()
                with Store(self.home) as store:
                    store.meeting_end(meeting)


if __name__ == "__main__":
    unittest.main()
