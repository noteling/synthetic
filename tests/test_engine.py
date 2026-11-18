import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from synthetics.engine import Engine, WorkerBusy, WorkerLock, worker_status
from synthetics.store import Store


class Brain:
    def __init__(self, action):
        self.action = action
        self.calls = 0

    def run(self, context):
        self.calls += 1
        self.action(context)
        return SimpleNamespace(summary="Work session ended.", cost=0.01)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        with Store(self.home) as store:
            self.employee = store.initialize("Rowan", "Noteling maintainer")
            store.entity_put("david", "David", "person")

    def test_onboarding_question_answer_restart_and_cross_project_memory(self):
        with Store(self.home) as store:
            work = store.work_put("Understand the release procedure")
            store.feed("david", "Read the notes and ask about anything undocumented.", external_id="kt-1")

        def investigate(context):
            self.assertEqual(context["identity"]["id"], self.employee["id"])
            with Store(self.home, run_id=context["run"]["id"]) as store:
                store.question_ask("david", "Who makes the final release decision?",
                                   work_id=work["id"], key="release-owner")
        first = Brain(investigate)
        Engine(self.home, first).run(max_turns=3)
        self.assertEqual(first.calls, 1)  # Waiting never causes idle model calls.
        with Store(self.home) as store:
            question = store.question_list()[0]
            store.channel_register("test-chan", Store.credential_hash(b"k" * 32), ["manual"], "test")
            store.feed("david", "I decide when to publish. Prepare the release and tell me it is ready.",
                       reply_to=question["id"], source="manual", external_id="kt-2", credential=b"k" * 32)

        def learn_and_finish(context):
            self.assertTrue(any(e["reply_to"] == question["id"] for e in context["events"]))
            with Store(self.home, run_id=context["run"]["id"]) as store:
                store.memory_put("david", "release_decision", "David decides when to publish.",
                                  source=context["events"][0]["id"], key="release-decision")
                store.work_put(id=work["id"], status="done", note="Release ownership learned from David's reply.",
                               evidence=[{"level": "verified", "ref": question["id"]}])
        Engine(self.home, Brain(learn_and_finish)).run()

        with Store(self.home) as store:
            store.entity_put("another-product", "Another product", "project")
            store.feed("david", "Remember our release discussion while helping with another product.")
        def next_project(context):
            # Employee memory is available after a fresh Engine and a different project.
            memories = json.dumps(context)
            self.assertIn("David decides when to publish", memories)
            self.assertEqual(context["identity"]["id"], self.employee["id"])
        Engine(self.home, Brain(next_project)).run()

    def test_input_during_execution_survives_for_next_session(self):
        with Store(self.home) as store:
            first = store.feed("david", "First instruction")
        def during(context):
            self.assertEqual([e["id"] for e in context["events"]], [first["id"]])
            with Store(self.home) as store:
                store.feed("david", "An update that arrived while you worked")
        Engine(self.home, Brain(during)).run()
        with Store(self.home) as store:
            pending = store.event_pending()
        self.assertEqual(len(pending), 1)
        self.assertIn("while you worked", pending[0]["body"])

    def test_ready_work_continues_without_another_human_prompt(self):
        with Store(self.home) as store:
            work = store.work_put("Two sessions of work")
        count = []
        def continue_work(context):
            count.append(context["run"]["id"])
            with Store(self.home) as store:
                closing = dict(evidence=[{"level": "observed", "ref": count[0]}]) if len(count) == 2 else {}
                store.work_put(id=work["id"], status="done" if len(count) == 2 else "ready",
                               note="Finished" if len(count) == 2 else "First investigation done; next inspect evidence.",
                               **closing)
        brain = Brain(continue_work)
        Engine(self.home, brain).run(max_turns=5)
        self.assertEqual(brain.calls, 2)

    def test_recovery_preserves_effects_and_failed_input_for_reconciliation(self):
        with Store(self.home) as store:
            store.feed("david", "Please investigate")
            old = store.run_begin()
            store.message_send("david", "I started investigating.", key="started")
        def reconcile(context):
            self.assertIn("interrupted", json.dumps(context))
            with Store(self.home, run_id=old["id"]) as stale:
                with self.assertRaises(ValueError):
                    stale.message_send("david", "late duplicate", key="late")
            with Store(self.home) as store:
                self.assertEqual(len(store.outbox_list()), 1)
        Engine(self.home, Brain(reconcile)).run()

    def test_single_worker_lock_released_after_exception(self):
        self.assertFalse(worker_status(self.home)["running"])
        with WorkerLock(self.home):
            self.assertTrue(worker_status(self.home)["running"])
            with self.assertRaises(WorkerBusy):
                Engine(self.home, Brain(lambda _: None)).run()
        with WorkerLock(self.home):
            pass
        self.assertFalse(worker_status(self.home)["running"])

    def test_no_model_call_when_idle_and_due_reminder_wakes_it(self):
        brain = Brain(lambda _: None)
        self.assertEqual(Engine(self.home, brain).run(), [])
        self.assertEqual(brain.calls, 0)
        with Store(self.home) as store:
            store.reminder_set("2020-01-01T10:00:00Z", "Check the promised result", key="follow-up")
        Engine(self.home, brain).run(max_turns=3)
        self.assertEqual(brain.calls, 1)

    def test_failure_retains_input_and_serve_stops_after_repeated_errors(self):
        with Store(self.home) as store:
            store.feed("david", "Work to retry")
        def fail(_):
            raise RuntimeError("fixture connection failure")
        brain = Brain(fail)
        with self.assertRaisesRegex(RuntimeError, "three failed"):
            Engine(self.home, brain).serve(poll_seconds=0.001, retry_seconds=0.001)
        self.assertEqual(brain.calls, 3)
        with Store(self.home) as store:
            self.assertEqual(len(store.event_pending()), 1)
            self.assertTrue(all(r["status"] == "failed" for r in store.run_list()))

    def test_setup_callback_failure_does_not_leave_run_active(self):
        with Store(self.home) as store:
            store.feed("david", "Work")
        def broken(event):
            if event["type"] == "started":
                raise RuntimeError("status callback failed")
        with self.assertRaises(RuntimeError):
            Engine(self.home, Brain(lambda _: None), on_update=broken).run()
        with Store(self.home) as store:
            self.assertEqual(store.run_list()[0]["status"], "failed")

    def test_completion_reporting_error_does_not_change_finished_work(self):
        with Store(self.home) as store:
            store.feed("david", "Work")
        def broken(event):
            if event["type"] == "finished":
                raise RuntimeError("completion display failed")
        with self.assertRaisesRegex(RuntimeError, "completion display failed"):
            Engine(self.home, Brain(lambda _: None), on_update=broken).run()
        with Store(self.home) as store:
            self.assertEqual(store.run_list()[0]["status"], "finished")
            self.assertEqual(store.event_pending(), [])


