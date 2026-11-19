"""Click P1 follow-ups from Vesper's PR #73 review (reviews/click/pr73/review.md): L1, n1, n2."""
import json
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from synthetics import click
from synthetics.engine import Engine
from synthetics.store import Store

from tests.test_click import FIRED, ClickBase


class ExpiryWithdrawsQuestion(ClickBase):
    """L1: an expired or abandoned request must not leave its question open in the human's Docket."""

    def status(self, qid):
        return self.store._get("questions", qid)["status"]

    def test_fire_after_ask_window_withdraws_open_question(self):
        asked = self.ask()
        self.shift(asked["request"], "asked_at", 31)
        out = self.fire(asked["request"])
        self.assertEqual((out["state"], out["reason"]), ("expired", "expired"))
        self.assertEqual(self.status(asked["question_id"]), "withdrawn")
        self.assertEqual(self.fake.fires(), [])

    def test_abandoned_request_is_settled_by_the_next_request(self):
        first = self.ask()
        self.shift(first["request"], "asked_at", 41)        # never fired; past both windows
        second = self.ask("Save for later")
        self.assertEqual(second["state"], "asked")
        self.assertEqual(self.record(first["request"])["state"], "expired")
        self.assertEqual(self.status(first["question_id"]), "withdrawn")
        open_qs = [q for q in self.questions() if q["status"] == "open"]
        self.assertEqual([q["id"] for q in open_qs], [second["question_id"]])

    def test_open_question_past_ask_window_is_settled_before_the_next_request(self):
        first = self.ask()
        self.shift(first["request"], "asked_at", 31)        # unanswered past 30 min: can never be approved
        self.ask("Save for later")
        self.assertEqual(self.record(first["request"])["state"], "expired")
        self.assertEqual(self.status(first["question_id"]), "withdrawn")

    def test_session_start_settles_an_abandoned_request(self):
        asked = self.ask()
        self.shift(asked["request"], "asked_at", 41)
        self.bound.close()
        self.store.run_finish(self.run, "done")
        self.store.feed("david", "anything new?", external_id="wake-1")
        seen = {}

        def look(context):
            seen["open"] = [q["id"] for q in context["questions"] if q.get("status") == "open"]
        Engine(self.home, SimpleNamespace(run=lambda c: (look(c), SimpleNamespace(summary="s", cost=0))[1])).run(
            max_turns=1)
        with Store(self.home) as store:
            self.assertEqual(store._get("clicks", asked["request"])["state"], "expired")
            self.assertEqual(store._get("questions", asked["question_id"])["status"], "withdrawn")
        self.assertNotIn(asked["question_id"], seen["open"])

    def test_withdraw_unlinks_the_waiting_work(self):
        work = self.bound.work_put("Add the lamp to the cart", key="lamp")
        asked = self.ask(work_id=work["id"])
        self.assertIn(asked["question_id"], self.store._get("works", work["id"])["waiting_question_ids"])
        self.shift(asked["request"], "asked_at", 31)
        self.fire(asked["request"])
        self.assertNotIn(asked["question_id"], self.store._get("works", work["id"])["waiting_question_ids"])

    def test_guards_live_and_answered_questions_are_left_alone(self):
        asked = self.ask()
        self.shift(asked["request"], "asked_at", 20)        # still inside the ask window
        self.assertEqual(click.expire_stale(self.store), [])
        self.assertEqual(self.status(asked["question_id"]), "open")
        self.assertEqual(self.fire(asked["request"])["reason"], "waiting")
        self.answer(asked["question_id"])                   # answered, then left past the fire window
        later = click._now() + timedelta(minutes=11)
        with mock.patch.object(click, "_now", return_value=later):
            self.assertEqual(click.expire_stale(self.store), [asked["request"]])
        self.assertEqual(self.status(asked["question_id"]), "answered")   # answered stays answered

    def test_declined_is_not_withdrawn(self):
        asked = self.ask()
        self.answer(asked["question_id"], body="no")
        self.assertEqual(self.fire(asked["request"])["state"], "declined")
        self.assertEqual(self.status(asked["question_id"]), "answered")


class AnswerMustTypeTheNamesMarks(ClickBase):
    """n1: punctuation in the control's name counts; punctuation the human adds around it doesn't."""

    def verdict(self, name, body):
        asked = self.ask(name)
        self.answer(asked["question_id"], body=body)
        return self.fire(asked["request"])["state"]

    def test_yes_c_does_not_approve_c_plus_plus(self):
        self.assertEqual(self.verdict("C++", "yes C"), "declined")

    def test_name_marks_typed_approve(self):
        for name, body in (("C++", "Yes, c++!"), ("Don’t save", "yes don't save"),
                           ("Add to cart", "Yes, add to CART!")):
            with self.subTest(name=name, body=body):
                self.assertEqual(self.verdict(name, body), "done")

    def test_helper(self):
        self.assertTrue(click.names_control('yes "Save & close".', "Save & close"))
        self.assertFalse(click.names_control("yes save close", "Save & close"))
        self.assertFalse(click.names_control("yes save and close", "Save & close"))


class UnexpectedReaderErrorIsUnknownAtOnce(ClickBase):
    """n2: a reader failure that isn't a GlanceError must not leave the request firing until FIRE_STALE."""

    def test_runtime_error_is_unknown_and_never_retried(self):
        asked = self.ask()
        self.answer(asked["question_id"])
        self.fake.fire = RuntimeError("boom")
        out = self.fire(asked["request"])
        self.assertEqual((out["state"], out["reason"], out.get("error")), ("unknown", "unknown", "RuntimeError"))
        self.assertEqual(self.record(asked["request"])["state"], "unknown")
        self.fake.fire = dict(FIRED)
        self.assertEqual(self.fire(asked["request"])["reason"], "already_used")
        self.assertEqual(len(self.fake.fires()), 1)


if __name__ == "__main__":
    unittest.main()
