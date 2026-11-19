"""MG-01a: alias-addressed questions answered by the adapter-stamped canonical sender."""
import io
import contextlib
import json
import tempfile
import unittest
from pathlib import Path

from synthetics.cli import main
from synthetics.mcp import TOOLS
from synthetics.store import Store

CANONICAL = "employee-a22b6a2a8f524cd6b6800b0a620dbe06"


class QuestionRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.store = Store(self.home)
        self.addCleanup(self.store.close)
        self.store.initialize("Vesper", "Maintainer")
        self.work = self.store.work_put("Needs an answer")
        self.question = self.store.question_ask("codex-manager", "Approve?", work_id=self.work["id"], key="q1")

    def audit(self, action):
        return [a for a in self.store.audit_list(limit=100) if a["action"] == action]

    def test_alias_question_answered_by_canonical_sender_after_routing(self):
        routed = self.store.question_routed(self.question["id"], CANONICAL, "fieldpost")
        self.assertEqual(routed["to_id"], CANONICAL)
        self.assertEqual(routed["to"], "codex-manager")
        event = self.store.feed(CANONICAL, "Approved", reply_to=self.question["id"], source="fieldpost")
        answered = self.store.question_list()[0]
        self.assertEqual(answered["status"], "answered")
        self.assertEqual(answered["answer_event_id"], event["id"])
        self.assertEqual(self.store.work_get(self.work["id"])["status"], "ready")

    def test_alias_sender_still_accepted(self):
        self.store.question_routed(self.question["id"], CANONICAL, "fieldpost")
        self.store.feed("codex-manager", "Approved", reply_to=self.question["id"])
        self.assertEqual(self.store.question_list()[0]["status"], "answered")

    def test_canonical_sender_rejected_without_routing(self):
        with self.assertRaises(ValueError):
            self.store.feed(CANONICAL, "Approved", reply_to=self.question["id"], source="fieldpost")

    def test_wrong_sender_still_rejected(self):
        self.store.question_routed(self.question["id"], CANONICAL, "fieldpost")
        for sender in ("rowan", "employee-28ed6d90e0fe4b42b64bae0e41e82cba", "david"):
            with self.assertRaises(ValueError):
                self.store.feed(sender, "I approve", reply_to=self.question["id"])
        self.assertEqual(self.store.question_list()[0]["status"], "open")

    def test_answered_question_cannot_be_answered_again(self):
        self.store.question_routed(self.question["id"], CANONICAL, "fieldpost")
        first = self.store.feed(CANONICAL, "Approved", reply_to=self.question["id"], external_id="m1", source="fieldpost")
        with self.assertRaises(ValueError):
            self.store.feed(CANONICAL, "Changed my mind", reply_to=self.question["id"], external_id="m2",
                            source="fieldpost")
        self.assertEqual(self.store.question_list()[0]["answer_event_id"], first["id"])

    def test_restamp_with_different_id_rejected_and_same_id_idempotent(self):
        self.store.question_routed(self.question["id"], CANONICAL, "fieldpost")
        again = self.store.question_routed(self.question["id"], CANONICAL, "fieldpost-backfill")
        self.assertEqual(again["routed_by"], "fieldpost")
        with self.assertRaises(ValueError):
            self.store.question_routed(self.question["id"], "employee-someone-else", "fieldpost")
        self.assertEqual(self.store.question_list()[0]["to_id"], CANONICAL)
        self.assertEqual(len(self.audit("question.routed")), 1)

    def test_unknown_question_rejected(self):
        with self.assertRaises(ValueError):
            self.store.question_routed("question-missing", CANONICAL, "fieldpost")

    def test_not_exposed_through_mcp(self):
        names = {tool["name"] for tool in TOOLS}
        # question_routed is adapter-only; "routed" (not "rout") so RTN's routine_rearm doesn't trip it.
        self.assertFalse({n for n in names if "routed" in n})
        for tool in TOOLS:
            self.assertNotIn("to_id", json.dumps(tool.get("inputSchema", {})))

    def test_cli_records_route(self):
        self.store.close()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--home", str(self.home), "question-routed", self.question["id"],
                         "--to-id", CANONICAL, "--by", "operator"])
        self.assertEqual(code or 0, 0)
        self.assertEqual(json.loads(out.getvalue())["to_id"], CANONICAL)
        self.store = Store(self.home)
        self.addCleanup(self.store.close)
        self.assertEqual(self.store.question_list()[0]["routed_by"], "operator")


if __name__ == "__main__":
    unittest.main()
