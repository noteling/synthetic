"""Backlog 51: read-only `project` commands on an unknown project refuse and create nothing.

Live case (Codex, 11-18): `synthetics project grants noteling` left an empty projects/noteling/memory.sqlite3.
Only `project grant` and `project import` create a project from the CLI.
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from synthetics.cli import main
from synthetics.store import SESSION_ENV, Store


class UnknownProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "manager"
        with Store(self.home) as store:
            store.initialize("Codex", "manager")

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args):
        env = {k: v for k, v in os.environ.items() if k != SESSION_ENV}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(["--home", str(self.home)] + list(args))
        return code, out.getvalue(), err.getvalue()

    def project_dir(self, name):
        return self.root / "projects" / name

    def test_read_commands_refuse_unknown_project_and_create_nothing(self):
        for args in (["grants", "ghost"], ["export", "ghost"], ["view", "ghost"],
                     ["redact", "ghost", "pm-x", "--reason", "r", "--by", "op"]):
            with self.subTest(args=args[0]):
                code, out, err = self.cli("project", *args)
                self.assertEqual(code, 1, out)
                self.assertIn("No project ghost", err)
                self.assertFalse(self.project_dir("ghost").exists(), "%s created the project dir" % args[0])

    def test_grant_still_creates_and_grants_then_reads_it(self):
        code, out, err = self.cli("project", "grant", "newp", "employee-v", "--mode", "read", "--by", "op")
        self.assertEqual(code, 0, err)
        self.assertTrue((self.project_dir("newp") / "memory.sqlite3").exists())
        code, out, err = self.cli("project", "grants", "newp")
        self.assertEqual(code, 0, err)
        self.assertEqual([g["employee"] for g in json.loads(out)], ["employee-v"])

    def test_import_still_creates(self):
        src = self.root / "empty.jsonl"
        src.write_text("", encoding="utf-8")
        code, out, err = self.cli("project", "import", "imp", str(src))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["head_seq"], 0)
        self.assertTrue((self.project_dir("imp") / "memory.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
