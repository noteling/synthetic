"""M27 (iii): parallel question waits, proxy close / withdraw, idempotent resolve."""
import json
import tempfile
import unittest
from pathlib import Path

from synthetics.mcp import METHODS, MUTATIONS
from synthetics.store import Store

CODEX = "employee-a22b6a2a8f524cd6b6800b0a620dbe06"


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name))
        self.addCleanup(self.store.close)
        self.store.initialize("Vesper", "Maintainer")
        self.key = "k" * 64
        self.store.channel_register("fieldpost", Store.credential_hash(self.key), ["fieldpost"], "test")
        self.work = self.store.work_put("Needs answers")

    def ask(self, to, key):
        return self.store.question_ask(to, "Approve?", work_id=self.work["id"], key=key)

    def q(self, question_id):
        return next(q for q in self.store.question_list() if q["id"] == question_id)

    def w(self):
        return self.store.work_get(self.work["id"])


class QuestionSetTests(_Base):
    def test_parallel_asks_wake_on_any_answer_and_record_remaining(self):
        qd = self.ask("david", "qd")
        qr = self.ask("rowan", "qr")
        self.assertEqual(self.w()["waiting_question_ids"], [qd["id"], qr["id"]])
        self.assertEqual(self.w()["waiting_question_id"], qr["id"])
        before = self.w()["waiting_for"]
        event = self.store.feed("david", "Yes.", reply_to=qd["id"], external_id="a1")
        work = self.w()
        self.assertEqual((work["status"], work["waiting_question_ids"]), ("ready", []))
        woke = work["woken_by_answer"]
        self.assertEqual((woke["question"], woke["event"], woke["remaining_open"], woke["prior_waiting_for"]),
                         (qd["id"], event["id"], [qr["id"]], before))
        self.assertIn("prior_wake_at", woke)
        self.assertEqual(self.q(qr["id"])["status"], "open")
        # The session can re-wait cheaply on what remains.
        self.store.work_put(id=self.work["id"], status="waiting", waiting_for=qr["id"])
        self.assertEqual(self.w()["waiting_question_ids"], [qr["id"]])

    def test_reask_same_recipient_supersedes_only_that_link(self):
        q1 = self.ask("david", "q1")
        qr = self.ask("rowan", "qr")
        q2 = self.ask("david", "q2")
        self.assertEqual(self.w()["waiting_question_ids"], [qr["id"], q2["id"]])
        self.assertEqual(self.q(q1["id"])["status"], "open")  # superseded link, question stays open
        self.store.feed("david", "Old answer", reply_to=q1["id"], external_id="a1")
        self.assertEqual(self.w()["status"], "waiting")

    def test_canonical_recipient_compares_to_id(self):
        # P3: a question routed to codex-desktop and a new one to david are different recipients.
        qd = self.ask("codex-desktop", "q1")
        self.store.question_routed(qd["id"], "codex-desktop", "channel:test")
        q2 = self.ask("david", "q2")
        self.assertEqual(self.w()["waiting_question_ids"], [qd["id"], q2["id"]])
        # An alias question routed to a canonical ID is the same recipient as a question to that ID.
        qa = self.ask("codex-manager", "q3")
        self.store.question_routed(qa["id"], CODEX, "channel:test")
        qb = self.ask(CODEX, "q4")
        self.assertEqual(self.w()["waiting_question_ids"], [qd["id"], q2["id"], qb["id"]])

    def test_work_ref_does_not_wake_work_with_any_linked_question(self):
        # P4
        self.ask("david", "qd")
        self.ask("rowan", "qr")
        event = self.store.feed(CODEX, "fyi", source="fieldpost", external_id="r1", credential=self.key,
                                recipient_work_id=self.work["id"])
        self.assertEqual(event["work_ref"]["effect"], "none")
        self.assertIn("waiting on", event["work_ref"]["reason"])
        self.assertEqual(self.w()["status"], "waiting")

    def test_legacy_single_link_folds_to_a_set(self):
        qd = self.ask("david", "qd")
        row = self.w()
        for field in ("waiting_question_ids",):
            row.pop(field)
        self.store.db.execute("UPDATE works SET data=? WHERE id=?", (json.dumps(row), row["id"]))
        self.assertEqual(self.w()["waiting_question_ids"], [qd["id"]])
        self.store.feed("david", "Yes", reply_to=qd["id"], external_id="a1")
        self.assertEqual(self.w()["status"], "ready")


