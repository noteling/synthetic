import json
from pathlib import Path
import tempfile
import unittest

from scripts.experiment_replay import TASK, initialize, manager_case
from synthetics.store import Store


class ReplayPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "source"
        (self.source / "synthetics/prompts").mkdir(parents=True)
        (self.source / "tests").mkdir()
        for relative, content in {
            "synthetics/__init__.py": "",
            "synthetics/store.py": "# supplied source\n",
            "synthetics/engine.py": "# supplied engine\n",
            "synthetics/prompts/employee.md": "Continue the assigned work as an employee.\n",
            "tests/test_example.py": "# existing source tests\n",
            "pyproject.toml": '[project]\nname="fixture"\n',
        }.items():
            (self.source / relative).write_text(content)
        self.fieldstone = self.base / "first-increment"
        for relative in ["pyproject.toml", "README.md", "backlog.md", "fieldstone/backend.py",
                         "fieldstone/__init__.py", "fieldstone/__main__.py", "fieldstone/display.py",
                         "tests/__init__.py", "tests/test_fieldstone.py"]:
            path = self.fieldstone / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# first increment\n")
        (self.fieldstone / "README.md").write_text("Run /Users/david/projects/synthetics-internal/fieldstone with /Users/david/projects/synthetics.\n")
        (self.fieldstone / "review-source.json").write_text("SECRET_METADATA")
        (self.fieldstone / "later-feedback.md").write_text("SECRET_FEEDBACK")
        self.review = self.base / "review.md"
        self.review.write_text("# Original review\n\nInitial published gaps.\n\n## Discovery observation: 2026-11-15\nSECRET_VERDICT\n")
        self.output = self.base / "replay"

    def prepare(self):
        return initialize(self.output, self.source, self.fieldstone, self.review)

    def test_pair_has_identical_common_material_and_no_later_feedback(self):
        protocol = self.prepare()
        self.assertTrue(protocol["prepared_only"])
        self.assertEqual({case["condition"] for case in protocol["cases"]}, {"baseline", "context"})
        self.assertEqual(len({case["fixture_digest"] for case in protocol["cases"]}), 1)
        for case in protocol["cases"]:
            workspace = self.output / "cases" / case["id"] / "workspace"
            self.assertFalse((workspace / "fieldstone/review-source.json").exists())
            self.assertFalse((workspace / "fieldstone/later-feedback.md").exists())
            self.assertEqual((workspace / "docs-source/openclaw-gap-review.md").read_text(),
                             "# Original review\n\nInitial published gaps.\n")
            for path in workspace.rglob("*"):
                if path.is_file():
                    text = path.read_text()
                    self.assertNotIn("SECRET_", text)
                    self.assertNotIn("/Users/david/", text)
        self.assertEqual(protocol["worker_turns"], 2)

    def test_diagnostic_assignment_and_waiting_commitment_are_preserved(self):
        protocol = self.prepare()
        home = self.output / "cases" / protocol["cases"][0]["id"]
        self.assertEqual((home / "workspace/TASK.md").read_text(), TASK)
        self.assertIn("record any implementation as separate work", TASK)
        self.assertIn("verifies unassisted detection/triage", TASK)
        self.assertNotIn("ten-second", TASK)
        with Store(home) as store:
            work = store.work_list()
            diagnosis = next(item for item in work if item["priority"] == 100)
            standing = next(item for item in work if item["status"] == "waiting")
            self.assertEqual(diagnosis["description"], TASK)
            self.assertTrue(standing["wake_at"].startswith("2026-11-22T20:00:00"))
            self.assertEqual(len(store.event_pending()), 1)
            self.assertEqual(store.run_list(), [])
            self.assertEqual(store.memory_search(), [])
        observation = json.loads((home / "workspace/observation.json").read_text())
        self.assertFalse(observation["passive_observation"]["self_pickup_observed"])
        self.assertNotIn("fallback_assignment", observation)

    def test_manager_sees_exact_original_task_and_all_work_records(self):
        protocol = self.prepare()
        home = self.output / "cases" / protocol["cases"][0]["id"]
        worker = {"work": [{"title": "Diagnosis", "status": "done"},
                           {"title": "Self-authored implementation", "status": "ready"}]}
        manager = self.base / "manager"
        task = manager_case(manager, home, worker, "same context", "same system prompt")
        self.assertEqual(task, TASK)
        self.assertEqual((manager / "workspace/TASK.md").read_text(), TASK)
        self.assertEqual(json.loads((manager / "workspace/WORKER_REPORT.json").read_text()), worker)
        with Store(manager) as store:
            self.assertEqual(store.run_list(), [])

    def test_frozen_inputs_do_not_change_with_original_source(self):
        protocol = self.prepare()
        (self.fieldstone / "README.md").write_text("CHANGED_AFTER_FREEZE")
        (self.review).write_text("CHANGED_AFTER_FREEZE")
        self.assertNotIn("CHANGED_AFTER_FREEZE", (self.output / "frozen/fieldstone/README.md").read_text())
        self.assertNotIn("CHANGED_AFTER_FREEZE", (self.output / "frozen/gap-review.md").read_text())
        self.assertEqual(json.loads((self.output / "protocol.json").read_text()), protocol)

    def test_later_remedy_source_is_rejected(self):
        (self.fieldstone / "fieldstone/backend.py").write_text("def sourcefeed(): pass\n")
        with self.assertRaisesRegex(ValueError, "later remedy"):
            self.prepare()


if __name__ == "__main__":
    unittest.main()
