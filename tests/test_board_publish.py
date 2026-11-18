"""M27 (iv) Board: work -> backlog projection (design test 4, C7 part, C8)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from synthetics import board
from synthetics.projects import ProjectStore
from synthetics.store import Store


class PublishTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "vesper")
        with self.ps() as ps:
            ps.grant("vesper", "post", by="operator")
            ps.grant("rowan", "read", by="operator")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def ps(self, project="synthetics"):
        return ProjectStore(self.root / "projects" / project / "memory.sqlite3", project)

    def rows(self, project="synthetics"):
        with self.ps(project) as ps:
            return [e for e in ps.entries() if e["kind"] == "backlog"]

    def test_4_publish_once_unchanged_none_crash_replay_unpublish(self):
        w = self.store.work_put("Fix X", description="SECRET-DESC", note="SECRET-NOTE", publish="synthetics",
                                origin="self", priority=3)
        self.assertTrue(board.publish_pending(self.store))
        self.assertEqual(board.publish_drain(self.store)["appended"], 1)
        self.assertFalse(board.publish_pending(self.store))
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["author"], "vesper")
        self.assertEqual(set(rows[0]["body"]), {"work_id", "title", "origin", "status", "waiting_for", "evidence",
                                                 "priority"})
        # Note-only change: projection unchanged -> no new entry.
        self.store.work_put(id=w["id"], note="more SECRET-NOTE")
        board.publish_drain(self.store)
        self.assertEqual(len(self.rows()), 1)
        # Crash between append and storing the generation: the same key replays, no duplicate.
        self.store.work_put(id=w["id"], status="waiting", waiting_for="Codex review")
        with mock.patch.object(board, "_record", side_effect=[RuntimeError("crash")]):
            with self.assertRaises(RuntimeError):
                board.publish_drain(self.store)
        self.assertEqual(len(self.rows()), 2)
        board.publish_drain(self.store)
        self.assertEqual(len(self.rows()), 2)
        # A -> B -> A gets a fresh generation, so the board ends in the latest state.
        self.store.work_put(id=w["id"], status="ready")
        board.publish_drain(self.store)
        self.store.work_put(id=w["id"], status="waiting", waiting_for="Codex review")
        board.publish_drain(self.store)
        with self.ps() as ps:
            row = ps.state()["backlog"]["vesper|" + w["id"]]
        self.assertEqual((row["status"], row["waiting_for"]), ("waiting", "Codex review"))
        # Unpublish drops it from the board view; history stays.
        self.store.work_put(id=w["id"], publish="")
        board.publish_drain(self.store)
        with self.ps() as ps:
            self.assertNotIn("vesper|" + w["id"], ps.state()["backlog"])
        self.assertEqual(self.rows()[-1]["body"]["status"], "unpublished")
        dump = json.dumps([e for e in self.rows()])
        self.assertNotIn("SECRET", dump)

    def test_move_between_projects_unpublishes_old(self):
        with self.ps("other") as ps:
            ps.grant("vesper", "write", by="operator")
        w = self.store.work_put("Y", publish="synthetics")
        board.publish_drain(self.store)
        self.store.work_put(id=w["id"], publish="other")
        board.publish_drain(self.store)
        with self.ps() as ps:
            self.assertEqual(ps.state()["backlog"], {})
        with self.ps("other") as ps:
            self.assertIn("vesper|" + w["id"], ps.state()["backlog"])

    def test_c8_publish_failing_problem_and_retry(self):
        me = self.store.initialize("Vesper", "maintainer")["id"]
        with self.ps("readonly") as ps:
            ps.grant(me, "read", by="operator")
        w = self.store.work_put("Z", publish="readonly")
        out = board.publish_drain(self.store)
        self.assertEqual(out["failed"], 1)
        self.assertTrue(board.publish_pending(self.store))
        problems = self.store.context()["publish_failing"]
        self.assertEqual(problems[0]["work_id"], w["id"])
        self.assertIn("no_grant", problems[0]["error"])
        # Backoff defers the retry; unknown projects fail visibly too.
        self.assertEqual(board.publish_drain(self.store, backoff=60)["deferred"], 1)
        self.store.work_put(id=w["id"], publish="nosuch")
        board.publish_drain(self.store)
        self.assertIn("unknown_project", self.store.context()["publish_failing"][0]["error"])
        # Fixed: unpublishing clears the problem.
        self.store.work_put(id=w["id"], publish="")
        board.publish_drain(self.store)
        self.assertEqual(self.store.context()["publish_failing"], [])

    def test_publish_validation(self):
        for bad in ("Bad Name", "company"):
            with self.assertRaises(ValueError):
                self.store.work_put("x", publish=bad)
        with self.assertRaises(ValueError):
            self.store.work_put("x", origin="other")
        self.store.work_put("plain")
        self.assertFalse(board.publish_pending(self.store))


if __name__ == "__main__":
    unittest.main()
