"""Docket P1a: one question blocking several works (shared blocker), work_ids on ask, docket view (C1, C3, C5)."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from synthetics import capture, cli
from synthetics.mcp import METHODS, MUTATIONS, Server
from synthetics.roster import CompanyStore
from synthetics.store import Store


def person(pid, name, kind="human"):
    return {"kind": "person", "body": {"id": pid, "kind": kind, "name": name, "role": "sponsor",
                                       "source": "operator statement", "certainty": "stated"}}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "manager"
        self.store = Store(self.home)
        self.addCleanup(self.store.close)
        self.store.initialize("Codex", "manager")
        self.key = "k" * 64
        self.store.channel_register("fieldpost", Store.credential_hash(self.key), ["fieldpost"], "test",
                                    routes=["rowan"])
        self.store.channel_register("codex-desktop", Store.credential_hash("d" * 64), ["codex-desktop"], "test",
                                    routes=["david"])

    def wget(self, work):
        return self.store.work_get(work["id"])


class SharedBlockerTests(_Base):
    def setUp(self):
        super().setUp()
        self.a, self.b, self.c = (self.store.work_put(t) for t in ("R03", "R04", "KT"))

    def test_work_put_links_open_question_of_another_work(self):
        q = self.store.question_ask("david", "Writable checkout for Rowan?", work_id=self.a["id"], key="q")
        out = self.store.work_put(id=self.b["id"], status="waiting", waiting_for="checkout %s" % q["id"])
        self.assertEqual(out["waiting_question_ids"], [q["id"]])
        self.assertEqual(self.store.question_list()[0]["work_id"], self.a["id"])  # origin kept
        # C3: each linked work lists the other works sharing the question.
        self.assertEqual(self.wget(self.a)["shared_with"], {q["id"]: [self.b["id"]]})
        self.assertEqual(self.wget(self.b)["shared_with"], {q["id"]: [self.a["id"]]})
        self.assertNotIn("shared_with", self.wget(self.c))

    def test_answered_question_still_refused(self):
        q = self.store.question_ask("david", "Checkout?", work_id=self.a["id"], key="q")
        self.store.feed("david", "Yes.", reply_to=q["id"], external_id="a1")
        with self.assertRaisesRegex(ValueError, "already been answered"):
            self.store.work_put(id=self.b["id"], status="waiting", waiting_for=q["id"])

    def test_work_ids_on_ask_link_all_and_one_answer_wakes_all(self):
        own = self.store.question_ask("rowan", "R03 scope?", work_id=self.a["id"], key="own")
        q = self.store.question_ask("david", "Checkout?", work_ids=[self.a["id"], self.b["id"], self.c["id"]],
                                    key="q")
        self.assertEqual(q["work_id"], self.a["id"])  # work_id defaults to the first
        self.assertEqual(self.wget(self.a)["waiting_question_ids"], [own["id"], q["id"]])
        for work in (self.b, self.c):
            self.assertEqual(self.wget(work)["waiting_question_ids"], [q["id"]])
        self.assertEqual(self.wget(self.c)["shared_with"], {q["id"]: sorted([self.a["id"], self.b["id"]])})
        event = self.store.feed("david", "Yes.", reply_to=q["id"], external_id="a1")
        for work in (self.a, self.b, self.c):
            got = self.wget(work)
            self.assertEqual(got["status"], "ready")
            self.assertEqual(got["woken_by_answer"]["event"], event["id"])
        self.assertEqual(self.wget(self.a)["woken_by_answer"]["remaining_open"], [own["id"]])

    def test_work_ids_idempotent_and_validated(self):
        ids = [self.a["id"], self.b["id"]]
        first = self.store.question_ask("david", "Checkout?", work_ids=ids, key="q")
        again = self.store.question_ask("david", "Checkout?", work_ids=ids, key="q")
        self.assertEqual(first["id"], again["id"])
        with self.assertRaises(ValueError):
            self.store.question_ask("david", "Checkout?", work_ids=[self.a["id"]], key="q")  # different payload
        with self.assertRaisesRegex(ValueError, "non-empty list"):
            self.store.question_ask("david", "x", work_ids=[], key="e")
        with self.assertRaisesRegex(ValueError, "questions only"):
            self.store._send("david", "x", None, "m", False, work_ids=[self.a["id"]])
        self.store.work_put(id=self.c["id"], status="cancelled", cancel_reason="test")
        with self.assertRaisesRegex(ValueError, "finished work"):
            self.store.question_ask("david", "y", work_ids=[self.a["id"], self.c["id"]], key="f")
        self.assertNotIn("y", [q["body"] for q in self.store.question_list()])  # nothing written

    def test_withdraw_lists_every_unlinked_work(self):
        own = self.store.question_ask("rowan", "R03 scope?", work_id=self.a["id"], key="own")
        q = self.store.question_ask("david", "Checkout?", work_ids=[self.a["id"], self.b["id"]], key="q")
        out = self.store.question_close(q["id"], "decided elsewhere")
        self.assertEqual(sorted(out["unlinked_from"]), sorted([self.a["id"], self.b["id"]]))
        self.assertEqual(out["work_unlinked"], [self.b["id"]])  # a still waits on its own question
        self.assertEqual(self.wget(self.a)["waiting_question_ids"], [own["id"]])
        self.assertNotIn("shared_with", self.wget(self.a))
        proxy = self.store.question_close(own["id"], "x", event_id=self.store.feed(
            "codex-desktop", "Rowan said fine", external_id="p1")["id"])
        self.assertEqual(proxy["unlinked_from"], [])

    def test_view_field_never_persisted(self):
        q = self.store.question_ask("david", "Checkout?", work_ids=[self.a["id"], self.b["id"]], key="q")
        self.store.reminder_set("2000-01-01T00:00:00Z", "nudge", work_id=self.a["id"], key="r")
        self.store.due_events()
        for work in (self.a, self.b):
            self.assertNotIn("shared_with", self.store._get("works", work["id"]))
        self.assertEqual(self.wget(self.a)["status"], "ready")
        self.assertEqual(self.wget(self.b)["waiting_question_ids"], [q["id"]])

    def test_mcp_and_cli_surface(self):
        self.assertIn("work_ids", METHODS["question_ask"]["inputSchema"]["properties"])
        self.assertIn("docket", METHODS)
        self.assertNotIn("docket", MUTATIONS)
        server = Server(self.store)
        q = server.call("question_ask", {"to": "david", "body": "Checkout?", "key": "q",
                                         "work_ids": [self.a["id"], self.b["id"]]})
        self.assertEqual(self.wget(self.b)["waiting_question_ids"], [q["id"]])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--home", str(self.home), "send", "david", "KT too?", "--key", "c1", "--question",
                      "--work-id", self.c["id"], "--also-work", self.a["id"]])
        asked = json.loads(buf.getvalue())
        self.assertEqual(self.wget(self.a)["waiting_question_ids"][-1], asked["id"])
        self.assertEqual(self.wget(self.c)["waiting_question_ids"], [asked["id"]])


class DocketViewTests(_Base):
    def setUp(self):
        super().setUp()
        capture.write_humans(self.home, {"default_human": "david", "senders": ["david", "telegram:42"]})
        with CompanyStore.at_root(self.root) as co:
            co.append([person("david", "David"),
                       {"kind": "alias", "body": {"alias": "Dana Example", "id": "david", "source": "operator statement",
                                                  "certainty": "stated"}}], author="operator", operator=True)
        self.work = self.store.work_put("Rowan checkout")

    def age(self, question, minutes):
        """Backdate a question and its outbox row."""
        at = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        row = self.store._get("questions", question["id"])
        self.store._save("questions", dict(row, created_at=at), "test.age")
        message = self.store._get("messages", row["message_id"])
        self.store._save("messages", dict(message, created_at=at), "test.age")

    def test_default_person_aliases_ages_and_ack_marking(self):
        old = self.store.question_ask("Dana Example", "Writable checkout for Rowan?\nDetails follow.",
                                      work_id=self.work["id"], key="old")
        self.age(old, 3 * 1440)
        new = self.store.question_ask("david", "x" * 80, key="new")
        self.age(new, 90)
        self.store.question_ask("rowan", "Not for David", key="r")
        # Deliver `old` over a routed channel with an ack (fieldpost routes david for this test).
        self.store.channel_register("fieldpost", Store.credential_hash(self.key), ["fieldpost"], "test",
                                    routes=["rowan", "Dana Example"])
        message_id = self.store._get("questions", old["id"])["message_id"]
        self.store.channel_claim(self.key, message_id)
        self.store.channel_ack(self.key, message_id, "delivered")
        view = self.store.docket()
        self.assertEqual((view["v"], view["person"], view["count"]), (1, "david", 2))
        self.assertIn("Dana Example", view["addresses"])
        self.assertIn("telegram:42", view["addresses"])
        first, second = view["items"]
        self.assertEqual(first["question_id"], old["id"])  # oldest first
        self.assertEqual(first["summary"], "Writable checkout for Rowan?")
        self.assertEqual((first["age_from"], first["ack"], first["delivery"]), ("delivered", "delivered", "delivered"))
        self.assertGreaterEqual(first["asked_age_minutes"], 3 * 1440)
        self.assertLess(first["delivered_age_minutes"], 5)
        self.assertEqual(first["age_minutes"], first["delivered_age_minutes"])
        self.assertEqual([w["id"] for w in first["works"]], [self.work["id"]])
        # C1: no ack → age from the ask, marked with the route; never dropped.
        self.assertEqual(len(second["summary"]), 60)
        self.assertEqual((second["age_from"], second["delivered_at"], second["ack"]),
                         ("asked", None, "not acked (route codex-desktop)"))
        self.assertEqual(second["age_minutes"], second["asked_age_minutes"])
        self.assertTrue(second["stuck"])
        self.assertIn("not acked (route codex-desktop)", view["text"])
        self.assertIn("blocks: %s Rowan checkout" % self.work["id"], view["text"])
        self.assertTrue(view["text"].startswith("Waiting on david"))

    def test_stable_fields_and_no_foreign_or_view_data(self):
        self.store.question_ask("david", "Q?", key="q")
        view = self.store.docket()
        self.assertEqual(set(view), {"v", "person", "addresses", "home", "generated_at", "count", "items", "text",
                                     "relay_owed"})  # P1b: additive
        self.assertEqual(set(view["items"][0]), {
            "question_id", "summary", "to", "to_id", "asked_at", "asked_age_minutes", "delivery", "delivered_at",
            "delivered_age_minutes", "age_from", "age_minutes", "ack", "routes", "stuck", "origin_work_id", "works",
            "carries", "hop", "hop_since"})  # P1b: additive, v1 fields unchanged
        self.assertEqual(view["home"], self.store.identity()["id"])
        # C5: a sibling home's questions and this home's private views never appear.
        other = Store(self.root / "rowan")
        self.addCleanup(other.close)
        other.initialize("Rowan", "maintainer")
        other.question_ask("david", "Rowan's own question", key="r")
        self.store.view_put("david", [{"cid": "c1", "text": "SECRET VIEW TEXT", "kind": "working",
                                       "source": "test", "certainty": "observed"}], key="v")
        dumped = json.dumps(self.store.docket())
        self.assertNotIn("Rowan's own question", dumped)
        self.assertNotIn("SECRET VIEW TEXT", dumped)

    def test_answered_and_withdrawn_drop_out_and_other_person(self):
        q1 = self.store.question_ask("david", "A?", key="a")
        q2 = self.store.question_ask("david", "B?", key="b")
        self.store.question_ask("rowan", "C?", key="c")
        self.store.feed("david", "yes", reply_to=q1["id"], external_id="x")
        self.store.question_close(q2["id"], "obsolete")
        self.assertEqual(self.store.docket()["count"], 0)
        self.assertIn("(nothing open)", self.store.docket()["text"])
        self.assertEqual(self.store.docket("rowan")["count"], 1)

    def test_no_default_human(self):
        capture.write_humans(self.home, {"default_human": None})
        with self.assertRaisesRegex(ValueError, "default_human"):
            self.store.docket()
        self.assertEqual(self.store.docket("david")["person"], "david")

    def test_cli_text_and_json(self):
        self.store.question_ask("david", "Checkout?", key="q")
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--home", str(self.home), "docket"])
        self.assertTrue(buf.getvalue().startswith("Waiting on david"))
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--home", str(self.home), "docket", "--format", "json", "--person", "Dana Example"])
        self.assertEqual(json.loads(buf.getvalue())["count"], 1)


if __name__ == "__main__":
    unittest.main()