class CliTests(unittest.TestCase):
    def test_operator_can_provision_and_revoke_declared_access(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "employee"
            root = Path(directory) / "internal"
            command = [sys.executable, "-m", "synthetics", "--home", str(home)]
            def call(*args):
                result = subprocess.run(command + list(args), text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)
            call("init", "--name", "Maintainer", "--role", "Internal tools", "--human", "david")
            grant = call("access", "grant", str(root), "--mode", "write", "--purpose", "Create internal projects")
            self.assertEqual(call("access", "list"), [grant])
            self.assertEqual(call("status")["access_grants"], [grant])
            call("access", "revoke", grant["id"])
            self.assertEqual(call("access", "list"), [])

    def test_cli_feed_can_be_retried_and_state_read_in_fresh_process(self):
        with tempfile.TemporaryDirectory() as home:
            command = [sys.executable, "-m", "synthetics", "--home", home]
            def call(*args, **kwargs):
                return subprocess.run(command + list(args), text=True, capture_output=True, **kwargs)
            init = call("init", "--name", "Rowan", "--role", "Maintainer", "--human", "david")
            self.assertEqual(init.returncode, 0, init.stderr)
            event = json.dumps({"sender": "david", "body": "A durable message", "external_id": "message-1"})
            result = call("ingest", input=event + "\n" + event + "\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            rows = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(rows[0]["id"], rows[1]["id"])
            inbox = call("inbox")
            self.assertEqual(len(json.loads(inbox.stdout)), 2)  # Welcome and deduplicated message.
            state = call("status")
            self.assertEqual(json.loads(state.stdout)["identity"]["name"], "Rowan")
            repeat = call("init", "--name", "Different", "--role", "Other")
            self.assertEqual(repeat.returncode, 1)

    def test_sigterm_stops_model_and_preserves_unhandled_input(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with Store(home) as store:
                store.initialize("Rowan", "Maintainer")
                store.feed("david", "A task interrupted by service shutdown")
            fake = home / "fake-claude"
            marker = home / "model-started"
            late = home / "late-effect"
            fake.write_text("#!" + sys.executable + "\nimport pathlib, time, sys\n"
                            "sys.stdin.read()\n"
                            "pathlib.Path(%r).write_text('started')\n" % str(marker) +
                            "time.sleep(1.5)\npathlib.Path(%r).write_text('must not happen')\n" % str(late))
            fake.chmod(0o700)
            process = subprocess.Popen([sys.executable, "-m", "synthetics", "--home", str(home),
                                        "run", "--claude", str(fake)], stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline and process.poll() is None:
                    time.sleep(0.02)
                self.assertTrue(marker.exists(), "fixture model did not start")
                stopped = subprocess.run([sys.executable, "-m", "synthetics", "--home", str(home), "stop"],
                                         text=True, capture_output=True)
                self.assertEqual(stopped.returncode, 0, stopped.stderr)
                self.assertTrue(json.loads(stopped.stdout)["stop_requested"])
                _, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 130, stderr)
                time.sleep(1.6)
                self.assertFalse(late.exists(), "model survived worker SIGTERM")
                with Store(home) as store:
                    self.assertEqual(store.run_list()[0]["status"], "failed")
                    self.assertEqual(len(store.event_pending()), 1)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()


if __name__ == "__main__":
    unittest.main()
