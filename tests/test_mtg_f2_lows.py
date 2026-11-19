"""MTG F2: Rowan's #74 lows, folded into F2 by Codex (event-14585fbe): L2 the watcher keeps polling until the stop hits
a live process, L3 attach's busy exit drops its pending row, n1 meeting_renew / floor_yield_audit refuse a
session-bound Store. (L1 `stopped_completed` is tested in test_mtg_f1_yield.)"""
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest import mock

from synthetics.brain import BrainError, ClaudeBrain
from synthetics.engine import Engine
from synthetics.store import Store

from tests import test_mtg_f1_yield as f1

Base = f1.Base  # no tests of its own; C4Attach is used via its attach helper only, so its tests aren't re-collected

try:
    from synthetics import floor
except ImportError:  # fail-first on a base without Floor
    floor = None


class LateSpawn:
    """A brain whose CLI isn't running for the first `misses` stop attempts (Rowan's probe: grace 0 + setup time)."""

    def __init__(self, misses=2):
        self.misses = misses
        self.calls = 0
        self.stopped = threading.Event()
        self.started = threading.Event()

    def yield_stop(self):
        self.calls += 1
        if self.calls <= self.misses:
            return False
        self.stopped.set()
        return True

    def run(self, context):
        self.started.set()
        if self.stopped.wait(3):
            raise BrainError("Claude CLI exited with status -15.", cost=0.1)
        return SimpleNamespace(summary="ran to the end: the stop never landed", cost=0.4)


class L2WatcherKeepsPolling(Base):
    def test_l2_stop_retried_until_a_live_process_is_stopped(self):
        self.feed()
        brain = LateSpawn(misses=2)
        engine = Engine(self.home, brain)
        engine.YIELD_POLL = 0.05
        out = {}
        thread = threading.Thread(target=lambda: out.setdefault("runs", engine.run()))
        thread.start()
        self.assertTrue(brain.started.wait(10))
        with Store(self.home) as store:
            store.meeting_begin("m-late", "stand", grace=0)
        thread.join(30)
        self.assertGreaterEqual(brain.calls, 3)
        self.assertEqual(out["runs"][0].get("yielded"), "m-late")
        outcomes = [a[2]["outcome"] for a in self.audits() if a[0] == "floor.yield"]
        self.assertEqual(outcomes, ["stopped"])

    def test_l2_claude_brain_yield_stop_says_whether_it_stopped(self):
        brain = ClaudeBrain(self.home, executable=sys.executable, timeout=30)
        self.assertIs(brain.yield_stop(), False)                      # nothing spawned yet
        done = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        done.wait(10)
        brain._current = done
        self.assertIs(brain.yield_stop(), False)                      # only a finished process
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
        self.addCleanup(lambda: live.poll() is None and live.kill())
        brain._current = live
        self.assertIs(brain.yield_stop(), True)
        live.wait(10)
        self.assertIsNotNone(live.poll())


class L3BusyDropsPending(Base):
    attach = f1.C4Attach.attach

    def test_l3_busy_exit_ends_the_pending_meeting(self):
        with Store(self.home) as store:
            store.run_begin()                                         # a session that never gets recorded as ended
        with mock.patch.object(floor, "WAIT_EXTRA", 0.3), mock.patch.object(floor, "WAIT_POLL", 0.05):
            rc, out = self.attach([{"type": "begin", "meeting": "m-busy", "grace": 0}])
        self.assertEqual(rc, 2)
        self.assertEqual(out[-1]["code"], "busy")
        with Store(self.home) as store:
            self.assertIsNone(store.meeting_pending())
        ended = [a for a in self.audits() if a[0] == "floor.ended"]
        self.assertEqual(len(ended), 1)
        self.assertEqual((ended[0][1], ended[0][2]["reason"], ended[0][2]["dropped_pending"]), ("m-busy", "busy", True))


class N1SessionStoreRefused(Base):
    def test_n1_renew_and_yield_audit_refuse_a_session_bound_store(self):
        with Store(self.home) as store:
            store.meeting_begin("m1", "stand")
            hold = store.meeting_attach("m1")
        with Store(self.home) as store:
            store.meeting_end("m1", token=hold["token"])
            run = store.run_begin()
        with Store(self.home, run_id=run["id"]) as bound:
            with self.assertRaises(ValueError):
                bound.meeting_renew(hold["token"])
            with self.assertRaises(ValueError):
                bound.floor_yield_audit(run["id"], "m1", 45, "stopped")
        self.assertEqual([a for a in self.audits() if a[0] == "floor.yield"], [])


class G1RoutineDueDuringHold(Base):
    """Rowan's P0 read G1 (second test), a guard: a routine that comes due while a meeting holds the floor starts no
    session and is not quarantined during the hold; after the release the next session gets its work_due."""

    def test_g1_routine_due_during_hold_fires_after_release(self):
        from datetime import datetime, timedelta, timezone
        with Store(self.home) as store:
            store.meeting_begin("m1", "stand")
            hold = store.meeting_attach("m1")
            store.routine_seed(["hourly-check"], now=datetime.now(timezone.utc) - timedelta(minutes=5),
                               first={"hourly-check": "+1"})
            store.due_events()
        seen = []

        class Look:
            def run(inner, context):
                seen.append([e.get("kind") for e in context["events"]])
                return SimpleNamespace(summary="looked", cost=0.01)

        self.assertEqual(Engine(self.home, Look()).run(), [])            # held: no session
        with Store(self.home) as store:
            routine = next(w for w in store.work_list() if (w.get("routine") or {}).get("kind") == "hourly-check")
            self.assertIsNone(routine["routine"].get("quarantine"))
            store.meeting_end("m1", token=hold["token"])
        runs = Engine(self.home, Look()).run()
        self.assertEqual(len(runs), 1)
        self.assertIn("work_due", seen[0])


class ToolUseIdInit(Base):
    """#76 r1: `self.tool_use_id = None` (E4a) had landed after `return` in Server.tools(); a fresh server sets it."""

    def test_fresh_server_has_tool_use_id_none(self):
        from synthetics import mcp
        with Store(self.home) as store:
            server = mcp.Server(store)
            self.assertIn("tool_use_id", vars(server))
            self.assertIsNone(server.tool_use_id)
