"""Backlog 60: materialised project state records which fold wrote it; stale state is re-folded.

Live case (Codex, 11-19 05:39Z): after PR #50 changed the newer-source-exists rule, the Fieldpost relay still had
the old projectlog in memory. Its captures re-materialised the ``state`` table with the old rule, and every
reader then served the stale flags although a fresh fold gave none.
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from synthetics import projectlog
from synthetics.projectlog import fold
from synthetics.projects import ProjectStore, _canon


def src(text, thread="t1"):
    return {"kind": "source", "bytes": text,
            "body": {"origin": {"role": "human", "auth": None, "thread": thread, "sender": "david"},
                     "capture": {"mode": "promotion"}, "quote_source": "event"}}


def intent(sources, text="x"):
    return {"kind": "intent", "body": {"act": "request", "title": "t", "sources": sources,
                                        "claims": [{"cid": "c1", "text": text}]}}


def old_fold(entries):
    """Stands in for an older fold rule: flags every intent the way the pre-#50 relay did."""
    st = copy.deepcopy(fold(entries))
    for rec in st["records"].values():
        if "newer-source-exists" not in rec.get("flags", []):
            rec.setdefault("flags", []).append("newer-source-exists")
    return st


class OlderProcessStore(ProjectStore):
    """A long-lived process still running older fold code (stamp or no stamp support)."""
    _fold = staticmethod(old_fold)
    _fold_code = "older-fold-code"


def canon(st):
    return json.loads(_canon(st))


class FoldStampTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "synthetics" / "memory.sqlite3"
        self.store = ProjectStore(self.path, "synthetics")
        s = self.store.append([src("a")], author="vesper", run="run-x")["ids"][0]
        self.rid = self.store.append([intent([s])], author="vesper", run="run-x")["ids"][0]

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def flags(self, st):
        return st["records"][self.rid].get("flags", [])

    def test_state_written_by_older_fold_is_refolded_on_read(self):
        self.assertNotIn("newer-source-exists", self.flags(self.store.state()))
        with OlderProcessStore(self.path, "synthetics") as old:
            old.append([src("b", thread="t9")], author="ingest:fieldpost")
        # The cache now holds the older fold's output ...
        self.assertIn("newer-source-exists", self.flags(self.store.materialised()))
        # ... but a reader running current code serves the current fold, from this and a fresh connection.
        self.assertEqual(canon(self.store.state()), canon(fold(self.store.entries())))
        self.assertNotIn("newer-source-exists", self.flags(self.store.state()))
        with ProjectStore(self.path, "synthetics") as fresh:
            self.assertNotIn("newer-source-exists", self.flags(fresh.state()))

    def test_state_written_without_any_stamp_is_refolded(self):
        # Code from before backlog 60 materialises with DELETE FROM state + one row per fold key, no stamp.
        db = self.store.db
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM state")
        for name, value in old_fold(self.store.entries()).items():
            db.execute("INSERT INTO state(name,value) VALUES (?,?)", (name, _canon(value)))
        db.execute("COMMIT")
        self.assertIn("newer-source-exists", self.flags(self.store.materialised()))
        self.assertNotIn("newer-source-exists", self.flags(self.store.state()))

    def test_next_current_append_rematerialises_with_current_stamp(self):
        with OlderProcessStore(self.path, "synthetics") as old:
            old.append([src("b", thread="t9")], author="ingest:fieldpost")
            self.assertEqual(old.fold_stamp(), "older-fold-code")
        self.assertNotEqual(self.store.fold_stamp(), self.store._fold_code)
        self.store.append([src("c", thread="t8")], author="vesper", run="run-x")
        self.assertEqual(self.store.fold_stamp(), projectlog.FOLD_CODE)
        self.assertEqual(canon(self.store.materialised()), canon(fold(self.store.entries())))
        self.assertNotIn("newer-source-exists", self.flags(self.store.materialised()))

    def test_matching_stamp_serves_cache_without_folding(self):
        calls = []

        class Counting(ProjectStore):
            @staticmethod
            def _fold(entries):
                calls.append(len(entries))
                return fold(entries)

        with Counting(self.path, "synthetics") as st:
            st.state()
        self.assertEqual(calls, [], "a cache stamped by this fold code must be served as is")
        self.assertNotIn("__fold_code__", self.store.materialised())
        self.assertNotIn("__fold_code__", self.store.state())

    def test_reads_never_write_the_cache(self):
        with OlderProcessStore(self.path, "synthetics") as old:
            old.append([src("b", thread="t9")], author="ingest:fieldpost")
        before = self.store.db.execute("SELECT name,value FROM state ORDER BY name").fetchall()
        self.store.state()
        after = self.store.db.execute("SELECT name,value FROM state ORDER BY name").fetchall()
        self.assertEqual(before, after)


class CodeIdTest(unittest.TestCase):
    def test_code_id_tracks_file_bytes_and_dependencies(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.py"
            p.write_text("A = 1\n")
            a = projectlog.code_id(str(p))
            self.assertEqual(a, projectlog.code_id(str(p)))
            self.assertNotEqual(a, projectlog.code_id(str(p), "dep"))
            p.write_text("A = 2\n")
            self.assertNotEqual(a, projectlog.code_id(str(p)))
            missing = str(Path(d) / "gone.py")
            self.assertNotEqual(projectlog.code_id(missing), projectlog.code_id(missing))

    def test_company_store_stamps_its_own_fold_code(self):
        from synthetics import roster
        self.assertNotEqual(roster.FOLD_CODE, projectlog.FOLD_CODE)
        with tempfile.TemporaryDirectory() as d:
            with roster.CompanyStore.at_root(Path(d)) as co:
                co.append([{"kind": "person", "body": {"id": "vesper", "name": "Vesper", "kind": "employee",
                                                       "source": "operator statement", "certainty": "stated"}}],
                          author="operator:test", operator=True)
                self.assertEqual(co.fold_stamp(), roster.FOLD_CODE)
                self.assertIn("vesper", co.state()["people"])


if __name__ == "__main__":
    unittest.main()
