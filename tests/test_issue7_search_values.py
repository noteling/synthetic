"""Issue #7: strict search phases match record values, never JSON key names."""
import tempfile, unittest
from pathlib import Path

from synthetics.store import Store


class SearchValuesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Store(Path(self.tmp.name) / "home")
        self.s.initialize("V", "m")
        self.s.entity_put("synthetics", "S", "project")

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def test_memory_all_match_ignores_key_names(self):
        for i in range(5):
            self.s.memory_put("synthetics", "other%d" % i, "unrelated", source="f")
        self.assertEqual(self.s.memory_search("subject value", match="all"), [])
        self.assertEqual(self.s.memory_search("source predicate superseded_by", match="all"), [])

    def test_memory_any_ranks_value_hit_first(self):
        target = self.s.memory_put("synthetics", "relevant thing", "the zebra source file", source="f")
        for i in range(10):
            self.s.memory_put("synthetics", "other%d" % i, "unrelated", source="f")
        hits = self.s.memory_search("zebra source", limit=3)
        self.assertEqual(hits[0]["id"], target["id"])
        self.assertEqual([m["id"] for m in self.s.memory_search("zebra source", match="all")], [target["id"]])

    def test_memory_values_still_match_including_id_and_subject(self):
        m = self.s.memory_put("synthetics", "pred", "val", source="file-x")
        self.assertEqual([x["id"] for x in self.s.memory_search(m["id"])][:1], [m["id"]])
        self.assertIn(m["id"], [x["id"] for x in self.s.memory_search("synthetics file-x", match="all")])

    def test_event_search_ignores_key_names(self):
        self.s.feed("dana", "zebra crossing", source="chat", external_id="e1")
        self.s.feed("dana", "plain words", source="chat", external_id="e2")
        self.assertEqual(self.s.event_search("body sender"), [])
        self.assertEqual([e["body"] for e in self.s.event_search("zebra")], ["zebra crossing"])

    def test_entities_query_ignores_key_names(self):
        self.assertEqual(self.s.entities("description"), [])
        self.assertEqual([e["id"] for e in self.s.entities("synthetics")], ["synthetics"])


if __name__ == "__main__":
    unittest.main()
