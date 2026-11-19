"""RTN P1: routines on self-reminders (docs/routines.md, design reviews/RTN/P0-r1 + P1 delta).

Covers P0-r1 §3 (a)-(d) absorb-never-skip, §4 quarantine (D1), the work_put refusal, routine_rearm validation
including the dream checkpoint (E1), manager routing (E2) and CLI session fencing (E3)."""
import contextlib, io, json, os, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from synthetics import authority, capture, routines
from synthetics.cli import main
from synthetics.mcp import METHODS, Server
from synthetics.store import Store, timestamp


def now():
    return datetime.now(timezone.utc)


def rearm(sess, work_id, review, **kw):
    """routine_rearm with the cycle routines_due shows (addendum (a) compare-and-set)."""
    kw.setdefault("cycle", (sess.work_get(work_id).get("routine") or {}).get("anchor_at") or "-")
    return sess.routine_rearm(work_id, review, **kw)


class RoutineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "a"
        self.s = Store(self.home)
        self.me = self.s.initialize("A", "developer")["id"]

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def seed(self, kind="hourly-check", every=60, wake_in=10, store=None):
        """Seed so that the first wake is wake_in minutes from now."""
        moment = now() + timedelta(minutes=wake_in - every)
        out = (store or self.s).routine_seed([kind], every={kind: every}, now=timestamp(moment))
        work = self.s.work_get(out["created"][0]["work_id"])
        return work, routines.parse(work["wake_at"])

    def session(self):
        run = self.s.run_begin()
        return run, Store(self.home, run_id=run["id"])

    def end(self, run, sess):
        sess.close()
        return self.s.run_finish(run["id"], "done")

    def fire_and_capture(self):
        work, wake = self.seed(wake_in=10)
        self.fired(wake + timedelta(seconds=1))
        run, sess = self.session()
        self.assertIn(work["id"], [e.get("work_id") for e in run["events"]])
        return work, run, sess

    def fired(self, at):
        self.s.due_events(now=timestamp(at))
        return [e for e in self.s.event_pending() if e["kind"] == "work_due"]


class AbsorbNeverSkip(RoutineCase):
    def test_a_recent_session_before_window_does_not_suppress(self):
        work, wake = self.seed(wake_in=30)
        run, sess = self.session()
        self.assertEqual(sess.context()["routines_due"], [])  # not due within 15 min
        self.end(run, sess)
        due = self.fired(wake)
        self.assertEqual([e["work_id"] for e in due], [work["id"]])
        after = self.s.work_get(work["id"])
        self.assertEqual(after["status"], "ready")
        self.assertIsNone(after["routine"]["rearmed_by_run"])
        self.assertIsNotNone(after["routine"]["last_fired_at"])

    def test_b_session_inside_window_without_rearm_still_fires(self):
        work, wake = self.seed(wake_in=10)
        run, sess = self.session()
        due = sess.context()["routines_due"]
        self.assertEqual([(d["work_id"], d["state"]) for d in due], [(work["id"], "due-soon")])
        self.assertEqual(due[0]["rearm_wake_at"], timestamp(wake + timedelta(minutes=60)))
        self.assertIn("own open work", due[0]["guidance"])
        self.end(run, sess)
        self.assertEqual(self.s.work_get(work["id"])["status"], "waiting")
        self.assertEqual([e["work_id"] for e in self.fired(wake)], [work["id"]])

    def test_c_session_inside_window_that_rearms_moves_to_next_cycle(self):
        work, wake = self.seed(wake_in=10)
        run, sess = self.session()
        out = rearm(sess, work["id"], "checked 3 works; no change, waits justified")
        self.assertEqual(out["wake_at"], timestamp(wake + timedelta(minutes=60)))
        self.assertEqual(out["routine"]["rearmed_by_run"], run["id"])
        self.assertEqual(out["routine"]["last_review"]["state"], "due-soon")
        self.end(run, sess)
        self.assertEqual(self.fired(wake), [])
        self.assertEqual(self.s.work_get(work["id"])["status"], "waiting")
        self.assertEqual([e["work_id"] for e in self.fired(wake + timedelta(minutes=60))], [work["id"]])

    def test_d_firing_leaves_other_works_and_question_links_intact(self):
        work, wake = self.seed(wake_in=10)
        later = timestamp(now() + timedelta(days=2))
        other = self.s.work_put("eng", status="waiting", wake_at=later)
        asked = self.s.question_ask("bob", "decide?", work_id=other["id"], key="q1")
        before = self.s.work_get(other["id"])
        self.assertEqual(before["waiting_question_ids"], [asked["id"]])
        self.fired(wake)
        after = self.s.work_get(other["id"])
        for field in ("status", "wake_at", "waiting_question_id", "waiting_question_ids", "updated_at"):
            self.assertEqual(after[field], before[field], field)


