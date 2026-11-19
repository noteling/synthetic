"""Issue #2 (noteling/synthetic#2): retracting a retract, retire or person entry in the company log.

One rule: an entry is in effect iff the company fold keeps it active. ``authority.covering`` (reads the fold) and
``authority.recheck`` (fenced writes) must agree, and only the owner may undo a retraction or a retirement or remove
a profile; a retract that changes a role follows A3/A4 like a person entry does.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from synthetics import authority
from synthetics.projects import ProjectError
from synthetics.roster import CompanyStore

SRC = {"source": "s", "certainty": "stated"}


def person(pid, **kw):
    return {"kind": "person", "body": dict({"id": pid, "kind": "employee", "name": pid.title()}, **SRC, **kw)}


def retract(target, **kw):
    return {"kind": "retract", "body": dict({"target": target, "reason": "r"}, **SRC, **kw)}


class Issue2Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cs = CompanyStore.at_root(self.root)
        self.op([person("codex"), person("mallory"), person("rowan")])
        self.cs.grant("mallory", "write", by="op")
        self.d = self.op([{"kind": "delegation", "body": dict(
            {"grantee": "codex", "actions": ["project.redact"], "scope": {"project.redact": {"projects": ["x"]}},
             "sources": [{"event": "e", "excerpt": "x", "auth": {"channel": "t"}}]}, **SRC)}])["ids"][0]
        self.r1 = self.op([retract(self.d)])["ids"][0]

    def tearDown(self):
        self.cs.close()
        self.tmp.cleanup()

    def op(self, batch):
        return self.cs.append(batch, author="operator:op", operator=True)

    def session(self, batch, author="mallory"):
        return self.cs.append(batch, author=author)

    def refused(self, code, batch, author="mallory"):
        head = self.cs.head_seq()
        with self.assertRaises(ProjectError) as cm:
            self.session(batch, author=author)
        self.assertEqual(cm.exception.code, code)
        self.assertEqual(self.cs.head_seq(), head)  # nothing written

    def covering(self):
        return authority.covering(self.cs.state(), "codex", "project.redact", lambda sc: True)

    def recheck(self):
        """authority.recheck against this company DB for a running actor whose chain is [D]."""
        actor = self.root / "actor.sqlite3"
        with sqlite3.connect(actor) as a:
            a.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, data TEXT)")
            a.execute("INSERT OR REPLACE INTO runs VALUES ('run-1', ?)", (json.dumps({"status": "running"}),))
        a.close()
        w = {"store_path": str(actor), "company_path": str(self.cs.path), "run": "run-1", "chain": [self.d]}
        db = sqlite3.connect(self.cs.path)
        try:
            authority.attach(db, w, company_is_main=True)
            try:
                authority.recheck(db, w, company_is_main=True)
                return True
            except authority.AuthorityError as error:
                self.assertEqual(error.code, "delegation_retracted")
                return False
        finally:
            db.close()

    def test_session_cannot_retract_the_owners_retract(self):
        # Behaviour: a company-write session may not undo the owner's revocation; D stays revoked everywhere.
        self.refused("owner_only", [retract(self.r1)])
        self.assertNotIn(self.d, self.cs.state()["delegations"])
        self.assertIsNone(self.covering())
        self.assertFalse(self.recheck())

    def test_fold_and_recheck_agree_on_retract_chains(self):
        # Revoked: both refuse.
        self.assertIsNone(self.covering())
        self.assertFalse(self.recheck())
        # The owner un-retracts: both accept (base: recheck still refused from the raw retract row).
        self.op([retract(self.r1)])
        self.assertIsNotNone(self.covering())
        self.assertTrue(self.recheck())

    def test_session_cannot_unretire(self):
        ret = self.op([{"kind": "retire", "body": dict({"id": "rowan", "reason": "left"}, **SRC)}])["ids"][0]
        self.refused("owner_only", [retract(ret)])
        self.assertIsNotNone(self.cs.person("rowan")["retired"])
        self.op([retract(ret)])  # the owner may
        self.assertIsNone(self.cs.person("rowan")["retired"])

    def test_session_retract_of_person_follows_role_rules(self):
        own = self.op([person("mallory", role="Restricted")])["ids"][0]
        other = self.op([person("rowan", role="Noteling maintainer")])["ids"][0]
        rename = self.op([person("rowan", name="Rowan R")])["ids"][0]
        # A3: nobody changes their own role, not even by retracting the entry that set it.
        self.refused("own_role", [retract(own)])
        # A4: changing someone else's role by retract needs an authenticated source.
        self.refused("unauthenticated_role", [retract(other)])
        # A profile's creating entry is owner-only to remove.
        creation = self.cs.state()["people"]["codex"]["history"][0]["entry"]
        self.refused("owner_only", [retract(creation)])
        # Unchanged: a retract that changes no role (a rename) is allowed; an authenticated role retract is too.
        self.session([retract(rename)])
        self.assertEqual(self.cs.person("rowan")["name"], "Rowan")
        self.session([retract(other, auth={"channel": "fieldpost"})])
        self.assertIsNone(self.cs.person("rowan")["role"])
        self.assertEqual(self.cs.person("mallory")["role"], "Restricted")


if __name__ == "__main__":
    unittest.main()
