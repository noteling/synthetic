"""RTN P1-r1: freeze conditions after P1 (reviews/RTN/P0-r1/addendum.md).

F1 a quarantined routine's pending work_due never starts a session (real Engine); F2 compare-and-set re-arm at
every=5; F3 an omission without a known earliest unread time blocks a covered_through advance; F4 manager route
through the OFFICIAL company fold (aliases); G1 `routine seed --first`. G2 lives in tests/test_authority.py."""
import contextlib, io, json, os, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from synthetics import authority, capture, routines
from synthetics.cli import main
from synthetics.engine import Engine
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


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "a"
        with Store(self.home) as s:
            self.me = s.initialize("A", "developer")["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def seed(self, every=60, wake_in=10, kind="hourly-check"):
        with Store(self.home) as s:
            out = s.routine_seed([kind], every={kind: every},
                                 now=timestamp(now() + timedelta(minutes=wake_in - every)))
            return out["created"][0]["work_id"]

    def work(self, wid):
        with Store(self.home) as s:
            return s.work_get(wid)


class F1EngineAdmission(Base):
    def test_failed_routine_session_does_not_restart_and_unrelated_input_runs(self):
        wid = self.seed(wake_in=-1)  # already due: the engine's own due_events fires it
        failing = Brain(fail="timeout after 1800 s")
        with self.assertRaises(RuntimeError):
            Engine(self.home, failing).run(max_turns=1)
        self.assertEqual(failing.calls, 1)
        self.assertTrue(self.work(wid)["routine"]["quarantine"])
        # The next polls with no new input stay idle: no session, no growing failure streak.
        idle = Brain()
        self.assertEqual(Engine(self.home, idle).run(max_turns=3), [])
        self.assertEqual(idle.calls, 0)
        with Store(self.home) as s:
            held = [e for e in s.event_pending() if e["kind"] == "work_due" and e["work_id"] == wid]
            self.assertEqual(len(held), 1)  # input evidence kept
            self.assertTrue(s.admission_held(held[0]))
            failed = [r for r in s.run_list(10) if r["status"] == "failed"]
            self.assertEqual(len(failed), 1)  # failure evidence kept
            s.feed("someone", "unrelated question", external_id="x-1")
        seen = Brain()
        self.assertEqual(len(Engine(self.home, seen).run(max_turns=3)), 1)
        ctx = seen.contexts[0]
        self.assertEqual([e["kind"] for e in ctx["events"]], ["message"])
        due = [d for d in ctx["routines_due"] if d["work_id"] == wid]
        self.assertEqual(due[0]["state"], "quarantined")
        after = self.work(wid)
        self.assertEqual(len(after["routine"]["failures"]), 1)  # the unrelated run added no failure

    def test_rearm_from_a_later_session_consumes_the_held_input(self):
        wid = self.seed(wake_in=-1)
        with self.assertRaises(RuntimeError):
            Engine(self.home, Brain(fail="boom")).run(max_turns=1)
        with Store(self.home) as s:
            s.feed("someone", "hello", external_id="x-2")

        def repair(context):
            due = [d for d in context["routines_due"] if d["work_id"] == wid][0]
            with Store(self.home, run_id=context["run"]["id"]) as sess:
                sess.routine_rearm(wid, "missed cycle reviewed", cycle=due["cycle"])
        Engine(self.home, Brain(repair)).run(max_turns=1)
        with Store(self.home) as s:
            self.assertEqual([e for e in s.event_pending() if e["kind"] == "work_due"], [])
            self.assertEqual(s.work_get(wid)["status"], "waiting")
        self.assertEqual(Engine(self.home, Brain()).run(max_turns=2), [])


class F2CompareAndSet(Base):
    def test_repeat_and_stale_rearm_refused_at_every_5(self):
        wid = self.seed(every=5, wake_in=3)  # due-soon inside the 15-min fold window
        with Store(self.home) as s:
            run = s.run_begin()
        with Store(self.home, run_id=run["id"]) as sess:
            due = sess.routines_due(run)[0]
            first = sess.routine_rearm(wid, "reviewed", cycle=due["cycle"])
            self.assertEqual(first["wake_at"], due["rearm_wake_at"])
            # The new target is again inside the fold window (every=5 < 15): a repeat with the old cycle is refused.
            self.assertEqual(routines.due_state(first), "due-soon")
            with self.assertRaisesRegex(ValueError, "stale re-arm"):
                sess.routine_rearm(wid, "reviewed again", cycle=due["cycle"])
            with self.assertRaisesRegex(ValueError, "cycle is required"):
                sess.routine_rearm(wid, "no cycle")
            self.assertEqual(sess.work_get(wid)["wake_at"], first["wake_at"])  # applied once
        with Store(self.home) as s:
            s.run_finish(run["id"], "done")
            audits = [a for a in s.audit_list(200) if a["action"] == "routine.rearmed"]
            self.assertEqual(len(audits), 1)


class F3UnknownOmission(Base):
    def test_omission_without_from_blocks_advance(self):
        with Store(self.home) as s:
            wid = s.routine_seed(["dream"], dream_at=(now() + timedelta(minutes=5)).strftime("%H:%M"))["created"][0]["work_id"]
            run = s.run_begin()
        with Store(self.home, run_id=run["id"]) as sess:
            cycle = sess.work_get(wid)["routine"]["anchor_at"]
            unknown = [{"reason": "memory_search hit its 200 cap; oldest unread unknown"}]
            with self.assertRaisesRegex(ValueError, "no known earliest unread time"):
                sess.routine_rearm(wid, "r", cycle=cycle, covered_through=run["started_at"], omissions=unknown)
            out = sess.routine_rearm(wid, "partial dream", cycle=cycle, omissions=unknown)
            self.assertIsNone(out["routine"]["covered_through"])
            self.assertFalse(out["routine"]["coverage"]["advanced"])
            self.assertEqual(out["routine"]["coverage"]["omissions"], unknown)
        with Store(self.home) as s:
            s.run_finish(run["id"], "done")


class F4OfficialRoster(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.mgr, self.dev = root / "mgr", root / "dev"
        self.ids = {}
        for home in (self.mgr, self.dev):
            with Store(home) as s:
                self.ids[home.name] = s.initialize(home.name, "employee")["id"]
        env = {k: v for k, v in os.environ.items() if k not in (authority.SESSION_ENV, authority.HOME_ENV)}
        for argv in (["put", self.ids["mgr"], "--name", "Manager", "--kind", "employee"],
                     ["put", self.ids["dev"], "--name", "Dev", "--kind", "employee"],
                     ["alias", self.ids["mgr"], "boss"]):
            with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(io.StringIO()):
                code = main(["--home", str(self.mgr), "roster", "company", *argv, "--by", "operator",
                             "--source", "test"])
            self.assertEqual(code, 0, argv)
        for home in (self.mgr, self.dev):
            (home / "runtime.json").write_text(json.dumps({"limit_notify": "boss"}))
        capture.write_humans(self.mgr, {"default_human": "owner-x", "senders": ["owner-x"], "channel_prefixes": []})

    def tearDown(self):
        self.tmp.cleanup()

    def test_alias_resolves_through_company_fold(self):
        with Store(self.mgr) as s:
            route = s.routine_route()
        self.assertEqual((route["manager"], route["self_is_manager"], route["report_to"], route["via"]),
                         (self.ids["mgr"], True, "owner-x", "limit_notify+roster"))
        with Store(self.dev) as s:
            route = s.routine_route()
        self.assertEqual((route["manager"], route["self_is_manager"], route["report_to"]),
                         (self.ids["mgr"], False, self.ids["mgr"]))

    def test_private_view_alias_does_not_route(self):
        # Only the official fold canonicalises: an unknown alias stays the raw configured name.
        (self.dev / "runtime.json").write_text(json.dumps({"limit_notify": "chief"}))
        with Store(self.dev) as s:
            route = s.routine_route()
        self.assertEqual((route["manager"], route["via"]), ("chief", "limit_notify"))

    def test_manager_notifying_owner_reports_to_owner(self):
        (self.mgr / "runtime.json").write_text(json.dumps({"limit_notify": "owner-x"}))
        with Store(self.mgr) as s:
            route = s.routine_route()
        self.assertEqual((route["resolved"], route["report_to"], route["self_is_manager"]), (True, "owner-x", False))


class G1FirstWake(Base):
    def test_first_honoured_on_create_ignored_on_existing(self):
        t0 = now()
        with Store(self.home) as s:
            out = s.routine_seed(["hourly-check"], first={"hourly-check": "+2"}, now=timestamp(t0))
            wid = out["created"][0]["work_id"]
            work = s.work_get(wid)
            self.assertEqual(work["wake_at"], timestamp(t0 + timedelta(minutes=2)))
            self.assertEqual(work["routine"]["anchor_at"], timestamp(t0))  # regular grid: first rearm -> t0+60
            again = s.routine_seed(["hourly-check"], first={"hourly-check": "+3"}, now=timestamp(t0))
            self.assertEqual(again["created"], [])
            self.assertTrue(again["existing"][0]["first_ignored"])
            self.assertEqual(s.work_get(wid)["wake_at"], work["wake_at"])
            s.due_events(now=timestamp(t0 + timedelta(minutes=3)))
            run = s.run_begin()
        with Store(self.home, run_id=run["id"]) as sess:
            out = sess.routine_rearm(wid, "first live cycle", cycle=work["routine"]["anchor_at"])
        self.assertEqual(out["wake_at"], timestamp(t0 + timedelta(minutes=60)))
        with Store(self.home) as s:
            s.run_finish(run["id"], "done")

    def test_dream_after_early_first_goes_to_next_dream_at(self):
        t0 = now().replace(second=0, microsecond=0)
        with Store(self.home) as s:
            out = s.routine_seed(["dream"], first={"dream": "+2"}, now=timestamp(t0))
            wid = out["created"][0]["work_id"]
            work = s.work_get(wid)
            s.due_events(now=timestamp(t0 + timedelta(minutes=3)))
            run = s.run_begin()
        regular = routines.first_wake("dream", 1440, t0, "01:30")
        with Store(self.home, run_id=run["id"]) as sess:
            out = sess.routine_rearm(wid, "first dream", cycle=work["routine"]["anchor_at"])
        self.assertEqual(out["wake_at"], timestamp(regular))
        self.assertTrue(out["wake_at"].endswith("T01:30:00.000000Z") or "01:30:00" in out["wake_at"])
        with Store(self.home) as s:
            s.run_finish(run["id"], "done")

    def test_out_of_range_first_refused(self):
        t0 = now()
        with Store(self.home) as s:
            for bad in ("+0", "+61", timestamp(t0 - timedelta(minutes=5)), "soon"):
                with self.assertRaisesRegex(ValueError, "--first"):
                    s.routine_seed(["hourly-check"], first={"hourly-check": bad}, now=timestamp(t0))
            self.assertEqual([w for w in s.work_list() if w.get("routine")], [])

    def test_cli_first_flag(self):
        env = {k: v for k, v in os.environ.items() if k not in (authority.SESSION_ENV, authority.HOME_ENV)}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(out):
            code = main(["--home", str(self.home), "routine", "seed", "--kinds", "hourly-check",
                         "--first", "hourly-check=+5"])
        self.assertEqual(code, 0)
        created = json.loads(out.getvalue())["created"][0]
        delta = routines.parse(created["wake_at"]) - now()
        self.assertTrue(timedelta(minutes=3) < delta <= timedelta(minutes=5), delta)


if __name__ == "__main__":
    unittest.main()
