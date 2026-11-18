"""M27 (iv) Board: snapshot sections board/board_open and the new-since cursor (design test 5)."""
import tempfile
import unittest
from pathlib import Path

from synthetics import board
from synthetics.projects import ProjectStore
from synthetics.store import Store


def post(topic, **kw):
    return [{"kind": "post", "body": dict(topic=topic, body="body of " + topic, **kw)}]


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "vesper")
        self.me = self.store.initialize("Vesper", "maintainer")["id"]

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def ps(self):
        return ProjectStore(self.root / "projects" / "synthetics" / "memory.sqlite3", "synthetics")

    def test_5_absent_when_empty(self):
        self.assertNotIn("board", self.store.context())
        with self.ps() as ps:  # a project with only intents/sources still adds no bytes
            ps.grant(self.me, "write", by="operator")
        self.assertNotIn("board", self.store.context())

    def test_5_cursor_advances_only_on_finish(self):
        with self.ps() as ps:
            ps.grant(self.me, "post", by="operator")
            ps.grant("rowan", "post", by="operator")
            first = ps.append(post("hello", mentions=[self.me]), author="rowan")["ids"][0]
        self.store.work_put("w")
        run_a = self.store.run_begin()["id"]
        ctx = self.store.context(run_id=run_a)
        self.assertEqual([i["post"] for i in ctx["board"]], [first])
        self.assertTrue(ctx["board"][0]["mentions_me"])
        self.assertEqual([o["post"] for o in ctx["board_open"]], [first])
        self.store.run_fail(run_a, "x")
        board.advance(self.store, run_a, finished=False)  # failed run keeps the cursor
        run_b = self.store.run_begin()["id"]
        self.assertEqual(len(self.store.context(run_id=run_b)["board"]), 1)
        with self.ps() as ps:  # arrives mid-session: stays new after the finish
            late = ps.append(post("late"), author="rowan")["ids"][0]
        self.store.run_finish(run_b, "ok")
        board.advance(self.store, run_b, finished=True)
        ctx = self.store.context()
        self.assertEqual([i["post"] for i in ctx["board"]], [late])
        self.assertEqual([o["post"] for o in ctx["board_open"]], [first])  # still unanswered
        with self.ps() as ps:
            ps.append(post("re", reply_to=first), author=self.me)
        self.assertEqual(self.store.context()["board_open"], [])

    def test_5_cap_and_more(self):
        with self.ps() as ps:
            ps.grant("rowan", "post", by="operator")
            ps.grant(self.me, "read", by="operator")
            for i in range(20):
                ps.append(post("t%d" % i), author="rowan")
        ctx = self.store.context()
        self.assertEqual(len(ctx["board"]), board.SECTION_ITEMS)
        self.assertEqual(ctx["board_more"], 20 - board.SECTION_ITEMS)
        self.assertEqual(ctx["board"][0]["topic"], "t19")

    def test_unreadable_project_hidden(self):
        with self.ps() as ps:
            ps.grant("rowan", "post", by="operator")
            ps.append(post("private"), author="rowan")
        self.assertNotIn("board", self.store.context())


if __name__ == "__main__":
    unittest.main()
