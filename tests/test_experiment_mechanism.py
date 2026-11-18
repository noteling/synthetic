import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.experiment_mechanism import _receiver_start, grade_mechanism


class MechanismDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.artifact = self.base / "artifact"
        self.workspace = self.artifact / "workspace"
        self.workspace.mkdir(parents=True)
        (self.workspace / "watcher.py").write_text("# unchanged implementation\n")
        (self.workspace / "receiver.py").write_text("# supplied instrument\n")
        (self.workspace / "test_watcher.py").write_text("raise RuntimeError('must not execute in wrapper tests')\n")
        self.submission = {"status": "complete", "summary": "Original delivery",
                           "setup": [["${PYTHON}", "setup.py"]],
                           "start": [["${PYTHON}", "watcher.py"],
                                     ["${PYTHON}", "${FIXTURE_RECEIVER}", "--home", "${FIXTURE_EMPLOYEE_HOME}", "serve"]],
                           "tests": [["${PYTHON}", "test_watcher.py"]]}
        self.original_bytes = (json.dumps(self.submission, separators=(",", ":")) + "\n").encode()
        (self.workspace / "submission.json").write_bytes(self.original_bytes)
        self.destination = self.base / "diagnostic"

    def test_records_exact_normalization_without_changing_original_or_code(self):
        def fake_grade(artifact, destination):
            normalized = json.loads((artifact / "workspace/submission.json").read_text())
            self.assertEqual(normalized["tests"], [])
            self.assertEqual(normalized["start"], [["${PYTHON}", "watcher.py"]])
            self.assertEqual(normalized["setup"], self.submission["setup"])
            self.assertEqual((artifact / "workspace/watcher.py").read_bytes(),
                             (self.workspace / "watcher.py").read_bytes())
            return {"behavioral_pass": True}

        with patch("scripts.experiment_mechanism.experiment_trials.grade_artifact", side_effect=fake_grade) as grader:
            report = grade_mechanism(self.artifact, self.destination)
        grader.assert_called_once()
        self.assertTrue(report["secondary_mechanism_pass"])
        self.assertIsNone(report["original_delivery_pass"])
        self.assertEqual(report["diagnostic_kind"], "secondary_mechanism_diagnostic")
        self.assertEqual(report["workspace_changes"], ["submission.json"])
        self.assertTrue(report["original_artifact_unchanged"])
        self.assertTrue(report["non_submission_workspace_unchanged"])
        self.assertEqual((self.workspace / "submission.json").read_bytes(), self.original_bytes)
        self.assertEqual((self.destination / "original-submission.json").read_bytes(), self.original_bytes)
        self.assertEqual(report["original_submission_sha256"], hashlib.sha256(self.original_bytes).hexdigest())
        removed = report["normalization"][1]["removed"]
        self.assertEqual(removed, [{"index": 1, "argv": self.submission["start"][1],
                                   "reason": "The unchanged grader provides the supplied receiver independently."}])
        original = json.loads((self.destination / "original-workspace-manifest.json").read_text())
        normalized = json.loads((self.destination / "normalized-workspace-manifest.json").read_text())
        self.assertEqual(original["watcher.py"], normalized["watcher.py"])

    def test_normalization_is_uniform_when_there_is_no_duplicate_receiver(self):
        self.submission["start"] = [["${PYTHON}", "watcher.py"]]
        (self.workspace / "submission.json").write_text(json.dumps(self.submission))
        with patch("scripts.experiment_mechanism.experiment_trials.grade_artifact", return_value={"behavioral_pass": False}):
            report = grade_mechanism(self.artifact, self.destination)
        normalized = json.loads((self.destination / "normalized-artifact/workspace/submission.json").read_text())
        self.assertEqual(normalized["tests"], [])
        self.assertEqual(normalized["start"], self.submission["start"])
        self.assertEqual(report["normalization"][1]["removed"], [])
        self.assertFalse(report["secondary_mechanism_pass"])

    def test_only_direct_supplied_receiver_invocations_are_removed(self):
        for command in (["${PYTHON}", "${FIXTURE_RECEIVER}", "serve"],
                        ["python3", "-u", "${FIXTURE_WORKSPACE}/receiver.py", "serve"],
                        ["/usr/bin/python3", "/old/case/workspace/receiver.py", "serve"],
                        ["python", "./receiver.py", "serve"]):
            self.assertTrue(_receiver_start(command), command)
        for command in (["bash", "-c", "python receiver.py serve"],
                        ["env", "python3", "receiver.py", "serve"],
                        ["python3", "-c", "print('receiver.py')"],
                        ["python3", "-m", "receiver"],
                        ["python3", "my_receiver.py"],
                        ["python3", "/other/receiver.py"],
                        ["python3", "watcher.py", "--receiver", "${FIXTURE_RECEIVER}"]):
            self.assertFalse(_receiver_start(command), command)

    def test_grader_failure_stays_a_secondary_infrastructure_result(self):
        with patch("scripts.experiment_mechanism.experiment_trials.grade_artifact", side_effect=OSError("grader unavailable")):
            report = grade_mechanism(self.artifact, self.destination)
        self.assertFalse(report["secondary_mechanism_pass"])
        self.assertEqual(report["grade"]["infrastructure_error"], "grader unavailable")
        self.assertIsNone(report["original_delivery_pass"])
        self.assertTrue(report["original_artifact_unchanged"])
        self.assertTrue((self.destination / "report.json").is_file())

    def test_changes_during_grading_invalidate_diagnostic_pass(self):
        def mutate_original(artifact, destination):
            (self.workspace / "watcher.py").write_text("changed during grading")
            return {"behavioral_pass": True}

        with patch("scripts.experiment_mechanism.experiment_trials.grade_artifact", side_effect=mutate_original):
            report = grade_mechanism(self.artifact, self.destination)
        self.assertFalse(report["secondary_mechanism_pass"])
        self.assertFalse(report["original_artifact_unchanged"])
        self.assertIn("integrity_error", report)

    def test_existing_or_nested_destination_is_rejected_before_grading(self):
        for destination in (self.artifact, self.workspace / "nested", self.base):
            with patch("scripts.experiment_mechanism.experiment_trials.grade_artifact") as grader:
                with self.assertRaises(ValueError):
                    grade_mechanism(self.artifact, destination)
                grader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
