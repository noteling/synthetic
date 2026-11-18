"""Backlog 24: unique short ID prefixes (>= 8 hex) of own-home IDs are expanded at the tool boundary (K1-K6)."""
import tempfile
import unittest
from pathlib import Path

from synthetics.mcp import Server
from synthetics.store import Store

CODEX = "employee-a22b6a2a8f524cd6b6800b0a620dbe06"


def short(record_id, n=8):
    kind, hexpart = record_id.split("-", 1)
    return "%s-%s" % (kind, hexpart[:n])


class ShortIdTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name))
        self.addCleanup(self.store.close)
        self.store.initialize("Vesper", "Maintainer")
        self.work = self.store.work_put("Needs an answer")
        self.question = self.store.question_ask("codex", "Approve?", work_id=self.work["id"], key="q1")

    def twin(self, table, record):
        """A second row sharing the first 31 hex chars, so every shorter prefix is ambiguous."""
        last = record["id"][-1]
        twin = dict(record, id=record["id"][:-1] + ("0" if last != "0" else "1"))
        self.store._save(table, twin, "test.twin")
        return twin

    def test_k1_waiting_for_token_is_rewritten_to_full_id(self):
        out = self.store.work_put(id=short(self.work["id"]), status="waiting",
                                  waiting_for="Codex's answer to %s" % short(self.question["id"]))
        self.assertEqual(out["id"], self.work["id"])
        self.assertEqual(out["waiting_for"], "Codex's answer to %s" % self.question["id"])
        self.assertEqual(out["waiting_question_ids"], [self.question["id"]])
        self.assertEqual(out["waiting_question_id"], self.question["id"])

    def test_k2_checks_run_on_resolved_ids(self):
        other = self.store.work_put("Other work")
        # Docket P1a: another work may share the open question; the short prefixes resolve first.
        shared = self.store.work_put(id=short(other["id"]), status="waiting",
                                     waiting_for="see %s" % short(self.question["id"]))
        self.assertEqual((shared["id"], shared["waiting_question_ids"]), (other["id"], [self.question["id"]]))
        event = self.store.feed("codex", "Yes, approved.", external_id="a1")
        resolved = self.store.question_resolve(short(self.question["id"]), short(event["id"], 10))
        self.assertEqual(resolved["answer_event_id"], event["id"])
        with self.assertRaisesRegex(ValueError, "already been answered"):
            self.store.work_put(id=self.work["id"], status="waiting",
                                waiting_for="see %s" % short(self.question["id"]))
        again = self.store.question_resolve(short(self.question["id"]), short(event["id"]))
        self.assertTrue(again.get("already"))
        q2 = self.store.question_ask("rowan", "Still needed?", work_id=self.work["id"], key="q2")
        closed = self.store.question_close(short(q2["id"]), "obsolete")
        self.assertEqual(closed["status"], "withdrawn")

    def test_k3_short_and_full_forms_replay_as_one_operation(self):
        first = self.store.work_put(id=short(self.work["id"]), note="step 1", key="n1",
                                    evidence=[{"level": "attempted", "ref": short(self.question["id"])}])
        second = self.store.work_put(id=self.work["id"], note="step 1", key="n1",
                                     evidence=[{"level": "attempted", "ref": self.question["id"]}])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual([e["ref"] for e in second["evidence"]], [self.question["id"]])
        sent = self.store.message_send("codex", "fyi", work_id=short(self.work["id"]), key="m1")
        replay = self.store.message_send("codex", "fyi", work_id=self.work["id"], key="m1")
        self.assertEqual(sent["id"], replay["id"])
        self.assertEqual(replay["work_id"], self.work["id"])

    def test_k4_ambiguous_prefix_rejects_whole_call(self):
        self.twin("questions", self.question)
        before = self.store.work_get(self.work["id"])
        event = self.store.feed(CODEX, "note", external_id="e1")
        with self.assertRaisesRegex(ValueError, "ambiguous_id: %s matches" % short(self.question["id"])):
            self.store.work_put(id=self.work["id"], status="waiting", note="changed", key="amb",
                                waiting_for="see %s" % short(self.question["id"]),
                                evidence=[{"level": "observed", "ref": event["id"]}])
        after = self.store.work_get(self.work["id"])
        self.assertEqual(after["note"], before["note"])
        self.assertEqual(after.get("evidence"), before.get("evidence"))
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM idempotency WHERE key='amb'").fetchone())
        # The full ID still works; a longer unique prefix too.
        out = self.store.work_put(id=self.work["id"], status="waiting", waiting_for="see %s" % self.question["id"])
        self.assertEqual(out["waiting_question_ids"], [self.question["id"]])

    def test_k5_scope_own_table_only_and_recipient_work_id_untouched(self):
        with self.assertRaisesRegex(ValueError, "recipient_work_id must be a work ID"):
            self.store.message_send("codex", "re", key="m2", recipient_work_id=short(self.work["id"]))
        # A question- prefix is never looked up in works (or vice versa).
        with self.assertRaisesRegex(ValueError, "No works record question-"):
            self.store.work_get(short(self.question["id"]))
        # Fewer than 8 hex chars, or upper case, is not a prefix: the old not-found error stands.
        with self.assertRaisesRegex(ValueError, "No works record"):
            self.store.work_get(short(self.work["id"], 7))
        # Backlog 30 flake fix: an all-digit prefix is unchanged by upper(); use a work whose prefix has a letter.
        lettered = self.work
        while short(lettered["id"]).upper() == short(lettered["id"]).replace("work-", "WORK-"):
            lettered = self.store.work_put("Lettered prefix")
        with self.assertRaisesRegex(ValueError, "No works record"):
            self.store.work_get(short(lettered["id"]).upper().replace("WORK-", "work-"))
        event = self.store.feed(CODEX, "hello", external_id="e2")
        reply = self.store.message_send("codex", "re", key="m3", in_reply_to=short(event["id"]))
        self.assertEqual(reply["in_reply_to"], event["id"])
        self.assertEqual(self.store.event_get(short(event["id"]))["id"], event["id"])
        reminder = self.store.reminder_set("2099-01-01T00:00:00Z", "later", work_id=short(self.work["id"]), key="r1")
        self.assertEqual(reminder["work_id"], self.work["id"])
        self.store.entity_put("synthetics", "Synthetics", "project")
        memory = self.store.memory_put("synthetics", "p", "v1", source="test")
        newer = self.store.memory_put("synthetics", "p", "v2", source="test", replaces=short(memory["id"]))
        self.assertEqual(newer["replaces"], memory["id"])

    def test_k6_mcp_echoes_expansions_only_when_used(self):
        server = Server(self.store)
        out = server.call("work_get", {"id": short(self.work["id"])})
        self.assertEqual(out["resolved"], {short(self.work["id"]): self.work["id"]})
        out = server.call("work_get", {"id": self.work["id"]})
        self.assertNotIn("resolved", out)
        prompt = (Path(__file__).resolve().parents[1] / "synthetics" / "prompts" / "employee.md").read_text()
        self.assertIn("unique prefix of at least 8 hex characters", prompt)


if __name__ == "__main__":
    unittest.main()
