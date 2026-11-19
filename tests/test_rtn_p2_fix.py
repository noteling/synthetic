"""RTN P2-fix: a dedicated routine work's status/wake_at change ONLY via routine_rearm, the engine quarantine or the
`routine` CLI (Root's repro on a037f6c / main 6784a8e).

Paths closed: (1) question_ask linking a routine; (2) reminder_set linking a routine, and quarantine after any wake
kind; (3) work_put waiting_for naming an open question; (4) an authenticated work ref is a no-op on a routine;
(5) an answer to a question somehow already linked to a routine keeps its status and wake_at. Normal works keep
their question, reminder and work-ref wakes (regression)."""
import tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from synthetics import routines
from synthetics.engine import Engine
from synthetics.store import Store, timestamp

KEY = b"f" * 64


def now():
    return datetime.now(timezone.utc)


class Brain:
    def __init__(self):
        self.calls, self.contexts = 0, []

    def run(self, context):
        self.calls += 1
        self.contexts.append(context)
        return SimpleNamespace(summary="ok", cost=0.01)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "a"
        self.s = Store(self.home)
        self.me = self.s.initialize("A", "developer")["id"]
        self.s.channel_register("fieldpost", Store.credential_hash(KEY), ["fieldpost"], "test")
        moment = now() + timedelta(minutes=10 - 60)
        out = self.s.routine_seed(["hourly-check"], every={"hourly-check": 60}, now=timestamp(moment))
        self.rid = out["created"][0]["work_id"]
        self.before = self.state(self.rid)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def state(self, wid):
        w = self.s.work_get(wid)
        return dict(status=w["status"], wake_at=w["wake_at"], waiting_question_ids=w["waiting_question_ids"])

    def assert_routine_unchanged(self):
        self.assertEqual(self.state(self.rid), self.before)
        self.assertEqual(self.before["status"], "waiting")
        self.assertIsNotNone(self.before["wake_at"])

    def assert_fires_and_is_listed(self):
        """Still on its timer: it fires at wake_at and routines_due lists it."""
        self.s.due_events(now=timestamp(routines.parse(self.before["wake_at"]) + timedelta(seconds=1)))
        due = self.s.routines_due()
        self.assertEqual([(d["work_id"], d["state"]) for d in due], [(self.rid, "fired")])

    def ref(self, work_id, external_id):
        return self.s.feed("employee-codex", "about your work", source="fieldpost", recipient_work_id=work_id,
                           credential=KEY, external_id=external_id)

    def legacy(self, wid, **fields):
        """A record in a state the fixed paths can no longer produce (homes written before the fix)."""
        with self.s._tx():
            self.s._save("works", dict(self.s._get("works", wid), **fields), "test.legacy")


class P1QuestionAsk(Base):
    def test_work_id_routine_is_refused(self):
        with self.assertRaisesRegex(ValueError, "real assignment work, or leave it unlinked"):
            self.s.question_ask("bob", "decide?", work_id=self.rid, key="q1")
        self.assertEqual(self.s.outbox_list(), [])
        self.assert_routine_unchanged()
        self.assert_fires_and_is_listed()

    def test_work_ids_containing_routine_is_refused_and_nothing_linked(self):
        eng = self.s.work_put("eng", status="waiting", wake_at=timestamp(now() + timedelta(days=2)))
        with self.assertRaisesRegex(ValueError, "routine: a question cannot be linked"):
            self.s.question_ask("bob", "decide?", work_ids=[eng["id"], self.rid], key="q2")
        self.assertEqual(self.s.work_get(eng["id"])["waiting_question_ids"], [])
        self.assert_routine_unchanged()

    def test_unlinked_question_is_fine(self):
        q = self.s.question_ask("bob", "decide?", key="q3")
        self.assertEqual(q["status"], "open")
        self.assert_routine_unchanged()


class P2Reminder(Base):
    def test_linked_reminder_is_refused(self):
        with self.assertRaisesRegex(ValueError, "a reminder cannot be linked"):
            self.s.reminder_set(timestamp(now() + timedelta(minutes=1)), "check", work_id=self.rid, key="r1")
        self.assert_routine_unchanged()

    def test_legacy_linked_reminder_fires_without_waking_routine(self):
        at = timestamp(now() + timedelta(minutes=1))
        with self.s._tx():
            self.s._save("reminders", dict(id="reminder-" + "a" * 32, at=at, reason="old", work_id=self.rid,
                                           created_at=timestamp(), fired_at=None), "test.legacy")
        self.s.due_events(now=timestamp(routines.parse(at) + timedelta(seconds=1)))
        self.assertEqual([e["kind"] for e in self.s.event_pending()], ["reminder"])  # still delivered
        self.assert_routine_unchanged()

    def test_routine_left_ready_after_reminder_wake_is_quarantined(self):
        # A routine woken by a (legacy) reminder: the run captures kind=reminder, not work_due.
        self.legacy(self.rid, status="ready", wake_at=None, waiting_for="")
        self.s._event(dict(sender="clock", body="old", kind="reminder", source="clock", external_id="reminder-x",
                           work_id=self.rid, thread=None, reply_to=None))
        run = self.s.run_begin()
        self.assertEqual([e["kind"] for e in run["events"]], ["reminder"])
        self.s.run_finish(run["id"], "done")
        work = self.s.work_get(self.rid)
        self.assertTrue(work["routine"]["quarantine"])
        self.assertEqual(len(work["routine"]["failures"]), 1)
        self.assertNotIn(self.rid, [w["id"] for w in self.s.runnable_work()])

    def test_ready_routine_with_no_input_does_not_spin_the_engine(self):
        # However it became ready, a session that leaves it ready quarantines it: the next polls stay idle.
        self.legacy(self.rid, status="ready", wake_at=None, waiting_for="")
        self.s.close()
        first = Brain()
        self.assertEqual(len(Engine(self.home, first).run(max_turns=1)), 1)
        idle = Brain()
        self.assertEqual(Engine(self.home, idle).run(max_turns=3), [])
        self.assertEqual(idle.calls, 0)
        self.s = Store(self.home)
        work = self.s.work_get(self.rid)
        self.assertTrue(work["routine"]["quarantine"])
        self.assertEqual(len(work["routine"]["failures"]), 1)

    def test_fired_routine_still_waits_for_its_own_session(self):
        # A ready routine whose work_due is still pending is not quarantined by an earlier, unrelated run.
        run = self.s.run_begin()  # starts before the fire; captures nothing of the routine
        self.s.due_events(now=timestamp(routines.parse(self.before["wake_at"]) + timedelta(seconds=1)))
        self.s.run_finish(run["id"], "done")
        work = self.s.work_get(self.rid)
        self.assertEqual(work["status"], "ready")
        self.assertIsNone(work["routine"].get("quarantine"))
        self.assertIn(self.rid, [w["id"] for w in self.s.runnable_work()])


