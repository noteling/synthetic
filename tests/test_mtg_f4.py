"""MTG F4 (Codex rulings R2-R4 on David's answer, 23:07Z 11-19): meetings are opt-in per home (floor.json, off by
default, refused before any pending row or yield); doctor shows the opt-in; the wrap session's one `meeting summary`
memory is checked by floor verify (present/missing/duplicate/unanchored); the journal has no automatic expiry.
Fail-first on main a6c3689; the guards are labelled."""

import json
from datetime import datetime, timezone
import unittest
from pathlib import Path

from synthetics import doctor, floor
from synthetics import floorjournal as fj
from synthetics.store import Store
from tests import test_mtg_f1_yield as f1  # module imports: never re-collect the F1/F3 test classes here
from tests import test_mtg_f3_gate as f3

flooraccept = f3.flooraccept
ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2099, 1, 1, tzinfo=timezone.utc)


class GateBase(f1.Base):
    attach = f1.C4Attach.attach

    def setUp(self):
        super().setUp()
        (self.home / "floor.json").unlink()   # f1.Base opts the home in; these tests start from the default


class G1Gate(GateBase):
    def assert_refused_untouched(self, state):
        with Store(self.home) as store:
            running = store.run_begin()["id"]                     # a running session that must not be yielded
        rc, out = self.attach([{"type": "begin", "meeting": "m1", "grace": 0}])
        self.assertEqual(rc, 2)
        self.assertEqual(out, [dict(v=1, type="error", code="not_enabled", message=out[0]["message"])])
        self.assertIn(state, out[0]["message"])
        with Store(self.home) as store:
            self.assertIsNone(store.meeting_pending())
            run = store.run_get(running)
        self.assertEqual(run["status"], "running")
        self.assertNotIn("yield", run)
        actions = [a[0] for a in self.audits()]
        self.assertEqual(actions, ["floor.refused"])
        self.assertEqual(self.audits()[0][2]["code"], "not_enabled")

    def test_absent_is_refused_before_any_pending_row_or_yield(self):
        self.assert_refused_untouched("absent")

    def test_disabled_is_refused(self):
        (self.home / "floor.json").write_text('{"enabled": false}')
        self.assert_refused_untouched("disabled")

    def test_unreadable_is_refused(self):
        (self.home / "floor.json").write_text('{"enabled": tru')
        self.assert_refused_untouched("unreadable")

    def test_enabled_attaches(self):  # guard: the opt-in lets the F1 flow run
        (self.home / "floor.json").write_text('{"enabled": true}')
        rc, out = self.attach([{"type": "begin", "meeting": "m1"}, {"type": "end"}])
        self.assertEqual(rc, 0)
        self.assertEqual([o["type"] for o in out], ["ready", "ended"])

    def audits(self):
        with Store(self.home) as store:
            rows = store.db.execute("SELECT action, record_id, data FROM audit ORDER BY seq").fetchall()
        return [(r["action"], r["record_id"], json.loads(r["data"])) for r in rows if r["action"].startswith("floor.")]