class Quarantine(RoutineCase):
    def assert_quarantined(self, work_id, failures=1):
        work = self.s.work_get(work_id)
        self.assertEqual(work["status"], "ready")
        self.assertTrue(work["routine"]["quarantine"])
        self.assertEqual(len(work["routine"]["failures"]), failures)
        self.assertNotIn(work_id, [w["id"] for w in self.s.runnable_work()])
        due = self.s.routines_due()
        self.assertEqual([(d["work_id"], d["state"]) for d in due], [(work_id, "quarantined")])
        codes = [f["code"] for f in routines.flags(self.s.work_list())]
        self.assertIn("routine_quarantined", codes)
        self.assertIn("routine_failures", codes)

    def test_self_rearm_records_no_failure(self):
        work, run, sess = self.fire_and_capture()
        rearm(sess, work["id"], "reviewed; next step recorded on eng work")
        self.end(run, sess)
        after = self.s.work_get(work["id"])
        self.assertEqual(after["status"], "waiting")
        self.assertIsNone(after["routine"]["quarantine"])
        self.assertEqual(after["routine"]["failures"], [])

    def test_finished_run_left_ready_is_quarantined_then_cleared_by_rearm(self):
        work, run, sess = self.fire_and_capture()
        self.end(run, sess)
        self.assert_quarantined(work["id"])
        # No invented wake: nothing to run while no event is pending.
        self.assertEqual(self.s.runnable_work(), [])
        run2, sess2 = self.session()
        self.assertEqual(sess2.context()["routines_due"][0]["state"], "quarantined")
        out = rearm(sess2, work["id"], "missed last cycle; reviewed now")
        self.end(run2, sess2)
        self.assertIsNone(out["routine"]["quarantine"])
        self.assertEqual(len(out["routine"]["failures"]), 1)  # never removed
        self.assertEqual(out["status"], "waiting")

    def test_failed_run_is_quarantined(self):
        work, run, sess = self.fire_and_capture()
        sess.close()
        self.s.run_fail(run["id"], "timeout after 1800 s")
        self.assert_quarantined(work["id"])
        self.assertIn("failed", self.s.work_get(work["id"])["routine"]["quarantine"]["reason"])

    def test_interrupted_run_is_quarantined(self):
        work, run, sess = self.fire_and_capture()
        sess.close()
        self.assertEqual(self.s.recover_runs(), 1)
        self.assert_quarantined(work["id"])

    def test_restore_clears_quarantine_keeps_failures(self):
        work, run, sess = self.fire_and_capture()
        self.end(run, sess)
        out = self.s.routine_restore("hourly-check")
        self.assertIsNone(out["routine"]["quarantine"])
        self.assertEqual(len(out["routine"]["failures"]), 1)
        self.assertEqual(out["status"], "waiting")
        self.assertGreater(out["wake_at"], timestamp())


