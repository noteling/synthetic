"""Backlog 30: workspace-relative file evidence refs (R1-R3, R5) and ceiling hints (R4)."""
import os
import tempfile
import unittest
from pathlib import Path

from synthetics.mcp import Server
from synthetics.store import Store


class EvidenceRefTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.store = Store(self.home)
        self.addCleanup(self.store.close)
        self.store.initialize("Vesper", "Maintainer")
        self.ws = self.store.workspace
        (self.ws / "reviews").mkdir()
        (self.ws / "reviews" / "test.log").write_text("665 OK\n")
        self.absolute = str((self.ws / "reviews" / "test.log").resolve())

    def done(self, work_id, ref, level="observed", key=None, store=None):
        return (store or self.store).work_put(id=work_id, status="done", key=key,
                                              evidence=[dict(level=level, ref=ref)])

    def test_relative_file_stored_as_absolute_and_echoed(self):
        work = self.store.work_put("Ship")
        out = Server(self.store).call("work_put", {"id": work["id"], "status": "done",
                                                   "evidence": [{"level": "observed", "ref": "reviews/test.log"}]})
        self.assertEqual(out["status"], "done")
        self.assertEqual(out["evidence"][0]["ref"], self.absolute)
        self.assertEqual(out["evidence"][0]["ref_kind"], "file")
        self.assertEqual(out["evidence"][0]["check"]["path"], self.absolute)
        self.assertEqual(out["resolved"], {"reviews/test.log": self.absolute})

    def test_resolves_against_workspace_not_cwd(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        Path(other.name, "reviews").mkdir()
        Path(other.name, "reviews", "elsewhere.log").write_text("x")
        cwd = os.getcwd()
        os.chdir(other.name)
        self.addCleanup(os.chdir, cwd)
        work = self.store.work_put("Ship")
        with self.assertRaisesRegex(ValueError, "Evidence ref must be"):
            self.done(work["id"], "reviews/elsewhere.log")
        self.assertEqual(self.done(work["id"], "reviews/test.log")["evidence"][0]["ref"], self.absolute)

    def test_r1_id_like_relative_file_takes_id_branch(self):
        (self.ws / "work-notes.md").write_text("notes")
        work = self.store.work_put("Ship")
        with self.assertRaises(ValueError) as caught:
            self.done(work["id"], "work-notes.md")
        self.assertNotIn("Evidence ref must be", str(caught.exception))
        self.assertEqual(self.store.work_get(work["id"])["status"], "ready")
        # The absolute path still works.
        out = self.done(work["id"], str((self.ws / "work-notes.md").resolve()))
        self.assertEqual(out["evidence"][0]["ref_kind"], "file")

    def test_r2_relative_and_absolute_replay_as_one_call(self):
        work = self.store.work_put("Ship")
        first = self.done(work["id"], "reviews/test.log", key="close")
        again = self.done(work["id"], self.absolute, key="close")
        self.assertEqual(first, again)
        self.assertEqual(len(self.store.work_get(work["id"])["evidence"]), 1)

    def test_parent_escape_allowed_but_runs_refused(self):
        outside = self.home / "notes.txt"
        outside.write_text("outside the workspace")
        work = self.store.work_put("Ship")
        out = self.done(work["id"], "../notes.txt")
        self.assertEqual(out["evidence"][0]["ref"], str(outside.resolve()))
        (self.home / "runs").mkdir(exist_ok=True)
        (self.home / "runs" / "run-x.mcp.jsonl").write_text("{}")
        other = self.store.work_put("Other")
        with self.assertRaisesRegex(ValueError, "Own session transcripts"):
            self.done(other["id"], "../runs/run-x.mcp.jsonl")
        # A symlink inside the workspace pointing into runs/ is refused after resolve().
        (self.ws / "link.jsonl").symlink_to(self.home / "runs" / "run-x.mcp.jsonl")
        with self.assertRaisesRegex(ValueError, "Own session transcripts"):
            self.done(other["id"], "link.jsonl")

    def test_r5_missing_relative_keeps_error_plus_hint(self):
        work = self.store.work_put("Ship")
        with self.assertRaisesRegex(ValueError, "Evidence ref must be") as caught:
            self.done(work["id"], "reviews/missing.log")
        self.assertIn("relative file path resolves against your workspace %s" % self.ws, str(caught.exception))
        self.assertEqual(self.store.work_get(work["id"])["status"], "ready")

    def test_r4_ceiling_hints_for_every_kind(self):
        work = self.store.work_put("Ship")
        memory = self.store.memory_put(self.store.identity()["id"], "fact", "x", source="file")
        cases = [
            ("observed", memory["id"], "cite the source it records"),
            ("verified", "reviews/test.log", "verified needs a colleague's reply or an authenticated answer"),
            ("observed", "receipt:telegram:1", "cite the delivered message- ID or a file"),
        ]
        for level, ref, hint in cases:
            with self.assertRaisesRegex(ValueError, "supports at most") as caught:
                self.done(work["id"], ref, level=level)
            self.assertIn(hint, str(caught.exception))
        message = self.store.message_send("david", "Shipped", key="m1")
        with self.assertRaises(ValueError) as caught:
            self.done(work["id"], message["id"])
        self.assertIn("only attempted", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
