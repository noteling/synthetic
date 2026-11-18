import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from synthetics.store import Store, timestamp


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.store = Store(self.home)
        self.employee = self.store.initialize("Rowan", "Noteling maintainer", "Maintain useful software.")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def future(self, seconds=60):
        return timestamp(datetime.now(timezone.utc) + timedelta(seconds=seconds))

    def test_identity_relationships_memory_and_work_survive_new_session(self):
        self.store.entity_put("david", "David", "person", "Works with me")
        self.store.entity_put("noteling", "Noteling", "project")
        self.store.memory_put(self.employee["id"], "works_with", "david", source="onboarding")
        self.store.memory_put("david", "explained", "KT means knowledge transfer", source="event-1")
        work = self.store.work_put("Investigate hints", note="Inspect scope handling next.")
        with Store(self.home) as later:
            self.assertEqual(later.identity(), self.employee)
            self.assertEqual(later.work_get(work["id"])["note"], "Inspect scope handling next.")
            self.assertEqual(later.memory_search("knowledge")[0]["subject"], "david")
            self.assertEqual(len(later.entities()), 3)
        with self.assertRaises(ValueError):
            self.store.initialize("Other", "Other role")
        with self.assertRaises(ValueError):
            self.store.entity_put(self.employee["id"], "Other", "person")
        with self.assertRaises(ValueError):
            self.store.entity_put("another-self", "Other", "self")

    def test_memory_correction_keeps_old_experience_but_uses_current_account(self):
        first = self.store.memory_put(self.employee["id"], "role", "maintainer", source="onboarding", key="role-1")
        second = self.store.memory_put(self.employee["id"], "role", "reviewer", source="new assignment", replaces=first["id"])
        self.assertEqual(self.store.memory_search(), [second])
        history = self.store.memory_search(include_superseded=True)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[1]["superseded_by"], second["id"])
        with self.assertRaises(ValueError):
            self.store.memory_put(self.employee["id"], "role", "another", source="new assignment", replaces=first["id"])
        # A repeated effect after interrupted work returns the original record.
        repeated = self.store.memory_put(self.employee["id"], "role", "maintainer", source="onboarding", key="role-1")
        self.assertEqual(repeated["id"], first["id"])
        with self.assertRaises(ValueError):
            self.store.memory_put(self.employee["id"], "role", "different", source="onboarding", key="role-1")

    def test_memory_requires_known_subject_and_valid_kind(self):
        with self.assertRaises(ValueError):
            self.store.memory_put("unknown", "owns", "system", source="onboarding")
        with self.assertRaises(ValueError):
            self.store.memory_put(self.employee["id"], "owns", "system", kind="certainty", source="onboarding")
        with self.assertRaises(ValueError):
            self.store.memory_put(self.employee["id"], "owns", "system")
        self.assertFalse(self.store.memory_search())

    def test_context_keeps_self_memories_visible_when_recent_project_activity_grows(self):
        remembered = self.store.memory_put(self.employee["id"], "works_with", "David", source="onboarding")
        self.store.entity_put("project", "Project", "project")
        for number in range(61):
            self.store.memory_put("project", "finding", "Finding %s" % number, source="investigation")
        context = self.store.context()
        # M26 C1: self-subject memories are indexed (ID + gist) inside the rendered `self` section.
        self.assertNotIn("self_memories", context)
        self.assertIn(remembered["id"], context["self"])
        self.assertEqual(context["memory_count"], 62)
        self.assertTrue(context["memory_context_truncated"])
        self.assertEqual(len(context["memories"]), 60)

    def test_work_updates_preserve_description_priority_and_notes(self):
        work = self.store.work_put("Fix hints", "User-visible regression", priority=2, note="Reproduction recorded")
        updated = self.store.work_put(id=work["id"], status="waiting", waiting_for="A reproduction from David")
        self.assertEqual(updated["description"], "User-visible regression")
        self.assertEqual(updated["note"], "Reproduction recorded")
        self.assertEqual(updated["priority"], 2)
        self.assertFalse(self.store.runnable_work())
        resumed = self.store.work_put(id=work["id"], status="ready")
        self.assertEqual(resumed["waiting_for"], "")
        self.assertIsNone(resumed["wake_at"])
        self.assertEqual(self.store.runnable_work(), [resumed])

    def test_bad_work_update_rolls_back_without_losing_previous_state(self):
        work = self.store.work_put("Fix hints")
        for kwargs in ({"status": "waiting"}, {"status": "unknown"}, {"priority": "high"},
                       {"status": "waiting", "wake_at": "2000-01-01T00:00:00Z"}):
            with self.assertRaises(ValueError):
                self.store.work_put(id=work["id"], **kwargs)
            self.assertEqual(self.store.work_get(work["id"]), work)

    def test_question_waits_and_correlated_answer_resumes_work_atomically(self):
        work = self.store.work_put("Investigate hints")
        question = self.store.question_ask("david", "Which release first showed it?", work["id"], key="release-question")
        self.assertEqual(self.store.work_get(work["id"])["waiting_for"], question["id"])
        self.assertEqual(len(self.store.outbox_list()), 1)
        self.assertEqual(self.store.question_ask("david", "Which release first showed it?", work["id"], key="release-question"), question)
        with self.assertRaises(ValueError):
            self.store.question_ask("david", "Different question", work["id"], key="release-question")
        with self.assertRaises(ValueError):
            self.store.feed("somebody-else", "0.10.4", reply_to=question["id"])
        self.assertEqual(self.store.question_list("open"), [question])
        answer = self.store.feed("david", "0.10.4", reply_to=question["id"], external_id="answer-1")
        self.assertEqual(self.store.work_get(work["id"])["status"], "ready")
        self.assertEqual(self.store.question_list("answered")[0]["answer_event_id"], answer["id"])
        duplicate = self.store.feed("david", "0.10.4", reply_to=question["id"], external_id="answer-1")
        self.assertEqual(duplicate, answer)
        self.assertEqual(len(self.store.event_pending()), 1)

    def test_answer_cannot_revive_cancelled_work_or_put_it_back_to_sleep(self):
        work = self.store.work_put("Investigate")
        question = self.store.question_ask("david", "Which version?", work["id"], key="version")
        self.store.work_put(id=work["id"], status="cancelled", cancel_reason="Superseded")
        self.store.feed("david", "0.10.4", reply_to=question["id"])
        self.assertEqual(self.store.work_get(work["id"])["status"], "cancelled")
        with self.assertRaises(ValueError):
            self.store.question_ask("david", "Another?", work["id"], key="another")
        with self.assertRaises(ValueError):
            self.store.work_put(id=work["id"], status="waiting", waiting_for=question["id"])
        with self.assertRaises(ValueError):
            self.store.work_put(id=work["id"], status="waiting", waiting_for="question-nonexistent")

    def test_explanatory_waiting_label_does_not_destroy_question_dependency(self):
        work = self.store.work_put("Onboard")
        question = self.store.question_ask("david", "Where is my working checkout?", work["id"], key="checkout")
        label = "David's answer to %s (checkout and priorities)" % question["id"]
        waiting = self.store.work_put(id=work["id"], status="waiting", waiting_for=label, note="Other onboarding is complete.")
        self.assertEqual(waiting["waiting_for"], label)
        self.assertEqual(waiting["waiting_question_id"], question["id"])
        self.store.feed("david", "A working copy is ready", reply_to=question["id"])
        resumed = self.store.work_get(work["id"])
        self.assertEqual(resumed["status"], "ready")
        self.assertIsNone(resumed["waiting_question_id"])
        self.assertEqual(resumed["note"], "Other onboarding is complete.")
        with self.assertRaises(ValueError):
            self.store.work_put(id=work["id"], status="waiting", waiting_for=label)

    def _make_legacy_wait(self, work_id, label):
        legacy = self.store.work_get(work_id)
        legacy.pop("waiting_question_id", None)
        legacy["waiting_for"] = label
        self.store.db.execute("UPDATE works SET data=? WHERE id=?", (json.dumps(legacy), work_id))

    def test_legacy_explanatory_wait_is_recovered_when_answer_arrives(self):
        work = self.store.work_put("Onboard")
        question = self.store.question_ask("david", "Where is the checkout?", work["id"], key="checkout")
        self._make_legacy_wait(work["id"], "David's answer to %s (checkout)" % question["id"])
        self.store.feed("david", "A working copy is ready", reply_to=question["id"])
        resumed = self.store.work_get(work["id"])
        self.assertEqual(resumed["status"], "ready")
        self.assertIsNone(resumed["waiting_question_id"])

    def test_legacy_wait_records_stable_link_when_work_notes_are_updated(self):
        work = self.store.work_put("Onboard")
        question = self.store.question_ask("david", "Where is the checkout?", work["id"], key="checkout")
        self._make_legacy_wait(work["id"], "Waiting for David's response")
        updated = self.store.work_put(id=work["id"], note="Ready to resume")
        self.assertEqual(updated["waiting_question_id"], question["id"])
        self.store.feed("david", "A working copy is ready", reply_to=question["id"])
        self.assertEqual(self.store.work_get(work["id"])["status"], "ready")

    def test_older_answer_never_wakes_work_waiting_on_newer_question(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                work = self.store.work_put("Onboard")
                first = self.store.question_ask("david", "Which project?", work["id"], key="project-%s" % legacy)
                newest = self.store.question_ask("david", "Which priorities?", work["id"], key="priorities-%s" % legacy)
                if legacy:
                    self._make_legacy_wait(work["id"], "David's answer")
                else:
                    self.store.work_put(id=work["id"], waiting_for="Waiting for David to provide priorities")
                self.store.feed("david", "Noteling", reply_to=first["id"])
                self.assertEqual(self.store.work_get(work["id"])["status"], "waiting")
                self.store.feed("david", "Fix the regression", reply_to=newest["id"])
                self.assertEqual(self.store.work_get(work["id"])["status"], "ready")

    def test_ready_and_terminal_transitions_clear_waiting_link(self):
        for target in ("ready", "done", "cancelled"):
            work = self.store.work_put("Onboard")
            question = self.store.question_ask("david", "Which priorities?", work["id"], key=target)
            closing = {"done": dict(override="Operator closes without citable evidence"),
                       "cancelled": dict(cancel_reason="No longer needed")}.get(target, {})
            updated = self.store.work_put(id=work["id"], status=target, **closing)
            self.assertIsNone(updated["waiting_question_id"])
            if target != "ready":
                self.store.feed("david", "Priorities supplied", reply_to=question["id"])
                self.assertEqual(self.store.work_get(work["id"])["status"], target)

    def test_late_answer_prevents_same_run_from_replacing_ready_with_uncorrelated_wait(self):
        work = self.store.work_put("Onboard")
        run = self.store.run_begin()
        with Store(self.home, run_id=run["id"]) as worker:
            question = worker.question_ask("david", "Where is the checkout?", work["id"], key="checkout")
            answer = self.store.feed("david", "The checkout is ready", reply_to=question["id"])
            with self.assertRaisesRegex(ValueError, "New answer arrived during this session"):
                worker.work_put(id=work["id"], status="waiting", waiting_for="Waiting for David to provide checkout")
            self.assertEqual(worker.work_get(work["id"])["status"], "ready")
            # Ordinary bookkeeping remains possible without reapplying stale status.
            worker.work_put(id=work["id"], note="I asked about the checkout")
            self.assertEqual(worker.work_get(work["id"])["status"], "ready")
        self.store.run_finish(run["id"], "Question sent; new information arrived")
        self.assertEqual(self.store.event_pending(), [answer])

    def test_fresh_session_that_captured_answer_can_choose_new_waiting_reason(self):
        work = self.store.work_put("Onboard")
        run = self.store.run_begin()
        with Store(self.home, run_id=run["id"]) as worker:
            question = worker.question_ask("david", "Where is the checkout?", work["id"], key="checkout")
            answer = self.store.feed("david", "The checkout is ready", reply_to=question["id"])
        self.store.run_finish(run["id"], "Waiting for next session to read the answer")
        next_run = self.store.run_begin()
        self.assertIn(answer["id"], next_run["event_ids"])
        with Store(self.home, run_id=next_run["id"]) as worker:
            waiting = worker.work_put(id=work["id"], status="waiting", waiting_for="A test machine is being provisioned")
            self.assertEqual(waiting["status"], "waiting")
            self.assertIsNone(waiting["waiting_question_id"])
        self.store.run_finish(next_run["id"], "Read the checkout answer and identified another dependency")
        self.store.work_put(id=work["id"], status="ready")
        later_run = self.store.run_begin()
        with Store(self.home, run_id=later_run["id"]) as worker:
            # Once handled, that earlier reply no longer prevents legitimate waiting.
            worker.work_put(id=work["id"], status="waiting", waiting_for="Test machine still pending")

    def test_uncaptured_answer_in_input_backlog_also_prevents_stale_wait(self):
        work = self.store.work_put("Onboard")
        question = self.store.question_ask("david", "Where is the checkout?", work["id"], key="checkout")
        for number in range(50):
            self.store.feed("david", "Earlier event %s" % number)
        answer = self.store.feed("david", "The checkout is ready", reply_to=question["id"])
        run = self.store.run_begin()
        self.assertNotIn(answer["id"], run["event_ids"])
        with Store(self.home, run_id=run["id"]) as worker:
            with self.assertRaisesRegex(ValueError, "input batch"):
                worker.work_put(id=work["id"], status="waiting", waiting_for="Waiting for David's checkout")
        self.assertEqual(self.store.work_get(work["id"])["status"], "ready")

    def test_new_correlated_question_after_late_answer_can_still_block_same_run(self):
        work = self.store.work_put("Onboard")
        run = self.store.run_begin()
        with Store(self.home, run_id=run["id"]) as worker:
            first = worker.question_ask("david", "Where is the checkout?", work["id"], key="checkout")
            self.store.feed("david", "The checkout is ready", reply_to=first["id"])
            newest = worker.question_ask("david", "Which issue is first?", work["id"], key="priorities")
            waiting = worker.work_put(id=work["id"], status="waiting", waiting_for="Waiting for David's priorities")
            self.assertEqual(waiting["waiting_question_id"], newest["id"])
            self.assertEqual(waiting["status"], "waiting")

    def test_messages_idempotency_does_not_claim_delivery(self):
        message = self.store.message_send("david", "I have a reproduction.", key="reproduction")
        self.assertEqual(self.store.message_send("david", "I have a reproduction.", key="reproduction"), message)
        with self.assertRaises(ValueError):
            self.store.message_send("david", "Different", key="reproduction")
        self.assertEqual(self.store.outbox_list(), [dict(message, delivery="pending")])  # M25 summary state
        self.assertNotIn("delivered", message)

    def test_feed_deduplication_uses_source_and_external_id(self):
        first = self.store.feed("david", "Hello", source="chat", external_id="1")
        self.assertEqual(self.store.feed("david", "Hello", source="chat", external_id="1"), first)
        with self.assertRaises(ValueError):
            self.store.feed("david", "Different", source="chat", external_id="1")
        second = self.store.feed("david", "Hello", source="email", external_id="1")
        self.assertNotEqual(first["id"], second["id"])

    def test_source_events_remain_retrievable_after_acknowledgement(self):
        first = self.store.feed("david", "Knowledge transfer means KT", thread="onboarding")
        second = self.store.feed("priya", "Release approved", thread="release")
        run = self.store.run_begin()
        self.store.run_finish(run["id"], "Learned the terms and release arrangement")
        self.assertFalse(self.store.event_pending())
        self.assertEqual(self.store.event_get(first["id"]), first)
        self.assertEqual(self.store.event_search("knowledge KT"), [first])
        self.assertEqual(self.store.event_search(thread="release"), [second])
        self.assertEqual(self.store.event_search(limit=1), [second])
        with self.assertRaises(ValueError):
            self.store.event_get("missing-event")

    def test_original_event_time_is_distinct_from_receipt_and_deduplicates(self):
        when = "2025-05-12T13:00:00-04:00"
        event = self.store.feed("david", "Earlier decision", occurred_at=when, source="email", external_id="1")
        self.assertEqual(event["occurred_at"], "2025-05-12T17:00:00.000000Z")
        self.assertGreater(event["created_at"], event["occurred_at"])
        repeated = self.store.feed("david", "Earlier decision", occurred_at=when, source="email", external_id="1")
        self.assertEqual(repeated, event)
        with self.assertRaises(ValueError):
            self.store.feed("david", "Earlier decision", occurred_at="2025-05-13T13:00:00-04:00", source="email", external_id="1")

    def test_reminders_and_timed_work_fire_once_after_restart(self):
        due = self.future()
        work = self.store.work_put("Follow up", status="waiting", wake_at=due)
        reminder = self.store.reminder_set(due, "Check whether the reply came", key="followup")
        self.assertEqual(self.store.reminder_set(due, "Check whether the reply came", key="followup"), reminder)
        self.assertEqual(self.store.due_events(), 0)
        with Store(self.home) as later:
            self.assertEqual(later.due_events(due), 2)
            self.assertEqual(later.due_events(due), 0)
            self.assertEqual(later.work_get(work["id"])["status"], "ready")
            self.assertEqual(len(later.event_pending()), 2)
            self.assertEqual(later.reminders_list(), [])

    def test_reminder_can_wake_waiting_work_but_not_terminal_work(self):
        work = self.store.work_put("Investigate", status="waiting", waiting_for="answer")
        due = self.future()
        self.store.reminder_set(due, "Follow up", work["id"], key="follow")
        self.store.due_events(due)
        self.assertEqual(self.store.work_get(work["id"])["status"], "ready")
        key = b"k" * 32
        self.store.channel_register("test-chan", Store.credential_hash(key), ["manual"], "test")
        confirmation = self.store.feed("david", "Looks finished", credential=key)
        self.store.work_put(id=work["id"], status="done", evidence=[{"level": "verified", "ref": confirmation["id"]}])
        self.store.reminder_set(due, "Late reminder", work["id"], key="late")
        self.store.due_events(due)
        self.assertEqual(self.store.work_get(work["id"])["status"], "done")

    def test_run_acknowledges_only_its_captured_inputs(self):
        first = self.store.feed("david", "Investigate hints")
        run = self.store.run_begin()
        self.assertEqual(run["events"], [first])
        second = self.store.feed("david", "Requirement changed")
        self.store.run_finish(run["id"], "Initial investigation complete")
        self.assertEqual(self.store.event_pending(), [second])
        with self.assertRaises(ValueError):
            self.store.run_finish(run["id"], "Again")

    def test_failure_and_restart_preserve_inputs_and_already_recorded_effects(self):
        event = self.store.feed("david", "Investigate")
        first = self.store.run_begin()
        self.store.message_send("david", "One question", key="question-once")
        self.store.run_fail(first["id"], "connection failed")
        second = self.store.run_begin()
        with self.assertRaises(ValueError):
            self.store.run_begin()
        with Store(self.home) as later:
            self.assertEqual(later.recover_runs(), 1)
            self.assertEqual(later.recover_runs(), 0)
            self.assertEqual(later.run_get(second["id"])["status"], "interrupted")
            self.assertEqual(later.event_pending(), [event])
            self.assertEqual(len(later.outbox_list()), 1)

    def test_recovery_fences_writes_from_orphaned_model_process(self):
        run = self.store.run_begin()
        with Store(self.home, run_id=run["id"]) as worker:
            worker.entity_put("david", "David", "person")
            self.store.recover_runs()
            with self.assertRaisesRegex(ValueError, "no longer active"):
                worker.message_send("david", "Stale result", key="stale")
            self.assertEqual(worker.outbox_list(), [])
            # Reads remain possible; only effects are fenced.
            self.assertEqual(worker.identity()["name"], "Rowan")

    def test_ambiguous_timestamps_are_rejected(self):
        with self.assertRaises(ValueError):
            self.store.reminder_set("2030-01-01T10:00:00", "Ambiguous", key="ambiguous")
        self.assertEqual(timestamp("2030-01-01T10:00:00-05:00"), "2030-01-01T15:00:00.000000Z")


if __name__ == "__main__":
    unittest.main()
