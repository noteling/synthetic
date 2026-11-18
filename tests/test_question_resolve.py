"""MG-01c: the asker resolves its own open question with an uncorrelated reply."""
import tempfile
import unittest
from pathlib import Path

from synthetics.mcp import METHODS, MUTATIONS
from synthetics.store import Store

CANONICAL = "employee-a22b6a2a8f524cd6b6800b0a620dbe06"


class QuestionResolveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.store = Store(self.home)
        self.addCleanup(self.store.close)
        self.store.initialize("Vesper", "Maintainer")
        self.work = self.store.work_put("Needs an answer")
        self.question = self.store.question_ask("codex-manager", "Approve?", work_id=self.work["id"], key="q1")

    def question_now(self):
        return next(q for q in self.store.question_list() if q["id"] == self.question["id"])

    def test_alias_sender_resolves_and_wakes_work(self):
        event = self.store.feed("codex-manager", "Approved.", external_id="m1")
        self.assertEqual(self.question_now()["status"], "open")
        self.store.question_resolve(self.question["id"], event["id"])
        q = self.question_now()
        self.assertEqual((q["status"], q["answer_event_id"]), ("answered", event["id"]))
        self.assertEqual(self.store.work_get(self.work["id"])["status"], "ready")
        actions = [a["action"] for a in self.store.audit_list(limit=100)]
        self.assertIn("question.resolved", actions)

    def test_canonical_sender_needs_routing(self):
        event = self.store.feed(CANONICAL, "Approved.", source="fieldpost", external_id="m1")
        with self.assertRaises(ValueError):
            self.store.question_resolve(self.question["id"], event["id"])
        self.store.question_routed(self.question["id"], CANONICAL, "fieldpost")
        self.store.question_resolve(self.question["id"], event["id"])
        self.assertEqual(self.question_now()["status"], "answered")

    def test_wrong_sender_rejected(self):
        for n, sender in enumerate(("rowan", "david", "source-watcher")):
            event = self.store.feed(sender, "I approve", external_id="x%d" % n)
            with self.assertRaises(ValueError):
                self.store.question_resolve(self.question["id"], event["id"])
        self.assertEqual(self.question_now()["status"], "open")

    def test_already_answered_rejected(self):
        first = self.store.feed("codex-manager", "Approved.", external_id="m1")
        second = self.store.feed("codex-manager", "Actually no.", external_id="m2")
        self.store.question_resolve(self.question["id"], first["id"])
        with self.assertRaises(ValueError):
            self.store.question_resolve(self.question["id"], second["id"])
        self.assertEqual(self.question_now()["answer_event_id"], first["id"])

    def test_message_before_question_rejected(self):
        early = self.store.feed("codex-manager", "Earlier note", external_id="m0")
        later_q = self.store.question_ask("codex-manager", "Second?", key="q2")
        with self.assertRaises(ValueError):
            self.store.question_resolve(later_q["id"], early["id"])

    def test_non_message_event_rejected(self):
        event = self.store.feed("codex-manager", "ping", kind="reminder", external_id="r1")
        with self.assertRaises(ValueError):
            self.store.question_resolve(self.question["id"], event["id"])

    def test_unknown_ids_rejected(self):
        event = self.store.feed("codex-manager", "Approved.", external_id="m1")
        with self.assertRaises(Exception):
            self.store.question_resolve("question-missing", event["id"])
        with self.assertRaises(Exception):
            self.store.question_resolve(self.question["id"], "event-missing")

    def test_exposed_through_mcp_but_routing_is_not(self):
        self.assertIn("question_resolve", METHODS)
        self.assertIn("question_resolve", MUTATIONS)
        self.assertNotIn("question_routed", METHODS)
        schema = METHODS["question_resolve"]["inputSchema"]
        self.assertEqual(sorted(schema["required"]), ["event_id", "question_id"])


if __name__ == "__main__":
    unittest.main()
