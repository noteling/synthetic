"""MTG F2 C6 + Rowan G3: the ready persona is built by code from a static allow-list. Temp homes only."""

import json
import re
import tempfile
import unittest
from pathlib import Path

from synthetics import floorpersona as fp
from synthetics.store import Store

SECRET = "Zebra42-private"


class PersonaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "vesper"
        self.store = Store(self.home)
        self.addCleanup(self.store.close)
        self.store.initialize("Vesper", "maintainer", purpose="Maintain Synthetics.")
        self.named = self.store.work_put(title="Ship Byline", note="note " + SECRET + "-named", status="ready")
        self.other = self.store.work_put(title="Other work " + SECRET, note=SECRET, status="ready",
                                         waiting_for=SECRET)
        self.store.memory_put(self.store.identity()["id"], "fact", SECRET + "-memory", source="test")

    def build(self, goal):
        return fp.build(self.store, goal)

    def test_ready_keys_are_the_allow_list(self):
        out = self.build("Status of %s please" % self.named["id"])
        self.assertEqual(sorted(out["ready"]), sorted(fp.READY_KEYS))
        self.assertIn("Ship Byline", out["ready"]["persona"])
        self.assertEqual(out["work_ids"], [self.named["id"]])

    def test_unnamed_works_memories_and_notes_never_reach_the_voice(self):
        out = self.build("Status of %s please" % self.named["id"][:13])   # 8-hex prefix
        ready = json.dumps(out["ready"])
        self.assertNotIn(SECRET, ready)            # no other work, no memory, no note at all in ready
        self.assertIn(SECRET + "-named", out["brief"])   # the named work's note only in the session brief
        self.assertNotIn(SECRET + "-memory", out["brief"])
        self.assertNotIn("Other work", out["brief"])

    def test_goal_bounds(self):
        for bad in ("", "   ", None, "x" * (fp.GOAL_MAX + 1)):
            with self.assertRaises(fp.PersonaError):
                self.build(bad)

    def test_unknown_work_ref_is_ignored(self):
        out = self.build("talk about work-" + "f" * 32)
        self.assertEqual(out["work_ids"], [])

    def test_persona_is_bounded(self):
        goal = ("word " * 800).strip()
        out = self.build(goal)
        self.assertLessEqual(len(out["ready"]["persona"].encode("utf-8")), fp.PERSONA_MAX + 4)

    def test_module_touches_only_listed_store_calls(self):
        source = Path(fp.__file__).read_text(encoding="utf-8")
        used = set(re.findall(r"store\.([A-Za-z_]+)", source))
        self.assertLessEqual(used, set(fp.STORE_CALLS), used - set(fp.STORE_CALLS))
        for banned in ("memory", "view", "outbox", "message", "self_note", "event"):
            self.assertNotRegex(source.split('"""', 2)[2], r"store\.\w*%s" % banned)


if __name__ == "__main__":
    unittest.main()
