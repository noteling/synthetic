"""M21 step 6 CLI: test 19 session rules (E2/B2, N2), redaction (D3.7), export/import, grants."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from synthetics.cli import main
from synthetics.projects import ProjectStore
from synthetics.store import SESSION_ENV, Store

T = "codex:00000000-0000-4000-8000-000000000001"


def env_without_session():
    env = dict(os.environ)
    env.pop(SESSION_ENV, None)
    return mock.patch.dict(os.environ, env, clear=True)


class ProjectCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "manager"
        with Store(self.home) as store:
            store.initialize("Codex", "manager")

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, session=None):
        out, err = io.StringIO(), io.StringIO()
        with env_without_session():
            if session:
                os.environ[SESSION_ENV] = session
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(["--home", str(self.home)] + list(args))
        return code, out.getvalue(), err.getvalue()

    def ok(self, *args, session=None):
        code, out, err = self.cli(*args, session=session)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def ps(self, project="synthetics"):
        return ProjectStore(self.root / "projects" / project / "memory.sqlite3", project)

    def bind(self, session=None):
        return self.ok("project", "bind", "synthetics", "--thread", T, "--source-prefix", "codex-desktop:00000000",
                       "--source-prefix", "desktop-secretary-", "--by", "codex-manager", "--reason", "M21 deploy",
                       session=session)

    def feed(self, body, source="codex-desktop:00000000-0000", sender="david", ext=None):
        return self.ok("feed", body, "--from", sender, "--source", source, "--thread", T,
                       *(["--external-id", ext] if ext else []))

    def source_ids(self):
        with self.ps() as ps:
            return [e["id"] for e in ps.entries() if e["kind"] == "source"]

    def test_19_bind_in_session_is_audited_redact_refused(self):
        b = self.bind(session="run-abc")
        self.assertEqual(b["source_prefixes"], ["codex-desktop:00000000", "desktop-secretary-"])
        with Store(self.home) as store:
            rows = [a for a in store.audit_list(limit=50) if a.get("action") == "binding.created"]
        self.assertEqual(len(rows), 1)
        self.assertIn("codex-manager", json.dumps(rows[0]))
        listed = self.ok("project", "bindings", session="run-abc")
        self.assertEqual([x["id"] for x in listed], [b["id"]])
        self.feed("hello")
        sid = self.source_ids()[0]
        code, out, err = self.cli("project", "redact", "synthetics", sid, "--reason", "x", "--by", "op",
                                  session="run-abc")
        self.assertNotEqual(code, 0)
        self.assertRegex(err, r"refused inside an employee session|actor_unverified|owner_only|not_delegated|run_inactive")  # M28
        for cmd in (["grant", "synthetics", "employee-x", "--mode", "read", "--by", "op"],
                    ["import", "synthetics", str(self.root / "none.jsonl")]):
            code, _, err = self.cli("project", *cmd, session="run-abc")
            self.assertNotEqual(code, 0)
            self.assertRegex(err, r"refused inside an employee session|actor_unverified|owner_only|not_delegated|run_inactive")  # M28
        with self.ps() as ps:
            self.assertEqual(ps.source_bytes(sid), b"hello")

    def test_bind_has_no_backdating_option(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(err):
            main(["--home", str(self.home), "project", "bind", "synthetics", "--thread", T, "--by", "a",
                  "--reason", "r", "--from-seq", "0"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("--from-seq", err.getvalue())

    def test_unbind_and_all(self):
        b = self.bind()
        ended = self.ok("project", "unbind", b["id"], "--by", "codex-manager", "--reason", "done", session="run-1")
        self.assertEqual(ended["ended"]["by"], "codex-manager")
        self.assertEqual(self.ok("project", "bindings"), [])
        self.assertEqual(len(self.ok("project", "bindings", "--all")), 1)

    def test_redact_operator_removes_bytes_and_export(self):
        self.bind()
        self.feed("secret-token-123")
        sid = self.source_ids()[0]
        r = self.ok("project", "redact", "synthetics", sid, "--reason", "pasted secret", "--by", "operator")
        self.assertEqual(r["size"], len(b"secret-token-123"))
        with self.ps() as ps:
            self.assertEqual(ps.source_bytes(sid), b"")
            self.assertTrue(ps.state()["sources"][sid]["redacted"])
            text = ps.export_path.read_text()
        self.assertNotIn("secret-token-123", text)
        self.assertIn('"kind":"redact"', text.replace(" ", ""))

    def test_export_import_grant(self):
        self.bind()
        self.feed("one", ext="e1")
        self.feed("two", ext="e2")
        exp = self.ok("project", "export", "synthetics", "--rebuild")
        with self.ps() as ps:
            head, state, path = ps.head_seq(), ps.state(), ps.export_path
        self.assertEqual(exp["export_through"], head)
        copy = self.root / "copy.jsonl"
        copy.write_bytes(path.read_bytes())
        res = self.ok("project", "import", "restored", str(copy))
        self.assertEqual(res["head_seq"], head)
        with self.ps("restored") as ps:
            self.assertEqual(ps.state(), state)
        code, _, err = self.cli("project", "import", "restored", str(copy))
        self.assertNotEqual(code, 0)
        self.assertIn("empty", err)
        g = self.ok("project", "grant", "synthetics", "employee-v", "--mode", "write", "--by", "op")
        self.assertEqual(g["mode"], "write")
        self.assertEqual([x["employee"] for x in self.ok("project", "grants", "synthetics")], ["employee-v"])

    def test_b31r3_grant_accepts_every_grant_mode(self):
        from synthetics.projects import GRANT_MODES
        self.bind()
        for i, mode in enumerate(GRANT_MODES):
            g = self.ok("project", "grant", "synthetics", f"employee-{i}", "--mode", mode, "--by", "op")
            self.assertEqual(g["mode"], mode)
        self.assertIn("post", GRANT_MODES)
        modes = {x["employee"]: x["mode"] for x in self.ok("project", "grants", "synthetics")}
        self.assertEqual(modes["employee-1"], "post")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            self.cli("project", "grant", "synthetics", "employee-z", "--mode", "admin", "--by", "op")
        self.assertNotEqual(cm.exception.code, 0)

    def test_capture_drain_runs(self):
        self.bind()
        res = self.ok("project", "capture-drain", session="run-1")
        self.assertIsInstance(res, dict)

    def test_bad_project_name(self):
        code, _, err = self.cli("project", "export", "../etc")
        self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