class Signals(RoutineCase):
    """§4/§5 discipline signal: doctor, shared/status.json and Lookout show the quarantine and failures."""

    def test_doctor_status_and_lookout(self):
        from synthetics import doctor, lookout, sharedstatus
        work, run, sess = self.fire_and_capture()
        self.end(run, sess)
        report = doctor.diagnose(self.home, launchd=lambda label: (False, None))
        codes = {f["code"]: f for f in report["findings"]}
        self.assertEqual(codes["routine_quarantined"]["level"], "error")
        self.assertIn("routine_failures", codes)
        self.assertIn("no manager resolved", codes["routine_no_manager"]["detail"])
        self.assertFalse(report["ok"])
        self.assertTrue(sharedstatus.write(self.s))
        value = sharedstatus.read(self.home)
        row = [w for w in value["works"] if w["id"] == work["id"]][0]
        self.assertEqual((row["routine"]["kind"], row["routine"]["quarantined"], row["routine"]["failures"]),
                         ("hourly-check", True, 1))
        self.assertIn("routine_quarantined", [f["code"] for f in value["routine_flags"]])
        page = lookout.team_page(self.home.parent, agents={})
        self.assertIn("<b>Routines</b>", page)
        self.assertIn("routine_quarantined", page)

    def test_review_text_stays_private(self):
        from synthetics import sharedstatus
        work, run, sess = self.fire_and_capture()
        rearm(sess, work["id"], "SENTINEL-REVIEW private reasoning")
        self.end(run, sess)
        sharedstatus.write(self.s)
        self.assertNotIn("SENTINEL-REVIEW", sharedstatus.path(self.home).read_text())

    def test_left_ready_and_unarmed_flags(self):
        stale = (now() - timedelta(minutes=10))
        base = dict(id="work-x", status="ready", updated_at=timestamp(stale),
                    routine=dict(kind="hourly-check", every_minutes=60, last_fired_at=timestamp(stale)))
        self.assertEqual([f["code"] for f in routines.flags([base])], ["routine_left_ready"])
        self.assertEqual(routines.flags([base], running=True), [])
        unarmed = dict(base, status="waiting", wake_at=None)
        self.assertEqual([f["code"] for f in routines.flags([unarmed])], ["routine_unarmed"])
        overdue = dict(base, status="waiting", wake_at=timestamp(now() - timedelta(minutes=45)))
        self.assertEqual([f["code"] for f in routines.flags([overdue])], ["routine_overdue"])
        closed = dict(base, status="done")
        self.assertIn("routine restore hourly-check", routines.flags([closed])[0]["detail"])


class RearmRules(RoutineCase):
    def test_work_put_cannot_change_status_or_wake(self):
        work, wake = self.seed(wake_in=10)
        run, sess = self.session()
        for change in (dict(status="done"), dict(status="cancelled", cancel_reason="x"), dict(status="ready"),
                       dict(wake_at=timestamp(now() + timedelta(hours=5)))):
            with self.assertRaisesRegex(ValueError, "routine_rearm"):
                sess.work_put(id=work["id"], **change)
        with self.assertRaisesRegex(ValueError, "routine_rearm"):
            self.s.work_put(id=work["id"], status="done")  # the owner's CLI path too
        sess.work_put(id=work["id"], note="a note is fine")
        self.end(run, sess)

    def test_rearm_refused_when_not_due_or_outside_session(self):
        work, _ = self.seed(wake_in=50)
        run, sess = self.session()
        with self.assertRaisesRegex(ValueError, "not due"):
            rearm(sess, work["id"], "early")
        with self.assertRaisesRegex(ValueError, "review"):
            rearm(sess, work["id"], "  ")
        self.end(run, sess)
        with self.assertRaisesRegex(ValueError, "session tool"):
            self.s.routine_rearm(work["id"], "operator", cycle=work["routine"]["anchor_at"])
        other = self.s.work_put("eng")
        run, sess = self.session()
        with self.assertRaisesRegex(ValueError, "not a routine"):
            rearm(sess, other["id"], "x")
        self.end(run, sess)

    def test_seed_is_idempotent_and_session_cannot_seed(self):
        first = self.s.routine_seed(["hourly-check"])
        again = self.s.routine_seed(["hourly-check"])
        self.assertEqual(len(first["created"]), 1)
        self.assertEqual(again["created"], [])
        self.assertEqual(again["existing"][0]["work_id"], first["created"][0]["work_id"])
        with self.assertRaisesRegex(ValueError, "every"):
            self.s.routine_seed(["team-review"], every={"team-review": 4})
        run, sess = self.session()
        for call in (lambda: sess.routine_seed(["team-review"]), lambda: sess.routine_restore("hourly-check"),
                     lambda: sess.routine_teardown(), lambda: sess.routine_cadence("hourly-check", 30)):
            with self.assertRaisesRegex(ValueError, "CLI action"):
                call()
        self.end(run, sess)

    def test_teardown_leaves_no_closed_flag_and_seed_recreates(self):
        first = self.s.routine_seed(["hourly-check"])["created"][0]["work_id"]
        self.assertEqual(self.s.routine_teardown(reason="worker remove")["torn_down"][0]["work_id"], first)
        self.assertEqual(self.s.work_get(first)["status"], "cancelled")
        self.assertEqual([f for f in routines.flags(self.s.work_list()) if f["code"] == "routine_closed"], [])
        self.assertEqual(len(self.s.routine_seed(["hourly-check"])["created"]), 1)

    def test_mcp_tool_and_snapshot_field(self):
        self.assertIn("routine_rearm", METHODS)
        self.assertEqual(self.s.context()["routines_due"], [])
        work, _ = self.seed(wake_in=5)
        run, sess = self.session()
        out = Server(sess, run["id"]).call("routine_rearm", {"work_id": work["id"], "review": "no change: waits justified", "cycle": work["routine"]["anchor_at"]})
        self.assertEqual(out["routine"]["rearmed_by_run"], run["id"])
        self.end(run, sess)


