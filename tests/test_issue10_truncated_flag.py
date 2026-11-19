"""Issue #10: memory_context_truncated is true only when some active memory is in neither `memories`
nor the self index (self-subject memories listed in `self` are not hidden)."""
import tempfile, unittest
from pathlib import Path

from synthetics.store import Store


class TruncatedFlagTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Store(Path(self.tmp.name) / "home")
        self.s.initialize("V", "m")
        self.s.entity_put("synthetics", "S", "project")
        self.me = self.s.identity()["id"]

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def shown(self, snap):
        return {m["id"] for m in snap["memories"]} | {m["id"] for m in snap["memory_index"]}

    def test_nothing_hidden_flag_false(self):
        # 50 project + 20 self memories: all 50 in `memories`, 20 in the self index -> nothing hidden.
        for i in range(50):
            self.s.memory_put("synthetics", "p%d" % i, "v", source="src")
        for i in range(20):
            self.s.memory_put(self.me, "self%d" % i, "v", source="src")
        snap = self.s.context()
        self.assertEqual(len(snap["memories"]), 50)
        self.assertFalse(snap["memory_context_truncated"])

    def test_hidden_project_memory_flag_true(self):
        for i in range(61):
            self.s.memory_put("synthetics", "p%d" % i, "v", source="src")
        snap = self.s.context()
        self.assertEqual(len(snap["memories"]), 60)
        self.assertTrue(snap["memory_context_truncated"])

    def test_superseded_memories_do_not_count(self):
        old = self.s.memory_put("synthetics", "p", "v1", source="src")
        self.s.memory_put("synthetics", "p", "v2", source="src", replaces=old["id"])
        for i in range(59):
            self.s.memory_put("synthetics", "q%d" % i, "v", source="src")
        snap = self.s.context()
        self.assertEqual(snap["memory_count"], 60)
        self.assertFalse(snap["memory_context_truncated"])


if __name__ == "__main__":
    unittest.main()
