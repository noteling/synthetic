"""M23 (MG-02): a message may name the recipient's work; the recipient Store decides what it means."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from synthetics.cli import main
from synthetics.mcp import METHODS, validate_arguments
from synthetics.store import SESSION_ENV, Store


class WorkRefTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "employee"
        self.store = Store(self.home)
        self.me = self.store.initialize("Vesper", "maintainer")["id"]
        self.key = b"f" * 64
        self.store.channel_register("fieldpost", Store.credential_hash(self.key), ["fieldpost"], "test")
        self.work = self.store.work_put("Waits for Codex", status="waiting", waiting_for="Codex's deploy",
                                        wake_at="2099-01-01T00:00:00Z")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def ref(self, work_id, sender="employee-codex", authenticated=True, external_id=None, **extra):
        return self.store.feed(sender, "about your work", source="fieldpost", recipient_work_id=work_id,
                               credential=self.key if authenticated else None, external_id=external_id, **extra)

    # ---- sender side ----------------------------------------------------
    def test_send_stores_recipient_work_id_and_keeps_work_id_own(self):
        message = self.store.message_send("codex", "done", work_id=self.work["id"], key="m",
                                          recipient_work_id="work-" + "1" * 32)
        self.assertEqual(message["work_id"], self.work["id"])
        self.assertEqual(message["recipient_work_id"], "work-" + "1" * 32)
        question = self.store.question_ask("codex", "ok?", key="q", recipient_work_id="work-" + "2" * 32)
        self.assertEqual(question["recipient_work_id"], "work-" + "2" * 32)

    def test_send_rejects_malformed_and_own_work(self):
        with self.assertRaisesRegex(ValueError, "must be a work ID"):
            self.store.message_send("codex", "x", key="bad", recipient_work_id="work-42ae5fae")
        with self.assertRaisesRegex(ValueError, "is your own work; use work_id"):
            self.store.message_send("codex", "x", key="own", recipient_work_id=self.work["id"])
        self.assertEqual(self.store.outbox_list(), [])

    def test_old_operation_keys_reconcile_without_the_field(self):
        first = self.store.message_send("codex", "x", key="old")
        self.assertNotIn("recipient_work_id", first)
        self.assertEqual(self.store.message_send("codex", "x", key="old")["id"], first["id"])
        with self.assertRaisesRegex(ValueError, "different content"):
            self.store.message_send("codex", "x", key="old", recipient_work_id="work-" + "1" * 32)

    def test_mcp_schema_offers_recipient_work_id(self):
        for name in ("message_send", "question_ask"):
            validate_arguments(METHODS[name], dict(to="a", body="b", key="k", recipient_work_id="work-" + "1" * 32))
            self.assertIn("only a hint", METHODS[name]["inputSchema"]["properties"]["recipient_work_id"]["description"])

    # ---- recipient side -------------------------------------------------
    def test_authenticated_foreign_ref_wakes_waiting_work_and_keeps_prior_wait(self):
        event = self.ref(self.work["id"])
        self.assertEqual(event["work_ref"], dict(id=self.work["id"], valid=True, found=True,
                                                 status_at_receipt="waiting", effect="awakened"))
        work = self.store.work_get(self.work["id"])
        self.assertEqual((work["status"], work["waiting_for"], work["wake_at"]), ("ready", "", None))
        self.assertEqual(work["woken_by_ref"]["event"], event["id"])
        self.assertEqual(work["woken_by_ref"]["prior_waiting_for"], "Codex's deploy")
        self.assertEqual(work["woken_by_ref"]["prior_wake_at"], "2099-01-01T00:00:00.000000Z")
        audit = [row for row in self.store.audit_list(20) if row["action"] == "work.awakened_by_ref"]
        self.assertEqual(audit[0]["data"]["woken_by_ref"]["prior_waiting_for"], "Codex's deploy")
        # C2: the model sees it in the snapshot.
        snapshot = self.store.context()
        self.assertEqual([w["woken_by_ref"]["event"] for w in snapshot["work"] if w["id"] == self.work["id"]],
                         [event["id"]])
        self.assertEqual(snapshot["events"][0]["work_ref"]["effect"], "awakened")

    def test_unauthenticated_ref_only_annotates(self):
        event = self.ref(self.work["id"], authenticated=False)
        self.assertEqual(event["work_ref"]["effect"], "none")
        self.assertEqual(event["work_ref"]["reason"], "unauthenticated")
        self.assertEqual(self.store.work_get(self.work["id"])["status"], "waiting")

    def test_own_sender_does_not_wake(self):
        event = self.ref(self.work["id"], sender=self.me)
        self.assertEqual(event["work_ref"]["reason"], "own message")
        self.assertEqual(self.store.work_get(self.work["id"])["status"], "waiting")

    def test_question_linked_work_resumes_only_through_the_question(self):
        work = self.store.work_put("Asks")
        question = self.store.question_ask("employee-codex", "Which?", work["id"], key="q")
        event = self.ref(work["id"])
        self.assertEqual(event["work_ref"]["effect"], "none")
        self.assertIn(question["id"], event["work_ref"]["reason"])
        self.assertEqual(self.store.work_get(work["id"])["status"], "waiting")
        self.assertEqual(self.store.question_list("open")[0]["id"], question["id"])

    def test_second_ref_to_ready_work_only_annotates(self):
        self.ref(self.work["id"], external_id="m1")
        second = self.ref(self.work["id"], external_id="m2")
        self.assertEqual(second["work_ref"]["effect"], "none")
        self.assertEqual(second["work_ref"]["status_at_receipt"], "ready")
        self.assertEqual(second["work_ref"]["reason"], "work is ready")
        woke = [r for r in self.store.audit_list(50) if r["action"] == "work.awakened_by_ref"]
        self.assertEqual(len(woke), 1)

    def test_finished_work_is_never_changed(self):
        for status, extra in (("done", dict(override="test")), ("cancelled", dict(cancel_reason="test"))):
            work = self.store.work_put("x", status=status, **extra)
            event = self.ref(work["id"], external_id=status)
            self.assertEqual(event["work_ref"]["effect"], "none")
            self.assertEqual(self.store.work_get(work["id"])["status"], status)

    def test_unknown_and_invalid_refs_are_delivered_and_audited(self):
        unknown = self.ref("work-" + "0" * 32, external_id="u")
        self.assertEqual(unknown["work_ref"], dict(id="work-" + "0" * 32, valid=True, found=False,
                                                   status_at_receipt=None, effect="none"))
        invalid = self.ref("garbage " * 30, external_id="i")
        self.assertEqual(invalid["work_ref"], dict(raw=("garbage " * 30)[:80], valid=False, found=False, effect="none"))
        actions = {row["action"] for row in self.store.audit_list(30)}
        self.assertTrue({"event.work_ref_unknown", "event.work_ref_invalid"} <= actions)

    def test_refeed_is_idempotent_and_does_not_reapply(self):
        first = self.ref(self.work["id"], external_id="same")
        self.store.work_put(id=self.work["id"], status="waiting", waiting_for="again")
        again = self.ref(self.work["id"], external_id="same")
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(self.store.work_get(self.work["id"])["status"], "waiting")
        with self.assertRaisesRegex(ValueError, "different content"):
            self.store.feed("employee-codex", "about your work", source="fieldpost", external_id="same",
                            credential=self.key)

    def test_event_without_ref_has_no_work_ref_and_ref_shows_in_history(self):
        plain = self.store.feed("employee-codex", "hi", thread="t")
        self.assertNotIn("work_ref", plain)
        ref = self.ref(self.work["id"], thread="t")
        history = self.store.thread_history("t")["items"]
        self.assertEqual([item.get("work_ref", {}).get("effect") for item in history if item["id"] == ref["id"]],
                         ["awakened"])
        self.assertEqual(self.store.event_get(ref["id"])["work_ref"]["effect"], "awakened")

    # ---- C1: race guard -------------------------------------------------
    def test_ref_during_session_blocks_stale_waiting_write(self):
        run = self.store.run_begin()
        with Store(self.home, run_id=run["id"]) as worker:
            event = self.ref(self.work["id"])
            with self.assertRaisesRegex(ValueError, "woke it during this session"):
                worker.work_put(id=self.work["id"], status="waiting", waiting_for="still Codex")
            self.assertEqual(worker.work_get(self.work["id"])["status"], "ready")
            worker.work_put(id=self.work["id"], note="bookkeeping is fine")
        self.store.run_finish(run["id"], "x")
        self.assertEqual([e["id"] for e in self.store.event_pending()], [event["id"]])
        # The next session captures the event and may wait again (restoring the prior wait).
        run = self.store.run_begin([event["id"]])
        with Store(self.home, run_id=run["id"]) as worker:
            prior = worker.work_get(self.work["id"])["woken_by_ref"]
            restored = worker.work_put(id=self.work["id"], status="waiting", waiting_for=prior["prior_waiting_for"],
                                       wake_at=prior["prior_wake_at"])
            self.assertEqual(restored["status"], "waiting")


class WorkRefDocsTests(unittest.TestCase):
    def test_prompt_and_docs_state_whose_work_each_field_is(self):
        root = Path(__file__).resolve().parent.parent
        prompt = (root / "synthetics" / "prompts" / "employee.md").read_text()
        self.assertIn("`work_id` on message_send and question_ask is always your own work", prompt)
        self.assertIn("only when the event is authenticated", prompt)
        docs = (root / "docs" / "human-channel.md").read_text()
        self.assertIn("is only a hint unless\nauthenticated", docs)


class WorkRefCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "employee"
        with Store(self.home) as store:
            store.initialize("Vesper", "maintainer")
        env = dict(os.environ)
        env.pop(SESSION_ENV, None)
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--home", str(self.home)] + list(args))
        return code, out.getvalue(), err.getvalue()

    def test_send_and_feed_flags(self):
        code, out, _ = self.cli("send", "codex", "hi", "--key", "k", "--recipient-work-id", "work-" + "3" * 32)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["recipient_work_id"], "work-" + "3" * 32)
        code, out, _ = self.cli("feed", "hi", "--from", "codex", "--recipient-work-id", "work-" + "4" * 32)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["work_ref"]["found"], False)
        code, _, err = self.cli("feed", "hi", "--from", "codex", "--recipient-work-id", "work-42ae")
        self.assertNotEqual(code, 0)
        self.assertIn("--recipient-work-id must be a work ID", err)


if __name__ == "__main__":
    unittest.main()
