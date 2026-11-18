"""M26 P1 step 3: rendered Markdown views. Design test 10 (stale), A1 ref status, A2 superseded basis,
A7 marker, C1 self index, drift detection, company people files."""

import os
import stat
import tempfile
import unittest
from pathlib import Path

from synthetics import roster
from synthetics.roster import CompanyStore
from synthetics.store import Store

CODEX = "employee-a22b6a2a8f524cd6b6800b0a620dbe06"
MARK = "PRIVATE VIEW — not official (private to Vesper)"


def person(role, source="operator statement"):
    return {"kind": "person", "body": {"id": CODEX, "kind": "employee", "name": "Codex", "role": role,
                                       "source": source, "certainty": "stated"}}


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "vesper"
        self.store = Store(self.home)
        self.me = self.store.initialize("Vesper", "maintainer")
        with CompanyStore.at_root(self.root) as co:
            co.append([person("Synthetics manager")], author="operator", operator=True)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_10_stale_header_reconcile_clears_history_kept(self):
        self.store.view_put("Codex", claims=[{"cid": "c1", "text": "routes design questions", "kind": "working",
                                              "source": "event-1", "certainty": "observed"}])
        r = self.store.roster_rendered()
        self.assertEqual(r["stale"], [])
        self.assertNotIn("STALE", r["views"][CODEX])
        with CompanyStore.at_root(self.root) as co:
            co.append([person("Head of engineering", "event-2")], author="operator", operator=True)
        r = self.store.roster_rendered()
        self.assertEqual(r["stale"], [CODEX])
        v = r["views"][CODEX]
        self.assertIn("STALE", v)
        self.assertIn("rev 1 → 2", v)
        self.assertIn("'Synthetics manager' → 'Head of engineering'", v)
        self.assertIn("routes design questions", v)  # nothing overwritten
        self.store.view_put("Codex", reconciles=2)
        r = self.store.roster_rendered()
        self.assertEqual(r["stale"], [])
        self.assertNotIn("STALE", r["views"][CODEX])
        self.assertIn("routes design questions", r["views"][CODEX])
        self.assertEqual(self.store.roster_state()["views"][CODEX]["reconciled"][0]["role_rev"], 2)

    def test_a7_marker_and_inferred_label(self):
        self.store.view_put("codex", claims=[{"cid": "c1", "text": "prefers short notes", "kind": "working",
                                              "source": "event-1", "certainty": "inferred"}])
        v = self.store.roster_rendered()["views"][CODEX]
        self.assertTrue(v.startswith("# " + MARK + "\n"))
        self.assertIn("prefers short notes (inferred)", v)

    def test_a1_ref_rendered_with_live_status(self):
        q = self.store.question_ask(CODEX, "which order?", key="q1")
        self.store.view_put("Codex", claims=[{"cid": "o1", "text": "owes me an order decision", "kind": "open",
                                              "source": "event-1", "certainty": "observed", "ref": q["id"]},
                                             {"cid": "o2", "text": "gone", "kind": "open", "source": "event-1",
                                              "certainty": "observed", "ref": "work-" + "0" * 32}])
        v = self.store.roster_rendered()["views"][CODEX]
        self.assertIn(f"→ {q['id']} (open)", v)
        self.assertIn("(not found)", v)

    def test_a2_superseded_basis_flagged_not_changed(self):
        self.store.entity_put("x", "X", "concept")
        old = self.store.memory_put("x", "p", "v1", source="event-1")
        self.store.view_put("Codex", claims=[{"cid": "c1", "text": "fast reviewer", "kind": "working",
                                              "source": old["id"], "certainty": "observed"}])
        self.assertNotIn("basis superseded", self.store.roster_rendered()["views"][CODEX])
        new = self.store.memory_put("x", "p", "v2", source="event-2", replaces=old["id"])
        v = self.store.roster_rendered()["views"][CODEX]
        self.assertIn(f"basis superseded → {new['id']}", v)
        self.assertIn("fast reviewer", v)
        self.assertEqual(self.store.roster_state()["views"][CODEX]["claims"]["c1"]["source"], old["id"])

    def test_c1_self_has_identity_entries_and_memory_index_within_budget(self):
        self.store.self_note("how-i-work", "checkpoint after each step", source="event-1")
        for i in range(60):
            self.store.memory_put(self.me["id"], f"lesson {i}", "x" * 200, source="event-1")
        text = self.store.roster_rendered()["self"]
        self.assertIn("# Self: Vesper", text)
        self.assertIn(self.me["id"], text)
        self.assertIn("checkpoint after each step", text)
        self.assertIn("## memory index", text)
        self.assertLessEqual(len(text.encode("utf-8")), roster.SELF_BUDGET)
        self.assertIn("truncated", text)
        self.assertNotIn("x" * 100, text)  # gists only, never full memory values

    def test_files_written_private_and_drift_detected(self):
        self.store.view_put("Codex", claims=[{"cid": "c1", "text": "t", "kind": "history", "source": "event-1",
                                              "certainty": "observed"}])
        idx = self.store.roster_render()
        d = self.home / "roster"
        self.assertEqual(sorted(idx), ["self.md", "views/" + CODEX + ".md"])
        for rel in idx:
            self.assertEqual(stat.S_IMODE(os.stat(d / rel).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(d / "views").st_mode), 0o700)
        self.assertEqual(roster.drift(d), [])
        (d / "self.md").write_text("hand edit\n")
        self.assertEqual(roster.drift(d), [{"file": "self.md", "problem": "roster_view_drift"}])
        self.store.roster_render()
        self.assertEqual(roster.drift(d), [])
        self.assertIn("# Self: Vesper", (d / "self.md").read_text())

    def test_company_people_files_official_no_private_marker(self):
        self.store.view_put("Codex", claims=[{"cid": "c1", "text": "secret-view-text", "kind": "history",
                                              "source": "event-1", "certainty": "observed"}])
        with CompanyStore.at_root(self.root) as co:
            idx = co.render()
        text = (self.root / "company" / "people" / (CODEX + ".md")).read_text()
        self.assertEqual(list(idx), ["people/" + CODEX + ".md"])
        self.assertIn("official company profile", text)
        self.assertIn("role (rev 1): Synthetics manager", text)
        self.assertIn("unauthenticated source", text)
        self.assertNotIn("PRIVATE VIEW", text)
        self.assertNotIn("secret-view-text", text)

    def test_render_is_deterministic(self):
        self.store.view_put("Codex", claims=[{"cid": "c1", "text": "t", "kind": "history", "source": "event-1",
                                              "certainty": "observed"}])
        self.assertEqual(self.store.roster_rendered(), self.store.roster_rendered())


if __name__ == "__main__":
    unittest.main()
