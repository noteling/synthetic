"""M27 (iv) Board: post/backlog entry kinds, fold, post grant mode (design tests 1-2, C5, C6)."""
import tempfile
import unittest
from pathlib import Path

from synthetics.projectlog import fold
from synthetics.projects import ProjectError, ProjectStore


def post(topic="t", body="b", **kw):
    return {"kind": "post", "body": dict(topic=topic, body=body, **kw)}


def backlog(work_id="work-1", status="ready", **kw):
    return {"kind": "backlog", "body": dict(work_id=work_id, title="T", status=status, **kw)}


class BoardLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProjectStore(Path(self.tmp.name) / "p" / "memory.sqlite3", "p")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_1_fold_posts_replies_backlog_refold(self):
        a = self.store.append([post("root", "x " * 200, mentions=["rowan", "rowan", "vesper"])], author="vesper")
        root = a["ids"][0]
        b = self.store.append([post("re", "y", reply_to=root)], author="rowan")["ids"][0]
        c = self.store.append([post("re2", "z", reply_to=b)], author="vesper")["ids"][0]
        st = self.store.state()
        self.assertEqual(st["posts"][root]["mentions"], ["rowan"])  # deduped, self dropped
        self.assertTrue(st["posts"][root]["gist"].endswith("…"))
        self.assertEqual(st["posts"][c]["root"], root)
        self.assertEqual(st["posts"][root]["replies"], [b])
        self.store.append([backlog(status="waiting", origin="self", waiting_for="Codex")], author="vesper")
        self.store.append([backlog(status="done", evidence=["event-1"])], author="vesper")
        row = self.store.state()["backlog"]["vesper|work-1"]
        self.assertEqual((row["status"], row["owner"], row["evidence"]), ("done", "vesper", ["event-1"]))
        self.store.append([backlog(status="unpublished")], author="vesper")
        self.assertNotIn("vesper|work-1", self.store.state()["backlog"])
        self.assertEqual(fold(self.store.entries()), self.store.materialised())
        with self.assertRaises(ProjectError) as cm:
            self.store.append([post("re", "y", reply_to="pm-nope")], author="rowan")
        self.assertEqual(cm.exception.code, "unknown_target")

    def test_c5_c6_retract_own_post_only_and_backlog_projection_only(self):
        pid = self.store.append([post()], author="vesper")["ids"][0]
        with self.assertRaises(ProjectError) as cm:
            self.store.append([{"kind": "retract", "body": {"target": pid}}], author="rowan")
        self.assertEqual(cm.exception.code, "not_author")
        self.store.append([{"kind": "retract", "body": {"target": pid}}], author="vesper")
        self.assertNotIn(pid, self.store.state()["posts"])
        bid = self.store.append([backlog()], author="vesper")["ids"][0]
        with self.assertRaises(ProjectError) as cm:
            self.store.append([{"kind": "retract", "body": {"target": bid}}], author="vesper")
        self.assertEqual(cm.exception.code, "projection_only")
        for bad in (backlog(status="open"), backlog(work_id="x"), backlog(origin="other"),
                    backlog(priority="1"), post(topic=""), post(mentions=["a"] * 9)):
            with self.assertRaises(ProjectError):
                self.store.append([bad], author="vesper")

    def test_2_post_grant_mode(self):
        self.store.grant("rowan", "post", by="op")
        self.store.grant("reader", "read", by="op")
        self.store.grant("vesper", "write", by="op")
        src = self.store.append([{"kind": "source", "bytes": "q", "body": {"origin": {"role": "other"}}}],
                               author="vesper")["ids"][0]
        pid = self.store.append([post()], author="rowan")["ids"][0]
        self.store.append([backlog(), {"kind": "considered", "body": {"source": src}}], author="rowan")
        self.store.append([{"kind": "retract", "body": {"target": pid}}], author="rowan")
        lesson = {"kind": "lesson", "body": {"statement": "s", "applies_when": "a", "uncertainty": "u"}}
        for batch in ([lesson], [post(), lesson], [{"kind": "retract", "body": {"target": src}}]):
            with self.assertRaises(ProjectError) as cm:
                self.store.append(batch, author="rowan")
            self.assertEqual((cm.exception.code, cm.exception.detail.get("need")), ("no_grant", "write"))
        with self.assertRaises(ProjectError) as cm:
            self.store.append([post()], author="reader")
        self.assertEqual((cm.exception.code, cm.exception.detail.get("need")), ("no_grant", "post"))
        self.store.append([lesson, post()], author="vesper")
        with self.assertRaises(ProjectError) as cm:
            self.store.grant("x", "admin", by="op")
        self.assertEqual(cm.exception.code, "bad_mode")


if __name__ == "__main__":
    unittest.main()
