"""M26 P1 step 4: snapshot sections. Design test 12 (self/people/views present, labelled, within budget,
truncation index above the cap) and C1 (self_memories replaced by the index inside `self`, no duplicates)."""

import tempfile
import unittest
from pathlib import Path

from synthetics.roster import CompanyStore
from synthetics.store import Store

CODEX = "employee-a22b6a2a8f524cd6b6800b0a620dbe06"
MARK = "PRIVATE VIEW — not official (private to Vesper)"


def person(pid, name, role="employee"):
    return {"kind": "person", "body": {"id": pid, "kind": "employee", "name": name, "role": role,
                                       "source": "operator statement", "certainty": "stated"}}


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "vesper")
        self.me = self.store.initialize("Vesper", "maintainer")
        with CompanyStore.at_root(self.root) as co:
            co.append([person(CODEX, "Codex", "Synthetics manager")], author="operator", operator=True)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_12_sections_present_labelled_and_before_memories(self):
        self.store.entity_put("david", "David", "person")
        self.store.entity_put("synthetics", "Synthetics", "project")
        self.store.view_put("Codex", claims=[{"cid": "c1", "text": "routes design questions", "kind": "working",
                                              "source": "event-1", "certainty": "observed"}])
        ctx = self.store.context()
        keys = list(ctx)
        for k in ("self", "people", "views"):
            self.assertLess(keys.index(k), keys.index("memories"))
        self.assertTrue(ctx["self"].startswith("# Self: Vesper"))
        self.assertIn("official company profile", ctx["people"][CODEX])
        self.assertTrue(ctx["views"][CODEX].startswith("# " + MARK))
        self.assertFalse(ctx["people_truncated"] or ctx["views_truncated"])
        self.assertEqual(ctx["views_stale"], [])
        # §5: self and company-person rows leave `entities`; a person without a profile stays, flagged unofficial.
        self.assertEqual({(e["id"], e["kind"], e.get("unofficial", False)) for e in ctx["entities"]},
                         {("david", "person", True), ("synthetics", "project", False)})
        self.assertLessEqual(len(ctx["self"].encode()), 4096)
        self.assertFalse((self.root / "vesper" / "roster").exists())  # context build writes no files

    def test_12_people_cap_keeps_referenced_profiles_and_indexes_the_rest(self):
        ids = ["employee-%032x" % n for n in range(40)]
        with CompanyStore.at_root(self.root) as co:
            co.append([dict(person(pid, "Person %d" % n), body=dict(person(pid, "Person %d" % n)["body"],
                       description="x" * 550)) for n, pid in enumerate(ids)], author="operator", operator=True)
        self.store.question_ask(ids[-1], "ping?", key="q")
        self.store.PEOPLE_BYTES = 6000
        ctx = self.store.context()
        self.assertTrue(ctx["people_truncated"])
        self.assertIn(ids[-1], ctx["people"])          # referenced by an open question: full
        self.assertLessEqual(sum(len(t.encode()) for t in ctx["people"].values()), 6000)
        indexed = {e["id"] for e in ctx["people_index"]}
        self.assertEqual(indexed | set(ctx["people"]), set(ids) | {CODEX})
        self.assertFalse(indexed & set(ctx["people"]))

    def test_c1_self_memories_become_index_in_self_without_duplicates(self):
        mine = self.store.memory_put(self.me["id"], "procedure", "Grep tests before patching.",
                                     kind="procedure", source="run-1")
        old = self.store.memory_put(self.me["id"], "name", "Old", source="boot")
        self.store.memory_put(self.me["id"], "name", "New", source="boot2", replaces=old["id"])
        self.store.entity_put("synthetics", "Synthetics", "project")
        for n in range(61):
            self.store.memory_put("synthetics", "finding", "Finding %d" % n, source="x")
        ctx = self.store.context()
        self.assertNotIn("self_memories", ctx)
        self.assertIn(mine["id"], ctx["self"])
        self.assertIn("Grep tests before patching.", ctx["self"])
        self.assertNotIn(old["id"], ctx["self"])  # superseded never indexed
        index_ids = {e["id"] for e in ctx["memory_index"]}
        self.assertNotIn(mine["id"], index_ids)   # listed once, in `self`
        self.assertTrue(ctx["memory_context_truncated"])

    def test_c1_self_memories_crowded_out_of_self_stay_in_memory_index(self):
        # N3 measurement: on the manager's home, seeded how-i-work notes filled the 4 KB self file, so the
        # self-memory index was cut; those memories must then remain in memory_index, not vanish.
        for n in range(40):
            self.store.self_note("how-i-work", "Working note %d " % n + "x" * 80, source="run-%d" % n)
        ids = [self.store.memory_put(self.me["id"], "fact %d" % n, "Self fact %d" % n, source="s")["id"]
               for n in range(5)]
        self.store.entity_put("synthetics", "Synthetics", "project")
        for n in range(61):
            self.store.memory_put("synthetics", "finding", "Finding %d" % n, source="x")
        ctx = self.store.context()
        self.assertLessEqual(len(ctx["self"].encode("utf-8")), 4096)
        index_ids = {e["id"] for e in ctx["memory_index"]}
        shown = {m["id"] for m in ctx["memories"]}
        for mid in ids:
            self.assertTrue(mid in ctx["self"] or mid in index_ids or mid in shown, mid)


if __name__ == "__main__":
    unittest.main()
