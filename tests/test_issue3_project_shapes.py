"""Issue #3 (noteling/synthetic): a malformed lesson/relate body must be refused before commit, a render error
after commit must never fail the append, and redaction must always rebuild log.jsonl."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from synthetics.projectlog import fold
from synthetics.projects import ProjectError, ProjectStore


def lesson(**extra):
    body = {"statement": "s", "applies_when": "a", "uncertainty": "none known"}
    body.update(extra)
    return {"kind": "lesson", "body": body}


def src(text):
    return {"kind": "source", "bytes": text,
            "body": {"origin": {"role": "human", "auth": None, "thread": "t1", "sender": "david"},
                     "capture": {"mode": "promotion"}, "quote_source": "event"}}


class Issue3Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ProjectStore(self.root / "p" / "memory.sqlite3", "p")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, batch):
        return self.store.append(batch, author="vesper", run="run-x")

    def refused(self, batch, code):
        head = self.store.head_seq()
        with self.assertRaises(ProjectError) as cm:
            self.add(batch)
        self.assertEqual(cm.exception.code, code)
        self.assertEqual(self.store.head_seq(), head, "a refused batch must commit nothing")

    def test_lesson_bad_shapes_refused_before_commit(self):
        for ev in (5, {"ref": "x"}, [5], ["x"], [{"level": "observed"}], [{"ref": ""}], [{"ref": 7}]):
            with self.subTest(evidence=ev):
                self.refused([lesson(evidence=ev)], "bad_lesson")
        for field in ("statement", "title", "lesson_kind"):
            with self.subTest(field=field):
                self.refused([lesson(**{field: 5})], "bad_lesson")
        # The project keeps working, and the shapes already in the live log stay accepted.
        self.add([lesson()])
        self.add([lesson(evidence="reviews/x/run1.txt line 4", title="t", lesson_kind="procedure")])
        self.add([lesson(evidence=[{"ref": "event-1", "level": "observed", "note": "n"}])])
        self.add([lesson(evidence=[])])
        self.assertEqual(self.store.head_seq(), 4)
        self.assertTrue(self.store.view_path.exists())

    def test_relate_from_must_name_an_existing_record_or_claim(self):
        a = self.add([lesson()])["ids"][0]
        b = self.add([lesson()])["ids"][0]
        for bad in (None, 5, "", ["x"]):
            with self.subTest(frm=bad):
                self.refused([{"kind": "relate", "body": {"rel": "disputes", "from": bad, "target": a}}], "bad_rel")
        self.refused([{"kind": "relate", "body": {"rel": "disputes", "target": a}}], "bad_rel")
        self.refused([{"kind": "relate", "body": {"rel": "disputes", "from": "pm-nope", "target": a}}],
                     "unknown_target")
        self.refused([{"kind": "relate", "body": {"rel": "context", "from": f"{b}#c9", "target": a}}],
                     "unknown_target")
        self.refused([{"kind": "relate", "body": {"rel": "disputes", "from": b, "target": 5}}], "unknown_target")
        self.add([{"kind": "relate", "body": {"rel": "disputes", "from": b, "target": a}}])
        st = fold(self.store.entries())
        self.assertEqual(st["records"][a]["status"], "disputed")
        # A claim ref (the documented shape) and a record created earlier in the same batch are accepted.
        s = self.add([src("why")])["ids"][0]
        self.add([{"kind": "intent", "id": "pm-new", "body": {"title": "t", "sources": [s],
                                                               "claims": [{"cid": "c1", "text": "x"}]}},
                  {"kind": "relate", "body": {"rel": "context", "from": "pm-new#c1", "target": s}}])

    def test_fold_ignores_a_malformed_relate_already_in_a_log(self):
        # A log written before this validation existed: later appends must not be wedged by fold().
        old = self.root / "old.jsonl"
        rows = [{"seq": 1, "id": "pm-a", "batch": "b1", "kind": "lesson", "author": "x", "run": None, "at": "",
                 "body": {"statement": "s", "applies_when": "a", "uncertainty": "u"}},
                {"seq": 2, "id": "pm-r", "batch": "b2", "kind": "relate", "author": "x", "run": None, "at": "",
                 "body": {"rel": "disputes", "from": None, "target": "pm-a"}}]
        old.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        st = ProjectStore.import_jsonl(old, self.root / "q" / "memory.sqlite3", "q")
        try:
            self.assertEqual(fold(st.entries())["records"]["pm-a"]["status"], "proposed")
            st.append([lesson()], author="vesper")
            self.assertEqual(st.head_seq(), 3)
        finally:
            st.close()

    def test_render_error_after_commit_does_not_fail_the_append(self):
        with mock.patch("synthetics.projectview.render", side_effect=TypeError("boom")):
            out = self.add([lesson()])
            out2 = self.add([lesson()])
        self.assertEqual(out2["seq_to"], 2)
        lines = self.store.export_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(x)["id"] for x in lines], out["ids"] + out2["ids"])
        self.assertTrue(self.store.write_view())  # the next good render regenerates view.md

    def test_redact_always_rebuilds_the_export(self):
        sid = self.add([src("SECRET-TEXT-xyz")])["ids"][0]
        self.assertIn("SECRET-TEXT-xyz", self.store.export_path.read_text(encoding="utf-8"))
        real = ProjectStore.export

        def flaky(store, rebuild=False):
            if not rebuild:
                raise RuntimeError("failure after the redact commit")
            return real(store, rebuild=rebuild)

        with mock.patch.object(ProjectStore, "export", flaky):
            with self.assertRaises(RuntimeError):
                self.store.redact(sid, reason="test", by="owner")
        text = self.store.export_path.read_text(encoding="utf-8")
        self.assertNotIn("SECRET-TEXT-xyz", text)
        self.assertIn('"kind":"redact"', text)


if __name__ == "__main__":
    unittest.main()