class DreamCheckpoint(RoutineCase):
    """E1/D2: routine_rearm's store-side validation of covered_through (P2 adds the dream guidance)."""

    def dream(self):
        dream_at = (now() + timedelta(minutes=5)).strftime("%H:%M")
        out = self.s.routine_seed(["dream"], dream_at=dream_at)
        return out["created"][0]["work_id"]

    def test_cutoff_and_omission_bounds(self):
        wid = self.dream()
        run, sess = self.session()
        cutoff = routines.parse(run["started_at"])
        self.assertEqual(sess.context()["routines_due"][0]["evidence_cutoff"], run["started_at"])
        with self.assertRaisesRegex(ValueError, "evidence cutoff"):
            rearm(sess, wid, "r", covered_through=timestamp(cutoff + timedelta(minutes=1)))
        omission = [{"reason": "event_search capped at 50 before the cutoff", "from": timestamp(cutoff - timedelta(hours=2))}]
        with self.assertRaisesRegex(ValueError, "unread material"):
            rearm(sess, wid, "r", covered_through=timestamp(cutoff - timedelta(hours=1)), omissions=omission)
        with self.assertRaisesRegex(ValueError, "reason"):
            rearm(sess, wid, "r", omissions=[{"from": timestamp(cutoff)}])
        through = timestamp(cutoff - timedelta(hours=2))
        out = rearm(sess, wid, "partial: read up to the capped search", covered_through=through,
                                 omissions=omission)
        self.assertEqual(out["routine"]["covered_through"], through)
        self.assertEqual(out["routine"]["coverage"]["omissions"][0]["reason"], omission[0]["reason"])
        self.assertEqual(out["routine"]["coverage"]["cutoff"], run["started_at"])
        self.end(run, sess)
        # Make it due again (test only) and check the checkpoint never moves back, and omitting T keeps it.
        work = self.s.work_get(wid)
        work["wake_at"] = timestamp(now() + timedelta(minutes=5))
        work["routine"] = dict(work["routine"], anchor_at=work["wake_at"])
        with self.s._tx():
            self.s._save("works", work, "test.due")
        run, sess = self.session()
        with self.assertRaisesRegex(ValueError, "never moves"):
            rearm(sess, wid, "r", covered_through=timestamp(cutoff - timedelta(hours=3)))
        out = rearm(sess, wid, "dream failed to finish the window")
        self.assertEqual(out["routine"]["covered_through"], through)
        self.end(run, sess)

    def test_covered_through_only_for_dream(self):
        work, _ = self.seed(wake_in=5)
        run, sess = self.session()
        with self.assertRaisesRegex(ValueError, "dream routine only"):
            rearm(sess, work["id"], "r", covered_through=run["started_at"])
        self.end(run, sess)


