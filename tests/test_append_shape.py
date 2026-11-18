"""Backlog 17: project_memory_append entry bodies are discoverable from the tool schema and from errors."""
import tempfile
import unittest
from pathlib import Path

from synthetics import projecttools
from synthetics.mcp import Server
from synthetics.projects import ProjectError
from synthetics.store import Store
from tests.humans_fixture import write_host_humans


class AppendShapeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "vesper"
        self.store = Store(self.home)
        write_host_humans(self.store.home)
        self.store.initialize("Vesper", "maintainer")
        self.run = self.store.run_begin()["id"]
        self.bound = Store(self.home, run_id=self.run)
        self.server = Server(self.bound, run_id=self.run)

    def tearDown(self):
        self.bound.close()
        self.store.close()
        self.tmp.cleanup()

    def append(self, entries, key):
        return self.server.call("project_memory_append", {"project": "synthetics", "entries": entries, "key": key})

    def test_schema_lists_body_fields_for_every_appendable_kind(self):
        tool = next(t for t in projecttools.TOOLS if t["name"] == "project_memory_append")
        desc = tool["inputSchema"]["properties"]["entries"]["items"]["description"]
        for kind in projecttools.APPEND_KINDS:
            self.assertIn(kind + " {", desc)
        for field in ("statement", "applies_when", "uncertainty", "expected_head", "topic"):
            self.assertIn(field, desc)
        self.assertIn("inside body", desc)

    def test_lesson_error_names_every_missing_field(self):
        with self.assertRaises(ProjectError) as cm:
            self.append([{"kind": "lesson", "body": {"statement": "x"}}], "l1")
        self.assertEqual(cm.exception.code, "bad_lesson")
        self.assertEqual(cm.exception.detail["missing"], ["applies_when", "uncertainty"])
        self.assertIn("applies_when, uncertainty required", str(cm.exception))

    def test_top_level_fields_error_says_to_move_them_into_body(self):
        with self.assertRaises(ProjectError) as cm:
            self.append([{"kind": "considered", "source": "pm-x", "reason": "routine"}], "c1")
        self.assertEqual(cm.exception.code, "bad_entry")
        self.assertIn("reason, source inside body", str(cm.exception))
        with self.assertRaises(ProjectError) as cm:
            projecttools._entries(["not an object"])
        self.assertEqual(cm.exception.code, "bad_entry")

    def test_complete_lesson_still_appends(self):
        out = self.append([{"kind": "lesson", "body": {"statement": "s", "applies_when": "a",
                                                        "uncertainty": "none known"}}], "l2")
        self.assertEqual(len(out["ids"]), 1)


if __name__ == "__main__":
    unittest.main()
