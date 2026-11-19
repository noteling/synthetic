"""VIS G2: the work_put tool text and the employee prompt say truly what leaves a home for Lookout."""
import json
import tempfile
import unittest
from pathlib import Path

from synthetics import sharedstatus
from synthetics.mcp import TOOLS
from synthetics.store import Store

PROMPT = Path(__file__).resolve().parents[1] / "synthetics" / "prompts" / "employee.md"
# status.json work fields that carry the employee's own words or plans (id/published/question IDs are references)
SHOWN = ("title", "status", "priority", "waiting_for", "wake_at")


def _publish_text():
    tool = next(t for t in TOOLS if t["name"] == "work_put")
    return tool["inputSchema"]["properties"]["publish"]["description"]


def _prompt_line():
    return next(line for line in PROMPT.read_text().splitlines() if "work_put publish=" in line)


class WorkVisibilityText(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "emp"
        self.store = Store(self.home)
        self.addCleanup(self.store.close)
        self.store.initialize("Aster", "G2 colleague")

    def test_status_json_shows_exactly_the_fields_the_text_names_for_unpublished_open_work(self):
        self.store.work_put(title="UNPUBLISHED-TITLE", description="DESC-SECRET", note="NOTE-SECRET",
                            status="waiting", waiting_for="WAIT-TEXT", wake_at="2030-01-01T00:00:00Z", priority=3)
        done = self.store.work_put(title="DONE-TITLE", description="d")
        self.store.work_put(id=done["id"], status="cancelled", cancel_reason="test")
        self.assertTrue(sharedstatus.write(self.store, paused=None))
        text = sharedstatus.path(self.home).read_text()
        works = json.loads(text)["works"]
        self.assertEqual([w["title"] for w in works], ["UNPUBLISHED-TITLE"])  # open works only
        self.assertIsNone(works[0]["published"])
        for secret in ("DESC-SECRET", "NOTE-SECRET", "DONE-TITLE"):
            self.assertNotIn(secret, text)
        self.assertTrue(set(SHOWN) <= set(works[0]))
        for source, words in (("work_put publish", _publish_text()), ("employee.md", _prompt_line())):
            with self.subTest(source=source):
                self.assertIn("Published or not", words)
                self.assertIn("Lookout", words)
                for field in SHOWN:
                    self.assertIn(field, words)


if __name__ == "__main__":
    unittest.main()
