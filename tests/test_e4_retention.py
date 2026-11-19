"""E4 runs/ retention (Codex Q2, C3; proposal test 7): age and size bounds, pins, running and newest runs kept,
only <run-id>.* regular files directly in runs/, dry run, audit, and nothing that reads costs changes."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from synthetics import doctor, ledgerline, lookout
from synthetics.store import Store
from tests.test_i17_export_verify import HomeCase, run_cli

LATER = (datetime.now(timezone.utc) + timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Retention(HomeCase):
    def make_runs(self, count, cost=0.5, status="finished"):
        folder = self.home / "runs"
        folder.mkdir(exist_ok=True)
        made = []
        for _ in range(count):
            run = self.store.run_begin()
            for suffix, body in ((".claude.json", json.dumps({"type": "result", "total_cost_usd": cost})),
                                 (".stderr.txt", ""), (".mcp.jsonl", '{"tool": "x"}\n'),
                                 (".stream.jsonl", '{"kind": "turn"}\n')):
                (folder / (run["id"] + suffix)).write_text(body)
            if status == "finished":
                self.store.run_finish(run["id"], "ok", cost=cost)
            elif status == "failed":
                self.store.run_fail(run["id"], "boom", cost=None)
            made.append(run["id"])
        return made

    def files(self, run_id):
        return sorted(p.name for p in (self.home / "runs").glob(run_id + ".*"))

    def audit_rows(self):
        return [json.loads(r["data"]) for r in self.store.db.execute(
            "SELECT data FROM audit WHERE action='runs.pruned' ORDER BY seq")]

    def test_age_bound_keeps_newest_50(self):
        runs = self.make_runs(55)
        result = self.store.runs_prune(now=LATER)
        self.assertEqual(result["runs"], 5)
        for run_id in runs[:5]:
            self.assertEqual(self.files(run_id), [])
            self.assertIn("receipt_chain", self.store.run_get(run_id))  # the row and its anchor stay
        for run_id in runs[5:]:
            self.assertEqual(len(self.files(run_id)), 4)
        self.assertEqual(self.audit_rows()[-1]["runs"], 5)

    def test_recent_runs_are_not_pruned_by_age(self):
        self.make_runs(55)
        self.assertEqual(self.store.runs_prune()["runs"], 0)
        self.assertEqual(self.audit_rows(), [])  # nothing removed: no row

    def test_size_bound_prunes_oldest_first(self):
        runs = self.make_runs(53)
        result = self.store.runs_prune(keep_days=3650, max_mb=0.000001)
        self.assertEqual(result["runs"], 3)  # everything over the bound, but never the newest 50
        self.assertEqual([r for r in runs if not self.files(r)], runs[:3])

    def test_each_pin_kind_is_kept(self):
        runs = self.make_runs(56)
        failed = self.make_runs(1, cost=None, status="failed")[0]
        work = "work-" + "e" * 32
        evidence = "work-" + "f" * 32
        with self.store._tx():
            self.store._save("works", {"id": work, "title": "Routine: dream", "status": "waiting", "routine": {
                "kind": "dream", "coverage": {"run": runs[0]}, "rearmed_by_run": runs[1],
                "last_review": {"run": runs[2]}}}, "work.created")
            self.store._save("works", {"id": evidence, "title": "t", "status": "done",
                                       "evidence": [{"ref": runs[3], "level": "observed"}]}, "work.created")
        result = self.store.runs_prune(now=LATER)
        for run_id in runs[:4] + [failed]:
            self.assertEqual(len(self.files(run_id)), 4, run_id)
        self.assertEqual(result["kept_pinned"], 5)
        self.assertEqual(result["runs"], 3)  # 57 runs: the oldest 7 are candidates, 4 of them pinned
        self.assertEqual([r for r in runs if not self.files(r)], runs[4:7])

    def test_running_run_kept(self):
        finished = self.make_runs(2)
        running = self.store.run_begin()
        (self.home / "runs" / (running["id"] + ".mcp.jsonl")).write_text("{}\n")
        result = self.store.runs_prune(now=LATER, keep_newest=0)
        self.assertEqual(result["runs"], 2)
        self.assertEqual([self.files(r) for r in finished], [[], []])
        self.assertEqual(self.files(running["id"]), [running["id"] + ".mcp.jsonl"])
        self.store.run_finish(running["id"], "ok")

    def test_only_regular_run_files_directly_in_runs(self):
        runs = self.make_runs(51)
        folder = self.home / "runs"
        outside = Path(self.temporary.name) / "outside.txt"
        outside.write_text("keep me")
        os.symlink(str(outside), str(folder / (runs[0] + ".link")))
        (folder / (runs[0] + ".dir")).mkdir()
        (folder / "notes.txt").write_text("not a run")
        (folder / ("run-" + "9" * 32 + ".claude.json")).write_text("{}")  # no such run row
        self.store.runs_prune(now=LATER)
        self.assertEqual(outside.read_text(), "keep me")
        self.assertTrue((folder / (runs[0] + ".link")).is_symlink())
        self.assertTrue((folder / (runs[0] + ".dir")).is_dir())
        self.assertTrue((folder / "notes.txt").exists())
        self.assertTrue((folder / ("run-" + "9" * 32 + ".claude.json")).exists())
        self.assertFalse((folder / (runs[0] + ".claude.json")).exists())

    def test_dry_run_writes_nothing(self):
        runs = self.make_runs(52)
        result = self.store.runs_prune(now=LATER, dry_run=True)
        self.assertEqual(result["would_remove"], runs[:2])
        self.assertEqual(len(self.files(runs[0])), 4)
        self.assertEqual(self.audit_rows(), [])

    def test_pruned_run_verifies_no_receipts_with_anchor_shown(self):
        runs = self.make_runs(51)
        self.store.runs_prune(now=LATER)
        verdict = ledgerline.verify_dream(self.home, runs[0])
        self.assertEqual(verdict["chain"], {"chain": "no_receipts"})
        self.assertEqual(verdict["verdict"], "no_evidence")
        self.assertIsNotNone(self.store.run_get(runs[0]).get("receipt_chain"))

    def test_c3_costs_lookout_doctor_and_recovery_unchanged(self):
        self.make_runs(52)
        self.make_runs(1, status="failed", cost=0.25)  # claude.json holds a cost the row lacks: pinned
        self.store.run_cost_backfill()
        def snapshot():
            agents = {}
            return {"usage": self.store.usage(days=60), "backfill": self.store.run_cost_backfill(),
                    "recover": self.store.recover_runs(),
                    "runs": [{k: r.get(k) for k in ("id", "status", "cost")} for r in self.store.run_list(limit=100)],
                    "team": lookout.team_page(self.home.parent, agents=agents, now=datetime.now(timezone.utc)),
                    "doctor": sorted(f["code"] for f in doctor.diagnose(self.home)["findings"])}
        before = snapshot()
        result = self.store.runs_prune(now=LATER)
        self.assertEqual(result["runs"], 3)
        self.assertEqual(snapshot(), before)

    def test_cli_owner_and_session_fence(self):
        self.make_runs(51)
        code, out, _ = run_cli("--home", str(self.home), "prune-runs", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["runs"], 0)  # recent: nothing by age
        other = Path(self.temporary.name) / "other"
        Store(other).initialize("Other", "x")
        run = self.store.run_begin()
        env = {"SYNTHETICS_SESSION_RUN": run["id"], "SYNTHETICS_HOME": str(self.home)}
        from synthetics.cli import main
        import contextlib, io
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--home", str(self.home), "prune-runs", "--dry-run"]), 0)
            self.assertNotEqual(main(["--home", str(other), "prune-runs", "--dry-run"]), 0)
        refused = [json.loads(r["data"]) for r in self.store.db.execute(
            "SELECT data FROM audit WHERE action='authority.refused'")]
        self.assertTrue(any(r.get("what") == "prune-runs" and r.get("code") == "own_home_only" for r in refused))
        self.store.run_finish(run["id"], "ok")


class WorkerIdle(HomeCase):
    def test_prunes_at_most_once_a_day(self):
        from synthetics.engine import Engine
        engine = Engine(self.home, brain=object())
        with mock.patch.object(Store, "runs_prune", autospec=True, return_value={"runs": 0}) as prune:
            engine.prune_runs_if_due()
            engine.prune_runs_if_due()
        self.assertEqual(prune.call_count, 1)

    def test_failure_never_raises(self):
        from synthetics.engine import Engine
        engine = Engine(self.home, brain=object())
        with mock.patch.object(Store, "runs_prune", side_effect=OSError("disk")):
            self.assertIsNone(engine.prune_runs_if_due())


if __name__ == "__main__":
    unittest.main()
