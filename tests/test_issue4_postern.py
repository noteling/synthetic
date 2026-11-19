"""Issue #4: Postern must not crash on input it cannot use, and one failing home must not block the others."""
import io
import json
import subprocess
import unittest
from unittest import mock

from synthetics import postern
from synthetics.store import Store
from tests import test_postern as base  # module import: keeps PosternTests from being collected twice

SEP = "a b c\u0085d"  # str.splitlines() splits on each of these; json.dumps(ensure_ascii=False) keeps them raw


class Issue4PosternTests(unittest.TestCase):
    setUp = base.PosternTests.setUp
    tearDown = base.PosternTests.tearDown
    bridge = base.PosternTests.bridge
    drop = base.PosternTests.drop
    line = base.PosternTests.line
    events = base.PosternTests.events

    def results(self, name):
        return [json.loads(t) for t in (self.inbox / (name + ".result.jsonl")).read_text().split("\n") if t]

    def raw_drop(self, name, data):
        tmp = self.inbox / (name + ".partial")
        tmp.write_bytes(data)
        tmp.replace(self.inbox / (name + ".jsonl"))

    def test_unicode_line_separators_in_a_body_go_in_and_out_intact(self):
        self.raw_drop("sep", (json.dumps(self.line("u1", "alpha", body=SEP), ensure_ascii=False) + "\n").encode())
        report = self.bridge().cycle()
        self.assertEqual(report.get("errors", []), [])
        self.assertEqual([r["result"] for r in self.results("sep")], ["accepted"])
        self.assertEqual([e["body"] for e in self.events(self.homes[0])], [SEP])
        with Store(self.homes[1]) as store:
            sent = store.message_send("david", "reply " + SEP, key="u2")
        report = self.bridge().cycle()
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["delivered"], [dict(home="beta", message_id=sent["id"])])
        record = json.loads((self.outbox / (sent["id"] + ".json")).read_text())
        self.assertEqual(record["body"], "reply " + SEP)

    def test_a_line_that_is_not_an_object_is_rejected_even_with_one_home(self):
        self.config_path.write_text(json.dumps(dict(v=1, channel="postern", key_file="postern.key",
                                                    homes=["alpha"], inbox="in", outbox="out")))
        self.raw_drop("arr", b'[1, 2]\n"text"\n' + json.dumps(self.line("o1", None)).encode() + b"\n")
        report = self.bridge().cycle()
        self.assertEqual(report["errors"], [])
        rows = self.results("arr")
        self.assertEqual([r["result"] for r in rows], ["rejected", "rejected", "accepted"])
        self.assertEqual([r["error"]["code"] for r in rows[:2]], ["bad_line", "bad_line"])
        self.assertTrue((self.inbox / "done" / "arr.jsonl").exists())

    def test_a_file_that_is_not_utf8_is_answered_and_moved_not_replayed(self):
        self.raw_drop("latin", b'{"v": 1, "body": "caf\xe9"}\n')
        report = self.bridge().cycle()
        self.assertEqual(report["errors"], [])
        self.assertEqual([(r["result"], r["error"]["code"]) for r in self.results("latin")], [("rejected", "bad_file")])
        self.assertTrue((self.inbox / "done" / "latin.jsonl").exists())
        self.assertFalse((self.inbox / "latin.jsonl").exists())

    def test_a_contract_timeout_is_a_postern_error(self):
        bridge = self.bridge()
        with mock.patch.object(postern.subprocess, "run", side_effect=subprocess.TimeoutExpired(["x"], 120)):
            with self.assertRaises(postern.PosternError):
                bridge._contract(str(self.homes[0]), "pending")

    def test_one_failing_home_does_not_block_the_others(self):
        with Store(self.homes[1]) as store:
            sent = store.message_send("david", "beta still delivers", key="h1")
        self.drop("both", [self.line("h3", "beta"), self.line("h2", "alpha")])
        bridge = self.bridge()
        real = bridge._contract

        def contract(home, name, *args, **kwargs):
            if postern.Path(home).name == "alpha":
                raise postern.PosternError("channel %s on alpha failed (exit 1): auth_failed" % name)
            return real(home, name, *args, **kwargs)

        with mock.patch.object(bridge, "_contract", side_effect=contract):
            report = bridge.cycle()
        self.assertEqual(report["delivered"], [dict(home="beta", message_id=sent["id"])])
        stages = sorted((e["stage"], e.get("home") or e.get("file")) for e in report["errors"])
        self.assertEqual(stages, [("inbound", "both.jsonl"), ("reconcile", "alpha")])
        # The file whose alpha batch failed stays for the next cycle; beta's line (fed first) replays as duplicate.
        self.assertTrue((self.inbox / "both.jsonl").exists())
        self.bridge().cycle()
        self.assertEqual([r["result"] for r in self.results("both")], ["duplicate", "accepted"])

    def test_run_logs_any_exception_and_keeps_cycling_and_once_fails_on_errors(self):
        calls = []

        def cycle(bridge):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            raise KeyboardInterrupt  # stop the loop after the second cycle

        err = io.StringIO()
        with mock.patch.object(postern.Postern, "cycle", cycle), mock.patch.object(postern.time, "sleep"), \
                mock.patch.object(postern.sys, "stderr", err):
            with self.assertRaises(KeyboardInterrupt):
                postern.run(self.config_path, out=io.StringIO())
        self.assertEqual(len(calls), 2)
        self.assertIn("RuntimeError: boom", err.getvalue())

        failing = dict(inbound=[], delivered=[], reconciled=[], errors=[dict(stage="deliver", home="alpha", error="x")])
        with mock.patch.object(postern.Postern, "cycle", lambda bridge: failing), \
                mock.patch.object(postern.sys, "stderr", io.StringIO()):
            self.assertEqual(postern.run(self.config_path, once=True, out=io.StringIO()), 1)


if __name__ == "__main__":
    unittest.main()
