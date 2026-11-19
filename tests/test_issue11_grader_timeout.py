"""Issue #11: the trial grader uses the task's 10 s receipt bound, and the grade report records its bounds."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scripts.experiment_trials as trials
import tests.test_experiment_grade as grade_tests  # module import: its GraderTests are not collected twice
from scripts.experiment_grade import grade


class GraderTimeoutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_task_states_ten_seconds_and_constant_matches(self):
        self.assertIn("within ten seconds", trials.TASK)
        self.assertEqual(trials.RECEIPT_TIMEOUT_SECONDS, 10)
        self.assertIn("Five seconds of unchanged content", trials.TASK)
        self.assertEqual(trials.DUPLICATE_WAIT_SECONDS, 5)

    def test_grade_artifact_passes_timeout_10(self):
        artifact = self.root / "artifact"
        (artifact / "workspace").mkdir(parents=True)
        destination = self.root / "dest"
        destination.mkdir()
        seen = {}

        def fake_run(command, **kwargs):
            seen["command"] = command
            return mock.Mock(stdout="", stderr="no report", returncode=1)

        with mock.patch.object(trials.subprocess, "run", side_effect=fake_run):
            trials.grade_artifact(artifact, destination)
        command = seen["command"]
        self.assertIn("--timeout", command)
        self.assertEqual(command[command.index("--timeout") + 1], "10")
        self.assertEqual(command[command.index("--duplicate-wait") + 1], "5")

    def test_report_records_bounds(self):
        case = self.root / "opaque-case"
        workspace = case / "workspace"
        (workspace / "docs-source").mkdir(parents=True)
        (workspace / "docs-source" / "handbook.md").write_text("A neutral initial note.")
        package = Path(grade_tests.__file__).resolve().parents[1] / "synthetics"
        shutil.copytree(package, workspace / "project" / "synthetics", ignore=shutil.ignore_patterns("__pycache__"))
        (workspace / "receiver.py").write_text(grade_tests.RECEIVER)
        (workspace / "submission.json").write_text(json.dumps(
            dict(status="partial", summary="s", setup=[], start=[], tests=[])))
        report = grade(case, timeout=1, settle_seconds=.1, duplicate_wait=.2)
        self.assertEqual((report["timeout_seconds"], report["duplicate_wait_seconds"]), (1, .2))


if __name__ == "__main__":
    unittest.main()
