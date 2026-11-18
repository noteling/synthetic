"""M27 (iv) Board: design test 8 + C7 — a published work leaks nothing private to the project or to readers."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from synthetics import board, projecttools
from synthetics.projects import ProjectStore
from synthetics.store import Store


class PrivacyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "vesper")
        self.me = self.store.initialize("Vesper", "maintainer")["id"]
        self.rowan = Store(self.root / "rowan")
        self.rid = self.rowan.initialize("Rowan", "maintainer")["id"]
        with self.ps() as ps:
            ps.grant(self.me, "post", by="operator")
            ps.grant(self.rid, "read", by="operator")

    def tearDown(self):
        self.store.close()
        self.rowan.close()
        self.tmp.cleanup()

    def ps(self):
        return ProjectStore(self.root / "projects" / "synthetics" / "memory.sqlite3", "synthetics")

    def test_8_note_description_question_ids_never_leave_home(self):
        w = self.store.work_put("Public title", description="SECRET-DESC", note="SECRET-NOTE",
                                publish="synthetics", origin="self")
        q = self.store.question_ask("david", "SECRET-QUESTION-BODY?", work_id=w["id"], key="q1")
        self.store.work_put(id=w["id"], status="waiting", waiting_for="David's answer")
        self.assertEqual(self.store.work_get(w["id"])["waiting_question_ids"], [q["id"]])
        board.publish_drain(self.store)
        db = sqlite3.connect(self.root / "projects" / "synthetics" / "memory.sqlite3")
        dump = "\n".join(db.iterdump())
        db.close()
        reader = json.dumps(self.rowan.context(), default=str)
        with self.ps() as ps:
            read = json.dumps(projecttools._board_read(ps, None, None))
        self.assertIn("Public title", dump)
        self.assertIn("Public title", reader)
        self.assertIn("Public title", read)
        for blob in (dump, reader, read):
            # Docket P1b (Codex Q2): the question ID may leave only as a blocked_by "<ID> <hop>" line.
            self.assertNotIn("SECRET-QUESTION-BODY", blob)
            blob = blob.replace("%s queued" % q["id"], "")
            for secret in ("SECRET-DESC", "SECRET-NOTE", "SECRET-QUESTION-BODY", q["id"],
                           "waiting_question_ids", "waiting_question_id", '"note"', '"description"'):
                self.assertNotIn(secret, blob)


if __name__ == "__main__":
    unittest.main()
