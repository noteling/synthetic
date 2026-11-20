import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from synthetics.engine import Engine
from synthetics.mcp import Server
from synthetics.store import Store


class ThreadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "employee"
        self.store = Store(self.home)
        self.addCleanup(self.store.close)
        self.identity = self.store.initialize("Morgan", "Maintainer")

    def test_outgoing_thread_and_generic_replies_survive_restart_and_conflict_check(self):
        incoming = self.store.feed("david", "Investigate", thread="chat-a")
        message = self.store.message_send("david", "Started", key="started", thread="chat-a",
                                          in_reply_to=incoming["id"])
        work = self.store.work_put("Investigate")
        question = self.store.question_ask("david", "Which release?", work_id=work["id"], key="release",
                                          thread="chat-a", in_reply_to=incoming["id"])
        with Store(self.home) as later:
            self.assertEqual(later.message_send("david", "Started", key="started", thread="chat-a",
                                                in_reply_to=incoming["id"]), message)
            self.assertEqual(later.question_list()[0]["thread"], "chat-a")
            self.assertEqual(later.outbox_list()[1]["in_reply_to"], incoming["id"])
            with self.assertRaises(ValueError):
                later.message_send("david", "Started", key="started", thread="another-chat",
                                   in_reply_to=incoming["id"])
            # Provider reply metadata alone must not silently answer a question.
            generic = later.feed("david", "One more thought", thread="chat-a", in_reply_to=question["message_id"],
                                 source="provider", external_id="generic-1")
            self.assertEqual(later.work_get(work["id"])["status"], "waiting")
            self.assertEqual(later.question_list()[0]["status"], "open")
            self.assertEqual(later.feed("david", "One more thought", thread="chat-a",
                                       in_reply_to=question["message_id"], source="provider", external_id="generic-1"), generic)
            with self.assertRaises(ValueError):
                later.feed("david", "One more thought", thread="chat-a", in_reply_to="another-message",
                           source="provider", external_id="generic-1")
            answer = later.feed("david", "Release 2", thread="chat-a", reply_to=question["id"],
                                in_reply_to=question["message_id"], source="provider", external_id="answer-1")
            self.assertEqual(answer["in_reply_to"], question["message_id"])
            self.assertEqual(later.work_get(work["id"])["status"], "ready")

    def test_pre_extension_operation_keys_reconcile_without_new_null_fields(self):
        # These are the exact pre-extension payload/record formats, not new API
        # calls masquerading as old state. All fixtures belong to this temp home.
        message_payload = dict(to="david", body="Old message", work_id=None)
        question_payload = dict(to="david", body="Old question", work_id=None)
        message = dict(message_payload, id="message-old", sender=self.identity["id"], kind="message",
                       question_id=None, created_at="2026-11-15T12:00:00Z")
        question = dict(question_payload, id="question-old", message_id="message-old-question", status="open",
                        answer_event_id=None, created_at="2026-11-15T12:00:00Z")
        feed_payload = dict(sender="david", body="Old input", kind="message", thread="old-thread", reply_to=None,
                            source="manual", external_id="old-input", occurred_at=None)
        with self.store._tx():
            self.store._save("messages", message, "outbox.queued")
            self.store._save("questions", question, "question.asked")
            self.store._key("message", "old-message", message_payload, message["id"])
            self.store._key("question", "old-question", question_payload, question["id"])
            old_event = self.store._event(feed_payload)
            self.store._key("feed:manual", "old-input", feed_payload, old_event["id"])
        with Store(self.home) as upgraded:
            self.assertEqual(upgraded.message_send("david", "Old message", key="old-message", thread=None,
                                                   in_reply_to=None), message)
            self.assertEqual(upgraded.question_ask("david", "Old question", key="old-question"), question)
            self.assertEqual(upgraded.feed("david", "Old input", thread="old-thread", external_id="old-input",
                                           in_reply_to=None), old_event)
            self.assertNotIn("thread", upgraded.outbox_list()[0])
            self.assertNotIn("in_reply_to", upgraded.event_get(old_event["id"]))

    def test_bidirectional_history_uses_stable_paginated_receipt_order(self):
        incoming = self.store.feed("david", "First", thread="a", occurred_at="2026-11-15T12:00:00Z")
        outgoing = self.store.message_send("david", "Second", key="second", thread="a")
        self.store.feed("other", "Unrelated", thread="b")
        # Original provider time can predate an earlier received item. Creation
        # order still gives a stable transcript and pagination boundary.
        third = self.store.feed("david", "Third", thread="a", occurred_at="2026-11-14T12:00:00Z")
        fourth = self.store.question_ask("david", "Fourth", key="fourth", thread="a")
        self.store.message_send("david", "Unthreaded", key="unthreaded")
        run = self.store.run_begin()
        self.store.run_finish(run["id"], "Read messages")
        with Store(self.home) as restarted:
            page = restarted.thread_history("a", limit=2)
            self.assertEqual([item["body"] for item in page["items"]], ["Third", "Fourth"])
            self.assertEqual([item["direction"] for item in page["items"]], ["incoming", "outgoing"])
            self.assertEqual(page["total"], 4)
            self.assertTrue(page["truncated"])
            restarted.feed("david", "New arrival", thread="a")
            older = restarted.thread_history("a", limit=2, before=page["next_before"])
            self.assertEqual([item["id"] for item in older["items"]], [incoming["id"], outgoing["id"]])
            self.assertFalse(older["truncated"])
            self.assertIsNone(older["next_before"])
            self.assertEqual(page["items"][1]["question_id"], fourth["id"])
            self.assertEqual(page["items"][0]["id"], third["id"])
            self.assertEqual(restarted.thread_history("absent")["items"], [])

    def test_history_limits_are_validated(self):
        for kwargs in ({"limit": True}, {"limit": 0}, {"limit": 501}, {"before": True}, {"before": 0},
                       {"before": "1"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.store.thread_history("a", **kwargs)

    def test_engine_context_excludes_uncaptured_and_late_inputs_from_all_threads(self):
        earlier = self.store.feed("david", "Earlier decision", thread="a")
        old_run = self.store.run_begin()
        self.store.run_finish(old_run["id"], "Learned the decision")
        previous_reply = self.store.message_send("david", "Earlier reply", key="earlier-reply", thread="a")
        captured = [self.store.feed("david", "Instruction %s" % number, thread="a") for number in range(50)]
        deferred = self.store.feed("david", "NOT CAPTURED YET", thread="a")
        original_begin = Store.run_begin
        late_ids = []

        def begin_then_deliver(store, **kwargs):
            run = original_begin(store, **kwargs)
            late_ids.append(store.feed("david", "LATE SAME THREAD", thread="a")["id"])
            late_ids.append(store.feed("david", "LATE OTHER THREAD", thread="late")["id"])
            return run

        def inspect(context):
            serialized = json.dumps(context)
            for secret in ("NOT CAPTURED YET", "LATE SAME THREAD", "LATE OTHER THREAD"):
                self.assertNotIn(secret, serialized)
            self.assertEqual(set(context["threads"]), {"a"})
            self.assertEqual([item["id"] for item in context["events"]], [item["id"] for item in captured])
            page = context["threads"]["a"]
            self.assertEqual(len(page["items"]), 30)
            self.assertEqual(page["total"], 52)
            self.assertTrue(page["truncated"])
            with Store(self.home, run_id=context["run"]["id"]) as bound:
                history = bound.thread_history("a", limit=100)
                self.assertEqual([item["id"] for item in history["items"]],
                                 [earlier["id"], previous_reply["id"]] + [item["id"] for item in captured])
                current = bound.message_send("david", "Reply made in this session", key="current", thread="a")
                self.assertEqual(bound.thread_history("a")["items"][-1]["id"], current["id"])
            return SimpleNamespace(summary="Checked context isolation", cost=0)

        with patch.object(Store, "run_begin", begin_then_deliver):
            Engine(self.home, SimpleNamespace(run=inspect)).run()
        self.assertEqual([item["id"] for item in self.store.event_pending()], [deferred["id"]] + late_ids)

    def test_context_only_hydrates_threads_with_pending_inputs(self):
        self.store.feed("david", "Other conversation", thread="other")
        first = self.store.run_begin()
        self.store.run_finish(first["id"], "Read other conversation")
        self.store.feed("david", "Current conversation", thread="current")
        self.assertEqual(set(self.store.context()["threads"]), {"current"})

    def test_mcp_roundtrip_preserves_thread_metadata_and_exposes_history(self):
        event = self.store.feed("david", "Question", thread="mcp-thread")
        server = Server(self.store)
        message = server.call("message_send", dict(to="david", body="Answer", key="answer", thread="mcp-thread",
                                                    in_reply_to=event["id"]))
        self.assertEqual(message["thread"], "mcp-thread")
        page = server.call("thread_history", dict(thread="mcp-thread", limit=1))
        self.assertEqual(page["items"][0]["in_reply_to"], event["id"])
        older = server.call("thread_history", dict(thread="mcp-thread", before=page["next_before"]))
        self.assertEqual(older["items"][0]["id"], event["id"])
        with self.assertRaises(ValueError):
            server.call("thread_history", dict(thread="mcp-thread", limit=True))

    def test_operator_history_import_is_durable_idempotent_and_has_no_live_effects(self):
        items = [dict(id="provider-user", sender="david", direction="incoming", body="  Original text\n",
                      occurred_at=None, turn_id="turn-1", provenance={"thread_id": "original", "role": "user"}),
                 dict(id="provider-assistant", sender="codex-manager", direction="outgoing", body="Existing answer",
                      occurred_at="2026-11-15T12:00:00Z", provenance={"role": "assistant"})]
        imported = self.store.thread_import("conversation", items, source="codex:original")
        self.assertFalse(self.store.event_pending())
        self.assertFalse(self.store.outbox_list())
        self.assertFalse(self.store.question_list())
        self.assertFalse(self.store.work_list())
        with Store(self.home) as restarted:
            self.assertEqual(restarted.thread_import("conversation", items, source="codex:original"), imported)
            page = restarted.thread_history("conversation")
            self.assertEqual(page["total"], 2)
            self.assertEqual(page["items"][0]["body"], "  Original text\n")
            self.assertIsNone(page["items"][0]["occurred_at"])
            self.assertEqual(page["items"][0]["turn_id"], "turn-1")
            self.assertEqual([item["external_id"] for item in page["items"]], [item["id"] for item in items])
            self.assertTrue(all(item["historical"] for item in page["items"]))
            # The same imported conversation can continue through a new channel.
            restarted.feed("david", "Continue here", thread="conversation", source="telegram", external_id="1")
            self.assertEqual([item["body"] for item in restarted.context()["threads"]["conversation"]["items"]],
                             ["  Original text\n", "Existing answer", "Continue here"])

    def test_import_conflict_rolls_back_entire_batch_and_model_cannot_import(self):
        original = dict(id="first", sender="david", direction="incoming", body="Original")
        self.store.thread_import("a", [original], source="export")
        new = dict(id="new", sender="david", direction="incoming", body="New")
        conflicting = dict(original, body="Changed")
        with self.assertRaises(ValueError):
            self.store.thread_import("a", [new, conflicting], source="export")
        self.assertEqual([item["external_id"] for item in self.store.thread_history("a")["items"]], ["first"])
        self.assertFalse(self.store.thread_history("b")["items"])
        with self.assertRaises(ValueError):
            self.store.thread_import("b", [original], source="export")
        run = self.store.run_begin()
        with Store(self.home, run_id=run["id"]) as bound:
            with self.assertRaises(ValueError):
                bound.thread_import("a", [new], source="export")
            with self.assertRaises(ValueError):
                Server(bound).call("thread_import", {})

    def test_full_imported_transcript_can_be_paged_without_body_truncation(self):
        long_body = "Original visible text \u2014 " * 2000
        items = [dict(id="item-%s" % number, sender="david" if number % 2 == 0 else "codex-manager",
                      direction="incoming" if number % 2 == 0 else "outgoing",
                      body=long_body if number == 7 else "Message %s" % number)
                 for number in range(133)]
        self.store.thread_import("long-conversation", items, source="visible-export")
        recovered = []
        before = None
        while True:
            page = self.store.thread_history("long-conversation", limit=30, before=before)
            recovered = page["items"] + recovered
            self.assertEqual(page["total"], 133)
            if page["next_before"] is None:
                break
            before = page["next_before"]
        self.assertEqual([item["external_id"] for item in recovered], [item["id"] for item in items])
        self.assertEqual(recovered[7]["body"], long_body)
        self.assertFalse(self.store.event_pending())
        self.assertFalse(self.store.outbox_list())


if __name__ == "__main__":
    unittest.main()
