"""M15 (SG-07): failed-run cost, per-day usage, daily soft limit, crash-loop guard, deliberate-stop exit codes."""

import json
import plistlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from synthetics.brain import BrainError, ClaudeBrain
from synthetics.engine import Engine, WorkerStopped, read_limit_override, utc_day, write_limit_override
from synthetics.service import write_launch_agent
from synthetics.store import Store
from synthetics import capture
from tests.humans_fixture import HOST_HUMANS, write_host_humans


class Brain:
    def __init__(self, cost=0.01, fail=None):
        self.cost = cost
        self.fail = fail
        self.contexts = []

    def run(self, context):
        self.contexts.append(context)
        if self.fail is not None:
            raise self.fail
        return SimpleNamespace(summary="ok", cost=self.cost)


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        write_host_humans(self.home)
        with Store(self.home) as store:
            store.initialize("Rowan", "Maintainer")
            store.entity_put("david", "David", "person")
            store.entity_put("codex-manager", "Codex", "system")
            # Consume the welcome so tests control pending input.
            run = store.run_begin()
            store.run_finish(run["id"], "welcomed", cost=0.0)

    def spend(self, cost, status="finished"):
        with Store(self.home) as store:
            run = store.run_begin(event_ids=[])
            if status == "finished":
                store.run_finish(run["id"], "spent", cost=cost)
            else:
                store.run_fail(run["id"], "boom", cost=cost)
            return run["id"]

    def outbox(self):
        with Store(self.home) as store:
            return store.outbox_list()


class BrainCostTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "home"

    def executable(self, body):
        path = Path(self.temporary.name) / ("fake-%d" % len(list(Path(self.temporary.name).glob("fake-*"))))
        path.write_text("#!" + sys.executable + "\n" + body)
        path.chmod(0o700)
        return str(path)

    def raised(self, body, **kwargs):
        with self.assertRaises(BrainError) as caught:
            ClaudeBrain(self.home, executable=self.executable(body), **kwargs).run({})
        return caught.exception

    def test_budget_stop_carries_reported_cost(self):
        payload = {"type": "result", "subtype": "error_max_budget_usd", "is_error": False, "total_cost_usd": 2.01}
        error = self.raised("import json,sys\nsys.stdin.read()\nprint(json.dumps(%r))\n" % payload)
        self.assertEqual(error.cost, 2.01)

    def test_nonzero_exit_with_json_cost_recovers_cost(self):
        payload = {"is_error": True, "total_cost_usd": 0.5}
        error = self.raised("import json,sys\nsys.stdin.read()\nprint(json.dumps(%r))\nsys.exit(3)\n" % payload)
        self.assertEqual(error.cost, 0.5)

    def test_empty_output_timeout_and_non_numeric_cost_are_unknown(self):
        self.assertIsNone(self.raised("import sys\nsys.stdin.read()\nsys.exit(1)\n").cost)
        self.assertIsNone(self.raised("import sys,time\nsys.stdin.read()\ntime.sleep(5)\n", timeout=0.3).cost)
        for bad in (True, "1.0", None):
            payload = {"is_error": True, "total_cost_usd": bad}
            error = self.raised("import json,sys\nsys.stdin.read()\nprint(json.dumps(%r))\n" % payload)
            self.assertIsNone(error.cost)

    def test_plain_brain_error_keeps_old_signature(self):
        self.assertIsNone(BrainError("x").cost)


