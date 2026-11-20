"""project_memory_get shows a lesson's applies_when, uncertainty and evidence (Codex event-d2540f61).

Before: the fold kept only the statement, so get() showed no evidence and a "no evidence refs" check read
wrong (pm-3c8d97c7, Rowan event-0740c5c6). projectview.py already showed the refs.
"""
import json
import tempfile
import unittest
from pathlib import Path

from synthetics.projects import GET_BUDGET, ProjectStore, _canon


def lesson(evidence=None, **extra):
    body = {"statement": "s", "applies_when": "when A", "uncertainty": "none known", **extra}
    if evidence is not None:
        body["evidence"] = evidence
    return {"kind": "lesson", "body": body}


class PmGetEvidence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProjectStore(Path(self.tmp.name) / "synthetics" / "memory.sqlite3", "synthetics")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, batch):
        return self.store.append([batch], author="vesper", run="run-x")["ids"][0]

    def test_structured_evidence_shown(self):
        rid = self.add(lesson([{"ref": "event-738fcaed", "level": "observed", "note": "fail-first"},
                               {"ref": "i25/dbl/failfirst2.log"}]))
        g = self.store.get(rid)
        self.assertEqual(g["lesson"]["evidence"], [
            {"ref": "event-738fcaed", "level": "observed", "note": "fail-first"},
            {"ref": "i25/dbl/failfirst2.log"}])
        self.assertEqual(g["lesson"]["evidence_count"], 2)
        self.assertEqual(g["lesson"]["applies_when"], "when A")
        self.assertEqual(g["lesson"]["uncertainty"], "none known")

    def test_free_text_evidence_shown(self):
        rid = self.add(lesson("seen in run-123 and PR #85"))
        g = self.store.get(rid)
        self.assertEqual(g["lesson"]["evidence"], "seen in run-123 and PR #85")
        self.assertEqual(g["lesson"]["evidence_count"], 1)

    def test_no_evidence_is_explicitly_empty(self):
        rid = self.add(lesson())
        g = self.store.get(rid)
        self.assertEqual(g["lesson"]["evidence"], [])
        self.assertEqual(g["lesson"]["evidence_count"], 0)

    def test_as_of_shows_evidence(self):
        rid = self.add(lesson([{"ref": "event-1"}]))
        seq = self.store.head_seq()
        self.add(lesson([{"ref": "event-2"}]))
        self.assertEqual(self.store.get(rid, as_of=seq)["lesson"]["evidence"], [{"ref": "event-1"}])

    def test_intent_has_no_lesson_block(self):
        sid = self.store.append([{"kind": "source", "bytes": "q",
                                  "body": {"origin": {"role": "human", "auth": None, "thread": "t", "sender": "david"},
                                           "capture": {"mode": "promotion"}, "quote_source": "event"}}],
                                author="vesper", run="run-x")["ids"][0]
        rid = self.add({"kind": "intent", "body": {"act": "request", "title": "t", "sources": [sid],
                                                    "claims": [{"cid": "c1", "text": "x"}]}})
        self.assertNotIn("lesson", self.store.get(rid))

    def test_budget_many_long_refs(self):
        ev = [{"ref": f"event-{i:04d}" + "r" * 100, "level": "observed", "note": "n" * 500} for i in range(200)]
        rid = self.add(lesson(ev, applies_when="a" * 5000, uncertainty="u" * 5000))
        g = self.store.get(rid)
        self.assertLessEqual(len(_canon(g).encode("utf-8")), GET_BUDGET)
        self.assertNotIn("budget_exceeded", g)
        self.assertEqual(g["lesson"]["evidence_count"], 200)
        self.assertTrue(g["lesson"]["evidence_truncated"])
        self.assertTrue(g["lesson"]["evidence"])
        self.assertEqual(g["lesson"]["evidence"][0]["ref"], ev[0]["ref"])
        self.assertTrue(all("note" not in x for x in g["lesson"]["evidence"]))

    def test_small_record_keeps_notes(self):
        rid = self.add(lesson([{"ref": "event-1", "note": "short"}]))
        g = self.store.get(rid)
        self.assertNotIn("evidence_truncated", g["lesson"])
        self.assertEqual(g["lesson"]["evidence"][0]["note"], "short")


if __name__ == "__main__":
    unittest.main()
