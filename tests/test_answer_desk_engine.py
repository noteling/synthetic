"""HTTP answer -> keyed channel subprocess -> restarted work engine -> visible follow-through.

Only reasoning is scripted. HTTP, pairing, contract subprocesses, MCP writes, worker recovery,
event processing, shared publication and status rendering use the maintained product.
"""
import json
from types import SimpleNamespace
from unittest import mock

from synthetics import answer_desk, sharedstatus
from synthetics.engine import Engine
from synthetics.mcp import Server
from synthetics.store import Store
from tests import test_i25_answer_desk as desk_fixture


class ScriptedBrain:
    def __init__(self, action):
        self.action = action
        self.calls = 0

    def run(self, context):
        self.calls += 1
        self.action(context)
        return SimpleNamespace(summary="Follow-through recorded through employee tools", cost=None)


class AnswerDeskEngine(desk_fixture.DeskBase):
    def setUp(self):
        super().setUp()
        with Store(self.home) as store:
            self.other_work = store.work_put(title="Unrelated blocked work")
            self.other_question = store.question_ask("david", "A separate decision?", work_id=self.other_work["id"],
                                                      key="separate-decision")
            sharedstatus.write(store)

    def submit(self, cookie, nonce, token, kind="answer", answer=desk_fixture.ANSWER):
        return self.request("POST", "/a/rowan/" + self.question["id"],
                            {"nonce": nonce, "csrf": token, "answer": answer, "kind": kind}, cookie=cookie)

    def assert_unrelated_still_waiting(self):
        with Store(self.home) as store:
            self.assertEqual(store.work_get(self.other_work["id"])["status"], "waiting")
            self.assertEqual(store._get("questions", self.other_question["id"])["status"], "open")

    def test_lost_http_result_then_worker_restart_follows_through_once(self):
        _, cookie = self.paired()
        _, nonce, token = self.form(cookie)
        contract = answer_desk.contract

        def lose_inbound_result(home, key_file, command, *args, lines=None):
            result = contract(home, key_file, command, *args, lines=lines)
            if command == "inbound":
                return {"result": "rejected", "error": {"code": "no_response", "message": "response lost"}}
            return result

        with mock.patch.object(answer_desk, "contract", lose_inbound_result):
            status, _, _ = self.submit(cookie, nonce, token)
        self.assertEqual(status, 503)
        # A client retry recovers the original accepted event, preserving exact answer text.
        status, headers, _ = self.submit(cookie, nonce, token)
        self.assertEqual(status, 303)
        location = headers["Location"]
        event_id = location.rsplit("/", 1)[1]
        _, _, page = self.request("GET", location, cookie=cookie)
        self.assertIn("✓ Submitted", page)
        self.assertIn("… Employee read it", page)
        self.assertIn("… Outcome", page)
        self.assert_unrelated_still_waiting()

        # Start an ordinary run, then recover it exactly as a replacement worker does.
        # No test writes event/work disposition or processing markers directly.
        with Store(self.home) as store:
            interrupted = store.run_begin([event_id])["id"]

        def finish(context):
            event = next(e for e in context["events"] if e["id"] == event_id)
            self.assertEqual(event["body"], desk_fixture.ANSWER)
            self.assertEqual(event["auth"]["channel"], "lookout-desk")
            self.assertEqual(event["reply_to"], self.question["id"])
            with Store(self.home, run_id=context["run"]["id"]) as store:
                self.assertEqual(store.run_get(interrupted)["status"], "interrupted")
                server = Server(store, run_id=context["run"]["id"])
                server.call("work_put", {"id": self.work["id"], "status": "done",
                            "note": "Applied the owner's answer", "evidence": [
                                {"level": "verified", "ref": self.question["id"]}]})
                server.call("message_send", {"to": "david", "body": "Applied your answer; the linked work is complete.",
                            "in_reply_to": event_id, "work_id": self.work["id"], "key": "answer-follow-through"})

        brain = ScriptedBrain(finish)
        runs = Engine(self.home, brain).run(max_turns=3)
        self.assertEqual(len(runs), 1)
        self.assertEqual(brain.calls, 1)
        with Store(self.home) as store:
            self.assertEqual(store.work_get(self.work["id"])["status"], "done")
            processing = store.channel_event_status(self.key.read_bytes(), event_id=event_id)
            self.assertEqual(processing["state"], "processed")
            self.assertEqual(processing["run_id"], runs[0]["id"])
            self.assertEqual(len([e for e in store.event_search() if e["id"] == event_id]), 1)
            reply = next(m for m in store.outbox_list() if m.get("in_reply_to") == event_id)
            self.assertEqual(reply["work_id"], self.work["id"])
        self.assert_unrelated_still_waiting()
        _, _, page = self.request("GET", location, cookie=cookie)
        self.assertIn("✓ Employee read it", page)
        self.assertIn("✓ Outcome", page)
        # Shared status intentionally exports only open work: the page must report that
        # it left that list, rather than infer done versus cancelled from its absence.
        self.assertIn(self.work["id"] + " is no longer open", page)
        self.assertNotIn(desk_fixture.ANSWER, page)
        # Even after processing, retrying the exact form cannot wake another session.
        self.assertEqual(self.submit(cookie, nonce, token)[0], 303)
        self.assertEqual(Engine(self.home, brain).run(), [])
        self.assertEqual(brain.calls, 1)

    def test_partial_context_is_processed_without_answering_or_releasing_work(self):
        _, cookie = self.paired()
        _, nonce, token = self.form(cookie)
        status, headers, _ = self.submit(cookie, nonce, token, kind="context", answer="One part only; more later.")
        self.assertEqual(status, 303)
        location = headers["Location"]
        event_id = location.rsplit("/", 1)[1]

        def retain_context(context):
            event = next(e for e in context["events"] if e["id"] == event_id)
            self.assertIsNone(event.get("reply_to"))
            self.assertEqual(event["in_reply_to"], self.question["id"])
            with Store(self.home, run_id=context["run"]["id"]) as store:
                server = Server(store, run_id=context["run"]["id"])
                server.call("memory_put", {"subject": store.identity()["id"], "predicate": "partial context",
                            "value": event["body"], "source": event_id, "key": "partial-context"})

        brain = ScriptedBrain(retain_context)
        self.assertEqual(len(Engine(self.home, brain).run(max_turns=3)), 1)
        self.assertEqual(self.stored()[:2], ("open", "waiting"))
        self.assert_unrelated_still_waiting()
        _, _, page = self.request("GET", location, cookie=cookie)
        self.assertIn("✓ Employee read it", page)
        self.assertIn("still open", page)
        self.assertNotIn("the question is no longer open", page)

    def test_unpaired_submission_cannot_wake_an_employee(self):
        status, _, _ = self.submit(None, "0" * 32, "0" * 64)
        self.assertEqual(status, 401)
        brain = ScriptedBrain(lambda context: self.fail("unauthorized request started a session"))
        self.assertEqual(Engine(self.home, brain).run(), [])
        self.assertEqual(brain.calls, 0)
        self.assertEqual(self.stored()[:2], ("open", "waiting"))
        self.assert_unrelated_still_waiting()
