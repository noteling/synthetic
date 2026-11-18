"""M19: runtime requests, idle restart, reconciliation, probation and revert.

launchd is never touched here: the restarter is a fake. The real launchd behaviour
is covered by tests/test_service_recovery.py and the reviews/M19/demo evidence.
"""

import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from synthetics import runtime
from synthetics.cli import main
from synthetics.engine import Engine
from synthetics.mcp import METHODS, Server
from synthetics.store import Store

LABEL = "com.synthetics.worker.m19test"


def baseline(**overrides):
    value = dict(claude=sys.executable, model="claude-opus-5-5", budget=100.0, timeout=600.0, poll=2.0,
                 retry=60.0, daily_limit=75.0, limit_notify="codex-manager", limit_allow=["david"])
    value.update(overrides)
    return value


class Brain:
    def __init__(self, action=None, error=None):
        self.action = action
        self.error = error
        self.calls = []

    def run(self, context):
        self.calls.append(dict(model=self.model, timeout=self.timeout, budget=self.budget))
        if self.action:
            self.action(context)
        if self.error:
            raise self.error
        return SimpleNamespace(summary="done", cost=0.01)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        with Store(self.home) as store:
            self.me = store.initialize("Vesper", "maintainer")["id"]
        self.restarts = []
        self.clock = Clock()
        self.pid = 4000

    def controller(self, label=LABEL, restarter=None, mode="kickstart", **policy):
        self.pid += 1
        restarter = restarter or (lambda label: self.restarts.append(label))
        return runtime.Controller(self.home, baseline(**policy.pop("base", {})), label=label, restarter=restarter,
                                  mode=mode, argv=["synthetics", "serve"], pid=self.pid, clock=self.clock, **policy)

    def start(self, **kwargs):
        controller = self.controller(**kwargs)
        with Store(self.home) as store:
            controller.start(store)
        return controller

    def self_request(self, settings=None, key="k1", reason="test"):
        with Store(self.home) as store:
            run = store.run_begin()
        try:
            with Store(self.home, run_id=run["id"]) as store:
                return store.runtime_restart(reason, key, settings=settings)
        finally:
            with Store(self.home) as store:
                store.run_finish(run["id"], "requested")

    def idle(self, controller, paused=False):
        with Store(self.home) as store:
            return controller.idle(store, failure_paused=paused)

    def requests(self):
        with Store(self.home) as store:
            return store.runtime_requests(20)

    def outbox(self):
        with Store(self.home) as store:
            return store.outbox_list()

    def audit_actions(self):
        with Store(self.home) as store:
            return [row["action"] for row in reversed(store.audit_list(500))]

    def file(self, name=runtime.CONFIG_FILE):
        return json.loads((self.home / name).read_text())

    # -- request validation (R1-R4) ------------------------------------------
    def test_request_needs_a_runtime_config_worker(self):
        with self.assertRaisesRegex(ValueError, "not started with --runtime-config"):
            self.self_request({"timeout": 300})

    def test_self_service_bounds(self):
        self.start()
        refused = [({"budget": 150}, "baseline"), ({"daily_limit": 100}, "lowered"), ({"daily_limit": 0}, "lowered"),
                   ({"limit_notify": "me"}, "manager or operator"), ({"limit_allow": ["x"]}, "manager or operator"),
                   ({"model": "claude-other-1"}, "allowed models"), ({"timeout": 5}, "timeout"),
                   ({"home": "/tmp"}, "Unsupported")]
        for number, (settings, message) in enumerate(refused):
            with self.subTest(settings=settings), self.assertRaisesRegex(ValueError, message):
                self.self_request(settings, key="bad-%d" % number)
        record = self.self_request({"budget": 50, "daily_limit": 40, "timeout": 300})
        self.assertEqual((record["status"], record["origin"], record["requested_by"]), ("requested", "self", self.me))
        self.assertEqual(record["proposed"]["timeout"], 300.0)

    def test_claude_allowlist_compares_realpath(self):
        link = self.home / "claude-link"
        os.symlink(sys.executable, str(link))
        other = self.home / "other-claude"
        other.write_text("#!/bin/sh\n")
        other.chmod(0o700)
        self.start()
        self.assertEqual(self.self_request({"claude": str(link)})["proposed"]["claude"], str(link))
        with self.assertRaisesRegex(ValueError, "not an allowed executable"):
            self.self_request({"claude": str(other)}, key="k2")

    def test_operator_may_raise_and_change_limits_but_not_through_mcp(self):
        self.start()
        with Store(self.home) as store:
            record = store.runtime_request({"budget": 100, "daily_limit": 120, "limit_allow": ["david", "codex"]},
                                           "raise for release day", "codex-manager")
        self.assertEqual((record["origin"], record["requested_by"]), ("operator", "codex-manager"))
        with Store(self.home) as store:
            run = store.run_begin()
        with Store(self.home, run_id=run["id"]) as store:
            with self.assertRaisesRegex(ValueError, "operator path"):
                store.runtime_request({}, "x", "codex-manager")
        with Store(self.home) as store:
            store.run_finish(run["id"], "x")
        with Store(self.home) as store, self.assertRaisesRegex(ValueError, "session tool"):
            store.runtime_restart("x", "k")
        self.assertNotIn("runtime_request", METHODS)
        self.assertIn("runtime_restart", METHODS)

    def test_later_request_supersedes_and_key_is_idempotent(self):
        self.start()
        first = self.self_request({"timeout": 300}, key="a")
        self.assertEqual(self.self_request({"timeout": 300}, key="a")["id"], first["id"])
        second = self.self_request({"timeout": 900}, key="b")
        statuses = {r["id"]: r["status"] for r in self.requests()}
        self.assertEqual((statuses[first["id"]], statuses[second["id"]]), ("superseded", "requested"))

    def test_mcp_tool_records_a_request_and_logs_receipt(self):
        self.start()
        with Store(self.home) as store:
            run = store.run_begin()
        with Store(self.home, run_id=run["id"]) as store:
            server = Server(store, run_id=run["id"])
            record = server.call("runtime_restart", {"reason": "shorter sessions", "key": "m",
                                                     "settings": {"timeout": 300}})
            with self.assertRaises(ValueError):
                server.call("runtime_restart", {"reason": "x", "key": "n", "settings": {"budget": 500}})
        with Store(self.home) as store:
            store.run_finish(run["id"], "x")
        self.assertEqual(record["settings"], {"timeout": 300.0})
        lines = (self.home / "runs" / (run["id"] + ".mcp.jsonl")).read_text().splitlines()
        self.assertEqual([json.loads(line)["success"] for line in lines], [True, False])

    # -- execution at idle ---------------------------------------------------------
    def test_full_cycle_restart_reconcile_probation_applied(self):
        first = self.start()
        request = self.self_request({"timeout": 300})
        self.assertTrue(self.idle(first))
        self.assertEqual(self.restarts, [LABEL])
        record = self.requests()[0]
        self.assertEqual(record["status"], "restarting")
        self.assertEqual(record["checkpoint"]["last_run"] is not None, True)
        self.assertEqual((self.file()["timeout"], self.file()["request_id"]), (300.0, request["id"]))
        self.assertTrue(self.idle(first))  # Still waiting for launchd; no sessions start.
        # launchd replaced the worker.
        second = self.start()
        self.assertEqual(second.probation, request["id"])
        self.assertEqual(self.requests()[0]["status"], "applied_unconfirmed")
        self.assertEqual(runtime.read_state(self.home)["effective"]["timeout"], 300.0)
        self.assertEqual(self.file(runtime.LAST_GOOD_FILE)["timeout"], 600.0)
        brain = Brain()
        engine = Engine(self.home, brain, runtime=second)
        engine.apply_runtime(second.effective)
        with Store(self.home) as store:
            store.feed("david", "hello")
        engine._step()
        self.assertEqual(brain.calls[0]["timeout"], 300.0)
        record = self.requests()[0]
        self.assertEqual(record["status"], "applied")
        self.assertEqual(self.file(runtime.LAST_GOOD_FILE)["timeout"], 300.0)
        self.assertIn("runtime.started", self.audit_actions())
        self.assertIsNone(runtime.read_state(self.home)["probation"])

    def test_kickstart_failure_restores_file_and_notifies(self):
        def broken(label):
            raise RuntimeError("launchctl said no")
        controller = self.start(restarter=broken)
        before = (self.home / runtime.CONFIG_FILE).read_bytes()
        self.self_request({"timeout": 300})
        self.assertFalse(self.idle(controller))
        self.assertEqual((self.home / runtime.CONFIG_FILE).read_bytes(), before)
        record = self.requests()[0]
        self.assertEqual(record["status"], "failed")
        self.assertIn("launchctl said no", record["outcome"])
        self.assertEqual([m["to"] for m in self.outbox()], ["codex-manager"])

    def _operator_request_fails(self):
        def broken(label):
            raise RuntimeError("launchctl said no")
        controller = self.start(restarter=broken)
        with Store(self.home) as store:
            store.runtime_request({"timeout": 300}, "operator change", "operator")
        self.assertFalse(self.idle(controller))
        self.assertEqual(self.requests()[0]["status"], "failed")

    def test_backlog22_notice_to_unroutable_address_is_not_queued(self):
        with Store(self.home) as store:
            store.channel_register("fieldpost", "0" * 64, ["fieldpost"], "operator", routes=["codex-manager"])
        self._operator_request_fails()
        self.assertEqual([m["to"] for m in self.outbox()], ["codex-manager"])
        with Store(self.home) as store:
            rows = [r for r in store.audit_list(500) if r["action"] == "runtime.notice_unroutable"]
        self.assertEqual(len(rows), 1)
        self.assertIn("operator", json.dumps(rows[0], default=str))

    def test_backlog22_home_without_channels_keeps_queuing(self):
        self._operator_request_fails()
        self.assertEqual(sorted(m["to"] for m in self.outbox()), ["codex-manager", "operator"])

    def test_not_under_launchd_is_refused(self):
        controller = self.start(label=None)
        self.self_request({"timeout": 300})
        self.assertFalse(self.idle(controller))
        self.assertIn("not launchd-managed", self.requests()[0]["outcome"])
        self.assertEqual(self.restarts, [])

    def test_failure_pause_refuses_restart(self):
        controller = self.start()
        self.self_request({"timeout": 300})
        self.assertFalse(self.idle(controller, paused=True))
        self.assertTrue(self.requests()[0]["outcome"].startswith("paused_recent_failures"))
        self.assertEqual(self.restarts, [])

    def test_self_request_rechecked_at_execution(self):
        controller = self.start()
        self.self_request({"budget": 100})
        controller.baseline["budget"] = 50.0  # e.g. the operator lowered the baseline since the request
        controller.loaded["effective"]["budget"] = 50.0
        self.assertFalse(self.idle(controller))
        self.assertIn("baseline", self.requests()[0]["outcome"])

    def test_launchd_never_replacing_the_worker_restores_and_fails(self):
        controller = self.start()
        before = (self.home / runtime.CONFIG_FILE).read_bytes()
        self.self_request({"timeout": 300})
        self.assertTrue(self.idle(controller))
        self.clock.now += controller.GRACE_SECONDS + 1
        self.assertFalse(self.idle(controller))
        self.assertEqual((self.home / runtime.CONFIG_FILE).read_bytes(), before)
        self.assertIn("did not replace", self.requests()[0]["outcome"])

    def test_exit_mode_commits_then_raises(self):
        controller = self.start(mode="exit")
        self.self_request({"timeout": 300})
        with self.assertRaises(runtime.SelfRestart):
            self.idle(controller)
        self.assertEqual(self.requests()[0]["status"], "restarting")
        self.assertEqual(self.restarts, [])

    # -- start-time reconciliation -------------------------------------------------
    def test_invalid_file_after_restart_fails_request_and_falls_back(self):
        controller = self.start()
        self.self_request({"timeout": 300})
        self.idle(controller)
        (self.home / runtime.CONFIG_FILE).write_text("{broken")
        second = self.start()
        self.assertEqual(second.loaded["source"], "last-good")
        record = self.requests()[0]
        self.assertEqual(record["status"], "failed")
        self.assertIn("fell back to last-good", record["outcome"])
        self.assertIn("runtime.fallback", self.audit_actions())
        bodies = [m["body"] for m in self.outbox()]
        self.assertTrue(any("started on last-good" in body for body in bodies))
        self.assertTrue(any("failed after restart" in body for body in bodies))

    def test_file_changed_on_disk_is_superseded(self):
        controller = self.start()
        self.self_request({"timeout": 300})
        self.idle(controller)
        runtime.write_atomic(self.home / runtime.CONFIG_FILE, runtime.validate(baseline(timeout=900)),
                             written_by="hand")
        self.start()
        self.assertIn("superseded_on_disk", self.requests()[0]["outcome"])

    def test_crash_between_commit_and_kickstart_still_reconciles(self):
        controller = self.start(restarter=lambda label: (_ for _ in ()).throw(KeyboardInterrupt()))
        self.self_request({"timeout": 300})
        with self.assertRaises(KeyboardInterrupt):
            self.idle(controller)
        self.assertEqual(self.requests()[0]["status"], "restarting")
        self.assertEqual(self.start().loaded["effective"]["timeout"], 300.0)
        self.assertEqual(self.requests()[0]["status"], "applied_unconfirmed")

    # -- probation failure and revert (R6) ---------------------------------------------
    def probation_failure(self, error):
        first = self.start()
        request = self.self_request({"timeout": 60})
        self.idle(first)
        second = self.start()
        brain = Brain(error=error)
        engine = Engine(self.home, brain, runtime=second)
        engine.apply_runtime(second.effective)
        with Store(self.home) as store:
            store.feed("david", "hello")
        return request, second, engine

    def test_probation_timeout_reverts_to_last_good(self):
        request, second, engine = self.probation_failure(
            RuntimeError("Claude session exceeded 60.0 seconds; work and tool effects remain recorded."))
        with self.assertRaises(RuntimeError):
            engine._step()
        records = {r["id"]: r for r in self.requests()}
        failed = records[request["id"]]
        self.assertEqual(failed["status"], "failed")
        self.assertIn("exceeded 60.0 seconds", failed["outcome"])
        self.assertEqual(self.file()["timeout"], 600.0)  # reverted on disk at once
        revert = [r for r in records.values() if r.get("revert")][0]
        self.assertEqual(revert["status"], "requested")
        self.assertTrue(any("failed" in m["body"] for m in self.outbox()))
        # The revert runs at the next idle point, even while failures pause the worker...
        self.assertTrue(self.idle(second, paused=True))
        # ...and is never itself on probation.
        third = self.start()
        self.assertIsNone(third.probation)
        self.assertEqual({r["id"]: r for r in self.requests()}[revert["id"]]["status"], "applied")
        self.assertEqual(third.effective["timeout"], 600.0)

    def test_deliberate_stop_during_probation_is_not_a_verdict(self):
        request, second, engine = self.probation_failure(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            engine._step()
        self.assertEqual({r["id"]: r for r in self.requests()}[request["id"]]["status"], "applied_unconfirmed")
        with Store(self.home) as store:
            store.recover_runs()
        self.assertEqual(self.start().probation, request["id"])

    # -- serve integration, context, CLI ---------------------------------------------
    def test_serve_applies_effective_settings_and_logs_runtime_line(self):
        runtime.write_atomic(self.home / runtime.CONFIG_FILE, runtime.validate(baseline(timeout=300, poll=0.5)),
                             written_by="test")
        runtime.write_atomic(self.home / runtime.LAST_GOOD_FILE, runtime.validate(baseline()), written_by="test")
        updates = []
        stop = threading.Event()
        brain = Brain(action=lambda context: stop.set())
        brain.model, brain.timeout, brain.budget = "x", 1, 1
        with Store(self.home) as store:
            store.feed("david", "hello")
        controller = self.controller()
        Engine(self.home, brain, on_update=updates.append, runtime=controller).serve(poll_seconds=5, stop=stop)
        line = [u for u in updates if u["type"] == "runtime"][0]
        self.assertEqual((line["source"], line["effective"]["timeout"], line["pid"]), ("file", 300.0, self.pid))
        self.assertEqual(brain.calls[0]["timeout"], 300.0)
        with Store(self.home) as store:
            view = store.context()["runtime"]
        self.assertEqual((view["effective"]["timeout"], view["label"], view["allowed"]["max_self_budget"]),
                         (300.0, LABEL, 100.0))

    def test_cli_request_show_and_serve_flag_check(self):
        self.start()
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--home", str(self.home), "runtime", "request", "--set", "daily_limit=120",
                         "--set", "limit_allow=david,codex-manager", "--reason", "release day", "--by", "codex-manager"])
        self.assertEqual(code, 0)
        record = json.loads(out.getvalue())
        self.assertEqual((record["origin"], record["proposed"]["limit_allow"]), ("operator", ["codex-manager", "david"]))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["--home", str(self.home), "runtime", "show"]), 0)
        shown = json.loads(out.getvalue())
        self.assertEqual((shown["state"]["source"], shown["requests"][0]["id"]), ("baseline", record["id"]))
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["--home", str(self.home), "serve", "--claude", sys.executable,
                         "--runtime-config", str(self.home / "elsewhere.json")])
        self.assertEqual(code, 1)
        self.assertIn("--runtime-config must be", err.getvalue())


if __name__ == "__main__":
    unittest.main()
