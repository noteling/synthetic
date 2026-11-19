"""E4-r2 (Rowan's PR #66 review at 88e462c): B1 a detached descendant holding the stream pipe can't hang the worker;
L1 no Store open per idle poll once today's prune check is done; L2 bytes no prune can remove never drive the size pass,
and doctor warns when runs/ is over its bound."""

import json
import os
from pathlib import Path
import signal
import tempfile
import time
import unittest
from unittest import mock

from synthetics import doctor, ledgerline
from synthetics.brain import ClaudeBrain
from synthetics.store import Store
from tests import test_e4_retention
from tests.test_i17_export_verify import HomeCase

RESULT = {"type": "result", "subtype": "success", "is_error": False, "result": "Saved.", "total_cost_usd": 0.5}

# Rowan's probe (rowan/workspace/rtn/pr66/probe/escaped_holder.py): print the result line, then a setsid +
# double-fork grandchild keeps fd 1 open after the CLI exits, so _stop_group can't see it.
HOLDER = r'''#!/usr/bin/env python3
import json, os, sys, time
sys.stdin.read()
print(%(result)r, flush=True)
pid = os.fork()
if pid == 0:
    os.setsid()
    if os.fork():
        os._exit(0)
    with open(os.environ["E4R2_PIDFILE"], "w") as f:
        f.write(str(os.getpid()))
    time.sleep(%(hold)d)
    os._exit(0)
os.waitpid(pid, 0)
sys.exit(0)
'''


class EscapedHolder(unittest.TestCase):
    """B1: run() returns within the reader join plus a margin, with the result kept and the detach recorded."""

    HOLD = 40
    JOIN = 1.0

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / "employee"
        self.home.mkdir()
        self.pidfile = Path(self.temporary.name) / "holder.pid"
        self.addCleanup(self.kill_holder)

    def tearDown(self):
        self.temporary.cleanup()

    def kill_holder(self):
        try:
            os.kill(int(self.pidfile.read_text()), signal.SIGKILL)  # only the holder this test made
        except (OSError, ValueError):
            pass

    def test_detached_holder_does_not_hang_the_worker(self):
        exe = Path(self.temporary.name) / "fake-claude"
        exe.write_text(HOLDER % {"result": json.dumps(RESULT), "hold": self.HOLD})
        exe.chmod(0o700)
        brain = ClaudeBrain(self.home, executable=str(exe), timeout=120, stream_output=True)
        with mock.patch.dict(os.environ, {"E4R2_PIDFILE": str(self.pidfile)}), \
                mock.patch.object(ClaudeBrain, "READER_JOIN", self.JOIN, create=True):
            started = time.monotonic()
            result = brain.run({"run": {"id": "run-hold", "profile": None}})
            took = time.monotonic() - started
        self.assertLess(took, self.JOIN + 5, "stream mode waited %.1f s for an escaped pipe holder" % took)
        self.assertEqual(result.summary, "Saved.")
        self.assertEqual(result.cost, 0.5)
        folder = self.home / "runs"
        self.assertEqual((folder / "run-hold.claude.json").read_text(), json.dumps(RESULT) + "\n")
        facts = [json.loads(l) for l in (folder / "run-hold.stream.jsonl").read_text().splitlines()]
        self.assertEqual([f["kind"] for f in facts], ["result", "reader_detached"])
        self.assertEqual(facts[-1]["join_seconds"], self.JOIN)
        self.assertEqual(ledgerline.check_chain(folder / "run-hold.stream.jsonl", None)["chain"], "unanchored")


