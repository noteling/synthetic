"""Click follow-ups from Vesper's PR #75 review (reviews/click/pr75/review.md) and Codex's rulings: L1, L2, n1, n2."""
import json
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from synthetics import click
from synthetics.engine import Engine
from synthetics.store import Store, timestamp

from tests.test_click import ClickBase, prepared


class ExpiryNeverOrphansAWork(ClickBase):
    """L1: a work waiting only on the click question gets wake_at=now (a re-check, not an answer wake)."""

    def work(self, key="lamp"):
        return self.bound.work_put("Add the lamp to the cart", key=key)

    def test_fire_after_expiry_wakes_the_unlinked_work_and_says_so(self):
        work = self.work()
        asked = self.ask(work_id=work["id"])
        self.assertEqual(self.store._get("works", work["id"])["status"], "waiting")
        self.shift(asked["request"], "asked_at", 31)
        out = self.fire(asked["request"])
        self.assertEqual(out["state"], "expired")
        self.assertEqual(out.get("work_unlinked"), [work["id"]])
        after = self.store._get("works", work["id"])
        self.assertEqual(after["waiting_question_ids"], [])
        self.assertTrue(after.get("wake_at"))
        self.assertLessEqual(after["wake_at"], timestamp())
        self.assertEqual(self.store.due_events(), 1)       # the ordinary due tick makes it ready again
        woke = self.store._get("works", work["id"])
        self.assertEqual(woke["status"], "ready")
        self.assertNotIn("woken_by_answer", woke)           # a re-check, never an answer

    def test_abandoned_request_settled_by_expire_stale_wakes_the_work(self):
        work = self.work()
        asked = self.ask(work_id=work["id"])
        self.shift(asked["request"], "asked_at", 41)
        self.assertEqual(click.expire_stale(self.store), [asked["request"]])
        self.assertTrue(self.store._get("works", work["id"]).get("wake_at"))

    def test_work_with_another_open_question_keeps_waiting_on_it(self):
        work = self.work()
        other = self.bound.question_ask("erin", "Which lamp did you mean?", work_id=work["id"], key="which")
        asked = self.ask(work_id=work["id"])
        self.shift(asked["request"], "asked_at", 31)
        out = self.fire(asked["request"])
        self.assertEqual(out.get("work_unlinked"), [])
        after = self.store._get("works", work["id"])
        self.assertEqual(after["waiting_question_ids"], [other["id"]])
        self.assertFalse(after.get("wake_at"))

    def test_other_final_results_carry_no_work_unlinked(self):
        asked = self.ask()
        self.answer(asked["question_id"], body="no")
        self.assertNotIn("work_unlinked", self.fire(asked["request"]))


class ExpiryNeverStopsOnABadRow(ClickBase):
    """L2: one unreadable clicks row is skipped (and counted); the session start never swallows a failure silently."""

    def bad_row(self, data="not json"):
        self.store.db.execute("INSERT INTO clicks(id, data) VALUES (?, ?)", ("click-" + "0" * 32, data))

    def audits(self, action):
        return [json.loads(r["data"]) for r in
                self.store.db.execute("SELECT data FROM audit WHERE action=?", (action,)).fetchall()]

    def test_bad_rows_are_skipped_and_the_good_one_expires(self):
        asked = self.ask()
        self.shift(asked["request"], "asked_at", 41)
        self.bad_row()
        self.store.db.execute("INSERT INTO clicks(id, data) VALUES (?, ?)", ("click-" + "1" * 32, "[]"))
        self.store.db.execute("INSERT INTO clicks(id, data) VALUES (?, ?)",
                              ("click-" + "2" * 32, json.dumps({"id": "click-" + "2" * 32, "state": "asked"})))
        self.assertEqual(click.expire_stale(self.store), [asked["request"]])
        self.assertEqual(self.record(asked["request"])["state"], "expired")
        self.assertEqual(self.audits("click.expire_skipped"), [{"rows": 3}])

    def test_engine_audits_an_expiry_failure_and_still_runs_the_session(self):
        self.bound.close()
        self.store.run_finish(self.run, "done")
        self.store.feed("david", "anything new?", external_id="wake-1")
        ran = []
        with mock.patch.object(click, "expire_stale", side_effect=RuntimeError("boom")):
            Engine(self.home, SimpleNamespace(run=lambda c: (ran.append(1), SimpleNamespace(summary="s", cost=0))[1])
                   ).run(max_turns=1)
        self.assertEqual(ran, [1])
        self.assertEqual(self.audits("click.expire_failed"), [{"error": "RuntimeError"}])


class FinalVerdictsSettleAfterTheWindows(ClickBase):
    """n1: an asked record whose question is gone settles once both windows passed, so it never stays asked."""

    def test_missing_question_settles_after_the_windows(self):
        asked = self.ask()
        self.store.db.execute("DELETE FROM questions WHERE id=?", (asked["question_id"],))
        self.shift(asked["request"], "asked_at", 20)
        self.assertEqual(click.expire_stale(self.store), [])
        self.assertEqual(self.record(asked["request"])["state"], "asked")      # inside the windows: left alone
        self.shift(asked["request"], "asked_at", 21)
        self.assertEqual(click.expire_stale(self.store), [asked["request"]])
        self.assertEqual((self.record(asked["request"])["state"], self.record(asked["request"])["reason"]),
                         ("refused", "not_approved"))


class SheetAtPrepareIsNamed(ClickBase):
    """n2: Noteling reports a sheet at prepare as exit 7 reason sheet_open; that is not a guard refusal."""

    def test_sheet_open_reason_kept(self):
        self.fake.prepare = {"ok": False, "exit": 7, "reason": "sheet_open", "page_token": "a" * 64,
                             "hit_token": "b" * 64, "find": prepared()["find"]}
        out = self.ask()
        self.assertEqual((out["state"], out["reason"]), ("refused", "sheet_open"))

    def test_real_guard_refusal_still_guard_refused(self):
        self.fake.prepare = {"ok": False, "exit": 7, "reason": "guard_forbidden", "guard": "forbidden",
                             "find": prepared()["find"]}
        self.assertEqual(self.ask()["reason"], "guard_refused")


if __name__ == "__main__":
    unittest.main()
