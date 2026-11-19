"""RTN P2: the dream (docs/routines.md "The dream").

since= retrieval with an explicit capped/unread_from result (E1), the D3 working_context snapshot field (own dream
session only, survives a new Store and 60+ newer memories), one quarantine notice per quarantined cycle (N3), and
`routine restore` consuming the held work_due (n3). The audit-row assertion for n1 lives in
tests/test_rtn_p1_routines.py SessionFencing."""
import json, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from synthetics import routines
from synthetics.engine import Engine
from synthetics.mcp import METHODS, Server
from synthetics.store import Store, timestamp


def now():
    return datetime.now(timezone.utc)


class Brain:
    def __init__(self, action=None, fail=None):
        self.action, self.fail, self.calls, self.contexts = action, fail, 0, []

    def run(self, context):
        self.calls += 1
        self.contexts.append(context)
        if self.fail:
            raise RuntimeError(self.fail)
        if self.action:
            self.action(context)
        return SimpleNamespace(summary="ok", cost=0.01)


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "a"
        self.s = Store(self.home)
        self.me = self.s.initialize("A", "developer")["id"]
        self.s.entity_put("topic", "Topic", "project")

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def dream(self):
        """A dream routine due within the 15-min fold, so a session may absorb it."""
        dream_at = (now() + timedelta(minutes=5)).strftime("%H:%M")
        return self.s.routine_seed(["dream"], dream_at=dream_at)["created"][0]["work_id"]

    def session(self):
        run = self.s.run_begin()
        return run, Store(self.home, run_id=run["id"])

    def end(self, run, sess):
        sess.close()
        return self.s.run_finish(run["id"], "done")

    def remember(self, n, prefix="note"):
        return [self.s.memory_put("topic", "%s %d" % (prefix, i), "value %d" % i, source="test")["id"]
                for i in range(n)]


class Since(Case):
    """(1) since= returns material at or after since, oldest first, and says when the limit capped it."""

    def test_memory_since_is_inclusive_oldest_first_and_reports_capping(self):
        ids = self.remember(5)
        first = self.s.memory_search(query=ids[0])[0]
        out = self.s.memory_search(since=first["created_at"], limit=3)
        self.assertEqual([m["id"] for m in out["memories"]], ids[:3])
        self.assertTrue(out["capped"])
        fourth = self.s.memory_search(query=ids[3])[0]
        self.assertEqual(out["unread_from"], fourth["created_at"])
        # The next page starts at unread_from; the last page is not capped and has no unread_from.
        rest = self.s.memory_search(since=out["unread_from"], limit=3)
        self.assertEqual([m["id"] for m in rest["memories"]], ids[3:])
        self.assertFalse(rest["capped"])
        self.assertIsNone(rest["unread_from"])
        self.assertEqual(rest["since"], out["unread_from"])

    def test_memory_since_excludes_older_and_keeps_query_and_list_shape_without_since(self):
        old = self.remember(2, "old")
        later = self.s.memory_search(query=old[1])[0]["created_at"]
        new = self.remember(2, "new")
        out = self.s.memory_search(query="new", since=later)
        self.assertEqual([m["id"] for m in out["memories"]], new)
        self.assertIsInstance(self.s.memory_search(query="new"), list)  # unchanged without since

    def test_event_since_and_capped(self):
        for i in range(4):
            self.s.feed("someone", "message %d" % i, external_id="x-%d" % i)
        events = sorted(self.s.event_search(), key=lambda e: e["created_at"])
        out = self.s.event_search(since=events[1]["created_at"], limit=2)
        self.assertEqual([e["id"] for e in out["events"]], [e["id"] for e in events[1:3]])
        self.assertTrue(out["capped"])
        self.assertEqual(out["unread_from"], events[3]["created_at"])
        self.assertIsInstance(self.s.event_search(), list)

    def test_since_needs_a_timezone(self):
        with self.assertRaisesRegex(ValueError, "timezone"):
            self.s.memory_search(since="2026-11-19T10:00:00")
        with self.assertRaisesRegex(ValueError, "timezone"):
            self.s.event_search(since="2026-11-19T10:00:00")

    def test_mcp_tools_take_since(self):
        for name in ("memory_search", "event_search"):
            self.assertIn("since", METHODS[name]["inputSchema"]["properties"])
        self.remember(3)
        run, sess = self.session()
        out = Server(sess, run["id"]).call("memory_search", {"since": "2000-01-01T00:00:00Z", "limit": 2})
        self.assertTrue(out["capped"])
        self.assertEqual(len(out["memories"]), 2)
        self.end(run, sess)

    def test_capped_search_bounds_the_checkpoint_through_the_dream_path(self):
        """E1 via the dream path: a capped since= page yields an omission whose from bounds covered_through."""
        wid = self.dream()
        self.remember(4)
        run, sess = self.session()
        due = [d for d in sess.context()["routines_due"] if d["kind"] == "dream"][0]
        start = due["covered_through"] or "2000-01-01T00:00:00Z"
        page = sess.memory_search(since=start, limit=2)
        self.assertTrue(page["capped"])
        omission = [{"reason": "memory_search since= capped at 2", "from": page["unread_from"]}]
        with self.assertRaisesRegex(ValueError, "unread material"):
            sess.routine_rearm(wid, "r", cycle=due["cycle"], covered_through=due["evidence_cutoff"],
                               omissions=omission)
        out = sess.routine_rearm(wid, "read 2 of 4 memories", cycle=due["cycle"],
                                 covered_through=page["unread_from"], omissions=omission)
        self.assertEqual(out["routine"]["covered_through"], page["unread_from"])
        self.end(run, sess)