class G1State(unittest.TestCase):
    def test_states(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.assertEqual(floor.opt_in_state(home), "absent")
            for text, state in (('{"enabled": true}', "enabled"), ('{"enabled": "yes"}', "disabled"),
                                ('{}', "disabled"), ('[true]', "unreadable"), ('nope', "unreadable")):
                (home / "floor.json").write_text(text)
                self.assertEqual(floor.opt_in_state(home), state, text)
                self.assertEqual(floor.enabled(home), state == "enabled")


class G1Doctor(GateBase):
    def report(self):
        return doctor.diagnose(self.home, launchd=lambda label: (False, None))

    def test_doctor_shows_the_opt_in(self):
        self.assertEqual(self.report()["floor"], {"floor_json": "absent", "meetings": False})
        (self.home / "floor.json").write_text('{"enabled": true}')
        self.assertEqual(self.report()["floor"], {"floor_json": "enabled", "meetings": True})
        self.assertIn("floor:", doctor.render(self.report()))

    def test_doctor_warns_when_unreadable(self):
        (self.home / "floor.json").write_text("{")
        report = self.report()
        self.assertEqual(report["floor"], {"floor_json": "unreadable", "meetings": False})
        self.assertIn(("warning", "floor_json_unreadable"), [(f["level"], f["code"]) for f in report["findings"]])


class G3Summary(f3.Base):
    def wrap_with(self, calls):
        return f3.G2Verify.run_with(self, calls, summary=False)

    def summary(self, **change):
        name, args = self.summary_call()
        args = dict(args, **change)
        return name, args

    def kinds(self, result):
        return [(v["kind"], v.get("summary")) for v in result["violations"]]

    def test_present_is_clean(self):
        self.promise_meeting()
        result = flooraccept.verify(self.home, self.wrap_with([self.summary()])[0])
        self.assertEqual(result["verdict"], "clean", result)
        self.assertEqual(result["meetings"][0]["summary"], "present")

    def test_missing(self):
        self.promise_meeting()
        with Store(self.home) as store:
            store.entity_put("meetings", "Meetings", "concept")
        result = flooraccept.verify(self.home, self.wrap_with([("memory_put", {
            "subject": "meetings", "predicate": "note", "value": "unrelated", "source": "event-x"})])[0])
        self.assertEqual(self.kinds(result), [("summary", "missing")])
        self.assertEqual(result["meetings"][0]["summary"], "missing")

    def test_duplicate(self):
        self.promise_meeting()
        result = flooraccept.verify(self.home, self.wrap_with([self.summary(), self.summary(value="m1 again, "
                                                                                       "meetings/m1")])[0])
        self.assertEqual(self.kinds(result), [("summary", "duplicate")])
        self.assertEqual(result["violations"][0]["receipt_lines"], [1, 2])

    def test_unanchored_source_not_the_wrap_event(self):
        self.promise_meeting()
        result = flooraccept.verify(self.home, self.wrap_with([self.summary(source="meeting m1")])[0])
        self.assertEqual(self.kinds(result), [("summary", "unanchored")])

    def test_unanchored_without_the_journal_path_or_head(self):
        cases = (lambda v, h: v.replace("/journal.jsonl", ""), lambda v, h: v.replace(h, h[:8]),
                 lambda v, h: v.replace(h, "0" * 64))
        for number, change in enumerate(cases):
            meeting = "u%d" % number          # one meeting and wrap run per case: a wrap event is captured once
            self.promise_meeting(meeting=meeting)
            _, args = self.summary()
            value = change(args["value"], fj.head(self.home, meeting)["head_sha256"])
            result = flooraccept.verify(self.home, self.wrap_with([self.summary(value=value)])[0])
            self.assertEqual(self.kinds(result), [("summary", "unanchored")], value)

    def test_head_prefix_of_twelve_is_enough_and_predicate_case_ignored(self):
        self.promise_meeting()
        _, args = self.summary()
        head = fj.head(self.home, "m1")["head_sha256"]
        result = flooraccept.verify(self.home, self.wrap_with([self.summary(
            predicate=" Meeting Summary ", value=args["value"].replace(head, head[:12]))])[0])
        self.assertEqual(result["verdict"], "clean", result)

    def test_other_predicates_and_other_meetings_do_not_count(self):  # guard against over-matching
        self.promise_meeting()
        result = flooraccept.verify(self.home, self.wrap_with([
            self.summary(predicate="meeting notes"),
            self.summary(source="event-0000000000", value="Meeting m2: meetings/m2/journal.jsonl"),
        ])[0])
        self.assertEqual(self.kinds(result), [("summary", "missing")])


class G2Retention(f3.Base):
    def test_journal_survives_every_runs_prune_bound(self):  # guard (R3: no automatic expiry), passes on base
        self.promise_meeting()
        journal = fj.path(self.home, "m1")
        before = Path(journal).read_bytes()
        with Store(self.home) as store:
            store.runs_prune(keep_days=1, max_mb=0.000001, keep_newest=0, now=NOW)
        self.assertEqual(Path(journal).read_bytes(), before)


class G3Prompt(unittest.TestCase):
    def test_wrap_guidance_asks_for_one_anchored_summary(self):
        text = (ROOT / "synthetics" / "prompts" / "employee.md").read_text(encoding="utf-8")
        block = text[text.index("Meetings (a `stand:meeting`"):]
        self.assertIn("exactly one memory_put with predicate `meeting summary`", block)
        self.assertIn("meetings/<id>/journal.jsonl", block)
        self.assertIn("wrap head sha256", block)


if __name__ == "__main__":
    unittest.main()
