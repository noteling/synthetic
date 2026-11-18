import fcntl
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from scripts.experiment_followup import eligible_branches, initialize_snapshots, restore_case, run_followups, snapshot_case
from synthetics.store import Store


class FollowupSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "case"
        with Store(self.home) as store:
            store.initialize("Test worker", "Maintain the fixture")
            self.original = store.feed("david", "Original assignment", source="assignment")
        (self.home / "workspace" / "code.py").write_text("original\n")
        (self.home / "worker.json").write_text('{"success": true}\n')
        self.snapshot = self.root / "frozen"

    def test_snapshot_and_restore_preserve_db_files_modes_and_symlinks(self):
        artifact = self.home / "workspace" / "code.py"
        artifact.chmod(0o700)
        (self.home / "workspace" / "alias.py").symlink_to("code.py")
        metadata = snapshot_case(self.home, self.snapshot)
        self.assertEqual(metadata["employee_home"], str(self.home))
        self.assertIn("state.sqlite3", metadata["sqlite_databases"])
        with Store(self.home) as store:
            store.feed("david", "Branch-only event")
        artifact.write_text("branch\n")
        (self.home / "branch-only").write_text("delete on restore")
        outside = self.root / "saved-result.json"
        outside.write_text("preserved")
        restore_case(self.snapshot, self.home)
        self.assertEqual(artifact.read_text(), "original\n")
        self.assertEqual(artifact.stat().st_mode & 0o777, 0o700)
        self.assertTrue((self.home / "workspace" / "alias.py").is_symlink())
        self.assertFalse((self.home / "branch-only").exists())
        self.assertEqual(outside.read_text(), "preserved")
        with Store(self.home) as store:
            self.assertEqual([item["id"] for item in store.event_pending()], [self.original["id"]])
        # The second arm begins from precisely the same file and durable state.
        artifact.write_text("second branch\n")
        restore_case(self.snapshot, self.home)
        self.assertEqual(artifact.read_text(), "original\n")

    def test_snapshot_includes_committed_data_still_in_wal(self):
        store = Store(self.home)
        self.addCleanup(store.close)
        store.db.execute("PRAGMA wal_autocheckpoint=0")
        event = store.feed("david", "Committed after connection opened")
        self.assertTrue((self.home / "state.sqlite3-wal").exists())
        snapshot_case(self.home, self.snapshot)
        copied = self.snapshot / "home" / "state.sqlite3"
        with sqlite3.connect(copied.as_uri() + "?immutable=1", uri=True) as database:
            row = database.execute("SELECT data FROM events WHERE id=?", (event["id"],)).fetchone()
        self.assertEqual(json.loads(row[0])["body"], event["body"])
        self.assertFalse((self.snapshot / "home" / "state.sqlite3-wal").exists())

    def test_snapshot_rejects_existing_destination_and_active_worker(self):
        lock = (self.home / "worker.lock").open("w")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "stopped workers"):
                snapshot_case(self.home, self.snapshot)
            self.assertFalse(self.snapshot.exists())
        finally:
            lock.close()
        snapshot_case(self.home, self.snapshot)
        with self.assertRaisesRegex(ValueError, "must be new"):
            snapshot_case(self.home, self.snapshot)

    def test_restore_rejects_wrong_path_or_tampered_snapshot(self):
        snapshot_case(self.home, self.snapshot)
        with self.assertRaisesRegex(ValueError, "original employee home"):
            restore_case(self.snapshot, self.root / "other")
        (self.snapshot / "home" / "workspace" / "code.py").write_text("tampered")
        with self.assertRaisesRegex(ValueError, "manifest"):
            restore_case(self.snapshot, self.home)
        self.assertEqual((self.home / "workspace" / "code.py").read_text(), "original\n")

    def test_initialize_snapshots_requires_finished_original_cohort(self):
        output = self.root / "experiment"
        (output / "cases").mkdir(parents=True)
        self.home.rename(output / "cases" / "c-one")
        (output / "protocol.json").write_text(json.dumps({"cases": [{"id": "c-one"}, {"id": "c-two"}]}))
        with self.assertRaisesRegex(ValueError, "Every original worker"):
            initialize_snapshots(output)
        self.assertFalse((output / "followup" / "snapshots").exists())
        (output / "protocol.json").write_text(json.dumps({"cases": [{"id": "c-one"}]}))
        frozen = initialize_snapshots(output)
        self.assertTrue((frozen / "c-one" / "snapshot.json").exists())
        with self.assertRaisesRegex(ValueError, "already exist"):
            initialize_snapshots(output)

    def test_eligibility_preserves_failure_and_baseline_requirements(self):
        result = {"case": {"condition": "baseline"}, "review": {"disposition": "rework"},
                  "grade": {"behavioral_pass": False, "claimed_complete_but_failed": True}}
        self.assertEqual(eligible_branches(result), ["manager-rework", "self-review"])
        result["case"]["condition"] = "context"
        self.assertEqual(eligible_branches(result), ["manager-rework"])
        result["review"]["disposition"] = "accept"
        self.assertEqual(eligible_branches(result), [])
        result["case"]["condition"] = "baseline"
        self.assertEqual(eligible_branches(result), ["self-review"])
        result["grade"]["behavioral_pass"] = True
        self.assertEqual(eligible_branches(result), [])
        result["grade"].update(behavioral_pass=False, infrastructure_error="grader unavailable")
        self.assertEqual(eligible_branches(result), [])

    def test_branches_start_from_same_state_hide_grades_and_preserve_evidence(self):
        output = self.root / "experiment"
        (output / "cases").mkdir(parents=True)
        home = output / "cases" / "c-one"
        self.home.rename(home)
        manager = output / "managers" / "m-one"
        (manager / "workspace").mkdir(parents=True)
        review_text = '{\n  "disposition": "rework", "reason": "Review evidence", "required_changes": ["Reconsider the delivery"]\n}\n'
        (manager / "workspace" / "review.json").write_text(review_text)
        case = {"id": "c-one", "condition": "baseline"}
        protocol = {"cases": [case], "worker_turns": 2, "worker_budget_per_turn": 2,
                    "worker_timeout_per_turn": 480, "manager_budget": 0.75}
        original = {"case": case, "manager_home": str(manager), "review": json.loads(review_text),
                    "grade": {"behavioral_pass": False, "claimed_complete_but_failed": True,
                              "errors": ["DO NOT REVEAL THIS HELD-OUT VERDICT"]}}
        (output / "protocol.json").write_text(json.dumps(protocol))
        (output / "results.json").write_text(json.dumps([original]))
        calls = []

        def fake_execute(case_home, stage, **kwargs):
            self.assertEqual(case_home, home)
            self.assertEqual((home / "workspace" / "code.py").read_text(), "original\n")
            self.assertFalse((home / "branch-change").exists())
            with Store(home) as store:
                pending = store.event_pending()
                self.assertEqual(len(pending), 2)
                self.assertEqual(pending[0]["id"], self.original["id"])
                body = pending[1]["body"]
                self.assertNotIn("HELD-OUT VERDICT", body)
                if stage == "manager-rework":
                    self.assertIn(review_text, body)
                    self.assertEqual((home / "workspace" / "MANAGER_REVIEW.json").read_text(), review_text)
                else:
                    self.assertNotIn("Review evidence", body)
                    self.assertFalse((home / "workspace" / "MANAGER_REVIEW.json").exists())
                store.feed("worker", "Branch event " + stage)
            (home / "workspace" / "code.py").write_text(stage)
            (home / "branch-change").write_text(stage)
            calls.append((stage, kwargs))
            return {"success": True, "stage": stage}

        def fake_grade(artifact, destination):
            self.assertTrue((artifact / "workspace" / "code.py").exists())
            destination.mkdir(parents=True)
            (destination / "evidence.json").write_text("preserved evaluator output")
            return {"behavioral_pass": True}

        with patch("scripts.experiment_followup.execute", side_effect=fake_execute), patch(
                "scripts.experiment_followup.grade_artifact", side_effect=fake_grade):
            results = run_followups(output)
        self.assertEqual([call[0] for call in calls], ["manager-rework", "self-review"])
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertEqual(calls[0][1], {"turns": 2, "budget": 2, "timeout": 480})
        self.assertEqual(len(results), 2)
        self.assertEqual((home / "workspace" / "code.py").read_text(), "original\n")
        with Store(home) as store:
            self.assertEqual([event["id"] for event in store.event_pending()], [self.original["id"]])
        for branch in ("manager-rework", "self-review"):
            root = output / "followup" / "branches" / "c-one" / branch
            self.assertEqual((root / "delivery" / "home" / "branch-change").read_text(), branch)
            self.assertTrue((root / "result.json").exists())
            self.assertTrue((root / "grade" / "evidence.json").exists())
        with self.assertRaisesRegex(ValueError, "already exists"):
            run_followups(output)

    def test_independent_cases_run_concurrently_with_at_most_two_workers(self):
        output = self.root / "parallel-experiment"
        (output / "cases").mkdir(parents=True)
        manager = output / "manager"
        (manager / "workspace").mkdir(parents=True)
        review = {"disposition": "rework", "reason": "Review evidence", "required_changes": []}
        (manager / "workspace" / "review.json").write_text(json.dumps(review))
        cases, originals = [], []
        for number in range(3):
            case = {"id": "c-%s" % number, "condition": "context"}
            shutil.copytree(self.home, output / "cases" / case["id"])
            cases.append(case)
            originals.append({"case": case, "manager_home": str(manager), "review": review,
                              "grade": {"behavioral_pass": False}})
        protocol = {"cases": cases, "worker_turns": 2, "worker_budget_per_turn": 2,
                    "worker_timeout_per_turn": 480, "manager_budget": 0.75}
        (output / "protocol.json").write_text(json.dumps(protocol))
        (output / "results.json").write_text(json.dumps(originals))
        barrier = threading.Barrier(2)
        guard = threading.Lock()
        counters = {"active": 0, "started": 0, "maximum": 0}

        def fake_execute(home, stage, **kwargs):
            with guard:
                counters["active"] += 1
                counters["started"] += 1
                order = counters["started"]
                counters["maximum"] = max(counters["maximum"], counters["active"])
            try:
                if order <= 2:
                    # This fails if independent cases are accidentally serialized.
                    barrier.wait(timeout=2)
                time.sleep(0.01)
                (home / "workspace" / "code.py").write_text(home.name)
                return {"success": True}
            finally:
                with guard:
                    counters["active"] -= 1

        with patch("scripts.experiment_followup.execute", side_effect=fake_execute), patch(
                "scripts.experiment_followup.grade_artifact", return_value={"behavioral_pass": True}):
            results = run_followups(output)
        self.assertEqual(counters["maximum"], 2)
        self.assertEqual(len(results), 3)
        for case in cases:
            home = output / "cases" / case["id"]
            self.assertEqual((home / "workspace" / "code.py").read_text(), "original\n")


if __name__ == "__main__":
    unittest.main()
