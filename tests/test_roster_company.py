"""M26 P1 step 1: company log (roster layer 2). Design tests 1, 2 (store part), 4; reserved name; C3."""

import tempfile
import unittest
from pathlib import Path

from synthetics import capture, projecttools
from synthetics.projects import ProjectError
from synthetics.roster import CompanyStore, company_fold, company_path, fold_alias, resolve

VESPER = "employee-0dd65b419de4483a8cefd6899bea5fa8"
CODEX = "employee-a22b6a2a8f524cd6b6800b0a620dbe06"


def person(pid, name=None, role=None, kind="employee", source="operator statement", certainty="stated", **kw):
    body = {"id": pid, "kind": kind, "source": source, "certainty": certainty, **kw}
    if name is not None:
        body["name"] = name
    if role is not None:
        body["role"] = role
    return {"kind": "person", "body": body}


def alias(a, pid, source="operator statement"):
    return {"kind": "alias", "body": {"alias": a, "id": pid, "source": source, "certainty": "stated"}}


class CompanyStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.co = CompanyStore.at_root(self.root)

    def tearDown(self):
        self.co.close()
        self.tmp.cleanup()

    def op(self, batch, **kw):
        return self.co.append(batch, author="operator:codex-desktop", operator=True, **kw)

    def seed(self):
        self.op([person("david", "David", "Human colleague setting product direction", kind="human"),
                 person(VESPER, "Vesper", "Synthetics maintainer and internal tools engineer"),
                 person(CODEX, "Codex", "Synthetics manager"),
                 alias("vesper", VESPER), alias("codex-manager", CODEX)])

    def test_path_and_project_name(self):
        self.assertEqual(self.co.path, company_path(self.root))
        self.assertEqual(self.co.path, self.root / "company" / "memory.sqlite3")
        self.assertEqual(self.co.project, "company")

    def test_01_fold_determinism_refold_and_as_of(self):
        self.seed()
        mid = self.co.head_seq()
        self.op([person(VESPER, role="Maintainer and roster owner", source="event-x")])
        self.op([alias("Dana  Example", "david", source="event-y")])
        self.op([{"kind": "unalias", "body": {"alias": "vesper", "source": "event-z", "certainty": "stated"}}])
        refold = company_fold(self.co.entries())
        self.assertEqual(self.co.materialised(), refold)
        self.assertEqual(company_fold(self.co.entries()), refold)
        old = self.co.state(as_of=mid)
        self.assertEqual(old["people"][VESPER]["role_rev"], 1)
        self.assertIn("vesper", old["aliases"])
        now = self.co.state()
        self.assertEqual(now["people"][VESPER]["role_rev"], 2)
        self.assertEqual([r["role"] for r in now["people"][VESPER]["roles"]],
                         ["Synthetics maintainer and internal tools engineer", "Maintainer and roster owner"])
        self.assertNotIn("vesper", now["aliases"])
        self.assertEqual(now["people"]["david"]["aliases"], ["Dana  Example"])

    def test_role_rev_only_on_role_change(self):
        self.seed()
        self.op([person(VESPER, description="writes frozen trees", source="event-a")])
        self.op([person(VESPER, role="Synthetics maintainer and internal tools engineer", source="event-b")])
        p = self.co.person(VESPER)
        self.assertEqual(p["role_rev"], 1)
        self.assertEqual(p["description"], "writes frozen trees")
        # 3 person entries plus the seed's alias entry (backlog 31: alias provenance is in history).
        self.assertEqual([h.get("change") for h in p["history"]], [None, "alias", None, None])
        self.assertEqual(p["history"][1]["alias"], "vesper")

    def test_02_store_grants_and_source_required(self):
        self.seed()
        self.co.grant(CODEX, "write", by="codex-desktop")
        with self.assertRaises(ProjectError) as cm:
            self.co.append([person(VESPER, role="x")], author=VESPER)
        self.assertEqual(cm.exception.code, "no_grant")
        out = self.co.append([person(VESPER, role="y", source="event-q", auth={"channel": "fieldpost"})],
                             author=CODEX)
        e = [x for x in self.co.entries() if x["id"] == out["ids"][0]][0]
        self.assertEqual(e["author"], CODEX)
        self.assertEqual(self.co.person(VESPER)["roles"][-1]["by"], CODEX)
        for bad in ({"source": ""}, {"source": None}):
            b = person(VESPER, role="z")
            b["body"].update(bad)
            with self.assertRaises(ProjectError) as cm:
                self.co.append([b], author=CODEX)
            self.assertEqual(cm.exception.code, "no_source")
        b = person(VESPER, role="z", certainty="authenticated")
        with self.assertRaises(ProjectError) as cm:
            self.co.append([b], author=CODEX)
        self.assertEqual(cm.exception.code, "bad_certainty")

    def test_a3_a4_role_changes_need_auth_and_never_self(self):
        self.seed()
        self.co.grant(CODEX, "write", by="codex-desktop")
        before = self.co.person(VESPER)["role_rev"]
        # A4: an unauthenticated source can edit a description but never change a role (no STALE trigger).
        with self.assertRaises(ProjectError) as cm:
            self.co.append([person(VESPER, role="CEO", source="event-unauth")], author=CODEX)
        self.assertEqual(cm.exception.code, "unauthenticated_role")
        self.co.append([person(VESPER, description="Builds Roster", source="event-unauth")], author=CODEX)
        self.assertEqual(self.co.person(VESPER)["role_rev"], before)
        # Restating the current role is not a revision and stays allowed.
        cur = self.co.person(VESPER)["role"]
        self.co.append([person(VESPER, role=cur, source="event-unauth")], author=CODEX)
        # A3: the manager cannot set its own role, even authenticated; the operator can.
        with self.assertRaises(ProjectError) as cm:
            self.co.append([person(CODEX, role="Owner", source="event-a", auth={"channel": "fieldpost"})],
                           author=CODEX)
        self.assertEqual(cm.exception.code, "own_role")
        self.op([person(CODEX, role="Synthetics manager", source="operator statement")])
        self.assertEqual(self.co.person(CODEX)["role"], "Synthetics manager")

    def test_c3_auth_recorded_as_supplied_never_upgraded(self):
        self.seed()
        p = self.co.person("david")
        self.assertIsNone(p["roles"][0]["auth"])
        self.assertEqual(p["roles"][0]["certainty"], "stated")
        self.op([person("david", role="Founder", source="event-auth", auth={"channel": "ch-1"})])
        p = self.co.person("david")
        self.assertEqual(p["roles"][-1]["auth"], {"channel": "ch-1"})
        self.assertIsNone(p["roles"][0]["auth"])

    def test_04_alias_uniqueness_and_resolve(self):
        self.seed()
        st = self.co.state()
        self.assertEqual(resolve(st, VESPER), {"id": VESPER, "via": "id"})
        # "VESPER" is the name and also the alias "vesper": the alias is reported (M26 feedback (1)).
        self.assertEqual(resolve(st, "  VESPER "), {"id": VESPER, "via": "alias"})
        self.assertEqual(resolve(st, "codex-manager"), {"id": CODEX, "via": "alias"})
        self.assertEqual(resolve(st, "Codex"), {"id": CODEX, "via": "name"})
        self.assertEqual(resolve(st, "nobody"), {"id": None, "via": "unknown"})
        self.assertEqual(resolve(st, ""), {"id": None, "via": "unknown"})
        self.op([alias("Dana Example", "david", source="event-y")])
        self.assertEqual(self.co.resolve("dana   EXAMPLE")["id"], "david")
        for text, holder in (("DANA example", "david"), ("Codex-Manager", CODEX), ("vesper", VESPER),
                             ("DAVID", "david")):
            with self.assertRaises(ProjectError) as cm:
                self.op([alias(text, VESPER if holder != VESPER else CODEX)])
            self.assertEqual(cm.exception.code, "alias_taken")
            self.assertEqual(cm.exception.detail["holder"], holder)
        with self.assertRaises(ProjectError) as cm:
            self.op([person("rowan", "Codex", kind="employee")])
        self.assertEqual(cm.exception.code, "alias_taken")
        with self.assertRaises(ProjectError) as cm:
            self.op([alias("x", "employee-nobody")])
        self.assertEqual(cm.exception.code, "unknown_person")
        # A whole batch is atomic: a colliding second entry rejects the first too.
        head = self.co.head_seq()
        with self.assertRaises(ProjectError):
            self.op([alias("rowan-alias", VESPER), alias("ROWAN-alias", CODEX)])
        self.assertEqual(self.co.head_seq(), head)
        self.assertEqual(fold_alias(" A\tB  c "), "a b c")

    def test_retire_and_retract(self):
        self.seed()
        out = self.op([alias("v2", VESPER)])
        self.op([{"kind": "retract", "body": {"target": out["ids"][0]}}])
        self.assertEqual(self.co.resolve("v2")["id"], None)
        self.op([{"kind": "retire", "body": {"id": CODEX, "reason": "test", "source": "event-r",
                                             "certainty": "stated"}}])
        p = self.co.person("codex-manager")
        self.assertEqual(p["retired"]["reason"], "test")
        self.assertEqual(p["name"], "Codex")

    def test_bad_kinds_and_redact(self):
        with self.assertRaises(ProjectError) as cm:
            self.op([{"kind": "intent", "body": {}}])
        self.assertEqual(cm.exception.code, "bad_kind")
        with self.assertRaises(ProjectError) as cm:
            self.op([person("x", None, kind="employee")])
        self.assertEqual(cm.exception.code, "bad_entry")
        with self.assertRaises(ProjectError) as cm:
            self.op([person("x", "X", kind="robot")])
        self.assertEqual(cm.exception.code, "bad_entry")
        with self.assertRaises(ProjectError):
            self.co.redact("co-x", reason="r", by="op")

    def test_export_import_roundtrip(self):
        self.seed()
        self.op([person("david", role="Founder", source="event-auth", auth={"channel": "ch-1"})])
        self.co.export(rebuild=True)
        other = CompanyStore.import_jsonl(self.co.export_path, self.root / "copy" / "memory.sqlite3", "company")
        try:
            self.assertEqual(other.materialised(), self.co.materialised())
            self.assertEqual(other.person("david")["roles"][-1]["auth"], {"channel": "ch-1"})
        finally:
            other.close()


class ReservedNameTest(unittest.TestCase):
    def test_company_is_not_a_project_name(self):
        self.assertFalse(capture.is_project_name("company"))
        self.assertTrue(capture.is_project_name("synthetics"))
        with self.assertRaises(ValueError):
            projecttools._project("company")
        with tempfile.TemporaryDirectory() as tmp:
            class FakeStore:
                home = Path(tmp) / "home"
            with self.assertRaises(ValueError):
                capture.project_path(FakeStore, "company")
            ref, cap = capture.tag_ref(FakeStore, "company", "sender", "x", [])
            self.assertEqual((ref["valid"], ref["reason"], cap), (False, "invalid", False))


if __name__ == "__main__":
    unittest.main()
