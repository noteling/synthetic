"""VIS G3 delta (backlog 52): a message linked to the sender's own published work defaults to that project.

Rules (Codex, event-cfb6b679 / event-b1c27125): only the sender's own work_id; an explicit project wins;
project="" opts out; the tag is recorded with by="work"; unpublished work or a project the sender cannot
write leaves the message untagged without refusing it."""
import tempfile
import unittest
from pathlib import Path

from synthetics.projects import ProjectStore
from synthetics.store import Store


class WorkProjectDefaultTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "vesper")
        self.store.initialize("Vesper", "test maintainer")
        self.me = self.store.identity()["id"]
        with self.ps("synthetics") as ps:
            ps.grant(self.me, "write", by="op")
        with self.ps("noteling") as ps:
            ps.grant(self.me, "write", by="op")
        with self.ps("readonly") as ps:
            ps.grant(self.me, "read", by="op")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def ps(self, project):
        return ProjectStore(self.root / "projects" / project / "memory.sqlite3", project)

    def out_sources(self, project):
        with self.ps(project) as ps:
            return [e for e in ps.entries() if e["kind"] == "source"]

    def work(self, publish=None, key="w"):
        kwargs = {"publish": publish} if publish is not None else {}
        return self.store.work_put("Fix X", key=key, **kwargs)["id"]

    def test_published_work_tags_message_by_work_and_captures_out(self):
        w = self.work("synthetics")
        m = self.store.message_send("codex-manager", "about fix X", work_id=w, key="m1")
        self.assertEqual((m.get("project"), m.get("project_by")), ("synthetics", "work"))
        self.assertEqual(m["project_default"], {"work_id": w, "project": "synthetics", "applied": True})
        stored = self.store._get("messages", m["id"])
        self.assertEqual((stored.get("project"), stored.get("project_by")), ("synthetics", "work"))
        self.assertNotIn("project_default", stored)
        self.assertEqual([e["body"]["origin"]["event_id"] for e in self.out_sources("synthetics")], [m["id"]])
        q = self.store.question_ask("codex-manager", "which way?", work_id=w, key="q1")
        self.assertEqual((q.get("project"), q.get("project_by")), ("synthetics", "work"))
        self.assertEqual(self.store._get("questions", q["id"])["project"], "synthetics")

    def test_explicit_project_wins_and_empty_opts_out(self):
        w = self.work("synthetics")
        m = self.store.message_send("codex-manager", "about noteling", work_id=w, key="m1", project="noteling")
        self.assertEqual(m["project"], "noteling")
        self.assertNotIn("project_by", m)
        self.assertNotIn("project_default", m)
        o = self.store.message_send("codex-manager", "mixed topics", work_id=w, key="m2", project="")
        self.assertNotIn("project", o)
        self.assertNotIn("project_default", o)
        self.assertEqual(self.out_sources("synthetics"), [])
        self.assertEqual([e["body"]["origin"]["event_id"] for e in self.out_sources("noteling")], [m["id"]])

    def test_unpublished_or_unwritable_or_missing_project_leaves_untagged(self):
        plain = self.store.message_send("codex-manager", "x", work_id=self.work(key="w0"), key="m0")
        self.assertNotIn("project", plain)
        self.assertNotIn("project_default", plain)
        w = self.work("readonly", key="w1")
        m = self.store.message_send("codex-manager", "y", work_id=w, key="m1")
        self.assertNotIn("project", m)
        self.assertEqual(m["project_default"],
                         {"work_id": w, "project": "readonly", "applied": False, "reason": "no-write-grant"})
        w2 = self.work("stand", key="w2")
        n = self.store.message_send("codex-manager", "z", work_id=w2, key="m2")
        self.assertNotIn("project", n)
        self.assertEqual(n["project_default"]["reason"], "unknown-project")
        self.assertFalse((self.root / "projects" / "stand").exists())
        # recipient_work_id is the recipient's work, never a source for the default.
        r = self.store.message_send("codex-manager", "r", key="m3", recipient_work_id="work-" + "0" * 32)
        self.assertNotIn("project", r)
        self.assertEqual(self.out_sources("readonly"), [])

    def test_recipient_records_by_work_and_checks_grant(self):
        rowan = Store(self.root / "rowan")
        rowan.initialize("Rowan", "test")
        try:
            ok = rowan.feed(self.me, "about fix X", source="fieldpost", external_id="e1",
                            project="synthetics", project_by="work")
            self.assertEqual(ok["project_ref"], {"id": "synthetics", "valid": True, "by": "work", "effect": "capture"})
            no = rowan.feed("employee-stranger", "spoof", source="fieldpost", external_id="e2",
                            project="synthetics", project_by="work")
            self.assertEqual((no["project_ref"]["effect"], no["project_ref"]["reason"]), ("none", "sender-not-granted"))
        finally:
            rowan.close()


if __name__ == "__main__":
    unittest.main()