class WorkingContext(Case):
    """(2) D3: written by the employee's own dream session through memory_put; its own snapshot key."""

    def write(self, sess, value, replaces=None):
        return sess.memory_put(self.me, routines.WORKING_CONTEXT, value, source="dream review", replaces=replaces)

    def test_survives_a_new_store_and_more_than_60_newer_memories(self):
        wid = self.dream()
        run, sess = self.session()
        wc = self.write(sess, "Focus: P2 PR; blocked on nothing; next: freeze.")
        due = [d for d in sess.context()["routines_due"] if d["kind"] == "dream"][0]
        sess.routine_rearm(wid, "dream done", cycle=due["cycle"])
        self.end(run, sess)
        self.s.close()
        self.s = Store(self.home)  # a worker restart opens a fresh Store
        self.remember(61, "newer")
        ctx = self.s.context()
        self.assertEqual(ctx["working_context"]["id"], wc["id"])
        self.assertEqual(ctx["working_context"]["value"], wc["value"])
        self.assertTrue(ctx["memory_context_truncated"])
        self.assertNotIn(wc["id"], [m["id"] for m in ctx["memories"]])
        self.assertNotIn(wc["id"], [m["id"] for m in ctx["memory_index"]])
        # A later session's snapshot carries it too (what the next session actually consumes).
        self.s.feed("someone", "hello", external_id="x-wc")
        seen = Brain()
        self.s.close()
        Engine(self.home, seen).run(max_turns=1)
        self.s = Store(self.home)
        self.assertEqual(seen.contexts[0]["working_context"]["id"], wc["id"])

    def test_refused_outside_a_dream_session(self):
        with self.assertRaisesRegex(ValueError, "dream session"):
            self.write(self.s, "operator text")  # CLI / operator: no run
        self.s.feed("someone", "hello", external_id="x-1")
        run, sess = self.session()  # a session with no dream due
        with self.assertRaisesRegex(ValueError, "dream"):
            self.write(sess, "not a dream")
        self.end(run, sess)
        self.assertIsNone(self.s.context()["working_context"])

    def test_one_active_bounded_and_replaced(self):
        wid = self.dream()
        run, sess = self.session()
        first = self.write(sess, "v1")
        with self.assertRaisesRegex(ValueError, first["id"]):
            self.write(sess, "v2 without replaces")
        with self.assertRaisesRegex(ValueError, "at most"):
            self.write(sess, "x" * (routines.WORKING_CONTEXT_MAX + 1), replaces=first["id"])
        second = self.write(sess, "v2", replaces=first["id"])
        self.assertEqual(sess.context()["working_context"]["id"], second["id"])
        with self.assertRaisesRegex(ValueError, "your own"):
            sess.memory_put("topic", routines.WORKING_CONTEXT, "v", source="s")
        due = [d for d in sess.context()["routines_due"] if d["kind"] == "dream"][0]
        sess.routine_rearm(wid, "dream done", cycle=due["cycle"])
        self.end(run, sess)


