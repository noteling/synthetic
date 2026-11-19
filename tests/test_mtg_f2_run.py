"""MTG F2: the meeting run row exists only under this meeting's floor hold. Temp homes only."""

import tempfile
import unittest
from pathlib import Path

from synthetics.store import Store, WorkerHeld


class MeetingRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "vesper"
        self.store = Store(self.home)
        self.addCleanup(self.store.close)
        self.store.initialize("Vesper", "maintainer")

    def attach(self, meeting="m1"):
        self.store.meeting_begin(meeting, "stand")
        return self.store.meeting_attach(meeting)

    def test_meeting_run_under_hold(self):
        hold = self.attach()
        with self.assertRaises(WorkerHeld):
            self.store.run_begin()                    # ordinary runs stay refused under the floor hold
        run = self.store.meeting_run_begin("m1", hold["token"])
        self.assertEqual((run["kind"], run["meeting"], run["event_ids"]), ("meeting", "m1", []))
        with self.assertRaises(ValueError):
            self.store.meeting_run_begin("m1", hold["token"])   # one running run at a time
        self.store.run_finish(run["id"], "meeting ended")

    def test_refused_without_the_hold_token_or_in_session(self):
        hold = self.attach()
        for meeting, token in (("m1", "wrong"), ("m2", hold["token"])):
            with self.assertRaises(ValueError):
                self.store.meeting_run_begin(meeting, token)
        bound = Store(self.home, run_id="run-" + "0" * 32)
        self.addCleanup(bound.close)
        with self.assertRaises(ValueError):
            bound.meeting_run_begin("m1", hold["token"])

    def test_worker_restart_keeps_a_live_meeting_run_and_recovers_a_dead_one(self):
        hold = self.attach()
        run = self.store.meeting_run_begin("m1", hold["token"])
        self.assertEqual(self.store.recover_runs(), 0)          # the attach still holds the floor
        self.assertEqual(self.store.run_get(run["id"])["status"], "running")
        self.store.meeting_end("m1", token=hold["token"])        # attach gone (or hold expired)
        self.assertEqual(self.store.recover_runs(), 1)
        self.assertEqual(self.store.run_get(run["id"])["status"], "interrupted")

    def test_meeting_runs_are_no_failure_streak_verdict(self):
        from synthetics.engine import failure_streak
        failed = [dict(id="r%d" % n, status="failed") for n in range(3)]
        meetings = [dict(id="m%d" % n, status="interrupted", kind="meeting") for n in range(3)]
        self.assertIsNone(failure_streak(meetings + failed[:2]))
        self.assertEqual(failure_streak(meetings + failed), "r0")

    def test_refused_after_meeting_end(self):
        hold = self.attach()
        self.store.meeting_end("m1", token=hold["token"])
        with self.assertRaises(ValueError):
            self.store.meeting_run_begin("m1", hold["token"])


if __name__ == "__main__":
    unittest.main()
