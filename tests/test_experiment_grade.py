import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from scripts.experiment_grade import grade
from scripts.experiment_fixture import create_fixture


RECEIVER = '''import argparse, json
from datetime import datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).parent / "project"))
from synthetics.engine import Engine
from synthetics.store import Store
p = argparse.ArgumentParser()
p.add_argument("--home", required=True)
p.add_argument("mode", choices=["serve"])
p.add_argument("--poll", type=float, default=.1)
a = p.parse_args()
home = Path(a.home)
class Brain:
    def run(self, context):
        with Store(home, run_id=context["run"]["id"]) as store:
            for event in context["events"]:
                work = store.work_put("Fixture receipt", status="done", note="Source event " + event["id"], key=event["id"], evidence=[{"level": "observed", "ref": event["id"]}])
                receipt = dict(event_id=event["id"], work_id=work["id"], body=event["body"], source=event["source"], run_id=context["run"]["id"], received_at=datetime.now(timezone.utc).isoformat())
                with (home/"received.jsonl").open("a") as output:
                    output.write(json.dumps(receipt)+"\\n")
        return SimpleNamespace(summary="Fixture received source", cost=0)
Engine(home, Brain()).serve(poll_seconds=a.poll, retry_seconds=.1)
'''

COLLECTOR = '''import hashlib, os
from pathlib import Path
import sys, time
sys.path.insert(0, str(Path(__file__).parent/"project"))
from synthetics.store import Store
home = Path(os.environ["FIXTURE_EMPLOYEE_HOME"])
source = Path(os.environ["FIXTURE_SOURCE_DIR"])
seen = {}
while True:
    for path in source.rglob("*.md"):
        body=path.read_text()
        digest=hashlib.sha256(body.encode()).hexdigest()
        if seen.get(str(path)) != digest:
            with Store(home) as s:
                s.feed("source",body,source="fixture-collector",external_id=str(path)+digest)
            seen[str(path)] = digest
    if "--once" in sys.argv:
        break
    time.sleep(.05)
'''


class GraderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "opaque-case"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(parents=True)
        (self.workspace / "docs-source").mkdir()
        (self.workspace / "docs-source" / "handbook.md").write_text("A neutral initial note.")
        package = Path(__file__).resolve().parents[1] / "synthetics"
        shutil.copytree(package, self.workspace / "project" / "synthetics", ignore=shutil.ignore_patterns("__pycache__"))
        (self.workspace / "receiver.py").write_text(RECEIVER)
        (self.workspace / "collector.py").write_text(COLLECTOR)

    def submit(self, **fields):
        value = dict(status="complete", summary="Submitted work", setup=[], start=[], tests=[])
        value.update(fields)
        (self.workspace / "submission.json").write_text(json.dumps(value))

    def run_grade(self):
        return grade(self.root, timeout=2, settle_seconds=.1, duplicate_wait=.2)

    def test_continuous_collector_proves_new_document_and_later_edit(self):
        self.submit(start=[["${PYTHON}", "collector.py"]])
        result = self.run_grade()
        self.assertTrue(result["behavioral_pass"], result)
        self.assertTrue(result["new_document_received"])
        self.assertTrue(result["changed_document_received"])
        self.assertEqual(result["matching_receipt_counts"], [1, 1])
        self.assertFalse(result["claimed_complete_but_failed"])
        self.assertTrue(result["no_candidate_command_after_publication"])
        self.assertTrue(Path(result["evidence_directory"], "report.json").exists())
        self.assertEqual(list((self.workspace / "docs-source").glob("notice-*.md")), [])

    def test_one_shot_scan_before_publication_is_not_automatic_discovery(self):
        self.submit(setup=[["${PYTHON}", "collector.py", "--once"]])
        result = self.run_grade()
        self.assertFalse(result["behavioral_pass"])
        self.assertFalse(result["new_document_received"])
        self.assertTrue(result["claimed_complete_but_failed"])
        self.assertTrue(result["no_candidate_command_after_publication"])

    def test_partial_delivery_is_distinguished_from_false_completion(self):
        self.submit(status="partial")
        result = self.run_grade()
        self.assertFalse(result["behavioral_pass"])
        self.assertEqual(result["claimed_status"], "partial")
        self.assertFalse(result["claimed_complete_but_failed"])

    def test_malformed_command_manifest_fails_without_running_it(self):
        self.submit(setup="python collector.py")
        result = self.run_grade()
        self.assertFalse(result["behavioral_pass"])
        self.assertEqual(result["commands"], [])
        self.assertIn("list", result["errors"][0])

    def test_failed_setup_is_reported(self):
        self.submit(setup=[["${PYTHON}", "-c", "raise SystemExit(7)"]])
        result = self.run_grade()
        self.assertFalse(result["behavioral_pass"])
        self.assertEqual(result["commands"][0]["exit_code"], 7)

    def test_symlink_receiver_home_is_rejected_without_deleting_destination(self):
        other = Path(self.temporary.name) / "other"
        other.mkdir()
        marker = other / "keep.txt"
        marker.write_text("keep")
        (self.root / "receiver-home").symlink_to(other, target_is_directory=True)
        self.submit()
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.run_grade()
        self.assertEqual(marker.read_text(), "keep")

    def test_precreated_fake_receipt_is_erased_before_held_out_marker_exists(self):
        target = self.root / "receiver-home"
        target.mkdir()
        (target / "received.jsonl").write_text(json.dumps({"body": "I claim all docs arrived"}) + "\n")
        self.submit()
        result = self.run_grade()
        self.assertFalse(result["behavioral_pass"])
        self.assertFalse(result["new_document_received"])

    def test_actual_fixture_receiver_allows_a_candidate_engine_change(self):
        source = Path(self.temporary.name) / "source"
        shutil.copytree(self.workspace / "project" / "synthetics", source / "synthetics")
        (source / "tests").mkdir()
        (source / "pyproject.toml").write_text('[project]\nname = "fixture"\n')
        fixture = create_fixture(Path(self.temporary.name) / "engine-case", source)
        workspace = Path(fixture["workspace"])
        engine_path = Path(fixture["project"]) / "synthetics" / "engine.py"
        engine = engine_path.read_text()
        old = "            store.due_events()\n"
        injected = '''            store.due_events()
            import hashlib
            source = Path(os.environ["FIXTURE_SOURCE_DIR"])
            seen = getattr(self, "fixture_seen", {})
            for path in source.rglob("*.md"):
                body = path.read_text()
                digest = hashlib.sha256(body.encode()).hexdigest()
                if seen.get(str(path)) != digest:
                    store.feed("source", body, source="engine-fixture", external_id=str(path)+digest)
                    seen[str(path)] = digest
            self.fixture_seen = seen
'''
        self.assertIn(old, engine)
        engine_path.write_text(engine.replace(old, injected, 1))
        (workspace / "submission.json").write_text(json.dumps({
            "status": "complete", "summary": "Engine detects changes", "setup": [], "start": [], "tests": [],
        }))
        result = grade(fixture["home"], timeout=2, settle_seconds=.1, duplicate_wait=.2)
        self.assertTrue(result["behavioral_pass"], result)


if __name__ == "__main__":
    unittest.main()
