import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from scripts.experiment_fixture import create_fixture
from synthetics.store import Store


ROOT = Path(__file__).resolve().parents[1]


class FixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        (self.source / "synthetics" / "prompts").mkdir(parents=True)
        (self.source / "tests").mkdir()
        # Use the actual public Store implementation without any employee data.
        (self.source / "synthetics" / "__init__.py").write_text("")
        (self.source / "synthetics" / "store.py").write_text((ROOT / "synthetics" / "store.py").read_text())
        (self.source / "synthetics" / "engine.py").write_text((ROOT / "synthetics" / "engine.py").read_text())
        # M21: store.py imports capture -> projects -> projectlog.
        # Backlog 40: capture imports sqlro.
        for name in ("capture.py", "projects.py", "projectlog.py", "roster.py", "rostertools.py", "board.py",
                     "sqlro.py"):
            (self.source / "synthetics" / name).write_text((ROOT / "synthetics" / name).read_text())
        (self.source / "synthetics" / "prompts" / "employee.md").write_text(
            "Persistent employee.\n- Original directory /Users/someone/projects/synthetics is special.\n")
        (self.source / "tests" / "test_example.py").write_text("# neutral source test\n")
        (self.source / "pyproject.toml").write_text('[project]\nname = "fixture"\n')
        # These must never be copied, even when present alongside the source.
        (self.source / "docs").mkdir()
        (self.source / "docs" / "old-failure.md").write_text("secret incident narrative")
        (self.source / ".synthetics").mkdir()
        (self.source / ".synthetics" / "state.sqlite3").write_text("private history")
        (self.source / "SOURCE.json").write_text('{"original":"/private/real/project"}')
        (self.source / "synthetics" / "notes.md").write_text("private incident notes")
        (self.source / "synthetics" / "__pycache__").mkdir()
        (self.source / "synthetics" / "__pycache__" / "leak.py").write_text("not source")

    def fixture(self, name="case"):
        return create_fixture(self.base / name, self.source)

    def receiver(self, fixture, *args):
        return subprocess.run([sys.executable, fixture["receiver"], "--home", fixture["receiver_home"]] + list(args),
                              text=True, capture_output=True, timeout=10)

    def test_copy_contains_only_selected_source_and_neutral_material(self):
        fixture = self.fixture()
        project = Path(fixture["project"])
        self.assertFalse((project / "docs").exists())
        self.assertFalse((project / ".synthetics").exists())
        self.assertFalse((project / "SOURCE.json").exists())
        self.assertFalse((project / "synthetics" / "__pycache__").exists())
        for path in Path(fixture["workspace"]).rglob("*"):
            if path.is_file():
                text = path.read_text()
                self.assertNotIn("/Users/someone/", text)
                self.assertNotIn("secret incident narrative", text)
                self.assertNotIn("private history", text)
                self.assertNotIn("private incident notes", text)
        self.assertFalse(Path(fixture["receiver_home"]).exists())
        self.assertFalse((Path(fixture["home"]) / "state.sqlite3").exists())

    def test_manifest_is_reproducible_between_different_case_paths(self):
        first = self.fixture("one")
        second = self.fixture("two")
        self.assertEqual(first["manifest"], second["manifest"])
        self.assertEqual(first["sha256"], second["sha256"])
        saved = json.loads((Path(first["home"]) / "fixture-manifest.json").read_text())
        self.assertEqual(saved["sha256"], first["sha256"])

    def test_existing_destination_is_never_overwritten(self):
        fixture = self.fixture()
        marker = Path(fixture["workspace"]) / "keep.txt"
        marker.write_text("owned")
        with self.assertRaisesRegex(ValueError, "must be new"):
            self.fixture()
        self.assertEqual(marker.read_text(), "owned")

    def test_receiver_uses_real_events_and_source_linked_work(self):
        fixture = self.fixture()
        with Store(fixture["receiver_home"]) as store:
            store.initialize("Fixture reader", "Record incoming fixture documents")
            event = store.feed("fixture-source", "A new document with an opaque fact.", kind="document", source="fixture")
        result = self.receiver(fixture, "--once")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["handled"], 1)
        receipt = json.loads((Path(fixture["receiver_home"]) / "received.jsonl").read_text().strip())
        self.assertEqual(receipt["event_id"], event["id"])
        self.assertEqual(receipt["body"], event["body"])
        self.assertEqual(receipt["source"], "fixture")
        with Store(fixture["receiver_home"]) as store:
            self.assertEqual(store.event_pending(), [])
            work = store.work_get(receipt["work_id"])
            self.assertEqual(work["status"], "done")
            self.assertIn(event["id"], work["note"])
        repeated = self.receiver(fixture, "--once")
        self.assertEqual(json.loads(repeated.stdout)["handled"], 0)
        self.assertEqual(len((Path(fixture["receiver_home"]) / "received.jsonl").read_text().splitlines()), 1)

    def test_receiver_rejects_uninitialized_or_external_home_without_creating_it(self):
        fixture = self.fixture()
        missing = self.receiver(fixture, "--once")
        self.assertEqual(missing.returncode, 1)
        self.assertFalse(Path(fixture["receiver_home"]).exists())
        outside = self.base / "outside"
        result = subprocess.run([sys.executable, fixture["receiver"], "--home", str(outside), "--once"],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("within this fixture", result.stderr)
        self.assertFalse(outside.exists())

    def test_receiver_idle_does_not_create_a_run_or_call_fixture_brain(self):
        fixture = self.fixture()
        with Store(fixture["receiver_home"]) as store:
            store.initialize("Fixture reader", "Record incoming fixture documents")
        result = self.receiver(fixture, "--once")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"handled": 0, "event_ids": []})
        self.assertFalse((Path(fixture["receiver_home"]) / "received.jsonl").exists())
        with Store(fixture["receiver_home"]) as store:
            self.assertEqual(store.run_list(), [])

    def test_receiver_serves_events_published_after_start(self):
        fixture = self.fixture()
        with Store(fixture["receiver_home"]) as store:
            store.initialize("Fixture reader", "Record incoming fixture documents")
        process = subprocess.Popen([sys.executable, fixture["receiver"], "--home", fixture["receiver_home"],
                                    "serve", "--poll", "0.02"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            time.sleep(0.1)
            with Store(fixture["receiver_home"]) as store:
                event = store.feed("fixture-source", "Published after receiver startup.", source="fixture")
            deadline = time.monotonic() + 3
            receipt_path = Path(fixture["receiver_home"]) / "received.jsonl"
            while time.monotonic() < deadline and not receipt_path.exists():
                time.sleep(0.02)
            self.assertTrue(receipt_path.exists())
            self.assertIn(event["id"], receipt_path.read_text())
            process.terminate()
            stdout, stderr = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 0, stderr)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


if __name__ == "__main__":
    unittest.main()
