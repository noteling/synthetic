"""M18 MG-03 (failed tools in recent_runs) and MG-07 (absence-claim guidance)."""
import json
import tempfile
import unittest
from pathlib import Path

from synthetics.store import Store

PROMPT = Path(__file__).resolve().parent.parent / "synthetics" / "prompts" / "employee.md"


class FailedToolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.store = Store(self.home)
        self.addCleanup(self.store.close)
        self.store.initialize("Vesper", "Maintainer")

    def run_with_log(self, lines):
        run = self.store.run_begin(event_ids=[])
        self.store.run_finish(run["id"], "done", cost=0.1)
        folder = self.home / "runs"
        folder.mkdir(exist_ok=True)
        (folder / (run["id"] + ".mcp.jsonl")).write_text(
            "".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines))
        return run["id"]

    def recent(self, run_id):
        return next(r for r in self.store.context()["recent_runs"] if r["id"] == run_id)

    def test_failed_calls_listed_with_truncated_error(self):
        run_id = self.run_with_log([
            {"at": "t1", "tool": "memory_put", "success": True},
            {"at": "t2", "tool": "message_send", "success": False, "error": "No works record work-x." + "y" * 500},
        ])
        entry = self.recent(run_id)
        self.assertEqual(entry["failed_tool_count"], 1)
        self.assertEqual(entry["failed_tools"][0]["tool"], "message_send")
        self.assertEqual(entry["failed_tools"][0]["at"], "t2")
        self.assertTrue(entry["failed_tools"][0]["error"].startswith("No works record"))
        self.assertEqual(len(entry["failed_tools"][0]["error"]), 200)
        self.assertNotIn("failed_tools_unavailable", entry)

    def test_list_capped_but_count_complete(self):
        run_id = self.run_with_log([{"at": str(i), "tool": "t%d" % i, "success": False, "error": "e"}
                                    for i in range(8)])
        entry = self.recent(run_id)
        self.assertEqual(len(entry["failed_tools"]), 5)
        self.assertEqual(entry["failed_tool_count"], 8)

    def test_missing_log_means_no_failures(self):
        run = self.store.run_begin(event_ids=[])
        self.store.run_finish(run["id"], "done", cost=0.1)
        entry = self.recent(run["id"])
        self.assertEqual((entry["failed_tools"], entry["failed_tool_count"]), ([], 0))
        self.assertNotIn("failed_tools_unavailable", entry)

    def test_corrupt_line_flagged_not_hidden(self):
        run_id = self.run_with_log(["{not json", {"at": "t", "tool": "x", "success": False, "error": "boom"}])
        entry = self.recent(run_id)
        self.assertTrue(entry["failed_tools_unavailable"])
        self.assertEqual(entry["failed_tool_count"], 1)

    def test_stored_run_record_unchanged(self):
        run_id = self.run_with_log([{"at": "t", "tool": "x", "success": False, "error": "boom"}])
        self.store.context()
        self.assertNotIn("failed_tools", self.store.run_get(run_id))


class AbsenceGuidanceTests(unittest.TestCase):
    def test_prompt_requires_search_or_belief_for_absence_claims(self):
        text = PROMPT.read_text(encoding="utf-8")
        self.assertIn("A claim that something is absent", text)
        self.assertIn("cite the query and the scope searched", text)
        self.assertIn("store it as a belief", text)


if __name__ == "__main__":
    unittest.main()
