"""M26 P1 step 2: the private roster log (layers 1 and 3). Design tests 6, 9, 15; schema 5; fold determinism."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from synthetics.projects import ProjectError
from synthetics.roster import CompanyStore, private_fold
from synthetics.store import Store

CODEX = "employee-a22b6a2a8f524cd6b6800b0a620dbe06"


def claim(text, source="event-1", kind="working", certainty="observed", cid=None):
    c = {"text": text, "source": source, "kind": kind, "certainty": certainty}
    if cid:
        c["cid"] = cid
    return c


class PrivateRosterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "vesper"
        self.store = Store(self.home)
        self.me = self.store.initialize("Vesper", "maintainer")
        with CompanyStore.at_root(self.root) as co:
            co.append([{"kind": "person", "body": {"id": CODEX, "kind": "employee", "name": "Codex",
                                                    "role": "Synthetics manager", "source": "operator statement",
                                                    "certainty": "stated"}},
                       {"kind": "alias", "body": {"alias": "codex-manager", "id": CODEX,
                                                   "source": "operator statement", "certainty": "stated"}}],
                      author="operator", operator=True)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def company(self):
        with CompanyStore.at_root(self.root) as co:
            return co.state()

    # test 6
    def test_unsourced_claim_rejected(self):
        with self.assertRaises(ProjectError) as cm:
            self.store.view_put("codex-manager", claims=[claim("reviews fast", source="")])
        self.assertEqual(cm.exception.code, "unsourced_claim")
        with self.assertRaises(ProjectError) as cm:
            self.store.self_note("how-i-work", "checkpoint often", source=" ")
        self.assertEqual(cm.exception.code, "unsourced_claim")
        self.assertEqual(self.store.roster_entries(), [])

    def test_unknown_person_rejected(self):
        with self.assertRaises(ProjectError) as cm:
            self.store.view_put("nobody", claims=[claim("x")])
        self.assertEqual(cm.exception.code, "unknown_person")

    def test_alias_resolves_to_canonical_and_inferred_kept(self):
        [e] = self.store.view_put("Codex-Manager", claims=[claim("prefers frozen trees", certainty="inferred",
                                                                  cid="c1")])
        self.assertEqual(e["body"]["person"], CODEX)
        self.assertEqual(e["body"]["basis_role_rev"], 1)
        view = self.store.roster_state()["views"][CODEX]
        self.assertEqual(view["claims"]["c1"]["certainty"], "inferred")

    def test_bad_values_rejected(self):
        for c, code in ((claim("x", kind="gossip"), "bad_entry"), (claim("x", certainty="sure"), "bad_certainty"),
                        (claim("x" * 700), "too_large")):
            with self.assertRaises(ProjectError) as cm:
                self.store.view_put(CODEX, claims=[c])
            self.assertEqual(cm.exception.code, code)
        with self.assertRaises(ProjectError) as cm:
            self.store.self_note("secrets", "x", source="s")
        self.assertEqual(cm.exception.code, "bad_entry")

    def test_revise_retract_reconcile(self):
        self.store.view_put(CODEX, claims=[claim("a", cid="c1"), claim("b", cid="c2")])
        self.store.view_put(CODEX, claims=[claim("a2", cid="c1")], retract=[{"cid": "c2", "reason": "wrong"}])
        view = self.store.roster_state()["views"][CODEX]
        self.assertEqual(view["claims"]["c1"]["text"], "a2")
        self.assertNotIn("c2", view["claims"])
        self.assertEqual(view["retracted"]["c2"]["text"], "b")
        with self.assertRaises(ProjectError) as cm:
            self.store.view_put(CODEX, retract=["c2"])
        self.assertEqual(cm.exception.code, "unknown_claim")
        with self.assertRaises(ProjectError) as cm:
            self.store.view_put(CODEX, reconciles=2)
        self.assertEqual(cm.exception.code, "bad_reconcile")
        self.store.view_put(CODEX, reconciles=1)
        self.assertEqual(len(self.store.roster_state()["views"][CODEX]["reconciled"]), 1)

    def test_batch_is_atomic(self):
        with self.assertRaises(ProjectError):
            self.store.view_put(CODEX, claims=[claim("a", cid="c1")], retract=["missing"])
        self.assertEqual(self.store.roster_entries(), [])

    def test_idempotent_key(self):
        a = self.store.view_put(CODEX, claims=[claim("a", cid="c1")], key="k1")
        b = self.store.view_put(CODEX, claims=[claim("a", cid="c1")], key="k1")
        self.assertEqual([e["id"] for e in a], [e["id"] for e in b])
        self.assertEqual(len(self.store.roster_entries()), 1)
        with self.assertRaises(ValueError):
            self.store.view_put(CODEX, claims=[claim("other", cid="c1")], key="k1")
        n1 = self.store.self_note("how-i-work", "t", source="s", key="s1")
        n2 = self.store.self_note("how-i-work", "t", source="s", key="s1")
        self.assertEqual(n1["id"], n2["id"])

    # test 9
    def test_private_writes_never_change_company(self):
        before = self.company()
        self.store.view_put(CODEX, claims=[claim("is actually the CEO", kind="history", cid="c1")])
        self.store.self_note("commitments", "I am the manager now", source="s")
        after = self.company()
        self.assertEqual(before, after)
        self.assertEqual(after["people"][CODEX]["role"], "Synthetics manager")

    # test 15
    def test_finished_run_is_fenced(self):
        run = self.store.run_begin()
        with Store(self.home, run_id=run["id"]) as s:
            s.view_put(CODEX, claims=[claim("a")])
        self.store.run_finish(run["id"], "done")
        with Store(self.home, run_id=run["id"]) as s:
            with self.assertRaises(ValueError):
                s.view_put(CODEX, claims=[claim("b")])
            with self.assertRaises(ValueError):
                s.self_note("how-i-work", "b", source="s")
        entries = self.store.roster_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["run"], run["id"])

    def test_a5_same_source_upgrade_rejected(self):
        self.store.view_put(CODEX, claims=[claim("prefers frozen trees", source="event-1", certainty="inferred",
                                                 cid="c1")])
        with self.assertRaises(ProjectError) as cm:
            self.store.view_put(CODEX, claims=[claim("prefers frozen trees", source="event-1", cid="c1")])
        self.assertEqual(cm.exception.code, "same_source_upgrade")
        self.store.view_put(CODEX, claims=[claim("prefers frozen trees", source="event-2", cid="c1")])
        self.assertEqual(self.store.roster_state()["views"][CODEX]["claims"]["c1"]["certainty"], "observed")

    def test_a1_open_thread_claims_need_a_ref(self):
        with self.assertRaises(ProjectError) as cm:
            self.store.view_put(CODEX, claims=[claim("owes me a review", kind="open")])
        self.assertEqual(cm.exception.code, "bad_entry")
        c = dict(claim("owes me a review", kind="open", cid="o1"), ref="question-7ca9582736cf452fb5e592ca58e213af")
        self.store.view_put(CODEX, claims=[c])
        got = self.store.roster_state()["views"][CODEX]["claims"]["o1"]
        self.assertEqual(got["ref"], "question-7ca9582736cf452fb5e592ca58e213af")

    def test_refold_equals_materialised(self):
        self.store.view_put(CODEX, claims=[claim("a", cid="c1")])
        self.store.self_note("how-i-work", "t", source="s")
        self.store.view_put(CODEX, retract=[{"cid": "c1", "reason": "r"}])
        entries = self.store.roster_entries()
        self.assertEqual(private_fold(entries), private_fold(list(reversed(entries))))
        self.assertEqual(private_fold(entries), self.store.roster_state())

    def test_no_company_log_means_unknown_person_and_no_file(self):
        other = self.root / "elsewhere" / "home"
        with Store(other) as s:
            s.initialize("X", "y")
            with self.assertRaises(ProjectError) as cm:
                s.view_put("codex-manager", claims=[claim("a")])
            self.assertEqual(cm.exception.code, "unknown_person")
        self.assertFalse((self.root / "elsewhere" / "company").exists())

    def test_v4_home_migrates_to_v5(self):
        self.store.close()
        db = sqlite3.connect(self.home / "state.sqlite3")
        db.execute("DROP TABLE roster_log")
        db.execute("PRAGMA user_version=4")
        db.commit()
        db.close()
        self.store = Store(self.home)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 5)
        self.assertEqual(self.store.roster_entries(), [])


if __name__ == "__main__":
    unittest.main()
