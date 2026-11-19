"""Issue #2 follow-up i2b (Codex ruling 00:44Z 11-19): only the owner writes a `retire` entry. A session holding the
company write grant keeps person/alias/unalias."""
import tempfile
import unittest
from pathlib import Path

from synthetics.projects import ProjectError
from synthetics.roster import CompanyStore

SRC = {"source": "s", "certainty": "stated"}


def person(pid, **kw):
    return {"kind": "person", "body": dict({"id": pid, "kind": "employee", "name": pid.title()}, **SRC, **kw)}


def retire(pid):
    return {"kind": "retire", "body": dict({"id": pid, "reason": "left"}, **SRC)}


class RetireOwnerOnlyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cs = CompanyStore.at_root(Path(self.tmp.name))
        self.cs.append([person("codex"), person("mallory"), person("rowan")], author="operator:op", operator=True)
        self.cs.grant("mallory", "write", by="op")

    def tearDown(self):
        self.cs.close()
        self.tmp.cleanup()

    def test_session_retire_is_owner_only_and_writes_nothing(self):
        for target in ("rowan", "mallory", "nobody"):
            with self.subTest(target=target):
                head = self.cs.head_seq()
                with self.assertRaises(ProjectError) as cm:
                    self.cs.append([retire(target)], author="mallory")
                self.assertEqual(cm.exception.code, "owner_only")
                self.assertEqual(self.cs.head_seq(), head)
        self.assertIsNone(self.cs.state()["people"]["rowan"]["retired"])
        # A batch that carries a retire is refused whole, including its harmless entries.
        head = self.cs.head_seq()
        with self.assertRaises(ProjectError):
            self.cs.append([{"kind": "alias", "body": dict({"id": "rowan", "alias": "ro"}, **SRC)}, retire("rowan")],
                           author="mallory")
        self.assertEqual(self.cs.head_seq(), head)

    def test_owner_retires_and_session_keeps_other_writes(self):
        self.cs.append([retire("rowan")], author="operator:op", operator=True)
        self.assertEqual(self.cs.state()["people"]["rowan"]["retired"]["reason"], "left")
        self.cs.append([{"kind": "alias", "body": dict({"id": "codex", "alias": "cx"}, **SRC)}], author="mallory")
        self.cs.append([person("codex", description="manager")], author="mallory")
        self.cs.append([{"kind": "unalias", "body": dict({"alias": "cx"}, **SRC)}], author="mallory")


if __name__ == "__main__":
    unittest.main()
