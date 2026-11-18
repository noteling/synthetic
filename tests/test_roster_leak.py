"""M26 P1 step 7: isolation and doctor. Design tests 7 (no view bytes outside the owner's private layer,
A7 incl. runs/ receipts and the audit table), 8 (promote refuses roster/), 14 (doctor roster checks), C6."""

import json
import os
import plistlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from synthetics import capture, doctor
from synthetics.mcp import Server
from synthetics.capture import project_path
from synthetics.projects import ProjectError, ProjectStore
from synthetics.roster import CompanyStore
from synthetics.store import Store

CODEX = "employee-a22b6a2a8f524cd6b6800b0a620dbe06"
ROWAN = "employee-28ed6d90e0fe4b42b64bae0e41e82cba"
SECRET = "zq-private-view-canary-7731"
MARK = "PRIVATE VIEW"


def person(pid, name, role):
    return {"kind": "person", "body": {"id": pid, "kind": "employee", "name": name, "role": role,
                                       "source": "operator statement", "certainty": "stated"}}


def all_bytes(paths):
    out = b""
    for base in paths:
        base = Path(base)
        files = [base] if base.is_file() else [p for p in base.rglob("*") if p.is_file()]
        for f in files:
            out += f.read_bytes()
    return out


class RosterLeakTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.a_home, self.b_home = self.root / "vesper", self.root / "rowan"
        self.a = Store(self.a_home)
        self.me = self.a.initialize("Vesper", "maintainer")["id"]
        with Store(self.b_home) as b:
            b.initialize("Rowan", "Noteling maintainer")
        with CompanyStore.at_root(self.root) as co:
            co.append([person(CODEX, "Codex", "Synthetics manager"), person(ROWAN, "Rowan", "Noteling maintainer")],
                      author="operator", operator=True)

    def tearDown(self):
        self.a.close()
        self.tmp.cleanup()

    def test_7_view_text_stays_in_owner_private_layer(self):
        run = self.a.run_begin()["id"]
        with Store(self.a_home, run_id=run) as bound:
            server = Server(bound, run_id=run)
            server.call("view_put", {"person": "Codex", "key": "k1", "claims": [
                {"cid": "c1", "text": SECRET, "kind": "working", "source": "event-1", "certainty": "observed"}]})
            server.call("self_note", {"section": "how-i-work", "text": SECRET + "-self", "source": "event-2", "key": "k2"})
            # a project exists and receives an ordinary write from this home
            with ProjectStore(project_path(bound, "synthetics"), "synthetics") as ps:
                ps.export()
        self.a.roster_render()
        with CompanyStore.at_root(self.root) as co:
            co.render()
            co.export()  # company log.jsonl lands under company/ and is grepped below
        # owner's private layer has it (sanity)
        self.assertIn(SECRET.encode(), all_bytes([self.a_home / "roster"]))
        self.assertIn(MARK.encode(), all_bytes([self.a_home / "roster" / "views"]))
        # A7: home runs/ receipts and audit table never carry view text or the marker
        runs = all_bytes([self.a_home / "runs"]) if (self.a_home / "runs").exists() else b""
        self.assertTrue(runs, "receipts were written")
        self.assertNotIn(SECRET.encode(), runs)
        self.assertNotIn(MARK.encode(), runs)
        audit = json.dumps([tuple(r) for r in self.a.db.execute("SELECT action,record_id,data FROM audit")])
        self.assertNotIn(SECRET, audit)
        self.assertNotIn(MARK, audit)
        # B's home, B's context, company log + people files + export, project logs
        with Store(self.b_home) as b:
            ctx = json.dumps(b.context(), ensure_ascii=False)
        self.assertNotIn(SECRET, ctx)
        self.assertNotIn(MARK, ctx)
        others = all_bytes([self.b_home, self.root / "company", self.root / "projects"]
                           + [p for p in self.root.glob("*/projects")])
        self.assertNotIn(SECRET.encode(), others)
        self.assertNotIn(MARK.encode(), others)

    def test_8_promote_refuses_private_roster(self):
        self.a.view_put("Codex", claims=[{"cid": "c1", "text": SECRET, "kind": "working", "source": "event-1",
                                          "certainty": "observed"}])
        self.a.roster_render()
        view = next((self.a_home / "roster" / "views").glob("*.md"))
        with self.assertRaises(ProjectError) as cm:
            capture.promote(self.a, "synthetics", text=SECRET, author=self.me, file=str(view),
                            granted_roots=[self.root])
        self.assertEqual(cm.exception.code, "private_roster")
        # another home's roster dir is refused too, even under a granted root
        with Store(self.b_home) as b:
            b.roster_render()
        with self.assertRaises(ProjectError) as cm:
            capture.promote(self.a, "synthetics", text="Rowan", author=self.me,
                            file=str(self.b_home / "roster" / "self.md"), granted_roots=[self.root])
        self.assertEqual(cm.exception.code, "private_roster")

    def _codes(self, report):
        return {f["code"] for f in report["findings"]}

    def test_14_doctor_roster_checks(self):
        (self.a_home / "humans.json").write_text(json.dumps({"v": 1, "senders": ["david"]}))
        self.a.view_put("Codex", claims=[{"cid": "c1", "text": "x", "kind": "working", "source": "e",
                                          "certainty": "observed"}])
        self.a.roster_render()
        report = doctor.diagnose(self.a_home, launchd=lambda label: None)
        codes = self._codes(report)
        self.assertIn("human_without_profile", codes)
        self.assertEqual(report["roster"], {"people": 2, "views": 1})
        self.assertNotIn("roster_view_drift", codes)
        view = next((self.a_home / "roster" / "views").glob("*.md"))
        view.write_text(view.read_text() + "hand edit\n")
        # alias collision: write directly (the fold forbids it via append), as a damaged/merged log would
        with CompanyStore.at_root(self.root) as co:
            co.append([{"kind": "alias", "body": {"alias": "Ro", "id": ROWAN, "source": "s", "certainty": "stated"}}],
                      author="operator", operator=True)
        db = sqlite3.connect(self.root / "company" / "memory.sqlite3")
        db.execute("UPDATE entries SET body=? WHERE kind='alias'",
                   (json.dumps({"alias": "Codex", "id": ROWAN, "source": "s", "certainty": "stated"}),))
        db.commit()
        db.close()
        report = doctor.diagnose(self.a_home, launchd=lambda label: None)
        codes = self._codes(report)
        self.assertIn("roster_view_drift", codes)
        self.assertIn("alias_collision", codes)
        self.assertFalse(report["ok"])

    def test_c6_collector_dirs_never_cover_a_home(self):
        docs = self.root / "docs"
        docs.mkdir()
        ok = doctor.diagnose(self.a_home, launchd=lambda label: None, collector_dirs=[str(docs)])
        self.assertNotIn("collector_covers_home", self._codes(ok))
        for bad in (self.root, self.b_home, self.a_home / "roster"):
            report = doctor.diagnose(self.a_home, launchd=lambda label: None, collector_dirs=[str(bad)])
            self.assertIn("collector_covers_home", self._codes(report), bad)
        plist = self.root / "collector.plist"
        plist.write_bytes(plistlib.dumps({"ProgramArguments": ["python3", "-m", "fieldstone", "watchfeed",
                                                               "--source-dir", str(docs),
                                                               "--source-dir", str(self.root)]}))
        report = doctor.diagnose(self.a_home, launchd=lambda label: None, collector_plists=[str(plist)])
        self.assertIn("collector_covers_home", self._codes(report))
        self.assertEqual(len(report["collector"]["source_dirs"]), 2)


if __name__ == "__main__":
    unittest.main()