class QuarantineNotice(Case):
    """(8) N3: one notice per quarantined cycle via the report route; none repeated, none guessed."""

    def setUp(self):
        super().setUp()
        (self.home / "runtime.json").write_text(json.dumps({"limit_notify": "boss"}))

    def seed(self, wake_in=-1):
        out = self.s.routine_seed(["hourly-check"], every={"hourly-check": 60},
                                  now=timestamp(now() + timedelta(minutes=wake_in - 60)))
        return out["created"][0]["work_id"]

    def notices(self, wid):
        return [m for m in self.s.outbox_list(limit=500) if m.get("work_id") == wid]

    def capture(self, wid):
        self.s.due_events()
        run = self.s.run_begin()
        self.assertIn(wid, [e.get("work_id") for e in run["events"]])
        return run

    def test_finish_sends_once_and_polls_do_not_repeat(self):
        wid = self.seed()
        run = self.capture(wid)
        self.s.run_finish(run["id"], "done")
        sent = self.notices(wid)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["to"], "boss")
        self.assertIn("quarantined", sent[0]["body"])
        self.assertIn("routine restore hourly-check", sent[0]["body"])
        quarantine = self.s.work_get(wid)["routine"]["quarantine"]
        self.assertEqual(quarantine["notice"]["message_id"], sent[0]["id"])
        for _ in range(3):  # later polls: due_events, snapshot, list, an idle engine
            self.s.due_events()
            self.s.context()
            self.s.routine_list()
        self.s.close()
        self.assertEqual(Engine(self.home, Brain()).run(max_turns=2), [])
        self.s = Store(self.home)
        self.assertEqual(len(self.notices(wid)), 1)
        codes = [f["code"] for f in routines.flags(self.s.work_list())]
        self.assertIn("routine_quarantined", codes)  # flags unchanged
        self.assertEqual(self.s.work_get(wid)["status"], "ready")  # no auto re-arm

    def test_failed_run_then_worker_restart_does_not_repeat(self):
        wid = self.seed()
        self.s.close()
        with self.assertRaises(RuntimeError):
            Engine(self.home, Brain(fail="timeout after 1800 s")).run(max_turns=1)
        self.s = Store(self.home)
        self.assertEqual(len(self.notices(wid)), 1)
        self.assertEqual(self.s.recover_runs(), 0)  # restart
        self.s.close()
        self.assertEqual(Engine(self.home, Brain()).run(max_turns=2), [])
        self.s = Store(self.home)
        self.assertEqual(len(self.notices(wid)), 1)

    def test_interrupted_run_sends_once_and_second_failure_of_same_cycle_does_not(self):
        wid = self.seed()
        run = self.capture(wid)
        self.assertEqual(self.s.recover_runs(), 1)
        self.assertEqual(len(self.notices(wid)), 1)
        # A second failure of the same quarantined cycle (same anchor_at) adds a failure, not a notice.
        again = dict(self.s.run_get(run["id"]), id="run-" + "0" * 32)
        with self.s._tx():
            self.s._quarantine_left_ready(again, "failed")
        self.assertEqual(len(self.s.work_get(wid)["routine"]["failures"]), 2)
        self.assertEqual(len(self.notices(wid)), 1)

    def test_repair_then_fresh_quarantine_sends_a_new_notice(self):
        wid = self.seed()
        self.s.run_finish(self.capture(wid)["id"], "done")
        self.assertEqual(len(self.notices(wid)), 1)
        self.s.routine_restore("hourly-check")
        work = self.s.work_get(wid)
        self.s.due_events(now=timestamp(routines.parse(work["wake_at"]) + timedelta(seconds=1)))
        self.s.run_finish(self.capture(wid)["id"], "done")
        sent = self.notices(wid)
        self.assertEqual(len(sent), 2)
        self.assertNotEqual(sent[0]["id"], sent[1]["id"])

    def test_no_manager_resolved_sends_nothing_and_says_why(self):
        (self.home / "runtime.json").unlink()
        wid = self.seed()
        self.s.run_finish(self.capture(wid)["id"], "done")
        self.assertEqual(self.notices(wid), [])
        notice = self.s.work_get(wid)["routine"]["quarantine"]["notice"]
        self.assertFalse(notice["sent"])
        self.assertIn("no manager resolved", notice["reason"])


class RestoreConsumesHeldInput(Case):
    """(6) n3: `routine restore` consumes the held work_due instead of leaving it pending."""

    def test_restore_consumes_held_work_due(self):
        out = self.s.routine_seed(["hourly-check"], every={"hourly-check": 60},
                                  now=timestamp(now() - timedelta(minutes=61)))
        wid = out["created"][0]["work_id"]
        self.s.close()
        with self.assertRaises(RuntimeError):
            Engine(self.home, Brain(fail="boom")).run(max_turns=1)
        self.s = Store(self.home)
        held = [e for e in self.s.event_pending() if e.get("work_id") == wid]
        self.assertEqual(len(held), 1)
        before = self.s.event_pending_count()
        self.s.routine_restore("hourly-check")
        self.assertEqual([e for e in self.s.event_pending() if e.get("work_id") == wid], [])
        self.assertEqual(self.s.event_pending_count(), before - 1)
        row = self.s.db.execute("SELECT processed_run FROM events WHERE id=?", (held[0]["id"],)).fetchone()
        self.assertEqual(row["processed_run"], Store.RESTORED)


class DreamSeedAndGuidance(Case):
    """(3) the dream is seedable and its guidance is complete and doesn't forbid planning."""

    def test_dream_is_a_seed_kind(self):
        self.assertIn("dream", routines.SEED_KINDS)

    def test_guidance_covers_retrieval_checkpoint_and_working_context(self):
        text = routines.guidance("dream")
        for needle in ("since=", "capped", "unread_from", "omissions", "covered_through", "evidence_cutoff",
                       "replaces", routines.WORKING_CONTEXT, "routine_rearm"):
            self.assertIn(needle, text)
        self.assertNotIn("without building a plan", text)
        self.assertNotRegex(text.lower(), r"(do not|don't|never) (make|build|write) (a )?plans?")


if __name__ == "__main__":
    unittest.main()