class StoreUsageTests(Base):
    def test_failed_run_stores_cost_and_old_callers_store_null(self):
        with Store(self.home) as store:
            run = store.run_begin(event_ids=[])
            failed = store.run_fail(run["id"], "budget", cost=1.05)
            run = store.run_begin(event_ids=[])
            legacy = store.run_fail(run["id"], "boom")
        self.assertEqual(failed["cost"], 1.05)
        self.assertIsNone(legacy["cost"])

    def test_usage_sums_known_counts_unknown_and_legacy_records(self):
        self.spend(1.5)
        self.spend(None, status="failed")
        self.spend(2.0, status="failed")
        with Store(self.home) as store:
            # A legacy record without a cost key, dated yesterday.
            run = store.run_begin(event_ids=[])
            store.run_finish(run["id"], "old")
            record = store.run_get(run["id"])
            record.pop("cost")
            record["started_at"] = "2026-01-01T10:00:00.000000Z"
            store._save("runs", record, "test.legacy")
            store.run_begin(event_ids=[])  # in progress
            usage = store.usage()
            old = store.usage(day="2026-01-02", days=2)
        self.assertEqual(usage["timezone"], "UTC")
        self.assertEqual(usage["today"]["day"], utc_day())
        self.assertAlmostEqual(usage["today"]["cost_usd"], 3.5)
        self.assertEqual(usage["today"]["unknown_cost_runs"], 1)
        self.assertEqual(usage["today"]["running"], 1)
        self.assertEqual(len(usage["recent"]), 7)
        self.assertEqual([d["day"] for d in old["recent"]], ["2026-01-02", "2026-01-01"])
        self.assertEqual(old["recent"][1]["unknown_cost_runs"], 1)
        self.assertEqual(old["recent"][0]["runs"], 0)

    def test_context_includes_usage(self):
        self.spend(0.25)
        with Store(self.home) as store:
            self.assertAlmostEqual(store.context()["usage"]["today"]["cost_usd"], 0.25)

    def test_backfill_recovers_failed_runs_once_and_audits(self):
        with_output = self.spend(None, status="failed")
        without_output = self.spend(None, status="failed")
        finished = self.spend(None)
        records = self.home / "runs"
        records.mkdir(exist_ok=True)
        for run_id in (with_output, finished):
            (records / (run_id + ".claude.json")).write_text(json.dumps({"subtype": "error_max_budget_usd", "total_cost_usd": 2.01}))
        with Store(self.home) as store:
            first = store.run_cost_backfill()
            second = store.run_cost_backfill()
            audit = [a for a in store.audit_list(limit=50) if a["action"] == "run.cost_backfilled"]
            self.assertEqual(store.run_get(with_output)["cost"], 2.01)
            self.assertIsNone(store.run_get(without_output)["cost"])
            self.assertIsNone(store.run_get(finished)["cost"])
        self.assertEqual(first["recovered"], [{"run_id": with_output, "cost": 2.01}])
        self.assertEqual(second["recovered"], [])
        self.assertEqual(len(audit), 1)