class QuestionCloseTests(_Base):
    def test_proxy_close_wakes_and_keeps_auth_as_received(self):
        qd = self.ask("david", "qd")
        event = self.store.feed(CODEX, "David said yes", source="fieldpost", external_id="p1", credential=self.key)
        closed = self.store.question_close(qd["id"], "Codex relayed David's answer", event_id=event["id"])
        self.assertEqual((closed["status"], closed["answered_by"], closed["work_unlinked"]),
                         ("answered_by_proxy", CODEX, []))
        self.assertTrue(closed["secondhand"])
        self.assertFalse(closed["unauthenticated"])
        self.assertEqual(closed["answer_auth"]["channel"], event["auth"]["channel"])
        self.assertEqual(self.w()["status"], "ready")
        self.assertEqual(self.w()["woken_by_answer"]["event"], event["id"])
        actions = [a["action"] for a in self.store.audit_list(limit=100)]
        self.assertIn("question.closed", actions)
        # Never verified evidence (Q3).
        with self.assertRaisesRegex(ValueError, "at most observed"):
            self.store.work_put(id=self.work["id"], status="done", evidence=[{"level": "verified", "ref": qd["id"]}])
        self.store.work_put(id=self.work["id"], status="done", evidence=[{"level": "observed", "ref": qd["id"]}])

    def test_unauthenticated_proxy_is_recorded_as_such(self):
        qd = self.ask("david", "qd")
        event = self.store.feed("rowan", "David told me yes", external_id="p1")
        closed = self.store.question_close(qd["id"], "Rowan passed it on", event_id=event["id"])
        self.assertEqual((closed["answer_auth"], closed["unauthenticated"], closed["secondhand"]), (None, True, True))

    def test_proxy_constraints(self):
        # P5
        early = self.store.feed("rowan", "before", external_id="e0")
        qd = self.ask("david", "qd")
        cases = [early["id"],
                 self.store.feed("david", "from the recipient", external_id="e1")["id"],
                 self.store.feed(self.store.identity()["id"], "self", external_id="e2")["id"]]
        reminder = self.store.reminder_set("2020-01-01T00:00:00Z", "tick", key="r")
        self.store.due_events()
        cases.append(next(e["id"] for e in self.store.event_pending() if e["kind"] == "reminder"))
        for event_id in cases:
            with self.assertRaises(ValueError):
                self.store.question_close(qd["id"], "x", event_id=event_id)
        with self.assertRaises(ValueError):
            self.store.question_close(qd["id"], "  ")
        self.assertEqual(self.q(qd["id"])["status"], "open")
        self.assertIsNotNone(reminder)

    def test_closed_questions_refused(self):
        qd = self.ask("david", "qd")
        self.store.question_close(qd["id"], "obsolete")
        with self.assertRaises(ValueError):
            self.store.question_close(qd["id"], "again")
        event = self.store.feed("rowan", "late", external_id="p1")
        with self.assertRaises(ValueError):
            self.store.question_close(qd["id"], "x", event_id=event["id"])

    def test_non_asker_refused(self):
        qd = self.ask("david", "qd")
        message = self.store._get("messages", qd["message_id"])
        message["sender"] = "someone-else"
        self.store.db.execute("UPDATE messages SET data=? WHERE id=?", (json.dumps(message), message["id"]))
        with self.assertRaises(ValueError):
            self.store.question_close(qd["id"], "not mine")

    def test_withdraw_keeps_status_and_reports_unlinked(self):
        # P6
        qd = self.ask("david", "qd")
        qr = self.ask("rowan", "qr")
        first = self.store.question_close(qd["id"], "superseded")
        self.assertEqual((first["status"], first["work_unlinked"]), ("withdrawn", []))
        self.assertEqual((self.w()["status"], self.w()["waiting_question_ids"]), ("waiting", [qr["id"]]))
        second = self.store.question_close(qr["id"], "no longer needed")
        self.assertEqual(second["work_unlinked"], [self.work["id"]])
        work = self.w()
        self.assertEqual((work["status"], work["waiting_question_ids"], work["waiting_question_id"]),
                         ("waiting", [], None))
        self.assertNotIn("woken_by_answer", work)
        # The label no longer names withdrawn questions, so the session can set a real blocker.
        self.store.work_put(id=self.work["id"], status="waiting", waiting_for=work["waiting_for"] + "; operator",
                            wake_at="2099-01-01T00:00:00Z")

    def test_late_correlated_reply_to_closed_question_is_not_correlated(self):
        # P7 (store side; the relay delivers it uncorrelated, see the Fieldpost test)
        qd = self.ask("david", "qd")
        self.store.question_close(qd["id"], "obsolete")
        with self.assertRaises(ValueError):
            self.store.feed("david", "late", reply_to=qd["id"], external_id="l1")
        plain = self.store.feed("david", "late", external_id="l2")
        self.assertIsNone(plain["reply_to"])
        self.assertEqual(self.q(qd["id"])["status"], "withdrawn")

    def test_resolve_is_idempotent_for_the_same_answer(self):
        qd = self.ask("david", "qd")
        event = self.store.feed("david", "Yes", external_id="a1")
        self.store.question_resolve(qd["id"], event["id"])
        again = self.store.question_resolve(qd["id"], event["id"])
        self.assertTrue(again["already"])
        other = self.store.feed("david", "No", external_id="a2")
        with self.assertRaisesRegex(ValueError, event["id"]):
            self.store.question_resolve(qd["id"], other["id"])

    def test_mcp_offers_question_close(self):
        self.assertIn("question_close", METHODS)
        self.assertIn("question_close", MUTATIONS)
        schema = METHODS["question_close"]["inputSchema"]
        self.assertEqual(sorted(schema["required"]), ["question_id", "reason"])
        prompt = (Path(__file__).resolve().parent.parent / "synthetics/prompts/employee.md").read_text()
        self.assertIn("question_close", prompt)


if __name__ == "__main__":
    unittest.main()
