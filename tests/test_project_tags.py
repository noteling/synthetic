"""M21 phase 2: per-message project tags (feed --project, peer project=) and configured human identities."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from synthetics import capture
from synthetics.projects import ProjectStore
from synthetics.store import Store
from tests.humans_fixture import write_host_humans

T = "codex:00000000-0000-4000-8000-000000000001"
ROOT = Path(__file__).resolve().parents[1]


class TagTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "manager")
        self.store.initialize("Codex", "test manager")
        with self.ps("synthetics"):
            pass
        with self.ps("noteling"):
            pass

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def ps(self, project="synthetics"):
        return ProjectStore(self.root / "projects" / project / "memory.sqlite3", project)

    def sources(self, project="synthetics"):
        with self.ps(project) as ps:
            return [e for e in ps.entries() if e["kind"] == "source"]

    def feed(self, body, project=None, by="feeder", sender="david", source="codex-desktop:abc", thread=T, ext=None):
        return self.store.feed(sender, body, thread=thread, source=source, external_id=ext, project=project,
                               project_by=by)

    def test_feeder_tag_on_unbound_channel_captures(self):
        write_host_humans(self.store.home)
        ev = self.feed("hello", project="synthetics", ext="x1")
        self.assertEqual(ev["project_ref"], {"id": "synthetics", "valid": True, "by": "feeder", "effect": "capture"})
        got = self.sources()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["body"]["capture"], {"mode": "tag", "binding_id": None})
        self.assertEqual(got[0]["body"]["origin"]["role"], "human")
        self.assertEqual(got[0]["body"]["origin"]["auth"], None)
        self.assertEqual(self.sources("noteling"), [])
        # Persisted, and a redelivery returns the same event and captures nothing more.
        again = self.feed("hello", project="synthetics", ext="x1")
        self.assertEqual(again["id"], ev["id"])
        self.assertEqual(self.store._get("events", ev["id"])["project_ref"]["effect"], "capture")
        self.assertEqual(len(self.sources()), 1)

    def test_untagged_unbound_not_captured_and_no_ref(self):
        ev = self.feed("hello")
        self.assertNotIn("project_ref", ev)
        self.assertEqual(self.sources(), [])

    def test_bound_elsewhere_and_bound_here(self):
        capture.bind(self.store, "synthetics", thread=T, source_prefixes=["codex-desktop:"], by="op", reason="t")
        a = self.feed("about noteling", project="noteling")
        self.assertEqual((a["project_ref"]["effect"], a["project_ref"]["reason"]), ("none", "bound-elsewhere"))
        b = self.feed("about synthetics", project="synthetics")
        self.assertEqual((b["project_ref"]["effect"], b["project_ref"]["reason"]), ("binding", "bound-here"))
        self.assertEqual(self.sources("noteling"), [])
        self.assertEqual(sorted(e["body"]["origin"]["event_id"] for e in self.sources()), sorted([a["id"], b["id"]]))
        self.assertTrue(all(e["body"]["capture"]["mode"] == "binding" for e in self.sources()))

    def test_invalid_and_unknown_project(self):
        a = self.feed("x", project="Bad Name")
        self.assertEqual((a["project_ref"]["valid"], a["project_ref"]["effect"]), (False, "none"))
        b = self.feed("y", project="stand")
        self.assertEqual(b["project_ref"]["reason"], "unknown-project")
        self.assertFalse((self.root / "projects" / "stand").exists())
        with self.assertRaises(ValueError):
            self.feed("z", project="synthetics", by="someone")

    def test_peer_tag_needs_sender_write_grant(self):
        with self.ps() as ps:
            ps.grant("employee-codex", "write", by="op")
            ps.grant("employee-rowan", "read", by="op")
        ok = self.feed("brief", project="synthetics", by="sender", sender="employee-codex", source="fieldpost")
        no = self.feed("note", project="synthetics", by="sender", sender="employee-rowan", source="fieldpost")
        self.assertEqual(ok["project_ref"]["effect"], "capture")
        self.assertEqual((no["project_ref"]["effect"], no["project_ref"]["reason"]), ("none", "sender-not-granted"))
        got = self.sources()
        self.assertEqual([e["body"]["origin"]["event_id"] for e in got], [ok["id"]])
        self.assertEqual(got[0]["body"]["origin"]["role"], "other")
        audits = [r[0] for r in self.store.db.execute("SELECT action FROM audit WHERE action='event.project_ref'")]
        self.assertEqual(len(audits), 2)

    def test_outgoing_tag_requires_own_grant_and_captures_out(self):
        me = self.store.identity()["id"]
        with self.ps() as ps:
            ps.grant("someone-else", "write", by="op")
        with self.assertRaises(ValueError):
            self.store.message_send("codex-manager", "hi", key="k1", project="synthetics")
        with self.assertRaises(ValueError):
            self.store.message_send("codex-manager", "hi", key="k2", project="nope")
        with self.ps() as ps:
            ps.grant(me, "write", by="op")
        m = self.store.message_send("codex-manager", "brief", key="k3", project="synthetics")
        self.assertEqual(m["project"], "synthetics")
        got = self.sources()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["body"]["origin"]["direction"], "out")
        self.assertEqual(got[0]["body"]["origin"]["role"], "out")
        # Reconciling the same key returns the same message; an untagged old key still reconciles.
        self.assertEqual(self.store.message_send("codex-manager", "brief", key="k3", project="synthetics")["id"],
                         m["id"])
        plain = self.store.message_send("codex-manager", "plain", key="k4")
        self.assertNotIn("project", plain)

    def test_humans_config(self):
        cfg = capture.humans(self.store)
        self.assertFalse(cfg["present"])
        self.assertEqual((cfg["senders"], cfg["default_human"]), ([], None))
        (self.root / "manager" / "humans.json").write_text(json.dumps(
            {"senders": ["alice"], "channel_prefixes": ["slack:"], "secretary_prefixes": ["assistant-"]}))
        a = self.feed("from alice", project="synthetics", sender="alice", source="slack:C1")
        d = self.feed("from david", project="synthetics", sender="david", source="codex-desktop:x")
        s = self.feed("relay", project="synthetics", sender="bot", source="assistant-1")
        roles = {e["body"]["origin"]["event_id"]: e["body"]["origin"]["role"] for e in self.sources()}
        self.assertEqual(roles, {a["id"]: "human", d["id"]: "other", s["id"]: "secretary"})
        priv = capture.uncovered(self.store, "x")["private"]
        # alice (sender) is a human-channel message; david on codex-desktop no longer is (all here are captured).
        self.assertEqual(priv, [])
        self.store.feed("bob", "untagged", source="slack:C2")
        self.store.feed("david", "untagged", source="codex-desktop:y")
        priv = [p["source"] for p in capture.uncovered(self.store, "x")["private"]]
        self.assertEqual(priv, ["slack:C2"])
        (self.root / "manager" / "humans.json").write_text(json.dumps({"senders": "alice"}))
        with self.assertRaises(ValueError):
            capture.humans(self.store)

    def test_cli_feed_and_ingest_project(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        env.pop("SYNTHETICS_SESSION_RUN", None)
        home = str(self.root / "manager")
        out = subprocess.run([sys.executable, "-m", "synthetics", "--home", home, "feed", "hi", "--from", "david",
                              "--source", "codex-desktop:c", "--project", "synthetics"],
                             capture_output=True, text=True, env=env, cwd=str(ROOT))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["project_ref"]["effect"], "capture")
        line = json.dumps({"sender": "david", "body": "x", "source": "codex-desktop:d", "project": "noteling"})
        out = subprocess.run([sys.executable, "-m", "synthetics", "--home", home, "ingest"], input=line + "\n",
                             capture_output=True, text=True, env=env, cwd=str(ROOT))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["project_ref"]["id"], "noteling")
        self.assertEqual(len(self.sources("noteling")), 1)


if __name__ == "__main__":
    unittest.main()
