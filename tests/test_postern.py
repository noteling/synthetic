"""M25 §5 / design test 12: Postern, the reference directory-bridge adapter, end to end in temp dirs."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from synthetics import postern
from synthetics.store import Store

ROOT = Path(__file__).resolve().parents[1]
KEY = "p" * 40


class PosternTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.homes = [self.base / "alpha", self.base / "beta"]
        self.key = self.base / "postern.key"
        self.key.write_text(KEY + "\n")
        self.key.chmod(0o600)
        for home in self.homes:
            with Store(home) as store:
                store.initialize(home.name, "tester")
                store.channel_register("postern", Store.credential_hash(KEY.encode()), ["postern:"], "test",
                                       routes=["david"])
        self.inbox, self.outbox = self.base / "in", self.base / "out"
        self.inbox.mkdir(mode=0o700)
        self.config_path = self.base / "postern.json"
        self.config_path.write_text(json.dumps(dict(v=1, channel="postern", key_file="postern.key",
                                                    homes=["alpha", "beta"], inbox="in", outbox="out")))
        env = {k: v for k, v in os.environ.items() if k != "SYNTHETICS_SESSION_RUN"}
        env["PYTHONPATH"] = str(ROOT)
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def bridge(self, instance=None):
        return postern.Postern(postern.load_config(self.config_path), instance=instance)

    def drop(self, name, lines):
        tmp = self.inbox / (name + ".partial")
        tmp.write_text("".join(json.dumps(line) + "\n" for line in lines))
        os.replace(tmp, self.inbox / (name + ".jsonl"))

    def line(self, external_id, home, **extra):
        return dict({"v": 1, "external_id": external_id, "occurred_at": "2026-11-17T21:00:00Z", "sender": "david",
                     "body": "hello " + external_id, "source": "postern:mail", "thread": None, "kind": "message",
                     "home": home}, **extra)

    def events(self, home):
        with Store(home) as store:
            return [e for e in store._all("events") if e.get("source", "").startswith("postern:")]

    def test_inbound_outbound_ack_replay(self):
        self.drop("batch1", [self.line("e1", "alpha"), self.line("e2", "beta"), self.line("e3", "gamma"),
                             self.line("e4", "alpha", source="other:x")])
        report = self.bridge().cycle()
        self.assertEqual(report["inbound"], [dict(file="batch1.jsonl", results=4)])
        results = [json.loads(t) for t in (self.inbox / "batch1.result.jsonl").read_text().splitlines()]
        self.assertEqual([r["result"] for r in results], ["accepted", "accepted", "rejected", "rejected"])
        self.assertEqual([r["error"]["code"] for r in results[2:]], ["not_routed", "source_not_allowed"])
        self.assertTrue((self.inbox / "done" / "batch1.jsonl").exists())
        self.assertFalse((self.inbox / "batch1.jsonl").exists())
        self.assertEqual([e["external_id"] for e in self.events(self.homes[0])], ["e1"])
        self.assertIsNotNone(self.events(self.homes[0])[0].get("auth"))

        # Replaying the same file (e.g. a crash before it moved to done/) yields duplicates, same event ids.
        shutil.copy(self.inbox / "done" / "batch1.jsonl", self.inbox / "batch1.jsonl")
        self.bridge().cycle()
        again = [json.loads(t) for t in (self.inbox / "batch1.result.jsonl").read_text().splitlines()]
        self.assertEqual([r["result"] for r in again[:2]], ["duplicate", "duplicate"])
        self.assertEqual([r["event_id"] for r in again[:2]], [r["event_id"] for r in results[:2]])
        self.assertEqual(len(self.events(self.homes[0])), 1)

        # Outbound: an employee message to a routed address becomes an outbox file and is acked delivered.
        with Store(self.homes[1]) as store:
            sent = store.message_send("david", "reply from beta", key="r1")
            store.message_send("codex-manager", "not routed here", key="r2")
        report = self.bridge().cycle()
        self.assertEqual(report["delivered"], [dict(home="beta", message_id=sent["id"])])
        record = json.loads((self.outbox / (sent["id"] + ".json")).read_text())
        self.assertEqual((record["body"], record["to"], record["home"]), ("reply from beta", "david", "beta"))
        with Store(self.homes[1]) as store:
            summary = {m["id"]: m["delivery"] for m in store.outbox_list()}
        self.assertEqual(summary[sent["id"]], "delivered")
        self.assertEqual(len(list(self.outbox.glob("*.json"))), 1)
        self.assertEqual(self.bridge().cycle()["delivered"], [])

    def test_crash_between_claim_and_write_is_reconciled_failed_then_sent(self):
        with Store(self.homes[0]) as store:
            sent = store.message_send("david", "lost in a crash", key="c1")
            store.channel_claim(KEY.encode(), sent["id"], instance="postern-dead")
        report = self.bridge(instance="postern-new").cycle()
        self.assertEqual(report["reconciled"], [dict(home="alpha", message_id=sent["id"], outcome="failed")])
        self.assertEqual(report["delivered"], [dict(home="alpha", message_id=sent["id"])])
        self.assertTrue((self.outbox / (sent["id"] + ".json")).exists())

    def test_crash_between_write_and_ack_is_reconciled_delivered_without_rewrite(self):
        with Store(self.homes[0]) as store:
            sent = store.message_send("david", "written then crashed", key="c2")
            store.channel_claim(KEY.encode(), sent["id"], instance="postern-dead")
        path = self.outbox / (sent["id"] + ".json")
        self.outbox.mkdir(exist_ok=True)
        path.write_text(json.dumps(dict(message_id=sent["id"], body="written then crashed")))
        before = path.stat().st_mtime_ns
        report = self.bridge(instance="postern-new").cycle()
        self.assertEqual(report["reconciled"], [dict(home="alpha", message_id=sent["id"], outcome="delivered")])
        self.assertEqual(report["delivered"], [])
        self.assertEqual(path.stat().st_mtime_ns, before)
        with Store(self.homes[0]) as store:
            self.assertEqual(store.outbox_list()[0]["delivery"], "delivered")

    def test_refuses_group_writable_inbox(self):
        self.inbox.chmod(0o770)
        with self.assertRaises(postern.PosternError):
            self.bridge()

    def test_cli_once_and_refused_in_session(self):
        self.drop("one", [self.line("c1", "alpha")])
        argv = [sys.executable, "-m", "synthetics", "postern", "run", "--config", str(self.config_path), "--once"]
        out = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["inbound"], [dict(file="one.jsonl", results=1)])
        env = dict(os.environ, SYNTHETICS_SESSION_RUN="run-x")
        refused = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=env)
        self.assertNotEqual(refused.returncode, 0)


if __name__ == "__main__":
    unittest.main()