class SoftLimitTests(Base):
    def engine(self, brain, **kwargs):
        kwargs.setdefault("daily_limit", 2.0)
        kwargs.setdefault("limit_notify", "codex-manager")
        return Engine(self.home, brain, **kwargs)

    def test_engine_records_cost_of_failed_session(self):
        with Store(self.home) as store:
            store.feed("david", "please work")
        engine = self.engine(Brain(fail=BrainError("cap", cost=1.25)))
        with self.assertRaises(BrainError):
            engine._step()
        with Store(self.home) as store:
            self.assertEqual(store.run_list(1)[0]["cost"], 1.25)
            self.assertEqual(len(store.event_pending()), 1)

    def test_over_limit_pauses_without_dropping_input_and_notifies_once(self):
        self.spend(2.0)
        with Store(self.home) as store:
            store.feed("codex-manager", "peer message")
            store.work_put("Ready work")
        brain = Brain()
        updates = []
        engine = self.engine(brain, on_update=updates.append)
        for _ in range(3):
            self.assertIsNone(engine._step())
        self.assertEqual(brain.contexts, [])
        with Store(self.home) as store:
            self.assertEqual(len(store.event_pending()), 1)
        notices = self.outbox()
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["to"], "codex-manager")
        self.assertIn("Daily soft limit", notices[0]["body"])
        self.assertEqual([u["reason"] for u in updates if u["type"] == "paused"], ["daily_limit"])

    def test_raise_lifts_pause_on_same_poll_and_other_dates_are_ignored(self):
        self.spend(2.0)
        with Store(self.home) as store:
            store.feed("codex-manager", "peer message")
        brain = Brain()
        engine = self.engine(brain)
        write_limit_override(self.home, date="2000-01-01", limit_usd=500.0, set_by="test")
        self.assertIsNone(engine._step())
        write_limit_override(self.home, date=utc_day(), limit_usd=5.0, set_by="test")
        self.assertIsNotNone(engine._step())
        self.assertEqual(read_limit_override(self.home)["set_by"], "test")

    def test_zero_limit_disables_and_day_rollover_resumes(self):
        self.spend(10.0)
        with Store(self.home) as store:
            store.feed("david", "hello")
            # Move today's spend to yesterday: the new UTC day admits again.
        self.assertIsNotNone(self.engine(Brain(), daily_limit=0)._step())
        with Store(self.home) as store:
            store.feed("david", "hello again")
            for run in store.run_list(10):
                run["started_at"] = "2000-01-01T00:00:00.000000Z"
                store._save("runs", run, "test.backdate")
        self.assertIsNotNone(self.engine(Brain())._step())

    def _channels(self, store):
        self.tg_key, self.desk_key = b"t" * 32, b"d" * 32
        store.channel_register("telegram", Store.credential_hash(self.tg_key), ["telegram:"], "test")
        store.channel_register("codex-desktop", Store.credential_hash(self.desk_key), ["codex-desktop:"], "test")

    def test_daily_limit_admits_only_authenticated_telegram_from_allowed_sender(self):
        self.spend(2.0)
        with Store(self.home) as store:
            self._channels(store)
            peer = store.feed("codex-manager", "peer message", source="fieldpost")
            spoof = store.feed("david", "manual david label", source="manual")
            # M22: the right labels without the channel credential stay paused.
            shell = store.feed("david", "shell-fed telegram label", source="telegram:12345", external_id="tg:0")
            telegram = store.feed("david", "from Telegram", source="telegram:12345", external_id="tg:1",
                                  credential=self.tg_key)
            store.work_put("Ready work")
        brain = Brain()
        engine = self.engine(brain, limit_notify="david", limit_allow=["david"])
        finished = engine._step()
        self.assertEqual(finished["event_ids"], [telegram["id"]])
        with Store(self.home) as store:
            pending = {e["id"] for e in store.event_pending()}
        self.assertEqual(pending, {peer["id"], spoof["id"], shell["id"]})
        self.assertIsNone(engine._step())  # only paused inputs remain
        self.assertEqual(len(brain.contexts), 1)
        self.assertEqual(self.outbox()[0]["to"], "david")

    # MG-08: David's orders now arrive from the desktop chat (source codex-desktop:<thread>).
    def test_daily_limit_admits_desktop_chat_from_allowed_sender(self):
        self.spend(2.0)
        with Store(self.home) as store:
            self._channels(store)
            desktop = store.feed("david", "order from desktop", source="codex-desktop:thread-1", external_id="d:1",
                                 credential=self.desk_key)
            telegram = store.feed("david", "from Telegram", source="telegram:12345", external_id="tg:2",
                                  credential=self.tg_key)
        engine = self.engine(Brain(), limit_notify="david", limit_allow=["david"])
        finished = engine._step()
        self.assertEqual(sorted(finished["event_ids"]), sorted([desktop["id"], telegram["id"]]))

    def test_daily_limit_holds_desktop_chat_from_sender_not_allowed(self):
        self.spend(2.0)
        with Store(self.home) as store:
            other = store.feed("rowan", "desktop-labelled peer", source="codex-desktop:thread-1")
        brain = Brain()
        engine = self.engine(brain, limit_notify="david", limit_allow=["david"])
        self.assertIsNone(engine._step())
        self.assertEqual(brain.contexts, [])
        with Store(self.home) as store:
            self.assertEqual([e["id"] for e in store.event_pending()], [other["id"]])

    def test_daily_limit_holds_operator_desktop_handoff_context(self):
        self.spend(2.0)
        with Store(self.home) as store:
            handoff = store.feed("codex-desktop", "context for a desktop turn", source="desktop-channel-handoff")
            prefixed = store.feed("codex-desktop", "operator note", source="codex-desktop:thread-1")
        brain = Brain()
        engine = self.engine(brain, limit_notify="david", limit_allow=["david"])
        self.assertIsNone(engine._step())
        self.assertEqual(brain.contexts, [])
        with Store(self.home) as store:
            self.assertEqual({e["id"] for e in store.event_pending()}, {handoff["id"], prefixed["id"]})

    def test_source_prefix_must_match_exactly(self):
        engine = self.engine(Brain(), limit_allow=["david"])
        cfg = capture.parse_humans(HOST_HUMANS)
        for source in ("codex-desktop", "desktop-channel-handoff", "xcodex-desktop:1", "telegram", "manual"):
            self.assertFalse(engine._admissible_during_limit({"source": source, "sender": "david",
                                                              "auth": {"channel": "x"}}, cfg), source)
        for source in ("codex-desktop:t", "telegram:1"):
            self.assertTrue(engine._admissible_during_limit({"source": source, "sender": "david",
                                                             "auth": {"channel": "x"}}, cfg), source)
            # M22 Q-C: without an auth stamp the bypass does not apply.
            self.assertFalse(engine._admissible_during_limit({"source": source, "sender": "david"}, cfg), source)
            self.assertFalse(engine._admissible_during_limit({"source": source, "sender": "david", "auth": None}, cfg))

    def test_failure_pause_admits_desktop_chat_nothing(self):
        for _ in range(3):
            self.spend(0.1, status="failed")
        with Store(self.home) as store:
            store.feed("david", "order from desktop", source="codex-desktop:thread-1")
            store.feed("david", "from Telegram", source="telegram:1")
        brain = Brain()
        engine = self.engine(brain, daily_limit=0, limit_allow=["david"])
        engine.failure_pause = "run-x"
        self.assertIsNone(engine._step())
        self.assertEqual(brain.contexts, [])


