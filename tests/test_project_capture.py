"""M21 tests 13, 14, 18, 19, 20, 21 and part of 7 (capture at ingest through bindings)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from synthetics import capture
from synthetics.projects import ProjectStore
from synthetics.store import Store
from tests.humans_fixture import write_host_humans

T = "codex:00000000-0000-4000-8000-000000000001"
P = ["codex-desktop:00000000", "desktop-secretary-"]


class CaptureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "manager")
        write_host_humans(self.store.home)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def ps(self, project="synthetics"):
        return ProjectStore(self.root / "projects" / project / "memory.sqlite3", project)

    def feed(self, body, source="codex-desktop:00000000-0000", sender="david", thread=T, ext=None):
        return self.store.feed(sender, body, thread=thread, source=source, external_id=ext)

    def sources(self, project="synthetics"):
        with self.ps(project) as ps:
            return [e for e in ps.entries() if e["kind"] == "source"]

    def bind(self):
        return capture.bind(self.store, "synthetics", thread=T, source_prefixes=P, by="codex-manager",
                            reason="M21 deploy")

    def test_19_lifecycle(self):
        before = self.feed("before", ext="e1")
        b = self.bind()
        self.feed("before", ext="e1")  # redelivery returns the existing event; not captured
        self.assertEqual(self.sources(), [])
        after = self.feed("after", ext="e2")
        sec = self.feed("handoff", source="desktop-secretary-handoff", sender="codex-desktop", ext="e3")
        self.feed("telegram", source="telegram:1000000001", ext="e4")   # same thread, unbound prefix
        self.feed("other thread", thread="codex:other", ext="e5")       # bound prefix, other thread
        got = {e["body"]["origin"]["event_id"]: e for e in self.sources()}
        self.assertEqual(set(got), {after["id"], sec["id"]})
        self.assertNotIn(before["id"], got)
        self.assertEqual(got[after["id"]]["body"]["capture"], {"mode": "binding", "binding_id": b["id"]})
        capture.unbind(self.store, b["id"], by="codex-manager", reason="test")
        self.feed("late", ext="e6")
        self.assertEqual(len(self.sources()), 2)
        actions = [r[0] for r in self.store.db.execute("SELECT action FROM audit")]
        self.assertIn("binding.created", actions)
        self.assertIn("binding.ended", actions)
        with self.ps() as ps:
            self.assertEqual([e["kind"] for e in ps.entries()].count("binding"), 2)

    def test_19_bind_allowed_in_session(self):
        with mock.patch.dict(os.environ, {"SYNTHETICS_SESSION_RUN": "run-x"}):
            b = self.bind()
        row = self.store.db.execute("SELECT data FROM audit WHERE action='binding.created'").fetchone()
        self.assertIn("codex-manager", row[0])
        self.assertEqual(b["active_from"]["seq"], 0)

    def test_18_bound_mentions_noteling_retained(self):
        self.bind()
        self.feed("noteling packs look odd; no action", ext="n1")
        self.assertEqual(len(self.sources()), 1)
        self.assertFalse((self.root / "projects" / "noteling").exists())
        with self.ps() as ps:
            st = ps.state()
        self.assertTrue(st)  # uncovered report is step 5; the source is retained

    def test_20_auth_copied_and_roles(self):
        self.bind()
        self.feed("hi", ext="a1")
        self.feed("relay", source="desktop-secretary-review", sender="codex-desktop", ext="a2")
        roles = sorted((e["body"]["origin"]["role"], e["body"]["quote_source"]) for e in self.sources())
        self.assertEqual(roles, [("human", "event"), ("secretary", "secondhand")])
        self.assertTrue(all(e["body"]["origin"]["auth"] is None for e in self.sources()))

    def test_13_14_failure_visible_then_idempotent(self):
        self.bind()
        with mock.patch.object(capture, "ProjectStore", side_effect=OSError("disk gone")):
            ev = self.feed("x" * 10, ext="f1")
        self.assertEqual(self.store._get("events", ev["id"])["body"], "x" * 10)  # delivered
        st = capture.capture_status(self.store)
        self.assertEqual(st["pending"], 1)
        self.assertIn("disk gone", st["failing"][0]["error"])
        self.assertEqual(capture.capture_drain(self.store), {"done": 1, "failed": 0})
        # crash between project commit and done: re-run is absorbed by the key
        self.store.db.execute("UPDATE capture_outbox SET status='pending'")
        capture.capture_drain(self.store)
        self.assertEqual(len(self.sources()), 1)

    def test_7_large_body_parts(self):
        self.bind()
        body = ("é" * 35000)  # 70000 bytes
        ev = self.feed(body, ext="big")
        parts = sorted(self.sources(), key=lambda e: e["body"]["part"]["i"])
        self.assertEqual(len(parts), 2)
        with self.ps() as ps:
            joined = b"".join(ps.source_bytes(e["id"]) for e in parts)
        self.assertEqual(joined, body.encode())
        self.assertEqual(parts[0]["body"]["origin"]["event_id"], ev["id"])


    def test_21_outgoing_on_bound_thread(self):
        from synthetics.projects import ProjectError
        self.store.initialize("Codex", "Manager")
        self.bind()
        sent = self.store.message_send("david", "our reply", key="k-out", thread=T)
        self.store.message_send("david", "elsewhere", key="k-other", thread="codex:other")
        self.store.message_send("david", "no thread", key="k-none")
        self.store.message_send("david", "our reply", key="k-out", thread=T)  # idempotent replay
        srcs = self.sources()
        self.assertEqual(len(srcs), 1)
        o = srcs[0]["body"]["origin"]
        self.assertEqual((o["event_id"], o["direction"], o["role"]), (sent["id"], "out", "out"))
        self.assertEqual(o["sender"], self.store.identity()["id"])
        sec = self.feed("relay", source="desktop-secretary-handoff", sender="codex-desktop", ext="s1")
        ids = {e["body"]["origin"]["event_id"]: e for e in self.sources()}
        self.assertEqual(ids[sec["id"]]["body"]["quote_source"], "secondhand")
        with self.ps() as ps:
            for e in (srcs[0], ids[sec["id"]]):
                with self.assertRaises(ProjectError) as cm:
                    ps.append([{"kind": "intent", "body": {"act": "request", "title": "t",
                                                           "claims": [{"cid": "c1", "text": "x"}],
                                                           "sources": [e["id"]]}}],
                              author="tester", operator=True)
                self.assertEqual(cm.exception.code, "not_human_source")

    def test_20_auth_survives_export_import_refold(self):
        self.bind()
        self.feed("plain", ext="x1")
        self.feed("also null", ext="x2")
        # A non-null auth is appended directly (an authenticated feed needs channel keys; M22 tests cover that).
        with self.ps() as ps:
            ps.append([{"kind": "source", "bytes": "authd",
                        "body": {"origin": {"event_id": "event-x", "auth": {"channel": "relay", "key": "k1"},
                                            "role": "human", "sender": "david"},
                                 "capture": {"mode": "promotion"}, "quote_source": "event"}}],
                      author="tester", operator=True)
            before = {e["id"]: e["body"]["origin"]["auth"] for e in ps.entries() if e["kind"] == "source"}
            ps.export(rebuild=True)
            jsonl = ps.export_path
            refold = ps.state()
        with ProjectStore.import_jsonl(jsonl, self.root / "copy.sqlite3", "synthetics") as cp:
            after = {e["id"]: e["body"]["origin"]["auth"] for e in cp.entries() if e["kind"] == "source"}
            self.assertEqual(cp.state()["sources"], refold["sources"])
        self.assertEqual(before, after)
        self.assertEqual(sorted(map(repr, before.values())).count("None"), 2)


    def test_snapshot_lines_and_backoff(self):
        self.store.initialize("Codex", "Manager")
        b = self.bind()
        ctx = self.store.context()
        self.assertEqual(len(ctx["project_bindings"]), 1)
        self.assertIn(b["id"], ctx["project_bindings"][0])
        self.assertIn("desktop-secretary-", ctx["project_bindings"][0])
        with mock.patch.object(capture, "ProjectStore", side_effect=OSError("down")):
            self.feed("x", ext="b1")
        self.assertTrue(capture.has_pending(self.store))
        self.assertEqual(self.store.context()["project_capture"]["pending"], 1)
        self.assertEqual(capture.capture_drain(self.store, backoff=60), {"done": 0, "failed": 0})  # backing off
        self.assertEqual(capture.capture_drain(self.store), {"done": 1, "failed": 0})
        self.assertEqual(self.store.context()["project_capture"], {"pending": 0, "failing": []})

if __name__ == "__main__":
    unittest.main()
