"""M21 step 5: tests 2 (substring), 15 (unbound not captured), 16 (promotion), 17 (coverage split privacy)."""
import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from synthetics import capture
from synthetics.projects import ProjectError, ProjectStore
from synthetics.store import Store
from tests.humans_fixture import write_host_humans

T = "codex:00000000-0000-4000-8000-000000000001"


def tree_digest(root: Path, skip_home: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and skip_home not in p.parents:
            out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


class PromotionTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("SYNTHETICS_SESSION_RUN", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mgr = Store(self.root / "manager")
        write_host_humans(self.mgr.home)
        self.other = Store(self.root / "rowan")

    def tearDown(self):
        self.mgr.close()
        self.other.close()
        self.tmp.cleanup()

    def ps(self, project):
        return ProjectStore(self.root / "projects" / project / "memory.sqlite3", project)

    def test_2_substring_bytes_not_normalised(self):
        ev = self.mgr.feed("david", "status, \n\nIs that fair? café", source="codex-desktop:x", thread=T)
        with self.assertRaises(ProjectError) as cm:
            capture.promote(self.mgr, "synthetics", event_id=ev["id"], text="status, Is that fair?", author="m")
        self.assertEqual(cm.exception.code, "not_substring")
        out = capture.promote(self.mgr, "synthetics", event_id=ev["id"], text="Is that fair? café", author="m")
        with self.ps("synthetics") as ps:
            e = [x for x in ps.entries() if x["id"] == out["ids"][0]][0]
            self.assertEqual(ps.source_bytes(e["id"]), "Is that fair? café".encode())
        self.assertIsNone(e["body"]["quote_checked_by"])
        self.assertEqual(e["body"]["capture"]["mode"], "promotion")
        self.assertEqual(e["body"]["origin"]["role"], "human")

    def test_2_file_origin_needs_granted_root(self):
        granted = self.root / "docs"
        granted.mkdir()
        f = granted / "a.md"
        f.write_text("alpha beta gamma")
        outside = self.root / "secret.txt"
        outside.write_text("alpha beta gamma")
        with self.assertRaises(ProjectError) as cm:
            capture.promote(self.mgr, "synthetics", file=str(outside), text="beta", author="m",
                            granted_roots=[granted])
        self.assertEqual(cm.exception.code, "unknown_origin")
        out = capture.promote(self.mgr, "synthetics", file=str(f), text="beta", author="m", granted_roots=[granted])
        self.assertEqual(len(out["ids"]), 1)

    def test_7_promotion_size(self):
        big = "x" * 65537
        ev = self.mgr.feed("david", big, source="codex-desktop:x", thread=T)
        with self.assertRaises(ProjectError) as cm:
            capture.promote(self.mgr, "synthetics", event_id=ev["id"], text=big, author="m")
        self.assertEqual(cm.exception.code, "quote_too_large")
        self.assertFalse((self.root / "projects" / "synthetics" / "memory.sqlite3").exists()
                         and self.ps("synthetics").entries())

    def test_15_unbound_input_not_captured(self):
        # unbound, untagged human message: no project entry, only in this receiver's private report
        ev = self.mgr.feed("david", "unbound note", source="telegram:1000000001", thread=T)
        self.assertFalse((self.root / "projects").exists())
        rep = capture.uncovered(self.mgr, "manager")
        self.assertEqual([r["event_id"] for r in rep["private"]], [ev["id"]])
        self.assertEqual(capture.uncovered(self.other, "rowan")["private"], [])
        # an event on another binding's thread but outside its prefixes is not captured either
        capture.bind(self.mgr, "synthetics", thread=T, source_prefixes=["codex-desktop:"], by="m", reason="t")
        self.mgr.feed("david", "tg again", source="telegram:1000000001", thread=T)
        with self.ps("synthetics") as ps:
            self.assertEqual([e["kind"] for e in ps.entries()], ["binding"])
        # peer (fieldpost) traffic is out of scope for the private report
        self.mgr.feed("employee-x", "peer note", source="fieldpost")
        self.assertEqual(len(capture.uncovered(self.mgr, "manager")["private"]), 2)

    def test_16_promotion_visibility_and_marks(self):
        ev = self.mgr.feed("david", "Do X for synthetics. Noteling: do Y.", source="codex-desktop:x", thread=T)
        out = capture.promote(self.mgr, "synthetics", event_id=ev["id"], text="Do X for synthetics.",
                              author="manager", considered={"outcome": "not-captured", "reason": "routine"})
        with self.ps("noteling") as ps:
            self.assertEqual(ps.entries(), [])
        with self.ps("synthetics") as ps:
            ps.grant("manager", "write", by="op")
            self.assertTrue(ps.may_read("manager"))
            self.assertFalse(ps.may_read("rowan"))
        row = self.mgr.db.execute("SELECT outcome, project FROM coverage_marks WHERE event_id=?",
                                  (ev["id"],)).fetchone()
        self.assertEqual(tuple(row), ("promoted", "synthetics"))
        self.assertEqual(capture.uncovered(self.mgr, "manager")["private"], [])
        self.assertEqual(capture.uncovered(self.other, "rowan")["project"], [])
        # an event outside the promoter's home is rejected
        foreign = self.other.feed("david", "rowan only", source="codex-desktop:x", thread=T)
        with self.assertRaises(ProjectError) as cm:
            capture.promote(self.mgr, "synthetics", event_id=foreign["id"], text="rowan", author="manager")
        self.assertEqual(cm.exception.code, "unknown_origin")
        self.assertEqual(len(out["ids"]), 2)

    def test_18_report_project_part_until_considered(self):
        capture.bind(self.mgr, "synthetics", thread=T, source_prefixes=["codex-desktop:"], by="m", reason="t")
        self.mgr.feed("david", "mentions noteling, no insight", source="codex-desktop:x", thread=T)
        rep = capture.uncovered(self.mgr, "manager")
        self.assertEqual(rep["project_total"], 1)
        self.assertEqual(rep["private"], [])   # captured, so not private
        sid = rep["project"][0]["id"]
        with self.ps("synthetics") as ps:
            ps.append([{"kind": "considered", "body": {"source": sid, "outcome": "not-captured",
                                                       "reason": "routine"}}], author="manager")
        self.assertEqual(capture.uncovered(self.mgr, "manager")["project_total"], 0)

    def test_17_skip_writes_only_own_home(self):
        capture.bind(self.mgr, "synthetics", thread=T, source_prefixes=["codex-desktop:"], by="m", reason="t")
        ev = self.mgr.feed("david", "private aside", source="telegram:1000000001", thread=T)
        before = tree_digest(self.root, self.root / "manager")
        capture.skip(self.mgr, ev["id"], "personal")
        self.assertEqual(tree_digest(self.root, self.root / "manager"), before)
        self.assertEqual(capture.uncovered(self.mgr, "manager")["private"], [])
        actions = [r[0] for r in self.mgr.db.execute("SELECT action FROM audit")]
        self.assertIn("coverage.skipped", actions)
        with self.assertRaises(ValueError):
            capture.skip(self.mgr, ev["id"], "  ")

    def test_report_starts_at_deploy_and_pages(self):
        old = self.mgr.feed("david", "pre", source="codex-desktop:x", thread=T)
        self.mgr.db.execute("UPDATE capture_meta SET v=? WHERE k='uncovered_from_seq'",
                            (str(self.mgr.db.execute("SELECT MAX(seq) FROM events").fetchone()[0]),))
        ids = [self.mgr.feed("david", "m%d" % i, source="codex-desktop:x", thread=T)["id"] for i in range(5)]
        rep = capture.uncovered(self.mgr, "manager", limit=2)
        got = [r["event_id"] for r in rep["private"]]
        while rep["next_cursor"]:
            rep = capture.uncovered(self.mgr, "manager", limit=2, cursor=rep["next_cursor"])
            got += [r["event_id"] for r in rep["private"]]
        self.assertEqual(got, ids)
        self.assertNotIn(old["id"], got)

    def test_snapshot_counts(self):
        self.mgr.initialize("Codex", "manager", "test")
        self.mgr.feed("david", "hello", source="codex-desktop:x", thread=T)
        snap = self.mgr.context()
        self.assertEqual(snap["project_uncovered"], {"project": 0, "private": 1, "private_more": False})


if __name__ == "__main__":
    unittest.main()