class P3WorkPutWaitingFor(Base):
    def test_waiting_for_open_question_is_refused(self):
        q = self.s.question_ask("bob", "decide?", key="q1")
        with self.assertRaisesRegex(ValueError, r"a question \(waiting_for\) cannot be linked"):
            self.s.work_put(id=self.rid, waiting_for="bob's answer (%s)" % q["id"])
        self.assert_routine_unchanged()
        # The answer later finds no routine to wake.
        reply = self.s.feed("bob", "yes")
        self.s.question_resolve(q["id"], reply["id"])
        self.assert_routine_unchanged()

    def test_note_still_writable(self):
        out = self.s.work_put(id=self.rid, note="checked")
        self.assertEqual(out["note"], "checked")
        self.assert_routine_unchanged()


class P4WorkRef(Base):
    def test_authenticated_ref_is_received_and_changes_nothing(self):
        event = self.ref(self.rid, "m-1")
        self.assertEqual(event["work_ref"]["effect"], "none")
        self.assertIn("routine", event["work_ref"]["reason"])
        self.assertIn(event["id"], [e["id"] for e in self.s.event_pending()])
        self.assert_routine_unchanged()
        self.assertNotIn("woken_by_ref", self.s.work_get(self.rid))
        self.assert_fires_and_is_listed()


class P5LegacyLink(Base):
    def test_answer_to_linked_question_keeps_status_and_wake(self):
        q = self.s.question_ask("bob", "decide?", key="q1")
        self.legacy(self.rid, waiting_question_ids=[q["id"]], waiting_question_id=q["id"])
        reply = self.s.feed("bob", "yes")
        self.s.question_resolve(q["id"], reply["id"])
        after = self.s.work_get(self.rid)
        self.assertEqual((after["status"], after["wake_at"]), (self.before["status"], self.before["wake_at"]))
        self.assertEqual(after["waiting_question_ids"], [])
        self.assertNotIn("woken_by_answer", after)

    def test_withdraw_of_linked_question_keeps_status_and_wake(self):
        q = self.s.question_ask("bob", "decide?", key="q1")
        self.legacy(self.rid, waiting_question_ids=[q["id"]], waiting_question_id=q["id"])
        self.s.question_close(q["id"], "obsolete")
        after = self.s.work_get(self.rid)
        self.assertEqual((after["status"], after["wake_at"]), (self.before["status"], self.before["wake_at"]))


class NormalWorksUnchanged(Base):
    """Regression: ordinary engineering works keep questions, reminders and work-ref wakes."""

    def eng(self):
        return self.s.work_put("eng", status="waiting", wake_at=timestamp(now() + timedelta(days=2)))

    def test_question_links_and_answer_wakes(self):
        w = self.eng()
        q = self.s.question_ask("bob", "decide?", work_id=w["id"], key="q1")
        self.assertEqual(self.s.work_get(w["id"])["waiting_question_ids"], [q["id"]])
        reply = self.s.feed("bob", "yes")
        self.s.question_resolve(q["id"], reply["id"])
        self.assertEqual(self.s.work_get(w["id"])["status"], "ready")
        self.assert_routine_unchanged()

    def test_waiting_for_question_links(self):
        w = self.eng()
        q = self.s.question_ask("bob", "decide?", key="q1")
        out = self.s.work_put(id=w["id"], waiting_for="bob (%s)" % q["id"])
        self.assertEqual(out["waiting_question_ids"], [q["id"]])

    def test_reminder_wakes(self):
        w = self.eng()
        at = timestamp(now() + timedelta(minutes=1))
        self.s.reminder_set(at, "check", work_id=w["id"], key="r1")
        self.s.due_events(now=timestamp(routines.parse(at) + timedelta(seconds=1)))
        self.assertEqual(self.s.work_get(w["id"])["status"], "ready")
        self.assert_routine_unchanged()

    def test_work_ref_wakes(self):
        w = self.eng()
        event = self.ref(w["id"], "m-2")
        self.assertEqual(event["work_ref"]["effect"], "awakened")
        self.assertEqual(self.s.work_get(w["id"])["status"], "ready")
        self.assert_routine_unchanged()

    def test_ready_normal_work_left_ready_is_untouched_at_run_end(self):
        w = self.s.work_put("eng-ready")
        run = self.s.run_begin()
        self.s.run_finish(run["id"], "done")
        self.assertEqual(self.s.work_get(w["id"])["status"], "ready")
        self.assertNotIn("routine", self.s.work_get(w["id"]))


if __name__ == "__main__":
    unittest.main()
