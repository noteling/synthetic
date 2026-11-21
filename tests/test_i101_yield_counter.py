"""#101: a run stopped for a meeting (yielded) neither counts as a serve failure nor resets serve's counter."""
import threading
import unittest
from unittest import mock

from synthetics import engine as engine_mod
from synthetics.brain import BrainError
from synthetics.engine import Engine, WorkerStopped
import tests.test_mtg_f1_yield as f1  # module import: its test classes are not collected again here

YieldStep = getattr(engine_mod, "YieldStep", None)


class YieldDoesNotResetServe(f1.Base):
    run_engine_until_yield = f1.C3YieldedRun.run_engine_until_yield

    def test_yielded_run_returns_yield_step(self):
        self.assertIsNotNone(YieldStep, "engine.YieldStep missing")  # fail-first: assert, don't skip
        self.feed(1)
        run, _ = self.run_engine_until_yield(f1.Blocking())
        self.assertIsInstance(run, YieldStep)
        self.assertEqual(run["yielded"], "m1")
        self.assertEqual(run["status"], "failed")

    def serve(self, steps):
        engine = Engine(self.home, f1.Blocking())
        stop = threading.Event()
        calls = []

        def step():
            calls.append(1)
            item = steps[len(calls) - 1] if len(calls) <= len(steps) else None
            if item is None:
                stop.set()
                return None
            if isinstance(item, Exception):
                raise item
            return item

        with mock.patch.object(engine, "_step", side_effect=step):
            engine.serve(poll_seconds=0.01, retry_seconds=0.01, stop=stop)
        return calls

    def test_fail_yield_fail_fail_stops_serve(self):
        self.assertIsNotNone(YieldStep, "engine.YieldStep missing")
        crash = BrainError("crash")
        y = YieldStep(id="y", status="failed", yielded="m1")
        with self.assertRaises(WorkerStopped):
            self.serve([crash, y, crash, crash, None])

    def test_control_fail_success_fail_fail_keeps_serving(self):
        """A real success still resets the counter (the change is only for yields)."""
        crash = BrainError("crash")
        calls = self.serve([crash, {"id": "ok", "status": "finished"}, crash, crash, None])
        self.assertEqual(len(calls), 5)


if __name__ == "__main__":
    unittest.main()