class FailurePauseTests(Base):
    def fail_three(self):
        with Store(self.home) as store:
            store.feed("david", "poisonous input")
        engine = Engine(self.home, Brain(fail=BrainError("crash")))
        with self.assertRaises(WorkerStopped):
            engine.serve(poll_seconds=0.01, retry_seconds=0.01)

    def test_three_failure_stop_then_restart_starts_paused_until_resume(self):
        self.fail_three()
        brain = Brain()
        stop_after = []

        def update(value):
            stop_after.append(value)
            if value["type"] == "paused":
                engine_stop.set()

        import threading
        engine_stop = threading.Event()
        engine = Engine(self.home, brain, on_update=update, daily_limit=0, limit_notify="codex-manager")
        engine.serve(poll_seconds=0.01, retry_seconds=0.01, stop=engine_stop)
        self.assertEqual(brain.contexts, [])
        paused = [u for u in stop_after if u["type"] == "paused"][0]
        self.assertEqual(paused["reason"], "recent_failures")
        self.assertIn("limit resume", self.outbox()[0]["body"])
        # Raising the daily limit does not clear a failure pause.
        write_limit_override(self.home, date=utc_day(), limit_usd=1000.0, set_by="test")
        engine = Engine(self.home, brain, daily_limit=0)
        engine.failure_pause = paused["run_id"]
        with Store(self.home) as store:
            self.assertIsNotNone(engine.pause_state(store))
        write_limit_override(self.home, resume_after_run=paused["run_id"], resumed_by="test")
        self.assertIsNotNone(engine._step())
        self.assertEqual(len(brain.contexts), 1)

    def test_one_finished_run_among_last_three_does_not_pause(self):
        for status in ("failed", "failed", "finished"):
            self.spend(0.1, status=status)
        with Store(self.home) as store:
            store.feed("david", "go")
        import threading
        stop = threading.Event()
        brain = Brain()

        def update(value):
            if value["type"] == "finished":
                stop.set()
        Engine(self.home, brain, on_update=update, daily_limit=0).serve(poll_seconds=0.01, retry_seconds=0.01, stop=stop)
        self.assertEqual(len(brain.contexts), 1)


class ExitCodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        with Store(self.home) as store:
            store.initialize("Rowan", "Maintainer")

    def fake(self, body):
        path = self.home / "fake-claude"
        path.write_text("#!" + sys.executable + "\n" + body)
        path.chmod(0o700)
        return str(path)

    def serve(self, fake, *extra):
        return subprocess.Popen([sys.executable, "-m", "synthetics", "--home", str(self.home), "serve",
                                 "--claude", fake, "--poll", "0.05", "--retry", "0.05", *extra],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def wait_started(self, process, marker):
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.02)
        self.assertTrue(marker.exists(), "fixture model did not start")

    def sleeping_fake(self):
        marker = self.home / "started"
        return self.fake("import pathlib,sys,time\nsys.stdin.read()\npathlib.Path(%r).write_text('x')\ntime.sleep(10)\n"
                         % str(marker)), marker

    def test_serve_stop_exits_zero_and_sigint_keeps_130(self):
        for sig, expected in ((None, 0), (signal.SIGINT, 130)):
            with self.subTest(signal=sig):
                with Store(self.home) as store:
                    store.feed("david", "task %s" % sig)
                fake, marker = self.sleeping_fake()
                marker.unlink(missing_ok=True)
                process = self.serve(fake)
                try:
                    self.wait_started(process, marker)
                    if sig is None:
                        stopped = subprocess.run([sys.executable, "-m", "synthetics", "--home", str(self.home), "stop"],
                                                 capture_output=True, text=True)
                        self.assertEqual(stopped.returncode, 0, stopped.stderr)
                    else:
                        process.send_signal(sig)
                    _, stderr = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, expected, stderr)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()

    def test_serve_three_failure_stop_exits_zero_and_busy_is_nonzero(self):
        with Store(self.home) as store:
            store.feed("david", "will fail")
        process = self.serve(self.fake("import sys\nsys.stdin.read()\nsys.exit(2)\n"), "--daily-limit", "0")
        _, stderr = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertIn("three failed sessions", stderr)
        with Store(self.home) as store:
            self.assertEqual([r["status"] for r in store.run_list(3)], ["failed"] * 3)
        fake, marker = self.sleeping_fake()
        write_limit_override(self.home, resume_after_run="none")
        holder = self.serve(fake)
        try:
            # Paused by the failure streak: holds the lock without calling the model.
            deadline = time.monotonic() + 5
            while not (self.home / "worker.lock").read_text().strip() and time.monotonic() < deadline:
                time.sleep(0.05)
            time.sleep(0.5)
            self.assertFalse(marker.exists())
            busy = self.serve(fake)
            _, stderr = busy.communicate(timeout=10)
            self.assertNotEqual(busy.returncode, 0, stderr)
        finally:
            holder.terminate()
            holder.communicate(timeout=10)
        self.assertEqual(holder.returncode, 0)

    def test_cli_usage_limit_and_backfill_commands(self):
        def call(*args):
            result = subprocess.run([sys.executable, "-m", "synthetics", "--home", str(self.home), *args],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertEqual(call("usage", "--days", "2")["timezone"], "UTC")
        self.assertEqual(call("limit", "raise", "120", "--by", "codex-manager")["limit_usd"], 120.0)
        self.assertEqual(call("limit", "resume")["resumed_by"], "operator")
        self.assertEqual(call("backfill-cost")["recovered"], [])
        bad = subprocess.run([sys.executable, "-m", "synthetics", "--home", str(self.home), "limit", "raise", "5",
                              "--date", "tomorrow"], capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)


class OnFailureKeepAliveTests(unittest.TestCase):
    def test_on_failure_writes_successful_exit_dict_and_throttle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = write_launch_agent("com.synthetics.worker.test", ["/usr/bin/python3", "-m", "synthetics"], root,
                                      root / "logs", launch_agents_dir=root / "agents", keep_alive="on-failure")
            data = plistlib.loads(Path(path).read_bytes())
            self.assertEqual(data["KeepAlive"], {"SuccessfulExit": False})
            self.assertEqual(data["ThrottleInterval"], 30)
            with self.assertRaises(ValueError):
                write_launch_agent("com.synthetics.worker.test2", ["/usr/bin/python3"], root, root / "logs",
                                   launch_agents_dir=root / "agents", keep_alive="always")


if __name__ == "__main__":
    unittest.main()