class ReducerDetach(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "s.jsonl"

    def tearDown(self):
        self.temporary.cleanup()

    def test_lines_after_detach_or_close_are_ignored(self):
        reducer = ledgerline.StreamReducer(self.path)
        reducer.feed(json.dumps(RESULT) + "\n")
        reducer.detach(10)
        reducer.detach(10)  # once only
        later = dict(RESULT, result="late")
        reducer.feed(json.dumps(later) + "\n")
        self.assertEqual(reducer.output_text(), json.dumps(RESULT) + "\n")
        reducer.close()
        reducer.feed(json.dumps(later) + "\n")  # after close: ignored, never raises
        kinds = [json.loads(l)["kind"] for l in self.path.read_text().splitlines()]
        self.assertEqual(kinds, ["result", "reader_detached"])

    def test_detached_fact_is_allow_listed(self):
        self.assertEqual(ledgerline.STREAM_ALLOWED["reader_detached"], frozenset({"join_seconds"}))
        self.assertFalse(ledgerline.STREAM_ALLOWED["reader_detached"] & set(ledgerline.STREAM_FORBIDDEN))


class IdlePoll(HomeCase):
    """L1: once today's prune check is done, later idle polls open no Store."""

    def engine(self):
        from synthetics.engine import Engine
        events = []
        engine = Engine(self.home, brain=object(), on_update=events.append)
        return engine, events

    def test_no_store_open_after_todays_check(self):
        from synthetics import engine as engine_module
        engine, _ = self.engine()
        with mock.patch.object(Store, "runs_prune", autospec=True, return_value={"runs": 0}), \
                mock.patch.object(engine_module, "Store", wraps=Store) as opened:
            engine.prune_runs_if_due()
            first = opened.call_count
            for _ in range(5):
                engine.prune_runs_if_due()
        self.assertEqual(first, 1)
        self.assertEqual(opened.call_count, 1)

    def test_already_pruned_today_in_store_is_cached_too(self):
        from synthetics import engine as engine_module
        first, _ = self.engine()
        with mock.patch.object(Store, "runs_prune", autospec=True, return_value={"runs": 0}):
            first.prune_runs_if_due()  # writes today's key
        second, _ = self.engine()  # a restarted worker reads the key once
        with mock.patch.object(engine_module, "Store", wraps=Store) as opened:
            second.prune_runs_if_due()
            second.prune_runs_if_due()
        self.assertEqual(opened.call_count, 1)

    def test_failure_reported_once_a_day(self):
        engine, events = self.engine()
        with mock.patch.object(Store, "runs_prune", side_effect=OSError("disk")):
            for _ in range(3):
                self.assertIsNone(engine.prune_runs_if_due())
        self.assertEqual([e for e in events if e.get("type") == "runs_prune_failed"],
                         [{"type": "runs_prune_failed", "error": "OSError"}])


class UnreachableBound(test_e4_retention.Retention):
    """L2: bytes no prune can remove are reported and don't count toward the size pass."""

    for _name in [n for n in dir(test_e4_retention.Retention) if n.startswith("test_")]:
        locals()[_name] = None  # don't re-run the inherited Retention tests here
    del _name

    def test_unknown_files_never_drive_the_size_pass(self):
        runs = self.make_runs(53)
        (self.home / "runs" / "notes.txt").write_text("x" * 4096)
        (self.home / "runs" / "run-ffffffffffffffffffffffffffffffff.claude.json").write_text("y" * 4096)
        managed = sum(p.stat().st_size for p in (self.home / "runs").iterdir()) - 8192
        result = self.store.runs_prune(keep_days=3650, max_mb=(managed + 1) / 1048576.0)
        self.assertEqual(result["runs"], 0)  # whole runs/ is over, but only by bytes no prune can remove
        self.assertEqual(result["unremovable_bytes"], 8192)
        self.assertTrue(result["over_bound_after"])
        self.assertTrue(all(len(self.files(r)) == 4 for r in runs))
        self.assertTrue((self.home / "runs" / "notes.txt").exists())

    def test_managed_files_over_bound_still_prune_oldest_first(self):
        runs = self.make_runs(53)
        (self.home / "runs" / "notes.txt").write_text("x" * 4096)
        result = self.store.runs_prune(keep_days=3650, max_mb=0.000001)
        self.assertEqual(result["runs"], 3)
        self.assertEqual([r for r in runs if not self.files(r)], runs[:3])
        self.assertEqual(result["unremovable_bytes"], 4096)

    def test_doctor_warns_when_over_bound(self):
        self.make_runs(1)
        (self.home / "runtime.json").write_text(json.dumps({"runs_max_mb": 0.00001}))
        found = []
        doctor.runs_size_check(self.home, lambda level, code, text: found.append((level, code)))
        self.assertEqual(found, [("warning", "runs_over_bound")])


if __name__ == "__main__":
    unittest.main()
