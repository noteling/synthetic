import json
import random
import tempfile
import unittest
from pathlib import Path

from synthetics.projectlog import fold
from synthetics.projects import ProjectError, ProjectStore, _canon


def src(text, role="human", auth=None, thread="t1"):
    return {"kind": "source", "bytes": text,
            "body": {"origin": {"role": role, "auth": auth, "thread": thread, "sender": "david"},
                     "capture": {"mode": "promotion"}, "quote_source": "event"}}


def intent(sources, claims, title="t"):
    return {"kind": "intent", "body": {"act": "request", "title": title, "sources": sources,
                                        "claims": [{"cid": c, "text": t} for c, t in claims]}}


class ProjectStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ProjectStore(self.root / "synthetics" / "memory.sqlite3", "synthetics")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, batch, **kw):
        return self.store.append(batch, author="vesper", run="run-x", **kw)

    def test_06_a1_chain_and_11_stale_head(self):
        s1 = self.add([src("a1")])["ids"][0]
        r1 = self.add([intent([s1], [("c1", "project memory"), ("c2", "native memory")])])["ids"][0]
        s2 = self.add([src("a2")])["ids"][0]
        r2 = self.add([intent([s2], [("c1", "project owned"), ("c2", "native not enough")])])["ids"][0]
        self.add([{"kind": "relate", "body": {"rel": "revises", "from": f"{r2}#c2", "target": f"{r1}#c2",
                                               "expected_head": f"{r1}#c2"}},
                  {"kind": "relate", "body": {"rel": "carries", "from": f"{r2}#c1", "target": f"{r1}#c1"}}])
        st = self.store.state()
        self.assertEqual(st["claims"][f"{r1}#c2"]["status"], "superseded")
        current = [k for k, c in st["claims"].items() if c["status"] == "current"]
        self.assertEqual(sorted(current), sorted([f"{r1}#c1", f"{r2}#c1", f"{r2}#c2"]))
        before = self.store.head_seq()
        with self.assertRaises(ProjectError) as cm:
            self.add([{"kind": "relate", "body": {"rel": "revises", "from": f"{r2}#c1", "target": f"{r1}#c2",
                                                   "expected_head": f"{r1}#c2"}}])
        self.assertEqual(cm.exception.code, "stale_head")
        self.assertEqual(self.store.head_seq(), before)

    def test_07_quote_too_large(self):
        before = self.store.db.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        with self.assertRaises(ProjectError) as cm:
            self.add([src("x" * 65537)])
        self.assertEqual(cm.exception.code, "quote_too_large")
        self.assertEqual(self.store.head_seq(), 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM audit").fetchone()[0], before)
        sid = self.add([src("y" * 65536)])["ids"][0]
        out, cur = "", 0
        while cur is not None:
            ch = self.store.get(sid, part="quote", cursor=cur)
            out += ch["chunk"]
            cur = ch["next_cursor"]
        self.assertEqual(out, "y" * 65536)

    def test_04_long_quote_paging_multibyte(self):
        text = ("é漢😀ab" * 2000)
        sid = self.add([src(text)])["ids"][0]
        g = self.store.get(sid)
        raw = text.encode("utf-8")
        self.assertLessEqual(len(g["quote_excerpt"].encode("utf-8")), 2048)
        self.assertEqual(g["quote_bytes"], len(raw))
        self.assertTrue(g["quote_truncated"])
        out, cur = g["quote_excerpt"], g["quote_cursor"]
        while cur is not None:
            ch = self.store.get(sid, part="quote", cursor=cur)
            self.assertLessEqual(len(ch["chunk"].encode("utf-8")), 4096)
            out += ch["chunk"]
            cur = ch["next_cursor"]
        self.assertEqual(out, text)
        mid = raw.index("é".encode()) + 1
        with self.assertRaises(ProjectError) as cm:
            self.store.get(sid, part="quote", cursor=mid)
        self.assertEqual(cm.exception.code, "bad_cursor")

    def test_05_budget_maximal_record(self):
        sid = self.add([src("q" * 65536)])["ids"][0]
        rid = self.add([intent([sid], [(f"c{i}", "w" * 300) for i in range(8)], title="T" * 120)])["ids"][0]
        for i in range(16):
            other = self.add([intent([sid], [("c1", "r" * 300)])])["ids"][0]
            self.add([{"kind": "relate", "body": {"rel": "refines", "from": f"{other}#c1",
                                                   "target": f"{rid}#c{i % 8}"}}])
        g = self.store.get(rid, history=5)
        self.assertLessEqual(len(_canon(g).encode("utf-8")), 8192)
        self.assertNotIn("budget_exceeded", g)

    def test_03_history_pagination(self):
        sid = self.add([src("q")])["ids"][0]
        rid = self.add([intent([sid], [("c1", "base")])])["ids"][0]
        others = []
        for i in range(40):
            o = self.add([intent([sid], [("c1", "z" * 300)], title="h%d" % i)])["ids"][0]
            self.add([{"kind": "relate", "body": {"rel": "refines", "from": f"{o}#c1", "target": f"{rid}#c1"}}])
            others.append(o)
        seen, cur = [], 0
        while cur is not None:
            g = self.store.get(rid, history=5, cursor=cur)
            self.assertLessEqual(len(_canon(g).encode("utf-8")), 8192)
            seen += [h["id"] for h in g["history"]]
            cur = g["next_cursor"]
        self.assertEqual(sorted(seen), sorted(others))
        self.assertEqual(len(seen), len(set(seen)))

    def test_12_atomic_batch(self):
        sid = self.add([src("ok")])["ids"][0]
        lines = self.store.export_path.read_text().count("\n")
        audits = self.store.db.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        with self.assertRaises(ProjectError):
            self.add([src("fine"), intent([sid], [("c1", "x")]), {"kind": "relate", "body": {"rel": "bogus"}}])
        self.assertEqual(self.store.head_seq(), 1)
        self.assertEqual(self.store.export_path.read_text().count("\n"), lines)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM audit").fetchone()[0], audits)

    def test_21_not_human_source(self):
        sec = self.add([src("secretary says", role="secretary")])["ids"][0]
        out = self.add([src("we replied", role="out")])["ids"][0]
        for s in (sec, out):
            with self.assertRaises(ProjectError) as cm:
                self.add([intent([s], [("c1", "x")])])
            self.assertEqual(cm.exception.code, "not_human_source")
        self.assertEqual(self.store.state()["sources"][sec]["quote_source"], "secondhand")

    def test_operator_kinds_and_key_idempotency(self):
        sid = self.add([src("s")])["ids"][0]
        with self.assertRaises(ProjectError) as cm:
            self.add([{"kind": "redact", "body": {"target": sid}}])
        self.assertEqual(cm.exception.code, "operator_only")
        a = self.add([src("k")], key="k1")
        b = self.add([src("k")], key="k1")
        self.assertEqual(a["ids"], b["ids"])
        self.assertTrue(b["replayed"])
        with self.assertRaises(ProjectError) as cm:
            self.add([src("different")], key="k1")
        self.assertEqual(cm.exception.code, "key_conflict")

    def test_09_refold_equals_materialised_and_10_as_of_and_import(self):
        rng = random.Random(7)
        sources, records, claims, lessons = [], [], [], []
        for i in range(240):  # b27: a repeated redact now raises already_redacted, so fewer appends land
            choice = rng.random()
            try:
                if choice < 0.25 or not sources:
                    role = rng.choice(["human", "human", "secretary", "out"])
                    sources.append(self.add([src(f"s{i} é", role=role, auth=rng.choice([None, {"ch": "k"}]),
                                                 thread=rng.choice(["t1", "t2"]))])["ids"][0])
                elif choice < 0.45:
                    s = rng.choice(sources)
                    r = self.add([intent([s], [("c1", f"x{i}"), ("c2", f"y{i}")])])["ids"][0]
                    records.append(r)
                    claims += [f"{r}#c1", f"{r}#c2"]
                elif choice < 0.55:
                    lessons.append(self.add([{"kind": "lesson", "body": {
                        "statement": f"l{i}", "applies_when": "x", "uncertainty": "none known"}}])["ids"][0])
                elif choice < 0.70 and len(claims) > 1:
                    a, b = rng.sample(claims, 2)
                    rel = rng.choice(["revises", "refines", "carries"])
                    st = self.store.state()
                    body = {"rel": rel, "from": a, "target": b}
                    if rel == "revises":
                        body["expected_head"] = st["claims"][b]["head"]
                    self.add([{"kind": "relate", "body": body}])
                elif choice < 0.78 and lessons:
                    self.add([{"kind": "status", "body": {"target": rng.choice(lessons),
                                                          "to": rng.choice(["accepted", "disputed", "retired"])}}])
                elif choice < 0.86:
                    ents = self.store.entries()
                    self.add([{"kind": "retract", "body": {"target": rng.choice(ents)["id"], "reason": "r"}}])
                elif choice < 0.93:
                    self.add([{"kind": "considered", "body": {"source": rng.choice(sources),
                                                              "outcome": "not-captured", "reason": "routine"}}])
                else:
                    self.store.append([{"kind": "redact", "body": {"target": rng.choice(sources), "reason": "r"}}],
                                      author="operator:codex", operator=True)
            except ProjectError:
                pass
        self.assertGreaterEqual(self.store.head_seq(), 200)
        self.assertEqual(json.loads(_canon(fold(self.store.entries()))), self.store.materialised())
        for n in rng.sample(range(1, self.store.head_seq()), 10):
            self.assertEqual(self.store.state(as_of=n), fold(self.store.entries(as_of=n)))
            self.assertEqual(self.store.state(as_of=n)["through"], n)
        imported = ProjectStore.import_jsonl(self.store.export_path, self.root / "copy" / "memory.sqlite3",
                                             "synthetics")
        try:
            self.assertEqual(imported.materialised(), self.store.materialised())
            live = self.store.state()["sources"]
            self.assertTrue(live)
            for sid, s in live.items():
                self.assertEqual(imported.state()["sources"][sid]["auth"], s["auth"])
        finally:
            imported.close()


if __name__ == "__main__":
    unittest.main()
