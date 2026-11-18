"""M26 P1 step 5: roster MCP tools and canonical subjects. Design tests 2, 5; C2; C3 via profile_put."""

import tempfile
import unittest
from pathlib import Path

from synthetics.mcp import METHODS, MUTATIONS, Server
from synthetics.projects import ProjectError
from synthetics.roster import CompanyStore
from synthetics.store import Store

CODEX = "employee-a22b6a2a8f524cd6b6800b0a620dbe06"
ROWAN = "employee-28ed6d90e0fe4b42b64bae0e41e82cba"


def person(pid, name, role):
    return {"kind": "person", "body": {"id": pid, "kind": "employee", "name": name, "role": role,
                                       "source": "operator statement", "certainty": "stated"}}


class RosterToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "manager"
        self.store = Store(self.home)
        self.me = self.store.initialize("Codex", "manager")["id"]
        with CompanyStore.at_root(self.root) as co:
            co.append([person(ROWAN, "Rowan", "Noteling maintainer"),
                       person(self.me, "Codex", "Synthetics manager"),
                       {"kind": "alias", "body": {"alias": "rowan", "id": ROWAN, "source": "operator statement",
                                                  "certainty": "stated"}}], author="operator", operator=True)
        self.run = self.store.run_begin()["id"]
        self.bound = Store(self.home, run_id=self.run)
        self.server = Server(self.bound, run_id=self.run)

    def tearDown(self):
        self.bound.close()
        self.store.close()
        self.tmp.cleanup()

    def grant(self):
        with CompanyStore.at_root(self.root) as co:
            co.grant(self.me, "write", by="operator")

    def test_tools_registered(self):
        for name in ("roster_get", "view_put", "self_note", "profile_put"):
            self.assertIn(name, METHODS)
        self.assertEqual({"view_put", "self_note", "profile_put"} & MUTATIONS, {"view_put", "self_note", "profile_put"})
        self.assertNotIn("auth", METHODS["profile_put"]["inputSchema"]["properties"])  # C3: never an argument

    def test_roster_get_resolves_alias_and_shows_only_own_view(self):
        self.server.call("view_put", {"person": "rowan", "key": "v1", "claims": [
            {"cid": "c1", "text": "prefers linked IDs", "kind": "working", "source": "event-1", "certainty": "observed"}]})
        out = self.server.call("roster_get", {"person": "Rowan"})
        # "Rowan" is both the name and (folded) the alias "rowan": alias is reported, both are listed.
        self.assertEqual((out["id"], out["via"], out["matched_as"]), (ROWAN, "alias", ["name", "alias"]))
        self.assertIn("Noteling maintainer", out["profile_text"])
        self.assertTrue(out["view"].startswith("# PRIVATE VIEW — not official (private to Codex)"))
        with self.assertRaises(ProjectError) as cm:
            self.server.call("roster_get", {"person": "nobody"})
        self.assertEqual(cm.exception.code, "unknown_person")

    def test_02_profile_put_needs_grant_and_records_event_auth(self):
        with self.assertRaises(ProjectError) as cm:
            self.server.call("profile_put", {"id": ROWAN, "description": "x", "source": "docs/team.md", "key": "p0"})
        self.assertEqual(cm.exception.code, "no_grant")
        self.grant()
        # Unauthenticated source: description edits only (A4); recorded auth is null (C3).
        ev = self.store.feed("david", "Rowan also reviews docs", source="desktop")
        self.server.call("profile_put", {"id": ROWAN, "description": "Also reviews docs", "aliases": ["R"],
                                         "source": ev["id"], "key": "p1"})
        with self.assertRaises(ProjectError) as cm:
            self.server.call("profile_put", {"id": ROWAN, "role": "CTO", "source": ev["id"], "key": "p2"})
        self.assertEqual(cm.exception.code, "unauthenticated_role")
        # Authenticated source: the role change is allowed and its auth is copied from the event.
        key = b"f" * 64
        self.store.channel_register("fieldpost", Store.credential_hash(key), ["fieldpost"], "test", routes=["david"])
        auth_ev = self.store.feed("david", "Rowan now leads Noteling", source="fieldpost", credential=key)
        self.server.call("profile_put", {"id": ROWAN, "role": "Noteling lead", "source": auth_ev["id"], "key": "p3"})
        with CompanyStore.at_root(self.root) as co:
            p = co.person("R")
            entries = co.entries()
        self.assertEqual((p["id"], p["role"]), (ROWAN, "Noteling lead"))
        mine = [e for e in entries if e["author"] == self.me]
        self.assertTrue(mine and all(e["run"] == self.run for e in mine))
        role_entry = [e for e in mine if e["body"].get("role") == "Noteling lead"][0]
        self.assertEqual(role_entry["body"]["auth"]["channel"], "fieldpost")
        self.assertIsNone([e for e in mine if e["body"].get("description")][0]["body"]["auth"])
        # A3: nobody sets its own role, grant or not.
        with self.assertRaises(ProjectError) as cm:
            self.server.call("profile_put", {"id": self.me, "role": "Owner", "source": auth_ev["id"], "key": "p4"})
        self.assertEqual(cm.exception.code, "own_role")
        self.assertTrue((self.root / "company" / "people").is_dir())  # writes re-render

    def test_b31_alias_unalias_retire_appear_in_roster_get_history(self):
        self.grant()
        ev = self.store.feed("david", "call me Dana Example", source="desktop")
        self.server.call("profile_put", {"id": ROWAN, "aliases": ["Dana Example"], "source": ev["id"], "key": "a1"})
        hist = self.server.call("roster_get", {"person": "Dana Example"})["profile"]["history"]
        added = [h for h in hist if h.get("change") == "alias"]
        self.assertEqual([h["alias"] for h in added], ["rowan", "Dana Example"])
        mine = added[-1]
        self.assertEqual((mine["by"], mine["source"], mine["auth"]), (self.me, ev["id"], None))
        self.assertRegex(mine["entry"], r"^co-[0-9a-f]{32}$")
        self.assertIsInstance(mine["seq"], int)
        with CompanyStore.at_root(self.root) as co:
            co.append([{"kind": "unalias", "body": {"alias": "dana example", "source": "operator statement",
                                                    "certainty": "stated"}},
                       {"kind": "retire", "body": {"id": ROWAN, "reason": "left", "source": "operator statement",
                                                   "certainty": "stated"}}], author="operator", operator=True)
        out = self.server.call("roster_get", {"person": ROWAN})
        changes = [(h.get("change"), h.get("alias") or h.get("reason")) for h in out["profile"]["history"]]
        self.assertEqual(changes[-2:], [("unalias", "Dana Example"), ("retire", "left")])
        self.assertNotIn("Dana Example", out["profile"]["aliases"])

    def test_profile_put_fenced_on_run(self):
        self.grant()
        self.store.run_finish(self.run, "done")
        with self.assertRaises(Exception):
            self.server.call("profile_put", {"id": ROWAN, "description": "late", "source": "file", "key": "late"})

    def test_05_C2_memory_subject_canonical_and_alias_search(self):
        m = self.bound.memory_put("rowan", "routes design questions", "via Codex", source="event-1")
        self.assertEqual(m["subject"], ROWAN)
        audit = self.store.db.execute("SELECT data FROM audit WHERE action='memory.subject_resolved'").fetchall()
        self.assertEqual(len(audit), 1)
        self.assertIn('"given": "rowan"', audit[0][0])
        for name in ("Rowan", "rowan", ROWAN):
            self.assertEqual([x["id"] for x in self.store.memory_search(subject=name)], [m["id"]])
        # replaces works across alias spellings of the same person.
        m2 = self.bound.memory_put("Rowan", "routes design questions", "via Codex, slowly", source="event-2",
                                   replaces=m["id"])
        self.assertEqual(m2["subject"], ROWAN)

    def test_05_old_alias_rows_found(self):
        self.store.entity_put("rowan", "Rowan", "concept")  # pre-roster entity row named by the alias
        # Simulate a pre-M26 memory filed under the alias string.
        with self.store._tx():
            self.store._save("memories", {"id": "memory-" + "a" * 32, "subject": "rowan", "predicate": "p",
                                          "value": "v", "kind": "fact", "source": "s", "replaces": None,
                                          "created_at": "2026-11-17T00:00:00Z", "superseded_by": None}, "memory.created")
        self.assertEqual([x["id"] for x in self.store.memory_search(subject=ROWAN)], ["memory-" + "a" * 32])

    def test_entity_put_person_redirects_to_company(self):
        with self.assertRaises(ProjectError) as cm:
            self.store.entity_put("rowan", "Rowan", "person")
        self.assertEqual((cm.exception.code, cm.exception.detail["id"]), ("use_company_person", ROWAN))
        self.store.entity_put("stranger", "Stranger", "person")  # unresolved: accepted, flagged unofficial
        ent = [e for e in self.store.context()["entities"] if e["id"] == "stranger"]
        self.assertTrue(ent and ent[0]["unofficial"])

    def test_self_note_via_mcp(self):
        out = self.server.call("self_note", {"section": "how-i-work", "text": "freeze before handoff",
                                             "source": "memory-x", "key": "s1"})
        self.assertEqual(out["run"], self.run)
        self.assertIn("freeze before handoff", self.store.context()["self"])


if __name__ == "__main__":
    unittest.main()