class ManagerRoute(RoutineCase):
    """E2: no hardcoded IDs; route from limit_notify (+ roster); explicit unresolved result."""

    def test_no_manager_resolved_is_explicit(self):
        out = self.s.routine_seed(["hourly-check"])
        self.assertFalse(out["route"]["resolved"])
        self.assertIn("no manager resolved", out["route"]["reason"])
        work, _ = self.seed(kind="team-review", wake_in=5)
        due = self.s.routines_due()
        self.assertIn("no manager resolved", due[0]["route_problem"])
        self.assertIsNone(due[0]["report_to"])

    def test_manager_from_configured_limit_notify(self):
        (self.home / "runtime.json").write_text(json.dumps({"limit_notify": "boss"}))
        route = self.s.routine_seed(["hourly-check"])["route"]
        self.assertEqual((route["resolved"], route["manager"], route["report_to"], route["via"]),
                         (True, "boss", "boss", "limit_notify"))

    def test_this_home_is_manager_reports_to_default_human(self):
        (self.home / "runtime.json").write_text(json.dumps({"limit_notify": self.me}))
        capture.write_humans(self.home, {"default_human": "dana", "senders": ["dana"], "channel_prefixes": []})
        route = self.s.routine_route()
        self.assertTrue(route["self_is_manager"])
        self.assertEqual(route["report_to"], "dana")

    def test_no_hardcoded_identities_in_routine_code_or_guidance(self):
        for path in (Path(routines.__file__), routines.GUIDANCE_FILE):
            text = path.read_text(encoding="utf-8")
            for needle in ("employee-", "codex", "vesper", "rowan", "david"):
                self.assertNotIn(needle, text.lower(), "%s mentions %s" % (path.name, needle))


class SessionFencing(unittest.TestCase):
    """E3: seed/cadence/restore/teardown are owner-only; a session never seeds or restores another home."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.a, self.b = root / "a", root / "b"
        for home in (self.a, self.b):
            with Store(home) as s:
                s.initialize(home.name, "developer")
        with Store(self.b) as s:
            s.routine_seed(["hourly-check"])
        with Store(self.a) as s:
            self.run = s.run_begin()["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, home, *argv, session=True):
        environ = {k: v for k, v in os.environ.items() if k not in (authority.SESSION_ENV, authority.HOME_ENV)}
        if session:
            environ[authority.SESSION_ENV] = self.run
            environ[authority.HOME_ENV] = str(self.a)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, environ, clear=True), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = main(["--home", str(home), "routine", *argv])
        return code, out.getvalue(), err.getvalue()

    def refusals(self, home):
        with Store(home) as s:
            return [json.loads(r["data"]) for r in
                    s.db.execute("SELECT data FROM audit WHERE action='authority.refused' ORDER BY seq")]

    def routines_in(self, home):
        with Store(home) as s:
            return [(w["routine"]["kind"], w["status"]) for w in s.work_list() if w.get("routine")]

    def test_session_cannot_seed_or_restore_another_home(self):
        before = self.routines_in(self.b)
        # G2 ruled owner-only (Codex event-d14945da): no delegated path.
        for argv in (("seed", "--kinds", "team-review"), ("restore", "hourly-check"),
                     ("cadence", "hourly-check", "30"), ("teardown", "--all")):
            code, out, err = self.cli(self.b, *argv)
            self.assertNotEqual(code, 0, out)
            self.assertIn("owner_only", err)
        self.assertEqual(self.routines_in(self.b), before)
        # n1 (P2): each refusal is audited in the session's own home (authority.py owner_only).
        refused = self.refusals(self.a)
        self.assertEqual(len(refused), 4)
        for row in refused:
            self.assertEqual((row["code"], row["run"]), ("owner_only", self.run))
        self.assertEqual(self.refusals(self.b), [])
        code, out, err = self.cli(self.b, "list")
        self.assertNotEqual(code, 0, out)
        self.assertIn("own_home_only", err)

    def test_session_on_own_home_may_list_but_not_seed(self):
        code, out, err = self.cli(self.a, "seed", "--kinds", "hourly-check")
        self.assertIn("owner_only", err)
        self.assertEqual(self.routines_in(self.a), [])
        code, out, err = self.cli(self.a, "list")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["routines"], [])

    def test_owner_seeds_team_review_and_dream(self):
        code, out, err = self.cli(self.b, "seed", "--kinds", "team-review", "--every", "team-review=30", session=False)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["created"][0]["every_minutes"], 30)
        # P2: the dream is seedable (P1 refused it until its guidance shipped).
        code, out, err = self.cli(self.b, "seed", "--kinds", "dream", session=False)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["created"][0]["kind"], "dream")


if __name__ == "__main__":
    unittest.main()
